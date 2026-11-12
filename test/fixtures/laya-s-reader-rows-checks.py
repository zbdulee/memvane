#!/usr/bin/env python3
"""A journal-selected schema3 endpoint keeps recall and its parity floor after the row-format conversion."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('schema3_journal_fixture', HERE / 'laya-journal-schema3-checks.py')
journal = importlib.util.module_from_spec(spec)
spec.loader.exec_module(journal)


ROOT = HERE.parent.parent
# Released 0.44.18, 0.44.19 and 0.44.20, archived from their exact commits (CI fetches them).
CONSUMER_CORES = ('6d26d8afeebf2931f515bb12849377b458e2872d', '5a90c2d4410f26b416140c4131b160fd38baa511',
                  # 0.44.20, the newest main release before the row format.
                  '9553e3aff5d5a601d8240ef67b4256de30a32f98')
CONSUMER_CHILD = r'''
import json, sys, time
from pathlib import Path
core, journal_path, project, plugin = sys.argv[1:5]
sys.path.insert(0, core)
import laya_journal_reader as R, laya_writer_open_plan as W, wiki_compile_retrieval as C
assert R.__file__.startswith(core), R.__file__
path, out = Path(journal_path), {}
def attempt(name, fn):
    try:
        fn(); out[name] = 'accepted'
    except Exception as exc:
        out[name] = str(getattr(exc, 'reason', None) or getattr(exc, 'code', None) or exc)
with R._readonly(path, path.parent) as db:
    record = dict(R._selection(db)[1])
def pin():
    with R._schema3_pin(dict(record), deadline=time.monotonic() + 20) as (store, handle, pinned):
        store.search(pinned, 'heron', 5)
def head():
    with R._schema3_pin(dict(record), deadline=time.monotonic() + 20, pin_view=False):
        pass
attempt('recall_pin', pin)
attempt('head_only', head)
attempt('writer_plan', lambda: W._snapshot(Path(project), Path(plugin)))
# Write-time dedup retrieval's vector lane pins the selected head the same way; a
# head other than the named generation answers no cosines.
attempt('dedup_cosines', lambda: C._schema3_cosines((None, {}), dict(record, vector_profile='0' * 64,
                                                                       vector_recipe='0' * 64),
                                                    {}, 'heron', 'g-' + '0' * 32 + '-b' + '0' * 64))
print(json.dumps(out, sort_keys=True))
'''


def file_state(*paths):
    state = {}
    for path in paths:
        for name in (path, Path(str(path) + '-journal'), Path(str(path) + '-wal')):
            if name.exists():
                info = name.stat()
                state[str(name)] = (info.st_ino, info.st_size, hashlib.sha256(name.read_bytes()).hexdigest())
    return state


class ReaderRowsChecks(journal.Schema3EndpointChecks):
    def answer(self):
        with self.resolved() as endpoint:
            result = self.query(endpoint)
        return ([(row['path'], row['score'], row['hash']) for row in result.rows],
                dict(result.source_locations), result.semantic_status)

    def convert(self):
        with self.writer() as view:
            view.convert_to_row_format(parity=dict(gen_id=self.gens[0], receipt_digest=self.parity()['digest']))
        store = self.views.ViewStore.open(self.store_path, repository=self.repository,
                                          scope_id=self.case.route['scopeId'])
        self.addCleanup(store.close)
        return store

    def test_converted_selected_store_answers_the_same_recall_and_parity_floor(self):
        self.switched()
        before = self.answer()
        self.assertTrue(before[0], before)
        with self.writer() as view:
            ordinal = view._db.execute('SELECT ordinal FROM generations WHERE gen_id=?',
                                       (self.gens[0],)).fetchone()[0]
            view_id = view.capture_view(self.worktree_id).view_id
        store = self.convert()
        self.assertTrue(store._rows)
        self.assertEqual(self.answer(), before)
        self.assertEqual(self.reader._generation_ordinal(store, self.gens[0], view_id), ordinal)
        anchor = store._db.execute('SELECT gen_id,ordinal,incarnation FROM s_parity_anchor').fetchall()
        self.assertEqual([tuple(row) for row in anchor], [(self.gens[0], ordinal, store._incarnation)])

    def test_recall_repins_once_after_the_view_head_moved(self):
        self.switched()
        before = self.answer()
        self.convert()
        import laya_view_rows as rows
        import laya_blob_values as v
        real, calls = rows.RowsMixin._rows_search, []

        def moved_once(self_, *args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                v.fail('view_head_changed')
            return real(self_, *args, **kwargs)
        with mock.patch.object(rows.RowsMixin, '_rows_search', moved_once):
            self.assertEqual(self.answer(), before)
        self.assertGreater(len(calls), 1)

        def always(self_, *args, **kwargs):
            v.fail('view_head_changed')
        with mock.patch.object(rows.RowsMixin, '_rows_search', always):
            with self.assertRaises(Exception) as caught:
                self.answer()
        self.assertIn('view_head_changed', str(getattr(caught.exception, 'reason', caught.exception)))

    def consumers(self, core):
        result = subprocess.run([sys.executable, '-B', '-c', CONSUMER_CHILD, str(core), str(self.case.journal),
                                 str(self.case.project), str(self.case.plugin)], capture_output=True, text=True,
                                timeout=120, env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout.strip().splitlines()[-1])

    def test_released_consumers_fail_closed_on_a_converted_selected_store(self):
        self.switched()
        self.convert()
        current = self.consumers(ROOT / 'core')
        self.assertEqual(current['recall_pin'], 'accepted', current)
        self.assertEqual(current['head_only'], 'accepted', current)
        self.assertEqual(current['dedup_cosines'], 'accepted', current)
        before = file_state(Path(self.case.journal), self.store_path)
        observed = {}
        for commit in CONSUMER_CORES:
            exported = Path(tempfile.mkdtemp(prefix='s-consumer-core-', dir=self.case.home))
            archive = subprocess.run(['git', '-C', str(ROOT), 'archive', commit, 'core'],
                                     capture_output=True, timeout=60)
            self.assertEqual(archive.returncode, 0, archive.stderr.decode(errors='replace'))
            subprocess.run(['tar', '-x', '-C', str(exported)], input=archive.stdout, check=True, timeout=60)
            observed[commit] = self.consumers(exported / 'core')
        print(json.dumps(dict(current=current, released=observed, python=sys.version.split()[0]), sort_keys=True))
        for commit, out in observed.items():
            with self.subTest(commit=commit):
                self.assertEqual(out['recall_pin'], 'candidate_format_mismatch', out)
                self.assertEqual(out['head_only'], 'candidate_format_mismatch', out)
                self.assertEqual(out['writer_plan'], 'writer_plan_store_changed', out)
                self.assertEqual(out['dedup_cosines'], 'candidate_format_mismatch', out)
        self.assertEqual(file_state(Path(self.case.journal), self.store_path), before)

    def test_conversion_refuses_a_parity_generation_outside_the_store(self):
        self.switched()
        with self.writer() as view:
            with self.assertRaises(Exception) as caught:
                view.convert_to_row_format(parity=dict(gen_id='g-' + 'a' * 32 + '-b' + 'b' * 64,
                                                       receipt_digest='d' * 64))
        self.assertEqual(getattr(caught.exception, 'reason', None), 'row_conversion_parity_invalid')


def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite()
    for name in loader.getTestCaseNames(ReaderRowsChecks):
        if name in ReaderRowsChecks.__dict__:
            suite.addTest(ReaderRowsChecks(name))
    return suite


if __name__ == '__main__':
    unittest.main()

"""Pure wiki source-intent planning and the enqueue path wired through it.

Synthetic values and a disposable project under the disposable HOME the runner
provides. No QMD, daemon, model, extractor or network: the source queue file is
the only product effect, and it lives inside that project.
"""
import ast
import copy
import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path.cwd() / 'core'))
import compile_paths as cp
import config as qmd_config
import file_change_plan
import wiki_compile_enqueue as E
import wiki_source_intent as I

FROZEN_TS = '2026-10-17T00:00:00Z'


def legacy_enqueue(project_root, config, changed_files, *, session_start=False):
    """The pre-boundary `enqueue_changed_sources`, kept verbatim as the oracle."""
    accepted = ('session_start_source',) if session_start else ('post_sync_source', 'post_tool_source')
    compile_cfg = E.compile_gate(config, accepted)
    if compile_cfg is None:
        return {"queued": 0, "deferred": 0, "deferredFiles": [], "reason": "compile_disabled"}
    queue_path = E._safe_queue_path(project_root, cp.rel(cp.SOURCE_QUEUE))
    if queue_path is None:
        return {"queued": 0, "deferred": 0, "deferredFiles": [], "reason": "queue_path_rejected"}
    trigger = 'session_start_source' if session_start else 'post_sync_source'
    engine = E.compile_engine(compile_cfg)
    candidates = []
    for name in sorted(changed_files):
        entry = changed_files[name]
        root = Path(entry["root"])
        for rel in sorted(set(entry["created"]) | set(entry["updated"])):
            record = E._source_record(str(root / rel), project_root, project_root,
                                      config, engine, trigger=trigger)
            if record is not None:
                candidates.append((name, rel, record))
    if not candidates:
        return {"queued": 0, "deferred": 0, "deferredFiles": [], "reason": "no_sources"}
    cap = E.compile_max_files()
    picked = candidates[:cap]
    deferred = [(name, rel) for name, rel, _ in candidates[cap:]]
    E._append_jsonl(queue_path, [record for _, _, record in picked])
    return {"queued": len(picked), "deferred": len(deferred), "deferredFiles": deferred,
            "reason": "capped" if deferred else "queued"}


class PurePlanning(unittest.TestCase):
    def test_module_imports_only_the_standard_library_nothing_at_all(self):
        tree = ast.parse(Path('core/wiki_source_intent.py').read_text(encoding='utf-8'))
        imports = [n for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))]
        self.assertEqual(imports, [], 'the planning boundary must stay free of effects')

    def test_trigger_policy_keeps_consent_per_caller(self):
        self.assertEqual(I.trigger_policy(False),
                         (('post_sync_source', 'post_tool_source'), 'post_sync_source'))
        # SessionStart accepts only its own grant; post_tool consent is not widened.
        self.assertEqual(I.trigger_policy(True), (('session_start_source',), 'session_start_source'))
        for label in ('post_sync_source', 'post_tool_source', 'session_start_source'):
            self.assertIn(label, qmd_config.COMPILE_TRIGGERS)

    def test_enqueue_module_aliases_are_the_same_labels(self):
        self.assertEqual(E.POST_TOOL_TRIGGER, 'post_tool_source')
        self.assertEqual(E.COMPILE_TRIGGER, 'post_sync_source')
        self.assertEqual(E.COMPILE_ACCEPTED_TRIGGERS, ('post_sync_source', 'post_tool_source'))

    def test_changed_paths_are_sorted_deduplicated_and_skip_deletes(self):
        changed = {
            'zeta': {'root': '/w/z', 'created': ['b.md'], 'updated': ['a.md', 'b.md'], 'deleted': ['c.md']},
            'alpha': {'root': '/w/a', 'created': [], 'updated': [], 'deleted': ['gone.md']},
            'mid': {'root': '/w/m', 'created': ['y.md', 'x.md'], 'updated': [], 'deleted': []},
        }
        before = copy.deepcopy(changed)
        self.assertEqual(list(I.changed_source_paths(changed)), [
            ('mid', '/w/m', 'x.md'), ('mid', '/w/m', 'y.md'),
            ('zeta', '/w/z', 'a.md'), ('zeta', '/w/z', 'b.md')])
        self.assertEqual(changed, before)

    def test_plan_splits_by_cap_in_candidate_order(self):
        rows = [('c', f'{i}.md', {'n': i}) for i in range(3)]
        cases = {
            50: ([0, 1, 2], [], 'queued'),
            3: ([0, 1, 2], [], 'queued'),
            2: ([0, 1], [('c', '2.md')], 'capped'),
            0: ([], [('c', '0.md'), ('c', '1.md'), ('c', '2.md')], 'capped'),
        }
        for cap, (picked, deferred, reason) in cases.items():
            plan = I.plan_enqueue(iter(rows), cap)
            self.assertEqual([r['n'] for r in plan['records']], picked, cap)
            self.assertEqual(plan['result'], {'queued': len(picked), 'deferred': len(deferred),
                                              'deferredFiles': deferred, 'reason': reason}, cap)
            # Records are passed through, not copied or rewritten.
            for record in plan['records']:
                self.assertIs(record, rows[record['n']][2])

    def test_empty_plan_and_result_shape(self):
        self.assertEqual(I.plan_enqueue([], 5),
                         {'records': [], 'result': {'queued': 0, 'deferred': 0,
                                                    'deferredFiles': [], 'reason': 'no_sources'}})
        first, second = I.enqueue_result('x'), I.enqueue_result('x')
        self.assertIsNot(first['deferredFiles'], second['deferredFiles'])

    def test_intent_identity_is_the_hook_duplicate_key(self):
        record = {'cwd': '/p', 'trigger': 't', 'engine': 'e', 'ts': 'now',
                  'source': {'kind': 'file', 'path': 'docs/a.md', 'collection': 'docs'}}
        self.assertEqual(I.intent_identity(record), ('/p', 'docs/a.md', 'docs'))


class EnqueueThroughSeam(unittest.TestCase):
    """New `enqueue_changed_sources` must equal the legacy oracle byte for byte."""

    def setUp(self):
        base = Path(tempfile.mkdtemp(prefix='qmd-wiki-source-intent-',
                                     dir=os.environ['HOME'])).resolve()
        self.addCleanup(shutil.rmtree, base, True)
        self.project = base / 'project'
        for rel, text in {
            'docs/a.md': '# a\n', 'docs/b.md': '# b\n', 'docs/c.md': '# c\n',
            'docs/note.txt': 'not markdown\n', 'docs/.draft/h.md': '# hidden\n',
            'src/s.md': '# source role\n', 'wiki/w.md': '# wiki role\n',
        }.items():
            path = self.project / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding='utf-8')
        self.config = {
            'indexing': True,
            'collections': ['docs', 'src', 'wiki'],
            'collectionPaths': {'docs': 'docs', 'src': 'src', 'wiki': 'wiki'},
            'collectionRoles': {'docs': 'raw', 'src': 'source', 'wiki': 'wiki'},
            'compile': {'mode': 'auto-wiki',
                        'triggers': ['post_sync_source', 'session_start_source'],
                        'extractor': {'builtins': ['claude']}},
        }
        root = lambda name: str(self.project / name)
        self.changed = {
            'docs': {'root': root('docs'), 'created': ['c.md', 'note.txt', '.draft/h.md'],
                     'updated': ['b.md', 'a.md'], 'deleted': ['gone.md']},
            'src': {'root': root('src'), 'created': ['s.md'], 'updated': [], 'deleted': []},
            'wiki': {'root': root('wiki'), 'created': ['w.md'], 'updated': [], 'deleted': []},
        }
        patches = [mock.patch.object(E, '_utc_now', return_value=FROZEN_TS),
                   mock.patch.dict(os.environ, {'QMD_ENGINE': 'claude'})]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.queue = self.project / cp.rel(cp.SOURCE_QUEUE)

    def source_digest(self):
        files = sorted(p for p in self.project.rglob('*')
                       if p.is_file() and '.auto-context' not in p.parts)
        return {str(p.relative_to(self.project)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in files}

    def drain(self):
        data = self.queue.read_bytes() if self.queue.exists() else None
        if self.queue.exists():
            self.queue.unlink()
        return data

    def compare(self, config=None, *, session_start=False, cap=None):
        config = config or self.config
        env = {} if cap is None else {'QMD_SYNC_COMPILE_MAX': str(cap)}
        sources = self.source_digest()
        with mock.patch.dict(os.environ, env):
            legacy = legacy_enqueue(str(self.project), config, copy.deepcopy(self.changed),
                                    session_start=session_start)
            legacy_bytes = self.drain()
            changed = copy.deepcopy(self.changed)
            current = E.enqueue_changed_sources(str(self.project), config, changed,
                                                session_start=session_start)
            current_bytes = self.drain()
        self.assertEqual(current, legacy)
        self.assertEqual(current_bytes, legacy_bytes)
        self.assertEqual(changed, self.changed, 'the change set is not mutated')
        self.assertEqual(self.source_digest(), sources, 'source files are never written')
        return current, current_bytes

    def rows(self, data):
        return [json.loads(line) for line in data.decode('utf-8').splitlines()]

    def test_sync_queues_eligible_markdown_in_cap_order(self):
        result, data = self.compare()
        self.assertEqual(result, {'queued': 4, 'deferred': 0, 'deferredFiles': [], 'reason': 'queued'})
        rows = self.rows(data)
        self.assertEqual([(r['source']['collection'], r['source']['path']) for r in rows], [
            ('docs', 'docs/a.md'), ('docs', 'docs/b.md'), ('docs', 'docs/c.md'), ('src', 'src/s.md')])
        self.assertEqual({r['trigger'] for r in rows}, {'post_sync_source'})
        self.assertEqual({r['engine'] for r in rows}, {'claude'})
        self.assertEqual({r['ts'] for r in rows}, {FROZEN_TS})

    def test_session_start_records_its_own_trigger(self):
        result, data = self.compare(session_start=True)
        self.assertEqual(result['reason'], 'queued')
        self.assertEqual({r['trigger'] for r in self.rows(data)}, {'session_start_source'})

    def test_cap_defers_the_tail_and_cap_zero_writes_nothing(self):
        result, data = self.compare(cap=2)
        # Deferred files are collection-root relative: the snapshot rollback keys.
        self.assertEqual(result, {'queued': 2, 'deferred': 2, 'reason': 'capped',
                                  'deferredFiles': [('docs', 'c.md'), ('src', 's.md')]})
        self.assertEqual(len(self.rows(data)), 2)
        result, data = self.compare(cap=0)
        self.assertEqual((result['queued'], result['deferred'], result['reason']), (0, 4, 'capped'))
        self.assertIsNone(data, 'an all-deferred wake must not touch the queue file')

    def test_invalid_cap_env_falls_back_to_default(self):
        with mock.patch.dict(os.environ, {'QMD_SYNC_COMPILE_MAX': 'many'}):
            result, _ = self.compare()
        self.assertEqual(result['reason'], 'queued')

    def test_consent_gates_and_rejections_match(self):
        post_tool_only = copy.deepcopy(self.config)
        post_tool_only['compile']['triggers'] = ['post_tool_source']
        # Manual sync honours post_tool consent; SessionStart does not.
        self.assertEqual(self.compare(post_tool_only)[0]['reason'], 'queued')
        result, data = self.compare(post_tool_only, session_start=True)
        self.assertEqual((result['reason'], data), ('compile_disabled', None))
        off = copy.deepcopy(self.config)
        off['compile']['mode'] = 'off'
        self.assertEqual(self.compare(off)[0]['reason'], 'compile_disabled')
        not_indexing = dict(self.config, indexing=False)
        self.assertEqual(self.compare(not_indexing)[0]['reason'], 'compile_disabled')

    def test_no_sources_when_only_ineligible_paths_changed(self):
        self.changed = {'docs': {'root': str(self.project / 'docs'), 'created': ['note.txt'],
                                 'updated': ['.draft/h.md'], 'deleted': ['a.md']},
                        'wiki': {'root': str(self.project / 'wiki'), 'created': ['w.md'],
                                 'updated': [], 'deleted': []}}
        result, data = self.compare()
        self.assertEqual((result['reason'], data), ('no_sources', None))

    def test_symlinked_compile_dir_is_rejected_before_planning(self):
        outside = self.project.parent / 'outside'
        outside.mkdir()
        (self.project / '.auto-context').mkdir()
        (self.project / '.auto-context' / 'compile').symlink_to(outside, target_is_directory=True)
        result, data = self.compare()
        self.assertEqual((result['reason'], data), ('queue_path_rejected', None))
        self.assertEqual(list(outside.iterdir()), [])

    def test_existing_queue_rows_are_appended_after(self):
        self.queue.parent.mkdir(parents=True, exist_ok=True)
        prior = b'{"prior": true}\n'
        self.queue.write_bytes(prior)
        E.enqueue_changed_sources(str(self.project), self.config, copy.deepcopy(self.changed))
        self.assertTrue(self.queue.read_bytes().startswith(prior))
        self.assertEqual(len(self.queue.read_bytes().splitlines()), 5)

    def test_deferred_files_keep_snapshot_entries_and_ack_sees_them(self):
        # The file-change apply contract consumes `deferredFiles` from this seam.
        meta = lambda n: {'mtimeNs': n, 'size': n}
        previous = {'collections': {
            'docs': {'root': str(self.project / 'docs'), 'files': {'a.md': meta(1), 'b.md': meta(1)}},
            'src': {'root': str(self.project / 'src'), 'files': {}}}}
        snapshot = {'collections': {
            'docs': {'root': str(self.project / 'docs'),
                     'files': {'a.md': meta(2), 'b.md': meta(2), 'c.md': meta(1)}},
            'src': {'root': str(self.project / 'src'), 'files': {'s.md': meta(1)}}}}
        persisted, acked = [], []
        with mock.patch.dict(os.environ, {'QMD_SYNC_COMPILE_MAX': '1'}):
            compiled = file_change_plan.apply_change_set(
                {}, copy.deepcopy(self.changed), snapshot, previous,
                enqueue_index=None,
                enqueue_wiki=lambda files: E.enqueue_changed_sources(
                    str(self.project), self.config, files),
                persist=lambda value: persisted.append(copy.deepcopy(value)),
                ack=acked.append)
        self.assertEqual(compiled['deferredFiles'],
                         [('docs', 'b.md'), ('docs', 'c.md'), ('src', 's.md')])
        files = persisted[0]['collections']
        self.assertEqual(files['docs']['files'], {'a.md': meta(2), 'b.md': meta(1)})
        self.assertEqual(files['src']['files'], {})
        # A deferred wake is reported to ACK, so the pending event is kept.
        self.assertEqual(acked, [compiled])
        self.assertEqual(compiled['deferred'], 3)
        self.assertEqual(len(self.rows(self.queue.read_bytes())), 1)


if __name__ == '__main__':
    loader = unittest.defaultTestLoader
    suite = unittest.TestSuite(loader.loadTestsFromTestCase(case)
                               for case in (PurePlanning, EnqueueThroughSeam))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(not result.wasSuccessful())

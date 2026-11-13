#!/usr/bin/env python3
"""Generation-format (3) view stores convert to the row format (4) in one transaction."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
CORE = HERE.parent.parent / 'core'
spec = importlib.util.spec_from_file_location('s_old_bundle_fixture', HERE / 'laya-s-old-bundle-checks.py')
old_bundle = importlib.util.module_from_spec(spec)
spec.loader.exec_module(old_bundle)
publish = old_bundle.publish

CONVERT_CHILD = r'''
import json, os, sys
args = json.loads(sys.argv[1])
sys.path.insert(0, args['core'])
import laya_view_store as views
store = views.ViewStore.open(args['db'], repository=args['repository'], scope_id=args['scope'])
def stop(*_):
    os._exit(17)
if args['point'] == 'before_commit':
    store._db.set_trace_callback(lambda sql: stop() if sql.strip().upper() == 'COMMIT' else None)
    store.convert_to_row_format()
else:
    store.convert_to_row_format()
    stop()
'''


class ConvertChecks(publish.IncrementalPublicationChecks):
    def generation_store(self, bodies, root=None):
        store, repository, handle = self.seeded(root)
        heads = [self.publish(store, handle, body) for body in bodies]
        return store, repository, handle, heads

    def meta(self):
        import sqlite3
        db = sqlite3.connect(str(self.db))
        try:
            return (db.execute('PRAGMA user_version').fetchone()[0],
                    tuple(db.execute('SELECT format,schema_version,incarnation FROM store_meta').fetchone()))
        finally:
            db.close()

    def converted(self, store, repository, handle):
        ino = self.db.stat().st_ino
        result = store.convert_to_row_format()
        self.assertEqual(self.db.stat().st_ino, ino)
        again = self.reopen(repository)
        return again, again.capture_view(handle.worktree_id), result

    def test_head_and_base_keep_ids_ordinals_and_epoch_and_roll_back(self):
        store, repository, handle, heads = self.generation_store(
            [b'# One\nuniqueone synthetic card\n', b'# Two\nuniquetwo synthetic card\n'])
        incarnation = store._incarnation
        ordinals = [row[0] for row in store._db.execute(
            'SELECT ordinal FROM generations WHERE gen_id IN (?,?) ORDER BY ordinal',
            (heads[0]['gen_id'], heads[1]['gen_id']))]
        again, fresh, result = self.converted(store, repository, handle)
        self.assertEqual(result[handle.view_id], dict(gen_id=heads[1]['gen_id'], epoch=2, ordinal=ordinals[1],
                                                      prior=True))
        self.assertEqual(self.meta(), (4, ('memvane-git-blob-candidate-v4', 4, incarnation)))
        self.assertEqual(again.head(fresh), dict(heads[1], incarnation=incarnation))
        self.assertEqual(self.found(again, fresh, 'uniquetwo'), ['cards/a.md'])
        tables = {row[0] for row in again._db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertFalse(tables & {'generation_slots', 'generations', 'wiki_members', 'view_heads'})
        self.assertEqual(len(self.fts_tables(again)), 1)
        back = again.rollback_generation(fresh, expected_head=again.head(fresh))
        self.assertEqual(self.found(again, fresh, 'uniqueone'), ['cards/a.md'])
        self.assertGreater(again._db.execute('SELECT ordinal FROM s_generations WHERE gen_id=?',
                                             (back['gen_id'],)).fetchone()[0], ordinals[1])
        third = self.publish(again, fresh, b'# Three\nuniquethree synthetic card\n')
        self.assertEqual(third['epoch'], 4)
        self.assertEqual(self.found(again, fresh, 'uniquethree'), ['cards/a.md'])

    def test_single_generation_view_converts_without_a_prior(self):
        store, repository, handle, heads = self.generation_store([b'# One\nuniqueone synthetic card\n'])
        again, fresh, result = self.converted(store, repository, handle)
        self.assertFalse(result[handle.view_id]['prior'])
        self.assertEqual(self.found(again, fresh, 'uniqueone'), ['cards/a.md'])
        self.assertEqual(self.refused(again.rollback_generation, fresh,
                                      expected_head=again.head(fresh)).reason, 'rollback_prior_missing')

    def test_two_views_convert_together(self):
        main, linked = self.project('conv'), self.home / 'projects' / 'conv-linked'
        for args in [('init', '-q'), ('add', '-A'), ('commit', '-q', '-m', 'fixture'),
                     ('worktree', 'add', '-q', '-b', 'side', str(linked))]:
            self.git(main, *args)
        store, repository, a = self.seeded(main)
        record = publish.seed.helpers.view.register_worktree(str(linked), repository)
        b = store.capture_view(store.register_worktree(str(linked), record))
        for handle, root, word in ((a, main, 'mainword'), (b, linked, 'sideword')):
            manifest = self.snapshot(store, handle, root=root, body=f'# X\nunique{word} card\n'.encode())
            head = store.head(handle)
            store.publish_generation(handle, store.prepare_generation(handle, manifest, expected_head=head),
                                     expected_head=head)
        result = store.convert_to_row_format()
        self.assertEqual(set(result), {a.view_id, b.view_id})
        again = self.reopen(repository, path=self.state / (main.name + '.db'))
        fa, fb = again.capture_view(a.worktree_id), again.capture_view(b.worktree_id)
        self.assertEqual(self.found(again, fa, 'uniquemainword'), ['cards/a.md'])
        self.assertEqual(self.found(again, fa, 'uniquesideword'), [])
        self.assertEqual(self.found(again, fb, 'uniquesideword'), ['cards/a.md'])

    def test_pending_prepared_generation_refuses_and_keeps_the_old_format(self):
        store, repository, handle, heads = self.generation_store([b'# One\nuniqueone synthetic card\n'])
        manifest = self.snapshot(store, handle, body=b'# Two\nuniquetwo synthetic card\n')
        store.prepare_generation(handle, manifest, expected_head=heads[0])
        self.assertEqual(self.refused(store.convert_to_row_format).reason, 'row_conversion_stage_pending')
        self.assertEqual(self.meta()[0], 3)
        self.assertEqual(self.found(store, handle, 'uniqueone'), ['cards/a.md'])

    def test_row_store_refuses_a_second_conversion(self):
        store, _, handle = self.rows_store()
        self.publish(store, handle, b'# One\nuniqueone synthetic card\n')
        self.assertEqual(self.refused(store.convert_to_row_format).reason, 'row_format_already')

    def crash(self, point):
        store, repository, handle, heads = self.generation_store(
            [b'# One\nuniqueone synthetic card\n', b'# Two\nuniquetwo synthetic card\n'])
        store.close()
        args = dict(core=str(CORE), db=str(self.db), repository=repository, scope=self.scope, point=point)
        result = subprocess.run([sys.executable, '-B', '-c', CONVERT_CHILD, json.dumps(args)],
                                env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'},
                                capture_output=True, encoding='utf8', timeout=120, check=False)
        self.assertEqual(result.returncode, 17, result.stdout + result.stderr)
        again = self.reopen(repository)
        return again, again.capture_view(handle.worktree_id), heads

    def test_process_exit_before_commit_keeps_the_generation_format(self):
        store, handle, heads = self.crash('before_commit')
        self.assertEqual(self.meta()[0], 3)
        self.assertEqual(store.head(handle)['gen_id'], heads[1]['gen_id'])
        self.assertEqual(self.found(store, handle, 'uniquetwo'), ['cards/a.md'])

    def test_process_exit_after_commit_leaves_the_complete_row_format(self):
        store, handle, heads = self.crash('after_commit')
        self.assertEqual(self.meta()[0], 4)
        self.assertEqual(store.head(handle)['gen_id'], heads[1]['gen_id'])
        self.assertEqual(self.found(store, handle, 'uniquetwo'), ['cards/a.md'])

    def test_released_cores_refuse_a_converted_store(self):
        store, repository, handle, _ = self.generation_store([b'# One\nuniqueone synthetic card\n'])
        store.convert_to_row_format()
        for commit in old_bundle.OLD_CORES:
            exported = Path(tempfile.mkdtemp(prefix='s-old-core-', dir=self.home))
            archive = subprocess.run(['git', '-C', str(old_bundle.ROOT), 'archive', commit, 'core'],
                                     capture_output=True, timeout=60)
            self.assertEqual(archive.returncode, 0, archive.stderr.decode(errors='replace'))
            subprocess.run(['tar', '-x', '-C', str(exported)], input=archive.stdout, check=True, timeout=60)
            out = subprocess.run([sys.executable, '-B', '-c', old_bundle.CHILD, str(exported / 'core'),
                                  json.dumps(dict(db=str(self.db), repository=repository, scope=self.scope))],
                                 capture_output=True, text=True, timeout=120,
                                 env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
            self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
            self.assertEqual(set(json.loads(out.stdout.strip().splitlines()[-1]).values()),
                             {'candidate_format_mismatch'}, commit)


def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite()
    for name in loader.getTestCaseNames(ConvertChecks):
        if name in ConvertChecks.__dict__:
            suite.addTest(ConvertChecks(name))
    return suite


if __name__ == '__main__':
    unittest.main()

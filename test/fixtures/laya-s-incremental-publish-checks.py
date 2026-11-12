#!/usr/bin/env python3
"""S view publication contract on an isolated, synthetic row-format ViewStore."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
CORE = HERE.parent.parent / 'core'
spec = importlib.util.spec_from_file_location('s_generation_fixture', HERE / 'laya-view-generations-checks.py')
seed = importlib.util.module_from_spec(spec)
spec.loader.exec_module(seed)

# A child publishes once and stops at a named point; parent verifies the store.
CRASH_CHILD = r'''
import json, os, sys
args = json.loads(sys.argv[1])
sys.path.insert(0, args['core'])
import laya_view_store as views
store = views.ViewStore.open(args['db'], repository=args['repository'], scope_id=args['scope'])
handle = store.capture_view(args['worktree_id'])
manifest = args['manifest']
head = store.head(handle)
gen = store.prepare_generation(handle, manifest, expected_head=head)
point = args['point']
def stop(*_):
    os._exit(17)
if point == 'before_commit':
    # The publication's guarded commit is the first commit on this connection
    # once the head CAS has run.
    import laya_view_fts as fts
    original = fts.ConnectionGuard.view_head
    from contextlib import contextmanager
    @contextmanager
    def crashing(self, view_id):
        with original(self, view_id):
            self._db.set_trace_callback(lambda sql: stop() if sql.strip().upper() == 'COMMIT' else None)
            yield
    fts.ConnectionGuard.view_head = crashing
    store.publish_generation(handle, gen, expected_head=head)
else:
    store.publish_generation(handle, gen, expected_head=head)
    stop()
'''


class IncrementalPublicationChecks(seed.GenerationCase):
    def rows_store(self, root=None):
        repository, worktree = self.bind(root)
        store = self.Store.create(str(self.db), project_root=str(root or self.root),
                                  repository=repository, worktree=worktree, scope_id=self.scope,
                                  row_generations=True)
        self.track(store)
        return store, repository, store.capture_view(worktree['worktreeId'])

    def publish(self, store, handle, body):
        manifest = self.snapshot(store, handle, body=body)
        head = store.head(handle)
        gen = store.prepare_generation(handle, manifest, expected_head=head)
        return store.publish_generation(handle, gen, expected_head=head)

    @staticmethod
    def fts_tables(store):
        return {row[0] for row in store._db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND upper(sql) LIKE '%USING FTS5%'").fetchall()}

    def found(self, store, handle, query):
        pin = store.pin_view(handle)
        try:
            return [row['path'] for row in store.search(pin, query, 10)]
        finally:
            store.release(pin)

    def test_one_card_change_reuses_view_fts_and_does_not_persist_full_stage(self):
        store, _, handle = self.rows_store()
        first_head = self.publish(store, handle, b'# First\nuniquealpha synthetic card\n')
        first_fts = self.fts_tables(store)
        self.assertEqual(len(first_fts), 1, first_fts)

        next_head = self.publish(store, handle, b'# Second\nuniquebeta synthetic card\n')
        self.assertEqual(next_head['epoch'], first_head['epoch'] + 1)
        self.assertEqual(self.found(store, handle, 'uniquebeta'), ['cards/a.md'])
        self.assertEqual(self.found(store, handle, 'uniquealpha'), [])
        self.assertEqual(self.fts_tables(store), first_fts,
                         'each view keeps one head FTS instead of one FTS per generation')
        tables = {row[0] for row in store._db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        self.assertNotIn('generation_slots', tables, 'full manifest/files must not accumulate per generation')

    def test_change_records_one_interval_and_one_delta(self):
        store, _, handle = self.rows_store()
        first = self.publish(store, handle, b'# First\nuniquealpha synthetic card\n')
        second = self.publish(store, handle, b'# Second\nuniquebeta synthetic card\n')
        db = store._db
        gens = db.execute('SELECT ordinal,gen_id,epoch,prior_ordinal FROM s_generations '
                          'WHERE view_id=? ORDER BY ordinal', (handle.view_id,)).fetchall()
        self.assertEqual([(row[1], row[2]) for row in gens],
                         [(first['gen_id'], 1), (second['gen_id'], 2)])
        self.assertEqual(gens[1][3], gens[0][0])
        self.assertEqual(tuple(db.execute('SELECT ordinal,epoch FROM s_heads WHERE view_id=?',
                                          (handle.view_id,)).fetchone()), (gens[1][0], 2))
        cards = db.execute('SELECT gen_from,gen_to FROM s_cards WHERE view_id=? AND path=? ORDER BY gen_from',
                           (handle.view_id, 'cards/a.md')).fetchall()
        self.assertEqual([tuple(row) for row in cards], [(gens[0][0], gens[1][0]), (gens[1][0], None)])
        deltas = db.execute('SELECT ordinal,rel,old_json IS NOT NULL,new_json IS NOT NULL FROM s_deltas '
                            'WHERE view_id=? ORDER BY ordinal', (handle.view_id,)).fetchall()
        self.assertEqual([tuple(row) for row in deltas],
                         [(gens[0][0], 'card', 0, 1), (gens[1][0], 'card', 1, 1)])
        self.assertEqual(db.execute("SELECT COUNT(*) FROM s_stage_jobs WHERE state<>'abandon'").fetchone()[0], 0)

    def test_unchanged_republish_writes_no_card_rows(self):
        store, _, handle = self.rows_store()
        body = b'# Same\nuniquegamma synthetic card\n'
        self.publish(store, handle, body)
        before = store._db.execute('SELECT COUNT(*) FROM s_cards').fetchone()[0]
        self.publish(store, handle, body)
        self.assertEqual(store._db.execute('SELECT COUNT(*) FROM s_cards').fetchone()[0], before)

    def test_stale_pin_refuses_after_new_head(self):
        store, _, handle = self.rows_store()
        self.publish(store, handle, b'# First\nuniquealpha synthetic card\n')
        pin = store.pin_view(handle)
        try:
            self.publish(store, handle, b'# Second\nuniquebeta synthetic card\n')
            self.assertEqual(self.refused(store.search, pin, 'uniquealpha', 10).reason, 'view_head_changed')
        finally:
            store.release(pin)

    def test_sealed_physical_digest_survives_commit_and_reopen(self):
        import laya_view_fts as fts
        store, repository, handle = self.rows_store()
        self.publish(store, handle, b'# First\nuniquealpha synthetic card\n')
        self.publish(store, handle, b'# Second\nuniquebeta synthetic card\n')
        store.close()
        again = self.reopen(repository)
        sealed = again._db.execute('SELECT g.physical_fts_digest FROM s_heads h JOIN s_generations g '
                                   'ON g.view_id=h.view_id AND g.ordinal=h.ordinal WHERE h.view_id=?',
                                   (handle.view_id,)).fetchone()[0]
        self.assertEqual(fts.head_storage_digest(again._db, handle.view_id), sealed)
        handle2 = again.capture_view(handle.worktree_id)
        self.assertEqual(self.found(again, handle2, 'uniquebeta'), ['cards/a.md'])

    def test_failure_before_commit_keeps_previous_head_and_fts(self):
        store, _, handle = self.rows_store()
        first = self.publish(store, handle, b'# First\nuniquealpha synthetic card\n')
        manifest = self.snapshot(store, handle, body=b'# Second\nuniquebeta synthetic card\n')
        gen = store.prepare_generation(handle, manifest, expected_head=first)
        calls = []

        def refuse(*args, **kwargs):
            calls.append(kwargs)
            self.views.v.fail('checkout_changed')
        with mock.patch.object(self.views, '_current_capture', side_effect=refuse):
            self.refused(store.publish_generation, handle, gen, expected_head=first)
        self.assertTrue(calls)
        self.assertEqual(store.head(handle), first)
        self.assertEqual(store._db.execute('SELECT COUNT(*) FROM s_generations').fetchone()[0], 1)
        self.assertEqual(self.found(store, handle, 'uniquealpha'), ['cards/a.md'])
        self.assertEqual(self.found(store, handle, 'uniquebeta'), [])

    def test_other_view_fts_is_not_changed_by_a_neighbor_publication(self):
        import laya_view_fts as fts
        main, linked = self.project('rows'), self.home / 'projects' / 'rows-linked'
        for args in [('init', '-q'), ('add', '-A'), ('commit', '-q', '-m', 'fixture'),
                     ('worktree', 'add', '-q', '-b', 'side', str(linked))]:
            self.git(main, *args)
        store, repository, a = self.rows_store(main)
        record = seed.helpers.view.register_worktree(str(linked), repository)
        b = store.capture_view(store.register_worktree(str(linked), record))
        ma = self.snapshot(store, a, root=main, body=b'# Main\nuniquemain synthetic card\n')
        ha = store.prepare_generation(a, ma, expected_head=store.head(a))
        ha = store.publish_generation(a, ha, expected_head=store.head(a))
        before = fts.head_storage_digest(store._db, a.view_id)
        mb = self.snapshot(store, b, root=linked, body=b'# Side\nuniqueside synthetic card\n')
        gb = store.prepare_generation(b, mb, expected_head=store.head(b))
        store.publish_generation(b, gb, expected_head=store.head(b))
        self.assertEqual(fts.head_storage_digest(store._db, a.view_id), before)
        self.assertEqual(store.head(a), ha)
        self.assertEqual(len(self.fts_tables(store)), 2)
        self.assertEqual(self.found(store, a, 'uniqueside'), [])
        self.assertEqual(self.found(store, b, 'uniqueside'), ['cards/a.md'])

    def test_structural_guards_refuse_rows_outside_the_in_flight_generation(self):
        import sqlite3
        store, repository, handle = self.rows_store()
        self.publish(store, handle, b'# First\nuniquealpha synthetic card\n')
        second = self.publish(store, handle, b'# Second\nuniquebeta synthetic card\n')
        store.close()
        with sqlite3.connect(self.db) as db:
            ordinals = [row[0] for row in db.execute('SELECT ordinal FROM s_generations ORDER BY ordinal')]
            blob = db.execute('SELECT blob_sha FROM s_cards LIMIT 1').fetchone()[0]
            for sql, args, reason in [
                    ('INSERT INTO s_cards(view_id,path,gen_from,gen_to,blob_sha,title,eligible,trust_digest,metadata_json) '
                     "VALUES (?,?,?,NULL,?,'t',0,NULL,'{}')", (handle.view_id, 'cards/z.md', ordinals[-1], blob),
                     'card_generation_invalid'),
                    ('UPDATE s_heads SET ordinal=?,epoch=epoch+1 WHERE view_id=?', (ordinals[0], handle.view_id),
                     'view_head_invalid'),
                    ('UPDATE s_generations SET epoch=9 WHERE ordinal=?', (ordinals[0],), 'generation_immutable'),
                    ('UPDATE s_cards SET title=? WHERE view_id=? AND gen_to IS NULL', ('x', handle.view_id),
                     'card_interval_immutable')]:
                with self.subTest(reason=reason):
                    with self.assertRaisesRegex(sqlite3.DatabaseError, reason):
                        db.execute(sql, args)
        again = self.reopen(repository)
        self.assertEqual(again.head(again.capture_view(handle.worktree_id)), second)

    def test_tampered_card_row_refuses_the_pin(self):
        import sqlite3
        store, repository, handle = self.rows_store()
        self.publish(store, handle, b'# First\nuniquealpha synthetic card\n')
        store.close()
        with sqlite3.connect(self.db) as db:
            trigger = db.execute("SELECT sql FROM sqlite_master WHERE name='s_cards_close_only'").fetchone()[0]
            db.execute('DROP TRIGGER s_cards_close_only')
            db.execute("UPDATE s_cards SET title='Forged' WHERE gen_to IS NULL")
            db.execute(trigger)
        again = self.reopen(repository)
        fresh = again.capture_view(handle.worktree_id)
        self.assertEqual(self.refused(again.pin_view, fresh).reason, 'generation_rows_mismatch')

    def test_prepared_stage_publishes_from_another_store_object(self):
        store, repository, handle = self.rows_store()
        first = self.publish(store, handle, b'# First\nuniquealpha synthetic card\n')
        manifest = self.snapshot(store, handle, body=b'# Second\nuniquebeta synthetic card\n')
        gen = store.prepare_generation(handle, manifest, expected_head=first)
        store.close()
        again = self.reopen(repository)
        fresh = again.capture_view(handle.worktree_id)
        head = again.publish_generation(fresh, gen, expected_head=again.head(fresh))
        self.assertEqual((head['gen_id'], head['epoch']), (gen, first['epoch'] + 1))
        self.assertEqual(self.found(again, fresh, 'uniquebeta'), ['cards/a.md'])
        self.assertEqual(again.publish_generation(fresh, gen, expected_head=first), head)

    def test_card_bytes_changed_after_prepare_refuse_the_publication(self):
        store, _, handle = self.rows_store()
        first = self.publish(store, handle, b'# First\nuniquealpha synthetic card\n')
        manifest = self.snapshot(store, handle, body=b'# Second\nuniquebeta synthetic card\n')
        gen = store.prepare_generation(handle, manifest, expected_head=first)
        (self.root / '.memvane/wiki/cards/a.md').write_bytes(b'# Third\nuniquedelta synthetic card\n')
        self.refused(store.publish_generation, handle, gen, expected_head=first)
        self.assertEqual(store.head(handle), first)
        self.assertEqual(store._db.execute('SELECT COUNT(*) FROM s_generations').fetchone()[0], 1)

    def test_unsupported_row_inputs_refuse_before_any_write(self):
        store, _, handle = self.rows_store()
        manifest = self.snapshot(store, handle)
        spec = dict(recipe_id='a' * 64, profile_id='b' * 64)
        self.refused(store.prepare_generation, handle, manifest, expected_head=store.head(handle),
                     vector_spec=spec)
        self.assertEqual(store._db.execute('SELECT COUNT(*) FROM s_stage_jobs').fetchone()[0], 0)

    def test_default_create_keeps_the_existing_format(self):
        store, _, handle = self.seeded()
        tables = {row[0] for row in store._db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        self.assertIn('generation_slots', tables)
        self.assertNotIn('s_generations', tables)

    def crash(self, point):
        store, repository, handle = self.rows_store()
        first = self.publish(store, handle, b'# First\nuniquealpha synthetic card\n')
        (self.root / '.memvane/wiki/cards/a.md').write_bytes(b'# Second\nuniquebeta synthetic card\n')
        manifest = self.snapshot(store, handle)
        store.close()
        args = dict(core=str(CORE), db=str(self.db), repository=repository, scope=self.scope,
                    worktree_id=handle.worktree_id, manifest=manifest, point=point)
        result = subprocess.run([sys.executable, '-B', '-c', CRASH_CHILD, json.dumps(args)],
                                env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'},
                                capture_output=True, encoding='utf8', timeout=120, check=False)
        self.assertEqual(result.returncode, 17, result.stdout + result.stderr)
        again = self.reopen(repository)
        return again, again.capture_view(handle.worktree_id), first

    def test_process_exit_before_commit_leaves_the_previous_head(self):
        store, handle, first = self.crash('before_commit')
        self.assertEqual(store.head(handle)['gen_id'], first['gen_id'])
        self.assertEqual(self.found(store, handle, 'uniquealpha'), ['cards/a.md'])

    def test_process_exit_after_commit_leaves_the_complete_new_head(self):
        store, handle, first = self.crash('after_commit')
        head = store.head(handle)
        self.assertEqual(head['epoch'], first['epoch'] + 1)
        self.assertEqual(self.found(store, handle, 'uniquebeta'), ['cards/a.md'])
        self.assertEqual(self.found(store, handle, 'uniquealpha'), [])


if __name__ == '__main__':
    unittest.main()

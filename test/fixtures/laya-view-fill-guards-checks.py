"""Synthetic fill progress guards; commitments do not grant operational authority."""
import copy
from contextlib import closing, contextmanager
import gc
import importlib.util
import json
from pathlib import Path
import sqlite3
import struct
import unittest
from unittest import mock
import weakref

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('view_fill_guard_helpers', HERE / 'laya-view-fill-checks.py')
helpers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helpers)


class FillGuardChecks(helpers.FillCase):
    def partial(self):
        values = self.configured(2)
        store, _, handle, manifest, recipe_id, profile_id = values
        self.assert_status(self.fill(store, handle, manifest, recipe_id, profile_id, max_files=1), 1, False)
        return values

    def prime_pending(self, values):
        store, _, handle, manifest, recipe_id, profile_id = values
        member = sorted(manifest['members'], key=lambda row: row['path'])[1]
        blob = member['blob_sha']
        body = store.document(blob)['body']
        store.put_chunk_set(blob, recipe_id, [helpers.sets.chunk(body)])
        store.put_embedding_set(blob, recipe_id, profile_id,
            [dict(seq=0, input_sha=store.vector_input_sha(blob, recipe_id, profile_id, 0),
                  embedding=struct.pack('<ff', 1, 0.5))])
        self.assert_status(store.fill_status(handle, manifest, recipe_id, profile_id), 1, False)
        self.calls = dict(chunker=[], embedder=[], fence=[])

    @staticmethod
    def rows(db):
        return {name: db.execute('SELECT * FROM ' + name + ' ORDER BY 1,2').fetchall()
                for name in ('fill_jobs', 'fill_targets')}

    def sql_refused(self, db, sql, parameters=()):
        before, changes = self.rows(db), db.total_changes
        with self.assertRaises(sqlite3.Error):
            db.execute(sql, parameters)
        db.rollback()
        self.assertEqual((self.rows(db), db.total_changes), (before, changes))

    def test_01_job_identity_changes_cannot_be_hidden_inside_valid_cursor_advance(self):
        store, _, handle, manifest, r, p = self.partial()
        job = store.fill_status(handle, manifest, r, p)['job_id']
        mutations = {
            'job_id': '0' * 64, 'incarnation': '0' * 32, 'view_id': '0' * 64,
            'worktree_id': '0' * 32, 'recipe_id': '0' * 64, 'profile_id': '0' * 64,
            'manifest_digest': '0' * 64, 'targets_digest': '0' * 64,
            'identity_json': '{}', 'target_count': 3, 'created_ns': 1,
        }
        with closing(sqlite3.connect(self.db)) as db:
            for field, value in mutations.items():
                with self.subTest(field=field):
                    self.sql_refused(db, f'UPDATE fill_jobs SET {field}=?,cursor=2 WHERE job_id=?', (value, job))
        self.assert_status(store.fill_status(handle, manifest, r, p), 1, False)

    def test_02_job_rewind_delete_and_replace_preserve_exact_progress_rows(self):
        store, _, handle, manifest, r, p = self.partial()
        job = store.fill_status(handle, manifest, r, p)['job_id']
        with closing(sqlite3.connect(self.db)) as db:
            for sql in ('UPDATE fill_jobs SET cursor=0 WHERE job_id=?',
                        'DELETE FROM fill_jobs WHERE job_id=?',
                        'INSERT OR REPLACE INTO fill_jobs SELECT * FROM fill_jobs WHERE job_id=?'):
                with self.subTest(sql=sql):
                    self.sql_refused(db, sql, (job,))
        self.assert_status(store.fill_status(handle, manifest, r, p), 1, False)

    def test_03_target_update_delete_and_replace_cannot_rebind_pending_content(self):
        store, _, handle, manifest, r, p = self.partial()
        job = store.fill_status(handle, manifest, r, p)['job_id']
        with closing(sqlite3.connect(self.db)) as db:
            for sql in ("UPDATE fill_targets SET path=path||'-changed' WHERE job_id=?",
                        'UPDATE fill_targets SET ordinal=ordinal+10 WHERE job_id=?',
                        'DELETE FROM fill_targets WHERE job_id=?',
                        'INSERT OR REPLACE INTO fill_targets SELECT * FROM fill_targets WHERE job_id=?'):
                with self.subTest(sql=sql):
                    self.sql_refused(db, sql, (job,))
        self.assert_status(store.fill_status(handle, manifest, r, p), 1, False)

    def test_04_empty_target_completion_repeat_is_readonly_and_provider_free(self):
        store, _, handle, manifest, r, p = self.configured()
        manifest = copy.deepcopy(manifest)
        manifest['members'] = []
        first = self.fill(store, handle, manifest, r, p)
        self.assert_status(first, 0, True)
        self.assertEqual((self.calls['chunker'], self.calls['embedder'], len(self.calls['fence'])), ([], [], 1))
        before, changes = store.counts(), store._db.total_changes
        self.calls = dict(chunker=[], embedder=[], fence=[])
        original = store._session
        @contextmanager
        def readonly(*, write=False):
            self.assertFalse(write, 'completed empty target must not request a write session')
            with original(write=False) as db:
                yield db
        with mock.patch.object(store, '_session', side_effect=readonly):
            again = self.fill(store, handle, manifest, r, p)
        self.assert_status(again, 0, True)
        self.assertEqual(again['job_id'], first['job_id'])
        self.assertEqual((store.counts(), store._db.total_changes), (before, changes))
        self.assertFalse(any(self.calls.values()))

    def test_05_existing_pending_cursor_repairs_from_cache_with_one_fence_and_no_provider(self):
        values = self.partial()
        store, _, handle, manifest, r, p = values
        self.prime_pending(values)
        sets_before, changes = store.counts()['embedding_sets'], store._db.total_changes
        result = self.fill(store, handle, manifest, r, p)
        self.assert_status(result, 2, True)
        self.assertEqual((result['filled'], result['cache_hits']), (0, 1))
        self.assertEqual((self.calls['chunker'], self.calls['embedder'], len(self.calls['fence'])), ([], [], 1))
        self.assertEqual(store.counts()['embedding_sets'], sets_before)
        self.assertGreater(store._db.total_changes, changes)

    def test_06_readonly_cursor_write_failure_keeps_cached_content_and_pending_position(self):
        values = self.partial()
        store, _, handle, manifest, r, p = values
        self.prime_pending(values)
        before, changes = store.counts(), store._db.total_changes
        store._db.execute('PRAGMA query_only=ON')
        try:
            self.bounded_refusal(self.fill, store, handle, manifest, r, p)
        finally:
            store._db.execute('PRAGMA query_only=OFF')
        self.assertEqual((store.counts(), store._db.total_changes), (before, changes))
        self.assert_status(store.fill_status(handle, manifest, r, p), 1, False)
        self.assertEqual((self.calls['chunker'], self.calls['embedder'], len(self.calls['fence'])), ([], [], 1))
        self.calls = dict(chunker=[], embedder=[], fence=[])
        self.assert_status(self.fill(store, handle, manifest, r, p), 2, True)
        self.assertEqual((self.calls['embedder'], len(self.calls['fence'])), ([], 1))

    def test_07_sql_writer_busy_cannot_advance_pending_cursor_or_repeat_provider(self):
        values = self.partial()
        store, _, handle, manifest, r, p = values
        self.prime_pending(values)
        before, changes = store.counts(), store._db.total_changes
        old_timeout = store._db.execute('PRAGMA busy_timeout').fetchone()[0]
        blocker = sqlite3.connect(self.db, timeout=0.1)
        try:
            blocker.execute('BEGIN IMMEDIATE')
            store._db.execute('PRAGMA busy_timeout=50')
            self.bounded_refusal(self.fill, store, handle, manifest, r, p)
        finally:
            blocker.rollback()
            blocker.close()
            store._db.execute(f'PRAGMA busy_timeout={old_timeout}')
        self.assert_status(store.fill_status(handle, manifest, r, p), 1, False)
        self.assertEqual((store.counts(), store._db.total_changes), (before, changes))
        self.assertEqual((self.calls['chunker'], self.calls['embedder']), ([], []))
        self.assertFalse(store._db.in_transaction)

    def test_08_settings_change_after_callback_and_observation_blocks_cursor_commit(self):
        store, _, handle, manifest, r, p = self.configured()
        path = self.root / '.memvane/settings.json'
        original_bytes, observe = path.read_bytes(), store._observe
        changed = []
        def observe_then_change(entry):
            saved = observe(entry)
            if self.calls['embedder'] and not changed:
                path.write_text(json.dumps(dict(name='blobfixture-renamed')), encoding='utf8')
                changed.append(True)
            return saved
        try:
            with mock.patch.object(store, '_observe', side_effect=observe_then_change):
                self.bounded_refusal(self.fill, store, handle, manifest, r, p)
        finally:
            path.write_bytes(original_bytes)
        self.assertEqual(changed, [True])
        self.assertEqual(store.counts()['embedding_sets'], 1, 'valid complete content survives stale view progress')
        self.assert_status(store.fill_status(handle, manifest, r, p), 0, False)
        self.assertEqual(store._db.execute('SELECT COUNT(*) FROM fill_jobs').fetchone()[0], 0)

    def test_09_checkout_root_replaced_during_provider_cannot_advance_capture(self):
        store, _, handle, manifest, r, p = self.configured()
        original_embed = self.callbacks(store)['embedder']
        moved = self.root.with_name(self.root.name + '-retained')
        changed = []
        def replace_root(inputs, profile):
            result = original_embed(inputs, profile)
            self.root.rename(moved)
            self.root.mkdir(mode=0o700)
            changed.append(True)
            return result
        try:
            self.bounded_refusal(self.fill, store, handle, manifest, r, p, embedder=replace_root)
        finally:
            if changed:
                self.root.rmdir()
                moved.rename(self.root)
        self.assertEqual(changed, [True])
        self.assertEqual(store.counts()['embedding_sets'], 1)
        self.assert_status(store.fill_status(handle, manifest, r, p), 0, False)
        self.assertEqual(store._db.execute('SELECT COUNT(*) FROM fill_jobs').fetchone()[0], 0)

    def test_10_rejected_first_fence_has_zero_candidate_mutations_and_callbacks(self):
        store, _, handle, manifest, r, p = self.configured()
        before, changes = store.counts(), store._db.total_changes
        invoked = []
        def reject():
            invoked.append(True)
            self.assertFalse(store._db.in_transaction)
            self.assertEqual((store.counts(), store._db.total_changes), (before, changes))
            raise RuntimeError('synthetic write fence rejected')
        with self.assertRaises((RuntimeError, self.Error)):
            self.fill(store, handle, manifest, r, p, write_fence=reject)
        self.assertEqual(invoked, [True])
        self.assertEqual((store.counts(), store._db.total_changes), (before, changes))
        self.assert_status(store.fill_status(handle, manifest, r, p), 0, False)
        self.assertEqual((self.calls['chunker'], self.calls['embedder']), ([], []))

    def test_11_readonly_status_does_not_retain_all_eligible_bodies(self):
        store, _, handle, manifest, r, p = self.configured(12)
        active, peak = {}, [0]
        original = store._blob
        class TrackedBody(str):
            pass
        def released(blob):
            active[blob] -= 1
            if not active[blob]:
                del active[blob]
        def observed(blob, **kwargs):
            body = TrackedBody(original(blob, **kwargs))
            active[blob] = active.get(blob, 0) + 1
            peak[0] = max(peak[0], len(active))
            weakref.finalize(body, released, blob)
            return body
        changes = store._db.total_changes
        with mock.patch.object(store, '_blob', side_effect=observed):
            self.assert_status(store.fill_status(handle, manifest, r, p), 0, False)
        gc.collect()
        self.assertGreater(peak[0], 0, 'actual blob body lifetimes are observed')
        self.assertLessEqual(peak[0], 2, 'readonly planning streams metadata instead of retaining every body')
        self.assertEqual(active, {})
        self.assertEqual(store._db.total_changes, changes)
        self.assertFalse(any(self.calls.values()))


if __name__ == '__main__':
    unittest.main()

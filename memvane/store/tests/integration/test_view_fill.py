"""Bounded capture-bound view fill over the published head; synthetic callbacks and private state only."""
import copy
from contextlib import contextmanager
from dataclasses import replace
import json
import unittest
from unittest import mock

from memvane.store.tests import support as sets
from memvane.store.tests.support import FillCase


class FillChecks(FillCase):
    def test_01_absent_status_is_readonly_and_deterministic(self):
        store, _, h, manifest, r, p = self.configured()
        before = store._db.total_changes
        first = store.fill_status(h, manifest, r, p)
        self.assert_status(first, 0, False)
        self.assertEqual(first, store.fill_status(h, copy.deepcopy(manifest), r, p))
        self.assertEqual(store._db.total_changes, before)
        self.assertFalse(any(self.calls.values()))

    def test_02_default_eight_members_use_sorted_synthetic_inputs(self):
        store, _, h, manifest, r, p = self.configured(9)
        result = self.fill(store, h, manifest, r, p)
        self.assert_status(result, 8, False)
        self.assertTrue({'status', 'filled', 'cache_hits', 'deferred'} <= set(result))
        self.assertEqual(result['filled'], 8)
        expected = sorted(manifest['members'], key=lambda row: row['path'])[:8]
        self.assertEqual([row['path'] for row in self.calls['chunker']],
                         [f"blobs/{row['blob_sha']}.md" for row in expected])
        for document, inputs in zip(self.calls['chunker'], self.calls['embedder']):
            self.assertEqual(inputs, [f"title: {document['title'] or 'none'} | text: {document['body']}"])
        self.assertEqual(len(self.calls['fence']), 1)

    def test_03_utf8_bytes_and_explicit_oversized_member_budget(self):
        body = ('# Big\n' + '별' * 700000 + '\n').encode('utf8')
        store, _, h, manifest, r, p = self.configured(body=body)
        first = self.fill(store, h, manifest, r, p)
        self.assert_status(first, 0, False)
        self.assertTrue(first['deferred'], 'default 2000000-byte budget explicitly defers this body')
        self.assertEqual(self.calls['embedder'], [])
        second = self.fill(store, h, manifest, r, p, max_bytes=len(body) - 1)
        self.assert_status(second, 0, False)
        final = self.fill(store, h, manifest, r, p, max_files=1, max_bytes=len(body))
        self.assert_status(final, 1, True)
        self.assertEqual(len(self.calls['embedder']), 1)

    def test_04_reopen_resumes_and_stale_cursor_cannot_advance(self):
        store, repository, h, manifest, r, p = self.configured(3)
        first = self.fill(store, h, manifest, r, p, max_files=1)
        self.assert_status(first, 1, False)
        store.close()
        other = self.reopen(repository)
        fresh = other.capture_view(h.worktree_id)
        self.assertEqual(other.fill_status(fresh, manifest, r, p)['job_id'], first['job_id'])
        before = other._db.total_changes
        self.bounded_refusal(self.fill, other, fresh, manifest, r, p, expected_cursor=0)
        self.assertEqual(other._db.total_changes, before)
        self.assert_status(self.fill(other, fresh, manifest, r, p, max_files=1), 2, False)
        self.assert_status(self.fill(other, fresh, manifest, r, p), 3, True)

    def test_05_limits_cursor_and_deadline_are_validated_before_progress(self):
        store, _, h, manifest, r, p = self.configured()
        options = self.callbacks(store)
        with self.assertRaises(TypeError):
            store.fill_view(h, manifest, r, p, **options)
        for change in [dict(expected_cursor=True), dict(expected_cursor=-1), dict(max_files=0),
                       dict(max_files=65), dict(max_files=True), dict(max_bytes=0),
                       dict(max_bytes=64 * 1024 * 1024 + 1), dict(max_bytes=True),
                       dict(deadline_seconds=0), dict(deadline_seconds=float('nan')),
                       dict(deadline_seconds=301), dict(deadline_seconds=10**1000)]:
            with self.subTest(change=change):
                self.bounded_refusal(self.fill, store, h, manifest, r, p, **change)
                self.assert_status(store.fill_status(h, manifest, r, p), 0, False)
        self.assertFalse(any(self.calls.values()))

    def test_06_job_identity_binds_head_recipe_profile_and_issued_capture(self):
        # Format 4 fills the published head: the caller manifest no longer names
        # the targets, a new head does.
        store, _, h, manifest, r, p = self.configured(2)
        self.fill(store, h, manifest, r, p, max_files=1)
        old = store.fill_status(h, manifest, r, p)
        rspec = sets.recipe(); rspec['chunker_sha'] = '8' * 64
        pspec = sets.profile(); pspec['model_sha'] = '9' * 64
        for rr, pp in [(store.register_recipe(rspec), p), (r, store.register_profile(pspec))]:
            with self.subTest(recipe=rr, profile=pp):
                status = store.fill_status(h, manifest, rr, pp)
                self.assertNotEqual(status['job_id'], old['job_id'])
                self.assert_status(status, 0, False)
                self.bounded_refusal(self.fill, store, h, manifest, rr, pp, expected_cursor=1)
        self.bounded_refusal(store.fill_status, replace(h, checkout_digest='f' * 64), manifest, r, p)
        self.assert_status(store.fill_status(h, manifest, r, p), 1, False)
        changed = copy.deepcopy(manifest)
        changed['members'][0]['display_title'] = 'different display title'
        self.lexical(store, h, changed)
        status = store.fill_status(h, changed, r, p)
        self.assertNotEqual(status['job_id'], old['job_id'])
        self.assert_status(status, 0, False)
        self.bounded_refusal(self.fill, store, h, changed, r, p, expected_cursor=1)

    def test_07_provider_failure_keeps_cursor_and_retry_is_possible(self):
        store, _, h, manifest, r, p = self.configured()
        def broken(inputs, profile):
            self.assertFalse(store._db.in_transaction)
            raise RuntimeError('synthetic provider interruption')
        with self.assertRaises((self.Error, RuntimeError)):
            self.fill(store, h, manifest, r, p, embedder=broken)
        self.assert_status(store.fill_status(h, manifest, r, p), 0, False)
        self.assertEqual(store.counts()['embedding_sets'], 0)
        self.assert_status(self.fill(store, h, manifest, r, p), 1, True)

    def test_08_callbacks_and_first_fence_never_run_in_sql_transaction(self):
        store, _, h, manifest, r, p = self.configured(2)
        initial = store._db.total_changes
        options = self.callbacks(store)
        original_fence = options['write_fence']
        def fence():
            self.assertEqual(store._db.total_changes, initial, 'fence precedes first candidate DB mutation')
            original_fence()
        self.assert_status(self.fill(store, h, manifest, r, p, write_fence=fence), 2, True)
        self.assertEqual(len(self.calls['fence']), 1)
        self.assertEqual(len(self.calls['chunker']), 2)

    def test_09_set_commit_survives_cursor_failure_and_repair_is_fenced(self):
        store, _, h, manifest, r, p = self.configured()
        original_session = store._session
        @contextmanager
        def interrupted(*, write=False):
            if write and store._db.execute('SELECT COUNT(*) FROM embedding_sets').fetchone()[0]:
                raise RuntimeError('synthetic cursor commit interruption')
            with original_session(write=write) as db:
                yield db
        with mock.patch.object(store, '_session', side_effect=interrupted):
            with self.assertRaisesRegex(RuntimeError, 'synthetic cursor commit interruption'):
                self.fill(store, h, manifest, r, p)
        self.assertEqual(store.counts()['embedding_sets'], 1, 'complete content commits before cursor')
        self.assert_status(store.fill_status(h, manifest, r, p), 0, False)
        self.calls = dict(chunker=[], embedder=[], fence=[])
        before = store._db.total_changes
        result = self.fill(store, h, manifest, r, p)
        self.assert_status(result, 1, True)
        self.assertEqual(result['cache_hits'], 1)
        self.assertEqual(self.calls['embedder'], [])
        self.assertEqual(self.calls['chunker'], [])
        self.assertEqual(len(self.calls['fence']), 1, 'pending cursor repair is fenced progress')
        self.assertGreater(store._db.total_changes, before)

    def test_10_completed_job_repeat_has_zero_mutations_fence_or_providers(self):
        store, _, h, manifest, r, p = self.configured()
        done = self.fill(store, h, manifest, r, p)
        self.calls = dict(chunker=[], embedder=[], fence=[])
        before = store._db.total_changes
        original = store._session
        @contextmanager
        def readonly(*, write=False):
            self.assertFalse(write, 'completed unchanged fill must not open a write session')
            with original(write=False) as db:
                yield db
        with mock.patch.object(store, '_session', side_effect=readonly):
            again = self.fill(store, h, manifest, r, p)
        self.assert_status(again, 1, True)
        self.assertEqual(again['job_id'], done['job_id'])
        self.assertEqual(store._db.total_changes, before)
        self.assertFalse(any(self.calls.values()))

    def test_11_settings_changed_by_callback_cannot_advance_stale_cursor(self):
        store, _, h, manifest, r, p = self.configured()
        path = self.root / '.memvane/settings.json'; original = path.read_bytes()
        options = self.callbacks(store); embedder = options['embedder']
        def changing(inputs, profile):
            vectors = embedder(inputs, profile)
            path.write_text(json.dumps(dict(name='blobfixture-renamed')), encoding='utf8')
            return vectors
        try:
            self.bounded_refusal(self.fill, store, h, manifest, r, p, embedder=changing)
        finally:
            path.write_bytes(original)
        self.assertEqual(store.counts()['embedding_sets'], 1, 'valid immutable content cache survives stale view')
        self.assert_status(store.fill_status(h, manifest, r, p), 0, False)
        self.calls = dict(chunker=[], embedder=[], fence=[])
        self.assert_status(self.fill(store, h, manifest, r, p), 1, True)
        self.assertEqual(self.calls['embedder'], [])

    def test_12_duplicate_content_paths_reuse_one_complete_set(self):
        store, _, h, manifest, r, p = self.configured(body='별빛 아래 titleless 😀\n'.encode('utf8'), publish=False)
        body = store.document(manifest['members'][0]['blob_sha'])['body'].encode('utf8')
        self.add_member(store, h, manifest, 'renamed/copy.md', body, 'renamed display')
        self.lexical(store, h, manifest)
        result = self.fill(store, h, manifest, r, p, max_files=64, max_bytes=64 * 1024 * 1024)
        self.assert_status(result, 2, True)
        self.assertEqual((result['filled'], result['cache_hits']), (1, 1))
        self.assertEqual(len(self.calls['embedder']), 1)
        self.assertEqual(store.counts()['embedding_sets'], 1)
        self.assertEqual(len(self.calls['fence']), 1)


if __name__ == '__main__':
    unittest.main()

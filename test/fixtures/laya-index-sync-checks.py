#!/usr/bin/env python3
"""Disposable selected-sync checks; every database and source is test-owned."""
import os
from pathlib import Path
from contextlib import closing
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'core'))

from laya_index_a import IndexContractError, LayaIndexA
from laya_index_claim_queue import ClaimQueue
from laya_index_runtime import LayaIndexRuntime
from laya_index_source_port import (publish_from_source, scan, search_verified,
                                    stage_from_source)
from laya_index_sync import _owner_lock, prepare_candidate_batch_off_hook, sync_full
from laya_index_vector import (LayaVectorIndex, qmd_markdown_title,
                               search_vector_verified)
import laya_index_sync

SCOPE = 'a' * 32


class SyncChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='laya-sync-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.source = self.home / 'source'
        self.source.mkdir()
        (self.source / 'chapter.md').write_text('silver otter', encoding='utf-8')
        self.index_path = self.home / 'index.sqlite3'
        self.queue_path = self.home / 'queue.sqlite3'
        with closing(LayaIndexA(self.index_path, create=True)) as index:
            self.incarnation = index.incarnation()
        ClaimQueue.create(self.queue_path,
                          index_incarnation=self.incarnation).close()

    def runtime(self, scope=SCOPE, source=None):
        return LayaIndexRuntime(source_root=str(source or self.source), scope=scope,
                                db_path=str(self.index_path),
                                queue_path=str(self.queue_path),
                                db_incarnation=self.incarnation)

    @staticmethod
    def sync(runtime, before_publish=lambda: None):
        # Historical lexical L0 behavior remains an explicit synthetic opt-out.
        return sync_full(runtime, before_publish=before_publish, require_vector=False)

    @staticmethod
    def vector_batch(runtime, *, fingerprint='f' * 64):
        source = scan(runtime.source_root, runtime.scope, runtime.index)
        identities = sorted({(f.sha256, qmd_markdown_title(f.text, f.path))
                             for f in source.files if f.eligible})
        return {'scope': runtime.scope, 'source_manifest': source.manifest,
                'model_id': 'synthetic-precomputed-only',
                'embedding_fingerprint': fingerprint, 'dimension': 2,
                'chunks': [{'hash': hash_, 'title': title, 'seq': 0,
                            'pos': 0, 'vector': [1.0, 0.0]}
                           for hash_, title in identities]}

    class FakeChunkProvider:
        profile = {'model_id': 'synthetic-precomputed-only',
                   'embedding_fingerprint': 'f' * 64, 'dimension': 2}
        manifest = {'recipeId': 'qmd-regex-token-chunks-candidate-v1'}
        def __init__(self, callback=None):
            self.calls = []
            self.callback = callback
        def embed_document_chunks(self, body, path):
            self.calls.append((body, path))
            if self.callback:
                self.callback()
            return [{'seq': 0, 'pos': 0, 'vector': [1.0, 0.0]}]

    def test_explicit_off_hook_chunk_batch_connects_to_sealed_sync(self):
        with self.runtime() as runtime:
            provider = self.FakeChunkProvider()
            batch = prepare_candidate_batch_off_hook(runtime, provider)
            self.assertEqual(len(provider.calls), 1)
            self.assertEqual(provider.calls[0][1], 'chapter.md')
            self.assertEqual(batch['scope'], runtime.scope)
            result = sync_full(runtime, before_publish=lambda: None,
                               vector_batch=batch)
            self.assertEqual(result['state'], 'indexed')
            token, _ = runtime.index.pin(runtime.scope)
            try:
                self.assertEqual(LayaVectorIndex(runtime.index).profile_for_pin(
                    token)['model_id'], 'synthetic-precomputed-only')
            finally:
                runtime.index.release(token)

    def test_retry_reuses_sealed_chunks_without_second_embedding(self):
        with self.runtime() as runtime:
            first = self.FakeChunkProvider()
            batch = prepare_candidate_batch_off_hook(runtime, first)
            self.assertEqual(sync_full(runtime, before_publish=lambda: None,
                                       vector_batch=batch)['state'], 'indexed')
            second = self.FakeChunkProvider()
            replay = prepare_candidate_batch_off_hook(runtime, second)
            self.assertEqual(second.calls, [])
            self.assertEqual(replay, batch)
            self.assertEqual(sync_full(runtime, before_publish=lambda: None,
                                       vector_batch=replay)['state'], 'unchanged')

    def test_candidate_batch_rejects_gap_and_wrong_chunk_order(self):
        with self.runtime() as runtime:
            batch = self.vector_batch(runtime)
            for seq, pos in ((1, 0), (0, 1)):
                bad = {**batch, 'chunks': [{**batch['chunks'][0],
                                            'seq': seq, 'pos': pos}]}
                if seq == 0:
                    bad['chunks'].append({**batch['chunks'][0],
                                          'seq': 1, 'pos': 0})
                with self.subTest(seq=seq, pos=pos):
                    with self.assertRaisesRegex(IndexContractError,
                                                'vector_coverage_incomplete'):
                        sync_full(runtime, before_publish=lambda: self.fail('marked'),
                                  vector_batch=bad)

    def test_long_qmd_heading_preserves_full_title_identity(self):
        heading = 'x' * 5000
        (self.source / 'chapter.md').write_text('# ' + heading + '\nbody',
                                                 encoding='utf-8')
        with self.runtime() as runtime:
            batch = prepare_candidate_batch_off_hook(runtime, self.FakeChunkProvider())
            self.assertEqual(batch['chunks'][0]['title'], heading)
            self.assertEqual(sync_full(runtime, before_publish=lambda: None,
                                       vector_batch=batch)['state'], 'indexed')

    def test_duplicate_content_distinct_titles_publish_and_reopen_with_own_vectors(self):
        (self.source / 'other.md').write_text('silver otter', encoding='utf8')
        with self.runtime() as runtime:
            class Distinct(self.FakeChunkProvider):
                def embed_document_chunks(self, body, path):
                    self.calls.append((body, path))
                    return [{'seq': 0, 'pos': 0,
                             'vector': [1.0, 0.0] if path == 'chapter.md'
                                       else [0.0, 1.0]}]
            provider = Distinct()
            batch = prepare_candidate_batch_off_hook(runtime, provider)
            self.assertEqual({path for _, path in provider.calls},
                             {'chapter.md', 'other.md'})
            self.assertEqual(sync_full(runtime, before_publish=lambda: None,
                                       vector_batch=batch)['state'], 'indexed')
        with self.runtime() as reopened:
            token, _ = reopened.index.pin(reopened.scope)
            try:
                vectors = LayaVectorIndex(reopened.index)
                args = {'model_id': provider.profile['model_id'],
                        'embedding_fingerprint': provider.profile['embedding_fingerprint'],
                        'dimension': 2, 'limit': 2}
                first = vectors.search_exact(token, [1.0, 0.0], **args)
                second = vectors.search_exact(token, [0.0, 1.0], **args)
                self.assertEqual([row['path'] for row in first],
                                 ['chapter.md', 'other.md'])
                self.assertEqual([row['path'] for row in second],
                                 ['other.md', 'chapter.md'])
                self.assertEqual(reopened.index.db.execute(
                    'SELECT COUNT(*) FROM gen_vector_rows').fetchone()[0], 2)
            finally:
                reopened.index.release(token)

    def test_source_change_during_embedding_rejects_stale_batch(self):
        with self.runtime() as runtime:
            provider = self.FakeChunkProvider(lambda:
                (self.source / 'chapter.md').write_text('new body', encoding='utf8'))
            with self.assertRaisesRegex(IndexContractError, 'index_stale'):
                prepare_candidate_batch_off_hook(runtime, provider)
            self.assertEqual(len(provider.calls), 1)

    def _child(self, mode):
        code = '''
import sys,time
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from laya_index_runtime import LayaIndexRuntime
from laya_index_source_port import scan
from laya_index_sync import _owner_lock
db,queue,source,inc,scope,marker,mode=sys.argv[2:]
with LayaIndexRuntime(source_root=source,scope=scope,db_path=db,
                      queue_path=queue,db_incarnation=inc) as runtime:
    with _owner_lock(Path(queue)) as owned:
        assert owned
        if mode=='lease':
            manifest=scan(source,scope,runtime.index).manifest
            job=runtime.queue.enqueue_next(scope,'g-child',manifest,None)
            assert runtime.queue.lease(job['claim']) is not None
        Path(marker).write_text('ready')
        while True: time.sleep(.1)
'''
        marker = self.home / ('child-' + mode)
        args = [sys.executable, '-c', code, str(ROOT / 'core'),
                str(self.index_path), str(self.queue_path), str(self.source),
                self.incarnation, SCOPE, str(marker), mode]
        env = {**os.environ, 'QMD_SANDBOX': '1', 'QMD_RECALL_LOG': '',
               'PYTHONDONTWRITEBYTECODE': '1'}
        child = subprocess.Popen(args, env=env, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.PIPE, text=True)
        deadline = time.monotonic() + 5
        while not marker.exists() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(.02)
        if not marker.exists():
            try:
                child.kill()
            except ProcessLookupError:
                pass
            _, error = child.communicate(timeout=3)
            self.fail('child did not acquire owner lock: ' + error)
        return child

    def test_initial_publish_and_clean_second_scan(self):
        with self.runtime() as runtime:
            first = self.sync(runtime)
            self.assertEqual(first['state'], 'indexed')
            self.assertRegex(first['generation'], r'^g-[0-9a-f]{32}$')
            self.assertEqual(runtime.queue.get(first['claim'])['state'], 'acked')
            self.assertEqual(self.sync(runtime),
                             {'state': 'unchanged', 'generation': first['generation'],
                              'claim': None})
            self.assertEqual([r['path'] for r in runtime.query('"otter"', 5)],
                             ['chapter.md'])

    def test_changed_and_deleted_source_publishes_complete_membership(self):
        with self.runtime() as runtime:
            first = self.sync(runtime)
            (self.source / 'chapter.md').write_text('green heron', encoding='utf-8')
            (self.source / 'new.md').write_text('blue heron', encoding='utf-8')
            changed = self.sync(runtime)
            self.assertEqual(changed['state'], 'indexed')
            self.assertNotEqual(first['generation'], changed['generation'])
            self.assertEqual(len(runtime.query('"heron"', 5)), 2)
            (self.source / 'chapter.md').unlink()
            deleted = self.sync(runtime)
            self.assertEqual(deleted['state'], 'indexed')
            self.assertEqual([r['path'] for r in runtime.query('"heron"', 5)],
                             ['new.md'])
            self.assertFalse(runtime.inspect()['deletion_enabled'])

    def test_exclusion_epoch_rebuilds_even_when_manifest_stays_same(self):
        with self.runtime() as runtime:
            first = self.sync(runtime)
            runtime.index.exclude(runtime.scope, path='absent.md')
            with self.assertRaisesRegex(IndexContractError, 'index_stale'):
                runtime.query('"otter"', 5)
            second = self.sync(runtime)
            self.assertEqual(second['state'], 'indexed')
            self.assertNotEqual(first['generation'], second['generation'])
            self.assertEqual(len(runtime.query('"otter"', 5)), 1)

    def test_pending_claim_replays_before_new_scan(self):
        with self.runtime() as runtime:
            manifest = scan(str(self.source), runtime.scope, runtime.index).manifest
            job = runtime.queue.enqueue_next(runtime.scope, 'g-pending', manifest, None)
            result = self.sync(runtime)
            self.assertEqual(result['claim'], job['claim'])
            self.assertEqual(result['state'], 'indexed')
            self.assertEqual(runtime.queue.get(job['claim'])['state'], 'acked')

    def test_receipt_without_ack_is_replayed_and_conditionally_acked(self):
        with self.runtime() as runtime:
            manifest = scan(str(self.source), runtime.scope, runtime.index).manifest
            job = runtime.queue.enqueue_next(runtime.scope, 'g-receipt', manifest, None)
            with _owner_lock(self.queue_path) as owned:
                self.assertTrue(owned)
                self.assertIsNotNone(runtime.queue.lease(job['claim']))
                stage_from_source(runtime.index, str(self.source), runtime.scope,
                                  job['gen'], base=None,
                                  claim_digest=job['claim_digest'],
                                  claim_epoch=job['claim_epoch'])
                self.assertEqual(publish_from_source(
                    runtime.index, str(self.source), runtime.scope, None,
                    job['gen'], job['claim'], claim_digest=job['claim_digest'],
                    claim_epoch=job['claim_epoch']), 'published')
            self.assertEqual(runtime.queue.get(job['claim'])['state'], 'leased')
            result = self.sync(runtime)
            self.assertEqual(result['state'], 'indexed')
            self.assertEqual(result['claim'], job['claim'])
            self.assertEqual(runtime.queue.get(job['claim'])['state'], 'acked')
            self.assertEqual(len(runtime.query('"otter"', 5)), 1)

    def test_competing_owner_defers_without_new_claim(self):
        child = self._child('hold')
        try:
            with self.runtime() as runtime:
                self.assertEqual(self.sync(runtime),
                                 {'state': 'deferred', 'generation': None,
                                  'claim': None, 'reason': 'owner_busy'})
                self.assertEqual(runtime.queue.active_for_scope(SCOPE), [])
        finally:
            child.terminate()
            child.wait(timeout=3)
            child.stderr.close()
        with self.runtime() as runtime:
            self.assertEqual(self.sync(runtime)['state'], 'indexed')

    def test_killed_owner_reopens_and_recovers_its_retained_lease(self):
        child = self._child('lease')
        try:
            with self.runtime() as runtime:
                active = runtime.queue.active_for_scope(SCOPE)
                self.assertEqual(len(active), 1)
                claim = active[0]['claim']
                self.assertEqual(active[0]['state'], 'leased')
                self.assertEqual(self.sync(runtime)['reason'], 'owner_busy')
        finally:
            child.kill()  # this exact test-owned process; the kernel drops its lock
            child.wait(timeout=3)
            child.stderr.close()
        with self.runtime() as runtime:
            result = self.sync(runtime)
            self.assertEqual(result['state'], 'indexed')
            self.assertEqual(result['claim'], claim)
            self.assertEqual(runtime.queue.get(claim)['state'], 'acked')
            self.assertEqual(runtime.index.db.execute('PRAGMA integrity_check').fetchone()[0],
                             'ok')

    def test_lock_symlink_and_wrong_mode_fail_closed(self):
        lock = Path(str(self.queue_path) + '.sync.lock')
        lock.symlink_to(self.home / 'other')
        with self.runtime() as runtime:
            with self.assertRaisesRegex(IndexContractError, 'sync_owner_lock_unavailable'):
                self.sync(runtime)
        lock.unlink()
        lock.write_text('')
        lock.chmod(0o666)
        with self.runtime() as runtime:
            with self.assertRaisesRegex(IndexContractError, 'sync_owner_lock_unavailable'):
                self.sync(runtime)

    def test_same_queue_keeps_scopes_separate(self):
        second_source = self.home / 'second'
        second_source.mkdir()
        (second_source / 'other.md').write_text('purple finch', encoding='utf-8')
        with self.runtime() as first, self.runtime('b' * 32, second_source) as second:
            self.assertEqual(self.sync(first)['state'], 'indexed')
            self.assertEqual(self.sync(second)['state'], 'indexed')
            self.assertEqual([r['path'] for r in first.query('"otter"', 5)],
                             ['chapter.md'])
            self.assertEqual([r['path'] for r in second.query('"finch"', 5)],
                             ['other.md'])
            self.assertEqual(first.query('"finch"', 5), [])

    def test_before_publish_failure_blocks_all_index_and_claim_writes(self):
        with self.runtime() as runtime:
            def deny():
                raise RuntimeError('journal_cas_failed')
            with self.assertRaisesRegex(RuntimeError, 'journal_cas_failed'):
                self.sync(runtime, deny)
            self.assertEqual(runtime.queue.active_for_scope(SCOPE), [])
            self.assertEqual(runtime.index.db.execute(
                'SELECT COUNT(*) FROM generations').fetchone()[0], 0)
            marker = self.home / 'durable-intent'
            def mark():
                marker.write_text('marked', encoding='utf-8')
            actual_process = laya_index_sync.process_one
            def checked_process(*args, **kwargs):
                self.assertEqual(marker.read_text(encoding='utf-8'), 'marked')
                return actual_process(*args, **kwargs)
            with patch.object(laya_index_sync, 'process_one', checked_process):
                self.assertEqual(self.sync(runtime, mark)['state'], 'indexed')
            self.assertEqual(marker.read_text(encoding='utf-8'), 'marked')
            marker.unlink()
            self.assertEqual(self.sync(runtime, mark)['state'], 'unchanged')
            self.assertFalse(marker.exists())

    def test_retained_claim_refuses_lease_when_journal_fence_fails(self):
        with self.runtime() as runtime:
            manifest = scan(str(self.source), runtime.scope, runtime.index).manifest
            job = runtime.queue.enqueue_next(runtime.scope, 'g-retained', manifest, None)
            def deny():
                raise RuntimeError('journal_cas_failed')
            with self.assertRaisesRegex(RuntimeError, 'journal_cas_failed'):
                self.sync(runtime, deny)
            self.assertEqual(runtime.queue.get(job['claim'])['state'], 'pending')
            self.assertIsNone(runtime.queue.get(job['claim'])['lease_token'])
            self.assertEqual(runtime.index.db.execute(
                'SELECT COUNT(*) FROM generations').fetchone()[0], 0)
            self.assertEqual(runtime.index.db.execute(
                'SELECT COUNT(*) FROM receipts').fetchone()[0], 0)

    def test_stale_base_replans_then_publishes_new_source(self):
        with self.runtime() as runtime:
            first = self.sync(runtime)
            (self.source / 'chapter.md').write_text('new otter', encoding='utf-8')
            manifest = scan(str(self.source), runtime.scope, runtime.index).manifest
            stale = runtime.queue.enqueue_next(runtime.scope, 'g-stale-base', manifest, None)
            result = self.sync(runtime)
            self.assertEqual(result['state'], 'indexed')
            self.assertNotEqual(result['claim'], stale['claim'])
            self.assertNotEqual(result['generation'], first['generation'])
            self.assertEqual(runtime.queue.get(stale['claim'])['state'], 'replan')
            self.assertEqual(runtime.queue.get(stale['claim'])['outcome'], 'head_moved')

    def test_stale_source_replans_then_publishes_new_manifest(self):
        with self.runtime() as runtime:
            manifest = scan(str(self.source), runtime.scope, runtime.index).manifest
            stale = runtime.queue.enqueue_next(runtime.scope, 'g-stale-source', manifest, None)
            (self.source / 'chapter.md').write_text('new otter', encoding='utf-8')
            result = self.sync(runtime)
            self.assertEqual(result['state'], 'indexed')
            self.assertNotEqual(result['claim'], stale['claim'])
            self.assertEqual(runtime.queue.get(stale['claim'])['state'], 'replan')
            self.assertEqual(runtime.queue.get(stale['claim'])['outcome'], 'index_stale')

    def test_backlog_makes_bounded_progress(self):
        with self.runtime() as runtime:
            manifest = scan(str(self.source), runtime.scope, runtime.index).manifest
            jobs = [runtime.queue.enqueue_next(runtime.scope, 'g-shared', manifest, None)
                    for _ in range(65)]
            first = self.sync(runtime)
            self.assertEqual(first['state'], 'deferred')
            self.assertEqual(first['reason'], 'claim_backlog')
            self.assertEqual(len(runtime.queue.active_for_scope(SCOPE)), 1)
            second = self.sync(runtime)
            self.assertEqual(second['state'], 'unchanged')
            self.assertEqual(second['reason'], 'intent_fenced')
            self.assertEqual(runtime.queue.active_for_scope(SCOPE), [])
            self.assertEqual(runtime.queue.get(jobs[0]['claim'])['state'], 'acked')

    def test_one_ordered_fence_covers_retained_and_fresh_claims(self):
        with self.runtime() as runtime:
            manifest = scan(str(self.source), runtime.scope, runtime.index).manifest
            runtime.queue.enqueue_next(runtime.scope, 'g-old', manifest, None)
            (self.source / 'chapter.md').write_text('blue otter', encoding='utf-8')
            calls = []
            real_enqueue = runtime.queue.enqueue_next
            real_process = laya_index_sync.process_one
            def checked_enqueue(*args, **kwargs):
                calls.append('enqueue')
                return real_enqueue(*args, **kwargs)
            def checked_process(*args, **kwargs):
                calls.append('process')
                return real_process(*args, **kwargs)
            with patch.object(runtime.queue, 'enqueue_next', checked_enqueue), \
                 patch.object(laya_index_sync, 'process_one', checked_process):
                self.assertEqual(self.sync(runtime, lambda: calls.append('fence'))['state'],
                                 'indexed')
            self.assertEqual(calls, ['fence', 'process', 'enqueue', 'process'])

    def test_replanned_retained_claim_reports_fenced_unchanged(self):
        with self.runtime() as runtime:
            self.sync(runtime)
            manifest = scan(str(self.source), runtime.scope, runtime.index).manifest
            job = runtime.queue.enqueue_next(runtime.scope, 'g-old-base', manifest, None)
            marks = []
            result = self.sync(runtime, lambda: marks.append('fenced'))
            self.assertEqual(result['state'], 'unchanged')
            self.assertEqual(result['reason'], 'intent_fenced')
            self.assertEqual(marks, ['fenced'])
            self.assertEqual(runtime.queue.get(job['claim'])['state'], 'replan')

    def test_product_default_without_precomputed_batch_fails_before_write(self):
        with self.runtime() as runtime:
            marks = []
            result = sync_full(runtime, before_publish=lambda: marks.append('marked'))
            self.assertEqual(result['state'], 'deferred')
            self.assertEqual(result['reason'], 'vector_provider_unavailable')
            self.assertEqual(marks, [])
            self.assertEqual(runtime.queue.active_for_scope(SCOPE), [])
            self.assertEqual(runtime.index.db.execute(
                'SELECT COUNT(*) FROM generations').fetchone()[0], 0)

    def test_precomputed_full_batch_seals_publishes_and_serves_exact(self):
        with self.runtime() as runtime:
            batch = self.vector_batch(runtime)
            marks = []
            first = sync_full(runtime, before_publish=lambda: marks.append('marked'),
                              vector_batch=batch)
            self.assertEqual(first['state'], 'indexed')
            self.assertEqual(marks, ['marked'])
            self.assertEqual(runtime.queue.get(first['claim'])['state'], 'acked')
            self.assertEqual(runtime.index.db.execute(
                'SELECT COUNT(*) FROM gen_vector_seals').fetchone()[0], 1)
            token, _ = runtime.index.pin(runtime.scope)
            try:
                found = search_vector_verified(runtime.index, runtime.source_root,
                                               token, [1.0, 0.0],
                                               model_id=batch['model_id'],
                                               embedding_fingerprint=batch['embedding_fingerprint'],
                                               dimension=batch['dimension'], limit=5)
                self.assertEqual([row['path'] for row in found], ['chapter.md'])
            finally:
                runtime.index.release(token)
            self.assertEqual(sync_full(runtime,
                before_publish=lambda: marks.append('again'), vector_batch=batch)['state'],
                'unchanged')
            self.assertEqual(marks, ['marked'])

    def test_incomplete_or_stale_precomputed_batch_cannot_mark_or_enqueue(self):
        with self.runtime() as runtime:
            batch = self.vector_batch(runtime)
            incomplete = {**batch, 'chunks': []}
            with self.assertRaisesRegex(IndexContractError, 'vector_coverage_incomplete'):
                sync_full(runtime, before_publish=lambda: self.fail('marked'),
                          vector_batch=incomplete)
            stale = {**batch, 'source_manifest': '0' * 64}
            with self.assertRaisesRegex(IndexContractError, 'vector_batch_stale'):
                sync_full(runtime, before_publish=lambda: self.fail('marked'),
                          vector_batch=stale)
            self.assertEqual(runtime.queue.active_for_scope(SCOPE), [])
            self.assertEqual(runtime.index.db.execute(
                'SELECT COUNT(*) FROM generations').fetchone()[0], 0)

    # RQA-RACE-01 Part B on the selected engine: an old attempt that accepted
    # the feature revision finishes only after main is back on disk.
    def checkout(self, revision):
        (self.source / 'chapter.md').unlink(missing_ok=True)
        (self.source / 'alpha.md').write_text(
            'alpha qzalphafeature' if revision == 'feature' else 'alpha qzalphamain',
            encoding='utf-8')
        feature = self.source / 'feature-only.md'
        if revision == 'feature':
            feature.write_text('marker qzfeatureonly', encoding='utf-8')
        else:
            feature.unlink(missing_ok=True)
        if revision == 'main-newer':
            (self.source / 'beta.md').write_text('beta qzbetav2', encoding='utf-8')

    def assert_serves_only(self, runtime, batch, paths):
        token, _ = runtime.index.pin(runtime.scope)
        try:
            for term in ('qzalphafeature', 'qzfeatureonly'):
                self.assertEqual(search_verified(runtime.index, runtime.source_root,
                                                 token, term, 5), [])
            found = search_vector_verified(runtime.index, runtime.source_root, token,
                                           [1.0, 0.0], model_id=batch['model_id'],
                                           embedding_fingerprint=batch['embedding_fingerprint'],
                                           dimension=batch['dimension'], limit=5)
            self.assertEqual(sorted(row['path'] for row in found), paths)
        finally:
            runtime.index.release(token)

    def test_late_feature_attempt_after_switch_back_never_publishes(self):
        self.checkout('main')
        with self.runtime() as runtime:
            first = sync_full(runtime, before_publish=lambda: None,
                              vector_batch=self.vector_batch(runtime))
            self.assertEqual(first['state'], 'indexed')
            self.checkout('feature')
            old_batch = prepare_candidate_batch_off_hook(runtime, self.FakeChunkProvider())
            # The old worker holds here; the user switches back to main.
            self.checkout('main')
            marks = []
            with self.assertRaisesRegex(IndexContractError, 'vector_batch_stale'):
                sync_full(runtime, before_publish=lambda: marks.append('old'),
                          vector_batch=old_batch)
            self.assertEqual(marks, [])
            self.assertEqual(runtime.queue.active_for_scope(SCOPE), [])
            self.assertEqual(runtime.index.db.execute(
                'SELECT COUNT(*) FROM generations').fetchone()[0], 1)
            fresh = self.vector_batch(runtime)
            self.assertEqual(sync_full(runtime, before_publish=lambda: None,
                                       vector_batch=fresh),
                             {'state': 'unchanged', 'generation': first['generation'],
                              'claim': None})
            self.assert_serves_only(runtime, fresh, ['alpha.md'])

    def test_dead_feature_worker_claim_and_generation_cannot_follow_newer_publish(self):
        self.checkout('main')
        with self.runtime() as runtime:
            first = sync_full(runtime, before_publish=lambda: None,
                              vector_batch=self.vector_batch(runtime))
            self.checkout('feature')
            source = scan(runtime.source_root, runtime.scope, runtime.index)
            old = runtime.queue.enqueue_next(runtime.scope, 'g-old-feature',
                                             source.manifest, first['generation'])
            leased = runtime.queue.lease(old['claim'])
            runtime.index.stage(old['gen'], runtime.scope, source.documents(),
                                base=first['generation'], source_manifest=source.manifest,
                                claim_digest=leased['claim_digest'],
                                claim_epoch=leased['claim_epoch'])
            # The old owner dies holding its lease; a newer revision lands.
            self.checkout('main-newer')
            fresh = self.vector_batch(runtime)
            newer = sync_full(runtime, before_publish=lambda: None, vector_batch=fresh)
            self.assertEqual(newer['state'], 'indexed')
            self.assertNotEqual(newer['generation'], old['gen'])
            self.assertEqual(runtime.queue.get(old['claim'])['state'], 'replan')
            self.assertIsNone(runtime.index.db.execute(
                'SELECT 1 FROM receipts WHERE claim=?', (old['claim'],)).fetchone())
            args = dict(claim_digest=leased['claim_digest'],
                        claim_epoch=leased['claim_epoch'])
            # Late completion against the newer source, or against a restored
            # feature tree with an outdated base, never moves the head.
            self.assertEqual(publish_from_source(runtime.index, runtime.source_root,
                                                 runtime.scope, first['generation'],
                                                 old['gen'], old['claim'], **args),
                             'index_stale')
            self.checkout('feature')
            (self.source / 'beta.md').unlink()
            self.assertEqual(publish_from_source(runtime.index, runtime.source_root,
                                                 runtime.scope, first['generation'],
                                                 old['gen'], old['claim'], **args),
                             'stale')
            self.checkout('main-newer')
            self.assertEqual(runtime.index.db.execute(
                'SELECT gen FROM head WHERE scope=?', (SCOPE,)).fetchone()[0],
                newer['generation'])
            self.assert_serves_only(runtime, fresh, ['alpha.md', 'beta.md'])

    def test_precomputed_batch_must_match_sealed_profile_on_noop(self):
        with self.runtime() as runtime:
            batch = self.vector_batch(runtime)
            self.assertEqual(sync_full(runtime, before_publish=lambda: None,
                                       vector_batch=batch)['state'], 'indexed')
            wrong = self.vector_batch(runtime, fingerprint='e' * 64)
            with self.assertRaisesRegex(IndexContractError, 'vector_profile_mismatch'):
                sync_full(runtime, before_publish=lambda: self.fail('marked'),
                          vector_batch=wrong)


if __name__ == '__main__':
    unittest.main()

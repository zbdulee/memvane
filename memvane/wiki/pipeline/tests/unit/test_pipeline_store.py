"""Unit tests for the ``wiki-pipeline.sqlite3`` single ledger."""
from __future__ import annotations

import os
import sqlite3

from memvane.wiki.pipeline import api as pipeline
from memvane.wiki.pipeline.types import (
    PIPELINE_APP_ID,
    PIPELINE_USER_VERSION,
    STAGE_QUOTA_BYTES,
    PipelineError,
)
from tests.support.case import FixtureCase
from tests.support.project import make_project


class PipelineStoreTests(FixtureCase):
    def setUp(self):
        super().setUp()
        self.project = make_project(self.home, git=False)

    def test_schema_app_id_and_readonly_query_uri(self):
        with pipeline.open_pipeline(self.project.root) as store:
            store.meta_set('schemaNote', 'v2')
            self.assertEqual(store.meta_get('schemaNote'), 'v2')
            app_id = store._conn.execute('PRAGMA application_id').fetchone()[0]
            user_ver = store._conn.execute('PRAGMA user_version').fetchone()[0]
            self.assertEqual((app_id, user_ver), (PIPELINE_APP_ID, PIPELINE_USER_VERSION))
            self.assertNotIn('.memvane/wiki', str(store.db_path))

        with pipeline.open_pipeline(self.project.root, readonly=True) as ro:
            self.assertEqual(ro.meta_get('schemaNote'), 'v2')
            with self.assertRaises(PipelineError):
                ro.meta_set('blocked', '1')
            with self.assertRaises(sqlite3.OperationalError):
                ro._conn.execute("INSERT INTO pipeline_meta(key, value) VALUES('x', 'y')")

    def test_staged_holds_exceeding_1024_stay_target_isolated_without_limit(self):
        with pipeline.open_pipeline(self.project.root) as store:
            for i in range(1100):
                store.insert_work(f'{i:032x}', 'verify-queue.jsonl', 'hold', 'held', '{"i":%d}' % i,
                                  raw_blob=b'raw', image_blob=b'blob', charged_bytes=7,
                                  target_path=f'topics/card-{i:04d}.md')
            self.assertEqual(len(store.list_works('verify-queue.jsonl', 'hold')), 1100)
            hit = store.works_for_target('topics/card-1099.md')
            self.assertEqual([h['stage_id'] for h in hit], [f'{1099:032x}'])
            self.assertEqual(store.staged_quota_bytes(), 1100 * 7)
            with self.assertRaisesRegex(PipelineError, 'stage_id_collision'):
                store.insert_work(f'{0:032x}', 'verify-queue.jsonl', 'hold', 'held', '{}',
                                  raw_blob=b'', image_blob=None)

    def test_staged_64mib_quota_enforced_and_reclaimed_on_delete(self):
        from memvane.wiki.pipeline.types import StageQuotaExceeded
        mib = 1024 * 1024
        with pipeline.open_pipeline(self.project.root) as store:
            small_quota = 2 * mib
            self.assertEqual(STAGE_QUOTA_BYTES, 64 * mib)
            for name in ('s1', 's2'):
                store.insert_work(name, 'q', 'hold', 'held', '{}', raw_blob=b'x', image_blob=None,
                                  charged_bytes=mib, quota_bytes=small_quota)
            with self.assertRaises(StageQuotaExceeded):
                store.insert_work('s3', 'q', 'hold', 'held', '{}', raw_blob=b'x', image_blob=None,
                                  charged_bytes=1, quota_bytes=small_quota)
            store.insert_work('own', 'q', 'hold', 'held', '{}', raw_blob=b'x', image_blob=None,
                              charged_bytes=0, quota_bytes=small_quota)
            self.assertTrue(store.delete_work('s1', '{}'))
            store.insert_work('s3', 'q', 'hold', 'held', '{}', raw_blob=b'x', image_blob=None,
                              charged_bytes=mib, quota_bytes=small_quota)
            self.assertFalse(store.cas_work('s3', '{"stale":1}', '{"x":1}', 'held'))
            self.assertTrue(store.cas_work('s3', '{}', '{"x":1}', 'finished', closed=True))
            self.assertEqual(store.get_work('s3')['closed'], 1)

    def test_refresh_compile_verify_dead_pid_recovery_audit_cooldown_and_fence(self):
        with pipeline.open_pipeline(self.project.root) as store:
            store.record_refresh('e1', 'docs/a.md', '2026-11-26T00:00:00Z', 'claude')
            self.assertEqual(len(store.unresolved_refreshes('docs/a.md')), 1)
            self.assertTrue(store.settle_refresh('e1', 'abc', 'failed_resolve', 100.0))
            self.assertEqual(len(store.unresolved_refreshes('docs/a.md')), 1)
            self.assertEqual(store.resolve_refreshes_for_source('docs/a.md', 200.0), 1)
            self.assertEqual(store.unresolved_refreshes('docs/a.md'), [])

            store.enqueue_compile_job('c1', 'docs', 'docs/a.md', 'post_tool_source', 'claude', {'v': 1})
            store.enqueue_compile_job('c2', 'docs', 'docs/a.md', 'post_tool_source', 'claude', {'v': 2})
            self.assertEqual(store.count_compile_jobs(state='queued'), 1)
            claimed = store.claim_compile_jobs(5, os.getpid(), 'tok-1')
            self.assertEqual([c['job_id'] for c in claimed], ['c2'])

            with self.assertRaises(AssertionError) if False else self.subTest('dead-pid'):
                orig = os.environ.get('QMD_FORCE_DEAD_PIDS')
                try:
                    os.environ['QMD_FORCE_DEAD_PIDS'] = str(os.getpid())
                    rec = store.recover_dead_compile_claims(max_attempts=3)
                    self.assertEqual(rec, {'requeued': 1, 'deferred': 0})
                finally:
                    if orig is None:
                        os.environ.pop('QMD_FORCE_DEAD_PIDS', None)
                    else:
                        os.environ['QMD_FORCE_DEAD_PIDS'] = orig

            store.enqueue_verify_job('v-old', 'topics/old.md', 'h1', {'n': 1})
            store.enqueue_verify_job('v-p1', 'topics/p1.md', 'h2', {'n': 2})
            store.enqueue_verify_job('v-p2', 'topics/p2.md', 'h3', {'n': 3})
            vclaimed = store.claim_verify_jobs(
                2, os.getpid(), 'vtok', priority_targets=['topics/p1.md', 'topics/p2.md'], reserve_backlog=True,
            )
            self.assertEqual([v['job_id'] for v in vclaimed], ['v-p1', 'v-old'])

            store.append_audit('verify_log', {'action': 'created'}, target_path='topics/p1.md')
            self.assertEqual(len(store.query_audit('verify_log', target_path='topics/p1.md')), 1)
            store.set_cooldown('compile', 'claude', 999.0, {'reason': 'limit'})
            self.assertEqual(store.get_cooldown('compile', 'claude')['until_ts'], 999.0)
            self.assertTrue(store.clear_cooldown('compile', 'claude'))
            store.set_fence('global_publish', 'opaque', {'n': 1})
            self.assertEqual(store.get_fence('global_publish')['reason'], 'opaque')
            self.assertTrue(store.clear_fence('global_publish'))


class DecisionLedgerTests(FixtureCase):
    def setUp(self):
        super().setUp()
        self.project = make_project(self.home, git=False)

    def test_generations_match_by_any_identity_in_append_order(self):
        rows = [{'targetPath': 'concepts/a.md', 'sourceHash': 'h1', 'canonicalKey': 'k1', 'action': 'created'},
                {'targetPath': 'concepts/b.md', 'sourceHash': 'h1', 'action': 'created'},
                {'targetPath': 'concepts/a.md', 'sourceHash': 'h2', 'action': 'verify-deleted'}]
        with pipeline.open_pipeline(self.project.root) as store:
            for row in rows:
                store.append_generation(row)
            self.assertIsNone(store.append_generation(rows[0], origin_key='o1') and None)
            self.assertIsNone(store.append_generation(rows[0], origin_key='o1'))
            self.assertEqual(store.generations(target_path='concepts/a.md'), [rows[0], rows[2], rows[0]])
            self.assertEqual(store.generations(source_hash='h1'), [rows[0], rows[1], rows[0]])
            self.assertEqual(store.latest_generation('concepts/a.md'), rows[0])
            self.assertEqual(store.generations(), [])

    def test_suppressions_keep_the_last_body_hash_per_key(self):
        with pipeline.open_pipeline(self.project.root) as store:
            store.set_suppression('verify', 'docs/a.md', 'b1', {'n': 1})
            store.set_suppression('verify', 'docs/a.md', 'b2', {'n': 2})
            store.set_suppression('dedup', 'a\nb', 'x:y', {})
            self.assertEqual(store.suppressions('verify'), {'docs/a.md': 'b2'})
            self.assertEqual(store.get_suppression('verify', 'docs/a.md'), {'bodyHash': 'b2', 'payload': {'n': 2}})
            with self.assertRaises(PipelineError):
                store.set_suppression('tombstone', 'docs/a.md', 'b', {})

    def test_dedup_pairs_queue_once_until_done(self):
        with pipeline.open_pipeline(self.project.root) as store:
            self.assertTrue(store.enqueue_dedup_pair('b.md', 'a.md', 0.95))
            self.assertFalse(store.enqueue_dedup_pair('a.md', 'b.md', 0.97))
            pair, = store.dedup_pairs()
            self.assertEqual((pair['pageA'], pair['pageB']), ('b.md', 'a.md'))
            self.assertTrue(store.set_dedup_pair_state(pair['pairKey'], 'done'))
            self.assertEqual(store.dedup_pairs(), [])
            self.assertTrue(store.enqueue_dedup_pair('a.md', 'b.md', 0.91))
            self.assertEqual(len(store.dedup_pairs()), 1)


class JobQueueTests(FixtureCase):
    def setUp(self):
        super().setUp()
        self.project = make_project(self.home, git=False)

    def job(self, path, **extra):
        return {'source': {'collection': 'docs', 'path': path}, 'trigger': 'post_tool_source',
                'engine': 'claude', **extra}

    def test_claim_all_then_return_the_unfinished_rows_ahead_of_newer_ones(self):
        with pipeline.open_pipeline(self.project.root) as store:
            store.append_jobs('source', [self.job('a.md'), self.job('b.md'), self.job('c.md')])
            claimed = store.claim_jobs('source', os.getpid(), 'tok')
            self.assertEqual([r['payload']['source']['path'] for r in claimed], ['a.md', 'b.md', 'c.md'])
            self.assertEqual(store.claim_jobs('source', os.getpid(), 'other'), [])
            store.append_jobs('source', [self.job('d.md')])
            store.finish_claim('source', 'tok', [self.job('b.md', partialCompile=True)])
            queued = store.queued_jobs('source')
            self.assertEqual([r['payload']['source']['path'] for r in queued], ['b.md', 'd.md'])
            self.assertTrue(queued[0]['payload']['partialCompile'])
            self.assertEqual(store.queued_jobs('source', states=('claimed',)), [])

    def test_plan_jobs_replaces_and_appends_in_one_transaction(self):
        with pipeline.open_pipeline(self.project.root) as store:
            store.append_jobs('source', [self.job('a.md', rev='1'), self.job('b.md')])
            def planner(rows):
                old = [r['jobId'] for r in rows if r['payload']['source']['path'] == 'a.md']
                return old, [self.job('a.md', rev='2')], {'queued': 1}
            self.assertEqual(store.plan_jobs('source', planner), {'queued': 1})
            self.assertEqual([(r['payload']['source']['path'], r['payload'].get('rev'))
                              for r in store.queued_jobs('source')], [('b.md', None), ('a.md', '2')])
            with self.assertRaises(RuntimeError):
                store.plan_jobs('source', lambda rows: (_ for _ in ()).throw(RuntimeError('planner')))
            self.assertEqual(len(store.queued_jobs('source')), 2)

    def test_a_dead_claimant_returns_rows_and_a_fourth_claim_drops_them(self):
        with pipeline.open_pipeline(self.project.root) as store:
            store.append_jobs('verify', [{'targetPath': 'concepts/a.md', 'sourceHash': 'h'}],
                              pending_blobs=[b'image'])
            for attempt in range(4):
                claimed = store.claim_jobs('verify', 999999, f'tok{attempt}')
                self.assertEqual(store.job_blob('verify', claimed[0]['jobId']), b'image')
                result = store.recover_dead_claims('verify', lambda pid: False)
            self.assertEqual(result['requeued'], 0)
            self.assertEqual([p['targetPath'] for p in result['dropped']], ['concepts/a.md'])
            self.assertEqual(store.queued_jobs('verify'), [])
            store.append_jobs('verify', [{'targetPath': 'concepts/b.md'}])
            store.claim_jobs('verify', os.getpid(), 'live')
            self.assertEqual(store.recover_dead_claims('verify', lambda pid: True), {'requeued': 0, 'dropped': []})

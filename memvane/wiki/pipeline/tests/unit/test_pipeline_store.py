"""Unit tests for the ``wiki-pipeline.sqlite3`` single ledger."""
from __future__ import annotations

import os
import sqlite3

from memvane.wiki.pipeline import api as pipeline
from memvane.wiki.pipeline.types import (
    PIPELINE_APP_ID,
    PIPELINE_USER_VERSION,
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

    def test_a_done_pair_queued_again_joins_the_end(self):
        # N5: resolver indexes follow created_ns; a requeued pair must not move listed ones.
        with pipeline.open_pipeline(self.project.root) as store:
            store.enqueue_dedup_pair('a.md', 'b.md', 0.95)
            store.set_dedup_pair_state(store.dedup_pair_key('a.md', 'b.md'), 'done')
            store.enqueue_dedup_pair('c.md', 'd.md', 0.95)
            listed = [p['pairKey'] for p in store.dedup_pairs()]
            self.assertTrue(store.enqueue_dedup_pair('a.md', 'b.md', 0.93))
            self.assertEqual([p['pairKey'] for p in store.dedup_pairs()],
                             listed + [store.dedup_pair_key('a.md', 'b.md')])



class VerifyQueueTests(FixtureCase):
    def setUp(self):
        super().setUp()
        self.project = make_project(self.home, git=False)

    def test_budget_takes_produced_targets_first_and_reserves_one_backlog_row(self):
        with pipeline.open_pipeline(self.project.root) as store:
            store.append_jobs('verify', [{'targetPath': 'old.md'}, {'targetPath': 'p1.md'}, {'targetPath': 'p2.md'}])
            batch = store.claim_verify_batch(os.getpid(), 'tok', budget=2, produced=['p1.md', 'p2.md'])
            self.assertEqual([r['payload']['targetPath'] for r in batch['claimed']], ['p1.md', 'p2.md', 'old.md'])
            self.assertEqual((batch['fresh'], batch['backlog'], batch['remaining']), (2, 1, 0))

    def test_backlog_beyond_1024_rows_has_no_scan_limit(self):
        with pipeline.open_pipeline(self.project.root) as store:
            store.append_jobs('verify', [{'targetPath': f'c{i}.md'} for i in range(1030)])
            store.append_jobs('verify', [{'targetPath': 'new.md'}])
            batch = store.claim_verify_batch(os.getpid(), 'tok', budget=3, produced=['new.md'])
            self.assertEqual([r['payload']['targetPath'] for r in batch['claimed']], ['new.md', 'c0.md', 'c1.md'])
            self.assertEqual(batch['remaining'], 1028)

    def test_a_newer_row_in_any_state_supersedes_an_older_queued_row(self):
        with pipeline.open_pipeline(self.project.root) as store:
            store.append_jobs('verify', [{'targetPath': 'a.md', 'rev': 1}], pending_blobs=[b'one'])
            store.append_jobs('verify', [{'targetPath': 'a.md', 'rev': 2}])
            first = store.claim_verify_batch(os.getpid(), 'tok', budget=5)
            self.assertEqual([r['payload']['rev'] for r in first['claimed']], [2])
            self.assertEqual(first['superseded'], 1)
            self.assertTrue(store.finish_verify_job(first['claimed'][0]['jobId'], 'tok', 'done', 'verified'))
            # An older row a dead worker held comes back after the newer one finished.
            store.append_jobs('verify', [{'targetPath': 'b.md', 'rev': 1}])
            old = store.claim_verify_batch(999999, 'dead', budget=5)['claimed'][0]
            store.append_jobs('verify', [{'targetPath': 'b.md', 'rev': 2}])
            newer = store.claim_verify_batch(os.getpid(), 'tok2', budget=5)['claimed'][0]
            self.assertTrue(store.finish_verify_job(newer['jobId'], 'tok2', 'done', 'verified'))
            self.assertEqual(store.recover_dead_claims('verify', lambda pid: False)['requeued'], 1)
            again = store.claim_verify_batch(os.getpid(), 'tok3', budget=5)
            self.assertEqual((again['claimed'], again['superseded']), ([], 1))
            self.assertEqual(store.verify_job(old['jobId'])['reason'], 'superseded')
            self.assertEqual(store.job_blob('verify', old['jobId']), None)

    def test_recorded_verdict_survives_a_dead_claim_and_terminal_states_drop_the_image(self):
        with pipeline.open_pipeline(self.project.root) as store:
            store.append_jobs('verify', [{'targetPath': 'a.md'}], pending_blobs=[b'img'])
            row = store.claim_verify_batch(999999, 'dead', budget=1)['claimed'][0]
            self.assertFalse(store.record_verify_verdict(row['jobId'], 'other', {'verdict': 'pass'}))
            self.assertTrue(store.record_verify_verdict(row['jobId'], 'dead', {'verdict': 'pass'}))
            self.assertEqual(store.recover_dead_claims('verify', lambda pid: False)['requeued'], 1)
            row = store.claim_verify_batch(os.getpid(), 'live', budget=1)['claimed'][0]
            self.assertEqual(row['payload']['_verdict'], {'verdict': 'pass'})
            self.assertEqual(row['attempts'], 2)
            self.assertEqual(store.job_blob('verify', row['jobId']), b'img')
            self.assertTrue(store.finish_verify_job(row['jobId'], 'live', 'done', 'verified'))
            self.assertFalse(store.finish_verify_job(row['jobId'], 'live', 'done', 'verified'))
            self.assertEqual(store.job_blob('verify', row['jobId']), None)
            self.assertEqual(store.open_verify_jobs(), [])

    def test_release_returns_an_interrupted_runs_rows(self):
        with pipeline.open_pipeline(self.project.root) as store:
            store.append_jobs('verify', [{'targetPath': 'a.md'}, {'targetPath': 'b.md'}])
            store.claim_verify_batch(os.getpid(), 'tok', budget=2)
            self.assertEqual(store.release_verify_claim('tok'), 2)
            self.assertEqual([r['state'] for r in store.open_verify_jobs()], ['queued', 'queued'])

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
            self.assertEqual([item['payload']['targetPath'] for item in result['exhausted']], ['concepts/a.md'])
            self.assertEqual(len(store.queued_jobs('verify', states=('claimed',))), 1)
            self.assertEqual(store.drop_jobs('verify', [result['exhausted'][0]['jobId']], 'requeue_exhausted'), 1)
            self.assertEqual(store.queued_jobs('verify', states=('queued', 'claimed')), [])
            store.append_jobs('verify', [{'targetPath': 'concepts/b.md'}])
            store.claim_jobs('verify', os.getpid(), 'live')
            self.assertEqual(store.recover_dead_claims('verify', lambda pid: True), {'requeued': 0, 'exhausted': []})

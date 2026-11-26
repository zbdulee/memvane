"""Retention of ledger rows (pipeline/retention.py), one test per state-table cell RT1-RT9."""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from unittest.mock import patch

from memvane.wiki.pipeline import api as pipeline, retention
from memvane.wiki.pipeline.schema import DDL_STATEMENTS
from memvane.wiki.pipeline.types import DAY_NS, PIPELINE_APP_ID, PIPELINE_USER_VERSION
from tests.support.case import FixtureCase
from tests.support.project import make_project

NOW = 2_000_000_000.0
NOW_NS = int(NOW * 10**9)


class Clock:
    """Writers stamp rows with ``time.time_ns``/``time.time``; the test moves this clock."""

    def __init__(self, ns):
        self.ns = ns

    def __enter__(self):
        self._patches = [patch.object(time, 'time_ns', lambda: self.ns),
                         patch.object(time, 'time', lambda: self.ns / 10**9)]
        for item in self._patches:
            item.start()
        return self

    def __exit__(self, *exc):
        for item in self._patches:
            item.stop()


class RetentionTests(FixtureCase):
    def setUp(self):
        super().setUp()
        self.project = make_project(self.home, git=False)
        self.root = self.project.root
        self.wiki = self.project.wiki
        self.wiki.mkdir(parents=True, exist_ok=True)

    def maintain(self, **kwargs):
        roots = kwargs.pop('source_roots', [self.root])
        with pipeline.open_pipeline(self.root) as store:
            return retention.maintain(store, wiki_root=self.wiki, source_roots=roots,
                                      now=lambda: NOW, **kwargs)

    def audit_ids(self, kind):
        with pipeline.open_pipeline(self.root, readonly=True) as store:
            return [row['id'] for row in store.query_audit(kind)]

    def test_rt1_audit_age_limit_per_kind(self):
        old, recent = NOW_NS - 91 * DAY_NS, NOW_NS - 29 * DAY_NS
        for stamp in (old, NOW_NS - 31 * DAY_NS, recent):
            with Clock(stamp):
                pipeline.audit_log(self.root, 'verify_log').append({'n': stamp})
                pipeline.audit_log(self.root, 'dedup_deleted').append({'n': stamp})
        self.maintain()
        self.assertEqual([r['n'] for r in pipeline.log_rows(self.root, 'verify_log')],
                         [NOW_NS - 31 * DAY_NS, recent])
        self.assertEqual([r['n'] for r in pipeline.log_rows(self.root, 'dedup_deleted')], [recent])

    def test_rt2_audit_row_limit_keeps_newest_and_same_second_rows_order_by_id(self):
        limits = {'default': (90 * DAY_NS, 4), 'dedup_deleted': (30 * DAY_NS, 2)}
        with Clock(NOW_NS):  # every row in the same second, same bytes
            for _ in range(7):
                pipeline.audit_log(self.root, 'candidate').append({'same': 1})
                pipeline.audit_log(self.root, 'dedup_deleted').append({'same': 1})
        before = self.audit_ids('candidate')
        with patch.object(retention, 'AUDIT_RETENTION', limits):
            self.maintain()
        self.assertEqual(self.audit_ids('candidate'), before[-4:])
        self.assertEqual(len(self.audit_ids('dedup_deleted')), 2)

    def test_rt3_interrupted_pass_resumes_to_the_same_result(self):
        def seed(root):
            with Clock(NOW_NS - 100 * DAY_NS):
                for i in range(3):
                    pipeline.audit_log(root, 'verify_log').append({'i': i})
            with pipeline.open_pipeline(root) as store, Clock(NOW_NS):
                for i in range(8):
                    store.append_generation({'targetPath': 'concepts/a.md', 'action': 'updated', 'i': i})
        seed(self.root)
        with patch.object(retention.RetentionMixin, 'prune_generations',
                          side_effect=sqlite3.OperationalError('crash')):
            report = self.maintain()
        self.assertEqual(report['stopped'], 'OperationalError')
        self.assertEqual(pipeline.log_rows(self.root, 'verify_log'), [])  # committed before the crash
        with pipeline.open_pipeline(self.root, readonly=True) as store:
            self.assertEqual(len(store.all_generations()), 8)  # untouched
        self.maintain()
        other = make_project(self.home, name='ref', dirname='ref', git=False)
        seed(other.root)
        with pipeline.open_pipeline(other.root) as store:
            retention.maintain(store, wiki_root=other.wiki, source_roots=[other.root], now=lambda: NOW)
        with pipeline.open_pipeline(self.root, readonly=True) as a, \
                pipeline.open_pipeline(other.root, readonly=True) as b:
            self.assertEqual(a.all_generations(), b.all_generations())

    def test_rt4_generations_keep_newest_five_per_card_by_id(self):
        with pipeline.open_pipeline(self.root) as store, Clock(NOW_NS):
            for i in range(7):
                store.append_generation({'targetPath': 'concepts/a.md', 'action': 'updated', 'i': i})
            store.append_generation({'targetPath': 'concepts/b.md', 'action': 'created'})
            for _ in range(6):  # same bytes, same second, no target: grouped by canonical key
                store.append_generation({'canonicalKey': 'k', 'action': 'created'})
            store.append_generation({'action': 'created'})  # no identity at all: kept alone
        self.maintain()
        with pipeline.open_pipeline(self.root, readonly=True) as store:
            rows = store.all_generations()
        self.assertEqual([r['i'] for r in rows if r.get('targetPath') == 'concepts/a.md'], [2, 3, 4, 5, 6])
        self.assertEqual(sum(r.get('targetPath') == 'concepts/b.md' for r in rows), 1)
        self.assertEqual(sum(r.get('canonicalKey') == 'k' for r in rows), 5)
        self.assertEqual(sum(set(r) == {'action'} for r in rows), 1)

    def test_rt5_suppressions_go_only_when_the_source_is_proven_gone(self):
        other_root = self.home / 'raw-b'
        (other_root / 'docs').mkdir(parents=True)
        (other_root / 'docs/second.md').write_text('x')
        self.project.write_note('docs/kept.md', 'x')
        (self.wiki / 'concepts').mkdir(exist_ok=True)
        for name in ('a.md', 'b.md'):
            (self.wiki / 'concepts' / name).write_text('x')
        with Clock(NOW_NS - 400 * DAY_NS):  # age never deletes
            for key in ('docs/kept.md', 'docs/gone.md', 'docs/second.md', '../docs/gone.md', '/abs/gone.md'):
                self.assertTrue(pipeline.set_suppression(self.root, 'verify', key, 'h', {}))
            for key in ('concepts/a.md\nconcepts/b.md', 'concepts/a.md\nconcepts/missing.md'):
                self.assertTrue(pipeline.set_suppression(self.root, 'dedup', key, 'h:h', {}))
        self.assertTrue(pipeline.set_suppression(self.root, 'verify', 'docs/kept.md', 'newer', {}))
        self.maintain(source_roots=[self.root, other_root])
        self.assertEqual(pipeline.suppressions(self.root, 'verify'),
                         {'docs/kept.md': 'newer', 'docs/second.md': 'h', '../docs/gone.md': 'h',
                          '/abs/gone.md': 'h'})
        self.assertEqual(set(pipeline.suppressions(self.root, 'dedup')), {'concepts/a.md\nconcepts/b.md'})

    def test_rt5_unknown_or_rewritten_suppressions_are_kept(self):
        self.assertTrue(pipeline.set_suppression(self.root, 'verify', 'docs/gone.md', 'h', {}))
        # No source roots (no selected route): nothing is proven gone.
        self.maintain(source_roots=[])
        self.assertIn('docs/gone.md', pipeline.suppressions(self.root, 'verify'))
        # Unreadable root: unknown, kept.
        real = Path.lstat

        def denied(path):
            if str(path).endswith('docs/gone.md'):
                raise PermissionError('denied')
            return real(path)
        with patch.object(Path, 'lstat', denied):
            self.maintain()
        self.assertIn('docs/gone.md', pipeline.suppressions(self.root, 'verify'))
        # Rewritten between the batch read and the delete: kept.
        with pipeline.open_pipeline(self.root) as store:
            batch = store.suppression_batch()
            with Clock(time.time_ns() + 1):
                store.set_suppression('verify', 'docs/gone.md', 'h2', {})
            self.assertEqual(store.prune_suppressions(batch, batch), 0)
        self.assertEqual(pipeline.suppressions(self.root, 'verify'), {'docs/gone.md': 'h2'})

    def test_rt5_cursor_walks_suppressions_in_batches(self):
        for i in range(5):
            self.assertTrue(pipeline.set_suppression(self.root, 'verify', f'docs/gone{i}.md', 'h', {}))
        with patch.object(retention, 'RETENTION_BATCH', 2), \
                patch.object(retention.RetentionMixin.suppression_batch, '__defaults__', (2,)), \
                patch.object(retention.RetentionMixin.prune_suppressions, '__defaults__', (2,)):
            counts = []
            for _ in range(3):
                counts.append(self.maintain()['suppressions'])
        self.assertEqual(counts, [2, 2, 1])
        self.assertEqual(pipeline.suppressions(self.root, 'verify'), {})

    def test_rt6_cooldowns_keep_one_active_row_per_key(self):
        with Clock(NOW_NS - 10**9 * 3600):
            self.assertTrue(pipeline.set_cooldown(self.root, 'compile', 60))  # expired by NOW
        with Clock(NOW_NS):
            self.assertTrue(pipeline.set_cooldown(self.root, 'dedup_judge_engine', 600, key='codex'))
            self.assertTrue(pipeline.set_cooldown(self.root, 'dedup_judge_engine', 900, key='codex'))
        self.maintain()
        with pipeline.open_pipeline(self.root, readonly=True) as store:
            self.assertEqual(store.cooldowns('compile'), {})
            self.assertEqual(list(store.cooldowns('dedup_judge_engine')), ['codex'])

    def test_rt7_terminal_rows_go_after_thirty_days_and_open_rows_stay(self):
        old, recent = NOW_NS - 31 * DAY_NS, NOW_NS - DAY_NS
        source = lambda n: {'cwd': str(self.root), 'source': {'collection': 'raw', 'path': n + '.md'}, 'n': n}
        with pipeline.open_pipeline(self.root) as store:
            with Clock(old):
                store.append_jobs('source', [source('dropped-old'), source('queued-old')])
                ids = {r['payload']['n']: r['jobId'] for r in store.queued_jobs('source')}
                store.drop_jobs('source', [ids['dropped-old']], 'requeue_exhausted')
                # The older row of t/q.md is claimed, a newer row is reviewed, then the
                # interrupted claim returns the older row to the queue.
                store.append_jobs('verify', [{'targetPath': 't/q.md', 'n': 'queued-older'}])
                first = store.claim_verify_batch(1, 'tok-a', budget=1)['claimed']
            with Clock(old + 1):
                store.append_jobs('verify', [{'targetPath': 't/q.md', 'n': 'done-newer'}])
                store.append_jobs('verify', [{'targetPath': 't/x.md', 'n': 'done-alone'}])
                for row in store.claim_verify_batch(1, 'tok-b', budget=2)['claimed']:
                    self.assertTrue(store.finish_verify_job(row['jobId'], 'tok-b', 'done', 'kept'))
                self.assertEqual(store.release_verify_claim('tok-a'), len(first))
                store.enqueue_dedup_pair('a.md', 'b.md', 0.9)
                store.set_dedup_pair_state(store.dedup_pair_key('a.md', 'b.md'), 'done')
                store.put_backfill_checkpoint('d' * 64, {'stage': 'claimed'}, b'raw')
            with Clock(recent):
                store.append_jobs('source', [source('dropped-recent')])
                recent_id = [r['jobId'] for r in store.queued_jobs('source') if r['payload']['n'] == 'dropped-recent']
                store.drop_jobs('source', recent_id, 'requeue_exhausted')
        self.maintain()
        with pipeline.open_pipeline(self.root, readonly=True) as store:
            states = ('queued', 'claimed', 'done', 'dropped')
            compile_rows = [r['payload']['n'] for r in store.queued_jobs('source', states=states)]
            verify_rows = [r['payload']['n'] for r in store.queued_jobs('verify', states=states)]
            self.assertEqual(sorted(compile_rows), ['dropped-recent', 'queued-old'])
            # The newer done row still supersedes the older queued row, so it stays.
            self.assertEqual(sorted(verify_rows), ['done-newer', 'queued-older'])
            self.assertEqual(store.dedup_pairs(state='done'), [])
            self.assertIsNone(store.backfill_checkpoint('d' * 64))

    def test_rt7_settled_publish_txns_go_and_publishing_stays(self):
        target = self.wiki / 'concepts/card.md'
        target.parent.mkdir(exist_ok=True)
        with Clock(NOW_NS - 31 * DAY_NS):
            outcome = pipeline.publish_card(self.root, self.wiki, 'concepts/card.md', b'card\n')
        self.assertEqual(outcome.state, 'published')
        with pipeline.open_pipeline(self.root) as store, Clock(NOW_NS - 31 * DAY_NS):
            open_tx = store.begin_publish_txn('concepts/other.md', '.publish-x.tmp', 1, 'a' * 64)
        self.maintain()
        with pipeline.open_pipeline(self.root, readonly=True) as store:
            rows = store._conn.execute('SELECT tx_id, state FROM publish_txns').fetchall()
        self.assertEqual([(r['tx_id'], r['state']) for r in rows], [(open_tx, 'publishing')])

    def test_rt8_new_ledger_is_incremental_and_a_dev_ledger_is_converted_once(self):
        with pipeline.open_pipeline(self.root) as store:
            self.assertEqual(store._conn.execute('PRAGMA auto_vacuum').fetchone()[0], 2)
        self.assertEqual(self.maintain()['vacuum'], 'incremental')
        other = make_project(self.home, name='dev', dirname='dev', git=False)
        db = pipeline.pipeline_db_path(other.root)
        db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db), isolation_level=None)  # a database made before auto_vacuum
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute(f'PRAGMA application_id={PIPELINE_APP_ID}')
        conn.execute(f'PRAGMA user_version={PIPELINE_USER_VERSION}')
        for stmt in DDL_STATEMENTS:
            conn.execute(stmt)
        self.assertEqual(conn.execute('PRAGMA auto_vacuum').fetchone()[0], 0)
        conn.close()
        with pipeline.open_pipeline(other.root) as store:
            self.assertEqual(retention.maintain(store, wiki_root=other.wiki, source_roots=[],
                                                now=lambda: NOW)['vacuum'], 'converted')
            self.assertEqual(store._conn.execute('PRAGMA auto_vacuum').fetchone()[0], 2)

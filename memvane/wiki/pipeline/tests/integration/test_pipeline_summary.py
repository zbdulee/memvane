"""``pipeline_summary``: read-only counts over what the real ledger and inbox writers left.

Each test is one or more cells of the state table in ``summary.py``. Rows come
from the product writers (``append_jobs``/``claim_*``/``finish_*``/``drop_jobs``,
``publish_card``, ``record_pending_refresh`` and the compile worker's inbox
absorption, ``settle_event``/``resolve_pending_event``, ``queue_dedup_pairs``,
``set_suppression``, ``AuditLog.append``). Every cell also proves the summary
created, locked and changed nothing: the project tree is the same before and
after, apart from SQLite's own ``-wal``/``-shm`` companions of an existing ledger.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import sqlite3
from unittest import mock

from memvane.wiki.card import api as card
from memvane.wiki.pipeline import api as pipeline, summary as summary_module
from memvane.wiki.pipeline.types import DB_NAME, JOB_STATES
from tests.support.case import FixtureCase
from tests.support.project import make_project

DEAD_PID = 2 ** 22 + 7


def _tree(root):
    """``relative path -> sha256`` of every file and ``-> 'dir'`` of every folder (no-follow)."""
    out = {}
    for folder, dirs, files in os.walk(root):
        for name in dirs + files:
            path = Path(folder) / name
            rel = str(path.relative_to(root))
            if rel.endswith(('-wal', '-shm')):
                continue
            if path.is_symlink():
                out[rel] = 'link:' + os.readlink(path)
            elif path.is_dir():
                out[rel] = 'dir'
            else:
                out[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


class PipelineSummaryTests(FixtureCase):
    def setUp(self):
        super().setUp()
        self.project = make_project(self.home, git=False)
        self.root = self.project.root
        self.wiki = self.root / '.memvane/wiki'
        self.compile = self.root / '.memvane/compile'

    def summary(self, root=None):
        root = self.root if root is None else root
        before = _tree(root)
        result = pipeline.pipeline_summary(root)
        self.assertEqual(_tree(root), before)
        return result

    def totals(self, result):
        return {name: table['total'] for name, table in result['tables'].items()}

    def test_absent_ledger_and_inbox_create_nothing(self):
        bare = self.home / 'bare'
        bare.mkdir()
        for root in (bare, self.root):
            with self.subTest(root=root.name):
                result = self.summary(root)
                self.assertEqual(result['ledger'], 'absent')
                self.assertEqual(set(self.totals(result).values()), {0})
                self.assertEqual(result['tables']['compileJobs']['byState'], {state: 0 for state in JOB_STATES})
                self.assertEqual(result['inbox'], {'inbox': 'absent', 'pending': 0, 'consuming': 0, 'files': 0,
                                                   'truncated': False})
        self.assertFalse((bare / '.memvane').exists())
        self.assertFalse(self.compile.exists())

    def test_inbox_counts_complete_hook_lines_without_its_lock_or_a_ledger(self):
        for name in ('docs/a.md', 'docs/b.md', 'docs/a.md'):
            self.assertIsNotNone(card.record_pending_refresh(self.root, name, 'claude'))
        inbox = self.compile / 'edit-inbox.jsonl'
        with open(inbox, 'ab') as handle:
            handle.write(b'\n{"schema": "memvane-edit-inbox-v1", "eventId": "par')  # an append still in flight
        result = self.summary()
        self.assertEqual(result['ledger'], 'absent')
        self.assertFalse((self.compile / DB_NAME).exists())
        self.assertEqual(result['inbox'], {'inbox': 'ok', 'pending': 3, 'consuming': 0, 'files': 1,
                                           'truncated': False})
        # The compile worker's absorb renames the inbox aside first; a stop after that leaves it there.
        aside = self.compile / ('edit-inbox.jsonl.consuming-%020d-%s' % (1, 'a1b2c3d4'))
        os.rename(inbox, aside)
        self.assertIsNotNone(card.record_pending_refresh(self.root, 'docs/c.md', 'claude'))
        result = self.summary()
        self.assertEqual((result['inbox']['pending'], result['inbox']['consuming'], result['inbox']['files']),
                         (1, 3, 2))
        os.unlink(aside)  # the partial line is not an event the absorb could read

        card.unresolved_pending_refreshes(self.root, compact=True)
        result = self.summary()
        self.assertEqual(result['inbox']['inbox'], 'absent')
        self.assertEqual(result['ledger'], 'ok')
        self.assertEqual(result['tables']['refreshEvents'],
                         {'total': 1, 'byState': {'pending': 1, 'settled': 0, 'resolved': 0}})

    def test_counts_follow_every_writer_state(self):
        with pipeline.open_pipeline(self.root) as store:
            store.append_jobs('source', [{'source': {'collection': 'docs', 'path': f'docs/{n}.md'}} for n in 'abc'])
            self.assertEqual(len(store.claim_jobs('source', os.getpid(), 'c1')), 3)
            store.finish_claim('source', 'c1', [{'source': {'collection': 'docs', 'path': 'docs/c.md'}}])
            store.append_jobs('source', [{'source': {'collection': 'docs', 'path': 'docs/d.md'}}])
            claimed = store.claim_jobs('source', DEAD_PID, 'c2')
            self.assertEqual(store.drop_jobs('source', [claimed[0]['jobId']], 'exhausted'), 1)

            store.append_jobs('verify', [{'targetPath': 'concepts/a.md', 'sourceHash': 'h1'},
                                         {'targetPath': 'concepts/a.md', 'sourceHash': 'h2'},
                                         {'targetPath': 'concepts/b.md'}], pending_blobs=[b'1', b'2', b'3'])
            batch = store.claim_verify_batch(os.getpid(), 'v1', budget=5)
            self.assertEqual((batch['superseded'], len(batch['claimed'])), (1, 2))
            first = next(row for row in batch['claimed'] if row['payload']['targetPath'] == 'concepts/a.md')
            self.assertTrue(store.finish_verify_job(first['jobId'], 'v1', 'done'))
            self.assertEqual(store.release_verify_claim('v1'), 1)

            events = [{'eventId': f'e{n}', 'ts': f'2026-11-2{n}T00:00:00Z', 'sourcePath': f'docs/{n}.md',
                       'engine': 'claude'} for n in (1, 2, 3)]
            self.assertTrue(store.settle_event(events[0], 'sha', 'no_card', 1.0))
            self.assertTrue(store.resolve_pending_event(events[1], 2.0))
            store.absorb_refresh_events([events[2]], [])

        self.assertEqual(pipeline.queue_dedup_pairs(self.root, [
            {'pageA': 'concepts/a.md', 'pageB': 'concepts/b.md', 'score': 0.9},
            {'pageA': 'concepts/c.md', 'pageB': 'concepts/d.md', 'score': 0.8}]), 2)
        self.assertTrue(pipeline.finish_dedup_pair(self.root, pipeline.dedup_queue(self.root)[0]['pairKey']))
        for body in ('h1', 'h2'):  # the same key keeps its last row
            self.assertTrue(pipeline.set_suppression(self.root, 'verify', 'docs/a.md', body, {'reason': 'x'}))
        self.assertTrue(pipeline.set_suppression(self.root, 'dedup', 'a|b', 'h', {}))
        for kind in ('candidate', 'candidate', 'merge_needed'):
            self.assertTrue(pipeline.audit_log(self.root, kind).append({'targetPath': 'concepts/a.md'}))

        created = pipeline.publish_card(self.root, self.wiki, 'concepts/alpha.md', '# Alpha\n',
                                        audit_kind='verify_log', audit_payload={'action': 'created'},
                                        generation={'targetPath': 'concepts/alpha.md', 'action': 'created'})
        same = pipeline.publish_card(self.root, self.wiki, 'concepts/alpha.md', '# Alpha\n',
                                     expected_target_sha=created.live_sha)
        (self.wiki / 'concepts/alpha.md').write_text('# Edited by a person\n', encoding='utf-8')
        conflict = pipeline.publish_card(self.root, self.wiki, 'concepts/alpha.md', '# Alpha 2\n',
                                         expected_target_sha=created.live_sha)
        self.assertEqual([created.state, same.state, conflict.state],
                         ['published', 'published', 'conflict_user_edited'])
        with pipeline.open_pipeline(self.root) as store:
            # A publish stopped between its ledger row and its rename (the first step of publish_card).
            store.begin_publish_txn('concepts/beta.md', '.publish-x.tmp', 1, 'f' * 64)

        result = self.summary()
        self.assertEqual(result['ledger'], 'ok')
        tables = result['tables']
        self.assertEqual(tables['compileJobs'], {'total': 2, 'byState': {
            'queued': 0, 'claimed': 1, 'done': 0, 'deferred': 0, 'dropped': 1}})
        self.assertEqual(tables['verifyJobs'], {'total': 3, 'byState': {
            'queued': 1, 'claimed': 0, 'done': 2, 'deferred': 0, 'dropped': 0}})
        self.assertEqual(tables['refreshEvents'], {'total': 3, 'byState': {
            'pending': 1, 'settled': 1, 'resolved': 1}})
        self.assertEqual(tables['dedupPairs'], {'total': 2, 'byState': {'queued': 1, 'claimed': 0, 'done': 1}})
        self.assertEqual(tables['suppressions'], {'total': 2, 'byKind': {'verify': 1, 'dedup': 1, 'verify_kept': 0}})
        self.assertEqual(tables['publishTxns'], {'total': 4, 'byState': {
            'publishing': 1, 'published': 2, 'published_user_edited': 0, 'conflict_user_edited': 1,
            'aborted': 0}})
        self.assertEqual(tables['auditEvents']['total'], 4)
        self.assertEqual({k: v for k, v in tables['auditEvents']['byKind'].items() if v},
                         {'candidate': 2, 'merge_needed': 1, 'verify_log': 1})

    def test_open_write_transaction_shows_committed_counts(self):
        with pipeline.open_pipeline(self.root) as store:
            store.append_jobs('source', [{'source': {'collection': 'docs', 'path': 'docs/a.md'}}])
        with pipeline.open_pipeline(self.root) as writer:
            writer._conn.execute('BEGIN IMMEDIATE')
            try:
                writer._insert_job(writer._conn.cursor(), 'source',
                                   {'source': {'collection': 'docs', 'path': 'docs/b.md'}}, 1)
                result = self.summary()
            finally:
                writer._conn.execute('ROLLBACK')
        self.assertEqual(result['ledger'], 'ok')
        self.assertEqual(result['tables']['compileJobs']['byState']['queued'], 1)

    def test_foreign_or_symlinked_ledger_is_unreadable_and_untouched(self):
        self.compile.mkdir(parents=True)
        foreign = self.compile / DB_NAME
        with sqlite3.connect(foreign) as conn:
            conn.execute('CREATE TABLE compile_jobs (state TEXT)')
            conn.execute("INSERT INTO compile_jobs VALUES ('queued')")
        conn.close()
        result = self.summary()
        self.assertEqual((result['ledger'], result['reason']), ('unreadable', 'pipeline_schema_invalid'))
        self.assertEqual(set(self.totals(result).values()), {0})

        foreign.unlink()
        elsewhere = self.home / 'elsewhere'
        elsewhere.mkdir()
        with pipeline.open_pipeline(self.root) as store:
            store.append_jobs('source', [{'source': {'collection': 'docs', 'path': 'docs/a.md'}}])
        os.rename(self.compile, elsewhere / 'compile')
        os.symlink(elsewhere / 'compile', self.compile)
        result = self.summary()
        self.assertEqual((result['ledger'], result['reason']), ('unreadable', 'unsafe_pipeline_path'))
        self.assertEqual((result['inbox']['inbox'], result['inbox']['reason']), ('unreadable', 'unsafe_pipeline_path'))

    def test_unsafe_or_oversized_inbox(self):
        for name in ('docs/a.md', 'docs/b.md', 'docs/c.md'):
            self.assertIsNotNone(card.record_pending_refresh(self.root, name, 'claude'))
        inbox = self.compile / 'edit-inbox.jsonl'
        line = inbox.read_bytes().split(b'\n')[0] + b'\n'
        with mock.patch.object(summary_module, '_INBOX_MAX_BYTES', len(line) * 2 + 1):
            result = self.summary()
        self.assertEqual((result['inbox']['pending'], result['inbox']['truncated']), (2, True))
        target = self.home / 'other-inbox.jsonl'
        os.rename(inbox, target)
        os.symlink(target, inbox)
        result = self.summary()
        self.assertEqual((result['inbox']['inbox'], result['inbox']['reason']), ('unreadable', 'ELOOP'))
        self.assertEqual(result['ledger'], 'absent')

#!/usr/bin/env python3
"""Disposable C2 publish-flow checks; no QMD binary, endpoint, model or user store."""
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

CORE = Path(__file__).resolve().parents[2] / 'core'
sys.path.insert(0, str(CORE))
import shadow_publish_flow as flow


class SimulatedCrash(BaseException):
    pass


def view(revision, generation, **changes):
    row = {'owner': 'project-a', 'incarnation': 'registration-a',
           'worktree': 'worktree-a', 'scopeRevision': 'settings-a',
           'storeGeneration': 'store-a', 'contentRevision': revision,
           'indexGeneration': generation}
    row.update(changes)
    return row


def body(path):
    with sqlite3.connect(path) as db:
        return db.execute('SELECT body FROM documents').fetchone()[0]


def edit(text):
    def apply(index):
        with sqlite3.connect(index) as db:
            db.execute('UPDATE documents SET body=?', (text,))
    return apply


class ShadowPublishFlow(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='qmd-shadow-flow-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source.sqlite'
        self.writer = sqlite3.connect(self.source)
        self.addCleanup(self.writer.close)
        self.writer.execute('PRAGMA journal_mode=WAL')
        self.writer.execute('CREATE TABLE documents(name TEXT PRIMARY KEY, body TEXT)')
        self.writer.execute('INSERT INTO documents VALUES (?,?)', ('story', 'old'))
        self.writer.commit()
        self.old = view('old', 'g-' + '0' * 32)
        self.pointer_path = self.root / 'published-view.json'
        self.pointer_path.write_text(json.dumps(self.old))
        self.pointer_path.chmod(0o600)
        self.acks = []

    def pointer(self):
        return json.loads(self.pointer_path.read_text())

    def test_wal_source_candidate_only_edit_publishes_then_acks(self):
        self.assertTrue(Path(str(self.source) + '-wal').exists())
        result = flow.run(self.root, self.source, self.old, 'new', edit('new'),
                          self.acks.append)
        self.assertEqual(result['outcome'], flow.PUBLISHED)
        self.assertEqual(result['ack'], 'acknowledged')
        self.assertEqual(result['view'], view('new', result['candidate']['generation']))
        self.assertEqual(self.pointer(), result['view'])
        self.assertEqual(self.acks, [result['view']])
        self.assertEqual(body(result['candidate']['index']), 'new')
        self.assertEqual(body(self.source), 'old')

    def test_stale_ab_publication_acks_only_winner(self):
        a = flow.prepare(self.root, self.source, self.old, 'a', edit('a'))
        b = flow.prepare(self.root, self.source, self.old, 'b', edit('b'))
        self.assertEqual((a['outcome'], b['outcome']), (flow.PREPARED, flow.PREPARED))
        won = flow.publish(self.root, a, self.acks.append)
        lost = flow.publish(self.root, b, self.acks.append)
        self.assertEqual(won['outcome'], flow.PUBLISHED)
        self.assertEqual((lost['outcome'], lost['reason']),
                         (flow.REJECTED, 'stale_expected_view'))
        self.assertEqual(lost['current'], a['view'])
        self.assertEqual(self.pointer(), a['view'])
        self.assertEqual(self.acks, [a['view']])
        # A late worker preparing from the old view is turned away early.
        late = flow.run(self.root, self.source, self.old, 'late', edit('late'),
                        self.acks.append)
        self.assertEqual((late['outcome'], late['reason'], late['candidate']),
                         (flow.REJECTED, 'stale_expected_view', None))
        self.assertEqual(self.acks, [a['view']])

    def test_failures_before_publication_never_ack_or_move_pointer(self):
        def broken(index):
            raise sqlite3.OperationalError('no such table: missing')
        failed = flow.run(self.root, self.source, self.old, 'new', broken, self.acks.append)
        self.assertEqual(failed['outcome'], flow.REJECTED)
        self.assertTrue(failed['reason'].startswith('candidate_preparation_failed'))
        refused = flow.run(self.root, self.source, self.old, 'new',
                           lambda index: False, self.acks.append)
        self.assertEqual(refused['reason'], 'candidate_preparation_failed')
        missing = flow.run(self.root, self.root / 'absent.sqlite', self.old, 'new',
                           edit('new'), self.acks.append)
        self.assertTrue(missing['reason'].startswith('candidate_backup_failed'))
        self.assertEqual((self.pointer(), self.acks), (self.old, []))

    def test_changed_seal_is_rejected_without_ack(self):
        prepared = flow.prepare(self.root, self.source, self.old, 'new', edit('new'))
        edit('after-seal')(prepared['candidate']['index'])
        result = flow.publish(self.root, prepared, self.acks.append)
        self.assertEqual((result['outcome'], result['reason']),
                         (flow.REJECTED, 'source_index_changed_during_migration'))
        self.assertEqual((self.pointer(), self.acks), (self.old, []))

    def test_simulated_crash_propagates_without_ack(self):
        def crash(index):
            edit('half')(index)
            raise SimulatedCrash()
        with self.assertRaises(SimulatedCrash):
            flow.run(self.root, self.source, self.old, 'new', crash, self.acks.append)
        self.assertEqual((self.pointer(), self.acks), (self.old, []))
        def crash_in_ack(published):
            raise SimulatedCrash()
        prepared = flow.prepare(self.root, self.source, self.old, 'new', edit('new'))
        with self.assertRaises(SimulatedCrash):
            flow.publish(self.root, prepared, crash_in_ack)
        # Crash after the CAS: pointer advanced, nothing ACKed, orphan left.
        self.assertEqual(self.pointer(), prepared['view'])
        self.assertEqual(self.acks, [])
        self.assertEqual(len(list((self.root / 'candidates').iterdir())), 2)

    def test_ack_failure_keeps_publication_and_retry_sees_superseded(self):
        def failing_ack(published):
            raise OSError('queue unavailable')
        first = flow.run(self.root, self.source, self.old, 'new', edit('new'), failing_ack)
        self.assertEqual((first['outcome'], first['ack']), (flow.PUBLISHED, 'failed'))
        self.assertIn('queue unavailable', first['ackError'])
        self.assertEqual(self.pointer(), first['view'])
        retry = flow.run(self.root, self.source, self.old, 'new', edit('new'),
                         self.acks.append)
        self.assertEqual((retry['outcome'], retry['reason']),
                         (flow.REJECTED, 'stale_expected_view'))
        self.assertEqual(self.acks, [])
        self.assertTrue(flow.is_own_publication(retry['current'], first['view']))
        # Once another publisher advances, the old claim cannot be proven landed.
        other = flow.run(self.root, first['candidate']['index'], first['view'], 'other', edit('other'),
                         lambda published: None)
        self.assertEqual(other['outcome'], flow.PUBLISHED)
        self.assertFalse(flow.is_own_publication(flow.read_pointer(self.root), first['view']))

    def test_next_publication_must_copy_the_current_published_candidate(self):
        first = flow.run(self.root, self.source, self.old, 'one', edit('one'),
                         self.acks.append)
        self.assertEqual(first['outcome'], flow.PUBLISHED)
        wrong = flow.run(self.root, self.source, first['view'], 'two', edit('two'),
                         self.acks.append)
        self.assertEqual((wrong['outcome'], wrong['reason']),
                         (flow.REJECTED, 'published_source_mismatch'))
        self.assertIsNone(wrong['candidate'])
        self.assertEqual(self.pointer(), first['view'])
        def append(index):
            with sqlite3.connect(index) as db:
                db.execute("UPDATE documents SET body=body || '-two'")
        second = flow.run(self.root, first['candidate']['index'], first['view'],
                          'two', append, self.acks.append)
        self.assertEqual(second['outcome'], flow.PUBLISHED)
        self.assertEqual(body(second['candidate']['index']), 'one-two')
        self.assertEqual(body(self.source), 'old')
        self.assertEqual(self.acks, [first['view'], second['view']])

    def test_missing_pointer_is_never_created_and_no_operating_access(self):
        self.pointer_path.unlink()
        result = flow.run(self.root, self.source, self.old, 'new', edit('new'),
                          self.acks.append)
        self.assertEqual((result['outcome'], result['reason']),
                         (flow.REJECTED, 'pointer_missing'))
        self.assertFalse(self.pointer_path.exists())
        self.assertFalse((self.root / 'candidates').exists())
        self.assertEqual(self.acks, [])
        with self.assertRaisesRegex(ValueError, 'invalid_shadow_publish_request'):
            flow.prepare(self.root, self.source, self.old, 'old', edit('x'))
        text = (CORE / 'shadow_publish_flow.py').read_text()
        for token in ('active.json', 'qmd_shared_store', 'qmd_endpoint', 'dirty_queue',
                      'subprocess', 'socket', 'urllib', 'os.environ', 'INDEX_PATH'):
            self.assertNotIn(token, text)
        for module in ('qmd_shared_store', 'qmd_endpoint', 'dirty_queue',
                       'dirty_queue_claim', 'runtime_update'):
            self.assertNotIn(module, sys.modules)


if __name__ == '__main__':
    unittest.main()

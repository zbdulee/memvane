#!/usr/bin/env python3
"""Synthetic A-only Laya Index contract checks. Never accesses operating data."""
from pathlib import Path
import contextlib
import hashlib
import os
import select
import signal
import sqlite3
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

CORE = Path(__file__).resolve().parents[2] / 'core'
sys.path.insert(0, str(CORE))
from laya_index_a import InjectedFault, IndexContractError, LayaIndexA, _digest, _cjk, _qmd_fts_text
import laya_index_a as laya_index_module
import laya_index_source_port as source_port
import laya_index_flow as flow
import laya_index_claim_queue as claim_queue
from laya_index_claim_queue import (ACK, ClaimQueue, ClaimConflict, DEFER,
                                    LEASE_LOST, UnknownDatabase, payload_digest)
from laya_index_vector import LayaVectorIndex, source_evidence

POSIX_ONLY = unittest.skipUnless(os.name == 'posix', 'owned children use POSIX pipes and signals')

# Synthetic owned child. The parent bounds every wait and reaps it in finally;
# SIGALRM is only a backstop if the parent vanishes. "Abnormal exit" means
# process death (os._exit or SIGKILL) on a live OS; nothing models power loss.
CHILD = r'''
import os
import signal
import sqlite3
import sys

signal.alarm(30)
sys.path.insert(0, sys.argv[1])
from laya_index_a import LayaIndexA

db_path, mode, args = sys.argv[2], sys.argv[3], sys.argv[4:]


def say(word):
    sys.stdout.write(word + '\n')
    sys.stdout.flush()


def hear():
    return sys.stdin.readline().strip()


def rows(start, body):
    return [('a-!', body, False)] + [('a-%03d' % i, body, True) for i in range(start, 8)]


class HoldAtCommit:
    """Pause with every write applied but uncommitted; 'crash' exits abnormally."""

    def __init__(self, db):
        self._db = db

    def __getattr__(self, name):
        return getattr(self._db, name)

    def commit(self):
        say('holding')
        os._exit(70 if hear() == 'crash' else 71)


if mode == 'hold':
    db = sqlite3.connect(db_path, timeout=0, isolation_level=None)
    db.execute('BEGIN IMMEDIATE')
    db.execute("INSERT INTO pins VALUES('child-pin','alpha','g0')")
    say('held')
    db.execute('COMMIT' if hear() == 'commit' else 'ROLLBACK')
    db.close()
    say('released')
elif mode == 'publish':
    index = LayaIndexA(db_path)
    say(index.stage('g1', 'alpha', rows(1, 'target shared content')))
    say(index.publish('g0', 'g1', 'claim-1'))
    index.close()
    say('done')
elif mode == 'race':
    gen, claim = args
    index = LayaIndexA(db_path)
    say(index.stage(gen, 'alpha', rows(1, 'target race ' + gen)))
    if hear() != 'go':
        os._exit(3)
    say(index.publish('g0', gen, claim))
    index.close()  # exit without touching the queue: the ACK is lost
elif mode == 'queue-publish-crash':
    from laya_index_claim_queue import ClaimQueue
    import laya_index_source_port as source_port
    queue_path, root_path, claim = args
    queue = ClaimQueue.open(queue_path)
    job = queue.lease(claim)
    if job is None:
        os._exit(4)
    say(job['lease_token'])
    index = LayaIndexA(db_path)
    source_port.stage_from_source(index, root_path, job['scope'], job['gen'],
        base=job['base_gen'], claim_digest=job['claim_digest'],
        claim_epoch=job['claim_epoch'])
    say(source_port.publish_from_source(index, root_path, job['scope'],
        job['base_gen'], job['gen'], claim,
        claim_digest=job['claim_digest'], claim_epoch=job['claim_epoch']))
    os._exit(74)  # committed index receipt; queue ACK never attempted
elif mode in ('crash-stage', 'crash-publish'):
    index = LayaIndexA(db_path)
    index.db = HoldAtCommit(index.db)
    if mode == 'crash-stage':
        index.stage('g1', 'alpha', rows(1, 'target crash content'))
    else:
        index.publish('g0', 'g1', 'claim-1')
    say('committed')
else:
    os._exit(2)
'''


class OwnedChild:
    """A child this test spawned; only it is ever signalled, and it is always reaped."""

    def __init__(self, proc):
        self.proc = proc
        self.buffer = b''

    def line(self, timeout=10.0):
        deadline = time.monotonic() + timeout
        while b'\n' not in self.buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError('owned child timed out; output %r' % self.buffer)
            ready, _, _ = select.select([self.proc.stdout], [], [], remaining)
            if ready:
                chunk = os.read(self.proc.stdout.fileno(), 4096)
                if not chunk:
                    raise AssertionError('owned child exited early; output %r' % self.buffer)
                self.buffer += chunk
        line, _, self.buffer = self.buffer.partition(b'\n')
        return line.decode('utf-8', 'replace')

    def expect(self, word):
        got = self.line()
        if got != word:
            raise AssertionError('owned child said %r, expected %r; rest %r'
                                 % (got, word, self.buffer))

    def send(self, word):
        self.proc.stdin.write((word + '\n').encode('utf-8'))

    def finish(self, timeout=10.0):
        try:
            return self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            raise AssertionError('owned child did not exit in time') from None

    def close(self):
        try:
            if self.proc.poll() is None:
                self.proc.kill()
            self.proc.wait(timeout=10)
        finally:
            for stream in (self.proc.stdin, self.proc.stdout):
                try:
                    stream.close()
                except OSError:
                    pass


def alpha_rows(start=0):
    rows = [('a-%03d' % i, 'target shared content', True) for i in range(start, 8)]
    return [('a-!', 'target shared content', False)] + rows


def hash_body(body):
    return hashlib.sha256(body.encode('utf-8')).hexdigest()


class LayaIndexASlice(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='laya-index-a-')
        self.root = Path(self.temp.name)
        self.db_path = self.root / 'index.sqlite'
        self.extra = []
        self.index = LayaIndexA(self.db_path, create=True)

    def tearDown(self):
        try:
            for index in reversed(self.extra):
                index.close()
            self.index.close()
        finally:
            self.temp.cleanup()

    def connect(self):
        index = LayaIndexA(self.db_path)
        self.extra.append(index)
        return index

    @contextlib.contextmanager
    def owned_child(self, mode, *args):
        proc = subprocess.Popen(
            [sys.executable, '-I', '-B', '-c', CHILD, str(CORE), str(self.db_path), mode, *args],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            cwd=self.root, bufsize=0)
        child = OwnedChild(proc)
        try:
            yield child
        finally:
            child.close()

    def head(self, db):
        return db.execute("SELECT gen FROM head WHERE scope='alpha'").fetchone()

    def table_counts(self, index):
        return {table: index.db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
                for table in ('generations', 'membership', 'content', 'content_fts',
                              'head', 'receipts', 'pins')}

    def publish_base(self):
        self.assertEqual(self.index.stage('g0', 'alpha', alpha_rows()), 'prepared')
        self.assertEqual(self.index.publish(None, 'g0', 'claim-0'), 'published')

    def test_shared_hash_expands_to_documents_before_topk_and_scope_predicate(self):
        self.publish_base()
        self.assertEqual(self.index.stage('other-0', 'other',
                         [('z-%03d' % i, 'target shared content', True)
                          for i in range(200)]), 'prepared')
        self.assertEqual(self.index.publish(None, 'other-0', 'claim-other'), 'published')
        token, gen = self.index.pin('alpha')
        self.assertEqual(gen, 'g0')
        found = self.index.search(token, 'target', 5)
        self.assertEqual([r['path'] for r in found],
                         ['a-000', 'a-001', 'a-002', 'a-003', 'a-004'])
        self.assertEqual(len({r['hash'] for r in found}), 1)
        self.assertTrue(all(r['scope'] == 'alpha' and r['generation'] == 'g0'
                            for r in found))
        # One shared hash, many in-scope documents: hash-level top-K=1 would
        # wrongly emit one document instead of the required five.
        self.assertEqual(self.index.db.execute('SELECT COUNT(*) FROM content').fetchone()[0], 1)
        self.assertEqual(self.index.inspect()['generations'], 2)
        self.index.release(token)

    def test_supplied_vector_scores_expand_before_scope_topk_and_tie(self):
        alpha = alpha_rows() + [('a-002a', 'target other content', True)]
        self.assertEqual(self.index.stage('g0', 'alpha', alpha), 'prepared')
        self.assertEqual(self.index.publish(None, 'g0', 'claim-0'), 'published')
        other = [('z-%03d' % i, 'target distractor %d' % i, True)
                 for i in range(220)]
        self.assertEqual(self.index.stage('other-0', 'other', other), 'prepared')
        self.assertEqual(self.index.publish(None, 'other-0', 'claim-other'), 'published')
        distances = {hash_body(body): i / 1000 for i, (_, body, _) in enumerate(other)}
        distances[hash_body('target shared content')] = 0.5
        distances[hash_body('target other content')] = 0.5
        token, _ = self.index.pin('alpha')
        found = self.index.search_scored(token, distances, 5)
        self.assertEqual([r['path'] for r in found],
                         ['a-000', 'a-001', 'a-002', 'a-002a', 'a-003'])
        self.assertEqual(len(found), 5)
        self.assertTrue(all(r['scope'] == 'alpha' and r['score'] == 0.5 for r in found))
        self.assertEqual(len({r['hash'] for r in found}), 2)
        with self.assertRaises(IndexContractError):
            self.index.search_scored(token, {hash_body('target shared content'): float('nan')}, 5)
        self.index.release(token)

    def test_pin_publish_release_and_non_destructive_gc_plan(self):
        self.publish_base()
        old, old_gen = self.index.pin('alpha')
        self.assertEqual(old_gen, 'g0')
        self.assertEqual(self.index.stage('g1', 'alpha', alpha_rows(1)), 'prepared')
        self.assertEqual(self.index.publish('g0', 'g1', 'claim-1'), 'published')
        fresh, fresh_gen = self.index.pin('alpha')
        self.assertEqual(fresh_gen, 'g1')
        self.assertEqual([r['path'] for r in self.index.search(old, 'target', 5)],
                         ['a-000', 'a-001', 'a-002', 'a-003', 'a-004'])
        self.assertEqual([r['path'] for r in self.index.search(fresh, 'target', 5)],
                         ['a-001', 'a-002', 'a-003', 'a-004', 'a-005'])
        self.assertEqual(self.index.stage('g2', 'alpha', alpha_rows(2)), 'prepared')
        self.assertEqual(self.index.gc_plan()['roots'], ['g0', 'g1', 'g2'])
        with self.assertRaises(InjectedFault):
            self.index.gc_plan(fault_at='after_roots')
        self.index.release(old)
        plan = self.index.gc_plan()
        self.assertEqual(plan['unrooted_candidates'], [])
        self.assertEqual(plan['receipt_referenced'], ['g0'])
        self.assertEqual(plan['base_referenced'], [])
        self.assertIs(plan['deletion_enabled'], False)
        self.assertEqual(self.index.db.execute("SELECT COUNT(*) FROM membership WHERE gen='g0'").fetchone()[0], 9)
        with self.assertRaises(IndexContractError):
            self.index.search(old, 'target', 5)
        self.index.release(fresh)

    def test_stale_writer_receipt_retry_and_fault_rollback(self):
        self.publish_base()
        self.assertEqual(self.index.stage('g1', 'alpha', alpha_rows(1)), 'prepared')
        self.assertEqual(self.index.publish('wrong', 'g1', 'late'), 'stale')
        self.assertIsNone(self.index.receipt('late'))
        for point in ('after_receipt', 'after_head'):
            with self.assertRaises(InjectedFault):
                self.index.publish('g0', 'g1', 'claim-1', fault_at=point)
            self.assertIsNone(self.index.receipt('claim-1'))
            self.assertEqual(self.index.db.execute("SELECT gen FROM head WHERE scope='alpha'").fetchone(), ('g0',))
        # Queue is a separate DB, so ACK cannot share the publish transaction.
        with contextlib.closing(sqlite3.connect(
                self.root / 'queue.sqlite', isolation_level=None)) as queue:
            queue.execute('CREATE TABLE jobs(claim TEXT PRIMARY KEY, acked INT NOT NULL)')
            queue.execute('INSERT INTO jobs VALUES(?,0)', ('claim-1',))
            self.assertEqual(self.index.publish('g0', 'g1', 'claim-1'), 'published')
            self.assertEqual(queue.execute('SELECT acked FROM jobs').fetchone()[0], 0)
            self.assertEqual(self.index.publish('g0', 'g1', 'claim-1'), 'replayed')
            queue.execute('UPDATE jobs SET acked=1 WHERE claim=? AND acked=0', ('claim-1',))
            self.assertEqual(queue.execute('SELECT acked FROM jobs').fetchone()[0], 1)
        self.assertEqual(self.index.receipt('claim-1'), ('alpha', 'g1'))
        self.assertEqual(self.index.db.execute('SELECT COUNT(*) FROM receipts WHERE claim=?',
                                               ('claim-1',)).fetchone()[0], 1)
        self.assertEqual(self.index.stage('g2', 'alpha', alpha_rows(2)), 'prepared')
        with self.assertRaisesRegex(IndexContractError, 'claim_conflict'):
            self.index.publish('g1', 'g2', 'claim-1')
        self.assertEqual(self.index.publish('g0', 'g2', 'late'), 'stale')

    def test_stage_retry_and_rollback_leave_no_orphan_or_partial_content(self):
        with self.assertRaises(InjectedFault):
            self.index.stage('g0', 'alpha', [('a', 'target one', True),
                                           ('b', 'target two', True)], fault_after=1)
        for table in ('generations', 'membership', 'content', 'content_fts'):
            self.assertEqual(self.index.db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0], 0)
        rows = [('a', 'target one', True), ('b', 'target two', True)]
        self.assertEqual(self.index.stage('g0', 'alpha', rows), 'prepared')
        self.assertEqual(self.index.stage('g0', 'alpha', list(reversed(rows))), 'already_staged')
        with self.assertRaises(IndexContractError):
            self.index.stage('g0', 'alpha', [('a', 'changed', True)])
        self.assertEqual(self.index.publish(None, 'g0', 'claim-0'), 'published')
        with self.assertRaises(InjectedFault):
            self.index.pin('alpha', fault_at='after_insert')
        self.assertEqual(self.index.inspect()['pins'], 0)
        token, _ = self.index.pin('alpha')
        self.assertEqual(len(self.index.search(token, 'target', 2)), 2)
        self.index.release(token)
        # Stage is sealed even though GC is disabled.
        with self.assertRaises(sqlite3.IntegrityError):
            self.index.db.execute("UPDATE membership SET eligible=0 WHERE gen='g0'")
        self.assertEqual(self.index.inspect()['generations'], 1)

    def test_base_exception_during_commit_releases_transaction(self):
        class InterruptCommit:
            def __init__(self, db):
                self.db = db

            def __getattr__(self, name):
                return getattr(self.db, name)

            def commit(self):
                raise KeyboardInterrupt('synthetic_interrupt')

        original = self.index.db
        self.index.db = InterruptCommit(original)
        try:
            with self.assertRaisesRegex(KeyboardInterrupt, 'synthetic_interrupt'):
                self.index.stage('g0', 'alpha', alpha_rows())
        finally:
            self.index.db = original
        self.assertFalse(original.in_transaction)
        self.assertEqual(original.execute('SELECT COUNT(*) FROM generations').fetchone()[0], 0)
        self.assertEqual(self.index.stage('g0', 'alpha', alpha_rows()), 'prepared')

    def test_query_error_rolls_back_and_clean_reopen_preserves_view(self):
        self.publish_base()
        token, gen = self.index.pin('alpha')
        with self.assertRaises(sqlite3.OperationalError):
            self.index.search(token, '"', 5)
        self.assertEqual(len(self.index.search(token, 'target', 5)), 5)
        self.index.close()
        self.index = LayaIndexA(self.root / 'index.sqlite')
        self.assertEqual(self.head(self.index.db), ('g0',))
        self.assertEqual(self.index.inspect(),
                         {'generations': 1, 'pins': 1, 'deletion_enabled': False})
        self.assertEqual([r['path'] for r in self.index.search(token, 'target', 5)],
                         ['a-000', 'a-001', 'a-002', 'a-003', 'a-004'])
        self.index.release(token)
        self.index.db.execute('DELETE FROM content_fts')
        with self.assertRaisesRegex(IndexContractError, 'fts_content_mismatch'):
            self.index.inspect()

    @POSIX_ONLY
    def test_old_pin_survives_other_process_publish_and_read_only_gc_plan(self):
        self.publish_base()
        old, old_gen = self.index.pin('alpha')
        self.assertEqual(old_gen, 'g0')
        planner = self.connect()
        snapshot = sqlite3.connect(self.db_path, isolation_level=None)
        try:
            snapshot.execute('BEGIN')
            self.assertEqual(self.head(snapshot), ('g0',))
            with self.owned_child('publish') as writer:
                writer.expect('prepared')
                writer.expect('published')
                writer.expect('done')
                self.assertEqual(writer.finish(), 0)
            # An already-open WAL read snapshot keeps the pre-publish head.
            self.assertEqual(self.head(snapshot), ('g0',))
            changes = planner.db.total_changes
            self.assertEqual(planner.gc_plan(), {'roots': ['g0', 'g1'],
                                                 'unrooted_candidates': [],
                                                 'receipt_referenced': [],
                                                 'base_referenced': [],
                                                 'deletion_enabled': False})
            self.assertEqual(planner.db.total_changes, changes)
            self.assertFalse(planner.db.in_transaction)
            snapshot.execute('COMMIT')
            self.assertEqual(self.head(snapshot), ('g1',))
        finally:
            snapshot.close()
        found = self.index.search(old, 'target', 5)
        self.assertEqual([r['path'] for r in found],
                         ['a-000', 'a-001', 'a-002', 'a-003', 'a-004'])
        self.assertTrue(all(r['generation'] == 'g0' for r in found))
        fresh, fresh_gen = planner.pin('alpha')
        self.assertEqual(fresh_gen, 'g1')
        self.assertEqual([r['path'] for r in planner.search(fresh, 'target', 5)],
                         ['a-001', 'a-002', 'a-003', 'a-004', 'a-005'])
        self.index.release(old)
        plan = planner.gc_plan()
        self.assertEqual(plan['unrooted_candidates'], [])
        self.assertEqual(plan['receipt_referenced'], ['g0'])
        self.assertEqual(plan['base_referenced'], [])
        self.assertIs(plan['deletion_enabled'], False)
        self.assertEqual(planner.db.execute(
            "SELECT COUNT(*) FROM membership WHERE gen='g0'").fetchone()[0], 9)
        planner.release(fresh)
        self.assertEqual(planner.inspect()['generations'], 2)

    @POSIX_ONLY
    def test_two_process_writers_one_cas_winner_then_receipt_redelivery_after_ack_loss(self):
        self.publish_base()
        with contextlib.closing(sqlite3.connect(self.root / 'queue.sqlite',
                                                isolation_level=None)) as queue:
            # Queue is a separate DB, so ACK cannot share the publish transaction.
            queue.execute('CREATE TABLE jobs(claim TEXT PRIMARY KEY, acked INT NOT NULL)')
            queue.executemany('INSERT INTO jobs VALUES(?,0)', [('claim-a',), ('claim-b',)])
            with self.owned_child('race', 'g1a', 'claim-a') as first:
                with self.owned_child('race', 'g1b', 'claim-b') as second:
                    first.expect('prepared')
                    second.expect('prepared')
                    first.send('go')
                    second.send('go')
                    outcomes = {'claim-a': ('g1a', first.line()),
                                'claim-b': ('g1b', second.line())}
                    self.assertEqual((first.finish(), second.finish()), (0, 0))
            self.assertEqual(sorted(result for _, result in outcomes.values()),
                             ['published', 'stale'])
            winner = next(c for c, (_, r) in outcomes.items() if r == 'published')
            loser = next(c for c, (_, r) in outcomes.items() if r == 'stale')
            win_gen, lose_gen = outcomes[winner][0], outcomes[loser][0]
            self.assertEqual(self.head(self.index.db), (win_gen,))
            self.assertEqual(self.index.receipt(winner), ('alpha', win_gen))
            self.assertIsNone(self.index.receipt(loser))
            states = dict(self.index.db.execute('SELECT gen,state FROM generations'))
            self.assertEqual((states[win_gen], states[lose_gen]), ('published', 'prepared'))
            # Both children exited without ACK: the queue redelivers both jobs.
            self.assertEqual(queue.execute(
                'SELECT COUNT(*) FROM jobs WHERE acked=0').fetchone()[0], 2)
            redelivery = self.connect()
            self.assertEqual(redelivery.publish('g0', win_gen, winner), 'replayed')
            queue.execute('UPDATE jobs SET acked=1 WHERE claim=? AND acked=0', (winner,))
            self.assertEqual(redelivery.publish('g0', lose_gen, loser), 'stale')
            self.assertIsNone(redelivery.receipt(loser))
            self.assertEqual(queue.execute(
                'SELECT claim FROM jobs WHERE acked=1').fetchall(), [(winner,)])
        self.assertEqual(self.head(redelivery.db), (win_gen,))
        self.assertEqual(redelivery.db.execute('SELECT COUNT(*) FROM receipts').fetchone()[0], 2)
        self.assertEqual(redelivery.inspect()['generations'], 3)

    @POSIX_ONLY
    def test_owned_writer_lock_gives_short_busy_timeout_then_recovers(self):
        self.publish_base()
        token, _ = self.index.pin('alpha')
        contender = self.connect()  # opened before the lock is taken
        contender.db.execute('PRAGMA busy_timeout=200')
        with self.owned_child('hold') as holder:
            holder.expect('held')
            started = time.monotonic()
            with self.assertRaisesRegex(sqlite3.OperationalError, 'locked'):
                contender.stage('g1', 'alpha', alpha_rows(1))
            self.assertLess(time.monotonic() - started, 5)
            self.assertFalse(contender.db.in_transaction)
            # WAL readers proceed and do not see the holder's uncommitted pin.
            self.assertEqual(len(self.index.search(token, 'target', 5)), 5)
            self.assertEqual(contender.inspect()['pins'], 1)
            holder.send('commit')
            holder.expect('released')
            self.assertEqual(holder.finish(), 0)
        self.assertEqual(contender.stage('g1', 'alpha', alpha_rows(1)), 'prepared')
        self.assertEqual(contender.publish('g0', 'g1', 'claim-1'), 'published')
        self.assertEqual(contender.inspect()['pins'], 2)
        self.assertEqual(contender.gc_plan()['roots'], ['g0', 'g1'])
        self.index.release(token)
        self.index.release('child-pin')
        self.assertEqual(contender.inspect()['pins'], 0)

    @POSIX_ONLY
    def test_owned_child_abnormal_exit_mid_stage_or_publish_then_reopen_verifies(self):
        self.publish_base()
        before = self.table_counts(self.index)
        with self.owned_child('crash-stage') as child:
            child.expect('holding')  # all stage rows written, COMMIT not yet issued
            observer = sqlite3.connect(self.db_path, timeout=0, isolation_level=None)
            try:
                self.assertIsNone(observer.execute(
                    "SELECT 1 FROM generations WHERE gen='g1'").fetchone())
                with self.assertRaisesRegex(sqlite3.OperationalError, 'locked'):
                    observer.execute('BEGIN IMMEDIATE')
            finally:
                observer.close()
            child.send('crash')
            self.assertEqual(child.finish(), 70)
        reopened = self.connect()
        self.assertEqual(reopened.db.execute('PRAGMA integrity_check').fetchall(), [('ok',)])
        self.assertEqual(reopened.inspect(), {'generations': 1, 'pins': 0, 'deletion_enabled': False})
        self.assertEqual(self.table_counts(reopened), before)
        crashed = hash_body('target crash content')
        for table in ('content', 'content_fts'):
            self.assertIsNone(reopened.db.execute(
                'SELECT 1 FROM ' + table + ' WHERE hash=?', (crashed,)).fetchone())
        self.assertEqual(reopened.stage('g1', 'alpha', alpha_rows(1)), 'prepared')
        with self.owned_child('crash-publish') as child:
            child.expect('holding')  # receipt, head and state written, COMMIT not yet issued
            self.assertEqual((self.head(self.index.db), self.index.receipt('claim-1')),
                             (('g0',), None))
            child.proc.kill()  # SIGKILL to this owned child only
            self.assertEqual(child.finish(), -signal.SIGKILL)
        fresh = self.connect()
        self.assertEqual(fresh.db.execute('PRAGMA integrity_check').fetchall(), [('ok',)])
        self.assertEqual(fresh.inspect()['generations'], 2)
        self.assertEqual((self.head(fresh.db), fresh.receipt('claim-1')), (('g0',), None))
        self.assertEqual(fresh.db.execute(
            "SELECT state FROM generations WHERE gen='g1'").fetchone(), ('prepared',))
        self.assertEqual(fresh.publish('g0', 'g1', 'claim-1'), 'published')
        self.assertEqual((self.head(fresh.db), fresh.receipt('claim-1')), (('g1',), ('alpha', 'g1')))
        self.assertEqual(fresh.publish('g0', 'g1', 'claim-1'), 'replayed')
        self.assertEqual(self.index.inspect()['generations'], 2)


    def test_separate_queue_receipt_replay_after_lost_ack_and_stale_lease(self):
        root = self.root / 'queue-source'
        root.mkdir()
        (root / 'one.md').write_text('target one', encoding='utf-8')
        root_path = str(root.resolve())
        manifest = source_port.scan(root_path, 'alpha', self.index).manifest
        job = {'claim':'job-0','scope':'alpha','gen':'g0','source_manifest':manifest,
               'claim_digest':payload_digest('alpha','g0',manifest,1,None),
               'claim_epoch':1,'base_gen':None}
        queue_path = self.root / 'claims.sqlite'
        queue = ClaimQueue.create(queue_path)
        self.assertEqual(queue.enqueue(job), 'pending')
        with self.owned_child('queue-publish-crash', str(queue_path), root_path, 'job-0') as child:
            old_token = child.line()
            child.expect('published')
            self.assertEqual(child.finish(), 74)
        old_lease = queue.get('job-0')
        self.assertEqual(old_lease['lease_token'], old_token)
        self.assertEqual(old_lease['state'], 'leased')
        queue.close()  # separate queue still says leased after child death
        queue = ClaimQueue.open(queue_path)
        try:
            self.assertEqual(flow.process_one(self.index, queue, root_path, 'job-0',
                                              recover=True), 'ack')
            self.assertEqual(queue.get('job-0')['state'], 'acked')
            self.assertEqual(queue.conditional_ack(old_lease, old_lease['lease_token'],
                                                   self.index), LEASE_LOST)
            self.assertEqual(queue.enqueue(job), 'acked')
            self.assertEqual(self.index.db.execute('SELECT COUNT(*) FROM receipts').fetchone()[0], 1)
        finally:
            queue.close()

    def test_queue_flow_replans_changed_source_and_defers_unreadable_receipt(self):
        root = self.root / 'queue-source'
        root.mkdir()
        (root / 'one.md').write_text('target one', encoding='utf-8')
        root_path = str(root.resolve())
        manifest = source_port.scan(root_path, 'alpha', self.index).manifest
        job = {'claim':'job-0','scope':'alpha','gen':'g0','source_manifest':manifest,
               'claim_digest':payload_digest('alpha','g0',manifest,1,None),
               'claim_epoch':1,'base_gen':None}
        queue = ClaimQueue.create(self.root / 'claims.sqlite')
        try:
            self.assertEqual(queue.enqueue(job), 'pending')
            with self.assertRaises(ClaimConflict):
                queue.enqueue({**job, 'gen':'different', 'claim_digest':payload_digest(
                    'alpha','different',manifest,1,None)})
            class Unreadable:
                def receipt_detail(self, claim):
                    raise sqlite3.OperationalError('unavailable')
            self.assertEqual(queue.decide(Unreadable(), job), DEFER)
            (root / 'one.md').write_text('target newer', encoding='utf-8')
            self.assertEqual(flow.process_one(self.index, queue, root_path, 'job-0'), 'replan')
            self.assertEqual(queue.get('job-0')['state'], 'replan')
            self.assertIsNone(self.index.receipt('job-0'))
        finally:
            queue.close()

    def test_flow_defer_releases_lease_and_identical_stage_replays(self):
        root = self.root / 'replay-source'
        root.mkdir()
        (root / 'one.md').write_text('target one', encoding='utf-8')
        root_path = str(root.resolve())
        manifest = source_port.scan(root_path, 'alpha', self.index).manifest
        job = {'claim':'defer-job','scope':'alpha','gen':'g0','source_manifest':manifest,
               'claim_digest':payload_digest('alpha','g0',manifest,1,None),
               'claim_epoch':1,'base_gen':None}
        queue = ClaimQueue.create(self.root / 'replay-claims.sqlite')
        try:
            queue.enqueue(job)
            class Unreadable:
                def receipt_detail(self, claim):
                    raise sqlite3.OperationalError('temporarily locked')
            self.assertEqual(flow.process_one(Unreadable(), queue, root_path,
                                              'defer-job'), 'deferred')
            self.assertEqual(queue.get('defer-job')['state'], 'pending')
            # Simulate process death after immutable stage commit, before publish.
            self.assertEqual(source_port.stage_from_source(self.index, root_path,
                'alpha','g0',base=None,claim_digest=job['claim_digest'],claim_epoch=1), 'prepared')
            self.assertEqual(flow.process_one(self.index,queue,root_path,'defer-job'), 'ack')
            self.assertEqual(queue.get('defer-job')['state'], 'acked')
        finally:
            queue.close()

    def test_distinct_claim_same_generation_and_payload_does_not_retry_forever(self):
        root = self.root / 'duplicate-source'
        root.mkdir()
        (root / 'one.md').write_text('target one', encoding='utf-8')
        root_path = str(root.resolve())
        manifest = source_port.scan(root_path, 'alpha', self.index).manifest
        job = {'claim':'first','scope':'alpha','gen':'g0','source_manifest':manifest,
               'claim_digest':payload_digest('alpha','g0',manifest,1,None),
               'claim_epoch':1,'base_gen':None}
        queue = ClaimQueue.create(self.root / 'duplicate-claims.sqlite')
        try:
            queue.enqueue(job)
            self.assertEqual(flow.process_one(self.index, queue, root_path, 'first'), 'ack')
            queue.enqueue({**job, 'claim':'second'})
            self.assertEqual(flow.process_one(self.index, queue, root_path, 'second'), 'replan')
            self.assertEqual((queue.get('second')['state'], queue.get('second')['outcome']),
                             ('replan', 'head_moved'))
            self.assertIsNone(self.index.receipt('second'))
            self.assertEqual(self.index.receipt('first'), ('alpha', 'g0'))
            self.assertEqual(self.index.db.execute('SELECT COUNT(*) FROM receipts').fetchone()[0], 1)
            self.assertEqual(flow.process_one(self.index, queue, root_path, 'second'), 'unavailable')
        finally:
            queue.close()

    def test_generation_collision_with_different_payload_replans_without_ack(self):
        root = self.root / 'collision-source'
        root.mkdir()
        path = root / 'one.md'
        path.write_text('target one', encoding='utf-8')
        root_path = str(root.resolve())
        first_manifest = source_port.scan(root_path, 'alpha', self.index).manifest
        first = {'claim':'first','scope':'alpha','gen':'g0','source_manifest':first_manifest,
                 'claim_digest':payload_digest('alpha','g0',first_manifest,1,None),
                 'claim_epoch':1,'base_gen':None}
        queue = ClaimQueue.create(self.root / 'collision-claims.sqlite')
        try:
            queue.enqueue(first)
            self.assertEqual(flow.process_one(self.index, queue, root_path, 'first'), 'ack')
            path.write_text('target two', encoding='utf-8')
            second_manifest = source_port.scan(root_path, 'alpha', self.index).manifest
            self.assertNotEqual(second_manifest, first_manifest)
            second = {**first, 'claim':'second', 'source_manifest':second_manifest,
                      'claim_digest':payload_digest('alpha','g0',second_manifest,2,None),
                      'claim_epoch':2}
            queue.enqueue(second)
            self.assertEqual(flow.process_one(self.index, queue, root_path, 'second'), 'replan')
            self.assertEqual((queue.get('second')['state'], queue.get('second')['outcome']),
                             ('replan', 'index_stale'))
            self.assertIsNone(self.index.receipt('second'))
            self.assertEqual(self.index.receipt('first'), ('alpha', 'g0'))
        finally:
            queue.close()

    def test_generation_collision_across_scopes_replans_without_wrong_ack(self):
        root = self.root / 'scope-collision-source'
        root.mkdir()
        (root / 'one.md').write_text('target one', encoding='utf-8')
        root_path = str(root.resolve())
        queue = ClaimQueue.create(self.root / 'scope-collision-claims.sqlite')
        try:
            for scope, claim in (('alpha', 'first'), ('beta', 'second')):
                manifest = source_port.scan(root_path, scope, self.index).manifest
                job = {'claim':claim,'scope':scope,'gen':'g0','source_manifest':manifest,
                       'claim_digest':payload_digest(scope,'g0',manifest,1,None),
                       'claim_epoch':1,'base_gen':None}
                queue.enqueue(job)
                expected = 'ack' if scope == 'alpha' else 'replan'
                self.assertEqual(flow.process_one(self.index, queue, root_path, claim), expected)
            self.assertEqual((queue.get('second')['state'], queue.get('second')['outcome']),
                             ('replan', 'index_stale'))
            self.assertIsNone(self.index.receipt('second'))
            self.assertIsNone(self.index.db.execute(
                "SELECT gen FROM head WHERE scope='beta'").fetchone())
        finally:
            queue.close()

    def test_queue_open_rejects_same_app_id_wrong_schema_without_mutation(self):
        path = self.root / 'forged-queue.sqlite'
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE jobs(claim TEXT PRIMARY KEY, state TEXT)')
            db.execute('PRAGMA application_id=1280921905')
            db.execute('PRAGMA user_version=1')
        before = path.read_bytes()
        with self.assertRaisesRegex(UnknownDatabase, 'queue_schema_unknown'):
            ClaimQueue.open(path)
        self.assertEqual(path.read_bytes(), before)

    def test_queue_open_rejects_redefined_seal_trigger(self):
        path = self.root / 'wrong-trigger-queue.sqlite'
        queue = ClaimQueue.create(path)
        queue.close()
        with sqlite3.connect(path, isolation_level=None) as db:
            db.execute('DROP TRIGGER delete_seal')
            db.execute('CREATE TRIGGER delete_seal BEFORE DELETE ON jobs '
                       'BEGIN SELECT 1; END')
        with self.assertRaisesRegex(UnknownDatabase, 'queue_schema_unknown'):
            ClaimQueue.open(path)

    def test_queue_open_rejects_invalid_payload_digest_and_state(self):
        manifest = 'a' * 64
        fields = ('job', 'alpha', 'g0', manifest,
                  payload_digest('alpha', 'g0', manifest, 1, None), 1, None)
        for suffix, values, reason in (
                ('proof', (*fields[:4], 'b' * 64, *fields[5:], 'pending', None, None),
                 'queue_identity_invalid'),
                ('state', (*fields, 'acked', None, 'published'),
                 'queue_state_invalid')):
            with self.subTest(suffix=suffix):
                path = self.root / ('invalid-' + suffix + '.sqlite')
                queue = ClaimQueue.create(path)
                queue.db.execute('''INSERT INTO jobs
                    (claim,scope,gen,source_manifest,claim_digest,claim_epoch,
                     base_gen,state,lease_token,outcome)
                    VALUES(?,?,?,?,?,?,?,?,?,?)''', values)
                queue.close()
                with self.assertRaisesRegex(UnknownDatabase, reason):
                    ClaimQueue.open(path)

    def test_queue_create_rejects_dangling_sidecar_symlink(self):
        path = self.root / 'sidecar-queue.sqlite'
        sidecar = Path(str(path) + '-wal')
        target = self.root / 'absent-queue-sidecar-target'
        sidecar.symlink_to(target)
        self.assertFalse(sidecar.exists())
        self.assertTrue(sidecar.is_symlink())
        with self.assertRaisesRegex(UnknownDatabase, 'stale_sidecar'):
            ClaimQueue.create(path)
        self.assertFalse(path.exists())
        self.assertTrue(sidecar.is_symlink())
        self.assertFalse(target.exists())

    def test_queue_binding_is_immutable_and_reopens_with_exact_index_identity(self):
        incarnation = self.index.incarnation()
        path = self.root / 'bound-queue.sqlite'
        queue = ClaimQueue.create(path, index_incarnation=incarnation)
        try:
            self.assertEqual(queue.binding(), incarnation)
            for sql in ("UPDATE queue_meta SET value='0' WHERE key='index_incarnation'",
                        "DELETE FROM queue_meta WHERE key='index_incarnation'",
                        "INSERT OR REPLACE INTO queue_meta VALUES('index_incarnation','0')"):
                with self.subTest(sql=sql), self.assertRaises(sqlite3.IntegrityError):
                    queue.db.execute(sql)
            self.assertEqual(queue.binding(), incarnation)
        finally:
            queue.close()
        reopened = ClaimQueue.open(path)
        try:
            self.assertEqual(reopened.binding(), incarnation)
        finally:
            reopened.close()
        unbound = ClaimQueue.create(self.root / 'unbound-queue.sqlite')
        try:
            self.assertIsNone(unbound.binding())
        finally:
            unbound.close()
        with self.assertRaisesRegex(ValueError, 'invalid_index_incarnation'):
            ClaimQueue.create(self.root / 'invalid-binding.sqlite',
                              index_incarnation='not-an-incarnation')
        self.assertFalse((self.root / 'invalid-binding.sqlite').exists())

    def test_queue_v1_reopens_only_as_unbound_legacy_queue(self):
        path = self.root / 'legacy-queue.sqlite'
        with sqlite3.connect(path, isolation_level=None) as db:
            db.execute('BEGIN IMMEDIATE')
            for statement in claim_queue._SCHEMA_V1_SQL:
                db.execute(statement)
            db.execute(f'PRAGMA application_id={claim_queue.APP_ID}')
            db.execute('PRAGMA user_version=1')
            db.commit()
        legacy = ClaimQueue.open(path)
        try:
            self.assertIsNone(legacy.binding())
        finally:
            legacy.close()

    def test_queue_open_rejects_invalid_binding_even_with_expected_objects(self):
        path = self.root / 'corrupt-binding-queue.sqlite'
        queue = ClaimQueue.create(path, index_incarnation=self.index.incarnation())
        queue.db.execute('DROP TRIGGER queue_meta_update_seal')
        queue.db.execute("UPDATE queue_meta SET value='bad' WHERE key='index_incarnation'")
        queue.db.execute(claim_queue._QUEUE_META_UPDATE_SEAL_SQL)
        queue.close()
        with self.assertRaisesRegex(UnknownDatabase, 'queue_binding_invalid'):
            ClaimQueue.open(path)

    def test_source_port_full_manifest_and_verified_body_fail_explicitly_on_change(self):
        root = self.root / 'approved-source'
        root.mkdir()
        (root / 'one.md').write_text('target one', encoding='utf-8')
        (root / 'two.md').write_text('target two', encoding='utf-8')
        root_path = str(root.resolve())
        self.assertEqual(source_port.stage_from_source(self.index, root_path, 'alpha', 'g0',
                         base=None, claim_digest='1' * 64, claim_epoch=1), 'prepared')
        self.assertEqual(source_port.publish_from_source(self.index, root_path, 'alpha', None,
                         'g0', 'source-claim-0', claim_digest='1' * 64,
                         claim_epoch=1), 'published')
        old, _ = self.index.pin('alpha')
        found = source_port.search_verified(self.index, root_path, old, 'target', 5)
        self.assertEqual([(r['path'], r['body']) for r in found],
                         [('one.md', 'target one'), ('two.md', 'target two')])
        self.assertEqual(source_port.stage_from_source(self.index, root_path, 'alpha', 'g1',
                         base='g0', claim_digest='2' * 64, claim_epoch=2), 'prepared')
        (root / 'one.md').write_text('target changed', encoding='utf-8')
        self.assertEqual(source_port.publish_from_source(self.index, root_path, 'alpha', 'g0',
                         'g1', 'source-claim-1', claim_digest='2' * 64,
                         claim_epoch=2), 'index_stale')
        self.assertIsNone(self.index.receipt('source-claim-1'))
        with self.assertRaisesRegex(IndexContractError, 'index_stale'):
            source_port.search_verified(self.index, root_path, old, 'target', 5)
        (root / 'one.md').write_text('target one', encoding='utf-8')
        self.assertEqual(self.index.exclude('alpha', path='one.md'), 'excluded')
        with self.assertRaisesRegex(IndexContractError, 'index_stale'):
            source_port.search_verified(self.index, root_path, old, 'target', 5)
        self.assertEqual(source_port.stage_from_source(self.index, root_path, 'alpha', 'g2',
                         base='g0', claim_digest='3' * 64, claim_epoch=3), 'prepared')
        self.assertEqual(source_port.publish_from_source(self.index, root_path, 'alpha', 'g0',
                         'g2', 'source-claim-2', claim_digest='3' * 64,
                         claim_epoch=3), 'published')
        fresh, _ = self.index.pin('alpha')
        self.assertEqual([r['path'] for r in source_port.search_verified(
                         self.index, root_path, fresh, 'target', 5)], ['two.md'])
        self.index.release(fresh)
        self.index.release(old)
        (root / 'link.md').symlink_to(root / 'two.md')
        with self.assertRaisesRegex(IndexContractError, 'index_stale'):
            source_port.scan(root_path, 'alpha', self.index)

    def _text_only_tree(self):
        root = self.root / 'text-only-source'
        outside = self.root / 'outside-source'
        for rel, body in (('one.md', 'one'), ('notes.txt', 'notes'),
                          ('guide.RST', 'guide'), ('tool.py', 'print(1)'),
                          ('docs/deep.mdx', 'deep'),
                          ('node_modules/pkg/README.md', 'dep'),
                          ('build/out.md', 'built'), ('vendor/v.md', 'vendored'),
                          ('__pycache__/x.md', 'cache'),
                          ('Library/PackageCache/y.md', 'unity cache')):
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding='utf-8')
        (root / 'latin1.txt').write_bytes(b'caf\xe9')
        (root / 'Temp').mkdir()
        (root / 'Temp' / 'broken.md').write_bytes(b'\xff')
        (root / 'assets').mkdir()
        for i in range(40):
            (root / 'assets' / f'blob{i:02d}.bin').write_bytes(b'\xff')
        outside.mkdir()
        (outside / 'linked.md').write_text('linked', encoding='utf-8')
        (root / 'link.py').symlink_to(root / 'tool.py')
        (root / 'dangling.json').symlink_to(root / 'missing.json')
        (root / 'linkdir').symlink_to(outside, target_is_directory=True)
        return root

    def test_source_port_walks_only_text_documents(self):
        root = self._text_only_tree()
        root_path = str(root.resolve())
        # 40 non-text names exceed the lowered bound; only visited directories
        # (root, docs, assets) and text files (5) count toward it.
        with mock.patch.object(source_port, 'MAX_ENTRIES', 10):
            scanned = source_port.scan(root_path, 'alpha', self.index)
        self.assertEqual([f.path for f in scanned.files],
                         ['docs/deep.mdx', 'guide.RST', 'notes.txt', 'one.md'])
        # A non-UTF-8 text file is not a source in a raw scan.
        self.assertEqual(scanned.undecodable, ('latin1.txt',))
        with mock.patch.object(source_port, 'MAX_ENTRIES', 6):
            with self.assertRaisesRegex(IndexContractError, 'index_stale'):
                source_port.scan(root_path, 'alpha', self.index)

    def test_source_port_text_symlink_still_fails_closed(self):
        root = self._text_only_tree()
        root_path = str(root.resolve())
        for name in ('link.md', 'link.txt'):
            link = root / name
            link.symlink_to(root / 'one.md')
            with self.assertRaises(IndexContractError) as caught:
                source_port.scan(root_path, 'alpha', self.index)
            self.assertEqual(str(caught.exception.__cause__), f'symlink: {name}')
            link.unlink()
        os.mkfifo(root / 'pipe.md')
        with self.assertRaises(IndexContractError) as caught:
            source_port.scan(root_path, 'alpha', self.index)
        self.assertEqual(str(caught.exception.__cause__), 'not a regular file: pipe.md')

    def test_source_port_wiki_only_scan_stays_markdown(self):
        wiki = self.root / 'wiki-only'
        wiki.mkdir()
        (wiki / 'card.md').write_text('# Card', encoding='utf-8')
        (wiki / 'notes.txt').write_text('not a card', encoding='utf-8')
        (wiki / 'log.md').write_text('# Auto-context Wiki Log\n\n- event', encoding='utf-8')
        (wiki / 'nested').mkdir()
        (wiki / 'nested' / 'log.md').write_text('# Auto-context Wiki Log\n\nnested',
                                                 encoding='utf-8')
        (wiki / 'nested' / 'extra.rst').write_text('not a card', encoding='utf-8')
        scanned = source_port.scan_collections({'wiki': str(wiki.resolve())}, 'alpha',
                                               self.index, wiki_only=True)
        self.assertEqual([f.path for f in scanned.files],
                         ['wiki/card.md', 'wiki/nested/log.md'])
        (wiki / 'bad.md').write_bytes(b'\xff')
        with self.assertRaises(IndexContractError) as caught:
            source_port.scan_collections({'wiki': str(wiki.resolve())}, 'alpha',
                                         self.index, wiki_only=True)
        self.assertEqual(str(caught.exception.__cause__), 'invalid UTF-8: wiki/bad.md')

    def test_bound_candidate_freezes_base_source_and_claim_receipt(self):
        docs = [('one', 'target one', True)]
        manifest, payload = _digest('alpha', [('one', hash_body('target one'), 1)]), 'b' * 64
        self.assertEqual(self.index.stage('g0', 'alpha', docs, base=None,
                         source_manifest=manifest, claim_digest=payload,
                         claim_epoch=7), 'prepared')
        with self.assertRaisesRegex(IndexContractError, 'claim_conflict'):
            self.index.publish(None, 'g0', 'c0')  # sealed candidate needs proof
        self.assertEqual(self.index.publish(None, 'g0', 'c0', source_manifest=manifest,
                         claim_digest=payload, claim_epoch=7), 'published')
        self.assertEqual(self.index.receipt_detail('c0'),
                         ('alpha', 'g0', manifest, payload, 7))
        self.assertEqual(self.index.publish(None, 'g0', 'c0', source_manifest=manifest,
                         claim_digest=payload, claim_epoch=7), 'replayed')
        with self.assertRaisesRegex(IndexContractError, 'claim_conflict'):
            self.index.publish(None, 'g0', 'c0', source_manifest=manifest,
                               claim_digest='c' * 64, claim_epoch=7)
        token, _ = self.index.pin('alpha')
        self.assertEqual(self.index.view_seal(token)['source_manifest'], manifest)
        self.index.release(token)
        second_manifest = _digest('alpha', [('two', hash_body('target two'), 1)])
        self.assertEqual(self.index.stage('g1', 'alpha', [('two', 'target two', True)],
                         base='g0', source_manifest=second_manifest,
                         claim_digest='e' * 64, claim_epoch=8), 'prepared')
        with self.assertRaisesRegex(IndexContractError, 'claim_conflict'):
            self.index.publish(None, 'g1', 'c1', source_manifest=second_manifest,
                               claim_digest='e' * 64, claim_epoch=8)
        self.assertEqual(self.index.stage('g2', 'alpha', [('three', 'target three', True)]),
                         'prepared')
        self.assertEqual(self.index.publish('g0', 'g2', 'c2'), 'published')
        self.assertEqual(self.index.publish('g0', 'g1', 'c1', source_manifest=second_manifest,
                         claim_digest='e' * 64, claim_epoch=8), 'stale')
        self.assertIsNone(self.index.receipt('c1'))
        self.assertEqual(self.index.exclude('alpha', path='two'), 'excluded')
        self.assertEqual(self.index.publish('g0', 'g1', 'c1', source_manifest=second_manifest,
                         claim_digest='e' * 64, claim_epoch=8), 'index_stale')

    def test_receipt_insert_trigger_rejects_wrong_sealed_or_unsealed_proof(self):
        docs = [('one', 'target one', True)]
        manifest = _digest('alpha', [('one', hash_body('target one'), 1)])
        self.assertEqual(self.index.stage('g0', 'alpha', docs, base=None,
                         source_manifest=manifest, claim_digest='a' * 64,
                         claim_epoch=1), 'prepared')
        self.assertEqual(self.index.publish(None, 'g0', 'good',
                         source_manifest=manifest, claim_digest='a' * 64,
                         claim_epoch=1), 'published')
        for proof in ((manifest, 'b' * 64, 1), (None, None, None)):
            with self.subTest(proof=proof), self.assertRaises(sqlite3.IntegrityError):
                self.index.db.execute('INSERT INTO receipts VALUES(?,?,?,?,?,?)',
                                      ('forged','alpha','g0',*proof))
        self.assertEqual(self.index.stage('g1', 'alpha', [('two','target two',True)]),
                         'prepared')
        self.assertEqual(self.index.publish('g0', 'g1', 'plain'), 'published')
        with self.assertRaises(sqlite3.IntegrityError):
            self.index.db.execute('INSERT INTO receipts VALUES(?,?,?,?,?,?)',
                                  ('forged','alpha','g1',manifest,'a'*64,1))
        self.assertIsNone(self.index.receipt('forged'))

    def test_gc_plan_retains_receipt_and_prepared_candidate_base(self):
        self.publish_base()
        docs = [('future', 'target future', True)]
        manifest = _digest('alpha', [('future', hash_body('target future'), 1)])
        self.assertEqual(self.index.stage('g1','alpha',docs,base='g0',
                         source_manifest=manifest,claim_digest='b'*64,
                         claim_epoch=2), 'prepared')
        self.assertEqual(self.index.stage('g2','alpha',[('new','target new',True)]),
                         'prepared')
        self.assertEqual(self.index.publish('g0','g2','new-head'), 'published')
        plan = self.index.gc_plan()
        self.assertEqual(plan['roots'], ['g0','g1','g2'])
        self.assertEqual(plan['unrooted_candidates'], [])
        self.assertEqual(plan['receipt_referenced'], ['g0'])
        self.assertEqual(plan['base_referenced'], ['g0'])
        self.assertIs(plan['deletion_enabled'], False)

    def test_replace_cannot_rewrite_shared_content_generation_or_receipt(self):
        self.publish_base()
        token, _ = self.index.pin('alpha')
        hash_ = hash_body('target shared content')
        ordinal = self.index.db.execute("SELECT ord FROM gen_index WHERE gen='g0'").fetchone()[0]
        cases = [
            ('content', (hash_, 'poisoned')),
            ('gen_index', (ordinal, 'g0')),
            ('generations', ('g0', 'alpha', 'published', 'bad', 0)),
            ('receipts', ('claim-0', 'alpha', 'g0', None, None, None)),
            ('pins', (token, 'alpha', 'g0')),
        ]
        for table, values in cases:
            placeholders = ','.join('?' for _ in values)
            with self.subTest(table=table):
                with self.assertRaises(sqlite3.IntegrityError):
                    self.index.db.execute('INSERT OR REPLACE INTO ' + table + ' VALUES(' +
                                          placeholders + ')', values)
        self.assertEqual(len(self.index.search(token, 'target', 3)), 3)
        self.assertEqual(self.index.receipt('claim-0'), ('alpha', 'g0'))
        self.index.release(token)

    def test_explicit_v6_create_refuses_unknown_existing_db_without_mutation(self):
        missing = self.root / 'absent.sqlite'
        with self.assertRaisesRegex(IndexContractError, 'database_missing'):
            LayaIndexA(missing)
        self.assertFalse(missing.exists())
        old = self.root / 'old.sqlite'
        with sqlite3.connect(old) as db:
            db.execute('CREATE TABLE sentinel(value TEXT)')
            db.execute("INSERT INTO sentinel VALUES('keep')")
        original = old.read_bytes()
        with self.assertRaisesRegex(IndexContractError, 'schema_version_unknown'):
            LayaIndexA(old)
        self.assertEqual(old.read_bytes(), original)
        with self.assertRaisesRegex(IndexContractError, 'database_already_exists'):
            LayaIndexA(old, create=True)
        with self.assertRaisesRegex(IndexContractError, 'database_already_exists'):
            LayaIndexA(self.db_path, create=True)
        self.assertEqual(self.index.db.execute('PRAGMA application_id').fetchone()[0],
                         LayaIndexA.APP_ID)
        self.assertEqual(self.index.db.execute('PRAGMA user_version').fetchone()[0], 6)

    def test_v4_header_is_refused_without_automatic_upgrade(self):
        other = sqlite3.connect(self.db_path, isolation_level=None)
        try:
            other.execute('PRAGMA user_version=4')
        finally:
            other.close()
        original = self.db_path.read_bytes()
        with self.assertRaisesRegex(IndexContractError, 'schema_version_unknown'):
            LayaIndexA(self.db_path)
        self.assertEqual(self.db_path.read_bytes(), original)
        self.assertEqual(self.index.db.execute('PRAGMA user_version').fetchone()[0], 4)

    def test_required_vector_publish_refuses_unsealed_generation(self):
        self.assertEqual(self.index.stage('g0', 'alpha', [('one', 'target one', True)]),
                         'prepared')
        with self.assertRaisesRegex(IndexContractError, 'vector_generation_unsealed'):
            self.index.publish(None, 'g0', 'claim-0', require_vector=True)
        self.assertIsNone(self.index.receipt('claim-0'))
        self.assertEqual(self.index.db.execute('SELECT COUNT(*) FROM head').fetchone()[0], 0)
        self.assertEqual(self.index.publish(None, 'g0', 'claim-0'), 'published')
        with self.assertRaisesRegex(IndexContractError, 'vector_generation_unsealed'):
            self.index.publish(None, 'g0', 'claim-0', require_vector=True)

    def test_v6_vector_direct_sql_insert_and_immutable_seals(self):
        self.assertEqual(self.index.stage('g0', 'alpha', [('one', 'target one', True)]),
                         'prepared')
        db = self.index.db
        profile, fingerprint, hash_ = 'a'*64, 'b'*64, hash_body('target one')
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute('INSERT INTO vector_profiles VALUES(?,?,?,?,?)',
                       ('bad', 'synthetic', fingerprint, 2, 'cosine'))
        db.execute('INSERT INTO vector_profiles VALUES(?,?,?,?,?)',
                   (profile, 'synthetic', fingerprint, 2, 'cosine'))
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute('INSERT INTO vector_chunks VALUES(?,?,?,?,?,?,?,?)',
                       (hash_, 'one', profile, 0, 0, b'wrong', 'e'*64, 'c'*64))
        db.execute('INSERT INTO vector_chunks VALUES(?,?,?,?,?,?,?,?)',
                   (hash_, 'one', profile, 0, 0, struct.pack('<ff', 1.0, 0.0),
                    'e'*64, 'c'*64))
        db.execute('INSERT INTO vector_chunk_sets VALUES(?,?,?,?,?)',
                   (hash_, 'one', profile, 1, 'd'*64))
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute('INSERT INTO gen_vector_seals VALUES(?,?,?,?,?)',
                       ('g0', 'alpha', profile, 'd'*64, 0))
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute('INSERT INTO gen_vector_rows VALUES(?,?,?,?,?,?,?)',
                       ('g0', 'other', 'one', hash_, 'one', profile, 0))
        db.execute('INSERT INTO gen_vector_rows VALUES(?,?,?,?,?,?,?)',
                   ('g0', 'alpha', 'one', hash_, 'one', profile, 0))
        db.execute('INSERT INTO gen_vector_seals VALUES(?,?,?,?,?)',
                   ('g0', 'alpha', profile, 'd'*64, 1))
        for statement in (
            "UPDATE vector_profiles SET model_id='changed'",
            'DELETE FROM vector_chunks',
            'UPDATE gen_vector_rows SET scope=scope',
            'DELETE FROM gen_vector_seals',
        ):
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute(statement)
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute('INSERT OR REPLACE INTO vector_profiles VALUES(?,?,?,?,?)',
                       (profile, 'synthetic', fingerprint, 2, 'cosine'))
        self.assertEqual(db.execute('SELECT COUNT(*) FROM gen_vector_rows').fetchone()[0], 1)

    def test_inspect_detects_tampered_vector_seal_with_static_schema_intact(self):
        self.assertEqual(self.index.stage('g0', 'alpha', [('one', 'target one', True)]),
                         'prepared')
        vectors = LayaVectorIndex(self.index)
        profile = vectors.register_profile('synthetic-model', 'b'*64, 2)
        vectors.insert_chunk(hash_body('target one'), profile, 0, 0,
                             [1.0, 0.0], source_evidence(hash_body('target one'), 'one'),
                             title='one')
        vectors.seal_chunk_set(hash_body('target one'), 'one', profile, 1)
        vectors.seal_generation('g0', profile)
        self.assertEqual(self.index.publish(None, 'g0', 'claim-0',
                                            require_vector=True), 'published')
        self.assertEqual(self.index.inspect()['generations'], 1)
        db = self.index.db
        original_trigger = db.execute("SELECT sql FROM sqlite_master WHERE "
            "name='gen_vector_seal_update_seal'").fetchone()[0]
        db.execute('DROP TRIGGER gen_vector_seal_update_seal')
        db.execute("UPDATE gen_vector_seals SET rows_digest=? WHERE gen='g0'", ('f'*64,))
        db.execute(original_trigger)
        with self.assertRaisesRegex(IndexContractError, 'vector_seal_invalid'):
            self.index.inspect()

    def test_create_rejects_dangling_wal_sidecar_symlink(self):
        fresh = self.root / 'sidecar-index.sqlite'
        sidecar = Path(str(fresh) + '-wal')
        missing = self.root / 'absent-sidecar-target'
        sidecar.symlink_to(missing)
        self.assertFalse(sidecar.exists())
        self.assertTrue(sidecar.is_symlink())
        with self.assertRaisesRegex(IndexContractError, 'stale_database_sidecar'):
            LayaIndexA(fresh, create=True)
        self.assertFalse(fresh.exists())
        self.assertTrue(sidecar.is_symlink())
        self.assertFalse(missing.exists())

    def test_concurrent_stage_during_readonly_header_snapshot_rechecks_new_schema(self):
        original = LayaIndexA._check_header
        interleaved = False

        def classify(cls, db):
            nonlocal interleaved
            if not interleaved:
                interleaved = True
                # Establish the read-only classifier's WAL snapshot first.
                self.assertEqual(db.execute('SELECT COUNT(*) FROM gen_index').fetchone()[0], 0)
                writer = LayaIndexA(self.db_path)
                try:
                    self.assertEqual(writer.stage('g0', 'alpha', [('one', 'target', True)]),
                                     'prepared')
                finally:
                    writer.close()
            return original(db)

        with mock.patch.object(LayaIndexA, '_check_header', classmethod(classify)):
            reopened = self.connect()
            self.assertEqual(reopened.inspect()['generations'], 1)
        self.assertTrue(interleaved)

    def test_readonly_to_readwrite_open_refuses_swapped_database_incarnation(self):
        first_path = self.root / 'swap-first.sqlite'
        second_path = self.root / 'swap-second.sqlite'
        first = LayaIndexA(first_path, create=True)
        first_id = first.incarnation()
        first.close()
        second = LayaIndexA(second_path, create=True)
        second_id = second.incarnation()
        second.close()
        self.assertNotEqual(first_id, second_id)
        parked = self.root / 'swap-first-parked.sqlite'
        rw_uri = first_path.resolve().as_uri() + '?mode=rw'
        original_connect = sqlite3.connect
        swapped = False

        def swap_before_rw(database, *args, **kwargs):
            nonlocal swapped
            if database == rw_uri and not swapped:
                first_path.rename(parked)
                second_path.rename(first_path)
                swapped = True
            return original_connect(database, *args, **kwargs)

        with mock.patch.object(laya_index_module.sqlite3, 'connect',
                               side_effect=swap_before_rw):
            with self.assertRaisesRegex(IndexContractError,
                                        'database_incarnation_mismatch'):
                LayaIndexA(first_path)
        self.assertTrue(swapped)
        replacement = LayaIndexA(first_path)
        try:
            self.assertEqual(replacement.incarnation(), second_id)
        finally:
            replacement.close()
        self.assertTrue(parked.is_file())

    def test_two_connections_version_change_defers_exact_receipt_decision(self):
        docs = [('one', 'target one', True)]
        manifest = _digest('alpha', [('one', hash_body('target one'), 1)])
        digest = payload_digest('alpha', 'g0', manifest, 1, None)
        job = {'claim':'receipt-job','scope':'alpha','gen':'g0',
               'source_manifest':manifest,'claim_digest':digest,
               'claim_epoch':1,'base_gen':None}
        self.assertEqual(self.index.stage('g0', 'alpha', docs, base=None,
                         source_manifest=manifest, claim_digest=digest,
                         claim_epoch=1), 'prepared')
        self.assertEqual(self.index.publish(None, 'g0', 'receipt-job',
                         source_manifest=manifest, claim_digest=digest,
                         claim_epoch=1), 'published')
        self.assertEqual(ClaimQueue.decide(self.index, job), ACK)
        other = sqlite3.connect(self.db_path, timeout=2, isolation_level=None)
        try:
            other.execute('PRAGMA user_version=7')
        finally:
            other.close()
        self.assertEqual(ClaimQueue.decide(self.index, job), DEFER)
        with self.assertRaisesRegex(IndexContractError, 'schema_version_unknown'):
            self.index.receipt_detail('receipt-job')

    def test_missing_generation_fts_row_fails_search_instead_of_partial_results(self):
        self.publish_base()
        token, _ = self.index.pin('alpha')
        ordinal = self.index.db.execute(
            "SELECT ord FROM gen_index WHERE gen='g0'").fetchone()[0]
        self.index.db.execute('DELETE FROM gen_fts_%d' % ordinal)
        with self.assertRaisesRegex(IndexContractError, 'fts_generation_mismatch'):
            self.index.search(token, 'target', 3)
        self.index.release(token)

    def test_source_ancestor_symlink_swap_fails_closed(self):
        parent = self.root / 'swap-parent'
        hop = parent / 'laya-hop'
        source = hop / 'source'
        source.mkdir(parents=True)
        (source / 'one.md').write_text('target approved', encoding='utf-8')
        decoy = self.root / 'decoy'
        (decoy / 'source').mkdir(parents=True)
        (decoy / 'source' / 'one.md').write_text('target decoy', encoding='utf-8')
        parked = parent / 'laya-hop-parked'
        root_path = str(source.resolve())
        original_exclusions = source_port._load_exclusions
        swapped = False

        def swap_before_root_open(index, scope):
            nonlocal swapped
            exclusions = original_exclusions(index, scope)
            os.rename(hop, parked)
            hop.symlink_to(decoy, target_is_directory=True)
            swapped = True
            return exclusions

        # The swap lands after canonical-path validation and capability checks,
        # just before the nofollow traversal opens the ancestor component.
        with mock.patch.object(source_port, '_load_exclusions',
                               side_effect=swap_before_root_open):
            with self.assertRaisesRegex(IndexContractError, 'index_stale'):
                source_port.scan(root_path, 'alpha', self.index)
        self.assertTrue(swapped)
        self.assertTrue((parked / 'source' / 'one.md').is_file())

    def test_schema_manifest_rejects_redefined_trigger_before_mutating_open(self):
        self.index.db.execute('DROP TRIGGER content_insert_seal')
        self.index.db.execute('CREATE TRIGGER content_insert_seal BEFORE INSERT ON content '
                              'BEGIN SELECT 1; END')
        with self.assertRaisesRegex(IndexContractError, 'schema_manifest_mismatch'):
            LayaIndexA(self.db_path)
        self.assertEqual(self.index.db.execute('PRAGMA user_version').fetchone()[0], 6)

    def test_dynamic_named_trigger_is_not_ignored_by_schema_manifest(self):
        self.index.db.execute('CREATE TRIGGER gen_fts_7 BEFORE INSERT ON receipts '
                              'BEGIN SELECT RAISE(IGNORE); END')
        with self.assertRaisesRegex(IndexContractError, 'schema_manifest_mismatch'):
            LayaIndexA(self.db_path)
        with self.assertRaisesRegex(IndexContractError, 'schema_manifest_mismatch'):
            self.index.stage('g0', 'alpha', [('a', 'target', True)])

    def test_direct_sql_cannot_delete_head_rewrite_meta_or_receipt_prepared(self):
        self.publish_base()
        self.assertEqual(self.index.stage('g1','alpha',[('a','target',True)]), 'prepared')
        for sql in ("DELETE FROM head WHERE scope='alpha'",
                    "UPDATE schema_meta SET value='wrong' WHERE key='static_manifest'",
                    "DELETE FROM schema_meta WHERE key='static_manifest'",
                    "INSERT INTO receipts VALUES('bad','alpha','g1',NULL,NULL,NULL)"):
            with self.subTest(sql=sql), self.assertRaises(sqlite3.IntegrityError):
                self.index.db.execute(sql)
        self.assertEqual(self.head(self.index.db), ('g0',))

    def test_schema_meta_replace_and_nonpositive_generation_ordinal_are_sealed(self):
        old = self.index.incarnation()
        for key in ('incarnation', 'static_manifest'):
            with self.subTest(key=key), self.assertRaises(sqlite3.IntegrityError):
                self.index.db.execute('INSERT OR REPLACE INTO schema_meta VALUES(?,?)',
                                      (key, 'wrong'))
        self.assertEqual(self.index.incarnation(), old)
        self.index.db.execute('SAVEPOINT direct_ordinal')
        try:
            self.index.db.execute("INSERT INTO generations VALUES('manual','alpha','building','digest',0)")
            for ordinal in (0, -1):
                with self.subTest(ordinal=ordinal), self.assertRaises(sqlite3.IntegrityError):
                    self.index.db.execute('INSERT INTO gen_index(ord,gen) VALUES(?,?)',
                                          (ordinal, 'manual'))
        finally:
            self.index.db.execute('ROLLBACK TO direct_ordinal')
            self.index.db.execute('RELEASE direct_ordinal')
        self.assertEqual(self.index.stage('g0','alpha',[('one','target',True)]), 'prepared')

    def test_stage_rejects_noncanonical_paths_and_stale_replay_after_exclusion(self):
        bad = ('/absolute.md', './dot.md', 'notes//double.md',
               'notes/../parent.md', 'notes/.hidden.md', 'bad\\name.md',
               'line\nbreak.md', 'bad\ud800.md', 'x' * 4097)
        for path in bad:
            with self.subTest(path=repr(path)), self.assertRaisesRegex(
                    IndexContractError, 'invalid_document'):
                self.index.stage('bad', 'alpha', [(path, 'target', True)])
        self.assertEqual(self.index.db.execute('SELECT COUNT(*) FROM generations').fetchone()[0], 0)
        docs = [('notes/one.md','target one',True)]
        self.assertEqual(self.index.stage('g0','alpha',docs), 'prepared')
        self.assertEqual(self.index.exclude('alpha', path='notes/one.md'), 'excluded')
        self.assertEqual(self.index.stage('g0','alpha',docs), 'index_stale')
        self.assertEqual(self.index.db.execute(
            "SELECT state FROM generations WHERE gen='g0'").fetchone(), ('prepared',))

    def test_identical_stage_refuses_mutated_generation_fts(self):
        docs = [('one', 'target one', True)]
        self.assertEqual(self.index.stage('g0','alpha',docs), 'prepared')
        ordinal = self.index.db.execute("SELECT ord FROM gen_index WHERE gen='g0'").fetchone()[0]
        self.index.db.execute('DELETE FROM gen_fts_%d' % ordinal)
        with self.assertRaisesRegex(IndexContractError, 'generation_conflict'):
            self.index.stage('g0','alpha',docs)

    def test_exclusion_rejects_noncanonical_path_and_hash(self):
        for value in ('/one.md', './one.md', 'private/../one.md',
                      'private//one.md', '.hidden/one.md', 'bad\\name.md'):
            with self.subTest(path=value), self.assertRaisesRegex(
                    IndexContractError, 'invalid_exclusion'):
                self.index.exclude('alpha', path=value)
        with self.assertRaisesRegex(IndexContractError, 'invalid_exclusion'):
            self.index.exclude('alpha', hash_='A' * 64)
        self.assertEqual(self.index.list_exclusions('alpha'), [])

    def test_inspect_detects_published_without_receipt_and_scored_bound(self):
        self.assertEqual(self.index.stage('g0','alpha',[('a','target',True)]), 'prepared')
        self.index.db.execute("UPDATE generations SET state='published' WHERE gen='g0'")
        with self.assertRaisesRegex(IndexContractError, 'published_without_receipt'):
            self.index.inspect()
        self.assertEqual(self.index.db.execute('PRAGMA integrity_check').fetchone(), ('ok',))
        with self.assertRaisesRegex(IndexContractError, 'invalid_scored_query'):
            self.index.search_scored('no-pin', {str(i):0.0 for i in range(499)}, 5)

    def test_inspect_rechecks_live_schema_manifest(self):
        self.publish_base()
        self.index.db.execute('DROP TRIGGER receipt_update_seal')
        with self.assertRaisesRegex(IndexContractError, 'schema_manifest_mismatch'):
            self.index.inspect()

    def test_inspect_rejects_receipt_proof_outside_sealed_candidate(self):
        body = 'target one'
        manifest = _digest('alpha', [('one', hash_body(body), 1)])
        self.assertEqual(self.index.stage('g0','alpha',[('one',body,True)],
                         base=None,source_manifest=manifest,claim_digest='a'*64,
                         claim_epoch=1), 'prepared')
        self.assertEqual(self.index.publish(None,'g0','good',source_manifest=manifest,
                         claim_digest='a'*64,claim_epoch=1), 'published')
        trigger = self.index.db.execute("SELECT sql FROM sqlite_master WHERE type='trigger' "
                                        "AND name='receipt_insert_scope'").fetchone()[0]
        self.index.db.execute('DROP TRIGGER receipt_insert_scope')
        self.index.db.execute('INSERT INTO receipts VALUES(?,?,?,?,?,?)',
                              ('rogue','alpha','g0','b'*64,'c'*64,2))
        self.index.db.execute(trigger)
        with self.assertRaisesRegex(IndexContractError, 'receipt_proof_integrity'):
            self.index.inspect()

    def test_bm25_population_is_generation_local_across_other_scope_and_prepared_view(self):
        self.assertEqual(self.index.stage('g0', 'alpha', [
            ('a-one', 'target one', True), ('a-two', 'target target two', True)]), 'prepared')
        self.assertEqual(self.index.publish(None, 'g0', 'claim-a'), 'published')
        token, _ = self.index.pin('alpha')
        baseline = [(r['path'], r['score']) for r in self.index.search(token, 'target', 5)]
        self.assertEqual(len(baseline), 2)
        self.assertEqual(self.index.stage('other', 'other', [
            (f'z-{n:03d}', 'target target target foreign', True) for n in range(90)]), 'prepared')
        self.assertEqual(self.index.publish(None, 'other', 'claim-other'), 'published')
        self.assertEqual(self.index.stage('g1', 'alpha', [
            (f'future-{n:03d}', 'target prepared', True) for n in range(40)]), 'prepared')
        self.assertEqual([(r['path'], r['score']) for r in self.index.search(token, 'target', 5)],
                         baseline)
        self.index.release(token)

    def test_v6_qmd_fts_title_path_cjk_and_collection_filter_before_limit(self):
        docs = [('raw/early.md', 'neutral content', True),
                ('raw/target.md', 'ordinary target body', True),
                ('wiki/target.md', '# Target\nplain body', True),
                ('wiki/한국어.md', '# 이야기\n한국어 본문', True)]
        self.assertEqual(self.index.stage('g0', 'alpha', docs), 'prepared')
        self.assertEqual(self.index.publish(None, 'g0', 'claim'), 'published')
        token, _ = self.index.pin('alpha')
        try:
            ordinal = self.index.db.execute(
                "SELECT ord FROM gen_index WHERE gen='g0'").fetchone()[0]
            table = f'gen_fts_{ordinal}'
            sql = self.index.db.execute(
                "SELECT sql FROM sqlite_master WHERE name=?", (table,)).fetchone()[0]
            self.assertIn("fts5(filepath,title,body,path_key UNINDEXED,tokenize='porter unicode61')", sql)
            self.assertEqual([r['path'] for r in self.index.search(
                token, '"한 국 어"', 5)], ['wiki/한국어.md'])
            self.assertEqual([r['path'] for r in self.index.search(
                token, '"target"*', 1, path_prefixes=('wiki/',))],
                ['wiki/target.md'])
            self.assertEqual([r['path'] for r in self.index.search(
                token, '"target"*', 1, path_prefixes=('raw/',))],
                ['raw/target.md'])
            with self.assertRaisesRegex(IndexContractError, 'invalid_collection_filter'):
                self.index.search(token, 'target', 1, path_prefixes=('../',))
        finally:
            self.index.release(token)

    def test_qmd_script_members_and_block_gaps_are_distinct(self):
        # QMD uses Unicode Script, whose supplementary members and block gaps
        # differ from broad CJK block checks.
        for ch in ('\U00031350', '\U0001b132', '\U0001aff0', '\u302e'):
            self.assertTrue(_cjk(ch), hex(ord(ch)))
            self.assertEqual(_qmd_fts_text('A' + ch + 'B'), 'A ' + ch + ' B')
        for ch in ('\u3001', '\ufa6e', '\ud7a4'):
            self.assertFalse(_cjk(ch), hex(ord(ch)))
            self.assertEqual(_qmd_fts_text('A' + ch + 'B'), 'A' + ch + 'B')

    def test_search_many_one_fts_check_and_explicit_deadline_failure(self):
        self.assertEqual(self.index.stage('g0', 'alpha', [
            ('raw/a.md', 'target alpha', True),
            ('wiki/b.md', 'other beta', True)]), 'prepared')
        self.assertEqual(self.index.publish(None, 'g0', 'claim'), 'published')
        token, _ = self.index.pin('alpha')
        try:
            with mock.patch.object(self.index, '_generation_fts_matches',
                                   wraps=self.index._generation_fts_matches) as check:
                batches = self.index.search_many(
                    token, ('target', 'other'), 5, path_prefixes=('raw/',))
            self.assertEqual(check.call_count, 1)
            self.assertEqual([[item['path'] for item in rows] for rows in batches],
                             [['raw/a.md'], []])
            with mock.patch.object(laya_index_module.time, 'monotonic',
                                   side_effect=iter([0, 1, 2, 3])):
                with self.assertRaisesRegex(IndexContractError,
                                            '^fts_deadline_exceeded$'):
                    self.index.search_many(token, ('target', 'other'), 5,
                                           deadline_seconds=0.5)
            # Timeout rolls back and unregisters SQLite's progress handler.
            self.assertEqual([item['path'] for item in
                              self.index.search(token, 'target', 5)], ['raw/a.md'])
            with self.assertRaisesRegex(IndexContractError, 'invalid_query'):
                self.index.search_many(token, tuple('q' for _ in range(9)), 1)
        finally:
            self.index.release(token)

    def test_exclusion_blocks_old_pin_and_rank_until_new_eligible_generation(self):
        self.assertEqual(self.index.stage('g0', 'alpha', [
            ('keep', 'target safe', True), ('remove', 'target secret', True)]), 'prepared')
        self.assertEqual(self.index.publish(None, 'g0', 'claim-0'), 'published')
        old, _ = self.index.pin('alpha')
        self.assertEqual(len(self.index.search(old, 'target', 5)), 2)
        self.assertEqual(self.index.exclude('alpha', path='remove'), 'excluded')
        self.assertEqual(self.index.exclude('alpha', path='remove'), 'already_excluded')
        for search in (lambda: self.index.search(old, 'target', 5),
                       lambda: self.index.search_scored(old, {hash_body('target safe'): 1.0}, 5)):
            with self.assertRaisesRegex(IndexContractError, 'index_stale'):
                search()
        self.assertEqual(self.index.stage('g1', 'alpha', [('keep', 'target safe', True)]), 'prepared')
        self.assertEqual(self.index.publish('g0', 'g1', 'claim-1'), 'published')
        fresh, _ = self.index.pin('alpha')
        self.assertEqual([r['path'] for r in self.index.search(fresh, 'target', 5)], ['keep'])
        with self.assertRaisesRegex(IndexContractError, 'index_stale'):
            self.index.search(old, 'target', 5)
        self.index.release(old)
        self.index.release(fresh)



if __name__ == '__main__':
    unittest.main()

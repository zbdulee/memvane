#!/usr/bin/env python3
"""Synthetic conditional-ACK checks over temp queues; no QMD, endpoint or model I/O."""
import copy
import fcntl
import inspect
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

CORE = Path(__file__).resolve().parents[2] / 'core'
sys.path.insert(0, str(CORE))
import dirty_queue_claim as claim
import shadow_ack_policy as policy

ROWS = b'a\t/tmp/a\t/tmp/project\nb\t/tmp/b\t/tmp/project\n'
TAIL = b'later\t/tmp/later\t/tmp/project\n'


def state(root):
    """Every file's bytes and inode/mtime: any write or replacement shows."""
    return {p.name: (p.read_bytes(), p.stat().st_ino, p.stat().st_mtime_ns)
            for p in sorted(Path(root).iterdir()) if p.is_file()}


def claim_file(queue):
    return Path(str(queue) + '.claim.json')


def lock_held(queue):
    """True if another open file description cannot take the sidecar lock."""
    fd = os.open(str(queue) + '.lock', os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


class Base(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory(prefix='qmd-claim-identity-')
        self.root = Path(self._temp.name)
        self.queue = self.root / 'dirty-queue'
        self.queue.write_bytes(ROWS)
        os.chmod(self.queue, 0o600)

    def tearDown(self):
        self._temp.cleanup()

    def claimed(self):
        self.assertTrue(claim.claim(self.queue, self.root / 'snapshot'))
        with self.queue.open('ab') as output:
            output.write(TAIL)
        return claim.claim_identity(self.queue)

    def assertRefusedUnchanged(self, reason, **kwargs):
        before = state(self.root)
        with patch.object(claim, '_atomic', side_effect=AssertionError('mutated')), \
                patch.object(claim, '_ack_locked', side_effect=AssertionError('acked')):
            with self.assertRaises(ValueError) as caught:
                claim.ack(self.queue, **kwargs)
        self.assertEqual(str(caught.exception), reason)
        self.assertEqual(state(self.root), before)


class Legacy(Base):
    def test_signature_and_cli_unchanged(self):
        params = inspect.signature(claim.ack).parameters
        self.assertEqual(list(params), ['queue', 'expect'])
        self.assertIsNone(params['expect'].default)
        self.claimed()
        self.assertEqual(claim.main(['ack', str(self.queue), 'extra']), 2)
        self.assertEqual(claim.main(['ack', str(self.queue)]), 0)
        self.assertEqual(self.queue.read_bytes(), TAIL)
        self.assertFalse(claim_file(self.queue).exists())

    def test_legacy_and_matching_ack_produce_identical_bytes(self):
        identity = self.claimed()
        other = self.root / 'other'
        other.mkdir(mode=0o700)
        queue2 = other / 'dirty-queue'
        queue2.write_bytes(ROWS)
        os.chmod(queue2, 0o600)
        self.assertTrue(claim.claim(queue2, other / 'snapshot'))
        with queue2.open('ab') as output:
            output.write(TAIL)
        claim.ack(queue2)
        claim.ack(self.queue, expect=identity)
        self.assertEqual(queue2.read_bytes(), self.queue.read_bytes())
        self.assertEqual(self.queue.read_bytes(), TAIL)
        self.assertFalse(claim_file(queue2).exists() or claim_file(self.queue).exists())

    def test_legacy_ack_ignores_identity_size_cap(self):
        self.claimed()
        with patch.object(claim, 'MAX_IDENTITY_CLAIM_BYTES', 16):
            claim.ack(self.queue)
        self.assertEqual(self.queue.read_bytes(), TAIL)


class IdentityRead(Base):
    def test_shape_and_no_mutation(self):
        self.assertTrue(claim.claim(self.queue, self.root / 'snapshot'))
        before = state(self.root)
        first = claim.claim_identity(self.queue)
        self.assertEqual(claim.claim_identity(self.queue), first)
        self.assertEqual(state(self.root), before)
        value = json.loads(claim_file(self.queue).read_text())
        self.assertEqual(first, {'id': 'qmd-dirty-claim-v1:%d:%d' % (value['inode'], value['size']),
                                 'digest': value['sha256']})

    def test_absent_claim_is_none_without_creating_lock(self):
        self.assertIsNone(claim.claim_identity(self.queue))
        self.assertFalse(Path(str(self.queue) + '.lock').exists())
        self.assertTrue(claim.claim(self.queue, self.root / 'snapshot'))
        claim.ack(self.queue)
        before = state(self.root)
        self.assertIsNone(claim.claim_identity(self.queue))
        self.assertEqual(state(self.root), before)

    def test_oversize_claim_refused_without_mutation(self):
        self.claimed()
        with patch.object(claim, 'MAX_IDENTITY_CLAIM_BYTES', 16):
            before = state(self.root)
            with self.assertRaises(ValueError) as caught:
                claim.claim_identity(self.queue)
            self.assertEqual(str(caught.exception), 'dirty_queue_claim_too_large')
            self.assertEqual(state(self.root), before)
            self.assertRefusedUnchanged('dirty_queue_claim_too_large',
                                        expect={'id': 'x', 'digest': '0' * 64})


class Conditional(Base):
    def test_malformed_expect_refused_before_lock(self):
        self.claimed()
        good = claim.claim_identity(self.queue)
        for bad in ({}, {'id': good['id']}, {**good, 'extra': 1}, {**good, 'id': ''},
                    {**good, 'id': 'x' * 257}, {**good, 'digest': 'A' * 64},
                    {**good, 'digest': good['digest'][:-1]}, [good['id'], good['digest']]):
            with patch.object(claim, 'queue_lock', side_effect=AssertionError('locked')):
                self.assertRefusedUnchanged('invalid_dirty_queue_claim_identity', expect=bad)

    def test_mismatch_refuses_before_phase_or_file_change(self):
        identity = self.claimed()
        self.assertRefusedUnchanged('dirty_queue_claim_identity_mismatch',
                                    expect={**identity, 'digest': '0' * 64})
        self.assertRefusedUnchanged('dirty_queue_claim_identity_mismatch',
                                    expect={**identity, 'id': identity['id'] + '0'})
        self.assertEqual(json.loads(claim_file(self.queue).read_text())['phase'], 'processing')
        self.assertEqual(self.queue.read_bytes(), ROWS + TAIL)

    def test_stale_identity_from_earlier_claim_refused(self):
        stale = self.claimed()
        claim.ack(self.queue, expect=stale)
        with self.queue.open('ab') as output:
            output.write(b'next\t/tmp/next\t/tmp/project\n')
        self.assertTrue(claim.claim(self.queue, self.root / 'snapshot'))
        self.assertNotEqual(claim.claim_identity(self.queue), stale)
        self.assertRefusedUnchanged('dirty_queue_claim_identity_mismatch', expect=stale)

    def test_missing_claim_refused(self):
        # Leave the lock sidecar in place so the refusal itself creates nothing.
        self.assertTrue(claim.claim(self.queue, self.root / 'snapshot'))
        claim.ack(self.queue)
        self.assertRefusedUnchanged('dirty_queue_claim_missing',
                                    expect={'id': 'x', 'digest': '0' * 64})

    def test_same_lock_hold_reads_and_acks(self):
        identity = self.claimed()
        reads = []
        original = claim._read_claim_bounded

        def spy(path):
            reads.append(lock_held(self.queue))
            return original(path)
        with patch.object(claim, '_read_claim_bounded', side_effect=spy), \
                patch.object(claim, '_read_claim', side_effect=AssertionError('unbounded read')):
            claim.ack(self.queue, expect=identity)
        self.assertEqual(reads, [True])
        self.assertEqual(self.queue.read_bytes(), TAIL)
        self.assertFalse(claim_file(self.queue).exists())

    def test_identity_stable_across_done_resume(self):
        identity = self.claimed()
        with patch.object(claim, '_ack_locked', side_effect=RuntimeError('after_done')):
            with self.assertRaises(RuntimeError):
                claim.ack(self.queue, expect=identity)
        self.assertEqual(json.loads(claim_file(self.queue).read_text())['phase'], 'done')
        self.assertEqual(claim.claim_identity(self.queue), identity)
        claim.ack(self.queue, expect=identity)
        self.assertEqual(self.queue.read_bytes(), TAIL)

    def test_reload_required_refusal_precedes_identity(self):
        identity = self.claimed()
        self.assertTrue(claim.reload_state(self.queue, True))
        for expect in (None, identity, {**identity, 'digest': '0' * 64}):
            self.assertRefusedUnchanged('dirty_queue_reload_required', expect=expect)
        self.assertFalse(claim.reload_state(self.queue, False))
        self.assertEqual(claim.claim_identity(self.queue), identity)
        self.assertRefusedUnchanged('dirty_queue_claim_identity_mismatch',
                                    expect={**identity, 'digest': '0' * 64})
        claim.ack(self.queue, expect=identity)
        self.assertEqual(self.queue.read_bytes(), TAIL)


def view(revision, generation):
    return {'owner': 'project-a', 'incarnation': 'registration-a',
            'worktree': 'worktree-a', 'scopeRevision': 'settings-a',
            'storeGeneration': 'store-a', 'contentRevision': revision,
            'indexGeneration': generation}


EXPECTED = view('rev-1', 'g-' + '1' * 32)
PREPARED = view('rev-2', 'g-' + '2' * 32)
SEAL = {'schema': 'qmd-source-snapshot-v1', 'identity': [41, 7], 'snapshotSha256': 'b' * 64}


def prepared_result():
    return {'outcome': 'prepared', 'reason': None, 'view': dict(PREPARED),
            'candidate': {'generation': PREPARED['indexGeneration'], 'index': '/synthetic/index.sqlite'},
            'sealed': copy.deepcopy(SEAL), 'current': dict(EXPECTED),
            'ack': 'not_called', 'expected': dict(EXPECTED)}


class PolicyCompatibility(Base):
    def test_identity_is_a_valid_policy_claim(self):
        identity = self.claimed()
        self.assertTrue(policy._valid_claim(identity))
        receipt = policy.build_receipt(identity, prepared_result())
        self.assertTrue(policy.valid_receipt(receipt))

    def test_policy_ack_feeds_conditional_ack(self):
        identity = self.claimed()
        receipt = policy.build_receipt(identity, prepared_result())
        verdict = policy.decide(receipt, dict(PREPARED), claim.claim_identity(self.queue), True)
        self.assertEqual((verdict['decision'], verdict['reason']), (policy.ACK, 'cas_landed_sealed'))
        claim.ack(self.queue, expect=verdict['ack'])
        self.assertEqual(self.queue.read_bytes(), TAIL)

    def test_policy_rejects_newer_claim_and_ack_refuses_stale(self):
        stale = self.claimed()
        receipt = policy.build_receipt(stale, prepared_result())
        claim.ack(self.queue)
        self.assertTrue(claim.claim(self.queue, self.root / 'snapshot'))
        current = claim.claim_identity(self.queue)
        verdict = policy.decide(receipt, dict(PREPARED), current, True)
        self.assertNotEqual(verdict['decision'], policy.ACK)
        self.assertRefusedUnchanged('dirty_queue_claim_identity_mismatch', expect=receipt['claim'])


if __name__ == '__main__':
    unittest.main()

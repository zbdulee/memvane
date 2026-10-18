#!/usr/bin/env python3
"""Synthetic C2 ACK-retry decision checks; no file, QMD, endpoint or model I/O."""
import copy
from pathlib import Path
import sys
import unittest

CORE = Path(__file__).resolve().parents[2] / 'core'
sys.path.insert(0, str(CORE))
import shadow_ack_policy as policy

EXPECTED_GEN = 'g-' + '1' * 32
PREPARED_GEN = 'g-' + '2' * 32
OTHER_GEN = 'g-' + '3' * 32
CLAIM = {'id': 'claim-0001', 'digest': 'a' * 64}
SEAL = {'schema': 'qmd-source-snapshot-v1', 'identity': [41, 7], 'snapshotSha256': 'b' * 64}


def view(revision, generation, **changes):
    row = {'owner': 'project-a', 'incarnation': 'registration-a',
           'worktree': 'worktree-a', 'scopeRevision': 'settings-a',
           'storeGeneration': 'store-a', 'contentRevision': revision,
           'indexGeneration': generation}
    row.update(changes)
    return row


EXPECTED = view('rev-1', EXPECTED_GEN)
PREPARED = view('rev-2', PREPARED_GEN)


def prepared_result(**changes):
    """Shape of a shadow_publish_flow.prepare() PREPARED result."""
    row = {'outcome': 'prepared', 'reason': None, 'view': dict(PREPARED),
           'candidate': {'generation': PREPARED_GEN, 'index': '/synthetic/index.sqlite'},
           'sealed': copy.deepcopy(SEAL), 'current': dict(EXPECTED),
           'ack': 'not_called', 'expected': dict(EXPECTED)}
    row.update(changes)
    return row


def receipt():
    return policy.build_receipt(dict(CLAIM), prepared_result())


class Receipt(unittest.TestCase):
    def test_build_receipt_copies_and_validates(self):
        source = prepared_result()
        built = policy.build_receipt(dict(CLAIM), source)
        self.assertTrue(policy.valid_receipt(built))
        source['view']['contentRevision'] = 'mutated'
        source['sealed']['identity'][0] = 99
        self.assertEqual(built['prepared'], PREPARED)
        self.assertEqual(built['seal'], SEAL)

    def test_build_receipt_rejects_non_prepared_or_mismatched(self):
        bad = [prepared_result(outcome='rejected'),
               prepared_result(candidate={'generation': OTHER_GEN, 'index': 'x'}),
               prepared_result(candidate=None),
               prepared_result(sealed=None),
               prepared_result(view=dict(EXPECTED)),
               None]
        for row in bad:
            with self.assertRaises(ValueError):
                policy.build_receipt(dict(CLAIM), row)
        with self.assertRaises(ValueError):
            policy.build_receipt({'id': 'c', 'digest': 'short'}, prepared_result())

    def test_missing_receipt_defers_even_when_cas_visibly_landed(self):
        # Death before the receipt was retained: the pointer equals what we
        # would have published, but nothing ties it to this run. Blocked.
        result = policy.decide(None, dict(PREPARED), dict(CLAIM), True)
        self.assertEqual((result['decision'], result['reason']),
                         (policy.DEFER, 'receipt_missing'))
        self.assertIsNone(result['ack'])

    def test_malformed_receipts_defer(self):
        def mutate(fn):
            row = receipt()
            fn(row)
            return row
        cases = [
            {},
            'receipt',
            mutate(lambda r: r.pop('seal')),
            mutate(lambda r: r.update(extra=1)),
            mutate(lambda r: r.update(schema='v0')),
            mutate(lambda r: r['claim'].update(digest='A' * 64)),
            mutate(lambda r: r['claim'].update(id='')),
            mutate(lambda r: r['prepared'].update(indexGeneration=EXPECTED_GEN)),
            mutate(lambda r: r['prepared'].update(indexGeneration='g-xyz')),
            mutate(lambda r: r['prepared'].update(owner='project-b')),
            mutate(lambda r: r['prepared'].update(contentRevision='rev-1')),
            mutate(lambda r: r['expected'].pop('worktree')),
            mutate(lambda r: r['seal'].update(identity=[True, 7])),
            mutate(lambda r: r['seal'].update(snapshotSha256='')),
        ]
        for row in cases:
            result = policy.decide(row, dict(PREPARED), dict(CLAIM), True)
            self.assertEqual((result['decision'], result['reason']),
                             (policy.DEFER, 'receipt_malformed'), row)


class Claim(unittest.TestCase):
    def test_claim_unavailable_or_malformed_defers(self):
        for claim, reason in ((None, 'claim_unavailable'),
                              ({'id': 'claim-0001'}, 'claim_malformed'),
                              ({'id': 'claim-0001', 'digest': 'z' * 64}, 'claim_malformed')):
            result = policy.decide(receipt(), dict(PREPARED), claim, True)
            self.assertEqual((result['decision'], result['reason']), (policy.DEFER, reason))

    def test_changed_claim_never_acks_even_with_landed_cas(self):
        other_id = dict(CLAIM, id='claim-0002')
        result = policy.decide(receipt(), dict(PREPARED), other_id, True)
        self.assertEqual((result['decision'], result['reason']),
                         (policy.DEFER, 'claim_identity_changed'))
        coalesced = dict(CLAIM, digest='c' * 64)
        result = policy.decide(receipt(), dict(PREPARED), coalesced, True)
        self.assertEqual((result['decision'], result['reason']),
                         (policy.REPROCESS, 'claim_digest_changed'))
        self.assertIsNone(result['ack'])


class Pointer(unittest.TestCase):
    def test_crash_after_cas_before_ack_acks_exact_claim(self):
        result = policy.decide(receipt(), dict(PREPARED), dict(CLAIM), True)
        self.assertEqual((result['decision'], result['reason']),
                         (policy.ACK, 'cas_landed_sealed'))
        self.assertEqual(result['ack'], CLAIM)
        self.assertIsNot(result['ack'], CLAIM)

    def test_seal_proof_forms(self):
        cases = [(policy.SEAL_MATCH, policy.ACK, 'cas_landed_sealed'),
                 (copy.deepcopy(SEAL), policy.ACK, 'cas_landed_sealed'),
                 (False, policy.DEFER, 'published_candidate_seal_mismatch'),
                 (policy.SEAL_MISMATCH, policy.DEFER, 'published_candidate_seal_mismatch'),
                 (dict(SEAL, snapshotSha256='d' * 64), policy.DEFER,
                  'published_candidate_seal_mismatch'),
                 (dict(SEAL, identity=[41, 8]), policy.DEFER,
                  'published_candidate_seal_mismatch'),
                 (None, policy.DEFER, 'published_candidate_unproved'),
                 (policy.SEAL_UNKNOWN, policy.DEFER, 'published_candidate_unproved'),
                 (1, policy.DEFER, 'published_candidate_unproved'),
                 ('yes', policy.DEFER, 'published_candidate_unproved'),
                 ({'schema': 'qmd-source-snapshot-v1'}, policy.DEFER,
                  'published_candidate_unproved')]
        for seal, decision, reason in cases:
            result = policy.decide(receipt(), dict(PREPARED), dict(CLAIM), seal)
            self.assertEqual((result['decision'], result['reason']), (decision, reason), seal)
            self.assertEqual(result['ack'] is not None, decision == policy.ACK)

    def test_expected_pointer_means_retry_not_ack(self):
        # Crash before CAS, CAS lost the lock, or CAS raised before rename:
        # the receipt and a valid seal are not proof that anything landed.
        result = policy.decide(receipt(), dict(EXPECTED), dict(CLAIM), True)
        self.assertEqual((result['decision'], result['reason']),
                         (policy.REPROCESS, 'cas_not_landed'))
        self.assertIsNone(result['ack'])

    def test_unreadable_pointer_defers(self):
        broken = dict(PREPARED)
        broken.pop('worktree')
        for current in (None, {}, 'published', broken,
                        dict(PREPARED, indexGeneration='g-bad'),
                        dict(PREPARED, contentRevision='')):
            result = policy.decide(receipt(), current, dict(CLAIM), True)
            self.assertEqual((result['decision'], result['reason']),
                             (policy.DEFER, 'pointer_unreadable'), current)

    def test_superseded_and_lost_race_are_indistinguishable_and_reprocess(self):
        later = view('rev-3', OTHER_GEN)
        # (a) ours landed, ACK crashed, another publisher advanced from ours;
        # (b) another publisher advanced first and our CAS was rejected.
        # Both leave the same observable pointer; neither may ACK.
        for scenario in ('landed_then_superseded', 'other_publisher_won'):
            result = policy.decide(receipt(), dict(later), dict(CLAIM), True)
            self.assertEqual((result['decision'], result['reason']),
                             (policy.REPROCESS, 'pointer_advanced_ownership_unproven'),
                             scenario)
            self.assertIsNone(result['ack'])
            self.assertEqual(result['current'], later)

    def test_foreign_identity_defers(self):
        for key in policy.IDENTITY_KEYS:
            current = dict(PREPARED, **{key: 'other'})
            result = policy.decide(receipt(), current, dict(CLAIM), True)
            self.assertEqual((result['decision'], result['reason']),
                             (policy.DEFER, 'pointer_identity_changed'), key)

    def test_reused_generation_or_revision_collision_defers(self):
        cases = [(view('rev-9', PREPARED_GEN), 'pointer_generation_reused'),
                 (view('rev-9', EXPECTED_GEN), 'pointer_generation_reused'),
                 (view('rev-2', OTHER_GEN), 'pointer_content_revision_collision'),
                 (view('rev-1', OTHER_GEN), 'pointer_content_revision_collision')]
        for current, reason in cases:
            result = policy.decide(receipt(), current, dict(CLAIM), True)
            self.assertEqual((result['decision'], result['reason']),
                             (policy.DEFER, reason), current)

    def test_decision_does_not_alias_inputs(self):
        row, current = receipt(), dict(PREPARED)
        before = copy.deepcopy(row)
        result = policy.decide(row, current, dict(CLAIM), True)
        result['current']['contentRevision'] = 'mutated'
        result['ack']['digest'] = 'f' * 64
        self.assertEqual(row, before)
        self.assertEqual(current, PREPARED)

    def test_only_three_decisions_and_ack_only_on_full_proof(self):
        outcomes = set()
        for rec in (None, receipt()):
            for current in (None, dict(EXPECTED), dict(PREPARED), view('rev-3', OTHER_GEN)):
                for claim in (None, dict(CLAIM), dict(CLAIM, digest='c' * 64)):
                    for seal in (True, False, None):
                        result = policy.decide(rec, current, claim, seal)
                        outcomes.add(result['decision'])
                        full = (rec is not None and current == PREPARED
                                and claim == CLAIM and seal is True)
                        self.assertEqual(result['decision'] == policy.ACK, full)
                        self.assertIsInstance(result['reason'], str)
        self.assertEqual(outcomes, {policy.ACK, policy.REPROCESS, policy.DEFER})


class Purity(unittest.TestCase):
    def test_module_has_no_io_surface(self):
        text = (CORE / 'shadow_ack_policy.py').read_text()
        for token in ('open(', 'import os', 'pathlib', 'subprocess', 'socket',
                      'sqlite3', 'import shadow_publish_flow'):
            self.assertNotIn(token, text)


if __name__ == '__main__':
    unittest.main(verbosity=2)

"""Compile and verdict provenance use stable, input-bound digest domains."""
import unittest

from memvane.store import api as store


class ProvenanceDigests(unittest.TestCase):
    def test_digest_domains_bind_verdict_card_target_and_source(self):
        verdict = {'verdict': 'pass', 'claims': [{'claim': 'one', 'supported': True}], 'reasons': []}
        fingerprint = {'worktree_id': '1' * 32, 'branch_key': 'refs/heads/main', 'path': 'notes/x',
                       'source_sha': '2' * 64, 'byte_length': 3, 'head_oid': '3' * 40,
                       'tracked_state': 'tracked', 'binding_state': 'observed', 'scan_id': '4' * 32,
                       'observed_ns': 5}
        fingerprints = {'schema': 'memvane-compile-source-fingerprints-v1', 'viewId': 'b' * 64,
                        'checkoutDigest': '5' * 64, 'gitObjectFormat': 'sha1', 'scanId': '4' * 32,
                        'observedNs': 5, 'fingerprints': [fingerprint]}
        result = {'cardSha': 'a' * 64, 'viewId': 'b' * 64, 'sourceFingerprints': fingerprints,
                  'targetPath': 'concepts/a.md', 'expectedTargetHash': 'd' * 64, 'sourceSpans': [],
                  'verifiedMode': 'self', 'verifyEngine': 'claude', 'producingEngine': 'codex'}
        one = store.provenance_digests(verdict, result)
        self.assertEqual(one, store.provenance_digests(dict(reversed(list(verdict.items()))),
                                                       dict(reversed(list(result.items())))))
        self.assertNotEqual(one[0], one[1])
        self.assertTrue(all(len(value) == 64 for value in one))
        self.assertNotEqual(store.provenance_digests({**verdict, 'verdict': 'fail'}, result), one)
        for change in ({'cardSha': 'c' * 64}, {'targetPath': 'concepts/b.md'},
                       {'expectedTargetHash': 'e' * 64},
                       {'sourceFingerprints': {**fingerprints,
                           'fingerprints': [{**fingerprint, 'source_sha': '6' * 64}]}}):
            with self.subTest(change=change):
                changed = store.provenance_digests(verdict, {**result, **change})
                self.assertEqual(changed[0], one[0])
                self.assertNotEqual(changed[1], one[1])
        self.assertIsNone(store.provenance_digests({**verdict, 'claims': [float('nan')]}, result))
        self.assertIsNone(store.provenance_digests(verdict, {**result, 'cardSha': 'invalid'}))
        self.assertIsNone(store.provenance_digests(verdict, {**result, 'sourceFingerprints':
                                  {**fingerprints, 'fingerprints': [{}]}}))
        self.assertIsNone(store.provenance_digests(verdict, {**result, 'sourceSpans':
                                  [{'kind': 'bytes', 'start': 0, 'end': 2}]}))


if __name__ == '__main__':
    unittest.main()

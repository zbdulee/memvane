"""A verified compile claim becomes one ``compiled`` attestation (C3 content evidence).

Format 4 only; synthetic fixtures. The claim's sources may sit in a dot
directory; the attestation keeps no ``s_attestation_sources`` row.
"""
import unittest

from memvane.store.blob import blob_values as values
from memvane.store.tests.integration import test_s_card_sources as fixture


class CompiledAttestationChecks(fixture.CardSourceChecks):
    def test_format4_compiled_attestation(self):
        store, _, handle = self.rows_store()
        manifest = self.snapshot(store, handle, trust=False)
        fp = store.fingerprint(manifest['sources'][0]['fingerprint_id'])
        fp['path'] = '.nova/notes.md'
        fingerprint = {'schema': 'memvane-compile-source-fingerprints-v1', 'viewId': handle.view_id,
                       'checkoutDigest': handle.checkout_digest,
                       'gitObjectFormat': store._repository['gitObjectFormat'],
                       'scanId': fp['scan_id'], 'observedNs': fp['observed_ns'], 'fingerprints': [fp]}
        result = {'viewId': handle.view_id, 'targetPath': 'cards/a.md',
                  'cardSha': manifest['members'][0]['blob_sha'], 'expectedTargetHash': None,
                  'sourceFingerprints': fingerprint, 'sourceSpans': [], 'verifiedMode': 'cross-engine',
                  'verifyEngine': 'claude', 'producingEngine': 'synthetic:codex'}
        vd, rd = values.provenance_digests({'verdict': 'pass', 'claims': [], 'reasons': []}, result)
        intent = {'kind': 'compile', **result, 'verdict_digest': vd, 'result_digest': rd}
        before = store.counts()['card_attestations']
        trust = store.put_compiled_attestation(handle.worktree_id, intent, policy_digest=handle.policy_digest)
        self.assertEqual(trust, store.put_compiled_attestation(handle.worktree_id, intent,
                                                               policy_digest=handle.policy_digest))
        self.assertEqual(store.counts()['card_attestations'], before + 1)
        evidence = store.attestation(trust)
        self.assertEqual(set(evidence['receipt']), values.RECEIPT_V2_FIELDS)
        self.assertEqual(store.fingerprint(evidence['sources'][0]['fingerprint_id'])['path'], '.nova/notes.md')
        self.assertTrue(store.verify())
        self.assertEqual(store._db.execute('SELECT COUNT(*) FROM s_attestation_sources').fetchone()[0], 0)


def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite()
    for name in loader.getTestCaseNames(CompiledAttestationChecks):
        if name in CompiledAttestationChecks.__dict__:
            suite.addTest(CompiledAttestationChecks(name))
    return suite


if __name__ == '__main__':
    unittest.main()

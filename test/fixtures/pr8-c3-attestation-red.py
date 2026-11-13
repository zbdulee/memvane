"""C3 content evidence: format 3/4, synthetic fixtures only."""
import importlib.util
from pathlib import Path
import unittest

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('row_fixture', HERE / 'laya-s-card-sources-checks.py')
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)


class CompiledAttestationChecks(fixture.CardSourceChecks):
    def check_compiled(self, store, handle):
        import laya_blob_values as values
        manifest = self.snapshot(store, handle, trust=False)
        fp = store.fingerprint(manifest['sources'][0]['fingerprint_id'])
        fp['path'] = '.nova/notes.md'
        fingerprint = {'schema': 'memvane-compile-source-fingerprints-v1', 'viewId': handle.view_id,
                       'checkoutDigest': handle.checkout_digest,
                       'gitObjectFormat': self.repository_format(store),
                       'scanId': fp['scan_id'], 'observedNs': fp['observed_ns'], 'fingerprints': [fp]}
        result = {'viewId': handle.view_id, 'targetPath': 'cards/a.md',
                  'cardSha': manifest['members'][0]['blob_sha'], 'expectedTargetHash': None,
                  'sourceFingerprints': fingerprint, 'sourceSpans': [], 'verifiedMode': 'cross-engine',
                  'verifyEngine': 'claude', 'producingEngine': 'synthetic:codex'}
        vd, rd = values.provenance_digests({'verdict': 'pass', 'claims': [], 'reasons': []}, result)
        intent = {'kind': 'compile', **result, 'verdict_digest': vd, 'result_digest': rd}
        before = store.counts()['card_attestations']
        trust = store.put_compiled_attestation(handle.worktree_id, intent, policy_digest=handle.policy_digest)
        self.assertEqual(trust, store.put_compiled_attestation(handle.worktree_id, intent, policy_digest=handle.policy_digest))
        self.assertEqual(store.counts()['card_attestations'], before + 1)
        evidence = store.attestation(trust)
        self.assertEqual(set(evidence['receipt']), values.RECEIPT_V2_FIELDS)
        self.assertEqual(store.fingerprint(evidence['sources'][0]['fingerprint_id'])['path'], '.nova/notes.md')
        self.assertTrue(store.verify())
        if store._rows:
            self.assertEqual(store._db.execute('SELECT COUNT(*) FROM s_attestation_sources').fetchone()[0], 0)

    @staticmethod
    def repository_format(store):
        return store._repository['gitObjectFormat']

    def test_format3_compiled_attestation(self):
        store, _, handle = self.seeded()
        self.check_compiled(store, handle)

    def test_format4_compiled_attestation(self):
        store, _, handle = self.rows_store()
        self.check_compiled(store, handle)


if __name__ == '__main__':
    unittest.main()

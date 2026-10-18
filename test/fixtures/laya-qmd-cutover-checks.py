"""Model-free QMD-origin selection and interruption fences."""
from __future__ import annotations

import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'core'))
import laya_qmd_cutover as cutover


class QmdCutoverBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='qmd-product-cutover-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.project = self.root / 'project'
        self.auto = self.project / '.auto-context'
        self.auto.mkdir(parents=True)
        self.install = self.root / 'install'
        self.install.mkdir(mode=0o700)
        self.journal = self.install / 'install-journal.sqlite'
        self.ledger = self.install / cutover.LEDGER
        self.marker = self.auto / cutover.MARKER
        self.route = self.auto / cutover.ROUTE
        self.route_bytes = b'{"synthetic":"selected"}'
        self.row = {'schema': cutover.SCHEMA, 'epoch': 1, 'phase': 'prepared',
                    'projectRoot': str(self.project), 'journalPath': str(self.journal),
                    'routeSha256': hashlib.sha256(self.route_bytes).hexdigest(),
                    'sourceDb': str(self.root / 'qmd.sqlite'),
                    'backupDb': str(self.root / 'backup.sqlite'),
                    'sourceFormat': 'qmd-2.5.3', 'collection': 'docs',
                    'sourceDigest': 'a' * 64, 'sourceRoot': str(self.project / 'docs'),
                    'candidateDb': str(self.root / 'candidate.sqlite'),
                    'queueDb': str(self.root / 'queue.sqlite'),
                    'scope': 'b' * 32, 'indexGeneration': 'migrated',
                    'sourceManifest': 'c' * 64, 'vectorProfileId': 'd' * 64,
                    'claim': 'qmd-migration', 'candidateIncarnation': 'e' * 32,
                    'attestationSha256': 'f' * 64,
                    'providerFileIdentitySha256': '1' * 64,
                    'sourceIdentity': [1, 1], 'backupIdentity': [1, 2],
                    'candidateIdentity': [1, 3], 'queueIdentity': [1, 4]}
        cutover._write(self.ledger, self.row, replace=False)
        cutover._write(self.marker, cutover._marker(self.row), replace=False)

    def _journal(self, state, *, new_write=False):
        return {'state': state, 'epoch': 1, 'pending': {'bundleKind': 'full_plugin'},
                'newWriteSeen': new_write,
                'selected': {'bundleKind': 'full_plugin'} if state == 'active' else None,
                'selectedRouteSha256': self.row['routeSha256'] if state == 'active' else None,
                'providerAttestationSha256': self.row['attestationSha256'] if state == 'active' else None,
                'providerFileIdentitySha256': self.row['providerFileIdentitySha256'] if state == 'active' else None}

    def test_absent_active_route_blocks_and_verified_restore_allows_qmd(self):
        with patch.object(cutover, 'inspect_journal', return_value=self._journal('candidate_ready')):
            self.assertEqual(cutover.classify_marker(self.project, self.marker, self.route), 'legacy')
        self.row['phase'] = 'active'
        cutover._write(self.ledger, self.row, replace=True)
        with patch.object(cutover, 'inspect_journal', return_value=self._journal('active')):
            self.assertEqual(cutover.classify_marker(self.project, self.marker, self.route), 'blocked')
            cutover._write(self.route, self.route_bytes, replace=False)
            self.assertEqual(cutover.classify_marker(self.project, self.marker, self.route), 'candidate')
            self.route.write_bytes(b'changed route')
            self.assertEqual(cutover.classify_marker(self.project, self.marker, self.route), 'blocked')
        self.route.unlink()
        self.row['phase'] = 'restored'
        cutover._write(self.ledger, self.row, replace=True)
        with patch.object(cutover, 'inspect_journal', return_value=self._journal('restored_qmd')):
            self.assertEqual(cutover.classify_marker(self.project, self.marker, self.route), 'legacy')

    def test_activation_rechecks_persisted_attestation_before_route_publish(self):
        with (patch.object(cutover, '_check_evidence'),
              patch.object(cutover, 'inspect_journal', return_value=self._journal('candidate_ready')),
              patch.object(cutover, 'preflight_product_candidate', return_value={
                  'attestationSha256': '0' * 64,
                  'providerFileIdentitySha256': self.row['providerFileIdentitySha256'],
                  'pendingRouteSha256': self.row['routeSha256']})):
            with self.assertRaisesRegex(cutover.CutoverBlocked, 'cutover_attestation_changed'):
                cutover.activate_qmd_origin(self.project, self.journal, self.route_bytes)
        self.assertFalse(self.route.exists())
        self.assertEqual(cutover._ledger(self.ledger)['phase'], 'prepared')

    def test_first_new_write_prevents_route_removal_and_recovery_reinstates_it(self):
        cutover._write(self.route, self.route_bytes, replace=False)
        self.row['phase'] = 'active'
        cutover._write(self.ledger, self.row, replace=True)
        with patch.object(cutover, 'inspect_journal', return_value=self._journal('active', new_write=True)):
            with self.assertRaisesRegex(cutover.CutoverBlocked, 'cutover_rollback_not_authorized'):
                cutover.restore_qmd_origin(self.project, self.journal)
        self.assertEqual(self.route.read_bytes(), self.route_bytes)
        self.route.unlink()
        self.row['phase'] = 'restoring'
        cutover._write(self.ledger, self.row, replace=True)
        with patch.object(cutover, 'inspect_journal', return_value=self._journal('active', new_write=True)):
            recovered = cutover.recover_qmd_origin(self.project, self.journal, self.route_bytes)
        self.assertEqual(recovered['phase'], 'active')
        self.assertEqual(self.route.read_bytes(), self.route_bytes)


if __name__ == '__main__':
    unittest.main()

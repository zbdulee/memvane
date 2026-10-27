"""Synthetic-only package and ownership evidence checks."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'core'))
import laya_package as package


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, sort_keys=True), encoding='utf-8')


class PackageEvidenceChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='laya-package-synthetic-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / 'source'
        (self.source / 'core').mkdir(parents=True)
        for artifact in package.ARTIFACTS:
            (self.source / artifact).write_text('# synthetic ' + artifact + '\n')
        self.user_qmd = self.root / 'separate-user-qmd'
        self.user_qmd.mkdir()
        (self.user_qmd / 'preserve-me').write_text('user-owned')

    def bundle(self):
        target = self.root / 'candidate'
        result = package.prepare_candidate(self.source, target)
        self.assertEqual(result['status'], 'source_bundle_consistent')
        self.assertFalse(result['exactCommitVerified'])
        return target, result

    def receipt(self, target, candidate):
        install_id = '1' * 32
        generation = 'g-' + '2' * 32
        entries = {}
        for name, row in candidate['files'].items():
            entries[name] = {k: row[k] for k in
                             ('sha256', 'bytes', 'dev', 'ino', 'nlink', 'mtimeNs', 'ctimeNs')}
            entries[name].update(ownerStatus='confirmed', role='engine_module',
                                 exclusive=True, shared=False, references=[generation])
        ownership = {'schema': package.OWNERSHIP_SCHEMA, 'pluginRoot': str(target.resolve()),
                     'installId': install_id, 'generation': generation, 'entries': entries}
        write_json(target / 'ownership-manifest.json', ownership)
        active = {'installId': install_id, 'generation': generation,
                  'bundleManifestSha256': candidate['manifestSha256'],
                  'ownershipManifestSha256': sha(target / 'ownership-manifest.json')}
        receipt = {'schema': package.INSTALL_SCHEMA, 'pluginRoot': str(target.resolve()),
                   **active}
        write_json(target / 'install-receipt.json', receipt)
        return active, ownership

    def test_candidate_is_private_source_only_and_does_not_touch_user_qmd(self):
        target, candidate = self.bundle()
        self.assertEqual(set(candidate['files']), set(package.ARTIFACTS))
        self.assertEqual(json.loads((target / 'manifest.json').read_text())
                         ['installationStateAtPackaging'], 'uninstalled')
        self.assertEqual((self.user_qmd / 'preserve-me').read_text(), 'user-owned')
        with self.assertRaises(package.PackageEvidenceError):
            package.prepare_candidate(self.source, target)

    def test_candidate_detects_file_change_extra_file_and_symlink(self):
        target, _ = self.bundle()
        artifact = target / package.ARTIFACTS[0]
        artifact.write_text('changed')
        with self.assertRaisesRegex(package.PackageEvidenceError, 'artifact_hash_changed'):
            package.inspect_candidate(target)
        artifact.write_text((self.source / package.ARTIFACTS[0]).read_text())
        (target / 'core' / 'unlisted.py').write_text('unlisted')
        with self.assertRaisesRegex(package.PackageEvidenceError, 'unexpected_candidate_file'):
            package.inspect_candidate(target)
        (target / 'core' / 'unlisted.py').unlink()
        artifact.unlink()
        artifact.symlink_to(self.source / package.ARTIFACTS[0])
        with self.assertRaisesRegex(package.PackageEvidenceError, 'unsafe_artifact_file'):
            package.inspect_candidate(target)

    def test_active_receipt_matches_identity_but_does_not_claim_origin(self):
        target, candidate = self.bundle()
        active, ownership = self.receipt(target, candidate)
        selected = package.verified_active_artifact(
            target, target / 'install-receipt.json', target / 'ownership-manifest.json', active,
            user_qmd_roots=[self.user_qmd])
        self.assertEqual(selected['status'], 'active_receipt_consistent')
        self.assertIn('authenticate selection journal', selected['limitations'])
        self.assertFalse((target / 'core' / 'unlisted.py').exists())
        self.assertEqual((self.user_qmd / 'preserve-me').read_text(), 'user-owned')
        changed = {**active, 'installId': '3' * 32}
        with self.assertRaisesRegex(package.PackageEvidenceError, 'install_receipt_mismatch'):
            package.verified_active_artifact(target, target / 'install-receipt.json',
                                             target / 'ownership-manifest.json', changed,
                                             user_qmd_roots=[self.user_qmd])
        with self.assertRaisesRegex(package.PackageEvidenceError, 'user_qmd_root_overlap'):
            package.verified_active_artifact(target, target / 'install-receipt.json',
                                             target / 'ownership-manifest.json', active,
                                             user_qmd_roots=[target])
        ownership['entries'][package.ARTIFACTS[0]]['ino'] += 1
        write_json(target / 'ownership-manifest.json', ownership)
        with self.assertRaisesRegex(package.PackageEvidenceError, 'installed_file_identity_changed'):
            changed = {**active, 'ownershipManifestSha256': sha(target / 'ownership-manifest.json')}
            receipt = {'schema': package.INSTALL_SCHEMA, 'pluginRoot': str(target.resolve()), **changed}
            write_json(target / 'install-receipt.json', receipt)
            package.verified_active_artifact(target, target / 'install-receipt.json',
                                             target / 'ownership-manifest.json', changed,
                                             user_qmd_roots=[self.user_qmd])
        relative = package.ARTIFACTS[0]
        ownership['entries'][relative]['ino'] = candidate['files'][relative]['ino']
        ownership['entries'][relative]['nlink'] = True  # bool compares equal to integer 1
        write_json(target / 'ownership-manifest.json', ownership)
        changed['ownershipManifestSha256'] = sha(target / 'ownership-manifest.json')
        write_json(target / 'install-receipt.json',
                   {'schema': package.INSTALL_SCHEMA, 'pluginRoot': str(target.resolve()), **changed})
        with self.assertRaisesRegex(package.PackageEvidenceError, 'installed_file_identity_changed'):
            package.verified_active_artifact(target, target / 'install-receipt.json',
                                             target / 'ownership-manifest.json', changed,
                                             user_qmd_roots=[self.user_qmd])

    def test_active_receipt_rejects_hardlinked_artifact(self):
        target, candidate = self.bundle()
        active, _ = self.receipt(target, candidate)
        os.link(target / package.ARTIFACTS[0], self.root / 'shared-link')
        with self.assertRaisesRegex(package.PackageEvidenceError, 'unsafe_artifact_file'):
            package.verified_active_artifact(target, target / 'install-receipt.json',
                                             target / 'ownership-manifest.json', active,
                                             user_qmd_roots=[self.user_qmd])

    def test_inventory_alias_overlap_and_strict_json(self):
        target, candidate = self.bundle()
        active, _ = self.receipt(target, candidate)
        with self.assertRaisesRegex(package.PackageEvidenceError, 'missing_user_qmd_inventory'):
            package.verified_active_artifact(target, target / 'install-receipt.json',
                                             target / 'ownership-manifest.json', active,
                                             user_qmd_roots=[])
        alias = self.root / 'alias-to-candidate'
        alias.symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(package.PackageEvidenceError, 'user_qmd_root_overlap'):
            package.verified_active_artifact(target, target / 'install-receipt.json',
                                             target / 'ownership-manifest.json', active,
                                             user_qmd_roots=[alias])
        receipt_file = target / 'install-receipt.json'
        receipt_file.write_text(receipt_file.read_text()[:-1] + ',"schema":"duplicate"}')
        with self.assertRaisesRegex(package.PackageEvidenceError, 'duplicate_evidence_key'):
            package.verified_active_artifact(target, receipt_file,
                                             target / 'ownership-manifest.json', active,
                                             user_qmd_roots=[self.user_qmd])


if __name__ == '__main__':
    unittest.main()

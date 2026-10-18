"""Synthetic-only package, ownership, and detached-backup evidence checks."""
from __future__ import annotations

import hashlib
from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'core'))
import laya_backup_preflight as backup
import laya_index_a as index
import laya_install_binding as binding
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

    def test_project_route_requires_exact_installer_selection_and_cas_receipt(self):
        target, candidate = self.bundle()
        active, _ = self.receipt(target, candidate)
        artifact = package.verified_active_artifact(
            target, target / 'install-receipt.json', target / 'ownership-manifest.json', active,
            user_qmd_roots=[self.user_qmd])
        identity = {k: artifact[k] for k in binding.IDENTITY_FIELDS}
        scope = '4' * 32
        profile_identity = ['synthetic-model-v1', 'b' * 64, 3, 'cosine']
        profile_id = hashlib.sha256(json.dumps(
            profile_identity, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
        profile = {'profileId': profile_id, 'modelId': profile_identity[0],
                   'embeddingFingerprint': profile_identity[1],
                   'dimension': profile_identity[2], 'metric': 'cosine',
                   'approvedProviderId': 'fixture-value-only',
                   'providerRelativePath': 'core/laya_index_a.py',
                   'providerArtifactSha256': candidate['files']['core/laya_index_a.py']['sha256']}
        route = {'schema': binding.ROUTE_SCHEMA, 'status': 'active',
                 'projectRoot': str(self.root), 'settingsSha256': 'a' * 64,
                 'dbPath': str(self.root / 'synthetic-index.sqlite'),
                 'queuePath': str(self.root / 'synthetic-queue.sqlite'),
                 'dbIncarnation': '5' * 32, 'scopeId': scope,
                 'collectionName': 'synthetic', 'sourceRoot': str(self.source / 'core'),
                 'journalPath': str(target.parent / 'install-journal.sqlite'),
                 'vectorProfile': profile,
                 **identity}
        route_raw = json.dumps(route, sort_keys=True).encode()
        installer = {'schema': binding.ACTIVE_SCHEMA, 'state': 'active',
                     'selectionEpoch': 3, 'selected': identity,
                     'routeBindings': {scope: {'routeEpoch': 2,
                                               'projectRoot': str(self.root),
                                               'projectDev': self.root.stat().st_dev,
                                               'projectIno': self.root.stat().st_ino,
                                               'routeSha256': binding.route_digest(route_raw)}}}
        cas = {'schema': binding.CAS_SCHEMA, 'scopeId': scope,
               'projectRoot': str(self.root),
               'projectDev': self.root.stat().st_dev,
               'projectIno': self.root.stat().st_ino,
               'selectionEpoch': 3, 'expectedRouteEpoch': 1, 'newRouteEpoch': 2,
               'newRouteSha256': binding.route_digest(route_raw),
               'selected': identity}
        def check(route_value=route, raw=route_raw, cas_value=cas, *, host_root=None,
                  inventory=None):
            return binding.validate_binding(installer, route_value, raw, cas_value, artifact,
                                            host_project_root=host_root or self.root,
                                            user_qmd_roots=[self.user_qmd] if inventory is None else inventory)
        checked = check()
        self.assertEqual(checked['status'], 'binding_values_consistent_no_atomicity_proof')
        self.assertFalse(checked['productRouteAllowed'])
        with self.assertRaisesRegex(binding.BindingEvidenceError, 'binding_cas_receipt_mismatch'):
            check(cas_value={**cas, 'newRouteEpoch': 3})
        other = {**route, 'scopeId': '6' * 32}
        with self.assertRaisesRegex(binding.BindingEvidenceError, 'binding_identity_mismatch'):
            check(other, json.dumps(other).encode())
        wrong_root = self.root / 'other-project'
        wrong_root.mkdir()
        with self.assertRaisesRegex(binding.BindingEvidenceError, 'binding_identity_mismatch'):
            check(host_root=wrong_root)
        with self.assertRaisesRegex(binding.BindingEvidenceError, 'binding_cas_receipt_mismatch'):
            check(cas_value={**cas, 'projectIno': self.root.stat().st_ino + 1})
        with self.assertRaisesRegex(binding.BindingEvidenceError, 'host_or_inventory_unavailable'):
            check(inventory=[])
        with self.assertRaisesRegex(binding.BindingEvidenceError, 'duplicate_route_key'):
            check(raw=route_raw[:-1] + b',"schema":"duplicate"}')
        with self.assertRaisesRegex(binding.BindingEvidenceError, 'nonfinite_route_number'):
            check(raw=route_raw.replace(b'"status": "active"', b'"status": NaN'))

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


class BackupEvidenceChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='laya-backup-synthetic-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'synthetic-source.sqlite'
        db = index.LayaIndexA(self.source, create=True)
        self.db_incarnation = db.incarnation()
        db.close()
        self.backups = self.root / 'detached-backups'
        self.backups.mkdir(mode=0o700)
        fresh = self.backups / 'fresh.sqlite'
        with closing(sqlite3.connect(self.source)) as source:
            with closing(sqlite3.connect(fresh)) as dest:
                source.backup(dest)
                dest.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        os.replace(fresh, self.backups / 'index.sqlite')
        self.selected = {'installId': '1' * 32, 'generation': 'g-' + '2' * 32,
                         'bundleManifestSha256': 'a' * 64,
                         'dbIncarnation': self.db_incarnation}
        self.receipt = {'schema': backup.SCHEMA, **self.selected,
                        'backupName': 'index.sqlite', 'backupSha256': sha(self.backups / 'index.sqlite'),
                        'expectedHeads': []}
        write_json(self.backups / 'backup-receipt.json', self.receipt)

    def check(self, **kwargs):
        return backup.preflight(self.backups, self.backups / 'backup-receipt.json',
                                self.selected, **kwargs)

    def test_backup_requires_independent_restore_and_never_authorizes_rollback(self):
        checked = self.check()
        self.assertEqual(checked['status'], 'backup_consistent_restore_required')
        self.assertEqual(checked['installId'], self.selected['installId'])
        self.assertEqual(checked['generation'], self.selected['generation'])
        self.assertEqual(checked['bundleManifestSha256'], self.selected['bundleManifestSha256'])
        self.assertEqual(checked['dbIncarnation'], self.db_incarnation)
        self.assertEqual(checked['receiptIdentity']['sha256'],
                         sha(self.backups / 'backup-receipt.json'))
        self.assertEqual(checked['backupIdentity']['sha256'], self.receipt['backupSha256'])
        self.assertFalse(checked['rollbackAuthorized'])
        shutil.copy2(self.backups / 'index.sqlite', self.backups / 'restored-index.sqlite')
        checked = self.check(restore_name='restored-index.sqlite')
        self.assertEqual(checked['status'], 'backup_and_restore_consistent_source_unproved')
        self.assertFalse(checked['rollbackAuthorized'])

    def test_backup_rejects_bad_digest_head_and_selection(self):
        self.receipt['backupSha256'] = '0' * 64
        write_json(self.backups / 'backup-receipt.json', self.receipt)
        with self.assertRaisesRegex(backup.BackupEvidenceError, 'backup_hash_changed'):
            self.check()
        self.receipt['backupSha256'] = sha(self.backups / 'index.sqlite')
        self.receipt['expectedHeads'] = [['some-scope', 'some-gen']]
        write_json(self.backups / 'backup-receipt.json', self.receipt)
        with self.assertRaisesRegex(backup.BackupEvidenceError, 'backup_heads_mismatch'):
            self.check()
        self.receipt['expectedHeads'] = []
        write_json(self.backups / 'backup-receipt.json', self.receipt)
        with self.assertRaisesRegex(backup.BackupEvidenceError, 'backup_receipt_mismatch'):
            backup.preflight(self.backups, self.backups / 'backup-receipt.json',
                             {**self.selected, 'generation': 'g-' + '3' * 32})
        self.receipt['dbIncarnation'] = '0' * 32
        write_json(self.backups / 'backup-receipt.json', self.receipt)
        with self.assertRaisesRegex(backup.BackupEvidenceError, 'backup_incarnation_mismatch'):
            backup.preflight(self.backups, self.backups / 'backup-receipt.json',
                             {**self.selected, 'dbIncarnation': '0' * 32})

    def test_backup_rejects_sidecar_and_hardlink_restore(self):
        (self.backups / 'index.sqlite-wal').write_text('fake')
        with self.assertRaisesRegex(backup.BackupEvidenceError, 'backup_has_sidecar'):
            self.check()
        (self.backups / 'index.sqlite-wal').unlink()
        os.link(self.backups / 'index.sqlite', self.backups / 'restored-index.sqlite')
        with self.assertRaisesRegex(backup.BackupEvidenceError, 'unsafe_backup_db'):
            self.check(restore_name='restored-index.sqlite')

    def test_nonempty_heads_and_strict_receipt_types(self):
        db = index.LayaIndexA(self.source)
        self.assertEqual(db.stage('g0', 'alpha', [('one', 'target body', True)]), 'prepared')
        self.assertEqual(db.publish(None, 'g0', 'claim-0'), 'published')
        db.close()
        fresh = self.backups / 'new-head.sqlite'
        with closing(sqlite3.connect(self.source)) as source:
            with closing(sqlite3.connect(fresh)) as dest:
                source.backup(dest)
                dest.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        os.replace(fresh, self.backups / 'index.sqlite')
        self.receipt['expectedHeads'] = [['alpha', 'g0']]
        self.receipt['backupSha256'] = sha(self.backups / 'index.sqlite')
        write_json(self.backups / 'backup-receipt.json', self.receipt)
        self.assertEqual(self.check()['expectedHeads'], [['alpha', 'g0']])
        self.receipt['expectedHeads'] = [['alpha', 'wrong']]
        write_json(self.backups / 'backup-receipt.json', self.receipt)
        with self.assertRaisesRegex(backup.BackupEvidenceError, 'backup_heads_mismatch'):
            self.check()
        self.receipt['expectedHeads'] = [['alpha', 'g0']]
        self.receipt['installId'] = 1
        write_json(self.backups / 'backup-receipt.json', self.receipt)
        with self.assertRaisesRegex(backup.BackupEvidenceError, 'backup_receipt_mismatch'):
            self.check()
        self.receipt['installId'] = self.selected['installId']
        receipt_file = self.backups / 'backup-receipt.json'
        write_json(receipt_file, self.receipt)
        receipt_file.write_text(receipt_file.read_text()[:-1] + ',"schema":"duplicate"}')
        with self.assertRaisesRegex(backup.BackupEvidenceError, 'duplicate_backup_receipt_key'):
            self.check()
        write_json(receipt_file, self.receipt)
        receipt_file.write_text(receipt_file.read_text().replace(
            '"installId": "' + self.selected['installId'] + '"', '"installId": NaN'))
        with self.assertRaisesRegex(backup.BackupEvidenceError, 'nonfinite_backup_receipt_number'):
            self.check()


if __name__ == '__main__':
    unittest.main()

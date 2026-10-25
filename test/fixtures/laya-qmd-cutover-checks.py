"""Model-free QMD-origin selection and interruption fences."""
from __future__ import annotations

import hashlib
import json
from contextlib import closing
from pathlib import Path
import runpy
import sqlite3
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
                'pendingRouteSha256': self.row['routeSha256'] if state == 'candidate_ready' else None,
                'newWriteSeen': new_write,
                'selected': {'bundleKind': 'full_plugin'} if state == 'active' else None,
                'selectedRouteSha256': self.row['routeSha256'] if state == 'active' else None,
                'providerAttestationSha256': self.row['attestationSha256'] if state == 'active' else None,
                'providerFileIdentitySha256': self.row['providerFileIdentitySha256'] if state == 'active' else None}

    def _v2(self, phase):
        self.row['schema'] = cutover.SCHEMA_V2
        del self.row['collection'], self.row['sourceRoot']
        self.row['collections'] = [
            {'name': 'docs', 'role': 'raw', 'sourceRoot': str(self.project / 'docs')},
            {'name': 'wiki', 'role': 'wiki', 'sourceRoot': str(self.auto / 'wiki')}]
        self.row['phase'] = phase
        cutover._write(self.ledger, self.row, replace=True)

    def _stale_settings_route(self):
        settings = self.auto / 'settings.json'
        original = b'{}'
        settings.write_bytes(original)
        route = {'schema': 'laya-index-route-v2',
                 'collections': self.row['collections'],
                 'settingsSha256': hashlib.sha256(original).hexdigest()}
        settings.write_bytes(b'{"changed":true}')
        return route

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
        with (patch.object(cutover, '_preflight_bound_settings'),
              patch.object(cutover, '_check_evidence'),
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

    def test_v2_marker_requires_complete_two_collection_ledger(self):
        self._v2('active')
        cutover._write(self.route, self.route_bytes, replace=False)
        with patch.object(cutover, 'inspect_journal', return_value=self._journal('active')):
            self.assertEqual(cutover.classify_marker(
                self.project, self.marker, self.route), 'candidate')
            self.row['collections'][1]['name'] = 'docs'
            cutover._write(self.ledger, self.row, replace=True)
            self.assertEqual(cutover.classify_marker(
                self.project, self.marker, self.route), 'blocked')

    def test_v2_activation_rejects_settings_changed_after_bind(self):
        self._v2('prepared')
        route = self._stale_settings_route()
        with patch('laya_install_journal._route', return_value=(route, self.row['routeSha256'])):
            with self.assertRaisesRegex(cutover.CutoverBlocked, 'cutover_settings_changed'):
                cutover.activate_qmd_origin(self.project, self.journal, self.route_bytes)
        self.assertFalse(self.route.exists())
        self.assertEqual(cutover._ledger(self.ledger)['phase'], 'prepared')

    def test_v2_candidate_ready_recovery_reverts_changed_settings(self):
        self._v2('switching')
        route = self._stale_settings_route()
        cutover._write(self.route, self.route_bytes, replace=False)
        with (patch.object(cutover, 'inspect_journal', return_value=self._journal('candidate_ready')),
              patch('laya_install_journal._route', return_value=(route, self.row['routeSha256']))):
            recovered = cutover.recover_qmd_origin(
                self.project, self.journal, self.route_bytes)
        self.assertEqual(recovered['phase'], 'prepared')
        self.assertFalse(self.route.exists())

    def test_v1_activation_rejects_settings_changed_after_bind(self):
        original = b'{}'
        settings = self.auto / 'settings.json'
        settings.write_bytes(original)
        route = {'schema': 'laya-index-route-v1', 'collectionName': 'docs',
                 'sourceRoot': self.row['sourceRoot'],
                 'settingsSha256': hashlib.sha256(original).hexdigest()}
        settings.write_bytes(b'{"changed":true}')
        with patch('laya_install_journal._route', return_value=(route, self.row['routeSha256'])):
            with self.assertRaisesRegex(cutover.CutoverBlocked, 'cutover_settings_changed'):
                cutover.activate_qmd_origin(self.project, self.journal, self.route_bytes)
        self.assertEqual(cutover._ledger(self.ledger)['phase'], 'prepared')
        self.assertFalse(self.route.exists())

    def test_candidate_ready_recovery_reverts_changed_attestation(self):
        self.row['phase'] = 'switching'
        cutover._write(self.ledger, self.row, replace=True)
        cutover._write(self.route, self.route_bytes, replace=False)
        approval = {'pendingRouteSha256': self.row['routeSha256'],
                    'attestationSha256': '0' * 64,
                    'providerFileIdentitySha256': self.row['providerFileIdentitySha256']}
        with (patch.object(cutover, 'inspect_journal', return_value=self._journal('candidate_ready')),
              patch.object(cutover, '_preflight_bound_settings'),
              patch.object(cutover, '_check_evidence'),
              patch.object(cutover, 'preflight_product_candidate', return_value=approval)):
            recovered = cutover.recover_qmd_origin(
                self.project, self.journal, self.route_bytes)
        self.assertEqual(recovered['phase'], 'prepared')
        self.assertFalse(self.route.exists())

    def test_candidate_ready_recovery_preserves_route_for_other_pending_identity(self):
        self.row['phase'] = 'switching'
        cutover._write(self.ledger, self.row, replace=True)
        cutover._write(self.route, self.route_bytes, replace=False)
        journal = self._journal('candidate_ready')
        journal['pendingRouteSha256'] = '0' * 64
        with patch.object(cutover, 'inspect_journal', return_value=journal):
            with self.assertRaisesRegex(cutover.CutoverBlocked, 'cutover_journal_uncertain'):
                cutover.recover_qmd_origin(self.project, self.journal, self.route_bytes)
        self.assertEqual(cutover._ledger(self.ledger)['phase'], 'switching')
        self.assertEqual(self.route.read_bytes(), self.route_bytes)

    def test_prepared_ledger_rejects_wrong_route_before_marker_recovery(self):
        self.marker.unlink()
        with patch.object(cutover, '_recover_missing_marker',
                          side_effect=AssertionError('must not complete marker')):
            with self.assertRaisesRegex(cutover.CutoverBlocked, 'cutover_route_mismatch'):
                cutover.recover_qmd_origin(self.project, self.journal, b'other')
        self.assertFalse(self.marker.exists())

    def test_prepared_active_journal_recovers_forward_after_withdrawal_race(self):
        with patch.object(cutover, 'inspect_journal', return_value=self._journal('active')):
            recovered = cutover.recover_qmd_origin(
                self.project, self.journal, self.route_bytes)
        self.assertEqual(recovered['phase'], 'active')
        self.assertEqual(self.route.read_bytes(), self.route_bytes)

    def test_v2_committed_recovery_settles_without_mutable_source_evidence(self):
        self._v2('switching')
        cutover._write(self.route, self.route_bytes, replace=False)
        with (patch.object(cutover, 'inspect_journal', return_value=self._journal('active')),
              patch.object(cutover, '_check_evidence', side_effect=AssertionError('stale source'))):
            recovered = cutover.recover_qmd_origin(
                self.project, self.journal, self.route_bytes)
        self.assertEqual(recovered['phase'], 'active')
        self.assertEqual(self.route.read_bytes(), self.route_bytes)

    def test_prepared_ledger_recovers_missing_marker_from_bound_evidence(self):
        self.marker.unlink()
        approval = {'pendingRouteSha256': self.row['routeSha256'],
                    'attestationSha256': self.row['attestationSha256'],
                    'providerFileIdentitySha256': self.row['providerFileIdentitySha256']}
        with (patch.object(cutover, 'inspect_journal', return_value=self._journal('candidate_ready')),
              patch.object(cutover, '_preflight_bound_settings'),
              patch.object(cutover, '_check_evidence'),
              patch.object(cutover, 'preflight_product_candidate', return_value=approval)):
            recovered = cutover.recover_qmd_origin(
                self.project, self.journal, self.route_bytes)
        self.assertEqual(recovered['phase'], 'prepared')
        self.assertEqual(cutover._read_json(self.marker), cutover._marker(self.row))
        self.assertFalse(self.route.exists())

    def test_missing_marker_recovery_rejects_changed_attestation(self):
        self.marker.unlink()
        approval = {'pendingRouteSha256': self.row['routeSha256'],
                    'attestationSha256': '0' * 64,
                    'providerFileIdentitySha256': self.row['providerFileIdentitySha256']}
        with (patch.object(cutover, 'inspect_journal', return_value=self._journal('candidate_ready')),
              patch.object(cutover, '_preflight_bound_settings'),
              patch.object(cutover, '_check_evidence'),
              patch.object(cutover, 'preflight_product_candidate', return_value=approval)):
            with self.assertRaisesRegex(cutover.CutoverBlocked, 'cutover_attestation_changed'):
                cutover.recover_qmd_origin(self.project, self.journal, self.route_bytes)
        self.assertFalse(self.marker.exists())
        self.assertFalse(self.route.exists())

    def test_auto_wiki_two_collection_bind_and_missing_marker_recovery(self):
        from laya_index_a import LayaIndexA
        from laya_index_claim_queue import ClaimQueue
        from laya_index_source_port import scan_collections
        from laya_index_vector import qmd_markdown_title
        from laya_qmd_migration import (QMD_253, backup_qmd_collections,
                                        inspect_qmd_collections,
                                        prepare_qmd_candidate)

        helpers = runpy.run_path(str(Path(__file__).with_name(
            'laya-qmd-migration-checks.py')))
        make_source = helpers['make_source']
        raw_body, wiki_body = 'astral manuscript', '# Card\nverified summary'
        raw_root, wiki_root = self.project / 'docs', self.auto / 'wiki'
        raw_root.mkdir()
        wiki_root.mkdir()
        (raw_root / 'live.md').write_text(raw_body, encoding='utf-8')
        (wiki_root / 'card.md').write_text(wiki_body, encoding='utf-8')
        settings = {
            'collections': ['wiki', 'docs'], 'indexing': True,
            'collectionPaths': {'docs': 'docs', 'wiki': '.auto-context/wiki'},
            'collectionRoles': {'docs': 'raw', 'wiki': 'wiki'},
            'recallStrategy': 'hierarchical', 'wikiPath': '.auto-context/wiki',
            'compile': {'mode': 'auto-wiki',
                        'triggers': ['post_tool_source'],
                        'extractor': {'builtins': ['claude']}}}
        settings_bytes = json.dumps(settings).encode('utf-8')
        (self.auto / 'settings.json').write_bytes(settings_bytes)
        names = ('docs', 'wiki')
        roots = {'docs': str(raw_root), 'wiki': str(wiki_root)}
        files = {'wiki/card.md': wiki_body}
        source, backup = self.root / 'qmd.sqlite', self.root / 'backup.sqlite'
        make_source(source, QMD_253, documents=[
            ('docs', 'live.md', raw_body, 1),
            ('wiki', 'card.md', wiki_body, 1)])
        with sqlite3.connect(source) as db:
            db.executemany('UPDATE store_collections SET path=? WHERE name=?',
                           [(root, name) for name, root in roots.items()])
        backup_qmd_collections(source, QMD_253, names, backup)
        source_digest = inspect_qmd_collections(source, QMD_253, names)['digest']
        candidate, queue_path = self.root / 'candidate.sqlite', self.root / 'queue.sqlite'
        scope, generation = 'a' * 32, 'gen-1'
        with closing(LayaIndexA(self.root / 'scan.sqlite', create=True)) as scan_index:
            manifest = scan_collections({'wiki': roots['wiki']}, scope, scan_index).manifest
        vector_batch = {
            'scope': scope, 'source_manifest': manifest,
            'qmd_source_digest': source_digest, 'model_id': 'synthetic-approved',
            'embedding_fingerprint': 'b' * 64, 'dimension': 2,
            'chunks': [{'hash': hashlib.sha256(body.encode('utf-8')).hexdigest(),
                        'title': qmd_markdown_title(body, path), 'seq': 0,
                        'pos': 0, 'vector': [1.0, 0.0]}
                       for path, body in sorted(files.items())]}
        prepared = prepare_qmd_candidate(
            backup, QMD_253, names, files, candidate, scope, generation,
            migration_allowed=True, expected_collection_root=roots,
            expected_source_digest=source_digest, vector_batch=vector_batch,
            provider_approved=True, current_source_db=source,
            scope_collections=names)
        with closing(LayaIndexA(candidate)) as index:
            incarnation = index.incarnation()
        with closing(ClaimQueue.create(queue_path, index_incarnation=incarnation)):
            pass
        route = {'schema': 'laya-index-route-v2', 'projectRoot': str(self.project),
                 'dbPath': str(candidate), 'queuePath': str(queue_path),
                 'scopeId': scope, 'collections': [
                     {'name': name, 'role': 'raw' if name == 'docs' else 'wiki',
                      'sourceRoot': roots[name]} for name in names],
                 'settingsSha256': hashlib.sha256(settings_bytes).hexdigest(),
                 'vectorProfile': {'profileId': prepared['vector_profile_id']},
                 'dbIncarnation': incarnation}
        approval = {'pendingRouteSha256': self.row['routeSha256'],
                    'attestationSha256': self.row['attestationSha256'],
                    'providerFileIdentitySha256': self.row['providerFileIdentitySha256']}
        journal = self._journal('candidate_ready')
        self.marker.unlink()
        self.ledger.unlink()
        with (patch('laya_install_journal._route', return_value=(route, self.row['routeSha256'])),
              patch.object(cutover, 'inspect_journal', return_value=journal),
              patch.object(cutover, 'preflight_product_candidate', return_value=approval)):
            bound = cutover.bind_qmd_origin(
                project_root=self.project, journal_path=self.journal,
                route_bytes=self.route_bytes, source_db=source, backup_db=backup,
                source_format=QMD_253, collection=names,
                source_digest=source_digest, source_root=roots,
                candidate_db=candidate, queue_db=queue_path, scope=scope,
                index_generation=generation, source_manifest=manifest,
                vector_profile_id=prepared['vector_profile_id'],
                claim='qmd-migration')
            self.assertEqual(bound['phase'], 'prepared')
            self.marker.unlink()  # synthetic crash after ledger, before marker
            recovered = cutover.recover_qmd_origin(
                self.project, self.journal, self.route_bytes)
        self.assertEqual(recovered['phase'], 'prepared')
        self.assertEqual(cutover._read_json(self.marker), cutover._marker(bound))
        self.assertFalse(self.route.exists())


if __name__ == '__main__':
    unittest.main()

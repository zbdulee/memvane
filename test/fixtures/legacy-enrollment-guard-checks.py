"""A legacy selected QMD cannot silently gain a new owner or start embedding."""
import hashlib
import contextlib
import io
import json
from pathlib import Path
import runpy
import sqlite3
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, 'core')
import qmd_shared_store as store
import shared_qmd_setup as setup
import setup_guard
import setup_entry

Shared = runpy.run_path('test/fixtures/shared-qmd-setup-checks.py')['SharedSetup']
Migration = runpy.run_path('test/fixtures/laya-qmd-migration-checks.py')


class LegacyEnrollmentGuard(Shared):
    def setUp(self):
        area = tempfile.TemporaryDirectory(prefix='qmd-legacy-enroll-',
                                           dir=Path.cwd().parent)
        self.addCleanup(area.cleanup)
        with patch('tempfile.tempdir', area.name):
            super().setUp()
        self.old = self.legacy_selected_store()
        self.qa = self.home / 'qa-project'
        (self.qa / 'docs').mkdir(parents=True)
        (self.qa / '.auto-context/wiki').mkdir(parents=True)
        settings = self.qa / '.auto-context/settings.json'
        settings.write_text(json.dumps({
            'indexing': True,
            'collections': ['qa-raw', 'qa-wiki'],
            'collectionPaths': {'qa-raw': 'docs', 'qa-wiki': '.auto-context/wiki'},
            'collectionRoles': {'qa-raw': 'raw', 'qa-wiki': 'wiki'},
        }))

    def test_new_settings_do_not_enroll_a_legacy_project(self):
        self.assertEqual(setup_guard.status(self.qa)['reason'],
                         'project_shared_migration_not_approved')
        with self.assertRaisesRegex(ValueError, 'project_shared_migration_not_approved'):
            store.check_project(self.qa)
        self.assertFalse((store.root() / store.LEGACY_ENROLL_GRANT).exists())
        self.assertEqual(self.old['pointer'].read_bytes(), self.old['pointerBytes'])

    def test_prepared_intent_cannot_queue_without_original_model_proof(self):
        request = {'project': str(self.qa),
                   'generation': str(self.old['generation']),
                   'pointerSha256': hashlib.sha256(self.old['pointerBytes']).hexdigest(),
                   'legacySelected': {'pointer': setup._private_snapshot(
                       self.old['pointer'])[0]}}
        accepted = setup.coordinator.digest(request)
        base = store.root()
        backup = base / ('legacy-enrollment-backup-' + accepted[:24] + '.sqlite')
        backup.write_bytes(b'synthetic private backup marker')
        backup.chmod(0o600)
        setup._save_legacy(base, {'schema': 'qmd-legacy-enrollment-v1',
            'phase': 'prepared', 'request': request, 'requestSha256': accepted,
            'backupPath': str(backup),
            'backupVerified': True})
        args = SimpleNamespace(project=str(self.qa),
            accepted_legacy_enrollment_sha256=accepted,
            approve_project_enrollment=True, approve_index_execution=True,
            writer_wait_seconds=0)
        with self.assertRaisesRegex(ValueError, 'legacy_embedding_model_proof_required'):
            setup.activate_legacy_enrollment(args)
        self.assertEqual(setup._legacy_journal(base)['phase'], 'prepared')
        self.assertFalse((base / store.LEGACY_ENROLL_GRANT).exists())
        self.assertEqual(self.old['pointer'].read_bytes(), self.old['pointerBytes'])
        self.assertEqual((self.old['generation'] / 'index.sqlite').read_bytes(),
                         self.old['indexBytes'])
        self.assertEqual(setup_guard.status(self.qa)['reason'],
                         'project_shared_migration_not_approved')

    def test_grandfathered_owner_stays_routed_without_new_name_authority(self):
        # Legacy registered projects keep their existing route. Merely adding
        # a name to settings cannot reserve another shared collection.
        (self.project / 'docs').mkdir()
        settings = self.project / '.auto-context/settings.json'
        config = {'indexing': True, 'collections': ['old-docs'],
                  'collectionPaths': {'old-docs': 'docs'},
                  'collectionRoles': {'old-docs': 'raw'}}
        settings.write_text(json.dumps(config))
        store._reserve_owners(store.root(), {
            'old-docs': {'scope': str(self.project),
                         'path': str(self.project / 'docs'), 'indexed': True}})
        store.check_project(self.project)
        store.register_config(self.project, config)
        (self.project / 'new-docs').mkdir()
        config['collections'].append('new-docs')
        config['collectionPaths']['new-docs'] = 'new-docs'
        config['collectionRoles']['new-docs'] = 'raw'
        settings.write_text(json.dumps(config))
        with self.assertRaisesRegex(ValueError, 'project_shared_migration_not_approved'):
            store.check_project(self.project)
        with self.assertRaisesRegex(ValueError, 'project_shared_migration_not_approved'):
            store.register_config(self.project, config)
        self.assertNotIn('new-docs', store._registry(store.root()))

    def _reviewable_source(self):
        """Three real QMD 2.5.3 rows, all in this disposable HOME."""
        roots = {'docs': self.project / 'docs',
                 'wiki': self.project / '.auto-context/wiki',
                 'other': self.project / 'other'}
        for root in roots.values():
            root.mkdir(parents=True, exist_ok=True)
        settings = self.project / '.auto-context/settings.json'
        settings.write_text(json.dumps({
            'indexing': True, 'collections': sorted(roots),
            'collectionPaths': {name: str(root.relative_to(self.project))
                                for name, root in roots.items()},
            'collectionRoles': {name: 'raw' for name in roots}}))
        physical = self.home / 'reviewed-qmd.sqlite'
        Migration['make_source'](physical, Migration['QMD_253'])
        with sqlite3.connect(physical) as db:
            db.executemany('UPDATE store_collections SET path=? WHERE name=?',
                           [(str(root), name) for name, root in roots.items()])
        store._write_registry(store.root(), {
            name: {'scope': str(self.project), 'path': str(root), 'indexed': True}
            for name, root in roots.items()})
        selected = {'generation': str(self.old['generation']),
                    'INDEX_PATH': str(physical), 'selected': True, 'shared': True}
        pinned = {'generation': selected['generation'],
                  'pointer': setup._private_snapshot(self.old['pointer'])[0],
                  'dimension': 768}
        return physical, selected, pinned

    def _entry(self, action, *options):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = setup_entry.main([action, '--project', str(self.qa), *options])
        return code, json.loads(out.getvalue())

    def test_plan_prepare_real_backup_restore_and_exact_cas(self):
        physical, selected, pinned = self._reviewable_source()
        before = physical.read_bytes()
        with patch.object(store, 'select', return_value=selected), \
             patch.object(setup, '_selected_replacement', return_value=pinned):
            code, planned = self._entry('plan-legacy-enroll')
            self.assertEqual(code, 0, planned)
            self.assertEqual(planned['status'], 'review_required')
            self.assertEqual(planned['activationBlockers'],
                             ['legacy_embedding_model_proof_required'])
            self.assertEqual(planned['request']['collectionNames'],
                             ['qa-raw', 'qa-wiki'])
            wrong = '0' * 64
            code, refused = self._entry('prepare-legacy-enroll',
                '--approve-project-enrollment',
                '--accepted-legacy-enrollment-sha256', wrong)
            self.assertEqual(code, 1, refused)
            self.assertEqual(refused['reason'], 'legacy_enrollment_proposal_changed')
            self.assertFalse((store.root() / store.LEGACY_ENROLL_JOURNAL).exists())
            code, prepared = self._entry('prepare-legacy-enroll',
                '--approve-project-enrollment',
                '--accepted-legacy-enrollment-sha256', planned['proposalSha256'])
            self.assertEqual(code, 0, prepared)
            self.assertEqual(prepared['status'], 'prepared_inactive')
            self.assertTrue(Path(prepared['backup']).is_file())
            self.assertEqual(Path(prepared['backup']).stat().st_mode & 0o777, 0o600)
            self.assertEqual(physical.read_bytes(), before)
            self.assertEqual(self.old['pointer'].read_bytes(), self.old['pointerBytes'])
            self.assertFalse((store.root() / store.LEGACY_ENROLL_GRANT).exists())
            code, refused = self._entry('activate-legacy-enroll',
                '--approve-project-enrollment', '--approve-index-execution',
                '--accepted-legacy-enrollment-sha256', planned['proposalSha256'])
            self.assertEqual(code, 1, refused)
            self.assertEqual(refused['reason'], 'legacy_embedding_model_proof_required')
            self.assertEqual(setup._legacy_journal(store.root())['phase'], 'prepared')
            self.assertFalse((store.root() / store.LEGACY_ENROLL_GRANT).exists())
            self.assertEqual(physical.read_bytes(), before)

    def test_changed_source_refuses_preparation_before_any_backup(self):
        physical, selected, pinned = self._reviewable_source()
        with patch.object(store, 'select', return_value=selected), \
             patch.object(setup, '_selected_replacement', return_value=pinned):
            code, planned = self._entry('plan-legacy-enroll')
            self.assertEqual(code, 0, planned)
            with sqlite3.connect(physical) as db:
                db.execute("UPDATE documents SET title='changed synthetic title' "
                           "WHERE collection='docs' AND path='live.md'")
            code, refused = self._entry('prepare-legacy-enroll',
                '--approve-project-enrollment',
                '--accepted-legacy-enrollment-sha256', planned['proposalSha256'])
            self.assertEqual(code, 1, refused)
            self.assertEqual(refused['reason'], 'legacy_enrollment_proposal_changed')
            self.assertFalse((store.root() / store.LEGACY_ENROLL_JOURNAL).exists())


if __name__ == '__main__':
    suite = unittest.TestSuite(LegacyEnrollmentGuard(name) for name in
        LegacyEnrollmentGuard.__dict__ if name.startswith('test_'))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(not result.wasSuccessful())

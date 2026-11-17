"""Interrupted enrollment recovers without rewriting unchanged project settings."""
import hashlib
import json
import os
from pathlib import Path
import stat
from unittest import mock

from tests.support.case import FixtureCase
from memvane.learning import default_enrollment, project_learning_setup as setup
from memvane.learning import project_learning_recovery as recovery
from memvane.learning.context_learning import learning_profile, selector_heal
from memvane.learning.context_learning.shared import _project_key
from memvane.learning.tests.integration import test_settings_profile_113 as settings_fixture


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def save(path, value):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True) + '\n', encoding='utf8')
    path.chmod(0o600)


class Recovery115(FixtureCase):
    def setUp(self):
        super().setUp()
        self.project = self.home / 'project'
        folder = self.project / '.memvane'
        folder.mkdir(mode=0o755, parents=True)
        self.settings = folder / 'settings.json'
        self.settings.write_text('{"name":"fixture"}', encoding='utf8')
        self.settings.chmod(0o644)

    def settings_snapshot(self):
        info = self.settings.stat()
        return (self.settings.read_bytes(), stat.S_IMODE(info.st_mode),
                info.st_ino, info.st_mtime_ns)

    def namespace_snapshot(self, root):
        return {str(path.relative_to(root)): path.read_bytes()
                for path in root.rglob('*') if path.is_file() and not path.name.endswith('.lock')}

    def transaction(self, root, *, phase='installing', profile=False, policy=False):
        """A fixed interrupted journal image; no runtime/model or production state."""
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        (root / setup.BACKUPS).mkdir(mode=0o700)
        backup = root / setup.BACKUPS / 'fixture'
        backup.mkdir(mode=0o700)
        original = self.settings.read_bytes()
        (backup / 'settings.json').write_bytes(original)
        (backup / 'settings.json').chmod(0o600)
        plan = {'mode': 'default-local', 'projectRoot': str(self.project), 'namespaceRoot': str(root)}
        accepted = setup.proposal_sha256(plan)
        files = {'settings.json': sha(original)}
        written = {}
        if profile:
            save(root / learning_profile.NAME, {'interrupted': True})
            written[learning_profile.NAME] = sha((root / learning_profile.NAME).read_bytes())
        if policy:
            save(root / 'shared-policy.json', {'stage': 'installed'})
            written['shared-policy.json'] = sha((root / 'shared-policy.json').read_bytes())
            save(backup / 'namespace/shared-policy.json', {'stage': 'before'})
            files['namespace/shared-policy.json'] = sha((backup / 'namespace/shared-policy.json').read_bytes())
        paths = ['shared-policy.json', 'project-learning-authorization.json',
                 'learning-profile.json', learning_profile.INSTALL,
                 'consents/' + _project_key(self.project) + '.json']
        save(backup / 'manifest.json', {'schema': setup.SCHEMA, 'acceptedSha256': accepted, 'files': files})
        save(root / setup.NAME, {'schema': setup.SCHEMA, 'phase': phase, 'plan': plan,
             'acceptedSha256': accepted, 'backupRoot': str(backup), 'written': written,
             'configurationPaths': paths, 'sourceSettingsSha256': sha(original)})
        return backup

    def recover(self, root):
        # Direct recovery cases isolate route ownership; all recovery IO is real.
        with mock.patch.object(default_enrollment, 'authorized', return_value=True):
            return setup.recover(self.project, root)

    def entry(self, *, old_profile=False):
        before = self.settings_snapshot()
        def seed(root, runtime, model, source):
            default_enrollment._prepare_namespace(root, runtime, model)
            default_enrollment.record(self.project, self.home / 'install', 'fixture')
            intent = json.loads((self.home / 'install' / default_enrollment.NAME).read_text())
            save(root / default_enrollment.BINDING, intent)
            self.transaction(root, phase='installing' if old_profile else 'recovering', profile=old_profile)
            if old_profile:
                anchor = self.home / 'old-cache/memvane/learning/context_learning/adapter.py'
                anchor.parent.mkdir(mode=0o700, parents=True)
                anchor.write_bytes(selector_heal.running_adapter().read_bytes())
                anchor.chmod(0o600)
                policy = json.loads((root / 'live-selector.json').read_text())
                policy['trainerArgv'][1] = str(anchor)
                save(root / 'live-selector.json', policy)
        def healed(project, *, state, **_kwargs):
            self.assertEqual(json.loads((state / setup.NAME).read_text())['phase'], 'recovered')
            self.assertFalse((state / learning_profile.NAME).exists())
            self.assertEqual(self.settings_snapshot(), before)
            return {'status': 'selector_ready'}
        # Selected host inventory is already synthetic in the 113 entry fixture.
        with mock.patch.object(selector_heal, '_official'):
            root = settings_fixture.SettingsProfile.run_profile_entry(
                self, seed_namespace=seed, heal_impl=healed)
        self.assertEqual(json.loads((root / setup.NAME).read_text())['phase'], 'activated')
        return root

    def test_recovering_0644_unchanged_settings_reaches_profile_and_grant(self):
        self.entry()

    def test_interrupted_profile_with_old_anchor_recovers_before_canonicalize(self):
        self.entry(old_profile=True)

    def test_unsafe_or_user_changed_settings_leave_journal_and_namespace_unchanged(self):
        for kind in ('changed', 'group_writable', 'symlink', 'hardlink'):
            with self.subTest(kind=kind):
                root = self.home / ('namespace-' + kind)
                original = self.settings.read_bytes()
                self.transaction(root, policy=True)
                alias = self.settings.with_name(kind + '-target.json')
                if kind == 'changed':
                    self.settings.write_text('{"name":"user-edited"}', encoding='utf8')
                elif kind == 'group_writable':
                    self.settings.chmod(0o664)
                elif kind == 'symlink':
                    self.settings.rename(alias)
                    self.settings.symlink_to(alias)
                else:
                    os.link(self.settings, alias)
                before = self.namespace_snapshot(root)
                settings_before = self.settings_snapshot()
                try:
                    reason = 'setup_recovery_settings_changed' if kind == 'changed' else 'unsafe'
                    with self.assertRaisesRegex(ValueError, reason):
                        self.recover(root)
                    self.assertEqual(self.namespace_snapshot(root), before)
                    self.assertEqual(self.settings_snapshot(), settings_before)
                finally:
                    if kind == 'symlink':
                        self.settings.unlink()
                        alias.rename(self.settings)
                    elif kind == 'hardlink':
                        alias.unlink()
                    self.settings.write_bytes(original)
                    self.settings.chmod(0o644)

    def test_backup_settings_must_match_recorded_source_before_any_write(self):
        root = self.home / 'namespace'
        backup = self.transaction(root, policy=True)
        (backup / 'settings.json').write_text('{"name":"different-backup"}', encoding='utf8')
        manifest = json.loads((backup / 'manifest.json').read_text())
        manifest['files']['settings.json'] = sha((backup / 'settings.json').read_bytes())
        save(backup / 'manifest.json', manifest)
        before = self.namespace_snapshot(root)
        settings_before = self.settings_snapshot()
        with self.assertRaisesRegex(ValueError, 'setup_backup_changed|setup_recovery_invalid'):
            self.recover(root)
        self.assertEqual(self.namespace_snapshot(root), before)
        self.assertEqual(self.settings_snapshot(), settings_before)

    def test_recovering_accepts_namespace_file_already_restored_from_backup(self):
        root = self.home / 'namespace'
        backup = self.transaction(root, phase='recovering', policy=True)
        original = (backup / 'namespace/shared-policy.json').read_bytes()
        (root / 'shared-policy.json').write_bytes(original)
        settings_before = self.settings_snapshot()
        self.assertEqual(self.recover(root)['status'], 'recovered')
        self.assertEqual((root / 'shared-policy.json').read_bytes(), original)
        self.assertEqual(self.settings_snapshot(), settings_before)

    def test_installing_does_not_accept_backup_value_as_written_configuration(self):
        root = self.home / 'namespace'
        backup = self.transaction(root, policy=True)
        (root / 'shared-policy.json').write_bytes((backup / 'namespace/shared-policy.json').read_bytes())
        before = self.namespace_snapshot(root)
        with self.assertRaisesRegex(ValueError, 'setup_recovery_configuration_changed'):
            self.recover(root)
        self.assertEqual(self.namespace_snapshot(root), before)

    def test_target_settings_0644_restore_original_bytes_and_keep_public_mode(self):
        root = self.home / 'namespace'
        self.transaction(root)
        original = self.settings.read_bytes()
        target = b'{"name":"activated-target"}\n'
        self.settings.write_bytes(target)
        journal = json.loads((root / setup.NAME).read_text())
        journal['targetSettingsSha256'] = sha(target)
        save(root / setup.NAME, journal)
        self.assertEqual(self.recover(root)['status'], 'recovered')
        self.assertEqual(self.settings.read_bytes(), original)
        self.assertEqual(stat.S_IMODE(self.settings.stat().st_mode), 0o644)

    def test_restore_target_parent_is_checked_before_recovering_phase(self):
        root = self.home / 'namespace'
        backup = self.transaction(root, policy=True)
        # A missing target still needs a safe destination before the phase write.
        relative = 'consents/' + _project_key(self.project) + '.json'
        save(backup / 'namespace' / relative, {'stage': 'before'})
        manifest = json.loads((backup / 'manifest.json').read_text())
        manifest['files']['namespace/' + relative] = sha((backup / 'namespace' / relative).read_bytes())
        save(backup / 'manifest.json', manifest)
        journal = json.loads((root / setup.NAME).read_text())
        journal['written'][relative] = sha(b'installed')
        save(root / setup.NAME, journal)
        (root / 'consents').mkdir(mode=0o755)
        before = self.namespace_snapshot(root)
        with self.assertRaisesRegex(ValueError, 'unsafe'):
            self.recover(root)
        self.assertEqual(self.namespace_snapshot(root), before)

    def test_settings_restore_parent_is_rechecked_before_recovering_phase(self):
        root = self.home / 'namespace'
        backup = self.transaction(root)
        self.settings.write_bytes(b'{"name":"activated-target"}\n')
        journal = json.loads((root / setup.NAME).read_text())
        journal['targetSettingsSha256'] = sha(self.settings.read_bytes())
        save(root / setup.NAME, journal)
        before = self.namespace_snapshot(root)
        settings_before = self.settings_snapshot()
        read = recovery._recovery_read
        def changed_parent(path, **kwargs):
            raw = read(path, **kwargs)
            if Path(path) == backup / 'settings.json':
                # The writer lock is held; a noncooperating owner changes mode.
                self.settings.parent.chmod(0o775)
            return raw
        try:
            with mock.patch.object(recovery, '_recovery_read', side_effect=changed_parent):
                with self.assertRaisesRegex(ValueError, 'unsafe'):
                    self.recover(root)
            self.assertEqual(self.namespace_snapshot(root), before)
            self.assertEqual(self.settings_snapshot(), settings_before)
        finally:
            self.settings.parent.chmod(0o755)

    def test_incomplete_default_setup_never_creates_missing_ownership_binding(self):
        root = default_enrollment.default_namespace(self.project, 'fixture')
        self.transaction(root, phase='recovering')
        state = self.home / 'install'
        state.mkdir(mode=0o700)
        default_enrollment.record(self.project, state, 'fixture')
        before = self.namespace_snapshot(root)
        with mock.patch.object(selector_heal, 'official_runtime', return_value=('/runtime/python', '/model')), \
                mock.patch.object(selector_heal, 'pressure', return_value=None):
            with self.assertRaises(ValueError):
                default_enrollment.prepare(self.project, state, 'fixture', lease_fd=-1)
        self.assertEqual(self.namespace_snapshot(root), before)
        self.assertFalse((root / default_enrollment.BINDING).exists())


if __name__ == '__main__':
    import unittest
    unittest.main()

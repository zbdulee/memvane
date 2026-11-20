"""Real entry canonicalizes an old unaccepted adapter before profile creation."""
import hashlib
import json
from pathlib import Path
import stat
from unittest import mock

from tests.support.case import FixtureCase
from memvane.learning import default_enrollment
from memvane.learning.context_learning import adapter_binding, selector_heal
from memvane.learning.tests.integration.test_settings_profile_113 import SettingsProfile


class AdapterProfile(FixtureCase):
    def setUp(self):
        super().setUp()
        self.project = self.home / 'project'
        folder = self.project / '.memvane'
        folder.mkdir(mode=0o755, parents=True)
        self.settings = folder / 'settings.json'
        self.settings.write_text('{"name":"fixture"}', encoding='utf8')
        self.settings.chmod(0o644)

    def entry(self, *, host='codex', version='1.0.12', present=True, changed=None):
        current = selector_heal.running_adapter()
        original_cache = adapter_binding._official_cache
        before = {}
        def seed(root, runtime, model, source):
            default_enrollment._prepare_namespace(root, runtime, model)
            cache = self.home / ('.'+host) / 'plugins/cache/memvane-marketplace/memvane' / version
            anchor = cache / 'memvane/learning/context_learning/adapter.py'
            anchor.parent.mkdir(mode=0o700, parents=True)
            anchor.write_bytes(current.read_bytes())
            anchor.chmod(0o600)
            manifest = cache / ('.'+host+'-plugin/plugin.json')
            manifest.parent.mkdir(mode=0o700)
            for path in (manifest, cache / 'package.json'):
                path.write_text(json.dumps({'name': 'memvane', 'version': version}), encoding='utf8')
                path.chmod(0o600)
            if not present:
                anchor.unlink()
            path = root / selector_heal.POLICY
            policy = json.loads(path.read_text())
            policy['trainerArgv'][1] = str(anchor)
            if changed is not None:
                policy['trainerArgv'][changed] += '-different'
            path.write_text(json.dumps(policy), encoding='utf8')
            path.chmod(0o600)
            before.update(policy=policy, raw=path.read_bytes())
        def cache(path, home):
            if Path(path) == current:
                # The executing worktree stands in for the selected current cache.
                return ('codex', '1.0.14', current.parents[3], hashlib.sha256(current.read_bytes()).digest())
            return original_cache(path, home)
        real_heal = selector_heal.heal
        with mock.patch.object(adapter_binding, '_official_cache', side_effect=cache), \
                mock.patch.object(adapter_binding, '_installed_current'), \
                mock.patch.object(selector_heal, 'admitted', side_effect=[False, True]), \
                mock.patch.object(selector_heal, 'attest') as attest, \
                mock.patch.object(selector_heal, 'prepare') as prepare:
            # Adapter rebind of a default-local namespace: the teacher-off switch pins the profile.
            root = SettingsProfile.run_profile_entry(self, seed_namespace=seed, heal_impl=real_heal,
                expected_error='default_learning_namespace_changed' if changed is not None else None,
                teacher_off=True)
        path = root / selector_heal.POLICY
        if changed is not None:
            self.assertEqual(path.read_bytes(), before['raw'])
            attest.assert_not_called()
            prepare.assert_not_called()
            return
        expected = {**before['policy'], 'trainerArgv': list(before['policy']['trainerArgv'])}
        expected['trainerArgv'][1] = str(current)
        self.assertEqual(json.loads(path.read_text()), expected)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        attest.assert_called_once()
        prepare.assert_called_once()
        self.assertEqual(prepare.call_args.args[1]['trainerArgv'], expected['trainerArgv'])
        snapshot = {name: (root / name).read_bytes() for name in (
            selector_heal.POLICY, selector_heal.WARM_MARKER, 'learning-profile.json', 'project-learning-authorization.json')}
        self.assertEqual(default_enrollment.prepare(self.project, self.home / 'install', 'fixture', lease_fd=-1),
                         {'status': 'preserved'})
        self.assertEqual(snapshot, {name: (root / name).read_bytes() for name in snapshot})

    def test_present_claude_109_namespace_rebinds_before_warm_profile_and_grant(self):
        self.entry(host='claude', version='1.0.9')

    def test_present_codex_112_namespace_rebinds_before_warm_profile_and_grant(self):
        self.entry()

    def test_removed_old_anchor_rebinds_before_warm_profile_and_grant(self):
        self.entry(present=False)

    def test_different_interpreter_is_refused_without_policy_write(self):
        self.entry(changed=0)

    def test_different_model_is_refused_without_policy_write(self):
        self.entry(changed=2)


if __name__ == '__main__':
    import unittest
    unittest.main()

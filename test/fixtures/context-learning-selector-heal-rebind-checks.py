"""Selector heal keeps another host's identical official adapter anchor.

Claude and Codex install the same bundle under different cache roots. A heal
run by one host must not rewrite a policy whose anchor is the other host's
identical copy: that rewrite invalidates prepared evidence on every run.
"""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'core'))
from context_learning import adapter_binding, selector_heal


class RebindTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='qmd-heal-rebind-')
        root = Path(self.temp.name).resolve(); root.chmod(0o700)
        self.state = root / 'state'; self.state.mkdir(mode=0o700)
        self.claude = root / 'claude/core/context_learning/laya_adapter.py'
        self.codex = root / 'codex/core/context_learning/laya_adapter.py'
        for path in (self.claude, self.codex):
            path.parent.mkdir(parents=True); path.write_text('adapter = 1\n')
        self.policy = {'schema': 'qmd-live-selector-v1', 'timeoutSeconds': 30, 'maxRssMib': 4096,
                       'trainerArgv': [sys.executable, str(self.claude), str(root / 'model')]}
        self.write(self.policy)
        self.scoped = []

    def tearDown(self):
        self.temp.cleanup()

    def write(self, policy):
        path = self.state / selector_heal.POLICY
        path.write_text(json.dumps(policy)); path.chmod(0o600)

    def read(self):
        return json.loads((self.state / selector_heal.POLICY).read_text())

    def rebind(self, binding):
        with patch.object(selector_heal, 'running_adapter', return_value=self.codex), \
                patch.object(adapter_binding, 'inference_argv', side_effect=binding), \
                patch.object(selector_heal, 'check_scope',
                             side_effect=lambda state, policy, home=None: self.scoped.append(policy)), \
                patch.object(selector_heal, '_official', return_value=None):
            return selector_heal.rebind(self.state, home=self.state)

    def test_identical_official_anchor_of_the_other_host_is_kept(self):
        before = (self.state / selector_heal.POLICY).read_bytes()
        policy, rebound = self.rebind(lambda argv, adapter, **kw: [argv[0], str(adapter), argv[2]])
        self.assertEqual((policy, rebound), (self.policy, False))
        self.assertEqual((self.state / selector_heal.POLICY).read_bytes(), before)
        self.assertEqual(self.scoped, [self.policy])

    def test_different_or_missing_anchor_is_still_rebound(self):
        def unsupported(argv, adapter, **kw): raise ValueError('unsupported_adapter')
        policy, rebound = self.rebind(unsupported)
        self.assertTrue(rebound)
        self.assertEqual(self.read()['trainerArgv'][1], str(self.codex))
        self.write(self.policy); self.claude.unlink()
        policy, rebound = self.rebind(lambda *a, **kw: self.fail('missing anchor needs no binding'))
        self.assertTrue(rebound)
        self.assertEqual(self.read()['trainerArgv'][1], str(self.codex))

    def test_binding_without_time_defers_without_rewriting(self):
        before = (self.state / selector_heal.POLICY).read_bytes()
        def late(argv, adapter, **kw): raise adapter_binding.BindingTimeout('trainer_timeout')
        with self.assertRaises(selector_heal.HealDeferred):
            self.rebind(late)
        self.assertEqual((self.state / selector_heal.POLICY).read_bytes(), before)


if __name__ == '__main__':
    unittest.main()

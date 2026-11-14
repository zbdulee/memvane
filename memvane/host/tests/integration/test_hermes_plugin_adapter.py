"""Hermes Agent plugin: manifest, registration and the no-route and sandbox no-ops."""
import json
import os
import unittest
from unittest import mock

from hermes_adapter import core_bridge as bridge
from tests.support.case import FixtureCase
from tests.support.install_builder import ROOT

HOOKS = ('pre_llm_call', 'on_session_start', 'pre_tool_call', 'post_tool_call')
QUIET = {'QMD_SANDBOX': '', 'HERMES_SANDBOX': '', 'HERMES_HEADLESS': '', 'CODEX_SANDBOX': '',
         'CLAUDE_SANDBOX': '', 'GEMINI_SANDBOX': '', 'CLAUDE_HEADLESS': ''}


class PluginFiles(unittest.TestCase):
    def test_manifest_declares_the_four_hooks_beside_the_claude_and_codex_manifests(self):
        manifest = (ROOT / 'plugin.yaml').read_text(encoding='utf-8')
        self.assertRegex(manifest, r'name:\s*memvane')
        for hook in HOOKS:
            self.assertIn(hook, manifest)
        self.assertTrue((ROOT / 'hooks' / 'hooks.json').is_file())
        self.assertTrue((ROOT / 'hooks' / 'hooks-codex.json').is_file())

    def test_root_shim_registers_the_lifecycle_hooks_through_the_adapter(self):
        self.assertIn('hermes_adapter.plugin', (ROOT / '__init__.py').read_text(encoding='utf-8'))
        from hermes_adapter import plugin
        registered = {}

        class Context:
            def register_hook(self, name, callback):
                registered[name] = callback
        plugin.register(Context())
        self.assertEqual(set(registered), set(HOOKS))

    def test_the_bridge_serves_only_the_selected_route_through_the_host_api(self):
        source = (ROOT / 'hermes_adapter' / 'core_bridge.py').read_text(encoding='utf-8')
        self.assertNotIn('/query', source)
        self.assertNotRegex(source, r'urlopen|requests\.')
        for script in ('recall.py', 'update.sh', 'index_enqueue.py', 'preflight_gate.py',
                       'backend_manager', 'setup_guard'):
            self.assertNotIn(script, source)
        self.assertIn('from memvane.host.api import serve_selected', source)


class NoRoute(FixtureCase):
    def test_hooks_do_nothing_for_a_project_without_a_selected_route(self):
        project = self.home / 'project'
        (project / '.memvane').mkdir(parents=True)
        (project / '.memvane' / 'settings.json').write_text(
            json.dumps({'name': 'demo', 'skipPaths': []}), encoding='utf-8')
        cwd = str(project)
        edit = {'tool_name': 'write_file', 'args': {'path': 'docs/a.md', 'content': 'x'}}
        with mock.patch.dict(os.environ, QUIET):
            results = [bridge.recall_context(user_message='프로젝트 설정은 어디에 있어?', cwd=cwd),
                       bridge.session_update(cwd=cwd),
                       bridge.pre_edit_gate(cwd=cwd, **edit),
                       bridge.post_edit_sync(result='{}', status='ok', cwd=cwd, **edit)]
        self.assertEqual(results, [None] * 4)
        self.assertEqual(os.listdir(project / '.memvane'), ['settings.json'])

    def test_sandbox_and_headless_no_op_before_any_route_lookup(self):
        for name in ('QMD_SANDBOX', 'HERMES_HEADLESS'):
            with self.subTest(env=name):
                with mock.patch.dict(os.environ, {**QUIET, name: '1'}), \
                        mock.patch.object(bridge, '_laya_route_state', return_value='legacy') as lookup:
                    bridge.post_edit_sync(tool_name='write_file',
                                          args={'path': 'docs/a.md', 'content': 'x'},
                                          result='{}', status='ok', cwd='/tmp/project')
                    bridge.session_update(cwd='/tmp/project')
                    bridge.recall_context(user_message='q', cwd='/tmp/project')
                    bridge.pre_edit_gate(tool_name='write_file', args={'path': 'a.md'},
                                         cwd='/tmp/project')
                lookup.assert_not_called()


if __name__ == '__main__':
    unittest.main()

"""Review regressions on real host boundaries.

R3: every registered Hermes hook takes the project from either cwd alias,
exactly once. R5: an onboarding-choice ancestor that is a symlink refuses
before any external mkdir, chmod or write. N2: an import fault after route
selection stays inside the registered recall hook as a refusal.
"""
import builtins
import os
import stat
import subprocess
import unittest
from unittest import mock

from hermes_adapter import core_bridge as bridge
from hermes_adapter import plugin
from memvane.host import onboarding_choice as choice
from memvane.host.tests.support import PROMPT, HostInstallCase
from tests.support.case import FixtureCase
from tests.support.install_builder import ROOT

QUIET = {'QMD_SANDBOX': '', 'HERMES_SANDBOX': '', 'HERMES_HEADLESS': '', 'CODEX_SANDBOX': '',
         'CLAUDE_SANDBOX': '', 'GEMINI_SANDBOX': '', 'CLAUDE_HEADLESS': ''}


def registered():
    hooks = {}

    class Context:
        def register_hook(self, name, callback):
            hooks[name] = callback
    plugin.register(Context())
    return hooks


class RegisteredHermesHooks(HostInstallCase):
    def test_r3_registered_hooks_accept_both_cwd_aliases_once(self):
        hooks = registered()
        with mock.patch.dict(os.environ, QUIET):
            for alias in ('working_directory', 'cwd'):
                with self.subTest(hook='pre_llm_call', alias=alias):
                    answer = hooks['pre_llm_call'](user_message=PROMPT, **{alias: str(self.project)})
                    self.assertIn('zqheronx', (answer or {}).get('context', ''))
        for hook, authority, kwargs in (
                ('on_session_start', 'session_update', {}),
                ('pre_tool_call', 'pre_edit_gate', {'tool_name': 'write_file', 'args': {'path': 'x.md'}}),
                ('post_tool_call', 'post_edit_sync', {'tool_name': 'write_file', 'args': {'path': 'x.md'},
                                                      'result': '{}', 'status': 'ok'})):
            for alias in ('working_directory', 'cwd'):
                with self.subTest(hook=hook, alias=alias):
                    captured = []
                    with mock.patch.object(plugin, authority,
                                           side_effect=lambda **kw: captured.append(kw)):
                        hooks[hook](**kwargs, **{alias: str(self.project)})
                    self.assertEqual(len(captured), 1)
                    self.assertEqual(captured[0]['cwd'], str(self.project))

    def test_n2_an_import_fault_after_selection_stays_a_refusal(self):
        real_import, real_run = builtins.__import__, subprocess.run
        selector = str(ROOT / 'entry' / 'run.py')
        classified, faults = [], []

        def observed_run(argv, *args, **kwargs):
            result = real_run(argv, *args, **kwargs)
            if len(argv) > 1 and str(argv[1]) == selector:
                classified.append(result.stdout.strip())
            return result

        def faulty_import(name, globals=None, locals=None, fromlist=(), level=0):
            if name == 'memvane.host.api':
                faults.append(list(classified))
                raise ImportError('synthetic host api import unavailable')
            return real_import(name, globals, locals, fromlist, level)
        with mock.patch.dict(os.environ, QUIET), \
                mock.patch.object(bridge.subprocess, 'run', side_effect=observed_run), \
                mock.patch.object(builtins, '__import__', faulty_import):
            answer = registered()['pre_llm_call'](user_message=PROMPT,
                                                  working_directory=str(self.project))
        self.assertEqual(classified[:1], ['candidate'])
        self.assertTrue(faults and all(seen for seen in faults), 'only post-selection imports fail')
        self.assertEqual(answer, {'context': '[memvane] selected proof or query failed; request refused'})


class OnboardingChoiceAncestors(FixtureCase):
    @staticmethod
    def snapshot(root):
        rows = {}
        for path in (root, *sorted(root.rglob('*'))):
            info = path.lstat()
            rows[str(path.relative_to(root))] = {
                'mode': stat.S_IMODE(info.st_mode),
                'kind': 'directory' if stat.S_ISDIR(info.st_mode) else 'file',
                **({'bytes': path.read_text(encoding='utf-8')} if stat.S_ISREG(info.st_mode) else {})}
        return rows

    def test_r5_a_symlinked_ancestor_refuses_before_any_external_change(self):
        # Every ancestor of the choice directory under HOME (.local … state base).
        depth = len(choice.choice_dir(self.home).parent.relative_to(self.home).parts)
        self.assertGreaterEqual(depth, 3)
        for ancestor in range(depth):
            for existing in (False, True):
                with self.subTest(ancestor=ancestor, existing=existing):
                    base = self.tmp / ('a%d%d' % (ancestor, existing))
                    home, outside = base / 'home', base / 'outside'
                    home.mkdir(parents=True, mode=0o700)
                    outside.mkdir(mode=0o755)
                    outside.chmod(0o755)
                    project = home / 'project'
                    (project / '.memvane').mkdir(parents=True)
                    (project / '.memvane' / 'settings.json').write_text(
                        '{"name":"ancestor","skipPaths":[]}', encoding='utf-8')
                    directory = choice.choice_dir(home)
                    parts = directory.parent.relative_to(home).parts
                    link = home.joinpath(*parts[:ancestor + 1])
                    link.parent.mkdir(parents=True, exist_ok=True)
                    link.symlink_to(outside, target_is_directory=True)
                    if existing:
                        target = outside / directory.relative_to(link)
                        target.mkdir(parents=True, mode=0o755)
                        for node in (outside, *outside.rglob('*')):
                            if node.is_dir():
                                node.chmod(0o755)
                        (target / 'preserved').write_text('external marker', encoding='utf-8')
                    (outside / 'root-marker').write_text('outside original bytes', encoding='utf-8')
                    before = self.snapshot(outside)
                    with mock.patch.dict(os.environ, {'HOME': str(home)}):
                        with self.assertRaises((ValueError, OSError)):
                            choice.resume(str(project), home=home)
                    self.assertEqual(self.snapshot(outside), before)


if __name__ == '__main__':
    unittest.main()

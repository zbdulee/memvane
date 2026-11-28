"""SessionStart opt-in dashboard auto-open (``_maybe_auto_open_ui``): guards and the detached spawn.

``Wiring`` serves ``update-queued`` over a real selected install (main checkout
and a linked worktree) and records whether the hook reached the helper.
``Guards`` drives the helper itself with the environment and the files the UI
preference writer leaves, and records ``subprocess.Popen`` instead of starting
a process. The fixture switch (``QMD_SETUP_GUARD_FIXTURE=1``) is itself a
guard, so only the tests that expect a spawn lift it inside the temporary home.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from unittest import mock

from memvane.host import host_response as response
from memvane.host.tests.support import HostInstallCase, Wakes, context
from memvane.kernel import memvane_paths
from tests.support.case import FixtureCase

GUI_ENV = {'TERM': 'xterm-256color', 'DISPLAY': ':0'}
BLOCKING = ('QMD_SETUP_GUARD_FIXTURE', 'MEMVANE_TEST_RUNTIME_ROOT', 'QMD_SANDBOX', 'CLAUDE_SANDBOX',
            'CODEX_SANDBOX', 'GEMINI_SANDBOX', 'HERMES_SANDBOX', 'CLAUDE_HEADLESS', 'HERMES_HEADLESS', 'CI',
            'SSH_CONNECTION', 'SSH_TTY')


class Wiring(HostInstallCase):
    def setUp(self):
        super().setUp()
        Wakes().install(self)
        self.seen = []
        patcher = mock.patch.object(response, '_maybe_auto_open_ui',
                                    side_effect=lambda decision, host: self.seen.append((decision['state'], host)))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_main_checkout_session_start_reaches_the_helper_once(self):
        answer = self.serve('update-queued', {'hook_event_name': 'SessionStart'})
        self.assertTrue(context(answer))
        self.assertEqual(self.seen, [('candidate', 'claude')])

    def test_plain_update_and_other_actions_never_reach_it(self):
        self.serve('update', {'hook_event_name': 'SessionStart'}, host='codex')
        self.serve('recall', {'prompt': 'what marker does the emerald heron keep?'})
        self.assertEqual(self.seen, [])

    def test_linked_worktree_session_start_reaches_the_helper(self):
        linked = self.fixture.linked_worktree('wt', 'wt-branch')
        answer = self.serve('update-queued', {'hook_event_name': 'SessionStart'}, cwd=linked.root)
        self.assertTrue(context(answer))
        self.assertEqual(self.seen, [('worktree', 'claude')])


class Guards(FixtureCase):
    def setUp(self):
        super().setUp()
        self.ui = memvane_paths.ensure_state_dir('ui', home=self.home, create=True)
        self.decision = {'state': 'candidate', 'projectRoot': str(self.home / 'proj')}
        self.spawned = []
        patcher = mock.patch('subprocess.Popen', side_effect=lambda *a, **k: self.spawned.append((a, k)))
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, name, value, mode=0o600):
        path = self.ui / name
        path.write_text(json.dumps(value), encoding='utf-8')
        os.chmod(path, mode)

    def opt_in(self, **extra):
        self.write('preferences.json', {'schema': 'memvane-ui-preferences-v1', 'autoOpenOnSessionStart': True,
                                        **extra})

    def run_helper(self, env=None, decision=None):
        clean = {key: value for key, value in os.environ.items() if key not in BLOCKING}
        clean.update(GUI_ENV)
        clean.update(env or {})
        with mock.patch.dict(os.environ, clean, clear=True):
            response._maybe_auto_open_ui(decision or self.decision, 'claude')

    def test_opt_in_spawns_entry_ui_open_detached_without_bytecode(self):
        self.opt_in()
        self.run_helper()
        self.assertEqual(len(self.spawned), 1)
        argv, options = self.spawned[0][0][0], self.spawned[0][1]
        self.assertEqual(argv[:2], [sys.executable, '-B'])
        self.assertTrue(argv[2].endswith(os.path.join('entry', 'run.py')))
        self.assertEqual(argv[3:], ['ui', 'open', '--cwd', str(self.home / 'proj'), '--from-session-start',
                                    '--host', 'claude'])
        self.assertEqual({key: options[key] for key in ('stdin', 'stdout', 'stderr')},
                         dict.fromkeys(('stdin', 'stdout', 'stderr'), subprocess.DEVNULL))
        self.assertIs(options['close_fds'], True)
        self.assertIs(options['start_new_session'], True)
        self.assertNotIn('shell', options)

    def test_worktree_decision_opens_the_worktree_project(self):
        self.opt_in()
        decision = {'state': 'worktree', 'worktreeProjectRoot': str(self.home / 'wt'),
                    'mainRoute': {'projectRoot': str(self.home / 'proj')}}
        self.run_helper(decision=decision)
        self.assertEqual(self.spawned[0][0][0][-4:], [str(self.home / 'wt'), '--from-session-start',
                                                      '--host', 'claude'])

    def test_opt_in_off_absent_or_damaged_preferences_do_nothing(self):
        self.run_helper()
        self.write('preferences.json', {'schema': 'memvane-ui-preferences-v1', 'autoOpenOnSessionStart': False})
        self.run_helper()
        self.write('preferences.json', {'autoOpenOnSessionStart': 'true'})
        self.run_helper()
        (self.ui / 'preferences.json').write_text('{not json', encoding='utf-8')
        self.run_helper()
        self.opt_in()
        os.chmod(self.ui / 'preferences.json', 0o644)
        self.run_helper()
        (self.ui / 'preferences.json').unlink()
        os.symlink(self.tmp / 'elsewhere.json', self.ui / 'preferences.json')
        (self.tmp / 'elsewhere.json').write_text(json.dumps({'autoOpenOnSessionStart': True}), encoding='utf-8')
        os.chmod(self.tmp / 'elsewhere.json', 0o600)
        self.run_helper()
        self.assertEqual(self.spawned, [])

    def test_each_environment_guard_blocks_the_spawn(self):
        self.opt_in()
        for name, value in (('QMD_SETUP_GUARD_FIXTURE', '1'), ('MEMVANE_TEST_RUNTIME_ROOT', '/x'),
                            ('QMD_SANDBOX', '1'), ('CLAUDE_SANDBOX', '1'), ('CODEX_SANDBOX', '1'),
                            ('GEMINI_SANDBOX', '1'), ('HERMES_SANDBOX', '1'), ('CLAUDE_HEADLESS', '1'),
                            ('HERMES_HEADLESS', '1'), ('CI', 'true'), ('SSH_CONNECTION', '1 2 3 4'),
                            ('SSH_TTY', '/dev/ttys001'), ('TERM', 'dumb')):
            with self.subTest(env=name):
                self.run_helper({name: value})
        with mock.patch.object(sys, 'platform', 'linux'):
            clean = {key: value for key, value in os.environ.items()
                     if key not in BLOCKING + ('DISPLAY', 'WAYLAND_DISPLAY')}
            clean['TERM'] = 'xterm'
            with mock.patch.dict(os.environ, clean, clear=True):
                response._maybe_auto_open_ui(self.decision, 'claude')
        self.assertEqual(self.spawned, [])

    def test_cooldown_from_the_last_open_or_close_blocks_the_spawn(self):
        self.opt_in(autoOpenCooldownMinutes=240)
        now = time.time()
        self.write('auto-open-state.json', {'schema': 'memvane-ui-auto-open-v1', 'lastAutoOpenAt': now - 60,
                                            'lastUserClosedAt': None})
        self.run_helper()
        self.write('auto-open-state.json', {'schema': 'memvane-ui-auto-open-v1', 'lastAutoOpenAt': None,
                                            'lastUserClosedAt': now - 600})
        self.run_helper()
        self.assertEqual(self.spawned, [])
        self.write('auto-open-state.json', {'schema': 'memvane-ui-auto-open-v1',
                                            'lastAutoOpenAt': now - 241 * 60, 'lastUserClosedAt': None})
        self.run_helper()
        self.assertEqual(len(self.spawned), 1)

    def test_a_spawn_failure_never_reaches_the_hook(self):
        self.opt_in()
        with mock.patch('subprocess.Popen', side_effect=OSError('no exec')):
            self.run_helper()
        self.run_helper(decision={'state': 'candidate'})
        self.assertEqual(self.spawned, [])

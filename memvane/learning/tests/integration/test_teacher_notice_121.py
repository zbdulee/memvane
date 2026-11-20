"""1.0.21 SessionStart teacher lines (plan §13, E-2, E-3, G-11) and X-1 at run time.

``hooks/run-hook update-queued claude|codex`` runs as a child of a selected
fixture install (``tests.support.install_builder``) under this test's
interpreter (a ``python3`` shim on ``PATH``). The teacher state is written
with ``teacher_state`` into the install's default namespace. A
``sitecustomize`` on the child ``PYTHONPATH`` appends each child's loaded
``memvane`` modules at exit, so the hook's real import set is checked.
Assertions are counts, files and lines; times are fixture values in the
state, never elapsed time.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from unittest import mock

from memvane.kernel.memvane_version import plugin_version
from memvane.learning import default_enrollment
from memvane.learning.context_learning import teacher_state
from tests.support.case import FixtureCase
from tests.support.install_builder import build_install
from tests.support.project import make_project

ROOT = Path(__file__).resolve().parents[4]
HOUR_MS = 3600 * 1000
FORBIDDEN = ('memvane.learning.context_learning.teacher_hosts',
             'memvane.learning.context_learning.teacher_isolation',
             'memvane.learning.context_learning.teacher_model_tiers',
             'memvane.learning.default_teacher_enable',
             'memvane.learning.context_learning.general_teacher',
             'memvane.learning.context_learning.default_teacher')
CLI_CLAUDE = ('[memvane] 학습 라벨링에는 Claude Code CLI(claude)가 필요합니다. CLI를 설치하면 '
              '대기 중인 학습 사례를 처리합니다. 검색과 수집은 계속됩니다.')
CLI_CODEX = CLI_CLAUDE.replace('Claude Code CLI(claude)', 'Codex CLI(codex)')
REST_CODEX = ('[memvane] Codex 학습 라벨링이 하루 넘게 멈춰 있습니다(격리 점검 실패). '
              'memvane:logs로 확인하세요. 검색과 수집은 계속됩니다.')
REST_CLAUDE_MODEL = ('[memvane] Claude 학습 라벨링이 하루 넘게 멈춰 있습니다(사용할 모델 없음). '
                     'memvane:logs로 확인하세요. 검색과 수집은 계속됩니다.')
ENABLE = ('[memvane] 학습 설정 전환이 끝나지 않아 학습과 선택 모델이 멈췄습니다. '
          'setup skill로 학습 설정 복구를 요청하세요.')
SITECUSTOMIZE = '''import atexit, json, os, sys
_target = os.environ.get('MV121_MODULES')
if _target:
    def _dump():
        with open(_target, 'a', encoding='utf-8') as out:
            out.write(json.dumps(sorted(m for m in sys.modules if m.startswith('memvane'))) + '\\n')
    atexit.register(_dump)
'''


class TeacherNotice(FixtureCase):
    def setUp(self):
        super().setUp()
        fixture = make_project(self.home)
        fixture.write_note('notes/heron.md', 'The heron nests by the pond.\n')
        fixture.commit('fixture')
        self.project = fixture.root
        self.install = build_install(self, fixture)
        self.install.settle()
        decision = self.install.select()
        self.root = default_enrollment.default_namespace(Path(self.project).resolve(),
                                                         decision['installId'])
        self.root.parent.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        for directory in (self.root.parent, self.root):
            directory.mkdir(mode=0o700, exist_ok=True)
        stub = self.tmp / 'stub'
        stub.mkdir(mode=0o700)
        (stub / 'sitecustomize.py').write_text(SITECUSTOMIZE, encoding='utf-8')
        shim = self.tmp / 'bin'
        shim.mkdir(mode=0o700)
        (shim / 'python3').write_text('#!/bin/sh\nexec %s "$@"\n' % json.dumps(sys.executable),
                                      encoding='utf-8')
        (shim / 'python3').chmod(0o700)
        self.modules = self.tmp / 'modules.jsonl'
        self.env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1',
                    'PATH': '%s:%s' % (shim, os.environ.get('PATH', '')),
                    'PYTHONPATH': '%s:%s' % (stub, ROOT), 'MV121_MODULES': str(self.modules)}
        for key in ('QMD_SANDBOX', 'CLAUDE_SANDBOX', 'CLAUDE_HEADLESS', 'CODEX_SANDBOX',
                    'CLAUDE_PLUGIN_ROOT', 'PLUGIN_ROOT', 'QMD_HOOK_DEADLINE_MONO', 'MEMVANE_TEACHER'):
            self.env.pop(key, None)
        self.sessions = 0

    def hook(self, host):
        """(exit code, SessionStart context lines) of one real ``update-queued`` hook."""
        self.sessions += 1
        payload = {'cwd': str(self.project), 'hook_event_name': 'SessionStart',
                   'session_id': 'mv121-%d' % self.sessions}
        result = subprocess.run(['bash', str(ROOT / 'hooks' / 'run-hook'), 'update-queued', host],
                                input=json.dumps(payload), capture_output=True, text=True,
                                cwd=str(self.project), env=self.env, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        answer = json.loads(result.stdout)
        context = answer['hookSpecificOutput']['additionalContext']
        self.assertTrue(context.startswith('[memvane] selected source update queued'), context)
        return context.splitlines()

    def set_host(self, host, **row):
        state = teacher_state.read_state(self.root)
        state['hosts'][host].update(row)
        self.assertTrue(teacher_state.write_state(self.root, state))

    def teacher_lines(self, lines):
        return [line for line in lines if line in (CLI_CLAUDE, CLI_CODEX, REST_CODEX,
                                                    REST_CLAUDE_MODEL, ENABLE,
                                                    teacher_state.ENABLE_DEFERRED_NOTICE)]

    def test_e2_missing_cli_notice_once_per_host_and_reason_for_seven_days(self):
        for host, line in (('claude', CLI_CLAUDE), ('codex', CLI_CODEX)):
            with self.subTest(host=host):
                self.set_host(host, reason=teacher_state.UNAVAILABLE, queued=2,
                              waitingSinceUnixMs=teacher_state.now_ms())
                self.assertEqual(self.teacher_lines(self.hook(host)), [line])
                self.assertEqual(self.teacher_lines(self.hook(host)), [])
                stamp = teacher_state.read_state(self.root)['hosts'][host]['lastNoticeUnixMs']
                self.assertEqual(set(stamp), {teacher_state.UNAVAILABLE})
                # Eight days after the recorded notice the line is due again, once.
                self.set_host(host, lastNoticeUnixMs={teacher_state.UNAVAILABLE:
                                                      stamp[teacher_state.UNAVAILABLE] - 8 * 24 * HOUR_MS})
                self.assertEqual(self.teacher_lines(self.hook(host)), [line])
                self.assertEqual(self.teacher_lines(self.hook(host)), [])

    def test_e2_no_line_without_queued_cases_or_for_the_other_host(self):
        self.set_host('codex', reason=teacher_state.UNAVAILABLE, queued=3)
        self.set_host('claude', reason=teacher_state.UNAVAILABLE, queued=0)
        self.assertEqual(self.teacher_lines(self.hook('claude')), [])
        self.assertEqual(teacher_state.read_state(self.root)['hosts']['codex']['lastNoticeUnixMs'], {})

    def test_e2_unwritable_state_gives_no_line_and_exit_zero(self):
        self.set_host('claude', reason=teacher_state.UNAVAILABLE, queued=1)
        lock = self.root / teacher_state.STATE_LOCK
        lock.touch(mode=0o600)
        before = (self.root / teacher_state.STATE_NAME).read_bytes()
        self.root.chmod(0o500)
        self.addCleanup(self.root.chmod, 0o700)
        for _ in range(2):
            self.assertEqual(self.teacher_lines(self.hook('claude')), [])
        self.root.chmod(0o700)
        self.assertEqual((self.root / teacher_state.STATE_NAME).read_bytes(), before)
        self.assertEqual(self.teacher_lines(self.hook('claude')), [CLI_CLAUDE])

    def test_e3_long_rest_notice_after_a_day_and_not_again_within_seven_days(self):
        now = teacher_state.now_ms()
        self.set_host('codex', reason='teacher_isolation_incomplete', queued=1,
                      waitingSinceUnixMs=now - 23 * HOUR_MS)
        self.assertEqual(self.teacher_lines(self.hook('codex')), [])
        self.set_host('codex', waitingSinceUnixMs=now - 25 * HOUR_MS)
        self.assertEqual(self.teacher_lines(self.hook('codex')), [REST_CODEX])
        self.assertEqual(self.teacher_lines(self.hook('codex')), [])
        self.set_host('claude', reason='teacher_model_unresolved', queued=4,
                      waitingSinceUnixMs=now - 30 * HOUR_MS)
        self.assertEqual(self.teacher_lines(self.hook('claude')), [REST_CLAUDE_MODEL])
        self.assertEqual(self.teacher_lines(self.hook('claude')), [])

    def test_quota_provider_and_breaker_waits_are_never_announced(self):
        now = teacher_state.now_ms()
        for reason in ('deferred_quota', 'teacher_provider_waiting', 'teacher_host_failing'):
            with self.subTest(reason=reason):
                self.set_host('codex', reason=reason, queued=5, waitingSinceUnixMs=now - 72 * HOUR_MS,
                              waitUntilUnixMs=now + HOUR_MS)
                self.assertEqual(self.teacher_lines(self.hook('codex')), [])
        self.assertEqual(teacher_state.read_state(self.root)['hosts']['codex']['lastNoticeUnixMs'], {})

    def test_g11_unfinished_enable_notice_every_session_after_ten_minutes(self):
        intent = self.root / teacher_state.ENABLE_INTENT
        intent.write_text('{}', encoding='utf-8')
        intent.chmod(0o600)
        self.assertEqual(self.teacher_lines(self.hook('claude')), [])
        old = intent.stat().st_mtime - 11 * 60
        os.utime(intent, (old, old))
        for host in ('claude', 'codex', 'claude'):
            self.assertEqual(self.teacher_lines(self.hook(host)), [ENABLE])
        intent.unlink()
        self.assertEqual(self.teacher_lines(self.hook('claude')), [])

    def test_g11_stopped_enable_requests_get_the_same_notice_until_the_teacher_is_off(self):
        state = teacher_state.read_state(self.root)
        state['enable'].update(failures=teacher_state.ENABLE_FAILURE_LIMIT,
                               reason='teacher_enable_mixed_state', pluginVersion=plugin_version())
        self.assertTrue(teacher_state.write_state(self.root, state))
        # 1.0.22 A-9 a: with no intent left nothing is fenced; the line says the enable is deferred.
        deferred = teacher_state.ENABLE_DEFERRED_NOTICE
        self.assertEqual(self.teacher_lines(self.hook('codex')), [deferred])
        self.assertEqual(self.teacher_lines(self.hook('codex')), [deferred])
        self.assertTrue(teacher_state.write_switch(self.root, False))
        self.assertEqual(self.teacher_lines(self.hook('codex')), [])
        state['enable']['pluginVersion'] = '0.0.1'
        self.assertTrue(teacher_state.write_state(self.root, state))
        self.assertTrue(teacher_state.write_switch(self.root, True))
        self.assertEqual(self.teacher_lines(self.hook('codex')), [])

    def test_hermes_session_gets_no_teacher_line(self):
        from memvane.host.api import serve_selected
        from memvane.journal.api import select_route
        from memvane.kernel import hook_budget
        intent = self.root / teacher_state.ENABLE_INTENT
        intent.write_text('{}', encoding='utf-8')
        old = intent.stat().st_mtime - 3600
        os.utime(intent, (old, old))
        self.set_host('claude', reason=teacher_state.UNAVAILABLE, queued=1)
        with mock.patch.object(hook_budget, '_deadline', None):
            answer = serve_selected('update-queued', 'hermes',
                                    select_route(str(self.project), action='update-queued'),
                                    {'cwd': str(self.project), 'hook_event_name': 'SessionStart'})
        self.assertEqual(answer['status'], 'queued')
        self.assertNotIn('학습 설정 전환', answer.get('notice') or '')
        self.assertNotIn('학습 라벨링', answer.get('notice') or '')
        self.assertEqual(teacher_state.read_state(self.root)['hosts']['claude']['lastNoticeUnixMs'], {})

    def test_runtime_message_without_arguments_is_unchanged(self):
        from memvane.journal.api import select_route
        from memvane.learning import api
        decision = select_route(str(self.project), action='update-queued')
        runtime = '[memvane] 학습 런타임이 활성화되지 않았습니다. setup skill로 준비하고 활성화하세요.'
        intent = self.root / teacher_state.ENABLE_INTENT
        cases = ((False, None), (True, runtime))
        for stale in (False, True):
            if stale:
                intent.write_text('{}', encoding='utf-8')
                old = intent.stat().st_mtime - 3600
                os.utime(intent, (old, old))
            for inactive, expected in cases:
                with self.subTest(stale=stale, inactive=inactive), \
                        mock.patch.object(api, 'learning_runtime_proof_failed', return_value=False), \
                        mock.patch.object(api, 'learning_runtime_needs_activation', return_value=inactive):
                    # The existing caller's value never depends on teacher state.
                    self.assertEqual(api.learning_runtime_message(), expected)
                    teacher = [ENABLE] if stale else []
                    lines = ([expected] if expected else []) + teacher
                    for host in ('claude', 'codex'):
                        self.assertEqual(api.learning_runtime_message(decision, host),
                                         '\n'.join(lines) or None)
                    self.assertEqual(api.learning_runtime_message(decision, 'hermes'), expected)
        with mock.patch.object(api, 'learning_runtime_proof_failed', return_value=False), \
                mock.patch.object(api, 'learning_runtime_needs_activation', return_value=True), \
                mock.patch.object(teacher_state, 'session_notices', side_effect=OSError('broken')):
            self.assertEqual(api.learning_runtime_message(decision, 'claude'), runtime)

    def test_x1_hook_processes_never_load_teacher_transport_modules(self):
        intent = self.root / teacher_state.ENABLE_INTENT
        intent.write_text('{}', encoding='utf-8')
        old = intent.stat().st_mtime - 3600
        os.utime(intent, (old, old))
        self.set_host('claude', reason=teacher_state.UNAVAILABLE, queued=1)
        self.set_host('codex', reason='teacher_model_unresolved', queued=1,
                      waitingSinceUnixMs=teacher_state.now_ms() - 48 * HOUR_MS)
        self.assertEqual(self.teacher_lines(self.hook('claude')), [ENABLE, CLI_CLAUDE])
        self.assertIn(ENABLE, self.teacher_lines(self.hook('codex')))
        loaded = [json.loads(line) for line in self.modules.read_text(encoding='utf-8').splitlines()]
        self.assertGreaterEqual(len(loaded), 4)  # route classification and serving, per hook
        union = set().union(*loaded)
        self.assertIn('memvane.journal.api', union)  # the serving process ran
        self.assertIn('memvane.learning.context_learning.teacher_state', union)
        self.assertEqual(sorted(union & set(FORBIDDEN)), [])

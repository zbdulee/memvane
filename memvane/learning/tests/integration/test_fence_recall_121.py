"""1.0.21 G-3 (enable intent fence) and the G-14 worker part (teacher-off/on).

Both run on ``old_bundle_121.EnabledInstall``: a real selected install whose
default namespace went through enrollment and the enable transaction. Hooks,
including the ``posttool`` edit captures and their detached capture children,
run as ``hooks/run-hook`` children of this bundle. Teacher runs go through
``run-project`` (``shared.main``) with fake ``claude``/``codex`` on the
teacher's ``PATH``.
"""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import tempfile
import time
import unittest
from unittest import mock

from memvane.learning.context_learning import (general_teacher, learning_profile, project_scope,
                                               shared)
from memvane.learning.tests.old_bundle_121 import PROMPT, EnabledInstall, edit_payload
from tests.support.install_builder import ROOT


class EnableIntentFence(EnabledInstall):
    """The enable stopped after its grant write: the intent stays (G-3)."""
    enable_fault = project_scope.NAME

    def last_shared_status(self):
        from memvane.learning.context_learning.contracts import digest
        path = self.root.parent / digest(str(self.project)) / 'shared-last-status.json'
        return {k: v for k, v in json.loads(path.read_text(encoding='utf8')).items() if k != 'request_id_hash'}

    def test_g3_intent_fences_learning_and_recall_answers_by_hybrid_rank(self):
        policy = self.policy()
        for consumer in (lambda: project_scope.require(self.root, policy, 'local_train'),
                         lambda: learning_profile.require(self.root, policy, 'local_train'),
                         lambda: learning_profile.selection_enabled(self.root, self.project)):
            with self.assertRaisesRegex(ValueError, 'teacher_enable_pending'):
                consumer()
        before = len(self.events())
        code, out, err = self.hook(ROOT, 'recall', {'session_id': 'fence-recall',
                                                    'hook_event_name': 'UserPromptSubmit',
                                                    'prompt': PROMPT})
        self.assertEqual(code, 0, err)
        context = json.loads(out)['hookSpecificOutput']['additionalContext']
        self.assertIn('zqheronx', context)
        self.assertIn('hybrid rank score', context)
        [row] = [row for row in self.events()[before:] if row.get('event') == 'recall']
        self.assertEqual((row['outcome'], row['selection'], row['count']), ('served', 'ranked', 2))
        self.assertTrue(row['selection_fallback'].startswith('laya_selector_'), row)
        # In this fixture the selector also lacks a prepared candidate
        # (``laya_selector_candidate_unavailable``); the fence itself is the
        # three refusals above.

    def test_g3_capture_during_intent_is_queued_for_the_teacher(self):
        # Plan §5-6/G-3: new-bundle captures keep queueing while the intent stays.
        answer, statuses = self.capture_edit('fence-edit-queued')
        self.assertIn('zqheronx', answer['hookSpecificOutput']['additionalContext'])
        self.assertEqual(statuses, ['stored'])
        [case] = self.rows()['cases']
        # Queued, and the worker launch waits for the transaction (no failure stage).
        self.assertEqual(self.last_shared_status(), {'status': 'queued'})
        # The same through this bundle's posttool hook and its detached capture child.
        before = len(self.events())
        code, out, err = self.hook(ROOT, 'posttool', edit_payload('fence-edit-child'))
        self.assertEqual(code, 0, err)
        self.assertEqual([row['outcome'] for row in self.wait_capture(before)], ['stored'])
        self.assertEqual(len(self.rows()['cases']), 2)

    def test_g3_only_capture_passes_the_fence(self):
        policy = shared._policy(self.root, capture=True)
        self.assertNotIn('_runtime_authorization_v2', policy)
        with self.assertRaisesRegex(ValueError, 'project_scope_teacher_enable_pending'):
            shared._policy(self.root)
        # Training retry (the worker's cycle and the owned training child), the
        # trainer dispatch and the selector all stay refused.
        with self.assertRaisesRegex(ValueError, 'project_scope_teacher_enable_pending'):
            shared.run_training_pending(self.root, self.project)
        self.assertEqual(shared.train_due(self.root, dict(policy, _runtime_authorization_v2=True)),
                         {'status': 'runtime_approval_required'})
        with self.assertRaisesRegex(ValueError, 'teacher_enable_pending'):
            learning_profile.selection_enabled(self.root, self.project)
        self.assertEqual(self.rows()['cases'], None)


class TeacherSwitchWorker(EnabledInstall):
    """G-14 worker part on the finished enable."""

    def setUp(self):
        super().setUp()
        self.user = self.home / 'user'
        self.user.mkdir(mode=0o700)
        # The teacher's empty working folder must not sit under a folder with
        # host markers; this HOME holds the synthetic ``.claude``/``.codex`` bundles.
        outside = Path(tempfile.mkdtemp(prefix='t', dir=str(self.home.parent))).resolve()
        self.addCleanup(shutil.rmtree, outside, True)
        self.patches.enter_context(mock.patch.object(tempfile, 'tempdir', str(outside)))
        self.environ = {'HOME': str(self.user), 'PATH': '%s:/usr/bin:/bin' % self.bin,
                        'TMPDIR': str(outside), 'LANG': 'C', 'USER': 'fixture', 'LOGNAME': 'fixture'}
        real = general_teacher.process_pending_default
        self.teacher = mock.Mock(side_effect=lambda root, project, policy, **k: real(
            root, project, policy, environ=self.environ, **k))
        self.cycle = mock.Mock(return_value={'status': 'no_synthetic_cases'})
        ready = {'ready': True, 'available_mib': 10000, 'load_per_cpu': 0.1, 'total_mib': 16384,
                 'query_status': {'memory_pressure': 'ok'},
                 'memory_pressure': {'level': 'normal', 'system_free_percent': 70}}
        for target, name, value in (
                (general_teacher, 'process_pending_default', self.teacher),
                (shared, 'run_training_pending', self.cycle),
                (shared, 'resource_report', mock.Mock(side_effect=lambda *a, **k:
                                                      dict(ready, observed_at_mono=time.monotonic()))),
                # The teacher reads its own binding of resource_report: without this the
                # pre-send check samples the machine the test runs on.
                (general_teacher, 'resource_report', mock.Mock(side_effect=lambda *a, **k:
                    dict(ready, observed_at_mono=time.monotonic())))):
            self.patches.enter_context(mock.patch.object(target, name, value))

    def run_project(self):
        import io
        from contextlib import redirect_stdout
        out = io.StringIO()
        with redirect_stdout(out):
            code = shared.main(['run-project', str(self.project)])
        return code, json.loads(out.getvalue())

    def test_g14_teacher_off_runs_no_cli_and_keeps_the_cycle_then_on_runs_it(self):
        _answer, statuses = self.capture_edit('switch-edit')
        self.assertEqual(statuses, ['stored'])
        [case] = self.rows()['cases']
        self.assertEqual(case[11], 'queued')
        code, result = self.learning_main('teacher-off', '--project', str(self.project))
        self.assertEqual((code, result['status'], result['teacherOff']), (0, 'teacher_off', True))
        for _ in range(2):
            code, result = self.run_project()
            self.assertEqual(code, 0, result)
            self.assertEqual(result['teacher']['status'], 'teacher_off')
            self.assertEqual(result['cycle'], {'status': 'no_synthetic_cases'})
        self.assertEqual(self.launches(), [])
        self.assertEqual(self.cycle.call_count, 2)
        self.assertEqual(self.rows()['cases'][0][11], 'queued')
        code, result = self.learning_main('teacher-on', '--project', str(self.project))
        self.assertEqual((code, result['status'], result['teacherOff']), (0, 'teacher_on', False))
        code, result = self.run_project()
        self.assertEqual(code, 0, result)
        self.assertEqual((result['teacher']['status'], result['teacher']['calls'],
                          result['teacher']['outcomes']), ('processed', 1, ['gold']))
        self.assertEqual([(row['host'], row['stage']) for row in self.launches()],
                         [('claude', 'launch'), ('claude', 'body')])
        self.assertEqual(self.cycle.call_count, 3)
        self.assertEqual(self.rows()['cases'][0][11], 'gold')

if __name__ == '__main__':
    unittest.main()

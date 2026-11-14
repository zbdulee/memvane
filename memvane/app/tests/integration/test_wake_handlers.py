"""``app.wiring``: the fixed per-kind wake handlers the runtime worker executes.

A Laya wake whose project directory is gone is void inside the platform temp
area and backs off anywhere else (a TMPDIR outside it never widens it). A
published sync wakes learning without changing its own ACK. The learning
handler defers while the install is in transition and runs one worker step
inside the learning worker lease.
"""
from contextlib import contextmanager
import os
import tempfile
import unittest
from unittest import mock

from memvane.app import runtime_dispatch, wiring
from memvane.runtime.types import Dropped, TargetMissing
from tests.support.case import FixtureCase


class LayaHandlers(FixtureCase):
    def setUp(self):
        super().setUp()
        self.project = self.home / 'project'
        self.project.mkdir()

    def test_a_missing_project_is_void_only_inside_the_platform_temp_area(self):
        with mock.patch.object(runtime_dispatch, 'run', side_effect=AssertionError('dispatched')):
            for handler in (wiring.laya_sync, wiring.laya_compile):
                with self.subTest(handler=handler.__name__):
                    dropped = handler({'kind': 'laya-sync', 'target': str(self.home / 'gone')})
                    self.assertIsInstance(dropped, Dropped)
                    self.assertEqual(dropped.reason, 'target_missing')
            # A TMPDIR pointing at a real directory is not temp: a project missing there backs off.
            with mock.patch.object(tempfile, 'tempdir', '/usr'), mock.patch.dict(os.environ, {'TMPDIR': '/usr'}):
                with self.assertRaises(TargetMissing):
                    wiring.laya_sync({'kind': 'laya-sync', 'target': '/usr/share/memvane-gone-project'})

    def test_a_published_sync_wakes_learning_without_changing_its_ack(self):
        row = {'kind': 'laya-sync', 'target': str(self.project), 'leaseFd': None}
        woken = []
        for state, acked, wakes in (('indexed', True, 1), ('unchanged', True, 0), ('pending', False, 0)):
            with self.subTest(state=state), \
                    mock.patch.object(runtime_dispatch, 'run', return_value={'state': state}) as run, \
                    mock.patch('memvane.learning.api.wake_after_index', side_effect=woken.append):
                before = len(woken)
                self.assertIs(wiring.laya_sync(row), acked)
                self.assertEqual(len(woken) - before, wakes)
                run.assert_called_once_with('laya-sync', str(self.project), owner_fd=None)
        self.assertEqual(woken, [self.project])
        with mock.patch.object(runtime_dispatch, 'run', return_value={'state': 'indexed'}), \
                mock.patch('memvane.learning.api.wake_after_index', side_effect=OSError('busy')):
            self.assertTrue(wiring.laya_sync(row))

    def test_compile_acks_only_a_drained_run(self):
        row = {'kind': 'laya-compile', 'target': str(self.project), 'leaseFd': 5}
        for state, acked in (('drained', True), ('pending', False), ('indexed', False)):
            with self.subTest(state=state), \
                    mock.patch.object(runtime_dispatch, 'run', return_value={'state': state}) as run:
                self.assertIs(wiring.laya_compile(row), acked)
                run.assert_called_once_with('laya-compile', str(self.project), owner_fd=5)


class LearningHandler(FixtureCase):
    def setUp(self):
        super().setUp()
        self.project = self.home / 'project'
        self.project.mkdir()
        self.row = {'kind': 'learning', 'target': str(self.project), 'revision': 1}
        self.inside = []

    @contextmanager
    def lease(self):
        self.inside.append(True)
        try:
            yield
        finally:
            self.inside.pop()

    def handler(self, *, ready=(True, True), root='SAME', step=None):
        """Run the learning handler with ``ready`` answers in turn; returns ``(ack, step calls)``."""
        answers = list(ready)
        calls = []

        def worker_step(root_, project, revision):
            self.assertEqual(self.inside, [True], 'the worker step runs inside the lease')
            calls.append((root_, project, revision))
            return step or {'status': 'settled'}
        with mock.patch('memvane.settings.api.find_project_config', return_value={'config': {}}), \
                mock.patch('memvane.learning.api.safe_root',
                           return_value=self.project if root == 'SAME' else root), \
                mock.patch('memvane.learning.api.ready', side_effect=lambda _project: answers.pop(0)), \
                mock.patch('memvane.learning.api.worker_lease', self.lease), \
                mock.patch('memvane.learning.api.learning_worker_step', side_effect=worker_step):
            return wiring.learning(self.row), calls

    def test_learning_defers_while_the_install_is_in_transition(self):
        self.assertEqual(self.handler(ready=(False,)), (False, []))
        # Readiness is checked again inside the lease.
        self.assertEqual(self.handler(ready=(True, False)), (False, []))

    def test_learning_runs_one_worker_step_inside_the_worker_lease(self):
        acked, calls = self.handler()
        self.assertEqual((acked, calls), (True, [(self.project, self.project, 1)]))
        for status in ('running', 'started'):
            with self.subTest(status=status):
                self.assertEqual(self.handler(step={'status': status})[0], False)

    def test_a_gone_project_or_no_learning_root_acks_without_a_step(self):
        self.assertEqual(self.handler(root=None), (True, []))
        self.row = {**self.row, 'target': str(self.home / 'gone')}
        self.assertEqual(self.handler(ready=()), (True, []))


if __name__ == '__main__':
    unittest.main()

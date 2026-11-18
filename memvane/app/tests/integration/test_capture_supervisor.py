"""Capture supervisor reports an owned worker's abnormal termination."""
import io
import os
import json
import sys
import subprocess
import tempfile
from pathlib import Path
from tests.support.case import wait_until
from memvane.kernel.resource_deferred import ResourceDeferred
import unittest
from unittest import mock

from memvane.app import capture_entry


class CaptureSupervisor(unittest.TestCase):
    def test_budget_refusal_is_deferred_without_a_worker(self):
        with mock.patch.object(sys, 'stdin', self.stream()), \
                mock.patch('memvane.runtime.api.lease', side_effect=ResourceDeferred('cpu_budget_busy')), \
                mock.patch('subprocess.Popen') as spawn, \
                mock.patch('memvane.diagnostics.api.emit') as emit:
            self.assertEqual(capture_entry.learning_capture(), 0)
        spawn.assert_not_called()
        self.assertEqual(emit.call_args.kwargs['outcome'], 'deferred')

    def test_actual_worker_ignoring_term_is_killed_and_reaped(self):
        original = subprocess.Popen
        children = []
        with tempfile.TemporaryDirectory() as directory:
            ready = Path(directory) / 'ready'
            def spawn(*_args, **kwargs):
                child = original([sys.executable, '-B', '-c',
                    'import signal,time,pathlib,sys; signal.signal(signal.SIGTERM,signal.SIG_IGN); '
                    'pathlib.Path(sys.argv[1]).write_text("ready"); time.sleep(60)', str(ready)],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    close_fds=True, pass_fds=kwargs.get('pass_fds', ()))
                children.append(child)
                wait_until(ready.exists, timeout=5)
                return child
            try:
                with mock.patch.object(sys, 'stdin', self.stream()), \
                        mock.patch.object(capture_entry, 'CAPTURE_SECONDS', .05), \
                        mock.patch.object(capture_entry, 'STOP_SECONDS', 1), \
                        mock.patch('subprocess.Popen', side_effect=spawn), \
                        mock.patch('memvane.diagnostics.api.emit'):
                    self.assertEqual(capture_entry.learning_capture(), 2)
                self.assertEqual(children[0].returncode, -9)
            finally:
                for child in children:
                    if child.poll() is None:
                        child.kill()
                    child.wait(timeout=5)

    def stream(self):
        payload = {'capture': ['codex', {'installId': 'fixture'}, {}, {}, [], []], 'started': 1.0}
        return io.TextIOWrapper(io.BytesIO(json.dumps(payload).encode()))

    def test_timeout_kills_and_reaps_worker_ignoring_terminate(self):
        child = mock.Mock()
        child.wait.side_effect = [subprocess.TimeoutExpired('capture', 45),
                                  subprocess.TimeoutExpired('capture', 5), -9]
        with mock.patch.object(sys, 'stdin', self.stream()), \
                mock.patch('subprocess.Popen', return_value=child), \
                mock.patch('memvane.diagnostics.api.emit') as emit:
            self.assertEqual(capture_entry.learning_capture(), 2)
        child.terminate.assert_called_once()
        child.kill.assert_called_once()
        self.assertEqual(child.wait.call_count, 3)
        self.assertEqual(emit.call_args.kwargs['reason'], 'worker_timeout')

    def test_handled_capture_status_is_not_reported_as_lost(self):
        for status in ('capture_unavailable', 'capture_deadline', 'disabled', 'stored'):
            with self.subTest(status=status), mock.patch.object(sys, 'stdin', self.stream()), \
                    mock.patch('memvane.learning.api.capture_recall', return_value=status):
                self.assertEqual(capture_entry.learning_capture_worker(), 0)

    def test_worker_passes_no_hook_start_time(self):
        # The hook's start is another process's monotonic clock; the detached
        # worker is bounded by the supervisor's time limit only.
        with mock.patch.object(sys, 'stdin', self.stream()), \
                mock.patch('memvane.learning.api.capture_recall', return_value='stored') as capture:
            self.assertEqual(capture_entry.learning_capture_worker(), 0)
        self.assertNotIn('started', capture.call_args.kwargs)

    def test_supervisor_forwards_inherited_budget_to_worker(self):
        with tempfile.NamedTemporaryFile() as budget:
            os.fchmod(budget.fileno(), 0o600)
            child = mock.Mock()
            child.wait.return_value = 0
            with mock.patch.object(sys, 'stdin', self.stream()), \
                    mock.patch.dict(os.environ, {'MEMVANE_CAPTURE_FDS': str(budget.fileno())}), \
                    mock.patch('subprocess.Popen', return_value=child) as spawn:
                self.assertEqual(capture_entry.learning_capture(), 0)
            self.assertEqual(spawn.call_args.kwargs['pass_fds'], (budget.fileno(),))

    def test_worker_killed_is_logged_as_lost(self):
        decision = {'journalPath': '/synthetic/journal', 'installId': 'fixture'}
        payload = {'capture': ['codex', decision, {}, {}, [], []], 'started': 1.0}
        stream = io.TextIOWrapper(io.BytesIO(json.dumps(payload).encode('utf8')))
        child = mock.Mock()
        child.wait.return_value = -9
        with mock.patch.object(sys, 'stdin', stream), \
             mock.patch('subprocess.Popen', return_value=child), \
             mock.patch('memvane.diagnostics.api.emit') as emit:
            self.assertEqual(capture_entry.learning_capture(), 2)
        self.assertEqual((emit.call_args.kwargs['outcome'], emit.call_args.kwargs['reason']),
                         ('lost', 'worker_failed'))
        self.assertEqual(emit.call_args.kwargs['decision'], decision)

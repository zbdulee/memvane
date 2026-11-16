"""Capture process trees retain an inherited, globally bounded CPU share."""
from contextlib import nullcontext
import os
from types import SimpleNamespace
from unittest import mock

from tests.support.case import FixtureCase
from memvane.host import host_response
from memvane.runtime import api as runtime
from memvane.kernel.resource_deferred import ResourceDeferred


class CaptureBudget(FixtureCase):
    def slot(self):
        return [('codex', {'installId': 'fixture'}, {}, {}, [], [])]

    def test_budget_refusal_does_not_spawn_a_supervisor(self):
        with mock.patch.object(runtime, 'lease', side_effect=ResourceDeferred('cpu_budget_busy')), \
                mock.patch('subprocess.Popen') as spawn, \
                mock.patch('memvane.diagnostics.api.emit') as emit:
            host_response._dispatch_learning_capture(self.slot(), 1.)
        spawn.assert_not_called()
        self.assertEqual(emit.call_args.kwargs['outcome'], 'deferred')
        self.assertEqual(emit.call_args.kwargs['reason'], 'cpu_budget_busy')

    def test_inherited_share_bounds_parallel_capture_trees(self):
        original = runtime.lease
        held = []
        def spawned(*_args, **kwargs):
            held.append(os.dup(kwargs['pass_fds'][0]))
            self.assertEqual(kwargs['env']['MEMVANE_CAPTURE_FDS'], str(kwargs['pass_fds'][0]))
            return SimpleNamespace(pid=1)
        self.addCleanup(lambda: [os.close(fd) for fd in held])
        with mock.patch.dict(os.environ, {'QMD_SETUP_GUARD_FIXTURE': '0'}), \
                mock.patch('os.cpu_count', return_value=4), \
                mock.patch.object(runtime, 'lease', side_effect=lambda: original(self.runtime)), \
                mock.patch('memvane.kernel.bundle_paths.entry_argv', return_value=['python', 'capture']), \
                mock.patch('subprocess.Popen', side_effect=spawned) as launch, \
                mock.patch('memvane.diagnostics.api.emit'):
            host_response._dispatch_learning_capture(self.slot(), 1.)
            host_response._dispatch_learning_capture(self.slot(), 1.)
            self.assertEqual(launch.call_count, 1)
            os.close(held.pop())
            host_response._dispatch_learning_capture(self.slot(), 1.)
            self.assertEqual(launch.call_count, 2)

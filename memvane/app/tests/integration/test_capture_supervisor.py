"""Capture supervisor reports an owned worker's abnormal termination."""
import io
import json
import sys
import unittest
from unittest import mock

from memvane.app import capture_entry


class CaptureSupervisor(unittest.TestCase):
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

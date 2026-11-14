"""SessionStart records a durable sync wake and answers without waiting for its worker.

The fixture switch turns ``runtime.api.request`` into a no-op, so the stand-in
offers into the real wake queue (``runtime.api.wake_queue``) of the test HOME
and starts nothing. The hook's answer is complete while the wake is still
pending and the published view is unchanged: no sync pass ran in the hook.
"""
import json
import unittest
from unittest import mock

from memvane.host.tests.support import HostInstallCase
from memvane.runtime import api as runtime

STATUS = {'queued': 'queued', 'retained': 'deferred_queue_capacity_retained',
          'dropped': 'deferred_queue_capacity'}


class SessionStartDoesNotAwait(HostInstallCase):
    def setUp(self):
        super().setUp()
        self.requests = []

        def offer_only(kind, target='', *, start=True, pull_due=False, delay=0):
            self.requests.append((kind, target, start))
            return {'status': STATUS[runtime.wake_queue().offer(kind, target, pull_due=pull_due)]}
        patcher = mock.patch('memvane.runtime.api.request', side_effect=offer_only)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_session_start_answers_while_its_sync_wake_is_pending(self):
        epoch = self.install.epoch()
        for host in ('codex', 'claude'):
            with self.subTest(host=host):
                code, out, err = self.main('update-queued', {'hook_event_name': 'SessionStart'}, host)
                self.assertEqual((code, err), (0, ''))
                self.assertIn('update queued',
                              json.loads(out)['hookSpecificOutput']['additionalContext'])
        pending = [(row['kind'], row['target'], row['status']) for row in runtime.wake_queue().pending()]
        self.assertIn(('laya-sync', str(self.project), 'pending'), pending)
        self.assertEqual(len([row for row in pending if row[0] == 'laya-sync']), 1, pending)
        self.assertEqual(self.install.epoch(), epoch)
        self.assertTrue(all(target == str(self.project) for _kind, target, _start in self.requests))


if __name__ == '__main__':
    unittest.main()

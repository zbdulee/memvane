"""Selector retry isolation through real request, claim and revision ACK."""
import json
import os
from contextlib import ExitStack
from unittest import mock

from tests.support.case import FixtureCase
from memvane.runtime import api, runtime_work
from memvane.learning import api as learning
from memvane.journal import api as journal


class SelectorRetryQueue(FixtureCase):
    def setUp(self):
        super().setUp()
        self.project = self.home / 'project'
        folder = self.project / '.memvane'
        folder.mkdir(parents=True, mode=0o700)
        (folder / 'settings.json').write_text(json.dumps({
            'name': 'retry', 'sharedLearning': {'autoApply': False}}), encoding='utf8')
        self.state = self.home / 'install'
        self.state.mkdir(mode=0o700)
        self.store = self.state / 'store.sqlite'
        self.store.touch(mode=0o600)
        self.decision = {'projectRoot': str(self.project), 'journalPath': str(self.state / 'journal'),
                         'storePath': str(self.store), 'installId': 'fixture', 'state': 'candidate'}
        self.path = self.state / 'selector-heal-request.json'
        self.row = {'requestedNs': 1000 * 10**9, 'failures': 1, 'retryAfterNs': 1600 * 10**9}
        journal.ledger_write(self.path, self.row, replace=False)
        self.now = 1100
        self.queue = api.wake_queue()
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(mock.patch('time.time', side_effect=lambda: self.now))
        stack.enter_context(mock.patch('time.time_ns', side_effect=lambda: self.now * 10**9))
        stack.enter_context(mock.patch.dict(os.environ, {'QMD_SETUP_GUARD_FIXTURE': '0',
                                                       'QMD_RUNTIME_DISPATCH': '1'}))
        # Enqueue is real; QMD_RUNTIME_DISPATCH prevents another dispatcher.
        stack.enter_context(mock.patch.object(journal, 'inspect_journal', return_value={
            'state': 'selected', 'installId': 'fixture', 'maxPluginVersion': '1.0.11'}))
        stack.enter_context(mock.patch.object(journal, 'select_route', return_value=self.decision))
        stack.enter_context(mock.patch.object(journal, 'verify_selected_route_for_write',
                                            return_value={'installId': 'fixture'}))

    def drain(self, execute):
        return runtime_work.drain(self.queue, execute=execute, ready=lambda: True,
            clock=lambda: 0, wall=lambda: self.now, sleep=lambda _: None, max_runs=1)

    def rows(self):
        return {row['kind']: row for row in self.queue.pending()}

    def backoff(self):
        with api.sync_owner(self.store) as owner:
            self.assertIsNotNone(owner)
            learning.request_selector_maintenance(self.decision, force=True, owner=owner, origin='sync')

    def probe(self, row):
        self.assertEqual(row['kind'], 'learning')
        result = learning.due_selector_maintenance(self.project)
        return result['status'] in ('none', 'requested')

    def test_edit_during_sync_stays_immediately_due_after_retry_and_ack(self):
        api.request('laya-sync', str(self.project), start=False)
        def execute(row):
            self.assertEqual(row['kind'], 'laya-sync')
            self.assertEqual(self.rows()['laya-sync']['status'], 'running')
            api.request('laya-sync', str(self.project), start=False)
            self.backoff()
            return True
        self.drain(execute)
        rows = self.rows()
        self.assertEqual(rows['laya-sync']['due'], self.now)
        self.assertEqual(rows['laya-sync']['status'], 'pending')
        self.assertEqual(rows['learning']['due'], 1600)

    def test_vector_thirty_second_reoffer_survives_learning_backoff_and_ack(self):
        api.request('laya-sync', str(self.project), start=False)
        def execute(_row):
            api.request('laya-sync', str(self.project), delay=30)
            self.backoff()
            return True
        self.drain(execute)
        rows = self.rows()
        self.assertEqual(rows['laya-sync']['due'], self.now + 30)
        self.assertEqual(rows['learning']['due'], 1600)

    def test_result_owner_timeout_keeps_file_and_enqueues_learning_retry(self):
        with api.sync_owner(self.store) as held, \
                mock.patch.dict(os.environ, {'MEMVANE_SELECTOR_REQUEST_NS': str(self.row['requestedNs'])}), \
                mock.patch.object(journal, 'verify_selected_route_for_write', side_effect=ValueError('proof')):
            self.assertIsNotNone(held)
            with self.assertRaisesRegex(ValueError, 'proof'):
                learning.run_selector_maintenance(self.project, force=True)
        self.assertEqual(journal.ledger_read_json(self.path), self.row)
        self.assertNotIn('laya-sync', self.rows())
        self.assertIn('learning', self.rows())
        self.assertEqual(self.rows()['learning']['due'], 1600)
        self.now = 1601
        with mock.patch('subprocess.Popen') as spawn:
            self.drain(self.probe)
        spawn.assert_called_once()
        self.assertEqual(self.rows(), {})

    def test_earlier_learning_wake_retains_retry_then_spawns_once_when_due(self):
        api.request('learning', str(self.project), start=False)
        self.backoff()
        self.assertEqual(self.rows()['learning']['due'], self.now)
        with mock.patch('subprocess.Popen') as spawn:
            self.drain(self.probe)
            spawn.assert_not_called()
            waiting = self.rows()['learning']
            self.assertEqual(waiting['status'], 'pending')
            self.assertLess(waiting['due'], 1600)
            self.now = 1601
            self.drain(self.probe)
            self.assertEqual(self.rows(), {})
            api.request('learning', str(self.project), start=False)
            self.drain(self.probe)
        spawn.assert_called_once()

    def test_completed_or_missing_request_does_not_enroll_or_spawn(self):
        journal.ledger_write(self.path, {'requestedNs': 1000*10**9, 'failures': 0, 'retryAfterNs': 0}, replace=True)
        with mock.patch('subprocess.Popen') as spawn:
            for remove in (False, True):
                if remove:
                    self.path.unlink()
                api.request('learning', str(self.project), start=False)
                self.drain(self.probe)
                self.assertEqual(self.rows(), {})
        spawn.assert_not_called()
        self.assertFalse((self.state / 'default-learning.json').exists())

    def test_due_probe_keeps_wake_when_owner_busy_then_requests_after_release(self):
        self.now = 1601
        api.request('learning', str(self.project), start=False)
        with mock.patch('subprocess.Popen') as spawn:
            with api.sync_owner(self.store) as held:
                self.assertIsNotNone(held)
                self.drain(self.probe)
                self.assertEqual(self.rows()['learning']['status'], 'pending')
                spawn.assert_not_called()
            self.now += 10
            self.drain(self.probe)
        spawn.assert_called_once()
        self.assertEqual(self.rows(), {})

    def test_due_probe_revalidates_selected_write_proof_before_spawn(self):
        self.now = 1601
        api.request('learning', str(self.project), start=False)
        with mock.patch('subprocess.Popen') as spawn, \
                mock.patch.object(journal, 'verify_selected_route_for_write', side_effect=ValueError('proof')):
            self.drain(self.probe)
        spawn.assert_not_called()
        self.assertEqual(journal.ledger_read_json(self.path), self.row)
        self.assertEqual(self.rows(), {})


if __name__ == '__main__':
    import unittest
    unittest.main()

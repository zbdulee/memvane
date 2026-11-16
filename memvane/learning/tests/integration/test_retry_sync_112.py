"""Real install/owner and prepare leases; due sync retries spawn only once."""
import json
import os
from pathlib import Path
from types import SimpleNamespace
import time
from contextlib import ExitStack
from unittest import mock

from tests.support.case import FixtureCase
from tests.support.install_builder import build_install
from tests.support.project import make_project
from memvane.learning import selector_maintenance as maintenance
from memvane.learning.context_learning import selector_heal
from memvane.journal import api as journal
from memvane.runtime import api as runtime


class RequestGates(FixtureCase):
    def test_sync_reoffers_backoff_but_does_not_poll_an_absent_runtime(self):
        state = self.home / 'retry-state'
        state.mkdir(mode=0o700)
        decision = {'projectRoot': str(self.home), 'journalPath': str(state / 'journal'),
                    'storePath': str(state / 'store'), 'installId': 'fixture'}
        row = {'requestedNs': 100 * 10**9, 'failures': 1, 'retryAfterNs': 1200 * 10**9}
        journal.ledger_write(state / maintenance.REQUEST_FILE, row, replace=False)
        with mock.patch.object(runtime, 'validate_sync_owner'), \
                mock.patch.object(journal, 'inspect_journal', return_value={
                    'state': 'selected', 'maxPluginVersion': '1.0.12', 'installId': 'fixture'}), \
                mock.patch('memvane.kernel.memvane_version.plugin_version', return_value='1.0.12'), \
                mock.patch('memvane.learning.default_enrollment.record', return_value={'status': 'pending_runtime'}), \
                mock.patch.object(maintenance.time, 'time_ns', return_value=1000 * 10**9), \
                mock.patch.object(selector_heal, 'official_runtime', return_value=('/synthetic/python', '/synthetic/model')) as official, \
                mock.patch.object(runtime, 'request', return_value={'status': 'queued'}) as offer, \
                mock.patch.object(maintenance.subprocess, 'Popen') as spawn:
            maintenance._request_owned(decision, object(), force=False, origin='sync')
            offer.assert_called_once_with('laya-sync', str(self.home), delay=200)
            offer.reset_mock()
            official.side_effect = selector_heal.HealRefused('learning_runtime_not_installed')
            maintenance._request_owned(decision, object(), force=False, origin='sync')
            offer.assert_not_called()
            official.side_effect = None
            official.return_value = ('/synthetic/python', None)
            maintenance._request_owned(decision, object(), force=False, origin='sync')
            offer.assert_not_called()
            spawn.assert_not_called()
        self.assertEqual(journal.ledger_read_json(state / maintenance.REQUEST_FILE), row)

    def test_dropped_retry_offer_is_reported(self):
        with mock.patch.object(runtime, 'request', return_value={'status': 'deferred_queue_capacity'}), \
                mock.patch('memvane.diagnostics.api.emit') as emit:
            maintenance._offer_retry({'projectRoot': str(self.home)}, {'retryAfterNs': time.time_ns()})
        self.assertEqual(emit.call_args.kwargs['reason'], 'retry_offer_dropped')

    def test_gates_report_reason_without_spawning_or_rewriting_backoff(self):
        state = self.home / 'state'
        state.mkdir(mode=0o700)
        decision = {'projectRoot': str(self.home), 'journalPath': str(state / 'journal'),
                    'storePath': str(state / 'store'), 'installId': 'fixture'}
        path = state / maintenance.REQUEST_FILE
        cases = [('prepared', '1.0.12', 'pending_runtime', None, 'not_selected'),
                 ('selected', '1.0.13', 'pending_runtime', None, 'version_behind'),
                 ('selected', '1.0.11', 'disabled', None, 'learning_disabled'),
                 ('selected', '1.0.12', 'preserved', None, 'version_current'),
                 ('selected', '1.0.12', 'pending_runtime', {'requestedNs': 999 * 10**9}, 'request_floor'),
                 ('selected', '1.0.12', 'pending_runtime',
                  {'requestedNs': 100 * 10**9, 'retryAfterNs': 1200 * 10**9}, 'backoff')]
        for selected, version, enrollment, old, reason in cases:
            with self.subTest(reason=reason):
                if path.exists():
                    path.unlink()
                if old is not None:
                    journal.ledger_write(path, old, replace=False)
                with ExitStack() as stack:
                    stack.enter_context(mock.patch.object(runtime, 'validate_sync_owner'))
                    stack.enter_context(mock.patch.object(journal, 'inspect_journal', return_value={
                        'state': selected, 'maxPluginVersion': version, 'installId': 'fixture'}))
                    stack.enter_context(mock.patch('memvane.kernel.memvane_version.plugin_version', return_value='1.0.12'))
                    stack.enter_context(mock.patch('memvane.learning.default_enrollment.record', return_value={'status': enrollment}))
                    stack.enter_context(mock.patch.object(maintenance, '_missing_warm_receipt', return_value=False))
                    stack.enter_context(mock.patch.object(maintenance.time, 'time_ns', return_value=1000 * 10**9))
                    spawn = stack.enter_context(mock.patch.object(maintenance.subprocess, 'Popen'))
                    emit = stack.enter_context(mock.patch('memvane.diagnostics.api.emit'))
                    maintenance._request_owned(decision, object(), force=False)
                spawn.assert_not_called()
                self.assertTrue(any(c.args[0] == 'learning.selector.request' and c.kwargs.get('reason') == reason
                                    for c in emit.call_args_list), reason)
                self.assertEqual(journal.ledger_read_json(path) if path.exists() else None, old)


class RetrySync(FixtureCase):
    def pair(self):
        installs = []
        for index in (1, 2):
            project = make_project(self.home, name='retry%d' % index, dirname='retry%d' % index)
            project.write_note('note.md', 'Synthetic retry note.\n')
            project.commit()
            installs.append(build_install(self, project, install_id=str(index) * 32))
        return installs

    def test_pending_runtime_does_not_schedule_an_hourly_poll(self):
        a, _b = self.pair()
        seed = time.time_ns()
        journal.ledger_write(a.state / maintenance.REQUEST_FILE,
            {'requestedNs': seed, 'failures': 0, 'retryAfterNs': 0}, replace=False)
        with mock.patch.dict(os.environ, {'MEMVANE_SELECTOR_REQUEST_NS': str(seed)}), \
                mock.patch('memvane.learning.runtime_activation.ensure_pending'), \
                mock.patch('memvane.learning.default_enrollment.prepare', return_value={'status': 'pending_runtime'}), \
                mock.patch.object(runtime, 'request') as offer:
            maintenance.run(a.project.root, force=True)
        offer.assert_not_called()
        row = journal.ledger_read_json(a.state / maintenance.REQUEST_FILE)
        self.assertEqual(row['failures'], 1)
        self.assertGreater(row['retryAfterNs'], seed)

    def test_owner_busy_records_reason_before_any_request_timestamp_write(self):
        a, b = self.pair()
        events = []
        with runtime.sync_owner(a.store_path) as owner:
            self.assertIsNotNone(owner)
            with mock.patch.dict(os.environ, {'QMD_SETUP_GUARD_FIXTURE': '0'}), \
                    mock.patch('memvane.learning.default_enrollment.record', return_value={'status': 'pending_runtime'}), \
                    mock.patch('memvane.diagnostics.api.emit', side_effect=lambda event, **row: events.append((event, row))), \
                    mock.patch.object(maintenance.subprocess, 'Popen') as spawn:
                maintenance.request(b.decision)
            spawn.assert_not_called()
        self.assertFalse((b.state / maintenance.REQUEST_FILE).exists())
        self.assertTrue(any(e == 'learning.selector.request' and r.get('reason') == 'owner_busy'
                            for e, r in events))

    def test_prepare_busy_is_reoffered_and_completed_sync_retries_once(self):
        _a, b = self.pair()
        path = b.state / maintenance.REQUEST_FILE
        seed = 1000 * 10**9
        journal.ledger_write(path, {'requestedNs': seed, 'failures': 0, 'retryAfterNs': 0}, replace=False)
        lease = selector_heal._prepare_lease(None)
        events = []
        try:
            with mock.patch.dict(os.environ, {'MEMVANE_SELECTOR_REQUEST_NS': str(seed)}), \
                    mock.patch.object(maintenance.time, 'time_ns', return_value=1001 * 10**9), \
                    mock.patch('memvane.learning.runtime_activation.ensure_pending'), \
                    mock.patch.object(runtime, 'request') as offer, \
                    mock.patch('memvane.diagnostics.api.emit', side_effect=lambda event, **row: events.append((event, row))):
                with self.assertRaisesRegex(selector_heal.HealDeferred, 'learning_prepare_busy'):
                    maintenance.run(b.project.root, force=True)
            deferred = journal.ledger_read_json(path)
            self.assertEqual(deferred['failures'], 1)
            self.assertGreaterEqual(deferred['retryAfterNs'], seed + 600 * 10**9)
            self.assertTrue(any(c.args[:2] == ('laya-sync', str(b.project.root)) and
                                0 < c.kwargs.get('delay', 0) <= 3600 for c in offer.call_args_list))
        finally:
            os.close(lease)
        # Fixture guard stays enabled for the actual store pass. Only the public
        # request's fixture no-op is replaced, keeping its real owned admission.
        def request(decision, *, owner=None, force=False, **_kwargs):
            self.assertIsNotNone(owner)
            return maintenance._request_owned(decision, owner, force=force, origin=_kwargs.get('origin', 'sync'))
        now = deferred['retryAfterNs'] + 10**9
        spawn = mock.Mock()
        with mock.patch.object(maintenance.time, 'time_ns', return_value=now-2*10**9) as clock, \
                mock.patch.object(selector_heal, 'official_runtime', return_value=('/synthetic/python', '/synthetic/model')), \
                mock.patch.object(maintenance, 'request', side_effect=request), \
                mock.patch.object(runtime, 'request', return_value={'status': 'queued'}) as reoffer, \
                mock.patch.object(maintenance, 'subprocess',
                                  SimpleNamespace(Popen=spawn, DEVNULL=maintenance.subprocess.DEVNULL)):
            # An edit/vector wake already queued can finish before retryAfter.
            # Its sync completion must re-offer the remaining delay under owner.
            result = b.run_sync()
            self.assertIn(result['state'], ('indexed', 'unchanged'), result)
            spawn.assert_not_called()
            self.assertTrue(any(call.args[:2] == ('laya-sync', str(b.project.root))
                                and call.kwargs.get('delay', 0) > 0 for call in reoffer.call_args_list))
            clock.return_value = now
            for _ in range(2):
                result = b.run_sync()
                self.assertIn(result['state'], ('indexed', 'unchanged'), result)
        self.assertEqual(spawn.call_count, 1)
        retried = journal.ledger_read_json(path)
        self.assertEqual(retried['requestedNs'], now)
        self.assertEqual(retried['failures'], 1)
        self.assertEqual(retried['retryAfterNs'], 0)

    def test_request_exception_is_reported_with_fixed_code(self):
        a, _b = self.pair()
        with mock.patch.dict(os.environ, {'QMD_SETUP_GUARD_FIXTURE': '0'}), \
                mock.patch.object(maintenance, '_request_owned', side_effect=RuntimeError('/private/synthetic/secret')), \
                mock.patch('memvane.diagnostics.api.emit') as emit:
            maintenance.request(a.decision)
        record = emit.call_args.kwargs
        self.assertEqual(record['reason'], 'request_failed')
        self.assertNotIn('/private/synthetic', json.dumps(record))

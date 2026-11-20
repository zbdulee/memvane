"""SessionStart warns only about recent hook calls from older sessions."""
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest import mock

from memvane.host import host_response
from memvane.kernel.memvane_version import version_key


class SessionNotice(unittest.TestCase):
    def row(self, version='1.0.9', *, session='old', event='recall', age=0, host='codex'):
        return {'pluginVersion': version, 'session': session, 'host': host,
                'event': event, 'ts': datetime.fromtimestamp(2000000000 - age,
                                                          timezone.utc).isoformat()}

    def notice(self, rows, *, running='1.0.9', session='old', host='codex'):
        from memvane.diagnostics import api as diagnostics
        from memvane.runtime import api as runtime
        bundle = SimpleNamespace(version=version_key(running),
                                 installed_version=lambda **kwargs: version_key('1.0.11'))
        with mock.patch.object(host_response, '_learning_settings_notice', return_value='existing'), \
                mock.patch.object(host_response, '_wiki_recovery_notice', return_value=''), \
                mock.patch.object(host_response, '_learning_runtime_notice', side_effect=lambda x, *_: x), \
                mock.patch.object(diagnostics, 'vector_damage_seen', return_value=False), \
                mock.patch.object(diagnostics, 'build_progress_events', return_value=rows), \
                mock.patch.object(runtime, 'background_bundle', return_value=bundle), \
                mock.patch('time.time', return_value=2000000000):
            return host_response._session_health_notice({'journalPath': '/synthetic/journal.sqlite'}, '', host,
                                                       {'session_id': session})

    def test_recent_old_hook_appends_one_restart_line(self):
        result = self.notice([self.row(), self.row(session='other', event='index')])
        self.assertTrue(result.startswith('existing\n'))
        self.assertEqual(result.count('세션을 다시 열어'), 1)

    def test_latest_event_for_same_session_clears_old_observation(self):
        self.assertEqual(self.notice([self.row(age=5), self.row('1.0.11')]), 'existing')
        self.assertIn('세션을 다시 열어', self.notice([
            self.row(age=5), self.row('1.0.11', session='new')]))

    def test_workers_stale_future_and_unknown_versions_are_ignored(self):
        self.assertEqual(self.notice([self.row(event='learning.capture'),
            self.row(age=3601), self.row(age=-10), self.row('unknown')]), 'existing')

    def test_latest_means_timestamp_order_and_missing_sessions_are_not_attributed(self):
        self.assertEqual(self.notice([self.row('1.0.11'), self.row(age=5)]), 'existing')
        self.assertEqual(self.notice([self.row(session=None)], session=None), 'existing')

    def test_reopened_current_hook_and_other_hosts_do_not_trigger_warning(self):
        self.assertEqual(self.notice([self.row()], running='1.0.11', session='reopened'), 'existing')
        self.assertEqual(self.notice([self.row(host='claude')]), 'existing')
        self.assertEqual(self.notice([self.row(session='closed')]), 'existing')

    def test_notice_metadata_failure_keeps_session_start_independent(self):
        from memvane.runtime import api as runtime
        with mock.patch.object(runtime, 'background_bundle', side_effect=RuntimeError('synthetic inventory failure')):
            from memvane.host.session_notice import append_notice
            self.assertEqual(append_notice({'journalPath': '/synthetic/journal'}, 'existing', 'codex',
                                           {'session_id': 'old'}), 'existing')

    def test_linked_worktree_session_start_carries_restart_notice_for_all_hosts(self):
        from memvane.host import session_notice
        from memvane.journal import api as journal
        main = {'state': 'candidate', 'projectRoot': '/synthetic'}
        decision = {'state': 'worktree', 'mainRoute': main}
        proof = {'productRouteAllowed': True, 'stateFormat': 1, 'writerFloor': '1.0.0'}
        with mock.patch.object(host_response, '_timed_route_proof', return_value=proof), \
                mock.patch.object(host_response, '_provider_recovery', return_value=None), \
                mock.patch.object(host_response, '_request_worktree_sync'), \
                mock.patch.object(journal, 'verify_selected_route', return_value=proof), \
                mock.patch.object(session_notice, 'append_notice',
                                  side_effect=lambda _decision, notice, _host, _payload: notice + '\nrestart'):
            for host in ('claude', 'codex', 'hermes'):
                result = host_response._serve_worktree('update-queued', host, decision,
                                                       {'hook_event_name': 'SessionStart'})
                context = result['notice'] if host == 'hermes' else result['hookSpecificOutput']['additionalContext']
                self.assertTrue(context.endswith('\nrestart'), host)

"""UI state files: preferences, auto-open record, server record, fixture home refusal, browser gate."""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
from unittest import mock

from memvane.kernel import memvane_paths
from memvane.ui import lifecycle, platform_io, preferences
from memvane.ui.types import DEFAULT_PREFERENCES, SERVER_SCHEMA
from tests.support.case import FixtureCase, account_home


class PreferenceTests(FixtureCase):
    def ui(self):
        return memvane_paths.state_base(self.home) / 'ui'

    def test_absent_preferences_read_as_defaults_and_create_nothing(self):
        value, status = preferences.load()
        self.assertEqual((value, status), (DEFAULT_PREFERENCES, 'absent'))
        self.assertFalse(value['autoOpenOnSessionStart'])
        self.assertFalse(memvane_paths.state_base(self.home).exists())

    def test_update_writes_an_owner_only_file(self):
        stored = preferences.update({'autoOpenOnSessionStart': True, 'idleShutdownMinutes': 5})
        self.assertTrue(stored['autoOpenOnSessionStart'])
        path = self.ui() / 'preferences.json'
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.ui().stat().st_mode), 0o700)
        value, status = preferences.load()
        self.assertEqual((value['idleShutdownMinutes'], status), (5, 'ok'))
        self.assertEqual(json.loads(path.read_text())['schema'], DEFAULT_PREFERENCES['schema'])

    def test_invalid_changes_are_refused_before_any_write(self):
        for change, code in (({}, 'ui_preferences_empty'), ({'nope': 1}, 'ui_preferences_unknown_key'),
                             ({'schema': 'x'}, 'ui_preferences_unknown_key'),
                             ({'idleShutdownMinutes': 0}, 'ui_preferences_invalid_value'),
                             ({'idleShutdownMinutes': True}, 'ui_preferences_invalid_value'),
                             ({'autoOpenOnSessionStart': 'yes'}, 'ui_preferences_invalid_value'),
                             ({'autoOpenScope': 'all_sessions'}, 'ui_preferences_invalid_value')):
            with self.assertRaisesRegex(preferences.PreferenceError, code):
                preferences.update(change)
        self.assertFalse(self.ui().exists())

    def test_a_malformed_or_unsafe_file_reads_as_defaults_with_auto_open_off(self):
        self.ui().mkdir(parents=True, mode=0o700)
        path = self.ui() / 'preferences.json'
        path.write_text(json.dumps({'autoOpenOnSessionStart': True, 'extra': 1}))
        path.chmod(0o600)
        self.assertEqual(preferences.load(), (DEFAULT_PREFERENCES, 'ui_preferences_invalid'))
        path.write_text('{not json')
        self.assertEqual(preferences.load()[1], 'ui_file_malformed')
        path.write_text(json.dumps({'autoOpenOnSessionStart': True}))
        path.chmod(0o644)
        self.assertEqual(preferences.load(), (DEFAULT_PREFERENCES, 'ui_file_unsafe'))
        path.unlink()
        target = self.tmp / 'elsewhere.json'
        target.write_text(json.dumps({'autoOpenOnSessionStart': True}))
        target.chmod(0o600)
        os.symlink(target, path)
        self.assertEqual(preferences.load(), (DEFAULT_PREFERENCES, 'ui_file_unsafe'))

    def test_auto_open_record_round_trips(self):
        self.assertIsNone(preferences.auto_open_state()['lastAutoOpenAt'])
        preferences.record_auto_open(opened_at=10.0)
        state = preferences.record_auto_open(closed_at=20.0)
        self.assertEqual((state['lastAutoOpenAt'], state['lastUserClosedAt']), (10.0, 20.0))
        self.assertEqual(preferences.auto_open_state()['lastUserClosedAt'], 20.0)
        self.assertEqual(stat.S_IMODE((self.ui() / 'auto-open-state.json').stat().st_mode), 0o600)

    def test_a_test_process_never_touches_the_account_home(self):
        account = account_home()
        with self.assertRaisesRegex(platform_io.UiStateRefused, 'ui_fixture_live_home'):
            platform_io.ui_dir(account)
        self.assertEqual(preferences.load(account)[1], 'ui_fixture_live_home')
        with self.assertRaisesRegex(preferences.PreferenceError, 'ui_fixture_live_home'):
            preferences.update({'idleShutdownMinutes': 5}, account)
        self.assertIsNone(lifecycle.read_server(account))


class ServerRecordTests(FixtureCase):
    def record(self, **extra):
        value = {'schema': SERVER_SCHEMA, 'pid': os.getpid(), 'port': 40123,
                 'bundleRoot': str(lifecycle.bundle_root()), 'bundleVersion': '1.0.0',
                 'startedAt': '2026-11-28T00:00:00+00:00', 'instanceNonce': 'n' * 32, 'controlSecret': 's' * 43}
        value.update(extra)
        return value

    def test_record_round_trip_and_nonce_guarded_clear(self):
        lifecycle.write_server(self.record())
        self.assertEqual(lifecycle.read_server(), self.record())
        path = memvane_paths.state_base(self.home) / 'ui' / 'server.json'
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertFalse(lifecycle.clear_server(nonce='other'))
        self.assertTrue(path.exists())
        self.assertTrue(lifecycle.clear_server(nonce='n' * 32))
        self.assertFalse(path.exists())

    def test_malformed_records_are_ignored_or_refused(self):
        for bad in (self.record(port=0), self.record(pid=True), self.record(schema='x'),
                    dict(self.record(), extra=1)):
            with self.assertRaisesRegex(lifecycle.LifecycleError, 'ui_server_record_invalid'):
                lifecycle.write_server(bad)
        lifecycle.write_server(self.record())
        path = memvane_paths.state_base(self.home) / 'ui' / 'server.json'
        path.write_text(json.dumps(self.record(port='x')))
        self.assertIsNone(lifecycle.read_server())

    def test_only_a_newer_or_a_damaged_bundle_replaces_a_running_server(self):
        current = lifecycle.bundle_version()
        self.assertIsNotNone(current)
        live = self.record(bundleVersion=current)
        self.assertFalse(lifecycle.should_replace(current, live))
        self.assertFalse(lifecycle.should_replace('0.0.1', live))
        self.assertTrue(lifecycle.should_replace('999.0.0', live))
        self.assertTrue(lifecycle.should_replace('1.2.0', self.record(bundleVersion='1.2.0-rc.1')))
        self.assertFalse(lifecycle.should_replace('1.2.0-rc.1', self.record(bundleVersion='1.2.0')))
        gone = self.record(bundleVersion=current, bundleRoot=str(self.tmp / 'removed-bundle'))
        self.assertTrue(lifecycle.should_replace(current, gone))
        self.assertTrue(lifecycle.should_replace(current, self.record(bundleVersion=current, bundleRoot='rel')))

    def test_idle_shutdown_needs_no_stream_no_grace_and_the_idle_time(self):
        due = dict(active_clients=0, grace_active=False, idle_seconds=1800, idle_minutes=30)
        self.assertTrue(lifecycle.idle_due(**due))
        self.assertFalse(lifecycle.idle_due(**dict(due, active_clients=1)))
        self.assertFalse(lifecycle.idle_due(**dict(due, grace_active=True)))
        self.assertFalse(lifecycle.idle_due(**dict(due, idle_seconds=1799)))


class BrowserGateTests(FixtureCase):
    def test_a_test_process_never_starts_a_browser(self):
        with mock.patch('webbrowser.open') as opener:
            self.assertEqual(platform_io.open_browser('http://127.0.0.1:4000/#tab=overview'),
                             (False, 'test_process'))
        opener.assert_not_called()

    def test_environment_gates(self):
        self.assertEqual(platform_io.browser_blocker({'MEMVANE_TEST_RUNTIME_ROOT': '/x'}), 'test_process')
        self.assertEqual(platform_io.browser_blocker({'CODEX_SANDBOX': 'seatbelt'}), 'sandbox')
        self.assertEqual(platform_io.browser_blocker({'CLAUDE_HEADLESS': '1'}), 'headless')
        self.assertEqual(platform_io.browser_blocker({'CI': 'true'}), 'headless')
        self.assertEqual(platform_io.browser_blocker({'SSH_CONNECTION': '1 2 3 4'}), 'ssh')
        self.assertEqual(platform_io.browser_blocker({'SSH_TTY': '/dev/ttys001'}), 'ssh')
        self.assertEqual(platform_io.browser_blocker({'BROWSER': 'w3m', 'DISPLAY': ':0'}), 'text_browser')

    def test_only_dashboard_urls_reach_an_opener(self):
        self.assertIsNotNone(platform_io.SAFE_URL.fullmatch('http://127.0.0.1:4000/#code=a-b_c&tab=wiki'))
        for url in ('http://127.0.0.1:4000/#x"&calc.exe', 'http://evil.test:80/', 'javascript:alert(1)',
                    'http://127.0.0.1:4000/ ; rm', 'http://127.0.0.1:4000/#a|b', 'http://localhost.evil:1/'):
            self.assertIsNone(platform_io.SAFE_URL.fullmatch(url), url)


class PrivateJsonTests(FixtureCase):
    def test_atomic_private_write_leaves_no_temporary_file(self):
        path = self.tmp / 'value.json'
        platform_io.write_private_json(path, {'a': 1})
        self.assertEqual(platform_io.read_private_json(path), {'a': 1})
        self.assertEqual(sorted(p.name for p in Path(self.tmp).iterdir()), ['value.json'])
        self.assertIsNone(platform_io.read_private_json(self.tmp / 'absent.json'))

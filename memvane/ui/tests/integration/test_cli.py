"""``entry/run.py ui`` as a user runs it: detached start, reuse, status, stop and preferences.

The server starts detached under the case HOME; no browser starts in a test
process (``open`` reports ``manualUrl`` instead) and the stop leaves no
process or ``server.json`` behind.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
from unittest import mock

from memvane.kernel import memvane_paths
from memvane.ui import lifecycle, local_client, platform_io
from tests.support.case import FixtureCase, scaled

ROOT = Path(__file__).resolve().parents[4]


class CliTests(FixtureCase):
    def setUp(self):
        super().setUp()
        self.addCleanup(self.kill_leftover)

    def kill_leftover(self):
        record = lifecycle.read_server(self.home)
        if record is not None and platform_io.is_ui_server(record['pid'], record['bundleRoot']):
            os.kill(record['pid'], signal.SIGKILL)

    def ui(self, *args):
        out = subprocess.run([sys.executable, '-B', str(ROOT / 'entry' / 'run.py'), 'ui', *args],
                             capture_output=True, text=True, timeout=scaled(60), cwd=str(self.tmp),
                             env=dict(os.environ))
        self.assertEqual(out.stderr, '')
        return out.returncode, json.loads(out.stdout)

    def exchange(self, port, code):
        body = json.dumps({'code': code}).encode()
        lines = ['POST /api/auth/exchange HTTP/1.1', f'Host: 127.0.0.1:{port}', 'X-MemVane-UI: 1',
                 'Connection: close', f'Content-Length: {len(body)}']
        with socket.create_connection(('127.0.0.1', port), scaled(10)) as conn:
            conn.sendall(('\r\n'.join(lines) + '\r\n\r\n').encode() + body)
            data = b''
            while True:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                data += chunk
        return local_client.parse_response(data)[0]

    def test_open_reuse_status_and_stop(self):
        code, opened = self.ui('open', '--no-open', '--cwd', str(self.tmp), '--tab', 'wiki')
        self.assertEqual(code, 0, opened)
        self.assertEqual((opened['opened'], opened['reason'], opened['reused']), (False, 'no_open', False))
        self.assertNotIn('code=', opened['url'])
        self.assertTrue(opened['url'].endswith('/#tab=wiki'), opened['url'])
        self.assertEqual(opened['manualUrlTtlSeconds'], 120)
        port = opened['port']
        self.assertTrue(opened['manualUrl'].startswith(f'http://127.0.0.1:{port}/#code='))
        record = lifecycle.read_server(self.home)
        self.assertEqual((record['port'], record['bundleRoot']), (port, str(ROOT)))
        path = memvane_paths.state_base(self.home) / 'ui' / 'server.json'
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

        manual = opened['manualUrl'].split('#code=', 1)[1].split('&', 1)[0]
        self.assertEqual(self.exchange(port, manual), 200)
        self.assertEqual(self.exchange(port, manual), 401)

        # A test process never starts a browser: the second open reuses the server and reports why.
        code, again = self.ui('open', '--cwd', str(self.tmp))
        self.assertEqual((code, again['reused'], again['port'], again['opened'], again['reason']),
                         (0, True, port, False, 'test_process'))
        self.assertNotEqual(again['manualUrl'], opened['manualUrl'])

        code, status = self.ui('status')
        self.assertEqual((code, status['running'], status['server']['port']), (0, True, port))
        self.assertNotIn('controlSecret', json.dumps(status))

        code, stopped = self.ui('stop')
        self.assertEqual((code, stopped), (0, {'stopped': True, 'how': 'shutdown'}))
        self.assertFalse(path.exists())
        self.assertFalse(platform_io.pid_alive(record['pid']))
        code, status = self.ui('status')
        self.assertEqual((code, status['running']), (0, False))
        code, stopped = self.ui('stop')
        self.assertEqual((code, stopped['how']), (0, 'not_running'))

    def test_preferences_through_the_command(self):
        code, shown = self.ui('pref')
        self.assertEqual((code, shown['status'], shown['preferences']['autoOpenOnSessionStart']),
                         (0, 'absent', False))
        code, changed = self.ui('pref', '--auto-open', 'on', '--idle-minutes', '5')
        self.assertEqual(code, 0)
        self.assertEqual((changed['preferences']['autoOpenOnSessionStart'],
                          changed['preferences']['idleShutdownMinutes']), (True, 5))
        code, refused = self.ui('pref', '--idle-minutes', '0')
        self.assertEqual((code, refused['status'], refused['error']),
                         (1, 'error', 'ui_preferences_invalid_value'))

    def test_a_session_start_opens_nothing_without_the_opt_in(self):
        from memvane.ui import cli, preferences
        opener = mock.Mock(side_effect=AssertionError('no browser may start'))
        self.assertEqual(cli.open_dashboard(cwd=str(self.tmp), from_session_start=True, home=self.home,
                                            opener=opener),
                         {'status': 'ok', 'opened': False, 'reason': 'auto_open_off'})
        preferences.update({'autoOpenOnSessionStart': True}, self.home)
        self.assertEqual(cli.open_dashboard(cwd=str(self.tmp), from_session_start=True, home=self.home,
                                            opener=opener)['reason'], 'test_process')
        opener.assert_not_called()
        self.assertIsNone(lifecycle.read_server(self.home))
        self.assertIsNone(preferences.auto_open_state(self.home)['lastAutoOpenAt'])

    def test_an_equal_or_older_bundle_reuses_the_running_server(self):
        from memvane.ui import server
        httpd, app = server.make_server(home=self.home)
        import threading
        thread = threading.Thread(target=httpd.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True)
        thread.start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(thread.join, scaled(10))
        self.addCleanup(app.request_stop, 'user_stop')
        for running in (lifecycle.bundle_version(), '999.0.0'):
            app.bundle_version = running
            lifecycle.write_server(app.record(), self.home)
            spawn = mock.Mock(side_effect=AssertionError('no new server may start'))
            result = lifecycle.ensure_server(self.home, spawn=spawn)
            self.assertEqual((result['reused'], result['server']['instanceNonce']), (True, app.nonce))
            spawn.assert_not_called()

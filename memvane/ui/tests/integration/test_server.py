"""The loopback server end to end over real sockets: security, auth exchange, API, SSE, idle stop."""
from __future__ import annotations

import json
import socket
import stat
import threading

from memvane.kernel import memvane_paths
from memvane.ui import local_client, server
from memvane.ui.types import CSP, MAX_SSE_CLIENTS
from tests.support.case import FixtureCase, scaled


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class ServerCase(FixtureCase):
    clock = None

    def setUp(self):
        super().setUp()
        options = {'clock': self.clock} if self.clock is not None else {}
        self.httpd, self.app = server.make_server(home=self.home, **options)
        self.port = self.app.port
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={'poll_interval': 0.05},
                                       daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)

    def stop(self):
        self.app.request_stop('user_stop')
        self.thread.join(scaled(10))
        self.httpd.server_close()

    def raw(self, method, path, headers=(), body=b''):
        """``(status, headers, body)`` of a request whose every header the test chooses."""
        lines = [f'{method} {path} HTTP/1.1', 'Connection: close', f'Content-Length: {len(body)}', *headers]
        data = ('\r\n'.join(lines) + '\r\n\r\n').encode('latin-1') + body
        with socket.create_connection(('127.0.0.1', self.port), scaled(10)) as conn:
            conn.sendall(data)
            chunks = []
            while True:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
        return local_client.parse_response(b''.join(chunks))

    def browser(self, method, path, *, cookie=None, body=None, extra=()):
        headers = [f'Host: 127.0.0.1:{self.port}', 'X-MemVane-UI: 1', 'Sec-Fetch-Site: same-origin',
                   f'Origin: http://127.0.0.1:{self.port}', *extra]
        if cookie:
            headers.append(f'Cookie: memvane_ui_session={cookie}')
        data = b'' if body is None else json.dumps(body).encode()
        status, found, payload = self.raw(method, path, headers, data)
        return status, found, (json.loads(payload) if payload and 'json' in found['content-type'][0] else payload)

    def login(self):
        code = self.app.codes.issue(60)
        status, found, _ = self.browser('POST', '/api/auth/exchange', body={'code': code})
        self.assertEqual(status, 200)
        cookie = found['set-cookie'][0]
        return cookie.split(';', 1)[0].split('=', 1)[1]

    def control(self, method, path, payload=None):
        return local_client.control(self.app.record(), method, path, payload)


class SecurityTests(ServerCase):
    def test_health_is_minimal_and_carries_the_security_headers(self):
        status, found, body = self.raw('GET', '/api/health', [f'Host: 127.0.0.1:{self.port}'])
        self.assertEqual((status, json.loads(body)), (200, {'status': 'ok'}))
        for name, value in (('cross-origin-resource-policy', 'same-origin'), ('x-content-type-options', 'nosniff'),
                            ('cache-control', 'no-store'), ('referrer-policy', 'no-referrer')):
            self.assertEqual(found[name], [value])
        self.assertNotIn(b'bundle', body.lower())

    def test_spoofed_hosts_are_refused_everywhere(self):
        for path in ('/api/health', '/', '/api/overview', '/api/internal/status'):
            for host in ('evil.test', f'evil.test:{self.port}', f'LOCALHOST:{self.port}', f'localhost.:{self.port}'):
                status, _found, body = self.raw('GET', path, [f'Host: {host}', 'X-MemVane-UI: 1'])
                self.assertEqual((status, json.loads(body)['error']), (403, 'host_refused'), (path, host))

    def test_every_api_but_health_needs_the_ui_header_and_a_session(self):
        for path in ('/api/overview', '/api/session', '/api/events/stream', f'/api/installs/{"a" * 32}/summary'):
            status, _f, body = self.raw('GET', path, [f'Host: 127.0.0.1:{self.port}'])
            self.assertEqual((status, json.loads(body)['error']), (403, 'ui_header_required'), path)
            status, _f, body = self.browser('GET', path)
            self.assertEqual((status, body['error']), (401, 'session_required'), path)

    def test_cross_site_requests_are_refused_even_with_a_session(self):
        cookie = self.login()
        for extra in (('Sec-Fetch-Site: same-site',), ('Sec-Fetch-Site: cross-site',)):
            headers = [f'Host: 127.0.0.1:{self.port}', 'X-MemVane-UI: 1', f'Cookie: memvane_ui_session={cookie}',
                       *extra]
            status, _f, body = self.raw('GET', '/api/session', headers)
            self.assertEqual((status, json.loads(body)['error']), (403, 'cross_site_refused'))
        headers = [f'Host: 127.0.0.1:{self.port}', 'X-MemVane-UI: 1', 'Origin: http://127.0.0.1:3000',
                   f'Cookie: memvane_ui_session={cookie}']
        status, _f, body = self.raw('GET', '/api/session', headers)
        self.assertEqual((status, json.loads(body)['error']), (403, 'origin_refused'))

    def test_other_methods_and_large_bodies_are_refused(self):
        for method in ('OPTIONS', 'DELETE', 'PATCH'):
            status, _f, _b = self.raw(method, '/api/overview', [f'Host: 127.0.0.1:{self.port}', 'X-MemVane-UI: 1'])
            self.assertEqual(status, 405, method)
        cookie = self.login()
        headers = [f'Host: 127.0.0.1:{self.port}', 'X-MemVane-UI: 1', f'Cookie: memvane_ui_session={cookie}',
                   'Content-Length: 1048577']
        lines = ['PUT /api/preferences HTTP/1.1', 'Connection: close', *headers]
        with socket.create_connection(('127.0.0.1', self.port), scaled(10)) as conn:
            conn.sendall(('\r\n'.join(lines) + '\r\n\r\n').encode())
            reply = conn.recv(65536)
        self.assertTrue(reply.startswith(b'HTTP/1.1 413'), reply[:40])

    def test_static_root_serves_html_with_the_policy_and_rejects_escapes(self):
        status, found, body = self.raw('GET', '/', [f'Host: localhost:{self.port}'])
        self.assertEqual(status, 200)
        self.assertEqual(found['content-security-policy'], [CSP])
        self.assertTrue(found['content-type'][0].startswith('text/html'))
        self.assertTrue(body)
        for path in ('/../plugin.json', '/%2e%2e/plugin.json', '/.env', '/assets/missing.js', '/x.exe'):
            status, _f, _b = self.raw('GET', path, [f'Host: localhost:{self.port}'])
            self.assertEqual(status, 404, path)


class AuthTests(ServerCase):
    def test_a_code_becomes_a_session_cookie_once(self):
        code = self.app.codes.issue(60)
        status, found, body = self.browser('POST', '/api/auth/exchange', body={'code': code})
        self.assertEqual((status, body), (200, {'ok': True}))
        cookie = found['set-cookie'][0]
        self.assertRegex(cookie, r'^memvane_ui_session=[A-Za-z0-9_-]{43}; HttpOnly; SameSite=Strict; Path=/$')
        status, _f, body = self.browser('POST', '/api/auth/exchange', body={'code': code})
        self.assertEqual((status, body['error']), (401, 'code_invalid'))
        token = cookie.split(';', 1)[0].split('=', 1)[1]
        status, _f, body = self.browser('GET', '/api/session', cookie=token)
        self.assertEqual(status, 200)
        self.assertEqual(body['activeClients'], 0)
        self.assertTrue(body['readOnly'])
        self.assertIn('preferences', body)
        self.assertNotIn('bundleRoot', body)

    def test_internal_endpoints_need_the_control_secret(self):
        headers = [f'Host: 127.0.0.1:{self.port}', 'X-MemVane-UI: 1', 'X-MemVane-Control: wrong']
        status, _f, body = self.raw('GET', '/api/internal/status', headers)
        self.assertEqual((status, json.loads(body)['error']), (401, 'control_required'))
        status, value = self.control('GET', '/api/internal/status')
        self.assertEqual((status, value['instanceNonce'], value['activeClients']), (200, self.app.nonce, 0))
        self.assertEqual(self.control('POST', '/api/internal/code', {'ttlSeconds': 999})[0], 400)
        status, value = self.control('POST', '/api/internal/code', {'ttlSeconds': 120})
        self.assertEqual((status, value['ttlSeconds']), (200, 120))
        status, _f, _b = self.browser('POST', '/api/auth/exchange', body={'code': value['code']})
        self.assertEqual(status, 200)

    def test_preferences_change_through_the_api(self):
        cookie = self.login()
        status, _f, body = self.browser('PUT', '/api/preferences', cookie=cookie, body={'idleShutdownMinutes': 5})
        self.assertEqual((status, body['preferences']['idleShutdownMinutes']), (200, 5))
        path = memvane_paths.state_base(self.home) / 'ui' / 'preferences.json'
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        status, _f, body = self.browser('PUT', '/api/preferences', cookie=cookie, body={'nope': True})
        self.assertEqual((status, body['error']), (400, 'ui_preferences_unknown_key'))

    def test_overview_without_a_registry_is_a_banner_not_an_error(self):
        cookie = self.login()
        status, _f, body = self.browser('GET', '/api/overview', cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual((body['registry']['status'], body['installs']), ('absent', []))
        self.assertIn('registry_absent', [item['code'] for item in body['banner']])
        status, _f, body = self.browser('GET', f'/api/installs/{"a" * 32}/summary', cookie=cookie)
        self.assertEqual((status, body['error']), (404, 'target_unknown'))
        status, _f, body = self.browser('GET', f'/api/installs/{"a" * 32}/errors?details=2', cookie=cookie)
        self.assertEqual((status, body['error']), (400, 'details_invalid'))


class StreamTests(ServerCase):
    def open_stream(self, cookie):
        conn = socket.create_connection(('127.0.0.1', self.port), scaled(10))
        lines = ['GET /api/events/stream HTTP/1.1', f'Host: 127.0.0.1:{self.port}', 'X-MemVane-UI: 1',
                 'Sec-Fetch-Site: same-origin', f'Cookie: memvane_ui_session={cookie}', 'Connection: close']
        conn.sendall(('\r\n'.join(lines) + '\r\n\r\n').encode())
        self.addCleanup(conn.close)
        return conn

    def read_until(self, conn, marker, buffer=b''):
        while marker not in buffer:
            chunk = conn.recv(65536)
            if not chunk:
                break
            buffer += chunk
        self.assertIn(marker, buffer)
        return buffer

    def test_stream_heartbeat_session_started_and_drain(self):
        cookie = self.login()
        conn = self.open_stream(cookie)
        head = self.read_until(conn, b'event: heartbeat')
        self.assertTrue(head.startswith(b'HTTP/1.1 200'))
        self.assertIn(b'text/event-stream', head)
        self.wait_until(lambda: self.app.hub.active_clients == 1)
        status, value = self.control('POST', '/api/internal/session-started',
                                     {'targetId': 'b' * 32, 'host': 'codex'})
        self.assertEqual((status, value['delivered']), (200, 1))
        data = self.read_until(conn, b'event: session_started')
        event = data.split(b'event: session_started\ndata: ', 1)[1].split(b'\n', 1)[0]
        self.assertEqual({k: v for k, v in json.loads(event).items() if k != 'ts'},
                         {'targetId': 'b' * 32, 'installId': 'b' * 32, 'host': 'codex'})
        self.app.request_stop('user_stop')
        self.read_until(conn, b'event: server_draining')

    def test_seventeenth_stream_is_refused_and_the_others_stay(self):
        cookie = self.login()
        conns = [self.open_stream(cookie) for _ in range(MAX_SSE_CLIENTS)]
        for conn in conns:
            self.read_until(conn, b'event: heartbeat')
        self.wait_until(lambda: self.app.hub.active_clients == MAX_SSE_CLIENTS)
        status, _f, body = self.browser('GET', '/api/events/stream', cookie=cookie)
        self.assertEqual((status, body['error']), (429, 'too_many_streams'))
        self.assertEqual(self.app.hub.active_clients, MAX_SSE_CLIENTS)
        conns[0].close()
        self.wait_until(lambda: self.app.hub.broadcast('runtime_updated', {'ts': 'x'}) < MAX_SSE_CLIENTS
                        and self.app.hub.active_clients == MAX_SSE_CLIENTS - 1)

    def test_bad_session_started_payloads_are_refused(self):
        self.assertEqual(self.control('POST', '/api/internal/session-started', {'targetId': 'zz'})[0], 400)
        self.assertEqual(self.control('POST', '/api/internal/session-started', {'host': 'other'})[0], 400)


class IdleTests(ServerCase):
    clock = Clock()

    def test_the_server_stops_after_the_idle_time_without_streams(self):
        self.app.touch()
        self.assertFalse(self.app.tick(self.clock()))
        self.clock.now += 30 * 60 - 1
        self.assertFalse(self.app.tick(self.clock()))
        client = self.app.hub.register()
        self.clock.now += 10
        self.assertFalse(self.app.tick(self.clock()))
        self.app.hub.unregister(client)
        self.assertFalse(self.app.tick(self.clock()))
        self.clock.now += 10
        self.assertTrue(self.app.tick(self.clock()))
        self.assertEqual(self.app.stop_reason, 'idle')
        self.thread.join(scaled(10))
        self.assertFalse(self.thread.is_alive())

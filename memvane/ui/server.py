"""The dashboard's loopback HTTP server: JSON API, SSE stream and the prebuilt ``ui/dist`` files.

Bound to ``127.0.0.1`` only, without address reuse, with daemon request
threads, a 10 s socket timeout, a 1 MiB request body limit and at most 32
requests in flight (one more gets ``503``). Every response carries the
security headers; HTML adds the content security policy. ``ui/dist`` may be
missing (a checkout that has not built the frontend): the API still serves
and ``/`` explains what is missing.
"""
from __future__ import annotations

from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import socket
import socketserver
import stat
import threading
import time
from urllib.parse import parse_qs

from memvane.ui import (auth, lifecycle, platform_io, preferences, read_adapters, recall_adapters,
                        runtime_adapters, wiki_adapters)
from memvane.ui.sse import FileWatcher, SseHub, now_iso
from memvane.ui.types import (BIND_HOST, CODE_TTLS, DRAIN_REASONS, HEARTBEAT_SECONDS, MANUAL_CODE_TTL_SECONDS,
                              MAX_BODY_BYTES, MAX_STATIC_BYTES, MAX_WORKERS, REQUEST_TIMEOUT_SECONDS,
                              SERVER_SCHEMA, STATIC_TYPES, TABS, WATCH_SECONDS, parse_target_id)

STATIC_PART = re.compile(r'[A-Za-z0-9_-][A-Za-z0-9._-]{0,127}')
WATCH_LIMIT = 8
FALLBACK_HTML = b"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>MemVane dashboard</title></head>
<body><h1>MemVane dashboard</h1>
<p>The prebuilt dashboard files (ui/dist) are not present in this plugin copy.
The local API is running; build the frontend or use a released plugin copy.</p>
</body></html>
"""


class UiApp:
    """Shared state of one server: auth, streams, preferences and the identity in ``server.json``."""

    def __init__(self, *, home=None, read_only=False, clock=time.monotonic):
        self.home = home
        self.clock = clock
        self.codes = auth.CodeBook(clock=clock)
        self.sessions = auth.SessionStore()
        self.hub = SseHub(clock=clock)
        self.watcher = FileWatcher()
        self.nonce = secrets.token_hex(16)
        self.secret = secrets.token_urlsafe(32)
        self.started_at = datetime.now(timezone.utc).isoformat(timespec='seconds')
        self.bundle_root = str(lifecycle.bundle_root())
        self.bundle_version = lifecycle.bundle_version() or 'unknown'
        self.read_only_flag = read_only
        self.last_activity = clock()
        self.port = None
        self.server = None
        self.stopping = threading.Event()
        self.stop_reason = None
        self.watched = {}
        self._lock = threading.Lock()

    # -- identity and state

    def installed(self):
        from memvane.kernel.memvane_execution_guard import installed_plugin_root
        try:
            return bool(installed_plugin_root())
        except (OSError, ValueError, RuntimeError):
            return False

    def preferences(self):
        return preferences.load(self.home)[0]

    def read_only(self):
        return bool(self.read_only_flag or self.preferences().get('readOnlyDefault') or not self.installed())

    def record(self):
        return {'schema': SERVER_SCHEMA, 'pid': os.getpid(), 'port': self.port, 'bundleRoot': self.bundle_root,
                'bundleVersion': self.bundle_version, 'startedAt': self.started_at,
                'instanceNonce': self.nonce, 'controlSecret': self.secret}

    def touch(self):
        self.last_activity = self.clock()

    def idle_remaining(self):
        minutes = self.preferences().get('idleShutdownMinutes', 30)
        return max(0, int(minutes * 60 - (self.clock() - self.last_activity)))

    def observe(self, install_id, state_root):
        """Watch an install's event log (the most recent ``WATCH_LIMIT`` installs)."""
        with self._lock:
            self.watched.pop(install_id, None)
            self.watched[install_id] = str(state_root)
            while len(self.watched) > WATCH_LIMIT:
                self.watched.pop(next(iter(self.watched)))

    def watched_files(self):
        with self._lock:
            files = {str(Path(root) / 'events.jsonl'): 'install:' + install
                     for install, root in self.watched.items()}
        try:
            from memvane.kernel import memvane_paths
            wake = memvane_paths.state_base(self.home) / 'wake'
            files[str(wake / 'runtime-wakes.sqlite3')] = 'runtime'
            files[str(wake / 'runtime-wakes.sqlite3-wal')] = 'runtime'
        except (OSError, RuntimeError):
            pass
        return files

    def status(self):
        return {'instanceNonce': self.nonce, 'bundleVersion': self.bundle_version, 'bundleRoot': self.bundle_root,
                'pid': os.getpid(), 'port': self.port, 'startedAt': self.started_at,
                'activeClients': self.hub.active_clients, 'lastSseClosedAt': self.hub.last_closed_at,
                'readOnly': self.read_only(), 'idleSecondsRemaining': self.idle_remaining()}

    # -- stopping and the background tick

    def request_stop(self, reason):
        with self._lock:
            if self.stopping.is_set():
                return
            self.stop_reason = reason
            self.stopping.set()
        self.hub.drain(reason)
        if self.server is not None:
            threading.Thread(target=self.server.shutdown, daemon=True).start()

    def tick(self, now):
        """One background step: heartbeat, watched files, idle shutdown. Returns the next due times."""
        prefs = self.preferences()
        changed = self.watcher.poll(self.watched_files())
        installs = [label.split(':', 1)[1] for label in changed if label.startswith('install:')]
        if installs:
            self.hub.broadcast('events_updated', {'installIds': installs})
        if 'runtime' in changed:
            self.hub.broadcast('runtime_updated', {'ts': now_iso()})
        if lifecycle.idle_due(active_clients=self.hub.active_clients,
                              grace_active=self.hub.grace_active(prefs['reconnectGraceSeconds']),
                              idle_seconds=now - self.last_activity,
                              idle_minutes=prefs['idleShutdownMinutes']):
            self.request_stop('idle')
            return True
        return False

    def run_ticks(self):
        next_beat = self.clock() + HEARTBEAT_SECONDS
        while not self.stopping.wait(WATCH_SECONDS):
            now = self.clock()
            try:
                if self.tick(now):
                    return
                if now >= next_beat:
                    next_beat = now + HEARTBEAT_SECONDS
                    if self.hub.active_clients:
                        self.hub.broadcast('heartbeat', {'ts': now_iso(), 'activeClients': self.hub.active_clients,
                                                         'idleSecondsRemaining': self.idle_remaining()})
            except Exception:  # noqa: BLE001 - a failed tick never stops the server
                continue


class UiServer(ThreadingHTTPServer):
    allow_reuse_address = False
    daemon_threads = True
    request_queue_size = 64

    def __init__(self, app, port=0):
        self.app = app
        self._slots = threading.BoundedSemaphore(MAX_WORKERS)
        super().__init__((BIND_HOST, port), Handler)
        app.server = self
        app.port = self.server_address[1]

    def server_bind(self):
        exclusive = getattr(socket, 'SO_EXCLUSIVEADDRUSE', None)
        if exclusive is not None:
            self.socket.setsockopt(socket.SOL_SOCKET, exclusive, 1)
        # TCPServer.server_bind only: HTTPServer's name lookup can stall on a resolver.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = BIND_HOST, self.server_address[1]

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            try:
                request.sendall(b'HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\n'
                                b'Connection: close\r\n\r\n')
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


_TARGET_RE = r'([0-9a-f]{32}(?:(?::|%3[Aa])[0-9a-f]{12})?)'


def _norm_target(raw):
    return raw.replace('%3A', ':').replace('%3a', ':')


ROUTES = (
    ('GET', re.compile(r'/api/session'), 'session'),
    ('GET', re.compile(r'/api/overview'), 'overview'),
    ('POST', re.compile(r'/api/overview/reconcile'), 'reconcile'),
    ('GET', re.compile(r'/api/installs/([0-9a-f]{32})/(summary|recalls|errors)'), 'install'),
    ('GET', re.compile(rf'/api/targets/{_TARGET_RE}/wiki'), 'wiki_list'),
    ('GET', re.compile(rf'/api/targets/{_TARGET_RE}/wiki/card'), 'wiki_card'),
    ('GET', re.compile(rf'/api/targets/{_TARGET_RE}/wiki/source'), 'wiki_source'),
    ('POST', re.compile(rf'/api/targets/{_TARGET_RE}/query-playground'), 'query_playground'),
    ('GET', re.compile(r'/api/runtime'), 'runtime'),
    ('GET', re.compile(r'/api/installs/([0-9a-f]{32})/learning'), 'learning'),
    ('POST', re.compile(r'/api/installs/([0-9a-f]{32})/learning/check'), 'learning_check'),
    ('GET', re.compile(r'/api/events/stream'), 'stream'),
    ('PUT', re.compile(r'/api/preferences'), 'put_preferences'),
    ('POST', re.compile(r'/api/shutdown'), 'shutdown'),
)
INTERNAL = (
    ('GET', '/api/internal/status', 'internal_status'),
    ('POST', '/api/internal/code', 'internal_code'),
    ('POST', '/api/internal/shutdown', 'internal_shutdown'),
    ('POST', '/api/internal/session-started', 'internal_session_started'),
)


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'MemVane-UI'
    sys_version = ''
    timeout = REQUEST_TIMEOUT_SECONDS

    @property
    def app(self):
        return self.server.app

    def log_message(self, format, *args):  # noqa: A002 - BaseHTTPRequestHandler's name
        return

    # -- responses

    def _send(self, status, body=b'', content_type='application/json', *, html=False, extra=()):
        self.close_connection = True
        self.send_response(status)
        for name, value in auth.response_headers(html=html):
            self.send_header(name, value)
        for name, value in extra:
            self.send_header(name, value)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Connection', 'close')
        self.end_headers()
        if body and self.command != 'HEAD':
            self.wfile.write(body)

    def _json(self, status, value, extra=()):
        body = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        self._send(status, body, 'application/json; charset=utf-8', extra=extra)

    def _error(self, status, code):
        self._json(status, {'error': code, 'code': code})

    def _body(self):
        """The JSON object of the request body ({} when empty); None after an error response."""
        if self.headers.get('Transfer-Encoding'):
            self._error(411, 'length_required')
            return None
        raw = self.headers.get('Content-Length', '0')
        if not (raw.isascii() and raw.isdigit()):
            self._error(400, 'content_length_invalid')
            return None
        length = int(raw)
        if length > MAX_BODY_BYTES:
            self._error(413, 'body_too_large')
            return None
        data = self.rfile.read(length) if length else b''
        if not data:
            return {}
        try:
            value = json.loads(data.decode('utf-8'))
        except (UnicodeDecodeError, ValueError):
            self._error(400, 'body_invalid')
            return None
        if not isinstance(value, dict):
            self._error(400, 'body_invalid')
            return None
        return value

    # -- methods

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler's naming
        self._dispatch('GET')

    def do_POST(self):  # noqa: N802
        self._dispatch('POST')

    def do_PUT(self):  # noqa: N802
        self._dispatch('PUT')

    def _not_allowed(self):
        self._error(405, 'method_not_allowed')

    do_OPTIONS = do_DELETE = do_PATCH = do_HEAD = _not_allowed

    def _dispatch(self, method):
        try:
            path, _sep, query = self.path.partition('?')
            if not path.startswith('/api/'):
                if method != 'GET':
                    return self._not_allowed()
                return self._static(path)
            if path == '/api/health':
                if not auth.host_ok(self.headers.get('Host'), self.app.port):
                    return self._error(403, 'host_refused')
                return self._json(200, {'status': 'ok'}) if method == 'GET' else self._not_allowed()
            if path.startswith('/api/internal/'):
                return self._internal(method, path)
            refusal = auth.browser_refusal(self.headers, self.app.port)
            if refusal is not None:
                return self._error(*refusal)
            if path == '/api/auth/exchange':
                return self._exchange() if method == 'POST' else self._not_allowed()
            if not self.app.sessions.valid(auth.cookie_value(self.headers.get('Cookie'))):
                return self._error(401, 'session_required')
            for verb, pattern, name in ROUTES:
                match = pattern.fullmatch(path)
                if match:
                    if verb != method:
                        return self._not_allowed()
                    self.app.touch()
                    return getattr(self, '_' + name)(*match.groups(), query=parse_qs(query))
            return self._error(404, 'not_found')
        except read_adapters.TargetError as exc:
            return self._error(exc.status, str(exc))
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            self.close_connection = True
        except Exception:  # noqa: BLE001 - one failed request never takes the server down
            try:
                self._error(500, 'internal_error')
            except OSError:
                self.close_connection = True

    # -- browser endpoints

    def _exchange(self):
        body = self._body()
        if body is None:
            return None
        if not self.app.codes.consume(body.get('code')):
            return self._error(401, 'code_invalid')
        token = self.app.sessions.create()
        self.app.touch()
        return self._json(200, {'ok': True}, extra=[('Set-Cookie', auth.session_cookie(token))])

    def _session(self, query):
        app = self.app
        return self._json(200, {'bundleVersion': app.bundle_version, 'activeClients': app.hub.active_clients,
                                'isInstalledPluginCache': app.installed(), 'readOnly': app.read_only(),
                                'preferences': app.preferences(), 'idleSecondsRemaining': app.idle_remaining(),
                                'startedAt': app.started_at, 'tabs': list(TABS)})

    def _overview(self, query, reconcile=False):
        value = read_adapters.overview(home=self.app.home, reconcile=reconcile, observe=self.app.observe)
        return self._json(200, value)

    def _reconcile(self, query):
        return self._overview(query, reconcile=True)

    def _install(self, install_id, view, query):
        first = {key: values[0] for key, values in query.items() if values}
        home = self.app.home
        if view == 'summary':
            value = read_adapters.install_summary(install_id, hours=first.get('hours'), home=home)
        elif view == 'recalls':
            value = read_adapters.install_recalls(install_id, hours=first.get('hours'),
                                                  limit=first.get('limit', 100), outcome=first.get('outcome'),
                                                  host=first.get('host'), target=first.get('target'), home=home)
        else:
            details = first.get('details', '0')
            if details not in ('0', '1'):
                return self._error(400, 'details_invalid')
            value = read_adapters.install_errors(install_id, hours=first.get('hours'), details=details == '1',
                                                 home=home)
        return self._json(200, value)

    def _wiki_list(self, target_id, query):
        first = {key: values[0] for key, values in query.items() if values}
        return self._json(200, wiki_adapters.wiki_list(
            _norm_target(target_id), status=first.get('status', ''), q=first.get('q', ''),
            limit=first.get('limit', 200), home=self.app.home))

    def _wiki_card(self, target_id, query):
        first = {key: values[0] for key, values in query.items() if values}
        return self._json(200, wiki_adapters.wiki_card(
            _norm_target(target_id), first.get('rel'), home=self.app.home))

    def _wiki_source(self, target_id, query):
        first = {key: values[0] for key, values in query.items() if values}
        return self._json(200, wiki_adapters.wiki_source(
            _norm_target(target_id), first.get('rel'), highlights=first.get('highlights'),
            home=self.app.home))

    def _query_playground(self, target_id, query):
        body = self._body()
        if body is None:
            return None
        return self._json(200, recall_adapters.query_playground(
            _norm_target(target_id), body, home=self.app.home))

    def _runtime(self, query):
        return self._json(200, runtime_adapters.runtime_overview(home=self.app.home))

    def _learning(self, install_id, query):
        return self._json(200, runtime_adapters.learning_overview(
            install_id, check_invariants=False, home=self.app.home))

    def _learning_check(self, install_id, query):
        body = self._body()
        if body is None:
            return None
        return self._json(200, runtime_adapters.learning_overview(
            install_id, check_invariants=True, home=self.app.home))

    def _put_preferences(self, query):
        body = self._body()
        if body is None:
            return None
        try:
            value = preferences.update(body, self.app.home)
        except preferences.PreferenceError as exc:
            return self._error(400, str(exc))
        except platform_io.UiStateRefused as exc:
            return self._error(409, str(exc))
        return self._json(200, {'preferences': value})

    def _shutdown(self, query):
        self._json(200, {'stopping': True})
        self.app.request_stop('user_stop')

    def _stream(self, query):
        client = self.app.hub.register()
        if client is None:
            return self._error(429, 'too_many_streams')
        try:
            self.close_connection = True
            self.send_response(200)
            for name, value in auth.response_headers():
                self.send_header(name, value)
            self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
            self.send_header('Connection', 'close')
            self.send_header('X-Accel-Buffering', 'no')
            self.end_headers()
            client.push(_heartbeat(self.app))
            while True:
                data = client.next(1.0)
                if data is None:
                    if client.closed.is_set() or self.app.stopping.is_set() and client.frames.empty():
                        break
                    continue
                self.wfile.write(data)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, socket.timeout, OSError):
            pass
        finally:
            self.app.hub.unregister(client)
        return None

    # -- internal (control secret) endpoints

    def _internal(self, method, path):
        if not auth.host_ok(self.headers.get('Host'), self.app.port):
            return self._error(403, 'host_refused')
        refusal = auth.browser_refusal(self.headers, self.app.port)
        if refusal is not None:
            return self._error(*refusal)
        if not auth.control_ok(self.headers, self.app.secret):
            return self._error(401, 'control_required')
        for verb, known, name in INTERNAL:
            if path == known:
                return getattr(self, '_' + name)() if verb == method else self._not_allowed()
        return self._error(404, 'not_found')

    def _internal_status(self):
        value = self.app.status()
        value['preferences'] = self.app.preferences()
        return self._json(200, value)

    def _internal_code(self):
        body = self._body()
        if body is None:
            return None
        ttl = body.get('ttlSeconds', CODE_TTLS[0])
        if ttl not in CODE_TTLS or isinstance(ttl, bool):
            return self._error(400, 'ttl_invalid')
        return self._json(200, {'code': self.app.codes.issue(ttl), 'ttlSeconds': ttl})

    def _internal_shutdown(self):
        body = self._body()
        if body is None:
            return None
        reason = body.get('reason', 'user_stop')
        if reason not in DRAIN_REASONS:
            return self._error(400, 'reason_invalid')
        self._json(200, {'stopping': True})
        self.app.request_stop(reason)
        return None

    def _internal_session_started(self):
        body = self._body()
        if body is None:
            return None
        target = body.get('targetId')
        host = body.get('host')
        if target is not None and parse_target_id(target) is None:
            return self._error(400, 'target_id_invalid')
        if host not in (None, 'claude', 'codex', 'hermes', 'gemini'):
            return self._error(400, 'host_invalid')
        parsed = parse_target_id(target) if target else None
        delivered = self.app.hub.broadcast('session_started', {
            'targetId': target, 'installId': parsed[0] if parsed else None, 'host': host, 'ts': now_iso()})
        return self._json(200, {'delivered': delivered, 'activeClients': self.app.hub.active_clients})

    # -- static files

    def _static(self, path):
        if not auth.host_ok(self.headers.get('Host'), self.app.port):
            return self._error(403, 'host_refused')
        dist = Path(self.app.bundle_root) / 'ui' / 'dist'
        parts = [part for part in path.split('/') if part]
        if not parts:
            parts = ['index.html']
        if not all(STATIC_PART.fullmatch(part) for part in parts):
            return self._error(404, 'not_found')
        content_type = STATIC_TYPES.get(os.path.splitext(parts[-1])[1].lower())
        if content_type is None:
            return self._error(404, 'not_found')
        data = _read_static(dist, parts)
        if data is None:
            if parts == ['index.html']:
                return self._send(200, FALLBACK_HTML, STATIC_TYPES['.html'], html=True)
            return self._error(404, 'not_found')
        return self._send(200, data, content_type, html=content_type.startswith('text/html'))


def _heartbeat(app):
    from memvane.ui.sse import frame
    return frame('heartbeat', {'ts': now_iso(), 'activeClients': app.hub.active_clients,
                               'idleSecondsRemaining': app.idle_remaining()})


def _read_static(dist, parts):
    """The bytes of ``dist/<parts>`` opened component by component without following a symlink."""
    nofollow = getattr(os, 'O_NOFOLLOW', 0)
    directory = getattr(os, 'O_DIRECTORY', 0)
    try:
        fd = os.open(dist, os.O_RDONLY | directory)
    except OSError:
        return None
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | directory | nofollow, dir_fd=fd)
            os.close(fd)
            fd = child
        leaf = os.open(parts[-1], os.O_RDONLY | nofollow | getattr(os, 'O_NONBLOCK', 0), dir_fd=fd)
        try:
            info = os.fstat(leaf)
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_STATIC_BYTES:
                return None
            chunks, left = [], info.st_size
            while left > 0:
                chunk = os.read(leaf, min(left, 1 << 20))
                if not chunk:
                    break
                chunks.append(chunk)
                left -= len(chunk)
            return b''.join(chunks)
        finally:
            os.close(leaf)
    except OSError:
        return None
    finally:
        os.close(fd)


def make_server(*, port=0, read_only=False, home=None, clock=time.monotonic):
    """A bound server (not yet serving) and its app; ``port`` 0 picks a free port."""
    if not isinstance(port, int) or isinstance(port, bool) or not 0 <= port < 65536:
        raise ValueError('ui_port_invalid')
    app = UiApp(home=home, read_only=read_only, clock=clock)
    return UiServer(app, port), app


def manual_url(app, *, target=None, tab='overview'):
    """A URL with a fresh 120 s one-time code, for a person to open by hand."""
    return page_url(app.port, code=app.codes.issue(MANUAL_CODE_TTL_SECONDS), target=target, tab=tab)


def page_url(port, *, code=None, target=None, tab='overview'):
    fragment = []
    if code:
        fragment.append('code=' + code)
    if target:
        fragment.append('target=' + target)
    fragment.append('tab=' + (tab if tab in TABS else 'overview'))
    return f'http://127.0.0.1:{port}/#' + '&'.join(fragment)


def serve(*, port=0, read_only=False, home=None, spawned=False, announce=None):
    """Run the server in the foreground until it is stopped; ``server.json`` names it meanwhile.

    ``spawned`` is the detached child of ``ensure_server`` (whose parent holds
    ``server.lock``); a foreground start takes the lock itself and refuses
    while another live server is recorded.
    """
    if spawned:
        httpd, app = make_server(port=port, read_only=read_only, home=home)
        lifecycle.write_server(app.record(), home)
    else:
        with platform_io.server_lock(lifecycle.lock_path(home)):
            server, _status = lifecycle.running(home)
            if server is not None:
                raise lifecycle.LifecycleError('ui_server_already_running')
            httpd, app = make_server(port=port, read_only=read_only, home=home)
            lifecycle.write_server(app.record(), home)
    _install_signal_handlers(app)
    ticker = threading.Thread(target=app.run_ticks, name='memvane-ui-tick', daemon=True)
    ticker.start()
    try:
        if announce is not None:
            announce(manual_url(app))
        httpd.serve_forever(poll_interval=0.5)
    finally:
        app.request_stop(app.stop_reason or 'user_stop')
        httpd.server_close()
        lifecycle.clear_server(home, app.nonce)
    return app.stop_reason


def _install_signal_handlers(app):
    import signal
    if threading.current_thread() is not threading.main_thread():
        return
    for name in ('SIGTERM', 'SIGINT', 'SIGHUP'):
        number = getattr(signal, name, None)
        if number is not None:
            signal.signal(number, lambda _signum, _frame: app.request_stop('user_stop'))

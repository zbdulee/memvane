"""Request checks of the dashboard: one-time codes, session cookies, Host/Origin/fetch metadata.

Every ``/api/*`` request but ``/api/health`` needs the ``X-MemVane-UI: 1``
header, an exact ``Host`` (``127.0.0.1:<port>`` or ``localhost:<port>``), a
``Sec-Fetch-Site`` of ``same-origin`` when present and an ``Origin`` equal to
the page origin when present. Browser endpoints need a live session cookie;
internal endpoints need the control secret instead. Secrets compare in
constant time and codes are consumed on first use.
"""
from __future__ import annotations

from collections import OrderedDict
import hmac
import secrets
import threading
import time

from memvane.ui.types import (CODE_TTLS, CONTROL_HEADER, CSP, MAX_PENDING_CODES, MAX_SESSIONS,
                              SECURITY_HEADERS, SESSION_COOKIE, UI_HEADER)


class CodeBook:
    """One-time exchange codes with a time to live; each code is accepted once."""

    def __init__(self, clock=time.monotonic, limit=MAX_PENDING_CODES):
        self._clock = clock
        self._limit = limit
        self._codes = OrderedDict()
        self._lock = threading.Lock()

    def issue(self, ttl):
        if ttl not in CODE_TTLS:
            raise ValueError('ui_code_ttl_invalid')
        code = secrets.token_urlsafe(32)
        with self._lock:
            self._expire()
            self._codes[code] = self._clock() + ttl
            while len(self._codes) > self._limit:
                self._codes.popitem(last=False)
        return code

    def consume(self, code):
        """Whether ``code`` is a live code; a matching code is removed either way."""
        if not isinstance(code, str) or not code or len(code) > 256:
            return False
        with self._lock:
            self._expire()
            found = None
            for known in list(self._codes):
                if hmac.compare_digest(known.encode(), code.encode()):
                    found = known
            if found is None:
                return False
            del self._codes[found]
            return True

    def pending(self):
        with self._lock:
            self._expire()
            return len(self._codes)

    def _expire(self):
        now = self._clock()
        for code in [code for code, end in self._codes.items() if end <= now]:
            del self._codes[code]


class SessionStore:
    """The most recently used ``MAX_SESSIONS`` session tokens (one per browser tab exchange)."""

    def __init__(self, limit=MAX_SESSIONS):
        self._limit = limit
        self._tokens = OrderedDict()
        self._lock = threading.Lock()

    def create(self):
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._tokens[token] = True
            while len(self._tokens) > self._limit:
                self._tokens.popitem(last=False)
        return token

    def valid(self, token):
        if not isinstance(token, str) or not token or len(token) > 256:
            return False
        with self._lock:
            found = None
            for known in list(self._tokens):
                if hmac.compare_digest(known.encode(), token.encode()):
                    found = known
            if found is None:
                return False
            self._tokens.move_to_end(found)
            return True

    def __len__(self):
        with self._lock:
            return len(self._tokens)


def allowed_hosts(port):
    return (f'127.0.0.1:{port}', f'localhost:{port}')


def host_ok(host, port):
    """Exact ``127.0.0.1:<port>`` or ``localhost:<port>``: no userinfo, case or trailing-dot variant."""
    return isinstance(host, str) and host in allowed_hosts(port)


def origin_for(host, port):
    """The page origin of an allowed Host header."""
    first, second = allowed_hosts(port)
    if host == first:
        return f'http://127.0.0.1:{port}'
    if host == second:
        return f'http://localhost:{port}'
    return None


def cookie_value(header):
    """The session token of a ``Cookie`` header, or None."""
    if not isinstance(header, str):
        return None
    for part in header.split(';'):
        name, sep, value = part.strip().partition('=')
        if sep and name == SESSION_COOKIE:
            return value
    return None


def session_cookie(token):
    return f'{SESSION_COOKIE}={token}; HttpOnly; SameSite=Strict; Path=/'


def browser_refusal(headers, port, *, api=True):
    """``(status, code)`` of a refused request by its headers, or None; sessions are checked apart."""
    host = headers.get('Host')
    if not host_ok(host, port):
        return 403, 'host_refused'
    site = headers.get('Sec-Fetch-Site')
    if site is not None and site != 'same-origin':
        return 403, 'cross_site_refused'
    origin = headers.get('Origin')
    if origin is not None and origin != origin_for(host, port):
        return 403, 'origin_refused'
    if api and headers.get(UI_HEADER) != '1':
        return 403, 'ui_header_required'
    return None


def control_ok(headers, secret):
    given = headers.get(CONTROL_HEADER)
    if not isinstance(given, str) or not isinstance(secret, str) or not secret:
        return False
    return hmac.compare_digest(given.encode(), secret.encode())


def response_headers(*, html=False):
    """Headers every response carries; HTML adds the content security policy."""
    headers = list(SECURITY_HEADERS)
    if html:
        headers.append(('Content-Security-Policy', CSP))
    return headers

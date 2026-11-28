"""Request checks: one-time codes, the session LRU, Host/Origin/fetch metadata, headers."""
from __future__ import annotations

import unittest

from memvane.ui import auth
from memvane.ui.types import CSP, MAX_SESSIONS, SESSION_COOKIE


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class CodeBookTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.book = auth.CodeBook(clock=self.clock)

    def test_a_code_is_accepted_once(self):
        code = self.book.issue(60)
        self.assertGreaterEqual(len(code), 43)
        self.assertTrue(self.book.consume(code))
        self.assertFalse(self.book.consume(code))
        self.assertEqual(self.book.pending(), 0)

    def test_codes_expire_after_their_time_to_live(self):
        short, manual = self.book.issue(60), self.book.issue(120)
        self.clock.now += 60
        self.assertFalse(self.book.consume(short))
        self.assertTrue(self.book.consume(manual))
        late = self.book.issue(120)
        self.clock.now += 120
        self.assertFalse(self.book.consume(late))

    def test_only_the_two_lifetimes_are_issued(self):
        for ttl in (0, 30, 61, 3600, True):
            with self.assertRaisesRegex(ValueError, 'ui_code_ttl_invalid'):
                self.book.issue(ttl)

    def test_wrong_or_malformed_codes_are_refused(self):
        code = self.book.issue(60)
        for given in (None, '', code[:-1], code + 'x', 'x' * 300, 42):
            self.assertFalse(self.book.consume(given))
        self.assertTrue(self.book.consume(code))

    def test_outstanding_codes_are_bounded_oldest_first(self):
        first = self.book.issue(60)
        for _ in range(32):
            self.book.issue(60)
        self.assertEqual(self.book.pending(), 32)
        self.assertFalse(self.book.consume(first))


class SessionTests(unittest.TestCase):
    def test_the_most_recent_sessions_stay_valid(self):
        store = auth.SessionStore()
        tokens = [store.create() for _ in range(MAX_SESSIONS + 1)]
        self.assertEqual(len(store), MAX_SESSIONS)
        self.assertFalse(store.valid(tokens[0]))
        self.assertTrue(all(store.valid(token) for token in tokens[1:]))

    def test_use_refreshes_a_session(self):
        store = auth.SessionStore(limit=2)
        old, newer = store.create(), store.create()
        self.assertTrue(store.valid(old))
        store.create()
        self.assertTrue(store.valid(old))
        self.assertFalse(store.valid(newer))

    def test_cookie_shape_and_parsing(self):
        cookie = auth.session_cookie('tok')
        self.assertEqual(cookie, f'{SESSION_COOKIE}=tok; HttpOnly; SameSite=Strict; Path=/')
        self.assertEqual(auth.cookie_value(f'a=1; {SESSION_COOKIE}=tok; b=2'), 'tok')
        self.assertIsNone(auth.cookie_value('other=1'))
        self.assertIsNone(auth.cookie_value(None))


class HeaderTests(unittest.TestCase):
    PORT = 8123

    def headers(self, **extra):
        value = {'Host': f'127.0.0.1:{self.PORT}', 'X-MemVane-UI': '1'}
        value.update(extra)
        return value

    def test_host_must_match_exactly(self):
        for host in ('127.0.0.1:8123', 'localhost:8123'):
            self.assertTrue(auth.host_ok(host, self.PORT), host)
        for host in ('LOCALHOST:8123', 'localhost.:8123', 'user@127.0.0.1:8123', '127.0.0.1:8124',
                     '127.0.0.1', 'evil.example:8123', '[::1]:8123', '', None):
            self.assertFalse(auth.host_ok(host, self.PORT), host)

    def test_browser_refusals(self):
        self.assertIsNone(auth.browser_refusal(self.headers(), self.PORT))
        self.assertIsNone(auth.browser_refusal(self.headers(**{'Sec-Fetch-Site': 'same-origin',
                                                                'Origin': 'http://127.0.0.1:8123'}), self.PORT))
        cases = {
            'host_refused': self.headers(Host='attacker.test:8123'),
            'cross_site_refused': self.headers(**{'Sec-Fetch-Site': 'same-site'}),
            'origin_refused': self.headers(Origin='http://localhost:8123'),
            'ui_header_required': {'Host': f'127.0.0.1:{self.PORT}'},
        }
        for code, headers in cases.items():
            self.assertEqual(auth.browser_refusal(headers, self.PORT), (403, code))
        self.assertEqual(auth.browser_refusal(self.headers(**{'Sec-Fetch-Site': 'cross-site'}), self.PORT),
                         (403, 'cross_site_refused'))
        self.assertIsNone(auth.browser_refusal({'Host': f'localhost:{self.PORT}'}, self.PORT, api=False))

    def test_control_secret_compares_exactly(self):
        self.assertTrue(auth.control_ok({'X-MemVane-Control': 's3cret'}, 's3cret'))
        for given in ({}, {'X-MemVane-Control': 's3cre'}, {'X-MemVane-Control': ''}):
            self.assertFalse(auth.control_ok(given, 's3cret'))
        self.assertFalse(auth.control_ok({'X-MemVane-Control': 'x'}, ''))

    def test_every_response_carries_the_security_headers(self):
        plain = dict(auth.response_headers())
        self.assertEqual(plain, {'Cross-Origin-Resource-Policy': 'same-origin', 'X-Content-Type-Options': 'nosniff',
                                 'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'})
        html = dict(auth.response_headers(html=True))
        self.assertEqual(html['Content-Security-Policy'], CSP)
        self.assertIn("frame-ancestors 'none'", CSP)


if __name__ == '__main__':
    unittest.main()

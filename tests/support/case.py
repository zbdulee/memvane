"""S2: the fixture test case: a private HOME and test runtime root per test.

``FixtureCase.setUp`` refuses unless the run is a fixture run (``runDomain``
or an e2e wrapper): HOME outside the account home, ``QMD_SETUP_GUARD_FIXTURE=1``
and an absolute ``MEMVANE_TEST_RUNTIME_ROOT``. Each test then gets
``self.home`` (a new 0700 directory under the run HOME, set as ``HOME``),
``self.runtime`` (its own 0700 test runtime root, set as
``MEMVANE_TEST_RUNTIME_ROOT``) and ``self.tmp``; the environment is restored
afterwards. Waiting is by polling a predicate in this process: no ``sleep``
child process.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import pwd
import tempfile
import time
import unittest


def account_home():
    return Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()


def require_fixture_env():
    """The run HOME (resolved); raises ``RuntimeError`` outside a fixture run."""
    home = Path(os.environ.get('HOME', '')).resolve()
    account = account_home()
    runtime = os.environ.get('MEMVANE_TEST_RUNTIME_ROOT')
    if (home == account or account in home.parents
            or os.environ.get('QMD_SETUP_GUARD_FIXTURE') != '1'
            or not runtime or not os.path.isabs(runtime)):
        raise RuntimeError('fixture run required: temporary HOME, QMD_SETUP_GUARD_FIXTURE=1 '
                           'and an absolute MEMVANE_TEST_RUNTIME_ROOT')
    return home


def wait_until(predicate, timeout=10.0, interval=0.02):
    """Poll ``predicate`` until it returns a truthy value (returned) or ``timeout`` passes."""
    end = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() >= end:
            raise AssertionError('condition not reached within %.1f s' % timeout)
        time.sleep(interval)


async def wait_until_async(predicate, timeout=10.0, interval=0.02):
    """``wait_until`` for an event loop: the loop keeps running (and reaping children)."""
    end = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() >= end:
            raise AssertionError('condition not reached within %.1f s' % timeout)
        await asyncio.sleep(interval)


class FixtureCase(unittest.TestCase):
    """A test with its own HOME, test runtime root and scratch directory."""

    def setUp(self):
        super().setUp()
        try:
            base = require_fixture_env()
        except RuntimeError as exc:
            self.fail(str(exc))
        # Short names: the per-install socket path under HOME is at most 100 bytes.
        self.home = Path(tempfile.mkdtemp(prefix='h', dir=base)).resolve()
        os.chmod(self.home, 0o700)
        self.runtime = self.home / 'rt'
        self.runtime.mkdir(mode=0o700)
        self.tmp = self.home / 'tmp'
        self.tmp.mkdir(mode=0o700)
        saved = {key: os.environ.get(key) for key in ('HOME', 'MEMVANE_TEST_RUNTIME_ROOT', 'TMPDIR')}

        def restore():
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            tempfile.tempdir = None
        self.addCleanup(restore)
        os.environ['HOME'] = str(self.home)
        os.environ['MEMVANE_TEST_RUNTIME_ROOT'] = str(self.runtime)
        os.environ['TMPDIR'] = str(self.tmp)
        tempfile.tempdir = None

    wait_until = staticmethod(wait_until)
    wait_until_async = staticmethod(wait_until_async)

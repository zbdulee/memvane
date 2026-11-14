"""S6: the fixture provider: the only test code that replaces the installed provider.

The behavior lives in the product seam (``memvane.provider.fixture_port``):
a deterministic provider answering from a declarative spec. This file writes
that spec as the test runtime root's ``provider-fixture.json`` (so hook,
route and wake paths, and child processes, find it) and is itself the port
in-process use cases take as ``provider=``: every port call goes to
``memvane.provider.api.fixture_port()``, the provider the current spec
describes. Defaults: review ``review_required`` with ``REVIEW_SHA``, a fixed
sealed runtime digest, the recorded manifest as identity proof, vector
``[1, 0, ...]`` per document, query admission refused (lexical recall), a
no-op warm service. ``change_identity()`` makes every identity proof refuse
with ``provider_identity_changed``.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

SPEC_SCHEMA = 'memvane-test-provider-fixture-v1'
REVIEW_SHA = '9' * 64
RUNTIME_FILES_SHA = 'c' * 64
PORT_METHODS = ('review', 'sealed_files_sha256', 'provider_manifest', 'reviewed_provider',
                'admit_query', 'start_service', 'vector_profile', 'manifest')


def _spec_path():
    raw = os.environ.get('MEMVANE_TEST_RUNTIME_ROOT')
    if not raw or not os.path.isabs(raw):
        raise RuntimeError('fixture provider needs an absolute MEMVANE_TEST_RUNTIME_ROOT')
    from memvane.provider.types import FIXTURE_FILE
    return Path(raw) / FIXTURE_FILE


class FixtureProvider:
    def __init__(self, dimension=2, *, vector=None, identity_changed=False, query_admission='refuse'):
        if type(dimension) is not int or dimension < 1:
            raise ValueError('dimension')
        self.dimension = dimension
        self.vector = list(vector) if vector is not None else [1.0] + [0.0] * (dimension - 1)
        self.identity_changed = identity_changed
        self.query_admission = query_admission
        self._path = None

    def spec(self):
        return {'schema': SPEC_SCHEMA, 'dimension': self.dimension, 'vector': self.vector,
                'proposalSha256': REVIEW_SHA, 'runtimeFilesSha256': RUNTIME_FILES_SHA,
                'identityChanged': self.identity_changed, 'queryAdmission': self.query_admission}

    # -- seam --------------------------------------------------------------------
    def install(self, case):
        """Publish the spec at the case's test runtime root; removed at cleanup."""
        self._path = _spec_path()
        self._publish()
        case.addCleanup(self.uninstall)
        return self

    def uninstall(self):
        if self._path is not None:
            try:
                os.unlink(self._path)
            except FileNotFoundError:
                pass
            self._path = None

    def change_identity(self, changed=True):
        """From now on every identity proof refuses with ``provider_identity_changed``."""
        self.identity_changed = changed
        if self._path is not None:
            self._publish()

    def _publish(self):
        raw = json.dumps(self.spec(), sort_keys=True).encode('utf-8')
        temporary = self._path.with_name('.' + self._path.name + '.partial')
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        try:
            os.write(fd, raw)
        finally:
            os.close(fd)
        os.replace(temporary, self._path)

    # -- port: the product fixture provider of the current spec --------------------
    def current(self):
        from memvane.provider.api import fixture_port
        if self._path is None:
            raise RuntimeError('install(case) the fixture provider before using it as a port')
        port = fixture_port()
        if port is None:
            raise RuntimeError('the test runtime root holds no fixture spec')
        return port

    def __getattr__(self, name):
        if name in PORT_METHODS:
            return getattr(self.current(), name)
        raise AttributeError(name)

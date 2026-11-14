"""The one place a test replaces the installed provider (inert in production).

Production obtains the provider review, the sealed runtime files digest, the
per-file provider identity proof, the reviewed provider handle, the query
admission and the warm embedding service from the installed Laya runtime.
A test replaces all six through a *port*: an object with

    review(*, plugin_root, state_root, model_path, dimension, runtime_generation) -> dict
    sealed_files_sha256(path) -> str
    provider_manifest(record, profile, *, allow_local_unavailable) -> manifest | (manifest, bool)
    reviewed_provider(manifest) -> types.ReviewedProviderHandle
    admit_query(*, coordinator, report) -> None (raises to refuse)
    start_service(manifest, *, admit) -> object with stop()

An in-process caller passes the port as the use case's ``provider=`` (or
``port=``) argument. A process the test cannot hand an argument to (a hook,
a wake) finds it only while ``MEMVANE_TEST_RUNTIME_ROOT`` names an absolute
path holding an owner-only ``provider-fixture.json``: the port is then built
by ``tests.support.fake_provider.port_from_spec`` from that file. Without the
argument and that file every call returns None and production code runs.
"""
from __future__ import annotations

import json
import os
import stat

from memvane.provider.types import FIXTURE_FILE

_FIXTURE_LIMIT = 1024 * 1024


def resolve(port=None):
    """``port`` when given, else the test runtime root's fixture port, else None."""
    if port is not None:
        return port
    raw = os.environ.get('MEMVANE_TEST_RUNTIME_ROOT')
    if not raw or not os.path.isabs(raw):
        return None
    path = os.path.join(raw, FIXTURE_FILE)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_size > _FIXTURE_LIMIT):
            raise ValueError('provider_fixture_unsafe')
        raw_spec = os.read(fd, _FIXTURE_LIMIT + 1)
    finally:
        os.close(fd)
    from tests.support.fake_provider import port_from_spec
    return port_from_spec(json.loads(raw_spec.decode('utf-8')))

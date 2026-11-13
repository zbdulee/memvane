"""Owner-private cutover request and cooperative host/worker acknowledgements.

An acknowledgement identifies code that actually ran after the request. It
does not claim that an older process exited; the SQLite legacy fence provides
that independent protection.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import time
import uuid

from .history_migration import _regular_owner_file, verify_prepared
from .storage_layout import root as learning_root


SCHEMA = 'qmd-history-lifecycle-v1'
ACK_SCHEMA = 'qmd-history-host-ack-v1'
MAX_AGE = 3600


def _atomic(path, value):
    from .history_activation import _atomic_file
    _atomic_file(path, value)


def _read(path):
    if path.is_symlink():
        raise ValueError('history_lifecycle_file_unsafe')
    if not path.exists():
        return None
    path = _regular_owner_file(path)
    if path.stat().st_mode & 0o077:
        raise ValueError('history_lifecycle_file_unsafe')
    return json.loads(path.read_text())


def _request_path(home=None):
    return learning_root(home) / 'history-lifecycle-request.json'


def _used_path(home=None):
    return learning_root(home) / 'history-lifecycle-used.json'


def _used_receipt(*, home=None):
    value = _read(_used_path(home))
    if value is None:
        return None
    if (not isinstance(value, dict) or set(value) !=
            {'schema', 'generation', 'nonce', 'used_at'}
            or value['schema'] != SCHEMA
            or not isinstance(value['generation'], str)
            or not re.fullmatch(r'g-[0-9a-f]{32}', value['generation'])
            or not isinstance(value['nonce'], str)
            or not re.fullmatch(r'[0-9a-f]{32}', value['nonce'])
            or isinstance(value['used_at'], bool)
            or not isinstance(value['used_at'], (int, float))
            or not math.isfinite(value['used_at'])):
        raise ValueError('history_lifecycle_used_receipt_invalid')
    return value


def _ack_path(engine, plugin_root, *, home=None):
    token = hashlib.sha256(str(plugin_root).encode()).hexdigest()[:24]
    return learning_root(home) / f'history-host-ack-{engine}-{token}.json'


def runtime_fingerprint(plugin_root):
    from .history_activation import HOST_FILES
    root = Path(plugin_root)
    hasher = hashlib.sha256()
    for relative in HOST_FILES:
        path = root / relative
        if not path.is_file() or path.is_symlink():
            raise ValueError('history_host_file_missing')
        hasher.update(relative.encode() + b'\0')
        hasher.update(hashlib.sha256(path.read_bytes()).digest())
    return hasher.hexdigest()


def request(generation, *, home=None, now=None):
    """Create a fresh one-use request without selecting or modifying history."""
    from .history_activation import _read_pointer, _PLUGIN_ROOT
    if _read_pointer(home) is not None:
        raise ValueError('history_already_active')
    verified = verify_prepared(generation, home=home)
    if not verified['sources_current']:
        raise ValueError('history_sources_changed')
    manifest = _regular_owner_file(
        learning_root(home) / 'migrations' / generation / 'prepared.json')
    issued = time.time() if now is None else now
    value = {'schema': SCHEMA, 'generation': generation,
             'manifest_sha256': hashlib.sha256(manifest.read_bytes()).hexdigest(),
             'runtime_sha256': runtime_fingerprint(_PLUGIN_ROOT),
             'nonce': uuid.uuid4().hex, 'created_at': issued,
             'expires_at': issued + MAX_AGE}
    _atomic(_request_path(home), value)
    return value


def current(*, home=None, now=None):
    value = _read(_request_path(home))
    if value is None:
        return None
    instant = time.time() if now is None else now
    if (not isinstance(value, dict) or set(value) !=
            {'schema', 'generation', 'manifest_sha256', 'runtime_sha256',
             'nonce', 'created_at', 'expires_at'}
            or value['schema'] != SCHEMA
            or not isinstance(value['generation'], str)
            or not re.fullmatch(r'g-[0-9a-f]{32}', value['generation'])
            or any(not isinstance(value[key], str)
                   or not re.fullmatch(r'[0-9a-f]{64}', value[key])
                   for key in ('manifest_sha256', 'runtime_sha256'))
            or not isinstance(value['nonce'], str)
            or not re.fullmatch(r'[0-9a-f]{32}', value['nonce'])
            or not isinstance(value['created_at'], (int, float))
            or not isinstance(value['expires_at'], (int, float))
            or value['expires_at'] - value['created_at'] != MAX_AGE
            or instant < value['created_at'] or instant >= value['expires_at']):
        raise ValueError('history_lifecycle_request_invalid_or_expired')
    return value


def acknowledge_host(engine, plugin_root, *, home=None, now=None):
    """Called only by a real SessionStart adapter; silent if no request."""
    from .history_activation import HOSTS
    if engine not in HOSTS:
        return False
    value = current(home=home, now=now)
    if value is None:
        return False
    root = Path(plugin_root)
    account = Path(home or Path.home()).resolve()
    cache = account / ('.claude' if engine == 'claude' else '.codex') / 'plugins/cache'
    if (not root.is_absolute() or root.is_symlink()
            or any(part.is_symlink() for part in root.parents)):
        raise ValueError('history_host_ack_root_invalid')
    if cache not in root.parents:
        # Claude runs a directory-marketplace plugin in place; accept exactly
        # the folder its own install records name, nothing else.
        from .history_activation import _claude_load_roots
        try:
            loaded = engine == 'claude' and root in _claude_load_roots(account)
        except (OSError, KeyError, TypeError, ValueError, AttributeError) as exc:
            raise ValueError('history_host_ack_root_invalid') from exc
        if not loaded:
            raise ValueError('history_host_ack_root_invalid')
    fingerprint = runtime_fingerprint(root)
    if fingerprint != value['runtime_sha256']:
        raise ValueError('history_host_ack_runtime_mismatch')
    instant = time.time() if now is None else now
    receipt = {'schema': ACK_SCHEMA, 'generation': value['generation'],
               'nonce': value['nonce'], 'engine': engine,
               'plugin_root': str(root), 'runtime_sha256': fingerprint,
               'ack_at': instant}
    _atomic(_ack_path(engine, root, home=home), receipt)
    return True


def verify_acks(generation, host_roots, *, home=None):
    value = current(home=home)
    if value is None or value['generation'] != generation:
        raise ValueError('history_lifecycle_request_missing')
    used = _used_receipt(home=home)
    if used is not None and used['nonce'] == value['nonce']:
        raise ValueError('history_lifecycle_request_used')
    manifest = _regular_owner_file(
        learning_root(home) / 'migrations' / generation / 'prepared.json')
    if value['manifest_sha256'] != hashlib.sha256(manifest.read_bytes()).hexdigest():
        raise ValueError('history_lifecycle_manifest_changed')
    from .history_activation import HOSTS, _PLUGIN_ROOT
    if value['runtime_sha256'] != runtime_fingerprint(_PLUGIN_ROOT):
        raise ValueError('history_lifecycle_runtime_changed')
    for engine in HOSTS:
        roots = host_roots[engine]
        if not isinstance(roots, (tuple, list)):
            roots = [roots]
        for plugin_root in roots:
            root = Path(plugin_root)
            ack = _read(_ack_path(engine, root, home=home))
            if (not isinstance(ack, dict) or set(ack) !=
                    {'schema', 'generation', 'nonce', 'engine', 'plugin_root',
                     'runtime_sha256', 'ack_at'}
                    or ack['schema'] != ACK_SCHEMA
                    or ack['generation'] != generation or ack['nonce'] != value['nonce']
                    or ack['engine'] != engine or ack['plugin_root'] != str(root)
                    or ack['runtime_sha256'] != value['runtime_sha256']
                    or not isinstance(ack['ack_at'], (int, float))
                    or not value['created_at'] <= ack['ack_at'] < value['expires_at']):
                raise ValueError('history_host_ack_missing:' + engine)
    return value


def consume_request(generation, *, expected_nonce, home=None):
    value = current(home=home)
    if (value is None or value['generation'] != generation
            or value['nonce'] != expected_nonce):
        raise ValueError('history_lifecycle_request_missing')
    used = _used_receipt(home=home)
    if used is not None and used['nonce'] == value['nonce']:
        raise ValueError('history_lifecycle_request_used')
    _atomic(_used_path(home), {'schema': SCHEMA, 'generation': generation,
                               'nonce': value['nonce'], 'used_at': time.time()})


@contextmanager
def worker_lease(*, home=None):
    """A new detached worker holds this across its whole run."""
    from .history_activation import _owner_lock
    base = learning_root(home)
    if not base.is_dir():
        yield
        return
    fd = _owner_lock(base / 'history-worker.lock')
    try:
        fcntl.flock(fd, fcntl.LOCK_SH)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


@contextmanager
def pause_workers(*, home=None, timeout=5):
    """Wait for cooperative workers to finish before a pointer transition."""
    from .history_activation import _owner_lock
    fd = _owner_lock(learning_root(home) / 'history-worker.lock')
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ValueError('history_worker_busy')
                time.sleep(.02)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

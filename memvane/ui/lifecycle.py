"""The per-user singleton dashboard server: ``server.json``, ``server.lock``, start, replace, stop.

One server runs per ``$HOME`` (``<state base>/ui``). ``ensure_server`` takes
``server.lock``, reuses a live server whose ``instanceNonce`` answers on the
control endpoint, and replaces it only for a strictly newer bundle version or
a running bundle that is gone or damaged (the resident bundle rule: an equal
or older 1.x host reuses the running server). A new server starts detached
(``python3 -B``, ``DEVNULL``, ``close_fds``, new session) and writes
``server.json`` itself once it is listening.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import time

from memvane.kernel.memvane_version import version_key
from memvane.ui import local_client, platform_io
from memvane.ui.types import LOCK_FILE, SERVER_FILE, SERVER_KEYS, SERVER_SCHEMA

START_WAIT_SECONDS = 15.0
STOP_WAIT_SECONDS = 10.0


class LifecycleError(RuntimeError):
    """The server could not be started, reached or stopped; the message is a fixed code."""


def bundle_root():
    return Path(__file__).resolve().parents[2]


def bundle_version(root=None):
    """The ``plugin.json`` version of a bundle root, or None when unreadable."""
    path = Path(root if root is not None else bundle_root()) / 'plugin.json'
    try:
        with open(path, 'rb') as handle:
            value = json.loads(handle.read(65536).decode('utf-8')).get('version')
    except (OSError, ValueError, AttributeError):
        return None
    return value if isinstance(value, str) and version_key(value) is not None else None


def bundle_intact(root):
    """The bundle still holds its version, entry point and server module."""
    if not isinstance(root, str) or not os.path.isabs(root):
        return False
    base = Path(root)
    return (bundle_version(base) is not None and (base / 'entry' / 'run.py').is_file()
            and (base / 'memvane' / 'ui' / 'server.py').is_file())


def newer(candidate, running):
    """An equal version never replaces a server from another host cache."""
    left, right = version_key(candidate), version_key(running)
    return left is not None and (right is None or left > right)


def should_replace(candidate_version, server):
    return newer(candidate_version, server.get('bundleVersion')) or not bundle_intact(server.get('bundleRoot'))


def server_path(home=None, *, create=False):
    return platform_io.ui_dir(home, create=create) / SERVER_FILE


def lock_path(home=None):
    return platform_io.ui_dir(home, create=True) / LOCK_FILE


def valid_server(value):
    if not isinstance(value, dict) or set(value) != set(SERVER_KEYS) or value['schema'] != SERVER_SCHEMA:
        return False
    port, pid = value['port'], value['pid']
    return (isinstance(port, int) and not isinstance(port, bool) and 0 < port < 65536
            and isinstance(pid, int) and not isinstance(pid, bool) and pid > 1
            and all(isinstance(value[key], str) and value[key]
                    for key in ('bundleRoot', 'bundleVersion', 'startedAt', 'instanceNonce', 'controlSecret')))


def read_server(home=None):
    """The recorded server, or None when absent, unsafe or malformed."""
    try:
        value = platform_io.read_private_json(server_path(home))
    except (FileNotFoundError, platform_io.UiStateRefused):
        return None
    return value if valid_server(value) else None


def write_server(record, home=None):
    if not valid_server(record):
        raise LifecycleError('ui_server_record_invalid')
    platform_io.write_private_json(server_path(home, create=True), record)


def clear_server(home=None, nonce=None):
    """Remove ``server.json`` when it is still this server's (``nonce``) or when no nonce is given."""
    current = read_server(home)
    if nonce is not None and (current is None or current['instanceNonce'] != nonce):
        return False
    try:
        return platform_io.remove_file(server_path(home))
    except (FileNotFoundError, platform_io.UiStateRefused):
        return False


def probe(server, *, timeout=3.0):
    """The control status of a live recorded server whose nonce answers, or None."""
    if not valid_server(server) or not platform_io.pid_alive(server['pid']):
        return None
    try:
        status, value = local_client.control(server, 'GET', '/api/internal/status', timeout=timeout)
    except local_client.LocalClientError:
        return None
    if status != 200 or not isinstance(value, dict) or value.get('instanceNonce') != server['instanceNonce']:
        return None
    return value


def running(home=None):
    """``(server record, status)`` of the live server, or ``(None, None)``."""
    server = read_server(home)
    status = probe(server) if server is not None else None
    return (server, status) if status is not None else (None, None)


def wait_gone(pid, seconds):
    end = time.monotonic() + seconds
    while platform_io.pid_alive(pid):
        if time.monotonic() >= end:
            return False
        time.sleep(0.05)
    return True


def request_shutdown(server, reason):
    try:
        status, _value = local_client.control(server, 'POST', '/api/internal/shutdown', {'reason': reason})
    except local_client.LocalClientError:
        return False
    return status == 200


def _wait_started(home, previous_nonce, pid, seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        server = read_server(home)
        if server is not None and server['instanceNonce'] != previous_nonce and server['pid'] == pid:
            status = probe(server)
            if status is not None:
                return server, status
        if not platform_io.pid_alive(pid):
            break
        time.sleep(0.05)
    raise LifecycleError('ui_server_start_failed')


def serve_argv(*, port=0, read_only=False):
    argv = platform_io.python_argv(bundle_root() / 'entry' / 'run.py', 'ui', 'serve', '--port', port, '--spawned')
    if read_only:
        argv.append('--read-only')
    return argv


def ensure_server(home=None, *, port=0, read_only=False, spawn=None, wait_seconds=START_WAIT_SECONDS):
    """``{server, status, reused, replaced}``: a live server, started or replaced under ``server.lock``."""
    spawn = spawn or platform_io.spawn_detached
    candidate = bundle_version()
    if candidate is None:
        raise LifecycleError('ui_bundle_version_unreadable')
    with platform_io.server_lock(lock_path(home)):
        server, status = running(home)
        replaced = False
        if server is not None:
            if not should_replace(candidate, server):
                return {'server': server, 'status': status, 'reused': True, 'replaced': False}
            request_shutdown(server, 'bundle_upgraded')
            if not wait_gone(server['pid'], STOP_WAIT_SECONDS):
                if not platform_io.is_ui_server(server['pid'], server['bundleRoot']):
                    raise LifecycleError('ui_server_replace_failed')
                platform_io.terminate(server['pid'])
                if not wait_gone(server['pid'], STOP_WAIT_SECONDS):
                    raise LifecycleError('ui_server_replace_failed')
            clear_server(home, server['instanceNonce'])
            replaced = True
        previous = read_server(home)
        pid = spawn(serve_argv(port=port, read_only=read_only))
        server, status = _wait_started(home, previous['instanceNonce'] if previous else None, pid, wait_seconds)
        return {'server': server, 'status': status, 'reused': False, 'replaced': replaced}


def stop_server(home=None, *, reason='user_stop'):
    """``{stopped, how}``: graceful shutdown, else a verified termination of the recorded process."""
    server = read_server(home)
    if server is None:
        return {'stopped': False, 'how': 'not_running'}
    if probe(server) is not None and request_shutdown(server, reason) and wait_gone(server['pid'], STOP_WAIT_SECONDS):
        clear_server(home, server['instanceNonce'])
        return {'stopped': True, 'how': 'shutdown'}
    if not platform_io.pid_alive(server['pid']):
        clear_server(home, server['instanceNonce'])
        return {'stopped': False, 'how': 'stale_record_removed'}
    if platform_io.is_ui_server(server['pid'], server['bundleRoot']) and platform_io.terminate(server['pid']):
        wait_gone(server['pid'], STOP_WAIT_SECONDS)
        clear_server(home, server['instanceNonce'])
        return {'stopped': True, 'how': 'terminated'}
    return {'stopped': False, 'how': 'unverified_process'}


def idle_due(*, active_clients, grace_active, idle_seconds, idle_minutes):
    """No stream, no reconnect grace, and no request for ``idle_minutes``."""
    return active_clients == 0 and not grace_active and idle_seconds >= idle_minutes * 60

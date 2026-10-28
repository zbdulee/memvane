"""Move the learning state from the pre-rename support directory to MemVane's.

Learning state lives under the support directory
(``~/Library/Application Support/<name>``, ``~/.local/share/<name>``
elsewhere): ``live-selection`` (selector policy, checkpoint pointer, evidence
per project namespace) and ``laya-inference`` (the inference service, its
resource lanes, the runtime wake queue). An account that started before the
rename keeps both under the pre-rename support directory
(``memvane_paths.legacy_support_root``). This module moves them once to
``memvane`` and records the switch (``learning-state.json``) that
``runtime_setup.learning_state_base`` reads.

The move drains every holder first and never lets the two locations be used
at once:

* the per-user resident service hosts the inference service with a learning
  root fixed at its start: the mover holds the service's ``successor.lock``
  (no new service can be elected), asks a running one to drain, waits for its
  lease and then holds that lease too;
* every lane lock in ``laya-inference`` (provider, compute, trainer, runtime
  worker, inference slots, CPU shares, service start/lease) is taken
  non-blocking; a busy lane defers the move;
* both directories move with ``rename(2)``, so a lock opened before the move
  and one opened after it are the same inode; ``live-selection`` goes first,
  so ``learning_state_base`` follows ``laya-inference`` the moment it lands,
  and the switch file is written right after.

Only checkpoint pointers name a path inside ``live-selection``; they are
rebased after the rename. Prepared inference evidence binds the namespace
path and the checkpoint digest, so it no longer matches and the selector heal
(``selector_heal``) prepares it again. The learning runtime
(``runtimes/laya``, absolute interpreter paths) stays where it is and is
re-provisioned, never moved.
"""
from __future__ import annotations

from contextlib import ExitStack
import fcntl
import json
import os
from pathlib import Path
import stat
import time

NAMES = ('live-selection', 'laya-inference')
INTENT = 'learning-state-move.json'
LOCK = 'learning-state-move.lock'
CHECKPOINTS = ('active-checkpoint.json', 'experimental-checkpoint.json')
# A namespace bound by the synthetic release-QA setup journal pins its
# checkpoint bytes; such an account is not moved automatically.
SETUP_BINDINGS = 'synthetic-binding-init.json'
# How long the worker waits for a draining service to release its lease.
DRAIN_WAIT_SECONDS = 120


class LearningMoveDeferred(RuntimeError):
    pass


class LearningMoveRefused(RuntimeError):
    pass


def _fsync(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _dirs(home):
    from .runtime_setup import _support_dirs
    return _support_dirs(home)


def _private_dir(path, *, create=False):
    path = Path(path)
    if not path.exists() and not path.is_symlink():
        if not create:
            return None
        os.mkdir(path, 0o700)
        _fsync(path.parent)
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or
            stat.S_IMODE(info.st_mode) & 0o077):
        raise LearningMoveRefused('learning_state_dir_unsafe')
    return path


def _lock(path):
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
            info.st_mode & 0o077):
        os.close(fd)
        raise LearningMoveRefused('learning_lock_unsafe')
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


def _held(stack, fd):
    stack.callback(os.close, fd)
    return fd


def status(home=None):
    """('switched' | 'fresh' | 'pending', legacy dir, MemVane dir)."""
    from .runtime_setup import _state_switched
    current, legacy = _dirs(home)
    if _state_switched(current):
        return 'switched', legacy, current
    if os.path.lexists(current / INTENT) or any((legacy / name).is_dir() for name in NAMES):
        return 'pending', legacy, current
    return 'fresh', legacy, current


def _hold_service(stack, home, *, wait):
    """Drain the per-user resident service and hold its successor lock and lease."""
    import laya_resident_service as svc
    root = svc.service_dir(home=home)
    if root is None:
        return
    successor = _lock(root / svc.SUCCESSOR)
    if successor is None:
        raise LearningMoveDeferred('learning_service_starting')
    _held(stack, successor)
    if svc.hello(root) is not None:
        reply = svc.exchange(root, {'command': 'drain', 'protocols': list(svc.PROTOCOLS),
                                    'bundle': svc.bundle_identity(), 'python': None,
                                    'reason': 'learning_state_move'})
        if reply is None or reply.get('status') not in ('draining', 'stopped'):
            raise LearningMoveDeferred('learning_service_drain_refused')
    deadline = time.monotonic() + wait
    while True:
        lease = _lock(root / svc.LEASE)
        if lease is not None:
            _held(stack, lease)
            return
        if time.monotonic() >= deadline:
            raise LearningMoveDeferred('learning_service_draining')
        time.sleep(.2)


def _hold_lanes(stack, inference):
    """Every lane lock of the inference directory, or a deferral."""
    if inference is None or not inference.is_dir():
        return
    for name in sorted(os.listdir(inference)):
        if not name.endswith('.lock'):
            continue
        fd = _lock(inference / name)
        if fd is None:
            raise LearningMoveDeferred('learning_lane_busy')
        _held(stack, fd)


def _rebase_checkpoints(selection, old, new):
    """Point checkpoint pointers that named the old ``live-selection`` at the new one."""
    from runtime_lease import atomic_json
    from .dataset import read_private_json
    changed = 0
    if not selection.is_dir():
        return changed
    for namespace in sorted(selection.iterdir()):
        if not namespace.is_dir() or namespace.is_symlink():
            continue
        for name in CHECKPOINTS:
            path = namespace / name
            if not path.is_file() or path.is_symlink():
                continue
            value = read_private_json(path)
            artifact = value.get('artifact_path') if isinstance(value, dict) else None
            if isinstance(artifact, str) and artifact.startswith(str(old) + '/'):
                atomic_json(path, {**value,
                                   'artifact_path': str(new) + artifact[len(str(old)):]})
                changed += 1
    return changed


def _write_switch(current):
    from runtime_lease import atomic_json
    from .runtime_setup import STATE_SWITCH, STATE_SWITCH_SCHEMA
    atomic_json(current / STATE_SWITCH, {'schema': STATE_SWITCH_SCHEMA, 'root': str(current)})
    _fsync(current)


# Test seam: called with each phase name while the move holds its locks.
_step = None


def move(home=None, *, wait=DRAIN_WAIT_SECONDS):
    """Move the learning state once; returns a status row, raises a deferral."""
    if home is None and os.environ.get('QMD_SETUP_GUARD_FIXTURE') == '1':
        return {'status': 'learning_state_skipped_fixture'}
    state, legacy, current = status(home)
    if state != 'pending':
        return {'status': 'learning_state_' + state}
    mark = _step or (lambda _name: None)
    for namespace in ((legacy / 'live-selection').iterdir()
                      if (legacy / 'live-selection').is_dir() else ()):
        if (namespace / SETUP_BINDINGS).exists():
            raise LearningMoveRefused('learning_state_move_bound')
    _private_dir(current, create=True)
    if any((legacy / name).is_dir() for name in NAMES):
        if os.stat(legacy).st_dev != os.stat(current).st_dev:
            raise LearningMoveRefused('learning_state_cross_device')
    with ExitStack() as stack:
        fd = _lock(current / LOCK)
        if fd is None:
            raise LearningMoveDeferred('learning_state_move_busy')
        _held(stack, fd)
        _hold_service(stack, home, wait=wait)
        inference = next((base / 'laya-inference' for base in (legacy, current)
                          if (base / 'laya-inference').is_dir()), None)
        _hold_lanes(stack, inference)
        for name in NAMES:
            if (legacy / name).is_dir() and os.path.lexists(current / name):
                raise LearningMoveRefused('learning_state_target_exists')
        from runtime_lease import atomic_json
        atomic_json(current / INTENT, {'schema': 'memvane-learning-state-move-v1',
                                       'from': str(legacy), 'to': str(current)})
        _fsync(current)
        mark('prepared')
        for name in NAMES:
            if (legacy / name).is_dir() and not os.path.lexists(current / name):
                os.rename(legacy / name, current / name)
                _fsync(legacy)
                _fsync(current)
                mark('moved:' + name)
        _write_switch(current)
        mark('switched')
        rebased = _rebase_checkpoints(current / 'live-selection', legacy / 'live-selection',
                                      current / 'live-selection')
        os.unlink(current / INTENT)
        _fsync(current)
    return {'status': 'learning_state_moved', 'checkpointsRebased': rebased}


def current_root(passed, home=None):
    """The inference service root now, for a service spawned with ``passed``.

    A successor elected before a move starts after it with the old path in its
    argv; it serves the moved root instead (never recreating the old one).
    """
    try:
        from .inference_service import service_root
        now = service_root(create=False, home=home)
    except Exception:
        return passed
    return str(now) if now is not None and str(now) != passed else passed

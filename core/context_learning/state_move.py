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

The move quiesces every holder first and never lets the two locations be
used at once. The per-user resident service also hosts embedding search, so
it is drained only once nothing else can defer the move, and a deferral
before that point leaves it untouched:

* the runtime wake worker (``runtime-worker.lock``) is the only lane holder
  that keeps re-acquiring its lane and dispatches the sync wakes that start
  the resident service again; the mover holds ``runtime-worker-handoff.lock``
  and waits for ``runtime-worker.lock`` as a kernel waiter (the worker's
  own self-rewake loses to it), bounded by the attempt budget;
* every other lane lock in ``laya-inference`` (provider, compute, trainer,
  inference slots, CPU control, service start) is taken non-blocking, and a
  learning-only inference service is asked to shut down; a busy lane defers;
* only then the mover holds the resident service's ``successor.lock`` (no
  service can be elected), asks a running one to drain, waits for its lease
  and holds it, then takes the lanes the hosted inference held (service
  lease, CPU share, runtime monitor) as they free;
* a drained service is started again right after the attempt, moved or not,
  so embedding search is back within one cold load;
* a deferral backs off per user (``learning-state-move-retry.json``: 2, 4,
  8 ... minutes, at most an hour), so repeated worker runs never drain the
  service in a loop; one attempt spends at most ``BUDGET_SECONDS``;
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
# Wall budget of one attempt: the runtime worker's turn (its drain runs at
# most 60 s), the resident service's drain and the lanes it held.
BUDGET_SECONDS = 75
RETRY = 'learning-state-move-retry.json'
RETRY_SCHEMA = 'memvane-learning-state-move-retry-v1'
BACKOFF_BASE_SECONDS = 120
BACKOFF_CAP_SECONDS = 3600
WORKER = 'runtime-worker.lock'
HANDOFF = 'runtime-worker-handoff.lock'
# Lanes the resident service holds while it hosts learning inference (and a
# learning-only service of an earlier bundle while it runs): they free only
# once it stops.
SERVICE_LANES = ('service-lease.lock', 'runtime-monitor.lock')


class LearningMoveDeferred(RuntimeError):
    retry_in = None


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
    """('switched' | 'fresh' | 'pending', legacy dir, MemVane dir).

    A remaining intent is pending even after the switch: the checkpoint
    fix-up and the sentinels finish before the move counts as done.
    """
    from .runtime_setup import _state_switched
    current, legacy = _dirs(home)
    if os.path.lexists(current / INTENT):
        return 'pending', legacy, current
    if _state_switched(current):
        return 'switched', legacy, current
    if any((legacy / name).is_dir() for name in NAMES):
        return 'pending', legacy, current
    return 'fresh', legacy, current


SENTINEL_SCHEMA = 'memvane-learning-state-moved-v1'


def moved_sentinel(path):
    """True when ``path`` is the regular file a move left in place of a tree."""
    try:
        info = Path(path).lstat()
    except OSError:
        return False
    return stat.S_ISREG(info.st_mode)


# Trees a stale process recreated and a move set aside (for the event log).
STRAYS: list = []


def _set_aside(legacy, name, path):
    """Move a recreated, non-empty old tree aside, never one still in use.

    Every lock file in it is tried non-blocking: a held one means a process of
    an earlier bundle still runs a lane there, so the move stays pending and a
    later run retries instead of sealing over it.
    """
    for lock in sorted(Path(path).rglob('*.lock')):
        if lock.is_symlink() or not lock.is_file():
            continue
        fd = _lock(lock)
        if fd is None:
            raise LearningMoveDeferred('learning_state_stray_busy')
        os.close(fd)
    target = legacy / (name + '.stray-' + str(time.time_ns()))
    os.rename(path, target)
    _fsync(legacy)
    STRAYS.append(str(target))


def _seal_old(legacy, name, current):
    """Leave a read-only regular file where the moved tree was.

    A process of an earlier bundle that resolved the old base before the move
    then fails its ``mkdir -p`` or lock ``open`` (not a directory) instead of
    creating a second lane or selection tree there.
    """
    path = legacy / name
    body = (json.dumps({'schema': SENTINEL_SCHEMA, 'movedTo': str(current / name)},
                       sort_keys=True) + '\n').encode()
    for _attempt in range(3):
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                         getattr(os, 'O_NOFOLLOW', 0), 0o400)
        except FileExistsError:
            if moved_sentinel(path):
                return
            if path.is_dir() and not path.is_symlink():
                # Recreated by a stale process inside the rename window.
                try:
                    os.rmdir(path)
                except OSError:
                    _set_aside(legacy, name, path)
                continue
            raise LearningMoveRefused('learning_state_sentinel_blocked')
        try:
            os.write(fd, body)
            os.fsync(fd)
        finally:
            os.close(fd)
        _fsync(legacy)
        return
    raise LearningMoveRefused('learning_state_sentinel_blocked')


def _finish(legacy, current):
    """Idempotent tail of a move: checkpoints, sentinels, switch, intent.

    The checkpoint fix-up precedes the switch, and a remaining intent keeps
    the move pending (``status``), so a stop anywhere here resumes here.
    """
    rebased = _rebase_checkpoints(current / 'live-selection', legacy / 'live-selection',
                                  current / 'live-selection')
    for name in NAMES:
        if os.path.lexists(current / name):
            _seal_old(legacy, name, current)
    _write_switch(current)
    (_step or (lambda _name: None))('switched')
    if os.path.lexists(current / INTENT):
        os.unlink(current / INTENT)
        _fsync(current)
    return rebased


def _open_lock(path):
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
            info.st_mode & 0o077):
        os.close(fd)
        raise LearningMoveRefused('learning_lock_unsafe')
    return fd


class _Expired(Exception):
    pass


def _flock_until(fd, deadline):
    """Wait as a kernel waiter for ``fd``'s lock until ``deadline``; True when held.

    A blocked waiter is handed the lock as its holder releases it, ahead of a
    holder that re-acquires it non-blocking right after (the runtime worker's
    self-rewake). The wait is bounded by ``ITIMER_REAL``; an outer alarm (the
    bundle upgrade deadline) is restored and still fires. Off the main thread
    the wait polls instead.
    """
    import signal
    import threading
    left = deadline - time.monotonic()
    if left <= 0:
        return False
    if threading.current_thread() is not threading.main_thread():
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    return False
                time.sleep(.002)
    prior_delay, prior_interval = signal.getitimer(signal.ITIMER_REAL)
    if prior_delay:
        left = min(left, prior_delay)

    def expired(_signum, _frame):
        raise _Expired()
    prior = signal.signal(signal.SIGALRM, expired)
    started = time.monotonic()
    held = False
    try:
        try:
            signal.setitimer(signal.ITIMER_REAL, left)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                held = True
            finally:
                signal.setitimer(signal.ITIMER_REAL, 0)
        except _Expired:
            pass
    finally:
        signal.signal(signal.SIGALRM, prior)
        if prior_delay:
            signal.setitimer(signal.ITIMER_REAL,
                             max(.001, prior_delay - (time.monotonic() - started)),
                             prior_interval)
    return held


def _wait_lock(path, deadline):
    fd = _lock(path)
    if fd is not None:
        return fd
    fd = _open_lock(path)
    try:
        if _flock_until(fd, deadline):
            return fd
    except BaseException:
        os.close(fd)
        raise
    os.close(fd)
    return None


def _service_lane(name):
    return name in SERVICE_LANES or (name.startswith('cpu-share-') and name.endswith('.lock'))


def _stop_learning_only(inference):
    """Ask a learning-only inference service (not hosted) to shut down.

    It stops only while idle; a busy one defers the move. A hosted service
    (inside the resident service) is left to the resident drain.
    """
    try:
        from . import inference_service as learning
        answer = learning._version(inference, time.monotonic() + .5)
    except Exception:
        return
    if not isinstance(answer, dict) or answer.get('hosted') is not False:
        return
    try:
        reply = learning._exchange(inference, {'command': 'shutdown'}, time.monotonic() + .5)
    except Exception:
        reply = None
    if not isinstance(reply, dict) or reply.get('status') != 'stopped':
        raise LearningMoveDeferred('learning_inference_busy')


def _quiesce_lanes(stack, inference, deadline):
    """Every lane but the service's own, or a deferral; the resident is untouched.

    Returns the names held. The runtime worker's handoff lock first (no
    second worker can line up), then its lease as a kernel waiter, then the
    other lanes non-blocking.
    """
    held = set()
    if inference is None or not inference.is_dir():
        return held
    fd = _lock(inference / HANDOFF)
    if fd is None:
        raise LearningMoveDeferred('learning_runtime_worker_busy')
    _held(stack, fd)
    held.add(HANDOFF)
    fd = _wait_lock(inference / WORKER, deadline)
    if fd is None:
        raise LearningMoveDeferred('learning_runtime_worker_busy')
    _held(stack, fd)
    held.add(WORKER)
    for name in sorted(os.listdir(inference)):
        if not name.endswith('.lock') or name in held or _service_lane(name):
            continue
        fd = _lock(inference / name)
        if fd is None:
            raise LearningMoveDeferred('learning_lane_busy')
        _held(stack, fd)
        held.add(name)
    _stop_learning_only(inference)
    return held


def _hold_service(stack, home, deadline, drained):
    """Drain the per-user resident service and hold its successor lock and lease.

    ``drained['service']`` is set once a drain was requested, so the caller
    starts the service again whatever this attempt's outcome.
    """
    import laya_resident_service as svc
    root = svc.service_dir(home=home)
    if root is None:
        return
    successor = _lock(root / svc.SUCCESSOR)
    if successor is None:
        raise LearningMoveDeferred('learning_service_starting')
    _held(stack, successor)
    if svc.hello(root) is not None:
        drained['service'] = True
        reply = svc.exchange(root, {'command': 'drain', 'protocols': list(svc.PROTOCOLS),
                                    'bundle': svc.bundle_identity(), 'python': None,
                                    'reason': 'learning_state_move'})
        if reply is None or reply.get('status') not in ('draining', 'stopped'):
            raise LearningMoveDeferred('learning_service_drain_refused')
    while True:
        lease = _lock(root / svc.LEASE)
        if lease is not None:
            _held(stack, lease)
            return
        if time.monotonic() >= deadline:
            raise LearningMoveDeferred('learning_service_draining')
        time.sleep(.05)


def _hold_lanes(stack, inference, held, deadline):
    """The lanes left (those a stopping service held) as they free, or a deferral."""
    if inference is None or not inference.is_dir():
        return
    for name in sorted(os.listdir(inference)):
        if not name.endswith('.lock') or name in held:
            continue
        while True:
            fd = _lock(inference / name)
            if fd is not None:
                break
            if time.monotonic() >= deadline:
                raise LearningMoveDeferred('learning_lane_busy')
            time.sleep(.05)
        _held(stack, fd)
        held.add(name)


def _now_ns():
    return time.time_ns()


def _read_retry(current):
    path = Path(current) / RETRY
    try:
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
                info.st_mode & 0o077 or info.st_size > 4096):
            return None
        value = json.loads(path.read_text('utf-8'))
    except (OSError, ValueError):
        return None
    if (not isinstance(value, dict) or value.get('schema') != RETRY_SCHEMA or
            type(value.get('deferrals')) is not int or type(value.get('nextNs')) is not int):
        return None
    return value


def _backoff_due(current):
    """Raise the backoff deferral while the last deferral's wait is still running."""
    value = _read_retry(current)
    if value is None:
        return
    left = (value['nextNs'] - _now_ns()) / 1e9
    if left > 0:
        exc = LearningMoveDeferred('learning_state_move_backoff')
        exc.retry_in = round(left, 1)
        raise exc


def _record_deferral(current, exc):
    """Exponential per-user backoff after a stopped attempt (under the move lock)."""
    from runtime_lease import atomic_json
    prior = _read_retry(current)
    deferrals = (prior['deferrals'] if prior is not None else 0) + 1
    wait = min(BACKOFF_CAP_SECONDS, BACKOFF_BASE_SECONDS * 2 ** min(deferrals - 1, 16))
    try:
        atomic_json(Path(current) / RETRY, {'schema': RETRY_SCHEMA, 'deferrals': deferrals,
                                            'nextNs': _now_ns() + wait * 10**9,
                                            'reason': str(exc)[:80]})
    except OSError:
        return
    try:
        exc.retry_in = wait
    except AttributeError:
        pass


def _clear_retry(current):
    try:
        os.unlink(Path(current) / RETRY)
    except FileNotFoundError:
        pass


def _restart_service(home):
    """Start the drained resident service again; embedding search comes back.

    Only for the account's own service (``home`` None) and never in a fixture;
    tests replace this seam.
    """
    if (home is not None or os.environ.get('QMD_SETUP_GUARD_FIXTURE') == '1' or
            os.environ.get('QMD_SANDBOX')):
        return None
    try:
        import laya_resident_service as svc
        return svc.ensure_service()
    except Exception:
        return None


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


def move(home=None, *, wait=None):
    """Move the learning state once; returns a status row, raises a deferral.

    ``wait`` bounds the attempt (``BUDGET_SECONDS`` by default). A deferral
    records the per-user backoff; while it runs, the next call defers at once
    (``learning_state_move_backoff``) without taking any lock.
    """
    if home is None and os.environ.get('QMD_SETUP_GUARD_FIXTURE') == '1':
        return {'status': 'learning_state_skipped_fixture'}
    state, legacy, current = status(home)
    if state != 'pending':
        return {'status': 'learning_state_' + state}
    for namespace in ((legacy / 'live-selection').iterdir()
                      if (legacy / 'live-selection').is_dir() else ()):
        if (namespace / SETUP_BINDINGS).exists():
            raise LearningMoveRefused('learning_state_move_bound')
    _private_dir(current, create=True)
    if any((legacy / name).is_dir() for name in NAMES):
        if os.stat(legacy).st_dev != os.stat(current).st_dev:
            raise LearningMoveRefused('learning_state_cross_device')
    _backoff_due(current)
    deadline = time.monotonic() + (BUDGET_SECONDS if wait is None else wait)
    drained = {'service': False}
    try:
        with ExitStack() as stack:
            fd = _lock(current / LOCK)
            if fd is None:
                raise LearningMoveDeferred('learning_state_move_busy')
            _held(stack, fd)
            try:
                result = _move_locked(stack, home, legacy, current, deadline, drained)
            except Exception as exc:
                # Any stop after the move lock (a busy lane, a service that
                # did not drain, a control socket that failed) backs off.
                _record_deferral(current, exc)
                raise
            _clear_retry(current)
        return result
    finally:
        if drained['service']:
            _restart_service(home)


def _move_locked(stack, home, legacy, current, deadline, drained):
    mark = _step or (lambda _name: None)
    del STRAYS[:]
    if os.path.lexists(current / INTENT):
        # A run stopped between a rename and its sentinel: an old tree a
        # stale process recreated there is sealed (or set aside) first.
        for name in NAMES:
            if os.path.lexists(current / name) and (legacy / name).is_dir():
                _seal_old(legacy, name, current)
    if not any((legacy / name).is_dir() for name in NAMES):
        # Both trees already moved (a run stopped after the renames):
        # only the idempotent tail is left, no drain needed.
        return {'status': 'learning_state_moved', 'checkpointsRebased':
                _finish(legacy, current), 'strays': list(STRAYS)}
    inference = next((base / 'laya-inference' for base in (legacy, current)
                      if (base / 'laya-inference').is_dir()), None)
    # Everything that can defer the move comes before the drain.
    held = _quiesce_lanes(stack, inference, deadline)
    for name in NAMES:
        if (legacy / name).is_dir() and os.path.lexists(current / name):
            raise LearningMoveRefused('learning_state_target_exists')
    _hold_service(stack, home, deadline, drained)
    _hold_lanes(stack, inference, held, deadline)
    from runtime_lease import atomic_json
    atomic_json(current / INTENT, {'schema': 'memvane-learning-state-move-v1',
                                   'from': str(legacy), 'to': str(current)})
    _fsync(current)
    mark('prepared')
    for name in NAMES:
        if (legacy / name).is_dir() and not os.path.lexists(current / name):
            os.rename(legacy / name, current / name)
            mark('renamed:' + name)
            _seal_old(legacy, name, current)
            _fsync(current)
            mark('moved:' + name)
    mark('renamed')
    rebased = _finish(legacy, current)
    return {'status': 'learning_state_moved', 'checkpointsRebased': rebased,
            'strays': list(STRAYS)}


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

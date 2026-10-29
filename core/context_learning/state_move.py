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
  the resident service again; the mover first asks workers to yield
  (``runtime-worker-yield.json``: a draining worker starts no further child
  and none is spawned while it stands), then waits as a kernel waiter for
  ``runtime-worker-handoff.lock`` (a handoff contender releases it as soon as
  it holds the lease) and for ``runtime-worker.lock`` (the worker's own
  self-rewake loses to it), all bounded by the attempt budget; only a child
  already running when the request is written is waited for;
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
import sys
import time

NAMES = ('live-selection', 'laya-inference')
INTENT = 'learning-state-move.json'
LOCK = 'learning-state-move.lock'
CHECKPOINTS = ('active-checkpoint.json', 'experimental-checkpoint.json')
# A namespace bound by the synthetic release-QA setup journal pins its
# checkpoint bytes; such an account is not moved automatically.
SETUP_BINDINGS = 'synthetic-binding-init.json'
# Bound on the waits of one attempt: the runtime worker's turn (after the
# yield request it finishes at most the child it is running), the resident
# service's drain and the lanes it held.
# The rename and checkpoint rebase after them are local filesystem work; the
# restart of a drained service adds at most RESTART_WAIT_SECONDS.
BUDGET_SECONDS = 75
# How long the attempt itself waits for a drained service to answer again;
# past it a detached restarter keeps trying for RESTARTER_SECONDS.
RESTART_WAIT_SECONDS = 10
RESTARTER_SECONDS = 900
# A drain is never started with less of the budget left than this: a drain
# cannot be taken back, so one that could not finish would cost search
# without moving anything.
DRAIN_RESERVE_SECONDS = 15
RETRY = 'learning-state-move-retry.json'
RETRY_SCHEMA = 'memvane-learning-state-move-retry-v1'
BACKOFF_BASE_SECONDS = 120
BACKOFF_CAP_SECONDS = 3600
WORKER = 'runtime-worker.lock'
HANDOFF = 'runtime-worker-handoff.lock'
# Read by runtime_work.yield_requested (same name, same {"until": epoch}).
YIELD = 'runtime-worker-yield.json'
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


def _resident_pid(home):
    """PID the running resident service reports, or None when none answers."""
    try:
        import laya_resident_service as svc
        root = svc.service_dir(home=home)
        answer = svc.hello(root) if root is not None else None
    except Exception:
        return None
    pid = answer.get('pid') if isinstance(answer, dict) else None
    return pid if type(pid) is int and pid > 1 else None


def _learning_pid(inference):
    try:
        info = (inference / 'service.pid.json').lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
                info.st_size > 512):
            return None
        pid = json.loads((inference / 'service.pid.json').read_text('utf-8')).get('pid')
    except (OSError, ValueError, AttributeError):
        return None
    return pid if type(pid) is int and pid > 1 else None


def _stop_learning_only(inference, home):
    """Settle who serves learning inference before any drain.

    ``absent`` (lease free, nothing answers) and ``hosted`` (the resident service's
    own, left to its drain) pass. A learning-only service of an earlier bundle
    is asked to shut down and must answer ``stopped``. Anything else (a socket
    or lease whose holder does not answer, a malformed answer) is unknown and
    defers the move before the shared resident service is drained.
    """
    from . import inference_service as learning
    try:
        answer = learning._version(inference, time.monotonic() + .5)
    except Exception:
        answer = False  # answered, but not a valid version reply
    if isinstance(answer, dict) and answer.get('hosted') is True:
        return 'hosted'
    if isinstance(answer, dict) and answer.get('hosted') is False:
        try:
            reply = learning._exchange(inference, {'command': 'shutdown'},
                                       time.monotonic() + .5)
        except Exception:
            reply = None
        if not isinstance(reply, dict) or reply.get('status') != 'stopped':
            raise LearningMoveDeferred('learning_inference_busy')
        return 'stopped'
    fd = _lock(inference / learning.LEASE_NAME)
    if fd is not None:
        os.close(fd)
        if answer is None:
            return 'absent'  # nobody holds the lease; a socket left is stale
    # A lease or socket without a valid answer: hosted only when the PID it
    # recorded is the resident service's own.
    resident = _resident_pid(home)
    if resident is not None and _learning_pid(inference) == resident:
        return 'hosted'
    raise LearningMoveDeferred('learning_inference_unknown')


def _request_yield(inference, deadline):
    """Ask runtime workers to step aside until ``deadline`` (best effort)."""
    from runtime_lease import atomic_json
    try:
        atomic_json(inference / YIELD,
                    {'until': time.time() + max(0.0, deadline - time.monotonic())})
    except (OSError, ValueError):
        pass


def _clear_yield(inference):
    try:
        (inference / YIELD).unlink()
    except OSError:
        pass


def _quiesce_lanes(stack, inference, deadline, home=None):
    """Every lane but the service's own, or a deferral; the resident is untouched.

    Returns the names held. A yield request first, then the runtime worker's
    handoff lock (no second worker can line up) and its lease, both as a
    kernel waiter within the budget, then the other lanes non-blocking.
    """
    held = set()
    if inference is None or not inference.is_dir():
        return held
    _request_yield(inference, deadline)
    try:
        fd = _wait_lock(inference / HANDOFF, deadline)
        if fd is None:
            raise LearningMoveDeferred('learning_runtime_worker_busy')
        _held(stack, fd)
        held.add(HANDOFF)
        fd = _wait_lock(inference / WORKER, deadline)
        if fd is None:
            raise LearningMoveDeferred('learning_runtime_worker_busy')
        _held(stack, fd)
        held.add(WORKER)
    finally:
        # Held or deferred, the request ends here: holding the lease already
        # keeps every worker out, and nothing may follow the directory move.
        _clear_yield(inference)
    for name in sorted(os.listdir(inference)):
        if not name.endswith('.lock') or name in held or _service_lane(name):
            continue
        fd = _lock(inference / name)
        if fd is None:
            raise LearningMoveDeferred('learning_lane_busy')
        _held(stack, fd)
        held.add(name)
    _stop_learning_only(inference, home)
    return held


def _hold_service(stack, home, deadline, drained, reserve):
    """Drain the per-user resident service and hold its successor lock and lease.

    A service serving a request, or too little budget left for its drain,
    defers before the drain. ``drained['service']`` is set once a drain was
    requested, so the caller starts the service again whatever this
    attempt's outcome.
    """
    import laya_resident_service as svc
    root = svc.service_dir(home=home)
    if root is None:
        return
    successor = _lock(root / svc.SUCCESSOR)
    if successor is None:
        raise LearningMoveDeferred('learning_service_starting')
    _held(stack, successor)
    answer = svc.hello(root)
    if answer is not None:
        if answer.get('inflight'):
            raise LearningMoveDeferred('learning_service_busy')
        if deadline - time.monotonic() < reserve:
            raise LearningMoveDeferred('learning_service_budget')
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


# Test seams: the resident service spawn handed to ``ensure_service`` (a
# synthetic home restarts only through it) and how a restarter outlives the
# attempt (``_spawn_restarter`` by default).
_service_spawn = None
_detach = None


def _emit(events, outcome, reason=None, **fields):
    if events is None:
        return
    try:
        import event_log
        event_log.emit('resident.restart', outcome=outcome, reason=reason,
                       state_root=Path(events), host='worker', **fields)
    except Exception:
        pass


def _serving(svc, root):
    try:
        answer = svc.hello(root)
    except Exception:
        return False
    return isinstance(answer, dict) and answer.get('status') != 'draining'


def _restart_until(home, deadline, *, step=.1, cap=.1):
    """``ensure_service`` until the service answers (not draining) or ``deadline``.

    A service that closed its control socket but still holds its lease gets
    no successor from one call (``starting`` without a spawn), so the call is
    repeated: once the lease is free (or the old one is draining) it elects
    exactly one successor (``successor.lock``), later calls see it waiting.
    """
    import laya_resident_service as svc
    try:
        root = svc.service_dir(create=True, home=home)
        learning_root = None
        if home is not None:
            from .inference_service import service_root
            learning_root = service_root(create=True, home=home)
    except Exception:
        return False
    while True:
        if _serving(svc, root):
            return True
        try:
            if home is None:
                svc.ensure_service(spawn=_service_spawn)
            else:
                svc.ensure_service(root=root, learning_root=learning_root,
                                   spawn=_service_spawn)
        except Exception:
            pass
        if _serving(svc, root):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(min(step, max(0, deadline - time.monotonic())))
        step = min(cap, step * 2)


def restarter(home=None, events=None, seconds=RESTARTER_SECONDS):
    """Detached: keep electing a successor until the service answers."""
    started = time.monotonic()
    done = _restart_until(home, started + seconds, step=1, cap=30)
    _emit(events, 'restarted' if done else 'failed', None if done else 'restart_timeout',
          detached=True, ms=(time.monotonic() - started) * 1000)
    return done


def _spawn_restarter(home, events):
    core = str(Path(__file__).resolve().parents[1])
    bootstrap = ('import sys;sys.path.insert(0,' + repr(core) + ');'
                 'from context_learning import state_move;'
                 'state_move.restarter(None,sys.argv[1] or None)')
    env = {key: os.environ[key] for key in ('PATH', 'HOME', 'LANG', 'TMPDIR', 'USER')
           if key in os.environ}
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    import subprocess
    subprocess.Popen([sys.executable, '-B', '-c', bootstrap, str(events or '')],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, close_fds=True,
                     start_new_session=True, env=env, cwd='/')


def _restart_service(home, events=None):
    """Bring the drained resident service back; embedding search returns.

    Waits up to ``RESTART_WAIT_SECONDS`` for it to answer, then leaves a
    detached restarter. The account's own service (``home`` None) never
    restarts in a fixture; a synthetic home restarts only through an
    injected spawn.
    """
    if home is None and (os.environ.get('QMD_SETUP_GUARD_FIXTURE') == '1' or
                         os.environ.get('QMD_SANDBOX')):
        return 'skipped_fixture'
    if home is not None and _service_spawn is None:
        return 'skipped_fixture'
    started = time.monotonic()
    if _restart_until(home, started + RESTART_WAIT_SECONDS):
        _emit(events, 'restarted', ms=(time.monotonic() - started) * 1000)
        return 'restarted'
    try:
        (_detach or _spawn_restarter)(home, events)
    except Exception as exc:
        _emit(events, 'failed', type(exc).__name__)
        return 'failed'
    _emit(events, 'restarter_started', ms=(time.monotonic() - started) * 1000)
    return 'restarter_started'


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


def move(home=None, *, wait=None, events=None):
    """Move the learning state once; returns a status row, raises a deferral.

    ``wait`` bounds the attempt's waits (``BUDGET_SECONDS`` by default).
    ``events`` is the state root whose event log records a restart. A deferral
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
    if not os.path.lexists(current / INTENT):
        # A started move (intent present) resumes at once: its old trees
        # may already be sealed.
        _backoff_due(current)
    budget = BUDGET_SECONDS if wait is None else wait
    deadline = time.monotonic() + budget
    reserve = min(DRAIN_RESERVE_SECONDS, budget / 2)
    drained = {'service': False}
    result = failure = None
    try:
        with ExitStack() as stack:
            fd = _lock(current / LOCK)
            if fd is None:
                raise LearningMoveDeferred('learning_state_move_busy')
            _held(stack, fd)
            try:
                result = _move_locked(stack, home, legacy, current, deadline, drained, reserve)
            except Exception as exc:
                # Any stop after the move lock (a busy lane, a service that
                # did not drain, a control socket that failed) backs off.
                _record_deferral(current, exc)
                raise
            _clear_retry(current)
        return result
    except BaseException as exc:
        failure = exc
        raise
    finally:
        if drained['service']:
            outcome = _restart_service(home, events)
            if failure is not None:
                try:
                    failure.restart = outcome
                except AttributeError:
                    pass
            elif isinstance(result, dict):
                result['residentRestart'] = outcome


def _move_locked(stack, home, legacy, current, deadline, drained, reserve):
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
    held = _quiesce_lanes(stack, inference, deadline, home)
    for name in NAMES:
        if (legacy / name).is_dir() and os.path.lexists(current / name):
            raise LearningMoveRefused('learning_state_target_exists')
    _hold_service(stack, home, deadline, drained, reserve)
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

"""One plugin-owned inference process per OS user; never a training transport."""
from __future__ import annotations

import hashlib
import math
import os
from pathlib import Path
import resource
import sys
import time

from . import resident


class ServiceFailure(ValueError):
    def __init__(self, code, timing=None):
        super().__init__('resident_service_' + code)
        self.code = code
        self.timing = timing or {}


def service_root(*, create=False):
    home = Path.home().resolve()
    parts = ('Library', 'Application Support') if sys.platform == 'darwin' else ('.local', 'share')
    root = home.joinpath(*parts, 'qmd-auto-context', 'laya-inference')
    for part in (root, *root.parents):
        if part == home:
            break
        if part.is_symlink():
            raise ServiceFailure('unsafe_root')
        if part.exists():
            info = part.stat()
            if not part.is_dir() or info.st_uid != os.getuid() or info.st_mode & 0o022:
                raise ServiceFailure('unsafe_root')
    if create:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return resident._private_state(root)


def namespace(state):
    state = Path(state)
    if any(part.is_symlink() for part in (state, *state.parents)):
        raise ServiceFailure('unsafe_namespace')
    state = resident._private_state(state)
    if state != state.resolve():
        raise ServiceFailure('unsafe_namespace')
    return hashlib.sha256(str(state).encode()).hexdigest()


def _rss_mib():
    # Portable conservative high-water mark, not another process's identity.
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak / (1024 * 1024 if sys.platform == 'darwin' else 1024)


def _load(job):
    from .laya_adapter import _head_path, _load as load
    head = _head_path(job['request']['artifact_path'], job['family'][1], job['family'][2])
    return load(job['model_dir'], head_artifact=head, input_view=job['family'][2])


def _forward(loaded, job, *, limits, timing, cancelled):
    from .laya_adapter import predict_loaded
    return predict_loaded(*loaded, job['request'], **limits,
                          timing=timing, cancelled=cancelled)


class GenerationEngine:
    """Serial owner: candidate object cannot mutate the incumbent reference."""
    def __init__(self, *, load=_load, forward=_forward,
                 admission=resident._admission, rss=_rss_mib):
        self.load, self.forward, self.admission, self.rss = load, forward, admission, rss
        self.active = None
        self.footprint_mib = None

    def run(self, job, *, cancelled, revalidate):
        def check():
            if cancelled() or time.monotonic() >= job['deadline']:
                raise ServiceFailure('deadline_expired')
        check()
        limits, report = self.admission(job['policy']['maxRssMib'],
                                         already_running=self.active is not None)
        if limits is None:
            raise ServiceFailure('resource_deferred')
        replacement = self.active is not None and self.active[0] != job['generation']
        if replacement and self.active[0][:3] != job['family']:
            raise ServiceFailure('model_family_mismatch')
        timing = {}
        pending = None
        if self.active is None or replacement:
            before = self.rss()
            if replacement:
                overlap = self.footprint_mib
                available = report.get('available_mib')
                if (overlap is None or type(available) not in (int, float)
                        or not math.isfinite(available) or available < overlap + 768
                        or before + overlap > job['policy']['maxRssMib']):
                    raise ServiceFailure('replacement_deferred')
            started = time.perf_counter()
            try:
                candidate = self.load(job)
            except Exception as exc:
                raise ServiceFailure('replacement_failed' if replacement else 'load_failed') from exc
            timing['model_load_seconds'] = round(time.perf_counter() - started, 6)
            check()
            fresh = revalidate()
            if (fresh['generation'] != job['generation'] or fresh['namespace'] != job['namespace']
                    or fresh['policy'] != job['policy']):
                raise ServiceFailure('request_changed')
            check()
            after = self.rss()
            if not math.isfinite(after) or after > job['policy']['maxRssMib']:
                raise ServiceFailure('replacement_deferred' if replacement else 'resource_deferred')
            footprint = max(256, math.ceil(max(0, after-before) * 1.5))
            pending = (job['generation'], candidate)
            footprint = max(self.footprint_mib or 0, footprint)
        else:
            timing['model_load_seconds'] = 0.0
            if self.rss() > job['policy']['maxRssMib']:
                raise ServiceFailure('resource_deferred')
        check()
        loaded = pending[1] if pending is not None else self.active[1]
        result = self.forward(loaded, job, limits=limits, timing=timing,
                              cancelled=lambda: cancelled() or time.monotonic() >= job['deadline'])
        check()
        fresh = revalidate()
        if (fresh['generation'] != job['generation'] or fresh['namespace'] != job['namespace']
                or fresh['policy'] != job['policy']):
            raise ServiceFailure('request_changed')
        check()
        if self.rss() > job['policy']['maxRssMib']:
            raise ServiceFailure('replacement_deferred' if replacement else 'resource_deferred')
        if pending is not None:
            self.active = pending
            self.footprint_mib = footprint
        return result, timing

# Protocol is independent of host cache paths. Different code never silently
# shuts down or replaces a running user's service.
import fcntl
from functools import lru_cache
import json
import queue
import socket
import stat
import subprocess
import threading

PROTOCOL = 1
CAPACITY = 8
SOCKET_NAME = 'service.sock'
PID_NAME = 'service.pid.json'
LEASE_NAME = 'service-lease.lock'
_CODES = {'queue_full', 'deadline_expired', 'resource_deferred', 'replacement_deferred',
          'replacement_failed', 'load_failed', 'model_family_mismatch',
          'request_changed', 'request_rejected', 'inference_attestation_required',
          'worker_error', 'version_mismatch', 'busy', 'starting'}


@lru_cache(maxsize=1)
def _signature():
    digest = hashlib.sha256()
    for path in sorted(Path(__file__).parent.glob('*.py')):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _lock(path):
    fd = resident._private_lock(path)
    if os.fstat(fd).st_nlink != 1:
        os.close(fd)
        raise ServiceFailure('unsafe_lock')
    return fd


def _address(root):
    path = root / SOCKET_NAME
    if len(os.fsencode(path)) >= 104:
        raise ServiceFailure('socket_path_too_long')
    return path


def _exchange(root, envelope, deadline):
    path = _address(root)
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077):
            raise ServiceFailure('unsafe_socket')
    with socket.socket(socket.AF_UNIX) as conn:
        left = deadline - time.monotonic()
        if left <= 0:
            raise ServiceFailure('deadline_expired')
        conn.settimeout(left)
        conn.connect(str(path))
        if not resident._same_owner(conn):
            raise ServiceFailure('request_rejected')
        resident._write(conn, envelope)
        return resident._read(conn, deadline=deadline)


def _ready(root, deadline):
    try:
        answer = _exchange(root, {'command': 'version'}, min(deadline, time.monotonic()+.5))
    except (FileNotFoundError, ConnectionRefusedError, socket.timeout):
        return False
    if (not isinstance(answer, dict) or answer.get('protocol') != PROTOCOL
            or answer.get('signature') != _signature()):
        raise ServiceFailure('version_mismatch')
    return answer.get('status') == 'ready'


def _ensure(root, python, deadline, *, spawn=subprocess.Popen):
    """No signaling: a held kernel lease forbids stale cleanup or respawn."""
    root = resident._private_state(root)
    fd = _lock(root / 'service-start.lock')
    try:
        while True:
            if time.monotonic() >= deadline:
                raise ServiceFailure('starting')
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                time.sleep(.01)
        if _ready(root, deadline):
            return
        lease = _lock(root / LEASE_NAME)
        try:
            while True:
                try:
                    fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if _ready(root, deadline):
                        return
                    if time.monotonic() >= deadline:
                        raise ServiceFailure('starting')
                    time.sleep(.02)
            path = _address(root)
            # _ready has already checked a surviving socket's file type/owner.
            if path.exists():
                path.unlink()
            pid = root / PID_NAME
            if pid.exists() or pid.is_symlink():
                resident._safe_file(pid, max_size=512)
                pid.unlink()
        finally:
            os.close(lease)
        env = {key: os.environ[key] for key in ('PATH', 'LANG', 'TMPDIR', 'HOME') if key in os.environ}
        env.update(PYTHONPATH=str(Path(__file__).resolve().parents[1]),
                   PYTHONDONTWRITEBYTECODE='1', HF_HUB_OFFLINE='1',
                   TRANSFORMERS_OFFLINE='1', HF_DATASETS_OFFLINE='1')
        spawn([python, '-m', 'context_learning.inference_service', 'worker', str(root)],
              env=env, cwd=root, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
              stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True)
        while time.monotonic() < deadline:
            if _ready(root, deadline):
                return
            time.sleep(.02)
        raise ServiceFailure('starting')
    finally:
        os.close(fd)


def _failure(exc):
    if isinstance(exc, ServiceFailure) and exc.code in _CODES:
        return exc.code
    if str(exc) == 'trainer_inference_attestation_required':
        return 'inference_attestation_required'
    if str(exc) in ('resident_deadline_expired', 'prediction_cancelled'):
        return 'deadline_expired'
    return 'worker_error'


def _validate_request(_envelope):
    # Core transport commit is fail-closed until selector policy integration.
    raise ServiceFailure('request_rejected')


def _serve(root, *, engine=None, validate=None, ready=None, capacity=CAPACITY):
    root = resident._private_state(root)
    engine = engine or GenerationEngine()
    validate = validate or _validate_request
    pending = queue.Queue(maxsize=capacity)
    busy = threading.Event()
    stopped = threading.Event()
    lease = _lock(root / LEASE_NAME)
    fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
    path = _address(root)
    pid_path = root / PID_NAME
    identity = None

    def respond(conn, row):
        try:
            conn.settimeout(.2)
            resident._write(conn, row)
        except (OSError, ValueError):
            pass
        finally:
            conn.close()

    def score():
        while not stopped.is_set() or not pending.empty():
            try:
                conn, envelope, deadline, queued = pending.get(timeout=.05)
            except queue.Empty:
                continue
            busy.set()
            queue_wait = round(time.monotonic()-queued, 6)
            try:
                if resident._cancelled(conn, deadline):
                    raise ServiceFailure('deadline_expired')
                job = validate(envelope)
                result, timing = engine.run(job,
                    cancelled=lambda: resident._cancelled(conn, deadline),
                    revalidate=lambda: validate(envelope))
                timing['queue_wait_seconds'] = queue_wait
                respond(conn, {'status': 'ok', 'prediction': result, 'timing': timing})
            except Exception as exc:
                respond(conn, {'status': _failure(exc), 'prediction': None, 'timing': {}})
            finally:
                busy.clear()
                pending.task_done()

    worker = threading.Thread(target=score, name='laya-inference-score', daemon=True)
    try:
        if path.exists() or path.is_symlink() or pid_path.exists() or pid_path.is_symlink():
            raise ServiceFailure('unsafe_socket')
        with socket.socket(socket.AF_UNIX) as server:
            server.bind(str(path));os.chmod(path, 0o600)
            identity = path.lstat()
            fd = os.open(pid_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'w') as target:
                json.dump({'pid': os.getpid(), 'socket_dev': identity.st_dev,
                           'socket_ino': identity.st_ino}, target)
            server.listen(CAPACITY);server.settimeout(.1)
            worker.start()
            if ready is not None:
                ready.set()
            while not stopped.is_set():
                try:
                    conn, _ = server.accept()
                except socket.timeout:
                    continue
                try:
                    if not resident._same_owner(conn):
                        raise ServiceFailure('request_rejected')
                    envelope = resident._read(conn, deadline=time.monotonic()+resident.HANDSHAKE_SECONDS)
                    if envelope == {'command': 'version'}:
                        respond(conn, {'status': 'ready', 'protocol': PROTOCOL,
                            'signature': _signature(), 'queued': pending.qsize(), 'busy': busy.is_set()})
                        continue
                    if envelope == {'command': 'shutdown'}:
                        if busy.is_set() or not pending.empty():
                            respond(conn, {'status': 'busy'})
                        else:
                            stopped.set();respond(conn, {'status': 'stopped'})
                        continue
                    deadline, wait = resident._request_window(envelope,
                        now_epoch=time.time(), now_monotonic=time.monotonic())
                    try:
                        pending.put_nowait((conn, envelope, deadline, time.monotonic()-wait))
                    except queue.Full:
                        respond(conn, {'status': 'queue_full', 'prediction': None, 'timing': {}})
                except Exception as exc:
                    respond(conn, {'status': _failure(exc), 'prediction': None, 'timing': {}})
    finally:
        stopped.set()
        if worker.is_alive():
            worker.join(timeout=1)
        if identity is not None:
            try:
                current = path.lstat()
                if (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino):
                    path.unlink();pid_path.unlink(missing_ok=True)
            except FileNotFoundError:
                pass
        if not worker.is_alive():
            os.close(lease)


def main():
    if len(sys.argv) != 3 or sys.argv[1] != 'worker':
        raise SystemExit(2)
    root = service_root()
    if Path(sys.argv[2]) != root:
        raise SystemExit(2)
    _serve(root)


if __name__ == '__main__':
    main()

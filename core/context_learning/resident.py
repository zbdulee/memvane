"""One owner-private, idle-expiring Laya reranker for a shared project.

QMD owns retrieval. This socket only receives its frozen candidate pool and
returns selected IDs. Learning, gold and hook orchestration stay elsewhere.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import resource
import select
import socket
import stat
import struct
import subprocess
import sys
import time

from .local_cycle import _safe_file
# Re-exported: the same objects as the neutral runtime_lease primitives.
from runtime_lease import private_lock as _private_lock, private_state as _private_state

MAX_WIRE_BYTES = 1024 * 1024
IDLE_SECONDS = 90
HANDSHAKE_SECONDS = .5
SOCKET_NAME = 'laya-resident.sock'
PID_NAME = 'laya-resident.pid.json'
LEASE_NAME = 'laya-resident-lease.lock'
WIRE_PROTOCOL = 2


class ResidentFailure(ValueError):
    def __init__(self, code, timing=None):
        super().__init__('resident_' + code)
        self.timing = timing if isinstance(timing, dict) else {}


def _hash(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def _read(conn, *, deadline=None):
    result = bytearray()
    while len(result) <= MAX_WIRE_BYTES:
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError('resident_handshake_timeout')
            conn.settimeout(remaining)
        try:
            block = conn.recv(min(65536, MAX_WIRE_BYTES + 1 - len(result)))
        except socket.timeout as exc:
            if deadline is not None:
                raise ValueError('resident_handshake_timeout') from exc
            raise
        if not block:
            break
        result.extend(block)
        if b'\n' in block:
            break
    if len(result) > MAX_WIRE_BYTES or not result.endswith(b'\n'):
        raise ValueError('resident_wire_invalid')
    return json.loads(result)


def _write(conn, value):
    encoded = (json.dumps(value, sort_keys=True, separators=(',', ':'),
                          ensure_ascii=False) + '\n').encode()
    if len(encoded) > MAX_WIRE_BYTES:
        raise ValueError('resident_wire_oversize')
    conn.sendall(encoded)


def _same_owner(conn):
    """The private socket is not a substitute for checking the peer UID."""
    if sys.platform == 'darwin':
        import ctypes
        uid, gid = ctypes.c_uint(), ctypes.c_uint()
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.getpeereid(conn.fileno(), ctypes.byref(uid),
                           ctypes.byref(gid)) != 0:
            return False
        return uid.value == os.getuid()
    if hasattr(socket, 'SO_PEERCRED'):
        data = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                               struct.calcsize('3i'))
        return struct.unpack('3i', data)[1] == os.getuid()
    return False


def _cancelled(conn, deadline):
    if time.monotonic() >= deadline:
        return True
    try:
        # MSG_DONTWAIT does not reliably override a socket timeout on macOS.
        # Only peek after a zero-wait readiness check, so an open idle client
        # cannot be mistaken for a disconnected one.
        if not select.select([conn], [], [], 0)[0]:
            return False
        # A closed caller must not consume model time while other turns wait.
        return not conn.recv(1, socket.MSG_PEEK)
    except OSError:
        return True


def _request_window(envelope, *, now_epoch, now_monotonic):
    if not isinstance(envelope, dict):
        raise ValueError('resident_wire_invalid')
    # macOS Python 3.9 and 3.12 can have different monotonic origins. Send a
    # bounded wall-clock interval, then enforce it using this worker's clock.
    queued = envelope.get('queued_at_epoch')
    deadline = envelope.get('deadline_epoch')
    if (type(queued) not in (int, float) or type(deadline) not in (int, float)
            or not math.isfinite(queued) or not math.isfinite(deadline)
            or queued <= 0 or queued > now_epoch + .05 or deadline <= queued
            or deadline - queued > 30):
        raise ValueError('resident_wire_invalid')
    if now_epoch >= deadline:
        raise ValueError('resident_deadline_expired')
    return now_monotonic + (deadline - now_epoch), max(0.0, now_epoch - queued)


def _failure_code(exc):
    # Only fixed, content-free codes cross the socket. Keep the useful budget
    # distinction without leaking paths, source bytes, or runtime messages.
    message = str(exc)
    if message in ('resident_deadline_expired', 'prediction_cancelled'):
        return 'deadline_expired'
    if message in ('model_token_budget_exceeded',
                   'tokenizer_model_limit_exceeded'):
        return 'input_budget_exceeded'
    return 'worker_error'


def _socket_ready(path):
    if not path.exists() and not path.is_symlink():
        return False
    info = path.lstat()
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError('resident_socket_unsafe')
    try:
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(.1)
            client.connect(str(path))
        return True
    except (OSError, socket.timeout):
        return False


def _protocol_compatible(path):
    """Identify an idle worker; a busy worker is left to finish its turn."""
    try:
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(.4)
            client.connect(str(path))
            _write(client, {'command': 'version'})
            answer = _read(client)
    except (OSError, ValueError, socket.timeout):
        return None
    if answer == {'status': 'ready', 'protocol': WIRE_PROTOCOL}:
        return True
    # The previous resident understands shutdown but responds to this new
    # command with its fixed worker_error envelope. Never interrupt a worker
    # that did not finish this handshake within the short timeout.
    if (isinstance(answer, dict) and set(answer) ==
            {'status', 'prediction', 'timing'}
            and answer['status'] == 'worker_error'
            and answer['prediction'] is None
            and isinstance(answer['timing'], dict)):
        return False
    return None


# A first model load is admitted on its measured footprint, not on the policy's
# RSS ceiling (maxRssMib still bounds a running service). The estimate is the
# peak RSS growth that inference preparation recorded for this model and view,
# with a 1.5x margin and a floor; without a record it is DEFAULT_PEAK_MIB.
FOOTPRINT_NAME = 'laya-load-footprint.json'
FOOTPRINT_SCHEMA = 'memvane-load-footprint-v1'
DEFAULT_PEAK_MIB = 1536
MIN_PEAK_MIB = 1024
LOAD_RESERVE_MIB = 768


def estimated_peak_mib(recorded=None):
    """The first-load peak estimate from a recorded growth, never below the floor."""
    if type(recorded) not in (int, float) or not math.isfinite(recorded) or recorded <= 0:
        return DEFAULT_PEAK_MIB
    return max(MIN_PEAK_MIB, math.ceil(recorded * 1.5))


def load_footprint(state, model_dir, view):
    """The recorded peak growth (MiB) for this model and view in ``state``, or None."""
    from .dataset import read_private_json
    try:
        row = read_private_json(Path(state) / FOOTPRINT_NAME)
    except (OSError, ValueError, TypeError):
        return None
    if (not isinstance(row, dict) or row.get('schema') != FOOTPRINT_SCHEMA
            or row.get('modelDir') != str(model_dir) or row.get('view') != view
            or type(row.get('peakMib')) not in (int, float)
            or not math.isfinite(row['peakMib']) or not 0 < row['peakMib'] <= 65536):
        return None
    return row['peakMib']


def record_footprint(state, model_dir, view, peak_mib):
    """Persist one measured first-load peak growth for later admissions."""
    from runtime_lease import atomic_json
    if type(peak_mib) not in (int, float) or not math.isfinite(peak_mib) or peak_mib <= 0:
        return
    atomic_json(Path(state) / FOOTPRINT_NAME, {
        'schema': FOOTPRINT_SCHEMA, 'modelDir': str(model_dir), 'view': view,
        'peakMib': math.ceil(peak_mib), 'recordedAt': time.time()})


def _admission(max_rss_mib, *, already_running, probe=None, peak_mib=None):
    from .shared import resource_report
    first_load = estimated_peak_mib(peak_mib) + LOAD_RESERVE_MIB
    policy = {'maxLoadPerCpu': 1.5,
              'minAvailableMib': 768 if already_running else first_load}
    report = (probe or resource_report)(policy, detailed=True)
    pressure = report.get('memory_pressure')
    free_percent = pressure.get('system_free_percent') if isinstance(pressure, dict) else None
    available = report.get('available_mib')
    if (not report.get('ready') or not isinstance(available, (int, float))
            or available < policy['minAvailableMib'] or
            free_percent is not None and free_percent < 10):
        return None, report
    batch = 1 if available < 2048 else 2 if available < 4096 else 4
    padded = 4096 if available < 4096 else 8192
    if report.get('load_per_cpu',0)>policy['maxLoadPerCpu']*.75:
        batch,padded=1,min(padded,4096)
    return {'max_batch_size': batch, 'max_padded_tokens': padded}, report


def _launch(state, python, model_dir, artifact, artifact_sha, input_view,
            deadline):
    path = state / SOCKET_NAME
    pid_path = state / PID_NAME
    lease_path = state / LEASE_NAME
    lock_path = state / 'laya-resident-start.lock'
    fd = _private_lock(lock_path)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError('resident_busy_or_starting') from exc
        if _socket_ready(path):
            return
        lease_fd = _private_lock(lease_path)
        try:
            try:
                fcntl.flock(lease_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ValueError('resident_busy_or_starting') from exc
            # A free kernel lease proves the previous worker has exited,
            # including after reboot and PID reuse. The socket was checked
            # as owner-held and non-symlink by _socket_ready above.
            if path.exists():
                path.unlink()
            if pid_path.exists() or pid_path.is_symlink():
                _safe_file(pid_path, max_size=512)
                pid_path.unlink()
        finally:
            fcntl.flock(lease_fd, fcntl.LOCK_UN)
            os.close(lease_fd)
        env = {key: os.environ[key] for key in ('PATH', 'LANG', 'TMPDIR') if key in os.environ}
        env.update(PYTHONPATH=str(Path(__file__).resolve().parents[1]),
                   PYTHONDONTWRITEBYTECODE='1', HF_HUB_OFFLINE='1',
                   TRANSFORMERS_OFFLINE='1', HF_DATASETS_OFFLINE='1',
                   QMD_SANDBOX='1')
        from runtime_cpu import environment
        env=environment(env)
        subprocess.Popen([python, '-m', 'context_learning.resident', 'worker',
                          str(state), model_dir, str(artifact), artifact_sha,
                          input_view], cwd=state, env=env, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         close_fds=True, start_new_session=True)
        while time.monotonic() < deadline:
            if _socket_ready(path):
                return
            time.sleep(.05)
        raise ValueError('resident_start_timeout')
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def predict(state, policy, request, artifact_sha, *, deadline, probe=None):
    """Return (prediction, metrics), or a safe fixed-code failure.

    The caller has already checked the private live-selector policy, artifact
    hash and exact adapter path. No extra service or download is started.
    """
    state = _private_state(state)
    path = state / SOCKET_NAME
    running = _socket_ready(path)
    if running:
        compatible = _protocol_compatible(path)
        if compatible is False:
            if not stop(state):
                raise ValueError('resident_busy_or_starting')
            running = False
        elif compatible is not True:
            raise ValueError('resident_busy_or_starting')
    limits, pressure = _admission(policy['maxRssMib'],
                                  already_running=running, probe=probe)
    if limits is None:
        raise ValueError('resident_resource_deferred')
    if deadline - time.monotonic() < 1:
        raise ValueError('resident_deadline')
    artifact = _safe_file(request['artifact_path'])
    if _hash(artifact) != artifact_sha:
        raise ValueError('resident_artifact_changed')
    argv = policy['trainerArgv']
    started_loading = time.perf_counter()
    if not running:
        _launch(state, argv[0], argv[2], artifact, artifact_sha,
                request['model_input_view'], deadline)
    start_seconds = time.perf_counter() - started_loading
    started = time.perf_counter()
    try:
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(max(.1, deadline - time.monotonic()))
            client.connect(str(path))
            queued = time.monotonic()
            queued_epoch = time.time()
            _write(client, {'artifact_sha256': artifact_sha,
                            'model_dir': argv[2],
                            'model_input_view': request['model_input_view'],
                            'request': request, 'limits': limits,
                            'queued_at_epoch': queued_epoch,
                            'deadline_epoch': queued_epoch + (deadline - queued)})
            response = _read(client)
    except socket.timeout as exc:
        raise ResidentFailure('deadline_expired', {
            'rerank_wall_seconds': round(time.perf_counter() - started, 6),
            'resident_start_seconds': round(start_seconds, 6),
            'admission_available_mib': pressure.get('available_mib'),
            'admission_memory_pressure': pressure.get('memory_pressure')}) from exc
    if not isinstance(response, dict) or set(response) != {'status', 'prediction', 'timing'}:
        raise ValueError('resident_unavailable')
    timing = response['timing']
    if not isinstance(timing, dict):
        raise ValueError('resident_unavailable')
    timing['rerank_wall_seconds'] = round(time.perf_counter() - started, 6)
    timing['resident_start_seconds'] = round(start_seconds, 6)
    timing['admission_available_mib'] = pressure.get('available_mib')
    timing['admission_memory_pressure'] = pressure.get('memory_pressure')
    if time.monotonic() >= deadline:
        raise ResidentFailure('deadline_expired', timing)
    if response['status'] != 'ok':
        code = response['status']
        if code not in ('deadline_expired', 'model_changed',
                        'input_budget_exceeded', 'worker_error'):
            code = 'unavailable'
        raise ResidentFailure(code, timing)
    return response['prediction'], timing


def stop(state):
    """Best-effort owner-requested shutdown, used by safe local smoke tests."""
    path = _private_state(state) / SOCKET_NAME
    if not _socket_ready(path):
        return False
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(2)
        client.connect(str(path))
        _write(client, {'command': 'shutdown'})
        stopped = _read(client) == {'status': 'stopped'}
    until = time.monotonic() + 2
    while stopped and path.exists() and time.monotonic() < until:
        time.sleep(.02)
    return stopped and not path.exists()


def _worker(state, model_dir, artifact, artifact_sha, input_view):
    from .laya_adapter import _head_path, _load, model_identity, predict_loaded
    from .local_cycle import _hash as safe_hash
    state = _private_state(state)
    lease_fd = _private_lock(state / LEASE_NAME)
    try:
        fcntl.flock(lease_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        os.close(lease_fd)
        raise ValueError('resident_already_running') from exc
    # Keep lease_fd live until the process exits. A crashed process releases
    # the kernel lock, so startup can distinguish a dead worker from backlog.
    artifact = _safe_file(artifact)
    if safe_hash(artifact) != artifact_sha:
        raise ValueError('resident_artifact_changed')
    base_sha = model_identity(model_dir)
    head = _head_path(artifact, base_sha, input_view)
    model, tok, cfg = _load(model_dir, head_artifact=head,
                            input_view=input_view)
    path = state / SOCKET_NAME
    pid_path = state / PID_NAME
    if path.exists() or path.is_symlink():
        raise ValueError('resident_socket_exists')
    if pid_path.exists() or pid_path.is_symlink():
        raise ValueError('resident_pid_exists')
    os.umask(0o077)
    with socket.socket(socket.AF_UNIX) as server:
        server.bind(str(path))
        os.chmod(path, 0o600)
        identity = path.lstat()
        made_pid = False
        try:
            fd = os.open(pid_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                         0o600)
            made_pid = True
            with os.fdopen(fd, 'w') as record:
                json.dump({'pid': os.getpid(), 'socket_dev': identity.st_dev,
                           'socket_ino': identity.st_ino}, record)
                record.flush()
                os.fsync(record.fileno())
            server.listen(4)
            server.settimeout(IDLE_SECONDS)
        except BaseException:
            try:
                current = path.lstat()
                if (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino):
                    path.unlink()
            except FileNotFoundError:
                pass
            if made_pid:
                pid_path.unlink(missing_ok=True)
            raise
        try:
            while True:
                try:
                    conn, _ = server.accept()
                except socket.timeout:
                    break
                with conn:
                    conn.settimeout(HANDSHAKE_SECONDS)
                    timing = {}
                    accepted_at = time.perf_counter()
                    try:
                        if not _same_owner(conn):
                            raise ValueError('resident_peer_unauthorized')
                        envelope = _read(conn, deadline=
                                         time.monotonic() + HANDSHAKE_SECONDS)
                        if envelope == {'command': 'shutdown'}:
                            _write(conn, {'status': 'stopped'})
                            break
                        if envelope == {'command': 'version'}:
                            _write(conn, {'status': 'ready',
                                          'protocol': WIRE_PROTOCOL})
                            continue
                        if (not isinstance(envelope, dict) or set(envelope) !=
                                {'artifact_sha256', 'model_dir',
                                 'model_input_view', 'request', 'limits',
                                 'queued_at_epoch', 'deadline_epoch'}):
                            raise ValueError('resident_wire_invalid')
                        if (envelope['artifact_sha256'] != artifact_sha or
                                envelope['model_dir'] != model_dir or
                                envelope['model_input_view'] != input_view):
                            _write(conn, {'status': 'model_changed',
                                          'prediction': None, 'timing': {}})
                            break
                        if (not isinstance(envelope['request'], dict) or
                                envelope['request'].get('artifact_path') != str(artifact)):
                            raise ValueError('resident_artifact_changed')
                        limits = envelope['limits']
                        if (not isinstance(limits, dict) or set(limits) !=
                                {'max_batch_size', 'max_padded_tokens'} or
                                type(limits['max_batch_size']) is not int or
                                type(limits['max_padded_tokens']) is not int or
                                not 1 <= limits['max_batch_size'] <= 4 or
                                limits['max_padded_tokens'] not in (4096, 8192)):
                            raise ValueError('resident_wire_invalid')
                        deadline, queue_wait = _request_window(
                            envelope, now_epoch=time.time(),
                            now_monotonic=time.monotonic())
                        if _cancelled(conn, deadline):
                            raise ValueError('resident_deadline_expired')
                        timing = {'queue_wait_seconds': round(queue_wait, 6)}
                        prediction = predict_loaded(model, tok, cfg,
                            envelope['request'],
                            max_batch_size=limits['max_batch_size'],
                            max_padded_tokens=limits['max_padded_tokens'],
                            timing=timing,
                            cancelled=lambda: _cancelled(conn, deadline))
                        if _cancelled(conn, deadline):
                            raise ValueError('resident_deadline_expired')
                        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
                        timing['process_peak_rss_kib'] = (round(peak / 1024)
                            if sys.platform == 'darwin' else peak)
                        _write(conn, {'status': 'ok', 'prediction': prediction,
                                      'timing': timing})
                    except (OSError, ValueError, TypeError, KeyError,
                            json.JSONDecodeError) as exc:
                        code = _failure_code(exc)
                        timing['worker_elapsed_seconds'] = round(
                            time.perf_counter() - accepted_at, 6)
                        try:
                            _write(conn, {'status': code,
                                          'prediction': None, 'timing': timing})
                        except OSError:
                            pass
        finally:
            try:
                current = path.lstat()
                if (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino):
                    path.unlink()
                    pid_path.unlink(missing_ok=True)
            except FileNotFoundError:
                pass


def main():
    if len(sys.argv) != 7 or sys.argv[1] != 'worker':
        raise SystemExit(2)
    from runtime_cpu import lease
    with lease():
        _worker(Path(sys.argv[2]), sys.argv[3], sys.argv[4], sys.argv[5],sys.argv[6])


if __name__ == '__main__':
    main()

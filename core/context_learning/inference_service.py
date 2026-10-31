"""One plugin-owned inference process per OS user; never a training transport."""
from __future__ import annotations

import hashlib
import math
import os
from pathlib import Path
import sys
import time

from . import resident
from .inference_identity import IdentityCache, has_evidence

_IDENTITY_CACHE = IdentityCache()


class ServiceFailure(ValueError):
    def __init__(self, code, timing=None):
        super().__init__('resident_service_' + code)
        self.code = code
        self.timing = timing or {}


def _check_root(root, home):
    for part in (root, *root.parents):
        if part == home:
            break
        if part.is_symlink():
            raise ServiceFailure('unsafe_root')
        if part.exists():
            info = part.stat()
            if not part.is_dir() or info.st_uid != os.getuid() or info.st_mode & 0o022:
                raise ServiceFailure('unsafe_root')


TEST_ROOT_ENV = 'MEMVANE_TEST_RUNTIME_ROOT'


def _test_root(create):
    """The test-only runtime root, or None.

    ``npm test`` (the development guard) and the shared fixtures export
    ``MEMVANE_TEST_RUNTIME_ROOT`` so every runtime participant of a test run
    (wake queue, worker lease, coordinator, monitor) resolves one temp root and
    never the user's. A value that is set but not an absolute path strictly
    inside a fixed platform temp root fails closed (``unsafe_test_root``): it
    never falls back to the user's root, and never redirects a real install.
    """
    raw = os.environ.get(TEST_ROOT_ENV)
    if not raw:
        return None
    from runtime_lease import in_platform_temp
    base = Path(raw)
    if not base.is_absolute() or not in_platform_temp(base):
        raise ServiceFailure('unsafe_test_root')
    base = Path(os.path.realpath(base))
    root = base / 'laya-inference'
    if create:
        base.mkdir(mode=0o700, parents=True, exist_ok=True)
        root.mkdir(mode=0o700, exist_ok=True)
    return resident._private_state(root)


def service_root(*, create=False, home=None):
    # Host HOME/CODEX_HOME overrides must not create a second user service.
    if home is None:
        test_root = _test_root(create)
        if test_root is not None:
            return test_root
        import pwd
        home = pwd.getpwuid(os.getuid()).pw_dir
    home = Path(home).resolve()
    from .runtime_setup import learning_state_base
    root = learning_state_base(home) / 'laya-inference'
    try:
        _check_root(root, home)
    except ServiceFailure:
        # Resolved just before a learning-state move sealed the old tree
        # (a regular file now stands there): resolve once more.
        moved = learning_state_base(home) / 'laya-inference'
        if moved == root:
            raise
        root = moved
        _check_root(root, home)
    if create:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        moved = learning_state_base(home) / 'laya-inference'
        if moved != root:
            # The learning state moved while this resolved the old base:
            # never leave (or use) a second lane directory there.
            try:
                root.rmdir()
            except OSError:
                pass
            moved.mkdir(mode=0o700, parents=True, exist_ok=True)
            root = moved
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
    """Current self RSS. A past failed allocation must not poison admission."""
    try:
        if sys.platform == 'darwin':
            import ctypes as c
            class Info(c.Structure):
                _fields_ = [('virtual_size', c.c_uint64), ('resident_size', c.c_uint64),
                            ('resident_size_max', c.c_uint64), ('user_time', c.c_int*2),
                            ('system_time', c.c_int*2), ('policy', c.c_int), ('suspend_count', c.c_int)]
            libc = c.CDLL('/usr/lib/libSystem.B.dylib', use_errno=True)
            task = c.c_uint.in_dll(libc, 'mach_task_self_')
            info = Info();count = c.c_uint(c.sizeof(info)//c.sizeof(c.c_uint))
            task_info = libc.task_info
            task_info.argtypes = [c.c_uint, c.c_int, c.POINTER(c.c_uint), c.POINTER(c.c_uint)]
            task_info.restype = c.c_int
            status = task_info(task, 20, c.cast(c.byref(info), c.POINTER(c.c_uint)), c.byref(count))
            if status != 0 or count.value < c.sizeof(info)//c.sizeof(c.c_uint):
                raise OSError('self_memory_unavailable')
            value = info.resident_size / (1024*1024)
        else:
            pages = int(Path('/proc/self/statm').read_text().split()[1])
            value = pages * os.sysconf('SC_PAGE_SIZE') / (1024*1024)
        if not math.isfinite(value) or value <= 0:
            raise ValueError('self_memory_unavailable')
        return value
    except (OSError, ValueError, IndexError, AttributeError) as exc:
        raise ServiceFailure('resource_unavailable') from exc


def _load(job):
    from .laya_adapter import _head_path, _load as load
    head = _head_path(job['request']['artifact_path'], job['family'][1], job['family'][2])
    return load(job['model_dir'], head_artifact=head, input_view=job['family'][2])


def _forward(loaded, job, *, limits, timing, cancelled):
    from .laya_adapter import predict_loaded
    result = predict_loaded(*loaded, job['request'], **limits,
                            timing=timing, cancelled=cancelled)
    case = job['request']['cases'][0]
    if (not isinstance(result, dict) or set(result) != {'schema_version','predictions'}
            or result['schema_version'] != 1 or not isinstance(result['predictions'], list)
            or len(result['predictions']) != 1):
        raise ServiceFailure('invalid_prediction')
    prediction = result['predictions'][0]
    if (not isinstance(prediction, dict) or set(prediction) != {'request_id','input_sha256','selected_ids'}
            or prediction['request_id'] != case['request_id']
            or prediction['input_sha256'] != case['input_sha256']):
        raise ServiceFailure('invalid_prediction')
    selected = prediction['selected_ids']
    ids = {row['id'] for row in case['candidates']}
    if (not isinstance(selected, list) or len(selected) > 3
            or any(not isinstance(key, str) or key not in ids for key in selected)
            or len(set(selected)) != len(selected)):
        raise ServiceFailure('invalid_prediction')
    return result


class GenerationEngine:
    """Serial owner: candidate object cannot mutate the incumbent reference."""
    def __init__(self, *, load=_load, forward=_forward,
                 admission=resident._admission, rss=_rss_mib):
        self.load, self.forward, self.admission, self.rss = load, forward, admission, rss
        self.active = None
        self.footprint_mib = None
        self.pending = None
        self.last_activation = None

    def _pending_key(self,job):
        return (job['apply_id'],job['namespace'],tuple(job['generation']),tuple(job['expected_old']))

    def prepare(self,job,*,cancelled,revalidate):
        from .contracts import digest
        from .store import canonical
        key=self._pending_key(job);token=digest(canonical(key))
        if self.pending is not None and time.monotonic()>=self.pending[4]:self.pending=None
        if self.pending is not None:
            if self.pending[0]!=token:raise ServiceFailure('busy')
            if self._pending_key(revalidate())!=key or cancelled():raise ServiceFailure('request_changed')
            return token
        candidate=GenerationEngine(load=self.load,forward=self.forward,admission=self.admission,rss=self.rss)
        candidate.active=self.active;candidate.footprint_mib=self.footprint_mib
        candidate.run(job,cancelled=cancelled,revalidate=revalidate)
        self.pending=(token,key,candidate.active,candidate.footprint_mib,job['deadline'])
        return token

    def activate(self,token,job):
        key=self._pending_key(job)
        if self.last_activation==(token,key) and self.active is not None and self.active[0]==job['generation']:return
        if (self.pending is None or self.pending[:2]!=(token,key)
                or time.monotonic()>=self.pending[4] or not math.isfinite(self.rss()) or self.rss()>job['policy']['maxRssMib']):
            raise ServiceFailure('request_changed')
        limits,_=self.admission(job['policy']['maxRssMib'],already_running=True)
        if limits is None:raise ServiceFailure('resource_deferred')
        self.active,self.footprint_mib=self.pending[2:4]
        self.last_activation=(token,key);self.pending=None

    def run(self, job, *, cancelled, revalidate):
        def check():
            if cancelled() or time.monotonic() >= job['deadline']:
                raise ServiceFailure('deadline_expired')
        check()
        if self.pending is not None:
            if time.monotonic()>=self.pending[4]:self.pending=None
            elif self.active is None or self.active[0]!=job['generation']:
                raise ServiceFailure('replacement_deferred')
        current = self.rss()
        if not math.isfinite(current) or current > job['policy']['maxRssMib']:
            if self.active is not None and self.pending is None:
                # A loaded model this process can no longer afford is released
                # (before system admission can refuse the request), never kept
                # to refuse every later request; the next one reloads.
                self.active = self.footprint_mib = None
                import gc
                gc.collect()
            raise ServiceFailure('resource_deferred')
        limits, report = self.admission(job['policy']['maxRssMib'],
                                         already_running=self.active is not None)
        if limits is None:
            raise ServiceFailure('resource_deferred')
        replacement = self.active is not None and self.active[0] != job['generation']
        if replacement and self.active[0][:3] != job['family']:
            raise ServiceFailure('model_family_mismatch')
        timing = {}
        pending = None
        warm = None
        try:
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
                if not replacement:
                    loaded_rss = self.rss()
                    if math.isfinite(loaded_rss) and loaded_rss <= job['policy']['maxRssMib']:
                        warm = ((job['generation'], candidate),
                                max(256, math.ceil(max(0, loaded_rss-before) * 1.5)))
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
            if not math.isfinite(self.rss()) or self.rss() > job['policy']['maxRssMib']:
                raise ServiceFailure('replacement_deferred' if replacement else 'resource_deferred')
        except Exception as exc:
            # A first load whose request runs out of time still leaves the
            # service warm while current RSS stays within policy. A later
            # request validates itself and uses the model only when it names
            # this exact generation. Any other failure, and every replacement,
            # publishes nothing.
            if warm is not None and self.active is None and _failure(exc) == 'deadline_expired':
                # Scoring may have grown memory since the load-time check.
                current = self.rss()
                if math.isfinite(current) and current <= job['policy']['maxRssMib']:
                    self.active, self.footprint_mib = warm
            raise
        if pending is not None:
            self.active = pending
            self.footprint_mib = footprint
        return result, timing

# Protocol is independent of host cache paths. The per-user Laya resident
# service (laya_resident_service) hosts this service in its own process and
# owns its lifecycle; a hosted service refuses the shutdown command. A
# learning-only service of an earlier bundle is replaced only while idle: a
# graceful shutdown request, or for a service without that command, SIGTERM
# to its exact verified PID. Never mid-request.
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
# The lease holder binds here, sets 0600 and renames onto SOCKET_NAME, so the
# public path never exists with the umask's mode. Same length: a root that
# fits SOCKET_NAME fits this name.
BIND_NAME = 'service.bind'
_CODES = {'queue_full', 'deadline_expired', 'resource_deferred', 'replacement_deferred',
          'replacement_failed', 'load_failed', 'model_family_mismatch',
          'request_changed', 'request_rejected', 'inference_attestation_required',
          'worker_error', 'version_mismatch', 'busy', 'starting', 'permission_denied',
          'resource_unavailable', 'invalid_prediction', 'input_budget_exceeded'}


@lru_cache(maxsize=1)
def _signature():
    """The executing bundle's code, not only this package.

    The hosted service binds its own adapter to the bundle installed when it
    started. A release that changes only host code (``core/*.py``) must still
    read as a different service, so the next request replaces it instead of
    keeping an earlier bundle that no longer binds. The file set matches the
    resident service's bundle digest; two host caches of one release agree.
    """
    core = Path(__file__).resolve().parents[1]
    digest = hashlib.sha256()
    for path in sorted([*core.glob('*.py'), *core.glob('*.mjs'),
                        *core.glob('context_learning/*.py')]):
        digest.update(path.relative_to(core).as_posix().encode() + b'\0')
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


def _version(root, deadline):
    """The listening service's version answer, or None when none listens."""
    try:
        return _exchange(root, {'command': 'version'}, min(deadline, time.monotonic()+.5))
    except (FileNotFoundError, ConnectionRefusedError, socket.timeout):
        return None


def _current(answer):
    return (isinstance(answer, dict) and answer.get('protocol') == PROTOCOL
            and answer.get('signature') == _signature())


def _idle(answer):
    return (isinstance(answer, dict) and answer.get('status') == 'ready'
            and answer.get('busy') is False and answer.get('queued') == 0)


def _ready(root, deadline):
    answer = _version(root, deadline)
    if answer is None:
        return False
    if not _current(answer):
        raise ServiceFailure('version_mismatch')
    return answer.get('status') == 'ready'


def _waiting_ready(root, deadline):
    """Readiness while a lease changes hands; a closing service is not ready."""
    try:
        return _ready(root, deadline)
    except ServiceFailure as exc:
        if exc.code != 'version_mismatch':
            raise
        return False
    except (ConnectionResetError, BrokenPipeError, ValueError):
        # A stopping service may accept and close without an answer.
        return False


def _process_command(pid):
    """(uid, full command line) of one exact PID, or None."""
    try:
        shown = subprocess.run(['ps', '-ww', '-o', 'uid=', '-o', 'command=', '-p', str(pid)],
                               stdin=subprocess.DEVNULL, capture_output=True,
                               text=True, timeout=1, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    line = shown.stdout.strip()
    if shown.returncode != 0 or not line:
        return None
    uid, _, command = line.partition(' ')
    try:
        return int(uid), command.strip()
    except ValueError:
        return None


def _terminate_idle(root, deadline):
    """SIGTERM one idle service that predates the shutdown command.

    The PID must come from the private PID file bound to the current socket
    inode, run as this user with this root's worker command line, and still
    report idle immediately before the signal. Anything else is left alone.
    """
    import signal
    path = _address(root)
    pid_path = root / PID_NAME
    try:
        resident._safe_file(pid_path, max_size=512)
        row = json.loads(pid_path.read_text(encoding='utf-8'))
        current = path.lstat()
    except (OSError, ValueError):
        raise ServiceFailure('starting') from None
    pid = row.get('pid') if isinstance(row, dict) else None
    if (type(pid) is not int or pid <= 1 or pid == os.getpid()
            or (row.get('socket_dev'), row.get('socket_ino')) != (current.st_dev, current.st_ino)):
        raise ServiceFailure('starting')
    shown = _process_command(pid)
    if (shown is None or shown[0] != os.getuid()
            or 'context_learning.inference_service' not in shown[1]
            or 'worker' not in shown[1] or str(root) not in shown[1]):
        raise ServiceFailure('starting')
    if _idle(_version(root, deadline)) is not True:
        raise ServiceFailure('starting')
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass


def _retire_stale(root, deadline):
    """Stop an idle service from another bundle; the caller then respawns.

    Runs under service-start.lock. A busy or queued old service keeps
    serving; the caller reports ``starting`` and a later request retries.
    """
    answer = _version(root, deadline)
    if answer is None or _current(answer):
        return
    if not _idle(answer):
        raise ServiceFailure('starting')
    try:
        reply = _exchange(root, {'command': 'shutdown'}, min(deadline, time.monotonic()+.5))
    except (FileNotFoundError, ConnectionRefusedError):
        return
    except (socket.timeout, OSError, ValueError):
        raise ServiceFailure('starting') from None
    status = reply.get('status') if isinstance(reply, dict) else None
    if status == 'stopped':
        return
    if status == 'busy':
        raise ServiceFailure('starting')
    _terminate_idle(root, deadline)


def _hosted_ready(root, python, deadline):
    try:
        answer = _version(root, deadline)
    except (ServiceFailure, ConnectionResetError, BrokenPipeError, ValueError):
        return False
    return (_current(answer) and answer.get('status') == 'ready'
            and answer.get('python', python) == python)


def _ensure(root, python, deadline, *, spawn=None):
    """Reach the current bundle's service under ``python``; never steal a lease.

    The product path asks the per-user Laya resident service, which hosts
    this service, to start or to drain and replace an older bundle. An
    injected ``spawn`` keeps the direct learning-only start for fixtures.
    """
    if spawn is not None:
        return _ensure_direct(root, python, deadline, spawn=spawn)
    root = resident._private_state(root)
    if _hosted_ready(root, python, deadline):
        return
    import laya_resident_service as host
    try:
        host.ensure_service(python=python, learning_root=root)
    except Exception as exc:
        raise ServiceFailure('starting') from exc
    while time.monotonic() < deadline:
        if _hosted_ready(root, python, deadline):
            return
        time.sleep(.02)
    raise ServiceFailure('starting')


def _ensure_direct(root, python, deadline, *, spawn=subprocess.Popen):
    """Start a learning-only service, replacing an idle stale one.

    A held kernel lease is never stolen: the stale owner exits first.
    """
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
        try:
            if _ready(root, deadline):
                return
        except ServiceFailure as exc:
            if exc.code != 'version_mismatch':
                raise
            _retire_stale(root, deadline)
        lease = _lock(root / LEASE_NAME)
        try:
            while True:
                try:
                    fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if _waiting_ready(root, deadline):
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
        env = {key: os.environ[key] for key in ('PATH', 'LANG', 'TMPDIR', 'HOME',
                                                'MEMVANE_TEST_RUNTIME_ROOT') if key in os.environ}
        env.update(PYTHONPATH=str(Path(__file__).resolve().parents[1]),
                   PYTHONDONTWRITEBYTECODE='1', HF_HUB_OFFLINE='1',
                   TRANSFORMERS_OFFLINE='1', HF_DATASETS_OFFLINE='1')
        from runtime_cpu import environment
        env=environment(env)
        bootstrap = ('import sys,runpy;sys.path.insert(0,' + repr(str(Path(__file__).resolve().parents[1]))
                     + ');sys.argv=["context_learning.inference_service","worker",sys.argv[1]];'
                     + 'runpy.run_module("context_learning.inference_service",run_name="__main__")')
        spawn([python, '-I', '-B', '-c', bootstrap, str(root)],
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
    if str(exc) == 'trainer_inference_attestation_required' or str(exc).startswith('inference_identity_'):
        return 'inference_attestation_required'
    # 'trainer_timeout': adapter binding found no time left while revalidating.
    if str(exc) in ('resident_deadline_expired', 'prediction_cancelled', 'learning_inference_deadline',
                    'trainer_timeout'):
        return 'deadline_expired'
    # A candidate longer than the model's token budget is refused, never
    # truncated: a fixed input code, not an unexplained worker failure.
    if str(exc) in ('model_token_budget_exceeded', 'tokenizer_model_limit_exceeded'):
        return 'input_budget_exceeded'
    return 'worker_error'


def _policy(state, pointer_name, *, deadline):
    from .dataset import read_private_json
    from .adapter_binding import attested_adapter, inference_argv
    policy = read_private_json(state / 'live-selector.json')
    if (not isinstance(policy, dict) or set(policy) != {
            'schema', 'trainerArgv', 'timeoutSeconds', 'maxRssMib'}
            or policy['schema'] != 'qmd-live-selector-v1'
            or type(policy['timeoutSeconds']) is not int or not 1 <= policy['timeoutSeconds'] <= 30
            or type(policy['maxRssMib']) is not int or not 128 <= policy['maxRssMib'] <= 8192):
        raise ServiceFailure('request_rejected')
    adapter = Path(__file__).with_name('laya_adapter.py').resolve()
    normalized = {**policy, 'trainerArgv': inference_argv(
        policy['trainerArgv'], adapter, deadline=deadline,
        attested_adapter_sha256=attested_adapter(state))}
    if pointer_name == 'experimental-checkpoint.json':
        active = read_private_json(state / pointer_name)
        from .laya_adapter import EDIT_INPUT_VIEW
        if active.get('model_input_view') == EDIT_INPUT_VIEW:
            from .shared import _policy as shared_policy, inference_argv as shared_argv, trial_eligible
            shared = shared_policy(state)
            if shared.get('modelInputView') != EDIT_INPUT_VIEW or not trial_eligible(state, deadline=deadline):
                raise ServiceFailure('request_rejected')
            normalized = {**normalized, 'trainerArgv': shared_argv(shared, 'predict'),
                          'maxRssMib': shared['maxRssMib']}
    return policy, normalized


def _policy_digest(policy):
    from .store import canonical
    return hashlib.sha256(canonical(policy).encode()).hexdigest()


def _make_envelope(state, request, artifact_sha, deadline, pointer_name):
    from .dataset import read_private_json
    now = time.time()
    left = min(30.0, deadline-time.monotonic())
    if left <= 0:
        raise ServiceFailure('deadline_expired')
    return {'schema': PROTOCOL, 'signature': _signature(),
            'state_dir': str(state), 'namespace': namespace(state),
            'pointer_name': pointer_name, 'artifact_sha256': artifact_sha,
            'policy_sha256': _policy_digest(read_private_json(Path(state)/'live-selector.json')),
            'request': request, 'queued_at_epoch': now, 'deadline_epoch': now+left}


def _validate_request(envelope):
    from .dataset import read_private_json
    from .local_cycle import VerifiedLayaTrainer
    from .laya_adapter import LEGACY_INPUT_VIEW, EXPERIMENT_INPUT_VIEW, EDIT_INPUT_VIEW
    if (not isinstance(envelope, dict) or set(envelope) != {
            'schema', 'signature', 'state_dir', 'namespace', 'pointer_name',
            'artifact_sha256', 'policy_sha256', 'request', 'queued_at_epoch', 'deadline_epoch'}
            or envelope['schema'] != PROTOCOL or envelope['signature'] != _signature()
            or not isinstance(envelope['state_dir'], str)
            or envelope['pointer_name'] not in ('active-checkpoint.json', 'experimental-checkpoint.json')):
        raise ServiceFailure('request_rejected')
    deadline, _ = resident._request_window(envelope, now_epoch=time.time(), now_monotonic=time.monotonic())
    state = Path(envelope['state_dir'])
    if envelope['pointer_name']=='active-checkpoint.json':
        from .candidate_apply import readable
        readable(state)
    scope = namespace(state)
    if envelope['namespace'] != scope:
        raise ServiceFailure('request_rejected')
    raw, policy = _policy(state, envelope['pointer_name'], deadline=deadline)
    if _policy_digest(raw) != envelope['policy_sha256']:
        raise ServiceFailure('request_changed')
    deadline = min(deadline, time.monotonic()+policy['timeoutSeconds'])
    argv = policy['trainerArgv']
    # Exact venv path, not resolved symlink target: two venvs can share Python.
    if argv[0] != sys.executable:
        raise ServiceFailure('model_family_mismatch')
    request = envelope['request']
    if (not isinstance(request, dict) or set(request) not in (
            {'schema_version', 'artifact_path', 'cases'},
            {'schema_version', 'artifact_path', 'cases', 'model_input_view'})
            or request['schema_version'] != 1 or not isinstance(request['cases'], list)
            or len(request['cases']) != 1):
        raise ServiceFailure('request_rejected')
    active = read_private_json(state / envelope['pointer_name'])
    if envelope['pointer_name']=='active-checkpoint.json' and active.get('application') is not None:
        from .learning_profile import NAME,selection_enabled
        profile=read_private_json(state/NAME)['proposal']
        if not selection_enabled(state,Path(profile['projectRoot'])):raise ServiceFailure('request_changed')
    if (not isinstance(active, dict) or active.get('schema_version') != 1
            or active.get('artifact_sha256') != envelope['artifact_sha256']
            or active.get('artifact_path') != request.get('artifact_path')
            or active.get('model_input_view') != request.get('model_input_view')):
        raise ServiceFailure('request_changed')
    artifact = resident._safe_file(request['artifact_path'])
    if resident._hash(artifact) != envelope['artifact_sha256']:
        raise ServiceFailure('request_changed')
    case = request['cases'][0]
    from .contracts import digest
    from .store import canonical
    if (not isinstance(case, dict) or set(case) != {'request_id', 'prompt', 'candidates', 'input_sha256'}
            or not isinstance(case['request_id'], str) or not 1 <= len(case['request_id']) <= 128
            or not isinstance(case['prompt'], str) or not case['prompt'].strip()
            or len(case['prompt'].encode()) > 65536
            or not isinstance(case['candidates'], list) or not 1 <= len(case['candidates']) <= 30
            or digest(canonical({key: value for key, value in case.items() if key != 'input_sha256'})) != case['input_sha256']):
        raise ServiceFailure('request_rejected')
    ids = set()
    for row in case['candidates']:
        if (not isinstance(row, dict) or set(row) != {'id','revision_sha256','input_text','input_kind'}
                or not isinstance(row['id'], str) or not row['id'] or row['id'] in ids
                or not isinstance(row['input_text'], str) or not isinstance(row['input_kind'], str)
                or not isinstance(row['revision_sha256'], str) or len(row['revision_sha256']) != 64):
            raise ServiceFailure('request_rejected')
        ids.add(row['id'])
    view = request.get('model_input_view', LEGACY_INPUT_VIEW)
    if view not in (LEGACY_INPUT_VIEW, EXPERIMENT_INPUT_VIEW, EDIT_INPUT_VIEW):
        raise ServiceFailure('request_rejected')
    identity_started = time.perf_counter()
    identity_timing = {}
    if has_evidence(state):
        verified = _IDENTITY_CACHE.verify(state, argv, view)
        identity_timing = dict(_IDENTITY_CACHE.last_timing)
    else:
        trainer = VerifiedLayaTrainer(argv, max_seconds=policy['timeoutSeconds'],
            max_rss_mib=policy['maxRssMib'], proof_cache_dir=state, deadline=deadline)
        verified = trainer.validate_prediction(cwd=state)
    identity_timing['validation_seconds'] = round(time.perf_counter()-identity_started, 6)
    family = (verified['runtime_identity_sha256'], verified['base_model_sha256'], view)
    return {'namespace': scope, 'family': family,
            'generation': (*family, envelope['artifact_sha256']), 'policy': policy,
            'model_dir': argv[2], 'request': request, 'deadline': deadline,
            'identity_timing': identity_timing}


def predict(state, policy, request, artifact_sha, *, deadline, pointer_name='active-checkpoint.json'):
    """Called after choose's read-only proof admission; worker rechecks scope."""
    envelope = _make_envelope(state, request, artifact_sha, deadline, pointer_name)
    root = service_root(create=True)
    started = time.perf_counter()
    try:
        from .resource_coordination import Coordinator
        with Coordinator(root).inference(deadline,envelope['namespace']):
            _ensure(root, policy['trainerArgv'][0], deadline)
            response = _exchange(root, envelope, deadline)
    except PermissionError as exc:
        raise ServiceFailure('permission_denied') from exc
    except socket.timeout as exc:
        raise ServiceFailure('deadline_expired') from exc
    except ValueError as exc:
        if str(exc) == 'resident_handshake_timeout':
            raise ServiceFailure('deadline_expired') from exc
        raise
    if not isinstance(response, dict) or set(response) != {'status', 'prediction', 'timing'}:
        raise ServiceFailure('worker_error')
    if response['status'] != 'ok':
        code = response['status'] if response['status'] in _CODES else 'worker_error'
        if code == 'inference_attestation_required':
            raise ValueError('trainer_inference_attestation_required')
        raise ServiceFailure(code)
    timing = response['timing']
    if not isinstance(timing, dict):
        raise ServiceFailure('worker_error')
    return response['prediction'], {**timing,
        'resident_wall_seconds': round(time.perf_counter()-started, 6)}


def _validate_apply(envelope):
    from .dataset import read_private_json
    from . import candidate_apply as application
    from .shared import _policy as shared_policy
    from .contracts import digest
    from .store import canonical
    if (set(envelope)!={'command','schema','signature','state_dir','namespace','apply_id','queued_at_epoch','deadline_epoch'}
            or envelope['command'] not in ('apply_prepare','apply_activate')
            or envelope['schema']!=PROTOCOL or envelope['signature']!=_signature()):raise ServiceFailure('request_rejected')
    deadline,_=resident._request_window(envelope,now_epoch=time.time(),now_monotonic=time.monotonic())
    state=Path(envelope['state_dir']);scope=namespace(state)
    if envelope['namespace']!=scope:raise ServiceFailure('request_rejected')
    record=application.read_journal(state)
    if record['namespace_root']!=str(state) or record['apply_id']!=envelope['apply_id']:raise ServiceFailure('request_changed')
    policy=shared_policy(state)
    profile,stage,_=application.validate_candidate(state,policy,record['cycle_id'],expected=record)
    application._check_receipts(state,record)
    if envelope['command']=='apply_activate' and (record['phase']!='pointer_committed'
            or read_private_json(state/'active-checkpoint.json')!=record['new_pointer']):raise ServiceFailure('request_changed')
    _,live=_policy(state,'active-checkpoint.json',deadline=deadline);argv=live['trainerArgv']
    if argv!=policy['trainerArgv'] or argv[0]!=sys.executable:raise ServiceFailure('model_family_mismatch')
    verified=_IDENTITY_CACHE.verify(state,argv,profile['view'])
    if verified['base_model_sha256']!=profile['modelIdentitySha256']:raise ServiceFailure('model_family_mismatch')
    case={'request_id':'apply-synthetic','prompt':'What is the synthetic Cobalt marker?',
          'candidates':[{'id':'synthetic/cobalt','revision_sha256':'0'*64,'input_text':'Synthetic Cobalt marker is COBALT-271.','input_kind':'full_compact_wiki_body'}]}
    case['input_sha256']=digest(canonical(case))
    family=(verified['runtime_identity_sha256'],verified['base_model_sha256'],profile['view'])
    return {'namespace':scope,'family':family,'generation':(*family,stage['artifactSha256']),
        'expected_old':(*family,record['old_pointer']['artifact_sha256']),'apply_id':record['apply_id'],
        'policy':live,'deadline':deadline,'model_dir':argv[2],
        'request':{'schema_version':1,'artifact_path':stage['artifactPath'],'model_input_view':profile['view'],'cases':[case]}}


def _serve(root, *, engine=None, validate=None, ready=None, capacity=CAPACITY,apply_validate=None,monitor_factory=None,
           stop=None, hosted=False):
    """Serve until shutdown; ``stop`` (hosted) ends it after queued work drains."""
    root = resident._private_state(root)
    production_engine=engine is None
    engine = engine or GenerationEngine()
    validate = validate or _validate_request
    from .resource_coordination import Coordinator
    coordinator=Coordinator(root)
    apply_validate=apply_validate or _validate_apply
    pending = queue.Queue(maxsize=capacity)
    busy = threading.Event()
    stopped = threading.Event()
    lease = _lock(root / LEASE_NAME)
    try:
        fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BaseException:
        os.close(lease)
        raise
    path = _address(root)
    pid_path = root / PID_NAME
    if hosted:
        # Under the lease only: a crashed earlier owner's socket and PID file.
        try:
            if path.exists() or path.is_symlink():
                info = path.lstat()
                if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid()
                        or info.st_mode & 0o077):
                    raise ServiceFailure('unsafe_socket')
                path.unlink()
            if pid_path.exists() or pid_path.is_symlink():
                resident._safe_file(pid_path, max_size=512)
                pid_path.unlink()
        except BaseException:
            os.close(lease)
            raise
    identity = None
    monitor=None

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
                if envelope.get('command') in ('apply_prepare','apply_activate'):
                    from .local_cycle import ResourceDeferred
                    # Background preparation yields to ordinary queued inference.
                    reload_pending=(engine.pending is None or time.monotonic()>=engine.pending[-1])
                    if (envelope['command']=='apply_prepare' or reload_pending) and coordinator.demand():
                        raise ServiceFailure('resource_deferred')
                    try:
                        with coordinator._gate('compute.lock'):
                            job=apply_validate(envelope)
                            token=engine.prepare(job,cancelled=lambda:resident._cancelled(conn,deadline),revalidate=lambda:apply_validate(envelope))
                            if envelope['command']=='apply_activate':
                                job=apply_validate(envelope);engine.activate(token,job)
                            respond(conn,{'status':'prepared' if envelope['command']=='apply_prepare' else 'activated','token':token,'generation':job['generation'][-1]})
                    except ResourceDeferred:raise ServiceFailure('resource_deferred') from None
                    continue
                with coordinator.inference(deadline,str(envelope.get('namespace',''))), coordinator.inference_compute(deadline):
                    validation_started = time.perf_counter()
                    job = validate(envelope)
                    initial_validation = time.perf_counter()-validation_started
                    revalidation_seconds = [0.0]
                    def revalidate():
                        began = time.perf_counter()
                        try: return validate(envelope)
                        finally: revalidation_seconds[0] += time.perf_counter()-began
                    result, timing = engine.run(job,
                        cancelled=lambda: resident._cancelled(conn, deadline),
                        revalidate=revalidate)
                timing.update(job.get('identity_timing', {}))
                timing['initial_validation_seconds'] = round(initial_validation,6)
                timing['revalidation_seconds'] = round(revalidation_seconds[0],6)
                timing['queue_wait_seconds'] = queue_wait
                respond(conn, {'status': 'ok', 'prediction': result, 'timing': timing})
            except Exception as exc:
                respond(conn, {'status': _failure(exc), 'prediction': None, 'timing': {}})
            finally:
                busy.clear()
                pending.task_done()

    worker = threading.Thread(target=score, name='laya-inference-score', daemon=True)
    bind_path = root / BIND_NAME
    bound = False
    try:
        if path.exists() or path.is_symlink() or pid_path.exists() or pid_path.is_symlink():
            raise ServiceFailure('unsafe_socket')
        if bind_path.exists() or bind_path.is_symlink():
            # An earlier owner stopped between bind and rename; only the
            # lease holder gets here.
            info = bind_path.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                raise ServiceFailure('unsafe_socket')
            bind_path.unlink()
        with socket.socket(socket.AF_UNIX) as server:
            server.bind(str(bind_path));bound = True
            os.chmod(bind_path, 0o600)
            os.rename(bind_path, path);bound = False
            identity = path.lstat()
            fd = os.open(pid_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'w') as target:
                json.dump({'pid': os.getpid(), 'socket_dev': identity.st_dev,
                           'socket_ino': identity.st_ino}, target)
            server.listen(CAPACITY);server.settimeout(.1)
            if monitor_factory is not None:monitor=monitor_factory(root)
            elif production_engine:
                from runtime_monitor import Supervisor
                monitor=Supervisor(root)
            if monitor is not None:monitor.tick()
            worker.start()
            if ready is not None:
                ready.set()
            while not stopped.is_set() and not (stop is not None and stop.is_set()):
                if monitor is not None:monitor.tick()
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
                            'signature': _signature(), 'queued': pending.qsize(), 'busy': busy.is_set(),
                            'python': sys.executable, 'hosted': hosted,
                            'resource_monitor':monitor.status if monitor is not None else 'not_attached'})
                        continue
                    if envelope == {'command': 'shutdown'}:
                        if hosted or pending.unfinished_tasks:
                            # The resident service owns a hosted lifecycle.
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
        if monitor is not None:monitor.close()
        if worker.is_alive():
            worker.join(timeout=1)
        if bound:
            bind_path.unlink(missing_ok=True)
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
    from runtime_cpu import lease
    with lease(root):_serve(root)


if __name__ == '__main__':
    main()

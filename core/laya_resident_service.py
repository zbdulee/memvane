"""One per-user Laya resident service: shared embedding host + learning inference.

A single process per OS user owns one exact local embedding worker per
provider fingerprint and serves every adopted installation through that
installation's own sealed socket path (``<state>/runtime/laya-embed.sock``).
The installation keeps its provider manifest, owner lease, owner state and
vector profile; only the process behind the socket is shared. The same
process hosts the learning inference service on its existing socket.

Lifecycle: started by the first sync wake (SessionStart) or learning request,
never idle-exits. A client from a newer bundle asks the running service to
drain: listeners close (clients see an absent socket and serve lexical
results), in-flight requests finish, owner leases are released, then the
successor takes the service lease. An older client attaches to a newer
service while their protocol matches; otherwise it stays degraded.

The embedding worker is started through ``start_owned_embed_service`` with
the reviewed provider/worker bytes unchanged. Hooks never wait for a model.
"""
from __future__ import annotations

from contextlib import suppress
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import socket
import stat
import struct
import subprocess
import sys
import threading
import time

PROTOCOL = 1
PROTOCOLS = (1,)
CONTROL = 'control.sock'
LEASE = 'service.lock'
SUCCESSOR = 'successor.lock'
PID = 'service.pid.json'
PROJECTS = 'projects'
INTERPRETER = 'interpreter.json'
WIRE_LIMIT = 64 * 1024
REQUEST_LIMIT = 5_100_000
RESPONSE_LIMIT = 65_000_000
QUERY_SLOTS = 3
DOCUMENT_SLOTS = 3          # QUERY_SLOTS + DOCUMENT_SLOTS stays below the worker's 7
DRAIN_SECONDS = 330         # one document_chunks request may take 300s
SUCCESSOR_WAIT_SECONDS = 600
LOAD_RETRY_SECONDS = (5, 10, 20, 40, 60)
_BUNDLE = None


def _fail(code):
    from laya_index_a import IndexContractError
    raise IndexContractError(code)


def _deferred(code):
    from resource_deferred import ResourceDeferred
    return ResourceDeferred(code)


def _account_home():
    # Host HOME/CODEX_HOME overrides must not create a second user service.
    import pwd
    return Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()


def service_dir(*, create=False, home=None):
    """``<account home>/.local/state/laya/service``: private, same owner."""
    cursor = Path(home).resolve() if home is not None else _account_home()
    for name in ('.local', 'state', 'laya', 'service'):
        cursor = cursor / name
        if not cursor.exists() and not cursor.is_symlink():
            if not create:
                return None
            os.mkdir(cursor, 0o700)
        info = cursor.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o022):
            _fail('laya_service_root_unsafe')
    return private_root(cursor)


def private_root(root):
    root = Path(root)
    info = root.lstat()
    if (not root.is_absolute() or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077
            or os.path.realpath(root) != str(root)):
        _fail('laya_service_root_unsafe')
    return root


def bundle_identity():
    """This executing bundle: plugin version plus a digest of its code."""
    global _BUNDLE
    if _BUNDLE is None:
        plugin = Path(__file__).resolve().parents[1]
        try:
            version = json.loads((plugin / 'plugin.json').read_text('utf-8'))['version']
        except (OSError, ValueError, KeyError, TypeError):
            version = '0'
        digest = hashlib.sha256()
        for path in sorted([*plugin.glob('core/*.py'), *plugin.glob('core/*.mjs'),
                            *plugin.glob('core/context_learning/*.py')]):
            digest.update(path.relative_to(plugin).as_posix().encode() + b'\0')
            digest.update(path.read_bytes())
        _BUNDLE = {'version': str(version), 'digest': digest.hexdigest()}
    return dict(_BUNDLE)


def _version_key(version):
    """Semver-like order; a prerelease sorts before its release."""
    match = re.fullmatch(r'(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+.*)?',
                         str(version))
    if match is None:
        return None
    pre = match.group(4)
    tail = (1,) if pre is None else (0, *[(0, int(part), '') if part.isdigit()
                                          else (1, 0, part) for part in pre.split('.')])
    return tuple(int(match.group(n)) for n in (1, 2, 3)) + (tail,)


def newer(candidate, current):
    """True only when ``candidate`` is a strictly newer bundle version.

    Equal versions never replace each other (two host caches of one release),
    so no two bundles can take turns draining the service.
    """
    if not isinstance(current, dict):
        return True  # a service without bundle identity predates this protocol
    left, right = _version_key(candidate.get('version')), _version_key(current.get('version'))
    if left is None:
        return False
    return right is None or left > right


# ---------------------------------------------------------------- wire helpers

def _same_owner(conn):
    if sys.platform == 'darwin':
        import ctypes
        uid, gid = ctypes.c_uint(), ctypes.c_uint()
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.getpeereid(conn.fileno(), ctypes.byref(uid), ctypes.byref(gid)) != 0:
            return False
        return uid.value == os.getuid()
    if hasattr(socket, 'SO_PEERCRED'):
        data = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i'))
        return struct.unpack('3i', data)[1] == os.getuid()
    return False


def _read_line(conn, deadline):
    data = bytearray()
    while b'\n' not in data:
        left = deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError('laya_service_wire_timeout')
        conn.settimeout(left)
        block = conn.recv(4096)
        if not block:
            break
        data.extend(block)
        if len(data) > WIRE_LIMIT:
            raise ValueError('laya_service_wire_invalid')
    if not data.endswith(b'\n'):
        raise ValueError('laya_service_wire_invalid')
    row = json.loads(data)
    if not isinstance(row, dict):
        raise ValueError('laya_service_wire_invalid')
    return row


def _write_line(conn, row):
    conn.sendall(json.dumps(row, sort_keys=True, separators=(',', ':')).encode() + b'\n')


def _socket_ok(path):
    info = os.lstat(path)
    if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077):
        _fail('laya_service_socket_unsafe')
    return info


def exchange(root, row, *, timeout=.5):
    """One control request; None when no service listens."""
    path = Path(root) / CONTROL
    try:
        info = _socket_ok(path)
    except FileNotFoundError:
        return None
    deadline = time.monotonic() + timeout
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(timeout)
            conn.connect(str(path))
            if os.lstat(path).st_ino != info.st_ino or not _same_owner(conn):
                _fail('laya_service_socket_unsafe')
            _write_line(conn, row)
            return _read_line(conn, deadline)
    except (FileNotFoundError, ConnectionRefusedError):
        return None
    except (OSError, ValueError) as exc:
        raise _deferred('laya_service_busy') from exc


def hello(root, *, timeout=.5):
    return exchange(root, {'command': 'hello', 'protocols': list(PROTOCOLS),
                           'bundle': bundle_identity()}, timeout=timeout)


def _lease_held(root):
    try:
        fd = os.open(Path(root) / LEASE, os.O_RDWR | os.O_NOFOLLOW)
    except FileNotFoundError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        return False
    finally:
        os.close(fd)


def _private_lock(path):
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_nlink != 1 or info.st_mode & 0o077):
        os.close(fd)
        _fail('laya_service_lock_unsafe')
    return fd


def _atomic_json(path, value):
    from runtime_lease import atomic_json
    atomic_json(path, value)


def _interpreter(root, python=None):
    """The service interpreter: requested, else the remembered one, else ours."""
    path = Path(root) / INTERPRETER
    if python is not None:
        _atomic_json(path, {'python': str(python)})
        return str(python)
    try:
        info = os.lstat(path)
        if stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and not info.st_mode & 0o077:
            remembered = json.loads(path.read_text('utf-8')).get('python')
            if (isinstance(remembered, str) and os.path.isabs(remembered)
                    and os.access(remembered, os.X_OK)):
                return remembered
    except (OSError, ValueError, AttributeError):
        pass
    return sys.executable


def default_spawn(argv, *, pass_fds):
    # The same sanitized, offline, one-thread environment as the learning
    # worker: the service may run the trainer interpreter's inference.
    env = {key: os.environ[key] for key in ('PATH', 'HOME', 'LANG', 'TMPDIR', 'USER')
           if key in os.environ}
    env.update(PYTHONDONTWRITEBYTECODE='1', HF_HUB_OFFLINE='1',
               TRANSFORMERS_OFFLINE='1', HF_DATASETS_OFFLINE='1')
    from runtime_cpu import environment
    env = environment(env)
    subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, close_fds=True, pass_fds=pass_fds,
                     start_new_session=True, env=env, cwd='/')


def _spawn(root, *, python=None, learning_root=None, spawn=None):
    """Start one successor that waits for the service lease; never a second."""
    fd = _private_lock(Path(root) / SUCCESSOR)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False  # a successor is already waiting
        if learning_root is None:
            learning_root = _learning_root()
        # -I: the hosted learning inference requires an isolated interpreter.
        argv = [_interpreter(root, python), '-I', '-B', str(Path(__file__).resolve()), 'serve',
                str(root), str(learning_root) if learning_root else '-', str(fd)]
        (spawn or default_spawn)(argv, pass_fds=(fd,))
        return True
    finally:
        os.close(fd)


def _learning_root():
    try:
        from context_learning.inference_service import service_root
        return service_root(create=True)
    except Exception:
        return None


def ensure_service(*, root=None, python=None, learning_root=None, spawn=None):
    """Start, attach to or replace the per-user service; never waits for it.

    Returns ``ready``, ``starting`` or ``replacing``; raises ResourceDeferred
    ``laya_service_protocol_mismatch`` for an older client of an incompatible
    newer service. ``python`` asks for that interpreter (learning inference).
    """
    if root is None:
        if os.environ.get('QMD_SETUP_GUARD_FIXTURE') == '1' or os.environ.get('QMD_SANDBOX'):
            raise _deferred('laya_service_fixture')
        root = service_dir(create=True)
    root = private_root(Path(root))
    answer = hello(root)
    mine = bundle_identity()
    if answer is None:
        if not _lease_held(root):
            _spawn(root, python=python, learning_root=learning_root, spawn=spawn)
        return 'starting'
    status = answer.get('status')
    if status == 'draining':
        _spawn(root, python=python, learning_root=learning_root, spawn=spawn)
        return 'replacing'
    replace = newer(mine, answer.get('bundle'))
    compatible = PROTOCOL in (answer.get('protocols') or ())
    if (not replace and python is not None and answer.get('python') != str(python)
            and compatible and mine.get('version') == (answer.get('bundle') or {}).get('version')):
        # A learning request needs the trainer interpreter; same release only.
        replace = True
    if replace:
        reply = exchange(root, {'command': 'drain', 'protocols': list(PROTOCOLS),
                                'bundle': mine, 'python': str(python) if python else None})
        if reply is not None and reply.get('status') not in ('draining', 'stopped'):
            raise _deferred('laya_service_replacement_refused')
        _spawn(root, python=python, learning_root=learning_root, spawn=spawn)
        return 'replacing'
    if not compatible:
        raise _deferred('laya_service_protocol_mismatch')
    return 'ready' if status == 'ready' else 'starting'


def register(project, *, root):
    """Record one installation for (re)adoption; the service proves it."""
    directory = Path(root) / PROJECTS
    if not directory.exists():
        os.mkdir(directory, 0o700)
    private_root(directory)
    name = hashlib.sha256(str(project).encode()).hexdigest()[:32] + '.json'
    target = directory / name
    row = {'projectRoot': str(project)}
    try:
        info = os.lstat(target)
        if (stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                and not info.st_mode & 0o077
                and json.loads(target.read_text('utf-8')) == row):
            return
    except (FileNotFoundError, ValueError):
        pass
    _atomic_json(target, row)


def request_adopt(project, *, root=None, spawn=None):
    """Hook/wake entry: register the project and wake the service.

    Never waits for model loading; the caller reports ``preparing``.
    """
    from resource_deferred import ResourceDeferred
    if root is None and (os.environ.get('QMD_SETUP_GUARD_FIXTURE') == '1'
                         or os.environ.get('QMD_SANDBOX')):
        raise _deferred('laya_service_fixture')
    try:
        root = private_root(Path(root)) if root is not None else service_dir(create=True)
        register(project, root=root)
        state = ensure_service(root=root, spawn=spawn)
    except ResourceDeferred:
        raise
    except Exception as exc:
        # A service-side problem never fails a query or a sync: the owner
        # state still says preparing and lexical results are served.
        raise _deferred('laya_service_unavailable') from exc
    if state == 'ready':
        with suppress(Exception):
            exchange(root, {'command': 'adopt', 'project': str(project)})
    return state


# ----------------------------------------------------------- legacy residents

def _process_rows(uid):
    try:
        shown = subprocess.run(['ps', '-ww', '-u', str(uid), '-o', 'pid=', '-o', 'command='],
                               stdin=subprocess.DEVNULL, capture_output=True, text=True,
                               timeout=2, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return []
    rows = []
    for line in shown.stdout.splitlines():
        pid, _, command = line.strip().partition(' ')
        if pid.isdigit():
            rows.append((int(pid), command.strip()))
    return rows


def _process_command(pid):
    try:
        shown = subprocess.run(['ps', '-ww', '-o', 'uid=', '-o', 'command=', '-p', str(pid)],
                               stdin=subprocess.DEVNULL, capture_output=True, text=True,
                               timeout=2, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    uid, _, command = shown.stdout.strip().partition(' ')
    if shown.returncode != 0 or not uid.isdigit():
        return None
    return int(uid), command.strip()


def _legacy_argv(command, project, socket_path):
    """``<python> -B <root>/core/laya_embed_resident.py serve <project> <socket> <fp> <fd>``."""
    parts = command.split(' ')
    for index, part in enumerate(parts):
        if (part.endswith('/core/laya_embed_resident.py')
                and parts[index + 1:index + 4] == ['serve', str(project), str(socket_path)]
                and len(parts) == index + 6
                and re.fullmatch(r'[0-9a-f]{64}', parts[index + 4])
                and parts[index + 5].isdigit()):
            return True
    return False


def retire_legacy_resident(project, socket_path, *, rows=None, inspect=None, kill=os.kill):
    """Stop one per-project resident of this installation by exact PID.

    The PID must run as this user with exactly the per-project owner argv
    for this project and socket, and be re-verified immediately before the
    signal. No pattern match ever selects more than this one process.
    """
    uid = os.getuid()
    rows = _process_rows(uid) if rows is None else rows
    matches = [pid for pid, command in rows
               if pid not in (os.getpid(), 1) and _legacy_argv(command, project, socket_path)]
    if len(matches) != 1:
        return None
    pid = matches[0]
    shown = (inspect or _process_command)(pid)
    if shown is None or shown[0] != uid or not _legacy_argv(shown[1], project, socket_path):
        return None
    with suppress(ProcessLookupError):
        kill(pid, signal.SIGTERM)
    return pid


# --------------------------------------------------------------- service side

def _abort(conn):
    with suppress(OSError):
        conn.close()


def _hold(conn, seconds=3):
    """Answer nothing until the client gives up.

    A Unix socket peer reads a close as an empty answer (``*_invalid``). A
    query or status client instead hits its own short timeout, which the
    provider and the owner probe report as unavailable/busy: lexical results.
    """
    time.sleep(seconds)  # the client half-closed already; outlive its timeout
    _abort(conn)


def _read_all(conn, limit, timeout):
    data = bytearray()
    conn.settimeout(timeout)
    while True:
        block = conn.recv(65536)
        if not block:
            return bytes(data)
        data.extend(block)
        if len(data) > limit:
            raise ValueError('laya_service_request_oversize')


class Worker:
    """One loaded embedding worker shared by every same-identity project."""

    def __init__(self, identity, socket_path):
        self.identity = identity
        self.socket_path = socket_path
        self.handle = None
        self.state = 'starting'
        self.failures = 0
        self.retry_at = 0.0
        self.loading = False
        self.slots = {'query': threading.BoundedSemaphore(QUERY_SLOTS),
                      'document': threading.BoundedSemaphore(DOCUMENT_SLOTS)}

    def alive(self):
        return (self.state == 'ready' and self.handle is not None
                and self.handle.process.poll() is None)

    def forward(self, payload, kind):
        slot = self.slots['query' if kind == 'query' else 'document']
        if not slot.acquire(timeout=5 if kind == 'query' else 300):
            raise TimeoutError('laya_service_slot_timeout')
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
                conn.settimeout(5)
                conn.connect(self.socket_path)
                conn.sendall(payload)
                conn.shutdown(socket.SHUT_WR)
                answer = _read_all(conn, RESPONSE_LIMIT, 320)
        finally:
            slot.release()
        if not answer:
            raise ConnectionError('laya_service_worker_closed')
        return answer


class Binding:
    """One adopted installation: its owner leases, socket and listener."""

    def __init__(self, project, manifest, identity, mode, owner_fd, service_fd, listener):
        self.project = project
        self.manifest = manifest
        self.identity = identity
        self.mode = mode
        self.owner_fd = owner_fd
        self.service_fd = service_fd
        self.listener = listener
        self.socket_inode = os.lstat(manifest['serviceSocketPath']).st_ino
        self.closed = False


def _worker_key(manifest):
    from laya_embed_resident import _IDENTITY
    return tuple(manifest[key] for key in _IDENTITY)


def default_prove(project):
    """(decision, proof, mode) for one selected, preview or drifted install."""
    from laya_embed_resident import _owned_route
    try:
        return _owned_route(Path(project))
    except Exception as exc:
        from laya_install_journal import is_bundle_drift, verify_selected_route_read_only
        if not is_bundle_drift(exc):
            raise
        from laya_index_host_route import select_route
        decision = select_route(str(project), action='update')
        if decision.get('state') != 'candidate' or decision.get('projectRoot') != str(project):
            raise
        proof = verify_selected_route_read_only(decision)
        if (proof.get('readOnlyAllowed') is not True
                or not isinstance(proof.get('providerManifest'), dict)):
            raise
        return decision, proof, 'read_only'


def default_attest(decision, proof, mode):
    if mode == 'selected':
        from laya_install_journal import verify_worker_attestation
        if verify_worker_attestation(decision) != proof['providerManifest']:
            _fail('provider_attestation_invalid')


def default_admit():
    from context_learning.resource_coordination import Coordinator
    from context_learning.shared import resource_report
    from laya_product_sync_worker import EMBED_POLICY
    # Memory/pressure admission and learning demand, under control.lock only.
    # The service never holds the provider lane: one cold load per service.
    return Coordinator().check(EMBED_POLICY, resource_report(EMBED_POLICY, detailed=True))['ready']


def default_start_worker(manifest, admit):
    from laya_local_embed_service import start_owned_embed_service
    return start_owned_embed_service(manifest, admit=admit)


class Service:
    def __init__(self, root, *, learning_root=None, bundle=None, prove=default_prove,
                 attest=default_attest, start_worker=default_start_worker,
                 admit=default_admit, retire=retire_legacy_resident, tick=1.0,
                 learning=None):
        self.root = private_root(Path(root))
        self.learning_root = learning_root
        self.bundle = bundle or bundle_identity()
        self.prove, self.attest = prove, attest
        self.start_worker, self.admit, self.retire = start_worker, admit, retire
        self.tick_seconds = tick
        self.learning = learning
        self.lock = threading.RLock()
        self.workers = {}
        self.bindings = {}
        self.retry = {}
        self.inflight = 0
        self.inflight_done = threading.Condition(self.lock)
        self.draining = threading.Event()
        self.stopped = threading.Event()
        self.wake = threading.Event()
        self.adopting = threading.Lock()
        self.lease = None
        self.control = None
        self.control_inode = None
        self.threads = []

    # ------------------------------------------------------------ lifecycle
    def acquire(self, *, successor_fd=None, wait=SUCCESSOR_WAIT_SECONDS):
        fd = _private_lock(self.root / LEASE)
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    os.close(fd)
                    return False
                time.sleep(.1)
        self.lease = fd
        if successor_fd is not None:
            os.close(successor_fd)
        path = self.root / CONTROL
        with suppress(FileNotFoundError):
            _socket_ok(path)
            path.unlink()  # only the lease holder removes a stale socket
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(path))
        os.chmod(path, 0o600)
        server.listen(32)
        server.settimeout(.2)
        self.control, self.control_inode = server, os.lstat(path).st_ino
        _atomic_json(self.root / PID, {'pid': os.getpid(), 'bundle': self.bundle,
                                       'socketInode': self.control_inode})
        return True

    def start(self):
        for target, name in ((self._control_loop, 'laya-service-control'),
                             (self._tick_loop, 'laya-service-tick')):
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self.threads.append(thread)
        if self.learning is not None:
            thread = threading.Thread(target=self.learning, args=(self.stopped,),
                                      name='laya-service-learning', daemon=True)
            thread.start()
            self.threads.append(thread)

    def run(self):
        self.start()
        while not self.stopped.wait(.2):
            pass
        self.close()

    def close(self):
        self.stopped.set()
        for thread in self.threads:
            if thread is not threading.current_thread():
                thread.join(timeout=5)
        if self.control is not None:
            with suppress(OSError):
                self.control.close()
            path = self.root / CONTROL
            with suppress(FileNotFoundError):
                if os.lstat(path).st_ino == self.control_inode:
                    path.unlink()
                    (self.root / PID).unlink(missing_ok=True)
            self.control = None
        if self.lease is not None:
            os.close(self.lease)
            self.lease = None

    def status(self):
        with self.lock:
            return {'status': ('draining' if self.draining.is_set() else
                               'ready' if any(w.alive() for w in self.workers.values())
                               else 'starting'),
                    'protocol': PROTOCOL, 'protocols': list(PROTOCOLS),
                    'bundle': self.bundle, 'python': sys.executable, 'pid': os.getpid(),
                    'projects': len(self.bindings), 'workers': len(self.workers),
                    'inflight': self.inflight}

    # -------------------------------------------------------------- control
    def _control_loop(self):
        while not self.stopped.is_set():
            try:
                conn, _ = self.control.accept()
            except (socket.timeout, TimeoutError):
                continue
            except OSError:
                return
            with conn:
                try:
                    if not _same_owner(conn):
                        continue
                    row = _read_line(conn, time.monotonic() + .5)
                    _write_line(conn, self._command(row))
                except (OSError, ValueError):
                    continue

    def _command(self, row):
        command = row.get('command')
        if command in ('hello', 'status'):
            return self.status()
        if command == 'adopt':
            project = row.get('project')
            if isinstance(project, str):
                with self.lock:
                    self.retry.pop(project, None)
            return {**self.status(), 'accepted': True}
        if command == 'drain':
            if not newer(row.get('bundle') or {}, self.bundle) and not (
                    isinstance(row.get('python'), str) and row['python'] != sys.executable
                    and (row.get('bundle') or {}).get('version') == self.bundle.get('version')):
                return {'status': 'refused', 'reason': 'not_newer', 'bundle': self.bundle}
            self.begin_drain()
            return {'status': 'draining'}
        return {'status': 'refused', 'reason': 'unknown_command'}

    def begin_drain(self):
        if self.draining.is_set():
            return
        self.draining.set()
        with self.lock:
            for binding in list(self.bindings.values()):
                self._close_listener(binding)
        thread = threading.Thread(target=self._finish_drain, name='laya-service-drain',
                                  daemon=True)
        thread.start()

    def _finish_drain(self):
        deadline = time.monotonic() + DRAIN_SECONDS
        with self.inflight_done:
            while self.inflight and time.monotonic() < deadline:
                self.inflight_done.wait(.2)
        while self.learning_root and time.monotonic() < deadline and self._learning_busy():
            time.sleep(.2)
        with self.lock:
            for project in list(self.bindings):
                self._release(project, reason='starting')
            for worker in self.workers.values():
                if worker.handle is not None:
                    with suppress(Exception):
                        worker.handle.stop()
            self.workers.clear()
        self.stopped.set()

    def _learning_busy(self):
        try:
            from context_learning import inference_service as learning
            answer = learning._version(Path(self.learning_root), time.monotonic() + .5)
        except Exception:
            return False
        return isinstance(answer, dict) and (answer.get('busy') is True
                                             or answer.get('queued', 0) > 0)

    # ------------------------------------------------------------- adoption
    def registered(self):
        directory = self.root / PROJECTS
        projects = []
        try:
            names = sorted(os.listdir(directory))
        except FileNotFoundError:
            return projects
        for name in names:
            if not re.fullmatch(r'[0-9a-f]{32}\.json', name):
                continue
            path = directory / name
            try:
                info = os.lstat(path)
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                        or info.st_mode & 0o077 or info.st_size > 4096):
                    continue
                project = json.loads(path.read_text('utf-8')).get('projectRoot')
            except (OSError, ValueError, AttributeError):
                continue
            if (isinstance(project, str) and os.path.isabs(project)
                    and hashlib.sha256(project.encode()).hexdigest()[:32] + '.json' == name):
                projects.append(project)
        return projects

    def _tick_loop(self):
        while not self.stopped.is_set() and not self.draining.is_set():
            try:
                self.tick()
            except Exception:
                pass
            self.wake.wait(self.tick_seconds)
            self.wake.clear()

    def tick(self):
        with self.adopting:
            self._tick()

    def _tick(self):
        now = time.monotonic()
        for project in list(self.bindings):
            self._check_owned(project)
        for project in self.registered():
            if self.draining.is_set():
                return
            with self.lock:
                if project in self.bindings or self.retry.get(project, 0) > now:
                    continue
            self._adopt(project)

    def _check_owned(self, project):
        with self.lock:
            binding = self.bindings.get(project)
        if binding is None:
            return
        worker = self.workers.get(_worker_key(binding.manifest))
        try:
            decision, proof, _mode = self.prove(project)
            from laya_embed_resident import _owner_identity
            owned = _owner_identity(decision, proof) == binding.identity
        except Exception:
            owned = False
        if not owned or worker is None or not worker.alive():
            with self.lock:
                self._release(project, reason='starting' if owned or worker is None
                              or worker.alive() else 'worker_exited')

    def _later(self, project, seconds):
        with self.lock:
            self.retry[project] = time.monotonic() + seconds

    def _adopt(self, project):
        from laya_embed_resident import _owner_identity, _write_state
        from laya_local_embed_service import _private_parent, _remove_stale_socket
        try:
            decision, proof, mode = self.prove(project)
            identity = _owner_identity(decision, proof)
            manifest = proof['providerManifest']
            if decision.get('projectRoot') != project:
                _fail('provider_owner_identity_invalid')
            self.attest(decision, proof, mode)
        except Exception:
            self._later(project, 5)
            return
        path = manifest['serviceSocketPath']
        key = _worker_key(manifest)
        try:
            _private_parent(path)
        except Exception:
            self._later(project, 30)
            return
        worker = self.workers.get(key)
        if worker is None or not worker.alive():
            self._load(project, manifest, key)
            return
        owner_fd = _private_lock(path + '.resident.lock')
        try:
            fcntl.flock(owner_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(owner_fd)
            # A per-project resident of an earlier bundle still owns it. The
            # shared worker is ready, so stop that exact owner, then retry.
            with suppress(Exception):
                self.retire(project, path)
            self._later(project, 1)
            return
        service_fd = listener = None
        try:
            service_fd = _private_lock(path + '.lock')
            fcntl.flock(service_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            _remove_stale_socket(path)
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(path)
            os.chmod(path, 0o600)
            listener.listen(16)
            listener.settimeout(.2)
            binding = Binding(project, manifest, identity, mode, owner_fd, service_fd, listener)
        except Exception:
            if listener is not None:
                listener.close()
            if service_fd is not None:
                os.close(service_fd)
            os.close(owner_fd)
            self._later(project, 2)
            return
        with self.lock:
            if self.draining.is_set():
                self._close_listener(binding)
                os.close(binding.service_fd)
                os.close(binding.owner_fd)
                return
            self.bindings[project] = binding
            self.retry.pop(project, None)
        try:
            _write_state(manifest, 'ready', attempts=0)
        except Exception:
            pass
        thread = threading.Thread(target=self._accept_loop, args=(binding,),
                                  name='laya-service-project', daemon=True)
        thread.start()

    def _load(self, project, manifest, key):
        from laya_embed_resident import _write_state
        with self.lock:
            worker = self.workers.get(key)
            if worker is None:
                name = 'w-' + manifest['embeddingFingerprint'][:16] + '.sock'
                worker = self.workers[key] = Worker(key, str(self.root / name))
            if worker.handle is not None and not worker.alive():
                # The model process died: stop its handle, count one failure.
                with suppress(Exception):
                    worker.handle.stop()
                worker.handle, worker.state = None, 'failed'
                worker.failures += 1
                worker.retry_at = time.monotonic() + 5
            if (worker.loading or worker.retry_at > time.monotonic()
                    or worker.state == 'terminal'):
                return
            worker.loading = True
        with suppress(Exception):
            _write_state(manifest, 'starting', attempts=min(3, worker.failures))
        thread = threading.Thread(target=self._load_worker, args=(project, manifest, worker),
                                  name='laya-service-load', daemon=True)
        thread.start()

    def _load_worker(self, project, manifest, worker):
        from laya_embed_resident import _MAX_FAILURES, _write_state
        reason = None
        try:
            handle = self.start_worker(dict(manifest, serviceSocketPath=worker.socket_path),
                                       self.admit)
        except Exception as exc:
            code = str(exc)
            if code in ('provider_admission_deferred', 'laya_service_resource_deferred') or \
                    type(exc).__name__ == 'ResourceDeferred':
                reason = 'resource_deferred'
            elif code in ('provider_artifact_hash_mismatch', 'provider_model_not_gguf',
                          'provider_attestation_invalid', 'provider_closure_unapproved',
                          'provider_recipe_mismatch'):
                reason = 'attestation_invalid'
            else:
                reason = 'startup_failed'
            handle = None
        with self.lock:
            worker.loading = False
            if handle is not None:
                if self.draining.is_set():
                    with suppress(Exception):
                        handle.stop()
                    return
                worker.handle, worker.state, worker.failures = handle, 'ready', 0
                self.retry.pop(project, None)
            elif reason == 'resource_deferred':
                worker.state = 'resource_deferred'
                worker.retry_at = time.monotonic() + LOAD_RETRY_SECONDS[
                    min(len(LOAD_RETRY_SECONDS) - 1, worker.failures)]
            else:
                worker.failures += 1
                terminal = reason == 'attestation_invalid' or worker.failures >= _MAX_FAILURES
                worker.state = 'terminal' if terminal else 'failed'
                worker.retry_at = time.monotonic() + 5 * 2 ** worker.failures
        if handle is not None:
            # The very next tick binds every waiting project at once.
            self.wake.set()
            return
        waiting = [p for p in self.registered() if p not in self.bindings]
        for name in waiting or [project]:
            try:
                decision, proof, _mode = self.prove(name)
                row = proof['providerManifest']
            except Exception:
                continue
            if _worker_key(row) != worker.identity:
                continue
            with suppress(Exception):
                if reason == 'resource_deferred':
                    _write_state(row, 'resource_deferred', attempts=0)
                else:
                    _write_state(row, reason, terminal=worker.state == 'terminal',
                                 attempts=min(_MAX_FAILURES, worker.failures))

    # ----------------------------------------------------------- per project
    def _close_listener(self, binding):
        if binding.closed:
            return
        binding.closed = True
        with suppress(OSError):
            binding.listener.close()
        path = binding.manifest['serviceSocketPath']
        with suppress(FileNotFoundError):
            if os.lstat(path).st_ino == binding.socket_inode:
                os.unlink(path)

    def _release(self, project, *, reason):
        from laya_embed_resident import _write_state
        binding = self.bindings.pop(project, None)
        if binding is None:
            return
        self._close_listener(binding)
        with suppress(Exception):
            _write_state(binding.manifest, reason, attempts=0)
        for fd in (binding.service_fd, binding.owner_fd):
            with suppress(OSError):
                os.close(fd)

    def _accept_loop(self, binding):
        while not binding.closed and not self.stopped.is_set():
            try:
                conn, _ = binding.listener.accept()
            except (socket.timeout, TimeoutError):
                continue
            except OSError:
                return
            with self.lock:
                self.inflight += 1
            threading.Thread(target=self._serve_client, args=(binding, conn),
                             name='laya-service-client', daemon=True).start()

    def _serve_client(self, binding, conn):
        kind = None
        try:
            if not _same_owner(conn):
                _abort(conn)
                return
            payload = _read_all(conn, REQUEST_LIMIT, 300)
            try:
                request = json.loads(payload.decode('utf-8'))
            except (UnicodeError, ValueError):
                request = None
            if not isinstance(request, dict):
                _abort(conn)
                return
            kind = request.get('kind')
            manifest = binding.manifest
            worker = self.workers.get(_worker_key(manifest))
            if worker is None or not worker.alive():
                raise ConnectionError('laya_service_worker_unavailable')
            if request == {'kind': 'status'}:
                from laya_embed_resident import _IDENTITY
                conn.sendall(json.dumps({**{key: manifest[key] for key in _IDENTITY},
                                         'status': 'ready'}).encode())
                conn.close()
                return
            answer = worker.forward(payload, kind)
            conn.sendall(answer)
            conn.close()
        except Exception:
            # Documents belong to an off-hook sync that retries a failed
            # member; a query or probe must read as unavailable, never invalid.
            if kind in ('query', 'status'):
                _hold(conn)
            else:
                _abort(conn)
        finally:
            with self.inflight_done:
                self.inflight -= 1
                self.inflight_done.notify_all()


def _learning_host(learning_root):
    """Host the learning inference service in this process until stopped."""
    def host(stopped):
        from context_learning import inference_service as learning
        root = Path(learning_root)
        while not stopped.is_set():
            try:
                learning._serve(root, stop=stopped, hosted=True)
                return
            except BlockingIOError:
                # A learning-only service of an earlier bundle holds the lease;
                # it stops only while idle (finished in-flight requests).
                with suppress(Exception):
                    learning._retire_stale(root, time.monotonic() + 1)
            except Exception:
                pass
            stopped.wait(1)
    return host


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 4 or args[0] != 'serve' or not args[3].isdigit():
        return 2
    successor_fd = int(args[3])
    try:
        held = os.fstat(successor_fd)
        named = os.lstat(Path(args[1]) / SUCCESSOR)
        if (held.st_dev, held.st_ino) != (named.st_dev, named.st_ino):
            return 2
        root = private_root(Path(args[1]))
    except (OSError, ValueError):
        return 2
    learning_root = None if args[2] == '-' else args[2]
    service = Service(root, learning_root=learning_root,
                      learning=_learning_host(learning_root) if learning_root else None)
    if not service.acquire(successor_fd=successor_fd):
        return 75
    try:
        if learning_root is None:
            service.run()
        else:
            from runtime_cpu import lease
            with lease(learning_root):
                service.run()
    except Exception:
        service.close()
        return 1
    return 0


if __name__ == '__main__':
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    raise SystemExit(main())

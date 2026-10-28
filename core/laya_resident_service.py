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
import selectors
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
# Client connections handled at once, across every project listener. At the
# cap the service stops accepting: further peers wait in the listen backlog
# (LISTEN_BACKLOG per project). A full backlog refuses at connect on macOS;
# on Linux the connect waits for the client's own timeout. The frozen
# provider reads either as provider_unavailable, so a query falls back to
# lexical results.
MAX_CONNECTIONS = 16
LISTEN_BACKLOG = 8
READ_SECONDS = 10           # whole request read, from accept (5.1MB over a local socket)
QUERY_SECONDS = 15          # a query or status request, from accept to answer
DOCUMENT_SECONDS = 320      # a document request, from accept to answer
RECLAIM_SECONDS = 60        # an unbound, idle worker is stopped after this long
# A successor waits this long for the lease, then gives up until a new request.
SUCCESSOR_WAIT_SECONDS = 600
# Every drain phase (in-flight requests and learning, a cold load to settle,
# the forced-exit kill and reap) fits one budget measured from drain start,
# strictly below the successor's wait; the margin covers stopping workers,
# process exit and the successor's own scheduling. A legitimate cold load
# (full runtime rehash of a few hundred MB, admission, the provider's 15 s
# READY bound) ends far inside it. A cold load still running at the budget is
# hung: the escape is process exit (``_force_exit``), never an in-process
# lease release.
DRAIN_MARGIN_SECONDS = 120
DRAIN_BUDGET_SECONDS = SUCCESSOR_WAIT_SECONDS - DRAIN_MARGIN_SECONDS
DRAIN_SECONDS = 330         # in-flight phase cap; one document_chunks request may take 300s
LOAD_DRAIN_SECONDS = 5      # re-check interval while a drain waits for a cold load
# Exit cleanup after the drain budget, on both paths: child discovery, kill
# and reap (forced exit) or stopping every worker (normal), under one
# deadline; a worker still stopping at the deadline is killed. The budget plus
# this bound stays at least DRAIN_SAFETY_SECONDS below the successor's wait.
MAX_EXIT_CLEANUP_SECONDS = 20
MIN_EXIT_CLEANUP_SECONDS = 1  # floor under a scaled test: a reap takes milliseconds
DRAIN_SAFETY_SECONDS = 60
FORCED_EXIT = 'forced-exit.json'
FORCED_EXIT_CODE = 70
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

def _ps(*args):
    try:
        shown = subprocess.run(['ps', *args], stdin=subprocess.DEVNULL, capture_output=True,
                               text=True, timeout=2, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return shown.stdout if shown.returncode == 0 else None


def _darwin_argv(pid):
    """(executable, argv) from ``KERN_PROCARGS2``: argc, exec path, then argv."""
    import ctypes
    libc = ctypes.CDLL(None, use_errno=True)
    argmax, size = ctypes.c_int(0), ctypes.c_size_t(ctypes.sizeof(ctypes.c_int))
    if libc.sysctl((ctypes.c_int * 2)(1, 8), 2, ctypes.byref(argmax),  # KERN_ARGMAX
                   ctypes.byref(size), None, ctypes.c_size_t(0)) != 0 or argmax.value <= 0:
        return None
    buffer = ctypes.create_string_buffer(argmax.value)
    size = ctypes.c_size_t(argmax.value)
    if libc.sysctl((ctypes.c_int * 3)(1, 49, pid), 3, buffer,  # KERN_PROCARGS2
                   ctypes.byref(size), None, ctypes.c_size_t(0)) != 0:
        return None
    raw = buffer.raw[:size.value]
    if len(raw) < 4:
        return None
    argc = struct.unpack_from('i', raw)[0]
    executable, _, rest = raw[4:].partition(b'\0')
    parts = rest.lstrip(b'\0').split(b'\0')
    if argc < 1 or len(parts) < argc:
        return None
    return os.fsdecode(executable), [os.fsdecode(part) for part in parts[:argc]]


def _linux_argv(pid):
    try:
        with open(f'/proc/{pid}/cmdline', 'rb') as stream:
            raw = stream.read(1 << 20)
        executable = os.readlink(f'/proc/{pid}/exe')
    except OSError:
        return None
    if not raw.endswith(b'\0'):
        return None
    return executable, [os.fsdecode(part) for part in raw[:-1].split(b'\0')]


def _process_argv(pid):
    """The exact (executable, argv) of one process; None when unreadable.

    A ``ps`` command string joins argv with spaces, so a project path that
    contains a space could never be told apart from extra arguments.
    """
    try:
        if sys.platform == 'darwin':
            return _darwin_argv(pid)
        if sys.platform.startswith('linux'):
            return _linux_argv(pid)
    except (OSError, ValueError, AttributeError, struct.error):
        return None
    return None


def _process_rows(uid):
    shown = _ps('-u', str(uid), '-o', 'pid=')
    rows = []
    for line in (shown or '').split():
        if line.isdigit():
            found = _process_argv(int(line))
            if found is not None:
                rows.append((int(line), found))
    return rows


def _process_command(pid):
    shown = (_ps('-o', 'uid=', '-p', str(pid)) or '').strip()
    found = _process_argv(pid)
    if not shown.isdigit() or found is None:
        return None
    return int(shown), found


def _legacy_argv(process, project, socket_path):
    """``<python> [flags] <root>/core/laya_embed_resident.py serve <project> <socket> <fp> <fd>``.

    ``process`` is ``(executable, argv)`` with real argv boundaries; the
    executable must be a Python interpreter.
    """
    try:
        executable, argv = process
    except (TypeError, ValueError):
        return False
    if (not isinstance(executable, str) or not os.path.isabs(executable)
            or 'python' not in os.path.basename(executable).lower()
            or not isinstance(argv, list) or not all(isinstance(a, str) for a in argv)):
        return False
    scripts = [index for index, part in enumerate(argv)
               if part.endswith('/core/laya_embed_resident.py')]
    if len(scripts) != 1:
        return False
    index = scripts[0]
    return (index >= 1 and os.path.isabs(argv[index])
            and all(flag.startswith('-') for flag in argv[1:index])
            and argv[index + 1:index + 4] == ['serve', str(project), str(socket_path)]
            and len(argv) == index + 6
            and re.fullmatch(r'[0-9a-f]{64}', argv[index + 4]) is not None
            and argv[index + 5].isdigit())


def retire_legacy_resident(project, socket_path, *, rows=None, inspect=None, kill=os.kill):
    """Stop one per-project resident of this installation by exact PID.

    The PID must run as this user with exactly the per-project owner argv
    for this project and socket, and be re-verified immediately before the
    signal. No pattern match ever selects more than this one process.
    """
    uid = os.getuid()
    rows = _process_rows(uid) if rows is None else rows
    matches = [(pid, process) for pid, process in rows
               if pid not in (os.getpid(), 1) and _legacy_argv(process, project, socket_path)]
    if len(matches) != 1:
        return None
    pid, seen = matches[0]
    shown = (inspect or _process_command)(pid)
    if (shown is None or shown[0] != uid or shown[1] != seen
            or not _legacy_argv(shown[1], project, socket_path)):
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


def _left(deadline):
    left = deadline - time.monotonic()
    if left <= 0:
        raise TimeoutError('laya_service_deadline')
    return left


def _read_all(conn, limit, deadline):
    """Read to EOF before one absolute deadline; a slow sender cannot extend it."""
    data = bytearray()
    while True:
        conn.settimeout(_left(deadline))
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
        self.inflight = 0           # reserved under the service lock with the lookup
        self.idle_since = None
        self.slots = {'query': threading.BoundedSemaphore(QUERY_SLOTS),
                      'document': threading.BoundedSemaphore(DOCUMENT_SLOTS)}

    def alive(self):
        return (self.state == 'ready' and self.handle is not None
                and self.handle.process.poll() is None)

    def forward(self, payload, kind, deadline=None):
        if deadline is None:
            deadline = time.monotonic() + (QUERY_SECONDS if kind == 'query'
                                           else DOCUMENT_SECONDS)
        slot = self.slots['query' if kind == 'query' else 'document']
        if not slot.acquire(timeout=min(_left(deadline), 5 if kind == 'query' else 300)):
            raise TimeoutError('laya_service_slot_timeout')
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
                conn.settimeout(min(_left(deadline), 5))
                conn.connect(self.socket_path)
                conn.settimeout(_left(deadline))
                conn.sendall(payload)
                conn.shutdown(socket.SHUT_WR)
                answer = _read_all(conn, RESPONSE_LIMIT, deadline)
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
    """The full vector profile a worker serves, not only the recipe fingerprint.

    The fingerprint omits ``dimension``, which the worker config applies, so
    two installs of one recipe at different dimensions never share a worker.
    """
    from laya_embed_resident import _IDENTITY
    return tuple(manifest[key] for key in _IDENTITY) + tuple(
        manifest[key] for key in ('modelId', 'dimension'))


def _worker_socket(root, key):
    digest = hashlib.sha256(json.dumps(list(key), sort_keys=True, default=str)
                            .encode()).hexdigest()
    return str(Path(root) / ('w-' + digest[:24] + '.sock'))


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
    """Full attestation before a worker starts or an installation binds.

    Selected: the journal-bound record, then every runtime file re-hashed.
    Preview: the preview proof validated the record; re-hash its runtime files.
    Read-only proofs never start a worker and attach without re-hashing.
    """
    if mode == 'selected':
        from laya_install_journal import verify_worker_attestation
        if verify_worker_attestation(decision) != proof['providerManifest']:
            _fail('provider_attestation_invalid')
    elif mode == 'preview':
        from laya_install_journal import verify_preview_worker_attestation
        if verify_preview_worker_attestation(decision['journalPath'],
                                             proof['providerManifest']) \
                != proof['providerManifest']:
            _fail('provider_attestation_invalid')


def _scaled(seconds):
    """A drain/lease time constant; a test (fixture flag only) may scale all of them.

    One factor for every constant keeps their ordering (budget below the
    successor's wait) in a scaled-down test.
    """
    factor = os.environ.get('LAYA_SERVICE_TIME_SCALE')
    if os.environ.get('QMD_SETUP_GUARD_FIXTURE') == '1' and factor:
        with suppress(ValueError, TypeError):
            factor = float(factor)
            if 0 < factor <= 1:
                return seconds * factor
    return seconds


def _exit_cleanup_seconds():
    return max(_scaled(MAX_EXIT_CLEANUP_SECONDS), MIN_EXIT_CLEANUP_SECONDS)


def _own_children(timeout=5):
    """Direct child PIDs of this process (the listing's own ``ps`` excluded)."""
    try:
        listing = subprocess.Popen(['ps', '-A', '-o', 'pid=', '-o', 'ppid='],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, text=True)
        out, _ = listing.communicate(timeout=max(.01, timeout))
    except subprocess.TimeoutExpired:
        with suppress(OSError):
            listing.kill()
            listing.wait(1)
        return []
    except OSError:
        return []
    me, children = os.getpid(), []
    for line in out.splitlines():
        parts = line.split()
        if (len(parts) == 2 and all(part.isdigit() for part in parts)
                and int(parts[1]) == me and int(parts[0]) != listing.pid):
            children.append(int(parts[0]))
    return children


def _kill_children(deadline):
    """SIGKILL every direct child (embed workers, a half-started one) and reap it.

    Discovery, kill and reap all end by ``deadline`` (monotonic).
    """
    killed = []
    for pid in _own_children(min(5, deadline - time.monotonic())):
        with suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
            killed.append(pid)
    left = set(killed)
    while left and time.monotonic() < deadline:
        for pid in list(left):
            try:
                done, _status = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                done = pid  # another thread's Popen.wait reaped it
            if done == pid:
                left.discard(pid)
        if left:
            time.sleep(.05)
    return killed, sorted(left)


def _stop_handles(handles, deadline):
    """Stop worker handles in parallel; kill any still stopping at ``deadline``."""
    threads = []
    for handle in handles:
        thread = threading.Thread(target=_quiet_stop, args=(handle,),
                                  name='laya-service-stop', daemon=True)
        thread.start()
        threads.append((handle, thread))
    for _handle, thread in threads:
        thread.join(max(0, deadline - time.monotonic()))
    for handle, thread in threads:
        if thread.is_alive():
            with suppress(Exception):
                handle.process.kill()
            thread.join(max(0, deadline - time.monotonic()))


def _quiet_stop(handle):
    with suppress(Exception):
        handle.stop()


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
                 learning=None, exit=os._exit):
        self.root = private_root(Path(root))
        self.learning_root = learning_root
        self.bundle = bundle or bundle_identity()
        self.prove, self.attest = prove, attest
        self.start_worker, self.admit, self.retire = start_worker, admit, retire
        self.tick_seconds = tick
        self.learning = learning
        self.exit = exit
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
        # One cold model load per service: admission measures memory before
        # a load and the lock is the reservation held until that load ends,
        # so loads of different profiles never pass the same check together.
        self.cold_load = threading.Lock()
        self.connections = threading.BoundedSemaphore(MAX_CONNECTIONS)
        self.reclaim_seconds = RECLAIM_SECONDS
        self.attested = set()
        self.lease = None
        self.control = None
        self.control_inode = None
        self.threads = []

    # ------------------------------------------------------------ lifecycle
    def acquire(self, *, successor_fd=None, wait=None):
        wait = _scaled(SUCCESSOR_WAIT_SECONDS) if wait is None else wait
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
        started = time.monotonic()
        budget_end = started + _scaled(DRAIN_BUDGET_SECONDS)
        deadline = min(started + _scaled(DRAIN_SECONDS), budget_end)
        with self.inflight_done:
            while self.inflight and time.monotonic() < deadline:
                self.inflight_done.wait(.2)
        while self.learning_root and time.monotonic() < deadline and self._learning_busy():
            time.sleep(.2)
        # A cold load started before the drain finishes (and stops its model,
        # seeing the drain) before any lease is released, so a load never
        # overlaps the successor. There is no timeout that releases the lease
        # early: while a loader runs, the service keeps its lease and stays
        # draining (clients see absent sockets and serve lexical results).
        # Holding the lock afterwards keeps any new load out.
        while not self.cold_load.acquire(timeout=max(.01, min(
                _scaled(LOAD_DRAIN_SECONDS), budget_end - time.monotonic()))):
            if time.monotonic() >= budget_end:
                self._force_exit(time.monotonic() - started)
                return
        cleanup_end = time.monotonic() + _exit_cleanup_seconds()
        with self.lock:
            for project in list(self.bindings):
                self._release(project, reason='starting')
            handles = [worker.handle for worker in self.workers.values()
                       if worker.handle is not None]
            self.workers.clear()
        _stop_handles(handles, cleanup_end)
        self.stopped.set()

    def _force_exit(self, waited):
        """A hung cold load: no worker may come up, then the process exits.

        Every direct child is killed and reaped first (a model that reached
        Popen included), then the process exits, so the kernel releases the
        lease flock and the stuck load thread dies with it. The lease is never
        released in-process while that thread is alive.
        """
        killed, alive = _kill_children(time.monotonic() + _exit_cleanup_seconds())
        with suppress(Exception):
            _atomic_json(self.root / FORCED_EXIT, {
                'pid': os.getpid(), 'bundle': self.bundle, 'reason': 'cold_load_hung',
                'waitedSeconds': round(waited, 1), 'killedChildren': killed,
                'unconfirmedChildren': alive, 'at': time.time()})
        path = self.root / CONTROL
        with suppress(OSError):
            if self.control_inode is not None and os.lstat(path).st_ino == self.control_inode:
                path.unlink()
        self.exit(FORCED_EXIT_CODE)

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
        self._reclaim()

    def _reclaim(self):
        """Stop a worker no binding uses and no request is using, after a grace."""
        now = time.monotonic()
        stopped = []
        with self.lock:
            if self.draining.is_set():
                return
            used = {_worker_key(binding.manifest) for binding in self.bindings.values()}
            for key, worker in list(self.workers.items()):
                if (key in used or worker.loading or worker.inflight
                        or worker.handle is None):
                    worker.idle_since = None
                    continue
                if worker.idle_since is None:
                    worker.idle_since = now
                if now - worker.idle_since >= self.reclaim_seconds:
                    del self.workers[key]
                    stopped.append(worker.handle)
        for handle in stopped:
            with suppress(Exception):
                handle.stop()

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

    def _forget(self, project):
        name = hashlib.sha256(project.encode()).hexdigest()[:32] + '.json'
        with suppress(OSError):
            os.unlink(self.root / PROJECTS / name)

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
        except Exception:
            if not os.path.isdir(project):
                self._forget(project)  # the project is gone: stop re-proving it
            self._later(project, 5)
            return
        path = manifest['serviceSocketPath']
        key = _worker_key(manifest)
        try:
            _private_parent(path)
        except Exception:
            self._later(project, 30)
            return
        seal = hashlib.sha256(json.dumps([project, identity], sort_keys=True,
                                         default=str).encode()).hexdigest()
        worker = self.workers.get(key)
        if worker is None or not worker.alive():
            if mode == 'read_only':
                # A drifted bundle only attaches to a worker that a full proof
                # started. Changed native or JS files also read as bundle
                # drift, so a read-only proof never starts a model process.
                self._later(project, 5)
                return
            self._load(project, decision, proof, mode, manifest, key, seal)
            return
        owner_fd = _private_lock(path + '.resident.lock')
        attested = seal in self.attested
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
        if not attested:
            # Full artifact rehash: once per installation identity, only on
            # the path that binds, never on a tick that merely waits.
            try:
                self.attest(decision, proof, mode)
            except Exception:
                os.close(owner_fd)
                with suppress(Exception):
                    _write_state(manifest, 'attestation_invalid', terminal=True, attempts=3)
                self._later(project, 60)
                return
            self.attested.add(seal)
        service_fd = listener = None
        try:
            service_fd = _private_lock(path + '.lock')
            fcntl.flock(service_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            _remove_stale_socket(path)
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(path)
            os.chmod(path, 0o600)
            listener.listen(LISTEN_BACKLOG)
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
        try:
            threading.Thread(target=self._accept_loop, args=(binding,),
                             name='laya-service-project', daemon=True).start()
        except Exception:
            # No listener thread: release the binding (listener, socket and
            # leases), so a later tick adopts this installation again.
            with self.lock:
                if self.bindings.get(project) is binding:
                    self._release(project, reason='starting')
            self._later(project, 2)

    def _load(self, project, decision, proof, mode, manifest, key, seal):
        from laya_embed_resident import _write_state
        with self.lock:
            worker = self.workers.get(key)
            if worker is None:
                worker = self.workers[key] = Worker(key, _worker_socket(self.root, key))
            if worker.handle is not None and not worker.alive():
                # The model process died: stop its handle, count one failure.
                with suppress(Exception):
                    worker.handle.stop()
                worker.handle, worker.state = None, 'failed'
                worker.failures += 1
                worker.retry_at = time.monotonic() + 5
            if (self.draining.is_set() or worker.loading
                    or worker.retry_at > time.monotonic() or worker.state == 'terminal'):
                return
            if not self.cold_load.acquire(blocking=False):
                return  # another profile is loading; a later tick retries
            worker.loading = True
        try:
            with suppress(Exception):
                _write_state(manifest, 'starting', attempts=min(3, worker.failures))
            thread = threading.Thread(target=self._load_worker,
                                      args=(project, decision, proof, mode, manifest,
                                            worker, seal),
                                      name='laya-service-load', daemon=True)
            thread.start()
        except BaseException:
            with self.lock:
                worker.loading = False
            self.cold_load.release()
            raise

    def _load_worker(self, project, decision, proof, mode, manifest, worker, seal):
        try:
            self._load_attested(project, decision, proof, mode, manifest, worker, seal)
        finally:
            self.cold_load.release()

    def _load_attested(self, project, decision, proof, mode, manifest, worker, seal):
        from laya_embed_resident import _MAX_FAILURES, _write_state
        try:
            # Every new model process starts only after this installation's
            # full attestation (model, worker, native closure, runtime files)
            # proves again; the frozen provider's own check hashes five files.
            self.attest(decision, proof, mode)
        except Exception:
            # This installation's evidence failed, not the shared worker.
            with self.lock:
                worker.loading = False
            with suppress(Exception):
                _write_state(manifest, 'attestation_invalid', terminal=True, attempts=3)
            self._later(project, 60)
            return
        self.attested.add(seal)
        with self.lock:
            if self.draining.is_set():
                # The drain began during attestation: never start a model.
                worker.loading = False
                return
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
        try:
            with selectors.DefaultSelector() as waiting:
                waiting.register(binding.listener, selectors.EVENT_READ)
                self._accept_until_closed(binding, waiting)
        except Exception:
            pass
        if not binding.closed and not self.stopped.is_set():
            # The listener thread failed while the binding still looked
            # ready: release it, so the next tick adopts it again instead of
            # every request timing out against a socket nobody accepts.
            with self.lock:
                if self.bindings.get(binding.project) is binding:
                    self._release(binding.project, reason='starting')
            self.wake.set()

    def _accept_until_closed(self, binding, waiting):
        while not binding.closed and not self.stopped.is_set():
            # An idle listener holds no slot: wait for a pending peer first.
            # A selector, not select(): a listener fd above FD_SETSIZE is valid.
            if not waiting.select(.2):
                continue
            # The cap is taken before accept, so nothing is read past it; at
            # the cap the peer stays in the kernel backlog.
            if not self.connections.acquire(timeout=.2):
                continue
            try:
                conn, _ = binding.listener.accept()
            except (socket.timeout, TimeoutError, BlockingIOError):
                self.connections.release()
                continue
            except OSError:
                self.connections.release()
                if binding.closed:
                    return
                raise
            accepted = time.monotonic()
            with self.lock:
                self.inflight += 1
            try:
                threading.Thread(target=self._serve_client, args=(binding, conn, accepted),
                                 name='laya-service-client', daemon=True).start()
            except BaseException:
                _abort(conn)
                self._client_done()
                raise

    def _client_done(self):
        self.connections.release()
        with self.inflight_done:
            self.inflight -= 1
            self.inflight_done.notify_all()

    def _serve_client(self, binding, conn, accepted=None):
        kind = reserved = None
        accepted = time.monotonic() if accepted is None else accepted
        try:
            if not _same_owner(conn):
                _abort(conn)
                return
            payload = _read_all(conn, REQUEST_LIMIT, accepted + READ_SECONDS)
            try:
                request = json.loads(payload.decode('utf-8'))
            except (UnicodeError, ValueError):
                request = None
            if not isinstance(request, dict):
                _abort(conn)
                return
            kind = request.get('kind')
            manifest = binding.manifest
            with self.lock:
                # Lookup and in-flight reservation under the lock reclaim
                # decides under: a found worker is never reclaimed mid-request.
                worker = self.workers.get(_worker_key(manifest))
                if worker is None or not worker.alive():
                    raise ConnectionError('laya_service_worker_unavailable')
                worker.inflight += 1
                reserved = worker
            if request == {'kind': 'status'}:
                from laya_embed_resident import _IDENTITY
                conn.sendall(json.dumps({**{key: manifest[key] for key in _IDENTITY},
                                         'status': 'ready'}).encode())
                conn.close()
                return
            deadline = accepted + (QUERY_SECONDS if kind == 'query' else DOCUMENT_SECONDS)
            answer = worker.forward(payload, kind, deadline)
            conn.settimeout(_left(deadline))
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
            if reserved is not None:
                with self.lock:
                    reserved.inflight -= 1
            self._client_done()


def _learning_host(learning_root):
    """Host the learning inference service in this process until stopped."""
    def host(stopped):
        from context_learning import inference_service as learning
        root = Path(learning_root)
        from runtime_cpu import lease
        while not stopped.is_set():
            try:
                # The CPU share gates learning only, never the embedding host.
                with lease(root):
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
        service.run()
    except Exception:
        service.close()
        return 1
    return 0


if __name__ == '__main__':
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    raise SystemExit(main())

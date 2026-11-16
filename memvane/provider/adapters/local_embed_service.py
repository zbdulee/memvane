"""Explicit, owner-bound off-hook lifecycle for the local embedding worker.

The installation owner supplies an independently attested provider manifest
and a real resource-admission callback. Query hooks never start this service.
"""
from __future__ import annotations

import fcntl
import json
import os
import selectors
import socket
import stat
import subprocess
import threading
import time

from memvane.kernel.errors import IndexContractError
from memvane.provider.adapters.local_embed_provider import ReviewedLocalQmdProvider


def _private_parent(path):
    parent_path = os.path.dirname(path)
    if os.path.realpath(parent_path) != parent_path:
        raise IndexContractError('provider_socket_invalid')
    info = os.stat(parent_path, follow_symlinks=False)
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077):
        raise IndexContractError('provider_socket_invalid')


def bind_private(server, path):
    """Bind ``server`` so ``path`` only ever exists with mode 0600.

    Binds a same-length sibling (``*.sock`` -> ``*.bind``, so a path within
    the socket length limit stays within it), sets 0600 and renames it onto
    ``path``. A client polling during startup never sees the umask's mode,
    which it would refuse as unsafe. The caller holds the path's owner lock
    and has removed any stale ``path``.
    """
    path = os.fspath(path)
    head, name = os.path.split(path)
    if not name.endswith('.sock') or len(name) < 6:
        raise IndexContractError('provider_socket_invalid')
    temp = os.path.join(head, name[:-5] + '.bind')
    try:
        info = os.lstat(temp)
    except FileNotFoundError:
        pass
    else:
        # An earlier owner stopped between bind and rename.
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
            raise IndexContractError('provider_socket_invalid')
        os.unlink(temp)
    server.bind(temp)
    try:
        os.chmod(temp, 0o600)
        os.rename(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


def _remove_stale_socket(path):
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return
    if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077):
        raise IndexContractError('provider_socket_invalid')
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.2)
        try:
            probe.connect(path)
        except (ConnectionRefusedError, FileNotFoundError):
            pass
        else:
            raise IndexContractError('provider_already_running')
    # Only the owner lock holder may remove a stale socket.
    if os.lstat(path).st_ino != info.st_ino:
        raise IndexContractError('provider_socket_changed')
    os.unlink(path)


class _GpuFailure(IndexContractError):
    pass


def _event(name, state_root, **fields):
    try:
        from memvane.diagnostics import api as diagnostics
        if state_root is None:
            diagnostics.emit(name, **fields)
        else:
            diagnostics.emit(name, state_root=state_root, **fields)
    except Exception:
        pass


def _diagnostics(pipe, state_root):
    prefix = b'[memvane-provider] '
    try:
        while True:
            line = pipe.readline(8192)
            if not line:
                break
            if not line.startswith(prefix):
                continue
            try:
                row = json.loads(line[len(prefix):].decode('utf8'))
                layers, backend = row.get('gpuLayers'), row.get('gpu')
                if type(layers) is int and 0 <= layers <= 10000 and backend in ('metal', 'cuda', 'vulkan', 'cpu'):
                    _event('provider.worker', state_root, outcome='ready',
                           gpu=layers > 0, gpuLayers=layers, backend=backend)
            except (ValueError, TypeError, UnicodeError):
                pass
    finally:
        pipe.close()


def _launch_worker(manifest, config, socket_path, timeout, *, cpu, state_root):
    process = subprocess.Popen([manifest['nodePath'], manifest['workerPath'], '--serve'],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        cwd='/tmp', env={'MEMVANE_EMBED_CPU': '1'} if cpu else {}, close_fds=True)
    threading.Thread(target=_diagnostics, args=(process.stderr, state_root), daemon=True).start()
    try:
        process.stdin.write(json.dumps(config, separators=(',', ':')).encode('utf-8') + b'\n')
        process.stdin.flush()
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            deadline = time.monotonic() + timeout
            ready = bytearray()
            while b'\n' not in ready:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise IndexContractError('provider_start_timeout')
                part = os.read(process.stdout.fileno(), 64)
                if not part or len(ready) + len(part) > 64:
                    raise IndexContractError('provider_start_failed')
                ready.extend(part)
            if ready != b'READY\n' or process.poll() is not None:
                raise IndexContractError('provider_start_failed')
        _private_parent(socket_path)
        info = os.lstat(socket_path)
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise IndexContractError('provider_socket_invalid')
        return process
    except BaseException as exc:
        terminated_by_owner = False
        try:
            result = process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            terminated_by_owner = True
            process.terminate()
            try:
                result = process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill(); result = process.wait(timeout=5)
        process.stdin.close(); process.stdout.close()
        try:
            os.unlink(socket_path)
        except FileNotFoundError:
            pass
        if not cpu and (result == 86 or (result < 0 and not terminated_by_owner)):
            raise _GpuFailure('provider_gpu_failed') from exc
        raise


class OwnedEmbedService:
    def __init__(self, process, lock_fd, socket_path, lease=None, *, restart=None, admit=None, cpu=False):
        self.process = process
        self.lock_fd = lock_fd
        self.socket_path = socket_path
        self.socket_inode = os.lstat(socket_path).st_ino
        self.lease = lease
        self._restart = restart
        self._admit = admit
        self._cpu_attempted = cpu
        self._restart_lock = threading.Lock()

    def restart_cpu(self, previous):
        """Retry one failed GPU process; concurrent clients share the restart."""
        with self._restart_lock:
            if self.process is not previous:
                return self.process.poll() is None
            if self._cpu_attempted or self._restart is None or self.lock_fd == -1:
                return False
            try:
                result = previous.wait(timeout=0.5)
                if result != 86 and result >= 0:
                    return False
            except subprocess.TimeoutExpired:
                return False
            if self.lease is None:
                admission = self._admit()
                if admission is not True and not callable(getattr(admission, 'release', None)):
                    raise IndexContractError('provider_admission_deferred')
                if admission is not True:
                    self.lease = admission
            self._cpu_attempted = True
            _remove_stale_socket(self.socket_path)
            try:
                self.process = self._restart()
            except BaseException:
                if self.lease is not None:
                    self.lease.release()
                    self.lease = None
                raise
            self.socket_inode = os.lstat(self.socket_path).st_ino
            return True

    def stop(self):
        """Stop only the child and socket owned by this handle."""
        with self._restart_lock:
            self._stop()

    def _stop(self):
        if self.lock_fd == -1:
            return
        try:
            self.process.stdin.close()
            if self.process.poll() is None:
                # Closing the owner pipe also kills the child if the owner
                # itself dies before it can call stop().
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.terminate()
                    try:
                        self.process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        self.process.kill()
                        self.process.wait(timeout=5)
            self.process.stdout.close()
            try:
                if os.lstat(self.socket_path).st_ino == self.socket_inode:
                    os.unlink(self.socket_path)
            except FileNotFoundError:
                pass
        finally:
            os.close(self.lock_fd)
            self.lock_fd = -1
            if self.lease is not None:
                self.lease.release()
                self.lease = None


def _valid_lock_file(info):
    return (stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
            and not info.st_mode & 0o077)


def start_owned_embed_service(manifest, *, admit, startup_timeout_s=15, execution_state=None):
    """Load once after owner admission; return a handle that owns its process.

    The callback is invoked after exact artifact verification and before model
    load. It must implement the product's CPU/memory/GPU resource admission.
    This function never retries, queues, or starts from a query hook.
    """
    if not callable(admit) or not 0 < startup_timeout_s <= 60:
        raise IndexContractError('provider_admission_required')
    provider = ReviewedLocalQmdProvider(manifest)
    socket_path = provider.manifest['serviceSocketPath']
    _private_parent(socket_path)
    if execution_state is None:
        provider._verify_artifacts()
    else:
        from memvane.provider.adapters.reattest import authorize
        try:
            current = authorize(execution_state, manifest)
        except (ValueError, OSError, KeyError, TypeError) as exc:
            raise IndexContractError('provider_artifact_hash_mismatch') from exc
        original = provider.manifest
        try:
            provider.manifest = {**original, 'workerSha256': current['worker'],
                                 'providerArtifactSha256': current['provider']}
            provider._verify_artifacts()
        finally:
            provider.manifest = original
    lock_path = socket_path + '.lock'
    try:
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    except OSError as exc:
        # Some invalid kinds (directory, socket, symlink) fail before fstat.
        # Classify their actual named metadata by the same private-file rule.
        # A valid/missing path with an OS resource error remains retryable.
        try:
            info = os.lstat(lock_path)
        except OSError:
            raise exc
        if not _valid_lock_file(info):
            raise IndexContractError('provider_lock_invalid') from exc
        raise
    try:
        info = os.fstat(lock_fd)
        if not _valid_lock_file(info):
            raise IndexContractError('provider_lock_invalid')
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise IndexContractError('provider_already_running') from exc
        _remove_stale_socket(socket_path)
        admission = admit()
        if admission is not True and not callable(getattr(admission, 'release', None)):
            raise IndexContractError('provider_admission_deferred')
        lease = None if admission is True else admission
        config = {key: provider.manifest[key] for key in (
            'modelPath', 'moduleEntryPath', 'dimension', 'serviceSocketPath',
            'approvedProviderId', 'providerArtifactSha256',
            'embeddingFingerprint')}
        def launch(cpu):
            return _launch_worker(provider.manifest, config, socket_path, startup_timeout_s,
                                  cpu=cpu, state_root=execution_state)
        def restart():
            _event('provider.fallback', execution_state, outcome='cpu', reason='gpu_failure')
            return launch(True)
        try:
            try:
                process = launch(False)
                cpu = False
            except _GpuFailure:
                process = restart()
                cpu = True
            return OwnedEmbedService(process, lock_fd, socket_path, lease,
                                     restart=restart, admit=admit, cpu=cpu)
        except BaseException:
            if lease is not None:
                lease.release()
            raise

    except BaseException:
        os.close(lock_fd)
        raise

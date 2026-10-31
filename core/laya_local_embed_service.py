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
import time

from laya_index_a import IndexContractError
from laya_local_embed_provider import ReviewedLocalQmdProvider


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


class OwnedEmbedService:
    def __init__(self, process, lock_fd, socket_path, lease=None):
        self.process = process
        self.lock_fd = lock_fd
        self.socket_path = socket_path
        self.socket_inode = os.lstat(socket_path).st_ino
        self.lease = lease

    def stop(self):
        """Stop only the child and socket owned by this handle."""
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


def start_owned_embed_service(manifest, *, admit, startup_timeout_s=15):
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
    provider._verify_artifacts()
    lock_path = socket_path + '.lock'
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(lock_fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077):
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
        try:
            process = subprocess.Popen(
                [provider.manifest['nodePath'], provider.manifest['workerPath'], '--serve'],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                cwd='/tmp', env={}, close_fds=True)
            process.stdin.write(json.dumps(config, separators=(',', ':')).encode('utf-8') + b'\n')
            process.stdin.flush()
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                deadline = time.monotonic() + startup_timeout_s
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
            if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o077):
                raise IndexContractError('provider_socket_invalid')
            return OwnedEmbedService(process, lock_fd, socket_path, lease)
        except BaseException:
            if 'process' in locals():
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            try:
                os.unlink(socket_path)
            except FileNotFoundError:
                pass
            if lease is not None:
                lease.release()
            raise
    except BaseException:
        os.close(lock_fd)
        raise

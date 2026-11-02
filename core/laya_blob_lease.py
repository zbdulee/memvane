"""Private cooperative content locks for the inactive Git-blob candidate.

Each lock is a 0600 single-link regular file inside the 0700 sibling
``<candidate filename>.content-locks`` directory, opened without following
symlinks and held by a nonblocking flock. Lock files stay on disk: there is no
unlink, TTL steal or process signal; a crashed holder is released by the
kernel. The acquired descriptor stays internal and close-on-exec. Hostile
same-UID descriptor introspection, explicit unlock or same-inode reopen is
outside this cooperative guarantee.
"""
from __future__ import annotations

from contextlib import suppress
import fcntl
import os
from pathlib import Path
import stat

import laya_blob_values as v
from laya_blob_values import BlobStoreError

ROOT_SUFFIX = '.content-locks'
NOFOLLOW = os.O_NOFOLLOW | os.O_CLOEXEC
DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | NOFOLLOW
LOCK_FILE = os.O_RDONLY | os.O_NONBLOCK | NOFOLLOW


class Lease:
    __slots__ = ('_path', '_root', '_name', '_parent_node', '_root_node', '_node', '_fd', '_pid')

    def __repr__(self):
        return '<content lease>'


def _node(info):
    return (info.st_dev, info.st_ino)


def _private(info, kind, mode):
    return kind(info.st_mode) and info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == mode


def _parent(path):
    """Open the candidate's private parent through a no-symlink descriptor chain."""
    if not path.is_absolute() or '..' in path.parts:
        v.fail('content_lock_path_invalid')
    fd = os.open('/', DIRECTORY)
    try:
        for part in path.parts[1:]:
            child = os.open(part, DIRECTORY, dir_fd=fd)
            os.close(fd); fd = child
        if not _private(os.fstat(fd), stat.S_ISDIR, 0o700):
            v.fail('content_lock_parent_unsafe')
        return fd
    except BaseException:
        os.close(fd)
        raise


def _root(parentfd, name, *, create):
    created = False
    if create:
        try:
            os.mkdir(name, 0o700, dir_fd=parentfd)
            created = True
        except FileExistsError:
            pass
    fd = os.open(name, DIRECTORY, dir_fd=parentfd)
    try:
        if created:
            os.fchmod(fd, 0o700)
        info = os.fstat(fd)
        named = os.stat(name, dir_fd=parentfd, follow_symlinks=False)
        if not _private(info, stat.S_ISDIR, 0o700) or _node(named) != _node(info):
            v.fail('content_lock_root_unsafe')
        return fd, _node(info)
    except BaseException:
        os.close(fd)
        raise


def _lock_file(rootfd, name):
    try:
        fd = os.open(name, LOCK_FILE | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=rootfd)
        created = True
    except FileExistsError:
        fd, created = os.open(name, LOCK_FILE, dir_fd=rootfd), False
    try:
        if created:
            os.fchmod(fd, 0o600)
        info = os.fstat(fd)
        named = os.stat(name, dir_fd=rootfd, follow_symlinks=False)
        if (not _private(info, stat.S_ISREG, 0o600) or info.st_nlink != 1
                or _node(named) != _node(info)):
            v.fail('content_lock_unsafe')
        return fd
    except BaseException:
        os.close(fd)
        raise


def acquire(database_path, key):
    """Nonblocking exclusive lease for one content key; caller holds no SQL transaction."""
    v.identifier(key)
    path = Path(database_path)
    parentfd = rootfd = fd = None
    try:
        parentfd = _parent(path.parent)
        parent_node = _node(os.fstat(parentfd))
        rootfd, root_node = _root(parentfd, path.name + ROOT_SUFFIX, create=True)
        fd = _lock_file(rootfd, key + '.lock')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            v.fail('content_claim_busy')
        held = Lease()
        held._path, held._root, held._name = path, path.name + ROOT_SUFFIX, key + '.lock'
        held._parent_node, held._root_node = parent_node, root_node
        held._node, held._fd, held._pid = _node(os.fstat(fd)), fd, os.getpid()
        fd = None
        try:
            check(held)  # the named inode may have been replaced before flock succeeded
        except BaseException:
            release(held)
            raise
        return held
    except BlobStoreError:
        raise
    except OSError:
        v.fail('content_lock_unavailable')
    finally:
        for descriptor in (fd, rootfd, parentfd):
            if descriptor is not None:
                os.close(descriptor)


def check(held):
    """Refuse unless the held descriptor, named lock, root and parent are unchanged."""
    if not isinstance(held, Lease) or held._fd < 0 or held._pid != os.getpid():
        v.fail('content_claim_stale')
    parentfd = rootfd = None
    try:
        info = os.fstat(held._fd)
        if (_node(info) != held._node or not _private(info, stat.S_ISREG, 0o600)
                or info.st_nlink != 1):
            v.fail('content_claim_lost')
        parentfd = _parent(held._path.parent)
        if _node(os.fstat(parentfd)) != held._parent_node:
            v.fail('content_lock_root_changed')
        rootfd, root_node = _root(parentfd, held._root, create=False)
        if root_node != held._root_node:
            v.fail('content_lock_root_changed')
        named = os.stat(held._name, dir_fd=rootfd, follow_symlinks=False)
        if _node(named) != held._node:
            v.fail('content_lock_replaced')
    except BlobStoreError:
        raise
    except OSError:
        v.fail('content_claim_lost')
    finally:
        for descriptor in (rootfd, parentfd):
            if descriptor is not None:
                os.close(descriptor)


def release(held):
    """Close only the acquired lock inode; a reused descriptor for another inode is left open."""
    fd, held._fd = held._fd, -1
    if fd < 0:
        return
    try:
        info = os.fstat(fd)
    except OSError:
        return
    if _node(info) != held._node:
        return
    if held._pid == os.getpid():  # a forked copy must not unlock the parent's open file description
        with suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
    with suppress(OSError):
        os.close(fd)

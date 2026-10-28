"""Directory removal anchored at verified directory file descriptors.

Every hop is opened relative to its already-open parent with ``O_NOFOLLOW``, so
a symlink swapped into an ancestor or into the tree never redirects a delete to
another directory. Only primitives present in Python 3.9 are used.
"""
from __future__ import annotations

import os
import stat


class UnsafeTree(OSError):
    pass


if not hasattr(os, 'O_NOFOLLOW') or not hasattr(os, 'O_DIRECTORY'):
    _DIR_FLAGS = None
else:
    _DIR_FLAGS = (os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW |
                  getattr(os, 'O_CLOEXEC', 0))


def _flags():
    if _DIR_FLAGS is None:
        # Without O_NOFOLLOW the walk cannot refuse links; never degrade.
        raise UnsafeTree('safe_rmtree_unsupported')
    return _DIR_FLAGS


def _check(info, uid):
    if not stat.S_ISDIR(info.st_mode):
        raise UnsafeTree('safe_rmtree_not_directory')
    if uid is not None and info.st_uid != uid:
        raise UnsafeTree('safe_rmtree_owner')


def open_root(path, *, uid=None):
    """Open an absolute directory path without following its last component."""
    before = os.lstat(path)
    if stat.S_ISLNK(before.st_mode):
        raise UnsafeTree('safe_rmtree_symlink')
    try:
        fd = os.open(path, _flags())
    except OSError as exc:
        if isinstance(exc, FileNotFoundError):
            raise
        raise UnsafeTree('safe_rmtree_open') from exc
    try:
        info = os.fstat(fd)
        _check(info, uid)
        if (info.st_dev, info.st_ino) != (before.st_dev, before.st_ino):
            raise UnsafeTree('safe_rmtree_changed')
    except BaseException:
        os.close(fd)
        raise
    return fd


def open_at(parent_fd, name, *, uid=None):
    """Open one child directory of ``parent_fd``; a link or other type refuses."""
    if not name or name in ('.', '..') or '/' in name:
        raise UnsafeTree('safe_rmtree_name')
    before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if stat.S_ISLNK(before.st_mode):
        raise UnsafeTree('safe_rmtree_symlink')
    _check(before, uid)
    try:
        fd = os.open(name, _flags(), dir_fd=parent_fd)
    except OSError as exc:
        if isinstance(exc, FileNotFoundError):
            raise
        raise UnsafeTree('safe_rmtree_open') from exc
    try:
        info = os.fstat(fd)
        _check(info, uid)
        if (info.st_dev, info.st_ino) != (before.st_dev, before.st_ino):
            raise UnsafeTree('safe_rmtree_changed')
    except BaseException:
        os.close(fd)
        raise
    return fd


def open_path(root_fd, parts, *, uid=None):
    """Walk ``parts`` below ``root_fd``; the caller closes the returned fd."""
    fd = os.dup(root_fd)
    try:
        for name in parts:
            child = open_at(fd, name, uid=uid)
            os.close(fd)
            fd = child
    except BaseException:
        os.close(fd)
        raise
    return fd


def rmtree_at(parent_fd, name, *, uid=None):
    """Remove ``name`` (a real directory under ``parent_fd``) and its contents.

    ``uid`` constrains only the top directory; links inside are unlinked, not
    followed.
    """
    fd = open_at(parent_fd, name, uid=uid)
    try:
        _empty(fd)
    finally:
        os.close(fd)
    os.rmdir(name, dir_fd=parent_fd)


def _empty(fd):
    with os.scandir(fd) as entries:
        names = [(entry.name, entry.is_dir(follow_symlinks=False)) for entry in entries]
    for child, is_dir in names:
        if is_dir:
            sub = open_at(fd, child)
            try:
                _empty(sub)
            finally:
                os.close(sub)
            os.rmdir(child, dir_fd=fd)
        else:
            os.unlink(child, dir_fd=fd)

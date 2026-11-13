"""Directory removal anchored at verified directory file descriptors.

Every hop is opened relative to its already-open parent with ``O_NOFOLLOW``, so
a symlink swapped into an ancestor or into the tree never redirects a delete to
another directory. A mount point inside the tree (a device change, or a path
the OS mount table lists) refuses the whole delete before anything is removed.
Only primitives present in Python 3.9 are used.

The checks guard against accidents (a dotfile link, a mounted volume, a stale
layout). They are not a boundary against a process of the same user that races
renames between the checks and the deletes.
"""
from __future__ import annotations

import os
import stat
import subprocess
import sys


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


def _dev(info):
    return info.st_dev


def _unescape_mountinfo(field):
    out, i = [], 0
    while i < len(field):
        if field[i] == '\\' and i + 3 < len(field) and field[i + 1:i + 4].isdigit():
            out.append(chr(int(field[i + 1:i + 4], 8)))
            i += 4
        else:
            out.append(field[i])
            i += 1
    return ''.join(out)


def _mount_points():
    """Absolute mount points the OS reports; empty when the table is unreadable.

    Same-device mounts (Linux bind mounts) keep ``st_dev``, so the walk's
    device check alone does not see them.
    """
    points = set()
    if sys.platform.startswith('linux'):
        try:
            with open('/proc/self/mountinfo', encoding='utf-8', errors='replace') as handle:
                for line in handle:
                    fields = line.split()
                    if len(fields) > 4:
                        points.add(_unescape_mountinfo(fields[4]))
        except OSError:
            pass
        return points
    try:
        result = subprocess.run(['/sbin/mount'], stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return points
    for line in result.stdout.splitlines():
        # "<device> on <mount point> (<type>, ...)"
        _device, sep, rest = line.partition(' on ')
        if sep:
            points.add(rest.rsplit(' (', 1)[0])
    return points


def _check_mounts(path):
    if path is None:
        return
    base = str(path).rstrip('/')
    for point in _mount_points():
        if point == base or point.startswith(base + '/'):
            raise UnsafeTree('safe_rmtree_mount_point')


def rmtree_at(parent_fd, name, *, uid=None, path=None):
    """Remove ``name`` (a real directory under ``parent_fd``) and its contents.

    ``uid`` constrains only the top directory; links inside are unlinked, not
    followed. ``path`` (the absolute path of the target) enables the mount
    table check. The whole tree is scanned for a device change first, so a
    mount point refuses before any entry is removed.
    """
    _check_mounts(path)
    fd = open_at(parent_fd, name, uid=uid)
    try:
        device = _dev(os.fstat(fd))
        if device != _dev(os.fstat(parent_fd)):
            raise UnsafeTree('safe_rmtree_mount_point')
        _walk(fd, device, remove=False)
        _walk(fd, device, remove=True)
    finally:
        os.close(fd)
    os.rmdir(name, dir_fd=parent_fd)


def _walk(fd, device, *, remove):
    with os.scandir(fd) as entries:
        names = [(entry.name, entry.is_dir(follow_symlinks=False)) for entry in entries]
    for child, is_dir in names:
        if is_dir:
            sub = open_at(fd, child)
            try:
                if _dev(os.fstat(sub)) != device:
                    raise UnsafeTree('safe_rmtree_mount_point')
                _walk(sub, device, remove=remove)
            finally:
                os.close(sub)
            if remove:
                os.rmdir(child, dir_fd=fd)
        elif remove:
            os.unlink(child, dir_fd=fd)

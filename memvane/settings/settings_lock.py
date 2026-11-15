"""The project settings writer lock: ``.memvane/.install-update.lock``.

The lock lives in the project's active folder (``project_dir.folder``: a
project not moved yet keeps ``.auto-context/``). Every writer of the
project's ``settings.json`` (learning profile activation
and recovery) holds this lock. Older installed bundles take the same file, so
its name stays fixed for cross-version mutual exclusion.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import os
import stat
from pathlib import Path

LOCK_NAME = '.install-update.lock'


def _private_auto(root, *, create=False):
    from memvane.settings import project_dir
    path = project_dir.folder(root)
    if create and not path.exists():
        path.mkdir(mode=0o700)
    if path.is_symlink() or (path.exists() and (not path.is_dir() or path.stat().st_uid != os.getuid()
                                                  or path.stat().st_mode & 0o022)):
        raise ValueError('unsafe_project_settings_dir')
    if not path.is_dir():
        raise ValueError('missing_project_settings_dir')
    return path


@contextmanager
def locked(root, *, folder=None):
    """Hold the exclusive settings writer lock of one project."""
    from memvane.settings import project_dir
    root = Path(root).resolve(strict=True)
    auto = _private_auto(root, create=True) if folder is None else Path(folder)
    if auto not in tuple(root / name for name in project_dir.NAMES):
        raise ValueError('unsafe_project_settings_dir')
    directory = auto.lstat()
    if (not stat.S_ISDIR(directory.st_mode) or directory.st_uid != os.getuid()
            or directory.st_mode & 0o022 or auto.resolve(strict=True) != auto):
        raise ValueError('unsafe_project_settings_dir')
    path = auto / LOCK_NAME
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid != os.getuid() or info.st_mode & 0o077):
            raise ValueError('unsafe_install_update_lock')
        fcntl.flock(fd, fcntl.LOCK_EX)
        acquired = os.fstat(fd)
        named = path.lstat()
        parent = auto.lstat()
        if (not stat.S_ISREG(acquired.st_mode) or acquired.st_uid != os.getuid()
                or acquired.st_mode & 0o077 or acquired.st_nlink != 1
                or not stat.S_ISREG(named.st_mode)
                or (info.st_dev, info.st_ino) != (named.st_dev, named.st_ino)
                or not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid()
                or parent.st_mode & 0o022
                or (directory.st_dev, directory.st_ino) != (parent.st_dev, parent.st_ino)
                or auto.resolve(strict=True) != auto
                or (folder is None and project_dir.folder(root) != auto)):
            raise ValueError('changed_install_update_lock')
        yield auto
    finally:
        os.close(fd)

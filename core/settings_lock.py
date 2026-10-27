"""The project settings writer lock: ``.auto-context/.install-update.lock``.

Every writer of ``.auto-context/settings.json`` (learning profile activation
and recovery) holds this lock. Older installed bundles take the same file, so
its name stays fixed for cross-version mutual exclusion.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path

LOCK_NAME = '.install-update.lock'


def _private_auto(root, *, create=False):
    path = Path(root) / '.auto-context'
    if create and not path.exists():
        path.mkdir(mode=0o700)
    if path.is_symlink() or (path.exists() and (not path.is_dir() or path.stat().st_uid != os.getuid()
                                                  or path.stat().st_mode & 0o022)):
        raise ValueError('unsafe_project_settings_dir')
    if not path.is_dir():
        raise ValueError('missing_project_settings_dir')
    return path


@contextmanager
def locked(root):
    """Hold the exclusive settings writer lock of one project."""
    auto = _private_auto(root, create=True)
    fd = os.open(auto / LOCK_NAME, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError('unsafe_install_update_lock')
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield auto
    finally:
        os.close(fd)

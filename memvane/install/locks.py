"""Private install owner lock."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import os
import stat

from memvane.install.types import InstallBlocked


@contextmanager
def install_lock(parent):
    target = parent / 'laya-install.lock'
    fd = os.open(target, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o077):
            raise InstallBlocked('unsafe_install_lock')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise InstallBlocked('install_busy') from exc
        yield
    finally:
        os.close(fd)

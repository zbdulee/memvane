"""Reentrant process-local wrapper around the project wiki cutover flock.

Lock order: a writer's own card/ledger lock, then this wiki lock. Setup holds
its install journal lock, then this wiki lock. No writer takes either outer
lock while already holding this one.
"""
from contextlib import contextmanager, nullcontext
import fcntl
from functools import wraps
import inspect
import os
from pathlib import Path
import stat
import threading

_local = threading.local()


def _project_for_path(path):
    path = Path(path).absolute()
    for parent in (path, *path.parents):
        if parent.name == 'wiki' and parent.parent.name in ('.memvane', '.auto-context'):
            return parent.parent.parent
    return None


def for_path(path):
    root = _project_for_path(path)
    return lock(root) if root is not None else nullcontext()


@contextmanager
def lock(root):
    root = Path(root).resolve()
    held = getattr(_local, 'held', None)
    if held is None:
        held = {}
        _local.held = held
    key = str(root)
    if key in held:
        fd, depth = held[key]
        held[key] = (fd, depth + 1)
        try:
            yield
        finally:
            held[key] = (fd, depth)
        return
    # Every installed bundle version locks this same file name; renaming it
    # would let an older bundle and a newer one mutate the wiki concurrently.
    path = root / '.topical-publish.lock'
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError('unsafe_publish_lock')
        fcntl.flock(fd, fcntl.LOCK_EX)
        held[key] = (fd, 1)
        try:
            yield
        finally:
            del held[key]
    finally:
        os.close(fd)


def guard_path(func):
    """Hold the project lock across a wiki read-modify-write helper."""
    first = next(iter(inspect.signature(func).parameters))
    @wraps(func)
    def wrapped(*args, **kwargs):
        path = args[0] if args else kwargs[first]
        with for_path(path):
            return func(*args, **kwargs)
    return wrapped

"""Owner-only state, lock and atomic JSON primitives; stdlib only.

Moved unchanged from ``context_learning`` so the runtime queue can use them
without loading the learning package. ``resident``, ``resource_coordination``
and ``local_cycle`` re-export these same function objects. No coordinator,
admission policy, default root or service registration lives here.
"""
import json
import os
from pathlib import Path
import stat
import sys
import tempfile

MAX_FILE = 16 * 1024 * 1024


def platform_temp_roots():
    """Fixed platform temp roots (realpath): /tmp, /var/tmp, the Darwin user temp dir.

    TMPDIR is never trusted here: these roots decide where a test runtime root
    may live and which missing project is a removed test fixture, so a TMPDIR
    pointing at a real directory must not widen them.
    """
    roots = ['/tmp', '/var/tmp', _darwin_user_temp_dir()]
    resolved = set()
    for root in roots:
        if root:
            real = os.path.realpath(root)
            if os.path.isdir(real) and real != '/':
                resolved.add(Path(real))
    return resolved


_DARWIN_USER_TEMP = []


def _darwin_user_temp_dir():
    """``confstr(_CS_DARWIN_USER_TEMP_DIR)`` (the per-user /var/folders/../T), or None.

    Python's ``os.confstr`` does not name this key, so libc is asked directly.
    The answer never depends on TMPDIR.
    """
    if not _DARWIN_USER_TEMP:
        value = None
        if sys.platform == 'darwin':
            try:
                import ctypes
                libc = ctypes.CDLL(None, use_errno=True)
                buffer = ctypes.create_string_buffer(1024)
                size = libc.confstr(65537, buffer, len(buffer))  # _CS_DARWIN_USER_TEMP_DIR
                if 0 < size <= len(buffer):
                    value = buffer.value.decode() or None
            except (OSError, AttributeError, ValueError, UnicodeDecodeError):
                value = None
        _DARWIN_USER_TEMP.append(value)
    return _DARWIN_USER_TEMP[0]


def trusted_temp_dir():
    """The per-user platform temp dir (Darwin) or /tmp: a place inside the roots above."""
    for candidate in (_darwin_user_temp_dir(), '/tmp'):
        if candidate and os.path.isdir(os.path.realpath(candidate)):
            return os.path.realpath(candidate)
    raise OSError('no_platform_temp_dir')


def in_platform_temp(path):
    """Whether ``path`` (realpath) lies strictly inside a fixed platform temp root."""
    try:
        real = Path(os.path.realpath(path))
    except (OSError, ValueError, TypeError):
        return False
    return any(real != root and real.is_relative_to(root) for root in platform_temp_roots())


def private_state(state):
    state = Path(state)
    if not state.is_absolute() or state.is_symlink() or not state.is_dir():
        raise ValueError('resident_state_unsafe')
    info = state.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError('resident_state_unsafe')
    return state


def private_lock(path):
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        os.close(fd)
        raise ValueError('resident_lock_unsafe')
    return fd


def coordination_lock(path):
    fd = private_lock(path)
    if os.fstat(fd).st_nlink != 1:
        os.close(fd); raise ValueError('unsafe_coordination_lock')
    return fd


def atomic_json(path, value):
    path = Path(path)
    # Same bytes as context_learning.store.canonical plus a trailing newline.
    data = (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(',', ':'))+'\n').encode()
    if len(data) > MAX_FILE: raise ValueError('output_budget_exceeded')
    fd, name = tempfile.mkstemp(prefix='.write-', dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'wb') as out:
            out.write(data); out.flush(); os.fsync(out.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name): os.unlink(name)

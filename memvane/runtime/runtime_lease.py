"""Owner-only state, lock and atomic JSON primitives; stdlib only.

Moved unchanged from ``context_learning`` so the runtime queue can use them
without loading the learning package. ``resident``, ``resource_coordination``
and ``local_cycle`` re-export these same function objects; ``private_state``,
``private_lock`` and ``atomic_json`` are defined in ``kernel.atomic_rename``. No
coordinator, admission policy, default root or service registration lives here.
"""
import os
from pathlib import Path
import sys

from memvane.kernel.atomic_rename import MAX_FILE, atomic_json, private_lock, private_state


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


def coordination_lock(path):
    fd = private_lock(path)
    if os.fstat(fd).st_nlink != 1:
        os.close(fd); raise ValueError('unsafe_coordination_lock')
    return fd

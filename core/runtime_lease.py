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
import tempfile

MAX_FILE = 16 * 1024 * 1024


def platform_temp_roots():
    """Resolved platform temp roots: /tmp, /var/tmp, /var/folders, TMPDIR, Darwin user temp."""
    roots = {'/tmp', '/var/tmp', '/var/folders'}
    try:
        roots.add(tempfile.gettempdir())
    except (OSError, RuntimeError):
        pass
    try:
        roots.add(os.confstr('CS_DARWIN_USER_TEMP_DIR'))
    except (AttributeError, ValueError, OSError):
        pass
    resolved = set()
    for root in roots:
        if root:
            try:
                resolved.add(Path(root).resolve())
            except (OSError, RuntimeError):
                pass
    return resolved


def in_platform_temp(path):
    """Whether ``path`` (resolved) lies in the platform temp area."""
    try:
        path = Path(path).resolve()
    except (OSError, RuntimeError, ValueError):
        return False
    return any(path.is_relative_to(root) for root in platform_temp_roots())


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

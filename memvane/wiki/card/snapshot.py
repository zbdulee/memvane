"""Bounded, stable file snapshots for card and learning reads."""
from __future__ import annotations

import hashlib
import os
import stat


def bounded_snapshot(path):
    # Bound bytes before decoding or hashing, including concurrent growth.
    fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as handle:
        before = os.fstat(handle.fileno())
        if not stat.S_ISREG(before.st_mode):
            return None
        content = handle.read(65537)
        after = os.fstat(handle.fileno())
    if len(content) > 65536 or (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
        return None
    return {"sha256": hashlib.sha256(content).hexdigest()}, content

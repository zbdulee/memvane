"""Bounded, nonblocking source snapshot below an already selected project root."""
from __future__ import annotations

import os
from pathlib import Path
import stat
import time

from .compact_input import MAX_SOURCE_BYTES


def snapshot_beneath(root, relative, *, deadline=None):
    """Reject links, devices, oversized files and concurrent replacement."""
    parts = Path(relative).parts
    if (not parts or Path(relative).is_absolute()
            or any(part in ("", ".", "..") for part in parts)):
        raise ValueError("unsafe_source_path")
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    descriptors = []
    try:
        if deadline is not None and time.monotonic() >= deadline:
            raise ValueError("source_deadline")
        parent = os.open(root, flags | os.O_DIRECTORY)
        descriptors.append(parent)
        for part in parts[:-1]:
            if deadline is not None and time.monotonic() >= deadline:
                raise ValueError("source_deadline")
            parent = os.open(part, flags | os.O_DIRECTORY, dir_fd=parent)
            descriptors.append(parent)
        fd = os.open(parts[-1], flags, dir_fd=parent)
        descriptors.append(fd)
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_SOURCE_BYTES:
            raise ValueError("unsafe_source_file")
        content = bytearray()
        while len(content) <= MAX_SOURCE_BYTES:
            if deadline is not None and time.monotonic() >= deadline:
                raise ValueError("source_deadline")
            block = os.read(fd, min(65536, MAX_SOURCE_BYTES + 1 - len(content)))
            if not block:
                break
            content.extend(block)
        after = os.fstat(fd)
        identity = lambda info: (info.st_dev, info.st_ino, info.st_size,
                                 info.st_mtime_ns, info.st_ctime_ns)
        if (len(content) > MAX_SOURCE_BYTES or len(content) != before.st_size
                or identity(before) != identity(after)):
            raise ValueError("source_changed_during_read")
        return bytes(content)
    finally:
        for fd in reversed(descriptors):
            os.close(fd)

"""Canonical filesystem path spelling."""
from __future__ import annotations

import os
from pathlib import Path
try:
    import fcntl
except ImportError:
    fcntl = None


def canonical(path) -> Path:
    """``path`` resolved, in the file system's spelling where it differs."""
    resolved = Path(path).resolve()
    getpath = getattr(fcntl, 'F_GETPATH', None) if fcntl is not None else None
    if getpath is not None:
        try:
            fd = os.open(resolved, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        except OSError:
            return Path(os.path.normcase(str(resolved)))
        try:
            # MAXPATHLEN on macOS; fcntl caps the buffer at 1024 bytes.
            raw = fcntl.fcntl(fd, getpath, bytes(1024))
        except (OSError, ValueError):
            raw = None
        finally:
            os.close(fd)
        if raw:
            spelled = os.fsdecode(raw.split(b'\0', 1)[0])
            if spelled and os.path.isabs(spelled):
                return Path(spelled)
    return Path(os.path.normcase(str(resolved)))

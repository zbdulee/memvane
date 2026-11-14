"""Compare persisted filesystem evidence with live observations.

APFS device numbers are boot-local. Only the explicitly named device fields
are masked; callers retain the original records and their content digests.
Live fd/path and before/after observations must use their full identities.
"""
from __future__ import annotations

from collections.abc import Mapping
import os
import stat

# (digest function, path, inode, size, mtime_ns) -> digest of a file this
# process already read; see ``cached_by_identity``.
_DIGESTS = {}
_DIGESTS_MAX = 65536


def cached_by_identity(path, digest):
    """``digest(path)``, read again only when the file's (path, inode, size, mtime_ns) moves.

    A path that is not a regular file goes to ``digest`` as is, so its own
    error names it. The caller still compares the digest on every use.
    """
    try:
        info = os.lstat(path)
    except (OSError, TypeError, ValueError):
        return digest(path)
    if not stat.S_ISREG(info.st_mode):
        return digest(path)
    key = (digest, str(path), info.st_ino, info.st_size, info.st_mtime_ns)
    if key in _DIGESTS:
        return _DIGESTS[key]
    value = digest(path)
    after = os.lstat(path)
    if (after.st_ino, after.st_size, after.st_mtime_ns) == key[2:]:
        if len(_DIGESTS) >= _DIGESTS_MAX:
            _DIGESTS.clear()
        _DIGESTS[key] = value
    return value


def persistent_identity_equal(stored, live, *, device_index=None, device_fields=('dev',)):
    if device_index is not None:
        return (isinstance(stored, (list, tuple)) and isinstance(live, (list, tuple))
                and len(stored) == len(live) and 0 <= device_index < len(stored)
                and all(a == b for i, (a, b) in enumerate(zip(stored, live))
                        if i != device_index))
    return (isinstance(stored, Mapping) and isinstance(live, Mapping)
            and stored.keys() == live.keys()
            and all(stored[key] == live[key] for key in stored if key not in device_fields))


def persistent_node_equal(stored, live):
    return (isinstance(stored, (list, tuple)) and len(stored) == 2
            and persistent_identity_equal(stored, live, device_index=0))


def persistent_provider_equal(stored, live):
    return (isinstance(stored, (list, tuple)) and isinstance(live, (list, tuple))
            and len(stored) == len(live)
            and all(isinstance(a, (list, tuple)) and len(a) == 6
                    and persistent_identity_equal(a, b, device_index=1)
                    for a, b in zip(stored, live)))

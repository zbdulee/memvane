"""Runtime file digests that later processes reuse while a file is unchanged.

Each Laya embed runtime root keeps one owner-only cache file beside its
generations (``embed-runtime/file-digests.json``, 0600, replaced atomically
inside the 0700 root). A row maps ``<hasher name> NUL <path>`` to ``[inode,
size, mtime_ns, ctime_ns, digest]`` and answers only while the file's live
identity equals it. Device numbers are boot-local on APFS and never enter it;
ctime_ns is set by the kernel on every write or utime, so a same-user process
cannot put it back the way it can put back mtime.

The file is a cache, not evidence: a missing, unsafe or corrupt one reads as
empty, a failed write only means the files are hashed again, and every caller
still compares each digest with the sealed runtime.json or the attestation.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat

CACHE_FILE = 'file-digests.json'
SCHEMA = 'memvane-file-digests-v1'
MAX_ROWS = 65536
MAX_BYTES = 16 * 1024 * 1024
# One wrapper per (cache file, hasher name, hasher), so the in-process cache
# (``kernel.persistent_identity``) keys on a stable digest function.
_WRAPPERS = {}


def _identity(info):
    return [info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def _value(raw):
    """A digest as the hashers return it: a str, or a tuple of str/int."""
    if isinstance(raw, str):
        return raw
    if isinstance(raw, (list, tuple)) and raw and all(
            isinstance(item, str) or type(item) is int for item in raw):
        return tuple(raw)
    return None


def _private_dir(path):
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return (stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
            and not stat.S_IMODE(info.st_mode) & 0o077)


class Digests:
    """The rows of one cache file, loaded once and written once by ``flush``."""

    def __init__(self, path):
        self.path = None if path is None else Path(path)
        self.rows = None
        self.used = set()
        self.changed = False

    def _load(self):
        if self.rows is not None:
            return self.rows
        self.rows = {}
        if self.path is None or not _private_dir(self.path.parent):
            return self.rows
        try:
            fd = os.open(self.path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        except OSError:
            return self.rows
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o077
                    or info.st_size > MAX_BYTES):
                return self.rows
            with os.fdopen(os.dup(fd), 'rb') as source:
                document = json.loads(source.read(MAX_BYTES + 1).decode('utf8'))
        except (OSError, ValueError, UnicodeError, RecursionError):
            return self.rows
        finally:
            os.close(fd)
        rows = document.get('rows') if isinstance(document, dict) else None
        if not isinstance(rows, dict) or document.get('schema') != SCHEMA:
            return self.rows
        for key, row in rows.items():
            if (isinstance(row, list) and len(row) == 5
                    and all(type(item) is int for item in row[:4]) and _value(row[4]) is not None):
                self.rows[key] = row
        return self.rows

    def lookup(self, key, info):
        row = self._load().get(key)
        if row is None or row[:4] != _identity(info):
            return None
        self.used.add(key)
        return _value(row[4])

    def store(self, key, info, value):
        value = _value(value)
        if self.path is None or value is None:
            return
        self._load()[key] = _identity(info) + [value if isinstance(value, str) else list(value)]
        self.used.add(key)
        self.changed = True

    def flush(self):
        """Write the rows once if any changed; a failure only costs a later re-hash."""
        if not self.changed or not _private_dir(self.path.parent):
            return
        from memvane.kernel.atomic_rename import atomic_json
        rows = self._load()
        if len(rows) > MAX_ROWS:
            kept = sorted(rows, key=lambda key: key not in self.used)[:MAX_ROWS]
            rows = {key: rows[key] for key in kept}
        try:
            atomic_json(self.path, {'schema': SCHEMA, 'rows': rows})
        except (OSError, ValueError):
            # Cache only: the files are hashed again by a later process.
            return
        self.changed = False


def cache_for(path):
    """The digest cache of the runtime root holding ``path`` (a generation or a
    file in one); a cache that stores nothing outside a Laya generation."""
    from memvane.provider.embed_runtime import MANIFEST, generation_of
    raw = os.fspath(path) if path is not None else None
    generation = generation_of(raw) if isinstance(raw, str) else None
    if generation is None and isinstance(raw, str):
        generation = generation_of(raw + '/' + MANIFEST)
    if generation is None:
        return Digests(None)
    return Digests(generation.parent.parent / CACHE_FILE)


def digest(path, hasher, cache, name):
    """``hasher(path)``, reused from this process or from ``cache`` while the
    file's identity is unchanged; the caller flushes ``cache``."""
    from memvane.kernel.persistent_identity import cached_by_identity
    token = (str(cache.path), name, hasher)
    holder = _WRAPPERS.get(token)
    if holder is None:
        if len(_WRAPPERS) >= 64:
            _WRAPPERS.clear()
        holder = _WRAPPERS[token] = [None, cache]
        holder[0] = lambda target: _persisted(target, hasher, holder[1], name)
    holder[1] = cache
    return cached_by_identity(path, holder[0])


def _persisted(path, hasher, cache, name):
    key = name + '\0' + str(path)
    try:
        info = os.lstat(path)
    except OSError:
        return hasher(path)
    value = cache.lookup(key, info) if stat.S_ISREG(info.st_mode) else None
    if value is not None:
        return value
    value = hasher(path)
    try:
        after = os.lstat(path)
    except OSError:
        return value
    if stat.S_ISREG(info.st_mode) and _identity(after) == _identity(info):
        cache.store(key, info, value)
    return value

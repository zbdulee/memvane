"""Inactive SQLite index and private publication pointer primitives.

Nothing in the operating QMD route calls this module yet. Callers must provide
an explicit owner-private root; this never selects the installed shared store,
starts QMD, or changes its active.json pointer.
"""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
import uuid

import published_view
import sqlite_read


GENERATION = re.compile(r'g-[0-9a-f]{32}\Z')
VIEW_KEYS = frozenset(('owner', 'incarnation', 'worktree', 'scopeRevision',
                       'storeGeneration', 'contentRevision', 'indexGeneration'))


def _private_dir(path):
    path = Path(path)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError('unsafe_shadow_index_directory')
    return path


def _private_file(path):
    path = Path(path)
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077 or info.st_nlink != 1):
        raise ValueError('unsafe_shadow_index_file')
    return path


def backup_inactive(source, root):
    """Copy one committed live-WAL snapshot into a fresh private candidate.

    A failed copy is left unreferenced for explicit cleanup. No publication or
    queue ACK happens here. QMD candidate updates, if later authorized, must
    use this candidate's explicit INDEX_PATH and a separately checked config.
    """
    root = _private_dir(root)
    candidates = root / 'candidates'
    candidates.mkdir(mode=0o700, exist_ok=True)
    _private_dir(candidates)
    generation = 'g-' + uuid.uuid4().hex
    folder = candidates / generation
    folder.mkdir(mode=0o700)
    index = folder / 'index.sqlite'
    fd = os.open(index, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    os.close(fd)
    with sqlite_read.connect(source) as live:
        candidate = sqlite3.connect(index)
        try:
            live.backup(candidate)
        finally:
            candidate.close()
    with index.open('rb') as stream:
        os.fsync(stream.fileno())
    directory = os.open(folder, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return {'generation': generation, 'index': str(index)}


def seal_candidate(root, generation):
    """Return an integrity proof after all candidate-only writes have ended."""
    root = _private_dir(root)
    if not isinstance(generation, str) or not GENERATION.fullmatch(generation):
        raise ValueError('invalid_shadow_index_generation')
    index = _private_file(_private_dir(_private_dir(root / 'candidates') /
                                       generation) / 'index.sqlite')
    with sqlite_read.connect(index) as db:
        if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise ValueError('invalid_shadow_index_candidate')
    return sqlite_read.snapshot_fingerprint(index)


def publish_candidate(root, expected, prepared, sealed):
    """CAS a separate private pointer after rechecking a sealed candidate.

    The lock and atomic rename fence competing *callers of this adapter*.
    Operating QMD writers and daemons do not use it yet. A rejected CAS keeps
    the old pointer and leaves the caller's queue claim unacknowledged.
    """
    root = _private_dir(root)
    if (not isinstance(expected, dict) or not isinstance(prepared, dict)
            or set(expected) != VIEW_KEYS or set(prepared) != VIEW_KEYS
            or not all(isinstance(row['indexGeneration'], str)
                       and GENERATION.fullmatch(row['indexGeneration'])
                       for row in (expected, prepared))
            or expected['indexGeneration'] == prepared['indexGeneration']):
        raise ValueError('invalid_shadow_index_view')
    lock = root / '.published-view.lock'
    fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        _private_file(lock)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('shadow_index_publication_busy') from None
        pointer = _private_file(root / 'published-view.json')
        if pointer.stat().st_size > 65536:
            raise ValueError('invalid_shadow_index_pointer')
        current = json.loads(pointer.read_text())
        if not isinstance(current, dict) or set(current) != VIEW_KEYS:
            raise ValueError('invalid_shadow_index_pointer')
        decision = published_view.publication_decision(current, expected, prepared)
        if decision is None:
            return None
        # Hold SQLite's writer reservation through the rename, so even a
        # candidate writer racing the seal cannot change the published bytes.
        with sqlite_read.cutover_guard(
                _private_file(_private_dir(_private_dir(root / 'candidates') /
                                           prepared['indexGeneration']) / 'index.sqlite'),
                sealed):
            temporary_fd, temporary_name = tempfile.mkstemp(prefix='.published-', dir=root)
            try:
                os.fchmod(temporary_fd, 0o600)
                with os.fdopen(temporary_fd, 'w') as output:
                    json.dump(decision, output, sort_keys=True, separators=(',', ':'))
                    output.write('\n')
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary_name, pointer)
                directory = os.open(root, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            finally:
                Path(temporary_name).unlink(missing_ok=True)
        return decision
    finally:
        os.close(fd)

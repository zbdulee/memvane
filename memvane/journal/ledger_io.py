"""Private canonical ledger file operations."""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import tempfile

from memvane.journal.types import CutoverBlocked
from memvane.kernel.atomic_rename import fsync_dir as _fsync_dir


def _fail(reason):
    raise CutoverBlocked(reason)

def _canon(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(',', ':')).encode('utf-8')

def _private_file(path, limit=65536):
    path = Path(path)
    if path.is_symlink():
        _fail('cutover_symlink')
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                or before.st_nlink != 1 or stat.S_IMODE(before.st_mode) & 0o077
                or before.st_size > limit):
            _fail('cutover_file_unsafe')
        raw = os.read(fd, limit + 1)
        after = os.fstat(fd)
        if (len(raw) != before.st_size or
                (before.st_dev, before.st_ino, before.st_mtime_ns, before.st_size)
                != (after.st_dev, after.st_ino, after.st_mtime_ns, after.st_size)):
            _fail('cutover_file_changed')
        return raw
    except OSError as exc:
        raise CutoverBlocked('cutover_file_unavailable') from exc
    finally:
        os.close(fd)

def _read_json(path):
    try:
        raw = _private_file(path)
        def unique(pairs):
            value = {}
            for key, item in pairs:
                if key in value:
                    _fail('cutover_record_duplicate_key')
                value[key] = item
            return value
        def invalid_constant(_value):
            _fail('cutover_record_nonfinite')
        parsed = json.loads(raw.decode('utf-8'), object_pairs_hook=unique,
                            parse_constant=invalid_constant)
        if raw != _canon(parsed):
            _fail('cutover_record_noncanonical')
        return parsed
    except (UnicodeError, ValueError, TypeError) as exc:
        raise CutoverBlocked('cutover_record_invalid') from exc


def _write(path, value, *, replace):
    path = Path(path)
    fd, name = tempfile.mkstemp(prefix='.laya-cutover-', dir=path.parent)
    temp = Path(name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(_canon(value) if not isinstance(value, bytes) else value)
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            if path.is_symlink():
                _fail('cutover_symlink')
            os.replace(temp, path)
        else:
            os.link(temp, path)
        _fsync_dir(path.parent)
    except FileExistsError as exc:
        raise CutoverBlocked('cutover_record_exists') from exc
    finally:
        temp.unlink(missing_ok=True)

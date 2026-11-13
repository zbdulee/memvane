"""Read and validate initial build records."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat

from memvane.kernel.errors import IndexContractError
from memvane.journal.types import MAX_RECORD_BYTES, V1_BUILD_SCHEMA, V1_BUILD_FILE, V1_BUILD_IDENTITY


def _fail(reason):
    raise IndexContractError(reason)

def _canon(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def _private_dir(path):
    path = Path(path)
    try:
        info = path.lstat()
    except OSError:
        _fail('initial_build_parent_unavailable')
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077 or not path.is_absolute()):
        _fail('initial_build_parent_unsafe')
    return path


def _read_json(path, *, max_bytes=MAX_RECORD_BYTES):
    """One owned, private, single-link canonical JSON object; None if absent."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    except FileNotFoundError:
        return None
    except OSError:
        _fail('initial_build_record_unavailable')
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                or before.st_nlink != 1 or stat.S_IMODE(before.st_mode) & 0o077
                or before.st_size > max_bytes):
            _fail('initial_build_record_unsafe')
        raw = os.read(fd, max_bytes + 1)
        after = os.fstat(fd)
    finally:
        os.close(fd)
    if (len(raw) != before.st_size or
            (before.st_ino, before.st_size, before.st_mtime_ns) !=
            (after.st_ino, after.st_size, after.st_mtime_ns)):
        _fail('initial_build_record_changed')
    try:
        value = json.loads(raw.decode('utf-8'))
    except (UnicodeError, ValueError):
        _fail('initial_build_record_invalid')
    if not isinstance(value, dict) or _canon(value) != raw:
        _fail('initial_build_record_invalid')
    return value


def read_v1(state_root):
    """Read a v1 build record without opening its store."""
    state = _private_dir(state_root)
    record = _read_json(state / V1_BUILD_FILE)
    if record is None:
        return None
    if (not isinstance(record, dict)
            or set(record) != {'schema', 'epoch', 'phase', 'identity', 'reason', 'updatedNs'}
            or record['schema'] != V1_BUILD_SCHEMA
            or type(record['epoch']) is not int or record['epoch'] < 1
            or record['phase'] not in ('registered', 'building', 'ready')
            or (record['reason'] is not None and
                (type(record['reason']) is not str or len(record['reason']) > 256))
            or type(record['updatedNs']) is not int or record['updatedNs'] < 1):
        _fail('initial_build_invalid')
    identity = record['identity']
    if (not isinstance(identity, dict) or set(identity) != V1_BUILD_IDENTITY
            or identity['stateRoot'] != str(state)
            or identity['storePath'] != str(state / 'store.sqlite')
            or identity['journalPath'] != str(state / 'journal.sqlite')
            or any(type(identity[k]) is not str or not re.fullmatch(r'[0-9a-f]{32}', identity[k])
                   for k in ('installId', 'scopeId', 'storeIncarnation'))
            or any(type(identity[k]) is not str or not re.fullmatch(r'[0-9a-f]{64}', identity[k])
                   for k in ('routeSha256', 'attestationSha256'))
            or type(identity['projectRoot']) is not str or not Path(identity['projectRoot']).is_absolute()
            or not isinstance(identity['storeNode'], list) or len(identity['storeNode']) != 2
            or any(type(n) is not int or n < 0 for n in identity['storeNode'])):
        _fail('initial_build_identity_invalid')
    return record

"""Read-only evidence check for a detached Laya Index rollback backup.

The caller supplies a non-operating backup directory and externally captured
source-selection evidence. This module never opens the source DB, creates a
backup, switches a pointer, or authorizes rollback after new writes. Hashing
and SQLite integrity checks are substantial work: run through the repository
development guard. An isolated restore copy is checked, not created here.
"""
from __future__ import annotations

import hashlib
from contextlib import closing
import json
import os
from pathlib import Path
import re
import sqlite3
import stat

from laya_index_a import IndexContractError, LayaIndexA


SCHEMA = 'laya-rollback-backup-v1'
APP_ID = LayaIndexA.APP_ID
SCHEMA_VERSION = LayaIndexA.SCHEMA_VERSION
MAX_DB_BYTES = 8 * 1024 * 1024 * 1024
SHA256 = re.compile(r'[0-9a-f]{64}\Z')
ID = re.compile(r'[0-9a-f]{32}\Z')
GENERATION = re.compile(r'g-[0-9a-f]{32}\Z')
_FLAGS = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_CLOEXEC', 0)


class BackupEvidenceError(ValueError):
    pass


def _fail(reason):
    raise BackupEvidenceError(reason)


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _fail('duplicate_backup_receipt_key')
        result[key] = value
    return result


def _nonfinite(_value):
    _fail('nonfinite_backup_receipt_number')


def _read_json(path):
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 1024 * 1024:
            _fail('unsafe_backup_receipt')
        fd = os.open(path, _FLAGS)
        try:
            chunks, size = [], 0
            while True:
                chunk = os.read(fd, min(65536, 1024 * 1024 + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > 1024 * 1024:
                    _fail('unsafe_backup_receipt')
            after = os.fstat(fd)
            now = path.lstat()
            identity = lambda s: (s.st_dev, s.st_ino, s.st_nlink, s.st_size,
                                  s.st_mtime_ns, s.st_ctime_ns)
            if identity(after) != identity(info) or identity(now) != identity(info):
                _fail('backup_receipt_changed')
            body = b''.join(chunks)
            return (json.loads(body.decode('utf-8'), object_pairs_hook=_unique_pairs,
                               parse_constant=_nonfinite),
                    {'sha256': hashlib.sha256(body).hexdigest(), 'dev': info.st_dev,
                     'ino': info.st_ino, 'mtimeNs': info.st_mtime_ns,
                     'ctimeNs': info.st_ctime_ns})
        finally:
            os.close(fd)
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise BackupEvidenceError('invalid_backup_receipt') from exc


def _snapshot(path):
    try:
        if any(Path(str(path) + suffix).exists() or Path(str(path) + suffix).is_symlink()
               for suffix in ('-wal', '-shm', '-journal')):
            _fail('backup_has_sidecar')
        before = path.lstat()
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or
                before.st_size > MAX_DB_BYTES):
            _fail('unsafe_backup_db')
        fd = os.open(path, _FLAGS)
        try:
            opened = os.fstat(fd)
            if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                _fail('backup_changed')
            digest = hashlib.sha256()
            size = 0
            while True:
                chunk = os.read(fd, 1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_DB_BYTES:
                    _fail('backup_too_large')
                digest.update(chunk)
            after = os.fstat(fd)
            now = path.lstat()
            identity = lambda s: (s.st_dev, s.st_ino, s.st_nlink, s.st_size,
                                  s.st_mtime_ns, s.st_ctime_ns)
            if identity(before) != identity(after) or identity(before) != identity(now):
                _fail('backup_changed')
            return {'sha256': digest.hexdigest(), 'bytes': size,
                    'dev': before.st_dev, 'ino': before.st_ino,
                    'mtimeNs': before.st_mtime_ns, 'ctimeNs': before.st_ctime_ns}
        finally:
            os.close(fd)
    except OSError as exc:
        raise BackupEvidenceError('backup_unavailable') from exc


def _inspect_db(path, expected_heads, expected_incarnation):
    # immutable is permitted only because no sidecar was accepted above.
    try:
        uri = path.resolve(strict=True).as_uri() + '?mode=ro&immutable=1'
        with closing(sqlite3.connect(uri, uri=True, timeout=2)) as db:
            db.execute('PRAGMA query_only=ON')
            if (db.execute('PRAGMA application_id').fetchone()[0] != APP_ID or
                    db.execute('PRAGMA user_version').fetchone()[0] != SCHEMA_VERSION):
                _fail('backup_schema_unknown')
            if db.execute('PRAGMA integrity_check').fetchone() != ('ok',):
                _fail('backup_integrity_failed')
            if db.execute('PRAGMA foreign_key_check').fetchone() is not None:
                _fail('backup_foreign_key_failed')
            try:
                LayaIndexA._check_header(db)
                incarnation = db.execute(
                    "SELECT value FROM schema_meta WHERE key='incarnation'").fetchone()
                if incarnation is None or incarnation[0] != expected_incarnation:
                    _fail('backup_incarnation_mismatch')
                checked = object.__new__(LayaIndexA)
                checked.db = db
                checked.inspect()
            except IndexContractError as exc:
                raise BackupEvidenceError('backup_logical_integrity_failed') from exc
            heads = [list(row) for row in db.execute('SELECT scope,gen FROM head ORDER BY scope')]
            if heads != expected_heads:
                _fail('backup_heads_mismatch')
            return heads
    except (sqlite3.Error, OSError, RuntimeError) as exc:
        raise BackupEvidenceError('backup_unreadable') from exc


def preflight(backup_root, receipt_path, selected_record, *, restore_name=None):
    """Check a backup and optional distinct isolated restore copy, without writes.

    selected_record is an independently read selection receipt with installId,
    generation, bundleManifestSha256 and dbIncarnation. A matching backup is only a byte and
    SQLite snapshot check. Even a matching restore does not prove source and
    selection were captured atomically, or permit rollback after new writes.
    """
    try:
        root = Path(backup_root)
        if not root.is_absolute() or root.is_symlink() or not root.is_dir():
            _fail('unsafe_backup_root')
        root = root.resolve(strict=True)
        root_info = root.stat()
    except (OSError, RuntimeError, TypeError) as exc:
        raise BackupEvidenceError('backup_unavailable') from exc
    if root_info.st_uid != os.getuid() or stat.S_IMODE(root_info.st_mode) & 0o077:
        _fail('unsafe_backup_permissions')
    try:
        receipt_path = Path(receipt_path)
        if receipt_path.is_symlink() or receipt_path.resolve() != root / 'backup-receipt.json':
            _fail('backup_receipt_outside_root')
    except (OSError, RuntimeError, TypeError) as exc:
        raise BackupEvidenceError('backup_unavailable') from exc
    row, receipt_identity = _read_json(root / 'backup-receipt.json')
    if (not isinstance(row, dict) or not isinstance(selected_record, dict) or
            row.get('schema') != SCHEMA or
            any(type(row.get(key)) is not str or type(selected_record.get(key)) is not str or
                row[key] != selected_record[key]
                for key in ('installId', 'generation', 'bundleManifestSha256', 'dbIncarnation')) or
            not ID.fullmatch(row['installId']) or
            not GENERATION.fullmatch(row['generation']) or
            not SHA256.fullmatch(row['bundleManifestSha256']) or
            not ID.fullmatch(row['dbIncarnation']) or
            row.get('backupName') != 'index.sqlite' or
            not isinstance(row.get('expectedHeads'), list) or
            any(not isinstance(h, list) or len(h) != 2 or
                not all(type(v) is str and v for v in h)
                for h in row['expectedHeads']) or
            row['expectedHeads'] != sorted(row['expectedHeads']) or
            len({h[0] for h in row['expectedHeads']}) != len(row['expectedHeads']) or
            type(row.get('backupSha256')) is not str or
            not SHA256.fullmatch(row['backupSha256'])):
        _fail('backup_receipt_mismatch')
    backup = root / 'index.sqlite'
    proof = _snapshot(backup)
    if proof['sha256'] != row['backupSha256']:
        _fail('backup_hash_changed')
    _inspect_db(backup, row['expectedHeads'], row['dbIncarnation'])
    if _snapshot(backup) != proof:
        _fail('backup_changed')
    result = {'status': 'backup_consistent_restore_required',
              'backupSha256': proof['sha256'], 'expectedHeads': row['expectedHeads'],
              'installId': row['installId'], 'generation': row['generation'],
              'bundleManifestSha256': row['bundleManifestSha256'],
              'dbIncarnation': row['dbIncarnation'],
              'receiptIdentity': receipt_identity,
              'backupIdentity': proof,
              'rollbackAuthorized': False}
    if restore_name is not None:
        if restore_name != 'restored-index.sqlite':
            _fail('unsafe_restore_name')
        restored = root / restore_name
        restored_proof = _snapshot(restored)
        if (restored_proof['sha256'] != proof['sha256'] or
                (restored_proof['dev'], restored_proof['ino']) ==
                (proof['dev'], proof['ino'])):
            _fail('restore_not_independent_copy')
        _inspect_db(restored, row['expectedHeads'], row['dbIncarnation'])
        if _snapshot(restored) != restored_proof or _snapshot(backup) != proof:
            _fail('backup_changed')
        result['status'] = 'backup_and_restore_consistent_source_unproved'
        result['restoreIdentity'] = restored_proof
    return result

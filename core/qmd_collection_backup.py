"""Read-only inventory, backup and restore check of QMD collections.

The shared QMD store's legacy enrollment (``shared_qmd_setup``) backs up the
whole QMD SQLite DB before it re-owns existing collections, and proves the
backup restores to the same logical rows. One digest binds the selected
collections' documents, collection config and vector metadata; sqlite-vec
bytes are never read. This module never changes the source DB.

Schema references: tobi/qmd v2.5.3 src/store.ts initializeDatabase; pinned
fork f4633f92a99ffdb2d0e885ed391fd8169133d70e src/store.ts and
src/metadata-store.ts. Neither DB contains a trustworthy package version
identity, so a structure match alone cannot prove the producing binary.
"""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import time

from laya_index_a import _valid_relative_path


QMD_253 = 'qmd-2.5.3'
FORK_F463 = 'qmd-fork-f4633f9'
FORMATS = frozenset((QMD_253, FORK_F463))
_COLLECTION = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z')


class QmdBackupBlocked(RuntimeError):
    """The source, backup or restore cannot be proved safe."""


_COLUMNS = {
    'content': ('hash', 'doc', 'created_at'),
    'documents': ('id', 'collection', 'path', 'title', 'hash', 'created_at',
                  'modified_at', 'active'),
    'content_vectors': ('hash', 'seq', 'pos', 'model', 'embed_fingerprint',
                        'total_chunks', 'embedded_at'),
    'store_collections': ('name', 'path', 'pattern', 'ignore_patterns',
                          'include_by_default', 'update_command', 'context'),
    'store_config': ('key', 'value'),
    'documents_fts': ('filepath', 'title', 'body'),
}
_FORK_COLUMNS = {
    'file_sync_state': ('collection', 'relative_path', 'mtime_ms', 'size',
                        'content_hash', 'document_id'),
    'document_metadata': ('document_id', 'metadata_json', 'extraction_version',
                          'extraction_error', 'extracted_at'),
    'document_metadata_values': ('document_id', 'key', 'ordinal', 'value_type',
                                 'text_value', 'number_value', 'boolean_value'),
}


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode('utf-8')).hexdigest()


def _hash_body(body):
    try:
        return hashlib.sha256(body.encode('utf-8')).hexdigest()
    except UnicodeEncodeError as exc:
        raise QmdBackupBlocked('source_document_invalid') from exc


def _bounded_backup(source, target):
    deadline = time.monotonic() + 10

    def progress(_status, _remaining, _total):
        if time.monotonic() > deadline:
            raise QmdBackupBlocked('backup_timeout')

    source.backup(target, pages=128, progress=progress, sleep=0.05)


def _seal_single_file(path, validator):
    """Prove a closed private temp DB is complete before sidecar cleanup."""
    with closing(sqlite3.connect(path)) as db:
        mode = db.execute('PRAGMA journal_mode=DELETE').fetchone()
        if mode is None or mode[0].lower() != 'delete':
            raise QmdBackupBlocked('single_file_journal_unavailable')
        if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise QmdBackupBlocked('single_file_integrity_failed')
        if not validator(db):
            raise QmdBackupBlocked('single_file_digest_mismatch')
    # A nonempty WAL or rollback journal could carry uncheckpointed rows even
    # after a nominal mode switch; never discard it. SQLite may leave a -shm
    # mapping after the last reader closes. Only private temp paths reach here.
    for suffix in ('-wal', '-journal'):
        sidecar = Path(str(path) + suffix)
        if sidecar.exists() and sidecar.stat().st_size:
            raise QmdBackupBlocked('single_file_sidecar_not_empty')
    for suffix in ('-wal', '-shm', '-journal'):
        Path(str(path) + suffix).unlink(missing_ok=True)


def _source(path):
    path = Path(path)
    if not path.is_file() or path.is_symlink():
        raise QmdBackupBlocked('source_missing_or_symlink')
    try:
        db = sqlite3.connect(path.resolve(strict=True).as_uri() + '?mode=ro',
                             uri=True, timeout=2)
        db.execute('PRAGMA query_only=ON')
        return db
    except (OSError, sqlite3.Error) as exc:
        raise QmdBackupBlocked('source_open_failed') from exc


def _check_schema(db, source_format):
    if source_format not in FORMATS:
        raise QmdBackupBlocked('unsupported_source_version')
    app = db.execute('PRAGMA application_id').fetchone()[0]
    version = db.execute('PRAGMA user_version').fetchone()[0]
    if app != 0 or version != (0 if source_format == QMD_253 else 1):
        raise QmdBackupBlocked('source_header_mismatch')
    objects = {name: (kind, sql or '') for name, kind, sql in db.execute(
        "SELECT name,type,sql FROM sqlite_master WHERE type IN ('table','trigger')")}
    columns = dict(_COLUMNS)
    if source_format == FORK_F463:
        columns.update(_FORK_COLUMNS)
    elif any(name in objects for name in _FORK_COLUMNS):
        raise QmdBackupBlocked('source_format_mismatch')
    for name, expected in columns.items():
        item = objects.get(name)
        if item is None or item[0] != 'table':
            raise QmdBackupBlocked('source_schema_mismatch')
        actual = tuple(row[1] for row in db.execute('PRAGMA table_info("' + name + '")'))
        # Fork has an explicit lazy migration for older content_vectors columns.
        # Refuse those until a separate, reviewed adapter proves their meaning.
        if actual != expected:
            raise QmdBackupBlocked('source_schema_mismatch')
    fts_sql = re.sub(r'\s+', '', objects['documents_fts'][1].lower())
    if not re.search(r'usingfts5\(filepath,title,body,tokenize=[\'\"]porterunicode61[\'\"]\)', fts_sql):
        raise QmdBackupBlocked('source_fts_mismatch')
    if any(objects.get(name, (None,))[0] != 'trigger'
           for name in ('documents_ai', 'documents_ad', 'documents_au')):
        raise QmdBackupBlocked('source_fts_mismatch')
    if source_format == FORK_F463:
        # The pinned fork's live format is version 1. A partially upgraded DB
        # without this marker is not safely identifiable as that release.
        if 'file_sync_state' not in objects:
            raise QmdBackupBlocked('source_format_mismatch')


def _inventory(db, source_format, collection):
    _check_schema(db, source_format)
    if not isinstance(collection, str) or not collection:
        raise QmdBackupBlocked('collection_required')
    config = db.execute('SELECT path,pattern,ignore_patterns FROM store_collections '
                        'WHERE name=?', (collection,)).fetchone()
    if config is None:
        raise QmdBackupBlocked('collection_unrecorded')
    rows = db.execute('''SELECT d.path,d.title,d.hash,d.active,c.doc
        FROM documents d LEFT JOIN content c ON c.hash=d.hash
        WHERE d.collection=? ORDER BY d.path''', (collection,)).fetchall()
    # Public tuple contract: (path, title, content_hash, active, body). Title
    # participates in QMD's searchable/embedding source and the backup digest.
    documents = []
    for path, title, hash_, active, body in rows:
        if (not _valid_relative_path(path) or not isinstance(title, str)
                or active not in (0, 1)
                or not isinstance(hash_, str) or not re.fullmatch('[0-9a-f]{64}', hash_)
                or not isinstance(body, str)
                or _hash_body(body) != hash_):
            raise QmdBackupBlocked('source_document_invalid')
        documents.append((path, title, hash_, active, body))
    # Legacy content_vectors is only inventory metadata. Its sqlite-vec payload
    # is never reused; A-int accepts independently supplied vectors instead.
    vectors = db.execute('''SELECT cv.hash,cv.seq,cv.model,cv.embed_fingerprint,
          cv.total_chunks FROM content_vectors cv JOIN documents d ON d.hash=cv.hash
          WHERE d.collection=? AND d.active=1
          GROUP BY cv.hash,cv.seq,cv.model,cv.embed_fingerprint,cv.total_chunks
          ORDER BY cv.hash,cv.seq''', (collection,)).fetchall()
    for hash_, seq, model, fingerprint, total in vectors:
        if (not isinstance(hash_, str) or type(seq) is not int or seq < 0
                or not isinstance(model, str) or not isinstance(fingerprint, str)
                or type(total) is not int or total < 1):
            raise QmdBackupBlocked('source_vector_metadata_invalid')
    payload = {'source_format': source_format, 'collection': collection,
               'collection_config': config,
               'documents': [(p, title, h, a) for p, title, h, a, _ in documents],
               'vectors': vectors}
    return {'source_format': source_format, 'collection': collection,
            'collection_config': config, 'documents': documents,
            'vector_metadata': vectors, 'digest': _digest(payload),
            'active_count': sum(row[3] for row in documents),
            'tombstone_count': sum(1 - row[3] for row in documents),
            'needs_reembed': sorted({h for _, _, h, active, _ in documents if active})}


def _collection_names(collections):
    if (not isinstance(collections, (tuple, list)) or
            not 1 <= len(collections) <= 16 or
            any(not isinstance(name, str) or not _COLLECTION.fullmatch(name)
                for name in collections) or
            len(set(collections)) != len(collections)):
        raise QmdBackupBlocked('invalid_collection_set')
    return tuple(sorted(collections))


def _inventory_collections(db, source_format, collections):
    names = _collection_names(collections)
    members = {name: _inventory(db, source_format, name) for name in names}
    documents = [(name + '/' + path, title, hash_, active, body)
                 for name in names
                 for path, title, hash_, active, body in members[name]['documents']]
    # One transaction and one digest bind the complete selected logical set.
    # The backup remains the whole SQLite DB, including unrelated collections.
    return {'source_format': source_format, 'collections': names,
            'collection_config': {name: members[name]['collection_config']
                                  for name in names},
            'documents': documents,
            'digest': _digest([source_format, [(name, members[name]['digest'])
                                               for name in names]]),
            'active_count': sum(member['active_count'] for member in members.values()),
            'tombstone_count': sum(member['tombstone_count'] for member in members.values()),
            'needs_reembed': sorted({hash_ for member in members.values()
                                    for hash_ in member['needs_reembed']})}


def inspect_qmd_collections(source_db, source_format, collections):
    """Inventory every selected collection in one read-only SQLite snapshot."""
    with closing(_source(source_db)) as db:
        try:
            db.execute('BEGIN')
            result = _inventory_collections(db, source_format, collections)
            db.rollback()
            return result
        except sqlite3.Error as exc:
            raise QmdBackupBlocked('source_read_failed') from exc


def backup_qmd_collections(source_db, source_format, collections, backup_path):
    """No-clobber whole-DB backup with one digest for selected collections."""
    names = _collection_names(collections)
    target = Path(backup_path)
    if target.exists() or target.is_symlink() or not target.parent.is_dir():
        raise QmdBackupBlocked('backup_target_unavailable')
    fd, raw = tempfile.mkstemp(prefix='.qmd-backup-', dir=target.parent)
    os.close(fd)
    temp = Path(raw)
    try:
        with closing(_source(source_db)) as old, closing(sqlite3.connect(temp)) as new:
            old.execute('BEGIN')
            before = _inventory_collections(old, source_format, names)
            _bounded_backup(old, new)
            old.rollback()
            new.commit()
            mode = new.execute('PRAGMA journal_mode=DELETE').fetchone()
            if mode is None or mode[0].lower() != 'delete':
                raise QmdBackupBlocked('backup_journal_unavailable')
        after = inspect_qmd_collections(temp, source_format, names)
        if before != after:
            raise QmdBackupBlocked('backup_snapshot_mismatch')
        _seal_single_file(temp, lambda db:
                          _inventory_collections(db, source_format, names) == before)
        if any(Path(str(temp) + suffix).exists()
               for suffix in ('-wal', '-shm', '-journal')):
            raise QmdBackupBlocked('backup_not_self_contained')
        with temp.open('rb') as stream:
            os.fsync(stream.fileno())
        try:
            os.link(temp, target)
        except FileExistsError as exc:
            raise QmdBackupBlocked('backup_target_unavailable') from exc
        directory = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return {'backup': str(target), 'source_digest': after['digest'],
                'source_format': source_format, 'collections': names}
    finally:
        temp.unlink(missing_ok=True)
        for suffix in ('-wal', '-shm', '-journal'):
            Path(str(temp) + suffix).unlink(missing_ok=True)


def verify_restore_collections(backup_path, source_format, collections,
                               expected_digest):
    names = _collection_names(collections)
    with tempfile.TemporaryDirectory(prefix='qmd-restore-') as directory:
        restored = Path(directory) / 'restored.sqlite'
        with closing(_source(backup_path)) as old, closing(sqlite3.connect(restored)) as new:
            _bounded_backup(old, new)
            new.commit()
            mode = new.execute('PRAGMA journal_mode=DELETE').fetchone()
            if mode is None or mode[0].lower() != 'delete':
                raise QmdBackupBlocked('restore_journal_unavailable')
        got = inspect_qmd_collections(restored, source_format, names)
        if got['digest'] != expected_digest:
            raise QmdBackupBlocked('restore_digest_mismatch')
        return {'restored_digest': got['digest'], 'verified': True}

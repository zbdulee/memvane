"""Read-only QMD source adapter for preparing an A-int v4 candidate.

No installed QMD, hook, model or product route imports this module. The caller
must attest the source release and supply a current, approved collection file
manifest. This module never publishes a generation or changes the source DB.

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
from types import SimpleNamespace

import laya_index_a as _a_int
from laya_index_a import LayaIndexA, _valid_relative_path


QMD_253 = 'qmd-2.5.3'
FORK_F463 = 'qmd-fork-f4633f9'
FORMATS = frozenset((QMD_253, FORK_F463))


class QmdMigrationBlocked(RuntimeError):
    """The caller cannot safely prepare a candidate from this source."""


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
        raise QmdMigrationBlocked('source_document_invalid') from exc


def _bounded_backup(source, target):
    deadline = time.monotonic() + 10

    def progress(_status, _remaining, _total):
        if time.monotonic() > deadline:
            raise QmdMigrationBlocked('backup_timeout')

    source.backup(target, pages=128, progress=progress, sleep=0.05)


def _seal_single_file(path, validator):
    """Prove a closed private temp DB is complete before sidecar cleanup."""
    with closing(sqlite3.connect(path)) as db:
        mode = db.execute('PRAGMA journal_mode=DELETE').fetchone()
        if mode is None or mode[0].lower() != 'delete':
            raise QmdMigrationBlocked('single_file_journal_unavailable')
        if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise QmdMigrationBlocked('single_file_integrity_failed')
        if not validator(db):
            raise QmdMigrationBlocked('single_file_digest_mismatch')
    # A nonempty WAL or rollback journal could carry uncheckpointed rows even
    # after a nominal mode switch; never discard it. SQLite may leave a -shm
    # mapping after the last reader closes. Only private temp paths reach here.
    for suffix in ('-wal', '-journal'):
        sidecar = Path(str(path) + suffix)
        if sidecar.exists() and sidecar.stat().st_size:
            raise QmdMigrationBlocked('single_file_sidecar_not_empty')
    for suffix in ('-wal', '-shm', '-journal'):
        Path(str(path) + suffix).unlink(missing_ok=True)


def _source(path):
    path = Path(path)
    if not path.is_file() or path.is_symlink():
        raise QmdMigrationBlocked('source_missing_or_symlink')
    try:
        db = sqlite3.connect(path.resolve(strict=True).as_uri() + '?mode=ro',
                             uri=True, timeout=2)
        db.execute('PRAGMA query_only=ON')
        return db
    except (OSError, sqlite3.Error) as exc:
        raise QmdMigrationBlocked('source_open_failed') from exc


def _check_schema(db, source_format):
    if source_format not in FORMATS:
        raise QmdMigrationBlocked('unsupported_source_version')
    app = db.execute('PRAGMA application_id').fetchone()[0]
    version = db.execute('PRAGMA user_version').fetchone()[0]
    if app != 0 or version != (0 if source_format == QMD_253 else 1):
        raise QmdMigrationBlocked('source_header_mismatch')
    objects = {name: (kind, sql or '') for name, kind, sql in db.execute(
        "SELECT name,type,sql FROM sqlite_master WHERE type IN ('table','trigger')")}
    columns = dict(_COLUMNS)
    if source_format == FORK_F463:
        columns.update(_FORK_COLUMNS)
    elif any(name in objects for name in _FORK_COLUMNS):
        raise QmdMigrationBlocked('source_format_mismatch')
    for name, expected in columns.items():
        item = objects.get(name)
        if item is None or item[0] != 'table':
            raise QmdMigrationBlocked('source_schema_mismatch')
        actual = tuple(row[1] for row in db.execute('PRAGMA table_info("' + name + '")'))
        # Fork has an explicit lazy migration for older content_vectors columns.
        # Refuse those until a separate, reviewed adapter proves their meaning.
        if actual != expected:
            raise QmdMigrationBlocked('source_schema_mismatch')
    fts_sql = re.sub(r'\s+', '', objects['documents_fts'][1].lower())
    if not re.search(r'usingfts5\(filepath,title,body,tokenize=[\'\"]porterunicode61[\'\"]\)', fts_sql):
        raise QmdMigrationBlocked('source_fts_mismatch')
    if any(objects.get(name, (None,))[0] != 'trigger'
           for name in ('documents_ai', 'documents_ad', 'documents_au')):
        raise QmdMigrationBlocked('source_fts_mismatch')
    if source_format == FORK_F463:
        # The pinned fork's live format is version 1. A partially upgraded DB
        # without this marker is not safely identifiable as that release.
        if 'file_sync_state' not in objects:
            raise QmdMigrationBlocked('source_format_mismatch')


def _inventory(db, source_format, collection):
    _check_schema(db, source_format)
    if not isinstance(collection, str) or not collection:
        raise QmdMigrationBlocked('collection_required')
    config = db.execute('SELECT path,pattern,ignore_patterns FROM store_collections '
                        'WHERE name=?', (collection,)).fetchone()
    if config is None:
        raise QmdMigrationBlocked('collection_unrecorded')
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
            raise QmdMigrationBlocked('source_document_invalid')
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
            raise QmdMigrationBlocked('source_vector_metadata_invalid')
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


def inspect_qmd(source_db, source_format, collection):
    """Inventory a single collection from a read-only SQLite transaction."""
    with closing(_source(source_db)) as db:
        try:
            db.execute('BEGIN')
            result = _inventory(db, source_format, collection)
            db.rollback()
            return result
        except sqlite3.Error as exc:
            raise QmdMigrationBlocked('source_read_failed') from exc


def backup_qmd(source_db, source_format, collection, backup_path):
    """No-clobber SQLite online snapshot, then compare selected logical rows.

    This is a backup/restore rehearsal, not a proof against power loss. The
    digest covers the selected collection's documents, collection config and
    vector metadata, not every table or sqlite-vec bytes. The source may advance
    after the transaction; cutover requires fresh inventory and a separate gate.
    """
    target = Path(backup_path)
    if target.exists() or target.is_symlink() or not target.parent.is_dir():
        raise QmdMigrationBlocked('backup_target_unavailable')
    fd, raw = tempfile.mkstemp(prefix='.qmd-backup-', dir=target.parent)
    os.close(fd)
    temp = Path(raw)
    try:
        with closing(_source(source_db)) as old, closing(sqlite3.connect(temp)) as new:
            old.execute('BEGIN')
            before = _inventory(old, source_format, collection)
            _bounded_backup(old, new)
            old.rollback()
            new.commit()
            mode = new.execute('PRAGMA journal_mode=DELETE').fetchone()
            if mode is None or mode[0].lower() != 'delete':
                raise QmdMigrationBlocked('backup_journal_unavailable')
        after = inspect_qmd(temp, source_format, collection)
        if before != after:
            raise QmdMigrationBlocked('backup_snapshot_mismatch')
        _seal_single_file(temp, lambda db: _inventory(db, source_format, collection) == before)
        if any(Path(str(temp) + suffix).exists()
               for suffix in ('-wal', '-shm', '-journal')):
            raise QmdMigrationBlocked('backup_not_self_contained')
        with temp.open('rb') as stream:
            os.fsync(stream.fileno())
        try:
            os.link(temp, target)
        except FileExistsError as exc:
            raise QmdMigrationBlocked('backup_target_unavailable') from exc
        directory = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return {'backup': str(target), 'source_digest': after['digest'],
                'source_format': source_format, 'collection': collection}
    finally:
        temp.unlink(missing_ok=True)
        for suffix in ('-wal', '-shm', '-journal'):
            Path(str(temp) + suffix).unlink(missing_ok=True)


def verify_restore(backup_path, source_format, collection, expected_digest):
    """Restore to a disposable file and compare selected logical inventory."""
    with tempfile.TemporaryDirectory(prefix='qmd-restore-') as directory:
        restored = Path(directory) / 'restored.sqlite'
        with closing(_source(backup_path)) as old, closing(sqlite3.connect(restored)) as new:
            _bounded_backup(old, new)
            new.commit()
            mode = new.execute('PRAGMA journal_mode=DELETE').fetchone()
            if mode is None or mode[0].lower() != 'delete':
                raise QmdMigrationBlocked('restore_journal_unavailable')
        got = inspect_qmd(restored, source_format, collection)
        if got['digest'] != expected_digest:
            raise QmdMigrationBlocked('restore_digest_mismatch')
        return {'restored_digest': got['digest'], 'verified': True}


def prepare_qmd_candidate(backup_path, source_format, collection, current_files,
                          candidate_path, scope, generation, *, migration_allowed,
                          expected_collection_root, expected_source_digest,
                          vector_batch=None, provider_approved=False,
                          current_source_db=None, scope_collections=None):
    """Stage a sealed title-aware A-int candidate from a verified QMD backup.

    `current_files` is an authoritative {relative_path: UTF-8 text} snapshot
    supplied by a separately approved source scanner. Missing active DB paths
    are treated as deleted; a new path or changed body requires reindexing and
    blocks preparation. QMD vectors are never copied. An approved provider may
    supply fresh precomputed vectors for the exact full scope; its model weights
    remain an external attestation. This never publishes or switches a route.
    """
    if (migration_allowed is not True or not isinstance(current_files, dict)
            or not isinstance(scope, str) or not scope
            or not isinstance(expected_collection_root, str)
            or not expected_collection_root
            or not isinstance(expected_source_digest, str)
            or not re.fullmatch('[0-9a-f]{64}', expected_source_digest)):
        raise QmdMigrationBlocked('invalid_migration_request')
    candidate = Path(candidate_path)
    if candidate.exists() or candidate.is_symlink() or not candidate.parent.is_dir():
        raise QmdMigrationBlocked('candidate_exists')
    inventory = inspect_qmd(backup_path, source_format, collection)
    if inventory['collection_config'][0] != expected_collection_root:
        raise QmdMigrationBlocked('collection_root_mismatch')
    if inventory['digest'] != expected_source_digest:
        raise QmdMigrationBlocked('backup_digest_mismatch')
    verify_restore(backup_path, source_format, collection, expected_source_digest)
    active = {p: (title, h, body) for p, title, h, flag, body in
              inventory['documents'] if flag}
    if (any(not _valid_relative_path(p) or not isinstance(body, str)
            for p, body in current_files.items()) or set(current_files) - set(active)):
        raise QmdMigrationBlocked('current_manifest_unindexed')
    for path, body in current_files.items():
        if _hash_body(body) != active[path][1]:
            raise QmdMigrationBlocked('current_manifest_changed')
    staged = [(path, current_files[path], True) for path in sorted(current_files)]
    source_manifest = _a_int._digest(scope, [
        (path, _hash_body(body), 1) for path, body, _ in staged])
    chunks = None
    profile_id = None
    expected_vector_rows = 0
    if vector_batch is not None:
        # QMD embeds the derived Markdown title with the body. The sealed
        # source inventory must agree with the title the v5 vector index will
        # derive again from each staged path and body.
        from laya_index_vector import qmd_markdown_title
        expected_keys = {}
        for path, body in current_files.items():
            title = qmd_markdown_title(body, path)
            if active[path][0] != title:
                raise QmdMigrationBlocked('source_title_mismatch')
            expected_keys[path] = (_hash_body(body), title)
        from laya_index_sync import _source_evidence
        from laya_index_vector import MAX_CANDIDATES, _blob, profile_identity
        if (provider_approved is not True or current_source_db is None
                or scope_collections != (collection,)
                or not isinstance(vector_batch, dict)
                or set(vector_batch) != {'scope', 'source_manifest', 'qmd_source_digest',
                                       'model_id', 'embedding_fingerprint', 'dimension',
                                       'chunks'}
                or vector_batch['scope'] != scope
                or vector_batch['source_manifest'] != source_manifest
                or vector_batch['qmd_source_digest'] != expected_source_digest
                or not isinstance(vector_batch['chunks'], list)
                or len(vector_batch['chunks']) > MAX_CANDIDATES):
            raise QmdMigrationBlocked('vector_provider_or_scope_unapproved')
        if inspect_qmd(current_source_db, source_format, collection)['digest'] != expected_source_digest:
            raise QmdMigrationBlocked('source_changed_since_backup')
        profile_id = profile_identity(vector_batch['model_id'],
                                      vector_batch['embedding_fingerprint'],
                                      vector_batch['dimension'])
        eligible = set(expected_keys.values())
        seen = set()
        grouped = {}
        chunks = []
        for item in vector_batch['chunks']:
            if (not isinstance(item, dict) or set(item) != {'hash','title','seq','pos','vector'}
                    or not isinstance(item['hash'], str)
                    or not isinstance(item['title'], str)
                    or (item['hash'], item['title']) not in eligible
                    or type(item['seq']) is not int
                    or item['seq'] < 0 or type(item['pos']) is not int
                    or item['pos'] < 0
                    or (item['hash'], item['title'], item['seq']) in seen):
                raise QmdMigrationBlocked('invalid_vector_batch')
            _blob(item['vector'], vector_batch['dimension'])
            key = (item['hash'], item['title'])
            seen.add((*key, item['seq']))
            grouped.setdefault(key, []).append((item['seq'], item['pos']))
            chunks.append((item['hash'], item['title'], item['seq'], item['pos'],
                           item['vector'], _source_evidence(item['hash'], item['title'])))
        if set(grouped) != eligible or any(
                [seq for seq, _ in sorted(rows)] != list(range(len(rows)))
                or any(right <= left for (_, left), (_, right) in
                       zip(sorted(rows), sorted(rows)[1:]))
                for rows in grouped.values()):
            raise QmdMigrationBlocked('vector_coverage_incomplete')
        expected_vector_rows = sum(len(grouped[key]) for key in expected_keys.values())
        if expected_vector_rows > MAX_CANDIDATES:
            raise QmdMigrationBlocked('vector_coverage_incomplete')
    elif provider_approved is not False or current_source_db is not None or scope_collections is not None:
        raise QmdMigrationBlocked('vector_provider_or_scope_unapproved')
    # Build privately and publish only a closed, complete single-file DB. A-int
    # itself also uses no-clobber creation, but direct final-path staging leaves
    # an incomplete candidate on an exception. The hard link closes that gap.
    with tempfile.TemporaryDirectory(prefix='.qmd-candidate-', dir=candidate.parent) as work:
        built = Path(work) / 'candidate.sqlite'
        index = LayaIndexA(built, create=True)
        try:
            result = index.stage(generation, scope, staged, base=None,
                source_manifest=source_manifest,
                claim_digest=inventory['digest'], claim_epoch=0)
            if chunks is not None:
                from laya_index_vector import LayaVectorIndex, verify_generation_seal
                vectors = LayaVectorIndex(index)
                registered = vectors.register_profile(vector_batch['model_id'],
                    vector_batch['embedding_fingerprint'], vector_batch['dimension'])
                if registered != profile_id:
                    raise QmdMigrationBlocked('vector_profile_mismatch')
                for hash_, title, seq, pos, values, evidence in chunks:
                    vectors.insert_chunk(hash_, profile_id, seq, pos, values,
                                         evidence, title=title)
                # Seal each complete title-bound set before the generation may
                # reference it. This private candidate is never exposed until
                # every set and the generation seal have verified successfully.
                for hash_, title in sorted(grouped):
                    vectors.seal_chunk_set(hash_, title, profile_id,
                                           len(grouped[(hash_, title)]))
                vectors.seal_generation(generation, profile_id)
                proof = verify_generation_seal(index, generation)
                if proof['rows_count'] != expected_vector_rows:
                    raise QmdMigrationBlocked('vector_seal_mismatch')
            if not index._generation_fts_matches(generation):
                raise QmdMigrationBlocked('candidate_fts_mismatch')
            if chunks is not None and inspect_qmd(current_source_db, source_format,
                                                  collection)['digest'] != expected_source_digest:
                raise QmdMigrationBlocked('source_changed_since_backup')
            mode = index.db.execute('PRAGMA journal_mode=DELETE').fetchone()
            if mode is None or mode[0].lower() != 'delete':
                raise QmdMigrationBlocked('candidate_journal_unavailable')
        finally:
            index.close()
        expected_members = expected_members_from_staged(staged)
        def candidate_valid(db):
            _a_int.LayaIndexA._check_header(db)
            if chunks is not None:
                from laya_index_vector import verify_generation_seal
                proof = verify_generation_seal(SimpleNamespace(db=db), generation)
                if (proof['profile_id'] != profile_id or
                        proof['rows_count'] != expected_vector_rows):
                    return False
            members = db.execute('SELECT path,hash,eligible FROM membership '
                                 'WHERE gen=? ORDER BY path', (generation,)).fetchall()
            state = db.execute('SELECT state,scope FROM generations WHERE gen=?',
                               (generation,)).fetchone()
            seal = db.execute('SELECT source_manifest,claim_digest,claim_epoch '
                              'FROM candidate_seals WHERE gen=?', (generation,)).fetchone()
            vector_seal = db.execute('SELECT profile_id,rows_count FROM gen_vector_seals '
                                     'WHERE gen=?', (generation,)).fetchone()
            return (members == expected_members and state == ('prepared', scope)
                    and seal == (source_manifest, inventory['digest'], 0)
                    and (vector_seal == (profile_id, expected_vector_rows) if chunks is not None
                         else vector_seal is None))
        _seal_single_file(built, candidate_valid)
        if any(Path(str(built) + suffix).exists()
               for suffix in ('-wal', '-shm', '-journal')):
            raise QmdMigrationBlocked('candidate_not_self_contained')
        with built.open('rb') as stream:
            os.fsync(stream.fileno())
        try:
            os.link(built, candidate)
        except FileExistsError as exc:
            raise QmdMigrationBlocked('candidate_exists') from exc
        directory = os.open(candidate.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    return {'state': result, 'source_digest': inventory['digest'],
            'source_manifest': source_manifest,
            'staged_count': len(staged),
            'deleted_count': len(active) - len(staged),
            'tombstone_count': inventory['tombstone_count'],
            'needs_reembed': ([] if chunks is not None else
                              sorted({active[path][1] for path in current_files})),
            'vector_reuse': False, 'vector_profile_id': profile_id,
            'candidate_ready': chunks is not None, 'cutover_ready': False,
            'candidate': str(candidate)}


def expected_members_from_staged(staged):
    return [(path, _hash_body(body), 1) for path, body, _ in staged]

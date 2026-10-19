"""A-int Laya Index contract slice: immutable generations in one SQLite DB.

Callers must supply a verified explicit database path and scope.
Callers inject an explicit database path. GC is a read-only plan, never deletion.
This is not the historical A-copy design that clones a whole QMD database.
"""
from __future__ import annotations

from bisect import bisect_right
from contextlib import closing

import hashlib
import json
import math
import os
import re
from pathlib import Path
import sqlite3
import time
import uuid


class IndexContractError(ValueError):
    pass


class InjectedFault(RuntimeError):
    pass


def _digest(scope, entries):
    return hashlib.sha256(json.dumps([scope, sorted(entries)], ensure_ascii=False,
                                     separators=(',', ':')).encode('utf-8')).hexdigest()


class _StreamDigest:
    """``_digest`` over entries that arrive in strictly increasing path order.

    Unique paths in increasing order are already ``sorted(entries)``, so the
    JSON text ``_digest`` hashes can be fed piecewise without keeping a list.
    """

    def __init__(self, scope):
        self._hash = hashlib.sha256(
            ('[' + json.dumps(scope, ensure_ascii=False) + ',[').encode('utf-8'))
        self._first = True

    def add(self, path, hash_, flag):
        part = json.dumps([path, hash_, flag], ensure_ascii=False, separators=(',', ':'))
        self._hash.update((part if self._first else ',' + part).encode('utf-8'))
        self._first = False

    def hexdigest(self):
        final = self._hash.copy()
        final.update(b']]')
        return final.hexdigest()


def _gen_fts(ordinal):
    # Only an integer allocated by SQLite becomes an SQL identifier.
    return f'gen_fts_{int(ordinal)}'


def _gen_fts_ddl(table):
    return (f'CREATE VIRTUAL TABLE {table} USING '
            "fts5(filepath,title,body,path_key UNINDEXED,tokenize='porter unicode61')")


def _qmd_title(body, path):
    match = re.search(r'^##?\s+(.+)$', body, re.MULTILINE)
    if match:
        title = match.group(1).strip()
        if title in ('📝 Notes', 'Notes'):
            next_title = re.search(r'^##\s+(.+)$', body, re.MULTILINE)
            if next_title:
                return next_title.group(1).strip()
        return title
    name = path.rsplit('/', 1)[-1]
    return name.rsplit('.', 1)[0] or name


# Expanded from QMD's four JavaScript ``\p{Script=...}`` properties under the
# selected Node 24.21.0 / Unicode 17.0 runtime. Unicode blocks include gaps
# and omit supplementary Script members, so broad block ranges are incorrect.
_QMD_CJK_SCRIPT_RANGES = (
    (0x2e80, 0x2e99), (0x2e9b, 0x2ef3), (0x2f00, 0x2fd5),
    (0x3005, 0x3005), (0x3007, 0x3007), (0x3021, 0x3029),
    (0x3038, 0x303b), (0x3400, 0x4dbf), (0x4e00, 0x9fff),
    (0xf900, 0xfa6d), (0xfa70, 0xfad9), (0x16fe2, 0x16fe3),
    (0x16ff0, 0x16ff6), (0x20000, 0x2a6df), (0x2a700, 0x2b81d),
    (0x2b820, 0x2cead), (0x2ceb0, 0x2ebe0), (0x2ebf0, 0x2ee5d),
    (0x2f800, 0x2fa1d), (0x30000, 0x3134a), (0x31350, 0x33479),
    (0x3041, 0x3096), (0x309d, 0x309f), (0x1b001, 0x1b11f),
    (0x1b132, 0x1b132), (0x1b150, 0x1b152), (0x1f200, 0x1f200),
    (0x30a1, 0x30fa), (0x30fd, 0x30ff), (0x31f0, 0x31ff),
    (0x32d0, 0x32fe), (0x3300, 0x3357), (0xff66, 0xff6f),
    (0xff71, 0xff9d), (0x1aff0, 0x1aff3), (0x1aff5, 0x1affb),
    (0x1affd, 0x1affe), (0x1b000, 0x1b000), (0x1b120, 0x1b122),
    (0x1b155, 0x1b155), (0x1b164, 0x1b167),
    (0x1100, 0x11ff), (0x302e, 0x302f), (0x3131, 0x318e),
    (0x3200, 0x321e), (0x3260, 0x327e), (0xa960, 0xa97c),
    (0xac00, 0xd7a3), (0xd7b0, 0xd7c6), (0xd7cb, 0xd7fb),
    (0xffa0, 0xffbe), (0xffc2, 0xffc7), (0xffca, 0xffcf),
    (0xffd2, 0xffd7), (0xffda, 0xffdc),
)
_QMD_CJK_SCRIPT_RANGES = tuple(sorted(_QMD_CJK_SCRIPT_RANGES))
_QMD_CJK_SCRIPT_STARTS = tuple(start for start, _end in _QMD_CJK_SCRIPT_RANGES)


def _cjk(ch):
    code = ord(ch)
    if code < 0x1100:
        return False
    offset = bisect_right(_QMD_CJK_SCRIPT_STARTS, code) - 1
    return offset >= 0 and code <= _QMD_CJK_SCRIPT_RANGES[offset][1]


def _qmd_fts_text(text, *, deadline=None):
    """Space CJK runs by code point, as QMD's normalizeCjkForFTS does."""
    out = []
    in_run = False
    for offset, ch in enumerate(text):
        if (deadline is not None and offset % 4096 == 0
                and time.monotonic() >= deadline):
            raise IndexContractError('fts_deadline_exceeded')
        if _cjk(ch):
            out.append(' ')
            out.append(ch)
            in_run = True
        else:
            if in_run:
                out.append(' ')
                in_run = False
            out.append(ch)
    if in_run:
        out.append(' ')
    return ''.join(out)


def _generation_fts_rows_match(db, gen, table, *, deadline=None):
    expected = []
    for path, body in db.execute(
            'SELECT m.path,c.body FROM membership m JOIN content c ON c.hash=m.hash '
            'WHERE m.gen=? AND m.eligible=1', (gen,)):
        expected.append((path, _qmd_fts_text(path, deadline=deadline),
                         _qmd_fts_text(_qmd_title(body, path), deadline=deadline),
                         _qmd_fts_text(body, deadline=deadline)))
    actual = db.execute(
        f'SELECT path_key,filepath,title,body FROM {table}').fetchall()
    if deadline is not None and time.monotonic() >= deadline:
        raise IndexContractError('fts_deadline_exceeded')
    expected.sort()
    if deadline is not None and time.monotonic() >= deadline:
        raise IndexContractError('fts_deadline_exceeded')
    actual.sort()
    if deadline is not None and time.monotonic() >= deadline:
        raise IndexContractError('fts_deadline_exceeded')
    return expected == actual


def _generation_fts_rows_stream_match(db, gen, table):
    """Lockstep FTS check for a ``stage_stream`` generation; one body at a time.

    ``stage_stream`` inserts FTS rows in path order, so rowid order must equal
    membership path order. Any other order is a mismatch (fail closed), never
    a fallback to the whole-generation sort in ``_generation_fts_rows_match``.
    """
    members = db.execute(
        'SELECT m.path,c.body FROM membership m JOIN content c ON c.hash=m.hash '
        'WHERE m.gen=? AND m.eligible=1 ORDER BY m.path', (gen,))
    rows = db.execute(f'SELECT path_key,filepath,title,body FROM {table} ORDER BY rowid')
    while True:
        member = members.fetchone()
        row = rows.fetchone()
        if member is None or row is None:
            return member is None and row is None
        path, body = member
        if row != (path, _qmd_fts_text(path), _qmd_fts_text(_qmd_title(body, path)),
                   _qmd_fts_text(body)):
            return False


def _path_predicate(prefixes):
    if prefixes is None:
        return '', ()
    if (not isinstance(prefixes, tuple) or not 1 <= len(prefixes) <= 16
            or any(not isinstance(p, str) or not p.endswith('/')
                   or not _valid_relative_path(p[:-1]) for p in prefixes)
            or len(set(prefixes)) != len(prefixes)):
        raise IndexContractError('invalid_collection_filter')
    clauses = ['substr(m.path,1,length(?))=?' for _ in prefixes]
    return ' AND (' + ' OR '.join(clauses) + ')', tuple(
        value for prefix in prefixes for value in (prefix, prefix))


def _valid_relative_path(path):
    """Accept exactly the source port's serveable relative path vocabulary."""
    if not isinstance(path, str) or not path or path.startswith('/') or '\\' in path:
        return False
    if (any(not part or part.startswith('.') for part in path.split('/'))
            or any(ord(ch) < 32 or ord(ch) == 127 for ch in path)):
        return False
    try:
        return len(path.encode('utf-8')) <= 4096
    except UnicodeEncodeError:
        return False


_NOT_EXCLUDED = '''
  NOT EXISTS (SELECT 1 FROM exclusions x
              WHERE x.scope=m.scope AND x.kind='path' AND x.value=m.path)
  AND NOT EXISTS (SELECT 1 FROM exclusions x
                  WHERE x.scope=m.scope AND x.kind='hash' AND x.value=m.hash)'''


_SCHEMA_V6 = '''
BEGIN IMMEDIATE;
          CREATE TABLE schema_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS content(hash TEXT PRIMARY KEY, body TEXT NOT NULL);
          CREATE VIRTUAL TABLE IF NOT EXISTS content_fts USING fts5(hash UNINDEXED, body);
          CREATE TABLE IF NOT EXISTS generations(
            gen TEXT PRIMARY KEY, scope TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('building','prepared','published')),
            digest TEXT NOT NULL, rev_epoch INTEGER NOT NULL DEFAULT 0);
          CREATE TABLE IF NOT EXISTS membership(
            gen TEXT NOT NULL REFERENCES generations(gen),
            scope TEXT NOT NULL, path TEXT NOT NULL,
            hash TEXT NOT NULL REFERENCES content(hash),
            eligible INTEGER NOT NULL CHECK(eligible IN (0,1)),
            PRIMARY KEY(gen,path));
          CREATE INDEX IF NOT EXISTS membership_scope ON membership(gen,scope,eligible,path);
          CREATE INDEX IF NOT EXISTS membership_hash ON membership(hash,gen,scope,eligible,path);
          CREATE TABLE IF NOT EXISTS head(
            scope TEXT PRIMARY KEY, gen TEXT NOT NULL REFERENCES generations(gen));
          CREATE TABLE IF NOT EXISTS candidate_seals(
            gen TEXT PRIMARY KEY REFERENCES generations(gen), scope TEXT NOT NULL,
            base_gen TEXT, source_manifest TEXT NOT NULL,
            claim_digest TEXT NOT NULL, claim_epoch INTEGER NOT NULL,
            entries_digest TEXT NOT NULL);
          CREATE TABLE vector_profiles(
            profile_id TEXT PRIMARY KEY, model_id TEXT NOT NULL,
            embedding_fingerprint TEXT NOT NULL,
            dimension INTEGER NOT NULL CHECK(dimension BETWEEN 1 AND 2048),
            metric TEXT NOT NULL CHECK(metric='cosine'));
          CREATE TABLE vector_chunks(
            hash TEXT NOT NULL REFERENCES content(hash),
            title TEXT NOT NULL,
            profile_id TEXT NOT NULL REFERENCES vector_profiles(profile_id),
            seq INTEGER NOT NULL CHECK(seq>=0),
            pos INTEGER NOT NULL CHECK(pos>=0),
            embedding BLOB NOT NULL, source_evidence_digest TEXT NOT NULL,
            provenance_digest TEXT NOT NULL,
            PRIMARY KEY(hash,title,profile_id,seq));
          CREATE TABLE vector_chunk_sets(
            hash TEXT NOT NULL, title TEXT NOT NULL, profile_id TEXT NOT NULL,
            rows_count INTEGER NOT NULL CHECK(rows_count BETWEEN 1 AND 20000),
            rows_digest TEXT NOT NULL,
            PRIMARY KEY(hash,title,profile_id));
          CREATE TABLE gen_vector_rows(
            gen TEXT NOT NULL REFERENCES generations(gen), scope TEXT NOT NULL,
            path TEXT NOT NULL, hash TEXT NOT NULL, title TEXT NOT NULL,
            profile_id TEXT NOT NULL, seq INTEGER NOT NULL,
            PRIMARY KEY(gen,path,seq),
            FOREIGN KEY(hash,title,profile_id,seq)
              REFERENCES vector_chunks(hash,title,profile_id,seq));
          CREATE INDEX gen_vector_rows_scope ON gen_vector_rows(gen,scope,profile_id,path);
          CREATE TABLE gen_vector_seals(
            gen TEXT PRIMARY KEY REFERENCES generations(gen), scope TEXT NOT NULL,
            profile_id TEXT NOT NULL REFERENCES vector_profiles(profile_id),
            rows_digest TEXT NOT NULL, rows_count INTEGER NOT NULL CHECK(rows_count>=0));
          CREATE TABLE IF NOT EXISTS receipts(
            claim TEXT PRIMARY KEY, scope TEXT NOT NULL,
            gen TEXT NOT NULL REFERENCES generations(gen),
            source_manifest TEXT, claim_digest TEXT, claim_epoch INTEGER);
          CREATE TABLE IF NOT EXISTS pins(
            token TEXT PRIMARY KEY, scope TEXT NOT NULL,
            gen TEXT NOT NULL REFERENCES generations(gen));
          CREATE TRIGGER IF NOT EXISTS membership_insert_seal BEFORE INSERT ON membership
          WHEN (SELECT state FROM generations WHERE gen=NEW.gen) IS NOT 'building'
            OR (SELECT scope FROM generations WHERE gen=NEW.gen) IS NOT NEW.scope
          BEGIN SELECT RAISE(ABORT,'generation_sealed'); END;
          CREATE TRIGGER IF NOT EXISTS membership_update_seal BEFORE UPDATE ON membership
          BEGIN SELECT RAISE(ABORT,'generation_sealed'); END;
          CREATE TRIGGER IF NOT EXISTS membership_delete_seal BEFORE DELETE ON membership
          BEGIN SELECT RAISE(ABORT,'generation_sealed'); END;
          CREATE TRIGGER schema_meta_update_seal BEFORE UPDATE ON schema_meta
          BEGIN SELECT RAISE(ABORT,'schema_meta_immutable'); END;
          CREATE TRIGGER schema_meta_delete_seal BEFORE DELETE ON schema_meta
          BEGIN SELECT RAISE(ABORT,'schema_meta_immutable'); END;
          CREATE TRIGGER schema_meta_insert_seal BEFORE INSERT ON schema_meta
          WHEN EXISTS (SELECT 1 FROM schema_meta WHERE key=NEW.key)
          BEGIN SELECT RAISE(ABORT,'schema_meta_immutable'); END;
          CREATE TRIGGER IF NOT EXISTS content_update_seal BEFORE UPDATE ON content
          BEGIN SELECT RAISE(ABORT,'content_immutable'); END;
          CREATE TRIGGER IF NOT EXISTS content_delete_seal BEFORE DELETE ON content
          BEGIN SELECT RAISE(ABORT,'content_immutable'); END;
          CREATE TRIGGER content_insert_seal BEFORE INSERT ON content
          WHEN EXISTS (SELECT 1 FROM content WHERE hash=NEW.hash)
          BEGIN SELECT RAISE(ABORT,'content_immutable'); END;
          CREATE TABLE IF NOT EXISTS gen_index(
            ord INTEGER PRIMARY KEY AUTOINCREMENT,
            gen TEXT NOT NULL UNIQUE REFERENCES generations(gen));
          CREATE TABLE IF NOT EXISTS exclusions(
            scope TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('path','hash')),
            value TEXT NOT NULL, PRIMARY KEY(scope,kind,value));
          CREATE TRIGGER IF NOT EXISTS gen_index_update_seal BEFORE UPDATE ON gen_index
          BEGIN SELECT RAISE(ABORT,'generation_sealed'); END;
          CREATE TRIGGER IF NOT EXISTS gen_index_delete_seal BEFORE DELETE ON gen_index
          BEGIN SELECT RAISE(ABORT,'generation_sealed'); END;
          CREATE TRIGGER IF NOT EXISTS exclusion_update_seal BEFORE UPDATE ON exclusions
          BEGIN SELECT RAISE(ABORT,'exclusion_append_only'); END;
          CREATE TRIGGER IF NOT EXISTS exclusion_delete_seal BEFORE DELETE ON exclusions
          BEGIN SELECT RAISE(ABORT,'exclusion_append_only'); END;

          CREATE TRIGGER generation_update_seal BEFORE UPDATE ON generations
          WHEN NEW.gen IS NOT OLD.gen OR NEW.scope IS NOT OLD.scope
            OR NEW.digest IS NOT OLD.digest OR NEW.rev_epoch IS NOT OLD.rev_epoch
            OR NOT ((OLD.state='building' AND NEW.state='prepared')
                 OR (OLD.state='prepared' AND NEW.state='published'))
          BEGIN SELECT RAISE(ABORT,'generation_sealed'); END;
          CREATE TRIGGER generation_delete_seal BEFORE DELETE ON generations
          BEGIN SELECT RAISE(ABORT,'generation_sealed'); END;
          CREATE TRIGGER generation_insert_seal BEFORE INSERT ON generations
          WHEN NEW.state IS NOT 'building'
            OR NEW.rev_epoch IS NOT (SELECT COUNT(*) FROM exclusions WHERE scope=NEW.scope)
            OR EXISTS (SELECT 1 FROM generations WHERE gen=NEW.gen)
          BEGIN SELECT RAISE(ABORT,'generation_sealed'); END;
          CREATE TRIGGER gen_index_insert_seal BEFORE INSERT ON gen_index
          WHEN EXISTS (SELECT 1 FROM gen_index WHERE gen=NEW.gen OR ord=NEW.ord)
            OR (SELECT state FROM generations WHERE gen=NEW.gen) IS NOT 'building'
          BEGIN SELECT RAISE(ABORT,'generation_sealed'); END;
          CREATE TRIGGER gen_index_positive_ord AFTER INSERT ON gen_index
          WHEN NEW.ord < 1
          BEGIN SELECT RAISE(ABORT,'generation_sealed'); END;
          CREATE TRIGGER head_insert_scope BEFORE INSERT ON head
          WHEN (SELECT scope FROM generations WHERE gen=NEW.gen) IS NOT NEW.scope
            OR (SELECT state FROM generations WHERE gen=NEW.gen) IS NOT 'published'
          BEGIN SELECT RAISE(ABORT,'head_scope_invalid'); END;
          CREATE TRIGGER head_delete_seal BEFORE DELETE ON head
          BEGIN SELECT RAISE(ABORT,'head_immutable'); END;
          CREATE TRIGGER head_update_scope BEFORE UPDATE ON head
          WHEN NEW.scope IS NOT OLD.scope OR
            (SELECT scope FROM generations WHERE gen=NEW.gen) IS NOT NEW.scope
            OR (SELECT state FROM generations WHERE gen=NEW.gen) IS NOT 'published'
          BEGIN SELECT RAISE(ABORT,'head_scope_invalid'); END;
          CREATE TRIGGER candidate_seal_insert BEFORE INSERT ON candidate_seals
          WHEN EXISTS (SELECT 1 FROM candidate_seals WHERE gen=NEW.gen)
            OR (SELECT scope FROM generations WHERE gen=NEW.gen) IS NOT NEW.scope
            OR (SELECT digest FROM generations WHERE gen=NEW.gen) IS NOT NEW.entries_digest
            OR (SELECT state FROM generations WHERE gen=NEW.gen) IS NOT 'building'
          BEGIN SELECT RAISE(ABORT,'candidate_seal_invalid'); END;
          CREATE TRIGGER candidate_seal_update BEFORE UPDATE ON candidate_seals
          BEGIN SELECT RAISE(ABORT,'candidate_seal_immutable'); END;
          CREATE TRIGGER candidate_seal_delete BEFORE DELETE ON candidate_seals
          BEGIN SELECT RAISE(ABORT,'candidate_seal_immutable'); END;
          CREATE TRIGGER vector_profile_insert_seal BEFORE INSERT ON vector_profiles
          WHEN EXISTS (SELECT 1 FROM vector_profiles WHERE profile_id=NEW.profile_id)
            OR length(NEW.profile_id)<>64 OR NEW.profile_id GLOB '*[^0-9a-f]*'
            OR length(NEW.model_id)<1 OR length(NEW.model_id)>512
            OR length(NEW.embedding_fingerprint)<>64
            OR NEW.embedding_fingerprint GLOB '*[^0-9a-f]*'
          BEGIN SELECT RAISE(ABORT,'vector_profile_invalid'); END;
          CREATE TRIGGER vector_profile_update_seal BEFORE UPDATE ON vector_profiles
          BEGIN SELECT RAISE(ABORT,'vector_profile_immutable'); END;
          CREATE TRIGGER vector_profile_delete_seal BEFORE DELETE ON vector_profiles
          BEGIN SELECT RAISE(ABORT,'vector_profile_immutable'); END;
          CREATE TRIGGER vector_chunk_insert_seal BEFORE INSERT ON vector_chunks
          WHEN EXISTS (SELECT 1 FROM vector_chunks WHERE hash=NEW.hash
                       AND title=NEW.title AND profile_id=NEW.profile_id AND seq=NEW.seq)
            OR EXISTS (SELECT 1 FROM vector_chunk_sets WHERE hash=NEW.hash
                       AND title=NEW.title AND profile_id=NEW.profile_id)
            OR length(CAST(NEW.title AS BLOB))<1
            OR length(CAST(NEW.title AS BLOB))>5000000
            OR NOT EXISTS (SELECT 1 FROM content WHERE hash=NEW.hash)
            OR NOT EXISTS (SELECT 1 FROM vector_profiles p
                           WHERE p.profile_id=NEW.profile_id
                             AND typeof(NEW.embedding)='blob'
                             AND length(NEW.embedding)=p.dimension*4)
            OR length(NEW.source_evidence_digest)<>64
            OR NEW.source_evidence_digest GLOB '*[^0-9a-f]*'
            OR length(NEW.provenance_digest)<>64
            OR NEW.provenance_digest GLOB '*[^0-9a-f]*'
          BEGIN SELECT RAISE(ABORT,'vector_chunk_invalid'); END;
          CREATE TRIGGER vector_chunk_update_seal BEFORE UPDATE ON vector_chunks
          BEGIN SELECT RAISE(ABORT,'vector_chunk_immutable'); END;
          CREATE TRIGGER vector_chunk_delete_seal BEFORE DELETE ON vector_chunks
          BEGIN SELECT RAISE(ABORT,'vector_chunk_immutable'); END;
          CREATE TRIGGER vector_chunk_set_insert_seal BEFORE INSERT ON vector_chunk_sets
          WHEN EXISTS (SELECT 1 FROM vector_chunk_sets WHERE hash=NEW.hash
                       AND title=NEW.title AND profile_id=NEW.profile_id)
            OR NEW.rows_count IS NOT (SELECT COUNT(*) FROM vector_chunks
                WHERE hash=NEW.hash AND title=NEW.title AND profile_id=NEW.profile_id)
            OR length(NEW.rows_digest)<>64
            OR NEW.rows_digest GLOB '*[^0-9a-f]*'
          BEGIN SELECT RAISE(ABORT,'vector_chunk_set_invalid'); END;
          CREATE TRIGGER vector_chunk_set_update_seal BEFORE UPDATE ON vector_chunk_sets
          BEGIN SELECT RAISE(ABORT,'vector_chunk_set_immutable'); END;
          CREATE TRIGGER vector_chunk_set_delete_seal BEFORE DELETE ON vector_chunk_sets
          BEGIN SELECT RAISE(ABORT,'vector_chunk_set_immutable'); END;
          CREATE TRIGGER gen_vector_row_insert_seal BEFORE INSERT ON gen_vector_rows
          WHEN EXISTS (SELECT 1 FROM gen_vector_rows WHERE gen=NEW.gen
                       AND path=NEW.path AND seq=NEW.seq)
            OR (SELECT state FROM generations WHERE gen=NEW.gen) IS NOT 'prepared'
            OR (SELECT scope FROM generations WHERE gen=NEW.gen) IS NOT NEW.scope
            OR EXISTS (SELECT 1 FROM gen_vector_seals WHERE gen=NEW.gen)
            OR NOT EXISTS (SELECT 1 FROM membership WHERE gen=NEW.gen
                           AND scope=NEW.scope AND path=NEW.path
                           AND hash=NEW.hash AND eligible=1)
            OR NOT EXISTS (SELECT 1 FROM vector_chunks WHERE hash=NEW.hash
                           AND title=NEW.title AND profile_id=NEW.profile_id
                           AND seq=NEW.seq)
            OR NOT EXISTS (SELECT 1 FROM vector_chunk_sets WHERE hash=NEW.hash
                           AND title=NEW.title AND profile_id=NEW.profile_id)
          BEGIN SELECT RAISE(ABORT,'gen_vector_row_invalid'); END;
          CREATE TRIGGER gen_vector_row_update_seal BEFORE UPDATE ON gen_vector_rows
          BEGIN SELECT RAISE(ABORT,'gen_vector_row_immutable'); END;
          CREATE TRIGGER gen_vector_row_delete_seal BEFORE DELETE ON gen_vector_rows
          BEGIN SELECT RAISE(ABORT,'gen_vector_row_immutable'); END;
          CREATE TRIGGER gen_vector_seal_insert_seal BEFORE INSERT ON gen_vector_seals
          WHEN EXISTS (SELECT 1 FROM gen_vector_seals WHERE gen=NEW.gen)
            OR (SELECT state FROM generations WHERE gen=NEW.gen) IS NOT 'prepared'
            OR (SELECT scope FROM generations WHERE gen=NEW.gen) IS NOT NEW.scope
            OR NOT EXISTS (SELECT 1 FROM vector_profiles WHERE profile_id=NEW.profile_id)
            OR length(NEW.rows_digest)<>64 OR NEW.rows_digest GLOB '*[^0-9a-f]*'
            OR NEW.rows_count IS NOT (SELECT COUNT(*) FROM gen_vector_rows WHERE gen=NEW.gen)
            OR EXISTS (SELECT 1 FROM gen_vector_rows WHERE gen=NEW.gen
                       AND (scope IS NOT NEW.scope OR profile_id IS NOT NEW.profile_id))
            OR EXISTS (SELECT 1 FROM membership m WHERE m.gen=NEW.gen AND m.eligible=1
                       AND NOT EXISTS (SELECT 1 FROM gen_vector_rows r
                                       WHERE r.gen=NEW.gen AND r.path=m.path
                                         AND r.hash=m.hash))
          BEGIN SELECT RAISE(ABORT,'gen_vector_seal_invalid'); END;
          CREATE TRIGGER gen_vector_seal_update_seal BEFORE UPDATE ON gen_vector_seals
          BEGIN SELECT RAISE(ABORT,'gen_vector_seal_immutable'); END;
          CREATE TRIGGER gen_vector_seal_delete_seal BEFORE DELETE ON gen_vector_seals
          BEGIN SELECT RAISE(ABORT,'gen_vector_seal_immutable'); END;
          CREATE TRIGGER receipt_insert_scope BEFORE INSERT ON receipts
          WHEN (SELECT scope FROM generations WHERE gen=NEW.gen) IS NOT NEW.scope
            OR (SELECT state FROM generations WHERE gen=NEW.gen) IS NOT 'published'
            OR (NOT EXISTS (SELECT 1 FROM candidate_seals WHERE gen=NEW.gen)
                AND (NEW.source_manifest IS NOT NULL OR NEW.claim_digest IS NOT NULL
                     OR NEW.claim_epoch IS NOT NULL))
            OR EXISTS (SELECT 1 FROM candidate_seals s WHERE s.gen=NEW.gen
                AND (s.scope IS NOT NEW.scope
                     OR NEW.source_manifest IS NOT s.source_manifest
                     OR NEW.claim_digest IS NOT s.claim_digest
                     OR NEW.claim_epoch IS NOT s.claim_epoch))
          BEGIN SELECT RAISE(ABORT,'receipt_scope_invalid'); END;
          CREATE TRIGGER receipt_insert_seal BEFORE INSERT ON receipts
          WHEN EXISTS (SELECT 1 FROM receipts WHERE claim=NEW.claim)
          BEGIN SELECT RAISE(ABORT,'receipt_immutable'); END;
          CREATE TRIGGER receipt_update_seal BEFORE UPDATE ON receipts
          BEGIN SELECT RAISE(ABORT,'receipt_immutable'); END;
          CREATE TRIGGER receipt_delete_seal BEFORE DELETE ON receipts
          BEGIN SELECT RAISE(ABORT,'receipt_immutable'); END;
          CREATE TRIGGER pin_insert_scope BEFORE INSERT ON pins
          WHEN (SELECT scope FROM generations WHERE gen=NEW.gen) IS NOT NEW.scope
            OR (SELECT state FROM generations WHERE gen=NEW.gen) IS NOT 'published'
          BEGIN SELECT RAISE(ABORT,'pin_scope_invalid'); END;
          CREATE TRIGGER pin_insert_seal BEFORE INSERT ON pins
          WHEN EXISTS (SELECT 1 FROM pins WHERE token=NEW.token)
          BEGIN SELECT RAISE(ABORT,'pin_immutable'); END;
          CREATE TRIGGER pin_update_seal BEFORE UPDATE ON pins
          BEGIN SELECT RAISE(ABORT,'pin_immutable'); END;

PRAGMA application_id=1280917809;
PRAGMA user_version=6;
        '''


class LayaIndexA:
    """One DB for control, full membership generations and shared content.

    `scope` is an opaque, already-approved project/worktree/collection identity.
    The caller must not derive it from a mutable branch name or untrusted path.
    """

    APP_ID = 0x4c594131  # LYA1; distinct from QMD database identity
    SCHEMA_VERSION = 6
    REQUIRED_TABLES = frozenset(('schema_meta','content','content_fts','generations','membership',
        'head','receipts','pins','gen_index','exclusions','candidate_seals',
        'vector_profiles','vector_chunks','vector_chunk_sets',
        'gen_vector_rows','gen_vector_seals'))
    REQUIRED_TRIGGERS = frozenset(('membership_insert_seal','membership_update_seal',
        'membership_delete_seal','content_update_seal','content_delete_seal',
        'content_insert_seal','schema_meta_update_seal','schema_meta_delete_seal',
        'schema_meta_insert_seal',
        'head_delete_seal','generation_insert_seal','gen_index_insert_seal',
        'gen_index_positive_ord',
        'receipt_insert_seal','pin_insert_seal',
        'gen_index_update_seal','gen_index_delete_seal','exclusion_update_seal',
        'exclusion_delete_seal','generation_update_seal','generation_delete_seal',
        'head_insert_scope','head_update_scope','receipt_insert_scope',
        'receipt_update_seal','receipt_delete_seal','pin_insert_scope','pin_update_seal',
        'candidate_seal_insert','candidate_seal_update','candidate_seal_delete',
        'vector_profile_insert_seal','vector_profile_update_seal','vector_profile_delete_seal',
        'vector_chunk_insert_seal','vector_chunk_update_seal','vector_chunk_delete_seal',
        'gen_vector_row_insert_seal','gen_vector_row_update_seal','gen_vector_row_delete_seal',
        'gen_vector_seal_insert_seal','gen_vector_seal_update_seal','gen_vector_seal_delete_seal',
        'vector_chunk_set_insert_seal','vector_chunk_set_update_seal',
        'vector_chunk_set_delete_seal'))

    @classmethod
    def _check_header(cls, db):
        if (db.execute('PRAGMA application_id').fetchone()[0] != cls.APP_ID
                or db.execute('PRAGMA user_version').fetchone()[0] != cls.SCHEMA_VERSION):
            raise IndexContractError('schema_version_unknown')
        objects = dict(db.execute("SELECT name,type FROM sqlite_master WHERE type IN ('table','trigger')"))
        if (any(objects.get(name) != 'table' for name in cls.REQUIRED_TABLES)
                or any(objects.get(name) != 'trigger' for name in cls.REQUIRED_TRIGGERS)):
            raise IndexContractError('schema_manifest_mismatch')
        cols = [row[1] for row in db.execute('PRAGMA table_info(generations)')]
        if cols != ['gen','scope','state','digest','rev_epoch']:
            raise IndexContractError('schema_manifest_mismatch')
        saved = db.execute("SELECT value FROM schema_meta WHERE key='static_manifest'").fetchone()
        if saved is None or saved[0] != cls._static_schema_digest(db):
            raise IndexContractError('schema_manifest_mismatch')
        incarnation = db.execute(
            "SELECT value FROM schema_meta WHERE key='incarnation'").fetchone()
        if (incarnation is None or not isinstance(incarnation[0], str)
                or re.fullmatch(r'[0-9a-f]{32}', incarnation[0]) is None):
            raise IndexContractError('schema_manifest_mismatch')
        # Only the five FTS5 tables for each allocated generation may vary.
        ordinals = {row[0] for row in db.execute('SELECT ord FROM gen_index')}
        suffixes = ('', '_data', '_idx', '_content', '_docsize', '_config')
        expected = {f'gen_fts_{ordinal}{suffix}' for ordinal in ordinals
                    for suffix in suffixes}
        actual = {name for (name,) in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name GLOB 'gen_fts_*'")}
        if actual != expected:
            raise IndexContractError('schema_manifest_mismatch')
        for ordinal in ordinals:
            table = _gen_fts(ordinal)
            definition = db.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            if definition is None or definition[0] != _gen_fts_ddl(table):
                raise IndexContractError('schema_manifest_mismatch')

    @staticmethod
    def _static_schema_digest(db):
        # Generation FTS tables and their shadow tables are created dynamically.
        dynamic = re.compile(r'gen_fts_[1-9][0-9]*(?:_(?:data|idx|content|docsize|config))?$')
        rows = db.execute('SELECT type,name,tbl_name,sql FROM sqlite_master').fetchall()
        static = sorted(row for row in rows if not row[1].startswith('sqlite_')
                        and not (row[0] == 'table' and dynamic.fullmatch(row[1])))
        return hashlib.sha256(json.dumps(static, separators=(',', ':'),
                                         ensure_ascii=False).encode('utf-8')).hexdigest()

    @classmethod
    def _create_new(cls, path):
        if path.exists() or path.is_symlink():
            raise IndexContractError('database_already_exists')
        if any(Path(str(path) + suffix).exists() or Path(str(path) + suffix).is_symlink()
               for suffix in ('-wal','-shm','-journal')):
            raise IndexContractError('stale_database_sidecar')
        tmp = path.parent / ('.' + path.name + '.create-' + uuid.uuid4().hex)
        fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        try:
            with closing(sqlite3.connect(tmp, timeout=2, isolation_level=None)) as db:
                db.execute('PRAGMA foreign_keys=ON')
                # Build a self-contained file with rollback journaling. WAL is
                # enabled and checked only after no-clobber publication below.
                if str(db.execute('PRAGMA journal_mode=DELETE').fetchone()[0]).lower() != 'delete':
                    raise IndexContractError('creation_journal_unavailable')
                db.execute('PRAGMA synchronous=FULL')
                db.executescript(_SCHEMA_V6)
                db.execute("INSERT INTO schema_meta VALUES('static_manifest',?)",
                           (cls._static_schema_digest(db),))
                db.execute("INSERT INTO schema_meta VALUES('incarnation',?)",
                           (uuid.uuid4().hex,))
                db.commit()
                cls._check_header(db)
            if any(Path(str(tmp) + suffix).exists() for suffix in ('-wal','-shm','-journal')):
                raise IndexContractError('creation_sidecar_unsettled')
            fd = os.open(tmp, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            # Atomic no-clobber publication. The temp never contains user content.
            os.link(tmp, path)
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            tmp.unlink(missing_ok=True)
            for suffix in ('-wal','-shm','-journal'):
                Path(str(tmp) + suffix).unlink(missing_ok=True)

    def __init__(self, path, *, create=False):
        path = Path(path)
        if not path.parent.is_dir() or path.is_symlink():
            raise IndexContractError('invalid_explicit_path')
        self.path = path
        if create:
            self._create_new(path)
        if not path.is_file():
            raise IndexContractError('database_missing')
        uri = path.resolve(strict=True).as_uri()
        try:
            with closing(sqlite3.connect(uri + '?mode=ro', uri=True, timeout=2)) as check:
                check.execute('BEGIN')
                try:
                    self._check_header(check)  # one snapshot; no DDL or WAL switch
                    ro_incarnation = check.execute(
                        "SELECT value FROM schema_meta WHERE key='incarnation'").fetchone()[0]
                    check.commit()
                except BaseException:
                    check.rollback()
                    raise
        except sqlite3.Error as exc:
            raise IndexContractError('database_unreadable') from exc
        self.db = sqlite3.connect(uri + '?mode=rw', uri=True, timeout=2,
                                  isolation_level=None)
        try:
            self.db.execute('PRAGMA foreign_keys=ON')
            if self._read_header_snapshot() != ro_incarnation:
                raise IndexContractError('database_incarnation_mismatch')
            mode = self.db.execute('PRAGMA journal_mode=WAL').fetchone()
            if mode is None or str(mode[0]).lower() != 'wal':
                raise IndexContractError('wal_unavailable')
            self.db.execute('PRAGMA synchronous=FULL')
            if self._read_header_snapshot() != ro_incarnation:
                raise IndexContractError('database_incarnation_mismatch')
        except BaseException:
            self.db.close()
            raise

    def close(self):
        self.db.close()

    def incarnation(self):
        """Stable identity for a newly created A-int database, not its file path."""
        self.db.execute('BEGIN')
        try:
            self._check_header(self.db)
            value = self.db.execute(
                "SELECT value FROM schema_meta WHERE key='incarnation'").fetchone()[0]
            self.db.commit()
            return value
        except BaseException:
            self._rollback()
            raise

    def _read_header_snapshot(self):
        self.db.execute('BEGIN')
        try:
            self._check_header(self.db)
            incarnation = self.db.execute(
                "SELECT value FROM schema_meta WHERE key='incarnation'").fetchone()[0]
            self.db.commit()
            return incarnation
        except BaseException:
            self.db.rollback()
            raise

    def _begin(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            self._check_header(self.db)
        except BaseException:
            self.db.rollback()
            raise

    def _generation_fts_matches(self, gen, *, deadline=None):
        row = self.db.execute('SELECT ord FROM gen_index WHERE gen=?', (gen,)).fetchone()
        if row is None:
            return False
        table = _gen_fts(row[0])
        if self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                           (table,)).fetchone() is None:
            return False
        return _generation_fts_rows_match(self.db, gen, table,
                                          deadline=deadline)

    def _rollback(self):
        try:
            self.db.rollback()
        except sqlite3.Error:
            pass  # never replace the exception that is already propagating

    def stage(self, gen, scope, documents, fault_after=None, *,
              base=None, source_manifest=None, claim_digest=None, claim_epoch=None):
        """Atomically store a full view; optional product seal freezes source/base/claim."""
        sealed = source_manifest is not None
        if sealed:
            if (not isinstance(source_manifest, str) or len(source_manifest) != 64
                    or not isinstance(claim_digest, str) or len(claim_digest) != 64
                    or type(claim_epoch) is not int or claim_epoch < 0
                    or (base is not None and (not isinstance(base, str) or not base))):
                raise IndexContractError('invalid_candidate_seal')
        elif base is not None or claim_digest is not None or claim_epoch is not None:
            raise IndexContractError('invalid_candidate_seal')
        if not isinstance(gen, str) or not gen or not isinstance(scope, str) or not scope:
            raise IndexContractError('invalid_identity')
        rows = []
        seen = set()
        for path, body, eligible in documents:
            if (not _valid_relative_path(path) or path in seen
                    or not isinstance(body, str) or type(eligible) is not bool):
                raise IndexContractError('invalid_document')
            seen.add(path)
            hash_ = hashlib.sha256(body.encode('utf-8')).hexdigest()
            rows.append((path, hash_, int(eligible), body))
        digest = _digest(scope, [(path, hash_, flag) for path, hash_, flag, _ in rows])
        if sealed and source_manifest != digest:
            raise IndexContractError('manifest_mismatch')
        self._begin()
        try:
            prior = self.db.execute('SELECT scope,state,digest,rev_epoch FROM generations WHERE gen=?',
                                    (gen,)).fetchone()
            if prior is not None:
                stored = self.db.execute('SELECT path,hash,eligible FROM membership WHERE gen=?',
                                         (gen,)).fetchall()
                seal = self.db.execute('''SELECT scope,base_gen,source_manifest,claim_digest,
                         claim_epoch,entries_digest FROM candidate_seals WHERE gen=?''', (gen,)).fetchone()
                expected_seal = ((scope,base,source_manifest,claim_digest,claim_epoch,digest)
                                 if sealed else None)
                if (prior[0] == scope and prior[1] in ('prepared', 'published')
                        and prior[2] == digest and _digest(scope, stored) == digest
                        and seal == expected_seal and self._generation_fts_matches(gen)):
                    current_epoch = self.db.execute(
                        'SELECT COUNT(*) FROM exclusions WHERE scope=?',
                        (scope,)).fetchone()[0]
                    self.db.rollback()
                    return ('already_staged' if prior[3] == current_epoch
                            else 'index_stale')
                raise IndexContractError('generation_conflict')
            if sealed:
                head = self.db.execute('SELECT gen FROM head WHERE scope=?', (scope,)).fetchone()
                if (head[0] if head else None) != base:
                    raise IndexContractError('head_moved')
            rev_epoch = self.db.execute(
                'SELECT COUNT(*) FROM exclusions WHERE scope=?', (scope,)).fetchone()[0]
            self.db.execute('INSERT INTO generations(gen,scope,state,digest,rev_epoch) VALUES(?,?,?,?,?)',
                            (gen, scope, 'building', digest, rev_epoch))
            if sealed:
                self.db.execute('''INSERT INTO candidate_seals
                    VALUES(?,?,?,?,?,?,?)''',
                    (gen,scope,base,source_manifest,claim_digest,claim_epoch,digest))
            ordinal = self.db.execute('INSERT INTO gen_index(gen) VALUES(?)', (gen,)).lastrowid
            table = _gen_fts(ordinal)
            self.db.execute(_gen_fts_ddl(table))
            for n, (path, hash_, flag, body) in enumerate(rows, 1):
                if flag and self.db.execute('''SELECT 1 FROM exclusions
                    WHERE scope=? AND ((kind='path' AND value=?) OR
                                       (kind='hash' AND value=?))''',
                    (scope, path, hash_)).fetchone():
                    raise IndexContractError('excluded_document')
                existing = self.db.execute('SELECT body FROM content WHERE hash=?',
                                           (hash_,)).fetchone()
                if existing is None:
                    self.db.execute('INSERT INTO content VALUES(?,?)', (hash_, body))
                    self.db.execute('INSERT INTO content_fts(hash,body) VALUES(?,?)',
                                    (hash_, body))
                elif existing[0] != body:
                    raise IndexContractError('hash_content_conflict')
                self.db.execute('INSERT INTO membership VALUES(?,?,?,?,?)',
                                (gen, scope, path, hash_, flag))
                if flag:
                    self.db.execute(
                        f'INSERT INTO {table}(filepath,title,body,path_key) VALUES(?,?,?,?)',
                        (_qmd_fts_text(path),
                         _qmd_fts_text(_qmd_title(body, path)),
                         _qmd_fts_text(body), path))
                if fault_after == n:
                    raise InjectedFault('stage_after_member')
            self.db.execute("UPDATE generations SET state='prepared' WHERE gen=?", (gen,))
            self.db.commit()
            return 'prepared'
        except BaseException:
            self._rollback()
            raise

    def stage_stream(self, gen, scope, documents, fault_after=None, *,
                     base, source_manifest, claim_digest, claim_epoch):
        """Sealed ``stage`` that consumes ``documents`` one at a time.

        Same seal, base, exclusion, content-immutability and replay rules as
        ``stage``, but no body list is built: each ``(path, body, eligible)``
        is validated, hashed and written as it arrives, and the manifest
        digest is computed incrementally, so paths must be strictly
        increasing. Peak memory is one body plus SQLite's cache. It is still
        ONE write transaction over the whole corpus: the A-int write lock is
        held while the caller re-reads every member (a concurrent ``pin`` or
        ``release`` raises SQLite busy after the 2 s timeout, which
        ``LayaIndexRuntime.query_bound`` reports as ``pending``), and any failure,
        including a digest that differs from ``source_manifest`` after the
        last row, rolls everything back. An identical prepared or published
        generation is compared row by row against the re-read documents
        (never by manifest alone) and returns ``already_staged``.
        """
        if (not isinstance(source_manifest, str) or len(source_manifest) != 64
                or not isinstance(claim_digest, str) or len(claim_digest) != 64
                or type(claim_epoch) is not int or claim_epoch < 0
                or (base is not None and (not isinstance(base, str) or not base))):
            raise IndexContractError('invalid_candidate_seal')
        if not isinstance(gen, str) or not gen or not isinstance(scope, str) or not scope:
            raise IndexContractError('invalid_identity')
        digest = _StreamDigest(scope)
        last = None

        def entries():
            nonlocal last
            for path, body, eligible in documents:
                if (not _valid_relative_path(path) or (last is not None and path <= last)
                        or not isinstance(body, str) or type(eligible) is not bool):
                    raise IndexContractError('invalid_document')
                last = path
                hash_ = hashlib.sha256(body.encode('utf-8')).hexdigest()
                digest.add(path, hash_, int(eligible))
                yield path, hash_, int(eligible), body

        self._begin()
        try:
            prior = self.db.execute('SELECT scope,state,digest,rev_epoch FROM generations WHERE gen=?',
                                    (gen,)).fetchone()
            if prior is not None:
                seal = self.db.execute('''SELECT scope,base_gen,source_manifest,claim_digest,
                         claim_epoch,entries_digest FROM candidate_seals WHERE gen=?''', (gen,)).fetchone()
                if (prior[0] != scope or prior[1] not in ('prepared', 'published')
                        or prior[2] != source_manifest
                        or seal != (scope, base, source_manifest, claim_digest, claim_epoch,
                                    source_manifest)):
                    raise IndexContractError('generation_conflict')
                stored = self.db.execute('SELECT path,hash,eligible FROM membership '
                                         'WHERE gen=? ORDER BY path', (gen,))
                same = True
                for path, hash_, flag, _body in entries():
                    same = same and stored.fetchone() == (path, hash_, flag)
                if digest.hexdigest() != source_manifest:
                    raise IndexContractError('manifest_mismatch')
                ordinal = self.db.execute('SELECT ord FROM gen_index WHERE gen=?',
                                          (gen,)).fetchone()
                if (not same or stored.fetchone() is not None or ordinal is None
                        or not _generation_fts_rows_stream_match(
                            self.db, gen, _gen_fts(ordinal[0]))):
                    raise IndexContractError('generation_conflict')
                current_epoch = self.db.execute(
                    'SELECT COUNT(*) FROM exclusions WHERE scope=?', (scope,)).fetchone()[0]
                self.db.rollback()
                return 'already_staged' if prior[3] == current_epoch else 'index_stale'
            head = self.db.execute('SELECT gen FROM head WHERE scope=?', (scope,)).fetchone()
            if (head[0] if head else None) != base:
                raise IndexContractError('head_moved')
            rev_epoch = self.db.execute(
                'SELECT COUNT(*) FROM exclusions WHERE scope=?', (scope,)).fetchone()[0]
            # The digest is checked after the last row; until then the generation
            # carries the claimed manifest, and a mismatch rolls the insert back.
            self.db.execute('INSERT INTO generations(gen,scope,state,digest,rev_epoch) VALUES(?,?,?,?,?)',
                            (gen, scope, 'building', source_manifest, rev_epoch))
            self.db.execute('''INSERT INTO candidate_seals
                VALUES(?,?,?,?,?,?,?)''',
                (gen,scope,base,source_manifest,claim_digest,claim_epoch,source_manifest))
            ordinal = self.db.execute('INSERT INTO gen_index(gen) VALUES(?)', (gen,)).lastrowid
            table = _gen_fts(ordinal)
            self.db.execute(_gen_fts_ddl(table))
            for n, (path, hash_, flag, body) in enumerate(entries(), 1):
                if flag and self.db.execute('''SELECT 1 FROM exclusions
                    WHERE scope=? AND ((kind='path' AND value=?) OR
                                       (kind='hash' AND value=?))''',
                    (scope, path, hash_)).fetchone():
                    raise IndexContractError('excluded_document')
                existing = self.db.execute('SELECT body FROM content WHERE hash=?',
                                           (hash_,)).fetchone()
                if existing is None:
                    self.db.execute('INSERT INTO content VALUES(?,?)', (hash_, body))
                    self.db.execute('INSERT INTO content_fts(hash,body) VALUES(?,?)',
                                    (hash_, body))
                elif existing[0] != body:
                    raise IndexContractError('hash_content_conflict')
                self.db.execute('INSERT INTO membership VALUES(?,?,?,?,?)',
                                (gen, scope, path, hash_, flag))
                if flag:
                    self.db.execute(
                        f'INSERT INTO {table}(filepath,title,body,path_key) VALUES(?,?,?,?)',
                        (_qmd_fts_text(path),
                         _qmd_fts_text(_qmd_title(body, path)),
                         _qmd_fts_text(body), path))
                if fault_after == n:
                    raise InjectedFault('stage_after_member')
            if digest.hexdigest() != source_manifest:
                raise IndexContractError('manifest_mismatch')
            self.db.execute("UPDATE generations SET state='prepared' WHERE gen=?", (gen,))
            self.db.commit()
            return 'prepared'
        except BaseException:
            self._rollback()
            raise

    def publish(self, expected, gen, claim, fault_at=None, *,
                source_manifest=None, claim_digest=None, claim_epoch=None,
                require_vector=False):
        """CAS a bound base/source/claim + receipt; separate queue ACK is replayable."""
        proof = (source_manifest,claim_digest,claim_epoch)
        if type(require_vector) is not bool:
            raise IndexContractError('invalid_vector_requirement')
        if not isinstance(claim, str) or not claim:
            raise IndexContractError('invalid_claim')
        if any(value is not None for value in proof) and (
                not isinstance(source_manifest, str) or len(source_manifest) != 64
                or not isinstance(claim_digest, str) or len(claim_digest) != 64
                or type(claim_epoch) is not int or claim_epoch < 0):
            raise IndexContractError('invalid_claim_proof')
        self._begin()
        try:
            prior = self.db.execute('''SELECT scope,gen,source_manifest,claim_digest,claim_epoch
                FROM receipts WHERE claim=?''', (claim,)).fetchone()
            candidate = self.db.execute('SELECT scope,state,rev_epoch FROM generations WHERE gen=?',
                                        (gen,)).fetchone()
            seal = self.db.execute('''SELECT base_gen,source_manifest,claim_digest,claim_epoch
                FROM candidate_seals WHERE gen=?''', (gen,)).fetchone()
            vector_seal = self.db.execute('SELECT 1 FROM gen_vector_seals WHERE gen=?',
                                          (gen,)).fetchone()
            if require_vector and vector_seal is None:
                raise IndexContractError('vector_generation_unsealed')
            if vector_seal is not None:
                # The verifier reads this exact write transaction. A forged or
                # incomplete seal must never accompany a receipt or new head.
                from laya_index_vector import verify_generation_seal
                verify_generation_seal(self, gen)
            if prior is not None:
                self.db.rollback()
                if candidate is not None and prior == (candidate[0], gen, *proof):
                    return 'replayed'
                raise IndexContractError('claim_conflict')
            if candidate is None or candidate[1] != 'prepared':
                self.db.rollback()
                return 'not_prepared'
            scope = candidate[0]
            if seal is not None:
                if any(value is None for value in proof):
                    raise IndexContractError('claim_conflict')
                if source_manifest != seal[1]:
                    self.db.rollback()
                    return 'index_stale'
                if (claim_digest, claim_epoch) != seal[2:] or expected != seal[0]:
                    raise IndexContractError('claim_conflict')
            elif any(value is not None for value in proof):
                raise IndexContractError('claim_conflict')
            if candidate[2] != self.db.execute(
                    'SELECT COUNT(*) FROM exclusions WHERE scope=?', (scope,)).fetchone()[0]:
                self.db.rollback()
                return 'index_stale'
            current = self.db.execute('SELECT gen FROM head WHERE scope=?', (scope,)).fetchone()
            if (current[0] if current else None) != expected:
                self.db.rollback()
                return 'stale'
            self.db.execute("UPDATE generations SET state='published' WHERE gen=?", (gen,))
            self.db.execute('''INSERT INTO receipts
                (claim,scope,gen,source_manifest,claim_digest,claim_epoch)
                VALUES(?,?,?,?,?,?)''', (claim, scope, gen, *proof))
            if fault_at == 'after_receipt':
                raise InjectedFault('publish_after_receipt')
            self.db.execute('INSERT INTO head VALUES(?,?) ON CONFLICT(scope) DO UPDATE SET gen=excluded.gen',
                            (scope, gen))
            if fault_at == 'after_head':
                raise InjectedFault('publish_after_head')
            self.db.commit()
            return 'published'
        except BaseException:
            self._rollback()
            raise

    def pin(self, scope, fault_at=None):
        self._begin()
        try:
            row = self.db.execute('SELECT gen FROM head WHERE scope=?', (scope,)).fetchone()
            if row is None:
                raise IndexContractError('no_published_generation')
            epoch = self.db.execute('SELECT rev_epoch FROM generations WHERE gen=? AND scope=?',
                                    (row[0], scope)).fetchone()
            current = self.db.execute('SELECT COUNT(*) FROM exclusions WHERE scope=?',
                                      (scope,)).fetchone()[0]
            if epoch is None or epoch[0] != current:
                raise IndexContractError('index_stale')
            token = uuid.uuid4().hex
            self.db.execute('INSERT INTO pins VALUES(?,?,?)', (token, scope, row[0]))
            if fault_at == 'after_insert':
                raise InjectedFault('pin_after_insert')
            self.db.commit()
            return token, row[0]
        except BaseException:
            self._rollback()
            raise

    def exclude(self, scope, path=None, hash_=None):
        """Append-only serving block, including old pins. Never deletes bytes."""
        value = path if hash_ is None else hash_
        if (not isinstance(scope, str) or not scope or (path is None) == (hash_ is None)
                or not isinstance(value, str) or not value):
            raise IndexContractError('invalid_exclusion')
        kind = 'path' if hash_ is None else 'hash'
        if kind == 'hash':
            if len(value) != 64 or any(ch not in '0123456789abcdef' for ch in value):
                raise IndexContractError('invalid_exclusion')
        elif not _valid_relative_path(value):
            raise IndexContractError('invalid_exclusion')
        self._begin()
        try:
            cur = self.db.execute('INSERT OR IGNORE INTO exclusions VALUES(?,?,?)',
                                  (scope, kind, value))
            self.db.commit()
            return 'excluded' if cur.rowcount else 'already_excluded'
        except BaseException:
            self._rollback()
            raise

    def list_exclusions(self, scope):
        if not isinstance(scope, str) or not scope:
            raise IndexContractError('invalid_scope')
        self.db.execute('BEGIN')
        try:
            self._check_header(self.db)
            rows = self.db.execute('SELECT scope,kind,value FROM exclusions WHERE scope=? '
                                   'ORDER BY kind,value', (scope,)).fetchall()
            self.db.commit()
            return rows
        except BaseException:
            self._rollback()
            raise

    def _pinned(self, token):
        row = self.db.execute("""
          SELECT p.scope,p.gen,i.ord,g.rev_epoch FROM pins p
          JOIN generations g ON g.gen=p.gen AND g.scope=p.scope
          LEFT JOIN gen_index i ON i.gen=p.gen
          WHERE p.token=?""", (token,)).fetchone()
        if row is None:
            raise IndexContractError('pin_missing')
        scope, gen, ordinal, generation_epoch = row
        current_epoch = self.db.execute(
            'SELECT COUNT(*) FROM exclusions WHERE scope=?', (scope,)).fetchone()[0]
        # An old FTS ranking population still contains newly excluded text.
        # Fail the whole query rather than return stale/partially ranked hits.
        if generation_epoch != current_epoch:
            raise IndexContractError('index_stale')
        return scope, gen, ordinal

    def search(self, token, fts_query, limit, *, path_prefixes=None):
        """Use only this pinned generation's FTS statistics and eligible rows."""
        return self.search_many(token, (fts_query,), limit,
                                path_prefixes=path_prefixes)[0]

    def search_many(self, token, fts_queries, limit, *, path_prefixes=None,
                    deadline_seconds=None):
        """Run at most eight searches after one sealed-generation FTS check."""
        if (not isinstance(fts_queries, tuple) or not 1 <= len(fts_queries) <= 8
                or any(not isinstance(query, str) or not query for query in fts_queries)
                or type(limit) is not int or not 1 <= limit <= 100
                or (deadline_seconds is not None and
                    (type(deadline_seconds) not in (int, float)
                     or not math.isfinite(deadline_seconds)
                     or not 0 < deadline_seconds <= 30))):
            raise IndexContractError('invalid_query')
        prefix_sql, prefix_params = _path_predicate(path_prefixes)
        deadline = (None if deadline_seconds is None else
                    time.monotonic() + deadline_seconds)
        if deadline is not None:
            self.db.set_progress_handler(
                lambda: int(time.monotonic() >= deadline), 1000)
        try:
            self.db.execute('BEGIN')
            self._check_header(self.db)
            scope, gen, ordinal = self._pinned(token)
            if ordinal is None:
                raise IndexContractError('generation_unindexed')
            if not self._generation_fts_matches(gen, deadline=deadline):
                raise IndexContractError('fts_generation_mismatch')
            table = _gen_fts(ordinal)
            answer = []
            for fts_query in fts_queries:
                if deadline is not None and time.monotonic() >= deadline:
                    raise IndexContractError('fts_deadline_exceeded')
                results = self.db.execute(f"""
                  SELECT m.path,m.hash,c.body,bm25({table},1.5,4.0,1.0,0.0) AS rank
                  FROM {table}
                  JOIN membership m ON m.gen=? AND m.path={table}.path_key
                  JOIN content c ON c.hash=m.hash
                  WHERE {table} MATCH ? AND m.scope=? AND m.eligible=1 AND {_NOT_EXCLUDED}
                  {prefix_sql}
                  ORDER BY rank ASC,m.path COLLATE BINARY ASC LIMIT ?
                """, (gen, fts_query, scope, *prefix_params, limit)).fetchall()
                if deadline is not None and time.monotonic() >= deadline:
                    raise IndexContractError('fts_deadline_exceeded')
                answer.append([{'generation': gen, 'scope': scope, 'path': path,
                                'hash': hash_, 'body': body, 'score': rank}
                               for path, hash_, body, rank in results])
            self.db.commit()
            return answer
        except sqlite3.OperationalError as exc:
            self._rollback()
            if deadline is not None and time.monotonic() >= deadline:
                raise IndexContractError('fts_deadline_exceeded') from exc
            raise
        except BaseException:
            self._rollback()
            raise
        finally:
            if deadline is not None:
                self.db.set_progress_handler(None, 0)

    def search_scored(self, token, distances, limit):
        """Expand supplied hash scores to scoped documents before top-K.

        This tests vector candidate semantics; it does not calculate embeddings
        or assert an ANN/exact scorer's coverage. Smaller distance ranks first.
        """
        if (type(limit) is not int or not 1 <= limit <= 100
                or not isinstance(distances, dict) or len(distances) > 498
                or any(not isinstance(key, str) or not key
                       or type(value) not in (int, float) or abs(value) > 1e9
                       or not math.isfinite(value)
                       for key, value in distances.items())):
            raise IndexContractError('invalid_scored_query')
        self.db.execute('BEGIN')
        try:
            self._check_header(self.db)
            scope, gen, _ = self._pinned(token)
            if not distances:
                self.db.commit()
                return []
            items = sorted(distances.items())
            placeholders = ','.join('(?,?)' for _ in items)
            params = [value for item in items for value in item]
            rows = self.db.execute('''
              WITH scores(hash,distance) AS (VALUES ''' + placeholders + ''')
              SELECT m.path,m.hash,c.body,scores.distance
              FROM scores
              JOIN membership m ON m.hash=scores.hash
              JOIN content c ON c.hash=m.hash
              WHERE m.gen=? AND m.scope=? AND m.eligible=1 AND ''' + _NOT_EXCLUDED + '''
              ORDER BY scores.distance ASC,m.path COLLATE BINARY ASC LIMIT ?
            ''', (*params, gen, scope, limit)).fetchall()
            self.db.commit()
            return [{'generation': gen, 'scope': scope, 'path': path,
                     'hash': hash_, 'body': body, 'score': distance}
                    for path, hash_, body, distance in rows]
        except BaseException:
            self._rollback()
            raise

    def view_seal(self, token):
        """Read a sealed pinned view; never silently serve an unsealed prototype."""
        self.db.execute('BEGIN')
        try:
            self._check_header(self.db)
            scope, gen, _ = self._pinned(token)
            row = self.db.execute('SELECT source_manifest FROM candidate_seals WHERE gen=?',
                                  (gen,)).fetchone()
            if row is None:
                raise IndexContractError('unsealed_view')
            epoch = self.db.execute('SELECT rev_epoch FROM generations WHERE gen=?',
                                    (gen,)).fetchone()[0]
            self.db.commit()
            return {'scope':scope,'gen':gen,'source_manifest':row[0],'rev_epoch':epoch}
        except BaseException:
            self._rollback()
            raise

    def receipt_detail(self, claim):
        self.db.execute('BEGIN')
        try:
            self._check_header(self.db)
            row = self.db.execute('''SELECT scope,gen,source_manifest,claim_digest,claim_epoch
                FROM receipts WHERE claim=?''', (claim,)).fetchone()
            self.db.commit()
            return row
        except BaseException:
            self._rollback()
            raise

    def release(self, token):
        self._begin()
        try:
            self.db.execute('DELETE FROM pins WHERE token=?', (token,))
            self.db.commit()
        except BaseException:
            self._rollback()
            raise

    def receipt(self, claim):
        self.db.execute('BEGIN')
        try:
            self._check_header(self.db)
            row = self.db.execute('SELECT scope,gen FROM receipts WHERE claim=?',
                                  (claim,)).fetchone()
            self.db.commit()
            return row
        except BaseException:
            self._rollback()
            raise

    def gc_plan(self, fault_at=None):
        """Read-only reference census. No result authorizes deletion."""
        self.db.execute('BEGIN')  # deferred: one read snapshot, no writer lock
        try:
            self._check_header(self.db)
            heads = {r[0] for r in self.db.execute('SELECT gen FROM head')}
            pins = {r[0] for r in self.db.execute('SELECT gen FROM pins')}
            prepared = {r[0] for r in self.db.execute(
                "SELECT gen FROM generations WHERE state='prepared'")}
            generations = {r[0] for r in self.db.execute('SELECT gen FROM generations')}
            receipted = {r[0] for r in self.db.execute('SELECT gen FROM receipts')}
            bases = {r[0] for r in self.db.execute(
                'SELECT base_gen FROM candidate_seals WHERE base_gen IS NOT NULL')}
            active = heads | pins | prepared
            retained = receipted | bases
            unrooted = generations - active - retained
            result = {'roots': sorted(active | retained),
                      'unrooted_candidates': sorted(unrooted),
                      'receipt_referenced': sorted(receipted - active),
                      'base_referenced': sorted(bases - active),
                      'deletion_enabled': False}
            if fault_at == 'after_roots':
                raise InjectedFault('gc_after_roots')
            self.db.commit()
            return result
        except BaseException:
            self._rollback()
            raise

    def inspect(self):
        """Read-only logical recovery checks; no pin expiry or repair implied."""
        self.db.execute('BEGIN')  # one read snapshot for every check and count
        try:
            self._check_header(self.db)
            if self.db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise IndexContractError('sqlite_integrity')
            if self.db.execute('PRAGMA foreign_key_check').fetchone() is not None:
                raise IndexContractError('foreign_key_integrity')
            orphan = self.db.execute('''SELECT DISTINCT gen FROM membership
                                        EXCEPT SELECT gen FROM generations''').fetchall()
            if orphan:
                raise IndexContractError('orphan_membership')
            if self.db.execute('SELECT COUNT(*) FROM content').fetchone()[0] != self.db.execute(
                    'SELECT COUNT(*) FROM content_fts').fetchone()[0]:
                raise IndexContractError('fts_content_mismatch')
            if (self.db.execute('SELECT hash,body FROM content EXCEPT '
                                'SELECT hash,body FROM content_fts').fetchone() is not None
                    or self.db.execute('SELECT hash,body FROM content_fts EXCEPT '
                                       'SELECT hash,body FROM content').fetchone() is not None):
                raise IndexContractError('fts_content_mismatch')
            if self.db.execute("SELECT 1 FROM generations WHERE state='building'").fetchone():
                raise IndexContractError('unfinished_generation')
            if self.db.execute('''SELECT 1 FROM head h LEFT JOIN generations g ON g.gen=h.gen
                    WHERE g.gen IS NULL OR g.scope<>h.scope OR g.state<>'published' LIMIT 1''').fetchone():
                raise IndexContractError('head_integrity')
            if self.db.execute('''SELECT 1 FROM receipts r LEFT JOIN generations g ON g.gen=r.gen
                    WHERE g.gen IS NULL OR g.scope<>r.scope OR g.state<>'published' LIMIT 1''').fetchone():
                raise IndexContractError('receipt_integrity')
            if self.db.execute('''SELECT 1 FROM generations g WHERE g.state='published'
                    AND NOT EXISTS(SELECT 1 FROM receipts r WHERE r.gen=g.gen) LIMIT 1''').fetchone():
                raise IndexContractError('published_without_receipt')
            if self.db.execute('''SELECT 1 FROM pins p LEFT JOIN generations g ON g.gen=p.gen
                    WHERE g.gen IS NULL OR g.scope<>p.scope OR g.state<>'published' LIMIT 1''').fetchone():
                raise IndexContractError('pin_integrity')
            if self.db.execute('''SELECT 1 FROM receipts r LEFT JOIN candidate_seals s
                    ON s.gen=r.gen WHERE
                    (s.gen IS NULL AND (r.source_manifest IS NOT NULL
                        OR r.claim_digest IS NOT NULL OR r.claim_epoch IS NOT NULL))
                    OR (s.gen IS NOT NULL AND (r.source_manifest IS NOT s.source_manifest
                        OR r.claim_digest IS NOT s.claim_digest
                        OR r.claim_epoch IS NOT s.claim_epoch)) LIMIT 1''').fetchone():
                raise IndexContractError('receipt_proof_integrity')
            if self.db.execute('''SELECT 1 FROM candidate_seals s
                    LEFT JOIN generations b ON b.gen=s.base_gen WHERE s.base_gen IS NOT NULL
                    AND (b.gen IS NULL OR b.scope<>s.scope) LIMIT 1''').fetchone():
                raise IndexContractError('candidate_base_integrity')
            for gen, scope, digest in self.db.execute('SELECT gen,scope,digest FROM generations'):
                entries = self.db.execute('SELECT path,hash,eligible FROM membership WHERE gen=?',
                                          (gen,)).fetchall()
                if _digest(scope, entries) != digest:
                    raise IndexContractError('membership_digest')
            for gen, ordinal in self.db.execute('''SELECT g.gen,i.ord FROM generations g
                    LEFT JOIN gen_index i ON i.gen=g.gen''').fetchall():
                if ordinal is None:
                    raise IndexContractError('generation_unindexed')
                table = _gen_fts(ordinal)
                definition = self.db.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
                ).fetchone()
                if definition is None:
                    raise IndexContractError('generation_fts_missing')
                if definition[0] != _gen_fts_ddl(table):
                    raise IndexContractError('generation_fts_definition')
                if not _generation_fts_rows_match(self.db, gen, table):
                    raise IndexContractError('generation_fts_mismatch')
            for gen,scope,digest,base,manifest,claim_digest,claim_epoch,entries_digest in self.db.execute(
                    '''SELECT g.gen,g.scope,g.digest,s.base_gen,s.source_manifest,s.claim_digest,
                       s.claim_epoch,s.entries_digest FROM generations g
                       JOIN candidate_seals s ON s.gen=g.gen'''):
                if scope != self.db.execute('SELECT scope FROM candidate_seals WHERE gen=?',
                                             (gen,)).fetchone()[0] or digest != entries_digest:
                    raise IndexContractError('candidate_seal_mismatch')
            from laya_index_vector import verify_generation_seal
            for (gen,) in self.db.execute('SELECT gen FROM gen_vector_seals').fetchall():
                verify_generation_seal(self, gen)
            result = {'generations': self.db.execute('SELECT COUNT(*) FROM generations').fetchone()[0],
                      'pins': self.db.execute('SELECT COUNT(*) FROM pins').fetchone()[0],
                      'deletion_enabled': False}
            self.db.commit()
            return result
        except BaseException:
            self._rollback()
            raise

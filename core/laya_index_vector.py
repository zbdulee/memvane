"""Precomputed, exact cosine vectors in an A-int v6 database.

The caller supplies vectors and an approved provider identity. This module never
loads a model, and the hashes here prove row consistency, not model provenance.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import struct
import time
import unicodedata

from laya_index_a import IndexContractError, _NOT_EXCLUDED, _path_predicate


_HEX = re.compile(r'[0-9a-f]{64}\Z')
MAX_DIMENSION = 2048
MAX_CANDIDATES = 20_000
# The generation seal streams rows; MAX_CANDIDATES remains the per-document
# chunk-set and bounded query/work limit, not a whole-generation ceiling.
MAX_GENERATION_SEAL_ROWS = None
MAX_FLOAT_WORK = 4_000_000
MAX_TITLE_BYTES = 5_000_000


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8')


def _hex(value):
    return isinstance(value, str) and _HEX.fullmatch(value) is not None


def _check_deadline(deadline):
    if deadline is not None and time.monotonic() >= deadline:
        raise IndexContractError('vector_deadline_exceeded')


def qmd_markdown_title(body: str, path: str) -> str:
    """The QMD Markdown title used in the document embedding input."""
    match = re.search(r'^##?\s+(.+)$', body, re.MULTILINE)
    if match:
        title = match.group(1).strip()
        if title in ('📝 Notes', 'Notes'):
            next_title = re.search(r'^##\s+(.+)$', body, re.MULTILINE)
            if next_title:
                title = next_title.group(1).strip()
    else:
        name = path.rsplit('/', 1)[-1]
        title = name.rsplit('.', 1)[0] or name
    try:
        title_bytes = title.encode('utf-8')
    except UnicodeEncodeError as exc:
        raise IndexContractError('invalid_vector_title') from exc
    if not title or len(title_bytes) > MAX_TITLE_BYTES:
        raise IndexContractError('invalid_vector_title')
    return title


def source_evidence(hash_: str, title: str) -> str:
    return hashlib.sha256(b'laya-live-source-title-v2\0' +
                          bytes.fromhex(hash_) + b'\0' +
                          title.encode('utf-8')).hexdigest()


def profile_identity(model_id, embedding_fingerprint, dimension):
    if (not isinstance(model_id, str) or not 1 <= len(model_id) <= 512
            or any(unicodedata.category(ch) in ('Cc', 'Cf', 'Cs', 'Zl', 'Zp')
                   for ch in model_id)
            or not _hex(embedding_fingerprint)
            or type(dimension) is not int or not 1 <= dimension <= MAX_DIMENSION):
        raise IndexContractError('invalid_vector_profile')
    return hashlib.sha256(_json([model_id, embedding_fingerprint,
                                 dimension, 'cosine'])).hexdigest()


def _blob(vector, dimension):
    if (not isinstance(vector, (list, tuple)) or len(vector) != dimension
            or any(type(v) not in (int, float) for v in vector)):
        raise IndexContractError('invalid_vector')
    try:
        if not all(math.isfinite(v) for v in vector):
            raise IndexContractError('invalid_vector')
        blob = struct.pack('<' + 'f' * dimension, *vector)
        values = struct.unpack('<' + 'f' * dimension, blob)
    except (OverflowError, struct.error) as exc:
        raise IndexContractError('invalid_vector') from exc
    if not all(math.isfinite(v) for v in values) or math.fsum(v * v for v in values) <= 0:
        raise IndexContractError('invalid_vector')
    return blob, values


def _unpack(blob, dimension):
    if not isinstance(blob, bytes) or len(blob) != dimension * 4:
        raise IndexContractError('vector_blob_invalid')
    values = struct.unpack('<' + 'f' * dimension, blob)
    if not all(math.isfinite(v) for v in values) or math.fsum(v * v for v in values) <= 0:
        raise IndexContractError('vector_blob_invalid')
    return values


def _provenance(hash_, title, profile_id, seq, pos, source_evidence_digest, blob):
    return hashlib.sha256(b'laya-vector-chunk-v2\0' +
        _json([hash_, title, profile_id, seq, pos, source_evidence_digest]) +
        b'\0' + blob).hexdigest()


def _rows_digest(gen, scope, profile_id, rows):
    return hashlib.sha256(b'laya-generation-vectors-v2\0' +
        _json([gen, scope, profile_id, sorted(rows)])).hexdigest()


class _OrderedRowsDigest:
    """The existing v2 JSON digest, fed in path/sequence order without a list."""
    def __init__(self, gen, scope, profile_id):
        self._hash = hashlib.sha256(b'laya-generation-vectors-v2\0')
        self._hash.update(b'[' + _json(gen) + b',' + _json(scope) + b',' +
                          _json(profile_id) + b',[')
        self._first = True

    def add(self, row):
        if not self._first:
            self._hash.update(b',')
        self._hash.update(_json(row))
        self._first = False

    def hexdigest(self):
        finish = self._hash.copy()
        finish.update(b']]')
        return finish.hexdigest()


def _chunk_set_digest(hash_, title, profile_id, rows):
    return hashlib.sha256(b'laya-vector-chunk-set-v1\0' +
        _json([hash_, title, profile_id,
               [(row['seq'], row['pos'], row['provenance']) for row in rows]])).hexdigest()


def _read_chunk_set(db, hash_, title, profile_id, dimension, *, allow_unsealed=False,
                    deadline=None):
    """Return only a fully sealed, contiguous, source/provenance-valid set."""
    seal = db.execute('''SELECT rows_count,rows_digest FROM vector_chunk_sets
        WHERE hash=? AND title=? AND profile_id=?''',
        (hash_, title, profile_id)).fetchone()
    raw = db.execute('''SELECT seq,pos,embedding,source_evidence_digest,provenance_digest
        FROM vector_chunks WHERE hash=? AND title=? AND profile_id=? ORDER BY seq''',
        (hash_, title, profile_id)).fetchall()
    if seal is None:
        if raw and not allow_unsealed:
            raise IndexContractError('vector_chunk_set_incomplete')
        if not raw:
            return None
    if (len(raw) > MAX_CANDIDATES or
            (seal is not None and
             (type(seal[0]) is not int or not 1 <= seal[0] <= MAX_CANDIDATES
              or len(raw) != seal[0] or not _hex(seal[1])))):
        raise IndexContractError('vector_chunk_set_invalid')
    evidence_expected = source_evidence(hash_, title)
    rows = []
    last_pos = -1
    for expected_seq, (seq, pos, blob, evidence, provenance) in enumerate(raw):
        _check_deadline(deadline)
        if (seq != expected_seq or type(pos) is not int or pos <= last_pos
                or evidence != evidence_expected or not _hex(provenance)
                or provenance != _provenance(hash_, title, profile_id, seq,
                                             pos, evidence, blob)):
            raise IndexContractError('vector_chunk_set_invalid')
        values = _unpack(blob, dimension)
        rows.append({'seq': seq, 'pos': pos, 'vector': list(values),
                     'provenance': provenance})
        last_pos = pos
    if seal is not None and _chunk_set_digest(hash_, title, profile_id, rows) != seal[1]:
        raise IndexContractError('vector_chunk_set_invalid')
    return rows


def _profile(db, profile_id):
    row = db.execute('''SELECT model_id,embedding_fingerprint,dimension,metric
                        FROM vector_profiles WHERE profile_id=?''', (profile_id,)).fetchone()
    if (row is None or row[3] != 'cosine'
            or profile_identity(row[0], row[1], row[2]) != profile_id):
        raise IndexContractError('vector_profile_mismatch')
    return {'profile_id': profile_id, 'model_id': row[0],
            'embedding_fingerprint': row[1], 'dimension': row[2], 'metric': row[3]}


def verify_generation_seal(index, gen, *, deadline=None):
    """Verify a seal in the caller's existing read/write transaction.

    This function neither begins nor commits; A-int publish calls it under its
    write transaction, and exact search calls it under its pinned snapshot.
    The full proof is O(generation rows). A query passes its monotonic
    deadline so verification fails closed with vector_deadline_exceeded
    instead of running unbounded; publish passes none and verifies fully.
    """
    db = index.db
    _check_deadline(deadline)
    seal = db.execute('''SELECT scope,profile_id,rows_digest,rows_count
                         FROM gen_vector_seals WHERE gen=?''', (gen,)).fetchone()
    state = db.execute('SELECT scope,state FROM generations WHERE gen=?', (gen,)).fetchone()
    if seal is None or state is None or state[0] != seal[0] or state[1] not in ('prepared','published'):
        raise IndexContractError('vector_generation_unsealed')
    scope, profile_id, saved_digest, saved_count = seal
    profile = _profile(db, profile_id)
    if type(saved_count) is not int or saved_count < 0 or not _hex(saved_digest):
        raise IndexContractError('vector_seal_invalid')
    missing = db.execute('''SELECT 1 FROM membership m WHERE m.gen=? AND m.eligible=1
        AND NOT EXISTS (SELECT 1 FROM gen_vector_rows r
                        WHERE r.gen=m.gen AND r.path=m.path AND r.hash=m.hash)
        LIMIT 1''', (gen,)).fetchone()
    if missing:
        raise IndexContractError('vector_coverage_incomplete')
    rows = db.execute('''SELECT r.scope,r.path,r.hash,r.title,r.profile_id,r.seq,
                              v.pos,v.embedding,v.source_evidence_digest,
                              v.provenance_digest,c.body
        FROM gen_vector_rows r JOIN vector_chunks v
          ON v.hash=r.hash AND v.title=r.title
         AND v.profile_id=r.profile_id AND v.seq=r.seq
        JOIN membership m ON m.gen=r.gen AND m.path=r.path AND m.hash=r.hash
        JOIN content c ON c.hash=m.hash
        WHERE r.gen=? ORDER BY r.path COLLATE BINARY,r.seq''', (gen,))
    digest_rows = _OrderedRowsDigest(gen, scope, profile_id)
    count = 0
    current_path = None
    current_key = None
    current_chunks = None
    current_title = None
    path_count = 0
    for row_scope, path, hash_, title, row_profile, seq, pos, blob, evidence, provenance, body in rows:
        _check_deadline(deadline)
        if (row_scope != scope or row_profile != profile_id or not _hex(evidence)
                or not _hex(provenance) or type(seq) is not int or seq < 0
                or type(pos) is not int or pos < 0):
            raise IndexContractError('vector_seal_invalid')
        # One path binds one (hash, title) key below, and content is keyed by
        # hash, so the body title is parsed once per path rather than per chunk.
        if path != current_path:
            current_title = qmd_markdown_title(body, path)
        if current_title != title:
            raise IndexContractError('vector_seal_invalid')
        _unpack(blob, profile['dimension'])
        if provenance != _provenance(hash_, title, profile_id, seq, pos, evidence, blob):
            raise IndexContractError('vector_provenance_mismatch')
        if not db.execute('''SELECT 1 FROM membership WHERE gen=? AND scope=?
                             AND path=? AND hash=? AND eligible=1 LIMIT 1''',
                          (gen, scope, path, hash_)).fetchone():
            raise IndexContractError('vector_seal_invalid')
        key = (hash_, title, profile_id)
        if path != current_path:
            if current_path is not None and path_count != current_chunks:
                raise IndexContractError('vector_coverage_incomplete')
            current_path, current_key, path_count = path, key, 0
            chunks = _read_chunk_set(db, hash_, title, profile_id,
                                     profile['dimension'], deadline=deadline)
            if chunks is None:
                raise IndexContractError('vector_chunk_set_incomplete')
            current_chunks = len(chunks)
        elif key != current_key:
            raise IndexContractError('vector_seal_invalid')
        if seq != path_count:
            raise IndexContractError('vector_coverage_incomplete')
        path_count += 1
        count += 1
        digest_rows.add((path, hash_, title, seq, provenance))
        if count > saved_count:
            raise IndexContractError('vector_seal_invalid')
    if current_path is not None and path_count != current_chunks:
        raise IndexContractError('vector_coverage_incomplete')
    if count != saved_count or db.execute(
            'SELECT COUNT(*) FROM gen_vector_rows WHERE gen=?', (gen,)).fetchone()[0] != saved_count:
        raise IndexContractError('vector_seal_invalid')
    if digest_rows.hexdigest() != saved_digest:
        raise IndexContractError('vector_seal_invalid')
    return {**profile, 'generation': gen, 'scope': scope,
            'rows_digest': saved_digest, 'rows_count': saved_count}


class LayaVectorIndex:
    def __init__(self, index):
        self.index = index

    def register_profile(self, model_id, embedding_fingerprint, dimension):
        profile_id = profile_identity(model_id, embedding_fingerprint, dimension)
        db = self.index.db
        self.index._begin()
        try:
            row = db.execute('SELECT 1 FROM vector_profiles WHERE profile_id=?',
                             (profile_id,)).fetchone()
            if row is None:
                db.execute('INSERT INTO vector_profiles VALUES(?,?,?,?,?)',
                           (profile_id, model_id, embedding_fingerprint, dimension, 'cosine'))
            _profile(db, profile_id)
            db.commit()
            return profile_id
        except BaseException:
            self.index._rollback()
            raise

    def insert_chunk(self, hash_, profile_id, seq, pos, vector, source_evidence_digest,
                     *, title):
        try:
            title_bytes = title.encode('utf-8')
        except (AttributeError, UnicodeEncodeError) as exc:
            raise IndexContractError('invalid_vector_chunk') from exc
        if (not _hex(hash_) or not _hex(profile_id) or not _hex(source_evidence_digest)
                or type(seq) is not int or seq < 0 or type(pos) is not int or pos < 0
                or not title or len(title_bytes) > MAX_TITLE_BYTES):
            raise IndexContractError('invalid_vector_chunk')
        db = self.index.db
        self.index._begin()
        try:
            profile = _profile(db, profile_id)
            blob, _ = _blob(vector, profile['dimension'])
            provenance = _provenance(hash_, title, profile_id, seq, pos,
                                     source_evidence_digest, blob)
            prior = db.execute('''SELECT pos,embedding,source_evidence_digest,provenance_digest
                FROM vector_chunks WHERE hash=? AND title=? AND profile_id=? AND seq=?''',
                (hash_, title, profile_id, seq)).fetchone()
            expected = (pos, blob, source_evidence_digest, provenance)
            if prior is not None and prior != expected:
                raise IndexContractError('vector_chunk_conflict')
            if prior is None:
                db.execute('INSERT INTO vector_chunks VALUES(?,?,?,?,?,?,?,?)',
                           (hash_, title, profile_id, seq, pos, blob,
                            source_evidence_digest, provenance))
            db.commit()
            return provenance
        except BaseException:
            self.index._rollback()
            raise

    def seal_chunk_set(self, hash_, title, profile_id, expected_count):
        """Seal an imported complete set; used before private candidate publish."""
        if (not _hex(hash_) or not _hex(profile_id)
                or type(expected_count) is not int
                or not 1 <= expected_count <= MAX_CANDIDATES):
            raise IndexContractError('vector_chunk_set_invalid')
        db = self.index.db
        self.index._begin()
        try:
            profile = _profile(db, profile_id)
            rows = _read_chunk_set(db, hash_, title, profile_id,
                                   profile['dimension'], allow_unsealed=True)
            if rows is None or len(rows) != expected_count:
                raise IndexContractError('vector_chunk_set_incomplete')
            digest = _chunk_set_digest(hash_, title, profile_id, rows)
            prior = db.execute('''SELECT rows_count,rows_digest FROM vector_chunk_sets
                WHERE hash=? AND title=? AND profile_id=?''',
                (hash_, title, profile_id)).fetchone()
            if prior is not None and prior != (expected_count, digest):
                raise IndexContractError('vector_chunk_set_invalid')
            if prior is None:
                db.execute('INSERT INTO vector_chunk_sets VALUES(?,?,?,?,?)',
                           (hash_, title, profile_id, expected_count, digest))
            _read_chunk_set(db, hash_, title, profile_id, profile['dimension'])
            db.commit()
            return expected_count
        except BaseException:
            self.index._rollback()
            raise

    def cached_chunk_set(self, hash_, title, profile_id):
        """Read a proven complete set; absent returns None, partial fails closed."""
        db = self.index.db
        db.execute('BEGIN')
        try:
            self.index._check_header(db)
            if db.execute('SELECT 1 FROM vector_profiles WHERE profile_id=?',
                          (profile_id,)).fetchone() is None:
                db.commit()
                return None
            profile = _profile(db, profile_id)
            rows = _read_chunk_set(db, hash_, title, profile_id,
                                   profile['dimension'])
            db.commit()
            return None if rows is None else [
                {key: row[key] for key in ('seq', 'pos', 'vector')} for row in rows]
        except BaseException:
            self.index._rollback()
            raise

    def insert_chunk_set(self, hash_, title, profile_id, rows):
        """Insert all immutable rows and their completeness seal in one commit."""
        if (not _hex(hash_) or not _hex(profile_id) or not isinstance(rows, list)
                or not 1 <= len(rows) <= MAX_CANDIDATES):
            raise IndexContractError('vector_chunk_set_invalid')
        db = self.index.db
        self.index._begin()
        try:
            profile = _profile(db, profile_id)
            cached = _read_chunk_set(db, hash_, title, profile_id,
                                     profile['dimension'])
            if cached is not None:
                if (len(cached) != len(rows) or any(
                        row['seq'] != current['seq'] or row['pos'] != current['pos']
                        or _blob(row['vector'], profile['dimension'])[0] !=
                           _blob(current['vector'], profile['dimension'])[0]
                        for row, current in zip(rows, cached))):
                    raise IndexContractError('vector_chunk_conflict')
                db.commit()
                return len(cached)
            evidence = source_evidence(hash_, title)
            last_pos = -1
            for expected_seq, row in enumerate(rows):
                if (not isinstance(row, dict) or set(row) != {'seq', 'pos', 'vector'}
                        or row['seq'] != expected_seq
                        or type(row['pos']) is not int or row['pos'] <= last_pos):
                    raise IndexContractError('vector_chunk_set_invalid')
                blob, _ = _blob(row['vector'], profile['dimension'])
                provenance = _provenance(hash_, title, profile_id,
                                         expected_seq, row['pos'], evidence, blob)
                db.execute('INSERT INTO vector_chunks VALUES(?,?,?,?,?,?,?,?)',
                           (hash_, title, profile_id, expected_seq, row['pos'],
                            blob, evidence, provenance))
                last_pos = row['pos']
            verified = _read_chunk_set(db, hash_, title, profile_id,
                                       profile['dimension'], allow_unsealed=True)
            db.execute('INSERT INTO vector_chunk_sets VALUES(?,?,?,?,?)',
                       (hash_, title, profile_id, len(rows),
                        _chunk_set_digest(hash_, title, profile_id, verified)))
            _read_chunk_set(db, hash_, title, profile_id, profile['dimension'])
            db.commit()
            return len(rows)
        except BaseException:
            self.index._rollback()
            raise

    def seal_generation(self, gen, profile_id):
        if not isinstance(gen, str) or not gen or not _hex(profile_id):
            raise IndexContractError('invalid_vector_generation')
        db = self.index.db
        self.index._begin()
        try:
            profile = _profile(db, profile_id)
            prior = db.execute('SELECT 1 FROM gen_vector_seals WHERE gen=?', (gen,)).fetchone()
            if prior is not None:
                result = verify_generation_seal(self.index, gen)
                if result['profile_id'] != profile_id:
                    raise IndexContractError('vector_profile_mismatch')
                db.commit()
                return result
            generation = db.execute('SELECT scope,state FROM generations WHERE gen=?',
                                    (gen,)).fetchone()
            if generation is None or generation[1] != 'prepared':
                raise IndexContractError('vector_generation_not_prepared')
            scope = generation[0]
            eligible = db.execute('''SELECT m.path,m.hash,c.body FROM membership m
                JOIN content c ON c.hash=m.hash
                WHERE m.gen=? AND m.scope=? AND m.eligible=1
                ORDER BY m.path COLLATE BINARY''', (gen, scope))
            # A process can die after retaining a subset of prepared rows in a
            # future per-batch caller. Replay accepts only exact rows and can
            # neither overwrite nor silently absorb an alien profile/scope.
            rows_digest = _OrderedRowsDigest(gen, scope, profile_id)
            rows_count = 0
            for path, hash_, body in eligible:
                title = qmd_markdown_title(body, path)
                complete = _read_chunk_set(db, hash_, title, profile_id,
                                           profile['dimension'])
                if complete is None:
                    raise IndexContractError('vector_coverage_incomplete')
                chunks = db.execute('''SELECT seq,pos,embedding,source_evidence_digest,
                                             provenance_digest FROM vector_chunks
                    WHERE hash=? AND title=? AND profile_id=? ORDER BY seq''',
                    (hash_, title, profile_id)).fetchall()
                if len(chunks) != len(complete):
                    raise IndexContractError('vector_chunk_set_invalid')
                for seq, pos, blob, evidence, provenance in chunks:
                    if (not _hex(evidence) or not _hex(provenance)
                            or provenance != _provenance(hash_, title, profile_id, seq,
                                                         pos, evidence, blob)):
                        raise IndexContractError('vector_provenance_mismatch')
                    _unpack(blob, profile['dimension'])
                    prior_row = db.execute('''SELECT scope,hash,title,profile_id
                        FROM gen_vector_rows WHERE gen=? AND path=? AND seq=?''',
                        (gen, path, seq)).fetchone()
                    if prior_row is not None:
                        if prior_row != (scope, hash_, title, profile_id):
                            raise IndexContractError('vector_row_conflict')
                    else:
                        db.execute('INSERT INTO gen_vector_rows VALUES(?,?,?,?,?,?,?)',
                                   (gen, scope, path, hash_, title, profile_id, seq))
                    rows_digest.add((path, hash_, title, seq, provenance))
                    rows_count += 1
            if db.execute('SELECT COUNT(*) FROM gen_vector_rows WHERE gen=?',
                          (gen,)).fetchone()[0] != rows_count:
                raise IndexContractError('vector_row_conflict')
            digest = rows_digest.hexdigest()
            db.execute('INSERT INTO gen_vector_seals VALUES(?,?,?,?,?)',
                       (gen, scope, profile_id, digest, rows_count))
            result = verify_generation_seal(self.index, gen)
            db.commit()
            return result
        except BaseException:
            self.index._rollback()
            raise

    def profile_for_pin(self, token):
        db = self.index.db
        db.execute('BEGIN')
        try:
            self.index._check_header(db)
            scope, gen, _ = self.index._pinned(token)
            profile = verify_generation_seal(self.index, gen)
            if profile['scope'] != scope:
                raise IndexContractError('vector_seal_invalid')
            db.commit()
            return profile
        except BaseException:
            self.index._rollback()
            raise

    def search_exact(self, token, query_vector, *, model_id, embedding_fingerprint,
                     dimension, limit, max_candidates=None,
                     max_float_work=None, deadline_seconds=None,
                     path_prefixes=None):
        if (type(limit) is not int or not 1 <= limit <= 100
                or (max_candidates is not None and
                    (type(max_candidates) is not int or max_candidates < 1))
                or (max_float_work is not None and
                    (type(max_float_work) is not int or max_float_work < 1))
                or (deadline_seconds is not None and
                    (type(deadline_seconds) not in (int, float)
                     or not math.isfinite(deadline_seconds) or deadline_seconds <= 0))):
            raise IndexContractError('invalid_vector_query')
        profile_id = profile_identity(model_id, embedding_fingerprint, dimension)
        prefix_sql, prefix_params = _path_predicate(path_prefixes)
        query_blob, query = _blob(query_vector, dimension)
        del query_blob
        qnorm = math.sqrt(math.fsum(v * v for v in query))
        deadline = None if deadline_seconds is None else time.monotonic() + deadline_seconds
        db = self.index.db
        # The seal proof is never skipped; it shares the query deadline. The
        # handler also interrupts single long statements inside the proof.
        if deadline is not None:
            db.set_progress_handler(
                lambda: int(time.monotonic() >= deadline), 1000)
        try:
            return self._search_exact_pinned(
                token, query, qnorm, profile_id, dimension, limit,
                max_candidates, max_float_work, deadline,
                prefix_sql, prefix_params)
        except sqlite3.OperationalError as exc:
            if deadline is not None and time.monotonic() >= deadline:
                raise IndexContractError('vector_deadline_exceeded') from exc
            raise
        finally:
            if deadline is not None:
                db.set_progress_handler(None, 0)

    def _search_exact_pinned(self, token, query, qnorm, profile_id, dimension,
                             limit, max_candidates, max_float_work, deadline,
                             prefix_sql, prefix_params):
        db = self.index.db
        db.execute('BEGIN')
        try:
            self.index._check_header(db)
            scope, gen, _ = self.index._pinned(token)
            _check_deadline(deadline)
            profile = verify_generation_seal(self.index, gen, deadline=deadline)
            if profile['scope'] != scope or profile['profile_id'] != profile_id:
                raise IndexContractError('vector_profile_mismatch')
            _check_deadline(deadline)
            # The same predicate is used in the count and scan. No global ANN
            # candidate can consume a scoped slot before scoring and LIMIT.
            predicate = '''FROM gen_vector_rows r
                JOIN membership m ON m.gen=r.gen AND m.scope=r.scope
                                 AND m.path=r.path AND m.hash=r.hash
                JOIN vector_chunks v ON v.hash=r.hash AND v.title=r.title
                                    AND v.profile_id=r.profile_id AND v.seq=r.seq
                JOIN content c ON c.hash=m.hash
                WHERE r.gen=? AND r.scope=? AND r.profile_id=? AND m.eligible=1
                  AND ''' + _NOT_EXCLUDED + prefix_sql
            params = (gen, scope, profile_id, *prefix_params)
            # A corpus-wide row count is not an index admission limit. Explicit
            # caller budgets still fail closed; the product hook supplies a
            # deadline. Keep only the best row for one path and the top K paths.
            top = []
            current_path = None
            current = None
            count = 0
            def finish_path():
                if current is not None:
                    top.append(current)
                    top.sort(key=lambda row: (row['score'], row['path'].encode('utf-8')))
                    del top[limit:]
            cursor = db.execute('''SELECT m.path,m.hash,c.body,v.seq,v.pos,v.embedding ''' +
                predicate + ' ORDER BY m.path COLLATE BINARY,v.seq', params)
            for path, hash_, body, seq, pos, blob in cursor:
                _check_deadline(deadline)
                count += 1
                if ((max_candidates is not None and count > max_candidates)
                        or (max_float_work is not None and
                            count * dimension > max_float_work)):
                    raise IndexContractError('vector_budget_exceeded')
                if path != current_path:
                    finish_path()
                    current_path, current = path, None
                values = _unpack(blob, dimension)
                norm = math.sqrt(math.fsum(v * v for v in values))
                similarity = math.fsum(a * b for a, b in zip(query, values)) / (qnorm * norm)
                distance = 1.0 - min(1.0, max(-1.0, similarity))
                if current is None or (distance, seq) < (current['score'], current['seq']):
                    current = {'generation': gen, 'scope': scope, 'path': path,
                               'hash': hash_, 'body': body, 'score': distance,
                               'seq': seq, 'position': pos}
            finish_path()
            db.commit()
            return top
        except BaseException:
            self.index._rollback()
            raise


def search_vector_verified(index, root, token, query_vector, *, model_id,
                           embedding_fingerprint, dimension, limit,
                           max_files=200, max_bytes=5_000_000,
                           path_prefixes=None, **budget):
    """Apply the source port's live manifest and row checks to vector hits."""
    from collections.abc import Mapping
    from laya_index_source_port import SourceStale, scan, scan_collections
    before = index.view_seal(token)
    scope, gen, manifest = before['scope'], before['gen'], before['source_manifest']
    source = (scan_collections(root, scope, index, max_files, max_bytes)
              if isinstance(root, Mapping) else
              scan(root, scope, index, max_files, max_bytes))
    if source.manifest != manifest:
        raise SourceStale('source manifest differs from view seal')
    rows = LayaVectorIndex(index).search_exact(
        token, query_vector, model_id=model_id,
        embedding_fingerprint=embedding_fingerprint, dimension=dimension,
        limit=limit, path_prefixes=path_prefixes, **budget)
    verified = []
    for row in rows:
        f = source.by_path.get(row['path'])
        if (row['scope'] != scope or row['generation'] != gen or f is None
                or not f.eligible or f.sha256 != row['hash']):
            raise SourceStale('vector row does not match source')
        verified.append({'scope': scope, 'generation': gen, 'path': f.path,
                         'hash': f.sha256, 'body': f.text, 'score': row['score']})
    if index.view_seal(token) != before:
        raise SourceStale('view seal changed during vector search')
    return verified

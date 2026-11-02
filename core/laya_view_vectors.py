"""Sealed vector coverage and pinned exact/hybrid lanes for candidate generations.

A generation is either lexical-only or holds one immutable reference row per
eligible member to the shared complete chunk/embedding sets of one sealed
recipe/profile. References commit set identities and digests; vector bytes are
never copied per generation. Exact search scores only the pin's filtered
eligible members after explicit float-work and deadline checks. Nothing here
imports a provider, embeds a query or grants operational authority; results
stay commitment-only. Elapsed checks never preempt arbitrary Python.
"""
from __future__ import annotations

import bisect
import math
import operator
import struct
import time

import laya_blob_values as v
from laya_blob_values import BlobStoreError

LEXICAL = 'lexical-only'
EXACT = 'exact-complete'
DOMAIN = 'memvane-generation-vectors-v1'
SPEC_FIELDS = ('recipe_id', 'profile_id')
REF_FIELDS = ('path', 'blob_sha', 'recipe_id', 'profile_id', 'chunk_count', 'chunk_digest', 'embedding_digest')
DEFAULT_WORK = 4000000
MAX_DEADLINE = 30
MAX_LIMIT = 100
MAX_DIMENSION = 2048
WORK_EXCEEDED = 'vector_work_exceeded'
DEADLINE_EXCEEDED = 'vector_deadline_exceeded'
# Only explicit budget/deadline reasons may defer the semantic lane of hybrid.
DEFERRED = {WORK_EXCEEDED: 'deferred_work_budget', DEADLINE_EXCEEDED: 'deferred_deadline',
            'fts_deadline_exceeded': 'deferred_deadline'}


def spec(value):
    """Exact ``{recipe_id, profile_id}`` or None (lexical default)."""
    if value is None:
        return None
    v.exact(value, SPEC_FIELDS)
    return {k: v.identifier(value[k]) for k in SPEC_FIELDS}


def mode(spec_value):
    return LEXICAL if spec_value is None else EXACT


def check_stage(mode_value, spec_value):
    if spec(spec_value) != spec_value or mode_value != mode(spec_value):
        v.fail('generation_binding_mismatch')


def coverage_digest(spec_value, rows):
    """Ordered actual references incl. path/blob/spec; lexical keeps the v1 constant."""
    if spec_value is None:
        if rows:
            v.fail('vector_rows_mismatch')
        return v.digest(DOMAIN, {'mode': LEXICAL, 'members': []})
    return v.digest(DOMAIN, {'mode': EXACT, 'spec': spec_value,
                             'members': [[row[k] for k in REF_FIELDS] for row in rows]})


def options(limit, work, seconds, prefixes, exclusions):
    """Public exact/hybrid bounds, validated before any pin or SQL use."""
    if type(limit) is not int or not 1 <= limit <= MAX_LIMIT:
        v.fail('vector_limit_invalid')
    if type(work) is not int or work < 1:
        v.fail('vector_work_invalid')
    if type(seconds) not in (int, float) or not 0 < seconds <= MAX_DEADLINE or not math.isfinite(seconds):
        v.fail('vector_deadline_invalid')
    for items, maximum in ((prefixes, 32), (exclusions, 256)):
        if items is not None:
            if type(items) is not list or len(items) > maximum:
                v.fail('vector_filter_invalid')
            for path in items:
                v.relative(path)
    return seconds


def query_values(value):
    """Finite non-bool numeric query -> unit tuple; dimension is checked by the caller."""
    if type(value) not in (list, tuple) or not 1 <= len(value) <= MAX_DIMENSION:
        v.fail('vector_query_invalid')
    items = []
    for x in value:
        if type(x) not in (int, float):
            v.fail('vector_query_invalid')
        try:
            number = float(x)
        except OverflowError:
            v.fail('vector_query_invalid')
        if not math.isfinite(number):
            v.fail('vector_query_invalid')
        items.append(number)
    # Scale first so squaring finite large components cannot overflow.
    scale = max(abs(x) for x in items)
    if scale == 0:
        v.fail('vector_query_invalid')
    scaled = [x / scale for x in items]
    norm = math.sqrt(math.fsum(x * x for x in scaled))
    return tuple(x / norm for x in scaled)


def _left(deadline):
    if deadline is not None and time.monotonic() >= deadline:
        v.fail(DEADLINE_EXCEEDED)


def _filters(prefixes, exclusions):
    conditions, args = [], []
    if prefixes is not None:
        if not prefixes:
            conditions.append('0')
        else:
            conditions.append('(' + ' OR '.join('(m.path=? OR substr(m.path,1,?)=?)' for _ in prefixes) + ')')
            for path in prefixes:
                args.extend((path, len(path) + 1, path + '/'))
    if exclusions:
        conditions.append('m.path NOT IN (' + ','.join('?' for _ in exclusions) + ')')
        args.extend(exclusions)
    return ''.join(' AND ' + clause for clause in conditions), args


def _set_score(query, rows, dimension, deadline):
    """Max cosine over every chunk of one validated little-endian float32 set."""
    unpack, best = struct.Struct('<' + 'f' * dimension).unpack, None
    for row in rows:
        _left(deadline)
        values = unpack(row['embedding'])
        norm = math.sqrt(math.fsum(x * x for x in values))
        if not norm or not math.isfinite(norm):
            v.fail('embedding_values_invalid')
        score = math.fsum(map(operator.mul, query, values)) / norm
        if best is None or score > best:
            best = score
    return max(-1.0, min(1.0, best))


def _keep(top, score, ref, limit):
    """Bounded top-limit: descending score, binary UTF-8 path ascending tie."""
    bisect.insort(top, (-score, ref['path'].encode('utf8'), ref['path'], ref))
    if len(top) > limit:
        top.pop()


class VectorMixin:
    def _vector_heads(self, db, blob, recipe_id, profile_id):
        chunk = db.execute('SELECT chunk_count,set_digest FROM chunk_sets WHERE blob_sha=? AND recipe_id=?',
                           (blob, recipe_id)).fetchone()
        embedded = db.execute('SELECT chunk_count,set_digest FROM embedding_sets '
                              'WHERE blob_sha=? AND recipe_id=? AND profile_id=?',
                              (blob, recipe_id, profile_id)).fetchone()
        if chunk is None or embedded is None:
            v.fail('vector_coverage_incomplete')
        if chunk['chunk_count'] != embedded['chunk_count']:
            v.fail('embedding_set_invalid')
        return dict(chunk_count=chunk['chunk_count'], chunk_digest=chunk['set_digest'],
                    embedding_digest=embedded['set_digest'])

    def _vector_coverage(self, db, plan, spec_value, *, full, deadline=None):
        """Expected references for EVERY eligible member; any hole refuses.

        ``full`` re-validates each distinct referenced set (digests, inputs and
        per-row provenance) one complete set at a time.
        """
        if spec_value is None:
            return []
        recipe_id, profile_id = spec_value['recipe_id'], spec_value['profile_id']
        self._recipe(recipe_id); self._profile(profile_id)
        rows, heads = [], {}
        for member in plan['members']:
            if not member['eligible']:
                continue
            _left(deadline)
            blob = member['blob_sha']
            if blob not in heads:
                head = self._vector_heads(db, blob, recipe_id, profile_id)
                if full and len(self._vectors(blob, recipe_id, profile_id)) != head['chunk_count']:
                    v.fail('embedding_set_invalid')
                heads[blob] = head
            rows.append(dict(path=member['path'], blob_sha=blob, recipe_id=recipe_id,
                             profile_id=profile_id, **heads[blob]))
        return rows

    @staticmethod
    def _stored_vectors(db, gen_id):
        return [dict(r) for r in db.execute(
            'SELECT ' + ','.join(REF_FIELDS) + ' FROM member_vectors WHERE gen_id=? ORDER BY path', (gen_id,))]

    def _verified_vectors(self, db, stage, plan, gen_id, *, full=False, deadline=None):
        """Actual stored references must equal complete coverage of the sealed spec."""
        stored = self._stored_vectors(db, gen_id)
        if stored != self._vector_coverage(db, plan, stage['vector_spec'], full=full, deadline=deadline):
            v.fail('vector_rows_mismatch')
        return stored

    def search_exact(self, pin, query_vector, limit, *, profile_id, max_float_work=DEFAULT_WORK,
                     deadline_seconds=2, path_prefixes=None, excluded_paths=None):
        deadline = time.monotonic() + options(limit, max_float_work, deadline_seconds,
                                              path_prefixes, excluded_paths)
        v.identifier(profile_id)
        query = query_values(query_vector)
        record = self._pinned(pin)
        entry = record['entry']
        _left(deadline)
        self._observe(entry)
        _left(deadline)
        with self._reading(deadline) as db:
            generation, seal, original, stage = self._read_generation(db, pin.gen_id, entry['handle'], deadline)
            if generation['state'] != 'published' or v.digest('memvane-read-pin-seal-v1', seal) != record['seal']:
                v.fail('view_pin_changed')
            sealed = stage['vector_spec']
            if sealed is None:
                v.fail('vector_coverage_absent')
            if profile_id != sealed['profile_id']:
                v.fail('vector_profile_mismatch')
            dimension = self._profile(profile_id)['dimension']
            if len(query) != dimension:
                v.fail('vector_query_invalid')
            # View, eligibility and path filters precede work counting and scoring.
            extra, args = _filters(path_prefixes, excluded_paths)
            refs = [dict(r) for r in db.execute(
                'SELECT ' + ','.join('x.' + k for k in REF_FIELDS) + ''',m.display_title
                FROM member_vectors x JOIN wiki_members m
                  ON m.gen_id=x.gen_id AND m.path=x.path AND m.blob_sha=x.blob_sha
                JOIN generations g ON g.gen_id=x.gen_id
                WHERE x.gen_id=? AND g.view_id=? AND m.eligible=1''' + extra +
                ' ORDER BY x.path COLLATE BINARY', [pin.gen_id, pin.view_id, *args])]
            # Conservative: every filtered path counts, even when content repeats.
            if sum(ref['chunk_count'] for ref in refs) * dimension > max_float_work:
                v.fail(WORK_EXCEEDED)
            groups = {}
            for ref in refs:
                groups.setdefault(ref['blob_sha'], []).append(ref)
            top = []
            for blob, members in groups.items():
                _left(deadline)
                rows = self._vectors(blob, sealed['recipe_id'], profile_id)
                if any(ref['recipe_id'] != sealed['recipe_id'] or ref['profile_id'] != profile_id
                       or ref['chunk_count'] != len(rows) for ref in members):
                    v.fail('vector_rows_mismatch')
                score = _set_score(query, rows, dimension, deadline)
                rows = None  # one complete set in memory at a time
                for ref in members:
                    _keep(top, score, ref, limit)
            state = self._snapshot_state(entry['capture'], original, deadline)
            answer = [dict(path=ref['path'], blob_sha=ref['blob_sha'], body=self._blob(ref['blob_sha']),
                           display_title=ref['display_title'], score=-negative, view_id=pin.view_id,
                           gen_id=pin.gen_id, evidence='commitment-only', owner_verified=False,
                           snapshot_state=state) for negative, _, _, ref in top]
            _left(deadline)
        self._observe(entry)
        _left(deadline)
        return answer

    def search_hybrid(self, pin, fts_query, query_vector, limit, *, profile_id=None,
                      max_float_work=DEFAULT_WORK, deadline_seconds=2, path_prefixes=None,
                      excluded_paths=None):
        """Two ordered lanes under one elapsed deadline; no fusion or ANN."""
        start = time.monotonic()
        options(limit, max_float_work, deadline_seconds, path_prefixes, excluded_paths)
        if profile_id is not None:
            v.identifier(profile_id)
        query = None if query_vector is None else query_values(query_vector)
        # The pinned record keeps the spec of the verified sealed stage; search
        # below re-verifies that seal, and exact re-verifies everything again.
        sealed = self._pinned(pin)['vector']
        if sealed is not None:
            if profile_id is not None and profile_id != sealed['profile_id']:
                v.fail('vector_profile_mismatch')
            if query is not None and len(query) != sealed['dimension']:
                v.fail('vector_query_invalid')
        deadline = start + deadline_seconds
        lexical_left = deadline - time.monotonic()
        if lexical_left <= 0:
            v.fail(DEADLINE_EXCEEDED)
        lexical = self.search(pin, fts_query, limit, path_prefixes=path_prefixes,
                              excluded_paths=excluded_paths,
                              deadline_seconds=lexical_left)
        if query is None:
            return dict(lexical=lexical, semantic=[], semantic_status='provider_absent')
        if sealed is None:
            return dict(lexical=lexical, semantic=[], semantic_status='lexical_only')
        left = (start + deadline_seconds) - time.monotonic()
        if left <= 0:
            return dict(lexical=lexical, semantic=[], semantic_status=DEFERRED[DEADLINE_EXCEEDED])
        try:
            semantic = self.search_exact(pin, query_vector, limit, profile_id=sealed['profile_id'],
                                         max_float_work=max_float_work,
                                         deadline_seconds=min(left, deadline_seconds),
                                         path_prefixes=path_prefixes, excluded_paths=excluded_paths)
        except BlobStoreError as exc:
            if exc.reason not in DEFERRED:
                raise
            return dict(lexical=lexical, semantic=[], semantic_status=DEFERRED[exc.reason])
        return dict(lexical=lexical, semantic=semantic, semantic_status='complete')

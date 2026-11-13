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
import hashlib
import math
import operator
import struct
import time
from pathlib import Path

from memvane.store import blob_values as v
from memvane.store.blob_values import BlobStoreError

SPEC_FIELDS = ('recipe_id', 'profile_id')
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








# The installed provider's view vector identity (decision K5). The recipe and
# profile are derived from the install's reviewed provider manifest and route
# vector profile only, so sync fill and the reader name the same
# content-addressed IDs. The recipe uses regex/token chunking
# (900/135/200 tokens), and the tokenizer is
# the model's own. A document the provider keeps on its first chunking pass
# (``qmd_first_pass``) has those chunks; a document the provider re-splits
# over its token window (no reproducible text here) has the fallback windows
# ``qmd_first_pass(body, *FALLBACK_CHARS)``, each embedded alone. Whether the
# provider re-splits is decided by the token count of the recipe's tokenizer,
# so the chunk set is a function of (body, tokenizer). The chunker identity
# commits the embedding fingerprint (provider bytes and chunk recipe text)
# and that fallback rule.
PROVIDER_CHUNK_RECIPE = 'qmd-regex-token-chunks-candidate-v1'
QMD_CHUNK_TOKENS = (900, 135, 200)
FALLBACK_CHARS = (600, 90, 135)  # max/overlap/window UTF-16 code units
# Increment when _QMD_BREAKS or qmd_first_pass changes.
CHUNK_RULES = 1


def chunker_sha(embedding_fingerprint):
    max_chars, overlap, window = FALLBACK_CHARS
    return v.digest('memvane-provider-chunker-v1', {
        'embedding_fingerprint': v.identifier(embedding_fingerprint),
        'chunker_js_sha256': hashlib.sha256((Path(__file__).resolve().parent.parent / 'memvane/provider/adapters/laya_qmd_chunking.mjs').read_bytes()).hexdigest(),
        'chunk_rules': CHUNK_RULES,
        'resplit_fallback': {'rule': 'qmd-first-pass', 'max_chars': max_chars, 'overlap_chars': overlap,
                             'window_chars': window, 'unit': 'utf16-code-unit'}})


def provider_specs(manifest, vector_profile):
    """``(recipe spec, profile spec)`` of the install provider, or None.

    None when the manifest is not the QMD chunk recipe or a field is not the
    shape the candidate store accepts; the view then stays lexical.
    """
    try:
        if (manifest.get('recipeId') != PROVIDER_CHUNK_RECIPE
                or vector_profile.get('metric') != 'cosine'):
            return None
        window, overlap, search = QMD_CHUNK_TOKENS
        recipe = v.recipe({
            'input_recipe': 'blob-input-v1', 'tokenizer_sha': manifest['modelSha256'],
            'chunker_sha': chunker_sha(vector_profile['embeddingFingerprint']), 'window_tokens': window,
            'overlap_tokens': overlap, 'window_search_tokens': search,
            'offset_unit': 'utf16-code-unit', 'heading_rule': 'qmd-markdown-v1',
            'document_template': 'title: {title} | text: {text}', 'synthetic_path': 'blobs/{blob_sha}.md'})
        profile = v.profile({
            'model_id': vector_profile['modelId'], 'model_sha': manifest['modelSha256'],
            'embedding_fingerprint': vector_profile['embeddingFingerprint'],
            'dimension': vector_profile['dimension'], 'metric': 'cosine',
            'document_input_recipe': 'blob-input-v1', 'query_input_recipe': 'qmd-query-v1',
            'query_template': 'task: search result | query: {text}'})
    except (AttributeError, KeyError, OSError, TypeError, BlobStoreError):
        return None
    if profile['dimension'] > MAX_DIMENSION:
        return None
    return recipe, profile


def provider_spec(manifest, vector_profile):
    """The ``{recipe_id, profile_id}`` vector spec of the install provider, or None (no write)."""
    specs = provider_specs(manifest, vector_profile)
    if specs is None:
        return None
    return {'recipe_id': v.digest('memvane-chunk-recipe-v1', specs[0]),
            'profile_id': v.digest('memvane-vector-profile-v1', specs[1])}


def register_provider_spec(store, manifest, vector_profile):
    """Register the install provider's recipe/profile in a writable store; returns the spec or None."""
    specs = provider_specs(manifest, vector_profile)
    if specs is None:
        return None
    value = {'recipe_id': store.register_recipe(specs[0]), 'profile_id': store.register_profile(specs[1])}
    if value != provider_spec(manifest, vector_profile):
        v.fail('vector_spec_mismatch')
    return value


# The provider's first chunking pass (laya_qmd_chunking.mjs ``chunkDocument``
# with the token chunker's 3-chars-per-token sizes), in UTF-16 code units. It
# needs no tokenizer; the provider then re-splits only a part over 900 model
# tokens, which this side cannot reproduce, so the caller compares the
# provider's chunk positions with these and gives a re-split document the
# recipe's fallback windows (``FALLBACK_CHARS``, same break rules).
_QMD_WS = '\t\n\v\f\r    -     　﻿'
_QMD_BREAKS = ((r'\n#(?!#)', 100), (r'\n##(?!#)', 90), (r'\n###(?!#)', 80), (r'\n####(?!#)', 70),
               (r'\n#####(?!#)', 60), (r'\n######(?!#)', 50), (r'\n```', 80),
               (r'\n(?:---|\*\*\*|___)[' + _QMD_WS + r']*\n', 60), (r'\n\n+', 20),
               (r'\n[-*][' + _QMD_WS + r']', 5), (r'\n[0-9]+\.[' + _QMD_WS + r']', 5), (r'\n', 1))


def _utf16_offsets(text):
    """Code point index -> UTF-16 offset (length len(text)+1)."""
    out, unit = [0], 0
    for char in text:
        unit += 2 if ord(char) > 0xFFFF else 1
        out.append(unit)
    return out


def qmd_first_pass(body, max_chars=2700, overlap_chars=405, window_chars=600):
    """``[(utf16 position, text)]`` of the provider's first chunking pass."""
    import re
    offsets = _utf16_offsets(body)
    length = offsets[-1]
    if length <= max_chars:
        return [(0, body)]
    index = {unit: i for i, unit in enumerate(offsets)}
    seen = {}
    for pattern, score in _QMD_BREAKS:
        for match in re.finditer(pattern, body):
            pos = offsets[match.start()]
            if pos not in seen or score > seen[pos]:
                seen[pos] = score
    points = sorted(seen.items())
    fences, start = [], None
    for match in re.finditer(r'\n```', body):
        if start is None:
            start = offsets[match.start()]
        else:
            fences.append((start, offsets[match.start()] + 4))
            start = None
    if start is not None:
        fences.append((start, length))

    def cutoff(target):
        best_score, best = -1, target
        for pos, score in points:
            if pos < target - window_chars:
                continue
            if pos > target:
                break
            if any(lo < pos < hi for lo, hi in fences):
                continue
            distance = (target - pos) / window_chars
            value = score * (1 - distance * distance * 0.7)
            if value > best_score:
                best_score, best = value, pos
        return best

    def boundary(pos, floor):
        # A position inside a surrogate pair is not a code point boundary.
        if pos in index:
            return pos
        return pos - 1 if pos - 1 > floor else pos + 1
    chunks, pos = [], 0
    while pos < length:
        target = min(pos + max_chars, length)
        end = target
        if end < length:
            cut = cutoff(target)
            if pos < cut <= target:
                end = cut
        if end <= pos:
            end = min(pos + max_chars, length)
        end = boundary(end, pos)
        chunks.append((pos, body[index[pos]:index[end]]))
        if end >= length:
            break
        following = end - overlap_chars
        if following <= pos:
            following = end
        pos = boundary(following, pos)
    return chunks


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




    def search_exact(self, pin, query_vector, limit, *, profile_id, max_float_work=DEFAULT_WORK,
                     deadline_seconds=2, path_prefixes=None, excluded_paths=None):
        return self._rows_search_exact(pin, query_vector, limit, profile_id=profile_id,
                                       max_float_work=max_float_work, deadline_seconds=deadline_seconds,
                                       path_prefixes=path_prefixes, excluded_paths=excluded_paths)

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
        # below checks that seal, and both reads re-verify the generation after
        # any commit to the store (_read_pinned).
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

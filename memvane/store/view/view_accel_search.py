"""Bounded store reads and candidate validation for the optional native lane.

Generation membership, coverage and seal checks belong to the caller. All
SQL here runs inside its pinned read transaction. Publication seals all sets;
recall validates shape/values everywhere and full digests on the top cards.
"""
from __future__ import annotations

import time

from memvane.store.blob import blob_values as v
from memvane.store.view import view_vectors as vectors

MAX_ROWS = 40000
MAX_BYTES = 128 * 1024 * 1024
VERIFY_CARDS = 60
_BLOB_BATCH = 256  # Below the system Python 3.9 SQLite variable limit.


def _load(db, groups, heads, recipe_id, profile_id, dimension, deadline, stats):
    count = sum(heads[blob]['chunk_count'] for blob in groups)
    if count > MAX_ROWS or count * dimension * 4 > MAX_BYTES:
        v.fail(vectors.WORK_EXCEEDED)
    vectors._left(deadline)
    packed = bytearray(count * dimension * 4)
    blobs = sorted(groups)
    ranges, offset = {}, 0
    started = time.perf_counter()
    try:
        for start in range(0, len(blobs), _BLOB_BATCH):
            vectors._left(deadline)
            batch = blobs[start:start + _BLOB_BATCH]
            marks = ','.join('?' for _ in batch)
            expected = sum(heads[b]['chunk_count'] for b in batch)
            seen = {blob: 0 for blob in batch}
            for blob in batch:
                ranges[blob] = (offset, heads[blob]['chunk_count'])
                offset += heads[blob]['chunk_count']
            cursor = db.execute('SELECT blob_sha,seq,embedding FROM vectors WHERE recipe_id=? '
                                'AND profile_id=? AND blob_sha IN (' + marks + ') '
                                'ORDER BY blob_sha,seq LIMIT ?',
                                (recipe_id, profile_id, *batch, expected + 1))
            for row in cursor:
                vectors._left(deadline)
                blob, seq, raw = row
                if (blob not in seen or seq != seen[blob] or seq >= heads[blob]['chunk_count']
                        or type(raw) is not bytes or len(raw) != dimension * 4):
                    v.fail('embedding_set_invalid')
                begin = (ranges[blob][0] + seq) * dimension * 4
                packed[begin:begin + dimension * 4] = raw
                seen[blob] += 1
                stats['vectorRows'] += 1
            if any(seen[blob] != heads[blob]['chunk_count'] for blob in batch):
                v.fail('embedding_set_invalid')
    finally:
        elapsed = (time.perf_counter() - started) * 1000
        stats['vectorLoadMs'] += elapsed
        stats['vectorReadMs'] += elapsed
    return packed, ranges


def search(store, db, groups, heads, recipe_id, profile_id, dimension, query,
           limit, deadline, stats, native):
    """Top cards from a bulk scan, then the store's original digest/score path."""
    packed, ranges = _load(db, groups, heads, recipe_id, profile_id, dimension, deadline, stats)
    started = time.perf_counter()
    try:
        scores = native.scores(packed, dimension, query, deadline, stats)
        candidates = []
        for blob, rows in groups.items():
            vectors._left(deadline)
            begin, count = ranges[blob]
            score = max(scores[begin:begin + count])
            for row in rows:
                vectors._keep(candidates, score,
                              dict(path=row['path'], blob_sha=blob, display_title=row['title']),
                              max(VERIFY_CARDS, limit))
    finally:
        elapsed = (time.perf_counter() - started) * 1000
        stats['vectorScanMs'] += elapsed
        stats['vectorDotMs'] += elapsed
    # Release the full corpus buffers before the candidate validation reads.
    del packed, scores
    chosen = list(dict.fromkeys(ref['blob_sha'] for _, _, _, ref in candidates))
    exact, started = {}, time.perf_counter()
    try:
        for batch in vectors._vector_batches(chosen, heads):
            vectors._left(deadline)
            saved = vectors._read_batch(db, batch, heads, recipe_id, profile_id, stats)
            for blob in batch:
                vectors._left(deadline)
                rows = store._vectors(blob, recipe_id, profile_id, saved=saved[blob])
                if len(rows) != heads[blob]['chunk_count']:
                    v.fail('embedding_set_invalid')
                stats['vectorVerified'] += 1
                exact[blob] = vectors._set_score(query, rows, dimension, deadline, timings=stats)
    finally:
        stats['vectorVerifyMs'] += (time.perf_counter() - started) * 1000
    top = []
    for _, _, _, ref in candidates:
        vectors._keep(top, exact[ref['blob_sha']], ref, limit)
    vectors._left(deadline)
    return top

"""Bounded store reads and candidate validation for the optional native lane.

Generation membership, coverage and seal checks belong to the caller. All
SQL here runs inside its pinned read transaction. Publication seals all sets;
recall validates shape/values everywhere and full digests on the top cards.
"""
from __future__ import annotations

import os
from pathlib import Path
import time

from memvane.store.blob import blob_values as v
from memvane.store.types import CACHE_BUILDING
from memvane.store.view import view_vectors as vectors

MAX_ROWS = 40000
MAX_BYTES = 128 * 1024 * 1024
VERIFY_CARDS = 60
# An arena score and the store's score of the same set may differ by float32 rounding only.
ARENA_TOLERANCE = 1e-4
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


def heads(store, db, cards, spec, deadline):
    """``{blob: set head}`` of the eligible cards' blobs, read in batches (``_rows_coverage`` without ``full``)."""
    recipe_id, profile_id = spec['recipe_id'], spec['profile_id']
    store._recipe(recipe_id); store._profile(profile_id)
    blobs = list(dict.fromkeys(row['blob_sha'] for row in cards if row['eligible']))
    answer = {}
    for start in range(0, len(blobs), _BLOB_BATCH):
        vectors._left(deadline)
        batch = blobs[start:start + _BLOB_BATCH]
        marks = ','.join('?' for _ in batch)
        chunks = {row[0]: row for row in db.execute(
            'SELECT blob_sha,chunk_count,set_digest FROM chunk_sets WHERE recipe_id=? AND blob_sha IN (' + marks + ')',
            (recipe_id, *batch))}
        embedded = {row[0]: row for row in db.execute(
            'SELECT blob_sha,chunk_count,set_digest FROM embedding_sets WHERE recipe_id=? AND profile_id=? '
            'AND blob_sha IN (' + marks + ')', (recipe_id, profile_id, *batch))}
        for blob in batch:
            if blob not in chunks or blob not in embedded:
                v.fail('vector_coverage_incomplete')
            if chunks[blob][1] != embedded[blob][1]:
                v.fail('embedding_set_invalid')
            answer[blob] = dict(chunk_count=chunks[blob][1], chunk_digest=chunks[blob][2],
                                embedding_digest=embedded[blob][2])
    return answer


def _arena_scores(store, groups, heads, recipe_id, profile_id, dimension, query, deadline, stats, native):
    """``(scores, covered, missing, state)`` from the vector arena beside this store.

    Sets the arena covers are scored from its mapped rows; ``missing`` lists
    the blobs the store read has to supply. Anything that goes wrong inside the
    arena reader answers as "no arena": only a deadline leaves this function.
    """
    from memvane.vectorcache import api as vectorcache
    if os.environ.get('MEMVANE_VECTOR_ACCEL', '').lower() == 'store':
        return {}, {}, list(groups), 'disabled'
    started = time.perf_counter()
    try:
        directory = Path(store._path).parent / vectorcache.DIRECTORY
        with vectorcache.opened(directory, profile_id=profile_id, recipe_id=recipe_id, dimension=dimension,
                                store_incarnation=store._incarnation) as found:
            if found is None:
                return {}, {}, list(groups), 'absent'
            covered, missing, wrong = found.cover(
                {blob: (heads[blob]['chunk_count'], heads[blob]['embedding_digest']) for blob in groups})
            scores, unreadable = vectorcache.scan(found, covered, dimension=dimension, queries=query, native=native,
                                                  deadline=deadline, stats=stats)
        for blob in unreadable:
            del covered[blob]
        missing = missing + unreadable
        stats['vectorRows'] += sum(rows for _first, rows in covered.values())
        state = 'inconsistent' if wrong or unreadable else 'building' if missing else 'ok'
        return scores, covered, missing, state
    except v.BlobStoreError as exc:
        if exc.reason == vectors.DEADLINE_EXCEEDED:
            raise
        return {}, {}, list(groups), 'absent'
    except Exception:  # noqa: BLE001 - a derived file never fails the search; the store answers
        return {}, {}, list(groups), 'absent'
    finally:
        elapsed = (time.perf_counter() - started) * 1000
        stats['vectorLoadMs'] += elapsed


def search(store, db, groups, heads, recipe_id, profile_id, dimension, query,
           limit, deadline, stats, native):
    """Top cards from the arena and a bulk store read, then the store's original digest/score path.

    The arena scores the sets it covers; the store read supplies the rest, and
    only that read is bounded (``MAX_ROWS``, ``MAX_BYTES``). Past the bound
    the search waits for the arena to catch up (``CACHE_BUILDING``), unless
    the arena is switched off, which keeps the work-budget refusal.
    """
    scores, covered, missing, state = _arena_scores(store, groups, heads, recipe_id, profile_id, dimension,
                                                    query, deadline, stats, native)
    stats.update(vectorSource='arena' if covered else 'store', vectorCache=state)
    if covered:
        stats['vectorMissing'] = sum(heads[blob]['chunk_count'] for blob in missing)
    packed = None
    if missing:
        try:
            packed, ranges = _load(db, {blob: groups[blob] for blob in missing}, heads, recipe_id, profile_id,
                                   dimension, deadline, stats)
        except v.BlobStoreError as exc:
            if exc.reason == vectors.WORK_EXCEEDED and state != 'disabled':
                v.fail(CACHE_BUILDING)
            raise
    started = time.perf_counter()
    try:
        if packed is not None:
            read = native.scores(packed, dimension, query[0], deadline, stats)
            for other in query[1:]:
                # One more scan of the same buffer per extra query vector; a card keeps its best.
                read = [max(pair) for pair in zip(read, native.scores(packed, dimension, other, deadline, stats))]
            for blob in missing:
                begin, count = ranges[blob]
                scores[blob] = max(read[begin:begin + count])
        candidates = []
        for blob, rows in groups.items():
            vectors._left(deadline)
            for row in rows:
                vectors._keep(candidates, scores[blob],
                              dict(path=row['path'], blob_sha=blob, display_title=row['title']),
                              max(VERIFY_CARDS, limit))
    finally:
        elapsed = (time.perf_counter() - started) * 1000
        stats['vectorScanMs'] += elapsed
        stats['vectorDotMs'] += elapsed
    # Release the full corpus buffers before the candidate validation reads.
    del packed
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
                if blob in covered and abs(exact[blob] - scores[blob]) > ARENA_TOLERANCE:
                    # The arena row does not score like the store's: the store's score is the one used.
                    stats['vectorCache'] = 'inconsistent'
    finally:
        stats['vectorVerifyMs'] += (time.perf_counter() - started) * 1000
    top = []
    for _, _, _, ref in candidates:
        vectors._keep(top, exact[ref['blob_sha']], ref, limit)
    vectors._left(deadline)
    return top

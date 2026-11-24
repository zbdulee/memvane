"""Scoring covered sets straight from the mapped arena."""
from __future__ import annotations

import os

from memvane.vectorcache.types import GAP_ROWS, VALUE_REASONS


def spans(covered, gap=GAP_ROWS):
    """``[(first row, rows, [blob, ...])]``: the covered sets grouped into spans to scan at once.

    Sets whose rows lie ``gap`` rows apart or closer share a span; the rows
    between them (dead sets, other views' sets) are scanned and ignored.
    """
    answer = []
    for blob, (first, rows) in sorted(covered.items(), key=lambda item: item[1]):
        if answer and first - (answer[-1][0] + answer[-1][1]) <= gap:
            answer[-1][1] = max(answer[-1][1], first + rows - answer[-1][0])
            answer[-1][2].append(blob)
        else:
            answer.append([first, rows, [blob]])
    return [(first, rows, blobs) for first, rows, blobs in answer]


def _best(native, buffer, dimension, queries, deadline, stats):
    best = None
    for query in queries:
        scores = native.scores(buffer, dimension, query, deadline, stats)
        best = scores if best is None else [max(pair) for pair in zip(best, scores)]
    return best


def scan(arena, covered, *, dimension, queries, native, deadline, stats):
    """``(scores, unreadable)`` of the covered sets: ``{blob: best cosine}`` and the blobs to read elsewhere.

    ``native`` is the scan kernel (``scores(buffer, dimension, query, deadline,
    stats)``). A span whose bytes the kernel refuses (a row of norm zero or a
    non-finite value, in a member set or in the rows between) is scanned again
    set by set; a set that is refused on its own is unreadable here and
    belongs to the caller's store read. A deadline or any other refusal is the
    caller's to see.
    """
    scores, unreadable = {}, []

    def one(first, rows, blobs):
        best = _best(native, arena.span(first, rows), dimension, queries, deadline, stats)
        for blob in blobs:
            start, count = covered[blob]
            scores[blob] = max(best[start - first:start - first + count])
    for first, rows, blobs in spans(covered):
        try:
            one(first, rows, blobs)
            continue
        except ValueError as exc:
            if getattr(exc, 'reason', None) not in VALUE_REASONS:
                raise
        for blob in blobs:
            try:
                one(*covered[blob], [blob])
            except ValueError as exc:
                if getattr(exc, 'reason', None) not in VALUE_REASONS:
                    raise
                unreadable.append(blob)
    return scores, unreadable


def score_sets(directory, sets, *, profile_id, recipe_id, dimension, store_incarnation,
               queries, native, deadline, stats, clock, open_arena):
    """Score the pinned sets covered by this arena and return the sets that need a store read."""
    if os.environ.get('MEMVANE_VECTOR_ACCEL', '').lower() == 'store':
        return {}, {}, list(sets), 'disabled'
    started, scanned, missing, wrong = clock(), 0.0, (), 0
    try:
        with open_arena(directory, profile_id=profile_id, recipe_id=recipe_id, dimension=dimension,
                        store_incarnation=store_incarnation) as found:
            if found is None:
                return {}, {}, list(sets), 'absent'
            covered, missing, wrong = found.cover(sets)
            mark = clock()
            scores, unreadable = scan(found, covered, dimension=dimension, queries=queries, native=native,
                                      deadline=deadline, stats=stats)
            scanned = (clock() - mark) * 1000
        for blob in unreadable:
            del covered[blob]
        missing = missing + unreadable
        stats['vectorRows'] += sum(rows for _first, rows in covered.values())
        state = 'inconsistent' if wrong or unreadable else 'building' if missing else 'ok'
        return scores, covered, missing, state
    except Exception as exc:  # A derived file can fail independently of the store.
        if getattr(exc, 'reason', None) == 'vector_deadline_exceeded':
            # The scan ran out of time on the arena's rows: that is where this search read.
            stats.update(vectorSource='arena', vectorCache='inconsistent' if wrong else 'building' if missing else 'ok')
            raise
        return {}, {}, list(sets), 'absent'
    finally:
        # Opening, mapping and the crc of the member sets are the load; the kernel scan is the scan.
        stats['vectorLoadMs'] += (clock() - started) * 1000 - scanned
        stats['vectorScanMs'] += scanned
        stats['vectorDotMs'] += scanned

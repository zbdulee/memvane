"""Scoring covered sets straight from the mapped arena."""
from __future__ import annotations

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

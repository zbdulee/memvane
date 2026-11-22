"""Read-only timing of the semantic read path on one view store (design 2-4: measure first).

    python3 -B memvane/store/tests/measure/vector_read_measure.py <store.sqlite> [--scratch <dir>]

Opens the store ``query_only`` and writes nothing beside a temporary arena
copy in ``--scratch`` (default: a new temporary directory, removed at exit;
never the install state root). The view with the most eligible head cards is
measured. Query vectors are synthetic; no card body, title, path or prompt is
read or printed. The output is one JSON object of counts and milliseconds:

    coverage      one ``_vector_heads`` pair of lookups per blob, and the same
                  facts in batches of 256 blobs
    storeRead     the released bulk read (256 blob ``IN`` batches) into one
                  buffer: first and repeated read
    mmapRead      one rowid-ordered SELECT with ``PRAGMA mmap_size``
    arena         a contiguous float32 copy mapped ``ACCESS_COPY``: the map, a
                  per-set crc32, and the kernel scan over the whole file, over
                  one call per member set and over merged neighbouring ranges
                  (with every second set dead)
    scan          the kernel scan with 1 and 4 query vectors
    verify        the top 60 sets read back from the store and scored again in
                  Python, with 1 and 4 query vectors

Run it alone under the development guard (``--kind test --exclusive``, through a node test that
spawns it). It is not part of any test tier.
"""
from __future__ import annotations

import argparse
import json
import math
import mmap
import os
from pathlib import Path
import random
import sqlite3
import sys
import tempfile
import time
import zlib

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

BATCH = 256
VERIFY_SETS = 60


def ms(started):
    return round((time.perf_counter() - started) * 1000, 2)


def connect(path, pragma=None):
    from memvane.kernel.errors import _query_only_uri
    db = sqlite3.connect(_query_only_uri(path), uri=True, timeout=5)
    db.execute('PRAGMA query_only=1')
    if pragma:
        db.execute(pragma)
    return db


def view_spec(db):
    """``(view id, recipe id, profile id)`` of the complete view with the most eligible head cards."""
    for view_id, _count in db.execute(
            'SELECT view_id,COUNT(*) FROM s_cards WHERE gen_to IS NULL AND eligible=1 '
            'GROUP BY view_id ORDER BY 2 DESC'):
        row = db.execute("SELECT recipe_id,profile_id FROM s_generations WHERE view_id=? AND coverage='complete' "
                         'ORDER BY ordinal DESC LIMIT 1', (view_id,)).fetchone()
        if row is not None:
            return view_id, row[0], row[1]
    raise SystemExit('no view with a complete vector generation')


def coverage(db, blobs, recipe, profile):
    started = time.perf_counter()
    single = {}
    for blob in blobs:
        chunk = db.execute('SELECT chunk_count,set_digest FROM chunk_sets WHERE blob_sha=? AND recipe_id=?',
                           (blob, recipe)).fetchone()
        embedded = db.execute('SELECT chunk_count,set_digest FROM embedding_sets '
                              'WHERE blob_sha=? AND recipe_id=? AND profile_id=?', (blob, recipe, profile)).fetchone()
        if chunk is not None and embedded is not None:
            single[blob] = embedded[0]
    one_by_one = ms(started)
    started = time.perf_counter()
    batched = {}
    for start in range(0, len(blobs), BATCH):
        part = blobs[start:start + BATCH]
        marks = ','.join('?' for _ in part)
        chunks = dict(db.execute('SELECT blob_sha,chunk_count FROM chunk_sets WHERE recipe_id=? AND blob_sha IN ('
                                 + marks + ')', (recipe, *part)))
        for blob, count in db.execute('SELECT blob_sha,chunk_count FROM embedding_sets WHERE recipe_id=? '
                                      'AND profile_id=? AND blob_sha IN (' + marks + ')', (recipe, profile, *part)):
            if blob in chunks:
                batched[blob] = count
    return single, {'blobs': len(blobs), 'perBlobMs': one_by_one, 'batchedMs': ms(started),
                    'same': single == batched}


def store_read(db, blobs, counts, recipe, profile, width):
    """The released bulk read: ``(buffer, {blob: (first row, rows)})``."""
    total = sum(counts[blob] for blob in blobs)
    packed, ranges, offset = bytearray(total * width), {}, 0
    for start in range(0, len(blobs), BATCH):
        part = blobs[start:start + BATCH]
        for blob in part:
            ranges[blob] = (offset, counts[blob])
            offset += counts[blob]
        marks = ','.join('?' for _ in part)
        for blob, seq, raw in db.execute(
                'SELECT blob_sha,seq,embedding FROM vectors WHERE recipe_id=? AND profile_id=? AND blob_sha IN ('
                + marks + ') ORDER BY blob_sha,seq', (recipe, profile, *part)):
            if seq < counts[blob] and len(raw) == width:
                begin = (ranges[blob][0] + seq) * width
                packed[begin:begin + width] = raw
    return packed, ranges


def mmap_read(path, members, recipe, profile):
    db = connect(path, 'PRAGMA mmap_size=1073741824')
    try:
        started = time.perf_counter()
        rows = kept = 0
        for blob, _seq, raw in db.execute('SELECT blob_sha,seq,embedding FROM vectors WHERE recipe_id=? '
                                          'AND profile_id=? ORDER BY rowid', (recipe, profile)):
            rows += 1
            kept += blob in members and bool(raw)
        return {'rowsWalked': rows, 'memberRows': kept, 'ms': ms(started)}
    finally:
        db.close()


def unit(dimension, seed):
    rng = random.Random(seed)
    values = [rng.gauss(0, 1) for _ in range(dimension)]
    norm = math.sqrt(sum(x * x for x in values))
    return tuple(x / norm for x in values)


def scan(native, buffer, dimension, queries):
    started = time.perf_counter()
    best = None
    for query in queries:
        scores = native.scores(buffer, dimension, query, time.monotonic() + 60, {})
        best = scores if best is None else [max(pair) for pair in zip(best, scores)]
    return best, ms(started)


def merged(ranges, width):
    """Byte ranges of the given ``(first row, rows)`` spans with neighbours joined."""
    spans = []
    for first, rows in sorted(ranges):
        if spans and spans[-1][1] == first * width:
            spans[-1][1] = (first + rows) * width
        else:
            spans.append([first * width, (first + rows) * width])
    return spans


def arena(native, packed, ranges, dimension, width, scratch, queries):
    path = Path(scratch) / 'measure-arena.vec'
    started = time.perf_counter()
    path.write_bytes(packed)
    wrote = ms(started)
    answer = {'bytes': len(packed), 'writeMs': wrote}
    with open(path, 'rb') as handle:
        started = time.perf_counter()
        mapped = mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_COPY)
        answer['mapMs'] = ms(started)
        try:
            view = memoryview(mapped)
            started = time.perf_counter()
            for first, rows in ranges.values():
                zlib.crc32(view[first * width:(first + rows) * width])
            answer['crcPerSetMs'] = ms(started)
            _scores, answer['scanWholeMs'] = scan(native, mapped, dimension, queries[:1])
            sets = sorted(ranges.values())
            live = sets[::2]  # every second set is dead: member ranges no longer touch
            for label, spans in (('perSet', [[first * width, (first + rows) * width] for first, rows in live]),
                                 ('mergedLive', merged(live, width)), ('mergedAll', merged(sets, width))):
                started = time.perf_counter()
                for begin, end in spans:
                    native.scores(view[begin:end], dimension, queries[0], time.monotonic() + 60, {})
                answer[label] = {'calls': len(spans), 'ms': ms(started)}
            view.release()
        finally:
            mapped.close()
    path.unlink()
    return answer


def verify(db, packed_scores, ranges, recipe, profile, dimension, queries):
    from memvane.store.view import view_vectors as vectors
    best = sorted(((max(packed_scores[first:first + rows]), blob) for blob, (first, rows) in ranges.items()),
                  reverse=True)[:VERIFY_SETS]
    blobs = [blob for _score, blob in best]
    started = time.perf_counter()
    marks = ','.join('?' for _ in blobs)
    saved = {}
    for blob, seq, raw in db.execute('SELECT blob_sha,seq,embedding FROM vectors WHERE recipe_id=? AND profile_id=? '
                                     'AND blob_sha IN (' + marks + ') ORDER BY blob_sha,seq',
                                     (recipe, profile, *blobs)):
        saved.setdefault(blob, []).append({'embedding': raw})
    read = ms(started)
    answer = {'sets': len(blobs), 'chunks': sum(len(rows) for rows in saved.values()), 'readMs': read}
    for label, used in (('score1Ms', queries[:1]), ('score4Ms', queries)):
        started = time.perf_counter()
        for blob in blobs:
            vectors._set_score(tuple(used), saved[blob], dimension, time.monotonic() + 60)
        answer[label] = ms(started)
    return answer


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('store')
    parser.add_argument('--scratch')
    args = parser.parse_args()
    from memvane.store.view import vector_accel
    native = vector_accel.kernel()
    report = {'kernel': 'accelerate' if native is not None else 'python'}
    db = connect(args.store)
    try:
        started = time.perf_counter()
        view_id, recipe, profile = view_spec(db)
        blobs = sorted({row[0] for row in db.execute(
            'SELECT blob_sha FROM s_cards WHERE view_id=? AND gen_to IS NULL AND eligible=1', (view_id,))})
        report['cardsMs'] = ms(started)
        counts, report['coverage'] = coverage(db, blobs, recipe, profile)
        blobs = [blob for blob in blobs if blob in counts]
        sample = db.execute('SELECT embedding FROM vectors WHERE recipe_id=? AND profile_id=? LIMIT 1',
                            (recipe, profile)).fetchone()
        width = len(sample[0])
        dimension = width // 4
        report.update(sets=len(blobs), rows=sum(counts.values()), dimension=dimension)
        reads = []
        for _ in range(3):
            started = time.perf_counter()
            packed, ranges = store_read(db, blobs, counts, recipe, profile, width)
            reads.append(ms(started))
        report['storeRead'] = {'firstMs': reads[0], 'repeatMs': reads[1:]}
        report['mmapRead'] = mmap_read(args.store, set(blobs), recipe, profile)
        queries = [unit(dimension, seed) for seed in range(4)]
        if native is not None:
            scores, one = scan(native, packed, dimension, queries[:1])
            _scores, four = scan(native, packed, dimension, queries)
            report['scan'] = {'vectors1Ms': one, 'vectors4Ms': four}
            report['verify'] = verify(db, scores, ranges, recipe, profile, dimension, queries)
            with tempfile.TemporaryDirectory(dir=args.scratch) as scratch:
                report['arena'] = arena(native, packed, ranges, dimension, width, scratch, queries)
    finally:
        db.close()
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == '__main__':
    main()

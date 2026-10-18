#!/usr/bin/env python3
"""Repeatable, model-free FTS5 microbenchmark; never opens a QMD store."""
import hashlib
import json
from pathlib import Path
import resource
import sqlite3
import statistics
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'core'))
import qmd_fork_scope_prototype as fts
import worktree_query_slice as candidate


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def percentile(samples, p):
    ordered = sorted(samples)
    return ordered[min(len(ordered) - 1, int((len(ordered) - 1) * p))]


def stage(db, worktree, pages):
    candidate.stage(db, worktree, 'g1',
                    {path: digest(body) for path, body in pages.items()},
                    {digest(body): (body, 1.0) for body in pages.values()})
    candidate.publish(db, worktree, None, 'g1')


def measure(call, rounds=30):
    values = []
    for _ in range(rounds):
        start = time.perf_counter_ns()
        result = call()
        values.append((time.perf_counter_ns() - start) / 1e6)
    return result, {'p50_ms': round(statistics.median(values), 4),
                    'p95_ms': round(percentile(values, 0.95), 4)}


def main():
    with tempfile.TemporaryDirectory(prefix='qmd-fork-fts-synthetic-') as temp:
        path = Path(temp) / 'index.sqlite'
        db = sqlite3.connect(path)
        try:
            db.execute('PRAGMA journal_mode=WAL')
            candidate.create_schema(db)
            fts.create_fts(db)
            wall = time.perf_counter()
            cpu = time.process_time()
            stage(db, 'target', {f'chapter-{i}.md': f'target chapter {i}'
                                 for i in range(5)})
            stage(db, 'other', {f'decoy-{i}.md': f'target target target decoy {i}'
                                for i in range(220)})
            fts.index_staged_content(db)
            build_ms = round((time.perf_counter() - wall) * 1000, 3)
            build_cpu_ms = round((time.process_time() - cpu) * 1000, 3)
            token = candidate.pin_request(db, 'target')
            exact, scoped = measure(lambda: fts.scoped_lex(db, token, 'target', 5))
            legacy, global_first = measure(
                lambda: fts.legacy_global_lex(db, token, 'target', 5))
            exact_paths = {row[0] for row in exact}
            recall = len(exact_paths & {row[0] for row in legacy}) / len(exact_paths)
            db.execute('PRAGMA wal_checkpoint(PASSIVE)')
            sizes = {name: (path.parent / (path.name + suffix)).stat().st_size
                     if (path.parent / (path.name + suffix)).exists() else 0
                     for name, suffix in (('db', ''), ('wal', '-wal'))}
            print(json.dumps({
                'synthetic': True, 'engine': 'stdlib SQLite FTS5',
                'not_measured': ['QMD latency', 'vec ANN', 'embedding',
                                 'native dependency', 'production DB migration'],
                'fixture': {'target_pages': 5, 'other_pages': 220,
                            'shared_content_hashes': 0, 'rounds': 30,
                            'query': 'target', 'limit': 5},
                'build_ms': build_ms, 'build_cpu_ms': build_cpu_ms,
                'legacy_global': {**global_first, 'recall_at_5': recall},
                'scoped_full_match': {**scoped, 'recall_at_5': 1.0},
                'files_bytes_after_passive_checkpoint': sizes,
                # macOS reports bytes; Linux reports KiB. Keep the raw value
                # so this fixture cannot imply a comparable cross-OS unit.
                'process_maxrss_raw_platform_units':
                    resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            }, sort_keys=True))
        finally:
            db.close()


if __name__ == '__main__':
    main()

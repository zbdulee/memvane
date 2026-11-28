"""Bounded retention of ``wiki-pipeline.sqlite3`` rows.

The compile worker runs :func:`maintain` once per pass under ``.compile-run.lock``;
hooks never open the ledger. Every step deletes at most ``RETENTION_BATCH`` rows in
its own transaction, so an interrupted pass leaves only rows the next pass removes.

- ``audit_events``: per kind, rows older than the age limit or beyond the newest
  row count, whichever comes first (``AUDIT_RETENTION``).
- ``suppressions``: never by age (a missing row means another paid call). A row goes
  only when its source is proven gone; a newer body for the same source already
  replaces the row because ``(kind, key)`` is the primary key.
- ``card_generations``: the newest ``GENERATIONS_PER_CARD`` rows per card.
- ``cooldowns``: one row per key; rows that are no longer active go.
- Terminal queue rows, settled publishes, done dedup pairs and stale backfill
  checkpoints go after ``TERMINAL_RETENTION_NS``; a verify row that still
  supersedes an older queued row of its target stays.
"""
from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
import sqlite3
import time
from typing import Callable, Iterable

from memvane.kernel import cooldown as kernel_cooldown
from memvane.wiki.pipeline.types import (AUDIT_KINDS, AUDIT_RETENTION, GENERATIONS_PER_CARD, RETENTION_BATCH,
                                         TERMINAL_RETENTION_NS, VACUUM_PAGES)

SUPPRESSION_CURSOR = 'retention.suppressionCursor'
TERMINAL_JOB_STATES = ('done', 'dropped')


def _delete_ids(cur, table: str, column: str, select_sql: str, values: tuple) -> int:
    ids = [row[0] for row in cur.execute(select_sql, values).fetchall()]
    if not ids:
        return 0
    cur.execute(f'DELETE FROM {table} WHERE {column} IN ({",".join("?" for _ in ids)})', ids)
    return cur.rowcount


class RetentionMixin:
    def prune_audit(self, now_ns: int, budget: int = RETENTION_BATCH) -> int:
        removed = 0
        with self._write_tx() as cur:
            for kind in AUDIT_KINDS:
                age_ns, keep_rows = AUDIT_RETENTION.get(kind, AUDIT_RETENTION['default'])
                left = budget - removed
                if left <= 0:
                    break
                removed += _delete_ids(
                    cur, 'audit_events', 'id',
                    'SELECT id FROM audit_events WHERE kind = ? AND created_ns < ? ORDER BY id LIMIT ?',
                    (kind, now_ns - age_ns, left))
                cutoff = cur.execute('SELECT id FROM audit_events WHERE kind = ? ORDER BY id DESC LIMIT 1 OFFSET ?',
                                     (kind, keep_rows)).fetchone()
                left = budget - removed
                if cutoff is not None and left > 0:
                    removed += _delete_ids(
                        cur, 'audit_events', 'id',
                        'SELECT id FROM audit_events WHERE kind = ? AND id <= ? ORDER BY id LIMIT ?',
                        (kind, cutoff[0], left))
        return removed

    def prune_generations(self, budget: int = RETENTION_BATCH) -> int:
        """Keep the newest rows of each card; ``id`` orders rows written in the same second."""
        with self._write_tx() as cur:
            return _delete_ids(
                cur, 'card_generations', 'id',
                'SELECT id FROM (SELECT id, ROW_NUMBER() OVER (PARTITION BY '
                "COALESCE(target_path, 'k:' || canonical_key, 'h:' || source_hash, 'id:' || id) "
                'ORDER BY id DESC) AS rank FROM card_generations) WHERE rank > ? ORDER BY id LIMIT ?',
                (GENERATIONS_PER_CARD, budget))

    def prune_inactive_cooldowns(self, now: float) -> int:
        """One row per key; an expired row (or one beyond the expiry ceiling) is never active."""
        with self._write_tx() as cur:
            rows = cur.execute('SELECT scope, key, until_ts FROM cooldowns').fetchall()
            gone = [(r['scope'], r['key']) for r in rows if not kernel_cooldown.expiry_active(r['until_ts'], now)]
            for scope, key in gone:
                cur.execute('DELETE FROM cooldowns WHERE scope = ? AND key = ?', (scope, key))
            return len(gone)

    def prune_terminal(self, now_ns: int, budget: int = RETENTION_BATCH) -> dict:
        cutoff = now_ns - TERMINAL_RETENTION_NS
        marks = ','.join('?' for _ in TERMINAL_JOB_STATES)
        counts = {}
        with self._write_tx() as cur:
            counts['compileJobs'] = _delete_ids(
                cur, 'compile_jobs', 'job_id',
                f'SELECT job_id FROM compile_jobs WHERE state IN ({marks}) AND updated_ns < ? '
                'ORDER BY updated_ns, job_id LIMIT ?', (*TERMINAL_JOB_STATES, cutoff, budget))
            # A newer row of any state supersedes an older queued row (verify_queue._NEWER).
            counts['verifyJobs'] = _delete_ids(
                cur, 'verify_jobs', 'job_id',
                f'SELECT job_id FROM verify_jobs AS v WHERE state IN ({marks}) AND updated_ns < ? '
                "AND NOT EXISTS (SELECT 1 FROM verify_jobs AS q WHERE q.state = 'queued' "
                'AND q.target_path = v.target_path AND (q.created_ns < v.created_ns OR '
                '(q.created_ns = v.created_ns AND q.job_id < v.job_id))) '
                'ORDER BY updated_ns, job_id LIMIT ?', (*TERMINAL_JOB_STATES, cutoff, budget))
            counts['publishTxns'] = _delete_ids(
                cur, 'publish_txns', 'tx_id',
                "SELECT tx_id FROM publish_txns WHERE state != 'publishing' AND updated_ns < ? "
                'ORDER BY updated_ns, tx_id LIMIT ?', (cutoff, budget))
            counts['dedupPairs'] = _delete_ids(
                cur, 'dedup_pairs', 'pair_key',
                "SELECT pair_key FROM dedup_pairs WHERE state = 'done' AND updated_ns < ? "
                'ORDER BY updated_ns, pair_key LIMIT ?', (cutoff, budget))
            counts['backfillCheckpoints'] = _delete_ids(
                cur, 'backfill_checkpoints', 'binding_digest',
                'SELECT binding_digest FROM backfill_checkpoints WHERE updated_ns < ? '
                'ORDER BY updated_ns, binding_digest LIMIT ?', (cutoff, budget))
        return counts

    def suppression_batch(self, budget: int = RETENTION_BATCH) -> list[dict]:
        """The next rows after the stored cursor; an empty or short page wraps the cursor."""
        raw = self.meta_get(SUPPRESSION_CURSOR)
        try:
            kind, key = json.loads(raw) if raw else ('', '')
        except (TypeError, ValueError):
            kind, key = '', ''
        rows = self._conn.execute(
            'SELECT kind, key, updated_ns FROM suppressions WHERE kind > ? OR (kind = ? AND key > ?) '
            'ORDER BY kind, key LIMIT ?', (kind, kind, key, budget)).fetchall()
        return [{'kind': r['kind'], 'key': r['key'], 'updatedNs': r['updated_ns']} for r in rows]

    def prune_suppressions(self, batch: list[dict], gone: list[dict], budget: int = RETENTION_BATCH) -> int:
        """Delete ``gone`` rows unless rewritten since the batch was read; advance the cursor."""
        removed = 0
        with self._write_tx() as cur:
            for item in gone:
                cur.execute('DELETE FROM suppressions WHERE kind = ? AND key = ? AND updated_ns = ?',
                            (item['kind'], item['key'], item['updatedNs']))
                removed += cur.rowcount
            last = json.dumps([batch[-1]['kind'], batch[-1]['key']]) if len(batch) >= budget else ''
            cur.execute('INSERT INTO pipeline_meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET '
                        'value = excluded.value', (SUPPRESSION_CURSOR, last))
        return removed

    def reclaim_space(self) -> str:
        """Convert a database created without incremental auto-vacuum once, then free pages."""
        if self._conn.execute('PRAGMA auto_vacuum').fetchone()[0] != 2:
            self._conn.execute('PRAGMA auto_vacuum=INCREMENTAL')
            self._conn.execute('VACUUM')
            return 'converted'
        self._conn.execute(f'PRAGMA incremental_vacuum({int(VACUUM_PAGES)})').fetchall()
        return 'incremental'


def _relative(value) -> PurePosixPath | None:
    if not isinstance(value, str) or not value or '\x00' in value:
        return None
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in ('', '.', '..') for part in path.parts):
        return None
    return path


def _absent(base: Path, rel: PurePosixPath) -> bool | None:
    """True only when the path is proven missing; any other failure is unknown."""
    try:
        (base / rel).lstat()
        return False
    except FileNotFoundError:
        return True
    except (OSError, ValueError):
        return None


def source_gone(kind: str, key: str, *, wiki_root: Path | None,
                source_roots: tuple[Path, ...]) -> bool:
    """A suppression's source is gone only on proof; unknown keeps the row."""
    if kind in ('verify', 'supersession'):
        rel = _relative(key)
        if rel is None or not source_roots:
            return False
        return all(_absent(base, rel) is True for base in source_roots)
    if kind == 'dedup':
        pages = key.split('\n')
        if wiki_root is None or len(pages) != 2:
            return False
        rels = [_relative(page) for page in pages]
        if any(rel is None for rel in rels):
            return False
        return any(_absent(wiki_root, rel) is True for rel in rels)
    return False


def maintain(store, *, wiki_root: Path | None, source_roots: Iterable[Path],
             now: Callable[[], float] = time.time) -> dict:
    """Run every retention step once; a failing step stops the pass (the next pass retries)."""
    roots = tuple(Path(item) for item in source_roots)
    report: dict = {}
    try:
        current = now()
        now_ns = int(current * 10**9)
        report['audit'] = store.prune_audit(now_ns)
        report['generations'] = store.prune_generations()
        report['cooldowns'] = store.prune_inactive_cooldowns(current)
        report['terminal'] = store.prune_terminal(now_ns)
        report['refreshes'] = store.prune_refreshes()
        batch = store.suppression_batch()
        gone = [item for item in batch
                if source_gone(item['kind'], item['key'], wiki_root=wiki_root, source_roots=roots)]
        report['suppressions'] = store.prune_suppressions(batch, gone)
        report['vacuum'] = store.reclaim_space()
    except (OSError, ValueError, sqlite3.Error) as exc:
        report['stopped'] = type(exc).__name__
    return report

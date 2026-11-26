"""Decision ledgers of ``wiki-pipeline.sqlite3``: generated-card history, paid-call
suppressions and the dedup pair queue, plus the one-time 1.x import.

Mixed into :class:`memvane.wiki.pipeline.store.PipelineStore`, which supplies
``_conn`` and ``_write_tx``.
"""
from __future__ import annotations

import json
import time

from memvane.wiki.pipeline.types import DEDUP_STATES, SUPPRESSION_KINDS, PipelineError


def _dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _loads(raw: str | None, default: object = None) -> object:
    if not raw:
        return default
    return json.loads(raw)


def insert_generation(cur, row: dict, origin_key: str | None, now: int) -> int:
    """Append one generated-card history row inside the caller's transaction.

    Returns 1 when inserted, 0 when ``origin_key`` was already present.
    """
    def text(name):
        value = row.get(name)
        return value if isinstance(value, str) and value else None
    cur.execute(
        'INSERT OR IGNORE INTO card_generations(target_path, source_hash, canonical_key, explicit, '
        'action, row_json, origin_key, created_ns) VALUES(?, ?, ?, ?, ?, ?, ?, ?)',
        (text('targetPath'), text('sourceHash'), text('canonicalKey'),
         1 if row.get('targetResolution') == 'explicit' else 0, text('action'),
         _dumps(row), origin_key, now),
    )
    return cur.rowcount


class DecisionLedgerMixin:
    def import_v1_ledgers(self, manifest: list[tuple[str, dict]], verify_skips: list[dict],
                          dedup_skips: list[dict], marker: str) -> dict:
        """Import the 1.x decision ledgers and set ``marker`` in one transaction.

        ``manifest`` items are ``(origin_key, row)``; a repeated origin key is ignored, so a
        rerun after a crash before the commit imports the same rows exactly once.
        """
        counts = {'generations': 0, 'verifySuppressions': 0, 'dedupSuppressions': 0}
        now = time.time_ns()
        with self._write_tx() as cur:
            if cur.execute('SELECT 1 FROM pipeline_meta WHERE key = ?', (marker,)).fetchone():
                return {**counts, 'alreadyMigrated': True}
            for origin_key, row in manifest:
                counts['generations'] += insert_generation(cur, row, origin_key, now)
            for kind, rows, counter in (('verify', verify_skips, 'verifySuppressions'),
                                        ('dedup', dedup_skips, 'dedupSuppressions')):
                for item in rows:
                    cur.execute(
                        'INSERT INTO suppressions(kind, key, body_hash, payload_json, updated_ns) '
                        'VALUES(?, ?, ?, ?, ?) ON CONFLICT(kind, key) DO UPDATE SET '
                        'body_hash = excluded.body_hash, payload_json = excluded.payload_json, '
                        'updated_ns = excluded.updated_ns',
                        (kind, item['key'], item['bodyHash'], _dumps(item['row']), now),
                    )
                    counts[counter] += 1
            cur.execute('INSERT INTO pipeline_meta(key, value) VALUES(?, ?)', (marker, str(time.time())))
        return {**counts, 'alreadyMigrated': False}

    def append_generation(self, row: dict, *, origin_key: str | None = None) -> int | None:
        """Append one generated-card history row; a repeated ``origin_key`` is ignored."""
        if not isinstance(row, dict):
            raise PipelineError('invalid_generation_row')
        with self._write_tx() as cur:
            added = insert_generation(cur, row, origin_key, time.time_ns())
            return cur.lastrowid if added else None

    def generations(self, *, target_path: str | None = None, source_hash: str | None = None,
                    canonical_key: str | None = None) -> list[dict]:
        """History rows matching any given key, oldest first."""
        clauses, values = [], []
        for column, value in (('target_path', target_path), ('source_hash', source_hash),
                              ('canonical_key', canonical_key)):
            if value:
                clauses.append(f'{column} = ?')
                values.append(value)
        if not clauses:
            return []
        rows = self._conn.execute(
            f'SELECT id, explicit, row_json FROM card_generations WHERE {" OR ".join(clauses)} ORDER BY id',
            values,
        ).fetchall()
        return [_loads(r['row_json'], {}) for r in rows]

    def latest_generation(self, target_path: str) -> dict | None:
        row = self._conn.execute(
            'SELECT row_json FROM card_generations WHERE target_path = ? ORDER BY id DESC LIMIT 1',
            (target_path,),
        ).fetchone()
        return _loads(row['row_json'], {}) if row is not None else None

    def all_generations(self) -> list[dict]:
        rows = self._conn.execute('SELECT row_json FROM card_generations ORDER BY id').fetchall()
        return [_loads(r['row_json'], {}) for r in rows]

    def set_suppression(self, kind: str, key: str, body_hash: str, payload: dict) -> None:
        """The last recorded body hash per (kind, key) wins, as in the 1.x append-only files."""
        if kind not in SUPPRESSION_KINDS or not key or not body_hash:
            raise PipelineError('invalid_suppression')
        with self._write_tx() as cur:
            cur.execute(
                'INSERT INTO suppressions(kind, key, body_hash, payload_json, updated_ns) VALUES(?, ?, ?, ?, ?) '
                'ON CONFLICT(kind, key) DO UPDATE SET body_hash = excluded.body_hash, '
                'payload_json = excluded.payload_json, updated_ns = excluded.updated_ns',
                (kind, key, body_hash, _dumps(payload), time.time_ns()),
            )

    def get_suppression(self, kind: str, key: str) -> dict | None:
        row = self._conn.execute(
            'SELECT body_hash, payload_json FROM suppressions WHERE kind = ? AND key = ?', (kind, key),
        ).fetchone()
        if row is None:
            return None
        return {'bodyHash': row['body_hash'], 'payload': _loads(row['payload_json'], {})}

    def suppressions(self, kind: str) -> dict[str, str]:
        rows = self._conn.execute('SELECT key, body_hash FROM suppressions WHERE kind = ?', (kind,)).fetchall()
        return {r['key']: r['body_hash'] for r in rows}

    @staticmethod
    def dedup_pair_key(page_a: str, page_b: str) -> str:
        first, second = sorted((page_a, page_b))
        return first + '\n' + second

    def enqueue_dedup_pair(self, page_a: str, page_b: str, score: float | None,
                           payload: dict | None = None) -> bool:
        """Queue a pair once; an already queued or claimed pair is left as is."""
        if not page_a or not page_b or page_a == page_b:
            raise PipelineError('invalid_dedup_pair')
        key = self.dedup_pair_key(page_a, page_b)
        now = time.time_ns()
        with self._write_tx() as cur:
            row = cur.execute('SELECT state FROM dedup_pairs WHERE pair_key = ?', (key,)).fetchone()
            if row is not None and row['state'] in ('queued', 'claimed'):
                return False
            cur.execute(
                'INSERT INTO dedup_pairs(pair_key, page_a, page_b, score, state, payload_json, created_ns, '
                "updated_ns) VALUES(?, ?, ?, ?, 'queued', ?, ?, ?) ON CONFLICT(pair_key) DO UPDATE SET "
                "page_a = excluded.page_a, page_b = excluded.page_b, score = excluded.score, state = 'queued', "
                'payload_json = excluded.payload_json, updated_ns = excluded.updated_ns',
                (key, page_a, page_b, score, _dumps(payload or {}), now, now),
            )
            return True

    def dedup_pairs(self, *, state: str = 'queued') -> list[dict]:
        rows = self._conn.execute(
            'SELECT pair_key, page_a, page_b, score, state, payload_json FROM dedup_pairs WHERE state = ? '
            'ORDER BY created_ns, pair_key',
            (state,),
        ).fetchall()
        return [{'pairKey': r['pair_key'], 'pageA': r['page_a'], 'pageB': r['page_b'], 'score': r['score'],
                 'state': r['state'], 'payload': _loads(r['payload_json'], {})} for r in rows]

    def set_dedup_pair_state(self, pair_key: str, state: str) -> bool:
        if state not in DEDUP_STATES:
            raise PipelineError('invalid_dedup_state')
        with self._write_tx() as cur:
            cur.execute('UPDATE dedup_pairs SET state = ?, updated_ns = ? WHERE pair_key = ?',
                        (state, time.time_ns(), pair_key))
            return cur.rowcount == 1

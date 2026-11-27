"""SQLite single ledger backing ``memvane.wiki.pipeline``."""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid

from memvane.kernel.errors import _query_only_uri
from memvane.wiki.pipeline.decisions import DecisionLedgerMixin, insert_generation
from memvane.wiki.pipeline.retention import RetentionMixin
from memvane.wiki.pipeline.queues import JobQueueMixin
from memvane.wiki.pipeline.verify_queue import VerifyQueueMixin
from memvane.wiki.pipeline.schema import DDL_STATEMENTS
from memvane.wiki.pipeline.types import (
    AUDIT_KINDS,
    COMPILE_SUBDIR,
    DB_NAME,
    PIPELINE_APP_ID,
    PIPELINE_USER_VERSION,
    WORKER_BUSY_TIMEOUT_MS,
    PipelineError,
)


def _check_no_symlink(path: Path) -> None:
    if path.is_symlink():
        raise PipelineError('unsafe_pipeline_path')


def pipeline_db_path(project_root: Path | str) -> Path:
    """Return ``<project_root>/.memvane/compile/wiki-pipeline.sqlite3`` after path checks."""
    root = Path(project_root)
    if not root.is_absolute():
        raise PipelineError('unsafe_pipeline_path')
    _check_no_symlink(root)
    memvane_dir = root / '.memvane'
    if memvane_dir.exists() or memvane_dir.is_symlink():
        _check_no_symlink(memvane_dir)
    compile_dir = root / COMPILE_SUBDIR
    if compile_dir.exists() or compile_dir.is_symlink():
        _check_no_symlink(compile_dir)
    db_path = compile_dir / DB_NAME
    if db_path.is_symlink():
        raise PipelineError('unsafe_pipeline_path')
    return db_path


def _ensure_compile_dir(project_root: Path | str) -> Path:
    db_path = pipeline_db_path(project_root)
    root = Path(project_root)
    for directory in (root / '.memvane', db_path.parent):
        _check_no_symlink(directory)
        if not directory.exists():
            os.makedirs(directory, mode=0o700, exist_ok=True)
        _check_no_symlink(directory)
        if not directory.is_dir():
            raise PipelineError('unsafe_pipeline_path')
    return db_path


def _is_pid_alive(pid: int | None) -> bool:
    if pid is None or pid <= 0:
        return False
    forced = os.environ.get('QMD_FORCE_DEAD_PIDS', '')
    if forced:
        dead = {item.strip() for item in forced.split(',') if item.strip()}
        if str(pid) in dead:
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _loads(raw: str | None, default: object = None) -> object:
    if not raw:
        return default
    return json.loads(raw)


class PipelineStore(DecisionLedgerMixin, JobQueueMixin, VerifyQueueMixin, RetentionMixin):
    """Transactional handle over ``wiki-pipeline.sqlite3``."""

    def __init__(self, db_path: Path, *, readonly: bool = False,
                 timeout_ms: int = WORKER_BUSY_TIMEOUT_MS) -> None:
        self.db_path = db_path
        self.readonly = readonly
        self.timeout_ms = int(timeout_ms)
        if readonly:
            if not db_path.exists():
                raise FileNotFoundError(str(db_path))
            self._conn = sqlite3.connect(
                _query_only_uri(db_path), uri=True,
                timeout=max(0.05, self.timeout_ms / 1000.0), isolation_level=None,
            )
            self._conn.row_factory = sqlite3.Row
            self._conn.execute('PRAGMA query_only=ON')
            self._conn.execute(f'PRAGMA busy_timeout={self.timeout_ms}')
            self._validate_schema()
        else:
            self._conn = sqlite3.connect(
                str(db_path),
                timeout=max(0.05, self.timeout_ms / 1000.0), isolation_level=None,
            )
            self._conn.row_factory = sqlite3.Row
            self._conn.execute(f'PRAGMA busy_timeout={self.timeout_ms}')
            # Takes effect only on a database without tables: it must precede WAL.
            self._conn.execute('PRAGMA auto_vacuum=INCREMENTAL')
            self._conn.execute('PRAGMA journal_mode=WAL')
            self._conn.execute('PRAGMA synchronous=FULL')
            self._init_or_validate()

    def _validate_schema(self) -> None:
        app_id = self._conn.execute('PRAGMA application_id').fetchone()[0]
        user_ver = self._conn.execute('PRAGMA user_version').fetchone()[0]
        if app_id != PIPELINE_APP_ID or user_ver != PIPELINE_USER_VERSION:
            self.close()
            raise PipelineError('pipeline_schema_invalid')

    def _init_or_validate(self) -> None:
        with self._write_tx() as cur:
            app_id = cur.execute('PRAGMA application_id').fetchone()[0]
            user_ver = cur.execute('PRAGMA user_version').fetchone()[0]
            if app_id == 0 and user_ver == 0:
                cur.execute(f'PRAGMA application_id={PIPELINE_APP_ID}')
                cur.execute(f'PRAGMA user_version={PIPELINE_USER_VERSION}')
                for stmt in DDL_STATEMENTS:
                    cur.execute(stmt)
            elif app_id != PIPELINE_APP_ID or user_ver != PIPELINE_USER_VERSION:
                raise PipelineError('pipeline_schema_invalid')

    @contextmanager
    def _write_tx(self):
        if self.readonly:
            raise PipelineError('pipeline_readonly')
        self._conn.execute('BEGIN IMMEDIATE')
        try:
            yield self._conn.cursor()
        except BaseException:
            try:
                self._conn.execute('ROLLBACK')
            except sqlite3.Error:
                pass
            raise
        self._conn.execute('COMMIT')

    def close(self) -> None:
        if getattr(self, '_conn', None) is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> PipelineStore:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def meta_get(self, key: str) -> str | None:
        row = self._conn.execute('SELECT value FROM pipeline_meta WHERE key = ?', (key,)).fetchone()
        return row['value'] if row is not None else None

    def meta_set(self, key: str, value: str) -> None:
        with self._write_tx() as cur:
            cur.execute(
                'INSERT INTO pipeline_meta(key, value) VALUES(?, ?) '
                'ON CONFLICT(key) DO UPDATE SET value = excluded.value',
                (key, value),
            )

    def record_refresh(self, event_id: str, source_path: str, ts: str, engine: str) -> dict:
        with self._write_tx() as cur:
            cur.execute(
                'INSERT OR IGNORE INTO refresh_events(event_id, source_path, ts, engine, state) '
                'VALUES(?, ?, ?, ?, ?)',
                (event_id, source_path, ts, engine, 'pending'),
            )
            row = cur.execute('SELECT * FROM refresh_events WHERE event_id = ?', (event_id,)).fetchone()
        return dict(row)

    def unresolved_refreshes(self, source_path: str | None = None) -> list[dict]:
        if source_path is None:
            rows = self._conn.execute(
                "SELECT * FROM refresh_events WHERE state IN ('pending', 'settled') ORDER BY ts, event_id"
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM refresh_events WHERE state IN ('pending', 'settled') AND source_path = ? "
                'ORDER BY ts, event_id',
                (source_path,),
            ).fetchall()
        return [dict(row) for row in rows]

    def refresh_rows(self) -> list[dict]:
        """Every refresh event ordered by its hook time, then arrival (``seq``), any state."""
        rows = self._conn.execute(
            'SELECT rowid AS seq, event_id, source_path, ts, engine, state FROM refresh_events ORDER BY ts, rowid'
        ).fetchall()
        return [dict(row) for row in rows]

    def absorb_refresh_events(self, events: list[dict], consumed: list[str]) -> int:
        """Insert hook inbox events once (``eventId`` key) and note the consumed files."""
        added = 0
        with self._write_tx() as cur:
            for event in events:
                cur.execute(
                    'INSERT OR IGNORE INTO refresh_events(event_id, source_path, ts, engine, state) '
                    "VALUES(?, ?, ?, ?, 'pending')",
                    (event['eventId'], event['sourcePath'], event['ts'], event['engine']),
                )
                added += cur.rowcount
            cur.execute(
                'INSERT INTO pipeline_meta(key, value) VALUES(?, ?) '
                'ON CONFLICT(key) DO UPDATE SET value = excluded.value',
                ('edit_inbox_consumed', _dumps(sorted(consumed))),
            )
        return added

    def resolve_pending_event(self, event: dict, resolved_at: float) -> bool:
        """Resolve ``event`` only while it is the newest event of its source."""
        with self._write_tx() as cur:
            cur.execute(
                'INSERT OR IGNORE INTO refresh_events(event_id, source_path, ts, engine, state) '
                "VALUES(?, ?, ?, ?, 'pending')",
                (event['eventId'], event['sourcePath'], event['ts'], event['engine']),
            )
            newest = cur.execute(
                'SELECT event_id, state FROM refresh_events WHERE source_path = ? ORDER BY ts DESC, rowid DESC '
                'LIMIT 1',
                (event['sourcePath'],),
            ).fetchone()
            if newest is None or newest['event_id'] != event['eventId'] or newest['state'] == 'resolved':
                return False
            cur.execute("UPDATE refresh_events SET state = 'resolved', resolved_at = ? WHERE event_id = ?",
                        (resolved_at, event['eventId']))
            return cur.rowcount == 1

    def prune_refreshes(self) -> int:
        """Keep only each source's newest event; an older one is superseded either way."""
        with self._write_tx() as cur:
            cur.execute(
                'DELETE FROM refresh_events WHERE EXISTS (SELECT 1 FROM refresh_events AS newer '
                'WHERE newer.source_path = refresh_events.source_path AND (newer.ts > refresh_events.ts '
                'OR (newer.ts = refresh_events.ts AND newer.rowid > refresh_events.rowid)))'
            )
            return cur.rowcount

    def settle_event(self, event: dict, source_sha256: str | None, reason: str, settled_at: float) -> bool:
        """Record that one compile attempt for ``event`` ended without a card (not resolved)."""
        with self._write_tx() as cur:
            cur.execute(
                'INSERT OR IGNORE INTO refresh_events(event_id, source_path, ts, engine, state) '
                "VALUES(?, ?, ?, ?, 'pending')",
                (event['eventId'], event['sourcePath'], event['ts'], event['engine']),
            )
            cur.execute(
                "UPDATE refresh_events SET state = 'settled', settled_sha = ?, settled_reason = ?, "
                "settled_at = ? WHERE event_id = ? AND state != 'resolved'",
                (source_sha256, reason, float(settled_at), event['eventId']),
            )
            return cur.rowcount == 1

    def settled_refreshes(self) -> dict[str, dict]:
        rows = self._conn.execute(
            "SELECT event_id, source_path, settled_sha, settled_reason, settled_at FROM refresh_events "
            "WHERE state = 'settled'"
        ).fetchall()
        return {r['event_id']: {'sourcePath': r['source_path'], 'sourceSha256': r['settled_sha'],
                                'reason': r['settled_reason'], 'settledAt': r['settled_at']} for r in rows}

    def settle_refresh(self, event_id: str, sha256: str, reason: str, settled_at: float) -> bool:
        with self._write_tx() as cur:
            cur.execute(
                "UPDATE refresh_events SET state = 'settled', settled_sha = ?, settled_reason = ?, "
                "settled_at = ? WHERE event_id = ? AND state != 'resolved'",
                (sha256, reason, float(settled_at), event_id),
            )
            return cur.rowcount > 0

    def resolve_refresh(self, event_id: str, resolved_at: float) -> bool:
        with self._write_tx() as cur:
            cur.execute(
                "UPDATE refresh_events SET state = 'resolved', resolved_at = ? "
                "WHERE event_id = ? AND state != 'resolved'",
                (float(resolved_at), event_id),
            )
            return cur.rowcount > 0

    def resolve_refreshes_for_source(self, source_path: str, resolved_at: float, *,
                                     event_ids: list[str] | tuple[str, ...] | None = None) -> int:
        with self._write_tx() as cur:
            if event_ids is None:
                cur.execute(
                    "UPDATE refresh_events SET state = 'resolved', resolved_at = ? "
                    "WHERE source_path = ? AND state != 'resolved'",
                    (float(resolved_at), source_path),
                )
            elif not event_ids:
                return 0
            else:
                marks = ','.join('?' for _ in event_ids)
                cur.execute(
                    f"UPDATE refresh_events SET state = 'resolved', resolved_at = ? "
                    f"WHERE source_path = ? AND event_id IN ({marks}) AND state != 'resolved'",
                    (float(resolved_at), source_path, *event_ids),
                )
            return cur.rowcount

    def enqueue_compile_job(self, job_id: str, source_collection: str, source_path: str,
                            trigger: str, engine: str, payload: dict, *,
                            replace_queued: bool = True) -> dict:
        now = time.time_ns()
        encoded = _dumps(payload)
        with self._write_tx() as cur:
            if replace_queued:
                cur.execute(
                    "DELETE FROM compile_jobs WHERE state = 'queued' AND source_collection = ? AND source_path = ?",
                    (source_collection, source_path),
                )
            cur.execute(
                'INSERT INTO compile_jobs(job_id, source_collection, source_path, trigger, engine, '
                'payload_json, state, created_ns, updated_ns) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (job_id, source_collection, source_path, trigger, engine, encoded, 'queued', now, now),
            )
            row = cur.execute('SELECT * FROM compile_jobs WHERE job_id = ?', (job_id,)).fetchone()
        return self._job_row(row)

    def _job_row(self, row: sqlite3.Row) -> dict:
        item = dict(row)
        item['payload'] = _loads(item.pop('payload_json'), {})
        return item

    def claim_compile_jobs(self, limit: int, claim_pid: int, claim_token: str) -> list[dict]:
        if limit <= 0:
            return []
        now = time.time_ns()
        with self._write_tx() as cur:
            rows = cur.execute(
                "SELECT * FROM compile_jobs WHERE state = 'queued' ORDER BY created_ns, job_id LIMIT ?",
                (int(limit),),
            ).fetchall()
            if not rows:
                return []
            ids = [row['job_id'] for row in rows]
            marks = ','.join('?' for _ in ids)
            cur.execute(
                f"UPDATE compile_jobs SET state = 'claimed', claim_pid = ?, claim_token = ?, "
                f'attempt_count = attempt_count + 1, updated_ns = ? WHERE job_id IN ({marks})',
                (int(claim_pid), claim_token, now, *ids),
            )
            claimed = cur.execute(
                f'SELECT * FROM compile_jobs WHERE job_id IN ({marks}) ORDER BY created_ns, job_id',
                ids,
            ).fetchall()
        return [self._job_row(row) for row in claimed]

    def complete_compile_job(self, job_id: str, claim_token: str, *, state: str = 'done') -> bool:
        now = time.time_ns()
        with self._write_tx() as cur:
            cur.execute(
                "UPDATE compile_jobs SET state = ?, claim_pid = NULL, claim_token = NULL, updated_ns = ? "
                "WHERE job_id = ? AND state = 'claimed' AND claim_token = ?",
                (state, now, job_id, claim_token),
            )
            return cur.rowcount > 0

    def requeue_compile_jobs(self, job_ids: list[str] | tuple[str, ...], claim_token: str) -> int:
        if not job_ids:
            return 0
        now = time.time_ns()
        marks = ','.join('?' for _ in job_ids)
        with self._write_tx() as cur:
            cur.execute(
                f"UPDATE compile_jobs SET state = 'queued', claim_pid = NULL, claim_token = NULL, "
                f"updated_ns = ? WHERE job_id IN ({marks}) AND state = 'claimed' AND claim_token = ?",
                (now, *job_ids, claim_token),
            )
            return cur.rowcount

    def recover_dead_compile_claims(self, *, max_attempts: int = 3) -> dict:
        return self._recover_dead_claims('compile_jobs', max_attempts=max_attempts)

    def _recover_dead_claims(self, table: str, *, max_attempts: int) -> dict:
        now = time.time_ns()
        requeued, deferred = 0, 0
        with self._write_tx() as cur:
            rows = cur.execute(
                f"SELECT job_id, claim_pid, attempt_count FROM {table} WHERE state = 'claimed'"
            ).fetchall()
            for row in rows:
                if _is_pid_alive(row['claim_pid']):
                    continue
                new_state = 'deferred' if row['attempt_count'] > max_attempts else 'queued'
                cur.execute(
                    f'UPDATE {table} SET state = ?, claim_pid = NULL, claim_token = NULL, updated_ns = ? '
                    'WHERE job_id = ?',
                    (new_state, now, row['job_id']),
                )
                if new_state == 'deferred':
                    deferred += 1
                else:
                    requeued += 1
        return {'requeued': requeued, 'deferred': deferred}

    def count_compile_jobs(self, *, state: str | None = None) -> int:
        return self._count_jobs('compile_jobs', state=state)

    def _count_jobs(self, table: str, *, state: str | None) -> int:
        if state is None:
            return self._conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
        return self._conn.execute(f'SELECT COUNT(*) FROM {table} WHERE state = ?', (state,)).fetchone()[0]

    def recover_dead_verify_claims(self, *, max_attempts: int = 3) -> dict:
        return self._recover_dead_claims('verify_jobs', max_attempts=max_attempts)

    def count_verify_jobs(self, *, state: str | None = None) -> int:
        return self._count_jobs('verify_jobs', state=state)

    def append_audit(self, kind: str, payload: dict, *, target_path: str | None = None,
                     source_path: str | None = None, body_hash: str | None = None) -> int:
        if kind not in AUDIT_KINDS:
            raise PipelineError(f'invalid_audit_kind:{kind}')
        now = time.time_ns()
        with self._write_tx() as cur:
            cur.execute(
                'INSERT INTO audit_events(kind, target_path, source_path, body_hash, payload_json, created_ns) '
                'VALUES(?, ?, ?, ?, ?, ?)',
                (kind, target_path, source_path, body_hash, _dumps(payload), now),
            )
            # The compile worker's retention pass bounds each kind (pipeline/retention.py).
            return int(cur.lastrowid)

    def query_audit(self, kind: str, *, target_path: str | None = None,
                    source_path: str | None = None, body_hash: str | None = None,
                    limit: int | None = None) -> list[dict]:
        clauses, params = ['kind = ?'], [kind]
        if target_path is not None:
            clauses.append('target_path = ?')
            params.append(target_path)
        if source_path is not None:
            clauses.append('source_path = ?')
            params.append(source_path)
        if body_hash is not None:
            clauses.append('body_hash = ?')
            params.append(body_hash)
        sql = f"SELECT * FROM audit_events WHERE {' AND '.join(clauses)} ORDER BY id"
        if limit is not None:
            sql += f' LIMIT {int(limit)}'
        rows = self._conn.execute(sql, params).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item['payload'] = _loads(item.pop('payload_json'), {})
            out.append(item)
        return out

    def set_cooldown(self, scope: str, key: str, until_ts: float, meta: dict | None = None) -> None:
        with self._write_tx() as cur:
            cur.execute(
                'INSERT INTO cooldowns(scope, key, until_ts, meta_json) VALUES(?, ?, ?, ?) '
                'ON CONFLICT(scope, key) DO UPDATE SET until_ts = excluded.until_ts, meta_json = excluded.meta_json',
                (scope, key, float(until_ts), _dumps(meta) if meta is not None else None),
            )

    def get_cooldown(self, scope: str, key: str) -> dict | None:
        row = self._conn.execute(
            'SELECT * FROM cooldowns WHERE scope = ? AND key = ?', (scope, key)
        ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item['meta'] = _loads(item.pop('meta_json'), None)
        return item

    def cooldowns(self, scope: str) -> dict[str, float]:
        rows = self._conn.execute('SELECT key, until_ts FROM cooldowns WHERE scope = ?', (scope,)).fetchall()
        return {r['key']: float(r['until_ts']) for r in rows}

    def prune_cooldowns(self, scope: str, now: float) -> int:
        with self._write_tx() as cur:
            cur.execute('DELETE FROM cooldowns WHERE scope = ? AND until_ts <= ?', (scope, float(now)))
            return cur.rowcount

    def clear_cooldown(self, scope: str, key: str) -> bool:
        with self._write_tx() as cur:
            cur.execute('DELETE FROM cooldowns WHERE scope = ? AND key = ?', (scope, key))
            return cur.rowcount > 0

    def set_fence(self, scope: str, reason: str, meta: dict | None = None) -> None:
        now = time.time_ns()
        with self._write_tx() as cur:
            cur.execute(
                'INSERT INTO pipeline_fences(scope, reason, meta_json, created_ns) VALUES(?, ?, ?, ?) '
                'ON CONFLICT(scope) DO UPDATE SET reason = excluded.reason, meta_json = excluded.meta_json, '
                'created_ns = excluded.created_ns',
                (scope, reason, _dumps(meta) if meta is not None else None, now),
            )

    def get_fence(self, scope: str) -> dict | None:
        row = self._conn.execute('SELECT * FROM pipeline_fences WHERE scope = ?', (scope,)).fetchone()
        if row is None:
            return None
        item = dict(row)
        item['meta'] = _loads(item.pop('meta_json'), None)
        return item

    def clear_fence(self, scope: str) -> bool:
        with self._write_tx() as cur:
            cur.execute('DELETE FROM pipeline_fences WHERE scope = ?', (scope,))
            return cur.rowcount > 0

    def begin_publish_txn(self, target_path: str, temp_name: str, temp_ino: int, temp_sha: str, *,
                          expected_target_sha: str | None = None,
                          tx_id: str | None = None, generation: dict | None = None) -> str:
        """Record an intended publish; ``generation`` is appended to ``card_generations``
        only when the card actually lands (``published`` or ``published_user_edited``)."""
        resolved_id = tx_id or uuid.uuid4().hex
        now = time.time_ns()
        with self._write_tx() as cur:
            cur.execute(
                'INSERT INTO publish_txns(tx_id, target_path, temp_name, temp_ino, temp_sha, '
                'expected_target_sha, generation_json, state, created_ns, updated_ns) '
                'VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (resolved_id, target_path, temp_name, int(temp_ino), temp_sha,
                 expected_target_sha, _dumps(generation) if generation is not None else None,
                 'publishing', now, now),
            )
        return resolved_id

    def finish_publish_txn(self, tx_id: str, state: str, *, audit_kind: str | None = None,
                           audit_payload: dict | None = None) -> bool:
        now = time.time_ns()
        with self._write_tx() as cur:
            cur.execute(
                'UPDATE publish_txns SET state = ?, updated_ns = ? WHERE tx_id = ?',
                (state, now, tx_id),
            )
            changed = cur.rowcount > 0
            if changed and state in ('published', 'published_user_edited'):
                row = cur.execute('SELECT generation_json FROM publish_txns WHERE tx_id = ?', (tx_id,)).fetchone()
                generation = _loads(row['generation_json'], None) if row else None
                if isinstance(generation, dict):
                    insert_generation(cur, generation, 'publish:' + tx_id, now)
            if changed and audit_kind is not None:
                if audit_kind not in AUDIT_KINDS:
                    raise PipelineError(f'invalid_audit_kind:{audit_kind}')
                row = cur.execute('SELECT target_path FROM publish_txns WHERE tx_id = ?', (tx_id,)).fetchone()
                cur.execute(
                    'INSERT INTO audit_events(kind, target_path, source_path, body_hash, payload_json, created_ns) '
                    'VALUES(?, ?, ?, ?, ?, ?)',
                    (audit_kind, row['target_path'] if row else None, None, None,
                     _dumps(audit_payload or {}), now),
                )
            return changed

    def open_publish_txns(self, *, state: str = 'publishing') -> list[dict]:
        rows = self._conn.execute(
            'SELECT * FROM publish_txns WHERE state = ? ORDER BY created_ns, tx_id', (state,)
        ).fetchall()
        return [dict(row) for row in rows]


def open_pipeline(project_root: Path | str, *, readonly: bool = False,
                  timeout_ms: int | None = None) -> PipelineStore:
    """Open ``wiki-pipeline.sqlite3`` for ``project_root``."""
    effective_timeout = WORKER_BUSY_TIMEOUT_MS if timeout_ms is None else int(timeout_ms)
    if readonly:
        db_path = pipeline_db_path(project_root)
        return PipelineStore(db_path, readonly=True, timeout_ms=effective_timeout)
    db_path = _ensure_compile_dir(project_root)
    return PipelineStore(db_path, readonly=False, timeout_ms=effective_timeout)


def open_pipeline_dir(compile_dir: Path | str, *, readonly: bool = False,
                      timeout_ms: int | None = None) -> PipelineStore:
    """Open ``<compile_dir>/wiki-pipeline.sqlite3`` beside an existing compile queue directory.

    Production compile directories are ``<project>/.memvane/compile``, so this is the same
    file as :func:`open_pipeline`; handoff callers know only their queue path.
    """
    directory = Path(compile_dir).absolute()
    if directory.is_symlink() or not directory.is_dir() or directory.resolve() != directory:
        raise PipelineError('unsafe_pipeline_path')
    db_path = directory / DB_NAME
    if db_path.is_symlink():
        raise PipelineError('unsafe_pipeline_path')
    effective_timeout = WORKER_BUSY_TIMEOUT_MS if timeout_ms is None else int(timeout_ms)
    return PipelineStore(db_path, readonly=readonly, timeout_ms=effective_timeout)

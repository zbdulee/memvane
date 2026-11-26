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
from memvane.wiki.pipeline.schema import DDL_STATEMENTS
from memvane.wiki.pipeline.types import (
    AUDIT_KINDS,
    COMPILE_SUBDIR,
    DB_NAME,
    PIPELINE_APP_ID,
    PIPELINE_USER_VERSION,
    STAGE_QUOTA_BYTES,
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


class PipelineStore:
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

    def enqueue_verify_job(self, job_id: str, target_path: str, source_hash: str | None, payload: dict) -> dict:
        now = time.time_ns()
        with self._write_tx() as cur:
            cur.execute(
                'INSERT OR REPLACE INTO verify_jobs(job_id, target_path, source_hash, payload_json, '
                'state, created_ns, updated_ns) VALUES(?, ?, ?, ?, ?, ?, ?)',
                (job_id, target_path, source_hash, _dumps(payload), 'queued', now, now),
            )
            row = cur.execute('SELECT * FROM verify_jobs WHERE job_id = ?', (job_id,)).fetchone()
        return self._job_row(row)

    def claim_verify_jobs(self, limit: int, claim_pid: int, claim_token: str, *,
                          priority_targets: list[str] | tuple[str, ...] = (),
                          reserve_backlog: bool = True) -> list[dict]:
        if limit <= 0:
            return []
        now = time.time_ns()
        with self._write_tx() as cur:
            queued = cur.execute(
                "SELECT * FROM verify_jobs WHERE state = 'queued' ORDER BY created_ns, job_id"
            ).fetchall()
            if not queued:
                return []
            prio_set = set(priority_targets)
            priority_rows = [r for r in queued if r['target_path'] in prio_set]
            backlog_rows = [r for r in queued if r['target_path'] not in prio_set]
            if reserve_backlog and limit > 1 and backlog_rows and len(priority_rows) >= limit:
                chosen = [*priority_rows[:limit - 1], backlog_rows[0]]
            else:
                chosen = [*priority_rows, *backlog_rows][:limit]
            ids = [r['job_id'] for r in chosen]
            marks = ','.join('?' for _ in ids)
            cur.execute(
                f"UPDATE verify_jobs SET state = 'claimed', claim_pid = ?, claim_token = ?, "
                f'attempt_count = attempt_count + 1, updated_ns = ? WHERE job_id IN ({marks})',
                (int(claim_pid), claim_token, now, *ids),
            )
            by_id = {r['job_id']: r for r in cur.execute(
                f'SELECT * FROM verify_jobs WHERE job_id IN ({marks})', ids).fetchall()}
        return [self._job_row(by_id[jid]) for jid in ids]

    def complete_verify_job(self, job_id: str, claim_token: str, *, state: str = 'done') -> bool:
        now = time.time_ns()
        with self._write_tx() as cur:
            cur.execute(
                "UPDATE verify_jobs SET state = ?, claim_pid = NULL, claim_token = NULL, updated_ns = ? "
                "WHERE job_id = ? AND state = 'claimed' AND claim_token = ?",
                (state, now, job_id, claim_token),
            )
            return cur.rowcount > 0

    def requeue_verify_jobs(self, job_ids: list[str] | tuple[str, ...], claim_token: str) -> int:
        if not job_ids:
            return 0
        now = time.time_ns()
        marks = ','.join('?' for _ in job_ids)
        with self._write_tx() as cur:
            cur.execute(
                f"UPDATE verify_jobs SET state = 'queued', claim_pid = NULL, claim_token = NULL, "
                f"updated_ns = ? WHERE job_id IN ({marks}) AND state = 'claimed' AND claim_token = ?",
                (now, *job_ids, claim_token),
            )
            return cur.rowcount

    def recover_dead_verify_claims(self, *, max_attempts: int = 3) -> dict:
        return self._recover_dead_claims('verify_jobs', max_attempts=max_attempts)

    def count_verify_jobs(self, *, state: str | None = None) -> int:
        return self._count_jobs('verify_jobs', state=state)

    def stage_card(self, stage_id: str, target_path: str, kind: str, state: str,
                   image_blob: bytes, image_sha: str, raw_meta: dict, binding: dict, *,
                   expected_target_sha: str | None = None, proof: dict | None = None,
                   quota_bytes: int = STAGE_QUOTA_BYTES) -> dict | None:
        now = time.time_ns()
        with self._write_tx() as cur:
            used = cur.execute(
                "SELECT COALESCE(SUM(LENGTH(image_blob)), 0) FROM staged_cards "
                "WHERE state IN ('reserved', 'held', 'manual')"
            ).fetchone()[0]
            if used + len(image_blob) > quota_bytes:
                return None
            cur.execute(
                'INSERT INTO staged_cards(stage_id, target_path, kind, state, image_blob, image_sha, '
                'raw_json, expected_target_sha, binding_json, proof_json, created_ns, updated_ns) '
                'VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (stage_id, target_path, kind, state, sqlite3.Binary(image_blob), image_sha,
                 _dumps(raw_meta), expected_target_sha, _dumps(binding),
                 _dumps(proof) if proof is not None else None, now, now),
            )
            row = cur.execute('SELECT * FROM staged_cards WHERE stage_id = ?', (stage_id,)).fetchone()
        return self._staged_row(row)

    def _staged_row(self, row: sqlite3.Row) -> dict:
        item = dict(row)
        item['image_blob'] = bytes(item['image_blob'])
        item['raw_meta'] = _loads(item.pop('raw_json'), {})
        item['binding'] = _loads(item.pop('binding_json'), {})
        item['proof'] = _loads(item.pop('proof_json'), None)
        return item

    def staged_for_target(self, target_path: str, *,
                          states: tuple[str, ...] = ('reserved', 'held', 'manual')) -> list[dict]:
        marks = ','.join('?' for _ in states)
        rows = self._conn.execute(
            f'SELECT * FROM staged_cards WHERE state IN ({marks}) AND target_path = ? ORDER BY created_ns, stage_id',
            (*states, target_path),
        ).fetchall()
        return [self._staged_row(r) for r in rows]

    def list_staged(self, *, states: tuple[str, ...] = ('reserved', 'held', 'manual')) -> list[dict]:
        marks = ','.join('?' for _ in states)
        rows = self._conn.execute(
            f'SELECT * FROM staged_cards WHERE state IN ({marks}) ORDER BY created_ns, stage_id',
            states,
        ).fetchall()
        return [self._staged_row(r) for r in rows]

    def update_staged(self, stage_id: str, state: str, *, proof: dict | None = None) -> bool:
        now = time.time_ns()
        with self._write_tx() as cur:
            if proof is None:
                cur.execute('UPDATE staged_cards SET state = ?, updated_ns = ? WHERE stage_id = ?',
                            (state, now, stage_id))
            else:
                cur.execute(
                    'UPDATE staged_cards SET state = ?, proof_json = ?, updated_ns = ? WHERE stage_id = ?',
                    (state, _dumps(proof), now, stage_id),
                )
            return cur.rowcount > 0

    def delete_staged(self, stage_id: str) -> bool:
        with self._write_tx() as cur:
            cur.execute('DELETE FROM staged_cards WHERE stage_id = ?', (stage_id,))
            return cur.rowcount > 0

    def staged_quota_bytes(self) -> int:
        return self._conn.execute(
            "SELECT COALESCE(SUM(LENGTH(image_blob)), 0) FROM staged_cards "
            "WHERE state IN ('reserved', 'held', 'manual')"
        ).fetchone()[0]

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
                          stage_id: str | None = None, expected_target_sha: str | None = None,
                          tx_id: str | None = None) -> str:
        resolved_id = tx_id or uuid.uuid4().hex
        now = time.time_ns()
        with self._write_tx() as cur:
            cur.execute(
                'INSERT INTO publish_txns(tx_id, target_path, stage_id, temp_name, temp_ino, temp_sha, '
                'expected_target_sha, state, created_ns, updated_ns) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (resolved_id, target_path, stage_id, temp_name, int(temp_ino), temp_sha,
                 expected_target_sha, 'publishing', now, now),
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

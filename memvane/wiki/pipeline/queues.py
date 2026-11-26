"""Source and verify job queues of ``wiki-pipeline.sqlite3``.

A worker claims every queued row of a queue at once and later returns the
rows it did not finish; the returned rows go back ahead of anything queued
meanwhile, exactly like rewriting the head of the former JSONL queue file.
Mixed into :class:`memvane.wiki.pipeline.store.PipelineStore`.
"""
from __future__ import annotations

import json
import time
import uuid

from memvane.wiki.pipeline.types import PipelineError

QUEUES = {'source': 'compile_jobs', 'verify': 'verify_jobs'}
MAX_ATTEMPTS = 3


def _dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _table(queue: str) -> str:
    try:
        return QUEUES[queue]
    except KeyError:
        raise PipelineError('invalid_queue') from None


def _columns(queue: str, payload: dict) -> tuple[list[str], list[object]]:
    if queue == 'source':
        source = payload.get('source') if isinstance(payload.get('source'), dict) else {}
        return (['source_collection', 'source_path', 'trigger', 'engine'],
                [str(source.get('collection') or ''), str(source.get('path') or ''),
                 str(payload.get('trigger') or ''), str(payload.get('engine') or '')])
    source_hash = payload.get('sourceHash')
    return (['target_path', 'source_hash'],
            [str(payload.get('targetPath') or ''), source_hash if isinstance(source_hash, str) else None])


class JobQueueMixin:
    def _insert_job(self, cur, queue: str, payload: dict, created_ns: int, *, pending_blob=None) -> str:
        names, values = _columns(queue, payload)
        job_id = uuid.uuid4().hex
        extra = ['pending_blob'] if queue == 'verify' else []
        cur.execute(
            f'INSERT INTO {_table(queue)}(job_id, {", ".join(names + extra)}, payload_json, state, '
            f'created_ns, updated_ns) VALUES(?, {", ".join("?" for _ in names + extra)}, ?, ?, ?, ?)',
            (job_id, *values, *([pending_blob] if extra else []), _dumps(payload), 'queued',
             created_ns, created_ns),
        )
        return job_id

    def queued_jobs(self, queue: str, *, states: tuple[str, ...] = ('queued',)) -> list[dict]:
        """``{'jobId', 'payload', 'state'}`` in queue order."""
        marks = ','.join('?' for _ in states)
        rows = self._conn.execute(
            f'SELECT job_id, payload_json, state FROM {_table(queue)} WHERE state IN ({marks}) '
            'ORDER BY created_ns, job_id', states,
        ).fetchall()
        return [{'jobId': r['job_id'], 'payload': json.loads(r['payload_json']), 'state': r['state']}
                for r in rows]

    def plan_jobs(self, queue: str, planner):
        """Run ``planner(queued_rows) -> (delete_ids, new_payloads, result)`` in one transaction."""
        table = _table(queue)
        with self._write_tx() as cur:
            rows = cur.execute(
                f"SELECT job_id, payload_json FROM {table} WHERE state = 'queued' ORDER BY created_ns, job_id"
            ).fetchall()
            delete_ids, payloads, result = planner(
                [{'jobId': r['job_id'], 'payload': json.loads(r['payload_json'])} for r in rows])
            for job_id in delete_ids:
                cur.execute(f"DELETE FROM {table} WHERE job_id = ? AND state = 'queued'", (job_id,))
            now = time.time_ns()
            for index, payload in enumerate(payloads):
                self._insert_job(cur, queue, payload, now + index)
        return result

    def append_jobs(self, queue: str, payloads: list[dict], *, pending_blobs: list | None = None) -> int:
        now = time.time_ns()
        with self._write_tx() as cur:
            for index, payload in enumerate(payloads):
                blob = pending_blobs[index] if pending_blobs is not None else None
                self._insert_job(cur, queue, payload, now + index, pending_blob=blob)
        return len(payloads)

    def claim_jobs(self, queue: str, claim_pid: int, claim_token: str) -> list[dict]:
        """Claim every queued row; returns ``{'jobId', 'payload'}`` in queue order."""
        table = _table(queue)
        now = time.time_ns()
        with self._write_tx() as cur:
            rows = cur.execute(
                f"SELECT job_id, payload_json FROM {table} WHERE state = 'queued' ORDER BY created_ns, job_id"
            ).fetchall()
            if rows:
                cur.execute(
                    f"UPDATE {table} SET state = 'claimed', claim_pid = ?, claim_token = ?, "
                    "attempt_count = attempt_count + 1, updated_ns = ? WHERE state = 'queued' AND job_id IN "
                    f"({','.join('?' for _ in rows)})",
                    (int(claim_pid), claim_token, now, *(r['job_id'] for r in rows)),
                )
        return [{'jobId': r['job_id'], 'payload': json.loads(r['payload_json'])} for r in rows]

    def job_blob(self, queue: str, job_id: str) -> bytes | None:
        if queue != 'verify':
            return None
        row = self._conn.execute('SELECT pending_blob FROM verify_jobs WHERE job_id = ?', (job_id,)).fetchone()
        return bytes(row[0]) if row is not None and row[0] is not None else None

    def finish_claim(self, queue: str, claim_token: str, remaining: list[dict], *,
                     pending_blobs: list | None = None) -> int:
        """Drop the claim's rows and queue ``remaining`` ahead of rows queued since the claim."""
        table = _table(queue)
        with self._write_tx() as cur:
            first = cur.execute(
                f'SELECT MIN(created_ns) FROM {table} WHERE claim_token = ?', (claim_token,),
            ).fetchone()[0]
            cur.execute(f'DELETE FROM {table} WHERE claim_token = ?', (claim_token,))
            base = int(first) if first is not None else time.time_ns()
            for index, payload in enumerate(remaining):
                blob = pending_blobs[index] if pending_blobs is not None else None
                self._insert_job(cur, queue, payload, base + index, pending_blob=blob)
        return len(remaining)

    def recover_dead_claims(self, queue: str, is_alive, *, max_attempts: int = MAX_ATTEMPTS) -> dict:
        """Return a dead worker's rows; a row claimed more than ``max_attempts`` times is dropped."""
        table = _table(queue)
        now = time.time_ns()
        requeued, dropped = 0, []
        with self._write_tx() as cur:
            rows = cur.execute(
                f"SELECT job_id, claim_pid, attempt_count, payload_json FROM {table} WHERE state = 'claimed'"
            ).fetchall()
            for row in rows:
                if is_alive(row['claim_pid']):
                    continue
                if row['attempt_count'] > max_attempts:
                    cur.execute(f"UPDATE {table} SET state = 'dropped', reason = 'requeue_exhausted', "
                                'claim_pid = NULL, claim_token = NULL, updated_ns = ? WHERE job_id = ?',
                                (now, row['job_id']))
                    dropped.append(json.loads(row['payload_json']))
                else:
                    cur.execute(f"UPDATE {table} SET state = 'queued', claim_pid = NULL, claim_token = NULL, "
                                'updated_ns = ? WHERE job_id = ?', (now, row['job_id']))
                    requeued += 1
        return {'requeued': requeued, 'dropped': dropped}

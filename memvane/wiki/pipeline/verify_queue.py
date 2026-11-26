"""Verify job rows of ``wiki-pipeline.sqlite3``.

One row is one review: ``kind: review`` checks a visible generated card,
``kind: replace`` carries the replacement image in ``pending_blob``. Rows end
as ``done`` with a reason, never deleted, so a newer row for a target is
visible to every older one. Mixed into
:class:`memvane.wiki.pipeline.store.PipelineStore`.
"""
from __future__ import annotations

import json
import time

TERMINAL_STATES = ('done', 'dropped')

_NEWER = ('EXISTS (SELECT 1 FROM verify_jobs AS n WHERE n.target_path = verify_jobs.target_path '
          'AND (n.created_ns > verify_jobs.created_ns OR '
          '(n.created_ns = verify_jobs.created_ns AND n.job_id > verify_jobs.job_id)))')


def _row(row) -> dict:
    return {'jobId': row['job_id'], 'payload': json.loads(row['payload_json']),
            'state': row['state'], 'claimPid': row['claim_pid'], 'attempts': row['attempt_count']}


def _dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


class VerifyQueueMixin:
    def claim_verify_batch(self, claim_pid: int, claim_token: str, *, budget: int,
                           produced: list[str] | tuple[str, ...] | set = ()) -> dict:
        """Close superseded rows, then claim one run's rows in one transaction.

        A queued row is superseded when any newer row names the same target, in
        any state: a row a dead claim returned never overrides a later review.
        The run takes the just-produced targets first, up to ``budget``; when
        they fill it, one backlog row is still taken so the backlog advances.
        """
        produced = set(produced)
        now = time.time_ns()
        with self._write_tx() as cur:
            superseded = cur.execute(
                "UPDATE verify_jobs SET state = 'done', reason = 'superseded', pending_blob = NULL, "
                f"updated_ns = ? WHERE state = 'queued' AND {_NEWER}", (now,)).rowcount
            rows = cur.execute(
                "SELECT * FROM verify_jobs WHERE state = 'queued' ORDER BY created_ns, job_id").fetchall()
            fresh = [r for r in rows if r['target_path'] in produced]
            backlog = [r for r in rows if r['target_path'] not in produced]
            take_fresh = fresh[:max(0, budget)]
            leftover = budget - len(take_fresh)
            if leftover <= 0 and backlog:
                leftover = 1
            chosen = take_fresh + backlog[:max(0, leftover)]
            for row in chosen:
                cur.execute(
                    "UPDATE verify_jobs SET state = 'claimed', claim_pid = ?, claim_token = ?, "
                    "attempt_count = attempt_count + 1, updated_ns = ? WHERE job_id = ? AND state = 'queued'",
                    (int(claim_pid), claim_token, now, row['job_id']))
            claimed = [_row(cur.execute('SELECT * FROM verify_jobs WHERE job_id = ?',
                                        (row['job_id'],)).fetchone()) for row in chosen]
        return {'claimed': claimed, 'superseded': superseded,
                'fresh': len(take_fresh), 'backlog': len(chosen) - len(take_fresh),
                'remaining': len(rows) - len(chosen)}

    def record_verify_verdict(self, job_id: str, claim_token: str, verdict: dict) -> bool:
        """Keep a paid verdict on its claimed row so a crash before publish never pays again."""
        with self._write_tx() as cur:
            row = cur.execute("SELECT payload_json FROM verify_jobs WHERE job_id = ? AND state = 'claimed' "
                              'AND claim_token = ?', (job_id, claim_token)).fetchone()
            if row is None:
                return False
            payload = json.loads(row['payload_json'])
            payload['_verdict'] = verdict
            cur.execute('UPDATE verify_jobs SET payload_json = ?, updated_ns = ? WHERE job_id = ?',
                        (_dumps(payload), time.time_ns(), job_id))
            return True

    def finish_verify_job(self, job_id: str, claim_token: str, state: str, reason: str | None = None) -> bool:
        """End a claimed row: ``done``/``dropped`` drop its image, ``queued`` returns it."""
        if state not in ('done', 'dropped', 'queued'):
            raise ValueError('invalid verify state')
        blob = ', pending_blob = NULL' if state in TERMINAL_STATES else ''
        with self._write_tx() as cur:
            return cur.execute(
                f'UPDATE verify_jobs SET state = ?, reason = ?, claim_pid = NULL, claim_token = NULL{blob}, '
                "updated_ns = ? WHERE job_id = ? AND state = 'claimed' AND claim_token = ?",
                (state, reason, time.time_ns(), job_id, claim_token)).rowcount > 0

    def release_verify_claim(self, claim_token: str) -> int:
        """Return every row still claimed by ``claim_token`` (an interrupted run)."""
        with self._write_tx() as cur:
            return cur.execute(
                "UPDATE verify_jobs SET state = 'queued', claim_pid = NULL, claim_token = NULL, "
                "updated_ns = ? WHERE state = 'claimed' AND claim_token = ?",
                (time.time_ns(), claim_token)).rowcount

    def open_verify_jobs(self) -> list[dict]:
        """Queued and claimed rows in queue order (compile reads them before extracting).

        ``claimLive`` says whether a claimed row's owner process is alive.
        """
        from memvane.wiki.pipeline.store import _is_pid_alive
        return [{**_row(r), 'claimLive': r['state'] == 'claimed' and _is_pid_alive(r['claim_pid'])}
                for r in self._conn.execute(
                    "SELECT * FROM verify_jobs WHERE state IN ('queued', 'claimed') ORDER BY created_ns, job_id")]

    def verify_job(self, job_id: str) -> dict | None:
        row = self._conn.execute('SELECT * FROM verify_jobs WHERE job_id = ?', (job_id,)).fetchone()
        return None if row is None else {**_row(row), 'reason': row['reason']}

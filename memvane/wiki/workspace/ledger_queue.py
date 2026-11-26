"""The source (and verify) job queue kept in ``wiki-pipeline.sqlite3``.

The queue path (``.memvane/compile/source-queue.jsonl``) stays the handle callers
pass around; no file is written there. A claim takes every queued row and hands
the worker ``(raw, job)`` rows, where ``raw`` is the job's canonical JSON. The
worker returns the lines it did not finish (``finish``); a line that is not a
JSON object carries no job and is dropped.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import uuid

from memvane.wiki.workspace import compile_paths as cp

KINDS = {cp.SOURCE_QUEUE: 'source', cp.VERIFY_QUEUE: 'verify'}


def kind(queue: Path) -> str | None:
    return KINDS.get(Path(queue).name)


def raw(job: dict) -> str:
    return json.dumps(job, ensure_ascii=False, sort_keys=True)


def _store(queue: Path, *, readonly: bool = False):
    from memvane.wiki.pipeline import api as pipeline
    directory = Path(queue).parent
    if not readonly:
        directory.mkdir(parents=True, exist_ok=True)
    return pipeline.open_pipeline_dir(directory, readonly=readonly)


def _alive(pid) -> bool:
    from memvane.wiki.workspace.queue import _is_pid_alive
    return isinstance(pid, int) and pid > 0 and _is_pid_alive(pid)


class LedgerClaim:
    """One worker's claim of a ledger queue; ``name`` is informational only."""

    def __init__(self, queue: Path, token: str, jobs: list[dict]):
        self.queue = Path(queue)
        self.token = token
        self.jobs = jobs
        self.name = f'{self.queue.name}.claimed.{os.getpid()}.{token}'
        self.parent = self.queue.parent

    def exists(self) -> bool:
        return True

    def __fspath__(self) -> str:
        return str(self.parent / self.name)


class LedgerRows(list):
    """``(raw, job)`` rows of a claim with the file-claim return methods."""

    def __init__(self, claim: LedgerClaim):
        super().__init__((raw(item['payload']), item['payload']) for item in claim.jobs)
        self.claim = claim

    def finish(self, queue, claim, remaining) -> None:
        jobs = []
        for line in remaining:
            try:
                job = json.loads(line)
            except (TypeError, ValueError):
                continue
            if isinstance(job, dict):
                jobs.append(job)
        with _store(queue) as store:
            store.finish_claim(kind(queue), claim.token, jobs)

    def privatize(self, queue, claim):
        return claim  # The claim is already private to this worker.


def claim(queue: Path, *, receipt_writer=None) -> LedgerClaim | None:
    """Return dead claims, then claim every queued row; ``None`` when nothing is queued.

    A dead claim past the retry limit is dropped only after its discard is recorded
    (``receipt_writer`` settles a frozen backfill job); if that record fails the row
    stays claimed and the error propagates, so the next claim tries again.
    """
    from memvane.wiki.workspace.types import MAX_REQUEUE_COUNT
    queue = Path(queue)
    with _store(queue) as store:
        recovered = store.recover_dead_claims(kind(queue), _alive, max_attempts=MAX_REQUEUE_COUNT)
        for item in recovered['exhausted']:
            receipt = receipt_writer(item['payload']) if receipt_writer is not None else None
            audit = {'queue': queue.name, 'reason': 'max_requeue_exceeded',
                     'attempts': item['attempts'], 'job': item['payload']}
            if receipt is not None:
                audit['backfillReceipt'] = receipt
            store.append_audit('discard', audit)
            store.drop_jobs(kind(queue), [item['jobId']], 'requeue_exhausted')
        token = uuid.uuid4().hex
        jobs = store.claim_jobs(kind(queue), os.getpid(), token)
    return LedgerClaim(queue, token, jobs) if jobs else None


def live(queue: Path) -> list[tuple[str, dict]]:
    """Queued and claimed rows for readers that only count or inspect them."""
    from memvane.wiki.pipeline import api as pipeline
    queue = Path(queue)
    if not pipeline.pipeline_db_path_in(queue.parent).exists():
        return []
    with _store(queue, readonly=True) as store:
        return [(raw(item['payload']), item['payload'])
                for item in store.queued_jobs(kind(queue), states=('queued', 'claimed'))]


def append(queue: Path, jobs: list[dict]) -> int:
    if not jobs:
        return 0
    with _store(queue) as store:
        return store.append_jobs(kind(queue), jobs)


def plan(queue: Path, planner):
    with _store(queue) as store:
        return store.plan_jobs(kind(queue), planner)

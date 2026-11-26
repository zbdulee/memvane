"""Enumerate native and released pending occurrences for consumer integration assertions."""
from memvane.wiki.workspace.handoff.api import native_live


def pending_files(queue):
    paths = [queue, *queue.parent.glob(queue.name + '.claimed.*')]
    live = native_live(queue)
    if live:
        paths.extend([live, *live.parent.glob('ready-*'), *live.parent.glob('native.claimed.*')])
    return [path for path in paths if path.exists()]


def pending_text(queue):
    """Every queued or claimed row of ``queue``, one JSON line each."""
    from memvane.wiki.workspace import api as workspace
    from memvane.wiki.workspace import types as workspace_types
    if queue.name == workspace_types.SOURCE_QUEUE:
        return ''.join(raw + '\n' for raw, _ in workspace.read_queue(queue))
    return ''.join(path.read_bytes().decode('utf-8') for path in pending_files(queue))


def replace_queue(queue, rows):
    """Make ``rows`` (JSON lines text or job dicts) the whole queued content of a ledger queue."""
    import json
    from memvane.wiki.workspace import api as workspace
    if isinstance(rows, str):
        jobs = [json.loads(line) for line in rows.splitlines() if line.strip()]
    else:
        jobs = list(rows)
    _plan(queue, lambda current: ([r['jobId'] for r in current], jobs, None))


def discard_audits(queue):
    """Logged discards of exhausted queue rows (``audit_events`` kind ``discard``)."""
    import json
    import sqlite3
    from memvane.wiki.pipeline.types import DB_NAME
    path = queue.parent / DB_NAME
    if not path.exists():
        return []
    with sqlite3.connect(str(path)) as db:
        return [json.loads(row[0]) for row in db.execute(
            "SELECT payload_json FROM audit_events WHERE kind = 'discard' ORDER BY id")]


def _plan(queue, planner):
    """Run ``planner`` over the queued rows of a ledger queue in one transaction."""
    from memvane.wiki.pipeline import api as pipeline
    queue.parent.mkdir(parents=True, exist_ok=True)
    kind = 'verify' if queue.name == 'verify-queue.jsonl' else 'source'
    with pipeline.open_pipeline_dir(queue.parent) as store:
        return store.plan_jobs(kind, planner)

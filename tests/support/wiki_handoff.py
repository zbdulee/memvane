"""Cross-domain fixtures for a compiler driving the pipeline ledger.

They read and fail the ledger the way a worker stopped mid-claim leaves it.
"""
def work_rows(queue, kind, *, closed=False):
    """(state, raw bytes or None, stage id) of each stored work, oldest first, read as stored."""
    import json
    import sqlite3
    from memvane.wiki.pipeline.types import DB_NAME
    path = queue.parent / DB_NAME
    if not path.exists():
        return []
    with sqlite3.connect(str(path)) as db:
        rows = db.execute('SELECT control_json, raw_blob, stage_id FROM staged_cards WHERE queue = ? AND kind = ? '
                          'AND closed = ? ORDER BY created_ns, stage_id',
                          (queue.name, kind, 1 if closed else 0)).fetchall()
    return [(json.loads(control), None if raw is None else bytes(raw), stage) for control, raw, stage in rows]


def corrupt_work(queue, stage_id, control=b'unreadable reservation'):
    import sqlite3
    from memvane.wiki.pipeline.types import DB_NAME
    with sqlite3.connect(str(queue.parent / DB_NAME)) as db:
        db.execute('UPDATE staged_cards SET control_json = ? WHERE stage_id = ?', (control.decode(), stage_id))


def work_control(queue, stage_id):
    import sqlite3
    from memvane.wiki.pipeline.types import DB_NAME
    with sqlite3.connect(str(queue.parent / DB_NAME)) as db:
        row = db.execute('SELECT control_json FROM staged_cards WHERE stage_id = ?', (stage_id,)).fetchone()
    return None if row is None else row[0].encode()


def failing_ledger_method(name, error):
    """Patch ``pipeline.api.open_pipeline`` so the handle's ``name`` method raises ``error``."""
    from unittest.mock import patch
    from memvane.wiki.pipeline import api as pipeline
    real = pipeline.open_pipeline

    class Handle:
        def __init__(self, store):
            self._store = store

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._store.close()

        def __getattr__(self, attr):
            if attr == name:
                def fail(*args, **kwargs):
                    raise error
                return fail
            return getattr(self._store, attr)

    return patch.object(pipeline, 'open_pipeline', lambda *a, **k: Handle(real(*a, **k)))


def dead_claim(queue, job, attempts):
    """Leave ``job`` as the only row of ``queue``, claimed by a dead pid ``attempts`` times."""
    import json
    import sqlite3
    import time
    import uuid
    from memvane.wiki.pipeline.types import DB_NAME
    from memvane.wiki.workspace import api as workspace
    _plan(queue, lambda rows: ([r['jobId'] for r in rows], [job], None))
    with sqlite3.connect(str(queue.parent / DB_NAME)) as db:
        db.execute("UPDATE compile_jobs SET state = 'claimed', claim_pid = 99999999, claim_token = ?, "
                   'attempt_count = ?, updated_ns = ?', (uuid.uuid4().hex, attempts, time.time_ns()))
    return json.dumps(job, ensure_ascii=False, sort_keys=True)


def claimed_jobs(queue):
    """Rows of ``queue`` still held by a claim (live or dead)."""
    import json
    import sqlite3
    from memvane.wiki.pipeline.types import DB_NAME
    path = queue.parent / DB_NAME
    if not path.exists():
        return []
    with sqlite3.connect(str(path)) as db:
        return [json.loads(row[0]) for row in db.execute(
            "SELECT payload_json FROM compile_jobs WHERE state = 'claimed' ORDER BY created_ns")]


def hold_claimed(queue, count, pid=None):
    """Mark the newest ``count`` queued rows of ``queue`` as held by a live claim of ``pid``."""
    import os
    import sqlite3
    from memvane.wiki.pipeline.types import DB_NAME
    with sqlite3.connect(str(queue.parent / DB_NAME)) as db:
        db.execute("UPDATE compile_jobs SET state = 'claimed', claim_pid = ?, claim_token = 'held' WHERE job_id IN "
                   "(SELECT job_id FROM compile_jobs WHERE state = 'queued' ORDER BY created_ns DESC LIMIT ?)",
                   (os.getpid() if pid is None else pid, count))


def _plan(queue, planner):
    """Run ``planner`` over the queued rows of a ledger queue in one transaction."""
    from memvane.wiki.pipeline import api as pipeline
    queue.parent.mkdir(parents=True, exist_ok=True)
    kind = 'verify' if queue.name == 'verify-queue.jsonl' else 'source'
    with pipeline.open_pipeline_dir(queue.parent) as store:
        return store.plan_jobs(kind, planner)


def verify_rows(root, *, states=('queued', 'claimed')):
    """Verify rows of a project as ``{'jobId','state','reason','payload','image'}``, oldest first."""
    import json
    import sqlite3
    from pathlib import Path
    from memvane.wiki.pipeline.types import DB_NAME
    path = Path(root) / '.memvane/compile' / DB_NAME
    if not path.exists():
        return []
    marks = ','.join('?' for _ in states)
    with sqlite3.connect(str(path)) as db:
        rows = db.execute(f'SELECT job_id, state, reason, payload_json, pending_blob FROM verify_jobs '
                          f'WHERE state IN ({marks}) ORDER BY created_ns, job_id', tuple(states)).fetchall()
    return [{'jobId': job_id, 'state': state, 'reason': reason, 'payload': json.loads(payload),
             'image': None if blob is None else bytes(blob)} for job_id, state, reason, payload, blob in rows]

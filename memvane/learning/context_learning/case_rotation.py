"""Rotating retention of closed cases in a default-teacher namespace.

The shared ``cases`` table stops capture at ``shared.CASE_LIMIT`` rows. Above
``ROTATE_LIMIT`` rows a worker pass deletes this project's oldest closed
cases until the table is back at the limit. One ``BEGIN IMMEDIATE``
transaction plans and deletes, so no capture, teacher or trainer write falls
between the two.

Never deleted:

* a case that is not closed: any status other than ``gold`` and
  ``pending_review`` while ``active`` (``queued``, ``first_running``,
  ``first_done`` and every other value);
* the training window: the newest ``WINDOW`` gold cases, the newest
  ``WINDOW`` consumed gold cases (replay) and every gold case no trained job
  has consumed (the next job's fresh members);
* the members and the evaluation cases of every job still to be judged: one
  not trained yet (``queued``, ``running``, ``deferred``), one whose cycle is
  ``staged`` or ``staged_evaluated`` and not closed, the application
  journal's cycle and the newest applied candidate;
* the members of the active checkpoint's cycle and of the newest job;
* a gold case that is active but not admitted, and every other project's row.

Deleted, oldest first: consumed gold, ``pending_review`` and ``historical``
cases outside the sets above, with their ``prequential_selection`` rows. The
private ``samples`` store and the gold ledger (input-hash duplicate check)
are not touched.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import prequential

ROTATE_LIMIT = 1800
WINDOW = 128
# A job whose members are not consumed yet: planned, never trained to the end.
UNTRAINED = ('queued', 'running', 'deferred')


def _state_cycle(root, name):
    """The cycle id a namespace state file names, or None when the file is absent.

    An unreadable file raises: without it the protected set is unknown and
    nothing is deleted.
    """
    from .dataset import read_private_json
    path = Path(root) / name
    if not path.exists() and not path.is_symlink():
        return None
    row = read_private_json(path)
    if not isinstance(row, dict):
        raise ValueError('case_rotation_state_invalid')
    return row['cycle_id'] if isinstance(row.get('cycle_id'), str) else None


def _jobs(db):
    """``(job_id, manifest, job_status, cycle_status, eval_status)`` of every job, oldest first."""
    if not prequential._has(db, 'learning_jobs'):
        return []
    cycles = prequential._has(db, 'cycles')
    evaluations = prequential._has(db, 'learning_evaluations')
    return db.execute(
        'SELECT j.job_id,j.manifest,j.status,' + ('c.status' if cycles else 'NULL') + ','
        + ('e.status' if evaluations else 'NULL') + ' FROM learning_jobs j'
        + (' LEFT JOIN cycles c ON c.cycle_id=j.job_id' if cycles else '')
        + (' LEFT JOIN learning_evaluations e ON e.cycle_id=j.job_id AND e.namespace=j.namespace'
           if evaluations else '') + ' ORDER BY j.rowid').fetchall()


def _plan(db, root, project, grant, key, excess):
    """``(request ids to delete, oldest first; protected case count)``."""
    from .learning_queue import semantic_key
    gold = prequential.training_rows(db, project, grant)
    keep = {row[1] for row in gold[-WINDOW:]}
    from .candidate_apply import JOURNAL
    jobs = _jobs(db)
    # Still to be judged: members and evaluation cases stay.
    judged = {_state_cycle(root, JOURNAL)}
    applied = prequential.candidate_job(db, grant, applied=True)
    if applied is not None:
        judged.add(applied['id'])
    # Members only: the evaluation of these is over.
    members_only = {_state_cycle(root, 'active-checkpoint.json'), jobs[-1][0] if jobs else None}
    consumed_members, consumed_keys, shapes = set(), set(), set()
    for job_id, manifest, status, cycle_status, evaluation in jobs:
        job = json.loads(manifest)
        members = prequential._members(job)
        if (job_id in judged or status in UNTRAINED
                or (cycle_status in prequential.OPEN and evaluation != 'blocked')):
            keep.update(rid for rid, _ in members)
            keep.update(row[1] for row in prequential.split(gold, job))
        elif job_id in members_only:
            keep.update(rid for rid, _ in members)
        if status == 'staged':
            consumed_members |= members
            consumed_keys.update(member['key'] for member in job['members'])
            shapes.add((job['namespace'], job['family'], job.get('queryNormalization') is not None))

    def consumed(row):
        if (row[1], row[2]) in consumed_members:
            return True
        # A repeat of a consumed case never becomes a member itself.
        return any(semantic_key(row, namespace, family, normalize_query=normalize) in consumed_keys
                   for namespace, family, normalize in shapes)
    used = [row for row in gold if consumed(row)]
    used_ids = {row[1] for row in used}
    keep.update(row[1] for row in used[-WINDOW:])
    keep.update(row[1] for row in gold if row[1] not in used_ids)
    admitted = {row[1] for row in gold}
    doomed = []
    for rid, status, use_state in db.execute(
            'SELECT request_id,status,use_state FROM cases WHERE project_key=? '
            'ORDER BY captured_at,request_id', (key,)):
        if len(doomed) >= excess:
            break
        if rid in keep:
            continue
        if ((status == 'gold' and rid in admitted) or status == 'pending_review'
                or (use_state == 'historical' and status in ('queued', 'gold'))):
            doomed.append((rid, status, use_state))
    return doomed, len(keep)


def rotate(root, project, grant, *, limit=None) -> dict:
    """Delete this project's oldest closed cases above ``limit`` (``ROTATE_LIMIT``) rows.

    Returns ``{'status', 'count', 'caseRows', 'protected'}``. Raises on any
    read it cannot trust and when a planned row changed under the delete;
    the transaction then rolls back and no row is gone.
    """
    from .shared import DATABASE_NAME, _db, _project_key
    root, project = Path(root), Path(project).resolve(strict=True)
    limit = ROTATE_LIMIT if limit is None else limit
    if not prequential.default_teacher_grant(grant):
        return {'status': 'not_applicable', 'count': 0}
    database = root / DATABASE_NAME
    if not database.exists() and not database.is_symlink():
        return {'status': 'within_limit', 'count': 0, 'caseRows': 0}
    key = _project_key(project)
    with _db(root) as db:
        if not db.in_transaction:
            db.execute('BEGIN IMMEDIATE')
        total = db.execute('SELECT COUNT(*) FROM cases').fetchone()[0]
        if total <= limit:
            return {'status': 'within_limit', 'count': 0, 'caseRows': total}
        doomed, protected = _plan(db, root, project, grant, key, total - limit)
        deleted = 0
        for rid, status, use_state in doomed:
            deleted += db.execute(
                'DELETE FROM cases WHERE project_key=? AND request_id=? AND status=? AND use_state=?',
                (key, rid, status, use_state)).rowcount
        # Checked write: a row that is not the planned one must not go silently.
        if deleted != len(doomed):
            raise ValueError('case_rotation_changed')
        if doomed and prequential._has(db, 'prequential_selection'):
            # Unchecked: the selection kind of a deleted case is read by nothing.
            db.executemany('DELETE FROM prequential_selection WHERE project_key=? AND request_id=?',
                           [(key, rid) for rid, _, _ in doomed])
    result = {'status': 'deleted' if deleted else 'nothing_closed', 'count': deleted,
              'caseRows': total - deleted, 'protected': protected}
    if deleted:
        prequential._emit(project, 'learning.rotation', outcome='deleted', count=deleted,
                          caseRows=total - deleted, protected=protected, limit=limit)
    return result


def rotate_quietly(root, project, grant) -> dict:
    """``rotate`` for the worker pass: a failure is reported, never raised."""
    import sqlite3
    try:
        return rotate(root, project, grant)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, sqlite3.Error) as exc:
        reason = str(exc)[:80] if isinstance(exc, ValueError) else type(exc).__name__
        return {'status': 'rotation_deferred', 'count': 0, 'reason': reason}

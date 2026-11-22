"""Candidates across approval changes, and closing the ones that can never be applied.

A plugin update (renewal) rewrites only the grant's ``binding``, yet the
approval hash covers the whole grant. ``supersede`` runs before the staged
selection of ``shared.run_training_pending``: it carries the default
teacher's frozen candidate to the current approval when every condition
holds and closes every other staged cycle of an earlier approval.
``close_judged`` closes a candidate the application judged (both profiles).
"""
from __future__ import annotations

import json
from pathlib import Path

from . import prequential
from .contracts import digest


def lineage(grant):
    """The grant without its ``binding``: what a plugin update (renewal) leaves unchanged.

    Every authority field (scope, project and namespace roots, operations,
    cohort, expiry) is in it, so a different value never shares a lineage.
    """
    from .store import canonical
    return digest(canonical({key: value for key, value in grant.items() if key != 'binding'}))


def _open_cycles(db, scope):
    """``(cycle_id, approval, manifest, cycle_status, evidence, eval_status)`` of every
    staged cycle of this namespace, newest first."""
    if not prequential._has(db, 'learning_jobs') or not prequential._has(db, 'cycles'):
        return []
    evaluations = prequential._has(db, 'learning_evaluations')
    return db.execute(
        'SELECT c.cycle_id,j.approval,j.manifest,c.status,c.evidence,'
        + ('e.status' if evaluations else 'NULL')
        + ' FROM cycles c JOIN learning_jobs j ON j.job_id=c.cycle_id'
        + (' LEFT JOIN learning_evaluations e ON e.cycle_id=c.cycle_id AND e.namespace=j.namespace'
           if evaluations else '')
        + " WHERE j.namespace=? AND c.status IN ('staged','staged_evaluated')"
        + ' ORDER BY c.started_at DESC,c.rowid DESC', (scope,)).fetchall()


def carry_candidate(root, policy, grant, project):
    """Move the newest open promotion candidate of an earlier approval to the current one.

    Only when the current approval has no open candidate and every condition
    holds: the same lineage, the current job family, the staged base artifact,
    input view and candidate artifact, an active stage whose pinned cases are
    unchanged, no unfinished application journal for it, never rejected, and
    the current grant admits ``local_train``. One transaction rewrites the
    job's approval, the stage's ``authorizationSha256`` (``carriedFrom`` keeps
    the earlier ones) and drops its evaluation row; the candidate is evaluated
    again. Returns ``(cycle_id, previous_approval, carried_count)`` or None.
    """
    from .adapter import EDIT_INPUT_VIEW
    from .automatic_staging import job_family
    from .candidate_apply import JOURNAL, read_journal
    from .learning_queue import LearningQueue
    from .local_cycle import _hash
    from .runtime_authorization import proposal_sha256, require_operation
    from .shared import _db
    from .store import canonical
    if not prequential.default_teacher_grant(grant):
        return None
    root = Path(root)
    approval = proposal_sha256(grant)
    queue = LearningQueue(root)
    with _db(root) as db:
        if prequential.candidate_job(db, grant) is not None:
            return None
        rows = prequential.training_rows(db, project, grant)
        found = None
        for cycle, approved, manifest, status, evidence, evaluation in _open_cycles(db, queue.scope):
            if approved == approval or evaluation == 'blocked':
                continue
            try:
                job = json.loads(manifest)
                if len(job['members']) >= grant['cohort']['minQualityTrainCases']:
                    found = (cycle, approved, manifest, status, evidence, job)
                    break
            except (ValueError, TypeError, KeyError):
                continue
    if found is None:
        return None
    cycle, approved, manifest, status, evidence, job = found
    try:
        stage = json.loads(evidence)
        artifact = root / 'cycles' / cycle / 'candidate.artifact'
        if (job.get('lineage') != lineage(grant) or job.get('family') != job_family(policy)
                or stage.get('baseArtifactSha256') != _hash(policy['baseArtifact'])
                or stage.get('model_input_view') != EDIT_INPUT_VIEW
                or stage.get('artifactPath') != str(artifact)
                or stage.get('artifactSha256') != _hash(artifact)
                or stage.get('sourceActiveAtStage') is not True):
            return None
        queue.rows(job, rows)
    except (OSError, ValueError, TypeError, KeyError):
        return None
    if cycle in prequential.rejected_cycles(root):
        return None
    path = root / JOURNAL
    if path.exists() or path.is_symlink():
        intent = read_journal(root)
        if intent['cycle_id'] == cycle and intent['phase'] not in ('activated', 'rejected'):
            return None
    require_operation(root, policy, 'local_train', approval_sha256=approval)
    carried = [value for value in stage.get('carriedFrom', []) if isinstance(value, str)]
    stage = {key: value for key, value in stage.items() if key not in ('metrics', 'validationCases')}
    stage.update(authorizationSha256=approval, carriedFrom=carried + [approved])
    moved = dict(job, approval=approval)
    # Checked writes in one transaction: a lost write would drop the candidate.
    with _db(root) as db:
        if db.execute('UPDATE learning_jobs SET approval=?,manifest=? WHERE job_id=? AND namespace=? '
                      'AND approval=? AND manifest=?', (approval, canonical(moved), cycle, queue.scope,
                                                        approved, manifest)).rowcount != 1:
            raise ValueError('carry_candidate_changed')
        if db.execute("UPDATE cycles SET status='staged',evidence=? WHERE cycle_id=? AND status=? "
                      'AND evidence=?', (canonical(stage), cycle, status, evidence)).rowcount != 1:
            raise ValueError('carry_candidate_changed')
        if prequential._has(db, 'learning_evaluations'):
            db.execute('DELETE FROM learning_evaluations WHERE cycle_id=? AND namespace=?',
                       (cycle, queue.scope))
    return cycle, approved, len(stage['carriedFrom'])


def close_superseded(root, grant):
    """Close every staged cycle of an approval other than the current grant's (both profiles).

    The approval hash never comes back, so such a cycle cannot be evaluated
    or applied again; without this the staged selection keeps picking it and
    no next job is planned. Artifacts and rows stay. Returns the closed ids.
    """
    from .evaluation_queue import EvaluationQueue
    from .runtime_authorization import proposal_sha256
    from .shared import _db
    approval = proposal_sha256(grant)
    queue = EvaluationQueue(root)
    with _db(root) as db:
        targets = [row[0] for row in _open_cycles(db, queue.scope)
                   if row[1] != approval and row[5] != 'blocked']
    for cycle in targets:
        queue.reject(cycle, 'authorization_superseded')
    return targets


def supersede(root, policy, project):
    """Before the staged selection: carry the candidate, then close older approvals' cycles.

    Only under a currently valid grant: an expired or invalid one is refused
    here and nothing is closed (renewal brings the same approval back). A
    ``ResourceDeferred`` while carrying is raised to the caller.
    """
    from memvane.kernel.resource_deferred import ResourceDeferred
    from .runtime_authorization import require_operation
    try:
        grant = require_operation(root, policy, 'quality_evaluate')
    except ValueError:
        return None
    try:
        carried = carry_candidate(root, policy, grant, project)
    except ResourceDeferred:
        raise  # Momentary pressure decides nothing: the pass ends and nothing is closed.
    except ValueError:
        carried = None  # Not carried (refused or changed meanwhile): it is closed below.
    closed = close_superseded(root, grant)
    if carried is not None:
        prequential._emit(project, 'learning.promotion', outcome='carried', cycle=carried[0], carriedFrom=carried[2])
    if closed:
        prequential._emit(project, 'learning.promotion', outcome='superseded', closed=len(closed))
    return {'carried': carried[0] if carried else None, 'closed': closed}


def close_judged(root, cycle, answer, queue):
    """Close ``cycle`` when the application judged it, or its journal ended ``rejected``.

    Also closes an older candidate whose evaluation completed without being
    applied (a refusal that, before closing existed, left it open): with the
    newer one closed the selection would pick it again. Returns the reason or None.
    """
    from .candidate_apply import JUDGED
    from .shared import _db
    judged = (answer.get('reason') if answer['status'] == 'apply_blocked' and answer.get('reason') in JUDGED
              else 'candidate_rejected' if cycle in prequential.rejected_cycles(root) else None)
    if judged is None:
        return None
    queue.reject(cycle, judged)
    with _db(root) as db:
        older = [row[0] for row in db.execute(
            "SELECT c.cycle_id FROM cycles c JOIN learning_jobs j ON j.job_id=c.cycle_id "
            "JOIN learning_evaluations e ON e.cycle_id=c.cycle_id AND e.namespace=j.namespace "
            "WHERE j.namespace=? AND c.status='staged_evaluated' AND e.status='completed' AND c.cycle_id!=? "
            "AND c.started_at<=(SELECT started_at FROM cycles WHERE cycle_id=?)", (queue.scope, cycle, cycle))]
    for other in older:
        queue.reject(other, 'superseded_by_judged')
    return judged


def unapplied(root, cycle, answer, queue, job):
    """An application that was refused without judging the candidate.

    A stale evaluation decides nothing: the cycle is evaluated again by the next
    pass. A failure under a journal whose pointer is written is counted there.
    Any other failure has no journal to count it (it happened before one
    existed): it is counted on the evaluation, which closes with the reason at
    the limit so that the next job can be planned.
    """
    from .candidate_apply import FAILURE_LIMIT, STALE
    from .shared import _db
    reason = str(answer.get('reason') or 'apply_blocked')[:80]
    if 'activationPending' in answer:
        if answer.get('failures') == FAILURE_LIMIT:
            _activation_pending(root, cycle, reason)
        return
    if reason == STALE:
        with _db(root) as db:
            db.execute("UPDATE cycles SET status='staged' WHERE cycle_id=? AND status='staged_evaluated'", (cycle,))
        queue.refresh(cycle)
        return
    queue.finish(job['id'], 'blocked', reason)
    queue.apply_failed(job['id'], reason, FAILURE_LIMIT)


def _activation_pending(root, cycle, reason):
    """One ``learning.promotion`` row when a written pointer's activation has failed the limit in a row.

    The application goes on being tried; the row is how the log shows that the
    learned selection is closed meanwhile. Diagnostic only: a failure is ignored.
    """
    try:
        prequential._emit(_project_of(root), 'learning.promotion', outcome='activation_pending', reason=reason,
                          cycle=cycle)
    except Exception:
        pass


def _project_of(root):
    from .dataset import read_private_json
    from .learning_profile import NAME
    return read_private_json(Path(root) / NAME)['proposal']['projectRoot']

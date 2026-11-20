"""Prequential (test-then-train) case selection for the default teacher.

Every active v5 ``tool_edit`` case of a default-teacher grant is a training
case: no cohort split, exposure table, ``blocked`` or ``historical`` role.
The cohort tables are never read or written here.

A job's training cutoff is the latest ``captured_at`` of its pinned cases.
Its evaluation cases are the gold cases outside the job captured after the
cutoff: neither the candidate nor the active checkpoint trained on them. A
job with at least ``minQualityTrainCases`` members is a promotion candidate;
while it is staged and not finished no new job is planned (decision S2).

Namespace files (0600): ``checkpoint-history.json`` (application times),
``prequential-promotion.json`` (the last emitted promotion row per cycle).
The ``prequential_selection`` table keeps the recall's selection kind per
gold case (``recall.edit`` ``selection``) for the operating score.
"""
from __future__ import annotations
from memvane.runtime.api import prepared, preparation_checkpoint

import json
from pathlib import Path
import time

from .contracts import EDIT_SCHEMA, digest

ROLE = 'train'
COLUMNS = ('project_key,request_id,input_sha256,sample,bodies,gold,split,project_root,'
           'corpus_fingerprint,family_keys,trained_cycle,captured_at,status,use_state')
BASE = 'base'
HISTORY = 'checkpoint-history.json'
HISTORY_SCHEMA = 'memvane-checkpoint-history-v1'
PROMOTION_STATE = 'prequential-promotion.json'
PROMOTION_SCHEMA = 'memvane-prequential-promotion-v1'
SELECTOR = 'selector'
UNKNOWN = 'unknown'
# Staged statuses of a candidate still under evaluation, and with an applied one.
OPEN = ('staged', 'staged_evaluated')
SETTLED = OPEN + ('applied',)
KEEP_CYCLES = 16
# K, the selector's input size: the same value as recall ``verified_rows.SELECTOR_CANDIDATES``
# (learning cannot import recall; a tests/structure test pins the equality).
SELECTOR_INPUT_K = 10


def _edit(sample) -> bool:
    return (type(sample.get('schema_version')) is int and sample['schema_version'] == EDIT_SCHEMA
            and sample.get('context_origin') == 'tool_edit')


def default_teacher_grant(grant) -> bool:
    from .project_scope import DEFAULT_TEACHER_OPERATIONS
    return isinstance(grant, dict) and grant.get('operations') == DEFAULT_TEACHER_OPERATIONS


# User decision (2026-11-20): a sequential promotion also needs this many evaluation
# cases with a non-empty gold, so an always-empty candidate cannot pass on empty gold
# alone. The grant's own cohort limits stay as they are.
MIN_NONEMPTY_VALIDATION = 10


def min_nonempty_validation(grant) -> int:
    return MIN_NONEMPTY_VALIDATION if default_teacher_grant(grant) else 0


def _admitted_row(row, statuses, cutoff):
    """The admitted row of one ``COLUMNS`` row, or None; no other row matters."""
    if row[12] not in statuses or row[13] != 'active' or row[11] < cutoff:
        return None
    try:
        if digest(row[3]) != row[2]:
            return None
        sample = json.loads(row[3])
        bodies = json.loads(row[4])
        if sample['request_id'] != row[1] or not _edit(sample):
            return None
        if row[5] is not None:
            gold = json.loads(row[5])
            if (gold['input_sha256'] != row[2] or not isinstance(gold['selected_ids'], list)
                    or len(gold['selected_ids']) > 3
                    or not set(gold['selected_ids']) <= set(bodies)):
                return None
        elif row[12] == 'gold':
            return None
    except (KeyError, TypeError, ValueError, AttributeError):
        return None
    return (ROLE, tuple(row[:12]) + (ROLE + '/' + digest(row[1]),))


def _scope(project, grant):
    project = Path(project).resolve(strict=True)
    if grant.get('projectRoot') != str(project):
        raise ValueError('project_cohort_scope_changed')
    return project, grant['cohort']['notBeforeUnixMs'] / 1000


@prepared
def admitted(db, project, grant, statuses=('queued',)):
    """Active v5 edit cases in capture order, shaped like ``general_cohort.admitted``.

    Each row is ``('train', row[:12] + (family,))`` where the family marker is
    unique per case, so family-count limits equal case-count limits.
    """
    project, cutoff = _scope(project, grant)
    rows = db.execute('SELECT ' + COLUMNS + ' FROM cases WHERE project_root=? '
                      'ORDER BY captured_at,request_id', (str(project),))
    result = []
    for row in rows:
        preparation_checkpoint()
        item = _admitted_row(row, statuses, cutoff)
        if item is not None:
            result.append(item)
    return result


@prepared
def admitted_one(db, project, grant, request_id, statuses=('queued',)):
    """``admitted`` for one case: its admitted row, or None. Reads only that row."""
    project, cutoff = _scope(project, grant)
    row = db.execute('SELECT ' + COLUMNS + ' FROM cases WHERE project_root=? AND request_id=?',
                     (str(project), request_id)).fetchone()
    return None if row is None else _admitted_row(row, statuses, cutoff)


def training_rows(db, project, grant):
    """Every admitted gold case: all of them train (no holdout)."""
    return [row for _, row in admitted(db, project, grant, statuses=('gold',))]


def _members(job):
    return {(m['request_id'], m['input_sha256']) for m in job['members']}


def cutoff(rows, job):
    """The latest ``captured_at`` of the job's pinned cases, or None."""
    members = _members(job)
    times = [row[11] for row in rows if (row[1], row[2]) in members]
    return max(times) if times else None


def split(rows, job):
    """Evaluation cases of ``job``: gold outside the job captured after its cutoff."""
    if job is None:
        return []
    limit = cutoff(rows, job)
    if limit is None:
        return []
    members = _members(job)
    return [row for row in rows if (row[1], row[2]) not in members and row[11] > limit]


def evaluation_rows(db, project, grant, job):
    return split(training_rows(db, project, grant), job)


def _has(db, table):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                      (table,)).fetchone() is not None


def _candidates(db, grant, statuses, limit=None):
    """Promotion-sized jobs of this grant, newest first.

    Each item is ``(job, cycle_status, evidence, eval_status, eval_reason)``.
    """
    from .runtime_authorization import proposal_sha256
    if not _has(db, 'learning_jobs') or not _has(db, 'cycles'):
        return []
    evaluations = _has(db, 'learning_evaluations')
    query = ('SELECT j.manifest,j.namespace,j.approval,c.status,c.evidence,'
             + ('e.status,e.reason' if evaluations else 'NULL,NULL')
             + ' FROM learning_jobs j JOIN cycles c ON c.cycle_id=j.job_id'
             + (' LEFT JOIN learning_evaluations e ON e.cycle_id=j.job_id AND e.namespace=j.namespace'
                if evaluations else '') + ' ORDER BY j.rowid DESC')
    namespace = grant.get('namespaceRoot')
    scope = digest(str(Path(namespace).resolve())) if isinstance(namespace, str) else None
    approval = proposal_sha256(grant)
    minimum = grant['cohort']['minQualityTrainCases']
    result = []
    for manifest, owner, approved, status, evidence, evaluation, reason in db.execute(query):
        if (scope is not None and owner != scope) or approved != approval or status not in statuses:
            continue
        try:
            job = json.loads(manifest)
            if len(job['members']) < minimum:
                continue
            stage = json.loads(evidence) if evidence else {}
        except (ValueError, TypeError, KeyError):
            continue
        result.append((job, status, stage, evaluation, reason))
        if limit is not None and len(result) >= limit:
            break
    return result


def candidate_job(db, grant, *, applied=False):
    """The newest unfinished promotion candidate (``applied`` also admits an applied one)."""
    for job, _, _, evaluation, _ in _candidates(db, grant, SETTLED if applied else OPEN):
        if evaluation != 'blocked':
            return job
        return None
    return None


def training_and_validation(db, project, grant, job=None):
    """All gold trains; evaluation rows belong to ``job`` or the current candidate.

    Callers without a job (candidate application revalidation) get the newest
    promotion candidate's evaluation rows, or none in the training phase.
    """
    rows = training_rows(db, project, grant)
    if job is None:
        job = candidate_job(db, grant, applied=True)
    return rows, split(rows, job)


def rejected_cycles(root):
    """Cycles whose application journal (current or archived) is ``rejected``."""
    from .dataset import read_private_json
    from .candidate_apply import ARCHIVE, JOURNAL
    root = Path(root)
    paths = [root / JOURNAL]
    folder = root / ARCHIVE
    if folder.is_dir() and not folder.is_symlink():
        paths.extend(sorted(folder.glob('*.json')))
    result = set()
    for path in paths:
        if not path.exists() and not path.is_symlink():
            continue
        try:
            row = read_private_json(path)
        except (OSError, ValueError):
            continue
        if isinstance(row, dict) and row.get('phase') == 'rejected' and isinstance(row.get('cycle_id'), str):
            result.add(row['cycle_id'])
    return result


def close(root, cycle_id, reason):
    """Block a candidate's evaluation (created when missing) so no step retries it."""
    from .shared import _db
    from .evaluation_queue import EvaluationQueue
    queue = EvaluationQueue(root)
    job_id = 'evaluate:' + cycle_id
    with _db(root) as db:
        queue.schema(db)
        if queue.get(job_id, db=db) is None:
            db.execute("INSERT INTO learning_evaluations VALUES (?,?,?,?,?,'blocked',0,0,0,?)",
                       (job_id, queue.scope, cycle_id, 'closed', 'closed', reason))
        else:
            db.execute("UPDATE learning_evaluations SET status='blocked',retry_at=0,reason=? "
                       "WHERE job_id=? AND namespace=?", (reason, job_id, queue.scope))


def close_rejected(root, grant):
    """Recovery: close every open candidate whose application journal is ``rejected``.

    ``shared._profile_apply`` closes a judged candidate in the pass that judged
    it; this only catches one a pass stopped before closing. Returns the
    closed cycle ids.
    """
    from .shared import _db, DATABASE_NAME
    root = Path(root)
    if not (root / DATABASE_NAME).is_file():
        return []
    judged = {cycle: 'candidate_rejected' for cycle in rejected_cycles(root)}
    if not judged:
        return []
    with _db(root) as db:
        targets = [(job['id'], judged[job['id']]) for job, _, _, evaluation, _ in _candidates(db, grant, OPEN)
                   if job['id'] in judged and evaluation != 'blocked']
    for cycle, reason in targets:
        close(root, cycle, reason)
    return [cycle for cycle, _ in targets]


# ------------------------------------------------- authorization changes

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
    if not _has(db, 'learning_jobs') or not _has(db, 'cycles'):
        return []
    evaluations = _has(db, 'learning_evaluations')
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
    if not default_teacher_grant(grant):
        return None
    root = Path(root)
    approval = proposal_sha256(grant)
    queue = LearningQueue(root)
    with _db(root) as db:
        if candidate_job(db, grant) is not None:
            return None
        rows = training_rows(db, project, grant)
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
    if cycle in rejected_cycles(root):
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
        if _has(db, 'learning_evaluations'):
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
    here and nothing is closed (renewal brings the same approval back).
    """
    from .runtime_authorization import require_operation
    try:
        grant = require_operation(root, policy, 'quality_evaluate')
    except ValueError:
        return None
    try:
        carried = carry_candidate(root, policy, grant, project)
    except ValueError:
        carried = None  # Not carried (refused, deferred or changed meanwhile): it is closed below.
    closed = close_superseded(root, grant)
    if carried is not None:
        _emit(project, 'learning.promotion', outcome='carried', cycle=carried[0], carriedFrom=carried[2])
    if closed:
        _emit(project, 'learning.promotion', outcome='superseded', closed=len(closed))
    return {'carried': carried[0] if carried else None, 'closed': closed}


# ------------------------------------------------------------- checkpoints

def read_history(root):
    from .dataset import read_private_json
    path = Path(root) / HISTORY
    if not path.exists() and not path.is_symlink():
        return []
    value = read_private_json(path)
    entries = value.get('entries') if isinstance(value, dict) and value.get('schema') == HISTORY_SCHEMA else None
    if not isinstance(entries, list):
        raise ValueError('checkpoint_history_invalid')
    for entry in entries:
        if (not isinstance(entry, dict) or not isinstance(entry.get('cycle_id'), str)
                or type(entry.get('appliedAtUnixMs')) is not int):
            raise ValueError('checkpoint_history_invalid')
    return entries


def _applied_at(root, pointer):
    """The application intent time of ``pointer`` (ms), from its journal or archive."""
    from .dataset import read_private_json
    from .candidate_apply import ARCHIVE, JOURNAL
    marker = pointer.get('application')
    if not isinstance(marker, dict) or not isinstance(marker.get('id'), str):
        return None
    root = Path(root)
    for path in (root / JOURNAL, root / ARCHIVE / (marker['id'] + '.json')):
        try:
            row = read_private_json(path)
        except (OSError, ValueError):
            continue
        if not isinstance(row, dict) or not isinstance(row.get('new_receipt'), dict):
            continue
        stamp = row['new_receipt'].get('createdAt')
        if row.get('apply_id') == marker['id'] and type(stamp) in (int, float):
            return int(stamp * 1000)
    return None


def sync_history(root):
    """Append the active checkpoint when it is not the history's last entry.

    Returns True when the file already held it or was written.
    """
    from .dataset import read_private_json
    from .local_cycle import _atomic_json
    root = Path(root)
    pointer = read_private_json(root / 'active-checkpoint.json')
    cycle = pointer.get('cycle_id') if isinstance(pointer, dict) else None
    if not isinstance(cycle, str) or cycle == BASE:
        return True
    entries = read_history(root)
    if entries and entries[-1]['cycle_id'] == cycle:
        return True
    stamp = _applied_at(root, pointer) or int(time.time() * 1000)
    entries.append({'cycle_id': cycle, 'appliedAtUnixMs': stamp})
    _atomic_json(root / HISTORY, {'schema': HISTORY_SCHEMA, 'entries': entries})
    return True


def intervals(entries):
    """``[(checkpoint, start_seconds)]``: the base manifest first, then each application."""
    result = [(BASE, float('-inf'))]
    for entry in entries:
        result.append((entry['cycle_id'], entry['appliedAtUnixMs'] / 1000))
    return result


def checkpoint_at(spans, captured_at):
    found = spans[0][0]
    for checkpoint, start in spans:
        if captured_at >= start:
            found = checkpoint
    return found


# ------------------------------------------------------------------ score

def gold_annotation(sample, gold_ids, *, root=None, project=None) -> dict:
    """Laya's captured choice against the teacher gold for one case.

    ``goldOutsideK`` counts gold cards outside the first K eligible ids, the
    selector's input: answers the selector could not have chosen. With
    ``root`` and ``project`` it adds the checkpoint active at capture and the
    recall's selection kind: the row a worker pass kept earlier
    (``prerecord_selections``), else one log lookup kept now. A failure there
    only leaves the case uncounted (``unknown``).
    """
    sampling = sample['sampling']
    laya = set(sampling['baseline_selected_ids'])
    gold = set(gold_ids)
    window = set(sampling['eligible_ids'][:SELECTOR_INPUT_K])
    result = {'layaCount': len(laya), 'overlap': len(laya & gold), 'exact': laya == gold,
              'goldOutsideK': len(gold - window)}
    if root is None or project is None:
        return result
    checkpoint, selection = None, None
    try:
        from .shared import _db, _project_key
        key, rid = _project_key(project), sample['request_id']
        with _db(root) as db:
            row = db.execute('SELECT captured_at,input_sha256 FROM cases WHERE project_key=? '
                             'AND request_id=?', (key, rid)).fetchone()
        if row is not None:
            checkpoint = checkpoint_at(intervals(read_history(root)), row[0])
            with _db(root) as db:
                selection = (_kept_selections(db, key).get((rid, row[1]))
                             if _has(db, 'prequential_selection') else None)
            if selection is None:
                selection = recall_selection(project, rid)
            if selection is not None:
                with _db(root) as db:
                    _selection_table(db)
                    # Ignored on failure: the case only drops to unknownSelection.
                    db.execute('INSERT OR REPLACE INTO prequential_selection VALUES (?,?,?,?)',
                               (key, rid, row[1], selection))
    except Exception:
        pass  # Diagnostic only: the gold row is already committed.
    result.update(checkpoint=checkpoint, selection=selection or UNKNOWN)
    return result


def _selection_table(db):
    db.execute('CREATE TABLE IF NOT EXISTS prequential_selection (project_key TEXT NOT NULL, '
               'request_id TEXT NOT NULL, input_sha256 TEXT NOT NULL, selection TEXT NOT NULL, '
               'PRIMARY KEY(project_key,request_id))')


def _kept_selections(db, key):
    return {(rid, sha): kind for rid, sha, kind in db.execute(
        'SELECT request_id,input_sha256,selection FROM prequential_selection WHERE project_key=?',
        (key,))}


def recall_selections(project):
    """``{request_id: selection}`` of the ``recall.edit`` rows in one install event log read.

    ``selector`` means the selector answered; ``default_rank`` and ``none``
    did not. Only the newest bounded log window is read.
    """
    from memvane.journal.api import select_route
    from memvane.diagnostics.api import build_progress_events, state_root_for_decision
    state = state_root_for_decision(select_route(str(project), action='update'))
    if state is None:
        return {}
    result = {}
    for row in build_progress_events(state):
        value = row.get('selection')
        if (row.get('event') == 'recall.edit' and isinstance(row.get('request_id'), str)
                and isinstance(value, str) and value):
            result[row['request_id']] = value
    return result


def recall_selection(project, request_id):
    """The ``selection`` of this request's ``recall.edit`` row, or None outside the window."""
    return recall_selections(project).get(request_id)


def prerecord_selections(root, project, grant):
    """Keep the selection kind of every active v5 edit case that has none yet.

    Runs each default-teacher worker pass whatever the teacher's state, so a
    case whose gold comes days later still has the kind the log held at
    capture. The log is read at most once; a case not found keeps no row and
    is looked up again next pass. Returns the number of rows written.
    """
    from .shared import _db, _project_key, DATABASE_NAME, STATUSES
    root = Path(root)
    if not (root / DATABASE_NAME).is_file():
        return 0
    key = _project_key(project)
    with _db(root) as db:
        kept = _kept_selections(db, key) if _has(db, 'prequential_selection') else {}
        missing = [(r[1], r[2]) for _, r in admitted(db, project, grant, statuses=STATUSES)
                   if (r[1], r[2]) not in kept]
    if not missing:
        return 0
    found = recall_selections(project)
    rows = [(key, rid, sha, found[rid]) for rid, sha in missing if rid in found]
    if rows:
        with _db(root) as db:
            _selection_table(db)
            # Ignored on failure: the next pass looks the same cases up again.
            db.executemany('INSERT OR REPLACE INTO prequential_selection VALUES (?,?,?,?)', rows)
    return len(rows)


def score(rows, spans):
    """Operating score per checkpoint (§2-1 (1)).

    ``rows`` are dicts with ``laya``, ``gold``, ``eligible``, ``capturedAt``
    and ``selection``. Only rows the selector answered are scored; the others
    count as ``defaultRank`` or ``unknownSelection``.
    """
    groups = {}
    for row in rows:
        checkpoint = checkpoint_at(spans, row['capturedAt'])
        group = groups.setdefault(checkpoint, {'cases': 0, 'exact': 0, 'overlap': 0, 'laya': 0,
                                               'gold': 0, 'empty': 0, 'emptyAgree': 0,
                                               'nonEmpty': 0, 'nonEmptyAgree': 0,
                                               'defaultRank': 0, 'unknownSelection': 0,
                                               'goldOutsideK': 0})
        selection = row.get('selection')
        if selection is None or selection == UNKNOWN:
            group['unknownSelection'] += 1
            continue
        if selection != SELECTOR:
            group['defaultRank'] += 1
            continue
        laya, gold = set(row['laya']), set(row['gold'])
        group['cases'] += 1
        group['exact'] += laya == gold
        group['overlap'] += len(laya & gold)
        group['laya'] += len(laya)
        group['gold'] += len(gold)
        if gold:
            group['nonEmpty'] += 1
            group['nonEmptyAgree'] += bool(laya)
        else:
            group['empty'] += 1
            group['emptyAgree'] += not laya
        group['goldOutsideK'] += bool(gold - set(row['eligible'][:SELECTOR_INPUT_K]))
    result = {}
    for checkpoint, group in groups.items():
        cases = group['cases']
        value = {'cases': cases,
                 'exactRate': group['exact'] / cases if cases else None,
                 'emptyAgree': group['emptyAgree'] / group['empty'] if group['empty'] else None,
                 'nonEmptyAgree': (group['nonEmptyAgree'] / group['nonEmpty']
                                   if group['nonEmpty'] else None),
                 'defaultRank': group['defaultRank'],
                 'unknownSelection': group['unknownSelection'],
                 'goldOutsideK': group['goldOutsideK']}
        if group['laya'] + group['gold']:
            value['microF1'] = 2 * group['overlap'] / (group['laya'] + group['gold'])
        result[checkpoint] = value
    return result


def score_rows(db, project, grant):
    """Gold cases with the selection kind kept at gold time, for ``score``."""
    from .shared import _project_key
    kinds = _kept_selections(db, _project_key(project)) if _has(db, 'prequential_selection') else {}
    result = []
    for row in training_rows(db, project, grant):
        sample = json.loads(row[3])
        result.append({'laya': sample['sampling']['baseline_selected_ids'],
                       'gold': json.loads(row[5])['selected_ids'],
                       'eligible': sample['sampling']['eligible_ids'],
                       'capturedAt': row[11], 'selection': kinds.get((row[1], row[2]))})
    return result


# ------------------------------------------------------------ worker pass

def _emit(project, event, **fields):
    try:
        from memvane.journal.api import select_route
        from memvane.diagnostics.api import emit
        emit(event, decision=select_route(str(project), action='update'), **fields)
    except Exception:
        pass  # Diagnostic only: the case rows and the cycle table are the record.


def record_pass(root, project, grant, result=None):
    """One worker pass's ``learning.score`` and ``learning.promotion`` rows; never raises."""
    root = Path(root)
    try:
        # Ignored on failure: the next pass re-derives the entry from the pointer.
        sync_history(root)
    except Exception:
        pass
    try:
        _record_score(root, project, grant)
    except Exception:
        pass
    try:
        _record_promotion(root, project, grant, result or {})
    except Exception:
        pass


def _record_score(root, project, grant):
    from .shared import _db
    with _db(root) as db:
        rows = score_rows(db, project, grant)
    spans = intervals(read_history(root))
    scores = score(rows, spans)
    current = spans[-1][0]
    previous = spans[-2][0] if len(spans) > 1 else None
    for label, checkpoint in (('previous', previous), ('current', current)):
        if checkpoint is not None and checkpoint in scores:
            _emit(project, 'learning.score', outcome=label, checkpoint=checkpoint,
                  **scores[checkpoint])


def _read_promotions(root):
    from .dataset import read_private_json
    path = Path(root) / PROMOTION_STATE
    if not path.exists() and not path.is_symlink():
        return {}
    value = read_private_json(path)
    cycles = value.get('cycles') if isinstance(value, dict) and value.get('schema') == PROMOTION_SCHEMA else None
    return dict(cycles) if isinstance(cycles, dict) else {}


def _record_promotion(root, project, grant, result):
    from .shared import _db
    from .local_cycle import _atomic_json
    seen = _read_promotions(root)
    with _db(root) as db:
        candidates = _candidates(db, grant, SETTLED, limit=KEEP_CYCLES // 2)
        rows = training_rows(db, project, grant)
    rejected = rejected_cycles(root)
    changed = False
    for job, status, stage, evaluation, closed in reversed(candidates):
        cycle = job['id']
        metrics = stage.get('metrics') if isinstance(stage.get('metrics'), dict) else None
        if status == 'applied':
            outcome = 'applied'
        elif evaluation == 'blocked' or (cycle in rejected and status == 'staged_evaluated'):
            outcome = 'rejected'
        elif status == 'staged_evaluated':
            outcome = 'evaluated'
        else:
            outcome = 'waiting'
        if outcome == 'waiting' or metrics is None:
            validation = split(rows, job)
            empty = sum(not json.loads(row[5])['selected_ids'] for row in validation)
            counts = [len(validation), empty, len(validation) - empty]
        else:
            total, empty = metrics.get('validation_questions'), metrics.get('empty_validation')
            counts = [total, empty, total - empty if type(total) is int and type(empty) is int else None]
        snapshot = {'outcome': outcome, 'counts': counts}
        before = seen.get(cycle)
        if before == snapshot or (isinstance(before, dict)
                                  and before.get('outcome') in ('applied', 'rejected')):
            continue
        fields = {'cycle': cycle, 'trainCases': len(job['members']), 'evalCases': counts[0],
                  'evalEmpty': counts[1], 'evalNonEmpty': counts[2]}
        if metrics is not None:
            fields.update(candidateExact=metrics.get('candidate', {}).get('exact_rate'),
                          activeExact=metrics.get('incumbent', {}).get('exact_rate'))
        if (outcome in ('applied', 'rejected') and metrics is not None
                and (not isinstance(before, dict) or before.get('outcome') != 'evaluated')):
            _emit(project, 'learning.promotion', outcome='evaluated', **fields)
        reason = None
        if outcome == 'rejected':
            reason = (closed if isinstance(closed, str) and closed
                      else result.get('reason') if result.get('cycle_id') == cycle
                      and isinstance(result.get('reason'), str) else 'candidate_rejected')
        _emit(project, 'learning.promotion', outcome=outcome, reason=reason, **fields)
        seen[cycle] = snapshot
        changed = True
    if changed:
        kept = dict(list(seen.items())[-KEEP_CYCLES:])
        # Ignored on failure: the next pass repeats the row once; nothing depends on it.
        _atomic_json(root / PROMOTION_STATE, {'schema': PROMOTION_SCHEMA, 'cycles': kept})

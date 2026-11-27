"""A read-only view of a project's learning namespace for status screens.

``inspect_project`` reads the namespace that ``invariants.project_counts`` reads
(the project's ``sharedLearning`` root): the case table under ``PRAGMA
query_only``, the accepted grant, the active pointer, the open application
journal, the teacher state and switch, and the operating scores. It opens
nothing for writing, creates no database or directory, takes no lease or
preparation gate, and never raises: the answer names a failure by its code.
"""
from __future__ import annotations

from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import stat

# The default-teacher cohort thresholds ``skills/logs`` shows as promotion progress.
PROMOTION_NEEDS = {'candidateTrain': 100, 'candidateEval': 50,
                   'candidateEvalNonEmpty': 10, 'candidateEvalEmpty': 10}
ACTIVE_POINTER = 'active-checkpoint.json'
POINTER_KEYS = ('cycle_id', 'artifact_sha256', 'schema_version', 'model_input_view')


def _code(exc):
    code = str(exc)
    return code if code.replace('_', '').isalnum() and code.islower() and len(code) <= 80 else type(exc).__name__


def _namespace(project):
    """``(project, root)`` of the learning namespace, root None when learning is not set up."""
    from memvane.settings import api as settings
    from memvane.learning.context_learning import shared
    project = Path(project).resolve(strict=True)
    found = settings.find_project_config(str(project))
    project = Path(found.get('projectRoot') or project).resolve(strict=True)
    root = shared._safe_root(found['config'], project)
    if root is None or not (Path(root) / shared.DATABASE_NAME).is_file():
        return project, None
    return project, Path(root)


def _open(path):
    """The existing database under ``PRAGMA query_only``; refuses a file it would not own."""
    from memvane.kernel.errors import _query_only_uri
    info = os.lstat(path)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1
            or info.st_mode & 0o077):
        raise ValueError('unsafe_shared_database')
    db = sqlite3.connect(_query_only_uri(path), uri=True, timeout=1)
    db.execute('PRAGMA query_only=ON')
    return db


def _private(path):
    """``read_private_json`` of ``path``; None when it does not exist."""
    from memvane.learning.context_learning.dataset import read_private_json
    if not os.path.lexists(path):
        return None
    return read_private_json(path)


def _grant(root):
    """The accepted proposal as written, or None; the selected-route proof is not repeated here."""
    from memvane.learning.context_learning import project_scope
    value = _private(root / project_scope.NAME)
    if (not isinstance(value, dict) or set(value) != {'acceptedSha256', 'proposal'}
            or not isinstance(value['proposal'], dict)
            or value['acceptedSha256'] != project_scope.proposal_sha256(value['proposal'])):
        return None
    proposal = value['proposal']
    cohort = proposal.get('cohort')
    if (not isinstance(cohort, dict) or type(cohort.get('notBeforeUnixMs')) is not int
            or type(cohort.get('minQualityTrainCases')) is not int
            or not isinstance(proposal.get('projectRoot'), str)):
        return None
    return proposal


def _gold_rows(db, project, grant):
    """``prequential.training_rows`` without its preparation gate (``admitted`` is ``@prepared``)."""
    from memvane.learning.context_learning import prequential
    project, cutoff = prequential._scope(project, grant)
    rows = db.execute('SELECT ' + prequential.COLUMNS + ' FROM cases WHERE project_root=? '
                      'ORDER BY captured_at,request_id', (str(project),))
    return [item[1] for item in (prequential._admitted_row(row, ('gold',), cutoff) for row in rows)
            if item is not None]


def _tables(db):
    return {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _promotion(db, project, grant, tables):
    """``prequential.progress`` counts and the newest candidate's evaluation metrics."""
    from memvane.learning.context_learning import prequential
    gold = _gold_rows(db, project, grant)
    counts = {'gold': len(gold), **{key: None for key in PROMOTION_NEEDS}}
    evaluation = None
    if {'learning_jobs', 'cycles'} <= tables:
        job = prequential.candidate_job(db, grant)
        if job is not None:
            rows = prequential.split(gold, job)
            empty = sum(not json.loads(row[5])['selected_ids'] for row in rows)
            counts.update(candidateTrain=len(job['members']), candidateEval=len(rows),
                          candidateEvalNonEmpty=len(rows) - empty, candidateEvalEmpty=empty)
        for job, status, stage, state, reason in prequential._candidates(db, grant, prequential.SETTLED,
                                                                          limit=1):
            metrics = stage.get('metrics') if isinstance(stage.get('metrics'), dict) else None
            evaluation = {'cycle': job['id'], 'cycleStatus': status, 'evaluation': state, 'reason': reason}
            if metrics is not None:
                evaluation.update(
                    candidateExact=(metrics.get('candidate') or {}).get('exact_rate'),
                    activeExact=(metrics.get('incumbent') or {}).get('exact_rate'),
                    validationQuestions=metrics.get('validation_questions'),
                    emptyValidation=metrics.get('empty_validation'))
    return gold, counts, evaluation


def _scores(db, project, root, gold, tables):
    """``prequential.score`` per checkpoint from the kept rows; no inference runs."""
    from memvane.learning.context_learning import prequential
    from memvane.learning.context_learning.shared import _project_key
    kinds = (prequential._kept_selections(db, _project_key(project))
             if 'prequential_selection' in tables else {})
    rows = []
    for row in gold:
        sample = json.loads(row[3])
        rows.append({'laya': sample['sampling']['baseline_selected_ids'],
                     'gold': json.loads(row[5])['selected_ids'],
                     'eligible': sample['sampling']['eligible_ids'],
                     'capturedAt': row[11], 'selection': kinds.get((row[1], row[2]))})
    spans = prequential.intervals(prequential.read_history(root))
    return prequential.score(rows, spans, baseline=len(rows) > prequential.BASELINE_MIN_GOLD)


def _pointers(root):
    from memvane.learning.context_learning import candidate_apply
    active = _private(root / ACTIVE_POINTER)
    journal = _private(root / candidate_apply.JOURNAL)
    pending = None
    if isinstance(journal, dict) and journal.get('phase') not in ('activated', 'rejected', None):
        new = journal.get('new_pointer') if isinstance(journal.get('new_pointer'), dict) else {}
        pending = {'phase': journal.get('phase'), 'cycle_id': journal.get('cycle_id'),
                   **{key: new.get(key) for key in POINTER_KEYS if key in new}}
    return {'active': ({key: active.get(key) for key in POINTER_KEYS if key in active}
                       if isinstance(active, dict) else None),
            'pending': pending,
            'journalPhase': journal.get('phase') if isinstance(journal, dict) else None}


def _teacher(root):
    from memvane.learning.context_learning import teacher_state
    state = teacher_state.read_state(root)
    return {'off': teacher_state.switch_off(root),
            'enablePending': teacher_state.enable_intent_present(root),
            'hosts': state['hosts'], 'enable': state['enable'],
            'consentUnreadable': state['consent']['unreadable']}


def _section(result, name, read):
    try:
        result[name] = read()
    except Exception as exc:  # A status read: one failed section leaves the others.
        result[name] = None
        result.setdefault('errors', {})[name] = _code(exc)


def inspect_project(project_root, *, check_invariants=False):
    """The learning status of ``project_root``; ``status`` ``read``, ``no_namespace`` or ``unreadable``.

    ``promotion`` holds the four promotion counts (``candidateTrain``, ``candidateEval``,
    ``candidateEvalNonEmpty``, ``candidateEvalEmpty``, None without a candidate) with
    ``needs``; ``caseRows``/``caseLimit`` the case table; ``pointers`` the active and
    pending checkpoint pointers; ``teacher`` the teacher switch and host state;
    ``scores`` the operating score per checkpoint and ``evaluation`` the newest
    candidate's evaluation. ``invariants`` (``invariants.project_counts``, which hashes
    artifacts) is read only with ``check_invariants``.
    """
    try:
        project, root = _namespace(project_root)
    except Exception as exc:  # A status read: the answer names the failure and nothing else.
        return {'status': 'unreadable', 'error': _code(exc)}
    if root is None:
        return {'status': 'no_namespace'}
    from memvane.learning.context_learning import shared
    result = {'status': 'read', 'caseRows': None, 'caseLimit': shared.CASE_LIMIT,
              'promotion': None, 'evaluation': None, 'scores': None}
    try:
        grant = _grant(root)
    except Exception as exc:  # An unreadable grant hides only the grant-scoped counts.
        grant = None
        result.setdefault('errors', {})['grant'] = _code(exc)
    try:
        with closing(_open(root / shared.DATABASE_NAME)) as db:
            tables = _tables(db)
            if 'cases' in tables:
                result['caseRows'] = db.execute('SELECT COUNT(*) FROM cases').fetchone()[0]
            if grant is not None and 'cases' in tables:
                gold, counts, evaluation = _promotion(db, project, grant, tables)
                result.update(promotion={**counts, 'needs': dict(PROMOTION_NEEDS)}, evaluation=evaluation)
                _section(result, 'scores', lambda: _scores(db, project, root, gold, tables))
    except Exception as exc:  # The file-backed sections below still read.
        result.setdefault('errors', {})['database'] = _code(exc)
    _section(result, 'pointers', lambda: _pointers(root))
    _section(result, 'teacher', lambda: _teacher(root))
    if check_invariants:
        from memvane.learning.context_learning import invariants
        result['invariants'] = invariants.project_counts(project)
    return result

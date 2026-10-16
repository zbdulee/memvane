"""Default-off, project-bound budget for explicit teacher creation/review calls.

Reservations happen before transport. Unknown provider billing is never claimed
as exact: configured per-call USD is a conservative reservation, not a bill.
Each external retry needs a new explicit attempt ID and consumes another slot.
"""
from runtime_admission import provider_job

import json
import re

from .contracts import digest
from .store import canonical, database
from .teacher import prompt


def _policy(policy, request_id, host, role):
    keys = {'enabled', 'project_id', 'host', 'allowed_request_ids', 'max_calls',
            'max_input_bytes_per_call', 'max_total_input_bytes', 'max_reserved_usd',
            'reserve_usd_per_call', 'creation_model', 'review_model'}
    if not isinstance(policy, dict) or set(policy) != keys or policy['enabled'] is not True:
        raise ValueError('teacher_not_opted_in')
    project = policy['project_id']
    if not isinstance(project, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}', project):
        raise ValueError('invalid_project_scope')
    if policy['host'] not in ('codex', 'claude') or policy['host'] != host or role not in ('creation', 'review'):
        raise ValueError('teacher_scope_mismatch')
    ids = policy['allowed_request_ids']
    if not isinstance(ids, list) or not 1 <= len(ids) <= 2000 or any(not isinstance(x, str) or not x for x in ids) or len(set(ids)) != len(ids) or request_id not in ids:
        raise ValueError('request_outside_scope')
    for name in ('creation_model', 'review_model'):
        model = policy[name]
        if not isinstance(model, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,79}', model):
            raise ValueError('invalid_model')
    for name in ('max_calls', 'max_input_bytes_per_call', 'max_total_input_bytes'):
        v = policy[name]
        if type(v) is not int or not 1 <= v <= (1000 if name == 'max_calls' else 10000000):
            raise ValueError('invalid_teacher_budget')
    for name in ('max_reserved_usd', 'reserve_usd_per_call'):
        v = policy[name]
        if type(v) not in (int, float) or not 0 < v <= 10000:
            raise ValueError('invalid_teacher_budget')
    if policy['reserve_usd_per_call'] > policy['max_reserved_usd']:
        raise ValueError('invalid_teacher_budget')


def _tables(db):
    db.execute('CREATE TABLE IF NOT EXISTS teacher_project (singleton INTEGER PRIMARY KEY CHECK(singleton=1), project_id TEXT NOT NULL, policy_sha256 TEXT NOT NULL, calls_used INTEGER NOT NULL, input_bytes_used INTEGER NOT NULL, reserved_usd_used REAL NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS teacher_attempts (project_id TEXT NOT NULL, request_id TEXT NOT NULL, role TEXT NOT NULL, attempt_id TEXT NOT NULL, input_sha256 TEXT NOT NULL, model TEXT NOT NULL, input_bytes INTEGER NOT NULL, reserved_usd REAL NOT NULL, status TEXT NOT NULL, PRIMARY KEY(project_id,request_id,role,attempt_id))')


def reserve_teacher(state_dir, request_id, role, attempt_id, policy):
    if not isinstance(attempt_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}', attempt_id):
        raise ValueError('invalid_attempt_id')
    with database(state_dir) as db:
        row = db.execute('SELECT body FROM samples WHERE request_id=?', (request_id,)).fetchone()
        if row is None: raise ValueError('unknown_or_deleted_request')
        sample = json.loads(row[0]); _policy(policy, request_id, sample['host'], role)
        payload_bytes = len(prompt(sample).encode())
        if payload_bytes > policy['max_input_bytes_per_call']:
            raise ValueError('teacher_input_budget')
        _tables(db)
        policy_sha = digest(canonical(policy))
        existing = db.execute('SELECT project_id,policy_sha256,calls_used,input_bytes_used,reserved_usd_used FROM teacher_project WHERE singleton=1').fetchone()
        if existing and existing[0] != policy['project_id']:
            raise ValueError('project_root_mismatch')
        if existing and existing[1] != policy_sha:
            raise ValueError('teacher_policy_changed')
        if not existing:
            db.execute('INSERT INTO teacher_project VALUES (1,?,?,0,0,0)', (policy['project_id'], policy_sha))
            existing = (policy['project_id'], policy_sha, 0, 0, 0)
        old = db.execute('SELECT 1 FROM teacher_attempts WHERE project_id=? AND request_id=? AND role=? AND attempt_id=?',
            (policy['project_id'], request_id, role, attempt_id)).fetchone()
        if old: raise ValueError('duplicate_teacher_attempt')
        count, total_bytes, reserved = existing[2:]
        if count >= policy['max_calls'] or total_bytes+payload_bytes > policy['max_total_input_bytes'] or reserved+policy['reserve_usd_per_call'] > policy['max_reserved_usd']+1e-9:
            raise ValueError('teacher_budget_exhausted')
        model = policy[role+'_model']
        db.execute('INSERT INTO teacher_attempts VALUES (?,?,?,?,?,?,?,?,?)',
            (policy['project_id'], request_id, role, attempt_id, digest(row[0]), model,
             payload_bytes, policy['reserve_usd_per_call'], 'reserved'))
        db.execute('UPDATE teacher_project SET calls_used=calls_used+1,input_bytes_used=input_bytes_used+?,reserved_usd_used=reserved_usd_used+? WHERE singleton=1',
            (payload_bytes, policy['reserve_usd_per_call']))
        return {'project_id': policy['project_id'], 'request_id': request_id, 'role': role,
                'attempt_id': attempt_id, 'model': model, 'input_bytes': payload_bytes,
                'reserved_usd': policy['reserve_usd_per_call'], 'status': 'reserved'}


def finish_teacher(state_dir, reservation, status):
    if status not in ('completed', 'failed', 'cancelled'):
        raise ValueError('invalid_teacher_status')
    with database(state_dir) as db:
        _tables(db)
        changed = db.execute('UPDATE teacher_attempts SET status=? WHERE project_id=? AND request_id=? AND role=? AND attempt_id=? AND status=?',
            (status, reservation['project_id'], reservation['request_id'], reservation['role'],
             reservation['attempt_id'], 'reserved')).rowcount
        if changed != 1: raise ValueError('unknown_teacher_reservation')
    return status


@provider_job
def budgeted_label_request(state_dir, request_id, attempt_id, policy, *, invoke=None, **options):
    """Explicit creation only. Review model is separately budgeted, never auto-gold."""
    with database(state_dir) as db:
        row = db.execute('SELECT body FROM samples WHERE request_id=?', (request_id,)).fetchone()
        if row is None: raise ValueError('unknown_or_deleted_request')
        sample = json.loads(row[0]); _policy(policy, request_id, sample['host'], 'creation')
        existing = db.execute('SELECT body FROM labels WHERE request_id=?', (request_id,)).fetchall()
        if len(existing) == len(sample['candidates']):
            from .teacher import validate_response
            validate_response(canonical({'labels': [json.loads(x[0]) for x in existing]}).encode(), sample)
            return {'status': 'completed', 'cli_invocations': 0, 'cached': True,
                    'stored_labels': len(existing), 'label_status': 'provisional-or-reviewed'}
    reservation = reserve_teacher(state_dir, request_id, 'creation', attempt_id, policy)
    if invoke is None:
        from .jobs import label_job
        invoke = label_job
    result = None
    try:
        result = invoke(state_dir, request_id, enabled=True, host=policy['host'],
                        model=policy['creation_model'], **options)
        finish_teacher(state_dir, reservation, 'completed' if result.get('status') == 'completed' else 'failed')
        return dict(result, teacher_budget=reservation)
    except BaseException:
        finish_teacher(state_dir, reservation, 'failed')
        raise


def reserve_review(state_dir, request_id, attempt_id, policy):
    """Reserve a separate review model call. Its output remains advisory only."""
    return reserve_teacher(state_dir, request_id, 'review', attempt_id, policy)


@provider_job
def budgeted_review_request(state_dir, request_id, attempt_id, policy, *, invoke=None, **options):
    reservation = reserve_review(state_dir, request_id, attempt_id, policy)
    if invoke is None:
        from .transport import run_teacher
        invoke = run_teacher
    try:
        with database(state_dir) as db:
            sample = json.loads(db.execute('SELECT body FROM samples WHERE request_id=?', (request_id,)).fetchone()[0])
            originals = {cid: json.loads(body) for cid, body in db.execute(
                'SELECT candidate_id,body FROM labels WHERE request_id=?', (request_id,)).fetchall()}
            original_sha = digest(canonical(originals))
        if len(originals) != len(sample['candidates']): raise ValueError('missing_creation_labels')
        result = invoke(sample, enabled=True, host=policy['host'], model=policy['review_model'], **options)
        if result.get('status') != 'completed':
            finish_teacher(state_dir, reservation, 'failed')
            return {'status': 'failed', 'reason': 'review_transport_failed', 'teacher_budget': reservation}
        from .teacher import validate_response
        labels = validate_response(canonical({'labels': result['labels']}).encode(), sample)
        comparisons = [{'candidate_id': label['candidate_id'],
            'class_agrees': (label['relevance'], label['abstain']) ==
                (originals[label['candidate_id']]['relevance'], originals[label['candidate_id']]['abstain']),
            'review_label_sha256': digest(canonical(label))} for label in labels]
        with database(state_dir) as db:
            _tables(db)
            db.execute('CREATE TABLE IF NOT EXISTS teacher_second_opinions (project_id TEXT NOT NULL, request_id TEXT NOT NULL, attempt_id TEXT NOT NULL, body TEXT NOT NULL, PRIMARY KEY(project_id,request_id,attempt_id))')
            row = db.execute('SELECT body FROM samples WHERE request_id=?', (request_id,)).fetchone()
            if row is None or digest(row[0]) != reservation_input_sha(db, reservation):
                raise ValueError('review_input_changed')
            current = {cid: json.loads(body) for cid, body in db.execute(
                'SELECT candidate_id,body FROM labels WHERE request_id=?', (request_id,)).fetchall()}
            if digest(canonical(current)) != original_sha:
                raise ValueError('review_labels_changed')
            db.execute('INSERT INTO teacher_second_opinions VALUES (?,?,?,?)',
                (reservation['project_id'], request_id, attempt_id, canonical(comparisons)))
        finish_teacher(state_dir, reservation, 'completed')
        return {'status': 'completed', 'second_opinion_only': True, 'comparisons': comparisons,
                'teacher_budget': reservation}
    except BaseException:
        finish_teacher(state_dir, reservation, 'failed')
        raise


def reservation_input_sha(db, reservation):
    row = db.execute('SELECT input_sha256 FROM teacher_attempts WHERE project_id=? AND request_id=? AND role=? AND attempt_id=?',
        (reservation['project_id'], reservation['request_id'], reservation['role'], reservation['attempt_id'])).fetchone()
    if row is None: raise ValueError('unknown_teacher_reservation')
    return row[0]

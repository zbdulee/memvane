"""Bind a real host edit only after its generated wiki pages are indexed.

The host ticket and exact prompt are reserved before PostToolUse returns. The
topical worker later replays the same query against the settled index. The
one-use ticket is claimed only by shared.enqueue's ordinary provenance gate.
"""
from __future__ import annotations

import json
import hashlib
from pathlib import Path
import sqlite3
import time

from .store import canonical
from .contracts import MAX_EDIT_PROMPT_CHARS
from .synthetic_experiment import shared_for, story_for
from .synthetic_scope import NOVEL_ROOT, generated_path

SCHEMA = 'qmd-deferred-host-edit-v1'
MAX_PENDING = 128
MAX_REPLAYS_PER_WAKE = 4
RETRY_BASE_SECONDS = 30
RETRY_MAX_SECONDS = 300


def _table(db):
    db.execute('CREATE TABLE IF NOT EXISTS deferred_edit_captures ('
               'ticket_id TEXT PRIMARY KEY, project_root TEXT NOT NULL, '
               'story_root TEXT NOT NULL, relative_path TEXT NOT NULL, '
               'after_sha256 TEXT NOT NULL, host TEXT NOT NULL, '
               'payload TEXT NOT NULL, before_content TEXT, '
               'after_content TEXT NOT NULL, created_at REAL NOT NULL, '
               'state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, '
               'last_error TEXT, completed_at REAL, '
               'case_request_id TEXT, index_fingerprint TEXT, '
               'attempted_card_fingerprint TEXT, next_retry_at REAL)')


def _owner(project):
    project = Path(project).resolve(strict=True)
    if project != NOVEL_ROOT.resolve(strict=True):
        raise ValueError('deferred_edit_scope_invalid')
    story = story_for(project).resolve(strict=True)
    root = shared_for(project).resolve(strict=True)
    return project, story, root


def reserve(project, payload, host, change):
    """Authenticate and save the real host payload before any case can bind."""
    from .shared import _db
    from .edit_ticket import _source_active
    project, story, root = _owner(project)
    if host not in ('codex', 'claude') or not isinstance(payload, dict):
        raise ValueError('deferred_edit_host_invalid')
    event = payload.get('edit_event')
    ticket_id = event.get('ticket_id') if isinstance(event, dict) else None
    cwd = payload.get('cwd')
    try:
        cwd_path = Path(cwd).resolve(strict=True)
    except (OSError, TypeError, ValueError) as exc:
        raise ValueError('deferred_edit_payload_invalid') from exc
    if (not isinstance(ticket_id, str) or
            payload.get('context_origin') != 'tool_edit' or
            cwd_path != story and story not in cwd_path.parents or
            not isinstance(payload.get('prompt'), str) or
            not payload['prompt'].strip() or len(payload['prompt']) > MAX_EDIT_PROMPT_CHARS):
        raise ValueError('deferred_edit_payload_invalid')
    if not isinstance(change, dict):
        raise ValueError('deferred_edit_content_invalid')
    before, after = change.get('before_content'), change.get('after_content')
    if ((before is not None and not isinstance(before, str)) or
            not isinstance(after, str) or
            change.get('before_sha256') != (hashlib.sha256(before.encode()).hexdigest()
                                            if before is not None else None) or
            change.get('after_sha256') != hashlib.sha256(after.encode()).hexdigest()):
        raise ValueError('deferred_edit_content_invalid')
    with _db(root) as db:
        _table(db)
        # The ticket pruner runs on the ordinary edit path. Do not let its
        # orphaned observations consume the bounded pending store forever.
        db.execute("UPDATE deferred_edit_captures SET state='superseded',"
                   "last_error='expired_or_source_changed' WHERE state='pending' "
                   'AND NOT EXISTS (SELECT 1 FROM edit_tickets t WHERE '
                   't.ticket_id=deferred_edit_captures.ticket_id AND '
                   't.expires_at>?)', (time.time(),))
        row = db.execute('SELECT relative_path,before_sha256,after_sha256,event_json,expires_at,'
                         'request_id FROM edit_tickets WHERE ticket_id=? AND '
                         'project_root=? AND story_root=?',
                         (ticket_id, str(project), str(story))).fetchone()
        if (row is None or row[5] is not None or time.time() >= row[4] or
                event != {**json.loads(row[3]), 'ticket_id': ticket_id} or
                change.get('path') != row[0] or
                change.get('before_sha256') != row[1] or
                change.get('after_sha256') != row[2] or
                event.get('segment_sha256') != hashlib.sha256(
                    payload['prompt'].encode()).hexdigest() or
                not _source_active(story, row[0], row[2])):
            raise ValueError('deferred_edit_ticket_invalid')
        old = db.execute('SELECT payload,host,before_content,after_content '
                         'FROM deferred_edit_captures WHERE ticket_id=?',
                         (ticket_id,)).fetchone()
        encoded = canonical(payload)
        if old is not None:
            if old != (encoded, host, before, after):
                raise ValueError('deferred_edit_conflict')
            return 'already_reserved'
        if db.execute("SELECT COUNT(*) FROM deferred_edit_captures WHERE "
                      "state='pending'").fetchone()[0] >= MAX_PENDING:
            raise ValueError('deferred_edit_capacity')
        db.execute('INSERT INTO deferred_edit_captures '
                   '(ticket_id,project_root,story_root,relative_path,after_sha256,'
                   'host,payload,before_content,after_content,created_at,state,attempts,last_error) '
                   'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   (ticket_id, str(project), str(story), row[0], row[2],
                    host, encoded, before, after, time.time(), 'pending', 0, None))
    return 'reserved'


def pending(project, ticket_id):
    from .shared import _db
    _project, _story, root = _owner(project)
    with _db(root) as db:
        _table(db)
        return db.execute('SELECT 1 FROM deferred_edit_captures WHERE '
                          "ticket_id=? AND state='pending'", (ticket_id,)).fetchone() is not None


def _active_paths(db, project, story, path, sha):
    cards = set()
    for card_path, snapshots in db.execute('SELECT card_path,source_snapshots '
            "FROM topical_worker_versions WHERE state='active' AND "
            'project_root=? AND story_root=?', (str(project), str(story))):
        try:
            sources = json.loads(snapshots)
            revisions = [item['revision'] for item in sources]
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError('deferred_edit_registration_invalid') from exc
        if (revisions and all(item.get('path') == path and item.get('sha256') == sha
                              for item in revisions)):
            cards.add(card_path)
    return cards


def allowed_paths(project, ticket_id):
    """Current indexed worker cards for the ticket's exact after snapshot."""
    from .shared import _db
    from .edit_ticket import _source_active
    project, story, root = _owner(project)
    with _db(root) as db:
        _table(db)
        row = db.execute('SELECT relative_path,after_sha256,state FROM '
                         'deferred_edit_captures WHERE ticket_id=?',
                         (ticket_id,)).fetchone()
        if row is None or row[2] != 'pending' or not _source_active(story, row[0], row[1]):
            return set()
        return _active_paths(db, project, story, row[0], row[1])


def candidate_allowed(candidate_id, allowed):
    try:
        return str(generated_path(candidate_id)) in allowed
    except (TypeError, ValueError):
        return False


def _ready(root):
    """Yield background replay to the existing CPU and memory admission gate."""
    try:
        from .shared import _policy, resource_report
        return resource_report(_policy(root))['ready'] is True
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
        return False


def _committed_case(db, project, ticket_id):
    row = db.execute('SELECT t.request_id,t.input_sha256,c.sample,'
        'c.corpus_fingerprint,c.captured_at,d.created_at,c.status,c.use_state '
        'FROM edit_tickets t '
        'JOIN deferred_edit_captures d ON d.ticket_id=t.ticket_id '
        'LEFT JOIN cases c ON c.request_id=t.request_id AND '
        'c.project_root=t.project_root AND c.input_sha256=t.input_sha256 '
        'WHERE t.ticket_id=? AND t.project_root=?',
        (ticket_id, str(project))).fetchone()
    if (row is None or row[0] is None or row[2] is None or
            row[6] == 'awaiting_local' or row[7] != 'active'):
        return None
    try:
        sample = json.loads(row[2])
    except (TypeError, ValueError) as exc:
        raise ValueError('deferred_edit_case_changed') from exc
    if (sample.get('request_id') != row[0] or
            sample.get('edit_event', {}).get('ticket_id') != ticket_id or
            not isinstance(row[3], str) or not row[3] or
            not isinstance(row[4], (int, float)) or row[4] < row[5]):
        raise ValueError('deferred_edit_case_changed')
    return row[0], row[3]


def validate_case_observation(db, project, story, ticket_id, sample, path,
                              before_sha, after_sha, input_sha):
    """Keep the complete host snapshot bound through gold and training reads."""
    _table(db)
    row = db.execute('SELECT project_root,story_root,relative_path,after_sha256,'
        'host,payload,before_content,after_content,created_at,state,'
        'case_request_id,index_fingerprint FROM deferred_edit_captures '
        'WHERE ticket_id=?', (ticket_id,)).fetchone()
    if row is None:
        raise ValueError('deferred_edit_observation_changed')
    (project_root, story_root, relative, saved_after, host, payload,
     before, after, created, state, case_id, index_fp) = row
    try:
        original = json.loads(payload)
    except (TypeError, ValueError) as exc:
        raise ValueError('deferred_edit_observation_changed') from exc
    case = db.execute('SELECT corpus_fingerprint,captured_at,use_state,status FROM cases '
        'WHERE project_root=? AND request_id=? AND input_sha256=?',
        (str(project), sample['request_id'], input_sha)).fetchone()
    if (project_root != str(project) or story_root != str(story)
            or relative != path or saved_after != after_sha
            or host != sample['host'] or
            original.get('prompt') != sample['prompt']['text'] or
            original.get('edit_event') != sample['edit_event'] or
            (hashlib.sha256(before.encode()).hexdigest()
             if isinstance(before, str) else None) != before_sha or
            not isinstance(after, str) or
            hashlib.sha256(after.encode()).hexdigest() != after_sha or
            not isinstance(created, (int, float)) or
            state not in ('pending', 'completed') or case is None or
            not isinstance(case[0], str) or not case[0] or
            not isinstance(case[1], (int, float)) or
            case[1] < created or case[2] != 'active' or
            (state == 'completed' and
             (case_id != sample['request_id'] or index_fp != case[0])) or
            (state == 'pending' and
             (case_id is not None or index_fp is not None or
              case[3] != 'awaiting_local'))):
        raise ValueError('deferred_edit_observation_changed')
    return {'before_sha256': before_sha, 'after_sha256': after_sha,
            'index_fingerprint': case[0], 'captured_at': case[1]}


def finalize_before_gold(db, project, sample, input_sha):
    """Seal the complete host observation before the case becomes runnable."""
    ticket_id = sample['edit_event']['ticket_id']
    row = db.execute('SELECT story_root,relative_path,before_sha256,after_sha256 '
                     'FROM edit_tickets WHERE ticket_id=? AND project_root=? '
                     'AND request_id=? AND input_sha256=?',
                     (ticket_id, str(project), sample['request_id'], input_sha)).fetchone()
    if row is None:
        raise ValueError('deferred_edit_observation_changed')
    story, path, before_sha, after_sha = row
    evidence = validate_case_observation(db, project, Path(story), ticket_id,
        sample, path, before_sha, after_sha, input_sha)
    changed = db.execute("UPDATE deferred_edit_captures SET state='completed',"
        'completed_at=?,case_request_id=?,index_fingerprint=?,last_error=NULL,'
        'next_retry_at=NULL '
        "WHERE ticket_id=? AND state='pending' AND case_request_id IS NULL",
        (time.time(), sample['request_id'], evidence['index_fingerprint'],
         ticket_id)).rowcount
    if changed != 1:
        raise ValueError('deferred_edit_observation_changed')


def _activate_bound_case(project, root, request_id):
    """Recover a crash after ticket binding and local capture without replay."""
    from .shared import _db, activate_edit_case
    with _db(root) as db:
        row = db.execute('SELECT sample FROM cases WHERE project_root=? '
            "AND request_id=? AND status='awaiting_local' AND use_state='active'",
            (str(project), request_id)).fetchone()
    if row is None:
        return
    try:
        import config as qmd_config
        found = qmd_config.find_project_config(str(project))
        if Path(found['projectRoot']).resolve(strict=True) != project:
            return
        sample = json.loads(row[0])
        activate_edit_case(found['config'], project, sample, captured=True)
    except (KeyError, TypeError, ValueError, OSError, sqlite3.Error):
        return


def next_retry_due(project):
    """Earliest durable local retry; no provider call or payload exposure."""
    from .shared import _db
    project, _story, root = _owner(project)
    with _db(root) as db:
        _table(db)
        row = db.execute('SELECT MIN(d.next_retry_at) FROM '
            'deferred_edit_captures d JOIN edit_tickets t '
            'ON t.ticket_id=d.ticket_id WHERE d.state=? '
            'AND d.next_retry_at IS NOT NULL AND t.expires_at>?',
            ('pending', time.time())).fetchone()
    return row[0] if row and isinstance(row[0], (int, float)) else None


def resume_ready(project, *, timeout=60, allow_replay=True, return_more=False):
    """Replay exact pending host inputs after topical registration/index ACK."""
    from .shared import _db
    from .edit_ticket import _source_active
    project, story, root = _owner(project)
    outcomes = []
    replayed = 0
    more = False
    with _db(root) as db:
        _table(db)
        rows = db.execute('SELECT d.ticket_id,d.relative_path,d.after_sha256,'
            'd.host,d.payload,t.expires_at,t.request_id,'
            'd.attempted_card_fingerprint,d.next_retry_at,d.attempts '
            'FROM deferred_edit_captures d '
            'LEFT JOIN edit_tickets t ON t.ticket_id=d.ticket_id WHERE d.state=? '
            'ORDER BY d.attempts,d.created_at,d.ticket_id', ('pending',)).fetchall()
    for ticket_id, path, sha, host, encoded, expiry, request_id, attempted, retry_at, attempts in rows:
        card_fingerprint = attempted
        replay_attempted = False
        if request_id is not None:
            _activate_bound_case(project, root, request_id)
            state, error = 'pending', 'case_not_committed'
        elif expiry is None or time.time() >= expiry or not _source_active(story, path, sha):
            state, error = 'superseded', 'expired_or_source_changed'
        else:
            with _db(root) as db:
                allowed = _active_paths(db, project, story, path, sha)
                revisions = sorted((card_path, card_sha) for card_path, card_sha in
                    db.execute('SELECT card_path,card_sha256 FROM '
                        "topical_worker_versions WHERE state='active' AND "
                        'project_root=? AND story_root=?',
                        (str(project), str(story))) if card_path in allowed)
            if not allowed:
                # A retired card cannot serve this edit. New registration
                # will wake the ordinary topical worker and set a new due.
                if retry_at is not None:
                    with _db(root) as db:
                        db.execute('UPDATE deferred_edit_captures SET '
                            'next_retry_at=NULL,attempted_card_fingerprint=NULL,'
                            'last_error=? WHERE ticket_id=? AND state=?',
                            ('registered_card_unavailable', ticket_id, 'pending'))
                continue
            card_fingerprint = hashlib.sha256(
                canonical(revisions).encode()).hexdigest()
            if attempted == card_fingerprint and retry_at is not None and time.time() < retry_at:
                continue
            if not allow_replay:
                continue
            if not _ready(root):
                with _db(root) as db:
                    db.execute('UPDATE deferred_edit_captures SET '
                        'last_error=?,attempted_card_fingerprint=?,next_retry_at=? '
                        'WHERE ticket_id=? AND state=?',
                        ('resource_admission_wait', card_fingerprint,
                         time.time() + RETRY_BASE_SECONDS, ticket_id, 'pending'))
                outcomes.append({'ticketId': ticket_id, 'state': 'pending',
                                 'reason': 'resource_admission_wait'})
                continue
            if replayed >= MAX_REPLAYS_PER_WAKE:
                more = True
                continue
            replayed += 1
            replay_attempted = True
            # Replay ran the QMD recall hook, which is gone. The capture stays
            # pending until it expires or a case commits.
            error = 'recall_unavailable'
            state = 'pending'
            if state == 'pending' and error is None:
                error = 'candidate_capture_pending'
        with _db(root) as db:
            committed = _committed_case(db, project, ticket_id)
            if committed is not None:
                state, error = 'completed', None
            elif request_id is not None:
                bound = db.execute('SELECT use_state FROM cases WHERE '
                    'project_root=? AND request_id=?',
                    (str(project), request_id)).fetchone()
                if ((bound is not None and bound[0] != 'active') or
                        expiry is None or time.time() >= expiry):
                    state, error = 'superseded', 'bound_case_not_activated'
            next_retry = (time.time() + min(RETRY_MAX_SECONDS,
                          RETRY_BASE_SECONDS * (2 ** min(attempts, 4)))
                          if (replay_attempted or request_id is not None)
                          and committed is None and state == 'pending' else None)
            db.execute('UPDATE deferred_edit_captures SET state=?,attempts=attempts+1,'
                       'last_error=?,completed_at=?,case_request_id=?,index_fingerprint=?, '
                       'attempted_card_fingerprint=?,next_retry_at=? '
                       'WHERE ticket_id=? AND state=?',
                       (state, error, time.time() if state == 'completed' else None,
                        committed[0] if committed else None,
                        committed[1] if committed else None, card_fingerprint,
                        next_retry,
                        ticket_id, 'pending'))
        outcomes.append({'ticketId': ticket_id, 'state': state, 'reason': error})
    return (outcomes, more) if return_more else outcomes

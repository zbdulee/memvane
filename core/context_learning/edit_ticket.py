"""One-use, owner-private provenance for a completed synthetic host edit.

The PostToolUse path reserves one ticket per complete tokenizer window. Shared
enqueue binds it to the exact candidate pool in the same SQLite transaction as
the case insert. A ticket alone never authorizes external transport.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import time
import uuid

from .contracts import digest
from .safe_source import snapshot_beneath
from .store import canonical
from .synthetic_experiment import story_for
from .synthetic_scope import generated_path

TTL = 600
SOURCE_REGISTRATION_TTL = 24 * 60 * 60


def _candidate_hash(sample):
    return digest(canonical([[row['candidate_id'], row['revision_sha256']]
                             for row in sample['candidates']]))


def _approval(project, story):
    from edit_context import _approved
    value = _approved(project, story)
    return value['parentApprovalSha256'], hashlib.sha256(
        Path.home().joinpath('.config/laya/real-edit-approval/current.json').read_bytes()).hexdigest()


def _source_active(story, path, after_sha):
    try:
        current = snapshot_beneath(story, path)
    except FileNotFoundError:
        current = None
    return ((hashlib.sha256(current).hexdigest() if current is not None else None)
            == after_sha)


def _prune(db):
    db.execute('DELETE FROM edit_tickets WHERE request_id IS NULL AND expires_at<=?',
               (time.time(),))


def reserve(project, change, event, metadata, segment):
    """Reserve only a segment just derived from a verified before/after hook."""
    from .shared import _db, _consent
    from .synthetic_experiment import shared_for
    project = Path(project).resolve(strict=True)
    story = story_for(project).resolve(strict=True)
    root = shared_for(project)
    if not _consent(root, project):
        raise ValueError('edit_shared_consent_missing')
    parent_approval, approval_sha = _approval(project, story)
    path = change['path']
    source_registration = metadata.get('source_registration') is True
    deferred_capture = metadata.get('deferred_capture') is True
    if source_registration and deferred_capture:
        raise ValueError('edit_ticket_mode_conflict')
    pattern = (r'(?:01_Settings|02_Characters|03_Plot|04_Manuscript)/[A-Za-z0-9_./-]+\.md'
               if not source_registration else
               r'(?:01_Settings|02_Characters|03_Plot|\.auto-context/wiki)/[A-Za-z0-9_./-]+\.md')
    if (not isinstance(path, str) or not re.fullmatch(pattern, path)
            or any(part in ('.', '..') for part in Path(path).parts)):
        raise ValueError('edit_ticket_path_invalid')
    if (change['source_side'] not in ('before', 'after')
            or not _source_active(story, path, change['after_sha256'])
            or metadata['parent_sha256'] != change['document_sha256']
            or metadata['path_sha256'] != digest(path)
            or metadata['operation'] != change['operation']
            or any(metadata[key] != event['edit_' + key]
                   for key in ('session_id', 'turn_id', 'tool_use_id'))):
        raise ValueError('edit_ticket_source_changed')
    left = metadata['start'] - change['source_start']
    right = metadata['end'] - change['source_start']
    if (not isinstance(segment, str) or not segment.strip()
            or not 0 <= left < right <= len(change['text'])
            or change['text'][left:right] != segment
            or digest(segment) != metadata['segment_sha256']):
        raise ValueError('edit_ticket_segment_mismatch')
    ticket_id = uuid.uuid4().hex
    episode_match = re.search(r'(?:^|/)ep(\d{3})\.md$', path)
    episode = int(episode_match.group(1)) if episode_match else 0
    with _db(root) as db:
        _prune(db)
        if db.execute('SELECT COUNT(*) FROM edit_tickets WHERE request_id IS NULL').fetchone()[0] >= 128:
            raise ValueError('edit_ticket_capacity')
        db.execute('INSERT INTO edit_tickets (ticket_id,project_root,story_root,relative_path,episode,approval_sha256,parent_approval_sha256,before_sha256,after_sha256,document_sha256,source_side,source_start,segment_start,segment_end,segment_sha256,event_json,session_id,turn_id,tool_use_id,operation,created_at,expires_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   (ticket_id, str(project), str(story), path, episode,
                    approval_sha, parent_approval, change['before_sha256'],
                    change['after_sha256'], change['document_sha256'],
                    change['source_side'], change['source_start'],
                    metadata['start'], metadata['end'], metadata['segment_sha256'],
                    canonical(metadata), metadata['session_id'], metadata['turn_id'],
                    metadata['tool_use_id'], metadata['operation'],
                    time.time(), time.time() + (SOURCE_REGISTRATION_TTL
                        if source_registration or deferred_capture else TTL)))
    return ticket_id


def bind(db, root, project, sample, bodies, input_sha, *, commit):
    """Verify source and candidate revisions, then claim ticket exactly once."""
    ticket_id = sample['edit_event']['ticket_id']
    row = db.execute('SELECT project_root,story_root,relative_path,episode,approval_sha256,parent_approval_sha256,before_sha256,after_sha256,document_sha256,source_side,source_start,segment_start,segment_end,segment_sha256,event_json,session_id,turn_id,tool_use_id,operation,expires_at,request_id FROM edit_tickets WHERE ticket_id=?',
                     (ticket_id,)).fetchone()
    if row is None or row[-1] is not None or time.time() >= row[-2]:
        raise ValueError('edit_ticket_missing_used_or_expired')
    (project_root, story_root, path, episode, approval_sha, parent_approval,
     before_sha, after_sha, document_sha, side, source_start, start, end,
     segment_sha, event_json, session, turn, tool, operation, _, _) = row
    story = story_for(project).resolve(strict=True)
    current_parent, current_approval = _approval(project, story)
    event = sample['edit_event']
    if (project_root != str(Path(project).resolve()) or story_root != str(story)
            or current_approval != approval_sha or current_parent != parent_approval
            or not _source_active(story, path, after_sha)
            or segment_sha != sample['prompt']['sha256']
            or json.loads(event_json).get('source_registration') is True
            or event != {**json.loads(event_json), 'ticket_id': ticket_id}):
        raise ValueError('edit_ticket_provenance_mismatch')
    if event.get('deferred_capture') is True:
        from .deferred_edit import _table, _active_paths
        _table(db)
        deferred = db.execute('SELECT payload,host,relative_path,after_sha256,'
            'before_content,after_content,state FROM deferred_edit_captures '
            'WHERE ticket_id=?', (ticket_id,)).fetchone()
        if deferred is None or deferred[-1] != 'pending':
            raise ValueError('deferred_edit_observation_missing')
        original = json.loads(deferred[0])
        if (original.get('prompt') != sample['prompt']['text'] or
                original.get('edit_event') != event or
                deferred[1] != sample['host'] or deferred[2] != path or
                deferred[3] != after_sha or
                (hashlib.sha256(deferred[4].encode()).hexdigest()
                 if deferred[4] is not None else None) != before_sha or
                hashlib.sha256(deferred[5].encode()).hexdigest() != after_sha):
            raise ValueError('deferred_edit_observation_changed')
        allowed = _active_paths(db, project, story, path, after_sha)
        if not sample['candidates'] or any(
                str(generated_path(candidate['candidate_id'])) not in allowed
                for candidate in sample['candidates']):
            raise ValueError('deferred_edit_candidate_not_registered')
    from .compact_input import derive
    for candidate in sample['candidates']:
        cid, revision = candidate['candidate_id'], candidate['revision_sha256']
        relative = generated_path(cid)
        generated = db.execute('SELECT sha256 FROM experiment_generated WHERE relative_path=? ORDER BY entry_id DESC LIMIT 1',
                               (str(relative),)).fetchone()
        if generated != (revision,):
            from .source_card_provenance import registered_revision
            if not registered_revision(db, root, project, cid, revision):
                raise ValueError('synthetic_source_not_generated_or_host_registered')
        source = snapshot_beneath(story, relative)
        if hashlib.sha256(source).hexdigest() != revision or bodies.get(cid) != derive(
                source.decode('utf8'), revision)['body_text']:
            raise ValueError('synthetic_capture_not_from_source')
    if commit:
        changed = db.execute('UPDATE edit_tickets SET request_id=?,input_sha256=?,candidate_sha256=? WHERE ticket_id=? AND request_id IS NULL',
                             (sample['request_id'], input_sha, _candidate_hash(sample),
                              ticket_id)).rowcount
        if changed != 1:
            raise ValueError('edit_ticket_reused')
    return episode, 0, 'edit:' + path, document_sha


def validate_case(db, root, project, sample, bodies):
    """Recheck the immutable claim before gold and every later reuse."""
    row = db.execute('SELECT project_root,story_root,relative_path,approval_sha256,parent_approval_sha256,after_sha256,document_sha256,segment_start,segment_end,segment_sha256,event_json,request_id,input_sha256,candidate_sha256 FROM edit_tickets WHERE ticket_id=?',
                     (sample['edit_event']['ticket_id'],)).fetchone()
    if row is None:
        raise ValueError('edit_ticket_not_bound')
    (project_root, story_root, path, approval_sha, parent_approval, after_sha,
     document_sha, start, end, segment_sha, event_json, request_id, input_sha,
     candidate_sha) = row
    story = story_for(project).resolve(strict=True)
    current_parent, current_approval = _approval(project, story)
    if (project_root != str(Path(project).resolve()) or story_root != str(story)
            or current_approval != approval_sha or current_parent != parent_approval
            or not _source_active(story, path, after_sha)
            or request_id != sample['request_id'] or input_sha != digest(canonical(sample))
            or candidate_sha != _candidate_hash(sample)
            or sample['edit_event'] != {**json.loads(event_json),
                                        'ticket_id': sample['edit_event']['ticket_id']}
            or sample['edit_event']['parent_sha256'] != document_sha
            or sample['edit_event']['segment_sha256'] != segment_sha
            or (sample['edit_event']['start'], sample['edit_event']['end']) != (start, end)):
        raise ValueError('edit_ticket_not_bound')
    if sample['edit_event'].get('deferred_capture') is True:
        from .deferred_edit import validate_case_observation
        before = db.execute('SELECT before_sha256 FROM edit_tickets '
                            'WHERE ticket_id=?',
                            (sample['edit_event']['ticket_id'],)).fetchone()
        if before is None:
            raise ValueError('deferred_edit_observation_changed')
        validate_case_observation(db, Path(project).resolve(strict=True), story,
            sample['edit_event']['ticket_id'], sample, path, before[0],
            after_sha, input_sha)
    return row

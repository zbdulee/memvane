"""Temporary synthetic ticket/candidate replay; no provider or story writes."""
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, 'core')
from context_learning import (deferred_edit, edit_ticket, shared,
                              source_card_provenance, synthetic_experiment)
from context_learning.compact_input import derive
from context_learning.contracts import request, validate_request
from context_learning.synthetic_scope import WIKI_COLLECTION
from context_learning.contracts import digest
from context_learning.store import canonical
import topical_hook_queue


def sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


with tempfile.TemporaryDirectory(prefix='qmd-deferred-edit-') as temporary:
    base = Path(temporary).resolve()
    project, story, state = base / 'project', base / 'project/story', base / 'shared'
    story.mkdir(parents=True)
    state.mkdir(mode=0o700)
    source_path = '01_Settings/qmd-probes/ep999.md'
    source = story / source_path
    source.parent.mkdir(parents=True)
    before, after = 'Old synthetic rule.\n', 'New synthetic rule.\n'
    source.write_text(after)
    change = {'path': source_path, 'operation': 'edit', 'text': after,
        'source_start': 0, 'before_content': before, 'after_content': after,
        'before_sha256': sha(before), 'after_sha256': sha(after),
        'document_sha256': sha(after), 'source_sha256': sha(after),
        'source_side': 'after'}
    event = {'changes': [change], 'edit_session_id': 'session',
             'edit_turn_id': 'turn', 'edit_tool_use_id': 'tool'}
    metadata = {'parent_sha256': sha(after), 'path_sha256': digest(source_path),
        'change_index': 0, 'change_count': 1, 'segment_sha256': sha(after),
        'start': 0, 'end': len(after), 'index': 0, 'count': 1,
        'session_id': 'session', 'turn_id': 'turn', 'tool_use_id': 'tool',
        'operation': 'edit', 'deferred_capture': True}
    with patch.object(synthetic_experiment, 'shared_for', return_value=state), \
         patch.object(edit_ticket, 'story_for', return_value=story), \
         patch.object(edit_ticket, '_approval', return_value=('parent', 'approval')), \
         patch.object(shared, '_consent', return_value=True), \
         patch.object(deferred_edit, 'NOVEL_ROOT', project), \
         patch.object(deferred_edit, 'story_for', return_value=story), \
         patch.object(deferred_edit, 'shared_for', return_value=state), \
         patch.object(deferred_edit, '_ready', return_value=True):
        ticket_id = edit_ticket.reserve(project, change, event, metadata, after)
        payload = {'prompt': after, 'cwd': str(story),
                   'context_origin': 'tool_edit',
                   'edit_event': {**metadata, 'ticket_id': ticket_id}}
        assert deferred_edit.reserve(project, payload, 'codex', change) == 'reserved'
        assert deferred_edit.reserve(project, payload, 'codex', change) == 'already_reserved'
        assert deferred_edit.pending(project, ticket_id)
        with shared._db(state) as db:
            row = db.execute('SELECT before_content,after_content FROM '
                'deferred_edit_captures WHERE ticket_id=?', (ticket_id,)).fetchone()
            assert row == (before, after)
            expiry = db.execute('SELECT expires_at-created_at FROM edit_tickets '
                'WHERE ticket_id=?', (ticket_id,)).fetchone()[0]
            assert expiry >= edit_ticket.SOURCE_REGISTRATION_TTL - 1
        assert deferred_edit.allowed_paths(project, ticket_id) == set()
        card_path = '.auto-context/wiki/topical-v2/' + 'a' * 24 + '/new-rule.md'
        card = story / card_path
        card.parent.mkdir(parents=True)
        card.write_text('---\ntitle: Synthetic\n---\nVerified synthetic card.\n')
        candidate_id = WIKI_COLLECTION + '/topical-v2/' + 'a' * 24 + '/new-rule.md'
        sampling = {'mode': 'eligible-policy-pool', 'scope': 'queried-phases-only',
                    'phase_counts': {'primary': 1}, 'per_phase_limit': 30,
                    'eligible_ids': [candidate_id], 'captured_ids': [candidate_id],
                    'baseline_selected_ids': [candidate_id], 'excluded': [],
                    'snapshot_failures': [], 'retrieval_truncated': False,
                    'assessment_complete': True,
                    'complete_within_returned_bound': True,
                    'index_revision': 'c' * 64, 'candidate_limit': 30}
        sample = request(payload, [{'candidate_id': candidate_id,
            'revision_sha256': sha(card.read_text()), 'eligible': True,
            'excerpt': 'Verified synthetic card.'}], host='codex',
            request_id='new-case', sampling=sampling)
        validate_request(sample)
        input_sha = sha(canonical(sample))
        bodies = {candidate_id: derive(card.read_text(), sha(card.read_text()))['body_text']}
        with patch.object(source_card_provenance, 'registered_revision',
                          return_value=True):
            with shared._db(state) as db:
                try:
                    edit_ticket.bind(db, state, project, sample, bodies,
                                     input_sha, commit=False)
                except ValueError as exc:
                    assert str(exc) == 'deferred_edit_candidate_not_registered'
                else:
                    raise AssertionError('unregistered card bound to edit ticket')
        revision = {'path': source_path, 'sha256': sha(after)}
        with shared._db(state) as db:
            db.execute('INSERT INTO topical_worker_versions VALUES '
                '(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                ('registration', str(project), str(story), card_path,
                 sha(card.read_text()), card.read_bytes(), 'a' * 24, 'new-rule',
                 json.dumps([{'revision': revision}]), 'evidence', 'attestation',
                 'publication', 'scope', 'receipts', '[]', 'active', 1.0))
        assert deferred_edit.allowed_paths(project, ticket_id) == {card_path}
        assert deferred_edit.candidate_allowed(candidate_id, {card_path})
        assert deferred_edit.candidate_allowed('wiki/topical-v2/' + 'a' * 24 +
            '/new-rule.md', {card_path}) is False
        with patch.object(deferred_edit, '_ready', return_value=False), \
             patch.object(deferred_edit.subprocess, 'run') as blocked_run:
            assert deferred_edit.resume_ready(project) == [{
                'ticketId': ticket_id, 'state': 'pending',
                'reason': 'resource_admission_wait'}]
            blocked_run.assert_not_called()
        assert deferred_edit.next_retry_due(project) > time.time()
        with shared._db(state) as db:
            db.execute('UPDATE deferred_edit_captures SET next_retry_at=0 '
                       'WHERE ticket_id=?', (ticket_id,))
        calls = []
        def replay(*args, **kwargs):
            calls.append(json.loads(kwargs['input']))
            assert kwargs['env']['QMD_DEFERRED_CAPTURE_REPLAY'] == '1'
            assert kwargs['env']['QMD_DEFERRED_CAPTURE_TICKET'] == ticket_id
            assert kwargs['env']['QMD_HOOK_TOTAL_SECONDS'] == '55'
            assert 'QMD_HOOK_DEADLINE_MONO' not in kwargs['env']
            assert kwargs['timeout'] == 60
            with patch.object(source_card_provenance, 'registered_revision',
                              return_value=True):
                with shared._db(state) as db:
                    edit_ticket.bind(db, state, project, sample, bodies,
                                     input_sha, commit=True)
                    db.execute('INSERT INTO cases VALUES '
                        '(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                        (shared._project_key(project), str(project), 'new-case', input_sha,
                         json.dumps(sample), json.dumps(bodies), 'index-after',
                         time.time(), 'train', '[]', 'active', 'awaiting_local',
                         None, None, None, None, None))
            return SimpleNamespace(returncode=0)
        with patch.dict(os.environ, {'QMD_HOOK_DEADLINE_MONO': '123'}), \
             patch.object(deferred_edit.subprocess, 'run', side_effect=replay):
            result = deferred_edit.resume_ready(project)
        assert result == [{'ticketId': ticket_id, 'state': 'pending',
                           'reason': 'candidate_capture_pending'}]
        assert calls == [payload]
        with shared._db(state) as db:
            assert db.execute('SELECT state FROM deferred_edit_captures WHERE '
                'ticket_id=?', (ticket_id,)).fetchone() == ('pending',)
            assert db.execute('SELECT status FROM cases WHERE request_id=?',
                ('new-case',)).fetchone() == ('awaiting_local',)
            db.execute('UPDATE deferred_edit_captures SET before_content=? '
                       'WHERE ticket_id=?', ('altered', ticket_id))
        with patch('config.find_project_config', return_value={
                'projectRoot': str(project), 'config': {}}), \
             patch.object(shared, '_safe_root', return_value=state), \
             patch.object(shared, '_case_active', return_value=True):
            assert deferred_edit.resume_ready(project) == [{
                'ticketId': ticket_id, 'state': 'pending',
                'reason': 'case_not_committed'}]
        with shared._db(state) as db:
            assert db.execute('SELECT state FROM deferred_edit_captures WHERE '
                'ticket_id=?', (ticket_id,)).fetchone() == ('pending',)
            assert db.execute('SELECT status FROM cases WHERE request_id=?',
                ('new-case',)).fetchone() == ('awaiting_local',)
            db.execute('UPDATE deferred_edit_captures SET before_content=? '
                       'WHERE ticket_id=?', (before, ticket_id))
        with patch('config.find_project_config', return_value={
                'projectRoot': str(project), 'config': {}}), \
             patch.object(shared, '_safe_root', return_value=state), \
             patch.object(shared, '_case_active', return_value=True):
            assert deferred_edit.resume_ready(project) == [{
                'ticketId': ticket_id, 'state': 'completed', 'reason': None}]
        with patch.object(deferred_edit.subprocess, 'run', side_effect=AssertionError(
                'one-use ticket must never replay again')):
            assert deferred_edit.resume_ready(project) == []
        assert not deferred_edit.pending(project, ticket_id)
        assert deferred_edit.resume_ready(project) == []
        with shared._db(state) as db:
            receipt = db.execute('SELECT case_request_id,index_fingerprint,'
                'completed_at FROM deferred_edit_captures WHERE ticket_id=?',
                (ticket_id,)).fetchone()
            assert receipt[0:2] == ('new-case', 'index-after') and receipt[2] > 0
            assert db.execute('SELECT status FROM cases WHERE request_id=?',
                ('new-case',)).fetchone() == ('queued',)
            edit_ticket.validate_case(db, state, project, sample, bodies)
            db.execute('UPDATE deferred_edit_captures SET before_content=? '
                       'WHERE ticket_id=?', ('altered', ticket_id))
            try:
                edit_ticket.validate_case(db, state, project, sample, bodies)
            except ValueError as exc:
                assert str(exc) == 'deferred_edit_observation_changed'
            else:
                raise AssertionError('changed full preimage reached gold/training')
        # Pruning an expired ticket must leave an auditable terminal outcome,
        # not an unserviceable pending observation consuming capacity forever.
        orphans = []
        for _ in range(5):
            orphan = edit_ticket.reserve(project, change, event, metadata, after)
            orphan_payload = {**payload,
                'edit_event': {**metadata, 'ticket_id': orphan}}
            assert deferred_edit.reserve(project, orphan_payload,
                                         'codex', change) == 'reserved'
            orphans.append(orphan)
        with patch.object(deferred_edit.subprocess, 'run', return_value=SimpleNamespace(
                returncode=0)) as no_capture:
            first, more = deferred_edit.resume_ready(project, return_more=True)
            second, more_after = deferred_edit.resume_ready(project,
                                                            return_more=True)
            quiet, no_more = deferred_edit.resume_ready(project, return_more=True)
        assert len(first) == 4 and more
        assert len(second) == 1 and not more_after
        assert quiet == [] and not no_more and no_capture.call_count == 5
        with shared._db(state) as db:
            retry = db.execute('SELECT attempted_card_fingerprint,next_retry_at '
                'FROM deferred_edit_captures WHERE ticket_id=?',
                (orphans[0],)).fetchone()
            assert retry[0] and retry[1] > time.time()
            db.execute('UPDATE deferred_edit_captures SET next_retry_at=0 '
                       'WHERE ticket_id=?', (orphans[0],))
        with patch.object(deferred_edit.subprocess, 'run', return_value=
                SimpleNamespace(returncode=0,
                    stderr='qmd_deferred_capture_deadline_exceeded\n')) as deadline:
            deadline_result = deferred_edit.resume_ready(project)
        assert deadline_result == [{'ticketId': orphans[0], 'state': 'pending',
                                    'reason': 'deferred_recall_deadline_exceeded'}]
        deadline.assert_called_once()
        with shared._db(state) as db:
            db.execute('UPDATE deferred_edit_captures SET next_retry_at=0 '
                       'WHERE ticket_id=?', (orphans[0],))
        with patch.object(deferred_edit.subprocess, 'run', return_value=
                SimpleNamespace(returncode=1)) as transient:
            retry_result = deferred_edit.resume_ready(project)
        assert retry_result == [{'ticketId': orphans[0], 'state': 'pending',
                                 'reason': 'recall_failed'}]
        transient.assert_called_once()
        with shared._db(state) as db:
            db.execute("UPDATE topical_worker_versions SET state='retired' "
                       "WHERE registration_id='registration'")
        assert deferred_edit.resume_ready(project) == []
        assert deferred_edit.next_retry_due(project) is None
        with shared._db(state) as db:
            db.executemany('DELETE FROM edit_tickets WHERE ticket_id=?',
                           [(ticket,) for ticket in orphans])
        cleaned = deferred_edit.resume_ready(project, allow_replay=False)
        assert {item['ticketId'] for item in cleaned} == set(orphans)
        assert {item['state'] for item in cleaned} == {'superseded'}
        assert all(not deferred_edit.pending(project, ticket)
                   for ticket in orphans)

    # One worker pass yields after the bounded local replay batch and leaves a
    # durable successor job; it cannot hold the Stop lock for all 128 tickets.
    queue = story / '.topical-hook-jobs'
    queue.mkdir(mode=0o700)
    first_job = queue / ('b' * 32 + '.json')
    first_job.write_text(json.dumps({'schema': 'topical-hook-job-v1',
        'action': 'deferred', 'turnKey': 'recovery'}))
    with patch.object(topical_hook_queue, '_root', return_value=story), \
         patch('setup_guard.status', return_value={'status': 'ready'}), \
         patch.object(topical_hook_queue, '_run', return_value={
             'status': 'deferred_capture_pass', 'deferredMore': True}), \
         patch.object(topical_hook_queue, '_spawn_worker') as spawned:
        topical_hook_queue.worker(story)
    successors = list(queue.glob('*.json'))
    assert len(successors) == 1 and successors[0] != first_job
    assert json.loads(successors[0].read_text())['action'] == 'deferred'
    spawned.assert_called_once_with(story)

    alarm_root = base / 'alarm-story'
    alarm_root.mkdir()
    (alarm_root / '.topical-hook-jobs').mkdir()
    with patch.object(topical_hook_queue, '_root', return_value=alarm_root), \
         patch('topical_story_scope.exact_story', return_value=True), \
         patch.object(deferred_edit, 'next_retry_due', side_effect=[
             time.time() - 1, time.time() - 1, None]), \
         patch.object(topical_hook_queue, '_auto_policy', return_value=(
             {'enabled': True}, None)), \
         patch('setup_guard.status', return_value={'status': 'ready'}), \
         patch.object(topical_hook_queue, 'worker') as alarm_worker, \
         patch.object(topical_hook_queue.time, 'sleep') as alarm_sleep:
        topical_hook_queue.deferred_alarm(alarm_root)
    assert alarm_worker.call_count == 2
    assert all(call.args == (alarm_root,) for call in alarm_worker.call_args_list)
    assert alarm_sleep.call_count == 2
    assert all(call.args == (30.0,) for call in alarm_sleep.call_args_list)
    assert len(list((alarm_root / '.topical-hook-jobs').glob('*.json'))) == 1

print(json.dumps({'immutablePrePost': True, 'ticketExtended': True,
                  'waitedForRegistration': True, 'oneReplay': True,
                  'orphanTerminates': True, 'lateValidation': True,
                  'awaitingLocalHeld': True, 'providerCalls': 0}))

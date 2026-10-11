"""No external calls: real edit receipt to registered source-card proof."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'core'))

import edit_context
import qmd_route
import wiki_compile
from context_learning import edit_ticket, source_card_provenance as provenance
from context_learning.shared import _db
from context_learning.synthetic_experiment import PLANNED_BUNDLE_NAME, SHARED_NAME
from context_learning.store import canonical


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def rejected(fn, reason=None):
    try:
        fn()
    except (ValueError, OSError) as exc:
        if reason is not None:
            assert reason in str(exc), (reason, str(exc))
        return
    raise AssertionError('unsafe source card accepted')


with tempfile.TemporaryDirectory(prefix='qmd-source-card-', dir='/tmp') as folder:
    project = Path(folder).resolve() / 'novel'; project.mkdir(mode=0o700)
    story = project / PLANNED_BUNDLE_NAME; story.mkdir(mode=0o700)
    shared = project / SHARED_NAME; shared.mkdir(mode=0o700)
    preimages = shared / 'edit-preimages'; preimages.mkdir(mode=0o700)
    source_relative = '01_Settings/qmd-prospective/world.md'
    card_relative = '.auto-context/wiki/qmd-prospective/world.md'
    source = story / source_relative
    card = story / card_relative
    source.parent.mkdir(parents=True, exist_ok=True)
    card.parent.mkdir(parents=True, exist_ok=True)
    index_path = Path(folder) / 'index.sqlite'
    with sqlite3.connect(index_path) as index:
        index.execute('CREATE TABLE documents(collection TEXT,path TEXT,hash TEXT,active INTEGER)')

    def host_edit(relative, text, tool):
        payload = {'cwd': str(story), 'session_id': 'actual-host-session',
                   'turn_id': 'actual-turn', 'tool_use_id': tool}
        target = story / relative
        with patch.object(edit_context, '_scope', return_value=(project, story, [relative])), patch.object(
                edit_context, '_receipt', side_effect=lambda _r, key: preimages / (key + '.json')), patch.object(
                edit_context, '_schedule_prune', return_value=None):
            edit_context.before({**payload, 'hook_event_name': 'PreToolUse'})
            target.write_text(text, encoding='utf8')
            event = edit_context.after({**payload, 'hook_event_name': 'PostToolUse'})
        assert event and event['changes'][0]['after_sha256'] == sha(text.encode())
        with patch('context_learning.shared._consent', return_value=True), patch.object(
                edit_ticket, '_approval', return_value=('b' * 64, 'a' * 64)):
            tickets = provenance.capture_host_edits(project, event)
        assert len(tickets) == 1
        return tickets[0]

    source_text = '# Lantern rule\nThe copper lantern burns for two measured hours.\n'
    source_ticket = host_edit(source_relative, source_text, 'raw-write')
    rejected(lambda: provenance.capture_host_edits(project, {
        'changes': [], 'edit_session_id': 'actual-host-session',
        'edit_turn_id': 'actual-turn', 'edit_tool_use_id': 'forged-tool'}),
        'completed_receipt_missing')
    shell_relative = '01_Settings/qmd-prospective/shell-authored.md'
    shell_source = story / shell_relative
    shell_source.write_text('A shell wrote this document.\n', encoding='utf8')
    host_edit(shell_relative, 'A shell wrote that document.\n', 'tiny-edit')
    with _db(shared) as db:
        rejected(lambda: provenance._ticket(
            db, project, story, shell_relative, shell_source.read_bytes(), None,
            'a' * 64, 'b' * 64), 'host_ticket_missing_or_ambiguous')
    source_raw = source.read_bytes()
    ref = {'kind': 'file', 'path': story.name + '/' + source_relative,
           'collection': 'fantasy-20261008-settings',
           'sha256': sha(source_raw), 'size': len(source_raw),
           'mtimeNs': source.stat().st_mtime_ns}
    candidate = {'title': 'Lantern rule', 'summary': 'The lantern burns for two measured hours.',
                 'sources': [{'kind': 'file', 'path': ref['path'],
                              'collection': ref['collection']}],
                 'sourceRevisions': [ref], 'suggestedType': 'world-rule'}
    card_text = wiki_compile.markdown_page(candidate, candidate['summary'],
                                           'generated', [], 'a' * 16)
    other_source = {**candidate, 'sources': [{'kind': 'file',
        'path': story.name + '/02_Characters/qmd-prospective/foreign.md',
        'collection': 'fantasy-20261008-characters'}]}
    conflicting = wiki_compile.markdown_page(other_source, candidate['summary'],
                                              'generated', [], 'a' * 16)
    rejected(lambda: provenance._card_source_ref(
        story, source_relative, ref['collection'], source_raw, conflicting.encode()),
        'provenance_mismatch')
    card_ticket = host_edit(card_relative, card_text, 'card-write')
    with sqlite3.connect(index_path) as index:
        index.executemany('INSERT INTO documents VALUES (?,?,?,1)', [
            ('fantasy-20261008-settings', 'qmd-prospective/world.md', sha(source_raw)),
            ('fantasy-20261008-wiki', 'qmd-prospective/world.md', sha(card.read_bytes()))])

    route = {'selected': True, 'INDEX_PATH': str(index_path)}
    scope = (shared, project, story)
    with patch.object(provenance, '_story_scope', return_value=scope), patch.object(
            provenance, 'NOVEL_ROOT', project), patch.object(
            edit_ticket, '_approval', return_value=('b' * 64, 'a' * 64)), patch.object(
            qmd_route, 'project_paths', return_value=route):
        rejected(lambda: provenance.register_card(shared, project, source_relative,
                 card_relative, method='episode_generator'), 'generation_method_unsupported')
        rejected(lambda: provenance.register_card(shared, project,
                 '04_Manuscript/ep028.md', card_relative), 'collection_invalid')
        rejected(lambda: provenance.register_card(shared, project,
                 '../other-novel/private.md', card_relative), 'path_invalid')
        with sqlite3.connect(index_path) as index:
            index.execute('UPDATE documents SET hash=? WHERE collection=?',
                          ('0' * 64, 'fantasy-20261008-wiki'))
        rejected(lambda: provenance.register_card(shared, project, source_relative,
                 card_relative), 'index_unacknowledged')
        with sqlite3.connect(index_path) as index:
            index.execute('UPDATE documents SET hash=? WHERE collection=?',
                          (sha(card.read_bytes()), 'fantasy-20261008-wiki'))
            index.execute('INSERT INTO documents VALUES (?,?,?,1)',
                          ('fantasy-20261008-wiki', 'qmd-prospective/world.md',
                           sha(card.read_bytes())))
        rejected(lambda: provenance.register_card(shared, project, source_relative,
                 card_relative), 'index_unacknowledged')
        with sqlite3.connect(index_path) as index:
            index.execute('DELETE FROM documents WHERE rowid=(SELECT MAX(rowid) '
                          'FROM documents)')
        with _db(shared) as db:
            db.execute('INSERT INTO experiment_generated '
                       '(kind,episode,slot,relative_path,sha256,recorded_at) '
                       'VALUES (?,?,?,?,?,?)',
                       ('raw', 0, 0, source_relative, sha(source_raw), time.time()))
        rejected(lambda: provenance.register_card(shared, project, source_relative,
                 card_relative), 'duplicate_or_generated')
        with _db(shared) as db:
            db.execute('DELETE FROM experiment_generated WHERE relative_path=?',
                       (source_relative,))
        with _db(shared) as db:
            original = db.execute('SELECT event_json FROM edit_tickets WHERE ticket_id=?',
                                  (source_ticket,)).fetchone()[0]
            db.execute('UPDATE edit_tickets SET event_json=? WHERE ticket_id=?',
                       (canonical({'source_registration': True, 'session_id': 'forged'}),
                        source_ticket))
        rejected(lambda: provenance.register_card(shared, project, source_relative,
                 card_relative), 'host_ticket_missing_or_ambiguous')
        with _db(shared) as db:
            db.execute('UPDATE edit_tickets SET event_json=? WHERE ticket_id=?',
                       (original, source_ticket))
        forged = card_text.replace(ref['sha256'], 'f' * 64)
        card.write_text(forged)
        rejected(lambda: provenance.register_card(shared, project, source_relative,
                 card_relative), 'provenance_mismatch')
        card.write_text(card_text)
        with sqlite3.connect(index_path) as index:
            index.execute('UPDATE documents SET hash=? WHERE collection=?',
                          ('0' * 64, 'fantasy-20261008-wiki'))
        # A later valid card must not sit behind the first 128 stale tickets.
        with _db(shared) as db:
            columns = [row[1] for row in db.execute('PRAGMA table_info(edit_tickets)')]
            original_row = dict(zip(columns, db.execute(
                'SELECT * FROM edit_tickets WHERE ticket_id=?',
                (card_ticket,)).fetchone()))
            for ordinal in range(129):
                stale = {**original_row,
                         'ticket_id': 'stale-card-' + str(ordinal),
                         'relative_path': ('.auto-context/wiki/qmd-prospective/'
                                           f'stale-{ordinal}.md'),
                         'created_at': original_row['created_at'] - 1000 + ordinal}
                db.execute('INSERT INTO edit_tickets (' + ','.join(columns) +
                           ') VALUES (' + ','.join('?' for _ in columns) + ')',
                           [stale[name] for name in columns])
        pending = provenance.reconcile_pending(shared, project)
        assert pending['registered'] == [] and pending['deferred']
        assert pending['skipped'] == 129, pending
        with sqlite3.connect(index_path) as index:
            index.execute('UPDATE documents SET hash=? WHERE collection=?',
                          (sha(card.read_bytes()), 'fantasy-20261008-wiki'))
        # Fresh process models a later SessionStart/index-worker retry.
        child_code = '''import json,sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, sys.argv[1])
import qmd_route
from context_learning import edit_ticket, source_card_provenance as provenance
from context_learning.shared import _reconcile_source_cards
shared,project,story,index = map(Path, sys.argv[2:])
with patch.object(provenance, '_story_scope', return_value=(shared,project,story)), patch.object(
        provenance, 'NOVEL_ROOT', project), patch.object(
        edit_ticket, '_approval', return_value=('b'*64,'a'*64)), patch.object(
        qmd_route, 'project_paths', return_value={'selected':True,'INDEX_PATH':str(index)}):
    print(json.dumps(_reconcile_source_cards(shared,project)))
'''
        child = subprocess.run([sys.executable, '-c', child_code,
            str(Path(__file__).resolve().parents[2] / 'core'), str(shared),
            str(project), str(story), str(index_path)], check=True,
            capture_output=True, text=True, timeout=10)
        admitted = json.loads(child.stdout)
        assert len(admitted['registered']) == 1, admitted
        assert admitted['skipped'] == 129, admitted
        last_telemetry = json.loads((shared / 'job-telemetry.jsonl').read_text().splitlines()[-1])
        assert (last_telemetry['status'], last_telemetry['registered_count'],
                last_telemetry['skipped_count']) == ('deferred', 1, 129)
        with _db(shared) as db:
            db.execute("DELETE FROM edit_tickets WHERE ticket_id LIKE 'stale-card-%'")
        assert provenance.reconcile_pending(shared, project)['registered'] == []
        with _db(shared) as db:
            registered = db.execute('SELECT registration_id,source_sha256,card_sha256,index_ack '
                                    'FROM source_card_versions').fetchone()
        result = {'registration_id': registered[0], 'source_sha256': registered[1],
                  'card_sha256': registered[2], 'index_ack': json.loads(registered[3]),
                  'generation_method': provenance.METHOD}
        assert result['generation_method'] == provenance.METHOD
        candidate_id = 'fantasy-20261008-wiki/qmd-prospective/world.md'
        with _db(shared) as db:
            assert provenance.registered_revision(db, shared, project,
                                                  candidate_id, result['card_sha256'])
            snapshots = db.execute('SELECT source_bytes,card_bytes,index_ack '
                                   'FROM source_card_versions').fetchone()
            assert snapshots[:2] == (source_raw, card_text.encode())
            assert len(json.loads(snapshots[2])) == 2
            assert db.execute('SELECT request_id FROM edit_tickets WHERE ticket_id=?',
                              (source_ticket,)).fetchone()[0].startswith('source-card:')
            frozen = db.execute('SELECT before_sha256 FROM edit_tickets WHERE ticket_id=?',
                                (source_ticket,)).fetchone()[0]
            db.execute('UPDATE edit_tickets SET before_sha256=? WHERE ticket_id=?',
                       ('f' * 64, source_ticket))
            assert not provenance.registered_revision(db, shared, project,
                                                      candidate_id, result['card_sha256'])
            db.execute('UPDATE edit_tickets SET before_sha256=? WHERE ticket_id=?',
                       (frozen, source_ticket))
            assert provenance.registered_revision(db, shared, project,
                                                  candidate_id, result['card_sha256'])
        rejected(lambda: provenance.register_card(shared, project, source_relative,
                 card_relative), 'host_ticket_missing_or_ambiguous')
        source.write_text(source_text + 'Changed after registration.\n')
        with _db(shared) as db:
            assert not provenance.registered_revision(db, shared, project,
                                                      candidate_id, result['card_sha256'])
        source.write_text(source_text)
        os.utime(source, ns=(ref['mtimeNs'], ref['mtimeNs']))
        with _db(shared) as db:
            assert provenance.registered_revision(db, shared, project,
                                                  candidate_id, result['card_sha256'])
            assert not provenance.registered_revision(db, shared, project / 'other',
                                                      candidate_id, result['card_sha256'])
        next_source_text = source_text.replace('two measured hours', 'three measured hours')
        host_edit(source_relative, next_source_text, 'raw-update')
        archive_relative = ('source-retired/' + sha(card_relative.encode())[:24]
                            + '-' + result['card_sha256'] + '.md')
        archive = shared / archive_relative
        archive.parent.mkdir(mode=0o700, exist_ok=True)
        os.replace(card, archive)
        with _db(shared) as db:
            db.execute('INSERT INTO synthetic_source_events '
                       '(event_id,relative_path,operation,before_sha256,after_sha256,'
                       'state,recorded_at,processed_at) VALUES (?,?,?,?,?,?,?,?)',
                       ('retire-source', source_relative, 'update', sha(source_raw),
                        sha(source.read_bytes()), 'needs_origin', time.time(), time.time()))
            db.execute('INSERT INTO synthetic_retired_cards '
                       '(relative_path,sha256,archive_relative,source_event_id,'
                       'source_refs,state,recorded_at) VALUES (?,?,?,?,?,?,?)',
                       (card_relative, result['card_sha256'], archive_relative,
                        'retire-source', canonical([ref]), 'queued', time.time()))
        next_ref = {**ref, 'sha256': sha(source.read_bytes()),
                    'size': len(source.read_bytes()), 'mtimeNs': source.stat().st_mtime_ns}
        next_candidate = {**candidate,
                          'summary': 'The lantern burns for three measured hours.',
                          'sourceRevisions': [next_ref]}
        next_card_text = wiki_compile.markdown_page(
            next_candidate, next_candidate['summary'], 'generated', [], 'a' * 16)
        host_edit(card_relative, next_card_text, 'card-recreate')
        with sqlite3.connect(index_path) as index:
            index.execute('UPDATE documents SET hash=? WHERE collection=?',
                          (next_ref['sha256'], 'fantasy-20261008-settings'))
            index.execute('UPDATE documents SET hash=? WHERE collection=?',
                          (sha(card.read_bytes()), 'fantasy-20261008-wiki'))
        archive_bytes = archive.read_bytes()
        archive.write_bytes(b'corrupted archive')
        rejected(lambda: provenance.register_card(shared, project, source_relative,
                 card_relative), 'host_ticket_missing_or_ambiguous')
        archive.write_bytes(archive_bytes)
        next_result = provenance.register_card(shared, project, source_relative,
                                               card_relative)
        with _db(shared) as db:
            assert provenance.registered_revision(db, shared, project,
                                                  candidate_id, next_result['card_sha256'])
            assert not provenance.registered_revision(db, shared, project,
                                                      candidate_id, result['card_sha256'])
        with sqlite3.connect(index_path) as index:
            index.execute('UPDATE documents SET hash=? WHERE collection=?',
                          ('0' * 64, 'fantasy-20261008-settings'))
        with _db(shared) as db:
            assert not provenance.registered_revision(db, shared, project,
                                                      candidate_id, next_result['card_sha256'])

    rejected(lambda: provenance.register_card(shared, project / 'other', source_relative,
             card_relative))
    print(json.dumps({'source_ticket': bool(source_ticket),
                      'card_ticket': bool(card_ticket),
                      'registration': 'verified', 'rejections': 14}, sort_keys=True))

"""A history snapshot preserves IDs, receipts, gold, and old originals."""
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile

from context_learning.history_migration import _table_digest, prepare, verify_prepared
from context_learning.contracts import digest
from context_learning.shared import _db
from context_learning.storage_layout import register_project
from context_learning.store import canonical, database


def rejects(action, reason):
    try:
        action()
    except ValueError as error:
        assert reason in str(error), str(error)
    else:
        raise AssertionError(f'expected {reason}')


with tempfile.TemporaryDirectory(prefix='qmd-history-migration-', dir='/tmp') as directory:
    home = Path(directory).resolve()
    project = home / 'novel'
    story = project / 'approved-story'
    story.mkdir(parents=True)
    shared = project / 'shared'
    shared.mkdir(mode=0o700)
    private_parent = project / 'private'
    private_parent.mkdir(mode=0o700)
    private = private_parent / digest(str(project))
    private.mkdir(mode=0o700)
    settings = project / '.auto-context/settings.json'
    settings.parent.mkdir()
    settings.write_text(json.dumps({'contextLearning': {'capture': True,
        'stateRoot': str(private_parent)}, 'sharedLearning': {'enabled': True,
        'root': str(shared)}}))
    project_id = register_project(project, story_root=story,
                                  display_name='Approved story', home=home)
    rid = 'real-edit-1'
    scripted = 'old-scripted-1'
    second = 'reserved-second-1'
    project_key = digest(str(project))
    samples_by_id = {}
    candidate = {'candidate_id': 'card-1', 'revision_sha256': 'e'*64}
    with _db(shared) as db:
        for request_id, origin, status, gold in [
                (rid, 'tool_edit', 'pending_review', None),
                (second, 'user_prompt', 'pending_review', None),
                (scripted, 'user_prompt', 'gold', json.dumps({'selected_ids': ['card-1']}))]:
            sample = {'context_origin': origin, 'candidates': [candidate]}
            if origin == 'tool_edit':
                sample['edit_event'] = {'ticket_id': 'ticket-1'}
                sample['prompt'] = {'sha256': '0'*64}
            samples_by_id[request_id] = sample
            body = canonical(sample)
            input_sha = hashlib.sha256(body.encode()).hexdigest()
            db.execute('INSERT INTO cases VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (project_key, str(project), request_id, input_sha,
                        body, json.dumps({'card-1': 'body'}),
                        'b'*64, 1.0, 'train', '[]', 'active', status,
                        json.dumps({'selected_ids': ['card-1']}) if request_id == second else None,
                        None, gold, None, 'cycle-1' if gold else None))
        db.execute('INSERT INTO experiment_calls VALUES (?,?,?,?)',
                   (f'gold:{rid}:first_running', 'gold', 1, 2.0))
        db.execute('INSERT INTO experiment_calls VALUES (?,?,?,?)',
                   (f'gold:{scripted}:first_running', 'gold', 1, 2.0))
        db.execute('INSERT INTO experiment_calls VALUES (?,?,?,?)',
                   (f'gold:{second}:second_running', 'gold', 1, 2.0))
        db.execute('INSERT INTO experiment_invocations VALUES (?,?,?,?,?)',
                   (f'gold:{rid}:first_running', 'c'*64, 'uncertain', None, None))
        db.execute('INSERT INTO cycles VALUES (?,?,?,?,?)',
                   ('cycle-1', 'accepted', '{}', 1.0, 2.0))
        db.execute('INSERT INTO edit_tickets VALUES ('+','.join('?' for _ in range(25))+')',
                   ('ticket-1', str(project), str(story), 'approved-story/file.md', 1,
                    'd'*64, 'e'*64, None, 'f'*64, 'f'*64, 'after', 0, 0, 10, '0'*64,
                    '{}', 'session', 'turn', 'tool', 'write', 1.0, 2.0, rid,
                    digest(canonical(samples_by_id[rid])),
                    digest(canonical([['card-1', 'e'*64]]))))
        # Topical/source-card registrations keep exact card bytes as BLOBs.
        # A JSON-only row digest cannot serialize them, so preparation of any
        # store that has registered a card used to fail with TypeError.
        db.execute('INSERT INTO topical_worker_versions VALUES ('
                   + ','.join('?' for _ in range(17)) + ')',
                   ('reg-1', str(project), str(story), 'wiki/card.md', 'a'*64,
                    b'\x00card \xed\x95\x9c bytes\xff', 'gen-1', 'card-1', '[]',
                    'b'*64, 'c'*64, 'd'*64, 'e'*64, 'f'*64, 'ack', 'active', 3.0))
        # Two BLOB columns, one empty: b'' must stay distinct from NULL.
        db.execute('INSERT INTO source_card_versions VALUES ('
                   + ','.join('?' for _ in range(16)) + ')',
                   ('src-reg-1', str(project), str(story), 'approved-story/file.md',
                    'a'*64, b'', 'src-ticket-1', 'b'*64, 'wiki/source-card.md', 'c'*64,
                    b'\x00source card\xff', 'card-ticket-1', 'd'*64, 'host', 'ack', 4.0))
    with database(private) as db:
        for request_id, origin in [(rid, 'tool_edit'), (second, 'user_prompt'),
                                   (scripted, 'user_prompt'),
                                   ('private-only-edit', 'tool_edit')]:
            sample = samples_by_id.get(request_id)
            if sample is None:
                sample = {'context_origin': origin,
                          'edit_event': {'ticket_id': 'unbound-ticket'}}
            db.execute('INSERT INTO samples VALUES (?,?,?)',
                       (request_id, 4 if origin == 'tool_edit' else 3,
                        canonical(sample)))
            db.execute('INSERT INTO compact_inputs VALUES (?,?,?)',
                       (request_id, 'card-1', canonical({
                           'body_text': 'body', 'body_sha256': digest('body'),
                           'source_sha256': 'e'*64})))
    shared_path = shared / 'shared-learning.sqlite3'
    private_path = private / 'learning.sqlite3'
    # Committed WAL frames from both source stores must be included.
    shared_wal_owner = sqlite3.connect(shared_path)
    assert shared_wal_owner.execute('PRAGMA journal_mode=WAL').fetchone()[0] == 'wal'
    shared_wal_owner.execute('UPDATE cases SET captured_at=? WHERE request_id=?',
                             (1.5, scripted))
    shared_wal_owner.commit()
    assert Path(str(shared_path) + '-wal').stat().st_size > 0
    wal_owner = sqlite3.connect(private_path)
    assert wal_owner.execute('PRAGMA journal_mode=WAL').fetchone()[0] == 'wal'
    wal_owner.execute('INSERT INTO compact_inputs VALUES (?,?,?)',
                      (rid, 'card-2', canonical({'body_text': 'committed-wal-row',
                           'body_sha256': digest('committed-wal-row'),
                           'source_sha256': 'e'*64})))
    wal_owner.commit()
    assert Path(str(private_path) + '-wal').stat().st_size > 0
    before = (hashlib.sha256(shared_path.read_bytes()).hexdigest(),
              hashlib.sha256(private_path.read_bytes()).hexdigest())
    result = prepare(project_id, shared_path, private_path, home=home)
    assert result['shared_cases'] == 3 and result['private_samples'] == 4
    assert result['gold_call_reservation_records'] == 3
    assert result['unresolved_gold_phases'] == 2
    assert result['pending_review_unanswered_first'] == 1 and result['active'] is False
    status = verify_prepared(result['generation'], home=home)
    assert status == {'generation': result['generation'], 'history_verified': True,
                      'sources_current': True, 'active': False}
    target = Path(result['path'])
    with sqlite3.connect(target) as db:
        assert db.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
        assert db.execute('PRAGMA journal_mode').fetchone()[0] == 'delete'
        assert db.execute('PRAGMA user_version').fetchone()[0] == 2
        assert db.execute('SELECT captured_at FROM cases WHERE request_id=?',
                          (scripted,)).fetchone()[0] == 1.5
        assert db.execute('SELECT count(*) FROM experiment_calls').fetchone()[0] == 3
        assert db.execute('SELECT count(*) FROM compact_inputs').fetchone()[0] == 5
        assert db.execute('SELECT count(*) FROM experiment_invocations').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM edit_tickets').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM cycles').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM cases WHERE gold IS NOT NULL').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM case_projects WHERE project_id=?',
                          (project_id,)).fetchone()[0] == 3
        assert db.execute('SELECT count(*) FROM sample_projects WHERE project_id=?',
                          (project_id,)).fetchone()[0] == 4
        assert db.execute('SELECT count(*) FROM case_projects WHERE story_root IS NOT NULL').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM sample_projects WHERE story_root IS NOT NULL').fetchone()[0] == 1
        assert db.execute('SELECT story_root FROM sample_projects WHERE request_id=?',
                          ('private-only-edit',)).fetchone()[0] is None
        assert db.execute('SELECT count(*) FROM model_selections').fetchone()[0] == 0
        assert db.execute('SELECT source_bytes,card_bytes FROM source_card_versions').fetchone() == \
            (b'', b'\x00source card\xff')
        assert db.execute('SELECT card_bytes FROM topical_worker_versions').fetchone()[0] == \
            b'\x00card \xed\x95\x9c bytes\xff'
        # Tables without BLOBs keep the original digest formula, so manifests
        # prepared before BLOB support stay verifiable.
        legacy = hashlib.sha256()
        for row in sorted(canonical(list(row)) for row in db.execute('SELECT * FROM cases')):
            legacy.update(row.encode('utf8'))
            legacy.update(b'\n')
        assert _table_digest(db, 'cases')['sha256'] == legacy.hexdigest()
    assert not (target.parent.parent.parent / 'history-active.json').exists()
    assert before == (hashlib.sha256(shared_path.read_bytes()).hexdigest(),
                      hashlib.sha256(private_path.read_bytes()).hexdigest())
    shared_wal_owner.close()
    wal_owner.close()
    # WAL mode with removed sidecars must also be readable through SQLite.
    with sqlite3.connect(private_path) as checkpoint:
        assert checkpoint.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0] == 0
    for suffix in ('-wal', '-shm'):
        Path(str(private_path) + suffix).unlink(missing_ok=True)
    assert not Path(str(private_path) + '-shm').exists()
    second_result = prepare(project_id, shared_path, private_path, home=home)
    assert verify_prepared(second_result['generation'], home=home)['sources_current']
    with sqlite3.connect(target) as tamper:
        assert tamper.execute('PRAGMA journal_mode=WAL').fetchone()[0] == 'wal'
        tamper.execute('INSERT INTO model_selections VALUES (?,?,?,?,?,?)',
                       ('fabricated', project_id, 'unknown-head', 'unknown-view', '[]', 'a'*64))
        tamper.commit()
        assert Path(str(target) + '-wal').stat().st_size > 0
        rejects(lambda: verify_prepared(result['generation'], home=home),
                'prepared_history_wal_not_empty')
    for bad in ('../' + result['generation'], result['generation'] + '/..', 'g-' + 'a'*31 + '/'):
        rejects(lambda: verify_prepared(bad, home=home), 'invalid_generation')
    other = project / 'other-private'
    other.mkdir(mode=0o700)
    other_db = other / 'learning.sqlite3'
    with sqlite3.connect(other_db) as db:
        db.execute('CREATE TABLE samples (request_id TEXT PRIMARY KEY,body TEXT)')
    rejects(lambda: prepare(project_id, shared_path, other_db, home=home),
            'project_history_binding_changed')
    with sqlite3.connect(private_path) as db:
        db.execute('UPDATE samples SET body=? WHERE request_id=?',
                   (json.dumps({'context_origin': 'other_project'}), rid))
    rejects(lambda: prepare(project_id, shared_path, private_path, home=home),
            'shared_private_capture_mismatch')
    with sqlite3.connect(private_path) as db:
        db.execute('UPDATE samples SET body=? WHERE request_id=?',
                   (canonical(samples_by_id[rid]), rid))
        db.execute('UPDATE compact_inputs SET body=? WHERE request_id=? AND candidate_id=?',
                   (canonical({'body_text': 'wrong', 'body_sha256': digest('wrong'),
                               'source_sha256': 'e'*64}), rid, 'card-1'))
    rejects(lambda: prepare(project_id, shared_path, private_path, home=home),
            'shared_candidate_snapshot_mismatch')
    with sqlite3.connect(private_path) as db:
        db.execute('UPDATE compact_inputs SET body=? WHERE request_id=? AND candidate_id=?',
                   (canonical({'body_text': 'body', 'body_sha256': digest('body'),
                               'source_sha256': 'e'*64}), rid, 'card-1'))
    with sqlite3.connect(shared_path) as db:
        db.execute('UPDATE edit_tickets SET candidate_sha256=? WHERE request_id=?',
                   ('a'*64, rid))
    rejects(lambda: prepare(project_id, shared_path, private_path, home=home),
            'edit_ticket_capture_mismatch')
    with sqlite3.connect(shared_path) as db:
        db.execute('UPDATE edit_tickets SET candidate_sha256=? WHERE request_id=?',
                   (digest(canonical([['card-1', 'e'*64]])), rid))
    with sqlite3.connect(shared_path) as db:
        db.execute('UPDATE cases SET project_key=? WHERE request_id=?',
                   ('wrong-key', rid))
    rejects(lambda: prepare(project_id, shared_path, private_path, home=home),
            'shared_history_project_key_mismatch')
    with sqlite3.connect(shared_path) as db:
        db.execute('UPDATE cases SET project_key=? WHERE request_id=?',
                   (project_key, rid))
    # Card registrations from another project must not migrate silently.
    with sqlite3.connect(shared_path) as db:
        db.execute('INSERT INTO topical_worker_versions VALUES ('
                   + ','.join('?' for _ in range(17)) + ')',
                   ('foreign-reg', str(home / 'other-novel'), str(home / 'other-novel/story'),
                    'wiki/other.md', '9'*64,
                    b'other', 'gen-2', 'card-9', '[]', 'b'*64, 'c'*64, 'd'*64, 'e'*64,
                    'f'*64, 'ack', 'active', 5.0))
    rejects(lambda: prepare(project_id, shared_path, private_path, home=home),
            'card_registration_scope_mismatch')
    with sqlite3.connect(shared_path) as db:
        db.execute("DELETE FROM topical_worker_versions WHERE registration_id='foreign-reg'")
    with sqlite3.connect(shared_path) as db:
        original_error = db.execute('SELECT error FROM cases WHERE request_id=?',
                                    (rid,)).fetchone()[0]
        db.execute("UPDATE cases SET error='new-after-snapshot' WHERE request_id=?", (rid,))
    with sqlite3.connect(shared_path) as db:
        db.execute('UPDATE cases SET error=? WHERE request_id=?', (original_error, rid))
    assert verify_prepared(second_result['generation'], home=home)['sources_current'] is True
    # A change confined to BLOB bytes is drift too.
    with sqlite3.connect(shared_path) as db:
        db.execute('UPDATE topical_worker_versions SET card_bytes=? WHERE registration_id=?',
                   (b'\x00card \xed\x95\x9c bytes\xfe', 'reg-1'))
    assert verify_prepared(second_result['generation'], home=home)['sources_current'] is False

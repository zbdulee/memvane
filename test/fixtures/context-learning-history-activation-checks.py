"""Disposable activation, crash, locking, routing, and rollback checks."""
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from unittest.mock import patch

from context_learning.contracts import digest, request
from context_learning.compact_input import derive
from context_learning import edit_ticket, synthetic_experiment, synthetic_scope
from context_learning.synthetic_teacher import process_pending
from context_learning.shared import enqueue, activate_edit_case
from context_learning.synthetic_experiment import PLANNED_BUNDLE_NAME, SHARED_NAME
import context_learning.shared as shared_module
import context_learning.synthetic_teacher as teacher_module
import edit_context
from context_learning.history_activation import (
    HOST_FILES, activate, recover, rollback, selected_database, writer_access,
    read_database, record_case_mapping, _write_state)
from context_learning import history_fence
from context_learning.history_migration import prepare
from context_learning.history_lifecycle import request as lifecycle_request
from context_learning.shared import _db
from context_learning.storage_layout import register_project
from context_learning.store import canonical, database, capture


def rejects(action, reason):
    try:
        action()
    except (ValueError, OSError, sqlite3.Error) as error:
        assert reason in str(error), str(error)
    else:
        raise AssertionError(f'expected {reason}')


with tempfile.TemporaryDirectory(prefix='qmd-history-activation-', dir='/tmp') as name:
    probe = sqlite3.connect(':memory:')
    probe.execute('CREATE TABLE meta (value TEXT)')
    history_fence.install(probe, {'meta': {'columns': [], 'rows': []}})
    rejects(lambda: probe.execute("INSERT INTO meta VALUES ('blocked')"),
            'qmd_history_fenced')
    probe.execute('CREATE TABLE selection_gold (value TEXT)')
    probe.execute("INSERT INTO selection_gold VALUES ('unfenced')")
    assert probe.execute('SELECT value FROM selection_gold').fetchone() == ('unfenced',)
    probe.close()

    home = Path(name).resolve()
    project = home / 'novel'
    story = project / PLANNED_BUNDLE_NAME
    story.mkdir(parents=True)
    shared = project / SHARED_NAME
    shared.mkdir(mode=0o700)
    private_parent = project / 'private'
    private_parent.mkdir(mode=0o700)
    private = private_parent / digest(str(project))
    private.mkdir(mode=0o700)
    settings = project / '.auto-context/settings.json'
    settings.parent.mkdir()
    wiki_collection = 'fantasy-20261008-wiki'
    wiki_rel = str(Path(story.name) / '.auto-context/wiki')
    settings_value = {'indexing': True, 'collections': [wiki_collection],
                      'collectionPaths': {wiki_collection: wiki_rel},
                      'collectionRoles': {wiki_collection: 'wiki'},
                      'contextLearning': {'capture': True,
                                          'stateRoot': str(private_parent)},
                      'sharedLearning': {'enabled': True, 'root': str(shared)}}
    settings.write_text(canonical(settings_value))
    consent_dir = shared / 'consents'
    consent_dir.mkdir(mode=0o700)
    consent = consent_dir / (digest(str(project)) + '.json')
    consent.write_text(canonical({'schema': 'qmd-shared-consent-v1',
                                  'projectRoot': str(project),
                                  'sharedRoot': str(shared), 'capture': True,
                                  'localTraining': True,
                                  'externalTeacher': False}))
    consent.chmod(0o600)
    project_id = register_project(project, story_root=story,
                                  display_name='Approved story', home=home)
    candidate = {'candidate_id': 'card-1', 'revision_sha256': 'e'*64}
    sample = request({'prompt': 'Which old synthetic card?'}, [{**candidate,
        'eligible': True, 'excerpt': 'body'}], host='codex',
        request_id='old-request-id')
    body = canonical(sample)
    request_id = 'old-request-id'
    with _db(shared, home=home) as db:
        db.execute('INSERT INTO cases VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   (digest(str(project)), str(project), request_id, digest(body),
                    body, canonical({'card-1': 'body'}), 'b'*64, 1.0,
                    'train', '[]', 'active', 'gold', '{}', '{}',
                    canonical({'selected_ids': ['card-1']}), None, 'old-cycle'))
        db.execute('INSERT INTO cycles VALUES (?,?,?,?,?)',
                   ('old-cycle', 'accepted', '{}', 1.0, 2.0))
        db.execute('INSERT INTO experiment_calls VALUES (?,?,?,?)',
                   ('gold:old-request-id:first_running', 'gold', 1, 1.0))
        db.execute('INSERT INTO experiment_invocations VALUES (?,?,?,?,?)',
                   ('gold:old-request-id:first_running', 'a'*64, 'uncertain', None, None))
    with database(private, home=home) as db:
        db.execute('INSERT INTO samples VALUES (?,?,?)',
                   (request_id, sample['schema_version'], body))
        db.execute('INSERT INTO compact_inputs VALUES (?,?,?)',
                   (request_id, 'card-1', canonical({'body_text': 'body',
                    'body_sha256': digest('body'), 'source_sha256': 'e'*64})))
    shared_db = shared / 'shared-learning.sqlite3'
    private_db = private / 'learning.sqlite3'
    prepared = prepare(project_id, shared_db, private_db, home=home)
    generation = prepared['generation']
    rejects(lambda: activate(generation, home=Path.home()),
            'history_legacy_ddl_unfenced')
    pointer = home / 'Library/Application Support/qmd-auto-context/learning/history-active.json'
    source = Path(__file__).resolve().parents[2]
    hosts = {}
    for host in ('claude', 'codex'):
        root = home / ('.claude' if host == 'claude' else '.codex') / 'plugins/cache/fake/qmd-auto-context/v-next'
        for relative in HOST_FILES:
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source / relative, target)
        hosts[host] = root

    def new_lifecycle():
        lifecycle = lifecycle_request(generation, home=home)
        for engine, plugin_root in hosts.items():
            manifest_name = 'hooks/hooks-codex.json' if engine == 'codex' else 'hooks/hooks.json'
            manifest = json.loads((plugin_root / manifest_name).read_text())
            commands = [hook['command'] for event in manifest['hooks']['SessionStart']
                        for hook in event['hooks']]
            assert len(commands) == 2
            environment = {**os.environ, 'HOME': str(home),
                           'CLAUDE_PLUGIN_ROOT': str(plugin_root),
                           'PYTHONPATH': str(plugin_root / 'core'),
                           'PYTHONDONTWRITEBYTECODE': '1',
                           'QMD_RECALL_LOG': '', 'QMD_SUPPRESS_NOTICE': '1',
                           'QMD_AUTO_COMPILE_DISABLED': '1',
                           'QMD_UPDATE_HOOK_QUEUE_DIR': str(home / 'update-queue')}
            for index, command in enumerate(commands):
                args = shlex.split(command.replace('${CLAUDE_PLUGIN_ROOT}',
                                                   str(plugin_root)))
                assert args[0] == str(plugin_root / 'hooks/run-hook')
                assert args[2] == engine
                assert args[1] == ('update-queued' if index == 0
                                   else 'topical-reconcile')
                result = subprocess.run(args, cwd=home, env=environment,
                                        input=canonical({'cwd': str(home),
                                            'hook_event_name': 'SessionStart'}),
                                        capture_output=True, text=True,
                                        timeout=5, check=False)
                assert result.returncode == 0 and not result.stdout, result.stderr
                token = hashlib.sha256(str(plugin_root).encode()).hexdigest()[:24]
                receipt = pointer.parent / f'history-host-ack-{engine}-{token}.json'
                if index == 0:
                    ack = json.loads(receipt.read_text())
                    assert (ack['nonce'], ack['engine'], ack['plugin_root']) == (
                        lifecycle['nonce'], engine, str(plugin_root))

    claude_protocol = hosts['claude'] / HOST_FILES[0]
    claude_protocol.unlink()
    rejects(lambda: activate(generation, host_roots=hosts, home=home),
            'history_host_not_coordinated:claude')
    assert not pointer.exists()
    shutil.copy2(source / HOST_FILES[0], claude_protocol)
    adapter = hosts['claude'] / 'core/context_learning/codex_adapter.py'
    adapter.write_bytes(adapter.read_bytes() + b'\n# old adapter\n')
    rejects(lambda: activate(generation, host_roots=hosts, home=home),
            'history_host_not_coordinated:claude')
    shutil.copy2(source / 'core/context_learning/codex_adapter.py', adapter)
    old_codex = home / '.codex/plugins/cache/old/qmd-auto-context/v-old'
    old_codex.mkdir(parents=True)
    hosts['codex'] = [hosts['codex'], old_codex]
    rejects(lambda: activate(generation, host_roots=hosts, home=home),
            'history_host_not_coordinated:codex')
    hosts['codex'] = hosts['codex'][0]
    rejects(lambda: activate(generation, host_roots={'codex': hosts['codex']},
                             home=home), 'history_host_inventory_incomplete')
    rejects(lambda: activate('../' + generation, host_roots=hosts, home=home),
            'invalid_generation')
    rejects(lambda: activate(generation, host_roots=hosts, home=home),
            'history_lifecycle_request_missing')
    new_lifecycle()
    malformed_used = pointer.parent / 'history-lifecycle-used.json'
    malformed_used.write_text('{"nonce":"x"}')
    malformed_used.chmod(0o600)
    rejects(lambda: activate(generation, host_roots=hosts, home=home),
            'history_lifecycle_used_receipt_invalid')
    malformed_used.unlink()

    with sqlite3.connect(shared_db) as db:
        db.execute("UPDATE cases SET error='new-source-change'")
    rejects(lambda: activate(generation, host_roots=hosts, home=home),
            'history_sources_changed')
    assert not pointer.exists()
    with sqlite3.connect(shared_db) as db:
        db.execute('UPDATE cases SET error=NULL')

    child_script = ('from context_learning.history_activation import writer_access; '
                    'import sys; '
                    'with_writer=writer_access(sys.argv[1],home=sys.argv[2]); '
                    'with_writer.__enter__(); print("held",flush=True); '
                    'sys.stdin.readline(); with_writer.__exit__(None,None,None)')
    child_env = {**os.environ, 'PYTHONPATH': str(source / 'core'),
                 'PYTHONDONTWRITEBYTECODE': '1'}
    child = subprocess.Popen([sys.executable, '-c', child_script,
                              str(shared_db), str(home)], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, env=child_env)
    try:
        assert child.stdout.readline().strip() == 'held'
        rejects(lambda: activate(generation, host_roots=hosts, home=home,
                                 lock_timeout=.05), 'history_writer_busy')
        assert not pointer.exists()
    finally:
        child.communicate('\n', timeout=5)
        assert child.returncode == 0

    worker_script = ('from context_learning.history_lifecycle import worker_lease; '
                     'import sys; lease=worker_lease(home=sys.argv[1]); '
                     'lease.__enter__(); print("held",flush=True); '
                     'sys.stdin.readline(); lease.__exit__(None,None,None)')
    worker = subprocess.Popen([sys.executable, '-c', worker_script, str(home)],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True, env=child_env)
    try:
        assert worker.stdout.readline().strip() == 'held'
        rejects(lambda: activate(generation, host_roots=hosts, home=home,
                                 lock_timeout=.05), 'history_worker_busy')
        assert not pointer.exists()
    finally:
        worker.communicate('\n', timeout=5)
        assert worker.returncode == 0

    old_transaction = sqlite3.connect(shared_db)
    old_transaction.execute('BEGIN IMMEDIATE')
    try:
        rejects(lambda: activate(generation, host_roots=hosts, home=home,
                                 lock_timeout=.05), 'database is locked')
        assert not pointer.exists()
    finally:
        old_transaction.rollback()
        old_transaction.close()
    assert recover(host_roots=hosts, home=home)['recovered_interrupted_phase'] == 'fencing'

    # A process may die after committing the first source's triggers but before
    # fencing the second. Recovery must remove only our verified first fence.
    manifest = json.loads((pointer.parent / 'migrations' / generation /
                           'prepared.json').read_text())
    _write_state(generation, 'fencing', home=home)
    with history_fence.exclusive_pair((shared_db, private_db)) as databases:
        history_fence.install(databases[0], manifest['shared_tables'])
    assert recover(host_roots=hosts, home=home) == {
        'active': False, 'generation': generation, 'sources_current': True,
        'recovered_interrupted_phase': 'fencing'}
    with sqlite3.connect(shared_db) as db:
        assert not history_fence._actual(db)

    with patch('context_learning.history_activation.os.replace',
               side_effect=OSError('simulated-crash-before-replace')):
        rejects(lambda: activate(generation, host_roots=hosts, home=home),
                'simulated-crash-before-replace')
    assert not pointer.exists()
    assert not list(pointer.parent.glob('.history-active-*'))

    crash_script = (
        'import os,sys; from unittest.mock import patch; '
        'from context_learning.history_activation import activate; '
        'stop=lambda *args,**kwargs: os._exit(94); '
        'guard=patch("context_learning.history_activation._atomic_pointer", '
        'side_effect=stop); guard.start(); '
        'activate(sys.argv[1],home=sys.argv[2],host_roots={'
        '"claude":sys.argv[3],"codex":sys.argv[4]})')
    crashed = subprocess.run([sys.executable, '-c', crash_script,
                              generation, str(home), str(hosts['claude']),
                              str(hosts['codex'])], env=child_env, check=False)
    assert crashed.returncode == 94 and not pointer.exists()
    rejects(lambda: selected_database(shared_db, home=home),
            'history_pointer_missing_after_activation')
    assert recover(host_roots=hosts, home=home) == {
        'active': False, 'generation': generation,
        'sources_current': True,
        'recovered_interrupted_phase': 'activating'}

    new_lifecycle()
    activated = activate(generation, host_roots=hosts, home=home)
    assert activated['generation'] == generation and pointer.exists()
    assert selected_database(shared_db, home=home) == Path(prepared['path'])
    assert selected_database(private_db, home=home) == Path(prepared['path'])
    stale_shared = sqlite3.connect(shared_db)
    stale_private = sqlite3.connect(private_db)
    for connection, statement in ((stale_shared,
                                   "INSERT INTO meta VALUES ('stale-old-writer','blocked')"),
                                  (stale_private,
                                   "INSERT INTO tombstones VALUES ('stale-old-writer')")):
        try:
            connection.execute(statement)
        except sqlite3.IntegrityError as exc:
            assert 'qmd_history_fenced' in str(exc), str(exc)
            connection.rollback()
        else:
            raise AssertionError('stale legacy writer changed source history')
        connection.close()
    with read_database(shared_db, home=home) as db:
        assert db.execute('SELECT request_id FROM cases').fetchone() == (request_id,)
    assert recover(host_roots=hosts, home=home) == {
        'active': True, 'generation': generation, 'project_id': project_id}
    with _db(shared, home=home) as db:
        assert db.execute('SELECT request_id,trained_cycle FROM cases').fetchone() == (
            request_id, 'old-cycle')
        assert db.execute('SELECT state FROM experiment_invocations').fetchone() == ('uncertain',)
        assert db.execute('SELECT count(*) FROM experiment_calls').fetchone()[0] == 1
    with database(private, home=home) as db:
        assert db.execute('SELECT request_id FROM samples').fetchone() == (request_id,)
    assert rollback(host_roots=hosts, home=home) == {
        'active': False, 'generation': generation}
    assert not pointer.exists()
    assert selected_database(shared_db, home=home) == shared_db
    assert recover(host_roots=hosts, home=home) == {'active': False}

    new_lifecycle()
    activate(generation, host_roots=hosts, home=home)
    rollback_crash = (
        'import os,sys; from unittest.mock import patch; '
        'from context_learning.history_activation import rollback; '
        'stop=lambda *args,**kwargs: os._exit(95); '
        'guard=patch("context_learning.history_activation._remove_partial_fences", '
        'side_effect=stop); guard.start(); '
        'rollback(home=sys.argv[1],host_roots={'
        '"claude":sys.argv[2],"codex":sys.argv[3]})')
    crashed = subprocess.run([sys.executable, '-c', rollback_crash,
                              str(home), str(hosts['claude']), str(hosts['codex'])],
                             env=child_env, check=False)
    assert crashed.returncode == 95 and not pointer.exists()
    rejects(lambda: selected_database(shared_db, home=home),
            'history_pointer_missing_after_activation')
    with sqlite3.connect(shared_db) as stale:
        try:
            stale.execute("INSERT INTO meta VALUES ('rollback-crash','blocked')")
        except sqlite3.IntegrityError as exc:
            assert 'qmd_history_fenced' in str(exc)
        else:
            raise AssertionError('legacy fence vanished during rollback crash')
    assert recover(host_roots=hosts, home=home) == {
        'active': False, 'generation': generation, 'sources_current': True,
        'recovered_interrupted_phase': 'rollback_pending'}

    new_lifecycle()
    activate(generation, host_roots=hosts, home=home)
    fresh = request({'prompt': 'Which synthetic card?'}, [
        {'candidate_id': 'card-1', 'revision_sha256': 'e'*64,
         'eligible': True, 'excerpt': 'body'}],
        host='codex', request_id='new-request-id')
    assert capture(fresh, enabled=True, state_dir=private, home=home) == 'stored'
    with database(private, home=home) as db:
        assert db.execute('SELECT project_id,project_root,story_root FROM sample_projects '
                          'WHERE request_id=?', ('new-request-id',)).fetchone() == (
                              project_id, str(project), None)
    with _db(shared, home=home) as db:
        db.execute('INSERT INTO cases VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   (digest(str(project)), str(project), 'new-request-id',
                    digest(canonical(fresh)), canonical(fresh), '{}', 'b'*64, 2.0,
                    'train', '[]', 'active', 'pending_review', None, None, None, None, None))
        record_case_mapping(db, project, fresh, home=home)
        assert db.execute('SELECT project_id,project_root,story_root FROM case_projects '
                          'WHERE request_id=?', ('new-request-id',)).fetchone() == (
                              project_id, str(project), None)
    with read_database(shared_db, home=home) as db:
        assert db.execute('SELECT request_id FROM cases WHERE request_id=?',
                          ('new-request-id',)).fetchone() == ('new-request-id',)
    assert sqlite3.connect(shared_db).execute(
        'SELECT request_id FROM cases WHERE request_id=?',
        ('new-request-id',)).fetchone() is None

    # A real edit ticket, shared enqueue, private capture and two stub gold
    # selections all persist through the selected history after cutover.
    draft = story / '04_Manuscript/ep028.md'
    draft.parent.mkdir(parents=True)
    before = '합성 규칙을 유지한다. 옛 내용은 사라진다. 끝.'
    after = '합성 규칙을 유지한다. 끝.'
    draft.write_text(after)
    wiki = story / '.auto-context/wiki/doc.md'
    wiki.parent.mkdir(parents=True)
    wiki_source = '---\ntitle: Synthetic rule\n---\nA synthetic rule stays active.\n'
    wiki.write_text(wiki_source)
    revision = digest(wiki_source)
    region, start, side = edit_context._changed_region(before, after)
    change = {'operation': 'edit', 'path': '04_Manuscript/ep028.md',
              'text': region, 'source_start': start, 'source_side': side,
              'source_sha256': digest(region), 'document_sha256': digest(before),
              'before_sha256': digest(before), 'after_sha256': digest(after)}
    event = {'edit_session_id': 'session', 'edit_turn_id': 'turn',
             'edit_tool_use_id': 'tool'}
    metadata = {'parent_sha256': digest(before),
                'path_sha256': digest(change['path']),
                'segment_sha256': digest(region), 'start': start,
                'end': start + len(region), 'index': 0, 'count': 1,
                'change_index': 0, 'change_count': 1,
                'session_id': 'session', 'turn_id': 'turn',
                'tool_use_id': 'tool', 'operation': 'edit'}
    cid = wiki_collection + '/doc.md'
    sampling = {'mode': 'eligible-policy-pool', 'scope': 'queried-phases-only',
                'phase_counts': {'primary': 1}, 'per_phase_limit': 30,
                'eligible_ids': [cid], 'captured_ids': [cid],
                'baseline_selected_ids': [cid], 'excluded': [],
                'snapshot_failures': [], 'retrieval_truncated': False,
                'assessment_complete': True,
                'complete_within_returned_bound': True,
                'index_revision': 'c' * 64, 'candidate_limit': 15}
    policy = {'projectRoot': str(project), 'maxCalls': 2,
              'reservedUsd': None, 'reserveUsdPerCall': None,
              'model': 'gpt-6.1-sol'}
    worker_policy = {'maxGoldCalls': 2, 'goldModel': 'gpt-6.1-sol'}
    with patch.dict(os.environ, {'HOME': str(home)}), patch.object(
            edit_ticket, '_approval', return_value=('a' * 64, 'b' * 64)), patch.object(
            synthetic_experiment, 'load', return_value=({'synthetic': True}, 'c' * 64)):
        with shared_module._db(shared) as db:
            db.execute('INSERT INTO experiment_generated '
                       '(kind,episode,slot,relative_path,sha256,prior_sha256,recorded_at) '
                       'VALUES (?,?,?,?,?,?,?)',
                       ('wiki_review', 28, 1, '.auto-context/wiki/doc.md',
                        revision, None, 1.0))
        ticket = edit_ticket.reserve(project, change, event, metadata, region)
        edit_sample = request({'prompt': region, 'context_origin': 'tool_edit',
                               'edit_event': {**metadata, 'ticket_id': ticket}},
                              [{'candidate_id': cid, 'revision_sha256': revision,
                                'eligible': True, 'excerpt': wiki_source}],
                              host='codex', request_id='edit-after-cutover',
                              sampling=sampling)
        with patch.object(shared_module, '_policy', return_value={
                'goldModel': 'gpt-6.1-sol'}):
            assert enqueue(settings_value, project, edit_sample,
                           {cid: wiki_source}) == 'awaiting_local'
        assert capture(edit_sample, enabled=True, state_dir=private,
                       compact_sources={cid: wiki_source}) == 'stored'
        assert activate_edit_case(settings_value, project, edit_sample,
                                  captured=True) == 'queued'
        calls = []
        def stub_provider(sample, *_args):
            calls.append(sample['request_id'])
            return {'selected_ids': [cid], 'usage': {},
                    'elapsed_seconds': .01, 'peak_rss_mib': 1}
        with patch.object(synthetic_scope, '_synthetic_bundle',
                          return_value=(shared, project)), patch.object(
                synthetic_experiment, 'load', return_value=None), patch.object(
                teacher_module, 'policy_for', return_value=policy), patch.object(
                teacher_module, 'allowed_case',
                return_value=(policy, '{}')), patch.object(
                teacher_module, 'resource_report',
                return_value={'ready': True}):
            assert process_pending(shared, project, worker_policy,
                                   invoke=stub_provider)['outcomes'] == [
                                       'first_done', 'gold']
        assert calls == ['edit-after-cutover', 'edit-after-cutover']
        with shared_module._db(shared) as db:
            assert db.execute('SELECT status,gold FROM cases WHERE request_id=?',
                              ('edit-after-cutover',)).fetchone()[0] == 'gold'
            assert db.execute('SELECT story_root FROM case_projects WHERE request_id=?',
                              ('edit-after-cutover',)).fetchone() == (str(story),)
            assert db.execute('SELECT input_sha256 FROM edit_tickets WHERE ticket_id=?',
                              (ticket,)).fetchone() == (digest(canonical(edit_sample)),)
        with database(private) as db:
            assert db.execute('SELECT story_root FROM sample_projects WHERE request_id=?',
                              ('edit-after-cutover',)).fetchone() == (str(story),)
        with sqlite3.connect(shared_db) as old_db:
            assert old_db.execute('SELECT 1 FROM cases WHERE request_id=?',
                                  ('edit-after-cutover',)).fetchone() is None
    rejects(lambda: rollback(host_roots=hosts, home=home),
            'history_changed_after_activation')
    assert pointer.exists()
    assert recover(host_roots=hosts, home=home)['active'] is True
    saved_pointer = pointer.read_bytes()
    pointer.unlink()
    rejects(lambda: selected_database(shared_db, home=home),
            'history_pointer_missing_after_activation')
    rejects(lambda: recover(host_roots=hosts, home=home),
            'history_pointer_missing_after_activation')
    pointer.symlink_to('missing-pointer')
    rejects(lambda: selected_database(shared_db, home=home),
            'unsafe_history_pointer')
    pointer.unlink()
    pointer.write_bytes(saved_pointer)
    pointer.chmod(0o600)
    assert recover(host_roots=hosts, home=home)['active'] is True

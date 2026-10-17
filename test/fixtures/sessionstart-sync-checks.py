"""Synthetic git transitions through the product SessionStart sync function."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest import mock

original_env = os.environ.copy()
temp_context = tempfile.TemporaryDirectory(prefix='qmd-session-sync-')
home = Path(temp_context.name).resolve()
os.environ.update({'HOME': str(home), 'QMD_SYNC_STATE_DIR': str(home / 'sync-state'),
                   'QMD_SYNC_LOCKDIR': str(home / 'sync.lock')})
sys.path.insert(0, str(Path.cwd() / 'core'))
import dirty_queue
import qmd_shared_store
import resolve_paths
import runtime_work
import setup_guard
import sync


def invoke(root, **kwargs):
    output, errors = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
        code = sync.run(str(root), json_output=True, **kwargs)
    return code, json.loads(output.getvalue()), errors.getvalue()


@contextlib.contextmanager
def forbidden(*names):
    targets = {
        'lock': (sync, 'acquire_lock'),
        'scan': (sync, 'build_snapshot'),
        'write': (sync, 'write_state_atomic'),
        'dirty': (dirty_queue, 'enqueue_project_collections'),
        'compile': (sync.compile_enqueue, 'enqueue_changed_sources'),
    }
    with contextlib.ExitStack() as stack:
        for name in names:
            owner, attr = targets[name]
            stack.enter_context(mock.patch.object(owner, attr, side_effect=AssertionError(name)))
        yield


INVALID_SYNC_VALUES = ('false', 'true', 0, 1, None, [], {})
for raw, normalized, invalid in (
    ({}, True, False),
    ({'sessionStartSync': True}, True, False),
    ({'sessionStartSync': False}, False, False),
    *(({'sessionStartSync': value}, False, True) for value in INVALID_SYNC_VALUES),
):
    assert sync.qmd_config.normalize_config(raw)['sessionStartSync'] is normalized, raw
    assert sync.qmd_config.invalid_session_start_sync(raw) is invalid, raw


def git(root, *args):
    result = subprocess.run(['git', '-c', 'commit.gpgsign=false', '-C', str(root), *args],
                            capture_output=True, text=True,
                            env={**os.environ, 'GIT_CONFIG_GLOBAL': os.devnull,
                                 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_SYSTEM': os.devnull,
                                 'GIT_TEMPLATE_DIR': str(home / 'empty-git-template')})
    assert result.returncode == 0, (args, result.stderr)


with temp_context as temp:
    (home / 'store').mkdir()
    project = home / 'story'
    source = project / 'chapters'
    source.mkdir(parents=True)
    settings = project / '.auto-context' / 'settings.json'
    settings.parent.mkdir()
    settings.write_text(json.dumps({
        'indexing': True, 'collections': ['chapters'],
        'collectionPaths': {'chapters': 'chapters'},
        'collectionRoles': {'chapters': 'raw'},
        'compile': {'mode': 'off'},
    }))
    git(project, 'init', '-q', '-b', 'main')
    git(project, 'config', 'user.name', 'Synthetic')
    git(project, 'config', 'user.email', 'synthetic@example.invalid')
    (source / 'a.md').write_text('A revision one.\n')
    (source / 'remove.md').write_text('Remove later.\n')
    git(project, 'add', '.')
    git(project, 'commit', '-qm', 'base')
    git(project, 'branch', 'base')
    queued = []
    try:
        with mock.patch.object(setup_guard, 'status', return_value={'status': 'ready'}), \
             mock.patch.object(qmd_shared_store, 'select', return_value={'generation': 'synthetic'}) as select, \
             mock.patch.object(qmd_shared_store, 'root', return_value=home / 'store'), \
             mock.patch.object(resolve_paths, 'is_risky_path', return_value=False), \
             mock.patch.object(runtime_work, 'request', return_value={'status': 'queued'}), \
             mock.patch.object(runtime_work, 'hook_wake', return_value=None), \
             mock.patch.object(dirty_queue, 'enqueue_project_collections',
                               side_effect=lambda root, changed: queued.append(sorted(changed))):
            def scan():
                output = io.StringIO()
                env_before = os.environ.copy()
                with contextlib.redirect_stdout(output):
                    assert sync.run(str(project), session_start=True, json_output=True) == 0
                assert os.environ == env_before, 'SessionStart sync must not mutate caller environment'
                return json.loads(output.getvalue())

            first = scan()
            assert first['reason'] == 'baseline' and first['compileQueued'] == 0, first
            assert queued == [], queued
            assert scan()['reason'] == 'unchanged'
            state = next((home / 'store' / 'session-sync').glob('*.json'))
            before_error = state.read_bytes()
            with mock.patch.object(sync, 'scan_files', side_effect=PermissionError('synthetic denial')):
                try:
                    scan()
                except PermissionError:
                    pass
                else:
                    raise AssertionError('I/O denial must not become a deletion')
            assert state.read_bytes() == before_error
            git(project, 'checkout', '-q', '-b', 'incoming')
            (source / 'a.md').write_text('A revision two.\n')
            (source / 'new.md').write_text('New branch file.\n')
            (source / 'remove.md').unlink()
            git(project, 'add', '-A')
            git(project, 'commit', '-qm', 'incoming change')
            git(project, 'checkout', '-q', 'main')
            git(project, 'pull', '--ff-only', '.', 'incoming')
            changed = scan()
            assert (changed['created'], changed['updated'], changed['deleted']) == (1, 1, 1), changed
            assert changed['compileQueued'] == 0 and queued == [['chapters']], (changed, queued)
            git(project, 'checkout', '-q', 'base')
            switched = scan()
            assert (switched['created'], switched['updated'], switched['deleted']) == (1, 1, 1), switched
            assert switched['compileQueued'] == 0
            blocked = source / 'blocked'
            blocked.mkdir()
            (blocked / 'held.md').write_text('Previous content.\n')
            assert scan()['created'] == 1
            original_open = Path.open
            def denied_open(path, *args, **kwargs):
                if path == blocked / 'held.md':
                    raise PermissionError('synthetic unreadable file')
                return original_open(path, *args, **kwargs)
            with mock.patch.object(Path, 'open', denied_open):
                (source / 'a.md').write_text('A changed while blocked.\n')
                partial = scan()
                assert partial['updated'] == 1 and partial['deleted'] == 0, partial
                assert partial['warnings'][0]['reason'] == 'unreadable_paths', partial
                assert 'held.md' in state.read_text()
            settings_obj = json.loads(settings.read_text())
            settings_obj['sessionStartSync'] = False
            settings_obj['compile'] = {'mode': 'auto-wiki', 'triggers': ['session_start_source']}
            settings.write_text(json.dumps(settings_obj))
            (source / 'a.md').write_text('Explicitly disabled sync.\n')
            state_before_disabled = state.read_bytes()
            assert scan()['reason'] == 'session_start_sync_disabled'
            assert queued == [['chapters']] * 4, queued
            assert not (project / '.auto-context' / 'compile' / 'source-queue.jsonl').exists()
            assert state.read_bytes() == state_before_disabled

            # A separate project pins both the first-run state shape and the
            # refusal behavior without disturbing the Git transition fixture.
            guarded = home / 'guarded'
            (guarded / 'docs').mkdir(parents=True)
            (guarded / 'empty').mkdir()
            source_file = guarded / 'docs' / 'one.md'
            source_file.write_text('Original source.\n')
            source_bytes = source_file.read_bytes()
            guarded_settings = guarded / '.auto-context' / 'settings.json'
            guarded_settings.parent.mkdir()
            base_settings = {
                'indexing': True, 'collections': ['g-docs', 'g-empty'],
                'collectionPaths': {'g-docs': 'docs', 'g-empty': 'empty'},
                'collectionRoles': {'g-docs': 'raw', 'g-empty': 'raw'},
                'compile': {'mode': 'off'},
            }
            guarded_settings.write_text(json.dumps(base_settings))
            code, result, _ = invoke(guarded, session_start=True)
            assert (code, result['reason']) == (0, 'baseline'), result
            session_state = Path(result['statePath'])
            assert sync.load_state(session_state) is not None  # empty collection round trip
            code, result, _ = invoke(guarded, baseline_only=True)
            assert (code, result['reason']) == (0, 'baseline'), result
            manual_state = Path(result['statePath'])
            assert manual_state != session_state and manual_state.parent == home / 'sync-state'
            valid_state = session_state.read_bytes()
            compile_settings = {**base_settings, 'compile': {
                'mode': 'auto-wiki', 'triggers': ['session_start_source', 'post_sync_source']}}
            guarded_settings.write_text(json.dumps(compile_settings))
            source_queue = guarded / '.auto-context' / 'compile' / 'source-queue.jsonl'

            def assert_refused(code, result, errors, target, reason, original):
                assert code == sync.EXIT_STATE_UNREADABLE, (reason, code, result)
                assert result['ok'] is False and result['reason'] == 'state_unreadable', result
                assert result['stateError'] == reason, result
                assert (result['created'], result['updated'], result['deleted']) == (0, 0, 0)
                assert result['collectionsQueued'] == [] and result['compileQueued'] == 0
                assert result['compileDeferred'] == 0 and result['compileReason'] == 'not_attempted'
                assert result['statePath'] == str(target), result
                assert f'sync state unreadable ({reason})' in errors, errors
                if original is not None:
                    assert target.read_bytes() == original, reason
                assert source_file.read_bytes() == source_bytes
                assert not source_queue.exists()
                assert not list(target.parent.glob(f'.{target.name}.tmp-*'))

            for original, reason in (
                (b'', 'invalid_json'),
                (b'{not json', 'invalid_json'),
                (b'\xff\xfe{}', 'not_utf8'),
                (b'[]', 'not_object'),
                (b'{}', 'bad_collections'),
                (b'{"collections": []}', 'bad_collections'),
                (b'{"collections": {"g-docs": {"files": {"one.md": 1}}}}', 'bad_collection_entry'),
            ):
                session_state.write_bytes(original)
                with forbidden('scan', 'write', 'dirty', 'compile'):
                    code, result, errors = invoke(guarded, session_start=True)
                assert_refused(code, result, errors, session_state, reason, original)

            outside = home / 'outside-state.json'
            outside.write_bytes(valid_state)
            for target in (outside, home / 'missing-state.json'):
                session_state.unlink()
                session_state.symlink_to(target)
                with forbidden('scan', 'write', 'dirty', 'compile'):
                    code, result, errors = invoke(guarded, session_start=True)
                assert_refused(code, result, errors, session_state, 'symlink', None)
                assert session_state.is_symlink() and os.readlink(session_state) == str(target)
            assert outside.read_bytes() == valid_state
            session_state.unlink()

            manual_original = b'{"collections": []}'
            manual_state.write_bytes(manual_original)
            for options in ({}, {'dry_run': True}, {'baseline_only': True}):
                with forbidden('scan', 'write', 'dirty', 'compile'):
                    code, result, errors = invoke(guarded, **options)
                assert_refused(code, result, errors, manual_state, 'bad_collections', manual_original)

            # A refusal releases its lock; removing the damaged state permits
            # the existing first-run baseline policy on the next invocation.
            guarded_settings.write_text(json.dumps(base_settings))
            code, result, _ = invoke(guarded, session_start=True)
            assert (code, result['reason']) == (0, 'baseline'), result
            manual_state.unlink()
            code, result, _ = invoke(guarded, baseline_only=True)
            assert (code, result['reason']) == (0, 'baseline'), result
            valid_state = session_state.read_bytes()

            for value in INVALID_SYNC_VALUES:
                guarded_settings.write_text(json.dumps({**compile_settings, 'sessionStartSync': value}))
                session_state.write_bytes(b'{')
                select.reset_mock()
                with forbidden('lock', 'scan', 'write', 'dirty', 'compile'):
                    code, result, errors = invoke(guarded, session_start=True)
                assert code == 0 and result == {
                    'ok': False, 'reason': 'session_start_sync_invalid', 'compileQueued': 0,
                }, (value, result)
                assert 'sessionStartSync' in errors and session_state.read_bytes() == b'{'
                assert not source_queue.exists()
                select.assert_not_called()
            session_state.write_bytes(valid_state)
            guarded_settings.write_text(json.dumps({**base_settings, 'sessionStartSync': False}))
            with forbidden('lock', 'scan', 'write', 'dirty', 'compile'):
                code, result, errors = invoke(guarded, session_start=True)
            assert code == 0 and result['reason'] == 'session_start_sync_disabled' and errors == ''
            for setting in (base_settings, {**base_settings, 'sessionStartSync': True}):
                guarded_settings.write_text(json.dumps(setting))
                code, result, errors = invoke(guarded, session_start=True)
                assert (code, result['reason'], errors) == (0, 'unchanged', ''), result

            # Two explicit compile grants already used independent manual and
            # SessionStart snapshots before this change. Pin the duplicate
            # pending record so the future wiki-owned revision key has a test.
            dual = home / 'dual-consent'
            (dual / 'docs').mkdir(parents=True)
            (dual / 'docs' / 'same.md').write_text('Same revision.\n')
            (dual / '.auto-context').mkdir()
            (dual / '.auto-context' / 'settings.json').write_text(json.dumps({
                'indexing': True, 'collections': ['dual-docs'],
                'collectionPaths': {'dual-docs': 'docs'},
                'collectionRoles': {'dual-docs': 'raw'},
                'compile': {'mode': 'auto-wiki',
                            'triggers': ['post_sync_source', 'session_start_source']},
            }))
            for session in (False, True):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    assert sync.run(str(dual), session_start=session, json_output=True) == 0
                assert json.loads(output.getvalue())['compileQueued'] == 1
            queue = dual / '.auto-context' / 'compile' / 'source-queue.jsonl'
            records = [json.loads(line) for line in queue.read_text().splitlines()]
            assert [row['source']['path'] for row in records] == ['docs/same.md'] * 2
            assert [row['trigger'] for row in records] == ['post_sync_source', 'session_start_source']
    finally:
        os.environ.clear(); os.environ.update(original_env)
print('sessionstart-sync-ok')

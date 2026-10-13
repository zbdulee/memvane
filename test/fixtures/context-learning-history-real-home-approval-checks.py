"""Real-account activation needs an owner approval bound to one exact switch.

The process HOME is a disposable directory, so Path.home() is the "real
account" this module guards without touching the developer's actual home.
Every refusal must leave the account untouched: no pointer, no sealed
state, legacy files at their original modes, no fence triggers.
"""
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import sys
import tempfile
from unittest.mock import patch

real_parent = Path(tempfile.mkdtemp(prefix='qmd-history-real-home-', dir='/tmp')).resolve()
os.environ['HOME'] = str(real_parent)
# Do not inherit the developer's sandbox/headless signals.
DENIALS = ('QMD_SANDBOX', 'GEMINI_SANDBOX', 'CLAUDE_SANDBOX', 'CODEX_SANDBOX',
           'HERMES_SANDBOX', 'CLAUDE_HEADLESS', 'HERMES_HEADLESS')
for name in DENIALS:
    os.environ.pop(name, None)
assert Path.home().resolve() == real_parent

from context_learning.contracts import digest, request
from context_learning.history_activation import (
    HOST_FILES, REAL_HOME_APPROVAL_SCHEMA, activate, rollback, selected_database)
import context_learning.history_activation as activation_module
import context_learning.history_lifecycle as lifecycle_module
from context_learning.history_lifecycle import acknowledge_host, request as lifecycle_request
from context_learning.history_migration import prepare
from context_learning.shared import _db
from context_learning.storage_layout import register_project, root as learning_root
from context_learning.store import canonical, database


def rejects(action, reason, kind=ValueError):
    try:
        action()
    except kind as error:
        assert reason in str(error), str(error)
    else:
        raise AssertionError(f'expected {reason}')


try:
    home = Path.home().resolve()
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
    settings.write_text(canonical({
        'contextLearning': {'capture': True, 'stateRoot': str(private_parent)},
        'sharedLearning': {'enabled': True, 'root': str(shared)}}))
    project_id = register_project(project, story_root=story,
                                  display_name='Approved story')
    sample = request({'prompt': 'Which card?'}, [{'candidate_id': 'card-1',
        'revision_sha256': 'e'*64, 'eligible': True, 'excerpt': 'body'}],
        host='codex', request_id='req-1')
    body = canonical(sample)
    with _db(shared) as db:
        db.execute('INSERT INTO cases VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   (digest(str(project)), str(project), 'req-1', digest(body), body,
                    canonical({'card-1': 'body'}), 'b'*64, 1.0, 'train', '[]',
                    'active', 'gold', '{}', '{}',
                    canonical({'selected_ids': ['card-1']}), None, None))
    with database(private) as db:
        db.execute('INSERT INTO samples VALUES (?,?,?)',
                   ('req-1', sample['schema_version'], body))
        db.execute('INSERT INTO compact_inputs VALUES (?,?,?)',
                   ('req-1', 'card-1', canonical({'body_text': 'body',
                    'body_sha256': digest('body'), 'source_sha256': 'e'*64})))
    shared_db = shared / 'shared-learning.sqlite3'
    private_db = private / 'learning.sqlite3'
    # Real private stores are 0644; the seal must restore exactly that.
    private_db.chmod(0o644)
    original_modes = (stat.S_IMODE(shared_db.stat().st_mode),
                      stat.S_IMODE(private_db.stat().st_mode))
    generation = prepare(project_id, shared_db, private_db)['generation']
    base = learning_root()
    pointer = base / 'history-active.json'
    state = base / 'history-state.json'

    source = Path(__file__).resolve().parents[2]
    hosts = {}
    for host in ('claude', 'codex'):
        root = home / ('.claude' if host == 'claude' else '.codex') / \
            'plugins/cache/fake/qmd-auto-context/v-next'
        for relative in HOST_FILES:
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source / relative, target)
        hosts[host] = [root]
    # The real account discovers hosts through their CLIs; pin the result.
    discover = patch.object(activation_module, '_discover_host_roots',
                            return_value={k: list(v) for k, v in hosts.items()})
    discover.start()

    def new_request():
        value = lifecycle_request(generation)
        for engine, roots in hosts.items():
            assert acknowledge_host(engine, roots[0]) is True
        return value

    def approval(nonce, **overrides):
        value = {'schema': REAL_HOME_APPROVAL_SCHEMA, 'generation': generation,
                 'project_id': project_id, 'learning_root': str(base),
                 'source_shared': str(shared_db), 'source_private': str(private_db),
                 'request_nonce': nonce, 'old_host_sessions_restarted': True}
        value.update(overrides)
        return value

    def triggers(path):
        with sqlite3.connect(Path(path).as_uri() + '?mode=ro', uri=True) as db:
            return db.execute("SELECT count(*) FROM sqlite_master "
                              "WHERE type='trigger'").fetchone()[0]

    def assert_untouched():
        assert not pointer.exists() and not pointer.is_symlink()
        if state.exists():
            assert '"phase":"inactive"' in state.read_text(), state.read_text()
        assert (stat.S_IMODE(shared_db.stat().st_mode),
                stat.S_IMODE(private_db.stat().st_mode)) == original_modes
        assert triggers(shared_db) == 0 and triggers(private_db) == 0
        assert selected_database(shared_db) == shared_db

    lifecycle = new_request()
    nonce = lifecycle['nonce']

    # 1. Default deny: the real account without an approval is refused.
    rejects(lambda: activate(generation), 'history_legacy_ddl_unfenced')
    rejects(lambda: activate(generation, home=home), 'history_legacy_ddl_unfenced')
    assert_untouched()
    # 1b. Another spelling of the same directory is still the real account.
    alias = Path(tempfile.mkdtemp(prefix='qmd-history-alias-', dir='/tmp')).resolve()
    try:
        (alias / 'home').symlink_to(home)
        rejects(lambda: activate(generation, home=alias / 'home'),
                'history_legacy_ddl_unfenced')
        rejects(lambda: activate(generation, home=str(home) + '/'),
                'history_legacy_ddl_unfenced')
        rejects(lambda: activate(generation, home=home / 'novel' / '..'),
                'history_legacy_ddl_unfenced')
        upper = Path(str(home).upper())
        if upper.exists():  # case-insensitive filesystem (APFS default)
            rejects(lambda: activate(generation, home=upper),
                    'history_legacy_ddl_unfenced')
        # HOME is caller-controlled: the OS account home is real regardless.
        import pwd
        from types import SimpleNamespace
        with patch.object(pwd, 'getpwuid',
                          return_value=SimpleNamespace(pw_dir=str(home))), \
                patch.dict(os.environ, {'HOME': str(alias)}):
            assert Path.home().resolve() == alias
            rejects(lambda: activate(generation, home=home),
                    'history_legacy_ddl_unfenced')
    finally:
        shutil.rmtree(alias)
    assert_untouched()

    # 2. Out of scope: an approval never applies to a disposable home.
    other = Path(tempfile.mkdtemp(prefix='qmd-history-other-', dir='/tmp')).resolve()
    try:
        rejects(lambda: activate(generation, home=other,
                                 real_home_approval=approval(nonce)),
                'history_real_home_approval_out_of_scope')
    finally:
        shutil.rmtree(other)
    assert_untouched()

    # 3. A sandboxed run is the environment refusing; approval cannot lift it.
    for name in DENIALS:
        with patch.dict(os.environ, {name: '1'}):
            rejects(lambda: activate(generation, real_home_approval=approval(nonce)),
                    'history_real_home_sandboxed')
        assert_untouched()
    with patch.object(sys, 'argv', [sys.argv[0], '--sandbox']):
        rejects(lambda: activate(generation, real_home_approval=approval(nonce)),
                'history_real_home_sandboxed')
    assert_untouched()

    # 4. Malformed approvals.
    malformed = ['yes', [], {},
                 approval(nonce, schema='qmd-history-real-home-approval-v0'),
                 approval(nonce, old_host_sessions_restarted=False),
                 approval(nonce, old_host_sessions_restarted='true'),
                 approval(nonce, generation=''),
                 {**approval(nonce), 'scope': 'all-projects'}]
    missing = approval(nonce)
    del missing['request_nonce']
    malformed.append(missing)
    for value in malformed:
        rejects(lambda: activate(generation, real_home_approval=value),
                'history_real_home_approval_invalid')
        assert_untouched()

    # 5. Approval for a different target or scope is refused before any change.
    backup = shared / 'shared-learning.backup.sqlite3'
    shutil.copy2(shared_db, backup)
    mismatched = [
        approval(nonce, generation='g-' + '0'*32),
        approval(nonce, project_id='00000000-0000-0000-0000-000000000000'),
        approval(nonce, learning_root=str(home / 'elsewhere')),
        approval(nonce, source_shared=str(backup)),
        approval(nonce, source_private=str(shared_db)),
        approval(nonce, source_shared=str(shared / '..' / 'shared' / 'other.sqlite3')),
        approval(nonce, request_nonce='0'*32),
    ]
    for value in mismatched:
        rejects(lambda: activate(generation, real_home_approval=value),
                'history_real_home_approval_mismatch')
        assert_untouched()
    backup.unlink()

    # 5b. A new lifecycle request issued after the approval invalidates it,
    # including one swapped in between the first check and the writer lock.
    real_verify = lifecycle_module.verify_acks
    calls = []

    def swapped(*args, **kwargs):
        calls.append(1)
        value = real_verify(*args, **kwargs)
        return value if len(calls) == 1 else {**value, 'nonce': 'f'*32}

    with patch.object(lifecycle_module, 'verify_acks', side_effect=swapped):
        rejects(lambda: activate(generation, real_home_approval=approval(nonce)),
                'history_real_home_approval_mismatch')
    assert len(calls) == 2
    assert_untouched()

    # 6. The approval does not relax any other check.
    with patch.object(activation_module, '_legacy_writer_pids', return_value={4242}):
        rejects(lambda: activate(generation, real_home_approval=approval(nonce)),
                'history_legacy_writer_open')
    assert_untouched()
    with patch.object(activation_module.subprocess, 'run',
                      side_effect=FileNotFoundError('lsof')):
        rejects(lambda: activate(generation, real_home_approval=approval(nonce)),
                'history_legacy_writer_probe_unavailable')
    assert_untouched()
    # An OS permission denial propagates unchanged and is unwound, not retried.
    real_fchmod = os.fchmod
    seal_attempts = []

    def denied(fd, mode):
        if mode == activation_module.SEALED_MODE:
            seal_attempts.append(mode)
            raise PermissionError(1, 'Operation not permitted')
        return real_fchmod(fd, mode)

    with patch.object(activation_module.os, 'fchmod', side_effect=denied):
        rejects(lambda: activate(generation, real_home_approval=approval(nonce)),
                'Operation not permitted', kind=PermissionError)
    assert len(seal_attempts) == 1
    assert_untouched()

    # 7. Approved: exactly this switch proceeds through every other check.
    # Paths are compared after resolution, so an equivalent spelling binds.
    spelled = approval(nonce, source_shared=str(shared / '..' / 'shared' /
                                                 'shared-learning.sqlite3'))
    selected = activate(generation, real_home_approval=spelled)
    assert selected['generation'] == generation and pointer.is_file()
    assert '"phase":"active"' in state.read_text()
    history = base / 'migrations' / generation / 'history.sqlite3'
    assert selected_database(shared_db) == history
    assert selected_database(private_db) == history
    assert {stat.S_IMODE(p.stat().st_mode) for p in (shared_db, private_db)} == {0o400}
    # The request is consumed: the same approval cannot switch again.
    rollback()
    assert_untouched()
    rejects(lambda: activate(generation, real_home_approval=approval(nonce)),
            'history_lifecycle_request_used')
    assert_untouched()
    # A new request after rollback needs a new approval.
    renewed = new_request()['nonce']
    assert renewed != nonce
    rejects(lambda: activate(generation, real_home_approval=approval(nonce)),
            'history_real_home_approval_mismatch')
    assert_untouched()
    # Changed sources are refused even with a matching approval.
    with _db(shared) as db:
        db.execute("UPDATE cases SET error='after-approval' WHERE request_id='req-1'")
    rejects(lambda: activate(generation, real_home_approval=approval(renewed)),
            'history_')
    assert_untouched()
    discover.stop()
finally:
    for path in real_parent.rglob('*'):
        if path.is_file() and not path.is_symlink():
            path.chmod(0o600)
    shutil.rmtree(real_parent)
print('ok')

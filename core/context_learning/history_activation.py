"""Coordinated, reversible selection of a prepared plugin-owned history.

This protocol is inert until every active host runs the same barrier-aware
writer code. Legacy source files remain intact. A rollback is permitted only
before the selected history receives new writes; later rollback needs a
separate, audited reverse migration.
"""
from __future__ import annotations

from contextlib import contextmanager, closing
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
import tempfile
import time

from .history_migration import _inventory, _regular_owner_file, verify_prepared
from . import history_fence
from .storage_layout import root as learning_root
from .store import canonical


PROTOCOL = 'qmd-history-active-v1'
STATE_PROTOCOL = 'qmd-history-state-v1'
HOSTS = ('claude', 'codex')
# Phases in which the legacy files may be read-only. The state then records
# each file's original mode (restored on unwind, never a hardcoded 0600) and
# the schema digest taken under the fence transaction.
SEALED_PHASES = ('sealing', 'activating', 'active', 'rollback_pending')
SEALED_MODE = 0o400
_PLUGIN_ROOT = Path(__file__).resolve().parents[2]
HOST_FILES = tuple(sorted({str(path.relative_to(_PLUGIN_ROOT)) for pattern in
                           ('core/*.py', 'core/*.sh', 'core/context_learning/*.py',
                            'hooks/run-hook', 'hooks/*.json',
                            '.claude-plugin/*.json', '.codex-plugin/*.json')
                           for path in _PLUGIN_ROOT.glob(pattern) if path.is_file()}))


def _owner_lock(path):
    path = Path(path)
    if not path.parent.is_dir() or path.parent.is_symlink():
        raise ValueError('history_lock_parent_unsafe')
    info = path.parent.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError('history_lock_parent_unsafe')
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_nlink != 1 or info.st_mode & 0o077):
        os.close(fd)
        raise ValueError('history_lock_unsafe')
    return fd


def _lock_path(database):
    return Path(database).parent / '.qmd-history-writer.lock'


@contextmanager
def _locks(databases, *, exclusive, timeout=5):
    # Callers nesting shared and private access enter shared first. The
    # activation pair follows that same order to avoid a lock cycle.
    paths = list(dict.fromkeys(_lock_path(path) for path in databases))
    held = []
    deadline = time.monotonic() + timeout
    try:
        for path in paths:
            fd = _owner_lock(path)
            held.append(fd)
            while True:
                try:
                    fcntl.flock(fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
                                | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise ValueError('history_writer_busy')
                    time.sleep(.02)
        yield
    finally:
        for fd in reversed(held):
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


@contextmanager
def writer_access(legacy_database, *, home=None):
    """Hold a shared barrier through the caller's complete DB transaction."""
    legacy_database = Path(legacy_database)
    account = Path(home or Path.home()).resolve()
    if account not in legacy_database.parents:
        yield legacy_database
        return
    with _locks((legacy_database,), exclusive=False):
        yield selected_database(legacy_database, home=home)


@contextmanager
def read_database(legacy_database, *, home=None, timeout=.02):
    """Read the selected store while holding the same cutover barrier."""
    with writer_access(legacy_database, home=home) as selected:
        with closing(sqlite3.connect(selected.as_uri() + '?mode=ro',
                                     uri=True, timeout=timeout)) as db:
            yield db


def _pointer(home=None):
    return learning_root(home) / 'history-active.json'


def _state_path(home=None):
    return learning_root(home) / 'history-state.json'


def _read_state(home=None):
    path = _state_path(home)
    if path.is_symlink():
        raise ValueError('unsafe_history_state')
    if not path.exists():
        return None
    path = _regular_owner_file(path)
    if path.stat().st_mode & 0o077:
        raise ValueError('unsafe_history_state')
    value = json.loads(path.read_text())
    if (not isinstance(value, dict) or not isinstance(value.get('phase'), str)
            or set(value) != ({'schema', 'generation', 'phase', 'seal'}
                              if value['phase'] in SEALED_PHASES else
                              {'schema', 'generation', 'phase'})
            or value['schema'] != STATE_PROTOCOL
            or value['phase'] not in ('fencing', 'inactive') + SEALED_PHASES
            or not isinstance(value['generation'], str)
            or not re.fullmatch(r'g-[0-9a-f]{32}', value['generation'])
            or ('seal' in value and not _valid_seal(value['seal']))):
        raise ValueError('invalid_history_state')
    return value


def _valid_seal(seal):
    return (isinstance(seal, dict) and set(seal) == {'modes', 'schema'}
            and isinstance(seal['modes'], list) and len(seal['modes']) == 2
            and all(type(mode) is int and 0 <= mode <= 0o777 for mode in seal['modes'])
            and isinstance(seal['schema'], list) and len(seal['schema']) == 2
            and all(isinstance(item, str) and re.fullmatch(r'[0-9a-f]{64}', item)
                    for item in seal['schema']))


def _write_state(generation, phase, *, home=None, seal=None):
    if (phase in SEALED_PHASES) != (seal is not None):
        raise ValueError('invalid_history_state')
    value = {'schema': STATE_PROTOCOL, 'generation': generation, 'phase': phase}
    if seal is not None:
        value['seal'] = seal
    _atomic_file(_state_path(home), value)


def _read_pointer(home=None):
    path = _pointer(home)
    if path.is_symlink():
        raise ValueError('unsafe_history_pointer')
    state = _read_state(home)
    if not path.exists():
        if state is not None and state['phase'] != 'inactive':
            raise ValueError('history_pointer_missing_after_activation')
        return None
    path = _regular_owner_file(path)
    if path.stat().st_mode & 0o077:
        raise ValueError('unsafe_history_pointer')
    value = json.loads(path.read_text())
    if (not isinstance(value, dict) or set(value) !=
            {'schema', 'generation', 'project_id', 'manifest_sha256',
             'source_shared', 'source_private'}
            or value['schema'] != PROTOCOL
            or not isinstance(value['generation'], str)
            or not re.fullmatch(r'g-[0-9a-f]{32}', value['generation'])
            or not isinstance(value['project_id'], str)):
        raise ValueError('invalid_history_pointer')
    if (state is None or state['generation'] != value['generation']
            or state['phase'] == 'inactive'):
        raise ValueError('history_pointer_state_mismatch')
    return value


def _manifest_for(value, *, home=None):
    base = learning_root(home)
    generation = value['generation']
    path = base / 'migrations' / generation / 'prepared.json'
    raw = _regular_owner_file(path).read_bytes()
    if hashlib.sha256(raw).hexdigest() != value['manifest_sha256']:
        raise ValueError('history_manifest_changed')
    manifest = json.loads(raw)
    if (manifest.get('generation') != generation
            or manifest.get('project_id') != value['project_id']
            or manifest.get('source_shared') != value['source_shared']
            or manifest.get('source_private') != value['source_private']):
        raise ValueError('history_pointer_scope_changed')
    target = _regular_owner_file(path.with_name('history.sqlite3'))
    if target.stat().st_mode & 0o077:
        raise ValueError('unsafe_history_database')
    return manifest, target


def selected_database(legacy_database, *, home=None):
    """Resolve one legacy writer to the canonical file, failing closed."""
    legacy_database = Path(legacy_database)
    account = Path(home or Path.home()).resolve()
    if account not in legacy_database.parents:
        return legacy_database
    pointer = _read_pointer(home)
    if pointer is None:
        return legacy_database
    manifest, target = _manifest_for(pointer, home=home)
    if str(legacy_database) in (manifest['source_shared'], manifest['source_private']):
        return target
    return legacy_database


def active_mapping(db, *, home=None):
    """Return the selected history's registry scope, if this is the legacy DB."""
    row = db.execute("SELECT value FROM history_metadata WHERE key='generation'").fetchone() \
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND "
                      "name='history_metadata'").fetchone() else None
    if row is None:
        return None
    pointer = _read_pointer(home)
    if pointer is None or pointer['generation'] != row[0]:
        raise ValueError('history_mapping_pointer_mismatch')
    from .storage_layout import project_view
    view = project_view(pointer['project_id'], home=home)
    if view is None or view['current_root'] is None:
        raise ValueError('history_mapping_project_missing')
    return pointer['project_id'], view['current_root'], view['story_root']


def record_case_mapping(db, project, sample, *, home=None):
    """Atomically map a new canonical case after its ticket is bound."""
    mapping = active_mapping(db, home=home)
    if mapping is None:
        return
    project_id, registered_root, registered_story = mapping
    if registered_root != str(Path(project).resolve()):
        raise ValueError('history_case_project_mismatch')
    edit = sample.get('context_origin') == 'tool_edit'
    story_scope = None
    if edit:
        ticket = db.execute(
            'SELECT story_root FROM edit_tickets WHERE request_id=? AND project_root=?',
            (sample['request_id'], registered_root)).fetchone()
        if not registered_story or ticket != (registered_story,):
            raise ValueError('history_case_story_missing')
        story_scope = registered_story
    from .contracts import digest
    project_key = digest(registered_root)
    db.execute('INSERT INTO case_projects VALUES (?,?,?,?,?)',
               (project_key, sample['request_id'], project_id,
                registered_root, story_scope))
    if story_scope:
        db.execute('UPDATE sample_projects SET story_root=? WHERE request_id=? '
                   'AND project_id=? AND project_root=? AND story_root IS NULL',
                   (story_scope, sample['request_id'], project_id, registered_root))


def _discover_host_roots(account):
    """Use host installation records, not arbitrary cached plugin folders."""
    try:
        listed = subprocess.run(('codex', 'plugin', 'list', '--json'),
                                capture_output=True, text=True, timeout=10,
                                check=False)
        if listed.returncode != 0:
            raise ValueError('history_codex_inventory_unavailable')
        codex = json.loads(listed.stdout)
        entries = [item for item in codex['installed']
                   if item.get('name') == 'qmd-auto-context'
                   and item.get('installed') is True
                   and item.get('enabled') is True]
        codex_roots = [account / '.codex/plugins/cache' / item['marketplaceName']
                       / 'qmd-auto-context' / item['version'] for item in entries]
        claude_registry = account / '.claude/plugins/installed_plugins.json'
        registry_info = claude_registry.stat()
        if (any(part.is_symlink() for part in (claude_registry, *claude_registry.parents))
                or not stat.S_ISREG(registry_info.st_mode)
                or registry_info.st_uid != os.getuid() or registry_info.st_nlink != 1):
            raise ValueError('history_claude_inventory_unsafe')
        claude = json.loads(claude_registry.read_text())
        claude_roots = [Path(item['installPath'])
                        for key, values in claude['plugins'].items()
                        if key.startswith('qmd-auto-context@')
                        for item in values]
    except (OSError, KeyError, TypeError, ValueError, subprocess.TimeoutExpired) as exc:
        raise ValueError('history_host_inventory_unavailable') from exc
    return {'codex': codex_roots, 'claude': claude_roots}


def _installed_hosts_compatible(host_roots=None, *, home=None):
    account = Path(home or Path.home()).resolve()
    if account == Path.home().resolve():
        discovered = _discover_host_roots(account)
        if host_roots is not None and {
                key: sorted(map(str, value if isinstance(value, (list, tuple)) else [value]))
                for key, value in host_roots.items()} != {
                key: sorted(map(str, value)) for key, value in discovered.items()}:
            raise ValueError('history_host_inventory_changed')
        host_roots = discovered
    if not isinstance(host_roots, dict) or set(host_roots) != set(HOSTS):
        raise ValueError('history_host_inventory_incomplete')
    base = Path(__file__).resolve().parents[2]
    for host in HOSTS:
        roots = host_roots[host]
        if not isinstance(roots, (list, tuple)):
            roots = [roots]
        if not roots:
            raise ValueError('history_host_not_coordinated:' + host)
        for raw in roots:
            path = Path(raw)
            cache = account / ('.claude' if host == 'claude' else '.codex') / 'plugins/cache'
            if (not path.is_absolute() or any(item.is_symlink() for item in (path, *path.parents))
                    or cache not in path.parents or not path.is_dir()):
                raise ValueError('history_host_not_coordinated:' + host)
            for name in HOST_FILES:
                installed = path / name
                source = base / name
                if (not installed.is_file() or installed.is_symlink()
                        or hashlib.sha256(installed.read_bytes()).digest()
                        != hashlib.sha256(source.read_bytes()).digest()):
                    raise ValueError('history_host_not_coordinated:' + host)
    return {host: list(host_roots[host] if isinstance(host_roots[host],
                                                     (list, tuple)) else
                       [host_roots[host]]) for host in HOSTS}


def _atomic_file(path, value):
    raw = (canonical(value) + '\n').encode()
    fd, temporary = tempfile.mkstemp(prefix='.history-active-', dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'wb') as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _atomic_pointer(value, *, home=None):
    _atomic_file(_pointer(home), value)


def _source_tables(manifest):
    return (manifest['shared_tables'], manifest['private_tables'])


def _verify_source_pair(databases, manifest):
    for db, expected in zip(databases, _source_tables(manifest)):
        if _inventory(db) != expected:
            raise ValueError('history_sources_changed_under_sqlite_lock')


def _verify_fences(paths, manifest):
    for path, tables in zip(paths, _source_tables(manifest)):
        with closing(sqlite3.connect(Path(path).as_uri() + '?mode=ro',
                                     uri=True)) as db:
            history_fence.verify(db, tables)


def schema_digest(db):
    """Digest of the complete SQLite schema (tables, indexes, triggers, views).

    The fence covers DML on manifest tables only; an old writer can still run
    DDL (CREATE TABLE IF NOT EXISTS ...). Any schema change, including a new
    table, changes this value.
    """
    rows = sorted(canonical(list(row)) for row in db.execute(
        'SELECT type,name,tbl_name,sql FROM sqlite_master'))
    version = db.execute('PRAGMA user_version').fetchone()[0]
    return hashlib.sha256(canonical({'rows': rows, 'user_version': version})
                          .encode('utf8')).hexdigest()


def _read_only(path):
    return closing(sqlite3.connect(Path(path).as_uri() + '?mode=ro', uri=True))


def _file_mode(path):
    info = os.lstat(path)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError('unsafe_legacy_database')
    return stat.S_IMODE(info.st_mode)


def _set_modes(paths, modes):
    """Change only the legacy files' modes (never their directories, whose
    policy/queue files other workers still write), then make it durable."""
    for path, mode in zip(paths, modes):
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_nlink != 1):
                raise ValueError('unsafe_legacy_database')
            os.fchmod(fd, mode)
            os.fsync(fd)
        finally:
            os.close(fd)
    for directory in dict.fromkeys(str(Path(path).parent) for path in paths):
        directory_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)


def _writer_pids_from_lsof(returncode, stdout, stderr, own_pid):
    """Parse `lsof -F pan`; return other pids holding a file read-write.

    Anything not understood fails closed as probe-unavailable.
    """
    unavailable = ValueError('history_legacy_writer_probe_unavailable')
    # lsof exits 1 when any named file has no opener (so also when none of
    # them has one). With -w, any stderr means it could not inspect fully.
    if returncode not in (0, 1) or stderr.strip():
        raise unavailable
    if not stdout.strip():
        if returncode == 1:
            return set()
        raise unavailable
    pid = None
    open_fd = False
    writers = set()
    fd = None
    for line in stdout.splitlines():
        if not line:
            continue
        tag, value = line[0], line[1:]
        if tag == 'p':
            if open_fd or not value.isdigit():
                raise unavailable
            pid = int(value)
        elif tag == 'f':
            if pid is None or open_fd:
                raise unavailable
            open_fd = True
            fd = value
        elif tag == 'a':
            if not open_fd:
                raise unavailable
            open_fd = False
            # A memory mapping (fd 'txt'/'mem', e.g. SQLite mmap_size>0) has
            # no access mode. It is not a write descriptor; any other blank
            # or unknown mode still fails closed.
            if value.strip() == '' and fd in ('txt', 'mem'):
                continue
            if value not in ('r', 'w', 'u'):
                raise unavailable
            if value in ('w', 'u') and pid != own_pid:
                writers.add(pid)
        elif tag == 'n':
            if pid is None:
                raise unavailable
        else:
            raise unavailable
    if pid is None or open_fd:
        raise unavailable
    return writers


def _legacy_writer_pids(paths):
    """Other processes holding a legacy DB open read-write (pre-seal fds)."""
    try:
        result = subprocess.run(('lsof', '-w', '-F', 'pan', '--',
                                 *map(str, paths)),
                                capture_output=True, text=True, timeout=30,
                                check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError('history_legacy_writer_probe_unavailable') from exc
    return _writer_pids_from_lsof(result.returncode, result.stdout,
                                  result.stderr, os.getpid())


def _verify_seal(paths, seal):
    if seal is None:
        raise ValueError('history_legacy_unsealed')
    if any(_file_mode(path) != SEALED_MODE for path in paths):
        raise ValueError('history_legacy_unsealed')
    for path, expected in zip(paths, seal['schema']):
        with _read_only(path) as db:
            if schema_digest(db) != expected:
                raise ValueError('history_legacy_schema_changed')


def _remove_partial_fences(paths, manifest, *, timeout=5):
    with history_fence.exclusive_pair(paths, timeout=timeout) as databases:
        for db, tables in zip(databases, _source_tables(manifest)):
            history_fence.remove_partial(db, tables)


def activate(generation, *, host_roots=None, home=None, lock_timeout=5):
    """Select a fresh sealed snapshot only while both writers are barred."""
    if not isinstance(generation, str) or not re.fullmatch(r'g-[0-9a-f]{32}', generation):
        raise ValueError('invalid_generation')
    # Old already-running host code can issue CREATE TABLE against a legacy
    # connection after the snapshot. SQLite DML triggers cannot fence DDL.
    # Keep real-account activation closed until a complete old-writer boundary
    # is proven; disposable homes still exercise the transition protocol.
    if Path(home or Path.home()).resolve() == Path.home().resolve():
        raise ValueError('history_legacy_ddl_unfenced')
    if _read_pointer(home) is not None:
        raise ValueError('history_already_active')
    from .history_lifecycle import pause_workers, verify_acks
    roots = _installed_hosts_compatible(host_roots, home=home)
    verify_acks(generation, roots, home=home)
    base = learning_root(home)
    manifest_path = base / 'migrations' / generation / 'prepared.json'
    manifest_raw = _regular_owner_file(manifest_path).read_bytes()
    manifest = json.loads(manifest_raw)
    paths = (manifest['source_shared'], manifest['source_private'])
    account = Path(home or Path.home()).resolve()
    if any(account not in Path(path).parents for path in paths):
        raise ValueError('history_project_outside_account')
    with pause_workers(home=home, timeout=lock_timeout):
        with _locks(paths, exclusive=True, timeout=lock_timeout):
            roots = _installed_hosts_compatible(host_roots, home=home)
            verified_request = verify_acks(generation, roots, home=home)
            if _read_pointer(home) is not None:
                raise ValueError('history_already_active')
            verified = verify_prepared(generation, home=home)
            if not verified['sources_current']:
                raise ValueError('history_sources_changed')
            target = _regular_owner_file(manifest_path.with_name('history.sqlite3'))
            with closing(sqlite3.connect(target.as_uri() + '?mode=ro', uri=True)) as db:
                if db.execute('PRAGMA foreign_key_check').fetchone() is not None:
                    raise ValueError('history_foreign_key_mismatch')
                if _inventory(db) != manifest['history_tables']:
                    raise ValueError('history_changed_under_barrier')
            pointer = {'schema': PROTOCOL, 'generation': generation,
                       'project_id': manifest['project_id'],
                       'manifest_sha256': hashlib.sha256(manifest_raw).hexdigest(),
                       'source_shared': paths[0], 'source_private': paths[1]}
            _write_state(generation, 'fencing', home=home)
            with history_fence.exclusive_pair(paths, timeout=lock_timeout) as databases:
                _verify_source_pair(databases, manifest)
                for source_db, tables in zip(databases, _source_tables(manifest)):
                    history_fence.install(source_db, tables)
                # Taken inside the fence transaction: the exact schema the
                # sealed files must still have (fence triggers included).
                fenced_schema = [schema_digest(db) for db in databases]
            _verify_fences(paths, manifest)
            # Seal: DML triggers cannot stop old host code's DDL, so the legacy
            # files become read-only before the pointer is published. Original
            # modes are recorded first so any unwind restores exactly them.
            seal = {'modes': [_file_mode(path) for path in paths],
                    'schema': fenced_schema}
            _write_state(generation, 'sealing', home=home, seal=seal)
            try:
                _set_modes(paths, (SEALED_MODE, SEALED_MODE))
                # chmod does not revoke an fd opened read-write before it.
                if _legacy_writer_pids(paths):
                    raise ValueError('history_legacy_writer_open')
                _verify_seal(paths, seal)
                for path, expected in zip(paths, _source_tables(manifest)):
                    with _read_only(path) as db:
                        if _inventory(db) != expected:
                            raise ValueError('history_sources_changed_under_sqlite_lock')
                _write_state(generation, 'activating', home=home, seal=seal)
                from .history_lifecycle import consume_request
                consume_request(generation, expected_nonce=verified_request['nonce'],
                                home=home)
                _atomic_pointer(pointer, home=home)
            except BaseException:
                # Never make legacy files writable while a pointer may exist;
                # that case stays sealed and fenced for recover().
                if not _pointer(home).exists() and not _pointer(home).is_symlink():
                    try:
                        _unseal(paths, manifest, seal, generation, home=home)
                    except Exception:
                        pass  # state still names a sealed phase; recover() unwinds
                raise
            _write_state(generation, 'active', home=home, seal=seal)
            return pointer


def _unseal(paths, manifest, seal, generation, *, home=None):
    """Unwind before pointer publication: modes first (fence removal needs a
    writable file), then our fences, then mark the state inactive."""
    _set_modes(paths, seal['modes'])
    _remove_partial_fences(paths, manifest)
    _write_state(generation, 'inactive', home=home)


def recover(*, host_roots=None, home=None):
    """Validate a restart's selected generation; never silently fall back."""
    state = _read_state(home)
    if (state is not None and state['phase'] in ('fencing', 'sealing', 'activating',
                                                'rollback_pending')
            and not _pointer(home).exists() and not _pointer(home).is_symlink()):
        from .history_lifecycle import pause_workers
        generation = state['generation']
        manifest_path = learning_root(home) / 'migrations' / generation / 'prepared.json'
        manifest = json.loads(_regular_owner_file(manifest_path).read_text())
        paths = (manifest['source_shared'], manifest['source_private'])
        target = _regular_owner_file(manifest_path.with_name('history.sqlite3'))
        with pause_workers(home=home):
            with _locks(paths, exclusive=True):
                if (_read_state(home) != state or _pointer(home).exists()
                        or _pointer(home).is_symlink()):
                    raise ValueError('history_recovery_state_changed')
                if hashlib.sha256(target.read_bytes()).hexdigest() != manifest['history_sha256']:
                    raise ValueError('history_changed_after_activation')
                if 'seal' in state:
                    # Restore the recorded original modes before the fence
                    # removal below opens the files read-write.
                    _set_modes(paths, state['seal']['modes'])
                with history_fence.exclusive_pair(paths) as databases:
                    source_current = all(_inventory(db) == expected for db, expected
                                         in zip(databases, _source_tables(manifest)))
                    for db, tables in zip(databases, _source_tables(manifest)):
                        history_fence.remove_partial(db, tables)
                _write_state(generation, 'inactive', home=home)
                return {'active': False, 'generation': generation,
                        'sources_current': source_current,
                        'recovered_interrupted_phase': state['phase']}
    pointer = _read_pointer(home)
    if pointer is None:
        return {'active': False}
    _installed_hosts_compatible(host_roots, home=home)
    manifest, target = _manifest_for(pointer, home=home)
    paths = (pointer['source_shared'], pointer['source_private'])
    _verify_fences(paths, manifest)
    _verify_seal(paths, (_read_state(home) or {}).get('seal'))
    with closing(sqlite3.connect(target.as_uri() + '?mode=ro', uri=True)) as db:
        if (db.execute('PRAGMA quick_check').fetchone()[0] != 'ok'
                or db.execute('PRAGMA foreign_key_check').fetchone() is not None
                or db.execute('SELECT value FROM history_metadata WHERE key=\'generation\'').fetchone()
                != (pointer['generation'],)):
            raise ValueError('history_restart_integrity_failed')
    return {'active': True, 'generation': pointer['generation'],
            'project_id': manifest['project_id']}


def rollback(*, host_roots=None, home=None):
    """Return to untouched legacy files only if canonical history has no writes."""
    pointer = _read_pointer(home)
    if pointer is None:
        return {'active': False}
    from .history_lifecycle import pause_workers
    _installed_hosts_compatible(host_roots, home=home)
    paths = (pointer['source_shared'], pointer['source_private'])
    with pause_workers(home=home):
        with _locks(paths, exclusive=True):
            if _read_pointer(home) != pointer:
                raise ValueError('history_pointer_changed')
            manifest, target = _manifest_for(pointer, home=home)
            if hashlib.sha256(target.read_bytes()).hexdigest() != manifest['history_sha256']:
                raise ValueError('history_changed_after_activation')
            verified = verify_prepared(pointer['generation'], home=home)
            if not verified['sources_current']:
                raise ValueError('history_legacy_changed')
            _verify_fences(paths, manifest)
            seal = (_read_state(home) or {}).get('seal')
            _verify_seal(paths, seal)
            _write_state(pointer['generation'], 'rollback_pending', home=home,
                         seal=seal)
            path = _pointer(home)
            os.unlink(path)
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            # Only now, with no pointer, may legacy files become writable again.
            _set_modes(paths, seal['modes'])
            _remove_partial_fences(paths, manifest)
            _write_state(pointer['generation'], 'inactive', home=home)
            return {'active': False, 'generation': pointer['generation']}

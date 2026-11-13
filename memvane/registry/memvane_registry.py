"""Selected project registry: a per-user lookup index, never an authority.

One owner-only SQLite file, ``<state base>/registry/projects.sqlite``, lists
the selected main checkouts of the installs under the HOME state bases and
their linked git worktrees. A row is a candidate for lookup only. Every
consumer repeats the existing selected-route proof (route record, install
journal, attestation) before it acts, and a row never grants recall, an
external edit hint, a write or a reselection.

The registry writes only its own file. It never writes a journal, route,
ledger, marker, store, queue or intent, and adds no lock beyond SQLite's own
file locking, no wake kind and no hook action. Writers are the setup commands
after their own success (``after_setup``), and the bounded opportunistic
``reconcile`` of the selected SessionStart hook and of ``snapshot``.

Row states: ``active`` (lookup candidate), ``missing_pending`` (root not
found; out of lookup at once), ``detached`` with a reason (``released`` after
a proved release, ``missing`` after ``MISSING_OBSERVATIONS`` independent
observations and ``MISSING_GRACE_SECONDS``, ``root_moved`` when the root
inode changed) and ``conflict`` (two installs claim one root; neither is a
candidate). A ``missing`` or ``root_moved`` row is a tombstone: an implicit
registration (SessionStart, backfill) never replaces it; an explicit
activation with a fresh route proof does.

Every store is a row-version compare-and-set in a short transaction; file,
git and journal checks run outside any transaction. A raced row is left for
the next reconcile. An absent, corrupt, busy, unsafe or unsupported registry
raises ``RegistryUnavailable`` to readers and is never deleted, recreated or
written.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
import time

SCHEMA = 'memvane-project-registry-v1'
# Inside the existing per-user ``auto-context`` entry of the state base: an
# older bundle counts at most ``registry.types.MAX_STATE_ENTRIES`` state
# base entries before its install-name filter, so the registry adds none.
PARENT = 'auto-context'
DIRECTORY = 'registry'
FILENAME = 'projects.sqlite'
STATUSES = ('active', 'missing_pending', 'detached', 'conflict')
TOMBSTONE_REASONS = ('missing', 'root_moved')
SELECTED_JOURNAL_STATES = ('selected',)
FAILED_SETUP_STATUSES = ('rejected', 'blocked', 'failed_preparing', 'partial',
                         'recovery_required', 'rolled_back_after_activation_failure',
                         'deferred')
REGISTER_ACTIONS = ('activate-index-origin', 'recover-index-origin')
RELEASE_ACTIONS = ('restore-index-origin',)
MISSING_GRACE_SECONDS = 24 * 3600
MISSING_OBSERVATIONS = 2
OBSERVATION_GAP_SECONDS = 60
CLOCK_SKEW_SECONDS = 60
RESCAN_SECONDS = 600
RECONCILE_ROWS = 32
RECONCILE_SECONDS = 2.0
BACKFILL_PAGE = 32
from memvane.registry.types import HOOK_ROWS, HOOK_SECONDS, HOOK_BACKFILL_PAGE, HOOK_BUSY_MS
BUSY_MS = 250
GIT_SECONDS = 2.0
MAX_LINKED = 16
MAX_LISTED_WORKTREES = 256
MAX_BASE_ENTRIES = 4096
MAX_ROWS = 8192
_HEX32 = re.compile(r'[0-9a-f]{32}\Z')
_TABLES = ('meta', 'entries', 'scans')
_DDL = (
    'CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)',
    "CREATE TABLE entries("
    "id INTEGER PRIMARY KEY,"
    "install_id TEXT NOT NULL,"
    "state_root TEXT NOT NULL,"
    "project_root TEXT NOT NULL,"
    "target_root TEXT NOT NULL,"
    "kind TEXT NOT NULL CHECK(kind IN ('main','worktree')),"
    "parent_install TEXT,"
    "incarnation TEXT,"
    "inode INTEGER,"
    "status TEXT NOT NULL CHECK(status IN ('active','missing_pending','detached','conflict')),"
    "reason TEXT CHECK(reason IS NULL OR reason IN ('released','missing','root_moved','withdrawn')),"
    "proof TEXT,"
    "missing_since REAL,"
    "missing_seen INTEGER NOT NULL DEFAULT 0,"
    "missing_last REAL,"
    "observed_at REAL NOT NULL,"
    "version INTEGER NOT NULL DEFAULT 1,"
    "UNIQUE(install_id, target_root))",
    'CREATE INDEX entries_target ON entries(target_root)',
    "CREATE TABLE scans(base TEXT PRIMARY KEY CHECK(base = 'current'),"
    'cursor TEXT, complete_at REAL, pass_started_at REAL)',
)
_FIELDS = {'install_id': 'installId', 'state_root': 'stateRoot', 'project_root': 'projectRoot',
           'target_root': 'targetRoot', 'kind': 'kind', 'parent_install': 'parentInstall',
           'incarnation': 'incarnation', 'inode': 'inode', 'status': 'status',
           'reason': 'reason', 'proof': 'proof', 'missing_since': 'missingSince',
           'missing_seen': 'missingSeen', 'observed_at': 'observedAt', 'version': 'version'}

# The busy wait of every connection of the current call (hooks lower it).
_busy_ms = BUSY_MS
# Linked worktree discovery runs git; a hook's reconcile turns it off.
_worktrees = True

# Test seam: called as ``_before_store(target_root, expected_version)`` after
# a row was observed and proved, immediately before its compare-and-set.
_before_store = None


class RegistryUnavailable(ValueError):
    """The registry cannot be read or written; the reason is the message."""


class _Unproved(ValueError):
    pass


def _fail(reason):
    raise RegistryUnavailable(reason)


@contextmanager
def _busy(ms):
    global _busy_ms
    saved, _busy_ms = _busy_ms, ms
    try:
        yield
    finally:
        _busy_ms = saved


@contextmanager
def _worktree_discovery(enabled):
    global _worktrees
    saved, _worktrees = _worktrees, enabled
    try:
        yield
    finally:
        _worktrees = saved


def _fixture_live_home(home):
    """A test process (``QMD_SETUP_GUARD_FIXTURE``) never touches the account's real registry."""
    if os.environ.get('QMD_SETUP_GUARD_FIXTURE') != '1':
        return False
    try:
        import pwd
        account = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
        return (Path(home) if home is not None else Path.home()).resolve() == account
    except (KeyError, OSError, RuntimeError, ImportError):
        return True


def registry_path(home=None):
    from memvane.kernel import memvane_paths
    return memvane_paths.state_base(home) / PARENT / DIRECTORY / FILENAME


def _owned_dir(path, *, create, private):
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        if not create:
            raise
        try:
            os.mkdir(path, 0o700)
        except FileExistsError:
            pass
        info = os.lstat(path)
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or
            info.st_mode & (0o077 if private else 0o022)):
        _fail('registry_unsafe')


def _directory(home, create):
    """HOME/.local/state/memvane/registry, every component a real owned dir."""
    from memvane.kernel import memvane_paths
    root = Path(home) if home is not None else Path.home()
    if not root.is_absolute() or not root.is_dir():
        _fail('registry_unsafe')
    cursor = root
    for name in (*memvane_paths.STATE_PARTS, PARENT, DIRECTORY):
        parent, cursor = cursor, cursor / name
        if name == PARENT and create and not os.path.lexists(cursor):
            # Creating it is one more state base entry: never past the
            # older bundles' entry limit.
            from memvane.registry.types import MAX_STATE_ENTRIES
            if len(os.listdir(parent)) >= MAX_STATE_ENTRIES:
                _fail('registry_state_base_full')
        _owned_dir(cursor, create=create, private=name == DIRECTORY)
    return cursor


def _check_file(path):
    info = os.lstat(path)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
            info.st_nlink != 1 or info.st_mode & 0o077):
        _fail('registry_unsafe')
    return info


def _classify(exc):
    text = str(exc).lower()
    return 'registry_busy' if ('locked' in text or 'busy' in text) else 'registry_corrupt'


def _require_schema(db, *, initialize):
    names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not names:
        # Only the connection that just created the file initializes it. An
        # existing empty file may have held tombstones: never rebuilt here.
        if not initialize:
            _fail('registry_corrupt')
        for statement in _DDL:
            db.execute(statement)
        db.execute("INSERT INTO meta(key,value) VALUES ('schema',?)", (SCHEMA,))
        return
    if not set(_TABLES) <= names:
        _fail('registry_corrupt')
    row = db.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
    if row is None:
        _fail('registry_corrupt')
    if row[0] != SCHEMA:
        _fail('registry_schema_unsupported')


def _connect(home, *, write, create, busy_ms):
    if _fixture_live_home(home):
        _fail('registry_fixture_live_home')
    busy_ms = _busy_ms if busy_ms is None else busy_ms
    try:
        directory = _directory(home, create=write and create)
    except FileNotFoundError:
        _fail('registry_absent')
    except OSError:
        _fail('registry_unsafe')
    path = directory / FILENAME
    created = False
    try:
        before = _check_file(path)
    except FileNotFoundError:
        if not (write and create):
            _fail('registry_absent')
        try:
            os.close(os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL |
                             getattr(os, 'O_NOFOLLOW', 0), 0o600))
            created = True
        except FileExistsError:
            pass
        before = _check_file(path)
    except OSError:
        _fail('registry_unsafe')
    try:
        # Rollback-journal mode (SQLite's default): a py3.9 hook reads a file
        # a newer python wrote without the WAL read-only trap.
        db = sqlite3.connect(path.as_uri() + '?mode=rw', uri=True,
                             timeout=max(busy_ms, 0) / 1000.0, isolation_level=None)
    except sqlite3.Error as exc:
        raise RegistryUnavailable(_classify(exc)) from exc
    try:
        after = _check_file(path)
        if (before.st_ino, before.st_dev) != (after.st_ino, after.st_dev):
            _fail('registry_unsafe')
        db.row_factory = sqlite3.Row
        if not write:
            db.execute('PRAGMA query_only=1')
        db.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
        try:
            _require_schema(db, initialize=created)
            db.execute('COMMIT')
        except BaseException:
            db.execute('ROLLBACK')
            raise
    except sqlite3.Error as exc:
        db.close()
        raise RegistryUnavailable(_classify(exc)) from exc
    except BaseException:
        db.close()
        raise
    return db


@contextmanager
def _session(home=None, *, write, create=False, busy_ms=None):
    db = _connect(home, write=write, create=create, busy_ms=busy_ms)
    try:
        yield db
    except sqlite3.Error as exc:
        raise RegistryUnavailable(_classify(exc)) from exc
    finally:
        db.close()


@contextmanager
def _transaction(home=None, *, create=False, busy_ms=None):
    with _session(home, write=True, create=create, busy_ms=busy_ms) as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            yield db
            db.execute('COMMIT')
        except BaseException:
            try:
                db.execute('ROLLBACK')
            except sqlite3.Error:
                pass
            raise


def _public(row):
    return {name: row[column] for column, name in _FIELDS.items()}


def _bases(home):
    from memvane.kernel import memvane_paths
    return (('current', memvane_paths.state_base(home)),)


def is_home_install(state, home=None):
    """``state`` is an install root directly under a HOME state base."""
    try:
        return _home_install(Path(state), home)
    except (OSError, RuntimeError, TypeError, ValueError):
        return False


@contextmanager
def _journal_reads(deadline):
    """Registry journal reads: never legacy recovery, busy waits bounded by ``deadline``."""
    from memvane.journal import install_journal as journal
    with journal.read_only_journals(), journal.reader_deadline(deadline):
        yield


def _home_install(state, home):
    """The install lives directly under a HOME state base (never a fixture root)."""
    if not _HEX32.fullmatch(state.name):
        return False
    parents = set()
    for _, base in _bases(home):
        parents.add(base)
        try:
            parents.add(base.resolve())
        except (OSError, RuntimeError):
            pass
    return state.parent in parents


def _canonical(path):
    """``path`` in the file system's spelling, through its nearest existing directory."""
    from memvane.kernel.paths import canonical
    current = Path(os.path.abspath(path))
    rest = []
    while not current.is_dir() and current.parent != current:
        rest.append(current.name)
        current = current.parent
    return Path(canonical(current), *reversed(rest))


def _within(root, path):
    root, path = Path(root), Path(path)
    return path == root or root in path.parents


def _journal_state(state_root):
    from memvane.journal import install_journal as journal
    return journal.inspect_journal(Path(state_root) / 'journal.sqlite')


def _prove_main(project, state=None):
    """The selected route proof of ``project``: candidate, its journal selected.

    A main checkout is classified by its own route record only
    (``_select_route``): the linked-worktree fallback of ``select_route``
    runs git and never classifies a selected main checkout.
    """
    from memvane.route import api as routes
    decision = routes.select_local_route(str(project), action='index')
    if decision.get('state') != 'candidate':
        raise _Unproved('route_' + str(decision.get('state')))
    journal_path = decision.get('journalPath')
    if decision.get('projectRoot') != str(project) or not isinstance(journal_path, str):
        raise _Unproved('route_mismatch')
    proved_state = Path(journal_path).parent
    if Path(journal_path).name != 'journal.sqlite' or (
            state is not None and proved_state != Path(state)):
        raise _Unproved('route_mismatch')
    install = decision.get('installId')
    if not isinstance(install, str) or install != proved_state.name:
        raise _Unproved('install_mismatch')
    meta = _journal_state(proved_state)
    if meta['state'] not in SELECTED_JOURNAL_STATES or meta['projectRoot'] != str(project):
        raise _Unproved('journal_unselected')
    return {'installId': install, 'stateRoot': str(proved_state), 'projectRoot': str(project),
            'incarnation': meta.get('scopeId')}


def _released(project, state):
    """A proved release: no route of this install, and its journal is not selected."""
    from memvane.route import api as routes
    try:
        decision = routes.select_local_route(str(project), action='index')
        route_state = decision.get('state')
        if route_state == 'candidate':
            if decision.get('journalPath') == str(Path(state) / 'journal.sqlite'):
                return False
        elif route_state != 'legacy':
            return False
        return _journal_state(state)['state'] not in SELECTED_JOURNAL_STATES
    except (OSError, RuntimeError, ValueError, KeyError, TypeError):
        return False


def _linked(project, deadline, clock=time.monotonic):
    """``(proved worktree roots, listed roots)`` or None when git cannot answer.

    Each listed worktree costs one route classification (which runs git), so
    the deadline is checked before every one; running out returns None and
    nothing is registered or released.
    """
    from memvane.kernel import paths as git
    from memvane.route import api as routes
    try:
        facts = git.repo_facts(project, deadline=deadline)
        if facts is None:
            return [], set()
        if facts['linked']:
            return None
        relative = Path(project).relative_to(facts['toplevel'])
        out = git.run_git(['worktree', 'list', '--porcelain', '-z'], Path(project),
                          deadline=deadline)
    except (git.GitUnavailable, OSError, RuntimeError, ValueError):
        return None
    listed = [field[len('worktree '):] for field in
              out.stdout.decode('utf-8', 'surrogateescape').split('\0')
              if field.startswith('worktree ')][1:]
    if len(listed) > MAX_LISTED_WORKTREES:
        return None
    roots, proved = set(), []
    for root in listed:
        candidate = Path(os.path.abspath(Path(root) / relative))
        roots.add(str(candidate))
        if len(proved) >= MAX_LINKED:
            continue
        if clock() >= deadline:
            return None
        try:
            if not candidate.is_dir():
                continue
            resolved = candidate.resolve(strict=True)
            roots.add(str(resolved))
            route = routes.select_route(str(resolved), action='index')
        except (OSError, RuntimeError, ValueError):
            continue
        main = route.get('mainRoute')
        if (route.get('state') == 'worktree' and isinstance(main, dict)
                and main.get('projectRoot') == str(project)
                and route.get('worktreeProjectRoot') == str(resolved)):
            proved.append(resolved)
    return proved, roots


def _rows(db, where='', args=(), *, limit=None):
    tail = '' if limit is None else f' LIMIT {int(limit)}'
    return [dict(row) for row in
            db.execute(f'SELECT * FROM entries {where} ORDER BY id{tail}', args)]


def _cas(home, row, values, *, bump=True):
    """Store ``values`` on ``row`` only while its version is unchanged."""
    if bump and _before_store is not None:
        _before_store(row['target_root'], row['version'])
    columns = ','.join(f'{key}=?' for key in values)
    with _transaction(home) as db:
        updated = db.execute(
            f'UPDATE entries SET {columns}{",version=version+1" if bump else ""} '
            'WHERE id=? AND version=?', (*values.values(), row['id'], row['version'])).rowcount
    return 'stored' if updated == 1 else 'raced'


def _detach_children(home, install, reason, now):
    with _session(home, write=True) as db:
        rows = _rows(db, "WHERE install_id=? AND kind='worktree' AND status IN "
                     "('active','missing_pending')", (install,))
    for row in rows:
        _cas(home, row, {'status': 'detached', 'reason': reason, 'observed_at': now})


def _sync_worktrees(home, proof, now, deadline, *, explicit):
    """Register the proved linked worktrees; release rows git no longer lists.

    Off in a hook's reconcile (``reconcile(worktrees=False)``): git is never
    run there. Setup and ``snapshot`` keep the worktree rows current.
    """
    if not _worktrees:
        return 0
    linked = _linked(Path(proof['projectRoot']), deadline)
    if linked is None:
        return 0
    proved, listed = linked
    install = proof['installId']
    with _session(home, write=True) as db:
        existing = {row['target_root']: row for row in _rows(
            db, "WHERE install_id=? AND kind='worktree'", (install,))}
    count = 0
    for root in proved:
        target = str(_canonical(root))
        try:
            inode = os.stat(root).st_ino
        except OSError:
            continue
        row = existing.get(target)
        values = {'state_root': proof['stateRoot'], 'project_root': proof['projectRoot'],
                  'parent_install': install, 'incarnation': proof['incarnation'],
                  'inode': inode, 'status': 'active', 'reason': None, 'proof': 'ok',
                  'missing_since': None, 'missing_seen': 0, 'missing_last': None,
                  'observed_at': now}
        if row is None:
            with _transaction(home) as db:
                db.execute('INSERT OR IGNORE INTO entries(install_id,target_root,kind,state_root,'
                           'project_root,parent_install,incarnation,inode,status,proof,'
                           "observed_at) VALUES (?,?,'worktree',?,?,?,?,?,'active','ok',?)",
                           (install, target, proof['stateRoot'], proof['projectRoot'], install,
                            proof['incarnation'], inode, now))
            count += 1
        elif row['status'] == 'detached' and row['reason'] in TOMBSTONE_REASONS and not explicit:
            continue
        elif any(row[key] != value for key, value in values.items() if key != 'observed_at'):
            _cas(home, row, values)
            count += 1
    kept = listed | {str(_canonical(root)) for root in proved}
    for target, row in existing.items():
        # Only a successful listing that no longer names the worktree releases it.
        if row['status'] in ('active', 'missing_pending') and target not in kept:
            _cas(home, row, {'status': 'detached', 'reason': 'released', 'observed_at': now})
    return count


def register(project_root, state_root, *, explicit, home=None, now=None, deadline=None):
    """Upsert the main row of a proved selected install; best effort, never raises.

    ``explicit`` (an activation) may replace this install's ``missing`` or
    ``root_moved`` tombstone and another install's tombstone at the same
    root; an implicit registration (SessionStart seed, backfill) never does.
    Another live install at the same root is released when its journal is no
    longer selected; otherwise both rows become ``conflict``. Journal reads
    never recover a journal and end by ``deadline`` (``deferred``).
    """
    deadline = time.monotonic() + GIT_SECONDS if deadline is None else deadline
    with _journal_reads(deadline):
        return _register(project_root, state_root, explicit=explicit, home=home, now=now,
                         deadline=deadline)


def _register(project_root, state_root, *, explicit, home, now, deadline):
    now = time.time() if now is None else float(now)
    try:
        project, state = Path(project_root).resolve(), Path(state_root).resolve()
    except (OSError, RuntimeError, TypeError, ValueError):
        return {'status': 'unproved', 'reason': 'invalid_path'}
    if not _home_install(state, home):
        return {'status': 'skipped', 'reason': 'state_root_outside_home'}
    try:
        proof = _prove_main(project, state)
        inode = os.stat(project).st_ino
        target = str(_canonical(project))
    except _Unproved as exc:
        return {'status': 'unproved', 'reason': str(exc)}
    except (OSError, RuntimeError, ValueError, KeyError, TypeError):
        return {'status': 'unproved', 'reason': 'proof_failed'}
    try:
        with _session(home, write=True, create=True) as db:
            before = _rows(db, 'WHERE target_root=?', (target,))
        others = {row['id']: _journal_selected(row) for row in before
                  if row['install_id'] != proof['installId'] and
                  row['status'] in ('active', 'missing_pending', 'conflict')}
        if time.monotonic() >= deadline:
            # A journal read cut short is no evidence of another selection.
            return {'status': 'deferred'}
        if _before_store is not None:
            mine = next((row for row in before if row['install_id'] == proof['installId']), None)
            _before_store(target, None if mine is None else mine['version'])
        with _transaction(home) as db:
            current = _rows(db, 'WHERE target_root=?', (target,))
            if [(r['id'], r['version']) for r in current] != \
                    [(r['id'], r['version']) for r in before]:
                return {'status': 'raced'}
            status = _decide(db, current, others, proof, target, inode, now, explicit)
    except RegistryUnavailable as exc:
        return {'status': 'unavailable', 'reason': str(exc)}
    if status in ('registered', 'unchanged') and time.monotonic() < deadline:
        try:
            _sync_worktrees(home, proof, now, deadline, explicit=explicit)
        except (RegistryUnavailable, OSError, RuntimeError, ValueError):
            pass
    return {'status': status}


def _journal_selected(row):
    try:
        return _journal_state(row['state_root'])['state'] in SELECTED_JOURNAL_STATES
    except (OSError, RuntimeError, ValueError, KeyError, TypeError):
        return None


def _grace_expired(row, now):
    since = row['missing_since']
    return (row['status'] == 'missing_pending' and since is not None and
            since <= now + CLOCK_SKEW_SECONDS and now - since >= MISSING_GRACE_SECONDS)


def _decide(db, current, others, proof, target, inode, now, explicit):
    install = proof['installId']
    mine = next((row for row in current if row['install_id'] == install), None)
    tombstoned = [row for row in current if row['status'] == 'detached' and
                  row['reason'] in TOMBSTONE_REASONS]
    if not explicit and (mine in tombstoned or (mine is None and tombstoned)):
        return 'tombstoned'
    if not explicit and mine is not None and mine['status'] in ('active', 'missing_pending'):
        if mine['inode'] is not None and mine['inode'] != inode:
            # Same path, another root: the stored identity decides (root_moved),
            # never the implicit proof of whatever lives there now.
            db.execute("UPDATE entries SET status='detached', reason='root_moved', "
                       'observed_at=?, version=version+1 WHERE id=?', (now, mine['id']))
            db.execute("UPDATE entries SET status='detached', reason='root_moved', "
                       "observed_at=?, version=version+1 WHERE install_id=? AND "
                       "kind='worktree' AND status IN ('active','missing_pending')",
                       (now, install))
            return 'root_moved'
        if _grace_expired(mine, now):
            # Past the grace only an explicit registration brings it back.
            return 'held'
    live = [row for row in current if row['id'] in others]
    contested = False
    for row in live:
        if others[row['id']] is False:
            db.execute("UPDATE entries SET status='detached', reason='released', observed_at=?, "
                       'version=version+1 WHERE id=?', (now, row['id']))
        else:
            contested = True
            if row['status'] != 'conflict':
                db.execute("UPDATE entries SET status='conflict', reason=NULL, observed_at=?, "
                           'version=version+1 WHERE id=?', (now, row['id']))
    values = {'state_root': proof['stateRoot'], 'project_root': proof['projectRoot'],
              'incarnation': proof['incarnation'], 'inode': inode,
              'status': 'conflict' if contested else 'active', 'reason': None, 'proof': 'ok',
              'missing_since': None, 'missing_seen': 0, 'missing_last': None}
    if mine is None:
        db.execute('INSERT INTO entries(install_id,target_root,kind,state_root,project_root,'
                   'incarnation,inode,status,proof,observed_at) '
                   "VALUES (?,?,'main',?,?,?,?,?,'ok',?)",
                   (install, target, values['state_root'], values['project_root'],
                    values['incarnation'], inode, values['status'], now))
        return 'conflict' if contested else 'registered'
    if all(mine[key] == value for key, value in values.items()) and mine['kind'] == 'main':
        db.execute('UPDATE entries SET observed_at=? WHERE id=?', (now, mine['id']))
        return 'conflict' if contested else 'unchanged'
    columns = ','.join(f'{key}=?' for key in values)
    db.execute(f"UPDATE entries SET {columns},kind='main',observed_at=?,version=version+1 "
               'WHERE id=?', (*values.values(), now, mine['id']))
    return 'conflict' if contested else 'registered'


def release(project_root, state_root, *, home=None, now=None):
    """Detach this install's rows after a proved release; never inserts a row."""
    with _journal_reads(time.monotonic() + GIT_SECONDS):
        return _release(project_root, state_root, home=home, now=now)


def _release(project_root, state_root, *, home, now):
    now = time.time() if now is None else float(now)
    try:
        project, state = Path(project_root).resolve(), Path(state_root).resolve()
    except (OSError, RuntimeError, TypeError, ValueError):
        return {'status': 'unproved'}
    if not _home_install(state, home):
        return {'status': 'skipped', 'reason': 'state_root_outside_home'}
    try:
        with _session(home, write=True) as db:
            rows = _rows(db, "WHERE install_id=? AND status IN "
                         "('active','missing_pending','conflict')", (state.name,))
        if not rows:
            return {'status': 'unchanged'}
        if not _released(project, state):
            return {'status': 'unproved'}
        outcomes = [_cas(home, row, {'status': 'detached', 'reason': 'released',
                                     'observed_at': now}) for row in rows]
    except RegistryUnavailable as exc:
        return {'status': 'unavailable', 'reason': str(exc)}
    return {'status': 'raced' if 'raced' in outcomes else 'released'}


def after_setup(action, project_root, state_root, result):
    """Reflect a finished setup command; never raises and never changes ``result``."""
    try:
        if (not isinstance(result, dict) or result.get('status') in FAILED_SETUP_STATUSES
                or not project_root or not state_root):
            return None
        if action in REGISTER_ACTIONS:
            if result.get('phase') != 'selected':
                return None
            outcome = register(project_root, state_root, explicit=True)
            if outcome.get('status') != 'skipped':
                reconcile()
            return outcome
        if action in RELEASE_ACTIONS:
            return release(project_root, state_root)
    except Exception:
        return None
    return None


def _missing_values(row, now):
    since, seen, last = row['missing_since'], row['missing_seen'] or 0, row['missing_last']
    if (since is None or since > now + CLOCK_SKEW_SECONDS or
            (last is not None and last > now + CLOCK_SKEW_SECONDS)):
        # First sighting, or a stored time in the future (clock moved back).
        since, seen, last = now, 1, now
    elif last is None or now - last >= OBSERVATION_GAP_SECONDS:
        seen, last = seen + 1, now
    values = {'status': 'missing_pending', 'reason': None, 'missing_since': since,
              'missing_seen': seen, 'missing_last': last, 'observed_at': now}
    if seen >= MISSING_OBSERVATIONS and now - since >= MISSING_GRACE_SECONDS:
        values.update(status='detached', reason='missing')
    return values


def _check_main(home, row, now, deadline):
    """One bounded observation of a live main row; returns an outcome name."""
    try:
        info = os.stat(row['project_root'])
        present = stat.S_ISDIR(info.st_mode)
    except FileNotFoundError:
        present = False
    except OSError:
        return 'unobservable'
    if not present:
        values = _missing_values(row, now)
        outcome = _cas(home, row, values)
        if outcome == 'stored' and values['status'] == 'detached':
            _detach_children(home, row['install_id'], 'missing', now)
        return outcome
    if row['inode'] is not None and info.st_ino != row['inode']:
        outcome = _cas(home, row, {'status': 'detached', 'reason': 'root_moved',
                                   'observed_at': now})
        if outcome == 'stored':
            _detach_children(home, row['install_id'], 'root_moved', now)
        return outcome
    if _grace_expired(row, now):
        # The original root is back after the grace: never reactivated or reset
        # here. Two sightings make it the ``missing`` tombstone it qualified as.
        if (row['missing_seen'] or 0) >= MISSING_OBSERVATIONS:
            outcome = _cas(home, row, {'status': 'detached', 'reason': 'missing',
                                       'observed_at': now})
            if outcome == 'stored':
                _detach_children(home, row['install_id'], 'missing', now)
            return outcome
        return _cas(home, row, {'observed_at': now}, bump=False)
    project = Path(row['project_root'])
    try:
        proof = _prove_main(project)
        if time.monotonic() >= deadline:
            return 'deferred'
        if proof['installId'] != row['install_id'] or not _home_install(
                Path(proof['stateRoot']), home):
            raise _Unproved('install_mismatch')
    except _Unproved as exc:
        if time.monotonic() >= deadline:
            return 'deferred'  # a read cut short by the budget proves nothing
        if _released(project, row['state_root']):
            outcome = _cas(home, row, {'status': 'detached', 'reason': 'released',
                                       'observed_at': now})
            if outcome == 'stored':
                _detach_children(home, row['install_id'], 'released', now)
            return outcome
        return _cas(home, row, {'proof': 'unproved:' + str(exc), 'observed_at': now},
                    bump=row['proof'] != 'unproved:' + str(exc))
    values = {'state_root': proof['stateRoot'], 'incarnation': proof['incarnation'],
              'status': 'active', 'reason': None, 'proof': 'ok', 'missing_since': None,
              'missing_seen': 0, 'missing_last': None}
    changed = any(row[key] != value for key, value in values.items())
    outcome = _cas(home, row, {**values, 'observed_at': now}, bump=changed)
    if outcome == 'stored' and time.monotonic() < deadline:
        try:
            _sync_worktrees(home, proof, now, deadline, explicit=False)
        except (RegistryUnavailable, OSError, RuntimeError, ValueError):
            pass
    return outcome


def _backfill(home, now, page, clock, deadline, git_deadline):
    """Page through the state base with a cursor and completion marker."""
    from memvane.journal import install_journal as journal
    done = 0
    for key, base in _bases(home):
        if done >= page or clock() >= deadline:
            break
        with _session(home, write=True) as db:
            scan = db.execute('SELECT cursor, complete_at, pass_started_at FROM scans '
                              'WHERE base=?', (key,)).fetchone()
        cursor, complete_at = (scan['cursor'], scan['complete_at']) if scan else (None, None)
        if (cursor is None and complete_at is not None and
                0 <= now - complete_at < RESCAN_SECONDS):
            continue
        try:
            names = os.listdir(base)
        except FileNotFoundError:
            names = []
        except OSError:
            continue
        if len(names) > MAX_BASE_ENTRIES:
            continue
        names = sorted(name for name in names if _HEX32.fullmatch(name))
        pending = [name for name in names if cursor is None or name > cursor]
        finished = True
        for name in pending:
            if done >= page or clock() >= deadline:
                finished = False
                break
            done += 1
            cursor = name
            state = base / name
            try:
                info = state.lstat()
                if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                        or info.st_mode & 0o077):
                    continue
                meta = journal.inspect_journal(state / 'journal.sqlite')
                project = meta.get('projectRoot')
                if meta.get('state') in SELECTED_JOURNAL_STATES and isinstance(project, str):
                    register(project, state, explicit=False, home=home, now=now,
                             deadline=git_deadline)
            except Exception:
                continue
        with _transaction(home) as db:
            if finished:
                db.execute('INSERT INTO scans(base,cursor,complete_at,pass_started_at) '
                           'VALUES (?,NULL,?,NULL) ON CONFLICT(base) DO UPDATE SET '
                           'cursor=NULL, complete_at=excluded.complete_at', (key, now))
            else:
                db.execute('INSERT INTO scans(base,cursor,complete_at,pass_started_at) '
                           'VALUES (?,?,NULL,?) ON CONFLICT(base) DO UPDATE SET '
                           'cursor=excluded.cursor', (key, cursor, now))
    return done


def _complete(db):
    found = {row['base']: row['complete_at'] for row in
             db.execute('SELECT base, complete_at FROM scans')}
    return all(found.get(key) is not None for key in ('current',))


def reconcile(*, home=None, now=None, clock=None, max_rows=RECONCILE_ROWS,
              seconds=RECONCILE_SECONDS, backfill_page=BACKFILL_PAGE, seed=None,
              busy_ms=BUSY_MS, worktrees=True):
    """One bounded pass: live main rows from the stored cursor, then backfill.

    Budgets are a row count, a backfill entry count and ``seconds`` on
    ``clock`` (monotonic by default), whichever ends first; every SQLite
    connection waits at most ``busy_ms``. ``worktrees=False`` (the hook) runs
    no git: linked worktree rows are neither added nor released. A failing row
    never stops the next.
    A ``seed`` (project root, state root) outside the HOME state bases skips
    the whole pass, so a fixture install never reconciles the account's
    installs. Never raises; an unavailable registry is reported.
    """
    if seed is not None:
        try:
            outside = not _home_install(Path(seed[1]).resolve(), home)
        except (OSError, RuntimeError, TypeError, ValueError):
            outside = True
        if outside:
            return {'status': 'skipped', 'reason': 'state_root_outside_home', 'checked': 0,
                    'raced': 0, 'backfilled': 0, 'complete': False}
    with _busy(busy_ms), _worktree_discovery(worktrees), \
            _journal_reads(time.monotonic() + max(seconds, 0)):
        return _reconcile(home=home, now=now, clock=clock, max_rows=max_rows,
                          seconds=seconds, backfill_page=backfill_page, seed=seed)


def _reconcile(*, home, now, clock, max_rows, seconds, backfill_page, seed):
    now = time.time() if now is None else float(now)
    clock = clock or time.monotonic
    deadline = clock() + seconds
    git_deadline = time.monotonic() + min(seconds, GIT_SECONDS)
    result = {'status': 'ok', 'checked': 0, 'raced': 0, 'backfilled': 0, 'complete': False}
    try:
        if seed is not None:
            register(seed[0], seed[1], explicit=False, home=home, now=now,
                     deadline=git_deadline)
        with _session(home, write=True, create=True) as db:
            rows = _rows(db, "WHERE kind='main' AND status IN ('active','missing_pending')")
            stored = db.execute("SELECT value FROM meta WHERE key='reconcile_cursor'").fetchone()
        cursor = int(stored[0]) if stored is not None and str(stored[0]).isdigit() else 0
        ordered = [row for row in rows if row['id'] > cursor] + \
                  [row for row in rows if row['id'] <= cursor]
        last = None
        for row in ordered[:max(max_rows, 0)]:
            if clock() >= deadline:
                break
            try:
                outcome = _check_main(home, row, now, git_deadline)
            except Exception:
                outcome = 'error'
            result['checked'] += 1
            result['raced'] += outcome == 'raced'
            last = row['id']
        if last is not None:
            with _transaction(home) as db:
                db.execute("INSERT INTO meta(key,value) VALUES ('reconcile_cursor',?) "
                           'ON CONFLICT(key) DO UPDATE SET value=excluded.value', (str(last),))
        if backfill_page > 0 and clock() < deadline:
            result['backfilled'] = _backfill(home, now, backfill_page, clock, deadline,
                                             git_deadline)
        with _session(home, write=False) as db:
            result['complete'] = _complete(db)
    except RegistryUnavailable as exc:
        return {**result, 'status': 'unavailable', 'reason': str(exc)}
    return result


_reconcile_pass = reconcile


def routing_rows(*, home=None, busy_ms=HOOK_BUSY_MS):
    """Active main rows and the backfill state; raises ``RegistryUnavailable``."""
    with _session(home, write=False, busy_ms=busy_ms) as db:
        rows = _rows(db, limit=MAX_ROWS + 1)
        complete = _complete(db)
    if len(rows) > MAX_ROWS:
        _fail('registry_oversized')
    return {'rows': [_public(row) for row in rows
                     if row['kind'] == 'main' and row['status'] == 'active'],
            'entries': [_public(row) for row in rows], 'complete': complete}


def excluded(view, install_id, project_root):
    """True when a fallback scan result is held out by the registry."""
    for row in view['entries']:
        # Worktree rows describe linked checkouts, never the main selection.
        if row['kind'] != 'main' or (row['projectRoot'] != project_root and
                                     row['targetRoot'] != project_root):
            continue
        if row['installId'] == install_id and row['status'] != 'active':
            return True
        if row['status'] == 'conflict' or (row['status'] == 'detached' and
                                           row['reason'] in TOMBSTONE_REASONS):
            return True
    return False


def lookup(path, *, home=None, busy_ms=HOOK_BUSY_MS):
    """The longest registered root holding ``path``; ties and conflicts are ambiguous."""
    query = _canonical(path)
    with _session(home, write=False, busy_ms=busy_ms) as db:
        rows = _rows(db, "WHERE status IN ('active','conflict')")
    matches = [row for row in rows if _within(row['target_root'], query)]
    if not matches:
        return {'status': 'none'}
    longest = max(len(Path(row['target_root']).parts) for row in matches)
    winners = [row for row in matches if len(Path(row['target_root']).parts) == longest]
    if len(winners) != 1 or winners[0]['status'] != 'active':
        return {'status': 'ambiguous'}
    return {'status': 'owner', 'row': _public(winners[0])}


def snapshot(*, home=None, reconcile=True, now=None):
    """Dashboard view: every row and scan state, after a bounded reconcile."""
    now = time.time() if now is None else float(now)
    if reconcile:
        _reconcile_pass(home=home, now=now)
    try:
        with _session(home, write=False, busy_ms=BUSY_MS) as db:
            rows = _rows(db, limit=MAX_ROWS)
            scans = {row['base']: dict(row) for row in db.execute('SELECT * FROM scans')}
    except RegistryUnavailable as exc:
        reason = str(exc)
        status = 'absent' if reason == 'registry_absent' else (
            'busy' if reason == 'registry_busy' else 'recovery_required')
        return {'status': status, 'reason': reason, 'rows': [], 'scans': {}}
    complete = all(scans.get(key, {}).get('complete_at') is not None
                   for key in ('current',))
    stale = not complete or any(
        now - (scans[key]['complete_at'] or 0) > 2 * RESCAN_SECONDS for key in scans)
    return {'status': 'ok', 'complete': complete, 'stale': stale,
            'scans': {key: {'completeAt': value['complete_at'], 'cursor': value['cursor']}
                      for key, value in scans.items()},
            'rows': [_public(row) for row in rows]}


if __name__ == '__main__':
    if sys.argv[1:] != ['status']:
        print('usage: memvane_registry.py status', file=sys.stderr)
        raise SystemExit(2)
    print(json.dumps(snapshot(reconcile=False), sort_keys=True, ensure_ascii=False))

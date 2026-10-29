"""Move an install's private state from ``~/.local/state/laya`` to the MemVane base.

Installs onboarded before the rename keep their state in
``~/.local/state/laya/<install id>``. The bundle upgrade worker moves each
selected install to ``~/.local/state/memvane/<install id>`` with one
same-volume ``rename(2)`` of the whole directory, so every store keeps its
inode (the selection ledger pins ``[st_dev, st_ino]``), its ``-wal``/``-shm``
files, backups and receipts. Only the records that name the directory are
rewritten, each from bytes precomputed into ``state-move-intent.json``:

* the project route record and selection marker,
* the reviewed route, the production attestation (journal and socket paths),
  the selection ledger, the build record and the build pointer,
* the incremental document store's bound candidate path,
* the install journal (``cas_relocate_state_root``; ``previous*`` cleared).

Phases: ``prepare`` (old root, nothing selected changes; a crash discards the
intent), ``move`` (the rename), ``switch`` (compare-and-replace writes; a
resume replays them), ``verify`` and ``settle``. After the rename a move only
goes forward and needs no bundle review, network or model.

While the project pointer ``.memvane/memvane-state-move.json`` exists a
hook that classifies the route ``blocked`` re-reads it briefly
(``settle_decision``); the window between the rename and the route write is a
few small file writes under every writer lock.
"""
from __future__ import annotations

from contextlib import ExitStack, closing
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import time

import memvane_paths
import project_dir

SCHEMA = 'memvane-state-move-intent-v1'
POINTER_SCHEMA = 'memvane-state-move-pointer-v1'
INTENT = 'state-move-intent.json'
POINTER = 'memvane-state-move.json'
JOURNAL = 'install-journal.sqlite'
LIMIT = 3 * 1024 * 1024
# A hook that sees a blocked route under a fresh pointer re-reads it this long.
WAIT_SECONDS = 1.5
POLL_SECONDS = 0.025
# A pointer older than this names a move whose worker stopped: no wait.
FRESH_SECONDS = 120
WAIT_ACTIONS = frozenset(('recall', 'update', 'update-queued'))
# The provider's own limit on its Unix socket path (bytes), which the move
# must not cross: ``laya`` -> ``memvane`` adds three bytes.
SOCKET_PATH_LIMIT = 100
# The move waits this long for a wiki grant writer to finish.
DISCOVERY_WAIT_SECONDS = 2.0
OTHER_INTENTS = ('inactive-upgrade-intent.json', 'bound-upgrade-intent.json')
ROUTE_KEYS = ('journalPath', 'dbPath', 'queuePath')
LEDGER_KEYS = ('journalPath', 'candidateDb', 'queueDb', 'catalogDb', 'bindingStore')
BUILD_KEYS = ('stateRoot', 'journalPath', 'candidateDb', 'queueDb', 'catalogDb',
              'bindingStore', 'routeFile')
INTENT_FIELDS = frozenset((
    'schema', 'projectRoot', 'installId', 'fromRoot', 'toRoot', 'oldRoute', 'newRoute',
    'oldAttestation', 'newAttestation', 'oldLedger', 'newLedger', 'oldMarker',
    'newMarker', 'oldBuild', 'newBuild', 'oldPointer', 'newPointer', 'docs', 'counts'))
POINTER_FIELDS = frozenset(('schema', 'projectRoot', 'installId', 'fromRoot', 'toRoot',
                            'oldRouteSha256', 'newRouteSha256'))
_HEX32 = re.compile(r'[0-9a-f]{32}\Z')
_HEX64 = re.compile(r'[0-9a-f]{64}\Z')
# Test seam: called with each phase name ('prepared', 'moved', 'records',
# 'journal', 'route', 'marker') while the move holds its locks.
_step = None


class StateMoveBlocked(RuntimeError):
    pass


def _fail(reason):
    raise StateMoveBlocked(reason)


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def _fsync(directory):
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


# ------------------------------------------------------------------ layout

def legacy_root(state, home=None):
    """True for ``<pre-rename state base>/<install id>``."""
    state = Path(state)
    return (state.parent == memvane_paths.legacy_state_base(home) and
            bool(_HEX32.fullmatch(state.name)))


def target_root(state, home=None):
    return memvane_paths.state_base(home) / Path(state).name


def _ensure_base(home=None):
    """``~/.local/state/memvane``: created 0700 under real, owned ancestors."""
    cursor = memvane_paths._home(home)
    for name in memvane_paths.STATE_PARTS:
        cursor = cursor / name
        if not cursor.exists() and not cursor.is_symlink():
            os.mkdir(cursor, 0o700)
            # The new entry is durable before a rename commits into it.
            _fsync(cursor.parent)
        info = cursor.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or
                stat.S_IMODE(info.st_mode) & 0o022):
            _fail('state_move_base_unsafe')
    info = cursor.lstat()
    if stat.S_IMODE(info.st_mode) & 0o077:
        _fail('state_move_base_unsafe')
    return cursor


def _private_root(path):
    path = Path(path)
    try:
        info = path.lstat()
    except FileNotFoundError:
        _fail('state_move_root_missing')
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or
            stat.S_IMODE(info.st_mode) & 0o077):
        _fail('state_move_root_unsafe')
    return path


# ----------------------------------------------------------------- pointer

def pointer_path(project):
    return project_dir.runtime_file(project, POINTER)


def read_pointer(project, home=None):
    """The project's pending-move pointer, validated, or None."""
    from laya_ledger_common import _read_json
    path = pointer_path(project)
    if not os.path.lexists(path):
        return None
    value = _read_json(path)
    if (not isinstance(value, dict) or set(value) != POINTER_FIELDS or
            value['schema'] != POINTER_SCHEMA or value['projectRoot'] != str(project) or
            type(value['installId']) is not str or not _HEX32.fullmatch(value['installId']) or
            value['fromRoot'] != str(memvane_paths.legacy_state_base(home) /
                                     value['installId']) or
            value['toRoot'] != str(memvane_paths.state_base(home) / value['installId']) or
            any(type(value[key]) is not str or not _HEX64.fullmatch(value[key])
                for key in ('oldRouteSha256', 'newRouteSha256'))):
        _fail('state_move_pointer_invalid')
    return value


def _write_pointer(intent):
    from laya_ledger_common import _write
    value = {'schema': POINTER_SCHEMA, 'projectRoot': intent['projectRoot'],
             'installId': intent['installId'], 'fromRoot': intent['fromRoot'],
             'toRoot': intent['toRoot'],
             'oldRouteSha256': _sha(intent['oldRoute'].encode('utf-8')),
             'newRouteSha256': _sha(intent['newRoute'].encode('utf-8'))}
    path = pointer_path(intent['projectRoot'])
    if os.path.lexists(path):
        if read_pointer(intent['projectRoot']) != value:
            _fail('state_move_pointer_changed')
        return value
    _write(path, value, replace=False)
    return value


def _drop_pointer(project):
    path = pointer_path(project)
    try:
        os.unlink(path)
    except FileNotFoundError:
        return
    _fsync(path.parent)


def located(pointer):
    """Where the pointer's install directory is now, or None."""
    for key in ('toRoot', 'fromRoot'):
        path = Path(pointer[key])
        if path.is_dir() and not path.is_symlink():
            return path
    return None


def pending_roots(project, home=None):
    """State roots of this project with a move started (pointer first)."""
    found = []
    try:
        pointer = read_pointer(project, home)
    except Exception:
        pointer = None
    if pointer is not None:
        where = located(pointer)
        if where is not None:
            found.append(where)
    return found


def intent_present(state):
    return os.path.lexists(Path(state) / INTENT)


# ------------------------------------------------------------- hook side

def _pointer_for(cwd):
    """(project, pointer is fresh) of the nearest pending pointer, or None."""
    try:
        current = Path(cwd).resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return None
    for directory in (current, *current.parents):
        path = pointer_path(directory)
        try:
            info = path.lstat()
        except (FileNotFoundError, NotADirectoryError):
            continue
        except OSError:
            return None
        if not stat.S_ISREG(info.st_mode):
            return None
        from cooldown import window_elapsed
        return directory, not window_elapsed(info.st_mtime, time.time(), FRESH_SECONDS)
    return None


def settle_decision(cwd, action, decision, select, *, clock=time.monotonic,
                    sleep=time.sleep, request=None):
    """Re-read a route that is blocked only because its install is moving.

    Used for recall and SessionStart hooks only (the gate keeps its own short
    timeout). A fresh pointer waits up to ``WAIT_SECONDS`` for the worker to
    publish the moved route; a stale one (the worker stopped mid-move) asks
    the upgrade worker to resume and returns the blocked decision.
    """
    if (not isinstance(decision, dict) or decision.get('state') != 'blocked' or
            action not in WAIT_ACTIONS):
        return decision
    found = _pointer_for(cwd)
    if found is None:
        return decision
    project, fresh = found
    if fresh:
        deadline = clock() + WAIT_SECONDS
        while clock() < deadline:
            sleep(POLL_SECONDS)
            again = select(cwd, action=action)
            if again.get('state') != 'blocked':
                return again
            if not os.path.lexists(pointer_path(project)):
                return select(cwd, action=action)
    try:
        pointer = read_pointer(project)
        where = located(pointer) if pointer is not None else None
        if where is not None:
            if request is None:
                import laya_bundle_upgrade
                request = laya_bundle_upgrade.request
            request(str(project), str(where / JOURNAL), 'state_root_move_pending')
    except Exception:
        pass
    return {**decision, 'reason': 'state_root_move_pending'}


def relocated_decision(old, new, home=None):
    """True when ``new`` is ``old`` after its install directory moved here."""
    if not isinstance(old, dict) or not isinstance(new, dict):
        return False
    if old.get('state') != 'candidate' or new.get('state') != 'candidate':
        return False
    try:
        journal = Path(old['journalPath'])
        if not legacy_root(journal.parent, home):
            return False
        target = target_root(journal.parent, home)
        if os.path.lexists(journal.parent):
            return False
        for key in ROUTE_KEYS:
            moved = Path(old[key])
            if moved.parent != journal.parent or new[key] != str(target / moved.name):
                return False
        return all(old.get(key) == new.get(key) for key in
                   ('projectRoot', 'installId', 'scopeId', 'dbIncarnation'))
    except (KeyError, TypeError, ValueError):
        return False


# ----------------------------------------------------------------- prepare

def _read(path, limit=1024 * 1024):
    from laya_product_install import _read_private
    return _read_private(Path(path), limit)


def _rebase(value, old, new):
    from laya_install_journal import rebased
    moved = rebased(value, old, new)
    if moved is None:
        _fail('state_move_path_outside_root')
    return moved


def _docs_candidate(state):
    """The incremental document store's bound candidate path, or None."""
    from laya_incremental_docs import FILE
    path = Path(state) / FILE
    if not path.exists():
        return None
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=2)) as db:
        try:
            rows = db.execute('SELECT candidate_db FROM meta').fetchall()
        except sqlite3.OperationalError:
            return None
    if not rows:
        return None
    if len(rows) != 1 or type(rows[0][0]) is not str:
        _fail('state_move_docs_invalid')
    return rows[0][0]


def _check_socket(old, new):
    """Refuse a move that would push the provider socket path past its limit."""
    def length(record):
        path = record.get('providerManifest', {}).get('serviceSocketPath')
        return len(path.encode('utf-8')) if isinstance(path, str) else None
    before, after = length(old), length(new)
    if (after is not None and after > SOCKET_PATH_LIMIT and
            (before is None or before <= SOCKET_PATH_LIMIT)):
        _fail('state_move_socket_path_too_long')


def _settle_active_intent(project, state):
    """Archive the settled bundle-upgrade intent: it names the old root."""
    import laya_active_upgrade as active
    intent = active._load_intent(state, project)
    if intent is None:
        return
    try:
        active._archive_finished_intent(state, project, intent)
    except Exception as exc:
        raise StateMoveBlocked('state_move_upgrade_pending') from exc


def _prepare(project, state, home=None):
    import laya_fresh_wiki_origin as fresh
    import laya_inactive_upgrade as inactive
    import laya_initial_build as build
    from laya_install_journal import (PRODUCTION_ATTESTATION, _canon as journal_canon,
                                      inspect_journal, relocated_attestation)
    from laya_ledger_common import refuse_qmd_origin
    refuse_qmd_origin(state)
    for name in OTHER_INTENTS:
        if os.path.lexists(state / name):
            _fail('state_move_upgrade_pending')
    target = target_root(state, home)
    base = _ensure_base(home)
    if os.stat(base).st_dev != os.stat(state.parent).st_dev:
        _fail('state_move_cross_device')
    if os.path.lexists(target):
        _fail('state_move_target_exists')
    _settle_active_intent(project, state)
    auto = project_dir.folder(project)
    journal = state / JOURNAL
    row = fresh._load(state / fresh.LEDGER)
    if row['phase'] != 'active' or row['journalPath'] != str(journal):
        _fail('state_move_not_selected')
    old_route = _read(auto / fresh.ROUTE, 8192)
    old_marker = _read(auto / fresh.MARKER, 8192)
    reviewed = _read(state / 'reviewed-route.json', 8192)
    old_att = _read(state / PRODUCTION_ATTESTATION)
    old_ledger = _read(state / fresh.LEDGER, 1024 * 1024)
    selected = inspect_journal(journal)
    if (old_marker != _canonical(fresh._marker(row)) or old_route != reviewed or
            _sha(old_route) != row['routeSha256'] or
            _sha(old_att) != row['attestationSha256'] or
            old_ledger != _canonical(row) or
            fresh.classify_marker(project, auto / fresh.MARKER,
                                  auto / fresh.ROUTE) != 'candidate' or
            selected['state'] not in ('active', 'restored_old') or
            selected['selectedRouteSha256'] != row['routeSha256'] or
            selected['providerAttestationSha256'] != row['attestationSha256']):
        _fail('state_move_selection_changed')
    route = json.loads(old_route)
    if _canonical(route) != old_route:
        _fail('state_move_route_noncanonical')
    from laya_index_host_route import SCHEMA as ROUTE_V1
    if route.get('schema') == ROUTE_V1:
        # A v1 route binds its journal to the plugin root's parent: not movable.
        _fail('state_move_not_selected')
    new_route = _canonical({**route, **{key: _rebase(route[key], state, target)
                                        for key in ROUTE_KEYS}})
    attestation = json.loads(old_att)
    if journal_canon(attestation).encode('utf-8') != old_att:
        _fail('state_move_attestation_noncanonical')
    moved_att = relocated_attestation(attestation, state, target)
    new_att = journal_canon(moved_att).encode('utf-8')
    _check_socket(attestation, moved_att)
    for key in ROUTE_KEYS:
        moved = json.loads(new_route)[key]
        if os.path.normpath(moved) != moved or not os.path.isabs(moved):
            _fail('state_move_path_invalid')
    new_row = {**row, **{key: _rebase(row[key], state, target) for key in LEDGER_KEYS},
               'epoch': row['epoch'] + 1, 'routeSha256': _sha(new_route),
               'attestationSha256': _sha(new_att)}
    fresh.validate_row(new_row, target / fresh.LEDGER)
    record = build.read(state)
    if (record is None or record['identity']['stateRoot'] != str(state) or
            record['identity']['projectRoot'] != str(project) or
            record['identity']['journalPath'] != str(journal)):
        _fail('state_move_build_binding_changed')
    old_build = _read(build.record_path(state))
    if old_build != build._canon(record):
        _fail('state_move_build_binding_changed')
    identity = {**record['identity'],
                **{key: _rebase(record['identity'][key], state, target)
                   for key in BUILD_KEYS}}
    new_record = {**record, 'epoch': record['epoch'] + 1, 'updatedNs': time.time_ns(),
                  'identity': identity}
    build._validate(new_record)
    old_pointer = {'schema': build.POINTER_SCHEMA, 'projectRoot': str(project),
                   'stateRoot': str(state), 'installId': record['identity']['installId']}
    docs = _docs_candidate(state)
    if docs is not None and docs != record['identity']['candidateDb']:
        _fail('state_move_docs_invalid')
    counts = inactive._store_counts(record['identity'])
    intent = {'schema': SCHEMA, 'projectRoot': str(project),
              'installId': state.name, 'fromRoot': str(state), 'toRoot': str(target),
              'oldRoute': old_route.decode('utf-8'), 'newRoute': new_route.decode('utf-8'),
              'oldAttestation': old_att.decode('utf-8'),
              'newAttestation': new_att.decode('utf-8'),
              'oldLedger': row, 'newLedger': new_row,
              'oldMarker': old_marker.decode('utf-8'),
              'newMarker': _canonical(fresh._marker(new_row)).decode('utf-8'),
              'oldBuild': record, 'newBuild': new_record,
              'oldPointer': old_pointer,
              'newPointer': {**old_pointer, 'stateRoot': str(target)},
              'docs': None if docs is None else
              [docs, _rebase(docs, state, target)],
              'counts': counts}
    build._write_json(state / INTENT, intent, replace=False, max_bytes=LIMIT)
    _fsync(state)
    _write_pointer(intent)
    return intent


def _load_intent(state, project):
    import laya_initial_build as build
    value = build._read_json(Path(state) / INTENT, max_bytes=LIMIT)
    if value is None:
        return None
    if (not isinstance(value, dict) or set(value) != INTENT_FIELDS or
            value['schema'] != SCHEMA or value['projectRoot'] != str(project) or
            value['installId'] != Path(state).name or
            str(Path(state)) not in (value['fromRoot'], value['toRoot']) or
            Path(value['fromRoot']).name != value['installId'] or
            Path(value['toRoot']).name != value['installId']):
        _fail('state_move_intent_invalid')
    return value


def _abort(state, project):
    """Drop a prepared, never-moved intent: nothing selected changed yet."""
    _drop_pointer(project)
    try:
        os.unlink(Path(state) / INTENT)
    except FileNotFoundError:
        return
    _fsync(state)


# ------------------------------------------------------------------ switch

def _expected_replace(path, old, new):
    import laya_inactive_upgrade as inactive
    try:
        inactive._atomic_expected(Path(path), old, new)
    except Exception as exc:
        raise StateMoveBlocked('state_move_metadata_changed') from exc


def _rebind_docs(state, docs):
    if docs is None:
        return
    from laya_incremental_docs import FILE
    old, new = docs
    path = Path(state) / FILE
    with closing(sqlite3.connect(path.as_uri() + '?mode=rw', uri=True,
                                 isolation_level=None, timeout=5)) as db:
        db.execute('PRAGMA synchronous=FULL')
        db.execute('BEGIN IMMEDIATE')
        try:
            rows = [row[0] for row in db.execute('SELECT candidate_db FROM meta')]
            if rows == [old]:
                db.execute('UPDATE meta SET candidate_db=? WHERE candidate_db=?', (new, old))
            elif rows != [new]:
                _fail('state_move_docs_invalid')
            db.execute('COMMIT')
        except BaseException:
            db.execute('ROLLBACK')
            raise


def _relocate_pointer(intent):
    import laya_initial_build as build
    try:
        build.relocate_project_pointer(intent['oldPointer'], intent['newPointer'])
    except Exception as exc:
        raise StateMoveBlocked('state_move_pointer_changed') from exc


def _replace_ledger(state, old_row, new_row):
    import laya_fresh_wiki_origin as fresh
    path = Path(state) / fresh.LEDGER
    with fresh._ledger_lock(path):
        _expected_replace(path, _canonical(old_row), _canonical(new_row))


def _journal(intent, state):
    from laya_install_journal import cas_relocate_state_root, inspect_journal
    journal = Path(state) / JOURNAL
    current = inspect_journal(journal)
    old_sha = _sha(intent['oldRoute'].encode('utf-8'))
    new_sha = _sha(intent['newRoute'].encode('utf-8'))
    if current['selectedRouteSha256'] == new_sha:
        return current
    if current['selectedRouteSha256'] != old_sha:
        _fail('state_move_journal_changed')
    return cas_relocate_state_root(journal, current['epoch'],
                                   intent['oldRoute'].encode('utf-8'),
                                   intent['newRoute'].encode('utf-8'),
                                   intent['oldAttestation'].encode('utf-8'))


def _switch(intent, *, step=None):
    import laya_fresh_wiki_origin as fresh
    import laya_initial_build as build
    from laya_install_journal import PRODUCTION_ATTESTATION
    project = Path(intent['projectRoot'])
    old, new = Path(intent['fromRoot']), Path(intent['toRoot'])
    auto = project_dir.folder(project)
    mark = step or _step or (lambda _name: None)
    if os.path.lexists(old):
        if os.path.lexists(new):
            _fail('state_move_both_roots')
        os.rename(old, new)
        _fsync(old.parent)
        _fsync(new.parent)
    _private_root(new)
    mark('moved')
    raw = lambda key: intent[key].encode('utf-8')
    _expected_replace(new / PRODUCTION_ATTESTATION, raw('oldAttestation'),
                      raw('newAttestation'))
    _expected_replace(new / 'reviewed-route.json', raw('oldRoute'), raw('newRoute'))
    _expected_replace(build.record_path(new), build._canon(intent['oldBuild']),
                      build._canon(intent['newBuild']))
    _rebind_docs(new, intent['docs'])
    _relocate_pointer(intent)
    mark('records')
    journal = _journal(intent, new)
    mark('journal')
    _replace_ledger(new, intent['oldLedger'], intent['newLedger'])
    _expected_replace(auto / fresh.ROUTE, raw('oldRoute'), raw('newRoute'))
    mark('route')
    _expected_replace(auto / fresh.MARKER, raw('oldMarker'), raw('newMarker'))
    mark('marker')
    return journal


def _verify(intent):
    import laya_fresh_wiki_origin as fresh
    import laya_inactive_upgrade as inactive
    from laya_index_host_route import select_route
    from laya_install_journal import verify_selected_route_read_only
    project, new = Path(intent['projectRoot']), Path(intent['toRoot'])
    auto = project_dir.folder(project)
    if fresh.classify_marker(project, auto / fresh.MARKER, auto / fresh.ROUTE) != 'candidate':
        _fail('state_move_unverified')
    decision = select_route(str(project), action='update')
    if (decision.get('state') != 'candidate' or
            decision.get('journalPath') != str(new / JOURNAL) or
            any(decision.get(key) != json.loads(intent['newRoute'])[key]
                for key in ROUTE_KEYS)):
        _fail('state_move_unverified')
    proof = verify_selected_route_read_only(decision)
    if proof.get('readOnlyAllowed') is not True:
        _fail('state_move_unverified')
    if inactive._store_counts(intent['newBuild']['identity']) != intent['counts']:
        _fail('state_move_store_changed')
    return proof


def _settle(intent):
    new = Path(intent['toRoot'])
    _drop_pointer(intent['projectRoot'])
    tag = _sha(_canonical(intent))[:20]
    target = new / ('state-move-' + tag + '-settled.json')
    if os.path.lexists(target):
        os.unlink(new / INTENT)
    else:
        os.rename(new / INTENT, target)
    _fsync(new)
    return target


# --------------------------------------------------------------------- run

def _locks(stack, project, state):
    """Every selected-writer exclusion the bundle upgrade takes, plus the build owner."""
    import laya_initial_build as build
    from laya_fresh_wiki_origin import MARKER
    from laya_ledger_common import _operation_lock
    from laya_product_install import _install_lock
    stack.enter_context(_install_lock(state))
    if not stack.enter_context(build.owner(state)):
        _fail('initial_build_busy')
    stack.enter_context(_operation_lock(project_dir.folder(project) / MARKER))
    stack.enter_context(_provider_lane())
    # Wiki grant writers (SessionStart hooks) serialize on this lock; one that
    # waited across the rename finds its path gone and re-resolves the route.
    import laya_wiki_discovery
    from laya_fresh_wiki_origin import ROUTE
    try:
        queue = json.loads(_read(project_dir.folder(project) / ROUTE, 8192))['queuePath']
    except Exception:
        queue = None
    if isinstance(queue, str):
        stack.enter_context(laya_wiki_discovery._locked(
            state / laya_wiki_discovery.lock_path(queue).name, DISCOVERY_WAIT_SECONDS))


def _provider_lane():
    """The per-user provider lane: no selected writer embeds while the move runs."""
    from context_learning.resource_coordination import Coordinator
    return Coordinator().quiesce_provider()


def move(project_root, state, *, home=None, step=None):
    """Move (or finish moving) one install; returns (state root now, result)."""
    project = Path(project_root)
    state = Path(state)
    with ExitStack() as stack:
        _locks(stack, project, state)
        intent = _load_intent(state, project)
        if intent is not None and str(state) == intent['fromRoot']:
            # Prepared but never moved: start over from the current selection.
            _abort(state, project)
            intent = None
        if intent is None:
            if not legacy_root(state, home):
                return state, {'status': 'state_move_current'}
            if read_pointer(project, home) is not None and located(
                    read_pointer(project, home)) != state:
                _fail('state_move_pointer_changed')
            from laya_journal_v4 import refuse_while_legacy_hermes
            refuse_while_legacy_hermes(home)
            intent = _prepare(project, state, home)
            (step or _step or (lambda _name: None))('prepared')
        else:
            _write_pointer(intent)
        journal = _switch(intent, step=step)
        _verify(intent)
        receipt = _settle(intent)
        new = Path(intent['toRoot'])
    try:
        from laya_product_install import _request_build_wake
        wake = _request_build_wake(str(project))
    except Exception:
        wake = None
    return new, {'status': 'state_move_complete', 'stateRoot': str(new),
                 'journalEpoch': journal['epoch'], 'receipt': receipt.name,
                 'syncWake': wake}


# ------------------------------------------------------------ shared roots

def _legacy_plugin_copies(home):
    """Host copies of the pre-rename plugin id that may still start a service."""
    home = memvane_paths._home(home)
    found = []
    record = home / '.claude' / 'plugins' / 'installed_plugins.json'
    key = memvane_paths.LEGACY_PLUGIN_ID + '@' + memvane_paths.LEGACY_MARKETPLACE
    if record.exists() or record.is_symlink():
        try:
            if json.loads(record.read_text('utf-8'))['plugins'].get(key):
                found.append('claude')
        except (OSError, ValueError, KeyError, AttributeError, TypeError):
            found.append('claude_unreadable')
    codex = (home / '.codex' / 'plugins' / 'cache' / memvane_paths.LEGACY_MARKETPLACE /
             memvane_paths.LEGACY_PLUGIN_ID)
    if codex.is_dir():
        found.append('codex')
    try:
        from laya_journal_v4 import legacy_hermes_present
        if legacy_hermes_present(home):
            found.append('hermes')
    except Exception:
        found.append('hermes_unreadable')
    return found


def _legacy_installs(home):
    base = memvane_paths.legacy_state_base(home)
    try:
        return sorted(path for path in base.iterdir()
                      if _HEX32.fullmatch(path.name))
    except FileNotFoundError:
        return []


def pending_moves(home=None):
    """Installs (either base) holding an unfinished move intent, with their project."""
    import laya_initial_build as build
    found = []
    for base in (memvane_paths.legacy_state_base(home), memvane_paths.state_base(home)):
        try:
            names = sorted(os.listdir(base))
        except FileNotFoundError:
            continue
        for name in names:
            state = base / name
            if not _HEX32.fullmatch(name) or not intent_present(state):
                continue
            try:
                intent = build._read_json(state / INTENT, max_bytes=LIMIT)
                project = intent.get('projectRoot') if isinstance(intent, dict) else None
            except Exception:
                project = None
            found.append({'installId': name, 'stateRoot': str(state),
                          'projectRoot': project if isinstance(project, str) else None})
    return found


def _runtime_users(runtime, ps_all):
    """Live processes whose executable or script lies inside ``runtime``."""
    from legacy_cleanup import _NODE_ARGV, _within
    users = []
    for pid, uid, command in ps_all():
        if pid == os.getpid():
            continue
        match = _NODE_ARGV.match(command)
        if match is None:
            continue
        exe, rest = match.group(1), match.group(2) or ''
        if _within(exe, str(runtime)) or rest.startswith(str(runtime) + '/'):
            users.append({'pid': pid, 'uid': uid})
    return users


def shared_plan(home=None, *, ps_all=None):
    """Read-only plan for the pre-rename shared trees under ``~/.local/state/laya``."""
    from legacy_cleanup import _ps_all, attestation_references
    home = memvane_paths._home(home)
    legacy = memvane_paths.legacy_state_base(home)
    current = memvane_paths.state_base(home)
    rows = []
    if not legacy.is_dir():
        return {'status': 'absent', 'legacyRoot': str(legacy), 'trees': rows}
    installs = _legacy_installs(home)
    copies = _legacy_plugin_copies(home)
    runtime = legacy / 'embed-runtime'
    if runtime.is_dir():
        blockers = []
        if installs:
            blockers.append('legacy_install_present')
        if attestation_references(home, [runtime]):
            blockers.append('runtime_attested')
        try:
            if _runtime_users(runtime, ps_all or _ps_all):
                blockers.append('runtime_in_use')
        except Exception:
            blockers.append('process_table_unavailable')
        rows.append({'tree': 'embed-runtime', 'path': str(runtime),
                     'action': 'keep' if blockers else 'remove', 'blockers': blockers})
    service = legacy / 'service'
    if service.is_dir():
        blockers = []
        if copies:
            blockers.append('legacy_plugin_installed')
        lease = _service_lease(service, create=False)
        if lease is None:
            # A pre-rename service, or the MemVane service that excludes one,
            # holds it. The MemVane service retires the tree itself.
            blockers.append('legacy_service_lease_held')
        elif lease >= 0:
            os.close(lease)
        rows.append({'tree': 'service', 'path': str(service),
                     'action': 'keep' if blockers else 'remove', 'blockers': blockers})
    pointers = legacy / 'initial-builds'
    if pointers.is_dir():
        left = [name for name in os.listdir(pointers) if not name.startswith('.')]
        rows.append({'tree': 'initial-builds', 'path': str(pointers),
                     'action': 'keep' if left else 'remove',
                     'blockers': ['pointer_present'] if left else []})
    auto = legacy / 'auto-context'
    if auto.is_dir():
        clashes = [name for name in os.listdir(auto)
                   if os.path.lexists(current / 'auto-context' / name)
                   and name != 'logs']
        rows.append({'tree': 'auto-context', 'path': str(auto),
                     'action': 'keep' if clashes else 'move',
                     'blockers': ['memvane_entry_present'] if clashes else []})
    known = {'embed-runtime', 'service', 'initial-builds', 'auto-context'}
    others = sorted(path.name for path in legacy.iterdir()
                    if path.name not in known and not _HEX32.fullmatch(path.name))
    return {'status': 'planned', 'legacyRoot': str(legacy),
            'legacyInstalls': [path.name for path in installs],
            'legacyPluginCopies': copies, 'unknownEntries': others, 'trees': rows}


def _service_lease(service, *, create=True):
    """The pre-rename service lease, held exclusively by this process, or None.

    Only this test decides that no service uses the directory: an answering
    control socket is not required (the MemVane service holds this lease
    while it answers on its own socket).
    """
    from laya_resident_service import LEASE
    try:
        fd = os.open(Path(service) / LEASE, os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0))
    except FileNotFoundError:
        if not create:
            return -1
        # No lease file: hold a fresh one so no service starts while we remove.
        fd = os.open(Path(service) / LEASE, os.O_RDWR | os.O_CREAT | os.O_EXCL |
                     getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


def legacy_service_retirable(home=None):
    """No pre-rename install and no pre-rename plugin copy is left.

    Then no client can open the pre-rename service lease again, so its holder
    may remove the directory without splitting the lock's identity.
    """
    return not _legacy_installs(home) and not _legacy_plugin_copies(home)


def _remove_service_tree(path, home):
    """Remove the pre-rename service directory while holding its lease."""
    from legacy_cleanup import CleanupBlocked, _remove_tree
    fd = _service_lease(path)
    if fd is None:
        raise CleanupBlocked('legacy_service_lease_held')
    try:
        if not legacy_service_retirable(home):
            raise CleanupBlocked('legacy_plugin_installed')
        _remove_tree(path, home)
    finally:
        os.close(fd)


def _move_auto_context(legacy, current):
    """Rename each pre-rename per-user log/scan entry into the MemVane tree."""
    target = current / 'auto-context'
    if not target.exists():
        os.mkdir(target, 0o700)
        _fsync(current)
    # Scan state first: ``memvane_paths.auto_context_state`` follows it, so
    # writers switch to the MemVane tree before their logs move.
    for name in sorted(os.listdir(legacy), key=lambda item: (item != 'scan-state', item)):
        source = legacy / name
        if name == 'logs' and (target / 'logs').exists():
            for entry in sorted(os.listdir(source)):
                if not os.path.lexists(target / 'logs' / entry):
                    os.rename(source / entry, target / 'logs' / entry)
            if not os.listdir(source):
                os.rmdir(source)
            continue
        if not os.path.lexists(target / name):
            os.rename(source, target / name)
    if not os.listdir(legacy):
        os.rmdir(legacy)


def collect_shared(home=None, *, ps_all=None):
    """Remove or move each pre-rename shared tree the plan allows; never signals."""
    from legacy_cleanup import CleanupBlocked, _remove_tree
    home = memvane_paths._home(home)
    plan = shared_plan(home, ps_all=ps_all)
    if plan['status'] == 'absent':
        return plan
    legacy = memvane_paths.legacy_state_base(home)
    current = _ensure_base(home)
    done, failed = [], []
    for row in plan['trees']:
        path = Path(row['path'])
        try:
            if row['action'] == 'remove' and row['tree'] == 'initial-builds':
                for name in os.listdir(path):
                    os.unlink(path / name)
                os.rmdir(path)
            elif row['action'] == 'remove' and row['tree'] == 'service':
                _remove_service_tree(path, home)
            elif row['action'] == 'remove':
                _remove_tree(path, home)
            elif row['action'] == 'move':
                _move_auto_context(path, current)
            else:
                continue
        except (OSError, CleanupBlocked) as exc:
            failed.append({'tree': row['tree'], 'reason': str(exc) or type(exc).__name__})
            continue
        done.append(row['tree'])
    try:
        if not os.listdir(legacy):
            os.rmdir(legacy)
            done.append('legacy_root')
    except OSError:
        pass
    return {**plan, 'status': 'partial' if failed else 'collected',
            'completed': done, 'failed': failed}


def status(home=None):
    """Owner view: legacy installs, pending moves and the shared-tree plan."""
    plan = shared_plan(home)
    moves = []
    for state in _legacy_installs(home):
        moves.append({'installId': state.name, 'stateRoot': str(state),
                      'intent': intent_present(state)})
    return {**plan, 'pendingInstalls': moves, 'pendingMoves': pending_moves(home)}

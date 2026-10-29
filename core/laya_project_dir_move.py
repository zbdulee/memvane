"""Move a selected project's folder from ``.auto-context/`` to ``.memvane/``.

The bundle upgrade worker (SessionStart) moves each selected project once:

* ``rename``: ``<project>/.auto-context`` is renamed to ``<project>/.memvane``
  with one ``rename(2)``; settings, wiki cards and runtime files keep their
  inodes (the wiki root keeps its ``(dev, ino)``).
* ``adopt``: git already brought ``.memvane/settings.json`` and
  ``.memvane/wiki`` (a pulled or checked-out rename) while the runtime files
  stayed in ``.auto-context/``; each runtime entry is renamed into
  ``.memvane/`` and the empty legacy folder is removed.

The index content is path-independent (``<collection>/<rel>`` members,
content by sha256, vectors by ``(hash, title, profile)``), but every stored
``GenerationSourceBinding`` commits to the absolute wiki root and is the
suffix of its generation's name, and the binding store's metadata is sealed to
one root set. The move therefore *rebinds* explicitly: a clone ``G'`` of the
published head is staged from the head's own published bodies (no file read,
same manifest), its vector seal is built from the cached chunk sets (no
model), and a new binding store holds ``G'``'s binding for
``<project>/.memvane/wiki``. The catalog keeps its scans (relative paths);
only its ``binding``/``roots`` metadata is rewritten.

Phases: ``prepare`` (nothing selected changes; the intent
``project-dir-move-intent.json`` in the private state root precomputes every
record and the pointer ``memvane-project-move.json`` goes into the project
folder), ``fence`` (ledger ``active`` -> ``upgrading``: readers classify
``blocked`` and wait), ``move``, ``switch`` (catalog metadata, binding store,
``G'`` publish, reviewed route, build record, discovery cursor, journal,
ledger, route, marker; each step accepts the already-new value), ``verify``
and ``settle``. A crash before the fence discards the intent; after it the
move only goes forward.
"""
from __future__ import annotations

from contextlib import ExitStack, closing
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import time

import project_dir

SCHEMA = 'memvane-project-dir-move-intent-v1'
POINTER_SCHEMA = 'memvane-project-dir-move-pointer-v1'
INTENT = 'project-dir-move-intent.json'
POINTER = 'memvane-project-move.json'
JOURNAL = 'install-journal.sqlite'
STORE_SUFFIX = '.project-move'
LIMIT = 3 * 1024 * 1024
# A hook that sees a blocked route under a fresh pointer re-reads it this long.
WAIT_SECONDS = 1.5
POLL_SECONDS = 0.025
FRESH_SECONDS = 120
WAIT_ACTIONS = frozenset(('recall', 'update', 'update-queued'))
DISCOVERY_WAIT_SECONDS = 2.0
OTHER_INTENTS = ('inactive-upgrade-intent.json', 'bound-upgrade-intent.json',
                 'state-move-intent.json')
INTENT_FIELDS = frozenset((
    'schema', 'mode', 'projectRoot', 'installId', 'stateRoot', 'oldRoute', 'newRoute',
    'oldLedger', 'fencedLedger', 'newLedger', 'oldMarker', 'newMarker', 'oldBuild',
    'newBuild', 'discovery', 'catalog', 'store', 'generation', 'counts'))
_HEX32 = re.compile(r'[0-9a-f]{32}\Z')
# Test seam: called with each phase name while the move holds its locks.
_step = None


class ProjectDirMoveBlocked(RuntimeError):
    pass


def _fail(reason):
    raise ProjectDirMoveBlocked(reason)


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


def _read(path, limit=1024 * 1024):
    from laya_product_install import _read_private
    return _read_private(Path(path), limit)


def _mark(step, name):
    (step or _step or (lambda _name: None))(name)


def legacy_folder(project):
    return Path(project) / project_dir.LEGACY


def current_folder(project):
    return Path(project) / project_dir.CURRENT


def wiki_roots(project):
    """(legacy wiki root, current wiki root) as canonical strings."""
    project = Path(project)
    return (str(project / project_dir.LEGACY / project_dir.WIKI),
            str(project / project_dir.CURRENT / project_dir.WIKI))


# ----------------------------------------------------------------- pointer

def pointer_path(project):
    return project_dir.runtime_file(project, POINTER)


def read_pointer(project):
    from laya_ledger_common import _read_json
    path = pointer_path(project)
    if not os.path.lexists(path):
        return None
    value = _read_json(path)
    if (not isinstance(value, dict) or
            set(value) != {'schema', 'projectRoot', 'stateRoot', 'mode'} or
            value['schema'] != POINTER_SCHEMA or value['projectRoot'] != str(project) or
            value['mode'] not in ('rename', 'adopt') or
            not isinstance(value['stateRoot'], str) or
            not os.path.isabs(value['stateRoot'])):
        _fail('project_dir_move_pointer_invalid')
    return value


def _write_pointer(intent):
    from laya_ledger_common import _write
    value = {'schema': POINTER_SCHEMA, 'projectRoot': intent['projectRoot'],
             'stateRoot': intent['stateRoot'], 'mode': intent['mode']}
    path = pointer_path(intent['projectRoot'])
    if os.path.lexists(path):
        if read_pointer(intent['projectRoot']) != value:
            _fail('project_dir_move_pointer_changed')
        return value
    _write(path, value, replace=False)
    return value


def _drop_pointer(project):
    for name in project_dir.NAMES:
        path = Path(project) / name / POINTER
        try:
            os.unlink(path)
        except FileNotFoundError:
            continue
        _fsync(path.parent)


def pending_roots(project):
    """State roots of this project with a folder move started."""
    try:
        pointer = read_pointer(project)
    except Exception:
        return []
    if pointer is None:
        return []
    state = Path(pointer['stateRoot'])
    return [state] if state.is_dir() and not state.is_symlink() else []


def intent_present(state):
    return os.path.lexists(Path(state) / INTENT)


def needed(project):
    """'rename', 'adopt' or None: the move this project's layout asks for."""
    layout = project_dir.layout(project)
    if layout == 'legacy':
        return 'rename'
    if layout == 'adopt':
        return 'adopt'
    return None


# ------------------------------------------------------------- hook side

def _pointer_for(cwd):
    try:
        current = Path(cwd).resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return None
    for directory in (current, *current.parents):
        for name in project_dir.NAMES:
            path = directory / name / POINTER
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
    """Re-read a route that is blocked only because its project folder moves.

    Recall and SessionStart only. A fresh pointer waits up to
    ``WAIT_SECONDS`` for the worker to publish the moved route; a stale one
    asks the upgrade worker to resume and returns the blocked decision.
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
            if not any(os.path.lexists(project / name / POINTER)
                       for name in project_dir.NAMES):
                return select(cwd, action=action)
    try:
        pointer = read_pointer(project)
        if pointer is not None:
            if request is None:
                import laya_bundle_upgrade
                request = laya_bundle_upgrade.request
            request(str(project), str(Path(pointer['stateRoot']) / JOURNAL),
                    'project_dir_move_pending')
    except Exception:
        pass
    return {**decision, 'reason': 'project_dir_move_pending'}


def _wiki_root_of(decision):
    from laya_index_host_route import wiki_index_roots
    return next(iter(wiki_index_roots(decision['collections']).values()))


def moved_away(decision):
    """The decision names ``.auto-context/wiki`` and the project now has ``.memvane/``."""
    if not isinstance(decision, dict) or decision.get('state') != 'candidate':
        return False
    try:
        legacy, _current = wiki_roots(decision['projectRoot'])
        return (_wiki_root_of(decision) == legacy and not os.path.lexists(legacy) and
                current_folder(decision['projectRoot']).is_dir())
    except Exception:
        return False


def relocated_decision(old, new):
    """True when ``new`` is ``old`` after its project folder moved to ``.memvane/``."""
    if not isinstance(old, dict) or not isinstance(new, dict):
        return False
    if old.get('state') != 'candidate' or new.get('state') != 'candidate':
        return False
    try:
        legacy, current = wiki_roots(old['projectRoot'])
        if _wiki_root_of(old) != legacy or _wiki_root_of(new) != current:
            return False
        if [item for item in old['collections'] if item['role'] != 'wiki'] != \
                [item for item in new['collections'] if item['role'] != 'wiki']:
            return False
        return all(old.get(key) == new.get(key) for key in
                   ('projectRoot', 'installId', 'scopeId', 'dbIncarnation', 'dbPath',
                    'queuePath', 'journalPath'))
    except (KeyError, TypeError, ValueError, StopIteration):
        return False


# ----------------------------------------------------------------- prepare

def _settle_active_intent(project, state):
    import laya_active_upgrade as active
    intent = active._load_intent(state, project)
    if intent is None:
        return
    try:
        active._archive_finished_intent(state, project, intent)
    except Exception as exc:
        raise ProjectDirMoveBlocked('project_dir_move_upgrade_pending') from exc


def _head(index, scope):
    index.db.execute('BEGIN')
    try:
        row = index.db.execute('''SELECT h.gen,s.source_manifest,g.state FROM head h
            JOIN generations g ON g.gen=h.gen
            LEFT JOIN candidate_seals s ON s.gen=h.gen WHERE h.scope=?''',
                               (scope,)).fetchone()
        index.db.commit()
    except BaseException:
        index.db.rollback()
        raise
    if row is None or row[2] != 'published' or row[1] is None:
        _fail('project_dir_move_head_unbound')
    return row[0], row[1]


def _documents(index, gen):
    """The head generation's members with their published bodies, in path order."""
    rows = index.db.execute('''SELECT m.path,c.body,m.eligible FROM membership m
        JOIN content c ON c.hash=m.hash WHERE m.gen=? ORDER BY m.path COLLATE BINARY''',
                            (gen,)).fetchall()
    index.db.commit()
    return [(path, body, bool(eligible)) for path, body, eligible in rows]


def _wiki_node(path):
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode):
        _fail('project_dir_move_wiki_unavailable')
    return info.st_dev, info.st_ino


def _check_adoptable(project):
    """Every legacy entry is a runtime file that ``.memvane/`` does not hold yet."""
    legacy, current = legacy_folder(project), current_folder(project)
    for name in sorted(os.listdir(legacy)):
        if name in (project_dir.SETTINGS, project_dir.WIKI):
            _fail('project_dir_conflict')
        if os.path.lexists(current / name) and not _lock_name(name):
            _fail('project_dir_conflict')


def _lock_name(name):
    """An empty lock file: a clash keeps the ``.memvane/`` one."""
    return name.endswith('.lock')


def _stage_clone(index, *, scope, head, manifest, gen, claim, claim_epoch, profile_id):
    """Prepare ``gen`` with exactly the head's members and seal its vectors."""
    from laya_index_claim_queue import payload_digest
    from laya_index_vector import LayaVectorIndex
    documents = _documents(index, head)
    claim_digest = payload_digest(scope, gen, manifest, claim_epoch, head)
    staged = index.stage(gen, scope, documents, base=head, source_manifest=manifest,
                         claim_digest=claim_digest, claim_epoch=claim_epoch)
    if staged not in ('prepared', 'already_staged'):
        _fail('project_dir_move_stage_' + str(staged))
    seal = LayaVectorIndex(index).seal_generation(gen, profile_id)
    return {'claim': claim, 'claimDigest': claim_digest, 'claimEpoch': claim_epoch,
            'rows': seal['rows_count'], 'members': len(documents)}


def _prepare(project, state, mode):
    import laya_fresh_wiki_origin as fresh
    import laya_initial_build as build
    import laya_wiki_discovery as discovery
    from laya_index_a import LayaIndexA
    from laya_index_capacity import (GenerationBindingStore, bind_generation,
                                     binding_digest)
    from laya_index_host_route import SCHEMA as ROUTE_V1, wiki_index_roots
    from laya_index_runtime import capacity_store_paths
    from laya_install_journal import (PRODUCTION_ATTESTATION, inspect_journal,
                                      rebound_project_dir_route)
    from laya_ledger_common import refuse_qmd_origin
    from laya_source_catalog import GenerationSourceBinding, _binding as catalog_binding
    refuse_qmd_origin(state)
    for name in OTHER_INTENTS:
        if os.path.lexists(state / name):
            _fail('project_dir_move_upgrade_pending')
    _settle_active_intent(project, state)
    if mode == 'adopt':
        _check_adoptable(project)
    legacy_dir = legacy_folder(project)
    journal = state / JOURNAL
    row = fresh._load(state / fresh.LEDGER)
    if row['phase'] != 'active' or row['journalPath'] != str(journal):
        _fail('project_dir_move_not_selected')
    old_route = _read(legacy_dir / fresh.ROUTE, 8192)
    old_marker = _read(legacy_dir / fresh.MARKER, 8192)
    reviewed = _read(state / 'reviewed-route.json', 8192)
    old_att = _read(state / PRODUCTION_ATTESTATION)
    old_ledger = _read(state / fresh.LEDGER, 1024 * 1024)
    selected = inspect_journal(journal)
    if (old_marker != _canonical(fresh._marker(row)) or old_route != reviewed or
            _sha(old_route) != row['routeSha256'] or
            _sha(old_att) != row['attestationSha256'] or
            old_ledger != _canonical(row) or
            fresh.classify_marker(project, legacy_dir / fresh.MARKER,
                                  legacy_dir / fresh.ROUTE) != 'candidate' or
            selected['state'] not in ('active', 'restored_old') or
            selected['selectedRouteSha256'] != row['routeSha256']):
        _fail('project_dir_move_selection_changed')
    route = json.loads(old_route)
    if _canonical(route) != old_route:
        _fail('project_dir_move_route_noncanonical')
    if route.get('schema') == ROUTE_V1:
        _fail('project_dir_move_not_selected')
    legacy_root, current_root = wiki_roots(project)
    try:
        new_route_value = rebound_project_dir_route(route, project)
    except Exception as exc:
        raise ProjectDirMoveBlocked('project_dir_move_route_unsupported') from exc
    new_route = _canonical(new_route_value)
    old_roots = wiki_index_roots(route['collections'])
    new_roots = wiki_index_roots(new_route_value['collections'])
    (wiki_name,) = tuple(new_roots)
    catalog_path, store_path = (capacity_store_paths(route['dbPath']) or (None, None))
    if (catalog_path is None or row['catalogDb'] != catalog_path or
            row['bindingStore'] != store_path):
        _fail('project_dir_move_capacity_missing')
    scope, incarnation = route['scopeId'], route['dbIncarnation']
    old_store = GenerationBindingStore(store_path, scope=scope, db_incarnation=incarnation,
                                       roots=old_roots)
    with closing(LayaIndexA(route['dbPath'])) as index, closing(old_store):
        if index.incarnation() != incarnation:
            _fail('project_dir_move_index_changed')
        head, manifest = _head(index, scope)
        bound = old_store.get(scope, head, incarnation)
        if bound is None or bound.source_manifest != manifest:
            _fail('project_dir_move_head_unbound')
        node = tuple(bound.roots[0][2:])
        live = _wiki_node(legacy_root if mode == 'rename' else current_root)
        if mode == 'rename' and live != node:
            _fail('project_dir_move_root_changed')
        provisional = GenerationSourceBinding(
            scope, 'g-' + '0' * 32, manifest, bound.catalog_scan_id,
            bound.catalog_raw_digest, incarnation,
            ((wiki_name, current_root, live[0], live[1]),))
        binding = bind_generation(provisional)
        tag = binding.generation[2:22]
        staged = _stage_clone(index, scope=scope, head=head, manifest=manifest,
                              gen=binding.generation, claim='project-dir-move-' + tag,
                              claim_epoch=0,
                              profile_id=route['vectorProfile']['profileId'])
    temp_store = Path(store_path + STORE_SUFFIX)
    for suffix in ('', '-journal', '-wal', '-shm'):
        Path(str(temp_store) + suffix).unlink(missing_ok=True)
    with closing(GenerationBindingStore(temp_store, scope=scope, db_incarnation=incarnation,
                                        roots=new_roots, create=True)) as store:
        store.put(binding)
    temp_info = os.lstat(temp_store)
    fenced = {**row, 'epoch': row['epoch'] + 1, 'phase': 'upgrading'}
    new_row = {**row, 'epoch': row['epoch'] + 2, 'phase': 'active',
               'collections': new_route_value['collections'],
               'routeSha256': _sha(new_route),
               'indexGeneration': binding.generation,
               'bindingDigest': binding_digest(binding),
               'sourceManifest': manifest,
               'catalogScanId': binding.catalog_scan_id,
               'catalogRawDigest': binding.catalog_raw_digest,
               'vectorRows': staged['rows'],
               'bindingStoreIdentity': [temp_info.st_dev, temp_info.st_ino]}
    fresh.validate_row(new_row, state / fresh.LEDGER)
    record = build.read(state)
    if (record is None or record['identity']['stateRoot'] != str(state) or
            record['identity']['projectRoot'] != str(project) or
            _read(build.record_path(state)) != build._canon(record)):
        _fail('project_dir_move_build_binding_changed')
    new_record = {**record, 'epoch': record['epoch'] + 1, 'updatedNs': time.time_ns(),
                  'identity': {**record['identity'],
                               'collections': new_route_value['collections']}}
    build._validate(new_record)
    decision = {'scopeId': scope, 'dbIncarnation': incarnation,
                'queuePath': route['queuePath'], 'projectRoot': str(project),
                'collections': route['collections'],
                'collectionRoots': {item['name']: item['sourceRoot']
                                    for item in route['collections']}}
    cursor, _grants, _lock = discovery._paths(decision)
    moved_cursor = None
    if os.path.lexists(cursor):
        old_cursor = _read(cursor, discovery.MAX_FILE_BYTES)
        value = json.loads(old_cursor)
        if value.get('collectionRoots', {}).get(wiki_name) != legacy_root:
            _fail('project_dir_move_discovery_changed')
        value['collectionRoots'] = {**value['collectionRoots'], wiki_name: current_root}
        moved_cursor = [str(cursor), old_cursor.decode('utf-8'),
                        discovery._encode(value).decode('utf-8')]
    catalog_meta = {'binding': [catalog_binding(scope, old_roots),
                                catalog_binding(scope, new_roots)],
                    'roots': [json.dumps(old_roots, ensure_ascii=False, sort_keys=True),
                              json.dumps(new_roots, ensure_ascii=False, sort_keys=True)]}
    intent = {'schema': SCHEMA, 'mode': mode, 'projectRoot': str(project),
              'installId': state.name, 'stateRoot': str(state),
              'oldRoute': old_route.decode('utf-8'), 'newRoute': new_route.decode('utf-8'),
              'oldLedger': row, 'fencedLedger': fenced, 'newLedger': new_row,
              'oldMarker': old_marker.decode('utf-8'),
              'newMarker': _canonical(fresh._marker(new_row)).decode('utf-8'),
              'oldBuild': record, 'newBuild': new_record, 'discovery': moved_cursor,
              'catalog': {'path': catalog_path, **catalog_meta},
              'store': {'path': store_path, 'temp': str(temp_store),
                        'identity': [temp_info.st_dev, temp_info.st_ino]},
              'generation': {'base': head, 'gen': binding.generation,
                             'manifest': manifest, 'binding': binding.as_record(),
                             **staged},
              'counts': {'members': staged.pop('members'), 'vectorRows': staged['rows']}}
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
            value['stateRoot'] != str(state) or value['mode'] not in ('rename', 'adopt')):
        _fail('project_dir_move_intent_invalid')
    return value


def _abort(intent):
    """Drop an intent whose fence was never written: nothing selected changed."""
    state = Path(intent['stateRoot'])
    _drop_pointer(intent['projectRoot'])
    temp = Path(intent['store']['temp'])
    for suffix in ('', '-journal'):
        Path(str(temp) + suffix).unlink(missing_ok=True)
    try:
        os.unlink(state / INTENT)
    except FileNotFoundError:
        return
    _fsync(state)


def _fenced(intent):
    """True once the ledger left the intent's old row (the commit point)."""
    import laya_fresh_wiki_origin as fresh
    path = Path(intent['stateRoot']) / fresh.LEDGER
    current = _read(path, 1024 * 1024)
    if current == _canonical(intent['oldLedger']):
        return False
    if current in (_canonical(intent['fencedLedger']), _canonical(intent['newLedger'])):
        return True
    _fail('project_dir_move_ledger_changed')


# ------------------------------------------------------------------ switch

def _expected_replace(path, old, new):
    import laya_inactive_upgrade as inactive
    try:
        inactive._atomic_expected(Path(path), old, new)
    except Exception as exc:
        raise ProjectDirMoveBlocked('project_dir_move_metadata_changed') from exc


def _replace_ledger(state, old_row, new_row):
    import laya_fresh_wiki_origin as fresh
    path = Path(state) / fresh.LEDGER
    with fresh._ledger_lock(path):
        _expected_replace(path, _canonical(old_row), _canonical(new_row))


def _move_folder(intent):
    project = Path(intent['projectRoot'])
    legacy, current = legacy_folder(project), current_folder(project)
    if intent['mode'] == 'rename':
        if os.path.lexists(legacy):
            if os.path.lexists(current):
                _fail('project_dir_move_both_folders')
            os.rename(legacy, current)
            _fsync(project)
        if not project_dir._real_dir(current):
            _fail('project_dir_move_folder_missing')
        return
    if os.path.lexists(legacy):
        for name in sorted(os.listdir(legacy)):
            if os.path.lexists(current / name):
                if not _lock_name(name):
                    _fail('project_dir_conflict')
                os.unlink(legacy / name)
                continue
            os.rename(legacy / name, current / name)
        _fsync(current)
        os.rmdir(legacy)
        _fsync(project)


def _rebind_catalog(intent):
    catalog = intent['catalog']
    with closing(sqlite3.connect(Path(catalog['path']).as_uri() + '?mode=rw', uri=True,
                                 isolation_level=None, timeout=5)) as db:
        db.execute('PRAGMA synchronous=FULL')
        db.execute('BEGIN IMMEDIATE')
        try:
            rows = dict(db.execute(
                "SELECT key,value FROM meta WHERE key IN ('binding','roots')").fetchall())
            old = {'binding': catalog['binding'][0], 'roots': catalog['roots'][0]}
            new = {'binding': catalog['binding'][1], 'roots': catalog['roots'][1]}
            if rows == old:
                for key, value in new.items():
                    db.execute('UPDATE meta SET value=? WHERE key=?', (value, key))
            elif rows != new:
                _fail('project_dir_move_catalog_changed')
            db.execute('COMMIT')
        except BaseException:
            db.execute('ROLLBACK')
            raise


def _swap_store(intent):
    store = intent['store']
    live, temp = Path(store['path']), Path(store['temp'])
    if not os.path.lexists(temp):
        info = os.lstat(live)
        if [info.st_dev, info.st_ino] != store['identity']:
            _fail('project_dir_move_store_changed')
        return
    # A hot rollback journal of the old store is rolled back by one open/close
    # under every writer lock before the file is replaced.
    with closing(sqlite3.connect(live.as_uri() + '?mode=rw', uri=True)) as db:
        db.execute('SELECT count(*) FROM sqlite_master').fetchone()
    if os.path.lexists(str(live) + '-journal'):
        _fail('project_dir_move_store_busy')
    os.rename(temp, live)
    _fsync(live.parent)


def _publish(intent):
    from laya_index_a import LayaIndexA
    route = json.loads(intent['newRoute'])
    generation = intent['generation']
    with closing(LayaIndexA(route['dbPath'])) as index:
        result = index.publish(generation['base'], generation['gen'], generation['claim'],
                               source_manifest=generation['manifest'],
                               claim_digest=generation['claimDigest'],
                               claim_epoch=generation['claimEpoch'], require_vector=True)
    if result not in ('published', 'replayed'):
        _fail('project_dir_move_publish_' + str(result))


def _journal(intent):
    from laya_install_journal import cas_rebind_project_dir, inspect_journal
    journal = Path(intent['stateRoot']) / JOURNAL
    current = inspect_journal(journal)
    old_sha = _sha(intent['oldRoute'].encode('utf-8'))
    new_sha = _sha(intent['newRoute'].encode('utf-8'))
    if current['selectedRouteSha256'] == new_sha:
        return current
    if current['selectedRouteSha256'] != old_sha:
        _fail('project_dir_move_journal_changed')
    return cas_rebind_project_dir(journal, current['epoch'],
                                  intent['oldRoute'].encode('utf-8'),
                                  intent['newRoute'].encode('utf-8'))


def _switch(intent, *, step=None):
    import laya_fresh_wiki_origin as fresh
    import laya_initial_build as build
    state = Path(intent['stateRoot'])
    raw = lambda key: intent[key].encode('utf-8')
    if not _fenced(intent):
        _replace_ledger(state, intent['oldLedger'], intent['fencedLedger'])
    _mark(step, 'fenced')
    _move_folder(intent)
    _mark(step, 'moved')
    _rebind_catalog(intent)
    _swap_store(intent)
    _mark(step, 'stores')
    _publish(intent)
    _mark(step, 'published')
    _expected_replace(state / 'reviewed-route.json', raw('oldRoute'), raw('newRoute'))
    _expected_replace(build.record_path(state), build._canon(intent['oldBuild']),
                      build._canon(intent['newBuild']))
    if intent['discovery'] is not None:
        path, old, new = intent['discovery']
        _expected_replace(path, old.encode('utf-8'), new.encode('utf-8'))
    _mark(step, 'records')
    journal = _journal(intent)
    _mark(step, 'journal')
    folder = current_folder(intent['projectRoot'])
    _expected_replace(folder / fresh.ROUTE, raw('oldRoute'), raw('newRoute'))
    _mark(step, 'route')
    _expected_replace(folder / fresh.MARKER, raw('oldMarker'), raw('newMarker'))
    _mark(step, 'marker')
    current = _read(state / fresh.LEDGER, 1024 * 1024)
    if current != _canonical(intent['newLedger']):
        _replace_ledger(state, intent['fencedLedger'], intent['newLedger'])
    _mark(step, 'ledger')
    return journal


def _verify(intent):
    import laya_fresh_wiki_origin as fresh
    from laya_index_host_route import select_route
    from laya_install_journal import verify_selected_route_read_only
    project = Path(intent['projectRoot'])
    folder = current_folder(project)
    if fresh.classify_marker(project, folder / fresh.MARKER, folder / fresh.ROUTE) != 'candidate':
        _fail('project_dir_move_unverified')
    decision = select_route(str(project), action='recall')
    new_route = json.loads(intent['newRoute'])
    if (decision.get('state') != 'candidate' or
            decision.get('collections') != new_route['collections']):
        _fail('project_dir_move_unverified')
    proof = verify_selected_route_read_only(decision)
    if proof.get('readOnlyAllowed') is not True:
        _fail('project_dir_move_unverified')
    return proof


def _settle(intent):
    state = Path(intent['stateRoot'])
    _drop_pointer(intent['projectRoot'])
    tag = _sha(_canonical(intent))[:20]
    target = state / ('project-dir-move-' + tag + '-settled.json')
    if os.path.lexists(target):
        os.unlink(state / INTENT)
    else:
        os.rename(state / INTENT, target)
    _fsync(state)
    return target


# --------------------------------------------------------------------- run

def _locks(stack, project, state, queue_path):
    """Every selected-writer exclusion the state move takes, plus the sync owner,
    the project settings writer and the wiki card writers."""
    import compile_paths as cp
    import laya_initial_build as build
    import laya_wiki_discovery
    import settings_lock
    import wiki_mutation_lock
    from laya_fresh_wiki_origin import MARKER
    from laya_index_sync import _owner_lock
    from laya_ledger_common import _operation_lock
    from laya_product_install import _install_lock
    import laya_state_move
    stack.enter_context(_install_lock(state))
    if not stack.enter_context(build.owner(state)):
        _fail('initial_build_busy')
    stack.enter_context(_operation_lock(project_dir.runtime_file(project, MARKER)))
    stack.enter_context(laya_state_move._provider_lane())
    stack.enter_context(laya_wiki_discovery._locked(
        state / laya_wiki_discovery.lock_path(queue_path).name, DISCOVERY_WAIT_SECONDS))
    if not stack.enter_context(_owner_lock(Path(queue_path))):
        _fail('sync_owner_busy')
    stack.enter_context(settings_lock.locked(project))
    stack.enter_context(cp.card_write_lock(project))
    stack.enter_context(wiki_mutation_lock.lock(project))


def _queue_path(project, intent):
    if intent is not None:
        return json.loads(intent['newRoute'])['queuePath']
    from laya_fresh_wiki_origin import ROUTE
    return json.loads(_read(project_dir.runtime_file(project, ROUTE), 8192))['queuePath']


def move(project_root, state, *, step=None):
    """Move (or finish moving) one selected project's folder; returns a result dict."""
    project = Path(project_root)
    state = Path(state)
    intent = _load_intent(state, project) if intent_present(state) else None
    mode = intent['mode'] if intent is not None else needed(project)
    if mode is None:
        return {'status': 'project_dir_current'}
    with ExitStack() as stack:
        _locks(stack, project, state, _queue_path(project, intent))
        intent = _load_intent(state, project)
        if intent is not None and not _fenced(intent):
            # Prepared but never fenced: start over from the current selection.
            _abort(intent)
            intent = None
        if intent is None:
            mode = needed(project)
            if mode is None:
                return {'status': 'project_dir_current'}
            from laya_journal_v4 import refuse_while_legacy_hermes
            refuse_while_legacy_hermes(None)
            intent = _prepare(project, state, mode)
            _mark(step, 'prepared')
        else:
            _write_pointer(intent)
        journal = _switch(intent, step=step)
        _verify(intent)
        receipt = _settle(intent)
    try:
        from laya_product_install import _request_build_wake
        wake = _request_build_wake(str(project))
    except Exception:
        wake = None
    return {'status': 'project_dir_move_complete', 'mode': intent['mode'],
            'journalEpoch': journal['epoch'], 'receipt': receipt.name,
            'syncWake': wake}


# ----------------------------------------------------------- owner notices

def gitignore_suggestion(project):
    """``.memvane`` lines the project's own .gitignore lacks for its
    ``.auto-context`` ignore lines (printed, never written)."""
    path = Path(project) / '.gitignore'
    try:
        if path.is_symlink() or not path.is_file():
            return []
        return project_dir.legacy_ignore_lines(path.read_text(encoding='utf-8'))
    except (OSError, UnicodeError):
        return []


def layout_notice(project):
    """One SessionStart line for a project whose folders need the owner, or None."""
    project = Path(project)
    layout = project_dir.layout(project)
    if layout == 'split':
        return ('[memvane] this checkout has .auto-context/ from a branch before the '
                '.memvane/ move: the index stays on .memvane/ and is not changed '
                '(sync, wiki compile and learning wait); switch back or merge the '
                'branch that moved the folder.')
    if layout == 'conflict':
        return ('[memvane] both .memvane/ and .auto-context/ hold settings or wiki cards '
                '(an older branch was merged): MemVane uses .memvane/ and ignores '
                '.auto-context/. Move the leftover cards with git mv into .memvane/wiki '
                'and remove .auto-context/.')
    return None


def owner_steps(project):
    """Lines the owner needs after a finished move: commit and .gitignore."""
    project = Path(project)
    lines = []
    if project_dir.layout(project) != 'current':
        return lines
    suggested = gitignore_suggestion(project)
    lines.append('[memvane] the project folder is now .memvane/; commit the rename: '
                 'git add -A .memvane .auto-context')
    if suggested:
        lines.append('[memvane] .gitignore ignores .auto-context runtime files; add: ' +
                     ', '.join(suggested))
    return lines

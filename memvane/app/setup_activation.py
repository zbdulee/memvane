"""Finalize, activate, recover and restore a 1.0 install selection."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time

from memvane.app import setup_workflow as _workflow

_checked_project = _workflow._checked_project
_fail = _workflow._fail
_install_lock = _workflow._install_lock
_ledger = _workflow._ledger
_private_parent = _workflow._private_parent
_unique = _workflow._unique
_read_private = _workflow._read_private
_request_build_wake = _workflow._request_build_wake
_write_or_verify_private = _workflow._write_or_verify_private
project_dir = _workflow.project_dir

_POINTER_NAMES = ('route.json', 'selection.json')


def foreign_pointers(project):
    """``{name: sha256}`` of project pointers that name an install missing here, else None.

    Only a route the hook classifies ``legacy``/``foreign_install`` (B5)
    qualifies, and only when every present pointer is a 1.x record naming one
    install whose state root does not exist on this machine. A pointer of a
    live install, of another major or of an unreadable shape never does.
    """
    from memvane.journal.api import select_route
    from memvane.kernel import memvane_paths
    from memvane.kernel.memvane_version import same_major
    from memvane.route.api import read_regular
    from memvane.route.types import MAX_RECORD_BYTES, SCHEMA_V4, SELECTION_SCHEMA_V1
    auto = project_dir.folder(project)
    decision = select_route(str(project), action='update')
    if decision.get('state') != 'legacy' or decision.get('reason') != 'foreign_install':
        return None
    found, installs = {}, set()
    for name, schema in zip(_POINTER_NAMES, (SCHEMA_V4, SELECTION_SCHEMA_V1)):
        path = auto / name
        if not os.path.lexists(path):
            continue
        try:
            raw = read_regular(path, MAX_RECORD_BYTES)
            record = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique)
        except (OSError, ValueError, UnicodeError, _workflow.InstallBlocked):
            return None
        if (not isinstance(record, dict) or record.get('schema') != schema
                or type(record.get('installId')) is not str
                or not re.fullmatch(r'[0-9a-f]{32}', record['installId'])):
            return None
        if name == 'route.json':
            state_root = record.get('stateRoot')
            try:
                if not same_major(record.get('stateFormat')):
                    return None
            except ValueError:
                return None
            if not isinstance(state_root, str) or (os.path.isabs(state_root)
                                                   and os.path.lexists(state_root)):
                return None
        if os.path.lexists(memvane_paths.install_root(record['installId'])):
            return None
        installs.add(record['installId'])
        found[name] = hashlib.sha256(raw).hexdigest()
    return found if len(installs) == 1 else None


def _retire_foreign_pointer(path):
    os.unlink(path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def retire_foreign_pointers(plan):
    """Remove the reviewed foreign pointers: the route first, the marker last."""
    foreign = plan.get('replacesForeignPointers')
    if not foreign:
        return
    project = Path(plan['projectRoot'])
    if foreign_pointers(project) != foreign:
        _fail('reviewed_origin_plan_changed')
    for name in _POINTER_NAMES:
        if name in foreign:
            _retire_foreign_pointer(project_dir.folder(project) / name)


def finalize_initial_build(*, project_root, state_root):
    """Seal the ready single-store head in the prepared journal and ledger."""
    from memvane.install.api import _initial_build as build
    from memvane.journal import api as journal
    from memvane.journal.api import schema3_reader_record, seal_reader
    from memvane.app.api import _runtime_dispatch as dispatch
    from memvane.sync import api as sync
    from memvane.store import api as store_api, types as store_types
    from memvane.wiki.workspace.api import file_coordination as workspace_files
    from contextlib import closing
    from memvane.store.api import regular as _regular
    _read_json, _write = _ledger._read_json, _ledger._write
    from memvane.store.api import provider_spec
    from memvane.runtime.api import sync_owner
    state = _private_parent(state_root)
    project = _checked_project(project_root)
    with _install_lock(state), build.owner(state) as owned:
        if not owned:
            _fail('initial_build_busy')
        record = build.read_v1(state)
        if record is None or record['identity']['projectRoot'] != str(project):
            _fail('initial_build_project_mismatch')
        if record['phase'] != 'ready':
            return {'status': 'initial_build_pending', 'phase': record['phase']}
        identity = record['identity']
        with sync_owner(Path(identity['storePath']), wait=False) as owner:
            if owner is None:
                _fail('owner_busy')
            with workspace_files.mutation_lock.lock(project):
                dispatch.resolve('laya-sync', str(project))
                row = journal.inspect_journal(identity['journalPath'])
                attestation, digest = journal.journal_attestation(Path(identity['journalPath']))
                if digest != identity['attestationSha256'] or row['state'] != 'prepared':
                    _fail('initial_build_journal_changed')
                manifest = journal.provider_manifest(attestation, row['vectorProfile'])
                spec = provider_spec(manifest, row['vectorProfile'])
                if spec is None:
                    _fail('initial_build_vector_spec_unavailable')
                decision = journal.select_route(str(project), action='recall')
                if decision.get('state') != 'preview':
                    _fail('initial_build_route_changed')
                from memvane.kernel.errors import _query_only_uri
                with closing(sqlite3.connect(_query_only_uri(Path(identity['storePath'])), uri=True)) as db:
                    db.execute('PRAGMA query_only=ON')
                    meta = store_api.stored_header(db)
                    trees = store_api.stored_worktrees(db, project_root=str(project))
                if meta is None or len(trees) != 1:
                    _fail('initial_build_store_changed')
                repository = json.loads(meta[0], object_pairs_hook=_unique)
                worktree_id = trees[0][0]
                store = store_api.open_store(identity['storePath'], repository=repository,
                                             scope_id=identity['scopeId'], reader=True)
                try:
                    if store_api.store_identity(store)['incarnation'] != identity['storeIncarnation']:
                        _fail('initial_build_store_changed')
                    handle = store.capture_view(worktree_id)
                    pin = store.pin_view(handle)
                    try:
                        head = store.head(handle)
                        vector = store_api.pinned_vector(store, pin)
                        if (head['gen_id'] is None or vector is None
                                or vector['recipe_id'] != spec['recipe_id']
                                or vector['profile_id'] != spec['profile_id']):
                            _fail('initial_build_vectors_pending')
                        tree, baseline, head_row = store_api.read_view_baseline(
                            store, handle, head, worktree_id=worktree_id, head_ordinal=True)
                        if tree is None or head_row is None:
                            _fail('initial_build_store_changed')
                        found, _skipped = sync.schema3_wiki_cards(tree['wiki_root'])
                        paths = store_api.admitted_paths(dict(tree), found)
                        current = {}
                        for path in paths:
                            raw, _node, _root = _regular(tree['wiki_root'], path, store_types.MAX_BODY)
                            current[path] = hashlib.sha256(raw).hexdigest()
                        stale = current != {p: member['blob_sha'] for p, member in baseline['old'].items()}
                        scan = {}
                        observed = sync.schema3_sources(decision, handle, time.monotonic() + 45,
                                                         raw_scan=scan)
                        if 'snapshot' not in scan:
                            _fail('raw_scan_unavailable')
                        stale = stale or not store_api.head_same(store, head, baseline, observed, handle)
                        if stale:
                            # Sources moved after the published head: one more
                            # build pass, then this finalize reseals its floor.
                            return _rescan_stale_build(record, project)
                        if store.head(handle) != head:
                            _fail('initial_build_head_changed')
                        sealed = schema3_reader_record(state, project, identity, decision, worktree_id,
                                                       digest, row, spec, head, head_row, fail=_fail)
                    finally:
                        store.release(pin)
                finally:
                    store.close()
                path = seal_reader(identity['journalPath'], row, sealed, spec, fail=_fail)
                ledger_path = state / 'ledger.json'
                ledger = {'schema': 'memvane-install-ledger-v1', 'epoch': 1,
                          'phase': 'prepared', 'identity': identity}
                if ledger_path.exists() or ledger_path.is_symlink():
                    if _read_json(ledger_path) != ledger:
                        _fail('initial_build_ledger_changed')
                else:
                    _write(ledger_path, ledger, replace=False)
                return {'status': 'initial_build_finalized', 'phase': 'prepared',
                        'journalPath': str(path), 'routeFile': str(state / 'route.json'),
                        'generation': head['gen_id']}


def _rescan_stale_build(record, project):
    """Move a ready but outdated unselected build back to ``building``; one wake."""
    from memvane.install.api import _initial_build as build
    if _workflow._project_pointers_present(project):
        _fail('initial_build_already_selected')
    record = build.advance_v1(record, phase='building', reason='initial_build_out_of_date')
    return {'status': 'initial_build_pending', 'phase': record['phase'],
            'reason': 'initial_build_out_of_date', 'changesApplied': 1,
            'wake': _request_build_wake(str(project))}


def activate_origin_selection(project_root, journal_path, route_bytes):
    """Publish or recover the proved v1 selection under the install lock."""
    from memvane.install.api import _initial_build as build
    from memvane.journal import api as journal_module
    from memvane.journal.api import commit_selection, pin_schema3
    from memvane.sync import api as sync
    from memvane.store import api as store_api
    from memvane.wiki.intake import api as discovery
    from memvane.settings import api as project_dir
    from memvane.wiki.workspace.api import file_coordination as workspace_files
    from memvane.route.api import classify_v4 as _classify_v4
    from memvane.route.types import SELECTION_SCHEMA_V1
    _read_json, _write = _ledger._read_json, _ledger._write
    from memvane.journal.api import reader_lifetime
    from memvane.runtime.api import sync_owner
    project = _checked_project(project_root)
    journal = Path(journal_path)
    state = _private_parent(journal.parent)
    if journal != state / 'journal.sqlite':
        _fail('initial_build_journal_changed')
    route, digest = journal_module.route_record(route_bytes, journal)
    with build.owner(state) as owned:
        if not owned:
            _fail('initial_build_busy')
        record = build.read_v1(state)
        if record is None or record['phase'] != 'ready':
            _fail('initial_build_not_ready')
        identity = record['identity']
        if (identity['projectRoot'] != str(project) or identity['journalPath'] != str(journal)
                or identity['routeSha256'] != digest
                or _read_private(state / 'route.json', 8192) != route_bytes):
            _fail('initial_build_route_changed')
        with sync_owner(Path(identity['storePath']), wait=False) as owner:
            if owner is None:
                _fail('owner_busy')
            with workspace_files.mutation_lock.lock(project), reader_lifetime(identity['installId'], exclusive=False):
                row = journal_module.inspect_journal(journal)
                ledger_path = state / 'ledger.json'
                ledger = _read_json(ledger_path)
                if (not isinstance(ledger, dict) or set(ledger) != {'schema', 'epoch', 'phase', 'identity'}
                        or ledger['schema'] != 'memvane-install-ledger-v1'
                        or ledger['identity'] != identity
                        or type(ledger['epoch']) is not int or ledger['epoch'] < 1
                        or ledger['phase'] not in ('prepared', 'selected')
                        or row['state'] not in ('prepared', 'selected')
                        or (ledger['phase'] == 'selected' and row['state'] != 'selected')
                        or row['reader'] is None or row['routeSha256'] != digest
                        or row['attestationSha256'] != identity['attestationSha256']
                        or row['projectInode'] != project.stat().st_ino
                        or row['storeInode'] != identity['storeNode'][1]
                        or any(row[key] != identity[key] for key in
                               ('projectRoot', 'installId', 'scopeId', 'storePath', 'storeIncarnation'))):
                    _fail('initial_build_ledger_changed')
                attestation, att_sha = journal_module.journal_attestation(journal)
                if att_sha != identity['attestationSha256']:
                    _fail('worker_attestation_changed')
                journal_module.provider_manifest(attestation, row['vectorProfile'])
                decision = _classify_v4(project, project_dir.settings_path(project), route)
                if decision.get('state') != 'candidate':
                    _fail('initial_build_route_changed')
                saved = row['reader']
                with pin_schema3(saved, deadline=time.monotonic() + 45, pin_view=False) as (store, handle, head):
                    floor = saved['head_floor']
                    if (floor is None or head['view_id'] != floor['view_id']
                            or head['epoch'] != floor['epoch']):
                        _fail('initial_build_head_changed')
                    _tree, baseline, _ordinal = store_api.read_view_baseline(store, handle, head)
                    scan = {}
                    observed = sync.schema3_sources(decision, handle, time.monotonic() + 45,
                                                     raw_scan=scan)
                    if 'snapshot' not in scan or not store_api.head_same(
                            store, head, baseline, observed, handle):
                        _fail('initial_build_out_of_date')
                    # Unselected and no pointer yet: an older inert snapshot gives way.
                    inert = row['state'] == 'prepared' and not _workflow._project_pointers_present(project)
                    discovery.seed_source_snapshot(decision, scan['snapshot'], generation=head['gen_id'],
                                                   replace_inert=inert)
                    if store.head(handle) != head:
                        _fail('initial_build_head_changed')
                marker = {'schema': SELECTION_SCHEMA_V1, 'projectRoot': str(project),
                          'installId': identity['installId'], 'routeSha256': digest}
                marker_raw = json.dumps(marker, sort_keys=True, ensure_ascii=False,
                                        separators=(',', ':')).encode('utf-8')
                _write_or_verify_private(project_dir.runtime_file(project, 'selection.json'), marker_raw)
                _write_or_verify_private(project_dir.runtime_file(project, 'route.json'), route_bytes)
                path = commit_selection(journal, row, fail=_fail)
                if ledger['phase'] == 'prepared':
                    ledger = {**ledger, 'phase': 'selected', 'epoch': ledger['epoch'] + 1}
                    _write(ledger_path, ledger, replace=True)
                proof = journal_module.verify_selected_route(decision)
                try:
                    from memvane.learning.api import request_selector_maintenance
                    request_selector_maintenance(decision, force=True, owner=owner)
                except (OSError, ValueError, RuntimeError):
                    pass  # Selection is complete; a later SessionStart can retry.
                return {'status': 'selected', 'phase': 'selected', 'journalPath': str(path),
                        'selectionEpoch': proof['selectionEpoch'],
                        'syncWake': _request_build_wake(str(project)),
                        'gitignoreEntries': ['.memvane/route.json', '.memvane/selection.json']}


def recover_origin_selection(project_root, journal_path, route_bytes):
    """Finish the same partially published or withdrawn v1 selection."""
    _read_json = _ledger._read_json
    state = Path(journal_path).parent
    ledger_path = state / 'ledger.json'
    ledger = _read_json(ledger_path) if os.path.lexists(ledger_path) else None
    if not isinstance(ledger, dict) or ledger.get('phase') in ('restoring', 'restored'):
        return _restore_v1_locked(_checked_project(project_root), state)
    return activate_origin_selection(project_root, journal_path, route_bytes)


def restore_origin_selection(*, project_root, state_root):
    """Withdraw the v1 selection, or abandon an install that was never selected.

    Before activation this abandons the install: the build pointer and build
    record go, so a new prepare can start. After activation it withdraws the
    project pointers before the first selected write; after that write it
    releases the selection only when the attested provider no longer matches
    this host (re-onboarding is then the supported path). The state root,
    store, journal and ledger stay in place.
    """
    project = _checked_project(project_root)
    state = _private_parent(state_root)
    with _install_lock(state):
        return _restore_v1_locked(project, state)


def _provider_changed(journal, path, row):
    """True when this host's provider, model or runtime differs from the attestation."""
    from memvane.journal.types import JournalEvidenceError
    attestation, digest = journal.journal_attestation(path)
    if digest != row['attestationSha256']:
        _fail('worker_attestation_changed')
    try:
        _manifest, available = journal.provider_manifest(
            attestation, row['vectorProfile'], allow_local_unavailable=True)
    except JournalEvidenceError as exc:
        if str(exc) != 'provider_identity_changed':
            raise
        return True
    return not available


def _restored_without_build(journal, project, state):
    """A rerun after the build record went: the journal already says restored."""
    row = journal.inspect_journal(state / 'journal.sqlite')
    if row['projectRoot'] != str(project):
        _fail('initial_build_project_mismatch')
    if row['state'] != 'restored':
        _fail('initial_build_missing')
    return {'status': 'restored', 'changesApplied': 0}


def _restore_v1_locked(project, state):
    from memvane.install.api import _initial_build as build
    from memvane.journal import api as journal
    from memvane.journal.api import restore_selection
    from memvane.route.types import SELECTION_SCHEMA_V1
    _read_json, _write, _fsync_dir = _ledger._read_json, _ledger._write, _ledger._fsync_dir
    from memvane.journal.api import reader_lifetime
    from memvane.runtime.api import sync_owner
    from memvane.wiki.workspace.api import file_coordination as workspace_files
    with build.owner(state) as owned:
        if not owned:
            _fail('initial_build_busy')
        record = build.read_v1(state)
        if record is None:
            return _restored_without_build(journal, project, state)
        if record['identity']['projectRoot'] != str(project):
            _fail('initial_build_project_mismatch')
        identity = record['identity']
        ledger_path = state / 'ledger.json'
        # Finalize writes the ledger; an install abandoned before it has none.
        ledger = _read_json(ledger_path) if os.path.lexists(ledger_path) else None
        if ledger is not None and (
                not isinstance(ledger, dict) or set(ledger) != {'schema', 'epoch', 'phase', 'identity'}
                or ledger['schema'] != 'memvane-install-ledger-v1' or ledger['identity'] != identity
                or type(ledger['epoch']) is not int or ledger['epoch'] < 1
                or ledger['phase'] not in ('prepared', 'selected', 'restoring', 'restored')):
            _fail('initial_build_ledger_changed')
        phase = None if ledger is None else ledger['phase']
        path = state / 'journal.sqlite'
        route_raw = _read_private(state / 'route.json', 8192)
        if hashlib.sha256(route_raw).hexdigest() != identity['routeSha256']:
            _fail('initial_build_route_changed')
        with sync_owner(Path(identity['storePath']), wait=False) as owner:
            if owner is None:
                _fail('owner_busy')
            with workspace_files.mutation_lock.lock(project), reader_lifetime(identity['installId'], exclusive=True):
                row = journal.inspect_journal(path)
                if (row['routeSha256'] != identity['routeSha256']
                        or row['attestationSha256'] != identity['attestationSha256']
                        or any(row[k] != identity[k] for k in
                               ('projectRoot', 'installId', 'scopeId', 'storePath', 'storeIncarnation'))):
                    _fail('initial_build_journal_changed')
                if ((phase is None and row['state'] == 'selected')
                        or (phase == 'prepared' and row['state'] == 'restored')
                        or (phase == 'selected' and row['state'] != 'selected')
                        or (phase == 'restored' and row['state'] != 'restored')):
                    _fail('restore_state_invalid')
                released = False
                if (row['newWriteSeen'] and row['state'] != 'restored'
                        and phase not in ('restoring', 'restored')):
                    # After the first selected write only a provider that no
                    # longer matches releases the selection (re-onboard).
                    if not _provider_changed(journal, path, row):
                        _fail('rollback_forward_only')
                    released = True
                if phase in ('prepared', 'selected'):
                    ledger = {**ledger, 'phase': 'restoring', 'epoch': ledger['epoch'] + 1}
                    _write(ledger_path, ledger, replace=True)
                if row['state'] in ('prepared', 'selected'):
                    restore_selection(path, row, fail=_fail)
                marker = {'schema': SELECTION_SCHEMA_V1, 'projectRoot': str(project),
                          'installId': identity['installId'], 'routeSha256': identity['routeSha256']}
                marker_raw = json.dumps(marker, sort_keys=True, ensure_ascii=False,
                                        separators=(',', ':')).encode('utf-8')
                for name, expected in (('route.json', route_raw), ('selection.json', marker_raw)):
                    target = project_dir.runtime_file(project, name)
                    if not target.exists() and not target.is_symlink():
                        continue
                    before = target.lstat()
                    if _read_private(target, 8192) != expected or not os.path.samestat(before, target.lstat()):
                        _fail('restore_project_pointer_changed')
                    target.unlink()
                    _fsync_dir(target.parent)
                build.retire_project_pointer(identity)
                if ledger is not None and ledger['phase'] != 'restored':
                    ledger = {**ledger, 'phase': 'restored', 'epoch': ledger['epoch'] + 1}
                    _write(ledger_path, ledger, replace=True)
                # Last: a rerun without the record reads the restored journal.
                os.unlink(state / build.V1_BUILD_FILE)
                _fsync_dir(state)
    return {'status': 'released' if released else 'restored', 'changesApplied': 1}

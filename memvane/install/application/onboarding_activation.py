"""Finalize, activate and recover a 1.0 install selection.

The one dispatch check that belongs to the app layer comes in as the
``resolve_sync_target`` keyword port of ``finalize_initial_build``.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time

from memvane.install.application import onboarding_workflow as _workflow
from memvane.install.application.onboarding_restore import _restore_v1_locked
from memvane.settings import api as project_dir

_checked_project = _workflow._checked_project
_fail = _workflow._fail
_install_lock = _workflow._install_lock
_private_parent = _workflow._private_parent
_unique = _workflow._unique
_read_private = _workflow._read_private
_request_build_wake = _workflow._request_build_wake
_write_or_verify_private = _workflow._write_or_verify_private
_project_pointers_present = _workflow._project_pointers_present


def finalize_initial_build(*, project_root, state_root, resolve_sync_target):
    """Seal the ready single-store head in the prepared journal and ledger.

    ``resolve_sync_target(project)`` is the caller's sync dispatch check for
    the registered build; it raises when the wake target does not resolve.
    """
    from memvane.install import initial_build
    from memvane.journal import api as journal
    from memvane.journal.api import schema3_reader_record, seal_reader
    from memvane.wiki.intake import api as intake
    from memvane.store import api as store_api, types as store_types
    from memvane.wiki.workspace import api as workspace_files
    from contextlib import closing
    from memvane.store.api import regular as _regular
    from memvane.install.ledger_common import _read_json, _write
    from memvane.store.api import provider_spec
    from memvane.runtime.api import sync_owner
    state = _private_parent(state_root)
    project = _checked_project(project_root)
    with _install_lock(state), initial_build.owner(state) as owned:
        if not owned:
            _fail('initial_build_busy')
        record = initial_build.read_v1(state)
        if record is None or record['identity']['projectRoot'] != str(project):
            _fail('initial_build_project_mismatch')
        if record['phase'] != 'ready':
            return {'status': 'initial_build_pending', 'phase': record['phase']}
        identity = record['identity']
        with sync_owner(Path(identity['storePath']), wait=False) as owner:
            if owner is None:
                _fail('owner_busy')
            with workspace_files.mutation_lock(project):
                resolve_sync_target(str(project))
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
                        found, _skipped = intake.schema3_wiki_cards(tree['wiki_root'])
                        paths = store_api.admitted_paths(dict(tree), found)
                        current = {}
                        for path in paths:
                            raw, _node, _root = _regular(tree['wiki_root'], path, store_types.MAX_BODY)
                            current[path] = hashlib.sha256(raw).hexdigest()
                        stale = current != {p: member['blob_sha'] for p, member in baseline['old'].items()}
                        scan = {}
                        observed = intake.schema3_sources(decision, handle, time.monotonic() + 45,
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
    from memvane.install import initial_build
    if _project_pointers_present(project):
        _fail('initial_build_already_selected')
    record = initial_build.advance_v1(record, phase='building', reason='initial_build_out_of_date')
    return {'status': 'initial_build_pending', 'phase': record['phase'],
            'reason': 'initial_build_out_of_date', 'changesApplied': 1,
            'wake': _request_build_wake(str(project))}


def activate_origin_selection(project_root, journal_path, route_bytes):
    """Publish or recover the proved v1 selection under the install lock."""
    from memvane.install import initial_build
    from memvane.journal import api as journal_module
    from memvane.journal.api import commit_selection, pin_schema3
    from memvane.wiki.intake import api as intake
    from memvane.store import api as store_api
    from memvane.wiki.intake import api as discovery
    from memvane.settings import api as project_dir
    from memvane.wiki.workspace import api as workspace_files
    from memvane.route.api import classify_v4 as _classify_v4
    from memvane.route.types import SELECTION_SCHEMA_V1
    from memvane.install.ledger_common import _read_json, _write
    from memvane.journal.api import reader_lifetime
    from memvane.runtime.api import sync_owner
    project = _checked_project(project_root)
    journal = Path(journal_path)
    state = _private_parent(journal.parent)
    if journal != state / 'journal.sqlite':
        _fail('initial_build_journal_changed')
    route, digest = journal_module.route_record(route_bytes, journal)
    with initial_build.owner(state) as owned:
        if not owned:
            _fail('initial_build_busy')
        record = initial_build.read_v1(state)
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
            with workspace_files.mutation_lock(project), reader_lifetime(identity['installId'], exclusive=False):
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
                    observed = intake.schema3_sources(decision, handle, time.monotonic() + 45,
                                                     raw_scan=scan)
                    if 'snapshot' not in scan or not store_api.head_same(
                            store, head, baseline, observed, handle):
                        _fail('initial_build_out_of_date')
                    # Unselected and no pointer yet: an older inert snapshot gives way.
                    inert = row['state'] == 'prepared' and not _project_pointers_present(project)
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
    """Finish the same partially published or withdrawn v1 selection.

    Without a ledger (finalize never completed) recover only finishes an
    abandon that already began: the journal says ``restored``. Any other
    unfinalized install refuses with ``initial_build_not_finalized``;
    abandoning it takes the approved restore.
    """
    from memvane.install.ledger_common import _read_json
    from memvane.journal.api import inspect_journal
    state = Path(journal_path).parent
    ledger_path = state / 'ledger.json'
    ledger = _read_json(ledger_path) if os.path.lexists(ledger_path) else None
    if ledger is None and inspect_journal(state / 'journal.sqlite')['state'] != 'restored':
        _fail('initial_build_not_finalized')
    if not isinstance(ledger, dict) or ledger.get('phase') in ('restoring', 'restored'):
        return _restore_v1_locked(_checked_project(project_root), state)
    return activate_origin_selection(project_root, journal_path, route_bytes)


def select_origin(*, project_root, state_root, route_file=None, recover=False):
    """Activate, or with ``recover`` recover, from one install state root.

    The route bytes come from ``route_file`` or the state root's staged
    ``route.json``; the step runs under the state root's install lock. The
    caller has already checked the user's approval.
    """
    state = _private_parent(state_root)
    project = _checked_project(project_root)
    route = _read_private(state / 'route.json' if route_file is None else route_file, 8192)
    step = recover_origin_selection if recover else activate_origin_selection
    with _install_lock(state):
        return step(project, state / 'journal.sqlite', route)

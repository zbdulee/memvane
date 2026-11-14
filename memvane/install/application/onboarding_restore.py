"""Replace foreign pointers, and withdraw or abandon a 1.0 install selection."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re

from memvane.install.application import onboarding_workflow as _workflow
from memvane.journal.types import V1_BUILD_FILE

_checked_project = _workflow._checked_project
_fail = _workflow._fail
_install_lock = _workflow._install_lock
_private_parent = _workflow._private_parent
_unique = _workflow._unique
_read_private = _workflow._read_private
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
    from memvane.install import initial_build
    from memvane.journal import api as journal
    from memvane.journal.api import restore_selection
    from memvane.route.types import SELECTION_SCHEMA_V1
    from memvane.install.ledger_common import _read_json, _write, _fsync_dir
    from memvane.journal.api import reader_lifetime
    from memvane.runtime.api import sync_owner
    from memvane.wiki.workspace import api as workspace_files
    with initial_build.owner(state) as owned:
        if not owned:
            _fail('initial_build_busy')
        record = initial_build.read_v1(state)
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
            with workspace_files.mutation_lock(project), reader_lifetime(identity['installId'], exclusive=True):
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
                initial_build.retire_project_pointer(identity)
                if ledger is not None and ledger['phase'] != 'restored':
                    ledger = {**ledger, 'phase': 'restored', 'epoch': ledger['epoch'] + 1}
                    _write(ledger_path, ledger, replace=True)
                # Last: a rerun without the record reads the restored journal.
                os.unlink(state / V1_BUILD_FILE)
                _fsync_dir(state)
    return {'status': 'released' if released else 'restored', 'changesApplied': 1}

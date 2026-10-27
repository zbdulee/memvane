"""One-time convergence of a selected install to the fresh-wiki shape.

A selected install converges in two parts, in this order:

1. QMD-origin detach. A ``qmd-cutover.json`` (v3, ``active``) selection
   becomes the fresh-wiki selection it is equivalent to. Its candidate,
   claim queue, catalog, binding store, route and journal stay as they are;
   only the QMD restore path is forfeited (every such install already has a
   previous bundle row, so ``restore-index-origin`` was forward-only).
2. Settings binding conversion. A route whose ``settingsSha256`` still holds
   the settings file's byte digest is rebound to the resolved settings
   identity (``laya_index_host_route.settings_identity_digest``) through the
   recoverable ``laya_settings_rebind`` intent, with the same settings bytes
   on both sides. Fresh-origin installs run this part only.

Detach steps, all under the locks a selected bundle upgrade takes, in its
order (install lock, project operation lock, provider lane, then the build
owner for the build record):

``settle``  archive a finished ``active-upgrade-intent.json`` as its settled
            receipt (``restore-active-index-bundle`` to the bundle before it
            is forfeited); an unfinished one refuses.
``build``   rewrite ``initial-build.json`` identity to the fresh-wiki shape:
            no ``sourceDb``/``sourceFormat``, ``originMode`` set, the
            ledger's collections. The original bytes are kept beside it.
``ledger``  write ``fresh-wiki-cutover.json``: the v3 row without its QMD
            fields, ``epoch + 1``, ``phase`` active.
``marker``  replace ``laya-index-selection.json`` with the fresh marker
            (``os.replace``). This is the commit point: before it the QMD
            marker names the still active QMD ledger, after it the fresh
            marker names the fresh ledger, and both classify ``candidate``.
``verify``  the fresh classifier says ``candidate``; otherwise the QMD marker
            is put back while the QMD ledger still exists.
``retire``  rename ``qmd-cutover.json`` to ``qmd-cutover.detached-<epoch>.json``.
``convert`` the settings binding conversion above.

Every step reads the files first and does nothing when its result is already
there, so a crashed run resumes from what it finds. A failure before the
commit point removes the fresh ledger and restores the build record; after it
the run only goes forward. QMD backups, ``qmd-cutover.lock`` and the user's
QMD stores are left in place.

``plan`` only reads. ``apply`` re-plans under the locks and runs the steps of
the accepted proposal. SessionStart asks the bundle-upgrade worker to run
``auto_converge`` once for an install that still needs either part.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import time

from laya_ledger_common import (QMD_LEDGER, QMD_MARKER_SCHEMA, _canon,
                                _fsync_dir, _ledger_lock, _operation_lock,
                                _private_file, _read_json, _write)

PLAN_SCHEMA = 'laya-origin-detach-plan-v1'
STATUS_SCHEMA = 'laya-origin-detach-status-v1'
STATUS = 'origin-detach-status.json'
BUILD_BACKUP = 'initial-build.pre-detach.json'
DETACHED = 'qmd-cutover.detached-%d.json'
MARKER = 'laya-index-selection.json'
ROUTE = 'laya-index-route.json'
REVIEWED = 'reviewed-route.json'
SETTINGS = 'settings.json'
STEPS = ('settle', 'build', 'ledger', 'marker', 'verify', 'retire', 'convert')
DETACH_STEPS = frozenset(STEPS) - {'convert'}
# v3 ledger fields the fresh-wiki ledger does not have.
QMD_ONLY = frozenset(('backupDb', 'backupIdentity', 'sourceDb', 'sourceDigest',
                      'sourceFormat', 'sourceIdentity'))
FRESH_MODE = 'fresh_wiki_only'
RETRY_SECONDS = 600
REFUSED_RETRY_SECONDS = 6 * 3600
DEFER_RETRY_SECONDS = 120
DEFERRALS = frozenset(('install_busy', 'cutover_operation_busy', 'selected_route_busy',
                       'initial_build_busy', 'learning_compute_busy',
                       'learning_inference_deadline'))


class DetachRefused(RuntimeError):
    def __init__(self, reason, **detail):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


def _fail(reason, **detail):
    raise DetachRefused(reason, **detail)


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _exists(path):
    return path.exists() or path.is_symlink()


def _project(project_root):
    project = Path(project_root)
    if (not project.is_absolute() or project.is_symlink() or
            project.resolve(strict=True) != project):
        _fail('invalid_project_root')
    return project


# ------------------------------------------------------------- observation

def _state_of(project):
    """(state root, route bytes, route record) named by the project route."""
    route_path = project / '.auto-context' / ROUTE
    if not _exists(route_path):
        _fail('selected_route_missing')
    raw = _private_file(route_path, 8192)
    route = json.loads(raw.decode('utf-8'))
    journal = Path(route.get('journalPath', '')) if isinstance(route, dict) else Path()
    if (not isinstance(route, dict) or not journal.is_absolute() or
            journal.name != 'install-journal.sqlite' or
            route.get('projectRoot') != str(project)):
        _fail('selected_route_invalid')
    return journal.parent, raw, route


def _fresh_target(row, fresh_path):
    """The fresh-wiki ledger row equivalent to an active v3 QMD-origin row."""
    import laya_fresh_wiki_origin as fresh
    import laya_qmd_cutover as qmd
    if (row.get('schema') != qmd.SCHEMA_V3 or set(row) != qmd.V3_FIELDS or
            row.get('phase') != 'active'):
        # Replacement-evidence, retired and upgrading rows are not detached.
        _fail('qmd_origin_not_detachable', phase=row.get('phase'),
              schema=row.get('schema'))
    if set(row) - fresh.FIELDS != QMD_ONLY or fresh.FIELDS - set(row):
        _fail('qmd_origin_fields_unexpected')
    target = {key: value for key, value in row.items() if key not in QMD_ONLY}
    target.update(schema=fresh.SCHEMA, epoch=row['epoch'] + 1, phase='active')
    try:
        return fresh.validate_row(target, fresh_path)
    except fresh.FreshWikiBlocked as exc:
        _fail('qmd_origin_not_fresh_shaped', ledgerReason=str(exc))


def _build_target(record, project, state, collections):
    """(identity to write or None when already aligned)."""
    identity = record['identity']
    if (identity.get('projectRoot') != str(project) or
            identity.get('stateRoot') != str(state)):
        _fail('initial_build_binding_changed')
    if 'replacementEvidenceSha256' in identity:
        _fail('qmd_origin_replacement_evidence_bound')
    target = {key: value for key, value in identity.items()
              if key not in ('sourceDb', 'sourceFormat')}
    target.update(originMode=FRESH_MODE, collections=collections)
    return None if target == identity else target


def _settle_receipt(project, state):
    """Settled receipt name of a leftover active-upgrade intent, or None."""
    import laya_active_upgrade as active
    if not _exists(state / active.INTENT):
        return None
    try:
        intent = active._load_intent(state, project)
        return active.finished_intent_receipt(state, project, intent).name
    except Exception as exc:
        _fail('active_upgrade_unsettled', error=str(exc) or type(exc).__name__)


def _converted_route(project, route):
    from laya_index_host_route import settings_identity_digest
    return _canon({**route, 'settingsSha256':
                   settings_identity_digest(project, route['collections'])})


def _observe(project):
    """Everything ``plan`` reports, read without any lock or write."""
    import laya_fresh_wiki_origin as fresh
    import laya_initial_build as build
    import laya_settings_rebind as rebind
    from laya_index_host_route import (MAX_SETTINGS_BYTES, _read_regular,
                                       select_route, settings_binding_form,
                                       settings_identity_digest)
    from laya_install_journal import inspect_journal
    state, route_raw, route = _state_of(project)
    auto = project / '.auto-context'
    qmd_path, fresh_path = state / QMD_LEDGER, state / fresh.LEDGER
    try:
        intent = rebind.load_intent(state)
    except rebind.RebindRefused as exc:
        _fail(exc.reason)
    if intent is not None and not rebind.is_conversion(intent):
        _fail('settings_rebind_in_progress')
    decision = select_route(str(project), action='recall')
    # An interrupted conversion is ``rebinding`` between its steps and
    # ``candidate`` before the first and after the last binding moved.
    if (decision.get('state') not in (('rebinding', 'candidate') if intent
                                      else ('candidate',)) or
            decision.get('projectRoot') != str(project) or
            decision.get('journalPath') != str(state / 'install-journal.sqlite')):
        _fail('selected_route_not_verified', routeState=decision.get('state'),
              routeReason=decision.get('reason'))
    marker_raw = _private_file(auto / MARKER, 8192)
    marker = json.loads(marker_raw.decode('utf-8'))
    schema = marker.get('schema') if isinstance(marker, dict) else None
    view = {'project': project, 'state': state, 'routeRaw': route_raw, 'route': route,
            'intent': intent, 'steps': [], 'forfeits': []}
    if _exists(qmd_path):
        import laya_qmd_cutover as qmd
        if intent is not None:
            _fail('origin_detach_state_unexpected')
        row = qmd._ledger(qmd_path)
        target = _fresh_target(row, fresh_path)
        journal = inspect_journal(state / 'install-journal.sqlite')
        if (journal['state'] not in ('active', 'restored_old') or
                journal['selectedRouteSha256'] != row['routeSha256'] or
                journal['providerAttestationSha256'] != row['attestationSha256'] or
                journal['providerFileIdentitySha256'] != row['providerFileIdentitySha256'] or
                _sha(route_raw) != row['routeSha256'] or
                _private_file(state / REVIEWED, 8192) != route_raw):
            _fail('qmd_origin_bindings_inconsistent')
        if schema == QMD_MARKER_SCHEMA and marker_raw == _canon(qmd._marker(row)):
            if _exists(fresh_path):
                if fresh._load(fresh_path) != target:
                    _fail('origin_detach_fresh_ledger_conflict')
            else:
                view['steps'].append('ledger')
            view['steps'] += ['marker', 'verify', 'retire']
            receipt = _settle_receipt(project, state)
            if receipt is not None:
                view['steps'].append('settle')
                view['settleReceipt'] = receipt
                view['forfeits'].append('active_bundle_restore')
        elif (schema == fresh.MARKER_SCHEMA and _exists(fresh_path) and
              fresh._load(fresh_path) == target and
              marker_raw == _canon(fresh._marker(target))):
            view['steps'] += ['verify', 'retire']
        else:
            _fail('origin_detach_state_unexpected')
        record = build.read(state)
        if record is not None:
            identity = _build_target(record, project, state, target['collections'])
            if identity is not None:
                view['steps'].append('build')
                view['buildIdentity'] = {
                    'removed': sorted(set(record['identity']) - set(identity)),
                    'added': sorted(set(identity) - set(record['identity'])),
                    'collectionsChanged':
                        record['identity']['collections'] != identity['collections']}
        view.update(origin='qmd', qmdLedger=row, freshLedger=target)
        view['forfeits'].append('qmd_origin_restore')
    elif _exists(fresh_path):
        row = fresh._load(fresh_path)
        if row['phase'] != 'active' or schema != fresh.MARKER_SCHEMA:
            _fail('origin_detach_state_unexpected')
        view.update(origin='fresh', freshLedger=row)
    else:
        _fail('selection_ledger_missing')
    settings = _read_regular(auto / SETTINGS, MAX_SETTINGS_BYTES)
    if intent is not None:
        view['bindingForm'] = 'converting'
        view['convertedRouteSha256'] = _sha(intent['newRoute'].encode('utf-8'))
        view['steps'].append('convert')
    else:
        form = settings_binding_form(route, settings)
        if form is None:
            _fail('selected_route_settings_changed')
        view['bindingForm'] = form
        if form == 'bytes':
            if view['origin'] == 'fresh':
                receipt = _settle_receipt(project, state)
                if receipt is not None:
                    view['steps'].append('settle')
                    view['settleReceipt'] = receipt
                    view['forfeits'].append('active_bundle_restore')
            view['convertedRouteSha256'] = _sha(_converted_route(project, route))
            view['steps'].append('convert')
    view['settingsIdentitySha256'] = settings_identity_digest(project,
                                                              route['collections'])
    view['steps'] = [step for step in STEPS if step in view['steps']]
    return view


def _proposal(view):
    proposal = {'schema': PLAN_SCHEMA, 'projectRoot': str(view['project']),
                'stateRoot': str(view['state']), 'origin': view['origin'],
                'bindingForm': view['bindingForm'], 'steps': view['steps'],
                'routeSha256': _sha(view['routeRaw']),
                'settingsIdentitySha256': view['settingsIdentitySha256'],
                'forfeits': sorted(set(view['forfeits']))}
    for key in ('convertedRouteSha256', 'settleReceipt', 'buildIdentity'):
        if key in view:
            proposal[key] = view[key]
    if view['origin'] == 'qmd':
        proposal['freshLedgerSha256'] = _sha(_canon(view['freshLedger']))
        proposal['qmdLedgerEpoch'] = view['qmdLedger']['epoch']
    return proposal


def plan(project_root):
    """Read-only proposal: ``review_required``, ``complete`` or ``refused``."""
    try:
        project = _project(project_root)
        proposal = _proposal(_observe(project))
    except DetachRefused as exc:
        return {'status': 'refused', 'reason': exc.reason, 'projectRoot': str(project_root),
                'changesApplied': 0, **exc.detail}
    except Exception as exc:
        return {'status': 'refused', 'reason': 'origin_detach_probe_failed',
                'error': str(exc) or type(exc).__name__,
                'projectRoot': str(project_root), 'changesApplied': 0}
    status = 'review_required' if proposal['steps'] else 'complete'
    return {'status': status, 'changesApplied': 0, **proposal,
            **({'proposalSha256': _sha(_canon(proposal))} if proposal['steps'] else {})}


# ------------------------------------------------------------------ writer

def _provider_lane():
    """Excludes selected-index writers (the sync owner holds this lane)."""
    from context_learning.resource_coordination import Coordinator
    return Coordinator().quiesce_provider()


@contextmanager
def locks(project, state):
    """The locks a selected bundle upgrade takes, in the same order."""
    from laya_product_install import _install_lock
    with _install_lock(Path(state)), \
            _operation_lock(Path(project) / '.auto-context' / MARKER), _provider_lane():
        yield


def _step_settle(view):
    import laya_settings_rebind as rebind
    try:
        rebind._settle_active_upgrade(view['project'], view['state'])
    except rebind.RebindRefused as exc:
        _fail(exc.reason, **exc.detail)


def _step_build(view):
    import laya_initial_build as build
    state = view['state']
    with build.owner(state) as owned:
        if not owned:
            _fail('initial_build_busy')
        record = build.read(state)
        if record is None:
            return
        identity = _build_target(record, view['project'], state,
                                 view['freshLedger']['collections'])
        if identity is None:
            return
        path = build.record_path(state)
        raw = _private_file(path, build.MAX_RECORD_BYTES)
        backup = state / BUILD_BACKUP
        if _exists(backup):
            if _private_file(backup, build.MAX_RECORD_BYTES) != raw:
                _fail('origin_detach_build_backup_conflict')
        else:
            _write(backup, raw, replace=False)
        updated = {**record, 'epoch': record['epoch'] + 1, 'updatedNs': time.time_ns(),
                   'identity': identity}
        build._validate(updated)
        build._write_json(path, updated, replace=True)


def _step_ledger(view):
    import laya_fresh_wiki_origin as fresh
    path = view['state'] / fresh.LEDGER
    with _ledger_lock(path):
        if not _exists(path):
            _write(path, view['freshLedger'], replace=False)
        if fresh._load(path) != view['freshLedger']:
            _fail('origin_detach_fresh_ledger_conflict')


def _qmd_marker(view):
    import laya_qmd_cutover as qmd
    return _canon(qmd._marker(view['qmdLedger']))


def _fresh_marker(view):
    import laya_fresh_wiki_origin as fresh
    return _canon(fresh._marker(view['freshLedger']))


def _step_marker(view):
    path = view['project'] / '.auto-context' / MARKER
    current = _private_file(path, 8192)
    if current == _fresh_marker(view):
        return
    if current != _qmd_marker(view):
        _fail('origin_detach_marker_changed')
    # Commit point: one atomic rename between two candidate selections.
    _write(path, _fresh_marker(view), replace=True)


def _step_verify(view):
    import laya_fresh_wiki_origin as fresh
    auto = view['project'] / '.auto-context'
    if fresh.classify_marker(view['project'], auto / MARKER, auto / ROUTE) == 'candidate':
        return
    if _exists(view['state'] / QMD_LEDGER):
        _write(auto / MARKER, _qmd_marker(view), replace=True)
    _fail('origin_detach_unverified')


def _step_retire(view):
    state = view['state']
    source, target = state / QMD_LEDGER, state / (DETACHED % view['qmdLedger']['epoch'])
    if not _exists(source):
        return
    if _private_file(view['project'] / '.auto-context' / MARKER, 8192) != _fresh_marker(view):
        _fail('origin_detach_marker_changed')
    if _exists(target):
        _fail('origin_detach_receipt_exists')
    os.rename(source, target)
    _fsync_dir(state)


def _step_convert(view, proposal_sha256):
    import laya_settings_rebind as rebind
    from laya_index_host_route import MAX_SETTINGS_BYTES, _read_regular
    state = view['state']
    try:
        intent = rebind.load_intent(state)
        if intent is None:
            settings = _read_regular(view['project'] / '.auto-context' / SETTINGS,
                                     MAX_SETTINGS_BYTES)
            intent = rebind.begin(view['project'], proposal_sha256, settings)
        if not rebind.is_conversion(intent):
            _fail('settings_rebind_in_progress')
        return rebind.forward(intent)
    except rebind.RebindRefused as exc:
        _fail(exc.reason, **exc.detail)


def _steps(view, proposal_sha256):
    """Ordered step table; tests interrupt a run by wrapping one entry."""
    return {'settle': lambda: _step_settle(view),
            'build': lambda: _step_build(view),
            'ledger': lambda: _step_ledger(view),
            'marker': lambda: _step_marker(view),
            'verify': lambda: _step_verify(view),
            'retire': lambda: _step_retire(view),
            'convert': lambda: _step_convert(view, proposal_sha256)}


def _committed(view):
    """Whether the project marker already names the fresh ledger."""
    try:
        return _private_file(view['project'] / '.auto-context' / MARKER,
                             8192) == _fresh_marker(view)
    except Exception:
        return False


def _rollback(view):
    """Undo the pre-commit detach writes; refuses after the commit point."""
    import laya_fresh_wiki_origin as fresh
    import laya_initial_build as build
    if view.get('origin') != 'qmd':
        _fail('origin_detach_not_pending')
    if _committed(view):
        _fail('origin_detach_forward_only')
    state = view['state']
    ledger = state / fresh.LEDGER
    if _exists(ledger):
        with _ledger_lock(ledger):
            if fresh._load(ledger) != view['freshLedger']:
                _fail('origin_detach_fresh_ledger_conflict')
            ledger.unlink()
            _fsync_dir(state)
    backup = state / BUILD_BACKUP
    if _exists(backup):
        with build.owner(state) as owned:
            if not owned:
                _fail('initial_build_busy')
            original = json.loads(_private_file(backup, build.MAX_RECORD_BYTES))
            record = build.read(state)
            if record is not None and record['identity'] != original['identity']:
                if _build_target(original, view['project'], state,
                                 view['freshLedger']['collections']) != record['identity']:
                    _fail('origin_detach_build_changed')
                build._write_json(build.record_path(state),
                                  {**original, 'epoch': record['epoch'] + 1,
                                   'updatedNs': time.time_ns()}, replace=True)
            backup.unlink()
            _fsync_dir(state)


def apply(project_root, accepted_proposal_sha256, *, rollback=False):
    """Run the accepted proposal's steps; resumable by planning again."""
    try:
        project = _project(project_root)
        state, _raw, _route = _state_of(project)
        with locks(project, state):
            view = _observe(project)
            if rollback:
                _rollback(view)
                return {'status': 'rolled_back', 'projectRoot': str(project),
                        'stateRoot': str(state), 'changesApplied': 1}
            proposal = _proposal(view)
            if not proposal['steps']:
                return {'status': 'complete', 'changesApplied': 0, **proposal}
            digest = _sha(_canon(proposal))
            if accepted_proposal_sha256 != digest:
                _fail('proposal_acceptance_required', proposalSha256=digest)
            steps = _steps(view, digest)
            done = []
            try:
                for name in proposal['steps']:
                    steps[name]()
                    done.append(name)
            except BaseException:
                if (view['origin'] == 'qmd' and not _committed(view) and
                        _exists(state / QMD_LEDGER)):
                    try:
                        _rollback(view)
                    except Exception:
                        pass  # the run resumes or rolls back from what it finds
                raise
            after = _proposal(_observe(project))
            if after['steps']:
                _fail('origin_detach_unconfirmed', remaining=after['steps'])
            return {'status': 'converged', 'projectRoot': str(project),
                    'stateRoot': str(state), 'origin': view['origin'],
                    'stepsApplied': done, 'forfeits': proposal['forfeits'],
                    'routeSha256': after['routeSha256'], 'changesApplied': 1}
    except DetachRefused as exc:
        return {'status': 'refused', 'reason': exc.reason, 'projectRoot': str(project_root),
                'changesApplied': 0, **exc.detail}
    except Exception as exc:
        reason = str(exc) or type(exc).__name__
        return {'status': 'refused', 'reason': reason if reason in DEFERRALS
                else 'origin_detach_step_failed', 'error': reason,
                'projectRoot': str(project_root), 'changesApplied': 0}


# ----------------------------------------------------------- automatic run

def needs_convergence(project_root, journal_path):
    """Cheap hook-side check: a QMD-origin ledger, a byte-bound route or an
    interrupted conversion remains for this selected install."""
    from laya_index_host_route import (MAX_SETTINGS_BYTES, _read_regular,
                                       settings_binding_form)
    state = Path(journal_path).parent
    if _exists(state / QMD_LEDGER):
        return True
    if _exists(state / 'settings-rebind-intent.json'):
        import laya_settings_rebind as rebind
        intent = rebind.load_intent(state)
        return intent is not None and rebind.is_conversion(intent)
    auto = Path(project_root) / '.auto-context'
    route = json.loads(_private_file(auto / ROUTE, 8192).decode('utf-8'))
    settings = _read_regular(auto / SETTINGS, MAX_SETTINGS_BYTES)
    return settings_binding_form(route, settings) == 'bytes'


def read_status(state):
    try:
        value = _read_json(Path(state) / STATUS)
    except Exception:
        return None
    if (not isinstance(value, dict) or value.get('schema') != STATUS_SCHEMA or
            value.get('status') not in ('converged', 'complete', 'refused', 'failed',
                                        'deferred') or
            type(value.get('updatedNs')) is not int or
            type(value.get('attempts')) is not int):
        return None
    return value


def _write_status(state, **fields):
    value = {'schema': STATUS_SCHEMA, 'updatedNs': time.time_ns(), **fields}
    _write(Path(state) / STATUS, value, replace=True)
    return value


def _due(status, now):
    if status is None:
        return True
    age = (now - status['updatedNs']) / 1e9
    if status['status'] == 'refused':
        return age >= REFUSED_RETRY_SECONDS
    if status['status'] == 'deferred':
        return age >= DEFER_RETRY_SECONDS
    if status['status'] == 'failed':
        return age >= min(REFUSED_RETRY_SECONDS, RETRY_SECONDS * max(1, status['attempts']))
    return age >= RETRY_SECONDS


def request(project_root, journal_path, *, spawn=None, now=None):
    """Hook side: never waits; starts the bundle-upgrade worker when due."""
    if not needs_convergence(project_root, journal_path):
        return {'status': 'complete'}
    status = read_status(Path(journal_path).parent)
    if not _due(status, time.time_ns() if now is None else now):
        return {'status': status['status'], 'reason': status.get('reason', '')}
    import laya_bundle_upgrade
    if spawn is None:
        if laya_bundle_upgrade._fixture():
            return {'status': 'queued', 'reason': 'fixture_no_spawn'}
        spawn = laya_bundle_upgrade._spawn
    spawn(str(project_root))
    return {'status': 'queued', 'reason': 'origin_convergence'}


def auto_converge(project_root):
    """Worker side: plan and apply once, log, and never raise.

    A failure leaves the install as ``apply`` left it: unchanged before the
    commit point, or a fresh selection that a later run resumes. Either way
    the route stays searchable.
    """
    import laya_bundle_upgrade
    try:
        project = _project(project_root)
        state, _raw, _route = _state_of(project)
        if not needs_convergence(project, state / 'install-journal.sqlite'):
            return {'status': 'complete'}
    except Exception as exc:
        return {'status': 'skipped', 'reason': str(exc) or type(exc).__name__}
    log = lambda message: laya_bundle_upgrade._log(state, 'origin-detach ' + message)
    prior = read_status(state)
    attempts = (prior or {}).get('attempts', 0) + 1
    planned = plan(project)
    if planned['status'] == 'complete':
        result = planned
    elif planned['status'] != 'review_required':
        result = planned
    else:
        log('apply steps=%s forfeits=%s' % (','.join(planned['steps']),
                                            ','.join(planned['forfeits']) or '-'))
        result = apply(project, planned['proposalSha256'])
    reason = result.get('reason', '')
    if result['status'] in ('converged', 'complete'):
        status, attempts = result['status'], 0
    elif reason in DEFERRALS:
        status, attempts = 'deferred', attempts - 1
    elif result['status'] == 'refused' and reason != 'origin_detach_step_failed':
        status = 'refused'
    else:
        status = 'failed'
    try:
        _write_status(state, status=status, reason=reason, attempts=attempts)
    except Exception:
        pass
    log('%s %s' % (status, reason or result.get('routeSha256', '')))
    return result

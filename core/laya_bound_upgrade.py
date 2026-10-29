"""Recoverable bundle replacement for a bound, not yet selected Laya build.

A bound build is a sealed candidate tied to a ``prepared`` selection ledger
(``fresh-wiki-cutover.json``) and the project marker; no Laya route is
selected yet. This replaces only the reviewed route, the provider attestation
and the journal's pending bundle, and restates the three
ledger/marker/record digests that name them. The generation, vector profile,
scope and stores never move; the build stays ``bound``. A state root that
still holds a QMD-origin ledger refuses as ``qmd_origin_detach_required``.

While the files switch, the marker no longer matches the ledger or journal
and the selector classifies the project ``blocked``; a crash leaves the intent
for the next run of this same action to finish.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import laya_active_upgrade as active
import laya_initial_build as build
import laya_inactive_upgrade as inactive
import project_dir
from laya_ledger_common import _operation_lock
from laya_install_journal import (PRODUCTION_ATTESTATION, _canon as journal_canon,
                                  _provider_file_identity, _route as checked_route,
                                  cas_replace_inactive_candidate, inspect_journal,
                                  preflight_product_candidate)
from laya_product_bundle import (PRODUCTION_ATTESTATION_SCHEMA, BundleEvidenceError,
                                 adopt_host_loaded_plugin,
                                 inspect_full_plugin_candidate,
                                 validate_production_provider_attestation)

INTENT = 'bound-upgrade-intent.json'
SCHEMA = 'laya-bound-upgrade-intent-v1'
LIMIT = 3 * 1024 * 1024
# A bound ledger changes only these; the phase stays ``prepared``.
LEDGER_CHANGES = frozenset(('epoch', 'routeSha256', 'attestationSha256',
                            'providerFileIdentitySha256'))
RECORD_CHANGES = frozenset(('routeSha256', 'attestationSha256',
                            'providerProposalSha256'))


def _fail(reason):
    inactive._fail(reason)


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _read(path, limit=8192):
    from laya_product_install import _read_private
    return _read_private(Path(path), limit)


def _load_intent(state, project):
    value = build._read_json(state / INTENT, max_bytes=LIMIT)
    if value is None:
        return None
    if (not isinstance(value, dict) or value.get('schema') != SCHEMA or
            value.get('projectRoot') != str(project) or
            value.get('stateRoot') != str(state) or
            value.get('origin') not in ('fresh', 'qmd') or
            any(type(value.get(key)) is not str for key in
                ('oldRoute', 'newRoute', 'oldAttestation', 'newAttestation',
                 'oldMarker', 'newMarker', 'backup', 'reviewSha256')) or
            any(not isinstance(value.get(key), dict) for key in
                ('oldRecord', 'oldLedger', 'newLedger', 'newIdentity', 'newProof',
                 'counts'))):
        _fail('bound_upgrade_intent_invalid')
    return value


def _check_intent(intent):
    """The intent alone must describe one exact, scoped replacement."""
    old, new = intent['oldLedger'], intent['newLedger']
    old_route = json.loads(intent['oldRoute'])
    new_route = json.loads(intent['newRoute'])
    record = intent['oldRecord']
    ledger = active._Ledger(intent['origin'])
    if (set(old) != set(new) or old['phase'] != 'prepared' or
            new['phase'] != 'prepared' or new['epoch'] != old['epoch'] + 1 or
            any(old[key] != new[key] for key in set(old) - LEDGER_CHANGES) or
            set(old_route) != set(new_route) or
            any(old_route[key] != new_route[key]
                for key in set(old_route) - active.REPLACEABLE) or
            old['routeSha256'] != _sha(intent['oldRoute'].encode('utf-8')) or
            new['routeSha256'] != _sha(intent['newRoute'].encode('utf-8')) or
            old['attestationSha256'] != _sha(intent['oldAttestation'].encode('utf-8')) or
            new['attestationSha256'] != _sha(intent['newAttestation'].encode('utf-8')) or
            intent['oldMarker'] != active._bytes(ledger.marker(old)).decode('utf-8') or
            intent['newMarker'] != active._bytes(ledger.marker(new)).decode('utf-8') or
            record.get('phase') != 'bound' or
            set(record['identity']) != set(intent['newIdentity']) or
            any(record['identity'][key] != intent['newIdentity'][key]
                for key in set(record['identity']) - RECORD_CHANGES) or
            intent['newIdentity']['routeSha256'] != new['routeSha256'] or
            intent['newIdentity']['attestationSha256'] != new['attestationSha256'] or
            new_route['pluginRoot'] != intent['newProof'].get('pluginRoot') or
            new_route['bundleManifestSha256'] !=
            intent['newProof'].get('bundleManifestSha256') or
            new_route['ownershipManifestSha256'] !=
            intent['newProof'].get('ownershipManifestSha256')):
        _fail('bound_upgrade_intent_changed')


def _prepare(project, state, journal_path, ledger, source_root, plugin_root,
             commit_id, accepted_provider_sha256, model_path, dimension):
    record = build.read(state)
    if (record is None or record['phase'] != 'bound' or
            record['identity']['projectRoot'] != str(project) or
            record['identity']['stateRoot'] != str(state) or
            record['identity']['journalPath'] != str(journal_path)):
        _fail('bound_upgrade_not_bound')
    identity = record['identity']
    auto = project_dir.folder(project)
    row = ledger.load(state / ledger.name)
    if (row['phase'] != 'prepared' or row['journalPath'] != str(journal_path) or
            row['routeSha256'] != identity['routeSha256'] or
            row['attestationSha256'] != identity['attestationSha256']):
        _fail('bound_upgrade_ledger_changed')
    marker = _read(auto / ledger.marker_name)
    route_file = Path(identity['routeFile'])
    old_route = _read(route_file)
    old_att = _read(state / PRODUCTION_ATTESTATION, 1024 * 1024)
    route_path = auto / ledger.route_name
    if (route_path.exists() or route_path.is_symlink() or
            route_file.parent != state or
            marker != active._bytes(ledger.marker(row)) or
            _sha(old_route) != row['routeSha256'] or
            _sha(old_att) != row['attestationSha256'] or
            ledger.classify(project, auto / ledger.marker_name, route_path) != 'legacy'):
        _fail('bound_upgrade_old_metadata_changed')
    journal = inspect_journal(journal_path)
    if (journal['state'] != 'candidate_ready' or journal['pending'] is None or
            journal['selected'] is not None or journal['previous'] is not None or
            journal['newWriteSeen'] or
            journal['pendingRouteSha256'] != row['routeSha256']):
        _fail('bound_upgrade_journal_changed')
    pending = journal['pending']
    try:
        old_proof = inspect_full_plugin_candidate(pending['pluginRoot'])
    except BundleEvidenceError:
        old_proof = None  # The superseded host cache may already be gone.
    if old_proof is not None and any(
            old_proof[key] != pending[key] for key in
            ('sourceCommit', 'bundleManifestSha256', 'ownershipManifestSha256',
             'vectorProfile')):
        _fail('bound_upgrade_old_bundle_changed')
    from laya_product_install import review_local_provider
    review = review_local_provider(source_root=source_root, plugin_root=plugin_root,
                                   state_root=state, commit_id=commit_id,
                                   model_path=model_path, dimension=dimension)
    if review['proposalSha256'] != accepted_provider_sha256:
        _fail('reviewed_provider_proposal_changed')
    from laya_provider_reproof import attestation_record, reattest
    # Same content at other paths keeps the bound vector profile.
    approved = reattest(json.loads(old_att), review['reviewedProvider'])
    if approved is None or approved['vectorProfile'] != pending['vectorProfile']:
        _fail('bound_upgrade_vector_profile_changed')
    proof = adopt_host_loaded_plugin(source_root, plugin_root, commit_id=commit_id,
                                     vector_profile=approved['vectorProfile'])
    decision = json.loads(old_route)
    if (proof['vectorProfile'] != pending['vectorProfile'] or
            decision['pluginRoot'] != pending['pluginRoot'] or
            decision['installId'] != pending['installId'] or
            decision['generation'] != pending['generation']):
        _fail('bound_upgrade_identity_mismatch')
    from laya_index_host_route import written_schema
    new_route = active._bytes({**decision,
                               'schema': written_schema(decision['schema']),
                               'pluginRoot': proof['pluginRoot'],
                               'bundleManifestSha256': proof['bundleManifestSha256'],
                               'ownershipManifestSha256': proof['ownershipManifestSha256']})
    checked_route(new_route, journal_path)
    if new_route == old_route:
        _fail('bound_upgrade_same_bundle')
    att = attestation_record(proof=proof, journal_path=journal_path,
                             reattested=approved, schema=PRODUCTION_ATTESTATION_SCHEMA)
    validate_production_provider_attestation(proof['vectorProfile'], proof, att)
    new_att = journal_canon(att).encode('utf-8')
    new_row = {**row, 'epoch': row['epoch'] + 1,
               'routeSha256': _sha(new_route), 'attestationSha256': _sha(new_att),
               'providerFileIdentitySha256': _sha(journal_canon(
                   _provider_file_identity(att)).encode('utf-8'))}
    new_identity = {**identity, 'routeSha256': new_row['routeSha256'],
                    'attestationSha256': new_row['attestationSha256'],
                    'providerProposalSha256': review['proposalSha256']}
    counts = inactive._store_counts(identity)
    backup = inactive._backup(identity, record, old_route, old_att, counts)
    intent = {'schema': SCHEMA, 'origin': ledger.origin,
              'projectRoot': str(project), 'stateRoot': str(state),
              'oldRecord': record, 'newIdentity': new_identity,
              'oldLedger': row, 'newLedger': new_row,
              'oldRoute': old_route.decode('utf-8'),
              'newRoute': new_route.decode('utf-8'),
              'oldAttestation': old_att.decode('utf-8'),
              'newAttestation': new_att.decode('utf-8'),
              'oldMarker': marker.decode('utf-8'),
              'newMarker': active._bytes(ledger.marker(new_row)).decode('utf-8'),
              'newProof': {key: proof[key] for key in
                           ('pluginRoot', 'sourceCommit', 'bundleManifestSha256',
                            'ownershipManifestSha256', 'vectorProfile')},
              'reviewSha256': review['proposalSha256'],
              'backup': str(backup), 'counts': counts}
    _check_intent(intent)
    build._write_json(state / INTENT, intent, replace=False, max_bytes=LIMIT)
    return intent


def _switch(intent):
    """Idempotent forward switch; every step accepts its finished form."""
    state, project = Path(intent['stateRoot']), Path(intent['projectRoot'])
    ledger = active._Ledger(intent['origin'])
    auto = project_dir.folder(project)
    identity = intent['oldRecord']['identity']
    journal_path = Path(identity['journalPath'])
    old_route, new_route = (intent[key].encode('utf-8') for key in ('oldRoute', 'newRoute'))
    old_att, new_att = (intent[key].encode('utf-8')
                        for key in ('oldAttestation', 'newAttestation'))
    new_proof = inspect_full_plugin_candidate(intent['newProof']['pluginRoot'])
    if any(new_proof[key] != intent['newProof'][key] for key in intent['newProof']):
        _fail('bound_upgrade_new_bundle_changed')
    validate_production_provider_attestation(new_proof['vectorProfile'], new_proof,
                                             json.loads(new_att))
    inactive._atomic_expected(state / PRODUCTION_ATTESTATION, old_att, new_att)
    journal = inspect_journal(journal_path)
    if (journal['state'] != 'candidate_ready' or journal['selected'] is not None or
            journal['newWriteSeen'] or journal['pendingRouteSha256'] not in
            (intent['oldLedger']['routeSha256'], intent['newLedger']['routeSha256'])):
        _fail('bound_upgrade_journal_changed')
    if journal['pendingRouteSha256'] == intent['oldLedger']['routeSha256']:
        cas_replace_inactive_candidate(journal_path, journal['epoch'], old_route,
                                       new_proof, new_route)
    inactive._atomic_expected(Path(identity['routeFile']), old_route, new_route)
    record = build.read(state)
    if record == intent['oldRecord']:
        record = build.rebind_bound_upgrade(record, intent['newIdentity'])
    if (record is None or record['phase'] != 'bound' or
            record['identity'] != intent['newIdentity']):
        _fail('bound_upgrade_record_changed')
    active._replace_ledger(state / ledger.name, intent['oldLedger'],
                           intent['newLedger'], ledger)
    inactive._atomic_expected(auto / ledger.marker_name,
                              intent['oldMarker'].encode('utf-8'),
                              intent['newMarker'].encode('utf-8'))
    approval = preflight_product_candidate(journal_path)
    if (approval['attestationSha256'] != intent['newLedger']['attestationSha256'] or
            approval['pendingRouteSha256'] != intent['newLedger']['routeSha256'] or
            approval['providerFileIdentitySha256'] !=
            intent['newLedger']['providerFileIdentitySha256'] or
            inactive._store_counts(identity) != intent['counts'] or
            ledger.classify(project, auto / ledger.marker_name,
                            auto / ledger.route_name) != 'legacy'):
        _fail('bound_upgrade_recheck_failed')
    tag = _sha(active._bytes(intent))[:20]
    target = state / ('bound-upgrade-' + tag + '-complete.json')
    if target.exists() or target.is_symlink():
        _fail('bound_upgrade_receipt_exists')
    os.rename(state / INTENT, target)
    inactive._fsync(state)
    return {'status': 'bound_upgrade_complete',
            'routeSha256': intent['newLedger']['routeSha256'],
            'vectorCounts': intent['counts'], 'backup': intent['backup']}


def upgrade(*, project_root, state_root, source_root, plugin_root, commit_id,
            accepted_provider_sha256, model_path, dimension):
    """Resume or start one exact bound-build bundle replacement."""
    from laya_product_install import _install_lock, _private_parent
    project = Path(project_root).resolve(strict=True)
    state = _private_parent(state_root)
    if project != Path(project_root):
        _fail('bound_upgrade_paths_invalid')
    journal_path = state / 'install-journal.sqlite'
    ledger = active._origin(state)
    _ledger_path, marker, _route = ledger.paths(project, journal_path)
    with _install_lock(state), _operation_lock(marker), build.owner(state) as owned:
        if not owned:
            _fail('initial_build_busy')
        intent = _load_intent(state, project)
        if intent is not None and intent['origin'] != ledger.origin:
            _fail('bound_upgrade_intent_changed')
        if intent is not None and (
                intent['newProof']['pluginRoot'] != str(plugin_root) or
                intent['newProof']['sourceCommit'] != commit_id or
                intent['reviewSha256'] != accepted_provider_sha256):
            # An interrupted earlier replacement finishes on its own recorded
            # bundle first; the new request then starts from that state.
            _check_intent(intent)
            inactive._verify_backup(intent)
            _switch(intent)
            intent = None
        if intent is None:
            intent = _prepare(project, state, journal_path, ledger, source_root,
                              plugin_root, commit_id, accepted_provider_sha256,
                              model_path, dimension)
        _check_intent(intent)
        inactive._verify_backup(intent)
        return _switch(intent)

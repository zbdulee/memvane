"""Recoverable replacement of the sealed code for a selected fresh-wiki index.

This changes bundle ownership, never the index generation, vector profile,
scope, project, store paths, or QMD backup.  The fresh ledger's upgrading
phase fences readers while the provider lane excludes selected writers.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import laya_fresh_wiki_origin as fresh
import laya_initial_build as build
import laya_inactive_upgrade as inactive
from laya_install_journal import (PRODUCTION_ATTESTATION,
                                  _canon as journal_canon,
                                  _provider_file_identity, cas_activate,
                                  cas_recover, cas_restore, cas_stage_update,
                                  inspect_journal)
from laya_product_bundle import (PRODUCTION_ATTESTATION_SCHEMA,
                                 adopt_host_loaded_plugin,
                                 inspect_full_plugin_candidate,
                                 validate_production_provider_attestation)
from laya_product_install import (_install_lock, _private_parent,
                                  _read_private, review_local_provider)

INTENT = 'active-upgrade-intent.json'
LIMIT = 3 * 1024 * 1024
REPLACEABLE = frozenset(('pluginRoot', 'bundleManifestSha256',
                         'ownershipManifestSha256'))


def _fail(reason):
    inactive._fail(reason)


def _sha(value):
    return hashlib.sha256(value).hexdigest()


def _bytes(value):
    return inactive._canonical(value)


def _read(path, limit=LIMIT):
    return _read_private(Path(path), limit)


def _locked_provider():
    from context_learning.resource_coordination import Coordinator
    return Coordinator().quiesce_provider()


def _intent_path(state):
    return state / INTENT


def _load_intent(state, project):
    path = _intent_path(state)
    value = build._read_json(path, max_bytes=LIMIT)
    if value is None:
        return None
    if (value.get('schema') != 'laya-active-upgrade-intent-v1' or
            value.get('projectRoot') != str(project) or
            value.get('stateRoot') != str(state) or
            not isinstance(value.get('backup'), str) or
            not isinstance(value.get('oldRecord'), dict) or
            not isinstance(value.get('oldLedger'), dict) or
            not isinstance(value.get('newLedger'), dict)):
        _fail('active_upgrade_intent_invalid')
    for name in ('oldRoute', 'newRoute', 'oldAttestation',
                 'newAttestation', 'oldMarker', 'newMarker'):
        if type(value.get(name)) is not str:
            _fail('active_upgrade_intent_invalid')
    if (value['oldRecord'].get('identity', {}).get('projectRoot') != str(project) or
            value['oldRecord']['identity'].get('stateRoot') != str(state) or
            value['oldLedger'].get('phase') != 'active' or
            value['newLedger'].get('phase') != 'upgrading' or
            value['oldLedger'].get('projectRoot') != str(project) or
            value['newLedger'].get('projectRoot') != str(project)):
        _fail('active_upgrade_intent_invalid')
    return value


def _old_state(project, state, journal):
    auto = project / '.auto-context'
    row = fresh._load(state / fresh.LEDGER)
    if row['phase'] != 'active' or row['journalPath'] != str(journal):
        _fail('active_upgrade_not_selected')
    marker = _read(auto / fresh.MARKER, 8192)
    route = _read(auto / fresh.ROUTE, 8192)
    reviewed = _read(state / 'reviewed-route.json', 8192)
    attestation = _read(state / PRODUCTION_ATTESTATION, 1024 * 1024)
    if (marker != _bytes(fresh._marker(row)) or route != reviewed or
            _sha(route) != row['routeSha256'] or
            _sha(attestation) != row['attestationSha256'] or
            fresh.classify_marker(project, auto / fresh.MARKER,
                                  auto / fresh.ROUTE) != 'candidate'):
        _fail('active_upgrade_old_metadata_changed')
    selected = inspect_journal(journal)
    if (selected['state'] not in ('active', 'restored_old') or
            selected['selectedRouteSha256'] != row['routeSha256'] or
            selected['providerAttestationSha256'] != row['attestationSha256'] or
            selected['providerFileIdentitySha256'] != row['providerFileIdentitySha256'] or
            selected['selected']['vectorProfile']['profileId'] != row['vectorProfileId']):
        _fail('active_upgrade_journal_changed')
    old_proof = inspect_full_plugin_candidate(selected['selected']['pluginRoot'])
    if any(old_proof[key] != selected['selected'][key] for key in
           ('sourceCommit', 'bundleManifestSha256', 'ownershipManifestSha256',
            'vectorProfile')):
        _fail('active_upgrade_old_bundle_changed')
    return row, selected, route, attestation, marker


def _prepare(project, state, journal, source_root, plugin_root, commit_id,
             accepted_provider_sha256, model_path, dimension):
    row, selected, old_route, old_att, old_marker = _old_state(project, state,
                                                               journal)
    record = build.read(state)
    if (record is None or record['identity']['projectRoot'] != str(project) or
            record['identity']['stateRoot'] != str(state) or
            record['identity']['vectorProfileId'] != row['vectorProfileId']):
        _fail('active_upgrade_build_binding_changed')
    review = review_local_provider(source_root=source_root, plugin_root=plugin_root,
                                   state_root=state, commit_id=commit_id,
                                   model_path=model_path, dimension=dimension)
    if review['proposalSha256'] != accepted_provider_sha256:
        _fail('reviewed_provider_proposal_changed')
    approved = review['reviewedProvider']
    proof = adopt_host_loaded_plugin(source_root, plugin_root,
                                     commit_id=commit_id,
                                     vector_profile=approved['vectorProfile'])
    old_decision = json.loads(old_route)
    if (proof['vectorProfile'] != selected['selected']['vectorProfile'] or
            proof['vectorProfile']['profileId'] != row['vectorProfileId'] or
            old_decision['pluginRoot'] != selected['selected']['pluginRoot'] or
            old_decision['installId'] != selected['selected']['installId'] or
            old_decision['generation'] != selected['selected']['generation']):
        _fail('active_upgrade_identity_mismatch')
    changed = {**old_decision,
               'pluginRoot': proof['pluginRoot'],
               'bundleManifestSha256': proof['bundleManifestSha256'],
               'ownershipManifestSha256': proof['ownershipManifestSha256']}
    new_route = _bytes(changed)
    from laya_install_journal import _route as checked_route
    checked_route(new_route, journal)
    if (set(old_decision) != set(changed) or
            not {key for key in changed if old_decision[key] != changed[key]}
            <= REPLACEABLE):
        _fail('active_upgrade_route_scope_changed')
    if new_route == old_route:
        _fail('active_upgrade_same_bundle')
    att = {'schema': PRODUCTION_ATTESTATION_SCHEMA,
           'sourceCommit': proof['sourceCommit'],
           'bundleManifestSha256': proof['bundleManifestSha256'],
           'ownershipManifestSha256': proof['ownershipManifestSha256'],
           'journalPath': str(journal),
           'vectorProfile': proof['vectorProfile'],
           'providerManifest': approved['providerManifest'],
           'dependencyClosure': approved['dependencyClosure']}
    validate_production_provider_attestation(proof['vectorProfile'], proof, att)
    new_att = journal_canon(att).encode('utf-8')
    provider_digest = _sha(journal_canon(_provider_file_identity(att)).encode('utf-8'))
    new_row = {**row, 'epoch': row['epoch'] + 2, 'phase': 'upgrading',
               'routeSha256': _sha(new_route),
               'attestationSha256': _sha(new_att),
               'providerFileIdentitySha256': provider_digest}
    # No source, vector, generation, collection or ownership path is rebound.
    for key in fresh.FIELDS - {'epoch', 'phase', 'routeSha256',
                               'attestationSha256', 'providerFileIdentitySha256'}:
        if row[key] != new_row[key]:
            _fail('active_upgrade_store_identity_changed')
    counts = inactive._store_counts(record['identity'])
    backup = inactive._backup(record['identity'], record, old_route, old_att,
                              counts)
    intent = {'schema': 'laya-active-upgrade-intent-v1',
              'projectRoot': str(project), 'stateRoot': str(state),
              'oldRecord': record, 'oldLedger': row, 'newLedger': new_row,
              'oldRoute': old_route.decode('utf-8'),
              'newRoute': new_route.decode('utf-8'),
              'oldAttestation': old_att.decode('utf-8'),
              'newAttestation': new_att.decode('utf-8'),
              'oldMarker': old_marker.decode('utf-8'),
              'newMarker': _bytes(fresh._marker(new_row)).decode('utf-8'),
              'newProof': {key: proof[key] for key in
                           ('pluginRoot', 'sourceCommit', 'bundleManifestSha256',
                            'ownershipManifestSha256', 'vectorProfile')},
              'reviewSha256': review['proposalSha256'],
              'backup': str(backup), 'counts': counts}
    build._write_json(_intent_path(state), intent, replace=False,
                      max_bytes=LIMIT)
    return intent


def _backup_proof(intent, *, verify_new=True):
    inactive._verify_backup(intent)
    old, new = intent['oldLedger'], intent['newLedger']
    old_route = json.loads(intent['oldRoute'])
    new_route = json.loads(intent['newRoute'])
    new_att = json.loads(intent['newAttestation'])
    if (intent['oldRecord']['identity']['journalPath'] !=
            str(Path(intent['stateRoot']) / 'install-journal.sqlite') or
            set(old) != fresh.FIELDS or set(new) != fresh.FIELDS or
            old['phase'] != 'active' or new['phase'] != 'upgrading' or
            new['epoch'] != old['epoch'] + 2 or
            any(old[key] != new[key] for key in fresh.FIELDS -
                {'epoch', 'phase', 'routeSha256', 'attestationSha256',
                 'providerFileIdentitySha256'}) or
            set(old_route) != set(new_route) or
            any(old_route[key] != new_route[key] for key in
                set(old_route) - REPLACEABLE) or
            old['routeSha256'] !=
            _sha(intent['oldRoute'].encode('utf-8')) or
            new['routeSha256'] !=
            _sha(intent['newRoute'].encode('utf-8')) or
            old['attestationSha256'] !=
            _sha(intent['oldAttestation'].encode('utf-8')) or
            new['attestationSha256'] !=
            _sha(intent['newAttestation'].encode('utf-8')) or
            intent['oldMarker'] != _bytes(fresh._marker(old)).decode('utf-8') or
            intent['newMarker'] != _bytes(fresh._marker(new)).decode('utf-8') or
            new_route['pluginRoot'] != intent['newProof']['pluginRoot'] or
            new_route['bundleManifestSha256'] !=
            intent['newProof']['bundleManifestSha256'] or
            new_route['ownershipManifestSha256'] !=
            intent['newProof']['ownershipManifestSha256'] or
            new_route['vectorProfile'] != intent['newProof']['vectorProfile']):
        _fail('active_upgrade_intent_changed')
    if verify_new:
        if new['providerFileIdentitySha256'] != \
                _sha(journal_canon(_provider_file_identity(new_att)).encode('utf-8')):
            _fail('active_upgrade_new_provider_changed')
        proof = inspect_full_plugin_candidate(intent['newProof']['pluginRoot'])
        if any(proof[key] != intent['newProof'][key] for key in intent['newProof']):
            _fail('active_upgrade_new_bundle_changed')
        validate_production_provider_attestation(proof['vectorProfile'], proof,
                                                 new_att)


def _fence(intent):
    state = Path(intent['stateRoot'])
    old = intent['oldLedger']
    row = fresh._load(state / fresh.LEDGER)
    if row == old:
        row = fresh._change(state / fresh.LEDGER, old['epoch'], 'active',
                            'upgrading')
    if row not in ({**old, 'epoch': old['epoch'] + 1, 'phase': 'upgrading'},
                   intent['newLedger']):
        _fail('active_upgrade_fence_changed')
    from laya_embed_resident import wait_for_release
    if not wait_for_release(state / 'runtime' / 'laya-embed.sock', timeout=15):
        _fail('active_upgrade_resident_busy')


def _replace_ledger(path, before, after):
    """Use the ledger's own canonical writer and lock during a blocked phase."""
    with fresh._ledger_lock(path):
        current = fresh._load(path)
        if current == after:
            return
        if current != before:
            _fail('active_upgrade_ledger_changed')
        fresh._write(path, after, replace=True)


def _switch(intent):
    state, project = Path(intent['stateRoot']), Path(intent['projectRoot'])
    journal = state / 'install-journal.sqlite'
    auto = project / '.auto-context'
    old_route, new_route = (intent[key].encode('utf-8') for key in
                            ('oldRoute', 'newRoute'))
    old_att, new_att = (intent[key].encode('utf-8') for key in
                        ('oldAttestation', 'newAttestation'))
    already = fresh._load(state / fresh.LEDGER)
    if (already['phase'] == 'active' and
            already['routeSha256'] == _sha(new_route) and
            fresh.classify_marker(project, auto / fresh.MARKER,
                                  auto / fresh.ROUTE) == 'candidate'):
        current = inspect_journal(journal)
        if (current['state'] == 'active' and
                current['selectedRouteSha256'] == _sha(new_route)):
            counts = inactive._store_counts(intent['oldRecord']['identity'])
            return {'status': 'active_upgrade_complete',
                    'routeSha256': _sha(new_route),
                    'vectorCounts': counts,
                    'backup': intent['backup'],
                    'journalEpoch': current['epoch']}
    _fence(intent)
    current = inspect_journal(journal)
    old_sha, new_sha = (_sha(value) for value in (old_route, new_route))
    if current['state'] in ('active', 'restored_old') and \
            current['selectedRouteSha256'] == old_sha:
        new_proof = inspect_full_plugin_candidate(intent['newProof']['pluginRoot'])
        if any(new_proof[key] != intent['newProof'][key]
               for key in intent['newProof']):
            _fail('active_upgrade_new_bundle_changed')
        current = cas_stage_update(journal, current['epoch'],
                                   new_proof, new_route)
    if current['state'] == 'pending' and current['pendingRouteSha256'] != new_sha:
        _fail('active_upgrade_pending_changed')
    if current['state'] not in ('pending', 'active'):
        _fail('active_upgrade_journal_changed')
    if (current['state'] == 'active' and
            current['selectedRouteSha256'] != new_sha):
        _fail('active_upgrade_journal_changed')
    inactive._atomic_expected(state / PRODUCTION_ATTESTATION, old_att, new_att)
    inactive._atomic_expected(state / 'reviewed-route.json', old_route, new_route)
    inactive._atomic_expected(auto / fresh.ROUTE, old_route, new_route)
    fenced = {**intent['oldLedger'],
              'epoch': intent['oldLedger']['epoch'] + 1,
              'phase': 'upgrading'}
    _replace_ledger(state / fresh.LEDGER, fenced, intent['newLedger'])
    inactive._atomic_expected(auto / fresh.MARKER,
                              intent['oldMarker'].encode('utf-8'),
                              intent['newMarker'].encode('utf-8'))
    if current['state'] == 'pending':
        current = cas_activate(journal, current['epoch'], new_route)
    if (current['state'] != 'active' or
            current['selectedRouteSha256'] != new_sha or
            current['providerAttestationSha256'] !=
            intent['newLedger']['attestationSha256'] or
            current['providerFileIdentitySha256'] !=
            intent['newLedger']['providerFileIdentitySha256'] or
            inactive._store_counts(intent['oldRecord']['identity']) !=
            intent['counts']):
        _fail('active_upgrade_recheck_failed')
    row = fresh._load(state / fresh.LEDGER)
    if row['phase'] == 'upgrading' and row == intent['newLedger']:
        row = fresh._change(state / fresh.LEDGER, row['epoch'], 'upgrading',
                            'active')
    if (row['phase'] != 'active' or row['routeSha256'] != new_sha or
            fresh.classify_marker(project, auto / fresh.MARKER,
                                  auto / fresh.ROUTE) != 'candidate'):
        _fail('active_upgrade_selection_unconfirmed')
    return {'status': 'active_upgrade_complete',
            'routeSha256': new_sha, 'vectorCounts': intent['counts'],
            'backup': intent['backup'], 'journalEpoch': current['epoch']}


def _restore(intent):
    """Return to the previous sealed Laya bundle before any new selected write."""
    state, project = Path(intent['stateRoot']), Path(intent['projectRoot'])
    journal = state / 'install-journal.sqlite'
    auto = project / '.auto-context'
    old_route, new_route = (intent[key].encode('utf-8') for key in
                            ('oldRoute', 'newRoute'))
    old_att, new_att = (intent[key].encode('utf-8') for key in
                        ('oldAttestation', 'newAttestation'))
    old_sha, new_sha = _sha(old_route), _sha(new_route)
    current = inspect_journal(journal)
    row = fresh._load(state / fresh.LEDGER)
    if (row['phase'] == 'active' and row['routeSha256'] == old_sha and
            current['state'] == 'restored_old' and
            current['selectedRouteSha256'] == old_sha and
            fresh.classify_marker(project, auto / fresh.MARKER,
                                  auto / fresh.ROUTE) == 'candidate'):
        return {'status': 'active_upgrade_restored_laya',
                'routeSha256': old_sha, 'vectorCounts': intent['counts'],
                'backup': intent['backup']}
    if current['newWriteSeen'] or current['previousRouteSha256'] != old_sha:
        if (row['phase'] == 'upgrading' and row['routeSha256'] == old_sha and
                current['state'] in ('active', 'restored_old') and
                current['selectedRouteSha256'] == old_sha and
                current['providerAttestationSha256'] ==
                intent['oldLedger']['attestationSha256'] and
                _read(auto / fresh.ROUTE, 8192) == old_route and
                _read(state / 'reviewed-route.json', 8192) == old_route and
                _read(auto / fresh.MARKER, 8192) ==
                intent['oldMarker'].encode('utf-8') and
                _read(state / PRODUCTION_ATTESTATION, 1024 * 1024) == old_att):
            row = fresh._change(state / fresh.LEDGER, row['epoch'],
                                'upgrading', 'active')
            return {'status': 'active_upgrade_restored_laya',
                    'routeSha256': old_sha, 'vectorCounts': intent['counts'],
                    'backup': intent['backup']}
        _fail('active_upgrade_rollback_forward_only')
    old_proof = inspect_full_plugin_candidate(
        json.loads(intent['oldRoute'])['pluginRoot'])
    if (current['previous'] is None or
            any(current['previous'][key] != old_proof[key] for key in
                ('sourceCommit', 'bundleManifestSha256',
                 'ownershipManifestSha256', 'vectorProfile'))):
        _fail('active_upgrade_previous_bundle_changed')
    if row['phase'] == 'active' and row['routeSha256'] == new_sha:
        row = fresh._change(state / fresh.LEDGER, row['epoch'], 'active',
                            'upgrading')
    if row['phase'] != 'upgrading' or row['routeSha256'] not in (old_sha, new_sha):
        _fail('active_upgrade_restore_phase_changed')
    from laya_embed_resident import wait_for_release
    if not wait_for_release(state / 'runtime' / 'laya-embed.sock', timeout=15):
        _fail('active_upgrade_resident_busy')
    inactive._atomic_expected(auto / fresh.ROUTE, new_route, old_route)
    inactive._atomic_expected(state / PRODUCTION_ATTESTATION, new_att, old_att)
    inactive._atomic_expected(state / 'reviewed-route.json', new_route, old_route)
    current = inspect_journal(journal)
    if current['state'] == 'pending' and current['pendingRouteSha256'] == new_sha:
        current = cas_recover(journal, current['epoch'], old_route)
    elif current['state'] == 'active' and current['selectedRouteSha256'] == new_sha:
        current = cas_restore(journal, current['epoch'], old_route)
    if (current['state'] != 'restored_old' or
            current['selectedRouteSha256'] != old_sha or
            current['providerAttestationSha256'] !=
            intent['oldLedger']['attestationSha256']):
        _fail('active_upgrade_restore_unconfirmed')
    inactive._atomic_expected(auto / fresh.MARKER,
                              intent['newMarker'].encode('utf-8'),
                              intent['oldMarker'].encode('utf-8'))
    with fresh._ledger_lock(state / fresh.LEDGER):
        row = fresh._load(state / fresh.LEDGER)
        if row['phase'] != 'upgrading' or row['routeSha256'] not in (old_sha, new_sha):
            _fail('active_upgrade_restore_phase_changed')
        old_row = {**intent['oldLedger'], 'epoch': row['epoch'] + 1,
                   'phase': 'upgrading'}
        fresh._write(state / fresh.LEDGER, old_row, replace=True)
    if inactive._store_counts(intent['oldRecord']['identity']) != intent['counts']:
        _fail('active_upgrade_restore_store_changed')
    row = fresh._change(state / fresh.LEDGER, old_row['epoch'], 'upgrading',
                        'active')
    if (row['routeSha256'] != old_sha or
            fresh.classify_marker(project, auto / fresh.MARKER,
                                  auto / fresh.ROUTE) != 'candidate'):
        _fail('active_upgrade_restore_unconfirmed')
    return {'status': 'active_upgrade_restored_laya',
            'routeSha256': old_sha, 'vectorCounts': intent['counts'],
            'backup': intent['backup']}


def _archive_finished_intent(state, project, intent):
    """Retain the old receipt when a later exact bundle is requested."""
    row = fresh._load(state / fresh.LEDGER)
    journal = inspect_journal(state / 'install-journal.sqlite')
    current = row['routeSha256']
    if (row['phase'] != 'active' or
            current not in (intent['oldLedger']['routeSha256'],
                            intent['newLedger']['routeSha256']) or
            journal['state'] not in ('active', 'restored_old') or
            journal['selectedRouteSha256'] != current or
            fresh.classify_marker(project, project / '.auto-context' / fresh.MARKER,
                                  project / '.auto-context' / fresh.ROUTE) != 'candidate'):
        _fail('active_upgrade_prior_unsettled')
    # The original old route can be active only after an explicit Laya restore.
    if (current == intent['oldLedger']['routeSha256'] and
            journal['state'] != 'restored_old'):
        _fail('active_upgrade_prior_unsettled')
    tag = _sha(_bytes(intent))[:20]
    target = state / ('active-upgrade-' + tag + '-settled.json')
    if target.exists() or target.is_symlink():
        _fail('active_upgrade_receipt_exists')
    os.rename(_intent_path(state), target)
    inactive._fsync(state)


def upgrade(*, project_root, state_root, source_root, plugin_root, commit_id,
            accepted_provider_sha256, model_path, dimension, restore=False):
    """Resume one exact sealed upgrade; a failed step leaves a blocked route."""
    project = Path(project_root).resolve(strict=True)
    state = _private_parent(state_root)
    if project != Path(project_root) or state / 'install-journal.sqlite' != \
            Path(state_root) / 'install-journal.sqlite':
        _fail('active_upgrade_paths_invalid')
    ledger, marker, _route = fresh._paths(project, state / 'install-journal.sqlite')
    with _install_lock(state), fresh._operation_lock(marker), _locked_provider():
        intent = _load_intent(state, project)
        if restore:
            if intent is None:
                _fail('active_upgrade_intent_missing')
            _backup_proof(intent, verify_new=False)
            return _restore(intent)
        if intent is not None:
            current_row = fresh._load(state / fresh.LEDGER)
            current_journal = inspect_journal(state / 'install-journal.sqlite')
            settled_restored = (
                current_row['phase'] == 'active' and
                current_row['routeSha256'] == intent['oldLedger']['routeSha256'] and
                current_journal['state'] == 'restored_old' and
                current_journal['selectedRouteSha256'] ==
                intent['oldLedger']['routeSha256'])
        else:
            settled_restored = False
        if intent is not None and (settled_restored or
                                   intent['newProof']['pluginRoot'] != str(plugin_root)
                                   or intent['newProof']['sourceCommit'] != commit_id):
            _backup_proof(intent, verify_new=not settled_restored)
            _archive_finished_intent(state, project, intent)
            intent = None
        if intent is None:
            intent = _prepare(project, state, state / 'install-journal.sqlite',
                              source_root, plugin_root, commit_id,
                              accepted_provider_sha256, model_path, dimension)
        elif (intent['newProof']['pluginRoot'] != str(plugin_root) or
              intent['newProof']['sourceCommit'] != commit_id or
              intent['reviewSha256'] != accepted_provider_sha256):
            _fail('active_upgrade_intent_changed')
        _backup_proof(intent)
        return _switch(intent)

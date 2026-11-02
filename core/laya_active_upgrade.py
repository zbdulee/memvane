"""Recoverable replacement of the sealed code for a selected memvane-db index.

The selection ledger is ``fresh-wiki-cutover.json``. This changes bundle
ownership, never the index generation, vector profile, scope, project or
store paths. The ledger's upgrading phase fences readers while the provider
lane excludes selected writers. A state root that still holds a QMD-origin
ledger refuses as ``qmd_origin_detach_required``.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3

import laya_fresh_wiki_origin as fresh
import project_dir
import laya_initial_build as build
import laya_inactive_upgrade as inactive
from laya_ledger_common import (QMD_ORIGIN_DETACH_REQUIRED, _operation_lock,
                                refuse_qmd_origin)
from laya_install_journal import (PRODUCTION_ATTESTATION,
                                  _canon as journal_canon,
                                  _provider_file_identity, cas_activate,
                                  cas_recover, cas_restore, cas_stage_update,
                                  cas_retarget_pending,
                                  inspect_journal)
from laya_product_bundle import (PRODUCTION_ATTESTATION_SCHEMA,
                                 OPTIONAL_ATTESTATION_FIELDS,
                                 adopt_host_loaded_plugin,
                                 inspect_full_plugin_candidate,
                                 validate_production_provider_attestation)
from laya_product_install import (_install_lock, _private_parent,
                                  _read_private, review_local_provider)
from laya_provider_reproof import attestation_record, reattest

INTENT = 'active-upgrade-intent.json'
LIMIT = 3 * 1024 * 1024
REPLACEABLE = frozenset(('schema', 'pluginRoot', 'bundleManifestSha256',
                         'ownershipManifestSha256'))
# The only ledger fields a bundle replacement may change.
LEDGER_CHANGES = frozenset(('epoch', 'phase', 'routeSha256', 'attestationSha256',
                            'providerFileIdentitySha256'))
V1 = 'laya-active-upgrade-intent-v1'
V2 = 'laya-active-upgrade-intent-v2'
JOURNAL_KEYS = ('epoch', 'state', 'selected', 'previous', 'pending',
                'selectedRouteSha256', 'previousRouteSha256', 'pendingRouteSha256',
                'newWriteSeen', 'providerAttestationSha256',
                'providerFileIdentitySha256', 'previousProviderAttestationSha256')
META_LIMITS = (1024 * 1024, 8192, 8192, 65536, 8192)


class _Ledger:
    """The fresh-wiki selection ledger's loader, CAS writer, lock and marker.

    Attributes are looked up on the module at call time so the ledger module
    keeps a single implementation of each operation. An intent that names
    the QMD origin refuses.
    """

    origin = 'fresh'

    def __init__(self, origin='fresh'):
        if origin == 'qmd':
            _fail(QMD_ORIGIN_DETACH_REQUIRED)
        if origin != 'fresh':
            _fail('active_upgrade_origin_invalid')
        self.module = fresh
        self.name = fresh.LEDGER
        self.marker_name = fresh.MARKER
        self.route_name = fresh.ROUTE

    def load(self, path):
        return fresh._load(path)

    def change(self, path, epoch, phase, next_phase):
        return self.module._change(path, epoch, phase, next_phase)

    def lock(self, path):
        return self.module._ledger_lock(path)

    def write(self, path, row):
        self.module._write(path, row, replace=True)

    def marker(self, row):
        return self.module._marker(row)

    def classify(self, project, marker, route):
        return self.module.classify_marker(project, marker, route)

    def paths(self, project, journal):
        return self.module._paths(project, journal)

    def fields(self, _row):
        return fresh.FIELDS


def _origin(state):
    """The selection ledger in this state root."""
    refuse_qmd_origin(state)
    if not (state / fresh.LEDGER).exists() and not (state / fresh.LEDGER).is_symlink():
        _fail('active_upgrade_not_selected')
    return _Ledger()


def _intent_ledger(intent):
    return _Ledger(intent.get('origin', 'fresh'))


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
    if isinstance(value, dict) and 'origin' in value:
        _Ledger(value['origin'])  # an intent of the QMD origin refuses here
    if (value.get('schema') not in (V1, V2) or
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
    if value['schema'] == V2:
        try:
            _validate_recovery(value)
        except (KeyError, TypeError, ValueError, UnicodeError):
            _fail('active_upgrade_recovery_invalid')
    return value


def _old_state(project, state, journal, ledger=None, *, prior_intent=None):
    ledger = ledger or _Ledger('fresh')
    auto = project_dir.folder(project)
    row = ledger.load(state / ledger.name)
    if prior_intent is not None:
        old = prior_intent['oldLedger']
        if row != {**old, 'epoch': old['epoch'] + 1, 'phase': 'upgrading'}:
            _fail('active_upgrade_fence_changed')
        row = old
    if row['phase'] != 'active' or row['journalPath'] != str(journal):
        _fail('active_upgrade_not_selected')
    marker = _read(auto / ledger.marker_name, 8192)
    route = _read(auto / ledger.route_name, 8192)
    reviewed = _read(state / 'reviewed-route.json', 8192)
    attestation = _read(state / PRODUCTION_ATTESTATION, 1024 * 1024)
    if (marker != _bytes(ledger.marker(row)) or route != reviewed or
            _sha(route) != row['routeSha256'] or
            _sha(attestation) != row['attestationSha256'] or
            (prior_intent is None and
             ledger.classify(project, auto / ledger.marker_name,
                             auto / ledger.route_name) != 'candidate')):
        _fail('active_upgrade_old_metadata_changed')
    if prior_intent is not None and (
            route != prior_intent['oldRoute'].encode('utf-8') or
            attestation != prior_intent['oldAttestation'].encode('utf-8') or
            marker != prior_intent['oldMarker'].encode('utf-8')):
        _fail('active_upgrade_old_metadata_changed')
    selected = inspect_journal(journal)
    if (selected['state'] not in ('active', 'restored_old') or
            selected.get('pending') is not None or
            selected['selectedRouteSha256'] != row['routeSha256'] or
            selected['providerAttestationSha256'] != row['attestationSha256'] or
            selected['providerFileIdentitySha256'] != row['providerFileIdentitySha256'] or
            selected['selected']['vectorProfile']['profileId'] != row['vectorProfileId']):
        _fail('active_upgrade_journal_changed')
    # The old bundle's files are not proved here: a host may remove or
    # re-extract the superseded cache in place (Codex reuses one version
    # directory across commits) before the upgrade runs. The journal keeps the
    # sealed identity; only a later restore needs the old files, and that
    # restore then refuses on its own proof.
    return row, selected, route, attestation, marker


def _prepare(project, state, journal, source_root, plugin_root, commit_id,
             accepted_provider_sha256, model_path, dimension, ledger=None,
             *, prior_intent=None):
    ledger = ledger or _Ledger('fresh')
    row, selected, old_route, old_att, old_marker = _old_state(project, state,
                                                               journal, ledger,
                                                               prior_intent=prior_intent)
    record = build.read(state)
    if (record is None or record['identity']['projectRoot'] != str(project) or
            record['identity']['stateRoot'] != str(state) or
            record['identity']['vectorProfileId'] != row['vectorProfileId']):
        _fail('active_upgrade_build_binding_changed')
    if prior_intent is not None and record != prior_intent['oldRecord']:
        _fail('active_upgrade_build_binding_changed')
    review = review_local_provider(source_root=source_root, plugin_root=plugin_root,
                                   state_root=state, commit_id=commit_id,
                                   model_path=model_path, dimension=dimension)
    if review['proposalSha256'] != accepted_provider_sha256:
        _fail('reviewed_provider_proposal_changed')
    # Same content at other paths keeps the selected vector profile.
    approved = reattest(json.loads(old_att), review['reviewedProvider'])
    if approved is None:
        _fail('provider_changed_rebuild_required')
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
    from laya_index_host_route import written_schema
    changed = {**old_decision,
               'schema': written_schema(old_decision['schema']),
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
    att = attestation_record(proof=proof, journal_path=journal, reattested=approved,
                             schema=PRODUCTION_ATTESTATION_SCHEMA)
    validate_production_provider_attestation(proof['vectorProfile'], proof, att)
    new_att = journal_canon(att).encode('utf-8')
    provider_digest = _sha(journal_canon(_provider_file_identity(att)).encode('utf-8'))
    new_row = {**row, 'epoch': row['epoch'] + 2, 'phase': 'upgrading',
               'routeSha256': _sha(new_route),
               'attestationSha256': _sha(new_att),
               'providerFileIdentitySha256': provider_digest}
    # No source, vector, generation, collection or ownership path is rebound.
    for key in ledger.fields(row) - LEDGER_CHANGES:
        if row[key] != new_row[key]:
            _fail('active_upgrade_store_identity_changed')
    counts = inactive._store_counts(record['identity'])
    if prior_intent is None:
        backup = inactive._backup(record['identity'], record, old_route, old_att,
                                  counts)
    else:
        if counts != prior_intent['counts']:
            _fail('active_upgrade_restore_store_changed')
        backup = prior_intent['backup']
    intent = {'schema': 'laya-active-upgrade-intent-v1',
              'projectRoot': str(project), 'stateRoot': str(state),
              'oldRecord': record, 'oldLedger': row, 'newLedger': new_row,
              'oldRoute': old_route.decode('utf-8'),
              'newRoute': new_route.decode('utf-8'),
              'oldAttestation': old_att.decode('utf-8'),
              'newAttestation': new_att.decode('utf-8'),
              'oldMarker': old_marker.decode('utf-8'),
              'newMarker': _bytes(ledger.marker(new_row)).decode('utf-8'),
              'newProof': {key: proof[key] for key in
                           ('pluginRoot', 'sourceCommit', 'bundleManifestSha256',
                            'ownershipManifestSha256', 'vectorProfile')},
              'reviewSha256': review['proposalSha256'],
              'backup': str(backup), 'counts': counts,
              'journalBefore': _journal_snapshot(selected)}
    build._write_json(_intent_path(state), intent, replace=prior_intent is not None,
                      max_bytes=LIMIT)
    return intent


def _backup_proof(intent, *, verify_new=True):
    inactive._verify_backup(intent)
    ledger = _intent_ledger(intent)
    old, new = intent['oldLedger'], intent['newLedger']
    old_route = json.loads(intent['oldRoute'])
    new_route = json.loads(intent['newRoute'])
    new_att = json.loads(intent['newAttestation'])
    if (intent['oldRecord']['identity']['journalPath'] !=
            str(Path(intent['stateRoot']) / 'install-journal.sqlite') or
            set(old) != ledger.fields(old) or set(new) != set(old) or
            old['phase'] != 'active' or new['phase'] != 'upgrading' or
            (new['epoch'] != old['epoch'] + 2 if intent['schema'] == V1 else
             type(new['epoch']) is not int or new['epoch'] <= old['epoch']) or
            any(old[key] != new[key] for key in set(old) - LEDGER_CHANGES) or
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
            intent['oldMarker'] != _bytes(ledger.marker(old)).decode('utf-8') or
            intent['newMarker'] != _bytes(ledger.marker(new)).decode('utf-8') or
            new_route['pluginRoot'] != intent['newProof']['pluginRoot'] or
            new_route['bundleManifestSha256'] !=
            intent['newProof']['bundleManifestSha256'] or
            new_route['ownershipManifestSha256'] !=
            intent['newProof']['ownershipManifestSha256'] or
            new_route['vectorProfile'] != intent['newProof']['vectorProfile']):
        _fail('active_upgrade_intent_changed')
    if verify_new:
        proof = inspect_full_plugin_candidate(intent['newProof']['pluginRoot'])
        if any(proof[key] != intent['newProof'][key] for key in intent['newProof']):
            _fail('active_upgrade_new_bundle_changed')
        if new['providerFileIdentitySha256'] != \
                _sha(journal_canon(_provider_file_identity(new_att)).encode('utf-8')):
            _fail('active_upgrade_new_provider_changed')
        validate_production_provider_attestation(proof['vectorProfile'], proof,
                                                 new_att)


def _journal_snapshot(value):
    return {key: value.get(key) for key in JOURNAL_KEYS}


def _target_identity(intent):
    route = json.loads(intent['newRoute'])
    return {**{key: intent['newProof'][key] for key in
               ('pluginRoot', 'sourceCommit', 'bundleManifestSha256',
                'ownershipManifestSha256', 'vectorProfile')},
            'installId': route['installId'], 'generation': route['generation'],
            'bundleKind': 'full_plugin'}


def _metadata_paths(intent):
    state, project = Path(intent['stateRoot']), Path(intent['projectRoot'])
    ledger = _intent_ledger(intent)
    return (state / PRODUCTION_ATTESTATION, state / 'reviewed-route.json',
            project_dir.runtime_file(project, ledger.route_name),
            state / ledger.name, project_dir.runtime_file(project, ledger.marker_name))


def _metadata(intent):
    return [_read(path, limit).decode('utf-8') for path, limit in
            zip(_metadata_paths(intent), META_LIMITS)]


def _target_metadata(intent):
    return [intent['newAttestation'], intent['newRoute'], intent['newRoute'],
            _bytes(intent['newLedger']).decode(), intent['newMarker']]


def _prefix(actual, before, after):
    if not any(actual == after[:i] + before[i:] for i in range(6)):
        _fail('active_upgrade_metadata_prefix_changed')


def _metadata_object(raw):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                _fail('active_upgrade_recovery_invalid')
            value[key] = item
        return value
    def nonfinite(_value):
        _fail('active_upgrade_recovery_invalid')
    return json.loads(raw, object_pairs_hook=unique, parse_constant=nonfinite)


def _validate_recovery(intent):
    required = {'schema', 'projectRoot', 'stateRoot', 'oldRecord', 'oldLedger',
                'newLedger', 'oldRoute', 'newRoute', 'oldAttestation', 'newAttestation',
                'oldMarker', 'newMarker', 'newProof', 'reviewSha256', 'backup',
                'counts', 'recovery'}
    if set(intent) - {'origin', 'journalBefore'} != required:
        _fail('active_upgrade_recovery_invalid')
    descriptor = intent['recovery']
    if (type(descriptor) is not dict or
            set(descriptor) != {'mode', 'before', 'after', 'journalBefore'} or
            descriptor['mode'] not in ('normalize', 'switch')):
        _fail('active_upgrade_recovery_invalid')
    snapshot = descriptor['journalBefore']
    if (type(snapshot) is not dict or set(snapshot) != set(JOURNAL_KEYS) or
            type(snapshot['epoch']) is not int or snapshot['epoch'] < 1 or
            type(snapshot['newWriteSeen']) is not bool or snapshot['newWriteSeen'] or
            snapshot['state'] not in ('pending', 'active') or
            type(snapshot['selected']) is not dict or
            type(snapshot['previous']) is not dict):
        _fail('active_upgrade_recovery_invalid')
    old_route = json.loads(intent['oldRoute'])
    import laya_install_journal as journal_module
    for key in ('selectedRouteSha256', 'previousRouteSha256', 'pendingRouteSha256',
                'providerAttestationSha256', 'providerFileIdentitySha256',
                'previousProviderAttestationSha256'):
        digest = snapshot[key]
        if digest is not None and (type(digest) is not str or not journal_module.SHA.fullmatch(digest)):
            _fail('active_upgrade_recovery_invalid')
    for identity in (snapshot['selected'], snapshot['previous'], snapshot['pending']):
        if identity is None:
            continue
        if (type(identity) is not dict or
                set(identity) != {'pluginRoot', 'sourceCommit', 'bundleManifestSha256',
                                  'ownershipManifestSha256', 'vectorProfile',
                                  'installId', 'generation', 'bundleKind'} or
                identity['bundleKind'] != 'full_plugin' or
                type(identity['pluginRoot']) is not str or
                not Path(identity['pluginRoot']).is_absolute() or
                type(identity['sourceCommit']) is not str or
                not journal_module.COMMIT.fullmatch(identity['sourceCommit']) or
                any(type(identity[key]) is not str or not journal_module.SHA.fullmatch(identity[key])
                    for key in ('bundleManifestSha256', 'ownershipManifestSha256')) or
                any(identity[key] != old_route[key] for key in
                    ('installId', 'generation', 'vectorProfile'))):
            _fail('active_upgrade_recovery_invalid')
    if snapshot['state'] == 'pending':
        if (snapshot['pending'] is None or snapshot['selected'] != snapshot['previous'] or
                snapshot['selectedRouteSha256'] != snapshot['previousRouteSha256'] or
                snapshot['providerAttestationSha256'] !=
                snapshot['previousProviderAttestationSha256']):
            _fail('active_upgrade_recovery_invalid')
    elif snapshot['pending'] is not None or snapshot['pendingRouteSha256'] is not None:
        _fail('active_upgrade_recovery_invalid')
    from laya_install_journal import _route as checked_route
    journal = Path(intent['stateRoot']) / 'install-journal.sqlite'
    for name in ('before', 'after'):
        metadata = descriptor[name]
        if (type(metadata) is not list or len(metadata) != 5 or
                any(type(raw) is not str or len(raw.encode()) > limit
                    for raw, limit in zip(metadata, META_LIMITS))):
            _fail('active_upgrade_recovery_invalid')
        try:
            parsed = [_metadata_object(raw) for raw in metadata]
        except (ValueError, TypeError):
            _fail('active_upgrade_recovery_invalid')
        if any(type(value) is not dict for value in parsed):
            _fail('active_upgrade_recovery_invalid')
        for raw in metadata[1:3]:
            route, _digest = checked_route(raw.encode(), journal)
            if (set(route) != set(old_route) or
                    any(route[key] != old_route[key] for key in set(old_route) - REPLACEABLE)):
                _fail('active_upgrade_recovery_scope_changed')
        row = parsed[3]
        if (set(row) != _intent_ledger(intent).fields(intent['oldLedger']) or
                row.get('phase') != 'upgrading' or type(row.get('epoch')) is not int or
                row['epoch'] <= intent['oldLedger']['epoch'] or
                any(row[key] != intent['oldLedger'][key] for key in
                    set(row) - LEDGER_CHANGES)):
            _fail('active_upgrade_recovery_scope_changed')
        if (parsed[0].get('journalPath') != str(journal) or
                set(parsed[0]) - OPTIONAL_ATTESTATION_FIELDS !=
                set(json.loads(intent['oldAttestation'])) - OPTIONAL_ATTESTATION_FIELDS or
                parsed[0].get('vectorProfile') != old_route['vectorProfile'] or
                set(parsed[4]) != set(json.loads(intent['oldMarker'])) or
                parsed[4].get('schema') != json.loads(intent['oldMarker'])['schema'] or
                parsed[4].get('projectRoot') != intent['projectRoot'] or
                parsed[4].get('journalPath') != str(journal) or
                parsed[4].get('ledgerPath') != str(journal.parent / fresh.LEDGER)):
            _fail('active_upgrade_recovery_scope_changed')
    if descriptor['mode'] == 'normalize':
        expected = list(descriptor['before'])
        if snapshot['state'] == 'pending' and _sha(expected[2].encode()) not in (
                snapshot['selectedRouteSha256'], snapshot['pendingRouteSha256']):
            expected[2] = intent['newRoute']
        if descriptor['after'] != expected:
            _fail('active_upgrade_recovery_invalid')
        if snapshot['state'] == 'pending' and (
                _sha(intent['newRoute'].encode()) != snapshot['pendingRouteSha256'] or
                _target_identity(intent) != snapshot['pending']):
            _fail('active_upgrade_recovery_invalid')
    elif descriptor['after'] != _target_metadata(intent):
        _fail('active_upgrade_recovery_invalid')


def _journal_successor(before, intent, *, activate=False):
    result = {**before, 'epoch': before['epoch'] + 1}
    identity = _target_identity(intent)
    if activate:
        result.update(state='active', selected=identity, pending=None,
                      selectedRouteSha256=intent['newLedger']['routeSha256'],
                      pendingRouteSha256=None,
                      providerAttestationSha256=intent['newLedger']['attestationSha256'],
                      providerFileIdentitySha256=intent['newLedger']['providerFileIdentitySha256'])
    else:
        if before['state'] == 'active':
            result.update(previous=before['selected'],
                          previousRouteSha256=before['selectedRouteSha256'],
                          previousProviderAttestationSha256=before['providerAttestationSha256'])
        result.update(state='pending', pending=identity,
                      pendingRouteSha256=intent['newLedger']['routeSha256'],
                      newWriteSeen=False)
    return result


def _recovery_step(_name):
    """Crash-test seam; only called after a durable operation."""


def _original_journal(intent):
    if 'journalBefore' in intent:
        return intent['journalBefore']
    # Older v1 intents seal their original journal in the verified backup.
    import laya_install_journal as journal
    with sqlite3.connect((Path(intent['backup']) / 'journal.sqlite').as_uri() + '?mode=ro',
                         uri=True) as db:
        row = journal._row(db)
    return {'epoch': row['epoch'], 'state': row['state'], 'selected': row['selected'],
            'previous': row['previous'], 'pending': row['pending'],
            'selectedRouteSha256': row['selected_route_sha'],
            'previousRouteSha256': row['previous_route_sha'],
            'pendingRouteSha256': row['pending_route_sha'],
            'newWriteSeen': bool(row['new_write_seen']),
            'providerAttestationSha256': row['provider_attestation_sha'],
            'providerFileIdentitySha256': _sha(journal_canon(row['provider_file_identity']).encode())
            if row['provider_file_identity'] is not None else None,
            'previousProviderAttestationSha256': row['previous_provider_attestation_sha']}


def _recovery_state(intent, actual, current):
    if intent['schema'] == V1:
        staged = _journal_successor(_original_journal(intent), intent)
        activated = _journal_successor(staged, intent, activate=True)
        before = [intent['oldAttestation'], intent['oldRoute'], intent['oldRoute'],
                  _bytes({**intent['oldLedger'], 'epoch': intent['oldLedger']['epoch'] + 1,
                          'phase': 'upgrading'}).decode(), intent['oldMarker']]
        _prefix(actual, before, _target_metadata(intent))
        if current not in (staged, activated):
            _fail('active_upgrade_recovery_journal_changed')
        if current == activated and actual != _target_metadata(intent):
            _fail('active_upgrade_recovery_journal_changed')
        return
    descriptor = intent['recovery']
    _prefix(actual, descriptor['before'], descriptor['after'])
    before = descriptor['journalBefore']
    if descriptor['mode'] == 'normalize':
        allowed = (before,)
    else:
        staged = _journal_successor(before, intent)
        activated = _journal_successor(staged, intent, activate=True)
        allowed = (before, staged, activated)
        if current == before and actual != descriptor['before']:
            _fail('active_upgrade_recovery_journal_changed')
        if current == activated and actual != descriptor['after']:
            _fail('active_upgrade_recovery_journal_changed')
    if current not in allowed:
        _fail('active_upgrade_recovery_journal_changed')


def _fresh_recovery_target(intent, before, source_root, plugin_root, commit_id,
                           accepted_provider_sha256, model_path, dimension):
    state = Path(intent['stateRoot'])
    review = review_local_provider(source_root=source_root, plugin_root=plugin_root,
                                   state_root=state, commit_id=commit_id,
                                   model_path=model_path, dimension=dimension)
    if review['proposalSha256'] != accepted_provider_sha256:
        _fail('reviewed_provider_proposal_changed')
    approved = reattest(json.loads(intent['oldAttestation']), review['reviewedProvider'])
    if approved is None:
        _fail('provider_changed_rebuild_required')
    proof = adopt_host_loaded_plugin(source_root, plugin_root, commit_id=commit_id,
                                     vector_profile=approved['vectorProfile'])
    old_route = json.loads(intent['oldRoute'])
    if proof['vectorProfile'] != old_route['vectorProfile']:
        _fail('active_upgrade_identity_mismatch')
    from laya_index_host_route import written_schema
    route = {**old_route, 'schema': written_schema(old_route['schema']),
             **{key: proof[key] for key in
                ('pluginRoot', 'bundleManifestSha256', 'ownershipManifestSha256')}}
    att = attestation_record(proof=proof, journal_path=state / 'install-journal.sqlite',
                             reattested=approved, schema=PRODUCTION_ATTESTATION_SCHEMA)
    validate_production_provider_attestation(proof['vectorProfile'], proof, att)
    row = {**intent['oldLedger'], 'phase': 'upgrading',
           'epoch': json.loads(before[3])['epoch'] + 1,
           'routeSha256': _sha(_bytes(route)),
           'attestationSha256': _sha(journal_canon(att).encode()),
           'providerFileIdentitySha256': _sha(journal_canon(_provider_file_identity(att)).encode())}
    return {**intent, 'schema': V2, 'newLedger': row,
            'newRoute': _bytes(route).decode(), 'newAttestation': journal_canon(att),
            'newMarker': _bytes(_intent_ledger(intent).marker(row)).decode(),
            'newProof': {key: proof[key] for key in
                         ('pluginRoot', 'sourceCommit', 'bundleManifestSha256',
                          'ownershipManifestSha256', 'vectorProfile')},
            'reviewSha256': review['proposalSha256']}


def _verify_recovery_project(journal, original):
    """Bind recovery writes to the journal's project directory and scope."""
    import laya_install_journal as installer_journal
    _path, connection = installer_journal._open(journal, write=False)
    try:
        connection.execute('BEGIN')
        bound = installer_journal._row(connection)
        if (installer_journal._project(original) != (
                bound['project_root'], bound['project_dev'], bound['project_ino']) or
                bound['scope_id'] != original['scopeId']):
            raise installer_journal.JournalEvidenceError('recovery_project_identity_changed')
    finally:
        connection.close()


def _recover_upgrade(intent, source_root, plugin_root, commit_id,
                      accepted_provider_sha256, model_path, dimension):
    state, project = Path(intent['stateRoot']), Path(intent['projectRoot'])
    journal = state / 'install-journal.sqlite'
    _backup_proof(intent, verify_new=False)
    if build.read(state) != intent['oldRecord']:
        _fail('active_upgrade_build_binding_changed')
    if inactive._store_counts(intent['oldRecord']['identity']) != intent['counts']:
        _fail('active_upgrade_restore_store_changed')
    from laya_index_host_route import (MAX_SETTINGS_BYTES, _derived_collections,
                                       _read_regular, settings_identity_digest)
    original = json.loads(intent['oldRoute'])
    _verify_recovery_project(journal, original)
    try:
        settings = project_dir.settings_path(project)
        if settings is None:
            _fail('active_upgrade_settings_changed')
        derived = _derived_collections(original, project,
                                        _read_regular(settings, MAX_SETTINGS_BYTES))
    except (OSError, ValueError, TypeError, KeyError, UnicodeError):
        _fail('active_upgrade_settings_changed')
    if settings_identity_digest(project, derived) != original['settingsSha256']:
        _fail('active_upgrade_settings_changed')
    actual = _metadata(intent)
    current = _journal_snapshot(inspect_journal(journal))
    if current['newWriteSeen']:
        _fail('active_upgrade_rollback_forward_only')
    _recovery_state(intent, actual, current)
    _wait_for_sockets(intent)
    same = (intent['newProof']['pluginRoot'] == str(plugin_root) and
            intent['newProof']['sourceCommit'] == commit_id and
            intent['reviewSha256'] == accepted_provider_sha256)
    resume_switch = intent['schema'] == V2 and intent['recovery']['mode'] == 'switch' and same
    if resume_switch:
        try:
            _verify_recovery_target(intent)
        except (OSError, ValueError, RuntimeError):
            resume_switch = False
    if not resume_switch:
        after = list(actual)
        if current['state'] == 'pending' and _sha(actual[2].encode()) not in (
                current['selectedRouteSha256'], current['pendingRouteSha256']):
            after[2] = intent['newRoute']
        normalized = {**intent, 'schema': V2, 'recovery': {
            'mode': 'normalize', 'before': actual, 'after': after,
            'journalBefore': current}}
        _validate_recovery(normalized)
        _verify_recovery_project(journal, original)
        build._write_json(_intent_path(state), normalized, replace=True, max_bytes=LIMIT)
        _recovery_step('normalize-descriptor')
        if actual[2] != after[2]:
            _verify_recovery_project(journal, original)
            inactive._atomic_expected(_metadata_paths(intent)[2],
                                      actual[2].encode(), after[2].encode())
            _recovery_step('normalize-route')
        if _metadata(intent) != after or _journal_snapshot(inspect_journal(journal)) != current:
            _fail('active_upgrade_recovery_journal_changed')
        intent = _fresh_recovery_target(normalized, after, source_root, plugin_root,
                                         commit_id, accepted_provider_sha256,
                                         model_path, dimension)
        intent['recovery'] = {'mode': 'switch', 'before': after,
                              'after': _target_metadata(intent), 'journalBefore': current}
        _validate_recovery(intent)
        _verify_recovery_project(journal, original)
        build._write_json(_intent_path(state), intent, replace=True, max_bytes=LIMIT)
        _recovery_step('switch-descriptor')
    descriptor = intent['recovery']
    current = _journal_snapshot(inspect_journal(journal))
    staged = _journal_successor(descriptor['journalBefore'], intent)
    activated = _journal_successor(staged, intent, activate=True)
    if current == descriptor['journalBefore']:
        _verify_recovery_target(intent)
        if current['state'] == 'pending':
            result = cas_retarget_pending(journal, current['epoch'], descriptor['before'][2].encode(),
                                           current['pending'], current['pendingRouteSha256'],
                                           inspect_full_plugin_candidate(str(plugin_root)),
                                           intent['newRoute'].encode())
        else:
            result = cas_stage_update(journal, current['epoch'],
                                       inspect_full_plugin_candidate(str(plugin_root)),
                                       intent['newRoute'].encode())
        if _journal_snapshot(result) != staged:
            _fail('active_upgrade_recovery_journal_changed')
        _recovery_step('cas')
        current = staged
    if current == staged:
        for index, (path, before, after) in enumerate(zip(
                _metadata_paths(intent), descriptor['before'], descriptor['after'])):
            _verify_recovery_project(journal, original)
            inactive._atomic_expected(path, before.encode(), after.encode())
            _recovery_step('metadata-' + str(index))
        _verify_recovery_target(intent)
        _verify_recovery_project(journal, original)
        result = cas_activate(journal, current['epoch'], intent['newRoute'].encode())
        if _journal_snapshot(result) != activated:
            _fail('active_upgrade_recovery_journal_changed')
        _recovery_step('activate')
    elif current != activated:
        _fail('active_upgrade_recovery_journal_changed')
    if (_metadata(intent) != descriptor['after'] or
            _journal_snapshot(inspect_journal(journal)) != activated or
            inactive._store_counts(intent['oldRecord']['identity']) != intent['counts']):
        _fail('active_upgrade_recheck_failed')
    _verify_recovery_project(journal, original)
    row = _intent_ledger(intent).change(state / fresh.LEDGER,
                                       intent['newLedger']['epoch'], 'upgrading', 'active')
    if (row['routeSha256'] != intent['newLedger']['routeSha256'] or
            _intent_ledger(intent).classify(project, _metadata_paths(intent)[4],
                                             _metadata_paths(intent)[2]) != 'candidate'):
        _fail('active_upgrade_selection_unconfirmed')
    return {'status': 'active_upgrade_complete', 'routeSha256': row['routeSha256'],
            'vectorCounts': intent['counts'], 'backup': intent['backup'],
            'journalEpoch': activated['epoch']}


def _verify_recovery_target(intent):
    proof = inspect_full_plugin_candidate(intent['newProof']['pluginRoot'])
    if any(proof[key] != intent['newProof'][key] for key in intent['newProof']):
        _fail('active_upgrade_new_bundle_changed')
    att = json.loads(intent['newAttestation'])
    validate_production_provider_attestation(proof['vectorProfile'], proof, att)
    if _sha(journal_canon(_provider_file_identity(att)).encode()) != \
            intent['newLedger']['providerFileIdentitySha256']:
        _fail('active_upgrade_new_provider_changed')


def _fence(intent):
    state = Path(intent['stateRoot'])
    ledger = _intent_ledger(intent)
    old = intent['oldLedger']
    row = ledger.load(state / ledger.name)
    if row == old:
        row = ledger.change(state / ledger.name, old['epoch'], 'active',
                            'upgrading')
    if row not in ({**old, 'epoch': old['epoch'] + 1, 'phase': 'upgrading'},
                   intent['newLedger']):
        _fail('active_upgrade_fence_changed')
    _wait_for_sockets(intent)


def _intent_sockets(intent):
    """Every provider socket the old or new attestation seals, in order."""
    sockets = []
    for key in ('oldAttestation', 'newAttestation'):
        try:
            path = json.loads(intent[key])['providerManifest']['serviceSocketPath']
        except (KeyError, TypeError, ValueError):
            _fail('active_upgrade_intent_changed')
        if not isinstance(path, str) or not path.startswith('/'):
            _fail('active_upgrade_intent_changed')
        if path not in sockets:
            sockets.append(path)
    return sockets


def _wait_for_sockets(intent):
    """The resident owner of each sealed socket must have let go of it."""
    from laya_embed_resident import wait_for_release
    for path in _intent_sockets(intent):
        if not wait_for_release(path, timeout=15):
            _fail('active_upgrade_resident_busy')


def _replace_ledger(path, before, after, ledger=None):
    """Use the ledger's own canonical writer and lock during a blocked phase."""
    ledger = ledger or _Ledger('fresh')
    with ledger.lock(path):
        current = ledger.load(path)
        if current == after:
            return
        if current != before:
            _fail('active_upgrade_ledger_changed')
        ledger.write(path, after)


def _switch(intent):
    if intent['schema'] == V2:
        row, current = _completed_recovery(intent)
        return {'status': 'active_upgrade_complete', 'routeSha256': row['routeSha256'],
                'vectorCounts': inactive._store_counts(intent['oldRecord']['identity']),
                'backup': intent['backup'], 'journalEpoch': current['epoch']}
    state, project = Path(intent['stateRoot']), Path(intent['projectRoot'])
    ledger = _intent_ledger(intent)
    journal = state / 'install-journal.sqlite'
    auto = project_dir.folder(project)
    old_route, new_route = (intent[key].encode('utf-8') for key in
                            ('oldRoute', 'newRoute'))
    old_att, new_att = (intent[key].encode('utf-8') for key in
                        ('oldAttestation', 'newAttestation'))
    already = ledger.load(state / ledger.name)
    if (already['phase'] == 'active' and
            already['routeSha256'] == _sha(new_route) and
            ledger.classify(project, auto / ledger.marker_name,
                                  auto / ledger.route_name) == 'candidate'):
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
    inactive._atomic_expected(auto / ledger.route_name, old_route, new_route)
    fenced = {**intent['oldLedger'],
              'epoch': intent['oldLedger']['epoch'] + 1,
              'phase': 'upgrading'}
    _replace_ledger(state / ledger.name, fenced, intent['newLedger'], ledger)
    inactive._atomic_expected(auto / ledger.marker_name,
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
    row = ledger.load(state / ledger.name)
    if row['phase'] == 'upgrading' and row == intent['newLedger']:
        row = ledger.change(state / ledger.name, row['epoch'], 'upgrading',
                            'active')
    if (row['phase'] != 'active' or row['routeSha256'] != new_sha or
            ledger.classify(project, auto / ledger.marker_name,
                                  auto / ledger.route_name) != 'candidate'):
        _fail('active_upgrade_selection_unconfirmed')
    return {'status': 'active_upgrade_complete',
            'routeSha256': new_sha, 'vectorCounts': intent['counts'],
            'backup': intent['backup'], 'journalEpoch': current['epoch']}


def _restore(intent):
    """Return to the previous sealed Laya bundle before any new selected write."""
    state, project = Path(intent['stateRoot']), Path(intent['projectRoot'])
    ledger = _intent_ledger(intent)
    journal = state / 'install-journal.sqlite'
    auto = project_dir.folder(project)
    old_route, new_route = (intent[key].encode('utf-8') for key in
                            ('oldRoute', 'newRoute'))
    old_att, new_att = (intent[key].encode('utf-8') for key in
                        ('oldAttestation', 'newAttestation'))
    old_sha, new_sha = _sha(old_route), _sha(new_route)
    current = inspect_journal(journal)
    row = ledger.load(state / ledger.name)
    if (row['phase'] == 'active' and row['routeSha256'] == old_sha and
            current['state'] == 'restored_old' and
            current['selectedRouteSha256'] == old_sha and
            ledger.classify(project, auto / ledger.marker_name,
                                  auto / ledger.route_name) == 'candidate'):
        return {'status': 'active_upgrade_restored_laya',
                'routeSha256': old_sha, 'vectorCounts': intent['counts'],
                'backup': intent['backup']}
    if current['newWriteSeen'] or current['previousRouteSha256'] != old_sha:
        if (row['phase'] == 'upgrading' and row['routeSha256'] == old_sha and
                current['state'] in ('active', 'restored_old') and
                current['selectedRouteSha256'] == old_sha and
                current['providerAttestationSha256'] ==
                intent['oldLedger']['attestationSha256'] and
                _read(auto / ledger.route_name, 8192) == old_route and
                _read(state / 'reviewed-route.json', 8192) == old_route and
                _read(auto / ledger.marker_name, 8192) ==
                intent['oldMarker'].encode('utf-8') and
                _read(state / PRODUCTION_ATTESTATION, 1024 * 1024) == old_att):
            row = ledger.change(state / ledger.name, row['epoch'],
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
        row = ledger.change(state / ledger.name, row['epoch'], 'active',
                            'upgrading')
    if row['phase'] != 'upgrading' or row['routeSha256'] not in (old_sha, new_sha):
        _fail('active_upgrade_restore_phase_changed')
    _wait_for_sockets(intent)
    inactive._atomic_expected(auto / ledger.route_name, new_route, old_route)
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
    inactive._atomic_expected(auto / ledger.marker_name,
                              intent['newMarker'].encode('utf-8'),
                              intent['oldMarker'].encode('utf-8'))
    with ledger.lock(state / ledger.name):
        row = ledger.load(state / ledger.name)
        if row['phase'] != 'upgrading' or row['routeSha256'] not in (old_sha, new_sha):
            _fail('active_upgrade_restore_phase_changed')
        old_row = {**intent['oldLedger'], 'epoch': row['epoch'] + 1,
                   'phase': 'upgrading'}
        ledger.write(state / ledger.name, old_row)
    if inactive._store_counts(intent['oldRecord']['identity']) != intent['counts']:
        _fail('active_upgrade_restore_store_changed')
    row = ledger.change(state / ledger.name, old_row['epoch'], 'upgrading',
                        'active')
    if (row['routeSha256'] != old_sha or
            ledger.classify(project, auto / ledger.marker_name,
                                  auto / ledger.route_name) != 'candidate'):
        _fail('active_upgrade_restore_unconfirmed')
    return {'status': 'active_upgrade_restored_laya',
            'routeSha256': old_sha, 'vectorCounts': intent['counts'],
            'backup': intent['backup']}


def _archive_finished_intent(state, project, intent):
    """Retain the old receipt when a later exact bundle is requested."""
    target = finished_intent_receipt(state, project, intent)
    os.rename(_intent_path(state), target)
    inactive._fsync(state)


def finished_intent_receipt(state, project, intent):
    """Receipt path of a settled intent; read-only, refuses an unsettled one."""
    ledger = _intent_ledger(intent)
    row = ledger.load(state / ledger.name)
    journal = inspect_journal(state / 'install-journal.sqlite')
    if intent['schema'] == V2:
        _completed_recovery(intent)
        inactive._store_counts(intent['oldRecord']['identity'])
    current = row['routeSha256']
    if (row['phase'] != 'active' or
            current not in (intent['oldLedger']['routeSha256'],
                            intent['newLedger']['routeSha256']) or
            journal['state'] not in ('active', 'restored_old') or
            journal['selectedRouteSha256'] != current or
            ledger.classify(project, project_dir.runtime_file(project, ledger.marker_name),
                                  project_dir.runtime_file(project, ledger.route_name))
            != 'candidate'):
        _fail('active_upgrade_prior_unsettled')
    # The original old route can be active only after an explicit Laya restore.
    if (current == intent['oldLedger']['routeSha256'] and
            journal['state'] != 'restored_old'):
        _fail('active_upgrade_prior_unsettled')
    tag = _sha(_bytes(intent))[:20]
    target = state / ('active-upgrade-' + tag + '-settled.json')
    if target.exists() or target.is_symlink():
        _fail('active_upgrade_receipt_exists')
    return target


def _reextracted(intent, plugin_root):
    """The settled intent's bundle root no longer holds its sealed files.

    A host re-extracting the same version in place gives the files new
    inodes; the receipt is archived and the same root is adopted again.
    """
    from laya_product_bundle import BundleEvidenceError
    try:
        live = inspect_full_plugin_candidate(str(plugin_root))
    except BundleEvidenceError:
        return True
    return live['ownershipManifestSha256'] != intent['newProof']['ownershipManifestSha256']


def _completed_recovery(intent):
    state = Path(intent['stateRoot'])
    row = _intent_ledger(intent).load(state / fresh.LEDGER)
    current = _journal_snapshot(inspect_journal(state / 'install-journal.sqlite'))
    expected = _target_metadata(intent)
    expected[3] = _bytes({**intent['newLedger'], 'phase': 'active',
                          'epoch': intent['newLedger']['epoch'] + 1}).decode()
    if (intent['recovery']['mode'] != 'switch' or _metadata(intent) != expected or
            current['state'] != 'active' or current['pending'] is not None or
            current['selected'] != _target_identity(intent) or
            current['selectedRouteSha256'] != intent['newLedger']['routeSha256'] or
            current['providerAttestationSha256'] != intent['newLedger']['attestationSha256'] or
            current['providerFileIdentitySha256'] !=
            intent['newLedger']['providerFileIdentitySha256']):
        _fail('active_upgrade_prior_unsettled')
    return row, current


def upgrade_fenced(state_root):
    path = Path(state_root) / fresh.LEDGER
    return path.exists() and fresh._load(path)['phase'] == 'upgrading'


def recovery_attestation(project_root, state_root):
    intent = _load_intent(Path(state_root), Path(project_root))
    if intent is None:
        _fail('active_upgrade_intent_missing')
    return json.loads(intent['oldAttestation'])


def finish_activated(*, project_root, state_root, requested_root=None):
    """Finish an upgrade that stopped after its journal switched; None otherwise.

    A worker can stop after ``cas_activate`` and before the ledger leaves
    ``upgrading``: the journal already selects the new route and bundle, so
    there is nothing left to review, and a later worker sees that bundle as
    current. This re-proves the intent's new bundle and backup and runs the
    rest of the switch (each step accepts its finished form), which releases
    the fence. Any other state returns None and changes nothing.
    """
    raw = Path(state_root)
    if not (raw / INTENT).exists() or not (raw / fresh.LEDGER).exists():
        return None  # nothing was ever fenced here: no locks, no proofs
    state = _private_parent(state_root)
    ledger = _origin(state)
    if ledger.load(state / ledger.name)['phase'] != 'upgrading':
        return None
    project = Path(project_root).resolve(strict=True)
    journal = state / 'install-journal.sqlite'
    _ledger_path, marker, _route = ledger.paths(project, journal)
    with _install_lock(state), _operation_lock(marker), _locked_provider():
        intent = _load_intent(state, project)
        if intent is None or _intent_ledger(intent).origin != ledger.origin:
            return None
        if (intent['schema'] == V2 or
                (requested_root is not None and
                 intent['newProof']['pluginRoot'] != str(requested_root))):
            return None
        row = ledger.load(state / ledger.name)
        current = inspect_journal(journal)
        if (row != intent['newLedger'] or row['phase'] != 'upgrading' or
                current['state'] != 'active' or
                current['selectedRouteSha256'] != intent['newLedger']['routeSha256']):
            return None
        if current['newWriteSeen']:
            _fail('active_upgrade_rollback_forward_only')
        from laya_product_bundle import BundleEvidenceError
        try:
            live = inspect_full_plugin_candidate(intent['newProof']['pluginRoot'])
        except BundleEvidenceError:
            return None
        if any(live[key] != intent['newProof'][key] for key in intent['newProof']):
            return None
        _backup_proof(intent)
        return _switch(intent)


def upgrade(*, project_root, state_root, source_root, plugin_root, commit_id,
            accepted_provider_sha256, model_path, dimension, restore=False):
    """Resume one exact sealed upgrade; a failed step leaves a blocked route."""
    project = Path(project_root).resolve(strict=True)
    state = _private_parent(state_root)
    if project != Path(project_root) or state / 'install-journal.sqlite' != \
            Path(state_root) / 'install-journal.sqlite':
        _fail('active_upgrade_paths_invalid')
    selected_ledger = _origin(state)
    _ledger_path, marker, _route = selected_ledger.paths(
        project, state / 'install-journal.sqlite')
    with _install_lock(state), _operation_lock(marker), _locked_provider():
        intent = _load_intent(state, project)
        if intent is not None and _intent_ledger(intent).origin != selected_ledger.origin:
            _fail('active_upgrade_intent_invalid')
        ledger = selected_ledger
        if restore:
            if intent is None:
                _fail('active_upgrade_intent_missing')
            if intent['schema'] == V2:
                _fail('active_upgrade_rollback_forward_only')
            _backup_proof(intent, verify_new=False)
            return _restore(intent)
        if intent is not None:
            current_row = ledger.load(state / ledger.name)
            current_journal = inspect_journal(state / 'install-journal.sqlite')
            if (current_row['phase'] == 'upgrading' and current_journal['newWriteSeen'] and
                    (current_journal['state'] == 'pending' or
                     current_journal['selectedRouteSha256'] == intent['newLedger']['routeSha256'])):
                _fail('active_upgrade_rollback_forward_only')
            if (current_row['phase'] == 'upgrading' and
                    (intent['schema'] == V2 or
                     ((current_journal['state'] == 'pending' or
                       (current_journal['state'] == 'active' and
                        current_journal['selectedRouteSha256'] ==
                        intent['newLedger']['routeSha256'])) and
                      (intent['newProof']['pluginRoot'] != str(plugin_root) or
                       intent['newProof']['sourceCommit'] != commit_id or
                       intent['reviewSha256'] != accepted_provider_sha256 or
                       _reextracted(intent, plugin_root))))):
                return _recover_upgrade(intent, source_root, plugin_root, commit_id,
                                         accepted_provider_sha256, model_path, dimension)
            settled_restored = (
                current_row['phase'] == 'active' and
                current_row['routeSha256'] == intent['oldLedger']['routeSha256'] and
                current_journal['state'] == 'restored_old' and
                current_journal['selectedRouteSha256'] ==
                intent['oldLedger']['routeSha256'])
            settled_forward = (
                current_row['phase'] == 'active' and
                current_row['routeSha256'] == intent['newLedger']['routeSha256'] and
                current_journal['state'] == 'active' and
                current_journal['selectedRouteSha256'] ==
                intent['newLedger']['routeSha256'])
        else:
            settled_restored = settled_forward = False
        if (intent is not None and current_row['phase'] == 'upgrading' and
                current_row['routeSha256'] == intent['oldLedger']['routeSha256'] and
                current_journal['state'] in ('active', 'restored_old') and
                current_journal['selectedRouteSha256'] ==
                intent['oldLedger']['routeSha256'] and
                (intent['newProof']['pluginRoot'] != str(plugin_root) or
                 intent['newProof']['sourceCommit'] != commit_id or
                 intent['reviewSha256'] != accepted_provider_sha256 or
                 _reextracted(intent, plugin_root))):
            # Before staging, no target metadata is visible. Keep the fence
            # and original snapshot while re-proving a replacement target.
            _backup_proof(intent, verify_new=False)
            intent = _prepare(project, state, state / 'install-journal.sqlite',
                              source_root, plugin_root, commit_id,
                              accepted_provider_sha256, model_path, dimension,
                              ledger, prior_intent=intent)
        if intent is not None and (settled_restored or
                                   intent['newProof']['pluginRoot'] != str(plugin_root)
                                   or intent['newProof']['sourceCommit'] != commit_id
                                   or (settled_forward and
                                       _reextracted(intent, plugin_root))):
            # Only a settled intent is archived, and ``_archive_finished_intent``
            # is that proof. A settled
            # intent is a receipt: its new bundle is now the bound one, which
            # its host may already have replaced or removed, so its files are
            # not re-proved here; ``_old_state`` proves the selection next.
            _backup_proof(intent, verify_new=False)
            _archive_finished_intent(state, project, intent)
            intent = None
        if intent is None:
            # A new switch publishes a route the earlier plugin id's Hermes
            # copy cannot read (its edit gate would block); an interrupted
            # one above still resumes.
            from laya_journal_v4 import refuse_while_legacy_hermes
            refuse_while_legacy_hermes()
            intent = _prepare(project, state, state / 'install-journal.sqlite',
                              source_root, plugin_root, commit_id,
                              accepted_provider_sha256, model_path, dimension,
                              ledger)
        elif (intent['newProof']['pluginRoot'] != str(plugin_root) or
              intent['newProof']['sourceCommit'] != commit_id or
              intent['reviewSha256'] != accepted_provider_sha256):
            _fail('active_upgrade_intent_changed')
        _backup_proof(intent)
        return _switch(intent)

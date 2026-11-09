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


def _old_state(project, state, journal, ledger=None, *, prior_intent=None, unfenced_v4=False,
               unfenced=False):
    ledger = ledger or _Ledger('fresh')
    auto = project_dir.folder(project)
    row = ledger.load(state / ledger.name)
    if prior_intent is not None:
        old = prior_intent['oldLedger']
        expected = (old if unfenced_v4 or unfenced else
                    {**old, 'epoch': old['epoch'] + 1, 'phase': 'upgrading'})
        if row != expected:
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
             *, prior_intent=None, runtime_generation=None, unfenced_v4=False,
             unfenced=False):
    """Write one exact intent; ``prior_intent`` re-proves a new target.

    ``unfenced`` replaces a pre-fence intent (``_prefence_intent``): its old
    row is still selected, so it gets no retarget seal and is sealed afresh.
    The selected store kept syncing meanwhile; its backup is reused only when
    the counts and journal still match, otherwise a new one is taken.
    """
    ledger = ledger or _Ledger('fresh')
    if unfenced and (prior_intent is None or unfenced_v4):
        _fail('active_upgrade_intent_invalid')
    if unfenced_v4:
        import laya_install_journal as install_journal
        if prior_intent is None or install_journal.journal_version(journal) != 4:
            _fail('active_upgrade_bridge_state_changed')
    row, selected, old_route, old_att, old_marker = _old_state(project, state,
                                                               journal, ledger,
                                                               prior_intent=prior_intent,
                                                               unfenced_v4=unfenced_v4,
                                                               unfenced=unfenced)
    if unfenced_v4 and _journal_snapshot(selected) != prior_intent['journalBefore']:
        _fail('active_upgrade_journal_changed')
    record = build.read(state)
    if (record is None or record['identity']['projectRoot'] != str(project) or
            record['identity']['stateRoot'] != str(state) or
            record['identity']['vectorProfileId'] != row['vectorProfileId']):
        _fail('active_upgrade_build_binding_changed')
    if prior_intent is not None and record != prior_intent['oldRecord']:
        _fail('active_upgrade_build_binding_changed')
    review = review_local_provider(source_root=source_root, plugin_root=plugin_root,
                                   state_root=state, commit_id=commit_id,
                                   model_path=model_path, dimension=dimension,
                                   **({'runtime_generation': runtime_generation}
                                      if runtime_generation is not None else {}))
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
    if prior_intent is None or (unfenced and (
            counts != prior_intent['counts'] or
            _journal_snapshot(selected) != prior_intent['journalBefore'])):
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
    if prior_intent is not None:
        if unfenced_v4 or unfenced:
            current_row, current, _route, _att, _marker = _old_state(
                project, state, journal, ledger, prior_intent=prior_intent,
                unfenced_v4=unfenced_v4, unfenced=unfenced)
            if (current_row != row or _journal_snapshot(current) != intent['journalBefore'] or
                    _load_intent(state, project) != prior_intent):
                _fail('active_upgrade_intent_changed')
            _backup_proof(prior_intent, verify_new=False)
        if not unfenced:
            _seal_prestage_retarget(prior_intent, intent)
    build._write_json(_intent_path(state), intent, replace=prior_intent is not None,
                      max_bytes=LIMIT)
    if prior_intent is None or unfenced:
        _seal_reader_intent(state, project, intent)
    return intent


def _seal_reader_intent(state, project, intent):
    """Commit the selected v5 reader and exact upgrade intent before fencing.

    This records evidence only; it does not issue a reader or writer token.
    """
    import laya_install_journal as journal
    import laya_journal_reader as reader
    path = state / 'install-journal.sqlite'
    if journal.journal_version(path) != 5:
        return
    _backup_proof(intent)
    if _load_intent(state, project) != intent or _read(_intent_path(state)) != _bytes(intent):
        _fail('active_upgrade_intent_changed')
    decision = journal.select_route(str(project), action='recall')
    proof = journal.verify_selected_route_read_only(decision)
    if (proof['readOnlyAllowed'] is not True or proof['productRouteAllowed'] is not False or
            proof['journalPath'] != str(path) or proof['projectRoot'] != str(project)):
        _fail('active_upgrade_reader_unproved')
    digest = _sha(_bytes(intent))

    def commit(db):
        selected, endpoint = reader._selection(db)
        row = journal._row(db)
        if endpoint['state'] != str(state):
            # A released state move relocated the journal but not the v6
            # reader. Re-seal it here only when the move proves the old root.
            try:
                reader.relocate_selection(db, path, row['provider_attestation_sha'],
                                          released=(project, decision, proof))
            except (ValueError, OSError, KeyError, TypeError):
                _fail('active_upgrade_reader_changed')
            selected, endpoint = reader._selection(db)
        # A v6 endpoint names the selected v6 store; a schema3 endpoint names
        # its own store incarnation, reproved by the fenced reader evidence.
        store_ok = ((endpoint['capability'] == 'v6-ro-v1' and
                     endpoint['incarnation'] == proof['dbIncarnation']) or
                    endpoint['capability'] == reader.SCHEMA3_CAPABILITY)
        if (_journal_snapshot(journal.inspect_journal(path)) != intent['journalBefore'] or
                row['epoch'] != proof['selectionEpoch'] or
                endpoint['scope'] != proof['scopeId'] or endpoint['install_id'] != proof['installId'] or
                endpoint['project'] != str(project) or endpoint['state'] != str(state) or
                not store_ok or
                endpoint['profile'] != proof['vectorProfile']['profileId']):
            _fail('active_upgrade_reader_changed')
        previous_sha = _sha(intent['oldAttestation'].encode('utf8'))
        if endpoint['attestation_sha256'] != previous_sha:
            reader._successor_chain(db, selected.preferred_endpoint, endpoint, previous_sha,
                                    intent['journalBefore']['selected'], max_epoch=row['epoch'])
        record = {'schema': 'memvane-upgrade-reader-v1', 'intentSha256': digest,
                  'endpointId': selected.preferred_endpoint, 'readerEpoch': selected.epoch,
                  'readerTransition': selected.transition_id, 'journalBefore': intent['journalBefore'],
                  'decision': decision}
        encoded = journal_canon(record)
        added_successor_table = db.execute("SELECT 1 FROM sqlite_master WHERE name='upgrade_read_successors'").fetchone() is None
        for sql in (*journal._UPGRADE_READER_DDL.values(), *journal._UPGRADE_SUCCESSOR_DDL.values()):
            db.execute(sql.replace('CREATE TABLE ', 'CREATE TABLE IF NOT EXISTS ', 1)
                       .replace('CREATE TRIGGER ', 'CREATE TRIGGER IF NOT EXISTS ', 1))
        existing = db.execute('SELECT endpoint_id,record FROM upgrade_read_seals WHERE intent_sha256=?',
                              (digest,)).fetchone()
        if existing is not None:
            if existing != (selected.preferred_endpoint, encoded):
                _fail('active_upgrade_reader_changed')
            return added_successor_table
        db.execute('INSERT INTO upgrade_read_seals VALUES(?,?,?)',
                   (digest, selected.preferred_endpoint, encoded))
        return True

    def recheck():
        if (_load_intent(state, project) != intent or _read(_intent_path(state)) != _bytes(intent) or
                _old_state(project, state, path)[0] != intent['oldLedger']):
            _fail('active_upgrade_reader_changed')
        _backup_proof(intent)

    reader.mutate_image(path, commit, before_replace=recheck)


def _reader_seal(db, digest, endpoint_id):
    """Validate the immutable descriptor chain back to its v1 reader seal."""
    import laya_journal_reader as reader
    seen, child, first = set(), None, None
    while True:
        reader._need(type(digest) is str and reader.journal.SHA.fullmatch(digest) and
                     digest not in seen, 'reader_upgrade_seal_unproved')
        seen.add(digest)
        row = db.execute('SELECT endpoint_id,record FROM upgrade_read_seals WHERE intent_sha256=?',
                         (digest,)).fetchone()
        reader._need(row is not None and row[0] == endpoint_id, 'reader_upgrade_seal_unproved')
        seal = json.loads(row[1], object_pairs_hook=reader.journal._pairs,
                          parse_constant=reader.journal._nonfinite)
        if first is None:
            first = seal
        common = {'schema','intentSha256','endpointId','readerEpoch','readerTransition','journalBefore','decision'}
        reader._need(type(seal) is dict and journal_canon(seal) == row[1] and
                     seal.get('intentSha256') == digest and seal.get('endpointId') == endpoint_id,
                     'reader_upgrade_seal_unproved')
        if child is not None:
            reader._need(all(seal.get(key) == child.get(key) for key in
                         ('endpointId','readerEpoch','readerTransition','journalBefore','decision')),
                         'reader_upgrade_seal_unproved')
        if seal.get('schema') == 'memvane-upgrade-reader-v1':
            reader._need(set(seal) == common, 'reader_upgrade_seal_unproved')
            return first
        schema = seal.get('schema')
        extra = {'previousIntentSha256','targetIdentity','targetAttestationSha256','targetFileIdentitySha256'}
        reader._need(schema in ('memvane-upgrade-reader-v2','memvane-upgrade-reader-retarget-v1') and
                     set(seal) == common | extra | ({'recovery'} if schema == 'memvane-upgrade-reader-v2' else set()) and
                     (schema != 'memvane-upgrade-reader-v2' or type(seal['recovery']) is dict) and
                     type(seal['targetIdentity']) is dict and
                     type(seal['targetAttestationSha256']) is str and
                     reader.journal.SHA.fullmatch(seal['targetAttestationSha256']) and
                     type(seal['targetFileIdentitySha256']) is str and
                     reader.journal.SHA.fullmatch(seal['targetFileIdentitySha256']),
                     'reader_upgrade_seal_unproved')
        child, digest = seal, seal['previousIntentSha256']


def _seal_prestage_retarget(previous, successor):
    """Bind a newly reviewed pre-stage target before replacing its intent."""
    import laya_install_journal as journal
    import laya_journal_reader as reader
    state, project = Path(previous['stateRoot']), Path(previous['projectRoot'])
    path = state / 'install-journal.sqlite'
    if journal.journal_version(path) != 5:
        return
    fixed = ('projectRoot','stateRoot','oldRecord','oldLedger','oldRoute','oldAttestation',
             'oldMarker','backup','counts','journalBefore')
    reader._need(successor['schema'] == V1 and all(previous.get(k) == successor.get(k) for k in fixed),
                 'reader_upgrade_intent_changed')
    old_digest, new_digest = _sha(_bytes(previous)), _sha(_bytes(successor))
    _backup_proof(successor)
    row, selected, _route, _att, _marker = _old_state(project, state, path, prior_intent=previous)
    reader._need(row == successor['oldLedger'] and _journal_snapshot(selected) == successor['journalBefore'] and
                 _load_intent(state,project) == previous, 'reader_upgrade_prefix_unproved')

    def commit(db):
        selected_reader, _stored = reader._selection(db)
        parent = _reader_seal(db, old_digest, selected_reader.preferred_endpoint)
        record = {key:parent[key] for key in ('endpointId','readerEpoch','readerTransition','journalBefore','decision')}
        record.update({'schema':'memvane-upgrade-reader-retarget-v1', 'intentSha256':new_digest,
                       'previousIntentSha256':old_digest, 'targetIdentity':_target_identity(successor),
                       'targetAttestationSha256':successor['newLedger']['attestationSha256'],
                       'targetFileIdentitySha256':successor['newLedger']['providerFileIdentitySha256']})
        encoded = journal_canon(record)
        existing = db.execute('SELECT endpoint_id,record FROM upgrade_read_seals WHERE intent_sha256=?',
                              (new_digest,)).fetchone()
        if existing is not None:
            reader._need(existing == (selected_reader.preferred_endpoint,encoded), 'reader_upgrade_seal_unproved')
            return False
        db.execute('INSERT INTO upgrade_read_seals VALUES(?,?,?)',
                   (new_digest,selected_reader.preferred_endpoint,encoded))
        return True

    def recheck():
        old, current, _route, _att, _marker = _old_state(project, state, path, prior_intent=previous)
        reader._need(_load_intent(state,project) == previous and old == successor['oldLedger'] and
                     _journal_snapshot(current) == successor['journalBefore'], 'reader_upgrade_prefix_unproved')
        _backup_proof(successor)
    reader.mutate_image(path, commit, before_replace=recheck)


def _seal_recovery_descriptor(previous, successor):
    """Persist a checked v2 descriptor before replacing the live intent."""
    import laya_install_journal as journal
    import laya_journal_reader as reader
    state, project = Path(previous['stateRoot']), Path(previous['projectRoot'])
    path = state / 'install-journal.sqlite'
    if journal.journal_version(path) != 5:
        return
    _validate_recovery(successor)
    old_digest, new_digest = _sha(_bytes(previous)), _sha(_bytes(successor))
    fixed = ('projectRoot','stateRoot','oldRecord','oldLedger','oldRoute','oldAttestation',
             'oldMarker','backup','counts','journalBefore')
    reader._need(all(previous.get(k) == successor.get(k) for k in fixed), 'reader_upgrade_intent_changed')
    actual, current = _metadata(previous), _journal_snapshot(inspect_journal(path))
    _recovery_state(previous, actual, current)
    descriptor = successor['recovery']
    reader._need(descriptor['before'] == actual and descriptor['journalBefore'] == current,
                 'reader_upgrade_prefix_unproved')
    _backup_proof(successor, verify_new=descriptor['mode'] == 'switch')

    def commit(db):
        selected, _stored = reader._selection(db)
        parent = _reader_seal(db, old_digest, selected.preferred_endpoint)
        if old_digest == new_digest:
            # A crash after writing the normalize descriptor can replay that
            # exact intent. Reuse its existing chain instead of constructing
            # a self-referential seal with a different parent.
            reader._need(parent.get('schema') == 'memvane-upgrade-reader-v2' and
                         parent.get('recovery') == descriptor and
                         parent.get('readerEpoch') == selected.epoch and
                         parent.get('readerTransition') == selected.transition_id and
                         parent.get('journalBefore') == successor['journalBefore'] and
                         parent.get('targetIdentity') == _target_identity(successor) and
                         parent.get('targetAttestationSha256') == successor['newLedger']['attestationSha256'] and
                         parent.get('targetFileIdentitySha256') == successor['newLedger']['providerFileIdentitySha256'],
                         'reader_upgrade_seal_unproved')
            return False
        record = {key:parent[key] for key in ('endpointId','readerEpoch','readerTransition','journalBefore','decision')}
        record.update({'schema':'memvane-upgrade-reader-v2', 'intentSha256':new_digest,
                  'previousIntentSha256':old_digest, 'recovery':descriptor,
                  'targetIdentity':_target_identity(successor),
                  'targetAttestationSha256':successor['newLedger']['attestationSha256'],
                  'targetFileIdentitySha256':successor['newLedger']['providerFileIdentitySha256']})
        encoded = journal_canon(record)
        existing = db.execute('SELECT endpoint_id,record FROM upgrade_read_seals WHERE intent_sha256=?',
                              (new_digest,)).fetchone()
        if existing is not None:
            reader._need(existing == (selected.preferred_endpoint,encoded), 'reader_upgrade_seal_unproved')
            return False
        db.execute('INSERT INTO upgrade_read_seals VALUES(?,?,?)',
                   (new_digest,selected.preferred_endpoint,encoded))
        return True

    def recheck():
        reader._need(_load_intent(state,project) == previous and _metadata(previous) == actual and
                     _journal_snapshot(inspect_journal(path)) == current, 'reader_upgrade_prefix_unproved')
        _backup_proof(successor, verify_new=descriptor['mode'] == 'switch')
    reader.mutate_image(path, commit, before_replace=recheck)


def _restore_key(intent):
    return _sha(b'memvane-upgrade-restore-v1\0' + _bytes(intent))


def _restore_progress(intent, record, actual, current):
    """Accept only the sealed restore's ordered metadata and journal states."""
    import laya_journal_reader as reader
    reader._need(type(record) is dict and set(record) ==
                 {'schema','intentSha256','endpointId','readerEpoch','readerTransition','before','journalBefore'} and
                 record['schema'] == 'memvane-upgrade-restore-v1' and
                 record['intentSha256'] == _sha(_bytes(intent)), 'reader_upgrade_seal_unproved')
    before, journal_before = record['before'], record['journalBefore']
    reader._need(type(before) is list and len(before) == 5 and
                 type(journal_before) is dict and set(journal_before) == set(JOURNAL_KEYS),
                 'reader_upgrade_seal_unproved')
    row = _metadata_object(before[3])
    reader._need(row.get('phase') in ('active','upgrading') and
                 journal_before.get('state') in ('pending','active') and
                 journal_before.get('newWriteSeen') is False and
                 journal_before.get('previousRouteSha256') == _sha(intent['oldRoute'].encode()),
                 'reader_upgrade_seal_unproved')
    restored = {**journal_before, 'epoch': journal_before['epoch'] + 1,
                'state':'restored_old', 'selected':journal_before['previous'],
                'selectedRouteSha256':journal_before['previousRouteSha256'],
                'pending':None, 'pendingRouteSha256':None, 'newWriteSeen':False,
                'providerAttestationSha256':intent['oldLedger']['attestationSha256'],
                'providerFileIdentitySha256':intent['oldLedger']['providerFileIdentitySha256']}
    states = [(list(before), journal_before)]
    work = list(before)
    if row['phase'] == 'active':
        new = intent['newLedger']
        reader._need(row == {**new, 'epoch':new['epoch'] + 1, 'phase':'active'},
                     'reader_upgrade_seal_unproved')
        row = {**row, 'epoch':row['epoch'] + 1, 'phase':'upgrading'}
        work[3] = _bytes(row).decode()
        states.append((list(work), journal_before))
    for index, value in ((2,intent['oldRoute']), (0,intent['oldAttestation']),
                         (1,intent['oldRoute'])):
        work = list(work); work[index] = value
        states.append((work, journal_before))
    states.append((list(work), restored))
    work = list(work); work[4] = intent['oldMarker']
    states.append((work, restored))
    old = intent['oldLedger']
    already_old = all(row[key] == old[key] for key in old if key not in ('epoch','phase'))
    old_row = row if already_old else {**old, 'epoch':row['epoch'] + 1, 'phase':'upgrading'}
    work = list(work); work[3] = _bytes(old_row).decode()
    states.append((work, restored))
    reader._need(any(actual == metadata and current == snapshot for metadata, snapshot in states),
                 'reader_upgrade_restore_prefix_unproved')


def _restore_record(db, intent, selected):
    import laya_journal_reader as reader
    row = db.execute('SELECT endpoint_id,record FROM upgrade_read_seals WHERE intent_sha256=?',
                     (_restore_key(intent),)).fetchone()
    if row is None:
        return None
    record = json.loads(row[1], object_pairs_hook=reader.journal._pairs,
                        parse_constant=reader.journal._nonfinite)
    reader._need(journal_canon(record) == row[1] and row[0] == selected.preferred_endpoint and
                 record.get('endpointId') == selected.preferred_endpoint and
                 record.get('readerEpoch') == selected.epoch and
                 record.get('readerTransition') == selected.transition_id,
                 'reader_upgrade_seal_unproved')
    _reader_seal(db, _sha(_bytes(intent)), selected.preferred_endpoint)
    return record


def _seal_restore(intent):
    """Commit restore intent to the existing immutable v5 reader journal."""
    import laya_install_journal as journal
    import laya_journal_reader as reader
    state, project = Path(intent['stateRoot']), Path(intent['projectRoot'])
    path = state / 'install-journal.sqlite'
    if journal.journal_version(path) != 5:
        return
    actual, current = _metadata(intent), _journal_snapshot(inspect_journal(path))
    _backup_proof(intent, verify_new=False)
    def commit(db):
        selected, _stored = reader._selection(db)
        existing = _restore_record(db, intent, selected)
        if existing is not None:
            _restore_progress(intent, existing, actual, current)
            return False
        # The live state must still be a proved forward prefix. A completed
        # upgrade re-enters the fence with the same ledger fields at a later
        # epoch; compare it with the sealed pre-activation ledger.
        checked = list(actual)
        row = _metadata_object(checked[3])
        new = intent['newLedger']
        if all(row.get(key) == value for key,value in new.items() if key not in ('epoch','phase')):
            reader._need((row['phase'],row['epoch']) in
                         (('upgrading',new['epoch']), ('active',new['epoch'] + 1),
                          ('upgrading',new['epoch'] + 2)),
                         'reader_upgrade_restore_prefix_unproved')
            checked[3] = _bytes(new).decode()
        _recovery_state(intent, checked, current)
        _reader_seal(db, _sha(_bytes(intent)), selected.preferred_endpoint)
        record = {'schema':'memvane-upgrade-restore-v1', 'intentSha256':_sha(_bytes(intent)),
                  'endpointId':selected.preferred_endpoint, 'readerEpoch':selected.epoch,
                  'readerTransition':selected.transition_id, 'before':actual, 'journalBefore':current}
        _restore_progress(intent, record, actual, current)
        db.execute('INSERT INTO upgrade_read_seals VALUES(?,?,?)',
                   (_restore_key(intent),selected.preferred_endpoint,journal_canon(record)))
        return True
    def recheck():
        reader._need(_load_intent(state,project) == intent and _metadata(intent) == actual and
                     _journal_snapshot(inspect_journal(path)) == current,
                     'reader_upgrade_restore_prefix_unproved')
        _backup_proof(intent, verify_new=False)
    reader.mutate_image(path, commit, before_replace=recheck)


def _fenced_reader_evidence(state, project, root, endpoint_id, stored, attestation):
    """Prove the initial fenced prefix against the pre-fence journal seal."""
    import laya_index_host_route as host
    import laya_install_journal as journal
    import laya_journal_reader as reader
    from laya_ledger_common import refuse_qmd_origin
    state, project, root = reader._state(state), Path(project), Path(root)
    refuse_qmd_origin(state)
    intent = _load_intent(state, project)
    reader._need(intent is not None and intent['schema'] in (V1,V2), 'reader_upgrade_intent_unproved')
    raw = _read(_intent_path(state))
    reader._need(raw == _bytes(intent), 'reader_upgrade_intent_changed')
    digest = _sha(raw)
    path = state / 'install-journal.sqlite'
    selected = inspect_journal(path)
    if intent['schema'] == V2 or _journal_snapshot(selected) != intent['journalBefore']:
        actual = _metadata(intent)
        with reader._readonly(path, state) as db:
            current, _stored = reader._selection(db)
            restore = _restore_record(db, intent, current) if intent['schema'] == V1 else None
        if restore is None:
            _recovery_state(intent, actual, _journal_snapshot(selected))
        else:
            _restore_progress(intent, restore, actual, _journal_snapshot(selected))
        old, att = intent['oldLedger'], intent['oldAttestation'].encode('utf8')
        store_info = intent['journalBefore']
    else:
        old, selected, _route, att, _marker = _old_state(project, state, path, prior_intent=intent)
        reader._need(_journal_snapshot(selected) == intent['journalBefore'],
                     'reader_upgrade_prefix_unproved')
        store_info = selected
    reader._need(
                 _sha(attestation) == stored['attestation_sha256'] and
                 build.read(state) == intent['oldRecord'], 'reader_upgrade_prefix_unproved')
    with reader._readonly(path, state) as db:
        current, current_record = reader._selection(db)
        reader._need(endpoint_id == current.preferred_endpoint and
                     current_record == stored, 'reader_upgrade_seal_unproved')
        if att != attestation:
            reader._successor_chain(db, endpoint_id, stored, _sha(att),
                                    intent['journalBefore']['selected'],
                                    max_epoch=intent['journalBefore']['epoch'], fenced=True)
        seal = _reader_seal(db, digest, endpoint_id)
        reader._need(seal['intentSha256'] == digest and seal['endpointId'] == endpoint_id and
                     seal['readerEpoch'] == current.epoch and
                     seal['readerTransition'] == current.transition_id and
                     seal['journalBefore'] == intent['journalBefore'], 'reader_upgrade_seal_unproved')
        if intent['schema'] == V2:
            reader._need(seal['schema'] == 'memvane-upgrade-reader-v2' and
                         seal['recovery'] == intent['recovery'] and
                         seal['targetIdentity'] == _target_identity(intent) and
                         seal['targetAttestationSha256'] == intent['newLedger']['attestationSha256'] and
                         seal['targetFileIdentitySha256'] == intent['newLedger']['providerFileIdentitySha256'],
                         'reader_upgrade_seal_unproved')
        elif seal['schema'] == 'memvane-upgrade-reader-retarget-v1':
            reader._need(seal['targetIdentity'] == _target_identity(intent) and
                         seal['targetAttestationSha256'] == intent['newLedger']['attestationSha256'] and
                         seal['targetFileIdentitySha256'] == intent['newLedger']['providerFileIdentitySha256'],
                         'reader_upgrade_seal_unproved')
    route_path = project_dir.runtime_file(project, fresh.ROUTE)
    observed = host._classify_record(project, route_path.parent, route_path, project)
    decision = seal['decision']
    expected = dict(decision)
    current_route = json.loads(_read(route_path, 8192))
    expected.update({key: current_route[key] for key in
                     ('pluginRoot','bundleManifestSha256','ownershipManifestSha256')})
    reader._need(observed == expected and decision.get('state') == 'candidate' and
                 reader._sha(reader._raw(decision['recallSettings'])) == stored['settings'] and
                 decision['collections'] == stored['roots'], 'reader_upgrade_route_changed')
    target = intent['newProof']
    target_host = str(root) == target['pluginRoot']
    if not target_host:
        try:
            old_identity = intent['journalBefore']['selected']
            owner = journal._inspect_identity(old_identity)
            executing = journal._verified_executing_root(old_identity, owner)
            reader._need(executing == str(root), 'reader_upgrade_host_unproved')
        except (ValueError, OSError):
            target_host = True
    if target_host:
        _verify_recovery_target(intent)
        if str(root) == target['pluginRoot']:
            journal._executing_from_candidate(root)
            executing = str(root)
        else:
            owner = inspect_full_plugin_candidate(target['pluginRoot'])
            executing = journal._verified_executing_root(target, owner)
    reader._need(executing == str(root), 'reader_upgrade_host_unproved')
    proof = {'readOnlyAllowed': True, 'productRouteAllowed': False,
             'executingPluginRoot': executing, 'selectionEpoch': selected['epoch'],
             'projectRoot': str(project), 'journalPath': str(path),
             'installId': decision['installId'], 'scopeId': decision['scopeId'],
             'dbIncarnation': decision['dbIncarnation'], 'vectorProfile': decision['vectorProfile'],
             'readerDecision': decision}
    if target_host:
        proof['providerManifest'] = json.loads(intent['newAttestation'])['providerManifest']
    if stored.get('store_format') == 'schema3':
        current_record, current_att, proof = reader._schema3_record(state, project, decision, proof,
                                                                     old, store_info, stored,
                                                                     sealed_attestation=att)
    else:
        current_record, current_att, proof = reader._evidence_stores(state, project, decision, proof,
                                                                      old, store_info,
                                                                      sealed_attestation=att)
    current_record['attestation_sha256'] = stored['attestation_sha256']
    if 'legacy_sidecar_baseline' in stored:
        current_record['legacy_sidecar_baseline'] = stored['legacy_sidecar_baseline']
    current_record['creation_epoch'] = stored['creation_epoch']
    reader._need(current_record == stored and current_att == att and
                 endpoint_id == reader._sha(b'memvane-reader-endpoint-v1\0' + reader._raw(stored)),
                 'reader_upgrade_endpoint_changed')
    return current_record, attestation, proof


def _commit_reader_successor(db, path, before, values):
    """Seal a v1 upgrade successor in the activation's SQLite transaction."""
    import laya_journal_reader as reader
    state, project = path.parent, Path(before['project_root'])
    intent = _load_intent(state, project)
    reader._need(intent is not None and intent['schema'] in (V1,V2) and
                 _read(_intent_path(state)) == _bytes(intent), 'reader_successor_intent_unproved')
    digest = _sha(_bytes(intent))
    pending = inspect_journal(path)
    _recovery_state(intent, _metadata(intent), _journal_snapshot(pending))
    source = intent['journalBefore'] if intent['schema'] == V1 else intent['recovery']['journalBefore']
    reader._need((intent['schema'] == V1 or intent['recovery']['mode'] == 'switch') and
                 _metadata(intent) == _target_metadata(intent) and
                 _journal_snapshot(pending) == _journal_successor(source, intent) and
                 before['epoch'] == pending['epoch'] and before['selected'] == pending['selected'] and
                 before['pending'] == pending['pending'], 'reader_successor_prefix_unproved')
    _backup_proof(intent)
    selected, stored = reader._selection(db)
    seal = _reader_seal(db, digest, selected.preferred_endpoint)
    reader._need(seal.get('intentSha256') == digest and seal.get('endpointId') == selected.preferred_endpoint and
                 seal.get('journalBefore') == intent['journalBefore'] and
                 seal.get('readerEpoch') == selected.epoch and
                 seal.get('readerTransition') == selected.transition_id,
                 'reader_successor_seal_unproved')
    if intent['schema'] == V2:
        reader._need(seal['schema'] == 'memvane-upgrade-reader-v2' and
                     seal['recovery'] == intent['recovery'] and
                     seal['targetIdentity'] == _target_identity(intent) and
                     seal['targetAttestationSha256'] == intent['newLedger']['attestationSha256'] and
                     seal['targetFileIdentitySha256'] == intent['newLedger']['providerFileIdentitySha256'],
                     'reader_successor_seal_unproved')
    elif seal['schema'] == 'memvane-upgrade-reader-retarget-v1':
        reader._need(seal['targetIdentity'] == _target_identity(intent) and
                     seal['targetAttestationSha256'] == intent['newLedger']['attestationSha256'] and
                     seal['targetFileIdentitySha256'] == intent['newLedger']['providerFileIdentitySha256'],
                     'reader_successor_seal_unproved')
    from_sha = before['provider_attestation_sha']
    to_sha = _sha(intent['newAttestation'].encode('utf8'))
    target = _target_identity(intent)
    reader._need(before['state'] == 'pending' and before['selected'] == source['selected'] and
                 before['pending'] == target and from_sha == source['providerAttestationSha256'] and
                 values['provider_attestation_sha'] == to_sha and
                 values['selected_route_sha'] == intent['newLedger']['routeSha256'] and
                 type(values['provider_file_identity']) is str and
                 _sha(values['provider_file_identity'].encode('utf8')) ==
                     intent['newLedger']['providerFileIdentitySha256'] and
                 json.loads(values['selected']) == target and
                 stored['install_id'] == target['installId'] and
                 stored['profile'] == target['vectorProfile']['profileId'], 'reader_successor_changed')
    if stored['attestation_sha256'] != from_sha:
        reader._successor_chain(db, selected.preferred_endpoint, stored, from_sha,
                                before['selected'], max_epoch=before['epoch'])
    epoch = before['epoch'] + 1
    successor = {'schema':'memvane-reader-successor-v1', 'intentSha256':digest,
                 'endpointId':selected.preferred_endpoint, 'epoch':epoch,
                 'fromAttestation':from_sha, 'toAttestation':to_sha,
                 'fromIdentity':before['selected'], 'toIdentity':target}
    db.execute('INSERT INTO upgrade_read_successors VALUES(?,?,?,?)',
               (epoch, digest, selected.preferred_endpoint, journal_canon(successor)))


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
        replace_route = snapshot['state'] == 'pending' and _sha(expected[2].encode()) not in (
            snapshot['selectedRouteSha256'], snapshot['pendingRouteSha256'])
        if replace_route:
            expected[2] = intent['newRoute']
        if descriptor['after'] != expected:
            _fail('active_upgrade_recovery_invalid')
        if replace_route and (
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
                           accepted_provider_sha256, model_path, dimension,
                           runtime_generation=None):
    state = Path(intent['stateRoot'])
    review = review_local_provider(source_root=source_root, plugin_root=plugin_root,
                                   state_root=state, commit_id=commit_id,
                                   model_path=model_path, dimension=dimension,
                                   **({'runtime_generation': runtime_generation}
                                      if runtime_generation is not None else {}))
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
        if (not installer_journal._project_matches(original, bound) or
                bound['scope_id'] != original['scopeId']):
            raise installer_journal.JournalEvidenceError('recovery_project_identity_changed')
    finally:
        connection.close()


def _verify_recovery_scope(journal, original):
    """Recheck the project and settings-derived scope before recovery writes."""
    _verify_recovery_project(journal, original)
    from laya_index_host_route import (MAX_SETTINGS_BYTES, _derived_collections,
                                       _read_regular, settings_identity_digest)
    project = Path(original['projectRoot'])
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


def _recover_upgrade(intent, source_root, plugin_root, commit_id,
                      accepted_provider_sha256, model_path, dimension,
                      runtime_generation=None):
    state, project = Path(intent['stateRoot']), Path(intent['projectRoot'])
    journal = state / 'install-journal.sqlite'
    _backup_proof(intent, verify_new=False)
    if build.read(state) != intent['oldRecord']:
        _fail('active_upgrade_build_binding_changed')
    if inactive._store_counts(intent['oldRecord']['identity']) != intent['counts']:
        _fail('active_upgrade_restore_store_changed')
    original = json.loads(intent['oldRoute'])
    _verify_recovery_scope(journal, original)
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
        _verify_recovery_scope(journal, original)
        _seal_recovery_descriptor(intent, normalized)
        build._write_json(_intent_path(state), normalized, replace=True, max_bytes=LIMIT)
        _recovery_step('normalize-descriptor')
        if actual[2] != after[2]:
            _verify_recovery_scope(journal, original)
            inactive._atomic_expected(_metadata_paths(intent)[2],
                                      actual[2].encode(), after[2].encode())
            _recovery_step('normalize-route')
        if _metadata(intent) != after or _journal_snapshot(inspect_journal(journal)) != current:
            _fail('active_upgrade_recovery_journal_changed')
        intent = _fresh_recovery_target(normalized, after, source_root, plugin_root,
                                         commit_id, accepted_provider_sha256,
                                         model_path, dimension, runtime_generation)
        intent['recovery'] = {'mode': 'switch', 'before': after,
                              'after': _target_metadata(intent), 'journalBefore': current}
        _validate_recovery(intent)
        _verify_recovery_scope(journal, original)
        _seal_recovery_descriptor(normalized, intent)
        build._write_json(_intent_path(state), intent, replace=True, max_bytes=LIMIT)
        _recovery_step('switch-descriptor')
    descriptor = intent['recovery']
    current = _journal_snapshot(inspect_journal(journal))
    staged = _journal_successor(descriptor['journalBefore'], intent)
    activated = _journal_successor(staged, intent, activate=True)
    if current == descriptor['journalBefore']:
        _verify_recovery_target(intent)
        _verify_recovery_scope(journal, original)
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
            _verify_recovery_scope(journal, original)
            inactive._atomic_expected(path, before.encode(), after.encode())
            _recovery_step('metadata-' + str(index))
        _verify_recovery_target(intent)
        _verify_recovery_scope(journal, original)
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
    _verify_recovery_scope(journal, original)
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
    if (_settled_restored(intent, row, current) and
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
    if row['phase'] not in ('active','upgrading') or row['routeSha256'] not in (old_sha, new_sha):
        _fail('active_upgrade_restore_phase_changed')
    _seal_restore(intent)
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
        old = intent['oldLedger']
        already_old = all(row[key] == old[key] for key in old if key not in ('epoch','phase'))
        old_row = row if already_old else {**old, 'epoch': row['epoch'] + 1,
                                           'phase': 'upgrading'}
        if not already_old:
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


def _settled_restored(intent, row, journal):
    """The intent's old route is selected again after an operator restore.

    A restore that rewound the journal leaves it ``restored_old``. One that
    found nothing staged only released the fence: the journal still selects
    the old route and the old ledger is two epochs on (fence, release). An
    intent that never fenced keeps the old ledger row itself and is not
    settled; its resume still fences and switches.
    """
    old = intent['oldLedger']
    if (row['phase'] != 'active' or row['routeSha256'] != old['routeSha256'] or
            journal['state'] not in ('active', 'restored_old') or
            journal['selectedRouteSha256'] != old['routeSha256']):
        return False
    return (journal['state'] == 'restored_old' or
            row == {**old, 'epoch': old['epoch'] + 2})


def _prefence_intent(intent, row, journal):
    """The intent was written but never fenced: nothing public changed.

    The ledger row is the intent's old row itself (a release after a fence
    leaves it two epochs on) and the journal still selects that old route
    with nothing pending. ``newWriteSeen`` is not compared: the selected old
    route may mark its own first write meanwhile; the target never staged.
    """
    old = intent['oldLedger']
    return (intent['schema'] == V1 and row == old and
            journal['state'] == 'active' and journal.get('pending') is None and
            journal['pendingRouteSha256'] is None and
            journal['selectedRouteSha256'] == old['routeSha256'])


def _target_changed(intent, plugin_root, commit_id, accepted_provider_sha256):
    return (intent['newProof']['pluginRoot'] != str(plugin_root) or
            intent['newProof']['sourceCommit'] != commit_id or
            intent['reviewSha256'] != accepted_provider_sha256 or
            _reextracted(intent, plugin_root))


def restored_away_from(project_root, state_root, plugin_root):
    """Read-only: the operator restored this install away from ``plugin_root``.

    True when the active upgrade intent is a settled restore whose target is
    that bundle root with its sealed files unchanged. The SessionStart worker
    then leaves the install on the old bundle instead of fencing it again for
    the same bundle; the setup entry point's explicit upgrade archives the
    receipt and retries. A re-extracted or different bundle is a new target.
    """
    try:
        state = Path(state_root)
        project = Path(project_root).resolve(strict=True)
        intent = _load_intent(state, project)
        if intent is None or intent['schema'] == V2 or \
                intent['newProof']['pluginRoot'] != str(plugin_root):
            return False
        ledger = _intent_ledger(intent)
        if not _settled_restored(intent, ledger.load(state / ledger.name),
                                 inspect_journal(state / 'install-journal.sqlite')):
            return False
    except (OSError, KeyError, TypeError, ValueError):
        return False
    return not _reextracted(intent, plugin_root)


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
            not _settled_restored(intent, row, journal)):
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


def _prepare_v4_bridge(project, state, marker, ledger, *, source_root, plugin_root,
                       commit_id, accepted_provider_sha256, model_path, dimension,
                       runtime_generation, readiness):
    """Prepare under owner locks; convert outside them using the same intent."""
    import laya_bridge_readiness as bridge
    import laya_install_journal as journal
    import laya_journal_reader as reader
    if readiness is None:
        readiness = bridge.ensure_bridge_preflight(state, release_commit=commit_id)
    refs, release, hosts, bound_state = bridge._issued(readiness, bridge.BridgeReadiness)
    if (release != commit_id or bound_state != str(state) or not {'claude','codex'} <= hosts):
        raise ValueError('reader_hosts_pending')
    path = state / 'install-journal.sqlite'
    with _install_lock(state), _operation_lock(marker), _locked_provider():
        version = journal.journal_version(path)
        if version == 5:
            return
        if version != 4 or ledger.load(state / ledger.name)['phase'] != 'active':
            _fail('active_upgrade_bridge_state_changed')
        from laya_journal_v4 import refuse_while_legacy_hermes
        refuse_while_legacy_hermes()
        intent = _load_intent(state, project)
        if intent is not None and (intent['newProof']['pluginRoot'] != str(plugin_root) or
                                   intent['newProof']['sourceCommit'] != commit_id or
                                   intent['reviewSha256'] != accepted_provider_sha256):
            # A previous completed upgrade is a receipt, not the intent for
            # this bridge. The existing proof refuses unsettled transitions.
            _backup_proof(intent, verify_new=False)
            if intent['schema'] == V1 and ledger.load(state / ledger.name) == intent['oldLedger']:
                intent = _prepare(project, state, path, source_root, plugin_root, commit_id,
                                  accepted_provider_sha256, model_path, dimension, ledger,
                                  prior_intent=intent, runtime_generation=runtime_generation,
                                  unfenced_v4=True)
            else:
                _archive_finished_intent(state, project, intent)
                intent = None
        if intent is None:
            intent = _prepare(project, state, path, source_root, plugin_root, commit_id,
                              accepted_provider_sha256, model_path, dimension, ledger,
                              runtime_generation=runtime_generation)
        else:
            if (intent['schema'] != V1 or intent['newProof']['pluginRoot'] != str(plugin_root) or
                    intent['newProof']['sourceCommit'] != commit_id or
                    intent['reviewSha256'] != accepted_provider_sha256 or
                    ledger.load(state / ledger.name) != intent['oldLedger']):
                _fail('active_upgrade_intent_changed')
            _backup_proof(intent)
    installer = reader.seal_upgrade_reader(project, state, executing_root=Path(plugin_root))
    # Preparing and sealing a large store can outlive the short host receipt.
    # Reuse it if still valid; otherwise observe the registered hosts again.
    readiness = bridge.ensure_bridge_preflight(state, release_commit=commit_id)
    reader.convert_reader_v5(state, project, installer=installer, readiness=readiness)


def upgrade(*, project_root, state_root, source_root, plugin_root, commit_id,
            accepted_provider_sha256, model_path, dimension, restore=False,
            runtime_generation=None, bridge_readiness=None):
    """Resume one exact sealed upgrade; a failed step leaves a blocked route."""
    project = Path(project_root).resolve(strict=True)
    state = _private_parent(state_root)
    if project != Path(project_root) or state / 'install-journal.sqlite' != \
            Path(state_root) / 'install-journal.sqlite':
        _fail('active_upgrade_paths_invalid')
    selected_ledger = _origin(state)
    _ledger_path, marker, _route = selected_ledger.paths(
        project, state / 'install-journal.sqlite')
    if not restore:
        import laya_install_journal as journal
        version = journal.journal_version(state / 'install-journal.sqlite')
        if version in (3,4) and selected_ledger.load(state / selected_ledger.name)['phase'] == 'active':
            if version == 3:
                from laya_journal_v4 import convert_install
                converted = convert_install(state, project, root=Path(plugin_root))
                if converted.get('status') not in ('journal_v4_converted','journal_v4_current','journal_v5_current'):
                    raise ValueError('reader_hosts_pending')
                version = journal.journal_version(state / 'install-journal.sqlite')
            if version not in (4,5):
                raise ValueError('reader_hosts_pending')
            if version == 4:
                _prepare_v4_bridge(project, state, marker, selected_ledger, source_root=source_root,
                    plugin_root=plugin_root, commit_id=commit_id, accepted_provider_sha256=accepted_provider_sha256,
                    model_path=model_path, dimension=dimension, runtime_generation=runtime_generation,
                    readiness=bridge_readiness)
    with _install_lock(state), _operation_lock(marker), _locked_provider():
        intent = _load_intent(state, project)
        prepared_here = False
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
                                         accepted_provider_sha256, model_path, dimension,
                                         runtime_generation)
            settled_restored = _settled_restored(intent, current_row, current_journal)
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
                _target_changed(intent, plugin_root, commit_id, accepted_provider_sha256)):
            # Before staging, no target metadata is visible. Keep the fence
            # and original snapshot while re-proving a replacement target.
            _backup_proof(intent, verify_new=False)
            intent = _prepare(project, state, state / 'install-journal.sqlite',
                              source_root, plugin_root, commit_id,
                              accepted_provider_sha256, model_path, dimension,
                              ledger, prior_intent=intent, runtime_generation=runtime_generation)
        elif (intent is not None and not settled_restored and
              _prefence_intent(intent, current_row, current_journal) and
              _target_changed(intent, plugin_root, commit_id, accepted_provider_sha256)):
            # Stopped before its fence, e.g. at the worker deadline, and its
            # bundle may since be gone. It is no receipt, so re-prove the
            # requested target in its place instead of resuming the old one.
            _backup_proof(intent, verify_new=False)
            intent = _prepare(project, state, state / 'install-journal.sqlite',
                              source_root, plugin_root, commit_id,
                              accepted_provider_sha256, model_path, dimension,
                              ledger, prior_intent=intent, runtime_generation=runtime_generation,
                              unfenced=True)
            prepared_here = True
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
                              ledger, runtime_generation=runtime_generation)
            prepared_here = True
        elif (intent['newProof']['pluginRoot'] != str(plugin_root) or
              intent['newProof']['sourceCommit'] != commit_id or
              intent['reviewSha256'] != accepted_provider_sha256):
            _fail('active_upgrade_intent_changed')
        _backup_proof(intent)
        # A process may have stopped after writing the intent but before
        # publishing its reader commitment. Resume that step before fencing.
        if not prepared_here and ledger.load(state / ledger.name) == intent['oldLedger']:
            _seal_reader_intent(state, project, intent)
        return _switch(intent)

"""Installer-owned reader selection in a v5 journal. No index writer authority."""
from __future__ import annotations

from contextlib import closing, contextmanager, ExitStack
from contextvars import ContextVar
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import re
from types import MappingProxyType, SimpleNamespace
import weakref

import laya_install_journal as journal
from laya_product_install import _install_lock, _read_private
from persistent_identity import persistent_node_equal

_JOURNAL = 'install-journal.sqlite'
_MAX_IMAGE = 16 * 1024 * 1024
_issued = weakref.WeakKeyDictionary()
_read_only = weakref.WeakSet()
_retained = weakref.WeakSet()
_upgrade_readers = weakref.WeakSet()
_bootstrap_endpoints = weakref.WeakSet()
_bootstrap_installers = weakref.WeakSet()

# Installation identity: every endpoint of one install shares these fields.
INSTALL_IDENTITY = ('scope', 'install_id', 'project', 'project_node', 'state', 'state_node',
                    'attestation_sha256', 'profile', 'settings', 'roots')
# v6 endpoints keep their existing stricter comparison with one another.
_V6_STABLE = INSTALL_IDENTITY + ('incarnation', 'checkout_policy')
SCHEMA3_CAPABILITY = 'schema3-ro-v1'
SCHEMA3_POLICY = 'selected-main-schema3-v1'
# Store identity of a persisted schema3 endpoint. No generation is pinned: each
# query resolves the view head and checks it against head_floor.
_SCHEMA3_STORE = ('store_format', 'capability', 'checkout_policy', 'incarnation', 'db_path', 'db_node',
                  'repository_digest', 'worktree_id', 'worktree_binding', 'settings_policy_digest',
                  'vector_profile', 'vector_recipe', 'head_floor', 'parity', 'creation_epoch')
_SCHEMA3_KEYS = frozenset(INSTALL_IDENTITY + _SCHEMA3_STORE)
_FLOOR_KEYS = frozenset(('view_id', 'epoch', 'ordinal'))
PARITY_KEYS = ('migration_id', 'generation', 'digest')
_RECEIPT_KEYS = frozenset(PARITY_KEYS + ('candidate', 'candidate_incarnation'))


# Endpoint record fields holding ``[st_dev, st_ino]``. APFS renumbers devices
# at boot, so two records sealed in different boots name the same file with
# another device: records compare these fields by inode (paths compare as they
# are). Live fd/path and before/after observations keep the full identity.
_RECORD_NODES = ('project_node', 'state_node', 'db_node', 'catalog_node', 'binding_node')


def records_agree(first, second, keys):
    """``first`` and ``second`` hold the same values at ``keys`` (record nodes by inode)."""
    return all(persistent_node_equal(first[k], second[k]) if k in _RECORD_NODES
               else first[k] == second[k] for k in keys)


def stable_fields(first, second):
    """Fields two endpoints of one install must share."""
    if 'schema3' in (first.get('store_format'), second.get('store_format')):
        return INSTALL_IDENTITY
    return _V6_STABLE


@dataclass(frozen=True, eq=False, init=False)
class VerifiedEndpoint:
    endpoint_id: str
    record: MappingProxyType

    def __init__(self):
        raise ValueError('reader_endpoint_not_issued')


@dataclass(frozen=True, eq=False, init=False)
class InstallerSeal:
    endpoint: VerifiedEndpoint

    def __init__(self):
        raise ValueError('reader_installer_not_issued')


@dataclass(frozen=True, init=False)
class ReaderSelection:
    epoch: int
    preferred_endpoint: str
    previous_endpoint: str | None
    transition_id: str

    def __init__(self):
        raise ValueError('reader_selection_not_issued')


def _need(value, reason):
    if not value:
        raise journal.JournalEvidenceError(reason)


def _raw(obj):
    return journal._canon(obj).encode('utf8')


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _preserve_node_devices(stored, current):
    # Compare live observations, but keep the committed endpoint bytes/ID.
    for key in _RECORD_NODES:
        if key in stored and key in current and persistent_node_equal(stored[key], current[key]):
            current[key] = list(stored[key])
    return current


def _node(path):
    info = path.lstat()
    return [info.st_dev, info.st_ino]


def _file(path, state):
    _need(path.parent == state and path.resolve(strict=True) == path, 'reader_store_path_invalid')
    info = path.lstat()
    _need(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_nlink == 1
          and not info.st_mode & 0o077, 'reader_store_unsafe')
    return [info.st_dev, info.st_ino]


def _state(state):
    from laya_journal_paths import check_journal_namespace
    state = Path(state)
    check_journal_namespace(state)
    _need(state.is_absolute() and state.resolve(strict=True) == state and
          journal._private_parent(state / _JOURNAL).parent == state, 'reader_state_unsafe')
    return state


def _freeze(value):
    if isinstance(value, dict):
        return MappingProxyType({k: _freeze(v) for k, v in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(v) for v in value)
    return value


_READONLY_ROLLBACK = getattr(sqlite3, 'SQLITE_READONLY_ROLLBACK', 776)


@contextmanager
def _readonly(path, state, *, recover=False):
    """One query_only snapshot transaction over a private store.

    ``recover=True`` is for reads under the queue's sync owner only (writer
    plans, the shadow driver's candidate): the connection opens ``mode=rw`` so SQLite rolls back a hot
    journal a killed writer left, which a ``mode=ro`` connection refuses with
    ``SQLITE_READONLY_ROLLBACK``. ``query_only`` stays on, so that rollback is
    the only write; the writer's own rw connection would perform it anyway.
    """
    before = _file(path, state)
    for suffix in ('-wal', '-shm', '-journal'):
        side = Path(str(path) + suffix)
        if os.path.lexists(side):
            _file(side, state)
    try:
        from laya_store_primitives import _query_only_uri
        uri = Path(path).as_uri() + '?mode=rw' if recover else _query_only_uri(path)
        with closing(sqlite3.connect(uri, uri=True,
                                     timeout=2, isolation_level=None)) as db:
            db.execute('PRAGMA query_only=ON')
            db.execute('PRAGMA trusted_schema=OFF')
            db.execute('BEGIN')
            yield db
            _need(before == _file(path, state), 'reader_store_changed')
    except sqlite3.Error as exc:
        # A killed writer's hot journal: a ro open refuses SQLite's rollback
        # and writes nothing. The owner's next pass rolls it back (``recover``).
        if (getattr(exc, 'sqlite_errorcode', None) == _READONLY_ROLLBACK
                and os.path.lexists(str(path) + '-journal')):
            raise journal.JournalEvidenceError('reader_store_recovery_pending') from exc
        raise journal.JournalEvidenceError('reader_store_invalid') from exc


def _selected_proof(state, project, executing_root, *, allow_drift):
    import laya_fresh_wiki_origin as fresh
    from laya_ledger_common import refuse_qmd_origin
    state = _state(state)
    project, executing_root = Path(project), Path(executing_root)
    refuse_qmd_origin(state)
    decision = journal.select_route(str(project), action='recall')
    try:
        proof = journal.verify_selected_route(decision)
    except Exception as exc:
        if not allow_drift or not journal.is_bundle_drift(exc):
            raise
        proof = journal.verify_selected_route_read_only(decision)
        _need(proof.get('readOnlyAllowed') is True and proof.get('productRouteAllowed') is False,
              'reader_installer_unproved')
    _need((proof.get('productRouteAllowed') is True or
           (allow_drift and proof.get('readOnlyAllowed') is True))
          and proof.get('executingPluginRoot') == str(executing_root)
          and proof['projectRoot'] == str(project) and proof['journalPath'] == str(state / _JOURNAL),
          'reader_installer_unproved')
    if allow_drift:
        proof = {**proof, 'readerDecision': decision}
    ledger = fresh._load(state / fresh.LEDGER)
    info = journal.inspect_journal(state / _JOURNAL)
    return state, project, decision, proof, ledger, info


def _install_checks(state, project, decision, proof, ledger, info):
    """Selection, ledger and build identity; no index store is opened."""
    import laya_initial_build as build
    _need(ledger['phase'] == 'active' and info['pending'] is None
          and info['state'] in ('active', 'restored_old')
          and ledger['attestationSha256'] == info['providerAttestationSha256']
          and ledger['providerFileIdentitySha256'] == info['providerFileIdentitySha256']
          and ledger['routeSha256'] == info['selectedRouteSha256']
          and ledger['scope'] == proof['scopeId'] and ledger['candidateIncarnation'] == proof['dbIncarnation'],
          'reader_selection_unproved')
    build_record = build.read(state)
    _need(build_record is not None, 'reader_build_missing')
    identity = build_record['identity']
    for k, expected in (('projectRoot', str(project)), ('stateRoot', str(state)),
                        ('scopeId', proof['scopeId']), ('installId', proof['installId']),
                        ('candidateDb', decision['dbPath']), ('dbIncarnation', proof['dbIncarnation']),
                        ('vectorProfileId', proof['vectorProfile']['profileId']),
                        ('catalogDb', ledger['catalogDb']), ('bindingStore', ledger['bindingStore'])):
        _need(identity[k] == expected, 'reader_build_changed')
    return identity


def _attestation(state, ledger, sealed_attestation=None):
    raw = (_read_private(state / journal.PRODUCTION_ATTESTATION, 1024 * 1024)
           if sealed_attestation is None else sealed_attestation)
    _need(type(raw) is bytes, 'reader_attestation_changed')
    _need(_sha(raw) == ledger['attestationSha256'], 'reader_attestation_changed')
    return raw


def _plain(value):
    return dict(value) if isinstance(value, (dict, MappingProxyType)) else value


def _repository_digest(text):
    return _sha(b'memvane-schema3-repository-v1\0' + text.encode('utf8'))


def _schema3_shape(record):
    """Exact persisted schema3 record shape; no store is opened."""
    try:
        from laya_blob_values import SHA, UUID
        from laya_view_values import GEN_ID
        floor, parity, node = record['head_floor'], record['parity'], record['db_node']
        return (set(record) == _SCHEMA3_KEYS and record['capability'] == SCHEMA3_CAPABILITY
                and record['checkout_policy'] == SCHEMA3_POLICY
                and all(type(record[k]) is str and SHA.fullmatch(record[k]) for k in
                        ('repository_digest', 'worktree_binding', 'settings_policy_digest'))
                and all(type(record[k]) is str and UUID.fullmatch(record[k]) for k in
                        ('incarnation', 'worktree_id'))
                and type(record['db_path']) is str and Path(record['db_path']).is_absolute()
                and Path(record['db_path']).parent == Path(record['state'])
                and isinstance(node, (list, tuple)) and len(node) == 2
                and all(type(n) is int for n in node)
                and ((record['vector_profile'] is None and record['vector_recipe'] is None) or
                     (type(record['vector_profile']) is str and type(record['vector_recipe']) is str))
                and isinstance(floor, (dict, MappingProxyType)) and set(floor) == _FLOOR_KEYS
                and type(floor['view_id']) is str and SHA.fullmatch(floor['view_id'])
                and type(floor['epoch']) is int and floor['epoch'] >= 1
                and type(floor['ordinal']) is int and floor['ordinal'] >= 1
                and isinstance(parity, (dict, MappingProxyType)) and set(parity) == set(PARITY_KEYS)
                and type(parity['migration_id']) is str and journal.ID.fullmatch(parity['migration_id'])
                and type(parity['generation']) is str and GEN_ID.fullmatch(parity['generation'])
                and type(parity['digest']) is str and SHA.fullmatch(parity['digest'])
                and type(record['creation_epoch']) is int and record['creation_epoch'] >= 1)
    except (KeyError, TypeError):
        return False


def _schema3_store(state, project, proof, decision, db_path, worktree_id):
    """Current store identity of one private schema3 store and its main worktree."""
    path = Path(db_path)
    _need(path.is_absolute() and str(path) != decision['dbPath'], 'reader_store_path_invalid')
    node = _file(path, state)
    with _readonly(path, state) as db:
        meta = db.execute('SELECT incarnation,scope_id,repository_json,settings_policy_digest '
                          'FROM store_meta WHERE singleton=1').fetchone()
        tree = db.execute('SELECT project_root,binding_digest FROM worktrees WHERE worktree_id=?',
                          (worktree_id,)).fetchone()
    _need(meta is not None and tree is not None and meta[1] == proof['scopeId']
          and isinstance(meta[2], str) and tree[0] == str(project), 'reader_store_changed')
    return dict(db_path=str(path), db_node=node, incarnation=meta[0],
                repository_digest=_repository_digest(meta[2]), worktree_id=worktree_id,
                worktree_binding=tree[1], settings_policy_digest=meta[3])


def _schema3_evidence(state, project, root, stored, *, allow_drift):
    """Reprove installation identity and the schema3 store; no v6 store is opened.

    CAS-time facts (head_floor, parity, vector profile) are copied from the
    stored record; the floor is checked against the live head at query/CAS.
    """
    state, project, decision, proof, ledger, info = _selected_proof(state, project, root,
                                                                    allow_drift=allow_drift)
    return _schema3_record(state, project, decision, proof, ledger, info, stored)


def _schema3_record(state, project, decision, proof, ledger, info, stored, *, sealed_attestation=None):
    """Schema3 record after the caller proves the selection (also the fenced upgrade proof)."""
    _install_checks(state, project, decision, proof, ledger, info)
    raw = _attestation(state, ledger, sealed_attestation)
    store = _schema3_store(state, project, proof, decision, stored['db_path'], stored['worktree_id'])
    record = dict(install_id=proof['installId'], project=str(project), project_node=_node(project),
                  state=str(state), state_node=_node(state), scope=proof['scopeId'],
                  profile=proof['vectorProfile']['profileId'], attestation_sha256=_sha(raw),
                  settings=_sha(_raw(decision['recallSettings'])), roots=decision['collections'],
                  store_format='schema3', capability=SCHEMA3_CAPABILITY, checkout_policy=SCHEMA3_POLICY,
                  vector_profile=stored['vector_profile'], vector_recipe=stored['vector_recipe'],
                  head_floor=_plain(stored['head_floor']), parity=_plain(stored['parity']), **store)
    return _preserve_node_devices(stored, record), raw, proof


@contextmanager
def _schema3_pin(record, *, deadline, linked=None, pin_view=True):
    """Pin the current main-view head of a schema3 endpoint and check its floor.

    With ``pin_view=False`` it yields the head record (``view_id``, ``gen_id``,
    ``epoch``) in place of a pin, without verifying the generation again.

    Floor limits: head_floor is fixed at CAS time; ViewStore._head refuses a
    head behind the view's highest published ordinal. A same-UID forger who
    removes the head trigger and also unpublishes every later generation above
    the floor is not detected (the accepted same-UID consistent forgery limit).

    ``linked`` (a ``worktree`` route decision) pins that linked worktree's own
    view in the same store instead: its registered row is found by project
    root, never the main view. No row or no published head is
    ``view_preparing``. Any pinned view may be sealed without vectors
    (lexical-first); a vector profile other than the endpoint's is refused.
    """
    from laya_view_store import ViewStore
    from laya_blob_values import fail
    state, path = Path(record['state']), Path(record['db_path'])
    target, project = record['worktree_id'], record['project']
    with _readonly(path, state) as db:
        row = db.execute('SELECT repository_json FROM store_meta WHERE singleton=1').fetchone()
        if linked is not None:
            project = linked.get('worktreeProjectRoot')
            _need(isinstance(project, str) and project != record['project'], 'reader_checkout_changed')
            trees = db.execute('SELECT worktree_id FROM worktrees WHERE project_root=?',
                               (project,)).fetchall()
    _need(row is not None and isinstance(row[0], str)
          and _repository_digest(row[0]) == record['repository_digest'], 'reader_store_changed')
    if linked is not None:
        _need(len(trees) <= 1 and all(tree[0] != record['worktree_id'] for tree in trees),
              'reader_store_changed')
        if not trees:
            fail('view_preparing')
        target = trees[0][0]
    store = ViewStore.open_reader(path, repository=json.loads(row[0]), scope_id=record['scope'],
                                 deadline=deadline)
    try:
        _need(store._incarnation == record['incarnation']
              and store._policy == record['settings_policy_digest']
              and persistent_node_equal(record['db_node'], _file(path, state)), 'reader_store_changed')
        handle = store.capture_view(target, deadline=deadline)
        _need(store._handles[handle.token]['capture']['projectRoot'] == project,
              'reader_checkout_changed')
        if not pin_view:
            # Head identity only (seal join, highest published ordinal, floor);
            # the generation itself is verified by the pin that served it.
            head = store.head(handle)
            if head['gen_id'] is None:
                fail('view_preparing')
            _need(head['incarnation'] == record['incarnation'], 'reader_store_changed')
            floor = record.get('head_floor')
            if floor is not None and head['view_id'] == floor['view_id']:
                _need(head['epoch'] >= floor['epoch'], 'reader_head_regressed')
            yield store, handle, head
            return
        pin = store.pin_view(handle, deadline=deadline)
        try:
            _need(pin.incarnation == record['incarnation'], 'reader_store_changed')
            floor = record.get('head_floor')
            if floor is not None and pin.view_id == floor['view_id']:
                _need(pin.epoch >= floor['epoch'], 'reader_head_regressed')
            vector = store._pinned(pin)['vector']
            # A view head may be lexical (vectors pending after a lexical-first
            # publish); a vector head must carry the endpoint's spec. A record
            # sealed without one reads any head lexically until the owner
            # re-seals it with the head's spec (resettle_selection).
            if ('vector_profile' in record and vector is not None
                    and record['vector_profile'] is not None):
                _need(vector['profile_id'] == record['vector_profile'] and
                      vector['recipe_id'] == record['vector_recipe'], 'reader_endpoint_changed')
            yield store, handle, pin
        finally:
            store.release(pin)
    finally:
        store.close()


def _successor_chain(db, endpoint_id, stored, target_sha, target_identity, *, max_epoch, fenced=False):
    """Validate immutable, activation-committed provider successors for one endpoint."""
    _need(db.execute("SELECT 1 FROM sqlite_master WHERE name='upgrade_read_successors'").fetchone(),
          'reader_successor_missing')
    current_sha, identity, last_epoch = stored['attestation_sha256'], None, 0
    previous_item = None

    def restored_predecessor(item, before_epoch):
        # Restore seals are committed before a supported rollback changes the
        # journal. They bind the exact activation, predecessor and epoch.
        for seal_endpoint, packed in db.execute(
                'SELECT endpoint_id,record FROM upgrade_read_seals WHERE endpoint_id=?', (endpoint_id,)):
            seal = json.loads(packed, object_pairs_hook=journal._pairs,
                              parse_constant=journal._nonfinite)
            if type(seal) is not dict or seal.get('schema') != 'memvane-upgrade-restore-v1':
                continue
            source = seal.get('journalBefore')
            if (seal_endpoint == endpoint_id and _raw(seal).decode('utf8') == packed
                    and seal.get('endpointId') == endpoint_id
                    and seal.get('intentSha256') == item['intentSha256']
                    and type(source) is dict and source.get('state') == 'active'
                    and source.get('newWriteSeen') is False
                    and type(source.get('epoch')) is int
                    and item['epoch'] <= source['epoch'] < before_epoch
                    and source.get('selected') == item['toIdentity']
                    and source.get('previous') == item['fromIdentity']
                    and source.get('providerAttestationSha256') == item['toAttestation']
                    and source.get('previousProviderAttestationSha256') == item['fromAttestation']):
                return True
        return False

    rows = db.execute('SELECT epoch,intent_sha256,record FROM upgrade_read_successors '
                      'WHERE endpoint_id=? ORDER BY epoch', (endpoint_id,)).fetchall()
    if fenced:
        rows = [row for row in rows if row[0] <= max_epoch]
    for epoch, intent_sha, packed in rows:
        item = json.loads(packed, object_pairs_hook=journal._pairs, parse_constant=journal._nonfinite)
        if (type(item) is dict and previous_item is not None
                and (item.get('fromAttestation') != current_sha or item.get('fromIdentity') != identity)):
            _need(item.get('fromAttestation') == previous_item['fromAttestation']
                  and item.get('fromIdentity') == previous_item['fromIdentity']
                  and restored_predecessor(previous_item, epoch), 'reader_successor_invalid')
            current_sha, identity = item['fromAttestation'], item['fromIdentity']
        _need(type(item) is dict and _raw(item).decode('utf8') == packed and
              set(item) == {'schema','intentSha256','endpointId','epoch','fromAttestation',
                            'toAttestation','fromIdentity','toIdentity'} and
              item['schema'] == 'memvane-reader-successor-v1' and item['intentSha256'] == intent_sha and
              item['endpointId'] == endpoint_id and item['epoch'] == epoch and
              type(item['fromIdentity']) is dict and type(item['toIdentity']) is dict and
              type(epoch) is int and last_epoch < epoch <= max_epoch and
              item['fromAttestation'] == current_sha and
              (identity is None or item['fromIdentity'] == identity), 'reader_successor_invalid')
        seal_row = db.execute('SELECT endpoint_id,record FROM upgrade_read_seals WHERE intent_sha256=?',
                              (intent_sha,)).fetchone()
        _need(seal_row is not None and seal_row[0] == endpoint_id, 'reader_successor_invalid')
        from laya_active_upgrade import _reader_seal
        seal = _reader_seal(db, intent_sha, endpoint_id)
        source = (seal.get('recovery',{}).get('journalBefore')
                  if type(seal) is dict and seal.get('schema') == 'memvane-upgrade-reader-v2'
                  else seal.get('journalBefore') if type(seal) is dict else None)
        _need(type(seal) is dict and type(source) is dict and
              type(source.get('epoch')) is int and
              _raw(seal).decode('utf8') == seal_row[1] and
              seal.get('intentSha256') == intent_sha and seal.get('endpointId') == endpoint_id and
              source.get('selected') == item['fromIdentity'] and
              source.get('providerAttestationSha256') == current_sha and
              source.get('epoch') + 2 == epoch and
              item['toIdentity'].get('installId') == stored['install_id'] and
              item['toIdentity'].get('vectorProfile',{}).get('profileId') == stored['profile'] and
              type(item['toAttestation']) is str and journal.SHA.fullmatch(item['toAttestation']),
              'reader_successor_invalid')
        if seal.get('schema') in ('memvane-upgrade-reader-v2','memvane-upgrade-reader-retarget-v1'):
            _need(seal['targetIdentity'] == item['toIdentity'] and
                  seal['targetAttestationSha256'] == item['toAttestation'] and
                  (seal['schema'] != 'memvane-upgrade-reader-v2' or
                   seal['recovery'].get('mode') == 'switch'), 'reader_successor_invalid')
        current_sha, identity, last_epoch = item['toAttestation'], item['toIdentity'], epoch
        previous_item = item
    if rows and (current_sha != target_sha or identity != target_identity):
        # A supported rollback restores the immediately preceding bundle while
        # the immutable receipt for the reverted activation remains present.
        # Validate the complete forward chain above before accepting that exact
        # predecessor and only when the live journal proves the restoration.
        row = journal._row(db)
        selected_restored = (row['state'] in ('restored_old', 'pending')
                             and row['selected'] == row['previous'] == target_identity
                             and row['provider_attestation_sha'] == target_sha)
        activating = (row['state'] == 'active' and row['epoch'] == max_epoch + 1
                      and row['previous'] == target_identity
                      and row['previous_provider_attestation_sha'] == target_sha)
        _need((selected_restored or activating or fenced) and row['epoch'] > last_epoch
              and item['fromAttestation'] == target_sha
              and item['fromIdentity'] == target_identity
              and restored_predecessor(item, max_epoch),
              'reader_successor_unproved')
        return
    _need(rows and current_sha == target_sha and identity == target_identity, 'reader_successor_unproved')


def _attestation_step(db, endpoint_id, stored, raw):
    """The live journal selects ``raw`` at the end of ``endpoint_id``'s sealed successor chain.

    ``stored`` (the endpoint's record) carries an earlier bundle's attestation:
    a bundle upgrade left it there and reads follow the chain.
    """
    row = journal._row(db)
    _need(row['state'] in ('active','restored_old') and row['pending'] is None and
          row['provider_attestation_sha'] == _sha(raw), 'reader_successor_unproved')
    _successor_chain(db, endpoint_id, stored, _sha(raw), row['selected'], max_epoch=row['epoch'])


def _match_persisted(state, endpoint_id, stored, attestation, current, raw, proof):
    _preserve_node_devices(stored, current)
    if 'legacy_sidecar_baseline' in stored:
        current['legacy_sidecar_baseline'] = stored['legacy_sidecar_baseline']
    _need(proof['selectionEpoch'] >= stored['creation_epoch'], 'reader_endpoint_changed')
    current['creation_epoch'] = stored['creation_epoch']
    if raw != attestation:
        current['attestation_sha256'] = stored['attestation_sha256']
        with _readonly(Path(state) / _JOURNAL, Path(state)) as db:
            _attestation_step(db, endpoint_id, stored, raw)
    packed = _raw(current)
    _need(packed == _raw(stored) and _sha(attestation) == stored['attestation_sha256'] and
          endpoint_id == _sha(b'memvane-reader-endpoint-v1\0' + packed), 'reader_endpoint_changed')
    return packed


def _persisted_endpoint(state, project, root, endpoint_id, stored, attestation):
    """Reissue a persisted reader after current full proof, without CAS authority."""
    from laya_reader_lease import _require_reader_scope, _bind_reader_scope
    scope = _require_reader_scope(stored.get('install_id'))
    _need(isinstance(stored, dict) and type(stored.get('creation_epoch')) is int
          and stored['creation_epoch'] >= 1 and stored.get('store_format') == 'schema3',
          'reader_endpoint_invalid')
    options = {}
    current, raw, proof = _schema3_evidence(state, project, root, stored, allow_drift=True)
    packed = _match_persisted(state, endpoint_id, stored, attestation, current, raw, proof)
    token = object.__new__(VerifiedEndpoint)
    object.__setattr__(token, 'endpoint_id', endpoint_id)
    object.__setattr__(token, 'record', _freeze(current))
    _issued[token] = (Path(state), Path(project), Path(root), options, packed, attestation)
    _read_only.add(token)
    _bind_reader_scope(token, scope)
    return token


def _revoke_reader(endpoint):
    _issued.pop(endpoint, None)
    _read_only.discard(endpoint)


def _recheck(endpoint):
    _need(type(endpoint) is VerifiedEndpoint and endpoint in _issued, 'reader_endpoint_not_issued')
    from laya_reader_lease import _check_reader_scope
    _check_reader_scope(endpoint)
    state, project, root, options, packed, raw = _issued[endpoint]
    saved = json.loads(packed)
    record, attestation, proof = _schema3_evidence(state, project, root, saved,
                                                   allow_drift=endpoint in _read_only)
    _need(_match_persisted(state, endpoint.endpoint_id, saved, raw, record, attestation, proof) == packed,
          'reader_endpoint_changed')
    return saved, raw, proof


def _sync(path, directory=False):
    flags = os.O_RDONLY | os.O_NOFOLLOW | (os.O_DIRECTORY if directory else 0)
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _lock_dir(install_id, *, create):
    from laya_journal_paths import journal_directory
    _need(type(install_id) is str and journal.ID.fullmatch(install_id), 'reader_install_invalid')
    return journal_directory('reader-locks', install_id, create=create)


@contextmanager
def _transition(install_id, *, create=False):
    path = _lock_dir(install_id, create=create) / 'journal-transition.lock'
    fd = os.open(path, os.O_RDWR | os.O_NONBLOCK | os.O_NOFOLLOW | (os.O_CREAT if create else 0), 0o600)
    try:
        st = os.fstat(fd)
        _need(stat.S_ISREG(st.st_mode) and st.st_uid == os.getuid() and st.st_nlink == 1
              and stat.S_IMODE(st.st_mode) == 0o600, 'reader_lock_unsafe')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError('reader_transition_busy') from exc
        _need(os.path.samestat(st, path.lstat()), 'reader_lock_changed')
        if create:
            _sync(path.parent, True)
        yield fd
    finally:
        os.close(fd)


def _stored_endpoint(db, endpoint_id):
    _need(type(endpoint_id) is str and journal.SHA.fullmatch(endpoint_id), 'reader_endpoint_invalid')
    row = db.execute('SELECT record,attestation FROM read_endpoints WHERE endpoint_id=?',
                     (endpoint_id,)).fetchone()
    _need(row and isinstance(row[0], str) and isinstance(row[1], bytes) and len(row[0]) <= 65536
          and len(row[1]) <= 1024 * 1024, 'reader_endpoint_invalid')
    packed = row[0].encode('utf8')
    record = json.loads(packed)
    _need(isinstance(record, dict) and _raw(record) == packed and
          _sha(b'memvane-reader-endpoint-v1\0' + packed) == endpoint_id
          and record.get('store_format') == 'schema3' and _schema3_shape(record)
          and record.get('attestation_sha256') == _sha(row[1]), 'reader_endpoint_invalid')
    return record


def _selection(db):
    _need(journal._schema_version(db) == 5, 'reader_schema_unknown')
    rows = db.execute('SELECT epoch,preferred_endpoint,previous_endpoint,transition_id FROM reader_selection').fetchall()
    _need(len(rows) == 1, 'reader_selection_invalid')
    selected = object.__new__(ReaderSelection)
    for name, value in zip(('epoch', 'preferred_endpoint', 'previous_endpoint', 'transition_id'), rows[0]):
        object.__setattr__(selected, name, value)
    last = db.execute('SELECT epoch,preferred_endpoint,previous_endpoint,transition_id '
                      'FROM reader_transitions ORDER BY epoch DESC LIMIT 1').fetchone()
    _need(last == rows[0] and type(selected.epoch) is int and selected.epoch >= 1 and
          (selected.epoch == 1) == (selected.previous_endpoint is None)
          and type(selected.transition_id) is str and journal.ID.fullmatch(selected.transition_id),
          'reader_selection_invalid')
    record = _stored_endpoint(db, selected.preferred_endpoint)
    if selected.previous_endpoint is not None:
        previous = _stored_endpoint(db, selected.previous_endpoint)
        fields = (INSTALL_IDENTITY if 'schema3' in (record['store_format'], previous['store_format'])
                  else ('install_id',))
        _need(records_agree(record, previous, fields) or resettled_pair(previous, record),
              'reader_install_changed')
    return selected, record


def _journal(path):
    path = Path(path)
    _need(path.name == _JOURNAL, 'invalid_journal_path')
    _state(path.parent)
    _file(path, path.parent)
    return path


def read_selection(journal: Path) -> ReaderSelection:
    from laya_install_journal import JournalEvidenceError
    for attempt in range(3):
        path = _journal(journal)
        try:
            with _readonly(path, path.parent) as db:
                return _selection(db)[0]
        except JournalEvidenceError as exc:
            if str(exc) != 'reader_store_changed' or attempt == 2:
                raise


def _insert_endpoint(db, endpoint, record, raw):
    row = (_raw(record).decode('utf8'), raw)
    existing = db.execute('SELECT record,attestation FROM read_endpoints WHERE endpoint_id=?',
                          (endpoint.endpoint_id,)).fetchone()
    if existing is None:
        db.execute('INSERT INTO read_endpoints VALUES(?,?,?)', (endpoint.endpoint_id, *row))
    else:
        _need(existing == row, 'reader_endpoint_conflict')


_RESETTLED = frozenset(('settings', 'vector_profile', 'vector_recipe'))
# Carried only alongside a ``_RESETTLED`` change, never alone.
_RESETTLED_WITH = frozenset(('attestation_sha256',))


def resettled_pair(old, new):
    """``new`` is the schema3 ``old`` re-sealed by its owner.

    Either the project's recall settings changed (``settings``), or a record
    sealed without a vector spec adopts the spec of a vector head the owner
    published (``vector_profile``/``vector_recipe``: both None before, both
    set after; never changed or cleared). A record a bundle upgrade left on
    its sealed predecessor attestation (read through the successor chain)
    is re-sealed with the current one (``attestation_sha256``), but only
    together with such a change; ``resettle_selection`` proves that step on
    the chain. Store, incarnation, worktree binding, policy, roots and every
    other field are identical.
    """
    try:
        if not (old.get('store_format') == new.get('store_format') == 'schema3'
                and set(old) == set(new) == _SCHEMA3_KEYS):
            return False
        changed = {k for k in _SCHEMA3_KEYS if not records_agree(old, new, (k,))} - _RESETTLED_WITH
        vector = changed & {'vector_profile', 'vector_recipe'}
        return (bool(changed) and changed <= _RESETTLED
                and (not vector or (old['vector_profile'] is None and old['vector_recipe'] is None
                                    and type(new['vector_profile']) is str
                                    and type(new['vector_recipe']) is str)))
    except (AttributeError, KeyError, TypeError):
        return False


def _adopted_vector_spec(record, proof, *, deadline):
    """The current main head's vector spec when it is the install provider's, else None."""
    from laya_view_vectors import provider_spec
    expected = provider_spec(proof.get('providerManifest'), proof.get('vectorProfile'))
    if expected is None:
        return None
    from laya_blob_values import BlobStoreError
    try:
        with _schema3_pin(record, deadline=deadline) as (store, _handle, pin):
            vector = store._pinned(pin)['vector']
    except BlobStoreError:
        # Best effort: a cold view, a moving checkout or a busy store leaves
        # the record as it is; the owner's next pass adopts the spec.
        return None
    if vector is None or {k: vector[k] for k in ('recipe_id', 'profile_id')} != expected:
        return None
    return expected


def _resettle_inputs(state, project, executing_root):
    """The selected schema3 record and the same record proved again now, or None for v6.

    Read-only; ``resettle_selection`` and ``settings_resettle_pending`` share it.
    """
    path = _journal(Path(state) / _JOURNAL)
    with _readonly(path, path.parent) as db:
        selected, stored = _selection(db)
        (attestation,) = db.execute('SELECT attestation FROM read_endpoints WHERE endpoint_id=?',
                                    (selected.preferred_endpoint,)).fetchone()
    if stored.get('store_format') != 'schema3':
        return None
    current, raw, proof = _schema3_evidence(path.parent, project, executing_root, stored, allow_drift=False)
    _need(proof['selectionEpoch'] >= stored['creation_epoch'], 'reader_endpoint_changed')
    current['creation_epoch'] = stored['creation_epoch']
    return path, selected, stored, attestation, current, raw, proof


def settings_resettle_pending(state, project, executing_root) -> bool:
    """The selected schema3 record differs from the install only in its recall settings.

    A record a bundle upgrade left on its predecessor attestation may differ
    in that attestation too, when the successor chain proves the step. That
    is the difference the sync owner re-seals (``resettle_selection``);
    until it does, the persisted reader refuses with ``reader_endpoint_changed``.
    Read-only and authority-free: a recall uses it only to answer without
    context instead of with an error. Any other difference, or a failed
    proof, is False.
    """
    try:
        found = _resettle_inputs(state, project, executing_root)
        if found is None:
            return False
        path, selected, stored, attestation, current, raw, _proof = found
        if not (stored['settings'] != current['settings'] and resettled_pair(stored, current)
                and {k for k in stored if stored[k] != current[k]} - _RESETTLED_WITH == {'settings'}):
            return False
        if raw != attestation:
            with _readonly(path, path.parent) as db:
                _attestation_step(db, selected.preferred_endpoint, stored, raw)
        return True
    except Exception:
        return False


def resettle_selection(state, project, executing_root):
    """Owner re-seal of the selected schema3 endpoint (recall settings or vector spec).

    The selected record is proved again from the current install (the same
    full proof a persisted reader needs, no bundle drift). A record sealed
    without a vector spec adopts the main head's spec once the owner has
    published a vector generation of the install provider's spec. When the
    result differs from the stored record only as ``resettled_pair`` allows,
    one journal image transaction selects the re-sealed record. A record a
    bundle upgrade left on its predecessor attestation is re-sealed with the
    current one only when that transaction proves the step on the
    endpoint's sealed successor chain (``_attestation_step``, as a persisted
    read does); an unproved step raises and writes nothing. Returns
    ``'resealed'`` or ``'unchanged'``; any other difference is left for the reader to refuse (``reader_endpoint_changed``).
    Recall itself never re-seals.
    """
    import time
    found = _resettle_inputs(state, project, executing_root)
    if found is None:
        return 'unchanged'
    path, selected, stored, attestation, current, raw, proof = found
    if stored['vector_profile'] is None:
        adopted = _adopted_vector_spec(current, proof, deadline=time.monotonic() + 10)
        if adopted is not None:
            current['vector_profile'], current['vector_recipe'] = adopted['profile_id'], adopted['recipe_id']
    if _raw(current) == _raw(stored) or not resettled_pair(stored, current):
        return 'unchanged'
    packed = _raw(current)
    endpoint_id = _sha(b'memvane-reader-endpoint-v1\0' + packed)

    def reseal(db):
        now, record = _selection(db)
        _need(now.preferred_endpoint == selected.preferred_endpoint and now.epoch == selected.epoch
              and record == stored, 'stale_reader_cas')
        if raw != attestation:
            _attestation_step(db, now.preferred_endpoint, record, raw)
        transition = _sha(b'memvane-reader-resettle-v1\0' + endpoint_id.encode('ascii')
                          + str(now.epoch).encode('ascii'))[:32]
        _need(not db.execute('SELECT 1 FROM reader_transitions WHERE transition_id=?',
                             (transition,)).fetchone(), 'reader_transition_reused')
        if not db.execute('SELECT 1 FROM read_endpoints WHERE endpoint_id=?', (endpoint_id,)).fetchone():
            db.execute('INSERT INTO read_endpoints VALUES(?,?,?)', (endpoint_id, packed.decode('utf8'), raw))
        epoch = now.epoch + 1
        db.execute('INSERT INTO reader_transitions VALUES(?,?,?,?)',
                   (transition, epoch, endpoint_id, now.preferred_endpoint))
        db.execute('UPDATE reader_selection SET epoch=?,preferred_endpoint=?,previous_endpoint=?,'
                   'transition_id=? WHERE id=1', (epoch, endpoint_id, now.preferred_endpoint, transition))
        return True
    mutate_image(path, reseal)
    return 'resealed'


def _discard_staging(lockfd, path):
    """Only unlink a previous writer's private staging inode under this same lock."""
    note = os.pread(lockfd, 4097, 0)
    if not note:
        return
    _need(len(note) <= 4096, 'reader_image_unsafe')
    saved = json.loads(note)
    _need(isinstance(saved, list) and len(saved) == 3 and saved[0] == str(path),
          'reader_image_unsafe')
    if os.path.lexists(path):
        _need(_file(path, path.parent) == saved[1:], 'reader_image_changed')
        for suffix in ('-journal', '-wal', '-shm'):
            side = Path(str(path) + suffix)
            if os.path.lexists(side):
                _need(suffix == '-journal', 'reader_image_unsafe')
                _file(side, path.parent)
                side.unlink()
        path.unlink()
    _sync(path.parent, True)
    os.ftruncate(lockfd, 0)
    os.fsync(lockfd)


def mutate_image(journal_path, operation, *, before_replace=None):
    """Mutate an owned sibling; the named v5 journal is never opened for write."""
    _need(before_replace is None or callable(before_replace), 'reader_publication_check_invalid')
    path = _journal(journal_path)
    with _readonly(path, path.parent) as before:
        _, record = _selection(before)
    with _transition(record['install_id']) as lockfd:
        _need(not os.path.lexists(path.parent / journal._INITIAL_INTENT), 'initial_conversion_unsettled')
        image = path.with_name('.reader-next.sqlite')
        _discard_staging(lockfd, image)
        for suffix in ('', '-journal', '-wal', '-shm'):
            _need(not os.path.lexists(str(image) + suffix), 'reader_image_unsafe')
        current_node = _file(path, path.parent)
        _need(path.stat().st_size <= _MAX_IMAGE, 'reader_image_too_large')
        fd = os.open(image, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            note = _raw([str(image), info.st_dev, info.st_ino])
            _need(os.pwrite(lockfd, note, 0) == len(note), 'reader_image_unowned')
            os.fsync(lockfd)
        finally:
            os.close(fd)
        try:
            with closing(sqlite3.connect(image.as_uri() + '?mode=rw', uri=True,
                                         isolation_level=None, timeout=2)) as db:
                with _readonly(path, path.parent) as source:
                    _, current = _selection(source)
                    _need(current['install_id'] == record['install_id'], 'reader_install_changed')
                    source.backup(db)
                journal._step('v5-image-copied')
                db.execute('PRAGMA synchronous=FULL')
                db.execute('BEGIN IMMEDIATE')
                try:
                    changed = operation(db)
                    if not changed:
                        db.rollback()
                        return
                    _selection(db)
                    db.commit()
                except BaseException:
                    db.rollback()
                    raise
                journal._step('v5-image-committed')
            _need(image.stat().st_size <= _MAX_IMAGE and
                  _file(image, path.parent) == [info.st_dev, info.st_ino], 'reader_image_changed')
            _sync(image)
            journal._step('v5-image-fsynced')
            if before_replace is not None:
                before_replace()
            _need(_file(path, path.parent) == current_node, 'reader_journal_changed')
            os.replace(image, path)
            journal._step('v5-image-replaced')
            _sync(path.parent, True)
            journal._step('v5-directory-fsynced')
        finally:
            _discard_staging(lockfd, image)

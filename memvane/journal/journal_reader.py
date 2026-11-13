"""Installer-owned reader selection in a v5 journal. No index writer authority."""
from __future__ import annotations

from contextlib import closing, contextmanager, ExitStack
from contextvars import ContextVar
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import re
from types import MappingProxyType, SimpleNamespace
import weakref

from memvane.journal import install_journal as journal
from memvane.kernel.persistent_identity import persistent_node_equal

_JOURNAL = 'journal.sqlite'
_issued = weakref.WeakKeyDictionary()
_read_only = weakref.WeakSet()
_v1_readers = weakref.WeakSet()
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
                  'vector_profile', 'vector_recipe', 'head_floor', 'creation_epoch')
_SCHEMA3_KEYS = frozenset(INSTALL_IDENTITY + _SCHEMA3_STORE)
_FLOOR_KEYS = frozenset(('view_id', 'epoch', 'ordinal'))


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
    from memvane.journal.journal_paths import check_journal_namespace
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
        from memvane.kernel.errors import _query_only_uri
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


def _plain(value):
    return dict(value) if isinstance(value, (dict, MappingProxyType)) else value


def _repository_digest(text):
    return _sha(b'memvane-schema3-repository-v1\0' + text.encode('utf8'))


def _schema3_shape(record):
    """Exact persisted schema3 record shape; no store is opened."""
    try:
        from memvane.store.types import SHA, UUID
        floor, node = record['head_floor'], record['db_node']
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
                and (floor is None or (isinstance(floor, (dict, MappingProxyType))
                     and set(floor) == _FLOOR_KEYS
                     and type(floor['view_id']) is str and SHA.fullmatch(floor['view_id'])
                     and type(floor['epoch']) is int and floor['epoch'] >= 1
                     and type(floor['ordinal']) is int and floor['ordinal'] >= 1))
                and type(record['creation_epoch']) is int and record['creation_epoch'] >= 1)
    except (KeyError, TypeError):
        return False


def _schema3_store(state, project, proof, decision, db_path, worktree_id):
    """Current store identity of one private schema3 store and its main worktree."""
    path = Path(db_path)
    _need(path.is_absolute() and str(path) == decision.get('storePath'),
          'reader_store_path_invalid')
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
    from memvane.store.api import view_store_class
    ViewStore = view_store_class()
    from memvane.store.api import fail
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


def _revoke_reader(endpoint):
    _issued.pop(endpoint, None)
    _read_only.discard(endpoint)
    _v1_readers.discard(endpoint)


def _v1_reader_evidence(state, project, saved, *, allow_settings_change=False):
    """Reprove a selected v1 reader without opening a writable component."""
    from memvane.journal.ledger_io import _private_file
    decision = journal.select_route(str(project), action='recall')
    proof = journal.verify_selected_route(decision)
    row = journal.inspect_journal(Path(state) / _JOURNAL)
    raw = _private_file(Path(state) / journal.PRODUCTION_ATTESTATION, 1024 * 1024)
    _need(row['state'] == 'selected' and row['reader'] == saved
          and _sha(raw) == saved['attestation_sha256']
          and proof['selectionEpoch'] >= saved['creation_epoch']
          and saved['project'] == str(project) and saved['state'] == str(state)
          and saved['install_id'] == proof['installId'] and saved['scope'] == proof['scopeId']
          and saved['profile'] == proof['vectorProfile']['profileId']
          and (allow_settings_change or saved['settings'] == _sha(_raw(decision['recallSettings'])))
          and saved['roots'] == decision['collections']
          and persistent_node_equal(saved['project_node'], _node(project))
          and persistent_node_equal(saved['state_node'], _node(state)),
          'reader_endpoint_changed')
    store = _schema3_store(state, project, proof, decision, saved['db_path'], saved['worktree_id'])
    _need(records_agree(saved, store, tuple(store)), 'reader_store_changed')
    _need(journal.inspect_journal(Path(state) / _JOURNAL) == row,
          'reader_selection_changed')
    return raw, {**proof, 'readerDecision': decision}


def _issue_v1_reader(state, project, root, saved):
    from memvane.journal.reader_lease import _require_reader_scope, _bind_reader_scope
    scope = _require_reader_scope(saved.get('install_id'))
    _need(_schema3_shape(saved), 'reader_endpoint_invalid')
    raw, _proof = _v1_reader_evidence(state, project, saved)
    packed = _raw(saved)
    endpoint_id = _sha(b'memvane-reader-endpoint-v1\0' + packed)
    token = object.__new__(VerifiedEndpoint)
    object.__setattr__(token, 'endpoint_id', endpoint_id)
    object.__setattr__(token, 'record', _freeze(saved))
    _issued[token] = (Path(state), Path(project), Path(root), {}, packed, raw)
    _read_only.add(token)
    _v1_readers.add(token)
    _bind_reader_scope(token, scope)
    return token


def _recheck(endpoint):
    _need(type(endpoint) is VerifiedEndpoint and endpoint in _issued, 'reader_endpoint_not_issued')
    from memvane.journal.reader_lease import _check_reader_scope
    _check_reader_scope(endpoint)
    state, project, root, options, packed, raw = _issued[endpoint]
    saved = json.loads(packed)
    _need(endpoint in _v1_readers, 'reader_endpoint_not_issued')
    current_raw, proof = _v1_reader_evidence(state, project, saved)
    _need(current_raw == raw and endpoint.endpoint_id ==
          _sha(b'memvane-reader-endpoint-v1\0' + packed), 'reader_endpoint_changed')
    return saved, raw, proof


def _sync(path, directory=False):
    flags = os.O_RDONLY | os.O_NOFOLLOW | (os.O_DIRECTORY if directory else 0)
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _lock_dir(install_id, *, create):
    from memvane.journal.journal_paths import journal_directory
    _need(type(install_id) is str and journal.ID.fullmatch(install_id), 'reader_install_invalid')
    return journal_directory('reader-locks', install_id, create=create)


def _journal(path):
    path = Path(path)
    _need(path.name == _JOURNAL, 'invalid_journal_path')
    _state(path.parent)
    _file(path, path.parent)
    return path


def selected_reader(journal_path):
    """``(endpoint_id, epoch, record)`` of a v1 journal's selected reader; grants nothing."""
    path = _journal(journal_path)
    row = journal.inspect_journal(path)
    record = row.get('reader')
    _need(row['state'] == 'selected' and isinstance(record, dict), 'reader_selection_invalid')
    return _sha(b'memvane-reader-endpoint-v1\0' + _raw(record)), row['epoch'], record


def _insert_endpoint(db, endpoint, record, raw):
    row = (_raw(record).decode('utf8'), raw)
    existing = db.execute('SELECT record,attestation FROM read_endpoints WHERE endpoint_id=?',
                          (endpoint.endpoint_id,)).fetchone()
    if existing is None:
        db.execute('INSERT INTO read_endpoints VALUES(?,?,?)', (endpoint.endpoint_id, *row))
    else:
        _need(existing == row, 'reader_endpoint_conflict')


_RESETTLED = frozenset(('settings', 'vector_profile', 'vector_recipe'))


def resettled_pair(old, new):
    """``new`` is the schema3 ``old`` re-sealed by its owner.

    Recall settings or the vector spec may change. The install profile and
    attestation stay fixed. The owner proves a new
    vector spec against the published main head before selecting it. Store,
    incarnation, worktree binding, policy, roots and other fields stay fixed.
    """
    try:
        if not (old.get('store_format') == new.get('store_format') == 'schema3'
                and set(old) == set(new) == _SCHEMA3_KEYS):
            return False
        changed = {k for k in _SCHEMA3_KEYS if not records_agree(old, new, (k,))}
        vector = changed & {'vector_profile', 'vector_recipe'}
        return (bool(changed) and changed <= _RESETTLED
                and (not vector or (type(new['vector_profile']) is str
                                    and type(new['vector_recipe']) is str)))
    except (AttributeError, KeyError, TypeError):
        return False


def _adopted_vector_spec(record, proof, *, deadline):
    """The current main head's vector spec when it is the install provider's, else None."""
    from memvane.store.api import provider_spec
    expected = provider_spec(proof.get('providerManifest'), proof.get('vectorProfile'))
    if expected is None:
        return None
    from memvane.store.types import BlobStoreError
    try:
        unbound = dict(record, vector_profile=None, vector_recipe=None)
        with _schema3_pin(unbound, deadline=deadline) as (store, _handle, pin):
            vector = store._pinned(pin)['vector']
    except BlobStoreError:
        # Best effort: a cold view, a moving checkout or a busy store leaves
        # the record as it is; the owner's next pass adopts the spec.
        return None
    if vector is None or {k: vector[k] for k in ('recipe_id', 'profile_id')} != expected:
        return None
    return expected


def resettle_selection(decision, owner):
    """Re-seal the selected reader under the existing sync owner and lifetime."""
    from contextlib import closing
    from memvane.runtime.api import validate_sync_owner
    from memvane.journal.reader_windows import _Lifetime
    from memvane.store.api import provider_spec
    import time
    project = Path(decision['projectRoot'])
    state = Path(decision['journalPath']).parent
    validate_sync_owner(owner, Path(decision['storePath']))
    lifetime = _Lifetime(decision['installId'])
    try:
        proof = journal.verify_selected_route_for_write(decision)
        row = journal.inspect_journal(state / _JOURNAL)
        saved = row['reader']
        _need(row['state'] == 'selected' and isinstance(saved, dict)
              and row['epoch'] == proof['selectionEpoch'], 'reader_selection_changed')
        raw, current_proof = _v1_reader_evidence(state, project, saved, allow_settings_change=True)
        _need(current_proof['selectionEpoch'] == row['epoch'], 'reader_selection_changed')
        expected = provider_spec(proof['providerManifest'], proof['vectorProfile'])
        replacement = dict(saved, settings=_sha(_raw(decision['recallSettings'])))
        unbound = dict(saved, vector_profile=None, vector_recipe=None)
        with _schema3_pin(unbound, deadline=time.monotonic() + 10) as (store, _handle, pin):
            vector = store._pinned(pin)['vector']
            if vector is not None and expected is not None and all(
                    vector[key] == expected[key] for key in ('profile_id', 'recipe_id')):
                replacement.update(vector_profile=expected['profile_id'], vector_recipe=expected['recipe_id'])
            if _raw(replacement) == _raw(saved):
                return 'unchanged', proof
            _need(resettled_pair(saved, replacement), 'reader_resettle_invalid')
            validate_sync_owner(owner, Path(decision['storePath']))
            lifetime.check()
            _need(journal.verify_selected_route_for_write(decision) == proof
                  and _v1_reader_evidence(state, project, saved, allow_settings_change=True)[0] == raw,
                  'reader_selection_changed')
            path, db = journal._open(state / _JOURNAL, write=True)
            with closing(db):
                db.execute('BEGIN IMMEDIATE')
                try:
                    current = journal._inspect_v1_row(db)
                    _need(current == {k: v for k, v in row.items() if k != 'journalPath'},
                          'reader_selection_changed')
                    _need(db.execute('UPDATE reader SET record=? WHERE id=1 AND record=?',
                          (journal._canon(replacement), journal._canon(saved))).rowcount == 1,
                          'reader_selection_changed')
                    _need(db.execute("UPDATE selection SET epoch=epoch+1,vector_recipe=? "
                          "WHERE id=1 AND state='selected' AND epoch=?",
                          (replacement['vector_recipe'], row['epoch'])).rowcount == 1,
                          'reader_selection_changed')
                    validate_sync_owner(owner, Path(decision['storePath']))
                    lifetime.check()
                    journal._inspect_v1_row(db)
                    db.commit()
                except BaseException:
                    db.rollback()
                    raise
        return 'resealed', journal.verify_selected_route_for_write(decision)
    finally:
        lifetime.close()


def settings_resettle_pending(state, project, executing_root) -> bool:
    """Recognize only a proved v1 reader with changed recall settings."""
    try:
        state, project = Path(state), Path(project)
        row = journal.inspect_journal(state / _JOURNAL)
        saved = row['reader']
        _need(row['state'] == 'selected' and isinstance(saved, dict), 'reader_endpoint_changed')
        _raw_attestation, proof = _v1_reader_evidence(state, project, saved, allow_settings_change=True)
        return saved['settings'] != _sha(_raw(proof['readerDecision']['recallSettings']))
    except Exception:
        return False



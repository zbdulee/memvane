"""Installer-owned reader selection in a v5 journal. No index writer authority."""
from __future__ import annotations

from contextlib import closing, contextmanager, ExitStack
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

_JOURNAL = 'install-journal.sqlite'
_MAX_IMAGE = 16 * 1024 * 1024
_issued = weakref.WeakKeyDictionary()
_read_only = weakref.WeakSet()
_retained = weakref.WeakSet()


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


@contextmanager
def _readonly(path, state):
    before = _file(path, state)
    for suffix in ('-wal', '-shm', '-journal'):
        side = Path(str(path) + suffix)
        if os.path.lexists(side):
            _file(side, state)
    try:
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True,
                                     timeout=2, isolation_level=None)) as db:
            db.execute('PRAGMA query_only=ON')
            db.execute('PRAGMA trusted_schema=OFF')
            db.execute('BEGIN')
            yield db
            _need(before == _file(path, state), 'reader_store_changed')
    except sqlite3.Error as exc:
        raise journal.JournalEvidenceError('reader_store_invalid') from exc


def _published(state, path, decision):
    from laya_index_a import LayaIndexA, _StreamDigest
    from laya_index_vector import verify_generation_seal
    scope = decision['scopeId']
    with _readonly(path, state) as db:
        LayaIndexA._check_header(db)
        incarnation = db.execute("SELECT value FROM schema_meta WHERE key='incarnation'").fetchone()
        _need(incarnation == (decision['dbIncarnation'],), 'reader_incarnation_changed')
        row = db.execute("""SELECT h.gen,s.source_manifest,s.entries_digest,g.rev_epoch
            FROM head h JOIN generations g ON g.scope=h.scope AND g.gen=h.gen
            JOIN candidate_seals s ON s.scope=h.scope AND s.gen=h.gen
            WHERE h.scope=? AND g.state='published' AND s.source_manifest=g.digest""",
                         (scope,)).fetchone()
        _need(row, 'reader_generation_unpublished')
        gen, manifest, entries, epoch = row
        exclusions = tuple(db.execute('SELECT kind,value FROM exclusions WHERE scope=? ORDER BY kind,value',
                                      (scope,)))
        _need(epoch == len(exclusions), 'reader_exclusions_changed')
        digest = _StreamDigest(scope)
        for item, hashed, eligible, body in db.execute("""SELECT m.path,m.hash,m.eligible,c.body
                FROM membership m LEFT JOIN content c ON c.hash=m.hash
                WHERE m.gen=? AND m.scope=? ORDER BY m.path COLLATE BINARY""", (gen, scope)):
            _need(isinstance(body, str) and _sha(body.encode('utf8')) == hashed,
                  'reader_member_changed')
            digest.add(item, hashed, eligible)
        _need(digest.hexdigest() == entries, 'reader_manifest_changed')
        index = SimpleNamespace(db=db)
        _need(LayaIndexA._generation_fts_matches(index, gen), 'reader_fts_changed')
        seal = verify_generation_seal(index, gen)
        profile = decision['vectorProfile']
        for left, right in (('profile_id', 'profileId'), ('model_id', 'modelId'),
                            ('embedding_fingerprint', 'embeddingFingerprint'),
                            ('dimension', 'dimension'), ('metric', 'metric')):
            _need(seal[left] == profile[right], 'reader_profile_changed')
        return gen, manifest, entries, seal['rows_digest'], exclusions


def _capacity(state, decision, catalog, binding, published):
    import laya_index_capacity as capacity
    import laya_source_catalog as source
    from laya_index_host_route import wiki_index_roots
    gen, manifest = published[:2]
    scope, incarnation = decision['scopeId'], decision['dbIncarnation']
    roots = source._check_roots(wiki_index_roots(decision['collections']))
    with _readonly(binding, state) as db:
        # Use the existing exact schema, metadata and record validators with
        # a caller-owned read transaction. Its normal constructor opens rw.
        store = object.__new__(capacity.GenerationBindingStore)
        store.path, store.db = binding, db
        store.scope, store.db_incarnation, store.roots = scope, incarnation, roots
        store._roots_value = capacity._canonical(list(roots.items())).decode('ascii')
        store._node, store.store_id = tuple(_node(binding)), None
        store._check_file_header()
        store._check_store()
        row = db.execute(capacity._LOOKUP_SQL, (scope, gen, incarnation)).fetchone()
        _need(row, 'reader_binding_missing')
        bound = store._decode(gen, row)
        _need(bound.source_manifest == manifest, 'reader_binding_changed')
        for _name, root, dev, ino in bound.roots:
            base = Path(root)
            _need(base.resolve(strict=True) == base and base.is_dir()
                  and _node(base) == [dev, ino], 'reader_root_changed')
    with _readonly(catalog, state) as db:
        identity = source._binding(scope, roots)
        source.LayaSourceCatalog._check_identity(SimpleNamespace(db=db, binding=identity))
        meta = dict(db.execute('SELECT key,value FROM meta'))
        _need(meta['scope'] == scope and json.loads(meta['roots']) == roots,
              'reader_catalog_changed')
        scan = db.execute('SELECT phase,raw_digest FROM scans WHERE scan_id=?',
                          (bound.catalog_scan_id,)).fetchone()
        _need(scan == ('complete', bound.catalog_raw_digest), 'reader_catalog_scan_changed')


def _evidence(state, project, executing_root, *, target=None, catalog=None, binding=None, side=None):
    import laya_fresh_wiki_origin as fresh
    import laya_incremental_docs as incremental
    import laya_initial_build as build
    from laya_ledger_common import refuse_qmd_origin
    state = _state(state)
    project, executing_root = Path(project), Path(executing_root)
    refuse_qmd_origin(state)
    decision = journal.select_route(str(project), action='recall')
    proof = journal.verify_selected_route(decision)
    _need(proof.get('productRouteAllowed') is True and proof.get('executingPluginRoot') == str(executing_root)
          and proof['projectRoot'] == str(project) and proof['journalPath'] == str(state / _JOURNAL),
          'reader_installer_unproved')
    ledger = fresh._load(state / fresh.LEDGER)
    info = journal.inspect_journal(state / _JOURNAL)
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
    live = Path(decision['dbPath'])
    target = live if target is None else Path(target)
    catalog = Path(catalog or ledger['catalogDb'])
    binding = Path(binding or ledger['bindingStore'])
    # Staging in T1 changes only the read-only base image. Neither root files
    # nor arbitrary other capacity/sidecar data may confer a new identity.
    _need(str(catalog) == ledger['catalogDb'] and str(binding) == ledger['bindingStore']
          and _file(catalog, state) == ledger['catalogIdentity']
          and _file(binding, state) == ledger['bindingStoreIdentity']
          and _file(live, state) == ledger['candidateIdentity'], 'reader_capacity_changed')
    sidecar = state / incremental.FILE
    _need(side is None or Path(side) == sidecar, 'reader_sidecar_changed')
    expected = _published(state, live, decision)
    if target != live:
        _need(_published(state, target, decision) == expected, 'reader_staged_store_changed')
    _capacity(state, decision, catalog, binding, expected)
    if os.path.lexists(sidecar):
        with _readonly(sidecar, state) as db:
            _need(db.execute('PRAGMA user_version').fetchone() == (1,)
                  and db.execute('SELECT * FROM meta').fetchall() == [incremental._expected_meta(identity)],
                  'reader_sidecar_changed')
    raw = _read_private(state / journal.PRODUCTION_ATTESTATION, 1024 * 1024)
    _need(_sha(raw) == ledger['attestationSha256'], 'reader_attestation_changed')
    record = dict(install_id=proof['installId'], project=str(project), project_node=_node(project),
                  state=str(state), state_node=_node(state), scope=proof['scopeId'], store_format='v6',
                  capability='v6-ro-v1', db_path=str(target), db_node=_file(target, state),
                  catalog_path=str(catalog), catalog_node=_file(catalog, state),
                  binding_path=str(binding), binding_node=_file(binding, state),
                  sidecar_path=str(sidecar), incarnation=proof['dbIncarnation'],
                  profile=proof['vectorProfile']['profileId'], attestation_sha256=_sha(raw),
                  checkout_policy='selected-main-v6', settings=_sha(_raw(decision['recallSettings'])),
                  roots=decision['collections'], creation_epoch=proof['selectionEpoch'])
    return record, raw, proof


def _endpoint(state, project, root, options):
    record, raw, proof = _evidence(state, project, root, **options)
    token = object.__new__(VerifiedEndpoint)
    packed = _raw(record)
    object.__setattr__(token, 'endpoint_id', _sha(b'memvane-reader-endpoint-v1\0' + packed))
    object.__setattr__(token, 'record', _freeze(record))
    _issued[token] = (Path(state), Path(project), Path(root), dict(options), packed, raw)
    return token, proof


def _persisted_endpoint(state, project, root, endpoint_id, stored, attestation):
    """Reissue a persisted reader after current full proof, without CAS authority."""
    if stored.get('capability') == 'v6-retained-ro-v1':
        from laya_move_reader_journal import resolve_retained
        return resolve_retained(state, project, root, endpoint_id, stored, attestation)
    _need(isinstance(stored, dict) and type(stored.get('creation_epoch')) is int
          and stored['creation_epoch'] >= 1, 'reader_endpoint_invalid')
    options = dict(target=stored.get('db_path'), catalog=stored.get('catalog_path'),
                   binding=stored.get('binding_path'), side=stored.get('sidecar_path'))
    current, raw, proof = _evidence(state, project, root, **options)
    _need(proof['selectionEpoch'] >= stored['creation_epoch'], 'reader_endpoint_changed')
    current['creation_epoch'] = stored['creation_epoch']
    packed = _raw(current)
    _need(packed == _raw(stored) and raw == attestation and
          endpoint_id == _sha(b'memvane-reader-endpoint-v1\0' + packed),
          'reader_endpoint_changed')
    token = object.__new__(VerifiedEndpoint)
    object.__setattr__(token, 'endpoint_id', endpoint_id)
    object.__setattr__(token, 'record', _freeze(current))
    _issued[token] = (Path(state), Path(project), Path(root), options, packed, raw)
    _read_only.add(token)
    return token


def _revoke_reader(endpoint):
    _issued.pop(endpoint, None)
    _read_only.discard(endpoint)
    _retained.discard(endpoint)


def _recheck(endpoint):
    _need(type(endpoint) is VerifiedEndpoint and endpoint in _issued, 'reader_endpoint_not_issued')
    from laya_move_reader_journal import _persisted, recheck_persisted
    if endpoint in _persisted:
        return recheck_persisted(endpoint)
    if endpoint in _retained:
        from laya_move_read_seal import _recheck_retained
        return _recheck_retained(endpoint)
    state, project, root, options, packed, raw = _issued[endpoint]
    record, attestation, proof = _evidence(state, project, root, **options)
    creation_epoch = json.loads(packed)['creation_epoch']
    _need(proof['selectionEpoch'] >= creation_epoch, 'reader_endpoint_changed')
    record['creation_epoch'] = creation_epoch
    _need(_raw(record) == packed and attestation == raw
          and endpoint.endpoint_id == _sha(b'memvane-reader-endpoint-v1\0' + packed),
          'reader_endpoint_changed')
    return record, raw, proof


def seal_current_reader(state: Path, project: Path, *, executing_root: Path) -> InstallerSeal:
    from laya_journal_recovery import recover_initial
    recover_initial(_state(state), Path(project), Path(executing_root))
    endpoint, _proof = _endpoint(state, project, executing_root, {})
    token = object.__new__(InstallerSeal)
    object.__setattr__(token, 'endpoint', endpoint)
    _issued[token] = endpoint
    return token


def _installer(token):
    _need(type(token) is InstallerSeal and token in _issued and _issued[token] is token.endpoint,
          'reader_installer_not_issued')
    return token.endpoint


def seal_reader_endpoint(installer: InstallerSeal, *, db_path: Path, catalog_path=None,
                         binding_path=None, sidecar_path=None) -> VerifiedEndpoint:
    endpoint = _installer(installer)
    _recheck(endpoint)
    state, project, root = _issued[endpoint][:3]
    staged, _proof = _endpoint(state, project, root, dict(target=db_path, catalog=catalog_path,
                                                         binding=binding_path, side=sidecar_path))
    return staged

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
          and record.get('store_format') == 'v6' and record.get('capability') in ('v6-ro-v1','v6-retained-ro-v1')
          and record.get('attestation_sha256') == _sha(row[1]), 'reader_endpoint_invalid')
    if record.get('capability') == 'v6-retained-ro-v1':
        from laya_move_reader_journal import validate_endpoint
        validate_endpoint(db, endpoint_id, record, row[1])
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
        _need(record['install_id'] == previous['install_id'], 'reader_install_changed')
    from laya_move_reader_journal import validate_selection
    validate_selection(db, selected, record)
    return selected, record


def _journal(path):
    path = Path(path)
    _need(path.name == _JOURNAL, 'invalid_journal_path')
    _state(path.parent)
    _file(path, path.parent)
    return path


def read_selection(journal: Path) -> ReaderSelection:
    path = _journal(journal)
    with _readonly(path, path.parent) as db:
        return _selection(db)[0]


def _insert_endpoint(db, endpoint, record, raw):
    row = (_raw(record).decode('utf8'), raw)
    existing = db.execute('SELECT record,attestation FROM read_endpoints WHERE endpoint_id=?',
                          (endpoint.endpoint_id,)).fetchone()
    if existing is None:
        db.execute('INSERT INTO read_endpoints VALUES(?,?,?)', (endpoint.endpoint_id, *row))
    else:
        _need(existing == row, 'reader_endpoint_conflict')


def convert_reader_v5(state: Path, project: Path, *, installer: InstallerSeal, readiness) -> ReaderSelection:
    import laya_journal_recovery as recovery
    import laya_bridge_readiness as bridge
    import laya_initial_build as build
    import laya_fresh_wiki_origin as fresh
    import project_dir
    from laya_index_sync import _owner_lock
    from laya_ledger_common import _operation_lock
    endpoint = _installer(installer)
    state, project = _state(state), Path(project)
    _need(_issued[endpoint][:2] == (state, project), 'reader_install_changed')
    recovery.recover_initial(state, project, _issued[endpoint][2])
    _record, _att, proof = _recheck(endpoint)
    bridge.require_bridge_preflight(readiness, state, proof)
    path = _journal(state / _JOURNAL)
    with ExitStack() as locks:
        locks.enter_context(_install_lock(state))
        _need(locks.enter_context(build.owner(state)), 'initial_build_busy')
        locks.enter_context(_operation_lock(project_dir.runtime_file(project, fresh.MARKER)))
        queue = Path(journal.select_route(str(project), action='recall')['queuePath'])
        _need(locks.enter_context(_owner_lock(queue)), 'sync_owner_busy')
        version = journal.journal_version(path)
        _need(version in (3, 4, 5), 'reader_schema_unknown')
        locks.enter_context(_transition(proof['installId'], create=version != 5))
        _need(journal.journal_version(path) == version, 'reader_selection_changed')
        from laya_reader_lease import _prepare_lifetime
        _prepare_lifetime(proof['installId'], create=version != 5)
        if journal.journal_version(path) == 3:
            journal.convert_to_v4(path)
        if journal.journal_version(path) == 5:
            selected = read_selection(path)
            _need(selected.preferred_endpoint == endpoint.endpoint_id, 'reader_selection_changed')
            return selected
        record, _, proof = _recheck(endpoint)
        bridge.require_bridge_preflight(readiness, state, proof)
        initial = recovery.prepare(state, project, endpoint, record, proof)
        with closing(journal._open(path, write=True)[1]) as db:
            db.execute('PRAGMA synchronous=FULL')
            db.execute('BEGIN IMMEDIATE')
            try:
                row = journal._row(db)
                _need(row == initial.row, 'reader_selection_changed')
                record, raw, proof = _recheck(endpoint)
                bridge.require_bridge_preflight(readiness, state, proof)
                _need(row['epoch'] == proof['selectionEpoch'] and
                      row['provider_attestation_sha'] == _sha(raw), 'reader_selection_changed')
                _need(row['_version'] == 4, 'reader_schema_unknown')
                journal._step('v5-locked')
                for sql in journal._READER_DDL.values():
                    db.execute(sql)
                _insert_endpoint(db, endpoint, record, raw)
                transition = initial.data['transition']
                db.execute('INSERT INTO reader_transitions VALUES(?,1,?,NULL)',
                           (transition, endpoint.endpoint_id))
                db.execute('INSERT INTO reader_selection VALUES(1,1,?,NULL,?)',
                           (endpoint.endpoint_id, transition))
                journal._step('v5-sealed')
                db.execute('UPDATE selection SET schema=? WHERE id=1', (journal.SCHEMA_V5,))
                db.execute('PRAGMA user_version=5')
                journal._step('v5-versioned')
                selected, _ = _selection(db)
                db.commit()
            except BaseException:
                db.rollback()
                raise
        journal._step('v5-committed')
        _sync(path)
        _sync(state, True)
        recovery.finish(initial)
        return selected


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


def mutate_image(journal_path, operation):
    """Mutate an owned sibling; the named v5 journal is never opened for write."""
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
            _need(_file(path, path.parent) == current_node, 'reader_journal_changed')
            os.replace(image, path)
            journal._step('v5-image-replaced')
            _sync(path.parent, True)
            journal._step('v5-directory-fsynced')
        finally:
            _discard_staging(lockfd, image)


def cas_reader(journal: Path, *, expected_epoch: int, expected_endpoint: str,
               target: VerifiedEndpoint, transition_id: str) -> ReaderSelection:
    path = _journal(journal)
    _need(type(target) is not VerifiedEndpoint or target not in _read_only,
          'reader_endpoint_read_only')
    _need(type(target) is VerifiedEndpoint and target in _issued and
          _issued[target][0] == path.parent, 'reader_endpoint_not_issued')
    _need(type(expected_epoch) is int and expected_epoch >= 1 and type(expected_endpoint) is str
          and re.fullmatch(r'[0-9a-f]{64}', expected_endpoint) and type(transition_id) is str
          and re.fullmatch(r'[0-9a-f]{32}', transition_id),
          'reader_transition_invalid')
    outcome = []

    def select(db):
        current, old = _selection(db)
        _need(old.get('capability') != 'v6-retained-ro-v1', 'reader_move_pin_active')
        record, raw, proof = _recheck(target)
        _need(all(record[k] == old[k] for k in ('scope', 'install_id', 'project', 'state',
              'incarnation', 'project_node', 'state_node', 'attestation_sha256', 'profile',
              'checkout_policy', 'settings', 'roots')), 'reader_install_changed')
        if (current.epoch == expected_epoch + 1 and current.previous_endpoint == expected_endpoint
                and current.transition_id == transition_id and current.preferred_endpoint == target.endpoint_id):
            outcome.append(current)
            return False
        _need(current.epoch == expected_epoch and current.preferred_endpoint == expected_endpoint
              and target.endpoint_id != expected_endpoint, 'stale_reader_cas')
        _need(not db.execute('SELECT 1 FROM reader_transitions WHERE transition_id=?',
                             (transition_id,)).fetchone(), 'reader_transition_reused')
        _insert_endpoint(db, target, record, raw)
        next_epoch = current.epoch + 1
        db.execute('INSERT INTO reader_transitions VALUES(?,?,?,?)',
                   (transition_id, next_epoch, target.endpoint_id, expected_endpoint))
        db.execute('UPDATE reader_selection SET epoch=?,preferred_endpoint=?,previous_endpoint=?,transition_id=? WHERE id=1',
                   (next_epoch, target.endpoint_id, expected_endpoint, transition_id))
        outcome.append(_selection(db)[0])
        return True
    mutate_image(path, select)
    return outcome[0]


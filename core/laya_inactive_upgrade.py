"""Installer-owned, recoverable bundle replacement for an inactive build.

The candidate DB, catalog, claim queue, binding store, incremental documents,
scope and install ID stay in place. A build owner lock excludes its writer;
the ``upgrading`` phase makes both old and new workers fail closed across a
crash. Only exact, separately installed and reviewed provider bundles may be
switched. This never publishes a project route or changes QMD selection.

The new bundle may sit at the pending root itself (the host re-extracted the
same version in place, so the old ownership seal no longer verifies). The
adoption proves the bytes against the commit and the switch rebinds the
journal to the new seal. The old seal no longer exists on disk, so such a
switch has no restore: a failure stays fenced (``upgrading``) with its intent,
and the next run finishes it forward.
"""
from __future__ import annotations

from contextlib import ExitStack, closing, contextmanager
from contextvars import ContextVar
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile

import laya_initial_build as build
from laya_install_journal import (PRODUCTION_ATTESTATION,
                                  _attestation, _canon as journal_canon,
                                  cas_replace_inactive_candidate,
                                  inspect_journal, preflight_product_candidate)
from laya_product_bundle import (BundleEvidenceError, adopt_host_loaded_plugin,
                                 inspect_full_plugin_candidate,
                                 PRODUCTION_ATTESTATION_SCHEMA,
                                 validate_production_provider_attestation)

INTENT = 'inactive-upgrade-intent.json'
BACKUP = 'inactive-upgrade-backup-'
INTENT_LIMIT = 3 * 1024 * 1024
_DB_KEYS = ('candidateDb', 'queueDb', 'catalogDb', 'bindingStore')


def _fail(reason):
    from laya_product_install import InstallBlocked
    raise InstallBlocked(reason)


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def _fsync(directory):
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_expected(path, old, new):
    """Replace only a still-owned exact metadata file, fsyncing both levels."""
    from laya_product_install import _read_private
    current = _read_private(path, 1024 * 1024)
    if current == new:
        return
    if current != old:
        _fail('inactive_upgrade_metadata_changed')
    fd, name = tempfile.mkstemp(prefix='.inactive-upgrade-', dir=path.parent)
    staged = Path(name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(new)
            stream.flush()
            os.fsync(stream.fileno())
        if path.is_symlink() or _read_private(path, 1024 * 1024) != old:
            _fail('inactive_upgrade_metadata_changed')
        os.replace(staged, path)
        _fsync(path.parent)
    finally:
        staged.unlink(missing_ok=True)


def _sqlite_count(path, table):
    from laya_index_a import _query_only_uri
    with closing(sqlite3.connect(_query_only_uri(path), uri=True)) as db:
        db.execute('PRAGMA query_only=ON')
        if db.execute('PRAGMA quick_check').fetchone() != ('ok',):
            _fail('inactive_upgrade_store_integrity_failed')
        return db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]


def _hash_private(path):
    source = Path(path)
    fd = os.open(source, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or
                before.st_nlink != 1 or stat.S_IMODE(before.st_mode) & 0o077):
            _fail('inactive_upgrade_backup_unsafe')
        digest = hashlib.sha256()
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        after = os.fstat(fd)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != \
                (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            _fail('inactive_upgrade_backup_changed')
        return digest.hexdigest()
    finally:
        os.close(fd)


def _store_counts(identity):
    """Read-only schema, binding and cache proof; no constructor migrations."""
    from laya_index_a import LayaIndexA
    from laya_index_claim_queue import ClaimQueue, APP_ID as QUEUE_APP
    from laya_index_capacity import (_check_header as binding_header,
                                     STORE_FORMAT, STORE_VERSION)
    from laya_index_host_route import wiki_index_roots
    from laya_source_catalog import (APP_ID as CATALOG_APP,
                                     SCHEMA_VERSION as CATALOG_VERSION,
                                     _binding, _check_roots)
    from laya_incremental_docs import FILE as DOCS
    from laya_index_a import _query_only_uri
    roots = _check_roots(wiki_index_roots(identity['collections']))
    schemas = {}

    def open_store(key, path):
        source = Path(path)
        info = source.lstat()
        if (source.is_symlink() or not stat.S_ISREG(info.st_mode) or
                info.st_uid != os.getuid() or info.st_nlink != 1 or
                stat.S_IMODE(info.st_mode) & 0o077):
            _fail('inactive_upgrade_store_unsafe')
        # WAL stores: Apple's libsqlite3 refuses mode=ro once a stock build
        # closed them without -wal/-shm.
        db = sqlite3.connect(_query_only_uri(source), uri=True,
                             isolation_level=None, timeout=2)
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        if db.execute('PRAGMA quick_check').fetchone() != ('ok',):
            _fail('inactive_upgrade_store_integrity_failed')
        rows = db.execute('SELECT type,name,tbl_name,sql FROM sqlite_master '
                          'ORDER BY type,name,tbl_name').fetchall()
        schemas[key] = {'applicationId': db.execute('PRAGMA application_id').fetchone()[0],
                        'userVersion': db.execute('PRAGMA user_version').fetchone()[0],
                        'ddlSha256': hashlib.sha256(_canonical(rows)).hexdigest()}
        return db

    with closing(open_store('candidateDb', identity['candidateDb'])) as db:
        LayaIndexA._check_header(db)
        if db.execute("SELECT value FROM schema_meta WHERE key='incarnation'").fetchone() != \
                (identity['dbIncarnation'],):
            _fail('inactive_upgrade_incarnation_changed')
        if db.execute('SELECT COUNT(*) FROM vector_chunk_sets WHERE profile_id<>?',
                      (identity['vectorProfileId'],)).fetchone()[0]:
            _fail('inactive_upgrade_vector_profile_changed')
        sets = db.execute('SELECT COUNT(*) FROM vector_chunk_sets').fetchone()[0]
        chunks = db.execute('SELECT COUNT(*) FROM vector_chunks').fetchone()[0]
    with closing(open_store('queueDb', identity['queueDb'])) as db:
        if ClaimQueue._check_database(db) != identity['dbIncarnation'] or \
                schemas['queueDb']['applicationId'] != QUEUE_APP:
            _fail('inactive_upgrade_claim_binding_changed')
    with closing(open_store('catalogDb', identity['catalogDb'])) as db:
        if (schemas['catalogDb']['applicationId'] != CATALOG_APP or
                schemas['catalogDb']['userVersion'] != CATALOG_VERSION or
                db.execute("SELECT value FROM meta WHERE key='binding'").fetchone() !=
                (_binding(identity['scopeId'], roots),)):
            _fail('inactive_upgrade_catalog_binding_changed')
    with closing(open_store('bindingStore', identity['bindingStore'])) as db:
        binding_header(db)
        metadata = dict(db.execute('SELECT key,value FROM meta'))
        if (metadata.get('format') != STORE_FORMAT or
                metadata.get('version') != str(STORE_VERSION) or
                metadata.get('scope') != identity['scopeId'] or
                metadata.get('db_incarnation') != identity['dbIncarnation'] or
                json.loads(metadata.get('roots', 'null')) !=
                [[name, root] for name, root in sorted(roots.items())]):
            _fail('inactive_upgrade_binding_changed')
    sidecar = Path(identity['stateRoot']) / DOCS
    if sidecar.is_symlink():
        _fail('inactive_upgrade_store_unsafe')
    if sidecar.exists():
        with closing(open_store('incrementalDocs', sidecar)) as db:
            expected = (identity['installId'], identity['scopeId'],
                        identity['dbIncarnation'], identity['vectorProfileId'],
                        identity['candidateDb'], identity['projectRoot'])
            if (schemas['incrementalDocs']['userVersion'] != 1 or
                    db.execute('SELECT install_id,scope_id,db_incarnation,profile_id,'
                               'candidate_db,project_root FROM meta').fetchall() != [expected]):
                _fail('inactive_upgrade_incremental_identity_changed')
            docs = db.execute('SELECT COUNT(*) FROM docs').fetchone()[0]
    else:
        docs = 0
    return {'vectorChunkSets': sets, 'vectorChunks': chunks,
            'publishedDocuments': docs, 'storeSchemas': schemas}


def _backup(identity, old_record, old_route, old_attestation, counts):
    """Consistent SQLite snapshots while the one candidate writer is locked."""
    state = Path(identity['stateRoot'])
    final = state / (BACKUP + os.urandom(12).hex())
    staged = Path(tempfile.mkdtemp(prefix='.inactive-upgrade-backup-', dir=state))
    try:
        dbs = {key: identity[key] for key in _DB_KEYS}
        from laya_incremental_docs import FILE as DOCS
        from laya_index_a import _query_only_uri
        sidecar = state / DOCS
        if sidecar.exists() or sidecar.is_symlink():
            dbs['incrementalDocs'] = str(sidecar)
        dbs['journal'] = identity['journalPath']
        digests = {}
        for key, path in dbs.items():
            src = Path(path)
            info = src.lstat()
            if (src.is_symlink() or not stat.S_ISREG(info.st_mode) or
                    info.st_uid != os.getuid() or info.st_nlink != 1 or
                    stat.S_IMODE(info.st_mode) & 0o077):
                _fail('inactive_upgrade_store_unsafe')
            target = staged / (key + '.sqlite')
            with closing(sqlite3.connect(_query_only_uri(src), uri=True)) as source:
                source.execute('PRAGMA query_only=ON')
                with closing(sqlite3.connect(target)) as copy:
                    source.backup(copy)
                    if copy.execute('PRAGMA integrity_check').fetchone() != ('ok',):
                        _fail('inactive_upgrade_backup_invalid')
            os.chmod(target, 0o600)
            digests[key] = _hash_private(target)
            with target.open('rb') as stream:
                os.fsync(stream.fileno())
        receipt = {'schema': 'laya-inactive-upgrade-backup-v1',
                   'record': old_record, 'routeSha256': hashlib.sha256(old_route).hexdigest(),
                   'attestationSha256': hashlib.sha256(old_attestation).hexdigest(),
                   'counts': counts, 'dbSha256': digests}
        manifest = staged / 'manifest.json'
        with manifest.open('xb') as stream:
            os.chmod(manifest, 0o600)
            stream.write(_canonical(receipt))
            stream.flush()
            os.fsync(stream.fileno())
        _fsync(staged)
        if final.exists() or final.is_symlink():
            _fail('inactive_upgrade_backup_already_exists')
        os.rename(staged, final)
        _fsync(state)
    finally:
        if staged.exists():
            # An interrupted private staging directory is kept for review;
            # it is never mistaken for the complete named backup.
            pass
    return final


_backup_proofs = ContextVar('inactive_backup_proofs', default=None)


@contextmanager
def backup_proof_scope():
    """Reuse a backup proof only while its observed files remain unchanged."""
    with ExitStack() as stack:
        token = _backup_proofs.set((os.getpid(), {}, stack))
        try:
            yield
        finally:
            _backup_proofs.reset(token)


def _backup_file_stamp(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if path.is_symlink():
        _fail('inactive_upgrade_backup_changed')
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _backup_stamp(backup, paths, monitors):
    def versions():
        return tuple(db.execute('PRAGMA data_version').fetchone()[0] for db in monitors)
    files = [backup, backup / 'manifest.json']
    for path in paths:
        files.extend((path, Path(str(path) + '-wal'), Path(str(path) + '-journal')))
    before_versions = versions()
    before_files = tuple(_backup_file_stamp(path) for path in files)
    after_versions = versions()
    after_files = tuple(_backup_file_stamp(path) for path in files)
    if before_versions != after_versions or before_files != after_files:
        _fail('inactive_upgrade_backup_changed')
    return after_files, after_versions


def _verify_backup(intent):
    scope = _backup_proofs.get()
    if scope is None:
        return _verify_backup_full(intent)
    from laya_product_install import _read_private
    pid, cache, stack = scope
    if pid != os.getpid():
        _fail('inactive_upgrade_backup_changed')
    key = hashlib.sha256(_canonical({key: intent[key] for key in
        ('backup', 'oldRecord', 'counts', 'oldRoute', 'oldAttestation')})).hexdigest()
    entry = cache.get(key)
    if entry is not None:
        backup, paths, monitors, expected = entry
        if _backup_stamp(backup, paths, monitors) != expected:
            _fail('inactive_upgrade_backup_changed')
        return
    backup = Path(intent['backup'])
    state = Path(intent['oldRecord']['identity']['stateRoot'])
    if (backup.parent != state or not backup.name.startswith(BACKUP) or backup.is_symlink()):
        _fail('inactive_upgrade_backup_invalid')
    info = backup.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or
            stat.S_IMODE(info.st_mode) & 0o077):
        _fail('inactive_upgrade_backup_invalid')
    receipt = json.loads(_read_private(backup / 'manifest.json', 65536))
    digests = receipt.get('dbSha256')
    keys = set(_DB_KEYS) | {'journal'}
    if isinstance(digests, dict) and 'incrementalDocs' in digests:
        keys.add('incrementalDocs')
    if not isinstance(digests, dict) or set(digests) != keys:
        _fail('inactive_upgrade_backup_invalid')
    paths = tuple(backup / (key + '.sqlite') for key in sorted(keys))
    from laya_index_a import _query_only_uri
    monitors = []
    for path in paths:
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
                info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o077):
            _fail('inactive_upgrade_backup_changed')
        for suffix in ('-wal', '-shm', '-journal'):
            if Path(str(path) + suffix).is_symlink():
                _fail('inactive_upgrade_backup_changed')
        db = stack.enter_context(closing(sqlite3.connect(_query_only_uri(path),
                                                         uri=True, isolation_level=None)))
        db.execute('PRAGMA query_only=ON')
        monitors.append(db)
    before = _backup_stamp(backup, paths, monitors)
    _verify_backup_full(intent)
    after = _backup_stamp(backup, paths, monitors)
    if before != after:
        _fail('inactive_upgrade_backup_changed')
    cache[key] = (backup, paths, tuple(monitors), after)


def _verify_backup_full(intent):
    """Require the complete private snapshot before changing any metadata."""
    from laya_product_install import _read_private
    from laya_index_a import _query_only_uri
    state = Path(intent['oldRecord']['identity']['stateRoot'])
    backup = Path(intent['backup'])
    if (backup.parent != state or not backup.name.startswith(BACKUP) or
            backup.is_symlink()):
        _fail('inactive_upgrade_backup_invalid')
    info = backup.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or
            stat.S_IMODE(info.st_mode) & 0o077):
        _fail('inactive_upgrade_backup_invalid')
    receipt = json.loads(_read_private(backup / 'manifest.json', 65536))
    if (receipt.get('schema') != 'laya-inactive-upgrade-backup-v1' or
            receipt.get('record') != intent['oldRecord'] or
            receipt.get('counts') != intent['counts'] or
            receipt.get('routeSha256') != hashlib.sha256(
                intent['oldRoute'].encode('utf-8')).hexdigest() or
            receipt.get('attestationSha256') != hashlib.sha256(
                intent['oldAttestation'].encode('utf-8')).hexdigest() or
            not isinstance(receipt.get('dbSha256'), dict)):
        _fail('inactive_upgrade_backup_invalid')
    expected_keys = set(_DB_KEYS) | {'journal'}
    if 'incrementalDocs' in receipt['dbSha256']:
        expected_keys.add('incrementalDocs')
    if set(receipt['dbSha256']) != expected_keys:
        _fail('inactive_upgrade_backup_invalid')
    for key, digest in receipt['dbSha256'].items():
        path = backup / (key + '.sqlite')
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
                info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o077 or
                _hash_private(path) != digest):
            _fail('inactive_upgrade_backup_changed')
        # A backup of a WAL store keeps the WAL header without sidecars.
        with closing(sqlite3.connect(_query_only_uri(path), uri=True)) as db:
            db.execute('PRAGMA query_only=ON')
            if db.execute('PRAGMA integrity_check').fetchone() != ('ok',):
                _fail('inactive_upgrade_backup_changed')
    if ('candidateDb' not in receipt['dbSha256'] or
            'journal' not in receipt['dbSha256'] or
            _sqlite_count(backup / 'candidateDb.sqlite', 'vector_chunk_sets') !=
            intent['counts']['vectorChunkSets']):
        _fail('inactive_upgrade_backup_invalid')


def _intent(state, old_record, old_route, old_attestation, new_route,
            new_attestation, new_identity, counts, backup, review_sha, new_proof):
    from laya_initial_build import _write_json, _read_json
    value = {'schema': 'laya-inactive-upgrade-intent-v1',
             'oldRecord': old_record,
             'oldRoute': old_route.decode('utf-8'),
             'oldAttestation': old_attestation.decode('utf-8'),
             'newRoute': new_route.decode('utf-8'),
             'newAttestation': new_attestation.decode('utf-8'),
             'newIdentity': new_identity, 'counts': counts,
             'backup': str(backup), 'reviewSha256': review_sha,
             'newProof': {key: new_proof[key] for key in
                          ('pluginRoot', 'sourceCommit', 'bundleManifestSha256',
                           'ownershipManifestSha256', 'vectorProfile')}}
    path = state / INTENT
    if path.exists() or path.is_symlink():
        if _read_json(path, max_bytes=INTENT_LIMIT) != value:
            _fail('inactive_upgrade_intent_changed')
    else:
        _write_json(path, value, replace=False, max_bytes=INTENT_LIMIT)
    return value


def _retire_intent(state, intent, outcome):
    """Leave an immutable private receipt; free the active intent name."""
    path = state / INTENT
    if build._read_json(path, max_bytes=INTENT_LIMIT) != intent:
        _fail('inactive_upgrade_intent_changed')
    tag = hashlib.sha256(_canonical(intent)).hexdigest()[:16]
    target = state / ('inactive-upgrade-' +
                      str(intent['oldRecord']['epoch']) + '-' + tag +
                      '-' + outcome + '.json')
    if target.exists() or target.is_symlink():
        _fail('inactive_upgrade_receipt_exists')
    os.rename(path, target)
    _fsync(state)


def _pending_route(journal, old_sha, new_sha):
    if (journal['state'] != 'candidate_ready' or journal['selected'] is not None or
            journal['previous'] is not None or journal['newWriteSeen'] or
            journal['pendingRouteSha256'] not in (old_sha, new_sha)):
        _fail('inactive_upgrade_journal_changed')


def _switch(intent, old_proof, new_proof):
    """Idempotent forward recovery while both locks are held."""
    state = Path(intent['oldRecord']['identity']['stateRoot'])
    old = intent['oldRecord']['identity']
    new = intent['newIdentity']
    old_route = intent['oldRoute'].encode('utf-8')
    new_route = intent['newRoute'].encode('utf-8')
    old_att = intent['oldAttestation'].encode('utf-8')
    new_att = intent['newAttestation'].encode('utf-8')
    record = build.read(state)
    if record == intent['oldRecord']:
        record = build.advance(record, phase='upgrading', blocked=None)
    if record['phase'] != 'upgrading' or record['identity'] not in (old, new):
        _fail('inactive_upgrade_record_changed')
    journal = inspect_journal(old['journalPath'])
    _pending_route(journal, old['routeSha256'], new['routeSha256'])
    _atomic_expected(state / PRODUCTION_ATTESTATION, old_att, new_att)
    if journal['pendingRouteSha256'] == old['routeSha256']:
        journal = cas_replace_inactive_candidate(
            old['journalPath'], journal['epoch'], old_route, new_proof, new_route)
    _atomic_expected(Path(old['routeFile']), old_route, new_route)
    if record['identity'] == old:
        record = build.rebind_inactive_upgrade(record, new)
    approval = preflight_product_candidate(old['journalPath'])
    if (approval['attestationSha256'] != new['attestationSha256'] or
            approval['pendingRouteSha256'] != new['routeSha256'] or
            _store_counts(new) != intent['counts']):
        _fail('inactive_upgrade_recheck_failed')
    original = intent['oldRecord']
    record = build.advance(record, phase=original['phase'],
                           blocked=original['blocked'],
                           progress=original['progress'],
                           freshness=original['freshness'])
    _retire_intent(state, intent, 'complete')
    return record


def _restore(intent, old_proof, new_proof):
    """Restore original metadata after a failed switch, while both locks hold."""
    state = Path(intent['oldRecord']['identity']['stateRoot'])
    old, new = intent['oldRecord']['identity'], intent['newIdentity']
    record = build.read(state)
    if record['phase'] != 'upgrading' or record['identity'] not in (old, new):
        _fail('inactive_upgrade_restore_state_changed')
    old_route, new_route = (intent[key].encode('utf-8') for key in
                            ('oldRoute', 'newRoute'))
    old_att, new_att = (intent[key].encode('utf-8') for key in
                        ('oldAttestation', 'newAttestation'))
    journal = inspect_journal(old['journalPath'])
    _pending_route(journal, old['routeSha256'], new['routeSha256'])
    _atomic_expected(Path(old['routeFile']), new_route, old_route)
    if journal['pendingRouteSha256'] == new['routeSha256']:
        cas_replace_inactive_candidate(old['journalPath'], journal['epoch'],
                                       new_route, old_proof, old_route)
    _atomic_expected(state / PRODUCTION_ATTESTATION, new_att, old_att)
    if record['identity'] == new:
        record = build.rebind_inactive_upgrade(record, old)
    approval = preflight_product_candidate(old['journalPath'])
    if (approval['attestationSha256'] != old['attestationSha256'] or
            approval['pendingRouteSha256'] != old['routeSha256'] or
            _store_counts(old) != intent['counts']):
        _fail('inactive_upgrade_restore_recheck_failed')
    original = intent['oldRecord']
    record = build.advance(record, phase=original['phase'],
                           blocked=original['blocked'],
                           progress=original['progress'],
                           freshness=original['freshness'])
    _retire_intent(state, intent, 'restored')
    return record


def upgrade(*, project_root, state_root, source_root, plugin_root, commit_id,
            accepted_provider_sha256, model_path, dimension,
            restore=False, runtime_generation=None):
    """Switch a private, unselected candidate to the currently installed host.

    Repeating after an interrupted attempt resumes the exact intent. A caught
    failure restores old metadata; an unclean crash remains fail closed until
    this action or its explicit restore mode is repeated.
    """
    from laya_product_install import (_install_lock, _private_parent,
                                      _read_private, review_local_provider,
                                      _request_build_wake)
    from laya_index_host_route import select_route
    state = _private_parent(state_root)
    with _install_lock(state), build.owner(state) as owned:
        if not owned:
            _fail('initial_build_busy')
        record = build.read(state)
        if record is None or record['identity']['projectRoot'] != str(project_root):
            _fail('initial_build_project_mismatch')
        old_identity = record['identity']
        existing = state / INTENT
        intent = (build._read_json(existing, max_bytes=INTENT_LIMIT)
                  if existing.exists() or existing.is_symlink() else None)
        if intent is not None:
            if intent.get('schema') != 'laya-inactive-upgrade-intent-v1' or \
                    intent.get('oldRecord', {}).get('identity', {}).get('projectRoot') != str(project_root):
                _fail('inactive_upgrade_intent_invalid')
            old_identity = intent['oldRecord']['identity']
            if record['phase'] != 'upgrading' and record['identity'] == old_identity:
                # A crash before the phase fence (or a completed rollback)
                # may leave an intent. The old worker could have advanced in
                # the meantime, so never reuse its snapshot or backup.
                if (_read_private(Path(old_identity['routeFile']), 8192) !=
                        intent['oldRoute'].encode('utf-8') or
                        _read_private(state / PRODUCTION_ATTESTATION, 1024 * 1024) !=
                        intent['oldAttestation'].encode('utf-8') or
                        inspect_journal(old_identity['journalPath'])['pendingRouteSha256'] !=
                        old_identity['routeSha256']):
                    _fail('inactive_upgrade_aborted_metadata_changed')
                _retire_intent(state, intent, 'aborted')
                intent = None
            elif record['phase'] != 'upgrading' and \
                    record['identity'] != intent['newIdentity']:
                _fail('inactive_upgrade_record_changed')
        selection = select_route(str(project_root), action='update')
        if (record['phase'] not in ('registered', 'building', 'blocked', 'upgrading') or
                old_identity['stateRoot'] != str(state) or
                (selection.get('state'), selection.get('reason')) !=
                ('legacy', 'no_route_record')):
            _fail('inactive_upgrade_not_unselected')
        old_journal = inspect_journal(old_identity['journalPath'])
        if (old_journal['pending'] is None or
                old_journal['pending']['installId'] != old_identity['installId']):
            _fail('inactive_upgrade_journal_changed')
        old_root = (old_journal['pending']['pluginRoot'] if intent is None else
                    json.loads(intent['oldRoute'])['pluginRoot'])
        same_root = old_root == str(plugin_root)
        try:
            old_proof = inspect_full_plugin_candidate(old_root)
        except BundleEvidenceError:
            if not same_root:
                raise
            # Re-extracted in place: nothing left to restore to.
            old_proof = {'pluginRoot': old_root}
        if restore:
            if intent is None:
                _fail('inactive_upgrade_intent_missing')
            new_proof = inspect_full_plugin_candidate(
                json.loads(intent['newRoute'])['pluginRoot'])
            restored = _restore(intent, old_proof, new_proof)
            return {'status': 'inactive_upgrade_restored',
                    'phase': restored['phase'], 'vectorCounts': intent['counts']}
        review = review_local_provider(source_root=source_root,
                                       plugin_root=plugin_root, state_root=state,
                                       commit_id=commit_id, model_path=model_path,
                                       dimension=dimension,
                                       **({'runtime_generation': runtime_generation}
                                          if runtime_generation is not None else {}))
        if review['proposalSha256'] != accepted_provider_sha256:
            _fail('reviewed_provider_proposal_changed')
        from laya_provider_reproof import attestation_record, reattest
        # Same content at other paths keeps the build's vector profile.
        prior_att = (intent['oldAttestation'].encode('utf-8') if intent is not None
                     else _read_private(state / PRODUCTION_ATTESTATION, 1024 * 1024))
        reviewed = reattest(json.loads(prior_att), review['reviewedProvider'])
        if reviewed is None:
            _fail('inactive_upgrade_vector_profile_changed')
        new_proof = adopt_host_loaded_plugin(
            source_root, plugin_root, commit_id=commit_id,
            vector_profile=reviewed['vectorProfile'])
        old_profile = old_journal['pending']['vectorProfile']
        if new_proof['vectorProfile'] != old_profile:
            _fail('inactive_upgrade_vector_profile_changed')
        if (intent is None and
                old_journal['pending']['pluginRoot'] == new_proof['pluginRoot'] and
                old_journal['pending']['sourceCommit'] == commit_id and
                old_journal['pending']['ownershipManifestSha256'] ==
                new_proof['ownershipManifestSha256'] and
                record['phase'] != 'upgrading'):
            approval = preflight_product_candidate(old_identity['journalPath'])
            if (approval['attestationSha256'] != old_identity['attestationSha256'] or
                    approval['pendingRouteSha256'] != old_identity['routeSha256']):
                _fail('inactive_upgrade_completed_proof_changed')
            return {'status': 'inactive_upgrade_complete',
                    'phase': record['phase'],
                    'vectorCounts': _store_counts(old_identity)}
        route_file = Path(old_identity['routeFile'])
        if intent is None:
            if (record['phase'] == 'upgrading' or
                    old_journal['state'] != 'candidate_ready' or
                    old_journal['selected'] is not None or
                    old_journal['previous'] is not None or
                    old_journal['newWriteSeen']):
                _fail('inactive_upgrade_journal_changed')
            old_route = _read_private(route_file, 8192)
            old_att = _read_private(state / PRODUCTION_ATTESTATION, 1024 * 1024)
            # A root re-extracted in place no longer proves its old seal; its
            # recorded route, attestation and journal row are compared instead.
            approval = ({'attestationSha256': hashlib.sha256(old_att).hexdigest(),
                         'pendingRouteSha256': old_journal['pendingRouteSha256']}
                        if same_root else
                        preflight_product_candidate(old_identity['journalPath']))
            if (hashlib.sha256(old_route).hexdigest() != old_identity['routeSha256'] or
                    hashlib.sha256(old_att).hexdigest() != old_identity['attestationSha256'] or
                    approval['attestationSha256'] != old_identity['attestationSha256'] or
                    approval['pendingRouteSha256'] != old_identity['routeSha256']):
                _fail('inactive_upgrade_old_proof_changed')
            route = json.loads(old_route)
            if route['pluginRoot'] != old_proof['pluginRoot'] or \
                    route['vectorProfile'] != new_proof['vectorProfile']:
                _fail('inactive_upgrade_old_route_changed')
            from laya_index_host_route import written_schema
            route.update(schema=written_schema(route['schema']),
                         pluginRoot=new_proof['pluginRoot'],
                         bundleManifestSha256=new_proof['bundleManifestSha256'],
                         ownershipManifestSha256=new_proof['ownershipManifestSha256'])
            new_route = _canonical(route)
            new_attestation = attestation_record(
                proof=new_proof, journal_path=old_identity['journalPath'],
                reattested=reviewed, schema=PRODUCTION_ATTESTATION_SCHEMA)
            validate_production_provider_attestation(new_proof['vectorProfile'],
                                                     new_proof, new_attestation)
            new_att = journal_canon(new_attestation).encode('utf-8')
            new_identity = {**old_identity,
                            'routeSha256': hashlib.sha256(new_route).hexdigest(),
                            'attestationSha256': hashlib.sha256(new_att).hexdigest(),
                            'providerProposalSha256': review['proposalSha256']}
            if new_identity['routeSha256'] == old_identity['routeSha256']:
                _fail('inactive_upgrade_same_bundle')
            counts = _store_counts(old_identity)
            backup = _backup(old_identity, record, old_route, old_att, counts)
            intent = _intent(state, record, old_route, old_att, new_route,
                             new_att, new_identity, counts, backup,
                             review['proposalSha256'], new_proof)
        elif (intent['newIdentity']['routeSha256'] !=
              hashlib.sha256(intent['newRoute'].encode('utf-8')).hexdigest() or
              json.loads(intent['newRoute'])['pluginRoot'] != new_proof['pluginRoot'] or
              intent['newIdentity']['vectorProfileId'] != old_identity['vectorProfileId'] or
              intent['reviewSha256'] != review['proposalSha256'] or
              intent['newProof'] != {key: new_proof[key] for key in
                                      ('pluginRoot', 'sourceCommit',
                                       'bundleManifestSha256',
                                       'ownershipManifestSha256', 'vectorProfile')} or
              journal_canon(json.loads(intent['newAttestation'])).encode('utf-8') !=
              intent['newAttestation'].encode('utf-8') or
              json.loads(intent['newAttestation'])['providerManifest'] !=
              reviewed['providerManifest'] or
              json.loads(intent['newAttestation'])['dependencyClosure'] !=
              reviewed['dependencyClosure'] or
              json.loads(intent['newAttestation']).get('profileDependencyClosure') !=
              reviewed.get('profileDependencyClosure')):
            _fail('inactive_upgrade_intent_changed')
        _verify_backup(intent)
        if (record['identity'] == intent['newIdentity'] and
                record['phase'] != 'upgrading'):
            approval = preflight_product_candidate(old_identity['journalPath'])
            if (approval['attestationSha256'] != intent['newIdentity']['attestationSha256'] or
                    approval['pendingRouteSha256'] != intent['newIdentity']['routeSha256'] or
                    _read_private(Path(old_identity['routeFile']), 8192) !=
                    intent['newRoute'].encode('utf-8')):
                _fail('inactive_upgrade_completed_proof_changed')
            if _store_counts(record['identity']) != intent['counts']:
                _fail('inactive_upgrade_cache_changed')
            _retire_intent(state, intent, 'complete')
            return {'status': 'inactive_upgrade_complete',
                    'phase': record['phase'], 'vectorCounts': intent['counts']}
        try:
            completed = _switch(intent, old_proof, new_proof)
        except BaseException:
            try:
                if build.read(state)['phase'] == 'upgrading':
                    _restore(intent, old_proof, new_proof)
            except BaseException:
                # Preserve the fail-closed phase and intent for explicit
                # recovery. Never mask the original failure with a guess.
                pass
            raise
    return {'status': 'inactive_upgrade_complete',
            'phase': completed['phase'], 'vectorCounts': intent['counts'],
            'backup': intent['backup'],
            'wake': _request_build_wake(str(project_root))}

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
import re
import sqlite3
import stat
import tempfile
import time

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
# Full online copies of every store (bundles up to 0.44.15).
RECEIPT_V1 = 'laya-inactive-upgrade-backup-v1'
# A journal copy plus an identity pin per store: an upgrade rebinds only
# metadata, and no restore reads store bytes back from a backup.
RECEIPT_V2 = 'laya-inactive-upgrade-backup-v2'
# Live intents of the three upgrade kinds; their backups are never deleted.
LIVE_INTENTS = (INTENT, 'bound-upgrade-intent.json', 'active-upgrade-intent.json')
PROGRESS_SECONDS = 30
# Pin fields a restore compares. ``dev`` is boot-dependent on APFS, and size,
# mtime, page and freelist counts move when a read-write close checkpoints the
# WAL or a recall adds and drops its read lease row; they are recorded only.
_PIN_COMPARED = ('ino', 'applicationId', 'userVersion', 'pageSize', 'ddlSha256',
                 'contentSha256', 'tailSha256')
# A selected sync pass scans the source catalog and claims work before it
# marks the journal's first new write (``before_publish``), so after a settled
# upgrade those two stores move while a restore is still authorized. Their
# bindings are rechecked by ``_store_counts``; the pin compares the file and
# its schema only.
_PIN_BOOKKEEPING = frozenset(('catalogDb', 'queueDb'))
_PIN_IDENTITY_ONLY = ('ino', 'applicationId', 'userVersion', 'pageSize', 'ddlSha256')
_PIN_INTS = ('dev', 'ino', 'size', 'mtimeNs', 'applicationId', 'userVersion',
             'pageSize', 'pageCount', 'freelistCount')
# Small binding/identity tables pinned row for row.
_IDENTITY_TABLES = frozenset(('schema_meta', 'head', 'queue_meta', 'meta', 'active',
                              'current', 'cached_backfill', 'sqlite_sequence'))
# v6 recall read leases and SQLite statistics are not index content.
_UNPINNED_TABLES = frozenset(('pins', 'sqlite_stat1', 'sqlite_stat2', 'sqlite_stat3',
                              'sqlite_stat4'))
_WITHOUT_ROWID = re.compile(r'\bWITHOUT\s+ROWID\b', re.IGNORECASE)
_SHA = re.compile(r'[0-9a-f]{64}\Z')


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
    """Count rows of a backup copy whose bytes the caller proved by sha256."""
    from laya_index_a import _query_only_uri
    with closing(sqlite3.connect(_query_only_uri(path), uri=True)) as db:
        db.execute('PRAGMA query_only=ON')
        return db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]


def _hash_private(path, progress=None):
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
            if progress is not None:
                progress.add(len(chunk))
        after = os.fstat(fd)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != \
                (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            _fail('inactive_upgrade_backup_changed')
        return digest.hexdigest()
    finally:
        os.close(fd)


_events = ContextVar('inactive_upgrade_events', default=None)


@contextmanager
def upgrade_events(**fields):
    """Tag this context's ``bundle.upgrade`` stage events (operation id, kind)."""
    current = _events.get() or {}
    token = _events.set({**current, **{key: value for key, value in fields.items()
                                       if value is not None}})
    try:
        yield
    finally:
        _events.reset(token)


def _stage(state, stage, reason=None, **fields):
    """One ``bundle.upgrade`` stage line in the install event log; never raises."""
    try:
        import event_log
        event_log.emit('bundle.upgrade', outcome=stage, reason=reason,
                       state_root=Path(state), **{**(_events.get() or {}), **fields})
    except Exception:  # noqa: BLE001 - diagnostics never fail an upgrade
        pass


class _Progress:
    """Byte progress of one long copy or hash; a line every PROGRESS_SECONDS."""

    def __init__(self, state, stage, total, **fields):
        self.state, self.stage, self.total, self.fields = state, stage, total, fields
        self.done = 0
        self.started = self.last = time.monotonic()

    def add(self, count):
        self.done += count
        now = time.monotonic()
        if now - self.last >= PROGRESS_SECONDS:
            self.last = now
            _stage(self.state, self.stage, bytesDone=self.done, bytesTotal=self.total,
                   ms=(now - self.started) * 1000, **self.fields)

    def ms(self):
        return (time.monotonic() - self.started) * 1000


def _quoted(name):
    return '"' + name.replace('"', '""') + '"'


def _pin_value(value):
    return {'blob': value.hex()} if isinstance(value, bytes) else value


def _store_paths(identity):
    """The pinned stores of an identity; the incremental sidecar when present."""
    from laya_incremental_docs import FILE as DOCS
    paths = {key: Path(identity[key]) for key in _DB_KEYS}
    sidecar = Path(identity['stateRoot']) / DOCS
    if sidecar.is_symlink():
        _fail('inactive_upgrade_store_unsafe')
    if sidecar.exists():
        paths['incrementalDocs'] = sidecar
    return paths


def _store_pin(path):
    """Identity and O(metadata) logical evidence of one store, one read snapshot.

    The evidence is the schema (application id, user version, DDL digest),
    the small identity tables row for row (``contentSha256``) and each rowid
    table's largest rowid, one b-tree descent per table (``tailSha256``).
    Nothing reads index content pages or counts rows.
    """
    from laya_index_a import _query_only_uri
    source = Path(path)
    before = source.lstat()
    if (source.is_symlink() or not stat.S_ISREG(before.st_mode) or
            before.st_uid != os.getuid() or before.st_nlink != 1 or
            stat.S_IMODE(before.st_mode) & 0o077):
        _fail('inactive_upgrade_store_unsafe')
    with closing(sqlite3.connect(_query_only_uri(source), uri=True,
                                 isolation_level=None, timeout=2)) as db:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        master = db.execute('SELECT type,name,tbl_name,sql FROM sqlite_master '
                            'ORDER BY type,name,tbl_name').fetchall()
        pragmas = {name: db.execute('PRAGMA ' + pragma).fetchone()[0] for name, pragma in
                   (('applicationId', 'application_id'), ('userVersion', 'user_version'),
                    ('pageSize', 'page_size'), ('pageCount', 'page_count'),
                    ('freelistCount', 'freelist_count'))}
        tables, identity = [], {}
        for kind, name, _table, sql in master:
            if (kind != 'table' or name in _UNPINNED_TABLES or
                    (sql or '').lstrip().upper().startswith('CREATE VIRTUAL')):
                continue
            if not _WITHOUT_ROWID.search((sql or '').rsplit(')', 1)[-1]):
                tables.append([name, db.execute(
                    'SELECT max(rowid) FROM ' + _quoted(name)).fetchone()[0]])
            if name in _IDENTITY_TABLES:
                rows = db.execute('SELECT * FROM ' + _quoted(name)).fetchall()
                identity[name] = sorted(([_pin_value(value) for value in row] for row in rows),
                                        key=lambda row: _canonical(row))
        db.execute('ROLLBACK')
    after = source.lstat()
    if (before.st_ino, before.st_dev) != (after.st_ino, after.st_dev):
        _fail('inactive_upgrade_backup_changed')
    return {'path': str(source), 'dev': before.st_dev, 'ino': before.st_ino,
            'size': before.st_size, 'mtimeNs': before.st_mtime_ns, **pragmas,
            'ddlSha256': hashlib.sha256(_canonical(master)).hexdigest(),
            'contentSha256': hashlib.sha256(_canonical(identity)).hexdigest(),
            'tailSha256': hashlib.sha256(_canonical(tables)).hexdigest()}


COUNTS_SCHEMA = 'laya-store-tails-v1'


def _store_counts(identity):
    """Read-only schema, binding and cache proof; no constructor migrations.

    O(metadata): no page scan, no full COUNT. The vector cache and published
    documents are pinned by their largest rowid (one b-tree descent), next to
    the schema digests and every binding row. Runs under per-user locks.
    """
    return _inspect_stores(identity, legacy=False)


def _legacy_store_counts(identity):
    """The counts intents of bundles up to 0.44.15 recorded (quick_check, COUNT)."""
    return _inspect_stores(identity, legacy=True)


def _counts_match(identity, expected):
    """The live stores still give ``expected``, in whichever form it was taken.

    Intents of older bundles carry exact row counts; only those resume
    through the O(data) legacy inspection.
    """
    current = _store_counts(identity)
    if current == expected:
        return True
    if isinstance(expected, dict) and expected.get('countsSchema') != COUNTS_SCHEMA:
        return _legacy_store_counts(identity) == expected
    return False


def _tail(db, table):
    return db.execute('SELECT max(rowid) FROM ' + table).fetchone()[0] or 0


def _inspect_stores(identity, *, legacy):
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
    started = time.monotonic()
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
        if legacy and db.execute('PRAGMA quick_check').fetchone() != ('ok',):
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
        # Chunk sets require a registered profile (insert triggers) and the
        # profile table is insert-only: one registered profile, the build's,
        # proves every set's profile without scanning the sets.
        profiles = {row[0] for row in db.execute('SELECT profile_id FROM vector_profiles')}
        if ((legacy or not profiles <= {identity['vectorProfileId']}) and
                db.execute('SELECT COUNT(*) FROM vector_chunk_sets WHERE profile_id<>?',
                           (identity['vectorProfileId'],)).fetchone()[0]):
            _fail('inactive_upgrade_vector_profile_changed')
        if legacy:
            sets = db.execute('SELECT COUNT(*) FROM vector_chunk_sets').fetchone()[0]
            chunks = db.execute('SELECT COUNT(*) FROM vector_chunks').fetchone()[0]
        else:
            sets, chunks = _tail(db, 'vector_chunk_sets'), _tail(db, 'vector_chunks')
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
            docs = (db.execute('SELECT COUNT(*) FROM docs').fetchone()[0] if legacy
                    else _tail(db, 'docs'))
    else:
        docs = 0
    _stage(identity['stateRoot'], 'store_check.done', 'legacy_counts' if legacy else None,
           ms=(time.monotonic() - started) * 1000)
    if legacy:
        return {'vectorChunkSets': sets, 'vectorChunks': chunks,
                'publishedDocuments': docs, 'storeSchemas': schemas}
    return {'countsSchema': COUNTS_SCHEMA, 'vectorChunkSetsTail': sets,
            'vectorChunksTail': chunks, 'publishedDocumentsTail': docs,
            'storeSchemas': schemas}


def _backup(identity, old_record, old_route, old_attestation, counts):
    """Journal snapshot and store pins while the one candidate writer is locked.

    No restore reads a store back from a backup: restores rebind metadata
    from the intent and rewind the journal by CAS. The stores are pinned
    instead of copied; a restore compares the pins (``_verify_store_pins``).
    The journal is copied: intents of older bundles without ``journalBefore``
    read their original journal row from it.
    """
    state = Path(identity['stateRoot'])
    final = state / (BACKUP + os.urandom(12).hex())
    staged = Path(tempfile.mkdtemp(prefix='.inactive-upgrade-backup-', dir=state))
    from laya_index_a import _query_only_uri
    source_path = Path(identity['journalPath'])
    info = source_path.lstat()
    if (source_path.is_symlink() or not stat.S_ISREG(info.st_mode) or
            info.st_uid != os.getuid() or info.st_nlink != 1 or
            stat.S_IMODE(info.st_mode) & 0o077):
        _fail('inactive_upgrade_store_unsafe')
    stores = _store_paths(identity)
    _stage(state, 'backup.start', backup=final.name, copies=1, pins=len(stores))
    progress = _Progress(state, 'backup.progress', info.st_size, backup=final.name)
    pins = {key: _store_pin(path) for key, path in stores.items()}
    target = staged / 'journal.sqlite'

    def copied(_status, remaining, total):
        progress.done = 0
        progress.add(max(total - remaining, 0) * page_size)
    with closing(sqlite3.connect(_query_only_uri(source_path), uri=True)) as source:
        source.execute('PRAGMA query_only=ON')
        page_size = source.execute('PRAGMA page_size').fetchone()[0]
        with closing(sqlite3.connect(target)) as copy:
            source.backup(copy, pages=4096, progress=copied)
            if copy.execute('PRAGMA integrity_check').fetchone() != ('ok',):
                _fail('inactive_upgrade_backup_invalid')
    os.chmod(target, 0o600)
    copied_bytes = target.stat().st_size
    digests = {'journal': _hash_private(target)}
    with target.open('rb') as stream:
        os.fsync(stream.fileno())
    receipt = {'schema': RECEIPT_V2,
               'record': old_record, 'routeSha256': hashlib.sha256(old_route).hexdigest(),
               'attestationSha256': hashlib.sha256(old_attestation).hexdigest(),
               'counts': counts, 'dbSha256': digests, 'storePins': pins}
    manifest = staged / 'manifest.json'
    with manifest.open('xb') as stream:
        os.chmod(manifest, 0o600)
        stream.write(_canonical(receipt))
        stream.flush()
        os.fsync(stream.fileno())
    _fsync(staged)
    if final.exists() or final.is_symlink():
        _fail('inactive_upgrade_backup_already_exists')
    # An interrupted private staging directory is kept for review; it is
    # never mistaken for the complete named backup.
    os.rename(staged, final)
    _fsync(state)
    _stage(state, 'backup.done', backup=final.name, copies=1, pins=len(pins),
           bytes=copied_bytes, ms=progress.ms())
    return final


def _read_receipt(intent):
    """The backup directory's receipt, checked against the intent.

    Accepts the full-copy receipt of older bundles (v1) and the pinned one
    (v2); anything else is ``inactive_upgrade_backup_invalid``. Live stores
    are not compared here: a settled intent's stores keep syncing.
    """
    from laya_product_install import _read_private
    state = Path(intent['oldRecord']['identity']['stateRoot'])
    backup = Path(intent['backup'])
    if (backup.parent != state or not backup.name.startswith(BACKUP) or
            backup.is_symlink()):
        _fail('inactive_upgrade_backup_invalid')
    try:
        info = backup.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or
                stat.S_IMODE(info.st_mode) & 0o077):
            _fail('inactive_upgrade_backup_invalid')
        receipt = json.loads(_read_private(backup / 'manifest.json', 65536))
    except (OSError, ValueError):
        _fail('inactive_upgrade_backup_invalid')
    if (not isinstance(receipt, dict) or
            receipt.get('schema') not in (RECEIPT_V1, RECEIPT_V2) or
            receipt.get('record') != intent['oldRecord'] or
            receipt.get('counts') != intent['counts'] or
            receipt.get('routeSha256') != hashlib.sha256(
                intent['oldRoute'].encode('utf-8')).hexdigest() or
            receipt.get('attestationSha256') != hashlib.sha256(
                intent['oldAttestation'].encode('utf-8')).hexdigest() or
            not isinstance(receipt.get('dbSha256'), dict) or
            any(not isinstance(digest, str) or not _SHA.fullmatch(digest)
                for digest in receipt['dbSha256'].values())):
        _fail('inactive_upgrade_backup_invalid')
    expected = _copy_keys(receipt)
    if set(receipt['dbSha256']) != expected:
        _fail('inactive_upgrade_backup_invalid')
    if receipt['schema'] == RECEIPT_V2:
        pins = receipt.get('storePins')
        if (set(receipt) != {'schema', 'record', 'routeSha256', 'attestationSha256',
                             'counts', 'dbSha256', 'storePins'} or
                not isinstance(pins, dict) or
                not set(_DB_KEYS) <= set(pins) <= set(_DB_KEYS) | {'incrementalDocs'}):
            _fail('inactive_upgrade_backup_invalid')
        for pin in pins.values():
            if (not isinstance(pin, dict) or
                    set(pin) != {'path', 'ddlSha256', 'contentSha256', 'tailSha256',
                                 *_PIN_INTS} or
                    not isinstance(pin['path'], str) or
                    any(type(pin[key]) is not int for key in _PIN_INTS) or
                    any(not isinstance(pin[key], str) or not _SHA.fullmatch(pin[key])
                        for key in ('ddlSha256', 'contentSha256', 'tailSha256'))):
                _fail('inactive_upgrade_backup_invalid')
    return receipt


def _copy_keys(receipt):
    """Store copies a receipt names: every store (v1) or the journal (v2)."""
    if receipt['schema'] == RECEIPT_V2:
        return {'journal'}
    keys = set(_DB_KEYS) | {'journal'}
    if 'incrementalDocs' in receipt['dbSha256']:
        keys.add('incrementalDocs')
    return keys


def _pin_mismatch(intent):
    """True when a pinned store no longer matches its pin; False for a full copy."""
    receipt = _read_receipt(intent)
    if receipt['schema'] == RECEIPT_V1:
        return False
    pins = receipt['storePins']
    paths = _store_paths(intent['oldRecord']['identity'])
    if set(paths) != set(pins):
        return True
    for key, path in paths.items():
        if pins[key]['path'] != str(path):
            return True
        live = _store_pin(path)
        fields = _PIN_IDENTITY_ONLY if key in _PIN_BOOKKEEPING else _PIN_COMPARED
        if any(live[field] != pins[key][field] for field in fields):
            return True
    return False


def _verify_store_pins(intent):
    """Fail closed unless every pinned store is the one the backup pinned.

    Called by restores before they rewind any metadata, so a changed store
    refuses with nothing rewound, and again after the rewind while the
    upgrade fence still stands. A full-copy backup of an older bundle has no pins;
    its restore keeps the counts recheck alone.
    """
    if _pin_mismatch(intent):
        _fail('inactive_upgrade_backup_changed')


def _pins_stale(intent):
    """A pre-fence intent's pins no longer match: take a new backup instead."""
    return _pin_mismatch(intent)


def _drop_backup(state, backup):
    """Delete a retired upgrade's own backup once its receipt is durable.

    Callers rename the intent to its receipt and fsync the state root first.
    A backup any live upgrade intent names, one outside the state root, a
    symlink or a directory holding anything but regular files is kept.
    Best effort: a failure is logged and never fails the finished upgrade.
    """
    state, path = Path(state), Path(backup)
    try:
        if (path.parent != state or not path.name.startswith(BACKUP) or
                path.is_symlink() or not path.is_dir()):
            _stage(state, 'backup.kept', 'backup_unsafe', backup=path.name)
            return False
        for name in LIVE_INTENTS:
            live = state / name
            if os.path.lexists(live):
                value = build._read_json(live, max_bytes=INTENT_LIMIT)
                if not isinstance(value, dict) or value.get('backup') == str(path):
                    _stage(state, 'backup.kept', 'backup_referenced', backup=path.name)
                    return False
        info = path.lstat()
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            _stage(state, 'backup.kept', 'backup_unsafe', backup=path.name)
            return False
        dfd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | getattr(os, 'O_NOFOLLOW', 0))
        try:
            names = os.listdir(dfd)
            sizes = [os.stat(name, dir_fd=dfd, follow_symlinks=False) for name in names]
            if any(not stat.S_ISREG(entry.st_mode) for entry in sizes):
                _stage(state, 'backup.kept', 'backup_unsafe', backup=path.name)
                return False
            for name in names:
                os.unlink(name, dir_fd=dfd)
        finally:
            os.close(dfd)
        os.rmdir(path)
        _fsync(state)
        _stage(state, 'backup.deleted', backup=path.name, files=len(names),
               bytes=sum(entry.st_size for entry in sizes))
        return True
    except Exception:  # noqa: BLE001 - the upgrade already settled
        _stage(state, 'backup.kept', 'backup_delete_failed', backup=path.name)
        return False


_backup_proofs = ContextVar('inactive_backup_proofs', default=None)


@contextmanager
def backup_proof_scope():
    """Reuse a backup proof only while its observed files remain unchanged.

    Nested scopes of one process share the outer cache, so the worker's
    retries reuse proofs whose observed file stamps did not move.
    """
    current = _backup_proofs.get()
    if current is not None and current[0] == os.getpid():
        yield
        return
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
    keys = _copy_keys(_read_receipt(intent))
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


def _verify_backup_receipt(intent):
    """The backup's receipt matches the intent and its copies are private files.

    For a settled intent archived in the same step that deletes its backup:
    no copy is re-hashed.
    """
    receipt = _read_receipt(intent)
    backup = Path(intent['backup'])
    for key in receipt['dbSha256']:
        path = backup / (key + '.sqlite')
        try:
            info = path.lstat()
        except OSError:
            _fail('inactive_upgrade_backup_changed')
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
                info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o077):
            _fail('inactive_upgrade_backup_changed')
    _stage(intent['oldRecord']['identity']['stateRoot'], 'verify.receipt',
           backup=backup.name, files=len(receipt['dbSha256']))


def _verify_backup_full(intent):
    """Require the complete private snapshot before changing any metadata.

    Every copy's sha256 is recomputed: all stores for a full-copy backup
    of an older bundle, the journal for a pinned one.
    """
    receipt = _read_receipt(intent)
    state = Path(intent['oldRecord']['identity']['stateRoot'])
    backup = Path(intent['backup'])
    paths = {}
    for key in sorted(receipt['dbSha256']):
        path = backup / (key + '.sqlite')
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
                info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o077):
            _fail('inactive_upgrade_backup_changed')
        paths[key] = (path, info.st_size)
    total = sum(size for _path, size in paths.values())
    _stage(state, 'verify.start', backup=backup.name, files=len(paths), bytes=total)
    progress = _Progress(state, 'verify.progress', total, backup=backup.name)
    for key, (path, _size) in paths.items():
        if _hash_private(path, progress) != receipt['dbSha256'][key]:
            _fail('inactive_upgrade_backup_changed')
        # No page scan here: _backup hashed these bytes only after they passed
        # integrity_check, so the matching sha256 already proves them.
    if (receipt['schema'] == RECEIPT_V1 and
            _sqlite_count(backup / 'candidateDb.sqlite', 'vector_chunk_sets') !=
            intent['counts'].get('vectorChunkSets')):
        _fail('inactive_upgrade_backup_invalid')
    _stage(state, 'verify.done', backup=backup.name, files=len(paths), bytes=total,
           ms=progress.ms())


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
    _drop_backup(state, intent['backup'])


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
            not _counts_match(new, intent['counts'])):
        _fail('inactive_upgrade_recheck_failed')
    original = intent['oldRecord']
    record = build.advance(record, phase=original['phase'],
                           blocked=original['blocked'],
                           progress=original['progress'],
                           freshness=original['freshness'])
    _retire_intent(state, intent, 'complete')
    _stage(state, 'activate.done', phase=record['phase'])
    return record


def _restore(intent, old_proof, new_proof):
    """Restore original metadata after a failed switch, while both locks hold."""
    state = Path(intent['oldRecord']['identity']['stateRoot'])
    old, new = intent['oldRecord']['identity'], intent['newIdentity']
    record = build.read(state)
    if record['phase'] != 'upgrading' or record['identity'] not in (old, new):
        _fail('inactive_upgrade_restore_state_changed')
    # A changed store refuses before anything is rewound; the same checks
    # repeat once the metadata is old again.
    if not _counts_match(old, intent['counts']):
        _fail('inactive_upgrade_restore_recheck_failed')
    _verify_store_pins(intent)
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
            not _counts_match(old, intent['counts'])):
        _fail('inactive_upgrade_restore_recheck_failed')
    # Recheck with the metadata old again; the fence stands until they match.
    _verify_store_pins(intent)
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
            if not _counts_match(record['identity'], intent['counts']):
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

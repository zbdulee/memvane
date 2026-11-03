"""Private per-document publication for initial builds and selected sync.

This is an incremental read source for the host, not an installer selection.
Each row becomes visible only after its complete vector chunk set is sealed in
the candidate index. The full A-int generation and its activation proof remain
the installer's separate completion gate. Selected sync uses the same writer.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import sqlite3
import stat
import sys
import time

from laya_index_a import IndexContractError
from laya_index_vector import LayaVectorIndex


SCHEMA = 'laya-incremental-docs-v1'
FILE = 'laya-incremental-docs.sqlite'
_FIXED_SCHEMA = None


def _fail(reason):
    raise IndexContractError(reason)


def _private_state(state_root):
    root = Path(state_root)
    info = root.lstat()
    if (not root.is_absolute() or root.is_symlink() or
            not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or
            stat.S_IMODE(info.st_mode) & 0o077):
        _fail('incremental_state_root_unsafe')
    return root


def _open(state_root, *, coordination=None, identity=None):
    path = _private_state(state_root) / FILE
    if coordination is not None:
        return _open_coordinated(path, coordination, identity,
                                 deadline=time.monotonic() + 2).db
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY |
                     getattr(os, 'O_NOFOLLOW', 0), 0o600)
    except FileExistsError:
        pass
    else:
        os.close(fd)
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
            info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o077):
        _fail('incremental_store_unsafe')
    db = sqlite3.connect(path.as_uri() + '?mode=rw', uri=True,
                         isolation_level=None, timeout=2)
    db.execute('PRAGMA trusted_schema=OFF')
    db.execute('PRAGMA foreign_keys=ON')
    db.execute('PRAGMA synchronous=FULL')
    db.execute('PRAGMA fullfsync=ON')
    db.execute('PRAGMA journal_mode=WAL')
    return db


def _open_coordinated(path, coordination, identity, *, deadline):
    if identity is None:
        _fail('incremental_identity_changed')
    from laya_reader_windows import open_owned
    expected = _expected_meta(identity)
    def validate(db):
        db.execute('BEGIN')
        try:
            if (db.execute('PRAGMA user_version').fetchone() != (1,) or
                    db.execute('SELECT * FROM meta').fetchall() != [expected]):
                _fail('incremental_identity_changed')
            db.commit()
        except BaseException:
            db.rollback()
            raise
    def initialize(db):
        db.execute('PRAGMA trusted_schema=OFF')
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA synchronous=FULL')
        db.execute('PRAGMA fullfsync=ON')
        db.execute('PRAGMA journal_mode=WAL')
    return open_owned(coordination, path=path, validate=validate,
                      initialize=initialize, deadline=deadline)


def _close(db, *, deadline=None):
    # Keep the legacy common path independent of journal/installer imports.
    # Coordinated opens load this module and keep the raw handle strongly owned.
    windows = sys.modules.get('laya_reader_windows')
    if windows is not None and windows.close_connection(db, deadline=deadline):
        return
    db.close()


def _expected_meta(identity):
    return (SCHEMA, identity['installId'], identity['scopeId'],
            identity['dbIncarnation'], identity['vectorProfileId'],
            identity['candidateDb'], identity['projectRoot'])


def _setup(db, identity, *, coordination=None):
    if coordination is not None:
        return _setup_managed(db, identity, coordination)
    expected = _expected_meta(identity)
    db.execute('BEGIN IMMEDIATE')
    try:
        db.execute('''CREATE TABLE IF NOT EXISTS meta(
            schema TEXT NOT NULL, install_id TEXT NOT NULL,
            scope_id TEXT NOT NULL, db_incarnation TEXT NOT NULL,
            profile_id TEXT NOT NULL, candidate_db TEXT NOT NULL,
            project_root TEXT NOT NULL)''')
        db.execute('''CREATE TABLE IF NOT EXISTS docs(
            path TEXT PRIMARY KEY, hash TEXT NOT NULL, title TEXT NOT NULL,
            body TEXT NOT NULL, profile_id TEXT NOT NULL,
            chunk_count INTEGER NOT NULL, scan_id TEXT NOT NULL)''')
        # Match the normal generation's filepath/title/body lexical fields.
        # A wiki filename query must work during the first and later fills.
        db.execute('CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts '
                   "USING fts5(path,title,body,tokenize='porter unicode61')")
        # A candidate prepared by an older bundle can have complete cached
        # vectors but no per-document read rows. This cursor is independent of
        # the vector-fill cursor, which must never be rewound to backfill reads.
        db.execute('''CREATE TABLE IF NOT EXISTS cached_backfill(
            id INTEGER PRIMARY KEY CHECK(id=1), scan_id TEXT NOT NULL,
            cursor TEXT NOT NULL)''')
        row = db.execute('SELECT * FROM meta').fetchall()
        if not row:
            db.execute('INSERT INTO meta VALUES(?,?,?,?,?,?,?)', expected)
        elif row != [expected]:
            _fail('incremental_identity_changed')
        if db.execute('PRAGMA user_version').fetchone()[0] not in (0, 1):
            _fail('incremental_schema_changed')
        db.execute('PRAGMA user_version=1')
        db.commit()
    except BaseException:
        db.rollback()
        raise


def _schema_rows(db):
    return db.execute('SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name').fetchall()


def _fixed_schema():
    global _FIXED_SCHEMA
    if _FIXED_SCHEMA is None:
        reference = sqlite3.connect(':memory:', isolation_level=None)
        try:
            # Shape only: the identity values do not enter the schema.
            _setup(reference, dict(installId='',scopeId='',dbIncarnation='',
                vectorProfileId='',candidateDb='',projectRoot=''))
            _FIXED_SCHEMA = _schema_rows(reference)
        finally:
            reference.close()
    return _FIXED_SCHEMA


@contextmanager
def _document_transaction(db, coordination, *, visible):
    if coordination is not None:
        from laya_reader_windows import write_connection
        with write_connection(db, coordination, visible=visible,
                deadline=time.monotonic() + (.05 if visible else 2)) as transaction:
            yield transaction
        return
    db.execute('BEGIN IMMEDIATE')
    try:
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise


def _setup_managed(db, identity, coordination):
    expected = _fixed_schema()
    without_cursor = [row for row in expected if row[1] != 'cached_backfill']
    with _document_transaction(db, coordination, visible=False) as transaction:
        if (db.execute('PRAGMA user_version').fetchone() != (1,)
                or db.execute('SELECT * FROM meta').fetchall() != [_expected_meta(identity)]):
            _fail('incremental_identity_changed')
        actual = _schema_rows(db)
        if actual == without_cursor:
            db.execute('''CREATE TABLE IF NOT EXISTS cached_backfill(
            id INTEGER PRIMARY KEY CHECK(id=1), scan_id TEXT NOT NULL,
            cursor TEXT NOT NULL)''')
            actual = _schema_rows(db)
        if actual != expected:
            _fail('incremental_schema_changed')
        stamp = _connection_stamp(transaction.owner, db)
    return stamp


def _source_owners(runtime, coordination):
    """Actual participant binding before any preparation SQL."""
    from laya_reader_windows import _component, connection_binding
    side_binding = _component(coordination)
    owners = (getattr(runtime.index, '_connection_owner', None),
              getattr(runtime.catalog, '_connection_owner', None))
    if any(owner is None for owner in owners):
        _fail('incremental_sources_uncoordinated')
    if coordination.role != 'sidecar':
        _fail('incremental_sources_uncoordinated')
    for role, owner, db in zip(('base', 'catalog'), owners,
                              (runtime.index.db, runtime.catalog.db)):
        connection_binding(owner, db)
        binding = _component(owner.component)
        if (owner.component.role != role or owner.component.install_id != coordination.install_id
                or binding[2] is not side_binding[2]):
            _fail('incremental_sources_uncoordinated')
    return owners


def _source_stamps(owners):
    from laya_reader_windows import ReaderWindowBusy
    result = []
    for owner in owners:
        db = owner.db
        # A surviving issued cursor may keep an implicit old read snapshot.
        # This writer boundary defers rather than finalizing caller cursors.
        if (owner.pending or owner.transaction is not None or db.in_transaction
                or any(not (getattr(cursor, '_window_closed', False) or getattr(cursor, '_window_exhausted', False))
                       for cursor in db._live_cursors.values())):
            raise ReaderWindowBusy('reader_writer_statement_active')
        result.append(_connection_stamp(owner, db))
    return tuple(result)


def _connection_stamp(owner, db):
    from laya_reader_windows import connection_binding
    return (connection_binding(owner, db),
        db.execute('PRAGMA data_version').fetchone()[0], db.total_changes,
        db.execute('PRAGMA schema_version').fetchone()[0])


def _source_rows(runtime, snapshot, path, source_hash):
    base, catalog = runtime.index.db, runtime.catalog
    return (base.execute('SELECT gen FROM head WHERE scope=?', (runtime.scope,)).fetchone(),
        catalog._publication_rows(),
        catalog.db.execute('SELECT sha256,size FROM scan_files WHERE seq=? AND path=?',
                           (snapshot.seq, path)).fetchone(),
        base.execute("SELECT kind,value FROM exclusions WHERE scope=? AND "
                     "((kind='path' AND value=?) OR (kind='hash' AND value=?)) ORDER BY kind,value",
                     (runtime.scope, path, source_hash)).fetchall())


def _recheck_sources(runtime, owners, stamps, rows, snapshot, path, source_hash):
    from laya_reader_windows import connection_binding, ReaderWindowBusy
    for owner, db in zip(owners, (runtime.index.db, runtime.catalog.db)):
        connection_binding(owner, db)
    if (_source_stamps(owners) != stamps
            or _source_rows(runtime, snapshot, path, source_hash) != rows
            or _source_stamps(owners) != stamps):
        raise ReaderWindowBusy('reader_sidecar_sources_changed')


_DOCUMENT_ROW = ('SELECT rowid,path,hash,title,body,profile_id,chunk_count,scan_id '
                 'FROM docs WHERE path=?')
_DOCUMENT_KEY = 'SELECT rowid,hash,profile_id,chunk_count,scan_id FROM docs WHERE path=?'


def _sidecar_open(identity, coordination):
    return (_open(identity['stateRoot']) if coordination is None else
            _open(identity['stateRoot'], coordination=coordination, identity=identity))


def _sidecar_setup(db, identity, coordination):
    return (_setup(db, identity) if coordination is None else
            _setup(db, identity, coordination=coordination))


def _sidecar_prepare(db, identity, setup_stamp, path):
    from laya_reader_windows import ReaderWindowBusy
    owner = setup_stamp[0][0]
    stamp = _connection_stamp(owner, db)
    if stamp[0] != setup_stamp[0] or stamp[3] != setup_stamp[3]:
        raise ReaderWindowBusy('reader_sidecar_changed')
    if (db.execute('PRAGMA user_version').fetchone() != (1,)
            or db.execute('SELECT * FROM meta').fetchall() != [_expected_meta(identity)]):
        _fail('incremental_identity_changed')
    row = db.execute(_DOCUMENT_ROW, (path,)).fetchone()
    if _connection_stamp(owner, db) != stamp:
        raise ReaderWindowBusy('reader_sidecar_changed')
    return stamp, row


def _recheck_sidecar(db, stamp, path, row):
    from laya_reader_windows import ReaderWindowBusy
    expected = None if row is None else (row[0], row[2], row[5], row[6], row[7])
    if (_connection_stamp(stamp[0][0], db) != stamp
            or db.execute(_DOCUMENT_KEY, (path,)).fetchone() != expected
            or _connection_stamp(stamp[0][0], db) != stamp):
        raise ReaderWindowBusy('reader_sidecar_changed')


def _base_owner(index, coordination):
    from laya_reader_windows import _component, connection_binding
    owner = getattr(index, '_connection_owner', None)
    side = _component(coordination)
    if owner is None:
        _fail('incremental_sources_uncoordinated')
    connection_binding(owner, index.db)
    base = _component(owner.component)
    if (coordination.role != 'sidecar' or owner.component.role != 'base'
            or coordination.install_id != owner.component.install_id or side[2] is not base[2]):
        _fail('incremental_sources_uncoordinated')
    return owner


def _recheck_base(index, owner, stamp, scope, base_gen):
    from laya_reader_windows import connection_binding, ReaderWindowBusy
    connection_binding(owner, index.db)
    if (_source_stamps((owner,)) != stamp
            or index.db.execute('SELECT gen FROM head WHERE scope=?', (scope,)).fetchone() != (base_gen,)
            or _source_stamps((owner,)) != stamp):
        raise ReaderWindowBusy('reader_sidecar_sources_changed')


def _preserve_pages(index, next_gen, base_gen, scope):
    after = ''
    while True:
        cursor = index.db.execute(
            'SELECT old.path,old.hash,old_body.body,new.hash,new_body.body '
            'FROM membership old JOIN membership new '
            'ON new.gen=? AND new.scope=old.scope AND new.path=old.path '
            'JOIN content old_body ON old_body.hash=old.hash '
            'JOIN content new_body ON new_body.hash=new.hash '
            'WHERE old.gen=? AND old.scope=? AND old.eligible=1 AND new.eligible=1 '
            'AND old.hash<>new.hash AND old.path>? ORDER BY old.path LIMIT 64',
            (next_gen, base_gen, scope, after))
        try:
            page = cursor.fetchall()
        finally:
            cursor.close()
        if not page:
            return
        yield from page
        after = page[-1][0]


def _verified(identity, path, body):
    from laya_host_response import _verified_wiki_row
    decision = {
        'projectRoot': identity['projectRoot'],
        'collections': identity['collections'],
        'collectionRoots': {item['name']: item['sourceRoot']
                            for item in identity['collections']},
    }
    return _verified_wiki_row({'path': path, 'body': body}, decision,
                              current_source=False)


class PublishedReader:
    """Read-only view of one identity's completed sidecar rows.

    A selected sync opens it once per wake. It never creates, migrates or
    writes the sidecar. An absent or unsafe file, another identity's sidecar
    or an unreadable row all answer "not published", so the caller falls
    back to the full ``publish_document`` path.
    """

    def __init__(self, identity):
        self.identity = identity
        self._db = None
        self._refused = False

    def _connect(self):
        if self._db is not None or self._refused:
            return self._db
        try:
            path = _private_state(self.identity['stateRoot']) / FILE
            info = path.lstat()
        except FileNotFoundError:
            return None  # the first publication of this wake may create it
        except (OSError, IndexContractError, KeyError, TypeError):
            self._refused = True
            return None
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
                info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o077):
            self._refused = True
            return None
        db = None
        try:
            db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True,
                                 isolation_level=None, timeout=2)
            db.execute('PRAGMA trusted_schema=OFF')
            if db.execute('SELECT * FROM meta').fetchall() != [_expected_meta(self.identity)]:
                raise IndexContractError('incremental_identity_changed')
        except (sqlite3.Error, IndexContractError, KeyError, TypeError):
            if db is not None:
                db.close()
            self._refused = True
            return None
        self._db = db
        return db

    def published(self, path, source_hash, title, profile_id):
        """True only for the exact completed row (hash, title and profile)."""
        db = self._connect()
        if db is None:
            return False
        try:
            row = db.execute('SELECT hash,title,profile_id FROM docs WHERE path=?',
                             (path,)).fetchone()
        except sqlite3.Error:
            return False
        return row == (source_hash, title, profile_id)

    def close(self):
        if self._db is not None:
            self._db.close()
            self._db = None


def publish_needed(reader, path, source_hash, title, body, profile_id, *,
                   identity, require_verified):
    """Whether ``publish_document`` could write anything for this member.

    An identical published row is a replay (``already_published``) and an
    unverified card under ``require_verified`` is never written, so neither
    needs the caller's route proof, journal fence or a sidecar transaction.
    """
    if reader is not None and reader.published(path, source_hash, title, profile_id):
        return False
    if require_verified and not _verified(identity, path, body):
        return False
    return True


def publish_document(runtime, identity, snapshot, path, source_hash,
                     title, body, profile_id, *, require_verified, coordination=None):
    """Atomically expose one wiki document after the candidate sealed its vectors.

    A failed or interrupted sidecar write never exposes an FTS-only document.
    The writer checks the live wiki file before publication. The reader checks
    the completed sidecar row and selected identity without reopening source
    files. Replays of the same document are idempotent.
    """
    owners = _source_owners(runtime, coordination) if coordination is not None else None
    stamps = _source_stamps(owners) if owners is not None else None
    from laya_index_host_route import wiki_index_roots
    if (profile_id != identity['vectorProfileId'] or
            runtime.scope != identity['scopeId'] or
            runtime.index.incarnation() != identity['dbIncarnation'] or
            str(runtime.index.path) != identity['candidateDb'] or
            not isinstance(path, str) or
            not any(path.startswith(name + '/') for name in
                    wiki_index_roots(identity['collections'])) or
            not isinstance(body, str) or
            hashlib.sha256(body.encode('utf-8')).hexdigest() != source_hash):
        _fail('incremental_document_identity_changed')
    catalog = runtime.catalog
    from laya_source_catalog import PendingMemberProof
    if catalog is None:
        _fail('incremental_snapshot_changed')
    if isinstance(snapshot, PendingMemberProof):
        catalog.require_pending_member(snapshot, path, source_hash)
    elif catalog.require_complete() != snapshot:
        _fail('incremental_snapshot_changed')
    found = catalog.lookup(snapshot, path)
    if found is None or found[0] != source_hash:
        _fail('incremental_source_changed')
    from laya_source_catalog import load_exclusions
    excluded_paths, excluded_hashes = load_exclusions(runtime.index, runtime.scope)
    if path in excluded_paths or source_hash in excluded_hashes:
        return {'status': 'excluded'}
    if catalog.read_verified(snapshot, path, source_hash).decode('utf-8') != body:
        _fail('incremental_source_changed')
    chunks = LayaVectorIndex(runtime.index).cached_chunk_set(
        source_hash, title, profile_id)
    if not chunks:
        _fail('incremental_vector_incomplete')
    if isinstance(snapshot, PendingMemberProof):
        catalog.require_pending_member(snapshot, path, source_hash)
    if require_verified and not _verified(identity, path, body):
        # A rewritten auto card is generated until the verifier stamps it.
        # Keep the last completed sidecar row searchable across that interval;
        # replacing it here would shadow the old head with an unusable draft.
        return {'status': 'unverified'}
    rows = _source_rows(runtime, snapshot, path, source_hash) if owners is not None else None
    if owners is not None:
        _recheck_sources(runtime, owners, stamps, rows, snapshot, path, source_hash)
    db = (_open(identity['stateRoot']) if coordination is None else
          _open(identity['stateRoot'], coordination=coordination, identity=identity))
    try:
        if coordination is None:
            _setup(db, identity)
        else:
            sidecar_stamp = _setup(db, identity, coordination=coordination)
        with _document_transaction(db, coordination, visible=True) as transaction:
            if owners is not None:
                if _connection_stamp(transaction.owner, db) != sidecar_stamp:
                    from laya_reader_windows import ReaderWindowBusy
                    raise ReaderWindowBusy('reader_sidecar_changed')
                _recheck_sources(runtime, owners, stamps, rows, snapshot, path, source_hash)
            old = db.execute('SELECT rowid,hash,title,body,profile_id,chunk_count '
                             'FROM docs WHERE path=?', (path,)).fetchone()
            wanted = (source_hash, title, body, profile_id, len(chunks))
            if old is not None and old[1:] == wanted:
                return {'status': 'already_published'}
            if old is None:
                rowid = db.execute('INSERT INTO docs VALUES(?,?,?,?,?,?,?)',
                                   (path, *wanted, snapshot.scan_id)).lastrowid
            else:
                rowid = old[0]
                db.execute('DELETE FROM docs_fts WHERE rowid=?', (rowid,))
                db.execute('UPDATE docs SET hash=?,title=?,body=?,profile_id=?, '
                           'chunk_count=?,scan_id=? WHERE rowid=?',
                           (*wanted, snapshot.scan_id, rowid))
            db.execute('INSERT INTO docs_fts(rowid,path,title,body) VALUES(?,?,?,?)',
                       (rowid, path, title, body))
    finally:
        _close(db)
    return {'status': 'published'}


def preserve_previous_head(index, identity, base_gen, next_gen, *, coordination=None):
    """Pin old verified rows before a full generation replaces them with drafts.

    The sidecar read path shadows a newly published head with these immutable
    old bytes until a verified replacement publishes through publish_document.
    This runs under the selected sync owner's queue lock, after next_gen has
    staged and sealed but before its head CAS. A failed pin stops that CAS.
    """
    from laya_host_response import _verified_wiki_row
    from laya_index_vector import LayaVectorIndex, qmd_markdown_title, verify_generation_seal
    if base_gen is None:
        return 0
    owner = _base_owner(index, coordination) if coordination is not None else None
    stamp = _source_stamps((owner,)) if owner is not None else None
    if (str(index.path) != identity['candidateDb'] or
            index.incarnation() != identity['dbIncarnation'] or
            index.db.execute('SELECT gen FROM head WHERE scope=?',
                             (identity['scopeId'],)).fetchone() != (base_gen,)):
        _fail('incremental_head_changed')
    seal = verify_generation_seal(index, base_gen)
    if seal['profile_id'] != identity['vectorProfileId']:
        _fail('incremental_head_profile_changed')
    decision = {'projectRoot': identity['projectRoot'],
                'collections': identity['collections'],
                'collectionRoots': {item['name']: item['sourceRoot']
                                    for item in identity['collections']}}
    rows = (_preserve_pages(index, next_gen, base_gen, identity['scopeId']) if owner is not None else index.db.execute(
        'SELECT old.path,old.hash,old_body.body,new.hash,new_body.body '
        'FROM membership old JOIN membership new '
        'ON new.gen=? AND new.scope=old.scope AND new.path=old.path '
        'JOIN content old_body ON old_body.hash=old.hash '
        'JOIN content new_body ON new_body.hash=new.hash '
        'WHERE old.gen=? AND old.scope=? AND old.eligible=1 AND new.eligible=1 '
        'AND old.hash<>new.hash ORDER BY old.path',
        (next_gen, base_gen, identity['scopeId'])))
    vectors = LayaVectorIndex(index)
    db = None
    pinned = 0
    try:
        for path, old_hash, old_body, new_hash, new_body in rows:
            old_row = {'path': path, 'body': old_body}
            new_row = {'path': path, 'body': new_body}
            if (_verified_wiki_row(new_row, decision, current_source=False) or
                    not _verified_wiki_row(old_row, decision, current_source=False)):
                continue
            if hashlib.sha256(old_body.encode('utf-8')).hexdigest() != old_hash:
                _fail('incremental_head_content_changed')
            title = qmd_markdown_title(old_body, path)
            chunks = vectors.cached_chunk_set(old_hash, title,
                                              identity['vectorProfileId'])
            if not chunks:
                _fail('incremental_old_vector_incomplete')
            if db is None:
                db = _sidecar_open(identity, coordination)
                setup_stamp = _sidecar_setup(db, identity, coordination)
            if owner is not None:
                side_stamp, prepared_row = _sidecar_prepare(db, identity, setup_stamp, path)
                existing = None if prepared_row is None else (prepared_row[0], prepared_row[2], prepared_row[4])
                newer = (existing is not None and existing[1] != old_hash and
                    _verified_wiki_row({'path': path, 'body': existing[2]}, decision, current_source=False))
            with _document_transaction(db, coordination, visible=True):
                if owner is not None:
                    _recheck_base(index, owner, stamp, identity['scopeId'], base_gen)
                    _recheck_sidecar(db, side_stamp, path, prepared_row)
                else:
                    existing = db.execute('SELECT rowid,hash,body FROM docs WHERE path=?', (path,)).fetchone()
                # A newer already verified sidecar row takes precedence over
                # the old head. Never downgrade it during claim replay.
                if (newer if owner is not None else (existing is not None and existing[1] != old_hash and
                        _verified_wiki_row({'path': path, 'body': existing[2]},
                                           decision, current_source=False))):
                    continue
                if existing is None:
                    rowid = db.execute('INSERT INTO docs VALUES(?,?,?,?,?,?,?)',
                                       (path, old_hash, title, old_body,
                                        identity['vectorProfileId'], len(chunks),
                                        'head-' + base_gen)).lastrowid
                elif existing[1] == old_hash and existing[2] == old_body:
                    continue
                else:
                    rowid = existing[0]
                    db.execute('DELETE FROM docs_fts WHERE rowid=?', (rowid,))
                    db.execute('UPDATE docs SET hash=?,title=?,body=?,profile_id=?, '
                               'chunk_count=?,scan_id=? WHERE rowid=?',
                               (old_hash, title, old_body, identity['vectorProfileId'],
                                len(chunks), 'head-' + base_gen, rowid))
                db.execute('INSERT INTO docs_fts(rowid,path,title,body) VALUES(?,?,?,?)',
                           (rowid, path, title, old_body))
            pinned += 1
    finally:
        if db is not None:
            _close(db)
    if owner is not None:
        _recheck_base(index, owner, stamp, identity['scopeId'], base_gen)
    elif index.db.execute('SELECT gen FROM head WHERE scope=?',
                        (identity['scopeId'],)).fetchone() != (base_gen,):
        _fail('incremental_head_changed')
    return pinned


def retire_missing_documents(runtime, identity, *, before_write, coordination=None):
    """Remove obsolete sidecar rows after a complete wiki catalog scan.

    This is owned by the index worker. A pending scan or a staged replacement
    cannot retire the previous completed row. After a head CAS, a newly
    verified head also retires an older pinned head; it must not be removed
    before the CAS, when the old completed bytes may still be needed.
    """
    owners = _source_owners(runtime, coordination) if coordination is not None else None
    stamps = _source_stamps(owners) if owners is not None else None
    if (runtime.scope != identity['scopeId'] or
            runtime.index.incarnation() != identity['dbIncarnation'] or
            str(runtime.index.path) != identity['candidateDb']):
        _fail('incremental_document_identity_changed')
    snapshot = runtime.catalog.require_complete()
    from laya_host_response import _verified_wiki_row
    from laya_source_catalog import load_exclusions
    excluded_paths, excluded_hashes = load_exclusions(runtime.index, runtime.scope)
    decision = {'projectRoot': identity['projectRoot'],
                'collections': identity['collections'],
                'collectionRoots': {item['name']: item['sourceRoot']
                                    for item in identity['collections']}}
    head = runtime.index.db.execute('SELECT gen FROM head WHERE scope=?',
                                    (runtime.scope,)).fetchone()
    db = _sidecar_open(identity, coordination)
    removed = 0
    try:
        setup_stamp = _sidecar_setup(db, identity, coordination)
        rows = db.execute('SELECT path,rowid,hash,scan_id FROM docs ORDER BY path').fetchall()
        for path, rowid, old_hash, scan_id in rows:
            member = runtime.catalog.lookup(snapshot, path)
            remove = (member is None or path in excluded_paths or old_hash in excluded_hashes
                      or (member is not None and member[0] in excluded_hashes))
            if not remove and isinstance(scan_id, str) and scan_id.startswith('head-') \
                    and head is not None:
                published = runtime.index.db.execute(
                    'SELECT member.hash,content.body FROM membership member '
                    'JOIN content ON content.hash=member.hash '
                    'WHERE member.gen=? AND member.scope=? AND member.path=? '
                    'AND member.eligible=1',
                    (head[0], runtime.scope, path)).fetchone()
                remove = (published is not None and published[0] != old_hash
                          and hashlib.sha256(published[1].encode('utf-8')).hexdigest()
                              == published[0]
                          and _verified_wiki_row({'path': path, 'body': published[1]},
                                                 decision, current_source=False))
            if not remove:
                continue
            if owners is not None:
                source_rows = _source_rows(runtime, snapshot, path, old_hash)
                side_stamp, prepared_row = _sidecar_prepare(db, identity, setup_stamp, path)
                if prepared_row is None or (prepared_row[0], prepared_row[2], prepared_row[7]) != (rowid, old_hash, scan_id):
                    continue
            before_write()
            with _document_transaction(db, coordination, visible=True):
                if owners is not None:
                    _recheck_sources(runtime, owners, stamps, source_rows, snapshot, path, old_hash)
                    _recheck_sidecar(db, side_stamp, path, prepared_row)
                if db.execute('SELECT rowid,hash,scan_id FROM docs WHERE path=?',
                              (path,)).fetchone() == (rowid, old_hash, scan_id):
                    db.execute('DELETE FROM docs_fts WHERE rowid=?', (rowid,))
                    db.execute('DELETE FROM docs WHERE rowid=?', (rowid,))
                    removed += 1
    finally:
        _close(db)
    return removed


def _backfill_cursor(runtime, identity, snapshot, db, coordination, setup_stamp,
                     expected, cursor):
    _source_owners(runtime, coordination)
    try:
        runtime.catalog.require_current(snapshot)
    except IndexContractError:
        return False
    side_stamp, absent = _sidecar_prepare(db, identity, setup_stamp, None)
    with _document_transaction(db, coordination, visible=False) as transaction:
        _recheck_sidecar(db, side_stamp, None, absent)
        prior = db.execute('SELECT scan_id,cursor FROM cached_backfill WHERE id=1').fetchone()
        if prior != expected:
            transaction.rollback()
            return False
        if prior is None:
            db.execute('INSERT INTO cached_backfill VALUES(1,?,?)', (snapshot.scan_id, cursor))
        else:
            changed = db.execute('UPDATE cached_backfill SET scan_id=?,cursor=? '
                'WHERE id=1 AND scan_id=? AND cursor=?',
                (snapshot.scan_id, cursor, *prior)).rowcount
            if changed != 1:
                transaction.rollback()
                return False
    return True


def backfill_cached(runtime, identity, profile_id, *, deadline, clock,
                    before_publish, should_yield, require_verified, coordination=None):
    """Publish older complete cached wiki members without re-embedding them.

    The separate cursor and each document's publication are durable. A crash
    between publication and cursor advance only replays an idempotent row.
    Uncached members are left for the ordinary fill callback. Its cursor
    restarts on a new catalog snapshot, while this cache-only pass resumes.
    """
    from laya_index_vector import LayaVectorIndex, qmd_markdown_title
    from laya_source_catalog import load_exclusions
    if coordination is not None:
        _source_stamps(_source_owners(runtime, coordination))
    if (profile_id != identity['vectorProfileId'] or
            runtime.scope != identity['scopeId'] or
            runtime.index.incarnation() != identity['dbIncarnation'] or
            str(runtime.index.path) != identity['candidateDb']):
        _fail('incremental_document_identity_changed')
    if runtime.catalog.status()['current'] is None:
        return {'state': 'complete', 'reason': 'no_complete_snapshot'}
    snapshot = runtime.catalog.require_complete()
    exclusions = load_exclusions(runtime.index, runtime.scope)
    vectors = LayaVectorIndex(runtime.index)
    db = _sidecar_open(identity, coordination)
    try:
        setup_stamp = _sidecar_setup(db, identity, coordination)
        prior = db.execute('SELECT scan_id,cursor FROM cached_backfill WHERE id=1').fetchone()
        # A product draft skipped before verification needs another attempt
        # after verification changes the wiki bytes and requests a new scan.
        # Revisit complete cached members on that new scan without embedding;
        # same-scan retries still resume the finite cursor. Synthetic callers
        # without the verification gate retain the original one-time backfill.
        cursor = (prior[1] if prior is not None and
                  (prior[0] == snapshot.scan_id or (coordination is None and not require_verified)) else '')
        if prior is None or prior[0] != snapshot.scan_id:
            if coordination is not None:
                if not _backfill_cursor(runtime, identity, snapshot, db, coordination,
                                        setup_stamp, prior, cursor):
                    return {'state': 'pending', 'reason': 'incremental_cursor_changed'}
            else:
                db.execute('BEGIN IMMEDIATE')
                db.execute('INSERT OR REPLACE INTO cached_backfill VALUES(1,?,?)',
                           (snapshot.scan_id, cursor))
                db.commit()
        while True:
            if clock() >= deadline:
                return {'state': 'pending', 'reason': 'incremental_backfill'}
            if should_yield():
                return {'state': 'deferred', 'reason': 'laya_sync_yield_priority'}
            try:
                rows = runtime.catalog.members(snapshot, exclusions, after=cursor,
                                               limit=64, allow_pending=True)
            except IndexContractError:
                return {'state': 'pending', 'reason': 'scan_changed'}
            if not rows:
                return {'state': 'complete', 'reason': 'incremental_backfill_complete'}
            fill_cursor = runtime.catalog.vector_fill(snapshot, profile_id)['cursor']
            for path, sha, eligible in rows:
                if clock() >= deadline:
                    return {'state': 'pending', 'reason': 'incremental_backfill'}
                if should_yield():
                    return {'state': 'deferred', 'reason': 'laya_sync_yield_priority'}
                before_publish()
                if eligible:
                    try:
                        body = runtime.catalog.read_verified(snapshot, path, sha).decode('utf-8')
                    except IndexContractError:
                        runtime.catalog.request_scan()
                        return {'state': 'pending', 'reason': 'source_changed'}
                    title = qmd_markdown_title(body, path)
                    if vectors.cached_chunk_set(sha, title, profile_id) is not None:
                        before_publish()
                        publish_document(runtime, identity, snapshot, path, sha,
                                         title, body, profile_id,
                                         require_verified=require_verified,
                                         **({} if coordination is None else {'coordination': coordination}))
                    elif fill_cursor and path <= fill_cursor:
                        _fail('incremental_cached_member_missing')
                # Never advance past a failed publication. An unverified draft
                # is intentionally skipped; verification rewrites its wiki
                # bytes and requests another scan, which restarts this cursor.
                # A repeated member with cached vectors needs no new model.
                before_publish()
                if coordination is not None:
                    if not _backfill_cursor(runtime, identity, snapshot, db, coordination,
                                            setup_stamp, (snapshot.scan_id, cursor), path):
                        return {'state': 'pending', 'reason': 'incremental_cursor_changed'}
                else:
                    db.execute('BEGIN IMMEDIATE')
                    db.execute('UPDATE cached_backfill SET cursor=? WHERE id=1', (path,))
                    db.commit()
                cursor = path
    finally:
        _close(db)

"""Private per-document publication for initial builds and selected sync.

This is an incremental read source for the host, not an installer selection.
Each row becomes visible only after its complete vector chunk set is sealed in
the candidate index. The full A-int generation and its activation proof remain
the installer's separate completion gate. Selected sync uses the same writer.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import sqlite3
import stat

from laya_index_a import IndexContractError
from laya_index_vector import LayaVectorIndex


SCHEMA = 'laya-incremental-docs-v1'
FILE = 'laya-incremental-docs.sqlite'


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


def _open(state_root):
    path = _private_state(state_root) / FILE
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


def _setup(db, identity):
    expected = (SCHEMA, identity['installId'], identity['scopeId'],
                identity['dbIncarnation'], identity['vectorProfileId'],
                identity['candidateDb'], identity['projectRoot'])
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


def publish_document(runtime, identity, snapshot, path, source_hash,
                     title, body, profile_id, *, require_verified):
    """Atomically expose one wiki document after the candidate sealed its vectors.

    A failed or interrupted sidecar write never exposes an FTS-only document.
    The writer checks the live wiki file before publication. The reader checks
    the completed sidecar row and selected identity without reopening source
    files. Replays of the same document are idempotent.
    """
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
    if require_verified:
        # A rewritten auto card is generated until the verifier stamps it.
        # Keep the last completed sidecar row searchable across that interval;
        # replacing it here would shadow the old head with an unusable draft.
        from laya_host_response import _verified_wiki_row
        decision = {
            'projectRoot': identity['projectRoot'],
            'collections': identity['collections'],
            'collectionRoots': {item['name']: item['sourceRoot']
                                for item in identity['collections']},
        }
        if not _verified_wiki_row({'path': path, 'body': body}, decision,
                                  current_source=False):
            return {'status': 'unverified'}
    db = _open(identity['stateRoot'])
    try:
        _setup(db, identity)
        db.execute('BEGIN IMMEDIATE')
        try:
            old = db.execute('SELECT rowid,hash,title,body,profile_id,chunk_count '
                             'FROM docs WHERE path=?', (path,)).fetchone()
            wanted = (source_hash, title, body, profile_id, len(chunks))
            if old is not None and old[1:] == wanted:
                db.commit()
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
            db.commit()
        except BaseException:
            db.rollback()
            raise
    finally:
        db.close()
    return {'status': 'published'}


def preserve_previous_head(index, identity, base_gen, next_gen):
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
    rows = index.db.execute(
        'SELECT old.path,old.hash,old_body.body,new.hash,new_body.body '
        'FROM membership old JOIN membership new '
        'ON new.gen=? AND new.scope=old.scope AND new.path=old.path '
        'JOIN content old_body ON old_body.hash=old.hash '
        'JOIN content new_body ON new_body.hash=new.hash '
        'WHERE old.gen=? AND old.scope=? AND old.eligible=1 AND new.eligible=1 '
        'AND old.hash<>new.hash ORDER BY old.path',
        (next_gen, base_gen, identity['scopeId']))
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
                db = _open(identity['stateRoot'])
                _setup(db, identity)
            db.execute('BEGIN IMMEDIATE')
            try:
                existing = db.execute('SELECT rowid,hash,body FROM docs WHERE path=?',
                                      (path,)).fetchone()
                # A newer already verified sidecar row takes precedence over
                # the old head. Never downgrade it during claim replay.
                if (existing is not None and existing[1] != old_hash and
                        _verified_wiki_row({'path': path, 'body': existing[2]},
                                           decision, current_source=False)):
                    db.commit()
                    continue
                if existing is None:
                    rowid = db.execute('INSERT INTO docs VALUES(?,?,?,?,?,?,?)',
                                       (path, old_hash, title, old_body,
                                        identity['vectorProfileId'], len(chunks),
                                        'head-' + base_gen)).lastrowid
                elif existing[1] == old_hash and existing[2] == old_body:
                    db.commit()
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
                db.commit()
                pinned += 1
            except BaseException:
                db.rollback()
                raise
    finally:
        if db is not None:
            db.close()
    if index.db.execute('SELECT gen FROM head WHERE scope=?',
                        (identity['scopeId'],)).fetchone() != (base_gen,):
        _fail('incremental_head_changed')
    return pinned


def retire_missing_documents(runtime, identity, *, before_write):
    """Remove obsolete sidecar rows after a complete wiki catalog scan.

    This is owned by the index worker. A pending scan or a staged replacement
    cannot retire the previous completed row. After a head CAS, a newly
    verified head also retires an older pinned head; it must not be removed
    before the CAS, when the old completed bytes may still be needed.
    """
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
    db = _open(identity['stateRoot'])
    removed = 0
    try:
        _setup(db, identity)
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
            before_write()
            db.execute('BEGIN IMMEDIATE')
            try:
                if db.execute('SELECT rowid,hash,scan_id FROM docs WHERE path=?',
                              (path,)).fetchone() == (rowid, old_hash, scan_id):
                    db.execute('DELETE FROM docs_fts WHERE rowid=?', (rowid,))
                    db.execute('DELETE FROM docs WHERE rowid=?', (rowid,))
                    removed += 1
                db.commit()
            except BaseException:
                db.rollback()
                raise
    finally:
        db.close()
    return removed


def backfill_cached(runtime, identity, profile_id, *, deadline, clock,
                    before_publish, should_yield, require_verified):
    """Publish older complete cached wiki members without re-embedding them.

    The separate cursor and each document's publication are durable. A crash
    between publication and cursor advance only replays an idempotent row.
    Uncached members are left for the ordinary fill callback. Its cursor
    restarts on a new catalog snapshot, while this cache-only pass resumes.
    """
    from laya_index_vector import LayaVectorIndex, qmd_markdown_title
    from laya_source_catalog import load_exclusions
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
    db = _open(identity['stateRoot'])
    try:
        _setup(db, identity)
        prior = db.execute('SELECT scan_id,cursor FROM cached_backfill WHERE id=1').fetchone()
        # A product draft skipped before verification needs another attempt
        # after verification changes the wiki bytes and requests a new scan.
        # Revisit complete cached members on that new scan without embedding;
        # same-scan retries still resume the finite cursor. Synthetic callers
        # without the verification gate retain the original one-time backfill.
        cursor = (prior[1] if prior is not None and
                  (prior[0] == snapshot.scan_id or not require_verified) else '')
        if prior is None or prior[0] != snapshot.scan_id:
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
                                         require_verified=require_verified)
                    elif fill_cursor and path <= fill_cursor:
                        _fail('incremental_cached_member_missing')
                # Never advance past a failed publication. An unverified draft
                # is intentionally skipped; verification rewrites its wiki
                # bytes and requests another scan, which restarts this cursor.
                # A repeated member with cached vectors needs no new model.
                before_publish()
                db.execute('BEGIN IMMEDIATE')
                db.execute('UPDATE cached_backfill SET cursor=? WHERE id=1', (path,))
                db.commit()
                cursor = path
    finally:
        db.close()

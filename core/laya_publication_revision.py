"""Append-only evidence for coordinated visible base publications.

These receipts do not establish a baseline or grant a reader capability.
The caller owns the SQLite write transaction and publication cut.
"""
import hashlib
from itertools import zip_longest
import json
import sqlite3


_PREFIX = 'reader-revision:base:'
_MAX = 10**20 - 1


def _identity(component, fields):
    raw = json.dumps([component, *fields], ensure_ascii=True,
                     separators=(',', ':'), allow_nan=False).encode('ascii')
    return hashlib.sha256(raw).hexdigest()


def _record(db, *, install_id, scope, before, after, transition):
    incarnation = db.execute("SELECT value FROM schema_meta WHERE key='incarnation'").fetchone()
    if incarnation is None or not isinstance(incarnation[0], str) or not incarnation[0]:
        raise ValueError('invalid_publication_revision_identity')
    if not isinstance(install_id, str) or not install_id:
        raise ValueError('invalid_publication_revision_identity')
    identity = _identity('base', (install_id, scope, incarnation[0]))
    row = db.execute('SELECT key,value FROM schema_meta WHERE key>=? AND key<? '
                     'ORDER BY key DESC LIMIT 1', (_PREFIX, _PREFIX + '\uffff')).fetchone()
    revision = 1
    if row is not None:
        suffix = row[0][len(_PREFIX):]
        if len(suffix) != 20 or not suffix.isascii() or not suffix.isdigit():
            raise ValueError('invalid_publication_revision')
        revision = int(suffix) + 1
    if revision > _MAX:
        raise ValueError('publication_revision_exhausted')
    if transition[0] == 'head':
        epoch = db.execute('SELECT rev_epoch FROM generations WHERE scope=? AND gen=?',
                           (scope,after)).fetchone()
        if epoch is None or type(epoch[0]) is not int or epoch[0] < 0:
            raise ValueError('invalid_publication_revision')
        exclusion_count = epoch[0]
    elif row is None:
        # An installer-only write without a baseline cannot prove a count.
        exclusion_count = None
    else:
        try:
            previous = json.loads(row[1])
            count = previous['exclusionCount']
            if previous.get('component') != 'base' or previous.get('identity') != identity:
                raise ValueError('invalid_publication_revision')
            if count is not None and (type(count) is not int or count < 0):
                raise ValueError('invalid_publication_revision')
            exclusion_count = None if count is None else count + 1
        except (TypeError,ValueError,KeyError) as exc:
            raise ValueError('invalid_publication_revision') from exc
    transition = json.dumps(transition, ensure_ascii=True,
                            separators=(',', ':'), allow_nan=False).encode('ascii')
    receipt = {'component': 'base', 'identity': identity, 'before': before, 'after': after,
               'exclusionCount': exclusion_count,
               'changeDigest': hashlib.sha256(transition).hexdigest()}
    value = json.dumps(receipt, sort_keys=True, separators=(',', ':'), ensure_ascii=True)
    db.execute('INSERT INTO schema_meta(key,value) VALUES(?,?)',
               (_PREFIX + f'{revision:020d}', value))
    return revision


def record_head(db, *, install_id, scope, before, after, claim, proof):
    """Record a head transition in the caller's existing transaction."""
    if (not db.in_transaction or not isinstance(scope, str) or not scope
            or (before is not None and (not isinstance(before, str) or not before))
            or not isinstance(after, str) or not after
            or not isinstance(claim, str) or not claim
            or not isinstance(proof, tuple) or len(proof) != 3):
        raise ValueError('invalid_publication_revision')
    return _record(db, install_id=install_id, scope=scope, before=before, after=after,
                   transition=['head', scope, before, after, claim, *proof])


def record_exclusion(db, *, install_id, scope, head, kind, value):
    """Record one newly inserted serving exclusion in its transaction."""
    if (not db.in_transaction or not isinstance(scope, str) or not scope
            or (head is not None and (not isinstance(head, str) or not head))
            or kind not in ('path', 'hash') or not isinstance(value, str) or not value):
        raise ValueError('invalid_publication_revision')
    return _record(db, install_id=install_id, scope=scope, before=head, after=head,
                   transition=['exclusion', scope, head, kind, value])


def ensure_base_baseline(index, *, scope, profile_id, deadline):
    """Seal one managed base image; this is not a cross-store/cohort proof."""
    import time
    from laya_index_a import _gen_fts
    from laya_index_vector import verify_generation_seal
    import laya_reader_windows as windows
    owner = index._connection_owner
    if owner is None or owner.component.role != 'base':
        raise ValueError('base_baseline_unowned')
    windows.connection_binding(owner, index.db)
    key = _PREFIX + '0' * 20
    with index._write_transaction(visible=False):
        db = index.db
        incarnation = db.execute("SELECT value FROM schema_meta WHERE key='incarnation'").fetchone()
        if incarnation is None or not isinstance(incarnation[0], str):
            raise ValueError('invalid_base_baseline')
        identity = _identity('base', (owner.component.install_id, scope, incarnation[0]))
        existing = db.execute('SELECT value FROM schema_meta WHERE key=?', (key,)).fetchone()
        if existing is not None:
            try:
                value = json.loads(existing[0])
                canonical = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True)
                fields = {'component','kind','identity','head','headExclusionEpoch',
                          'exclusionCount','exclusionDigest','profile','vectorDigest'}
                hex_digest = lambda text: (isinstance(text, str) and len(text) == 64
                                           and all(ch in '0123456789abcdef' for ch in text))
                valid = (isinstance(value, dict) and set(value) == fields
                         and value.get('component') == 'base'
                         and value.get('kind') == 'baseline' and value.get('identity') == identity
                         and isinstance(value.get('head'), str) and bool(value['head'])
                         and type(value.get('headExclusionEpoch')) is int and value['headExclusionEpoch'] >= 0
                         and type(value.get('exclusionCount')) is int and value['exclusionCount'] >= 0
                         and hex_digest(value.get('exclusionDigest')) and hex_digest(value.get('vectorDigest'))
                         and value.get('profile') == profile_id and canonical == existing[0])
            except (TypeError, ValueError):
                valid = False
            if not valid:
                raise ValueError('invalid_base_baseline')
            return
        if db.execute('SELECT 1 FROM schema_meta WHERE key>=? AND key<? LIMIT 1',
                      (_PREFIX, _PREFIX+'\uffff')).fetchone() is not None:
            raise ValueError('base_baseline_missing')
        head = db.execute('SELECT h.gen,g.state,g.rev_epoch,i.ord FROM head h '
                          'JOIN generations g ON g.gen=h.gen AND g.scope=h.scope '
                          'JOIN gen_index i ON i.gen=h.gen WHERE h.scope=?', (scope,)).fetchone()
        if head is None or head[1] != 'published' or type(head[3]) is not int:
            raise ValueError('invalid_base_baseline')
        if time.monotonic() >= deadline or not index._generation_fts_matches(head[0], deadline=deadline):
            raise ValueError('invalid_base_baseline')
        table = _gen_fts(head[3])
        try:
            db.execute(f"INSERT INTO {table}({table}) VALUES('integrity-check')")
        except sqlite3.DatabaseError as exc:
            raise ValueError('invalid_base_baseline') from exc
        vector = verify_generation_seal(index, head[0], deadline=deadline)
        if vector['profile_id'] != profile_id:
            raise ValueError('invalid_base_baseline')
        digest = hashlib.sha256(b'memvane-base-exclusions-v1\0')
        count = 0
        for row in db.execute('SELECT kind,value FROM exclusions WHERE scope=? ORDER BY kind,value', (scope,)):
            if time.monotonic() >= deadline:
                raise ValueError('base_baseline_deadline')
            raw = json.dumps(row, ensure_ascii=True, separators=(',', ':')).encode('ascii')
            digest.update(len(raw).to_bytes(8, 'big'));digest.update(raw);count += 1
        if time.monotonic() >= deadline:
            raise ValueError('base_baseline_deadline')
        value = {'component':'base', 'kind':'baseline', 'identity':identity,
                 'head':head[0], 'headExclusionEpoch':head[2], 'exclusionCount':count,
                 'exclusionDigest':digest.hexdigest(), 'profile':profile_id,
                 'vectorDigest':vector['rows_digest']}
        db.execute('INSERT INTO schema_meta(key,value) VALUES(?,?)',
                   (key,json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True)))


def sidecar_row(db, path):
    return db.execute('SELECT path,hash,title,body,profile_id,chunk_count,scan_id '
                      'FROM docs WHERE path=?', (path,)).fetchone()


def _sidecar_row_digest(row):
    if row is None:
        return None
    encoded=json.dumps(row,ensure_ascii=True,separators=(',', ':'),allow_nan=False).encode('ascii')
    return hashlib.sha256(encoded).hexdigest()


def _sidecar_state(state, before, after):
    def number(value):
        if value is None:
            return 0
        if not isinstance(value,str) or len(value)!=64 or any(c not in '0123456789abcdef' for c in value):
            raise ValueError('invalid_publication_revision')
        return int(value,16)
    return f'{number(state) ^ number(before) ^ number(after):064x}'


def ensure_sidecar_baseline(db, *, identity):
    """Seal the locally consistent initial sidecar, without granting read authority."""
    if not db.in_transaction or not isinstance(identity, tuple) or len(identity) != 7:
        raise ValueError('invalid_sidecar_baseline')
    if db.execute('SELECT * FROM meta').fetchall() != [identity]:
        raise ValueError('invalid_sidecar_baseline')
    identity_hash = _identity('sidecar', identity)
    existing = db.execute('SELECT value FROM publication_revisions WHERE revision=0').fetchone()
    if existing is not None:
        try:
            value = json.loads(existing[0])
            canonical = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True)
            valid = (isinstance(value, dict) and set(value) == {'component', 'kind', 'identity', 'rows', 'digest', 'stateDigest'}
                     and value['component'] == 'sidecar' and value['kind'] == 'baseline'
                     and value['identity'] == identity_hash and type(value['rows']) is int
                     and 0 <= value['rows'] <= 20000 and isinstance(value['digest'], str)
                     and len(value['digest']) == 64 and all(c in '0123456789abcdef' for c in value['digest'])
                     and isinstance(value['stateDigest'],str) and len(value['stateDigest'])==64
                     and all(c in '0123456789abcdef' for c in value['stateDigest'])
                     and canonical == existing[0])
        except (TypeError, ValueError):
            valid = False
        if not valid:
            raise ValueError('invalid_sidecar_baseline')
        return
    if db.execute('SELECT 1 FROM publication_revisions LIMIT 1').fetchone() is not None:
        raise ValueError('sidecar_baseline_missing')
    # Content-table reads alone cannot prove that MATCH sees those rows.
    # FTS5 checks its actual index inside this already-owned transaction.
    try:
        db.execute("INSERT INTO docs_fts(docs_fts) VALUES('integrity-check')")
    except sqlite3.DatabaseError as exc:
        raise ValueError('invalid_sidecar_baseline') from exc
    digest = hashlib.sha256(b'memvane-sidecar-baseline-v1\0')
    docs = db.execute('SELECT path,hash,title,body,profile_id,chunk_count,scan_id FROM docs ORDER BY path')
    fts = db.execute('SELECT path,title,body FROM docs_fts ORDER BY path')
    count = 0
    state_digest='0'*64
    for document, search in zip_longest(docs, fts):
        if document is None or search is None:
            raise ValueError('invalid_sidecar_baseline')
        path, source_hash, title, body, profile, chunks, scan = document
        if (not all(isinstance(v, str) and v for v in (path, source_hash, profile))
                or not all(isinstance(v, str) for v in (title, body, scan))
                or profile != identity[4]
                or type(chunks) is not int or chunks < 1
                or hashlib.sha256(body.encode('utf8')).hexdigest() != source_hash
                or search != (path, title, body)):
            raise ValueError('invalid_sidecar_baseline')
        count += 1
        if count > 20000:
            raise ValueError('invalid_sidecar_baseline')
        encoded = json.dumps(document, ensure_ascii=True, separators=(',', ':'), allow_nan=False).encode('ascii')
        digest.update(len(encoded).to_bytes(8, 'big'))
        digest.update(encoded)
        state_digest=_sidecar_state(state_digest,None,_sidecar_row_digest(document))
    value = {'component': 'sidecar', 'kind': 'baseline', 'identity': identity_hash,
             'rows': count, 'digest': digest.hexdigest(), 'stateDigest':state_digest}
    db.execute('INSERT INTO publication_revisions(revision,value) VALUES(0,?)',
               (json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True),))


def record_sidecar(db, *, path, before):
    """Commit an opaque row transition beside the sidecar mutation."""
    if not db.in_transaction or not isinstance(path, str) or not path:
        raise ValueError('invalid_publication_revision')
    meta = db.execute('SELECT * FROM meta').fetchall()
    if (len(meta) != 1 or len(meta[0]) != 7
            or any(not isinstance(value, str) or not value for value in meta[0])):
        raise ValueError('invalid_publication_revision_identity')
    identity = _identity('sidecar', meta[0])
    after = sidecar_row(db, path)
    if before == after:
        raise ValueError('unchanged_publication_revision')
    previous, current = _sidecar_row_digest(before), _sidecar_row_digest(after)
    transition = json.dumps(['sidecar', path, previous, current], ensure_ascii=True,
                            separators=(',', ':')).encode('ascii')
    row = db.execute('SELECT revision,value FROM publication_revisions ORDER BY revision DESC LIMIT 1').fetchone()
    if row is None or type(row[0]) is not int or row[0] < 0:
        raise ValueError('sidecar_baseline_missing')
    try:
        last=json.loads(row[1])
        if not isinstance(last,dict):
            raise ValueError('invalid_publication_revision')
        old_state=last['stateDigest'] if row[0]==0 else last['stateAfter']
        old_count=last['rows'] if row[0]==0 else last['rowsAfter']
        if (last.get('component')!='sidecar' or last.get('identity')!=identity
                or not isinstance(old_state,str) or len(old_state)!=64
                or any(c not in '0123456789abcdef' for c in old_state)
                or type(old_count) is not int or not 0<=old_count<=20000
                or json.dumps(last,sort_keys=True,separators=(',', ':'),ensure_ascii=True)!=row[1]):
            raise ValueError('invalid_publication_revision')
        new_state=_sidecar_state(old_state,previous,current)
        new_count=old_count+int(after is not None)-int(before is not None)
        if not 0<=new_count<=20000 or db.execute('SELECT count(*) FROM docs').fetchone()!=(new_count,):
            raise ValueError('invalid_publication_revision')
    except (KeyError,TypeError,ValueError) as exc:
        raise ValueError('invalid_publication_revision') from exc
    revision = row[0] + 1
    if type(revision) is not int or not 1 <= revision <= 2**63 - 1:
        raise ValueError('invalid_publication_revision')
    value = json.dumps({'component': 'sidecar', 'identity': identity, 'before': previous, 'after': current,
                        'stateBefore':old_state,'stateAfter':new_state,
                        'rowsBefore':old_count,'rowsAfter':new_count,
                        'changeDigest': hashlib.sha256(transition).hexdigest()},
                       sort_keys=True, separators=(',', ':'), ensure_ascii=True)
    db.execute('INSERT INTO publication_revisions(revision,value) VALUES(?,?)', (revision, value))
    return revision

"""Generation-owned FTS5 commitments for the inactive candidate format.

Shadow values are opaque SQLite storage commitments, not an FTS format parser.
No shadow table is written directly. Prepared statements are uncached and the
internal build capability is connection/generation/thread specific.
"""
from contextlib import contextmanager
from functools import lru_cache
import hashlib
import math
import re
import sqlite3
import threading
import time

import laya_blob_values as v

PREFIX='wiki_fts_'
_FIELDS=('filepath','title','body','path_key')
MAX_ROWS=20000
MAX_FTS_BYTES=128*1024*1024


def table(ordinal, *, sidecar=False):
    if type(ordinal) is not int or not 1 <= ordinal <= 2**63-1:
        v.fail('invalid_fts_ordinal')
    if type(sidecar) is not bool:v.fail('invalid_fts_ordinal')
    return PREFIX+('side_' if sidecar else '')+str(ordinal)


def head_table(view_id, *, sidecar=False):
    v.identifier(view_id)
    if type(sidecar) is not bool:v.fail('invalid_fts_view')
    return PREFIX+('hs_' if sidecar else 'h_')+view_id


def head_ddl(view_id, *, sidecar=False):
    return (f'CREATE VIRTUAL TABLE {head_table(view_id,sidecar=sidecar)} USING '
            "fts5(filepath,title,body,path_key UNINDEXED,tokenize='porter unicode61')")


def _ddl(ordinal, *, sidecar=False):
    return (f'CREATE VIRTUAL TABLE {table(ordinal,sidecar=sidecar)} USING '
            "fts5(filepath,title,body,path_key UNINDEXED,tokenize='porter unicode61')")


def _execute(db,sql,args=()):
    cursor=db.execute(sql,args)
    try:return cursor.fetchall()
    finally:cursor.close()


def _deadline(deadline):
    if deadline is not None and time.monotonic() >= deadline:
        v.fail('fts_deadline_exceeded')


class ConnectionGuard:
    """Internal FTS write capability; callers cannot submit arbitrary SQL."""
    def __init__(self,db):
        self._db=db
        self._active=None
        self._thread=threading.get_ident()
        db.set_authorizer(self._authorize)

    def _authorize(self,action,a,b,database,origin):
        if action in (sqlite3.SQLITE_ATTACH,sqlite3.SQLITE_DETACH):
            return sqlite3.SQLITE_DENY
        if action==sqlite3.SQLITE_PRAGMA and isinstance(a,str):
            key=a.lower()
            if key in ('writable_schema','trusted_schema'):
                return sqlite3.SQLITE_DENY
            if key in ('foreign_keys','recursive_triggers') and b is not None and str(b).lower() not in ('1','on','true'):
                return sqlite3.SQLITE_DENY
        if action==sqlite3.SQLITE_FUNCTION and str(b).lower()=='load_extension':
            return sqlite3.SQLITE_DENY
        if action in (sqlite3.SQLITE_CREATE_VIEW,sqlite3.SQLITE_CREATE_TEMP_VIEW):
            return sqlite3.SQLITE_DENY
        targets=[x for x in (a,b) if isinstance(x,str) and x.startswith(PREFIX)]
        if not targets:return sqlite3.SQLITE_OK
        if action in (sqlite3.SQLITE_CREATE_TRIGGER,sqlite3.SQLITE_CREATE_TEMP_TRIGGER):
            return sqlite3.SQLITE_DENY  # shadow triggers are never supported
        writes={sqlite3.SQLITE_INSERT,sqlite3.SQLITE_UPDATE,sqlite3.SQLITE_DELETE,
                sqlite3.SQLITE_CREATE_TABLE,sqlite3.SQLITE_DROP_TABLE,
                sqlite3.SQLITE_CREATE_INDEX,sqlite3.SQLITE_DROP_INDEX,
                sqlite3.SQLITE_CREATE_VTABLE,sqlite3.SQLITE_DROP_VTABLE,
                sqlite3.SQLITE_ALTER_TABLE}
        if action not in writes:return sqlite3.SQLITE_OK
        if self._active is None or threading.get_ident()!=self._thread:
            return sqlite3.SQLITE_DENY
        if isinstance(self._active,tuple):
            base=head_table(self._active[1])
            side=head_table(self._active[1],sidecar=True)
        else:
            base=table(self._active)
            side=table(self._active,sidecar=True)
        return (sqlite3.SQLITE_OK if all(x==base or x.startswith(base+'_') or
                                          x==side or x.startswith(side+'_') for x in targets)
                else sqlite3.SQLITE_DENY)

    @contextmanager
    def building(self,ordinal):
        table(ordinal)
        if self._active is not None or threading.get_ident()!=self._thread:
            v.fail('fts_build_capability_invalid')
        rows=_execute(self._db,
            "SELECT COALESCE(g.state,'building') FROM generation_slots s LEFT JOIN generations g ON g.ordinal=s.ordinal WHERE s.ordinal=?",(ordinal,))
        if len(rows)!=1 or rows[0][0]!='building':v.fail('fts_generation_not_building')
        self._active=ordinal
        self._db.set_authorizer(self._authorize)
        try:
            yield
            # FTS5 flushes pending segments during COMMIT; keep this exact
            # connection/generation capability until its sync has completed.
            self._db.commit()
        except BaseException:
            self._db.rollback()
            raise
        finally:
            self._active=None
            self._db.set_authorizer(self._authorize)

    @contextmanager
    def view_head(self,view_id):
        """Hold one view's FTS capability while the caller commits.

        The caller must separately establish owner/writer proof and validate
        head identity, schema, and seal; this capability only scopes writes.
        """
        head_table(view_id)
        if self._active is not None or threading.get_ident()!=self._thread or not self._db.in_transaction:
            v.fail('fts_build_capability_invalid')
        self._active=('head',view_id)
        self._db.set_authorizer(self._authorize)
        try:
            yield
            if self._db.in_transaction:
                v.fail('fts_transaction_unfinished')
        except BaseException:
            self._db.rollback()
            raise
        finally:
            self._active=None
            self._db.set_authorizer(self._authorize)


def create(db,ordinal,*,sidecar=False):
    table(ordinal,sidecar=sidecar)
    rows=_execute(db,
        "SELECT COALESCE(g.state,'building') FROM generation_slots s LEFT JOIN generations g ON g.ordinal=s.ordinal WHERE s.ordinal=?",(ordinal,))
    if len(rows)!=1 or rows[0][0]!='building':v.fail('fts_generation_not_building')
    _execute(db,_ddl(ordinal,sidecar=sidecar))


def _entries(rows, *, insertion_order=False):
    if not isinstance(rows,list) or len(rows)>MAX_ROWS:v.fail('fts_rows_invalid')
    seen=set();result=[];size=0
    for row in rows:
        v.exact(row,_FIELDS)
        v.relative(row['path_key'])
        if row['path_key'] in seen:v.fail('fts_rows_invalid')
        seen.add(row['path_key'])
        if any(not isinstance(row[k],str) for k in _FIELDS):v.fail('fts_rows_invalid')
        try:size+=sum(len(row[k].encode('utf8')) for k in _FIELDS)
        except UnicodeError:v.fail('fts_rows_invalid')
        if size>MAX_FTS_BYTES:v.fail('fts_rows_too_large')
        result.append(tuple(row[k] for k in _FIELDS))
    return result if insertion_order else sorted(result,key=lambda r:r[3])


def populate(db,ordinal,rows,*,sidecar=False):
    for row in _entries(rows,insertion_order=sidecar):
        _execute(db,f'INSERT INTO {table(ordinal,sidecar=sidecar)}(filepath,title,body,path_key) VALUES(?,?,?,?)',row)


def apply_head_changes(db,view_id,changes,*,sidecar=False,deadline=None):
    """Apply checked FTS row deltas inside an existing view-head capability.

    Old rows are compared before writing. The caller proves the current head,
    supplies trusted normalized FTS inputs, flushes and verifies the seal, and
    commits within the guard. Path lookup scans the corpus once and its cost
    must be measured for the final publication path.
    """
    name=head_table(view_id,sidecar=sidecar)
    if not db.in_transaction or not isinstance(changes,list) or len(changes)>MAX_ROWS:
        v.fail('fts_changes_invalid')
    old_rows=[];new_rows=[];seen=set();normalized=[]
    for change in changes:
        if not isinstance(change,dict) or set(change) not in ({'old','new'},{'old','new','restore_rowid'}):
            v.fail('fts_changes_invalid')
        old,new=change['old'],change['new']
        restore=change.get('restore_rowid')
        if restore is not None:
            v.integer(restore,1)
            if old is not None or new is None:v.fail('fts_changes_invalid')
        if old is None and new is None:v.fail('fts_changes_invalid')
        if old is not None:old_rows.append(old)
        if new is not None:new_rows.append(new)
        row=old if old is not None else new
        if not isinstance(row,dict):v.fail('fts_changes_invalid')
        path=row.get('path_key')
        if not isinstance(path,str) or path in seen:v.fail('fts_changes_invalid')
        if old is not None and new is not None and (not isinstance(new,dict) or new.get('path_key')!=path):
            v.fail('fts_changes_invalid')
        seen.add(path);normalized.append((path,old,new,restore))
    _entries(old_rows);_entries(new_rows)
    indexed={}
    if normalized:
        cursor=db.execute(f'SELECT rowid,path_key FROM {name}')
        try:
            for rowid,path in cursor:
                _deadline(deadline)
                if path in seen:
                    if path in indexed:v.fail('fts_head_changed')
                    indexed[path]=rowid
        finally:cursor.close()
    db.execute('SAVEPOINT s_head_changes')
    try:
        result=[]
        for path,old,new,restore in normalized:
            _deadline(deadline)
            old_rowid=indexed.get(path)
            existing=(_execute(db,f'SELECT rowid,filepath,title,body,path_key FROM {name} WHERE rowid=?',(old_rowid,))
                      if old_rowid is not None else [])
            expected=[] if old is None else [tuple(old[k] for k in _FIELDS)]
            if [tuple(row[1:]) for row in existing]!=expected:v.fail('fts_head_changed')
            if old==new:
                result.append(dict(path=path,old_rowid=old_rowid,new_rowid=old_rowid))
                continue
            if old is not None:_execute(db,f'DELETE FROM {name} WHERE rowid=?',(old_rowid,))
            new_rowid=None
            if new is not None:
                cursor=db.execute(f'INSERT INTO {name}(rowid,filepath,title,body,path_key) VALUES(?,?,?,?,?)',
                                  (old_rowid if old_rowid is not None else restore,*(new[k] for k in _FIELDS)))
                try:new_rowid=cursor.lastrowid
                finally:cursor.close()
            result.append(dict(path=path,old_rowid=old_rowid,new_rowid=new_rowid))
    except BaseException:
        db.execute('ROLLBACK TO s_head_changes')
        db.execute('RELEASE s_head_changes')
        raise
    db.execute('RELEASE s_head_changes')
    return result


def _schema(db):
    return [tuple(r) for r in _execute(db,
        "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT GLOB 'sqlite_*' ORDER BY type,name")]


@lru_cache(maxsize=64)
def _expected_schema(ordinal,sidecar=False):
    db=sqlite3.connect(':memory:',cached_statements=0)
    try:
        _execute(db,_ddl(ordinal,sidecar=sidecar))
        return tuple(_schema(db))
    finally:db.close()


def schema_rows(db):
    actual=[r for r in _schema(db) if r[1].startswith(PREFIX) or r[2].startswith(PREFIX)]
    expected=[]
    try:
        slots=_execute(db,"SELECT ordinal,json_type(stage_json,'$.legacy.sidecar_corpus') "
                          'FROM generation_slots ORDER BY ordinal')
    except sqlite3.Error:
        v.fail('fts_generation_ownership_invalid')
    ordinals=[r[0] for r in slots]
    if len(set(ordinals))!=len(ordinals):v.fail('fts_generation_ownership_invalid')
    for ordinal,sidecar_type in slots:
        expected.extend(_expected_schema(ordinal))
        if sidecar_type is not None:
            expected.extend(_expected_schema(ordinal,True))
    if sorted(actual)!=sorted(expected):v.fail('fts_schema_mismatch')
    return actual


def _one_schema(db,ordinal,*,sidecar=False):
    base=table(ordinal,sidecar=sidecar)
    actual=[r for r in _schema(db) if r[1]==base or r[1].startswith(base+'_') or r[2]==base or r[2].startswith(base+'_')]
    expected=list(_expected_schema(ordinal,sidecar))
    if sorted(actual)!=sorted(expected):v.fail('fts_schema_mismatch')
    return expected


@lru_cache(maxsize=64)
def _expected_head_schema(view_id,sidecar=False):
    db=sqlite3.connect(':memory:',cached_statements=0)
    try:
        _execute(db,head_ddl(view_id,sidecar=sidecar))
        return tuple(_schema(db))
    finally:db.close()


def _one_head_schema(db,view_id,*,sidecar=False):
    base=head_table(view_id,sidecar=sidecar)
    actual=[r for r in _schema(db) if r[1]==base or r[1].startswith(base+'_') or r[2]==base or r[2].startswith(base+'_')]
    expected=list(_expected_head_schema(view_id,sidecar))
    if sorted(actual)!=sorted(expected):v.fail('fts_schema_mismatch')
    return expected


def head_schema_rows(db):
    """Validate the exact dynamic FTS schema selected by all view heads."""
    actual=[r for r in _schema(db) if r[1].startswith(PREFIX) or r[2].startswith(PREFIX)]
    try:
        heads=_execute(db,'SELECT h.view_id,g.sidecar_present FROM s_heads h JOIN s_generations g '
                          'ON g.view_id=h.view_id AND g.ordinal=h.ordinal ORDER BY h.view_id')
        count=_execute(db,'SELECT COUNT(*) FROM s_heads')[0][0]
    except sqlite3.Error:
        v.fail('fts_generation_ownership_invalid')
    if len(heads)!=count:v.fail('fts_generation_ownership_invalid')
    expected=[];seen=set()
    for view_id,sidecar in heads:
        v.identifier(view_id)
        if view_id in seen or type(sidecar) is not int or sidecar not in (0,1):
            v.fail('fts_generation_ownership_invalid')
        seen.add(view_id)
        expected.extend(_expected_head_schema(view_id))
        if sidecar:expected.extend(_expected_head_schema(view_id,True))
    if sorted(actual)!=sorted(expected):v.fail('fts_schema_mismatch')
    return actual


def assert_rows(db,ordinal,expected_rows,deadline=None,*,sidecar=False):
    _deadline(deadline);_one_schema(db,ordinal,sidecar=sidecar)
    expected=_entries(expected_rows,insertion_order=sidecar)
    order = 'rowid' if sidecar else 'path_key COLLATE BINARY'
    cursor=db.execute(f'SELECT filepath,title,body,path_key FROM {table(ordinal,sidecar=sidecar)} ORDER BY {order}')
    try:
        for row in expected:
            _deadline(deadline)
            actual=cursor.fetchone()
            if actual is None or tuple(actual)!=row:v.fail('fts_rows_mismatch')
        if cursor.fetchone() is not None:v.fail('fts_rows_mismatch')
    finally:cursor.close()
    _deadline(deadline)
    return expected


def _typed(value):
    if value is None:return ['null']
    if type(value) is int:return ['integer',str(value)]
    if type(value) is float and math.isfinite(value):return ['real',value.hex()]
    if type(value) is str:return ['text',value]
    if type(value) is bytes:return ['blob',value.hex()]
    v.fail('fts_storage_value_invalid')


def storage_digest(db,ordinal,deadline=None,*,sidecar=False):
    """Read opaque shadow rows before any MATCH; perform no shadow writes."""
    metadata=_one_schema(db,ordinal,sidecar=sidecar)
    return _storage_digest(db,table(ordinal,sidecar=sidecar),metadata,deadline)


def head_storage_digest(db,view_id,deadline=None,*,sidecar=False):
    metadata=_one_head_schema(db,view_id,sidecar=sidecar)
    return _storage_digest(db,head_table(view_id,sidecar=sidecar),metadata,deadline)


def _storage_digest(db,base,metadata,deadline):
    h=hashlib.sha256(b'memvane-fts-physical-v1\0')
    def add(value):
        raw=v.canonical(value).encode('utf8');h.update(len(raw).to_bytes(8,'big'));h.update(raw)
    add(metadata)
    total=0
    for kind,name,_,_ in sorted(metadata):
        if kind!='table' or name==base:continue
        _deadline(deadline)
        columns=[r[1] for r in _execute(db,f'PRAGMA table_info({name})')]
        if not columns or any(not re.fullmatch('[A-Za-z0-9_]+',col) for col in columns):
            v.fail('fts_schema_mismatch')
        add([name,columns])
        order=','.join('"'+col+'"' for col in columns)
        cursor=db.execute(f'SELECT * FROM {name} ORDER BY {order}')
        try:
            for row in cursor:
                _deadline(deadline)
                total+=sum(len(x) if isinstance(x,(str,bytes)) else 8 for x in row)
                if total>MAX_FTS_BYTES*3:v.fail('fts_storage_too_large')
                add([_typed(x) for x in row])
        finally:cursor.close()
    _deadline(deadline)
    return h.hexdigest()



def verify_index(db,ordinal,*,sidecar=False):
    """Validate FTS postings against internal content before sealing a build."""
    name=table(ordinal,sidecar=sidecar)
    try:
        _execute(db,f"INSERT INTO {name}({name}) VALUES('integrity-check')")
    except sqlite3.Error:
        v.fail('fts_integrity_failed')


def verify_head_index(db,view_id,*,sidecar=False):
    name=head_table(view_id,sidecar=sidecar)
    try:
        _execute(db,f"INSERT INTO {name}({name}) VALUES('integrity-check')")
    except sqlite3.Error:
        v.fail('fts_integrity_failed')


def head_proof(db,view_id,rows,deadline=None,*,expected_storage=None,sidecar=False):
    """Stream an ordered head corpus and its physical commitment in one snapshot."""
    try:rows=iter(rows)
    except TypeError:v.fail('fts_rows_invalid')
    physical=head_storage_digest(db,view_id,deadline,sidecar=sidecar)
    if expected_storage is not None and physical!=expected_storage:
        v.fail('fts_storage_mismatch')
    order='rowid' if sidecar else 'path_key COLLATE BINARY'
    name=head_table(view_id,sidecar=sidecar)
    cursor=db.execute(f'SELECT filepath,title,body,path_key FROM {name} ORDER BY {order}')
    logical=hashlib.sha256(b'memvane-view-head-fts-logical-v1\0')
    seen=set();previous=None;size=0
    try:
        for count,item in enumerate(rows,1):
            _deadline(deadline)
            if count>MAX_ROWS:v.fail('fts_rows_invalid')
            row=_entries([item],insertion_order=True)[0]
            path=row[3];encoded_path=path.encode('utf8')
            if path in seen or (not sidecar and previous is not None and encoded_path<=previous):
                v.fail('fts_rows_invalid')
            seen.add(path);previous=encoded_path
            size+=sum(len(value.encode('utf8')) for value in row)
            if size>MAX_FTS_BYTES:v.fail('fts_rows_too_large')
            actual=cursor.fetchone()
            if actual is None or tuple(actual)!=row:v.fail('fts_rows_mismatch')
            raw=v.canonical(row).encode('utf8')
            logical.update(len(raw).to_bytes(8,'big'));logical.update(raw)
        if cursor.fetchone() is not None:v.fail('fts_rows_mismatch')
    finally:cursor.close()
    _deadline(deadline)
    return logical.hexdigest(),physical


def proof(db,ordinal,rows,deadline=None,*,return_storage=False,expected_storage=None,sidecar=False):
    physical=storage_digest(db,ordinal,deadline,sidecar=sidecar)
    if expected_storage is not None and physical!=expected_storage:
        v.fail('fts_storage_mismatch')
    entries=assert_rows(db,ordinal,rows,deadline,sidecar=sidecar)
    lexical=v.digest('memvane-generation-fts-v1',{'rows':entries,'storage':physical})
    return (lexical,physical) if return_storage else lexical


def proof_pair(db,ordinal,base_rows,sidecar_rows,deadline=None,*,expected_storage=None):
    """Bind both corpora before reading either virtual FTS table."""
    base_storage=storage_digest(db,ordinal,deadline)
    side_storage=storage_digest(db,ordinal,deadline,sidecar=True)
    combined=v.digest('memvane-generation-fts-pair-storage-v1',
                      {'base':base_storage,'sidecar':side_storage})
    if expected_storage is not None and combined!=expected_storage:
        v.fail('fts_storage_mismatch')
    base_entries=assert_rows(db,ordinal,base_rows,deadline)
    side_entries=assert_rows(db,ordinal,sidecar_rows,deadline,sidecar=True)
    base_lexical=v.digest('memvane-generation-fts-v1',{'rows':base_entries,'storage':base_storage})
    side_lexical=v.digest('memvane-generation-fts-v1',{'rows':side_entries,'storage':side_storage})
    lexical=v.digest('memvane-generation-fts-pair-v1',{'base':base_lexical,'sidecar':side_lexical})
    return lexical,combined

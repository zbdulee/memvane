"""View-head FTS5 commitments for the row format.

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




def head_table(view_id):
    v.identifier(view_id)
    return PREFIX+'h_'+view_id


def head_ddl(view_id):
    return (f'CREATE VIRTUAL TABLE {head_table(view_id)} USING '
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
        self._frozen=False
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
        if self._active is None or self._frozen or threading.get_ident()!=self._thread:
            return sqlite3.SQLITE_DENY
        if not isinstance(self._active,tuple):
            return sqlite3.SQLITE_DENY
        base=head_table(self._active[1])
        return (sqlite3.SQLITE_OK if all(x==base or x.startswith(base+'_') for x in targets)
                else sqlite3.SQLITE_DENY)


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
        self._frozen=False
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
            self._frozen=False
            self._db.set_authorizer(self._authorize)


    def freeze_head(self):
        """Flush pending FTS5 segments and refuse any later FTS write.

        A savepoint after the last FTS write makes the shadow bytes read before
        COMMIT equal to the committed ones; the commitment is computed after it.
        """
        if (not isinstance(self._active,tuple) or self._frozen or threading.get_ident()!=self._thread
                or not self._db.in_transaction):
            v.fail('fts_build_capability_invalid')
        _execute(self._db,'SAVEPOINT s_head_flush')
        _execute(self._db,'RELEASE s_head_flush')
        self._frozen=True
        self._db.set_authorizer(self._authorize)




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




def create_head(db,view_id):
    _execute(db,head_ddl(view_id))


def head_map_matches(db,view_id,rows):
    """True when the rowid map names exactly the head FTS table's ``rows`` rows (path and rowid).

    ``rows`` is the row count the caller already proved for the table.
    """
    name=head_table(view_id);origin='base'
    table=rows
    mapped=_execute(db,'SELECT COUNT(*) FROM s_head_fts_map WHERE view_id=? AND origin=?',(view_id,origin))[0][0]
    joined=_execute(db,f'SELECT COUNT(*) FROM {name} t JOIN s_head_fts_map m ON m.view_id=? AND m.origin=? '
                       f'AND m.fts_rowid=t.rowid AND m.path_key=t.path_key',(view_id,origin))[0][0]
    return table==mapped==joined


def apply_mapped_head_changes(db,view_id,changes,*,deadline=None):
    """Apply checked FTS row deltas, finding each path's rowid in the head map.

    Reads and writes only the changed paths. The caller holds the view's FTS
    capability, proves the head and owns the transaction.
    """
    name=head_table(view_id);origin='base'
    if not db.in_transaction or not isinstance(changes,list) or len(changes)>MAX_ROWS:
        v.fail('fts_changes_invalid')
    seen=set()
    for change in changes:
        _deadline(deadline)
        if not isinstance(change,dict) or set(change)!={'old','new'}:v.fail('fts_changes_invalid')
        old,new=change['old'],change['new']
        if old is None and new is None:v.fail('fts_changes_invalid')
        for row in (old,new):
            if row is not None:_entries([row],insertion_order=True)
        path=(old if old is not None else new)['path_key']
        if path in seen or (old is not None and new is not None and new['path_key']!=path):
            v.fail('fts_changes_invalid')
        seen.add(path)
        mapped=_execute(db,'SELECT fts_rowid FROM s_head_fts_map WHERE view_id=? AND origin=? AND path_key=?',
                        (view_id,origin,path))
        rowid=mapped[0][0] if mapped else None
        existing=(_execute(db,f'SELECT filepath,title,body,path_key FROM {name} WHERE rowid=?',(rowid,))
                  if rowid is not None else [])
        if [tuple(r) for r in existing]!=([] if old is None else [tuple(old[k] for k in _FIELDS)]):
            v.fail('fts_head_changed')
        if old is not None:
            _execute(db,f'DELETE FROM {name} WHERE rowid=?',(rowid,))
            _execute(db,'DELETE FROM s_head_fts_map WHERE view_id=? AND origin=? AND path_key=?',(view_id,origin,path))
        if new is not None:
            cursor=db.execute(f'INSERT INTO {name}(rowid,filepath,title,body,path_key) VALUES(?,?,?,?,?)',
                              (rowid,*(new[k] for k in _FIELDS)))
            try:new_rowid=cursor.lastrowid
            finally:cursor.close()
            _execute(db,'INSERT INTO s_head_fts_map(view_id,origin,path_key,fts_rowid) VALUES (?,?,?,?)',
                     (view_id,origin,path,new_rowid))


def apply_head_changes(db,view_id,changes,*,deadline=None):
    """Apply checked FTS row deltas inside an existing view-head capability.

    Old rows are compared before writing. The caller proves the current head,
    supplies trusted normalized FTS inputs, flushes and verifies the seal, and
    commits within the guard. Path lookup scans the corpus once and its cost
    must be measured for the final publication path.
    """
    name=head_table(view_id)
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
def _expected_head_schema(view_id):
    db=sqlite3.connect(':memory:',cached_statements=0)
    try:
        _execute(db,head_ddl(view_id))
        return tuple(_schema(db))
    finally:db.close()


def _one_head_schema(db,view_id):
    base=head_table(view_id)
    actual=[r for r in _schema(db) if r[1]==base or r[1].startswith(base+'_') or r[2]==base or r[2].startswith(base+'_')]
    expected=list(_expected_head_schema(view_id))
    if sorted(actual)!=sorted(expected):v.fail('fts_schema_mismatch')
    return expected


def head_schema_rows(db):
    """Validate the exact dynamic FTS schema selected by all view heads."""
    actual=[r for r in _schema(db) if r[1].startswith(PREFIX) or r[2].startswith(PREFIX)]
    try:
        heads=_execute(db,'SELECT h.view_id FROM s_heads h JOIN s_generations g '
                          'ON g.view_id=h.view_id AND g.ordinal=h.ordinal ORDER BY h.view_id')
        count=_execute(db,'SELECT COUNT(*) FROM s_heads')[0][0]
    except sqlite3.Error:
        v.fail('fts_generation_ownership_invalid')
    if len(heads)!=count:v.fail('fts_generation_ownership_invalid')
    expected=[];seen=set()
    for (view_id,) in heads:
        v.identifier(view_id)
        if view_id in seen:
            v.fail('fts_generation_ownership_invalid')
        seen.add(view_id)
        expected.extend(_expected_head_schema(view_id))
    if sorted(actual)!=sorted(expected):v.fail('fts_schema_mismatch')
    return actual




def _typed(value):
    if value is None:return ['null']
    if type(value) is int:return ['integer',str(value)]
    if type(value) is float and math.isfinite(value):return ['real',value.hex()]
    if type(value) is str:return ['text',value]
    if type(value) is bytes:return ['blob',value.hex()]
    v.fail('fts_storage_value_invalid')




def head_storage_digest(db,view_id,deadline=None):
    metadata=_one_head_schema(db,view_id)
    return _storage_digest(db,head_table(view_id),metadata,deadline)


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





def verify_head_index(db,view_id):
    name=head_table(view_id)
    try:
        _execute(db,f"INSERT INTO {name}({name}) VALUES('integrity-check')")
    except sqlite3.Error:
        v.fail('fts_integrity_failed')


def head_proof(db,view_id,rows,deadline=None,*,expected_storage=None,logical=True):
    """Stream an ordered head corpus and its physical commitment in one snapshot."""
    try:rows=iter(rows)
    except TypeError:v.fail('fts_rows_invalid')
    physical=head_storage_digest(db,view_id,deadline)
    if expected_storage is not None and physical!=expected_storage:
        v.fail('fts_storage_mismatch')
    order='path_key COLLATE BINARY'
    name=head_table(view_id)
    cursor=db.execute(f'SELECT filepath,title,body,path_key FROM {name} ORDER BY {order}')
    digest=hashlib.sha256(b'memvane-view-head-fts-logical-v1\0') if logical else None
    seen=set();previous=None;size=0
    try:
        for count,item in enumerate(rows,1):
            _deadline(deadline)
            if count>MAX_ROWS:v.fail('fts_rows_invalid')
            row=_entries([item],insertion_order=True)[0]
            path=row[3];encoded_path=path.encode('utf8')
            if path in seen or (previous is not None and encoded_path<=previous):
                v.fail('fts_rows_invalid')
            seen.add(path);previous=encoded_path
            size+=sum(len(value.encode('utf8')) for value in row)
            if size>MAX_FTS_BYTES:v.fail('fts_rows_too_large')
            actual=cursor.fetchone()
            if actual is None or tuple(actual)!=row:v.fail('fts_rows_mismatch')
            if digest is not None:
                raw=v.canonical(row).encode('utf8')
                digest.update(len(raw).to_bytes(8,'big'));digest.update(raw)
        if cursor.fetchone() is not None:v.fail('fts_rows_mismatch')
    finally:cursor.close()
    _deadline(deadline)
    return (None if digest is None else digest.hexdigest()),physical





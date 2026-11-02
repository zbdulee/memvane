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


def table(ordinal):
    if type(ordinal) is not int or not 1 <= ordinal <= 2**63-1:
        v.fail('invalid_fts_ordinal')
    return PREFIX+str(ordinal)


def _ddl(ordinal):
    return (f'CREATE VIRTUAL TABLE {table(ordinal)} USING '
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
        base=table(self._active)
        return (sqlite3.SQLITE_OK if all(x==base or x.startswith(base+'_') for x in targets)
                else sqlite3.SQLITE_DENY)

    @contextmanager
    def building(self,ordinal):
        table(ordinal)
        if self._active is not None or threading.get_ident()!=self._thread:
            v.fail('fts_build_capability_invalid')
        rows=_execute(self._db,'SELECT state FROM generations WHERE ordinal=?',(ordinal,))
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


def create(db,ordinal):
    table(ordinal)
    rows=_execute(db,'SELECT state FROM generations WHERE ordinal=?',(ordinal,))
    if len(rows)!=1 or rows[0][0]!='building':v.fail('fts_generation_not_building')
    _execute(db,_ddl(ordinal))


def _entries(rows):
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
    return sorted(result,key=lambda r:r[3])


def populate(db,ordinal,rows):
    for row in _entries(rows):
        _execute(db,f'INSERT INTO {table(ordinal)}(filepath,title,body,path_key) VALUES(?,?,?,?)',row)


def _schema(db):
    return [tuple(r) for r in _execute(db,
        "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT GLOB 'sqlite_*' ORDER BY type,name")]


@lru_cache(maxsize=64)
def _expected_schema(ordinal):
    db=sqlite3.connect(':memory:',cached_statements=0)
    try:
        _execute(db,_ddl(ordinal))
        return tuple(_schema(db))
    finally:db.close()


def schema_rows(db):
    actual=[r for r in _schema(db) if r[1].startswith(PREFIX) or r[2].startswith(PREFIX)]
    expected=[]
    ordinals=[r[0] for r in _execute(db,'SELECT ordinal FROM generations ORDER BY ordinal')]
    if len(set(ordinals))!=len(ordinals):v.fail('fts_generation_ownership_invalid')
    for ordinal in ordinals:expected.extend(_expected_schema(ordinal))
    if sorted(actual)!=sorted(expected):v.fail('fts_schema_mismatch')
    return actual


def _one_schema(db,ordinal):
    base=table(ordinal)
    actual=[r for r in _schema(db) if r[1]==base or r[1].startswith(base+'_') or r[2]==base or r[2].startswith(base+'_')]
    expected=list(_expected_schema(ordinal))
    if sorted(actual)!=sorted(expected):v.fail('fts_schema_mismatch')
    return expected


def assert_rows(db,ordinal,expected_rows,deadline=None):
    _deadline(deadline);_one_schema(db,ordinal)
    expected=_entries(expected_rows)
    cursor=db.execute(f'SELECT filepath,title,body,path_key FROM {table(ordinal)} ORDER BY path_key COLLATE BINARY')
    try:
        for row in expected:
            _deadline(deadline)
            actual=cursor.fetchone()
            if actual is None or tuple(actual)!=row:v.fail('fts_rows_mismatch')
        if cursor.fetchone() is not None:v.fail('fts_rows_mismatch')
    finally:cursor.close()
    _deadline(deadline)


def _typed(value):
    if value is None:return ['null']
    if type(value) is int:return ['integer',str(value)]
    if type(value) is float and math.isfinite(value):return ['real',value.hex()]
    if type(value) is str:return ['text',value]
    if type(value) is bytes:return ['blob',value.hex()]
    v.fail('fts_storage_value_invalid')


def storage_digest(db,ordinal,deadline=None):
    """Read opaque shadow rows before any MATCH; perform no shadow writes."""
    metadata=_one_schema(db,ordinal)
    h=hashlib.sha256(b'memvane-fts-physical-v1\0')
    def add(value):
        raw=v.canonical(value).encode('utf8');h.update(len(raw).to_bytes(8,'big'));h.update(raw)
    add(metadata)
    total=0
    for kind,name,_,_ in sorted(metadata):
        if kind!='table' or name==table(ordinal):continue
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


def proof(db,ordinal,rows,deadline=None):
    physical=storage_digest(db,ordinal,deadline)
    assert_rows(db,ordinal,rows,deadline)
    return v.digest('memvane-generation-fts-v1',{'rows':_entries(rows),'storage':physical})

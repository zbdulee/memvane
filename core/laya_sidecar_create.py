"""Publish a complete empty sidecar without exposing an unfinished database.

Only an issued absent sidecar descriptor and the real current writer owner can
enter. Private material is a cooperative filesystem receipt, not authority to
open arbitrary caller paths. No existing target or unknown orphan is removed.
"""
from contextlib import closing
import ctypes
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import sqlite3
import stat
import sys
import threading
import time
from types import SimpleNamespace
import uuid
import weakref

import laya_incremental_docs as incremental
import laya_initial_build as build
import laya_journal_reader as R
import laya_reader_windows as W
from laya_index_capacity import _fsync

TEMP_PREFIX = '.reader-sidecar-new-'
_materials = weakref.WeakKeyDictionary()
_AUX = ('-wal', '-shm', '-journal')


@dataclass(frozen=True, eq=False, init=False)
class _Material:
    def __init__(self):
        raise ValueError('reader_create_material_not_issued')


def _native_renamer():
    library = ctypes.CDLL(None, use_errno=True)
    # macOS SDK sys/stdio.h: RENAME_EXCL = 0x00000004.
    # Linux UAPI include/uapi/linux/fs.h: RENAME_NOREPLACE = (1 << 0).
    if sys.platform == 'darwin':
        name, flag = 'renameatx_np', 4
    elif sys.platform.startswith('linux'):
        name, flag = 'renameat2', 1
    else:
        raise ValueError('reader_atomic_create_unavailable')
    function = getattr(library, name, None)
    R._need(function is not None, 'reader_atomic_create_unavailable')
    function.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                         ctypes.c_char_p, ctypes.c_uint)
    function.restype = ctypes.c_int
    return function, flag


def _rename_noreplace(parent_fd, source, target):
    function, flag = _native_renamer()
    if function(parent_fd, os.fsencode(source), parent_fd, os.fsencode(target), flag) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), target)


def _parent(state, fd, info):
    current = os.fstat(fd)
    R._need(stat.S_ISDIR(current.st_mode) and current.st_uid == os.getuid()
            and stat.S_IMODE(current.st_mode) == 0o700
            and os.path.samestat(current, info) and os.path.samestat(current, state.lstat())
            and state.resolve(strict=True) == state, 'reader_create_parent_changed')


def _absent(state, parent_fd, info):
    _parent(state, parent_fd, info)
    for suffix in ('', *_AUX):
        R._need(not os.path.lexists(state / (incremental.FILE + suffix)),
                'reader_create_target_changed')


def _no_aux(path):
    R._need(not any(os.path.lexists(str(path) + suffix) for suffix in _AUX),
            'reader_create_material_changed')


def _allocate(component, state, parent_fd, info):
    _pid, installer, scope, node, _manifest = W._component(component)
    endpoint, current = W._owner(installer)
    R._need(current is scope and component.role == 'sidecar' and node is None
            and state == Path(endpoint.record['state'])
            and component.path == state / incremental.FILE,
            'reader_create_material_not_issued')
    name = TEMP_PREFIX + uuid.uuid4().hex + '.sqlite'
    _parent(state, parent_fd, info)
    _no_aux(state / name)
    fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                 0o600, dir_fd=parent_fd)
    try:
        opened = os.fstat(fd)
        W.L._private_file(opened)
        token = object.__new__(_Material)
        _materials[token] = SimpleNamespace(component=component, state=state, parent_fd=parent_fd,
            parent_info=info, name=name, fd=fd, node=[opened.st_dev, opened.st_ino],
            pid=os.getpid(), thread=threading.current_thread())
        return token
    except BaseException:
        os.close(fd)
        raise


def _material(token):
    R._need(type(token) is _Material and token in _materials, 'reader_create_material_not_issued')
    item = _materials[token]
    R._need(item.pid == os.getpid() and item.thread is threading.current_thread(),
            'reader_create_material_expired')
    _parent(item.state, item.parent_fd, item.parent_info)
    opened = os.fstat(item.fd)
    named = os.stat(item.name, dir_fd=item.parent_fd, follow_symlinks=False)
    W.L._private_file(opened)
    W.L._private_file(named)
    R._need(os.path.samestat(opened, named) and item.node == [opened.st_dev, opened.st_ino],
            'reader_create_material_changed')
    return item


def _connect_material(token, mode):
    item = _material(token)
    import laya_writer_scope as normal
    installer = W._component(item.component)[1]
    pin = None
    if type(installer) is normal.NormalWriter:
        normal.validate_normal_writer(installer)
        pin = normal._record(installer)['context'].queue_owner.pin()
    lifetime = None
    try:
        lifetime = W._Lifetime(item.component.install_id)
        db = sqlite3.connect((item.state / item.name).as_uri() + '?mode=' + mode,
                             uri=True, timeout=2, isolation_level=None, factory=W._OwnedSQLite)
    except BaseException:
        if lifetime is not None:
            lifetime.close()
        if pin is not None:
            pin.close()
        raise
    owner = W._OwnedConnection(item.component, db, lifetime, pin)
    owner.private_material = token
    W._owned[id(owner)] = owner
    try:
        _material(token)
    except BaseException:
        owner.close_locked()
        raise
    return owner


def _schema(db):
    return db.execute('SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name').fetchall()


def _initialize(token, identity):
    owner = _connect_material(token, 'rw')
    try:
        db = owner.db
        db.execute('PRAGMA trusted_schema=OFF')
        db.execute('PRAGMA foreign_keys=ON')
        R._need(db.execute('PRAGMA journal_mode=DELETE').fetchone() == ('delete',),
                'reader_create_material_changed')
        db.execute('PRAGMA synchronous=FULL')
        db.execute('PRAGMA fullfsync=ON')
        incremental._setup(db, identity)
    finally:
        # Already inside the creating physical EX; never wait on another EX.
        # A close failure remains registered/pending and the temp is retained.
        owner.close_locked()


def _verify(token, identity):
    item = _material(token)
    _no_aux(item.state / item.name)
    owner = _connect_material(token, 'ro')
    try:
        db = owner.db
        db.execute('PRAGMA query_only=ON')
        db.execute('PRAGMA trusted_schema=OFF')
        R._need(db.execute('PRAGMA journal_mode').fetchone() == ('delete',),
                'reader_create_material_changed')
        db.execute('BEGIN')
        R._need(db.execute('PRAGMA integrity_check').fetchall() == [('ok',)]
                and db.execute('PRAGMA application_id').fetchone() == (0,)
                and db.execute('PRAGMA user_version').fetchone() == (1,)
                and db.execute('SELECT * FROM meta').fetchall() == [incremental._expected_meta(identity)],
                'reader_create_material_changed')
        for table in ('docs', 'docs_fts', 'cached_backfill', 'publication_revisions'):
            R._need(db.execute('SELECT count(*) FROM ' + table).fetchone() == (0,),
                    'reader_create_material_changed')
        # A same-engine reference checks the full DDL including FTS shadows;
        # schema text can differ between supported SQLite engine versions.
        with closing(sqlite3.connect(':memory:', isolation_level=None)) as reference:
            incremental._setup(reference, identity)
            R._need(_schema(db) == _schema(reference), 'reader_create_material_changed')
        db.commit()
    except BaseException:
        # End the read transaction even if the following SQLite close fails.
        # Its strong pending owner must remain eligible for same-thread drain.
        db.rollback()
        raise
    finally:
        owner.close_locked()
    _material(token)
    _no_aux(item.state / item.name)
    _fsync(item.fd)
    _fsync(item.parent_fd)
    size = os.fstat(item.fd).st_size
    R._need(0 < size <= 2 * 1024 * 1024, 'reader_create_material_changed')
    return hashlib.sha256(os.pread(item.fd, size, 0)).digest()


def _dispose(token, *, published):
    item = _materials.get(token)
    if item is None:
        return
    try:
        # Do not unlink material with a failed, still-live SQLite close. The
        # strong connection owner will drain it; its orphan is kept, not GC'd.
        live = any(owner.db is not None and getattr(owner, 'private_material', None) is token
                   for owner in W._owned.values())
        if not published and not live and os.path.lexists(item.state / item.name):
            _material(token)
            _no_aux(item.state / item.name)
            os.unlink(item.name, dir_fd=item.parent_fd)
            _fsync(item.parent_fd)
    finally:
        os.close(item.fd)
        _materials.pop(token, None)


def resume_empty_sidecar_owned(component, *, identity, deadline):
    """Resume a fenced, empty DELETE-format sidecar after interrupted creation."""
    import laya_writer_scope as normal
    W._deadline(deadline)
    _pid, writer, scope, node, _manifest = W._component(component)
    R._need(type(writer) is normal.NormalWriter and component.role == 'sidecar'
            and node is not None, 'reader_component_owner_changed')
    record = normal._record(writer)
    R._need(record.get('marked') is True and record['context'] is scope,
            'normal_writer_sidecar_creation_unapproved')
    state = R._state(scope.state)
    path = component.path
    try:
        with W.mutation_window(component, exclusive=False, deadline=deadline):
            with W._proof_windows(component, deadline=deadline):
                with W.publish_window(component, exclusive=True, deadline=deadline):
                    normal.validate_normal_writer(writer, deadline=deadline)
                    R._need(path == state / incremental.FILE and
                            not any(os.path.lexists(str(path)+suffix) for suffix in _AUX),
                            'normal_writer_sidecar_creation_changed')
                    record['created_sidecar_node'] = list(node)
                    before = normal._connection_image(writer, 'sidecar', path)
                    owner = W._connect_owned(component, 'ro')
                    try:
                        db = owner.db
                        db.execute('PRAGMA query_only=ON')
                        normal._validate_identity(writer, 'sidecar', db)
                        R._need(db.execute('PRAGMA integrity_check').fetchall() == [('ok',)]
                                and incremental._schema_rows(db) in incremental._schema_variants()
                                and all(db.execute('SELECT count(*) FROM ' + name).fetchone() == (0,)
                                        for name in ('docs', 'docs_fts'))
                                and all(db.execute('SELECT count(*) FROM ' + name).fetchone() == (0,)
                                        for name in ('cached_backfill', 'publication_revisions')
                                        if any(row[1] == name for row in incremental._schema_rows(db))),
                                'normal_writer_sidecar_creation_changed')
                    finally:
                        owner.close_locked()
                    R._need(normal._connection_image(writer, 'sidecar', path) == before
                            and not any(os.path.lexists(str(path)+suffix) for suffix in _AUX),
                            'normal_writer_sidecar_creation_changed')
                    normal.validate_normal_writer(writer, deadline=deadline)
        opened = incremental._open_coordinated(path, component, identity, deadline=deadline)
        try:
            normal.finish_created_sidecar(writer, deadline=deadline)
        except BaseException:
            W.close_owned(opened, deadline=deadline)
            raise
        return opened
    except BaseException:
        record.pop('created_sidecar_node', None)
        raise


def create_sidecar_owned(component, *, identity=None, deadline):
    W._deadline(deadline)
    R._need(threading.current_thread() is threading.main_thread(),
            'reader_connection_main_thread_required')
    _pid, installer, scope, node, _manifest = W._component(component)
    R._need(component.role == 'sidecar' and node is None, 'reader_component_not_absent')
    endpoint, current = W._owner(installer)
    import laya_writer_scope as normal
    is_normal = type(installer) is normal.NormalWriter
    if is_normal:
        R._need(normal._record(installer).get('marked') is True,
                'normal_writer_intent_required')
    def recheck():
        if is_normal:
            normal.validate_normal_writer(installer, deadline=deadline)
        else:
            R._recheck(endpoint)
    R._need(current is scope, 'reader_component_owner_changed')
    state = R._state(endpoint.record['state'])
    R._need(component.path == state / incremental.FILE, 'reader_component_changed')
    _native_renamer()  # unsupported platforms refuse before private allocation
    lifetime = W._Lifetime(component.install_id)
    parent_fd = None
    material = None
    published = False
    try:
        parent_fd = os.open(state, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        info = os.fstat(parent_fd)
        with W.mutation_window(component, exclusive=False, deadline=deadline):
            with W._proof_windows(component, deadline=deadline):
                # Admission precedes even the first full-proof content open.
                with W.publish_window(component, exclusive=True, deadline=deadline):
                    _absent(state, parent_fd, info)
                recheck()
                record = build.read(state)
                R._need(record is not None, 'reader_build_missing')
                actual_identity = record['identity']
                R._need(identity is None or R._raw(identity) == R._raw(actual_identity),
                        'reader_create_identity_changed')
                R._need(time.monotonic() < deadline, 'reader_create_deadline')
                material = _allocate(component, state, parent_fd, info)
                _initialize(material, actual_identity)
                digest = _verify(material, actual_identity)
                R._need(time.monotonic() < deadline, 'reader_create_deadline')
                # Full validation stays outside the short publication cut.
                recheck()
                with W.publish_window(component, exclusive=True, deadline=deadline):
                    _endpoint, current = W._owner(installer)
                    R._need(current is scope, 'reader_component_owner_changed')
                    _absent(state, parent_fd, info)
                    # The same publication cut protects the base dependency
                    # marker and this rename. A lost published sidecar must
                    # never be replaced by a fresh, apparently valid baseline.
                    R._need(_endpoint.record.get('legacy_sidecar_baseline') is None,
                            'reader_sidecar_revision_unproved')
                    base = W.component_for(installer, 'base')
                    base_node = W._component(base)[3]
                    R._need(tuple(R._file(base.path, state)) == base_node,
                            'reader_component_changed')
                    opened_base = W._connect_owned(base, 'ro')
                    try:
                        if is_normal:
                            normal._validate_identity(installer, 'base', opened_base.db)
                        required = opened_base.db.execute(
                            "SELECT 1 FROM schema_meta WHERE key='reader-sidecar-required:v1'").fetchone()
                        R._need(not required, 'reader_sidecar_revision_unproved')
                        R._need(tuple(R._file(base.path, state)) == base_node,
                                'reader_component_changed')
                    finally:
                        opened_base.close_locked()
                    item = _material(material)
                    _no_aux(state / item.name)
                    size = os.fstat(item.fd).st_size
                    R._need(0 < size <= 2 * 1024 * 1024
                            and hashlib.sha256(os.pread(item.fd, size, 0)).digest() == digest,
                            'reader_create_material_changed')
                    R._need(time.monotonic() < deadline, 'reader_create_deadline')
                    _rename_noreplace(parent_fd, item.name, incremental.FILE)
                    R._need(R._file(component.path, state) == item.node,
                            'reader_create_target_changed')
                    published_node = tuple(item.node)
                    _no_aux(component.path)
                    _fsync(parent_fd)
                    published = True
                    if is_normal:
                        normal.adopt_created_sidecar(installer, material, published_node,
                                                     deadline=deadline)
        # The old absence descriptor is never promoted into an RW capability.
        # Release creation cut/mutation/physical locks before normal reopen.
        new_component = W.component_for(installer, 'sidecar')
        R._need(W._component(new_component)[3] == published_node,
                'reader_create_target_changed')
        R._need(time.monotonic() < deadline, 'reader_create_deadline')
        opened = incremental._open_coordinated(component.path, new_component, actual_identity,
                                                deadline=deadline)
        if is_normal:
            try:
                normal.finish_created_sidecar(installer, deadline=deadline)
            except BaseException:
                W.close_owned(opened, deadline=deadline)
                raise
        return opened
    finally:
        try:
            if material is not None:
                _dispose(material, published=published)
        finally:
            if parent_fd is not None:
                os.close(parent_fd)
            lifetime.close()

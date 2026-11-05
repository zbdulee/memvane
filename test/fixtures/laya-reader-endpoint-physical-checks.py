#!/usr/bin/env python3
"""Actual v5 endpoint resolution must not precede physical read admission."""
from contextlib import closing, ExitStack, contextmanager
import fcntl
import importlib
import importlib.util
import os
from pathlib import Path
import sqlite3
import threading
import time
import unittest
from unittest import mock

ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('endpoint_physical_seed',ROOT/'test/fixtures/laya-reader-write-checks.py')
existing=importlib.util.module_from_spec(spec);spec.loader.exec_module(existing)

class EndpointPhysicalChecks(unittest.TestCase):
    def setUp(self):
        self.fixture=existing.WriteChecks('runTest')
        self.addCleanup(self.fixture.doCleanups);self.fixture.setUp()
        self.seed,self.case,self.W=self.fixture.seed,self.fixture.case,self.fixture.W

    def test_busy_base_physical_lock_refuses_before_any_content_open(self):
        lease=importlib.import_module('laya_reader_lease')
        with self.seed.prepared():
            self.W.prepare_windows(self.seed.installer)
        # An existing writer keeps WAL auxiliaries warm; this is not cold proof.
        with self.seed.authority(),closing(sqlite3.connect(self.case.db,isolation_level=None)) as warm:
            warm.execute('SELECT 1 FROM head LIMIT 1').fetchone()
            calls=[];connect=sqlite3.connect
            def observe(database,*args,**kwargs):
                if str(database).startswith(self.case.db.as_uri()):calls.append(str(database))
                return connect(database,*args,**kwargs)
            with self.fixture.held('base-physical.lock','EX'),mock.patch.object(sqlite3,'connect',side_effect=observe):
                with self.assertRaisesRegex(ValueError,'reader_.*busy|reader_.*deadline'):
                    with lease.resolve_endpoint(self.case.project,action='recall',host_root=self.case.plugin):pass
            self.assertEqual(calls,[],'content SQLite must not open before physical SH admission')

    def test_initial_reader_sql_obeys_deadline_and_releases_physical_lock(self):
        opens=importlib.import_module('laya_reader_open')
        lease=importlib.import_module('laya_reader_lease')
        real_clock=time.monotonic
        execute=opens._ReaderSQLite.execute
        late=[]
        def clock():
            return real_clock()+(20 if late else 0)
        def delayed(db,sql,*args):
            if sql=='SELECT count(*) FROM main.sqlite_master':
                late.append(True)
                execute(db,'WITH RECURSIVE n(x) AS (VALUES(0) UNION ALL '
                           'SELECT x+1 FROM n WHERE x<50000) SELECT sum(x) FROM n').fetchone()
                raise AssertionError('initial SQL was not interrupted')
            return execute(db,sql,*args)
        with self.seed.authority(),closing(sqlite3.connect(self.case.db,isolation_level=None)) as warm:
            warm.execute('SELECT 1 FROM head LIMIT 1').fetchone()
            with lease.resolve_endpoint(self.case.project,action='recall',host_root=self.case.plugin) as endpoint:
                deadline=real_clock()+10
                with mock.patch.object(opens.time,'monotonic',side_effect=clock), \
                        mock.patch.object(opens._ReaderSQLite,'execute',delayed):
                    with self.assertRaisesRegex(ValueError,'reader_open_deadline'):
                        opens.open_warm_component(endpoint,'base',deadline=deadline)
                self.assertEqual(late,[True])
                self.assertEqual(opens.drain_pending(deadline=real_clock()+2),0)
                root=self.seed.reader._lock_dir(self.seed.install,create=False)
                fd=os.open(root/'base-physical.lock',os.O_RDONLY|os.O_NOFOLLOW)
                try:
                    fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                    fcntl.flock(fd,fcntl.LOCK_UN)
                finally:os.close(fd)

    def test_actual_content_opens_hold_their_physical_shared_locks(self):
        lease=importlib.import_module('laya_reader_lease')
        # Conversion prepares the complete lock namespace before publishing v5.
        root=self.seed.reader._lock_dir(self.seed.install,create=False)
        paths={str(path):role for role,path in (('base',self.case.db),('queue',self.case.queue),
               ('catalog',self.case.catalog),('binding',self.case.store),('sidecar',self.seed.sidecar))}
        with self.seed.authority(),closing(sqlite3.connect(self.case.db,isolation_level=None)) as warm:
            warm.execute('SELECT 1 FROM head LIMIT 1').fetchone()
            actual=sqlite3.connect;observed=[]
            def connect(database,*args,**kwargs):
                text=str(database)
                for path,role in paths.items():
                    if text==path or text.startswith(Path(path).as_uri()+'?'):
                        fd=os.open(root/(role+'-physical.lock'),os.O_RDONLY|os.O_NOFOLLOW)
                        try:
                            try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                            except BlockingIOError:observed.append((role,True))
                            else:observed.append((role,False))
                        finally:os.close(fd)
                        break
                return actual(database,*args,**kwargs)
            with mock.patch.object(sqlite3,'connect',side_effect=connect):
                with lease.resolve_endpoint(self.case.project,action='recall',host_root=self.case.plugin):pass
            self.assertTrue(any(role=='base' for role,_ in observed),observed)
            self.assertTrue(all(held for _role,held in observed),observed)

    def test_missing_manifest_refuses_without_content_open_or_recreation(self):
        lease=importlib.import_module('laya_reader_lease')
        root=self.seed.reader._lock_dir(self.seed.install,create=False)
        manifest=root/'component-windows-v1.json';saved=root/'fixture-saved-manifest'
        original=manifest.lstat();manifest.rename(saved)
        try:
            with self.seed.authority(),closing(sqlite3.connect(self.case.db,isolation_level=None)) as warm:
                warm.execute('SELECT 1 FROM head LIMIT 1').fetchone()
                calls=[];actual=sqlite3.connect
                def connect(database,*args,**kwargs):
                    if str(database).startswith(self.case.db.as_uri()):calls.append(str(database))
                    return actual(database,*args,**kwargs)
                with mock.patch.object(sqlite3,'connect',side_effect=connect):
                    with self.assertRaisesRegex(ValueError,'reader_windows_not_prepared'):
                        with lease.resolve_endpoint(self.case.project,action='recall',host_root=self.case.plugin):pass
                self.assertEqual(calls,[]);self.assertFalse(manifest.exists())
                self.assertTrue(os.path.samestat(original,saved.lstat()))
        finally:saved.rename(manifest)

    def test_prepared_conversion_recheck_coexists_with_active_reader(self):
        lease=importlib.import_module('laya_reader_lease')
        manifest=self.seed.reader._lock_dir(self.seed.install,create=False)/'component-windows-v1.json'
        before=manifest.lstat();raw=manifest.read_bytes()
        with self.seed.authority(),lease.reader_lifetime(self.seed.install,exclusive=False):
            self.W._prepare_conversion_windows(self.seed.installer)
            with self.assertRaisesRegex(ValueError,'reader_installer_not_issued'):
                self.W._prepare_conversion_windows(object())
        self.assertTrue(os.path.samestat(before,manifest.lstat()))
        self.assertEqual(manifest.read_bytes(),raw)

    def test_warm_reader_keeps_physical_lock_until_creator_native_close(self):
        self.assertIsNotNone(importlib.util.find_spec('laya_reader_open'),
                             'T3 owned reader open is not implemented')
        opens=importlib.import_module('laya_reader_open')
        lease=importlib.import_module('laya_reader_lease')
        root=self.seed.reader._lock_dir(self.seed.install,create=False)
        def exclusive_available():
            fd=os.open(root/'base-physical.lock',os.O_RDONLY|os.O_NOFOLLOW)
            try:
                try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError:return False
                return True
            finally:os.close(fd)
        with self.seed.authority(),closing(sqlite3.connect(self.case.db,isolation_level=None)) as warm:
            warm.execute('SELECT 1 FROM head LIMIT 1').fetchone()
            owner=None
            try:
                with lease.resolve_endpoint(self.case.project,action='recall',host_root=self.case.plugin) as endpoint:
                    owner=opens.open_warm_component(endpoint,'base',deadline=time.monotonic()+2)
                    self.assertIsNotNone(owner.db.execute('SELECT gen FROM head LIMIT 1').fetchone())
                    self.assertFalse(exclusive_available())
                    errors=[]
                    def wrong_thread():
                        try:owner.close()
                        except Exception as exc:errors.append(str(exc))
                    child=threading.Thread(target=wrong_thread);child.start();child.join(timeout=3)
                    self.assertFalse(child.is_alive());self.assertEqual(errors,['reader_connection_owner_changed'])
                    self.assertFalse(exclusive_available())
                    def fail(_db):raise sqlite3.OperationalError('synthetic_reader_close_failure')
                    with mock.patch.object(type(owner.db),'_native_close_locked',new=fail):
                        with self.assertRaisesRegex(sqlite3.OperationalError,'synthetic_reader_close_failure'):
                            owner.close()
                    self.assertFalse(exclusive_available())
                self.assertFalse(exclusive_available(),'connection must retain SH after endpoint context exits')
                opens.drain_pending(deadline=time.monotonic()+2)
                self.assertTrue(exclusive_available())
                self.assertIsNone(owner.db)
            finally:
                if owner is not None and owner.db is not None:
                    opens.drain_pending(deadline=time.monotonic()+2)

    def test_snapshot_rollback_failure_can_be_drained_by_creator(self):
        opens=importlib.import_module('laya_reader_open')
        lease=importlib.import_module('laya_reader_lease')
        target=[None];failed=[];actual=sqlite3.Connection.rollback
        def exclusive_available():
            path=self.seed.reader._lock_dir(self.seed.install,create=False)/'sidecar-physical.lock'
            fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
            try:
                try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError:return False
                return True
            finally:os.close(fd)
        def rollback(db):
            if id(db)==target[0] and not failed:
                failed.append(True)
                raise sqlite3.OperationalError('synthetic_snapshot_rollback_failure')
            return actual(db)
        with self.seed.authority(),ExitStack() as warm:
            for path in (self.case.db,self.case.catalog,self.seed.sidecar):
                db=warm.enter_context(closing(sqlite3.connect(path,isolation_level=None)))
                db.execute('SELECT count(*) FROM sqlite_master').fetchone()
            with lease.resolve_endpoint(self.case.project,action='recall',host_root=self.case.plugin) as endpoint:
                callbacks={role:lambda db:db.execute('SELECT count(*) FROM sqlite_master').fetchone()[0]
                           for role in ('base','binding','catalog','sidecar')}
                with mock.patch.object(opens._ReaderSQLite,'rollback',new=rollback):
                    try:
                        with self.assertRaisesRegex(ValueError,'reader_connection_transaction_active'):
                            with opens.pin_warm_components(endpoint,callbacks,deadline=time.monotonic()+5) as pinned:
                                target[0]=id(pinned.connections[3])
                        self.assertEqual(failed,[True])
                        self.assertEqual(sum(owner.pending for owner in opens._owners.values()),1)
                        self.assertFalse(exclusive_available())
                        pending=next(owner for owner in opens._owners.values() if owner.pending)
                        with self.assertRaisesRegex(ValueError,'reader_close_deadline'):
                            opens.drain_pending(deadline=time.monotonic()-1)
                        self.assertTrue(pending.db.in_transaction,'expired drain must not start rollback')
                        self.assertFalse(exclusive_available())
                        self.assertEqual(opens.drain_pending(deadline=time.monotonic()+2),0)
                        self.assertEqual(opens._owners,{})
                        self.assertTrue(exclusive_available())
                    finally:
                        opens.drain_pending(deadline=time.monotonic()+2)

    def test_drain_continues_after_one_pending_rollback_still_fails(self):
        opens=importlib.import_module('laya_reader_open')
        lease=importlib.import_module('laya_reader_lease')
        targets={};failed=set();blocked=[True];actual=sqlite3.Connection.rollback
        def rollback(db):
            role=targets.get(id(db))
            if role is not None and role not in failed:
                failed.add(role)
                raise sqlite3.OperationalError('synthetic_first_rollback_failure')
            if role=='catalog' and blocked[0]:
                raise sqlite3.OperationalError('synthetic_persistent_rollback_failure')
            return actual(db)
        with self.seed.authority(),ExitStack() as warm:
            for path in (self.case.db,self.case.catalog,self.seed.sidecar):
                db=warm.enter_context(closing(sqlite3.connect(path,isolation_level=None)))
                db.execute('SELECT count(*) FROM sqlite_master').fetchone()
            with lease.resolve_endpoint(self.case.project,action='recall',host_root=self.case.plugin) as endpoint:
                callbacks={role:lambda db:db.execute('SELECT count(*) FROM sqlite_master').fetchone()[0]
                           for role in ('base','binding','catalog','sidecar')}
                try:
                    with mock.patch.object(opens._ReaderSQLite,'rollback',new=rollback):
                        with self.assertRaisesRegex(ValueError,'reader_connection_transaction_active'):
                            with opens.pin_warm_components(endpoint,callbacks,deadline=time.monotonic()+5) as pinned:
                                targets.update({id(pinned.connections[2]):'catalog',id(pinned.connections[3]):'sidecar'})
                        self.assertEqual(failed,{'catalog','sidecar'})
                        self.assertEqual(sum(owner.pending for owner in opens._owners.values()),2)
                        with self.assertRaisesRegex(sqlite3.OperationalError,'synthetic_persistent_rollback_failure'):
                            opens.drain_pending(deadline=time.monotonic()+2)
                        remaining=[owner for owner in opens._owners.values() if owner.pending]
                        self.assertEqual(len(remaining),1)
                        self.assertEqual(targets[id(remaining[0].db)],'catalog')
                        blocked[0]=False
                        self.assertEqual(opens.drain_pending(deadline=time.monotonic()+2),0)
                finally:
                    blocked[0]=False
                    opens.drain_pending(deadline=time.monotonic()+2)

    def test_warm_reader_sees_committed_wal_without_creating_auxiliaries(self):
        opens=importlib.import_module('laya_reader_open')
        lease=importlib.import_module('laya_reader_lease')
        with self.seed.authority(),closing(sqlite3.connect(self.case.db,isolation_level=None)) as warm:
            warm.execute('PRAGMA wal_autocheckpoint=0')
            warm.execute('SELECT 1 FROM head LIMIT 1').fetchone()
            with lease.resolve_endpoint(self.case.project,action='recall',host_root=self.case.plugin) as endpoint:
                warm.execute("INSERT INTO schema_meta VALUES('synthetic-wal-reader','committed')")
                wal=Path(str(self.case.db)+'-wal');shm=Path(str(self.case.db)+'-shm')
                self.assertGreater(wal.stat().st_size,0);self.assertTrue(shm.is_file())
                before=self.case.db.read_bytes()
                names={suffix:Path(str(self.case.db)+suffix).lstat().st_ino for suffix in ('-wal','-shm')}
                owner=opens.open_warm_component(endpoint,'base',deadline=time.monotonic()+2)
                try:
                    self.assertEqual(owner.db.execute("SELECT value FROM schema_meta WHERE key='synthetic-wal-reader'").fetchone(),('committed',))
                finally:owner.close()
                self.assertEqual(self.case.db.read_bytes(),before)
                self.assertEqual({suffix:Path(str(self.case.db)+suffix).lstat().st_ino for suffix in names},names)

    def test_cold_wal_read_is_query_only_and_preserves_database(self):
        opens=importlib.import_module('laya_reader_open')
        lease=importlib.import_module('laya_reader_lease')
        with self.seed.authority():
            warm=sqlite3.connect(self.case.db,isolation_level=None)
            try:
                warm.execute('SELECT 1 FROM head LIMIT 1').fetchone()
                with lease.resolve_endpoint(self.case.project,action='recall',host_root=self.case.plugin) as endpoint:
                    warm.close();warm=None
                    names=[Path(str(self.case.db)+suffix) for suffix in ('-wal','-shm','-journal')]
                    self.assertFalse(any(os.path.lexists(path) for path in names),'cold fixture must have no auxiliaries')
                    before=self.case.db.read_bytes();actual=sqlite3.connect;calls=[]
                    def connect(database,*args,**kwargs):
                        if str(database).startswith(self.case.db.as_uri()):calls.append(str(database))
                        return actual(database,*args,**kwargs)
                    with mock.patch.object(sqlite3,'connect',side_effect=connect):
                        owner=opens.open_warm_component(endpoint,'base',deadline=time.monotonic()+2)
                    try:
                        self.assertEqual(owner.db.execute('PRAGMA query_only').fetchone(),(1,))
                        self.assertIsNotNone(owner.db.execute('SELECT gen FROM head LIMIT 1').fetchone())
                        with self.assertRaises(sqlite3.OperationalError):
                            owner.db.execute("INSERT INTO schema_meta VALUES('cold-write','forbidden')")
                    finally: owner.close()
                    self.assertTrue(calls);self.assertTrue(all('?mode=ro' in call for call in calls))
                    self.assertEqual(self.case.db.read_bytes(),before)
                    self.assertFalse(names[2].exists())
            finally:
                if warm is not None:warm.close()

    def test_unissued_reader_endpoint_is_rejected_before_sqlite(self):
        opens=importlib.import_module('laya_reader_open');calls=[]
        with mock.patch.object(sqlite3,'connect',side_effect=lambda *a,**kw:calls.append(a)):
            with self.assertRaisesRegex(ValueError,'reader_endpoint_not_issued'):
                opens.open_warm_component(object(),'base',deadline=time.monotonic()+2)
        self.assertEqual(calls,[])

    def test_real_endpoint_four_component_common_pin_retains_physical_locks(self):
        opens=importlib.import_module('laya_reader_open')
        common=importlib.import_module('laya_reader_lease')
        lease=importlib.import_module('laya_reader_lease')
        root=self.seed.reader._lock_dir(self.seed.install,create=False)
        roles=(('base','db_path','head'),('binding','binding_path','sqlite_master'),
               ('catalog','catalog_path','sqlite_master'),('sidecar','sidecar_path','docs'))
        with self.seed.authority(),ExitStack() as keepalive:
            for path in (self.case.db,self.case.catalog,self.seed.sidecar):
                db=keepalive.enter_context(closing(sqlite3.connect(path,isolation_level=None)))
                db.execute('SELECT count(*) FROM sqlite_master').fetchone()
            with lease.resolve_endpoint(self.case.project,action='recall',host_root=self.case.plugin) as endpoint:
                @contextmanager
                def factory():
                    with ExitStack() as stack:
                        entries=[]
                        for role,key,table in roles:
                            monitor=opens.open_warm_component(endpoint,role,deadline=time.monotonic()+3)
                            stack.callback(monitor.close)
                            snapshot=opens.open_warm_component(endpoint,role,deadline=time.monotonic()+3)
                            stack.callback(snapshot.close)
                            entries.append((Path(endpoint.record[key]),monitor.db,snapshot.db,
                                            lambda db,table=table:db.execute('SELECT count(*) FROM '+table).fetchone()[0]))
                        yield entries
                with common.pin_common(factory,deadline=time.monotonic()+5) as pinned:
                    self.assertEqual(len(pinned.connections),4)
                    self.assertGreater(pinned.values[0],0);self.assertGreater(pinned.values[3],0)
                    self.assertTrue(callable(getattr(self.seed.reader,'_published_snapshot',None)),
                                    'T3 pinned base proof is not implemented')
                    self.assertTrue(callable(getattr(self.seed.reader,'_capacity_snapshot',None)),
                                    'T3 pinned capacity proof is not implemented')
                    self.assertTrue(callable(getattr(self.seed.reader,'_sidecar_snapshot',None)),
                                    'T3 pinned sidecar identity is not implemented')
                    decision=self.seed.reader.journal.select_route(str(self.case.project),action='recall')
                    published=self.seed.reader._published_snapshot(pinned.connections[0],decision,
                                                                  deadline=time.monotonic()+5)
                    self.seed.reader._capacity_snapshot(pinned.connections[1],pinned.connections[2],
                        Path(endpoint.record['binding_path']),decision,published)
                    sidecar=self.seed.reader._sidecar_snapshot(pinned.connections[3],decision)
                    self.assertEqual(sidecar['rows'],pinned.values[3])
                    self.assertFalse(sidecar['baseline'],'old populated sidecar is not a sealed revision zero')
                    self.assertEqual(published[0],pinned.connections[0].execute(
                        'SELECT gen FROM head WHERE scope=?',(decision['scopeId'],)).fetchone()[0])
                    forged=('different-generation',*published[1:])
                    with self.assertRaisesRegex(ValueError,'reader_binding_missing'):
                        self.seed.reader._capacity_snapshot(pinned.connections[1],pinned.connections[2],
                            Path(endpoint.record['binding_path']),decision,forged)
                    for role,_key,_table in roles:
                        fd=os.open(root/(role+'-physical.lock'),os.O_RDONLY|os.O_NOFOLLOW)
                        try:
                            with self.assertRaises(BlockingIOError):fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                        finally:os.close(fd)
                for role,_key,_table in roles:
                    fd=os.open(root/(role+'-physical.lock'),os.O_RDONLY|os.O_NOFOLLOW)
                    try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                    finally:os.close(fd)

    def test_replaced_base_name_is_refused_before_sqlite_open(self):
        opens=importlib.import_module('laya_reader_open')
        lease=importlib.import_module('laya_reader_lease')
        with self.seed.authority(),closing(sqlite3.connect(self.case.db,isolation_level=None)) as warm:
            warm.execute('SELECT 1 FROM head LIMIT 1').fetchone()
            with lease.resolve_endpoint(self.case.project,action='recall',host_root=self.case.plugin) as endpoint:
                saved=self.case.state/'fixture-saved-base.sqlite'
                self.case.db.rename(saved)
                try:
                    fd=os.open(self.case.db,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
                    os.close(fd);calls=[];actual=sqlite3.connect
                    def connect(database,*args,**kwargs):
                        if str(database).startswith(self.case.db.as_uri()):calls.append(str(database))
                        return actual(database,*args,**kwargs)
                    with mock.patch.object(sqlite3,'connect',side_effect=connect):
                        with self.assertRaisesRegex(ValueError,'reader_component_changed'):
                            opens.open_warm_component(endpoint,'base',deadline=time.monotonic()+2)
                    self.assertEqual(calls,[])
                finally:
                    self.case.db.unlink();saved.rename(self.case.db)

if __name__=='__main__':unittest.main(verbosity=2)

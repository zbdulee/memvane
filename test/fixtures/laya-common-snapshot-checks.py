#!/usr/bin/env python3
"""Real SQLite snapshot race; no endpoint/cohort/host capability claim."""
from contextlib import ExitStack, closing, contextmanager
import importlib
import importlib.util
import fcntl
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'core'))

class CommonSnapshotChecks(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve();self.paths=[self.root/'a.sqlite',self.root/'b.sqlite']
        self.writers=[];self.opened=[]
        for path in self.paths:
            writer=sqlite3.connect(path,isolation_level=None)
            writer.execute('PRAGMA journal_mode=WAL');writer.execute('CREATE TABLE content(value INTEGER NOT NULL)')
            writer.execute('INSERT INTO content VALUES(0)');self.writers.append(writer)
        self.addCleanup(lambda:[db.close() for db in reversed(self.writers)])
    def api(self):
        self.assertIsNotNone(importlib.util.find_spec('laya_reader_lease'),
            'T3 common SQLite pin primitive is not implemented')
        return importlib.import_module('laya_reader_lease')
    @contextmanager
    def members(self,callback):
        with ExitStack() as stack:
            members=[]
            for i,path in enumerate(self.paths):
                monitor=stack.enter_context(closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,isolation_level=None)))
                snapshot=stack.enter_context(closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,isolation_level=None)))
                monitor.execute('PRAGMA query_only=ON');snapshot.execute('PRAGMA query_only=ON')
                self.opened.extend((monitor,snapshot))
                members.append((path,monitor,snapshot,lambda db,i=i:callback(i,db)))
            yield members
    def closed(self):
        for db in self.opened:
            with self.assertRaises(sqlite3.ProgrammingError):db.execute('SELECT 1')
    def test_commit_between_pins_retries_then_keeps_common_snapshot(self):
        api=self.api();injected=[];seen=[]
        def pin(i,db):
            value=db.execute('SELECT value FROM content').fetchone()[0];seen.append((i,value))
            if i==0 and not injected:
                self.writers[1].execute('UPDATE content SET value=1');injected.append(True)
            return value
        with api.pin_common(lambda:self.members(pin),deadline=time.monotonic()+3) as result:
            self.assertEqual(result.values,(0,1));self.assertEqual(seen,[(0,0),(1,1),(0,0),(1,1)])
            self.writers[1].execute('UPDATE content SET value=2')
            self.assertEqual(result.connections[1].execute('SELECT value FROM content').fetchone(),(1,))
        self.closed()
    def test_repeated_commits_refuse_and_release_every_pin(self):
        api=self.api();count=[]
        def pin(i,db):
            value=db.execute('SELECT value FROM content').fetchone()[0]
            if i==0:
                self.writers[1].execute('UPDATE content SET value=value+1');count.append(True)
            return value
        with self.assertRaisesRegex(ValueError,'reader_snapshot_changed'):
            with api.pin_common(lambda:self.members(pin),deadline=time.monotonic()+3):pass
        self.assertEqual(len(count),2)
        self.closed()
    def test_alias_monitor_refused_before_snapshot_begin(self):
        api=self.api()
        @contextmanager
        def aliased():
            with self.members(lambda _i,db:db.execute('SELECT value FROM content').fetchone()) as members:
                path,monitor,_snapshot,pin=members[0]
                yield [(path,monitor,monitor,pin)]
        with self.assertRaisesRegex(ValueError,'reader_snapshot_input_invalid'):
            with api.pin_common(aliased,deadline=time.monotonic()+3):pass
        self.closed()
    def test_cut_fallback_pins_then_releases_real_lock_before_yield(self):
        api=self.api();lock=self.root/'publish.lock';lock.touch(mode=0o600)
        blocked=[];committed=[];cut_entered=[]
        @contextmanager
        def cut():
            fd=os.open(lock,os.O_RDWR)
            try:
                fcntl.flock(fd,fcntl.LOCK_SH);cut_entered.append(True);yield
            finally:os.close(fd)
        def pin(i,db):
            value=db.execute('SELECT value FROM content').fetchone()[0]
            if i==0:
                fd=os.open(lock,os.O_RDWR)
                try:
                    try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                    except BlockingIOError:blocked.append(True)
                    else:self.writers[1].execute('UPDATE content SET value=value+1');committed.append(True)
                finally:os.close(fd)
            return value
        with api.pin_common(lambda:self.members(pin),deadline=time.monotonic()+3,fallback=cut) as result:
            self.assertEqual((len(committed),len(blocked),len(cut_entered)),(2,1,1))
            self.assertEqual(result.values,(0,2))
            fd=os.open(lock,os.O_RDWR)
            try:
                fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                self.writers[1].execute('UPDATE content SET value=3')
            finally:os.close(fd)
            self.assertEqual(result.connections[1].execute('SELECT value FROM content').fetchone(),(2,))
        self.closed()
    def test_fallback_still_rejects_noncooperative_changes(self):
        api=self.api();entered=[];writes=[]
        @contextmanager
        def cut():entered.append(True);yield
        def pin(i,db):
            value=db.execute('SELECT value FROM content').fetchone()[0]
            if i==0:self.writers[1].execute('UPDATE content SET value=value+1');writes.append(True)
            return value
        with self.assertRaisesRegex(ValueError,'reader_snapshot_changed'):
            with api.pin_common(lambda:self.members(pin),deadline=time.monotonic()+3,fallback=cut):pass
        self.assertEqual((len(entered),len(writes)),(1,3));self.closed()
    def test_stable_absent_sidecar_is_pinned_without_creating_files(self):
        api=self.api();sidecar=self.root/'absent.sqlite';before=set(self.root.iterdir())
        @contextmanager
        def factory():
            with self.members(lambda _i,db:db.execute('SELECT value FROM content').fetchone()[0]) as base:
                yield [*base,(sidecar,None,None,None)]
        with api.pin_common(factory,deadline=time.monotonic()+3) as result:
            self.assertEqual(result.values,(0,0,None));self.assertIsNone(result.connections[2])
            self.assertFalse(sidecar.exists())
        self.assertEqual(set(self.root.iterdir()),before);self.closed()

    def test_new_sidecar_between_pins_forces_retry_with_new_connection(self):
        api=self.api();sidecar=self.root/'sidecar.sqlite';injected=[]
        def pin(i,db):
            value=db.execute('SELECT value FROM content').fetchone()[0]
            if i==0 and not injected:
                writer=sqlite3.connect(sidecar,isolation_level=None)
                writer.execute('PRAGMA journal_mode=WAL');writer.execute('CREATE TABLE content(value INTEGER NOT NULL)')
                writer.execute('INSERT INTO content VALUES(9)');self.writers.append(writer);injected.append(True)
            return value
        @contextmanager
        def factory():
            with self.members(pin) as base,ExitStack() as stack:
                members=list(base)
                if not sidecar.exists():members.append((sidecar,None,None,None))
                else:
                    monitor=stack.enter_context(closing(sqlite3.connect(sidecar.as_uri()+'?mode=ro',uri=True,isolation_level=None)))
                    snapshot=stack.enter_context(closing(sqlite3.connect(sidecar.as_uri()+'?mode=ro',uri=True,isolation_level=None)))
                    monitor.execute('PRAGMA query_only=ON');snapshot.execute('PRAGMA query_only=ON')
                    self.opened.extend((monitor,snapshot))
                    members.append((sidecar,monitor,snapshot,lambda db:db.execute('SELECT value FROM content').fetchone()[0]))
                yield members
        with api.pin_common(factory,deadline=time.monotonic()+3) as result:
            self.assertEqual(result.values,(0,0,9));self.assertIsNotNone(result.connections[2])
        self.assertEqual(injected,[True]);self.closed()
    def test_pin_callback_error_closes_all_opened_connections(self):
        api=self.api()
        def pin(i,db):
            result=db.execute('SELECT value FROM content').fetchone()
            if i==1:raise RuntimeError('synthetic_pin_failure')
            return result
        with self.assertRaisesRegex(RuntimeError,'synthetic_pin_failure'):
            with api.pin_common(lambda:self.members(pin),deadline=time.monotonic()+3):pass
        self.closed()

    def test_failed_rollback_does_not_leave_other_snapshot_pinned(self):
        api=self.api();observed=[];attempts=[]
        class Connection(sqlite3.Connection):
            def rollback(db):
                if db.fail_rollback:
                    db.fail_rollback=False;attempts.append('failed')
                    raise sqlite3.OperationalError('synthetic_rollback_failure')
                attempts.append('rolled_back')
                return super().rollback()
        @contextmanager
        def factory():
            with ExitStack() as stack:
                members=[]
                for i,path in enumerate(self.paths):
                    monitor=stack.enter_context(closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,isolation_level=None)))
                    snapshot=stack.enter_context(closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,
                        isolation_level=None,factory=Connection)))
                    snapshot.fail_rollback=(i==1)
                    monitor.execute('PRAGMA query_only=ON');snapshot.execute('PRAGMA query_only=ON')
                    members.append((path,monitor,snapshot,lambda db:db.execute('SELECT value FROM content').fetchone()))
                try:yield members
                finally:observed.extend(member[2].in_transaction for member in members)
        with self.assertRaisesRegex(sqlite3.OperationalError,'synthetic_rollback_failure'):
            with api.pin_common(factory,deadline=time.monotonic()+3):pass
        self.assertEqual(attempts,['failed','rolled_back'])
        self.assertEqual(observed,[False,True])

if __name__=='__main__':unittest.main(verbosity=2)

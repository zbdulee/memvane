#!/usr/bin/env python3
"""Actual WAL last-close and outstanding-cursor GC physical boundary."""
from contextlib import ExitStack
import gc
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
import unittest
from unittest import mock
from urllib.parse import unquote, urlparse
import weakref

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('native_close_windows',
    ROOT / 'test/fixtures/laya-reader-windows-checks.py')
existing = importlib.util.module_from_spec(spec); spec.loader.exec_module(existing)


def image(path):
    result = {}
    for suffix in ('', '-wal', '-shm', '-journal'):
        current = Path(str(path) + suffix)
        if not os.path.lexists(current):
            result[suffix] = None
        else:
            info = current.lstat()
            result[suffix] = (None if suffix == '-shm' else current.read_bytes(),
                info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_nlink)
    return result


def delta(before, after):
    return [suffix or 'main' for suffix in before if before[suffix] != after[suffix]]


def bare_control(path):
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600); os.close(fd)
    db = sqlite3.connect(path.as_uri() + '?mode=rw', uri=True, isolation_level=None)
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('CREATE TABLE sample(value TEXT NOT NULL)')
    db.execute('BEGIN IMMEDIATE')
    db.executemany('INSERT INTO sample VALUES(?)', [('one',), ('two',), ('three',)])
    db.commit()
    cursor = db.execute('SELECT value FROM sample')
    if cursor.fetchone() != ('one',):raise RuntimeError('real cursor/control prerequisite missing')
    db.close()
    closed = False
    try:db.execute('SELECT 1')
    except sqlite3.ProgrammingError:closed = True
    before = image(path)
    cursor = None; gc.collect()
    after = image(path)
    return dict(pythonClosed=closed, walAfterPythonClose=before['-wal'] is not None,
        changesAfterCursorGc=delta(before, after), walAfterCursorGc=after['-wal'] is not None,
        sqlite=sqlite3.sqlite_version, python=list(sys.version_info[:3]))


class NativeCloseChecks(unittest.TestCase):
    def setUp(self):
        self.fixture = existing.ReaderWindowsChecks('runTest')
        self.addCleanup(self.fixture.doCleanups); self.fixture.setUp()
        self.seed, self.case = self.fixture.seed, self.fixture.case

    def _cleanup_writer(self, windows, component, writer):
        if writer is not None:
            try:writer.close(deadline=time.monotonic()+1)
            except sqlite3.ProgrammingError:
                # Unsafe BASE public db.close already closed its raw SQLite
                # object, but left the real owner registered. Cleanup only:
                # use its actual final-close method under an actual EX; never
                # alter registry state or count this repair as test success.
                owner=getattr(writer,'_connection_owner',None)
                if owner is None:raise
                with windows.physical_window(component,exclusive=True,deadline=time.monotonic()+1):
                    owner.close_locked()
        windows.drain_pending(deadline=time.monotonic()+1)

    def test_direct_managed_db_close_defers_under_physical_sh_including_later_cursor_gc(self):
        windows=importlib.import_module('laya_reader_windows')
        from laya_index_a import LayaIndexA
        writer=None;cursor=None;external=ExitStack();component=None
        try:
            with self.seed.prepared():
                windows.prepare_windows(self.seed.installer)
                component=windows.component_for(self.seed.installer,'base')
                writer=LayaIndexA(self.case.db,coordination=component)
                before_write=self.case.db.read_bytes()
                body='synthetic direct-managed-close committed WAL sentinel'
                writer.ensure_content(hashlib.sha256(body.encode()).hexdigest(),body)
                if self.case.db.read_bytes()!=before_write or not Path(str(self.case.db)+'-wal').is_file():
                    raise RuntimeError('actual uncheckpointed WAL prerequisite missing')
                cursor=writer.db.execute('SELECT key,value FROM schema_meta ORDER BY key')
                if cursor.fetchone() is None:raise RuntimeError('actual held cursor prerequisite missing')
                external.enter_context(windows.physical_window(component,exclusive=False,deadline=time.monotonic()+.2))
                before=image(self.case.db);deferred=False;error=None
                try:writer.db.close()
                except windows.ReaderCloseDeferred as exc:deferred=True;error=str(exc)
                after_close=image(self.case.db);pending=windows.pending_count()
                alive=True
                try:writer.db.execute('SELECT 1').fetchone()
                except sqlite3.ProgrammingError:alive=False
                cursor=None;gc.collect();after_gc=image(self.case.db)
                print(json.dumps(dict(case='direct-managed-db-close',deferred=deferred,error=error,
                    pending=pending,actualConnectionAlive=alive,changesAtClose=delta(before,after_close),
                    changesAfterCursorGc=delta(before,after_gc),walPresentAfterGc=after_gc['-wal'] is not None)),flush=True)
                with self.subTest(boundary='public-db-close-dispatches-owner'):
                    self.assertTrue(deferred);self.assertEqual(pending,1);self.assertTrue(alive)
                with self.subTest(boundary='native-effects-blocked-by-sh-even-cursor-gc'):
                    self.assertEqual(delta(before,after_close),[]);self.assertEqual(delta(before,after_gc),[])
                external.close()
                with self.subTest(boundary='pending-handle-lifetime-survives-reader-sh'):
                    with self.assertRaisesRegex(ValueError,'reader_lifetime_busy'):
                        with self.seed.lease.reader_lifetime(self.seed.install,exclusive=True):pass
                self.assertEqual(windows.drain_pending(deadline=time.monotonic()+1),0)
        finally:
            external.close();cursor=None;gc.collect()
            if component is not None:self._cleanup_writer(windows,component,writer)

    def test_one_cursor_close_failure_keeps_real_owner_until_same_thread_native_retry(self):
        windows=importlib.import_module('laya_reader_windows')
        from laya_index_a import LayaIndexA
        writer=None;cursor=None;component=None;events=[];references=[];attempts=[]
        actual_connect=sqlite3.connect
        try:
            with self.seed.prepared():
                windows.prepare_windows(self.seed.installer)
                component=windows.component_for(self.seed.installer,'base')
                def ex_held():
                    try:
                        with windows.physical_window(component,exclusive=False,deadline=time.monotonic()+.03):return False
                    except windows.ReaderWindowBusy:return True
                class OneFailureCursor(sqlite3.Cursor):
                    def close(cur):
                        attempts.append(dict(attempt=len(attempts)+1,physicalExclusive=ex_held()))
                        if len(attempts)==1:raise sqlite3.OperationalError('synthetic_one_time_actual_cursor_close_failure')
                        return super().close()
                def connect(database,*args,**kwargs):
                    # Preserve the product's supplied managed factory and all
                    # of its cursor tracking/public-close/native-close logic.
                    supplied=kwargs.get('factory',sqlite3.Connection)
                    class ObserveNative(supplied):
                        def __init__(db,*a,**kw):
                            super().__init__(*a,**kw)
                            raw=str(database);path=Path(unquote(urlparse(raw).path)) if raw.startswith('file:') else Path(raw)
                            if path==self.case.db and 'mode=rw' in raw:references.append(weakref.ref(db))
                        def _native_close_locked(db):
                            held=ex_held()
                            result=super()._native_close_locked()
                            events.append(dict(event='actual-native-close-completed',physicalExclusive=held))
                            return result
                    kwargs['factory']=ObserveNative
                    return actual_connect(database,*args,**kwargs)
                with mock.patch.object(sqlite3,'connect',side_effect=connect):
                    writer=LayaIndexA(self.case.db,coordination=component)
                cursor=writer.db.cursor(factory=OneFailureCursor)
                cursor.execute('SELECT key,value FROM schema_meta ORDER BY key')
                if cursor.fetchone() is None or not references:raise RuntimeError('actual managed cursor/RW prerequisite missing')
                before_events=len(events);error=None
                try:writer.close(deadline=time.monotonic()+.2)
                except (sqlite3.Error,windows.ReaderCloseDeferred) as exc:error=type(exc).__name__+':'+str(exc);exc.__traceback__=None
                pending=windows.pending_count();alive=existing.ReaderWindowsChecks.live(references[-1])
                closes_before_retry=len(events)-before_events
                print(json.dumps(dict(case='actual-cursor-close-one-failure',error=error,attempts=list(attempts),
                    pending=pending,actualConnectionAlive=alive,nativeCompletedBeforeCursorSuccess=closes_before_retry)),flush=True)
                with self.subTest(boundary='cursor-failure-before-native-close'):
                    self.assertIsNotNone(error);self.assertEqual(len(attempts),1)
                    self.assertTrue(attempts[0]['physicalExclusive']);self.assertEqual(closes_before_retry,0)
                    self.assertEqual(pending,1);self.assertTrue(alive)
                with self.subTest(boundary='failed-cursor-close-retains-lifetime'):
                    with self.assertRaisesRegex(ValueError,'reader_lifetime_busy'):
                        with self.seed.lease.reader_lifetime(self.seed.install,exclusive=True):pass
                remaining=windows.drain_pending(deadline=time.monotonic()+1)
                with self.subTest(boundary='same-thread-retries-cursor-then-native-close'):
                    self.assertEqual(remaining,0);self.assertEqual(len(attempts),2)
                    self.assertTrue(attempts[-1]['physicalExclusive']);self.assertEqual(len(events)-before_events,1)
                    self.assertTrue(events[-1]['physicalExclusive'])
                    existing.ReaderWindowsChecks.assert_closed(self,references[-1])
                cursor=None;gc.collect()
        finally:
            cursor=None;gc.collect()
            if component is not None:self._cleanup_writer(windows,component,writer)

    def test_outstanding_cursor_cannot_finalize_last_writer_after_physical_ex_release(self):
        baseline = bare_control(self.case.home / 'native-close-control.sqlite')
        print(json.dumps(dict(case='bare-native-close-control', **baseline)), flush=True)
        windows = importlib.import_module('laya_reader_windows')
        from laya_index_a import LayaIndexA
        writer = None; cursor = None; external = ExitStack()
        try:
            with self.seed.prepared():
                windows.prepare_windows(self.seed.installer)
                component = windows.component_for(self.seed.installer, 'base')
                writer = LayaIndexA(self.case.db, coordination=component)
                before_write = self.case.db.read_bytes()
                body = 'actual native last-close uncheckpointed content'
                writer.ensure_content(hashlib.sha256(body.encode()).hexdigest(), body)
                if self.case.db.read_bytes() != before_write:
                    raise RuntimeError('real uncheckpointed WAL prerequisite missing')
                if not Path(str(self.case.db) + '-wal').is_file():
                    raise RuntimeError('committed WAL prerequisite missing')
                count = writer.db.execute('SELECT count(*) FROM schema_meta').fetchone()[0]
                if count < 2:raise RuntimeError('multiple actual schema rows required for held cursor')
                cursor = writer.db.execute('SELECT key,value FROM schema_meta ORDER BY key')
                if cursor.fetchone() is None:raise RuntimeError('actual outstanding cursor missing')
                self.assertFalse(writer.db.in_transaction, 'implicit read cursor is not an explicit BEGIN')
                deferred = False
                try:writer.close(deadline=time.monotonic() + .1)
                except windows.ReaderCloseDeferred:deferred = True
                pending = windows.pending_count()
                if deferred:
                    self.assertEqual(pending, 1)
                    self.assertEqual(writer.db.execute('SELECT 1').fetchone(), (1,))
                external.enter_context(windows.physical_window(component,
                    exclusive=False, deadline=time.monotonic() + .2))
                before_gc = image(self.case.db)
                cursor = None; gc.collect()
                after_gc = image(self.case.db)
                changed = delta(before_gc, after_gc)
                print(json.dumps(dict(case='owned-native-close-cursor-gc', deferred=deferred,
                    pendingAtClose=pending, walBeforeGc=before_gc['-wal'] is not None,
                    changedDuringPhysicalSH=changed, pendingAfterGc=windows.pending_count(),
                    rawDriverControl=baseline)), flush=True)
                self.assertTrue(not changed, 'native cursor GC changes files under physical SH: ' + str(changed))
                if deferred:
                    self.assertEqual(windows.drain_pending(deadline=time.monotonic() + .03), 1)
                external.close()
                self.assertEqual(windows.drain_pending(deadline=time.monotonic() + 1), 0)
                writer.close(deadline=time.monotonic() + 1); writer = None
                self.assertEqual(windows.pending_count(), 0)
        finally:
            external.close()
            cursor = None; gc.collect()
            if writer is not None:writer.close(deadline=time.monotonic() + 1)
            windows.drain_pending(deadline=time.monotonic() + 1)


if __name__ == '__main__':unittest.main(verbosity=2)

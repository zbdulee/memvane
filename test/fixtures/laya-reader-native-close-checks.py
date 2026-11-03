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

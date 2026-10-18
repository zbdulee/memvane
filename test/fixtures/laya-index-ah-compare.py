#!/usr/bin/env python3
"""Disposable A/H full-membership comparison; no QMD, models or user files.

A: one SQLite DB for control, content and generation membership.
H: control DB, shared content DB and one immutable membership DB per generation.
Both use the same deterministic rows and the same sequential crash/retry script.
This is a physical-layout probe, not an implementation of Laya Index.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import resource
import signal
import sqlite3
import tempfile
import time


def connect(path):
    db = sqlite3.connect(path, timeout=2)
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('PRAGMA synchronous=FULL')
    return db


def content_rows():
    return [(hashlib.sha256(f'body-{i}'.encode()).hexdigest(), f'body-{i}')
            for i in range(480)]


def memberships(version, contents):
    rows = []
    for scope in ('alpha', 'beta'):
        for i in range(300):
            if scope == 'alpha' and version > 0 and i < 20:
                continue
            number = (i + (version * 31 if scope == 'alpha' and 20 <= i < 40 else 0)) % 480
            rows.append((scope, f'doc-{i:04d}', contents[number][0]))
    return rows


def fingerprint(rows):
    return hashlib.sha256(json.dumps(sorted(rows), separators=(',', ':')).encode()).hexdigest()


class Store:
    def __init__(self, root, mode):
        self.root, self.mode = root, mode
        self.query_readers = {}
        self.control = connect(root / 'control.sqlite')
        self.control.executescript('''
          CREATE TABLE head(singleton INTEGER PRIMARY KEY CHECK(singleton=1), gen TEXT);
          INSERT INTO head VALUES(1,NULL);
          CREATE TABLE generations(gen TEXT PRIMARY KEY, state TEXT NOT NULL, digest TEXT NOT NULL);
          CREATE TABLE receipts(claim TEXT PRIMARY KEY, gen TEXT NOT NULL);
          CREATE TABLE pins(gen TEXT PRIMARY KEY, count INTEGER NOT NULL);
        ''')
        if mode == 'A':
            self.content = self.control
            self.control.execute('CREATE TABLE content(hash TEXT PRIMARY KEY, body TEXT NOT NULL)')
            self.control.execute('CREATE TABLE members(gen TEXT, scope TEXT, path TEXT, hash TEXT, PRIMARY KEY(gen,scope,path))')
        else:
            self.content = connect(root / 'content.sqlite')
            self.content.execute('CREATE TABLE content(hash TEXT PRIMARY KEY, body TEXT NOT NULL)')
        self.content.executemany('INSERT INTO content VALUES(?,?)', content_rows())
        self.content.commit()
        self.control.commit()

    def member_db(self, gen):
        if self.mode == 'A':
            return self.control
        # Existing generation files are never opened for writing after stage.
        uri = (self.root / f'{gen}.sqlite').as_uri() + '?mode=ro'
        return sqlite3.connect(uri, uri=True, timeout=2)

    def stage(self, gen, rows):
        if self.mode == 'A':
            self.control.executemany('INSERT INTO members VALUES(?,?,?,?)',
                                     [(gen, *row) for row in rows])
        else:
            path = self.root / f'{gen}.sqlite'
            temporary = self.root / f'.{gen}.stage.sqlite'
            if path.exists() or temporary.exists():
                raise FileExistsError(gen)
            db = sqlite3.connect(temporary)
            db.execute('CREATE TABLE members(scope TEXT,path TEXT,hash TEXT,PRIMARY KEY(scope,path))')
            db.executemany('INSERT INTO members VALUES(?,?,?)', rows)
            db.commit()
            assert db.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
            db.close()
            temporary.chmod(0o400)
            with temporary.open('rb') as stream:
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            directory = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        self.control.execute('INSERT INTO generations VALUES(?,?,?)',
                             (gen, 'prepared', fingerprint(rows)))
        self.control.commit()

    def publish(self, expected, gen, claim):
        self.control.execute('BEGIN IMMEDIATE')
        try:
            prior = self.control.execute('SELECT gen FROM receipts WHERE claim=?', (claim,)).fetchone()
            if prior is not None:
                self.control.rollback()
                return prior == (gen,)  # Lost queue ACK: no second publish.
            current = self.control.execute('SELECT gen FROM head WHERE singleton=1').fetchone()[0]
            state = self.control.execute('SELECT state FROM generations WHERE gen=?', (gen,)).fetchone()
            if current != expected or state != ('prepared',):
                self.control.rollback()
                return False
            self.control.execute('UPDATE head SET gen=? WHERE singleton=1', (gen,))
            self.control.execute('UPDATE generations SET state=? WHERE gen=?', ('published', gen))
            self.control.execute('INSERT INTO receipts VALUES(?,?)', (claim, gen))
            self.control.commit()
            return True
        except Exception:
            self.control.rollback()
            raise

    def pin(self):
        # BEGIN IMMEDIATE serializes pointer read and pin registration.
        self.control.execute('BEGIN IMMEDIATE')
        gen = self.control.execute('SELECT gen FROM head').fetchone()[0]
        if gen is None:
            self.control.rollback()
            raise ValueError('no_published_generation')
        self.control.execute('INSERT INTO pins VALUES(?,1) ON CONFLICT(gen) DO UPDATE SET count=count+1', (gen,))
        self.control.commit()
        return gen

    def release(self, gen):
        self.control.execute('UPDATE pins SET count=count-1 WHERE gen=?', (gen,))
        self.control.execute('DELETE FROM pins WHERE count=0')
        self.control.commit()

    def query(self, gen, scope, count=10):
        self.require_visible(gen)
        if self.mode == 'A':
            db = self.control
        else:
            if gen not in self.query_readers:
                self.query_readers[gen] = self.member_db(gen)
            db = self.query_readers[gen]
        if self.mode == 'A':
            return db.execute('SELECT m.path,c.body FROM members m JOIN content c ON c.hash=m.hash WHERE m.gen=? AND m.scope=? ORDER BY m.path LIMIT ?',
                              (gen, scope, count)).fetchall()
        members = db.execute('SELECT path,hash FROM members WHERE scope=? ORDER BY path LIMIT ?',
                             (scope, count)).fetchall()
        hashes = sorted({hash_ for _, hash_ in members})
        if hashes:
            bodies = dict(self.content.execute(
                'SELECT hash,body FROM content WHERE hash IN (' +
                ','.join('?' for _ in hashes) + ')', hashes).fetchall())
        else:
            bodies = {}
        return [(path, bodies[hash_]) for path, hash_ in members]

    def has_path(self, gen, scope, path):
        self.require_visible(gen)
        db = self.member_db(gen)
        try:
            if self.mode == 'A':
                return db.execute('SELECT 1 FROM members WHERE gen=? AND scope=? AND path=?',
                                  (gen, scope, path)).fetchone() is not None
            return db.execute('SELECT 1 FROM members WHERE scope=? AND path=?',
                              (scope, path)).fetchone() is not None
        finally:
            if db is not self.control:
                db.close()

    def require_visible(self, gen):
        head = self.control.execute('SELECT gen FROM head').fetchone()[0]
        pin = self.control.execute('SELECT count FROM pins WHERE gen=?', (gen,)).fetchone()
        if gen != head and (pin is None or pin[0] < 1):
            raise ValueError('generation_not_pinned')

    def orphan_inventory(self):
        if self.mode != 'H':
            return []
        tracked = {r[0] for r in self.control.execute('SELECT gen FROM generations')}
        return sorted(p.name for p in self.root.glob('g*.sqlite') if p.stem not in tracked) + sorted(
            p.name for p in self.root.glob('.g*.stage.sqlite'))

    def gc(self):
        self.control.execute('BEGIN IMMEDIATE')
        head = self.control.execute('SELECT gen FROM head').fetchone()[0]
        kept = {head} | {r[0] for r in self.control.execute('SELECT gen FROM pins')}
        kept |= {r[0] for r in self.control.execute("SELECT gen FROM generations WHERE state='prepared'")}
        all_gens = {r[0] for r in self.control.execute('SELECT gen FROM generations')}
        stale = all_gens - kept
        for gen in stale:
            if self.mode == 'A':
                self.control.execute('DELETE FROM members WHERE gen=?', (gen,))
            self.control.execute('DELETE FROM generations WHERE gen=?', (gen,))
        self.control.commit()
        if self.mode == 'H':
            # The control commit precedes unlink. An interrupted unlink leaves
            # an orphan for later inventory; it must never delete a rooted file.
            for gen in stale:
                (self.root / f'{gen}.sqlite').unlink()
        return sorted(stale)

    def recover(self):
        assert self.control.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
        assert self.content.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
        head = self.control.execute('SELECT gen FROM head').fetchone()[0]
        assert head in {r[0] for r in self.control.execute('SELECT gen FROM generations')}
        referenced = set()
        for gen, digest in self.control.execute('SELECT gen,digest FROM generations'):
            db = self.member_db(gen)
            try:
                assert db.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
                if self.mode == 'A':
                    rows = db.execute('SELECT scope,path,hash FROM members WHERE gen=?', (gen,)).fetchall()
                else:
                    rows = db.execute('SELECT scope,path,hash FROM members').fetchall()
                assert fingerprint(rows) == digest
                referenced.update(row[2] for row in rows)
            finally:
                if db is not self.control:
                    db.close()
        available = {r[0] for r in self.content.execute('SELECT hash FROM content')}
        assert referenced <= available
        return head

    def close(self):
        for db in self.query_readers.values():
            db.close()
        if self.content is not self.control:
            self.content.close()
        self.control.close()


def run(mode):
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix='laya-ah-') as temp:
        root = Path(temp)
        contents = content_rows()
        store = Store(root, mode)
        queue = connect(root / 'queue.sqlite')  # separate runtime queue in both modes
        queue.execute('CREATE TABLE claims(id TEXT PRIMARY KEY, acked INTEGER NOT NULL)')
        queue.execute('INSERT INTO claims VALUES(?,0)', ('claim-g1',))
        queue.commit()
        stages = {}
        t = time.perf_counter()
        store.stage('g0', memberships(0, contents))
        assert store.publish(None, 'g0', 'claim-g0')
        store.stage('g1', memberships(1, contents))
        stages['stage_sec'] = time.perf_counter() - t
        old_pin = store.pin()
        assert old_pin == 'g0'
        t = time.perf_counter()
        assert store.publish('g0', 'g1', 'claim-g1')
        assert not store.publish('g0', 'g1', 'late-writer')
        stages['publish_sec'] = time.perf_counter() - t
        # Crash/lost ACK: receipt and head committed, queue still pending.
        assert queue.execute('SELECT acked FROM claims').fetchone()[0] == 0
        assert store.publish('g0', 'g1', 'claim-g1')  # exact same claim replay
        assert not store.publish('g1', 'g2', 'claim-g1')  # conflicting claim identity
        receipt = store.control.execute('SELECT gen FROM receipts WHERE claim=?', ('claim-g1',)).fetchone()
        assert receipt == ('g1',)
        queue.execute('UPDATE claims SET acked=1 WHERE id=? AND acked=0', ('claim-g1',))
        queue.commit()
        assert queue.execute('SELECT acked FROM claims').fetchone()[0] == 1
        assert store.has_path('g0', 'alpha', 'doc-0000')
        assert not store.has_path('g1', 'alpha', 'doc-0000')
        assert store.has_path('g1', 'beta', 'doc-0000')
        t = time.perf_counter()
        expected = None
        for _ in range(30):
            got = store.query('g1', 'alpha')
            expected = got if expected is None else expected
            assert got == expected and len(got) == 10
        stages['query30_sec'] = time.perf_counter() - t
        store.stage('g2', memberships(2, contents))
        assert store.gc() == []  # published, pinned and prepared roots
        assert store.has_path(old_pin, 'alpha', 'doc-0000')
        store.release(old_pin)
        t = time.perf_counter()
        assert store.gc() == ['g0']
        stages['gc_sec'] = time.perf_counter() - t
        if mode == 'H':
            assert store.orphan_inventory() == []
            orphan = root / 'g-interrupted.sqlite'
            orphan.write_bytes(b'interrupted stage')
            assert store.orphan_inventory() == [orphan.name]
            orphan.unlink()  # disposable fixture only; no product cleanup policy
        assert store.recover() == 'g1'
        store.close()
        queue.close()
        # Reopen demonstrates minimal clean-process recovery, not power loss.
        check = sqlite3.connect(root / 'control.sqlite')
        assert check.execute('SELECT gen FROM head').fetchone()[0] == 'g1'
        assert {r[0] for r in check.execute('SELECT gen FROM generations')} == {'g1', 'g2'}
        check.close()
        files = [p for p in root.iterdir() if p.is_file()]
        footprint = sum(p.stat().st_size for p in files)
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (
            1 if platform.system() == 'Darwin' else 1024)
        assert footprint <= 8 * 1024 * 1024 and len(files) <= 24
        assert rss <= 64 * 1024 * 1024
        checks = ['visibility', 'deletion', 'scope', 'stale_writer', 'pin',
                  'prepared_gc_root', 'same_claim_retry_after_lost_ack',
                  'content_reference', 'reopen']
        if mode == 'H':
            checks.append('orphan_inventory')
        result = {'mode': mode, 'rows_per_generation_initial': 600,
                  'generations_after_gc': ['g1', 'g2'], 'checks': checks,
                  'elapsed_sec': round(time.perf_counter() - started, 6),
                  'stages_sec': {k: round(v, 6) for k, v in stages.items()},
                  'file_count': len(files), 'bytes': footprint,
                  'files': {p.name: p.stat().st_size for p in sorted(files)},
                  'rss_peak_bytes': rss}
        return result


if __name__ == '__main__':
    if not __debug__:
        raise SystemExit('assertions_required')
    resource.setrlimit(resource.RLIMIT_CPU, (10, 10))
    resource.setrlimit(resource.RLIMIT_FSIZE, (64 * 1024 * 1024, 64 * 1024 * 1024))
    def wall_timeout(*_):
        raise TimeoutError('wall_limit')
    signal.signal(signal.SIGALRM, wall_timeout)
    signal.alarm(30)
    parser = argparse.ArgumentParser()
    parser.add_argument('--mode', choices=('A', 'H'), required=True)
    args = parser.parse_args()
    print(json.dumps(run(args.mode), sort_keys=True))

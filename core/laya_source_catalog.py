"""Durable, step-bounded source catalog for explicit Laya collection roots.

The source port proves source state with one synchronous full scan per call and
treats ``max_files``/``max_bytes`` as a whole-corpus cap. This catalog keeps the
same source contract but spreads the work over durable steps:

  * ``request_scan()`` records one pending scan; it never touches the source.
  * ``step()`` runs one ``BEGIN IMMEDIATE`` transaction with a finite budget:
    ``max_files`` units (one directory listing or one file hash each),
    ``max_bytes`` streamed file bytes and ``max_stats`` verification stats.
    Phases: enumerate -> hash -> verify_dirs -> verify_files -> publish.
  * The complete snapshot pointer moves only in the transaction that finishes
    verification. Pending, deferred or blocked scans never replace it, and a
    partial listing is never evidence of deletion.
  * Serving is allowed only while the newest requested scan is the current
    complete one. A pending or blocked newer scan fails closed.

Source validation reuses the port helpers: canonical no-follow root binding,
hidden-entry skip, symlink/device/non-regular ``.md`` rejection, safe UTF-8
names, depth/entry bounds and strict UTF-8 content. Violations block the scan;
nothing is skipped silently.

Limitations (see docs/plans/2026-10-19-laya-source-batched-scan.md):
  * Serving rehashes returned rows and stat-sweeps every snapshot directory
    and file (``verify_unchanged``) so an out-of-band add, delete or edit of an
    unreturned file fails closed. The sweep is a stat-signature check, not the
    port's per-query rehash, and is bounded by ``max_verify_stats``; a larger
    snapshot fails closed until a change feed can prove freshness.
  * A single file larger than one step's byte budget defers the scan: stdlib
    hash state cannot be persisted, so one file's hash cannot span steps.
  * ``stage_from_catalog`` still materializes the whole generation because
    ``LayaIndexA.stage`` holds every row in one transaction.
"""
from __future__ import annotations

import codecs
import errno
import hashlib
import json
import os
import sqlite3
import stat
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, FrozenSet, Iterator, List, Optional, Tuple

from laya_index_a import IndexContractError, _digest
from laya_index_source_port import (MAX_DEPTH, MAX_ENTRIES, SourceStale,
                                    _COLLECTION, _DIR_FLAGS, _FILE_FLAGS,
                                    _READ_CHUNK, _check_root, _load_exclusions,
                                    _open_root_fd, _positive_int, _raise_stale,
                                    _require_platform, _safe_rel, _seal, _sig)

APP_ID = 0x4c594343  # LYCC; distinct from the A-int database identity
SCHEMA_VERSION = 1
DEFAULT_STEP_FILES = 200
DEFAULT_STEP_BYTES = 5_000_000
DEFAULT_STEP_STATS = 2_000
DEFAULT_VERIFY_BYTES = 5_000_000
DEFAULT_VERIFY_STATS = 10_000
PENDING_PHASES = ('enumerate', 'hash', 'verify_dirs', 'verify_files')
MAX_COLLECTIONS = 16
_BUSY_TIMEOUT = 0.2
_PAGE = 256
_CHANGED_ERRNOS = frozenset({errno.ENOENT, errno.ENOTDIR, errno.ELOOP})
_SCAN_COLUMNS = ('seq', 'scan_id', 'phase', 'cursor', 'entries', 'steps',
                 'rerequested', 'blocked_reason', 'detail', 'deferred_reason',
                 'deferred_path', 'raw_digest', 'files', 'bytes', 'started_ns',
                 'finished_ns')

_SCHEMA = (
    'CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)',
    """CREATE TABLE scans(
         seq INTEGER PRIMARY KEY AUTOINCREMENT,
         scan_id TEXT NOT NULL UNIQUE,
         phase TEXT NOT NULL CHECK(phase IN ('enumerate','hash','verify_dirs',
                                            'verify_files','complete','blocked')),
         cursor TEXT NOT NULL DEFAULT '',
         entries INTEGER NOT NULL DEFAULT 0,
         steps INTEGER NOT NULL DEFAULT 0,
         rerequested INTEGER NOT NULL DEFAULT 0,
         blocked_reason TEXT, detail TEXT,
         deferred_reason TEXT, deferred_path TEXT,
         raw_digest TEXT, files INTEGER, bytes INTEGER,
         started_ns INTEGER NOT NULL, finished_ns INTEGER)""",
    # active = newest requested scan; current = newest complete snapshot.
    'CREATE TABLE active(id INTEGER PRIMARY KEY CHECK(id=1), seq INTEGER NOT NULL)',
    'CREATE TABLE current(id INTEGER PRIMARY KEY CHECK(id=1), seq INTEGER NOT NULL)',
    """CREATE TABLE scan_dirs(
         seq INTEGER NOT NULL, rel TEXT NOT NULL, collection TEXT NOT NULL,
         depth INTEGER NOT NULL, node TEXT, sig TEXT,
         listed INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(seq, rel))""",
    'CREATE INDEX scan_dirs_unlisted ON scan_dirs(seq, rel) WHERE listed=0',
    """CREATE TABLE scan_files(
         seq INTEGER NOT NULL, path TEXT NOT NULL, dir_rel TEXT NOT NULL,
         name TEXT NOT NULL, size INTEGER NOT NULL, sig TEXT NOT NULL,
         sha256 TEXT, PRIMARY KEY(seq, path))""",
)

Exclusions = Tuple[FrozenSet[str], FrozenSet[str]]


class _Blocked(Exception):
    """A durable scan block with a stable reason and a diagnostic detail."""

    def __init__(self, reason: str, detail: str):
        super().__init__(reason, detail)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class CatalogSnapshot:
    scope: str
    seq: int
    scan_id: str
    raw_digest: str
    files: int
    bytes: int
    completed_ns: int


class _Work:
    def __init__(self, max_files: int, max_bytes: int, max_stats: int):
        self.max_files = max_files
        self.max_bytes = max_bytes
        self.max_stats = max_stats
        self.units = 0
        self.bytes = 0
        self.stats = 0
        self.dirs = 0
        self.files = 0

    def unit(self) -> bool:
        if self.units >= self.max_files:
            return False
        self.units += 1
        return True

    def fits(self, size: int) -> bool:
        return self.units < self.max_files and self.bytes + size <= self.max_bytes

    def stat(self) -> bool:
        if self.stats >= self.max_stats:
            return False
        self.stats += 1
        return True

    def report(self) -> Dict[str, int]:
        return {'units': self.units, 'dirs_listed': self.dirs,
                'files_hashed': self.files, 'bytes_read': self.bytes,
                'stats': self.stats}


def _sig_text(st: os.stat_result) -> str:
    return ':'.join(str(value) for value in _sig(st))


def _node_text(st: os.stat_result) -> str:
    return f'{st.st_dev}:{st.st_ino}'


def _detail(exc: IndexContractError) -> str:
    cause = exc.__cause__
    return str(cause) if isinstance(cause, SourceStale) else str(exc)


def _is_busy(exc: sqlite3.OperationalError) -> bool:
    message = str(exc).lower()
    return 'locked' in message or 'busy' in message


def _check_roots(roots: Any) -> Dict[str, str]:
    """Same root vocabulary as ``scan_collections``."""
    if not isinstance(roots, Mapping) or not 1 <= len(roots) <= MAX_COLLECTIONS:
        raise ValueError('invalid collection roots')
    if any(not isinstance(name, str) or not _COLLECTION.fullmatch(name)
           for name in roots):
        raise ValueError('invalid collection name')
    ordered: Dict[str, str] = {}
    for name, root in sorted(roots.items()):
        ordered[name] = _check_root(root)
    values = tuple(ordered.values())
    if len(set(values)) != len(values) or any(
            left.startswith(right + '/') or right.startswith(left + '/')
            for i, left in enumerate(values) for right in values[i + 1:]):
        _raise_stale('collection roots overlap')
    return ordered


def _binding(scope: str, roots: Mapping[str, str]) -> str:
    return hashlib.sha256(json.dumps([scope, sorted(roots.items())],
                                     ensure_ascii=False,
                                     separators=(',', ':')).encode('utf-8')).hexdigest()


def _hash_entry(dfd: int, name: str, rel: str, size: int, sig: str) -> str:
    """Stream one enumerated file: SHA-256 plus strict UTF-8, no whole-file buffer."""
    fd = os.open(name, _FILE_FLAGS, dir_fd=dfd)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or _sig_text(before) != sig:
            raise _Blocked('source_changed', f'changed before read: {rel}')
        digest = hashlib.sha256()
        decoder = codecs.getincrementaldecoder('utf-8')()
        total = 0
        try:
            while True:
                chunk = os.read(fd, min(_READ_CHUNK, size + 1 - total))
                if not chunk:
                    break
                total += len(chunk)
                if total > size:
                    raise _Blocked('source_changed', f'changed during read: {rel}')
                digest.update(chunk)
                decoder.decode(chunk)
            decoder.decode(b'', True)
        except UnicodeDecodeError:
            raise _Blocked('source_invalid', f'invalid UTF-8: {rel}') from None
        after = os.fstat(fd)
    finally:
        os.close(fd)
    if _sig_text(after) != sig or total != size:
        raise _Blocked('source_changed', f'changed during read: {rel}')
    return digest.hexdigest()


def _read_exact(dfd: int, name: str, rel: str, size: int) -> bytes:
    """Read one cataloged file for serving; bounded by its recorded size."""
    fd = os.open(name, _FILE_FLAGS, dir_fd=dfd)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size != size:
            raise _Blocked('source_changed', f'size changed: {rel}')
        chunks: List[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, min(_READ_CHUNK, size + 1 - total))
            if not chunk:
                break
            total += len(chunk)
            if total > size:
                raise _Blocked('source_changed', f'changed during read: {rel}')
            chunks.append(chunk)
        after = os.fstat(fd)
    finally:
        os.close(fd)
    if _sig_text(after) != _sig_text(before) or total != size:
        raise _Blocked('source_changed', f'changed during read: {rel}')
    return b''.join(chunks)


class _DirOpener:
    """No-follow directory chain opener bound to the nodes one scan recorded."""

    def __init__(self, db: sqlite3.Connection, roots: Mapping[str, str], seq: int):
        self._db = db
        self._roots = roots
        self._seq = seq
        self._root_fds: Dict[str, int] = {}
        self._last: Optional[Tuple[str, int]] = None

    def _expected(self, rel: str) -> Optional[str]:
        row = self._db.execute('SELECT node FROM scan_dirs WHERE seq=? AND rel=?',
                               (self._seq, rel)).fetchone()
        return None if row is None else row[0]

    def root(self, collection: str) -> int:
        fd = self._root_fds.get(collection)
        if fd is not None:
            return fd
        try:
            fd = _open_root_fd(self._roots[collection])
        except OSError as exc:
            raise _Blocked('root_unavailable',
                           f'{collection}: {exc.strerror or exc}') from None
        except IndexContractError as exc:
            raise _Blocked('source_changed', _detail(exc)) from None
        expected = self._expected(collection + '/')
        if expected is not None and expected != _node_text(os.fstat(fd)):
            os.close(fd)
            raise _Blocked('source_changed', f'collection root replaced: {collection}')
        self._root_fds[collection] = fd
        return fd

    def root_dev(self, collection: str) -> int:
        return os.fstat(self.root(collection)).st_dev

    def open_dir(self, rel: str) -> int:
        """Return a cached fd for ``collection/a/b/``; the opener owns it."""
        if self._last is not None and self._last[0] == rel:
            return self._last[1]
        parts = rel[:-1].split('/')
        fd = os.dup(self.root(parts[0]))
        try:
            prefix = parts[0] + '/'
            for part in parts[1:]:
                prefix += part + '/'
                child = os.open(part, _DIR_FLAGS, dir_fd=fd)
                os.close(fd)
                fd = child
                if self._expected(prefix) != _node_text(os.fstat(fd)):
                    raise _Blocked('source_changed', f'directory replaced: {prefix}')
        except BaseException:
            os.close(fd)
            raise
        self._drop_last()
        self._last = (rel, fd)
        return fd

    def _drop_last(self) -> None:
        if self._last is not None:
            os.close(self._last[1])
            self._last = None

    def close(self) -> None:
        self._drop_last()
        while self._root_fds:
            os.close(self._root_fds.popitem()[1])


class LayaSourceCatalog:
    """One catalog DB bound to an exact scope and set of disjoint collection roots.

    The catalog file is separate from the A-int DB, whose header check rejects
    extra schema objects. One instance is single-threaded; concurrent writers
    in other instances or processes receive ``busy``.
    """

    def __init__(self, path: Any, roots: Mapping[str, str], scope: str, *,
                 create: bool = False):
        self.roots = _check_roots(roots)
        if not isinstance(scope, str) or not scope:
            raise TypeError('scope must be a non-empty str')
        self.scope = scope
        self.binding = _binding(scope, self.roots)
        path = Path(path)
        if not path.parent.is_dir() or path.is_symlink():
            raise IndexContractError('invalid_catalog_path')
        if create:
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            except FileExistsError as exc:
                raise IndexContractError('catalog_already_exists') from exc
            os.close(fd)
        elif not path.is_file():
            raise IndexContractError('catalog_missing')
        self.path = path
        self.db: Optional[sqlite3.Connection] = sqlite3.connect(
            str(path), timeout=_BUSY_TIMEOUT, isolation_level=None)
        try:
            mode = self.db.execute('PRAGMA journal_mode=WAL').fetchone()
            if mode is None or str(mode[0]).lower() != 'wal':
                raise IndexContractError('wal_unavailable')
            self.db.execute('PRAGMA synchronous=FULL')
            if create:
                self._initialize()
            self._check_identity()
        except sqlite3.DatabaseError as exc:
            self._discard(path, create)
            raise IndexContractError('catalog_unreadable') from exc
        except BaseException:
            self._discard(path, create)
            raise

    def _discard(self, path: Path, create: bool) -> None:
        self.db.close()
        self.db = None
        if create:
            for suffix in ('', '-wal', '-shm', '-journal'):
                Path(str(path) + suffix).unlink(missing_ok=True)

    def _initialize(self) -> None:
        self.db.execute(f'PRAGMA application_id={APP_ID}')
        self.db.execute(f'PRAGMA user_version={SCHEMA_VERSION}')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            for ddl in _SCHEMA:
                self.db.execute(ddl)
            self.db.executemany('INSERT INTO meta VALUES(?,?)', (
                ('binding', self.binding), ('scope', self.scope),
                ('roots', json.dumps(self.roots, ensure_ascii=False, sort_keys=True))))
            self.db.commit()
        except BaseException:
            self._rollback()
            raise

    def _check_identity(self) -> None:
        try:
            if (self.db.execute('PRAGMA application_id').fetchone()[0] != APP_ID
                    or self.db.execute('PRAGMA user_version').fetchone()[0] != SCHEMA_VERSION):
                raise IndexContractError('catalog_schema_unknown')
            row = self.db.execute("SELECT value FROM meta WHERE key='binding'").fetchone()
        except sqlite3.Error as exc:
            raise IndexContractError('catalog_unreadable') from exc
        if row is None or row[0] != self.binding:
            raise IndexContractError('catalog_binding_mismatch')

    def close(self) -> None:
        if self.db is not None:
            self.db.close()
            self.db = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    # -- transactions ---------------------------------------------------------

    def _rollback(self) -> None:
        try:
            self.db.rollback()
        except sqlite3.Error:
            pass  # never replace the exception that is already propagating

    def _begin_write(self) -> bool:
        try:
            self.db.execute('BEGIN IMMEDIATE')
        except sqlite3.OperationalError as exc:
            if _is_busy(exc):
                return False
            raise
        return True

    def _read(self, fn):
        self.db.execute('BEGIN')
        try:
            result = fn()
            self.db.commit()
            return result
        except BaseException:
            self._rollback()
            raise

    def _pointer(self, table: str) -> Optional[int]:
        row = self.db.execute(f'SELECT seq FROM {table} WHERE id=1').fetchone()
        return None if row is None else row[0]

    def _scan_row(self, seq: Optional[int]) -> Optional[Dict[str, Any]]:
        if seq is None:
            return None
        row = self.db.execute(f'SELECT {",".join(_SCAN_COLUMNS)} FROM scans WHERE seq=?',
                              (seq,)).fetchone()
        return None if row is None else dict(zip(_SCAN_COLUMNS, row))

    def _drop_scan(self, seq: int) -> None:
        # Catalog metadata only; never source or A-int data.
        for table in ('scan_files', 'scan_dirs', 'scans'):
            self.db.execute(f'DELETE FROM {table} WHERE seq=?', (seq,))

    def _start_scan(self) -> Tuple[int, str]:
        current = self._pointer('current')
        stale = [seq for (seq,) in self.db.execute(
            'SELECT seq FROM scans WHERE seq IS NOT ?', (current,)).fetchall()]
        for seq in stale:
            self._drop_scan(seq)
        scan_id = uuid.uuid4().hex
        seq = self.db.execute("INSERT INTO scans(scan_id,phase,started_ns) "
                              "VALUES(?,'enumerate',?)",
                              (scan_id, time.time_ns())).lastrowid
        self.db.executemany('INSERT INTO scan_dirs(seq,rel,collection,depth) VALUES(?,?,?,0)',
                            [(seq, name + '/', name) for name in self.roots])
        self.db.execute('INSERT OR REPLACE INTO active VALUES(1,?)', (seq,))
        return seq, scan_id

    # -- scan control ---------------------------------------------------------

    def request_scan(self) -> Dict[str, Any]:
        """Start a scan, or coalesce into the pending one.

        A request during a pending scan marks it ``rerequested``. Publication
        then chains one more scan, so serving stays closed for a change that
        may have landed after its item was verified.
        """
        if not self._begin_write():
            return {'status': 'busy', 'scan_id': None}
        try:
            active = self._scan_row(self._pointer('active'))
            if active is not None and active['phase'] in PENDING_PHASES:
                self.db.execute('UPDATE scans SET rerequested=1 WHERE seq=?',
                                (active['seq'],))
                self.db.commit()
                return {'status': 'coalesced', 'scan_id': active['scan_id']}
            _seq, scan_id = self._start_scan()
            self.db.commit()
            return {'status': 'started', 'scan_id': scan_id}
        except BaseException:
            self._rollback()
            raise

    def step(self, max_files: int = DEFAULT_STEP_FILES,
             max_bytes: int = DEFAULT_STEP_BYTES,
             max_stats: int = DEFAULT_STEP_STATS) -> Dict[str, Any]:
        """Advance the active scan by at most one finite work budget."""
        work = _Work(_positive_int('max_files', max_files),
                     _positive_int('max_bytes', max_bytes),
                     _positive_int('max_stats', max_stats))
        _require_platform()
        if not self._begin_write():
            return {'status': 'busy', 'scan_id': None, 'phase': None,
                    'work': work.report()}
        try:
            scan = self._scan_row(self._pointer('active'))
            if scan is None or scan['phase'] not in PENDING_PHASES:
                self.db.rollback()
                result = self._settled(scan)
                result['work'] = work.report()
                return result
            seq = scan['seq']
            result: Dict[str, Any] = {'scan_id': scan['scan_id']}
            opener = _DirOpener(self.db, self.roots, seq)
            try:
                result.update(self._advance(scan, work, opener))
            except _Blocked as blocked:
                result.update(self._block(seq, blocked.reason, blocked.detail))
            except OSError as exc:
                reason = ('source_changed' if exc.errno in _CHANGED_ERRNOS
                          else 'source_unreadable')
                result.update(self._block(seq, reason,
                                          f'{type(exc).__name__}: {exc.strerror or exc}'))
            finally:
                opener.close()
            self.db.execute('UPDATE scans SET steps=steps+1 WHERE seq=?', (seq,))
            self.db.commit()
        except BaseException:
            self._rollback()
            raise
        result['work'] = work.report()
        return result

    @staticmethod
    def _settled(scan: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        if scan is None:
            return {'status': 'idle', 'scan_id': None, 'phase': None}
        if scan['phase'] == 'blocked':
            return {'status': 'blocked', 'scan_id': scan['scan_id'], 'phase': 'blocked',
                    'blocked': {'reason': scan['blocked_reason'], 'detail': scan['detail']}}
        return {'status': 'idle', 'scan_id': scan['scan_id'], 'phase': scan['phase']}

    def _block(self, seq: int, reason: str, detail: str) -> Dict[str, Any]:
        self.db.execute("UPDATE scans SET phase='blocked',blocked_reason=?,detail=?,"
                        "finished_ns=? WHERE seq=?", (reason, detail, time.time_ns(), seq))
        return {'status': 'blocked', 'phase': 'blocked',
                'blocked': {'reason': reason, 'detail': detail}}

    def _advance(self, scan: Dict[str, Any], work: _Work,
                 opener: _DirOpener) -> Dict[str, Any]:
        seq = scan['seq']
        state = {'phase': scan['phase'], 'cursor': scan['cursor'],
                 'entries': scan['entries']}
        self.db.execute('UPDATE scans SET deferred_reason=NULL,deferred_path=NULL '
                        'WHERE seq=?', (seq,))
        handlers = {'enumerate': self._enumerate_one, 'hash': self._hash_one,
                    'verify_dirs': self._verify_dir_one,
                    'verify_files': self._verify_file_one}
        outcome = None
        while outcome is None:
            outcome = handlers[state['phase']](seq, state, work, opener)
        if outcome == 'complete':
            return self._publish(seq)
        self.db.execute('UPDATE scans SET phase=?,cursor=?,entries=? WHERE seq=?',
                        (state['phase'], state['cursor'], state['entries'], seq))
        result: Dict[str, Any] = {'status': outcome, 'phase': state['phase']}
        if outcome == 'deferred':
            result['deferred'] = {'reason': 'file_exceeds_step_bytes',
                                  'path': state['deferred_path'],
                                  'size': state['deferred_size']}
        return result

    def _enumerate_one(self, seq: int, state: Dict[str, Any], work: _Work,
                       opener: _DirOpener) -> Optional[str]:
        row = self.db.execute('SELECT rel,collection,depth FROM scan_dirs '
                              'WHERE seq=? AND listed=0 ORDER BY rel LIMIT 1',
                              (seq,)).fetchone()
        if row is None:
            state['phase'], state['cursor'] = 'hash', ''
            return None
        if not work.unit():
            return 'pending'
        rel, collection, depth = row
        if depth > MAX_DEPTH:
            raise _Blocked('source_invalid', 'too deep')
        root_dev = opener.root_dev(collection)
        dfd = opener.open_dir(rel)
        before = os.fstat(dfd)
        names = sorted(os.listdir(dfd))
        state['entries'] += len(names)
        if state['entries'] > MAX_ENTRIES:
            raise _Blocked('source_invalid', 'too many entries')
        for name in names:
            if name.startswith('.'):
                continue
            try:
                path = _safe_rel(rel, name)
            except IndexContractError as exc:
                raise _Blocked('source_invalid', _detail(exc)) from None
            st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
            if stat.S_ISLNK(st.st_mode):
                raise _Blocked('source_invalid', f'symlink: {path}')
            if st.st_dev != root_dev:
                raise _Blocked('source_invalid', f'crosses device: {path}')
            if stat.S_ISDIR(st.st_mode):
                self.db.execute('INSERT INTO scan_dirs(seq,rel,collection,depth,node) '
                                'VALUES(?,?,?,?,?)',
                                (seq, path + '/', collection, depth + 1, _node_text(st)))
            elif name.endswith('.md'):
                if not stat.S_ISREG(st.st_mode):
                    raise _Blocked('source_invalid', f'not a regular file: {path}')
                self.db.execute('INSERT INTO scan_files(seq,path,dir_rel,name,size,sig) '
                                'VALUES(?,?,?,?,?,?)',
                                (seq, path, rel, name, st.st_size, _sig_text(st)))
        if _sig_text(os.fstat(dfd)) != _sig_text(before):
            raise _Blocked('source_changed', f'directory changed during listing: {rel}')
        self.db.execute('UPDATE scan_dirs SET listed=1,node=?,sig=? WHERE seq=? AND rel=?',
                        (_node_text(before), _sig_text(before), seq, rel))
        work.dirs += 1
        return None

    def _hash_one(self, seq: int, state: Dict[str, Any], work: _Work,
                  opener: _DirOpener) -> Optional[str]:
        row = self.db.execute('SELECT path,dir_rel,name,size,sig FROM scan_files '
                              'WHERE seq=? AND path>? ORDER BY path LIMIT 1',
                              (seq, state['cursor'])).fetchone()
        if row is None:
            state['phase'], state['cursor'] = 'verify_dirs', ''
            return None
        path, dir_rel, name, size, sig = row
        if size > work.max_bytes:
            # One file's hash cannot span steps; keep the scan pending, not skipped.
            self.db.execute("UPDATE scans SET deferred_reason='file_exceeds_step_bytes',"
                            'deferred_path=? WHERE seq=?', (path, seq))
            state['deferred_path'], state['deferred_size'] = path, size
            return 'deferred'
        if not work.fits(size):
            return 'pending'
        digest = _hash_entry(opener.open_dir(dir_rel), name, path, size, sig)
        self.db.execute('UPDATE scan_files SET sha256=? WHERE seq=? AND path=?',
                        (digest, seq, path))
        work.units += 1
        work.bytes += size
        work.files += 1
        state['cursor'] = path
        return None

    def _verify_dir_one(self, seq: int, state: Dict[str, Any], work: _Work,
                        opener: _DirOpener) -> Optional[str]:
        row = self.db.execute('SELECT rel,sig FROM scan_dirs WHERE seq=? AND rel>? '
                              'ORDER BY rel LIMIT 1', (seq, state['cursor'])).fetchone()
        if row is None:
            state['phase'], state['cursor'] = 'verify_files', ''
            return None
        if not work.stat():
            return 'pending'
        rel, sig = row
        if _sig_text(os.fstat(opener.open_dir(rel))) != sig:
            raise _Blocked('source_changed', f'directory changed after listing: {rel}')
        state['cursor'] = rel
        return None

    def _verify_file_one(self, seq: int, state: Dict[str, Any], work: _Work,
                         opener: _DirOpener) -> Optional[str]:
        row = self.db.execute('SELECT path,dir_rel,name,sig FROM scan_files '
                              'WHERE seq=? AND path>? ORDER BY path LIMIT 1',
                              (seq, state['cursor'])).fetchone()
        if row is None:
            return 'complete'
        if not work.stat():
            return 'pending'
        path, dir_rel, name, sig = row
        st = os.stat(name, dir_fd=opener.open_dir(dir_rel), follow_symlinks=False)
        if _sig_text(st) != sig:
            raise _Blocked('source_changed', f'file changed after hashing: {path}')
        state['cursor'] = path
        return None

    def _publish(self, seq: int) -> Dict[str, Any]:
        """Swap the complete snapshot pointer inside the step transaction."""
        digest = hashlib.sha256()
        files = total = 0
        cursor = self.db.execute('SELECT path,sha256,size FROM scan_files WHERE seq=? '
                                 'ORDER BY path', (seq,))
        for path, sha, size in cursor:
            if sha is None:
                raise _Blocked('catalog_inconsistent', f'unhashed at publish: {path}')
            digest.update(json.dumps([path, sha, size], ensure_ascii=False)
                          .encode('utf-8') + b'\n')
            files += 1
            total += size
        previous = self._pointer('current')
        changes = self._changes(previous, seq, files)
        raw_digest = digest.hexdigest()
        self.db.execute("UPDATE scans SET phase='complete',cursor='',raw_digest=?,files=?,"
                        'bytes=?,finished_ns=? WHERE seq=?',
                        (raw_digest, files, total, time.time_ns(), seq))
        self.db.execute('INSERT OR REPLACE INTO current VALUES(1,?)', (seq,))
        if previous is not None and previous != seq:
            self._drop_scan(previous)
        result: Dict[str, Any] = {'status': 'complete', 'phase': 'complete',
                                  'published': {'raw_digest': raw_digest, 'files': files,
                                                'bytes': total, **changes}}
        if self.db.execute('SELECT rerequested FROM scans WHERE seq=?', (seq,)).fetchone()[0]:
            result['next_scan_id'] = self._start_scan()[1]
        return result

    def _changes(self, previous: Optional[int], seq: int, files: int) -> Dict[str, int]:
        # Only two complete scans are compared; a partial listing never deletes.
        if previous is None:
            return {'added': files, 'changed': 0, 'deleted': 0}
        missing = ('SELECT COUNT(*) FROM scan_files a WHERE a.seq=? AND NOT EXISTS '
                   '(SELECT 1 FROM scan_files b WHERE b.seq=? AND b.path=a.path)')
        added = self.db.execute(missing, (seq, previous)).fetchone()[0]
        deleted = self.db.execute(missing, (previous, seq)).fetchone()[0]
        changed = self.db.execute('SELECT COUNT(*) FROM scan_files a JOIN scan_files b '
                                  'ON b.seq=? AND b.path=a.path '
                                  'WHERE a.seq=? AND a.sha256<>b.sha256',
                                  (previous, seq)).fetchone()[0]
        return {'added': added, 'changed': changed, 'deleted': deleted}

    # -- serving state --------------------------------------------------------

    @staticmethod
    def _servable(current: Optional[int],
                  active: Optional[Dict[str, Any]]) -> Tuple[bool, Optional[str]]:
        if active is not None and active['seq'] != current:
            return False, ('scan_blocked' if active['phase'] == 'blocked'
                           else 'scan_pending')
        if current is None:
            return False, 'no_complete_snapshot'
        return True, None

    def _backlog(self, seq: int) -> Dict[str, int]:
        dirs, dirs_pending = self.db.execute(
            'SELECT COUNT(*),COALESCE(SUM(listed=0),0) FROM scan_dirs WHERE seq=?',
            (seq,)).fetchone()
        files, hashed = self.db.execute(
            'SELECT COUNT(*),COUNT(sha256) FROM scan_files WHERE seq=?', (seq,)).fetchone()
        return {'dirs_known': dirs, 'dirs_pending': dirs_pending,
                'files_known': files, 'files_hashed': hashed,
                'files_pending': files - hashed}

    def status(self) -> Dict[str, Any]:
        """Serving state plus the explicit backlog of the newest requested scan."""
        def read():
            current_seq = self._pointer('current')
            active = self._scan_row(self._pointer('active'))
            servable, reason = self._servable(current_seq, active)
            out: Dict[str, Any] = {'scope': self.scope, 'servable': servable,
                                   'reason': reason, 'current': None, 'active': None}
            current = self._scan_row(current_seq)
            if current is not None:
                out['current'] = {key: current[key] for key in
                                  ('scan_id', 'raw_digest', 'files', 'bytes')}
                out['current']['completed_ns'] = current['finished_ns']
            if active is not None:
                out['active'] = {key: active[key] for key in
                                 ('scan_id', 'phase', 'steps', 'blocked_reason',
                                  'detail', 'deferred_reason', 'deferred_path')}
                out['active']['rerequested'] = bool(active['rerequested'])
                out['active']['backlog'] = self._backlog(active['seq'])
            return out
        return self._read(read)

    def require_servable(self) -> CatalogSnapshot:
        """Return the current complete snapshot or fail closed with index_stale."""
        def read():
            current_seq = self._pointer('current')
            active = self._scan_row(self._pointer('active'))
            servable, reason = self._servable(current_seq, active)
            if not servable:
                return reason
            row = self._scan_row(current_seq)
            return CatalogSnapshot(self.scope, row['seq'], row['scan_id'],
                                   row['raw_digest'], row['files'], row['bytes'],
                                   row['finished_ns'])
        snapshot = self._read(read)
        if isinstance(snapshot, str):
            _raise_stale(f'catalog not servable: {snapshot}')
        return snapshot

    def require_same(self, snapshot: CatalogSnapshot) -> None:
        if self.require_servable() != snapshot:
            _raise_stale('catalog snapshot changed')

    def _is_current(self, snapshot: CatalogSnapshot) -> bool:
        return (isinstance(snapshot, CatalogSnapshot) and snapshot.scope == self.scope
                and self._pointer('current') == snapshot.seq
                and self._pointer('active') == snapshot.seq)

    def manifest(self, snapshot: CatalogSnapshot, exclusions: Exclusions) -> str:
        """A-int source manifest for the snapshot; equals the port's for the same bytes."""
        excluded_paths, excluded_hashes = exclusions

        def read():
            if not self._is_current(snapshot):
                return None
            return [(path, sha, int(path not in excluded_paths
                                    and sha not in excluded_hashes))
                    for path, sha in self.db.execute(
                        'SELECT path,sha256 FROM scan_files WHERE seq=?', (snapshot.seq,))]
        entries = self._read(read)
        if entries is None:
            _raise_stale('catalog snapshot changed')
        return _digest(self.scope, entries)

    def lookup(self, snapshot: CatalogSnapshot, path: str) -> Optional[Tuple[str, int]]:
        """Raw ``(sha256, size)`` of one snapshot member; no servability claim."""
        row = self.db.execute('SELECT sha256,size FROM scan_files WHERE seq=? AND path=?',
                              (snapshot.seq, path)).fetchone()
        return None if row is None else (row[0], row[1])

    def read_verified(self, snapshot: CatalogSnapshot, path: str, sha256: str) -> bytes:
        """Re-read one member's current bytes; they must rehash to ``sha256``."""
        row = self.db.execute('SELECT dir_rel,name,size,sha256 FROM scan_files '
                              'WHERE seq=? AND path=?', (snapshot.seq, path)).fetchone()
        if row is None or row[3] != sha256:
            _raise_stale('path is not in the catalog snapshot')
        opener = _DirOpener(self.db, self.roots, snapshot.seq)
        try:
            return self._read_member(opener, path, row[0], row[1], row[2], sha256)
        finally:
            opener.close()

    def _read_member(self, opener: _DirOpener, path: str, dir_rel: str, name: str,
                     size: int, sha256: str) -> bytes:
        try:
            data = _read_exact(opener.open_dir(dir_rel), name, path, size)
        except _Blocked as blocked:
            _raise_stale(f'{blocked.reason}: {blocked.detail}')
        except OSError as exc:
            _raise_stale(f'source unreadable: {path}', exc)
        if hashlib.sha256(data).hexdigest() != sha256:
            _raise_stale(f'source bytes differ from catalog snapshot: {path}')
        return data

    def verify_unchanged(self, snapshot: CatalogSnapshot,
                         max_stats: int = DEFAULT_VERIFY_STATS) -> None:
        """Stat every snapshot directory and file; any change fails closed.

        Uses the scan's no-follow identities and stat signatures, so an
        out-of-band add, delete, rename or edit since the scan is detected even
        for files a caller never reads. This is not a rehash. A snapshot above
        ``max_stats`` fails closed: serving it needs a change feed (plan doc).
        """
        max_stats = _positive_int('max_stats', max_stats)

        def sweep() -> Optional[str]:
            if not self._is_current(snapshot):
                return 'catalog snapshot changed'
            dirs = self.db.execute('SELECT rel,sig FROM scan_dirs WHERE seq=? '
                                   'ORDER BY rel LIMIT ?',
                                   (snapshot.seq, max_stats + 1)).fetchall()
            files = self.db.execute('SELECT path,dir_rel,name,sig FROM scan_files '
                                    'WHERE seq=? ORDER BY dir_rel,name LIMIT ?',
                                    (snapshot.seq, max_stats + 1)).fetchall()
            if len(dirs) + len(files) > max_stats:
                return 'freshness sweep exceeds max_verify_stats'
            opener = _DirOpener(self.db, self.roots, snapshot.seq)
            try:
                for rel, sig in dirs:
                    if _sig_text(os.fstat(opener.open_dir(rel))) != sig:
                        return f'directory changed since scan: {rel}'
                for path, dir_rel, name, sig in files:
                    st = os.stat(name, dir_fd=opener.open_dir(dir_rel),
                                 follow_symlinks=False)
                    if _sig_text(st) != sig:
                        return f'file changed since scan: {path}'
            except _Blocked as blocked:
                return f'{blocked.reason}: {blocked.detail}'
            except OSError as exc:
                return f'source unreadable: {type(exc).__name__}: {exc.strerror or exc}'
            finally:
                opener.close()
            return None
        problem = self._read(sweep)
        if problem is not None:
            _raise_stale(f'catalog not fresh: {problem}')

    def iter_documents(self, snapshot: CatalogSnapshot,
                       exclusions: Exclusions) -> Iterator[Tuple[str, str, bool]]:
        """Yield ``(path, text, eligible)`` re-read and rehashed one file at a time."""
        excluded_paths, excluded_hashes = exclusions
        self.require_same(snapshot)
        opener = _DirOpener(self.db, self.roots, snapshot.seq)
        try:
            cursor = ''
            while True:
                rows = self.db.execute('SELECT path,dir_rel,name,size,sha256 FROM scan_files '
                                       'WHERE seq=? AND path>? ORDER BY path LIMIT ?',
                                       (snapshot.seq, cursor, _PAGE)).fetchall()
                if not rows:
                    break
                for path, dir_rel, name, size, sha in rows:
                    data = self._read_member(opener, path, dir_rel, name, size, sha)
                    yield (path, data.decode('utf-8'),
                           path not in excluded_paths and sha not in excluded_hashes)
                cursor = rows[-1][0]
        finally:
            opener.close()
        self.require_same(snapshot)


def load_exclusions(index: Any, scope: str) -> Exclusions:
    """Read A-int exclusions once per operation (same accessor as the port)."""
    return _load_exclusions(index, scope)


def stage_from_catalog(index: Any, catalog: LayaSourceCatalog, gen: Any, *, base: Any,
                       claim_digest: Any, claim_epoch: Any,
                       max_verify_stats: int = DEFAULT_VERIFY_STATS) -> Any:
    snapshot = catalog.require_servable()
    exclusions = load_exclusions(index, catalog.scope)
    manifest = catalog.manifest(snapshot, exclusions)
    # LayaIndexA.stage holds every row in one transaction, so this list is the
    # documented whole-generation materialization bound; the catalog is not.
    documents = list(catalog.iter_documents(snapshot, exclusions))
    # The rehash above cannot see files added since the scan; the sweep can.
    catalog.verify_unchanged(snapshot, max_verify_stats)
    return index.stage(gen, catalog.scope, documents, base=base,
                       source_manifest=manifest, claim_digest=claim_digest,
                       claim_epoch=claim_epoch)


def publish_from_catalog(index: Any, catalog: LayaSourceCatalog, expected: Any, gen: Any,
                         claim: Any, *, claim_digest: Any, claim_epoch: Any,
                         require_vector: bool = False,
                         max_verify_stats: int = DEFAULT_VERIFY_STATS) -> Any:
    """Bind publication to the last complete catalog snapshot, still unchanged."""
    snapshot = catalog.require_servable()
    manifest = catalog.manifest(snapshot, load_exclusions(index, catalog.scope))
    catalog.verify_unchanged(snapshot, max_verify_stats)
    return index.publish(expected, gen, claim, source_manifest=manifest,
                         claim_digest=claim_digest, claim_epoch=claim_epoch,
                         require_vector=require_vector)


def _selected(roots: Mapping[str, str], names: Any) -> Tuple[str, ...]:
    if names is None:
        return tuple(sorted(roots))
    if (not isinstance(names, tuple) or not names
            or any(not isinstance(name, str) or name not in roots for name in names)
            or len(set(names)) != len(names)):
        raise ValueError('invalid collection selection')
    return names


def search_catalog_verified(index: Any, catalog: LayaSourceCatalog, token: Any,
                            fts_query: str, limit: int, *,
                            collection_names: Optional[Tuple[str, ...]] = None,
                            max_verify_bytes: int = DEFAULT_VERIFY_BYTES,
                            max_verify_stats: int = DEFAULT_VERIFY_STATS
                            ) -> List[Dict[str, Any]]:
    """Serve one pinned view only from a servable catalog snapshot.

    Checks: no pending/blocked newer scan; catalog manifest == view seal; each
    returned row's current bytes rehash to the indexed hash and are served
    from that re-read; every snapshot directory and file is stat-unchanged
    since the scan (unreturned edits/adds/deletes fail closed); seal and
    snapshot unchanged afterwards.
    """
    limit = _positive_int('limit', limit)
    max_verify_bytes = _positive_int('max_verify_bytes', max_verify_bytes)
    max_verify_stats = _positive_int('max_verify_stats', max_verify_stats)
    names = _selected(catalog.roots, collection_names)
    seal = _seal(index, token)
    scope, gen, sealed_manifest, _ = seal
    if scope != catalog.scope:
        _raise_stale('view seal scope differs from catalog')
    snapshot = catalog.require_servable()
    exclusions = load_exclusions(index, scope)
    excluded_paths, excluded_hashes = exclusions
    if catalog.manifest(snapshot, exclusions) != sealed_manifest:
        _raise_stale('catalog manifest differs from view seal')
    rows = index.search(token, fts_query, limit,
                        path_prefixes=tuple(name + '/' for name in names))
    verified = []
    spent = 0
    try:
        rows = list(rows)
        if len(rows) > limit:
            raise ValueError('too many rows')
        for row in rows:
            path = row['path']
            entry = catalog.lookup(snapshot, path)
            if (row['scope'] != scope or row['generation'] != gen or entry is None
                    or path in excluded_paths or entry[0] in excluded_hashes
                    or row['hash'] != entry[0]
                    or not any(path.startswith(name + '/') for name in names)):
                _raise_stale('search row does not match catalog snapshot')
            spent += entry[1]
            if spent > max_verify_bytes:
                _raise_stale('row verification budget exceeded')
            data = catalog.read_verified(snapshot, path, entry[0])
            verified.append({'scope': scope, 'generation': gen, 'path': path,
                             'hash': entry[0], 'body': data.decode('utf-8'),
                             'score': row['score'], 'snapshot': snapshot.scan_id})
    except IndexContractError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        _raise_stale('malformed search rows', exc)
    catalog.verify_unchanged(snapshot, max_verify_stats)
    catalog.require_same(snapshot)
    if _seal(index, token) != seal:
        _raise_stale('view seal changed during search')
    return verified

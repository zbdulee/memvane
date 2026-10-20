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
  * The catalog is servable only while the newest requested scan is the
    current complete one; a pending or blocked newer scan fails closed.
    Servability is a precondition, never a freshness claim (see below).
  * Binding-path indexing (``require_complete``/``require_current``) uses
    the current COMPLETE snapshot even while a newer scan is pending: that
    scan never touches the snapshot's rows until it publishes. A blocked
    newer scan, or a newer scan that publishes and replaces the snapshot,
    fails closed; the caller re-captures and retries.

Source validation reuses the port helpers: canonical no-follow root binding,
hidden-entry skip, symlink/device/non-regular ``.md`` rejection, safe UTF-8
names, depth/entry bounds and strict UTF-8 content. Violations block the scan;
nothing is skipped silently.

Two correctness claims are kept apart
(see docs/plans/2026-10-19-laya-source-batched-scan.md):
  * Generation correctness (immutable). A complete multi-step scan is
    scan-time evidence. It lets a full-membership generation be staged and
    published. ``capture_generation_binding`` records which complete snapshot,
    source manifest, A-int incarnation and root ``(dev, ino)`` identities that
    generation was built from.
  * Returned-candidate correctness (live). ``CandidateQuery`` /
    ``search_candidates_verified`` serve only rows whose *current* bytes,
    re-read no-follow under the bound roots, hash to the row's indexed hash.
    A candidate that fails is dropped. The whole query fails if the binding,
    seal, incarnation or a root identity does not match. Ranking reflects the
    pinned generation, not the live corpus: new or changed files are not
    discoverable until async sync (index lag), but a stale body is never
    returned. Stat signatures and TTLs are never acceptance evidence.
  * ``search_catalog_verified`` is kept as an optional full-proof audit
    oracle: one fresh full enumeration and byte hash per call, bounded by
    ``max_proof_files``/``max_proof_bytes``. Above budget it raises
    ``CatalogPending`` with zero rows before any source byte is read. It is
    not the large-corpus query gate.
  * ``verify_unchanged`` (stat sweep) is an early-rejection hint only. With
    an explicit binding, stage/publish skip it, so its ``max_stats`` is no
    corpus cap there; returned candidates are byte-checked at query time.
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
import re
import sqlite3
import stat
import time
import uuid
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import (Any, Callable, Dict, FrozenSet, Iterable, Iterator, List,
                    Optional, Tuple)

from laya_index_a import IndexContractError, _digest, _valid_relative_path
from laya_index_source_port import (MAX_DEPTH, MAX_ENTRIES, STALE, SourceStale,
                                    _COLLECTION, _DIR_FLAGS, _FILE_FLAGS,
                                    _READ_CHUNK, _check_root, _load_exclusions,
                                    _node, _open_root_fd, _positive_int,
                                    _raise_stale, _require_platform, _safe_rel,
                                    _seal, _sig, scan_collections)

APP_ID = 0x4c594343  # LYCC; distinct from the A-int database identity
SCHEMA_VERSION = 1
DEFAULT_STEP_FILES = 200
DEFAULT_STEP_BYTES = 5_000_000
DEFAULT_STEP_STATS = 2_000
# Per-query full-proof budget; the same bounds the port's per-query scan uses.
DEFAULT_PROOF_FILES = 200
DEFAULT_PROOF_BYTES = 5_000_000
DEFAULT_VERIFY_STATS = 10_000
# Returned-candidate verification: finite per-file / per-query read bounds.
# Arbitrary technical defaults, not an agreed product cap. A candidate over a
# bound is dropped (``file_budget``/``query_budget``), never deleted; if no
# candidate survives the phase raises ``CatalogPending``. Callers may pass
# larger explicit values.
DEFAULT_CANDIDATE_FILE_BYTES = 5_000_000
DEFAULT_CANDIDATE_QUERY_BYTES = 5_000_000
DEFAULT_CANDIDATE_QUERY_FILES = 200
DEFAULT_REFILL_FACTOR = 4
A_INT_MAX_LIMIT = 100  # LayaIndexA.search_many/search_scored, vector search_exact
PENDING_PHASES = ('enumerate', 'hash', 'verify_dirs', 'verify_files')
MAX_COLLECTIONS = 16
_BUSY_TIMEOUT = 0.2
_PAGE = 256
_CHANGED_ERRNOS = frozenset({errno.ENOENT, errno.ENOTDIR, errno.ELOOP})
_HEX64 = re.compile(r'[0-9a-f]{64}\Z')
_HEX32 = re.compile(r'[0-9a-f]{32}\Z')
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


class CatalogPending(IndexContractError):
    """A query outcome with zero rows and an explicit reason; never catalog state.

    It is ``IndexContractError('index_stale')``, so a caller that only knows
    index_stale still fails closed. ``detail`` carries the reason and counts.
    There are two producers:

    * ``reason='proof_budget_exceeded'`` (audit oracle
      ``search_catalog_verified``): raised from snapshot metadata before any
      source byte is read or A-int is searched. It is cleared only by a
      smaller corpus or a larger explicit budget.
    * ``reason='candidates_unverified'`` (``CandidateQuery.phase``): every
      candidate of at most two bounded rounds was dropped. The cause is a
      changed, deleted, unsafe or over-budget file, or a sync lag. Nothing is
      deleted; async sync or a larger budget clears it.
    """

    def __init__(self, detail: Dict[str, Any]):
        super().__init__(STALE)
        self.reason = detail['reason']
        self.detail = detail


class ReturnedCandidateDiverged(SourceStale):
    """A returned row no longer matches the pinned generation's source."""


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


def _raw_line(path: str, sha256: str, size: int) -> bytes:
    """One line of a snapshot's raw digest; shared by publication and proof."""
    return json.dumps([path, sha256, size], ensure_ascii=False).encode('utf-8') + b'\n'


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
        # One listing is atomic and held in memory, so the entry bound applies
        # to it. Rows are durable per step, so the scan total is only counted:
        # a corpus-wide entry cap would be a corpus-size refusal.
        state['entries'] += len(names)
        if len(names) > MAX_ENTRIES:
            raise _Blocked('source_invalid', f'too many entries in one directory: {rel}')
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
            digest.update(_raw_line(path, sha, size))
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

    def _snapshot(self, seq: int) -> CatalogSnapshot:
        row = self._scan_row(seq)
        return CatalogSnapshot(self.scope, row['seq'], row['scan_id'], row['raw_digest'],
                               row['files'], row['bytes'], row['finished_ns'])

    def require_servable(self) -> CatalogSnapshot:
        """Return the current complete snapshot or fail closed with index_stale."""
        def read():
            current_seq = self._pointer('current')
            active = self._scan_row(self._pointer('active'))
            servable, reason = self._servable(current_seq, active)
            if not servable:
                return reason
            return self._snapshot(current_seq)
        snapshot = self._read(read)
        if isinstance(snapshot, str):
            _raise_stale(f'catalog not servable: {snapshot}')
        return snapshot

    def require_same(self, snapshot: CatalogSnapshot) -> None:
        if self.require_servable() != snapshot:
            _raise_stale('catalog snapshot changed')

    def require_complete(self) -> CatalogSnapshot:
        """The current COMPLETE snapshot for binding-path indexing.

        Unlike ``require_servable``, a newer *pending* scan does not block it:
        that scan writes only its own rows and drops this snapshot only in
        the transaction that publishes its replacement. A blocked newer scan
        or no complete snapshot fails closed. This is scan-time indexing
        evidence, not live corpus freshness.
        """
        def read():
            current_seq = self._pointer('current')
            if current_seq is None:
                return 'no_complete_snapshot'
            active = self._scan_row(self._pointer('active'))
            if (active is not None and active['seq'] != current_seq
                    and active['phase'] not in PENDING_PHASES):
                return 'scan_blocked'
            return self._snapshot(current_seq)
        snapshot = self._read(read)
        if isinstance(snapshot, str):
            _raise_stale(f'catalog has no usable complete snapshot: {snapshot}')
        return snapshot

    def require_current(self, snapshot: CatalogSnapshot) -> None:
        """``snapshot`` is still the current pointer; a pending scan is allowed."""
        if self.require_complete() != snapshot:
            _raise_stale('catalog snapshot changed')

    def _is_current(self, snapshot: CatalogSnapshot, allow_pending: bool = False) -> bool:
        if not (isinstance(snapshot, CatalogSnapshot) and snapshot.scope == self.scope
                and self._pointer('current') == snapshot.seq):
            return False
        active = self._pointer('active')
        if active == snapshot.seq:
            return True
        if not allow_pending:
            return False
        row = self._scan_row(active)
        return row is not None and row['phase'] in PENDING_PHASES

    def manifest(self, snapshot: CatalogSnapshot, exclusions: Exclusions, *,
                 allow_pending: bool = False) -> str:
        """A-int source manifest for the snapshot; equals the port's for the same bytes."""
        excluded_paths, excluded_hashes = exclusions

        def read():
            if not self._is_current(snapshot, allow_pending):
                return None
            return [(path, sha, int(path not in excluded_paths
                                    and sha not in excluded_hashes))
                    for path, sha in self.db.execute(
                        'SELECT path,sha256 FROM scan_files WHERE seq=?', (snapshot.seq,))]
        entries = self._read(read)
        if entries is None:
            _raise_stale('catalog snapshot changed')
        return _digest(self.scope, entries)

    def root_nodes(self, snapshot: CatalogSnapshot, *,
                   allow_pending: bool = False) -> Dict[str, Tuple[int, int]]:
        """Root ``(dev, ino)`` identities the complete ``snapshot`` listed."""
        def read():
            if not self._is_current(snapshot, allow_pending):
                return None
            return dict(self.db.execute(
                "SELECT collection,node FROM scan_dirs WHERE seq=? AND depth=0 "
                "AND rel=collection||'/'", (snapshot.seq,)).fetchall())
        nodes = self._read(read)
        if nodes is None:
            _raise_stale('catalog snapshot changed')
        if sorted(nodes) != sorted(self.roots) or any(
                not isinstance(node, str) for node in nodes.values()):
            _raise_stale('catalog snapshot lacks root identities')
        out: Dict[str, Tuple[int, int]] = {}
        for name, node in nodes.items():
            dev, ino = node.split(':')
            out[name] = (int(dev), int(ino))
        return out

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
        """Early-rejection hint: stat every snapshot directory and file.

        A signature mismatch fails closed cheaply, before any byte is read.
        A *match* proves nothing about content: size, mtime and the signature
        computation can all appear unchanged while bytes or membership
        changed. Never use a passing sweep as freshness proof; only the full
        byte proof in ``search_catalog_verified`` is. Above ``max_stats`` the
        hint itself fails closed.
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

    def iter_documents(self, snapshot: CatalogSnapshot, exclusions: Exclusions, *,
                       allow_pending: bool = False) -> Iterator[Tuple[str, str, bool]]:
        """Yield ``(path, text, eligible)`` re-read and rehashed one file at a time.

        ``snapshot`` must be current before the first and after the last row.
        If a newer scan publishes mid-iteration, the dropped rows end the
        pages early and the final check fails, so a partial list is never
        complete output. ``allow_pending`` is the binding-path rule.
        """
        excluded_paths, excluded_hashes = exclusions
        check = self.require_current if allow_pending else self.require_same
        check(snapshot)
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
        check(snapshot)

    def snapshot_started_ns(self, snapshot: CatalogSnapshot) -> int:
        """When the scan that produced the current ``snapshot`` was requested.

        A scan lists and hashes only after it starts, so a snapshot whose scan
        started at or after a time T reflects no source state older than T
        (up to the scan's own mutation checks). This is the freshness fact an
        activation gate needs; ``finished_ns`` alone is not, because a scan
        that started before T may finish after it. A pending newer scan is
        allowed, as in ``require_complete``.
        """
        def read():
            if not self._is_current(snapshot, True):
                return None
            row = self.db.execute('SELECT started_ns FROM scans WHERE seq=?',
                                  (snapshot.seq,)).fetchone()
            return None if row is None else row[0]
        started = self._read(read)
        if type(started) is not int:
            _raise_stale('catalog snapshot changed')
        return started

    def members(self, snapshot: CatalogSnapshot, exclusions: Exclusions, *,
                after: str = '', limit: int = _PAGE,
                allow_pending: bool = False) -> List[Tuple[str, str, bool]]:
        """One page of ``(path, sha256, eligible)`` after ``after``; no body read.

        Same rows and eligibility rule as ``manifest`` and ``iter_documents``,
        in path order, so a caller can walk the snapshot in bounded pages.
        """
        if type(limit) is not int or limit < 1:
            raise ValueError('invalid member page limit')
        if not isinstance(after, str):
            raise ValueError('invalid member cursor')
        excluded_paths, excluded_hashes = exclusions

        def read():
            if not self._is_current(snapshot, allow_pending):
                return None
            return [(path, sha, path not in excluded_paths and sha not in excluded_hashes)
                    for path, sha in self.db.execute(
                        'SELECT path,sha256 FROM scan_files WHERE seq=? AND path>? '
                        'ORDER BY path LIMIT ?', (snapshot.seq, after, limit))]
        rows = self._read(read)
        if rows is None:
            _raise_stale('catalog snapshot changed')
        return rows

    def vector_fill(self, snapshot: CatalogSnapshot, profile_id: str) -> Dict[str, Any]:
        """Durable vector fill progress for ``(snapshot, profile_id)``.

        ``cursor`` is the last member path whose chunk set is known complete;
        ``done`` means every eligible member is; ``rows`` counts the chunk rows
        of the members up to ``cursor``, as a generation seal would. Another snapshot or profile
        starts from the beginning. The table is created on first write, so an
        older catalog file without it reads as no progress.
        """
        if not isinstance(snapshot, CatalogSnapshot) or not _HEX64.match(profile_id or ''):
            raise ValueError('invalid vector fill key')

        def read():
            columns = [row[1] for row in
                       self.db.execute('PRAGMA table_info(vector_fill)')]
            if 'rows' not in columns:
                # Absent, or an unreleased pre-rows layout: no progress.
                return None
            return self.db.execute('SELECT cursor,done,rows FROM vector_fill '
                                   'WHERE seq=? AND profile_id=?',
                                   (snapshot.seq, profile_id)).fetchone()
        row = self._read(read)
        if row is None:
            return {'cursor': '', 'done': False, 'rows': 0}
        return {'cursor': row[0], 'done': bool(row[1]), 'rows': row[2]}

    def set_vector_fill(self, snapshot: CatalogSnapshot, profile_id: str,
                        cursor: str, done: bool, rows: int = 0) -> bool:
        """Record fill progress; False when the catalog is busy or moved on.

        Rows of other snapshots are dropped in the same write, so the table
        holds at most one row per profile.
        """
        if (not isinstance(snapshot, CatalogSnapshot) or not _HEX64.match(profile_id or '')
                or not isinstance(cursor, str) or type(done) is not bool
                or type(rows) is not int or rows < 0):
            raise ValueError('invalid vector fill record')
        if not self._begin_write():
            return False
        try:
            if self._pointer('current') != snapshot.seq:
                self.db.rollback()
                return False
            columns = [row[1] for row in self.db.execute('PRAGMA table_info(vector_fill)')]
            if columns and 'rows' not in columns:
                # Progress only (rebuilt by the next fill), never source data.
                self.db.execute('DROP TABLE vector_fill')
            self.db.execute('CREATE TABLE IF NOT EXISTS vector_fill('
                            'seq INTEGER NOT NULL, profile_id TEXT NOT NULL, '
                            'cursor TEXT NOT NULL, done INTEGER NOT NULL, '
                            'rows INTEGER NOT NULL, PRIMARY KEY(seq, profile_id))')
            self.db.execute('DELETE FROM vector_fill WHERE seq<>?', (snapshot.seq,))
            self.db.execute('INSERT OR REPLACE INTO vector_fill VALUES(?,?,?,?,?)',
                            (snapshot.seq, profile_id, cursor, int(done), rows))
            self.db.commit()
            return True
        except BaseException:
            self._rollback()
            raise


def load_exclusions(index: Any, scope: str) -> Exclusions:
    """Read A-int exclusions once per operation (same accessor as the port)."""
    return _load_exclusions(index, scope)


def stage_from_catalog(index: Any, catalog: LayaSourceCatalog, gen: Any, *, base: Any,
                       claim_digest: Any, claim_epoch: Any,
                       max_verify_stats: int = DEFAULT_VERIFY_STATS,
                       binding: Optional['GenerationSourceBinding'] = None) -> Any:
    """Stage one full-membership generation sealed to the catalog snapshot.

    Every member is re-read and rehashed against the snapshot; any mismatch
    stages nothing. The seal is scan-time evidence: generation correctness,
    not live freshness. Live serving goes through ``CandidateQuery`` with a
    ``GenerationSourceBinding``.

    ``binding=None`` keeps the strict behaviour: a pending newer scan fails
    closed and the stat hint (capped at ``max_verify_stats``) must pass.
    With a ``binding`` it must describe exactly this snapshot, manifest,
    generation, roots and A-int incarnation. A newer *pending* scan does not
    block it, and the stat hint is skipped, so ``max_verify_stats`` is not a
    corpus cap: returned candidates are byte-checked at query time instead.
    """
    bound = binding is not None
    snapshot = catalog.require_complete() if bound else catalog.require_servable()
    exclusions = load_exclusions(index, catalog.scope)
    manifest = catalog.manifest(snapshot, exclusions, allow_pending=bound)
    _require_binding_for(index, catalog, snapshot, binding, gen, manifest)
    # LayaIndexA.stage holds every row in one transaction, so this list is the
    # documented whole-generation materialization bound; the catalog is not.
    documents = list(catalog.iter_documents(snapshot, exclusions, allow_pending=bound))
    if not bound:
        # The rehash above cannot see added files; the stat hint may reject
        # some membership changes but proves nothing (scan-time evidence only).
        catalog.verify_unchanged(snapshot, max_verify_stats)
    return index.stage(gen, catalog.scope, documents, base=base,
                       source_manifest=manifest, claim_digest=claim_digest,
                       claim_epoch=claim_epoch)


def publish_from_catalog(index: Any, catalog: LayaSourceCatalog, expected: Any, gen: Any,
                         claim: Any, *, claim_digest: Any, claim_epoch: Any,
                         require_vector: bool = False,
                         max_verify_stats: int = DEFAULT_VERIFY_STATS,
                         binding: Optional['GenerationSourceBinding'] = None) -> Any:
    """Bind publication to the last complete catalog snapshot.

    Publication is generation (scan-time) evidence only. A published
    generation may be served only with a matching ``GenerationSourceBinding``
    that the parent persisted durably *before* this call (see the design
    doc). ``binding=None`` keeps the strict rule (no pending newer scan, stat
    hint within ``max_verify_stats``; it can reject, never prove). With a
    binding, the same snapshot rule as ``stage_from_catalog`` applies and the
    stat hint is skipped.
    """
    bound = binding is not None
    snapshot = catalog.require_complete() if bound else catalog.require_servable()
    manifest = catalog.manifest(snapshot, load_exclusions(index, catalog.scope),
                                allow_pending=bound)
    _require_binding_for(index, catalog, snapshot, binding, gen, manifest)
    if not bound:
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


def _proof_backlog(snapshot: CatalogSnapshot, max_files: int,
                   max_bytes: int) -> Optional[Dict[str, Any]]:
    """Explicit backlog when one full proof of ``snapshot`` exceeds the budget."""
    if snapshot.files <= max_files and snapshot.bytes <= max_bytes:
        return None
    return {'reason': 'proof_budget_exceeded', 'scan_id': snapshot.scan_id,
            'snapshot_files': snapshot.files, 'snapshot_bytes': snapshot.bytes,
            'max_proof_files': max_files, 'max_proof_bytes': max_bytes,
            'files_over': max(0, snapshot.files - max_files),
            'bytes_over': max(0, snapshot.bytes - max_bytes),
            'requires': 'trusted_change_feed_or_immutable_source_snapshot'}


def search_catalog_verified(index: Any, catalog: LayaSourceCatalog, token: Any,
                            fts_query: str, limit: int, *,
                            collection_names: Optional[Tuple[str, ...]] = None,
                            max_proof_files: int = DEFAULT_PROOF_FILES,
                            max_proof_bytes: int = DEFAULT_PROOF_BYTES,
                            max_verify_stats: int = DEFAULT_VERIFY_STATS
                            ) -> List[Dict[str, Any]]:
    """Audit/test oracle: serve one pinned view only after a full byte proof.

    This is not the production large-corpus query path; that path is
    ``CandidateQuery``/``search_candidates_verified``. Kept for backward
    compatible tests and optional audits. Its strict ``CatalogPending`` is
    never converted into normal rows.

    Sequence: view-seal scope == catalog scope; no pending/blocked newer scan
    (``index_stale``); snapshot within ``max_proof_files``/``max_proof_bytes``
    (else ``CatalogPending``: zero rows, no source read, no A-int search);
    catalog manifest == seal; stat sweep as an early-rejection hint only; one
    fresh full enumeration and hash of every root through the port, which
    also fails closed if the actual source exceeds the budget; its raw digest
    == the snapshot's and its manifest == the seal; every row matched to and
    served from those scanned buffers; snapshot and seal unchanged afterwards.
    A TOCTOU window after the final check remains, as with the port.
    """
    limit = _positive_int('limit', limit)
    max_proof_files = _positive_int('max_proof_files', max_proof_files)
    max_proof_bytes = _positive_int('max_proof_bytes', max_proof_bytes)
    max_verify_stats = _positive_int('max_verify_stats', max_verify_stats)
    names = _selected(catalog.roots, collection_names)
    seal = _seal(index, token)
    scope, gen, sealed_manifest, _ = seal
    if scope != catalog.scope:
        _raise_stale('view seal scope differs from catalog')
    snapshot = catalog.require_servable()
    backlog = _proof_backlog(snapshot, max_proof_files, max_proof_bytes)
    if backlog is not None:
        raise CatalogPending(backlog)
    exclusions = load_exclusions(index, scope)
    excluded_paths, excluded_hashes = exclusions
    if catalog.manifest(snapshot, exclusions) != sealed_manifest:
        _raise_stale('catalog manifest differs from view seal')
    catalog.verify_unchanged(snapshot, max_verify_stats)  # hint only, not proof
    # The proof: every root re-enumerated and every byte rehashed now.
    source = scan_collections(catalog.roots, scope, index,
                              max_proof_files, max_proof_bytes)
    raw = hashlib.sha256()
    for f in source.files:
        raw.update(_raw_line(f.path, f.sha256, len(f.data)))
    if raw.hexdigest() != snapshot.raw_digest:
        _raise_stale('source bytes or membership differ from catalog snapshot')
    if source.manifest != sealed_manifest:
        _raise_stale('source manifest differs from view seal')
    rows = index.search(token, fts_query, limit,
                        path_prefixes=tuple(name + '/' for name in names))
    verified = []
    try:
        rows = list(rows)
        if len(rows) > limit:
            raise ValueError('too many rows')
        for row in rows:
            path = row['path']
            f = source.by_path.get(path)
            if (row['scope'] != scope or row['generation'] != gen or f is None
                    or not f.eligible or path in excluded_paths
                    or f.sha256 in excluded_hashes or row['hash'] != f.sha256
                    or not any(path.startswith(name + '/') for name in names)):
                _raise_stale('search row does not match proven source')
            # Served from the same buffer the proof hashed.
            verified.append({'scope': scope, 'generation': gen, 'path': path,
                             'hash': f.sha256, 'body': f.text,
                             'score': row['score'], 'snapshot': snapshot.scan_id})
    except IndexContractError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        _raise_stale('malformed search rows', exc)
    catalog.require_same(snapshot)
    if _seal(index, token) != seal:
        _raise_stale('view seal changed during search')
    return verified


# -- generation source binding ------------------------------------------------

RootIdentity = Tuple[str, str, int, int]  # (collection, canonical root, dev, ino)


def _syntactic_root(root: Any) -> bool:
    return (isinstance(root, str) and root.startswith('/') and '\x00' not in root
            and os.path.normpath(root) == root and not root.startswith('//'))


@dataclass(frozen=True)
class GenerationSourceBinding:
    """Immutable source identity of one published A-int generation.

    Captured from a COMPLETE catalog snapshot (``capture_generation_binding``)
    before stage, and supplied explicitly by the caller to every query. The
    parent must persist it atomically/recoverably with the generation (route,
    journal or A-int change); this module chooses no storage location. A
    published generation without a matching binding must not be served.
    Construction validates syntax only. Filesystem identity is checked by
    each query.
    """
    scope: str
    generation: str
    source_manifest: str
    catalog_scan_id: str
    catalog_raw_digest: str
    db_incarnation: str
    roots: Tuple[RootIdentity, ...]

    def __post_init__(self):
        roots = self.roots
        if (not isinstance(self.scope, str) or not self.scope
                or not isinstance(self.generation, str) or not self.generation
                or any(not isinstance(value, str) or not _HEX64.fullmatch(value)
                       for value in (self.source_manifest, self.catalog_raw_digest))
                or any(not isinstance(value, str) or not _HEX32.fullmatch(value)
                       for value in (self.catalog_scan_id, self.db_incarnation))
                or not isinstance(roots, tuple)
                or not 1 <= len(roots) <= MAX_COLLECTIONS):
            raise ValueError('invalid generation source binding')
        for item in roots:
            if (not isinstance(item, tuple) or len(item) != 4
                    or not isinstance(item[0], str) or not _COLLECTION.fullmatch(item[0])
                    or not _syntactic_root(item[1])
                    or any(type(value) is not int or value < 0 for value in item[2:])):
                raise ValueError('invalid generation source binding')
        names = [item[0] for item in roots]
        paths = [item[1] for item in roots]
        if (names != sorted(names) or len(set(names)) != len(names)
                or len(set(paths)) != len(paths) or any(
                    left.startswith(right + '/') or right.startswith(left + '/')
                    for i, left in enumerate(paths) for right in paths[i + 1:])):
            raise ValueError('invalid generation source binding')

    @property
    def collections(self) -> Dict[str, str]:
        return {name: root for name, root, _dev, _ino in self.roots}

    def as_record(self) -> Dict[str, Any]:
        """JSON-ready record for the parent's durable binding owner."""
        return {'scope': self.scope, 'generation': self.generation,
                'source_manifest': self.source_manifest,
                'catalog_scan_id': self.catalog_scan_id,
                'catalog_raw_digest': self.catalog_raw_digest,
                'db_incarnation': self.db_incarnation,
                'roots': [list(item) for item in self.roots]}

    @classmethod
    def from_record(cls, record: Any) -> 'GenerationSourceBinding':
        keys = ('scope', 'generation', 'source_manifest', 'catalog_scan_id',
                'catalog_raw_digest', 'db_incarnation', 'roots')
        if (not isinstance(record, Mapping)
                or any(not isinstance(key, str) for key in record)
                or sorted(record) != sorted(keys)
                or not isinstance(record['roots'], (list, tuple))):
            raise ValueError('invalid generation source binding')
        roots = tuple(tuple(item) if isinstance(item, (list, tuple)) else item
                      for item in record['roots'])
        return cls(record['scope'], record['generation'], record['source_manifest'],
                   record['catalog_scan_id'], record['catalog_raw_digest'],
                   record['db_incarnation'], roots)


def _open_bound_root(collection: str, root: str, node: Tuple[int, int]) -> int:
    """Open one root no-follow; its live identity must equal the bound one."""
    try:
        _check_root(root)
        fd = _open_root_fd(root)
    except OSError as exc:
        _raise_stale(f'collection root unavailable: {collection}', exc)
    if _node(os.fstat(fd)) != node:
        os.close(fd)
        _raise_stale(f'collection root replaced: {collection}')
    return fd


def _db_incarnation(index: Any) -> str:
    try:
        value = index.incarnation()
    except IndexContractError:
        raise
    except Exception as exc:
        _raise_stale('database incarnation unavailable', exc)
    if not isinstance(value, str) or not _HEX32.fullmatch(value):
        _raise_stale('database incarnation unavailable')
    return value


def capture_generation_binding(index: Any, catalog: LayaSourceCatalog,
                               snapshot: CatalogSnapshot,
                               generation: str) -> GenerationSourceBinding:
    """Synthetic-ready binding factory for one COMPLETE, current snapshot.

    Same snapshot rule as bound stage/publish: a newer pending scan is
    allowed, a blocked or published newer scan fails closed. Root identities
    come from the snapshot's own listing and must still match the live
    roots. The manifest uses exclusions read now, as stage does. The caller
    passes the result explicitly to stage/publish and to every query.
    Nothing reads the latest snapshot implicitly at query time.
    """
    if not isinstance(generation, str) or not generation:
        raise IndexContractError('invalid_identity')
    catalog.require_current(snapshot)
    manifest = catalog.manifest(snapshot, load_exclusions(index, catalog.scope),
                                allow_pending=True)
    nodes = catalog.root_nodes(snapshot, allow_pending=True)
    roots: List[RootIdentity] = []
    for name, root in catalog.roots.items():
        os.close(_open_bound_root(name, root, nodes[name]))
        roots.append((name, root, nodes[name][0], nodes[name][1]))
    binding = GenerationSourceBinding(catalog.scope, generation, manifest,
                                      snapshot.scan_id, snapshot.raw_digest,
                                      _db_incarnation(index), tuple(roots))
    catalog.require_current(snapshot)
    return binding


def _require_binding_for(index: Any, catalog: LayaSourceCatalog,
                         snapshot: CatalogSnapshot, binding: Any, gen: Any,
                         manifest: str) -> None:
    """Stage/publish guard: an explicit binding must describe exactly this state."""
    if binding is None:
        return
    nodes = catalog.root_nodes(snapshot, allow_pending=True)
    expected = tuple((name, root, *nodes[name]) for name, root in catalog.roots.items())
    if (not isinstance(binding, GenerationSourceBinding)
            or binding.scope != catalog.scope or binding.generation != gen
            or binding.source_manifest != manifest
            or binding.catalog_scan_id != snapshot.scan_id
            or binding.catalog_raw_digest != snapshot.raw_digest
            or binding.roots != expected
            or binding.db_incarnation != _db_incarnation(index)):
        _raise_stale('generation source binding does not match the catalog snapshot')


# -- returned-candidate verification -------------------------------------------

def _lstat_at(dfd: int, name: str) -> os.stat_result:
    return os.stat(name, dir_fd=dfd, follow_symlinks=False)


def _node_reason(st: os.stat_result, root_dev: int, want_dir: bool) -> Optional[str]:
    if stat.S_ISLNK(st.st_mode):
        return 'symlink'
    if want_dir and not stat.S_ISDIR(st.st_mode):
        return 'missing'
    if not want_dir and not stat.S_ISREG(st.st_mode):
        return 'not_regular'
    if st.st_dev != root_dev:
        return 'crosses_device'
    return None


def _os_reason(exc: OSError) -> str:
    if exc.errno in (errno.ENOENT, errno.ENOTDIR):
        return 'missing'
    if exc.errno == errno.ELOOP:
        return 'symlink'
    return 'unreadable'


Verdict = Tuple[str, ...]  # ('ok', sha256, text) or ('drop', reason)


class CandidateQuery:
    """Live verification of returned candidates for one pinned, bound generation.

    Opening checks the whole query: binding present and well formed, view
    seal scope/generation/manifest equal to the binding, A-int incarnation
    equal, and every bound root opened no-follow with its recorded
    ``(dev, ino)``. Exclusions are loaded once. Each phase (lexical, vector,
    wiki, raw) verifies only the rows it is given. It never lists a
    directory: the namespaced path is walked component by component under
    the held root fd, and only the candidate's bytes are read.

    Rows returned by ``phase``/``search`` are deliverable only after
    ``finish()`` returns. ``finish`` re-checks seal, incarnation and live root
    identities. If it raises, every row of the query must be discarded.
    """

    def __init__(self, index: Any, token: Any, binding: Any, *,
                 max_file_bytes: int = DEFAULT_CANDIDATE_FILE_BYTES,
                 max_query_bytes: int = DEFAULT_CANDIDATE_QUERY_BYTES,
                 max_query_files: int = DEFAULT_CANDIDATE_QUERY_FILES):
        self._max_file_bytes = _positive_int('max_file_bytes', max_file_bytes)
        self._max_query_bytes = _positive_int('max_query_bytes', max_query_bytes)
        self._max_query_files = _positive_int('max_query_files', max_query_files)
        self._index = index
        self._token = token
        self._root_fds: Dict[str, int] = {}
        self._root_devs: Dict[str, int] = {}
        self._memo: Dict[str, Verdict] = {}
        self._open = False
        self._failed = False
        self.stats: Dict[str, Any] = {'candidates': 0, 'memo_hits': 0,
                                      'files_opened': 0, 'bytes_read': 0,
                                      'rounds': 0, 'dropped': {}}
        if not isinstance(binding, GenerationSourceBinding):
            _raise_stale('generation source binding missing')
        self.binding = binding
        # Candidates are walked, never listed: only dir_fd stat/open is needed.
        if os.stat not in os.supports_dir_fd or os.open not in os.supports_dir_fd:
            _raise_stale('platform lacks dir_fd support')
        self._open = True
        try:
            self._seal = _seal(index, token)
            scope, gen, manifest, _epoch = self._seal
            if (scope != binding.scope or gen != binding.generation
                    or manifest != binding.source_manifest):
                _raise_stale('view seal does not match generation source binding')
            if _db_incarnation(index) != binding.db_incarnation:
                _raise_stale('database incarnation differs from binding')
            self._exclusions = load_exclusions(index, scope)
            for name, root, dev, ino in binding.roots:
                self._root_fds[name] = _open_bound_root(name, root, (dev, ino))
                self._root_devs[name] = dev
        except BaseException:
            self.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self) -> None:
        self._open = False
        while self._root_fds:
            os.close(self._root_fds.popitem()[1])

    def _require_open(self) -> None:
        if not self._open:
            raise IndexContractError('candidate_query_closed')

    # -- phases ---------------------------------------------------------------

    def verify_rows(self, rows: Iterable[Any], *,
                    collection_names: Optional[Tuple[str, ...]] = None
                    ) -> List[Dict[str, Any]]:
        """Verify rows the caller already fetched from the pinned token.

        For a caller that batches several queries into one A-int call
        (``search_many``), so the generation's FTS check runs once rather than
        once per ``phase``. Same per-row rules as ``phase``: a malformed,
        foreign-generation or out-of-selection row poisons the query; a stale
        candidate is dropped and counted in ``stats['dropped']``. There is no
        refill round and no ``CatalogPending``: an all-dropped list is ``[]``.
        """
        self._require_open()
        names = _selected(self.binding.collections, collection_names)
        served: List[Dict[str, Any]] = []
        try:
            for row in rows:
                verified, _path, _reason = self._verify_row(row, names)
                if verified is not None:
                    served.append(verified)
        except BaseException:
            self._failed = True
            raise
        return served

    def search(self, fts_query: str, limit: int, *,
               collection_names: Optional[Tuple[str, ...]] = None,
               refill_factor: int = DEFAULT_REFILL_FACTOR) -> List[Dict[str, Any]]:
        """Lexical phase over the pinned token, collection scope applied in SQL."""
        names = _selected(self.binding.collections, collection_names)
        prefixes = tuple(name + '/' for name in names)
        return self.phase(lambda size: self._index.search(self._token, fts_query, size,
                                                          path_prefixes=prefixes),
                          limit, collection_names=names, refill_factor=refill_factor)

    def phase(self, fetch: Callable[[int], Iterable[Any]], limit: int, *,
              collection_names: Optional[Tuple[str, ...]] = None,
              refill_factor: int = DEFAULT_REFILL_FACTOR) -> List[Dict[str, Any]]:
        """Verify one phase's candidates with at most two bounded fetch rounds.

        ``fetch(size)`` must query the same pinned token with ``size <= 100``
        (lexical ``search``, ``search_scored`` or vector ``search_exact``) and
        return only paths under ``collection_names``; a row outside them
        fails and poisons the whole query, like a foreign-generation row.
        Round 1 fetches ``limit``. If it was full, dropped some candidates and
        left fewer than ``limit`` valid rows, round 2 fetches
        ``min(100, limit * refill_factor)``. If no candidate survives, the
        call raises ``CatalogPending``; if A-int returned no candidates, it
        returns ``[]``. That empty answer is normal index lag: files not yet
        synced are undiscoverable. Unverified rows are never returned.
        """
        self._require_open()
        limit = _positive_int('limit', limit)
        refill_factor = _positive_int('refill_factor', refill_factor)
        if limit > A_INT_MAX_LIMIT:
            raise ValueError('limit exceeds the A-int query limit')
        names = _selected(self.binding.collections, collection_names)
        sizes = [limit]
        if min(A_INT_MAX_LIMIT, limit * refill_factor) > limit:
            sizes.append(min(A_INT_MAX_LIMIT, limit * refill_factor))
        dropped: Dict[str, str] = {}
        valid: List[Dict[str, Any]] = []
        rounds = 0
        try:
            for size in sizes:
                rounds += 1
                rows = self._fetch(fetch, size)
                valid = []
                for row in rows:
                    served, path, reason = self._verify_row(row, names)
                    if served is None:
                        dropped[path] = reason
                    else:
                        dropped.pop(path, None)
                        valid.append(served)
                if len(valid) >= limit or len(rows) < size or len(valid) == len(rows):
                    break
        except BaseException:
            # A malformed/foreign row or a failed fetch fails the whole query.
            self._failed = True
            raise
        self.stats['rounds'] += rounds
        if not valid and dropped:
            raise CatalogPending({
                'reason': 'candidates_unverified', 'scope': self.binding.scope,
                'generation': self.binding.generation,
                'snapshot': self.binding.catalog_scan_id, 'limit': limit,
                'rounds': rounds, 'fetched': sizes[:rounds],
                'candidates': len(dropped),
                'dropped': dict(sorted(Counter(dropped.values()).items()))})
        return valid[:limit]

    def require_returned_verified(self) -> None:
        """Refuse a partial answer after checking returned candidates only.

        An unreturned new file is index lag, not evidence that a returned row
        is stale. Exclusions may remove rows; every other drop withholds the
        complete answer. Source mismatches request sync at the host boundary;
        read and size limits report unavailable source without a futile scan.
        """
        self._require_open()
        if self._failed:
            _raise_stale('candidate query already failed')
        dropped = {reason: count for reason, count in self.stats['dropped'].items()
                   if reason != 'excluded' and count}
        if not dropped:
            return
        self._failed = True
        unavailable = frozenset(('file_budget', 'query_budget', 'unreadable'))
        if set(dropped) <= unavailable:
            raise CatalogPending({'reason': 'returned_unverified',
                                  'dropped': dropped,
                                  'scope': self.binding.scope,
                                  'generation': self.binding.generation})
        raise IndexContractError(STALE) from ReturnedCandidateDiverged(
            'returned source differs from the pinned generation')

    def finish(self) -> Dict[str, Any]:
        """Re-check seal, incarnation and live root identities; then close."""
        self._require_open()
        try:
            if self._failed:
                _raise_stale('candidate query already failed')
            if _seal(self._index, self._token) != self._seal:
                _raise_stale('view seal changed during query')
            if _db_incarnation(self._index) != self.binding.db_incarnation:
                _raise_stale('database incarnation changed during query')
            for name, root, dev, ino in self.binding.roots:
                os.close(_open_bound_root(name, root, (dev, ino)))
        finally:
            self.close()
        return {**self.stats, 'dropped': dict(self.stats['dropped'])}

    # -- one candidate --------------------------------------------------------

    def _fetch(self, fetch: Callable[[int], Iterable[Any]], size: int) -> List[Any]:
        rows = fetch(size)
        try:
            rows = list(rows)
        except TypeError as exc:
            _raise_stale('malformed candidate rows', exc)
        if len(rows) > size:
            _raise_stale('too many candidate rows')
        return rows

    def _verify_row(self, row: Any, names: Tuple[str, ...]
                    ) -> Tuple[Optional[Dict[str, Any]], str, str]:
        try:
            path, row_hash = row['path'], row['hash']
            row_scope, row_gen, score = row['scope'], row['generation'], row['score']
        except (KeyError, TypeError, IndexError) as exc:
            _raise_stale('malformed candidate row', exc)
        if row_scope != self.binding.scope or row_gen != self.binding.generation:
            _raise_stale('candidate row outside the pinned generation')
        if (not isinstance(path, str) or not isinstance(row_hash, str)
                or not _HEX64.fullmatch(row_hash)):
            _raise_stale('malformed candidate row')
        self.stats['candidates'] += 1
        reason = self._path_reason(path, names)
        if reason == 'out_of_selection':
            # The fetch ignored its own collection scope: a caller contract
            # violation, not a stale candidate.
            _raise_stale('candidate row outside the selected collections')
        excluded_paths, excluded_hashes = self._exclusions
        if reason is None and (path in excluded_paths or row_hash in excluded_hashes):
            reason = 'excluded'
        verdict: Verdict = ('drop', reason or '')
        if reason is None:
            cached = self._memo.get(path)
            if cached is None:
                cached = self._read_candidate(path)
                self._memo[path] = cached
            else:
                self.stats['memo_hits'] += 1
            verdict = cached
            if verdict[0] == 'drop':
                reason = verdict[1]
            elif verdict[1] != row_hash:
                reason = 'hash_mismatch'
        if reason is not None:
            counts = self.stats['dropped']
            counts[reason] = counts.get(reason, 0) + 1
            return None, path, reason
        return ({'scope': self.binding.scope, 'generation': self.binding.generation,
                 'path': path, 'hash': row_hash, 'body': verdict[2], 'score': score,
                 'snapshot': self.binding.catalog_scan_id}, path, '')

    @staticmethod
    def _path_reason(path: str, names: Tuple[str, ...]) -> Optional[str]:
        """Port/A-int path vocabulary; never touches the filesystem."""
        if not _valid_relative_path(path) or not path.endswith('.md'):
            return 'unsafe_path'
        parts = path.split('/')
        if len(parts) < 2 or len(parts) - 2 > MAX_DEPTH:
            return 'unsafe_path'
        try:
            prefix = ''
            for part in parts:
                prefix = _safe_rel(prefix, part) + '/'
        except IndexContractError:
            return 'unsafe_path'
        if parts[0] not in names:
            return 'out_of_selection'
        return None

    def _read_candidate(self, path: str) -> Verdict:
        if self.stats['files_opened'] >= self._max_query_files:
            return ('drop', 'query_budget')
        parts = path.split('/')
        root_dev = self._root_devs[parts[0]]
        fd = os.dup(self._root_fds[parts[0]])
        try:
            for part in parts[1:-1]:
                st = _lstat_at(fd, part)
                reason = _node_reason(st, root_dev, want_dir=True)
                if reason is not None:
                    return ('drop', reason)
                child = os.open(part, _DIR_FLAGS, dir_fd=fd)
                os.close(fd)
                fd = child
                if _node(os.fstat(fd)) != _node(st):
                    return ('drop', 'changed_during_read')
            return self._read_file(fd, parts[-1], root_dev)
        except OSError as exc:
            return ('drop', _os_reason(exc))
        finally:
            os.close(fd)

    def _read_file(self, dfd: int, name: str, root_dev: int) -> Verdict:
        st = _lstat_at(dfd, name)
        reason = _node_reason(st, root_dev, want_dir=False)
        if reason is not None:
            return ('drop', reason)
        size = st.st_size
        if size > self._max_file_bytes:
            return ('drop', 'file_budget')
        if size > self._max_query_bytes - self.stats['bytes_read']:
            return ('drop', 'query_budget')
        self.stats['files_opened'] += 1
        fd = os.open(name, _FILE_FLAGS, dir_fd=dfd)
        try:
            before = os.fstat(fd)
            if (not stat.S_ISREG(before.st_mode) or _node(before) != _node(st)
                    or before.st_size != size):
                return ('drop', 'changed_during_read')
            chunks: List[bytes] = []
            total = 0
            while True:
                # One byte past ``size`` is read to detect growth by content.
                chunk = os.read(fd, min(_READ_CHUNK, size + 1 - total))
                if not chunk:
                    break
                total += len(chunk)
                self.stats['bytes_read'] += len(chunk)
                if total > size:
                    return ('drop', 'changed_during_read')
                chunks.append(chunk)
            after = os.fstat(fd)
        finally:
            os.close(fd)
        if total != size or _sig_text(after) != _sig_text(before):
            return ('drop', 'changed_during_read')
        data = b''.join(chunks)
        try:
            text = data.decode('utf-8')
        except UnicodeDecodeError:
            return ('drop', 'invalid_utf8')
        # Acceptance is this hash of these bytes; the body is the same buffer.
        return ('ok', hashlib.sha256(data).hexdigest(), text)


def search_candidates_verified(index: Any, token: Any, binding: Any, fts_query: str,
                               limit: int, *,
                               collection_names: Optional[Tuple[str, ...]] = None,
                               refill_factor: int = DEFAULT_REFILL_FACTOR,
                               max_file_bytes: int = DEFAULT_CANDIDATE_FILE_BYTES,
                               max_query_bytes: int = DEFAULT_CANDIDATE_QUERY_BYTES,
                               max_query_files: int = DEFAULT_CANDIDATE_QUERY_FILES
                               ) -> List[Dict[str, Any]]:
    """One lexical phase with candidate verification, then ``finish``.

    Production-design query path (not yet wired to product callers). Ranking
    is the pinned generation's; it is not fresh corpus ranking.
    """
    with CandidateQuery(index, token, binding, max_file_bytes=max_file_bytes,
                        max_query_bytes=max_query_bytes,
                        max_query_files=max_query_files) as query:
        rows = query.search(fts_query, limit, collection_names=collection_names,
                            refill_factor=refill_factor)
        query.finish()
    return rows

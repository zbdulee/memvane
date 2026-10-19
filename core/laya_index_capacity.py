"""Durable SQLite sidecar for ``GenerationSourceBinding`` records.

Standalone capacity slice (see docs/plans/2026-10-19-laya-source-batched-scan.md,
"Durable binding sidecar"). It gives the candidate query path a durable binding
owner without any A-int schema change:

  * Bound generation names. A generation that may be served through this
    sidecar is named ``g-<uuid32>-b<digest64>``. ``digest`` is
    ``binding_digest(binding)``: a domain-separated SHA-256 of the canonical
    ``GenerationSourceBinding`` record *without* its self-referential
    ``generation`` field. A-int stores the name immutably, so the name itself
    commits to the scope, manifest, catalog scan, raw digest, incarnation and
    root identities. ``capture_bound_generation_binding`` /
    ``bind_generation`` allocate such names; the parent passes the same name
    to the claim queue (``enqueue_bound_generation``), stage and publish.
  * ``GenerationBindingStore`` is one SQLite file next to the A-int DB
    (rollback journal, ``synchronous=FULL``, ``fullfsync``). Its insert-only
    ``meta`` table pins it to one scope, one A-int ``incarnation`` and one set
    of ``(collection, canonical root)`` pairs. ``bindings`` is keyed by
    ``(scope, generation, db_incarnation)``; a lookup is one primary-key
    search, never a scan of the store.
  * Rows are insert-only: triggers refuse UPDATE, DELETE and REPLACE, and a
    CHECK ties ``binding_digest`` to the generation-name suffix. ``put``
    re-inserting the identical binding is ``exists``; a binding whose name
    does not commit to it is refused before any write.
  * Every transaction re-checks the file node, application id, schema and
    store identity. A looked-up row is decoded strictly and must match its
    key, the store identity and the digest in its generation name.
  * Triggers and CHECKs only stop accidents: anyone who can write the file
    can drop them. The name suffix is what stops a self-consistent rewrite:
    a row rewritten to another root, manifest or incarnation no longer hashes
    to the suffix of the A-int generation it claims, and A-int will not
    rename a generation.

Query helpers load the binding for the *pinned* generation
(``view_seal(token)['gen']``), never for the current head, and hand it to the
existing ``CandidateQuery``. A pinned head with an unsuffixed (legacy) name or
without a stored binding fails closed.

Product-path helpers (used by ``laya_index_flow.process_bound_one``,
``laya_index_sync.sync_step`` and ``LayaIndexRuntime.query_bound``; nothing
in host response or install calls them):

  * ``stage_bound_streaming`` is the bound ``stage_from_catalog`` without the
    whole-corpus document list: it streams the catalog's re-read, rehashed
    members into ``LayaIndexA.stage_stream``. It is still one full-corpus
    A-int transaction.
  * ``publish_bound`` refuses unless the store already holds exactly this
    binding, so persist-before-publish is enforced, not a caller contract.
  * ``load_job_binding`` lets a replayed claim reuse the binding stored under
    its own generation name; it never re-captures one.
  * ``sqlite_busy`` / ``binding_store_busy`` separate lock contention (retry)
    from damage, so callers can answer ``pending``/``deferred`` instead of
    raising or retiring work.
"""
from __future__ import annotations

import dataclasses
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import stat
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from laya_index_a import IndexContractError, InjectedFault
from laya_index_source_port import SourceStale, _raise_stale, _seal
from laya_source_catalog import (_HEX32, DEFAULT_CANDIDATE_FILE_BYTES,
                                 DEFAULT_CANDIDATE_QUERY_BYTES,
                                 DEFAULT_CANDIDATE_QUERY_FILES,
                                 DEFAULT_REFILL_FACTOR, CandidateQuery,
                                 GenerationSourceBinding, _check_roots,
                                 _db_incarnation, _require_binding_for,
                                 capture_generation_binding, load_exclusions,
                                 publish_from_catalog, search_candidates_verified)

STORE_FORMAT = 'laya-generation-binding-store'
STORE_VERSION = 2  # 1 was the unshipped append-only JSONL prototype
APP_ID = 0x4c594742  # LYGB; distinct from A-int, catalog and claim queue
SCHEMA_VERSION = 1
NAME_DOMAIN = b'laya.generation-name-binding.v1\x00'
MAX_RECORD_BYTES = 262_144
_GENERATION = re.compile(r'g-[0-9a-f]{32}-b([0-9a-f]{64})')
_CLOEXEC = getattr(os, 'O_CLOEXEC', 0)
_SIDECARS = ('-journal', '-wal', '-shm')
_META_KEYS = ('db_incarnation', 'format', 'roots', 'scope', 'store_id', 'version')

_SCHEMA = '''
CREATE TABLE meta(key TEXT PRIMARY KEY NOT NULL, value TEXT NOT NULL);
CREATE TABLE bindings(
  scope TEXT NOT NULL,
  generation TEXT NOT NULL,
  db_incarnation TEXT NOT NULL,
  binding_digest TEXT NOT NULL,
  record TEXT NOT NULL,
  PRIMARY KEY(scope, generation, db_incarnation),
  CHECK(length(binding_digest) = 64),
  CHECK(length(generation) = 100 AND substr(generation, 1, 2) = 'g-'
        AND substr(generation, 35, 2) = '-b'
        AND substr(generation, 37) = binding_digest),
  CHECK(length(record) <= 262144)
) WITHOUT ROWID;
CREATE TRIGGER meta_insert_seal BEFORE INSERT ON meta
  WHEN (SELECT COUNT(*) FROM meta) >= 6
  BEGIN SELECT RAISE(ABORT, 'binding_store_sealed'); END;
CREATE TRIGGER meta_update_seal BEFORE UPDATE ON meta
  BEGIN SELECT RAISE(ABORT, 'binding_store_sealed'); END;
CREATE TRIGGER meta_delete_seal BEFORE DELETE ON meta
  BEGIN SELECT RAISE(ABORT, 'binding_store_sealed'); END;
CREATE TRIGGER binding_insert_seal BEFORE INSERT ON bindings
  WHEN EXISTS (SELECT 1 FROM bindings WHERE scope=NEW.scope
               AND generation=NEW.generation AND db_incarnation=NEW.db_incarnation)
  BEGIN SELECT RAISE(ABORT, 'binding_immutable'); END;
CREATE TRIGGER binding_update_seal BEFORE UPDATE ON bindings
  BEGIN SELECT RAISE(ABORT, 'binding_immutable'); END;
CREATE TRIGGER binding_delete_seal BEFORE DELETE ON bindings
  BEGIN SELECT RAISE(ABORT, 'binding_immutable'); END;
'''
_LOOKUP_SQL = ('SELECT binding_digest, record FROM bindings '
               'WHERE scope=? AND generation=? AND db_incarnation=?')
_INSERT_SQL = ('INSERT INTO bindings(scope, generation, db_incarnation, binding_digest, '
               'record) VALUES(?,?,?,?,?)')


def _schema_rows(db: sqlite3.Connection) -> List[Tuple[Any, ...]]:
    return sorted(tuple(row) for row in
                  db.execute('SELECT type, name, tbl_name, sql FROM sqlite_master'))


def _expected_schema() -> List[Tuple[Any, ...]]:
    with closing(sqlite3.connect(':memory:')) as db:
        db.executescript(_SCHEMA)
        return _schema_rows(db)


_EXPECTED_SCHEMA = _expected_schema()


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True,
                      allow_nan=False).encode('ascii')


def _corrupt(detail: str, cause: Optional[BaseException] = None):
    _raise_stale(f'binding store corrupt: {detail}', cause)


def _reject_constant(name: str):
    raise ValueError(f'non-finite constant {name}')


def _fsync(fd: int) -> None:
    # macOS fsync does not flush the drive cache; F_FULLFSYNC does.
    full = getattr(fcntl, 'F_FULLFSYNC', None)
    if full is not None:
        try:
            fcntl.fcntl(fd, full)
            return
        except OSError:
            pass
    os.fsync(fd)


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | _CLOEXEC)
    try:
        _fsync(fd)
    finally:
        os.close(fd)


def sqlite_busy(exc: BaseException) -> bool:
    """SQLITE_BUSY/LOCKED after the busy timeout: retryable, not damage."""
    message = str(exc).lower()
    return (isinstance(exc, sqlite3.OperationalError)
            and ('locked' in message or 'busy' in message))


def binding_store_busy(exc: BaseException) -> bool:
    """``exc`` is a store failure caused by lock contention, not a bad file or row."""
    cause = exc.__cause__ if isinstance(exc, IndexContractError) else None
    return isinstance(cause, SourceStale) and str(cause) == 'binding store busy'


def _raise_sqlite(detail: str, exc: sqlite3.Error):
    if sqlite_busy(exc):
        detail = 'binding store busy'
    _raise_stale(detail, exc)


def _present(path: Path) -> bool:
    return path.exists() or path.is_symlink()


# -- bound generation names ----------------------------------------------------

def binding_digest(binding: GenerationSourceBinding) -> str:
    """SHA-256 over the canonical binding record minus ``generation``."""
    if not isinstance(binding, GenerationSourceBinding):
        raise ValueError('invalid generation source binding')
    record = binding.as_record()
    del record['generation']  # the name embeds this digest; it cannot hash itself
    return hashlib.sha256(NAME_DOMAIN + _canonical(record)).hexdigest()


def generation_digest(generation: Any) -> Optional[str]:
    """Digest committed by a bound name ``g-<uuid32>-b<digest64>``, else ``None``."""
    if not isinstance(generation, str):
        return None
    match = _GENERATION.fullmatch(generation)
    return match.group(1) if match else None


def bound_generation_name(binding: GenerationSourceBinding,
                          nonce: Optional[str] = None) -> str:
    """Fresh generation name committing to ``binding`` (its own name ignored)."""
    nonce = uuid.uuid4().hex if nonce is None else nonce
    if not isinstance(nonce, str) or not _HEX32.fullmatch(nonce):
        raise ValueError('invalid generation nonce')
    return f'g-{nonce}-b{binding_digest(binding)}'


def bind_generation(binding: GenerationSourceBinding,
                    nonce: Optional[str] = None) -> GenerationSourceBinding:
    """The same binding renamed to a bound generation name."""
    return dataclasses.replace(binding, generation=bound_generation_name(binding, nonce))


def require_bound_generation(binding: GenerationSourceBinding) -> str:
    """Raise unless ``binding.generation`` commits to ``binding``; returns the digest."""
    digest = generation_digest(binding.generation)
    if digest is None:
        raise IndexContractError('generation_name_unbound')
    if digest != binding_digest(binding):
        raise IndexContractError('generation_name_mismatch')
    return digest


def capture_bound_generation_binding(index: Any, catalog: Any, snapshot: Any, *,
                                     nonce: Optional[str] = None
                                     ) -> GenerationSourceBinding:
    """``capture_generation_binding`` that also allocates the bound name.

    The capture runs under a provisional ``g-<nonce>`` that is never queued,
    staged or stored; the returned binding carries the final bound name.
    """
    nonce = uuid.uuid4().hex if nonce is None else nonce
    if not isinstance(nonce, str) or not _HEX32.fullmatch(nonce):
        raise ValueError('invalid generation nonce')
    provisional = capture_generation_binding(index, catalog, snapshot, 'g-' + nonce)
    return bind_generation(provisional, nonce)


def enqueue_bound_generation(queue: Any, binding: GenerationSourceBinding,
                             base_gen: Optional[str]) -> Dict[str, Any]:
    """Claim-queue allocation for a bound generation.

    Calls the existing ``ClaimQueue.enqueue_next(scope, gen, source_manifest,
    base_gen)`` with the binding's own name and manifest. The returned job's
    ``claim``, ``claim_digest`` and ``claim_epoch`` go to stage/publish.
    """
    if not isinstance(binding, GenerationSourceBinding):
        raise ValueError('invalid generation source binding')
    require_bound_generation(binding)
    job = queue.enqueue_next(binding.scope, binding.generation, binding.source_manifest,
                             base_gen)
    if (not isinstance(job, dict) or job.get('scope') != binding.scope
            or job.get('gen') != binding.generation
            or job.get('source_manifest') != binding.source_manifest
            or job.get('base_gen') != base_gen):
        _raise_stale('claim job does not match generation binding')
    return job


# -- store -----------------------------------------------------------------------

class GenerationBindingStore:
    """Insert-only binding rows for one scope/incarnation/root set.

    ``create=True`` builds the schema in a temp file (rollback journal,
    ``synchronous=FULL``), fsyncs it, publishes it with a no-clobber ``link``
    and fsyncs the directory. An existing path, or a leftover
    ``-journal``/``-wal``/``-shm`` next to it, is refused. Every transaction
    first ``lstat``s the path and requires the node seen at open with exactly
    one link, then
    re-checks application id, schema and store metadata inside the
    transaction.
    """

    def __init__(self, path: Any, *, scope: str, db_incarnation: str, roots: Any,
                 create: bool = False):
        path = Path(path)
        if not path.is_absolute() or not path.parent.is_dir() or path.is_symlink():
            raise IndexContractError('invalid_explicit_path')
        if (not isinstance(scope, str) or not scope or not isinstance(db_incarnation, str)
                or not _HEX32.fullmatch(db_incarnation)):
            raise ValueError('invalid binding store identity')
        self.path = path
        self.scope = scope
        self.db_incarnation = db_incarnation
        self.roots = _check_roots(roots)
        self._roots_value = _canonical([[name, root] for name, root
                                        in self.roots.items()]).decode('ascii')
        self._node: Optional[Tuple[int, int]] = None
        self.store_id: Optional[str] = None
        if create:
            self._create()
        self._node = self._check_node()
        self._check_file_header()  # before SQLite may roll a journal or open a WAL
        try:
            self.db = sqlite3.connect(path.as_uri() + '?mode=rw', uri=True, timeout=2,
                                      isolation_level=None)
        except sqlite3.Error as exc:
            _raise_stale('binding store unavailable', exc)
        try:
            self._check_node()  # the path was not swapped while connecting
            try:
                _configure(self.db)
            except sqlite3.Error as exc:
                _raise_stale('binding store unreadable', exc)
            self.store_id = self._transaction(False, lambda store_id: store_id)
        except BaseException:
            self.db.close()
            raise

    def close(self) -> None:
        self.db.close()

    # -- file access ----------------------------------------------------------

    def _meta(self, store_id: str) -> Dict[str, str]:
        return {'format': STORE_FORMAT, 'version': str(STORE_VERSION),
                'store_id': store_id, 'scope': self.scope,
                'db_incarnation': self.db_incarnation, 'roots': self._roots_value}

    def _create(self) -> None:
        path = self.path
        if _present(path):
            raise IndexContractError('binding_store_exists')
        if any(_present(Path(str(path) + suffix)) for suffix in _SIDECARS):
            raise IndexContractError('stale_binding_store_sidecar')
        tmp = path.with_name(f'.{path.name}.create-{uuid.uuid4().hex}')
        fd = os.open(tmp, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | _CLOEXEC,
                     0o600)
        os.close(fd)
        try:
            with closing(sqlite3.connect(tmp, timeout=2, isolation_level=None)) as db:
                mode = db.execute('PRAGMA journal_mode=DELETE').fetchone()
                if mode is None or str(mode[0]).lower() != 'delete':
                    raise IndexContractError('creation_journal_unavailable')
                _configure(db)
                db.executescript(_SCHEMA)  # unpublished temp; atomicity not needed yet
                db.execute(f'PRAGMA application_id={APP_ID}')
                db.execute(f'PRAGMA user_version={SCHEMA_VERSION}')
                db.execute('BEGIN IMMEDIATE')
                db.executemany('INSERT INTO meta(key, value) VALUES(?,?)',
                               sorted(self._meta(uuid.uuid4().hex).items()))
                db.execute('COMMIT')
                _check_header(db)
            if any(_present(Path(str(tmp) + suffix)) for suffix in _SIDECARS):
                raise IndexContractError('creation_sidecar_unsettled')
            fd = os.open(tmp, os.O_RDONLY | os.O_NOFOLLOW | _CLOEXEC)
            try:
                _fsync(fd)
            finally:
                os.close(fd)
            try:
                os.link(tmp, path)  # atomic, never clobbers
            except FileExistsError:
                raise IndexContractError('binding_store_exists') from None
            _fsync_dir(path.parent)
        finally:
            tmp.unlink(missing_ok=True)
            for suffix in _SIDECARS:
                Path(str(tmp) + suffix).unlink(missing_ok=True)

    def _check_node(self) -> Tuple[int, int]:
        try:
            st = os.lstat(self.path)
        except FileNotFoundError as exc:
            _raise_stale('binding store missing', exc)
        except OSError as exc:
            _raise_stale('binding store unavailable', exc)
        node = (st.st_dev, st.st_ino)
        if not stat.S_ISREG(st.st_mode) or (self._node is not None and node != self._node):
            _raise_stale('binding store replaced')
        # A crash between ``link`` and the temp unlink in ``_create`` can leave a
        # second name whose ``-journal`` SQLite would track separately.
        if st.st_nlink != 1:
            _raise_stale('binding store has multiple links')
        return node

    def _check_file_header(self) -> None:
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | _CLOEXEC)
        except OSError as exc:
            _raise_stale('binding store unavailable', exc)
        try:
            st = os.fstat(fd)
            if (st.st_dev, st.st_ino) != self._node:
                _raise_stale('binding store replaced')
            header = os.pread(fd, 100, 0)
        finally:
            os.close(fd)
        if len(header) < 100 or header[:16] != b'SQLite format 3\x00':
            _raise_stale('binding store unreadable')
        if header[18:20] != b'\x01\x01':  # legacy rollback journal, not WAL
            _raise_stale('binding store journal mode differs')

    def _check_store(self) -> str:
        _check_header(self.db)
        if str(self.db.execute('PRAGMA journal_mode').fetchone()[0]).lower() != 'delete':
            _raise_stale('binding store journal mode differs')
        rows = self.db.execute('SELECT key, value FROM meta').fetchall()
        if any(not isinstance(key, str) or not isinstance(value, str) for key, value in rows):
            _corrupt('invalid store metadata')
        meta = dict(rows)
        if (sorted(meta) != list(_META_KEYS) or meta['format'] != STORE_FORMAT or meta['version'] != str(STORE_VERSION)
                or not _HEX32.fullmatch(meta['store_id'])):
            _corrupt('invalid store metadata')
        if meta['scope'] != self.scope:
            _raise_stale('binding store scope differs')
        if meta['db_incarnation'] != self.db_incarnation:
            _raise_stale('binding store incarnation differs')
        if meta['roots'] != self._roots_value:
            _raise_stale('binding store roots differ')
        if self.store_id is not None and meta['store_id'] != self.store_id:
            _raise_stale('binding store replaced')
        return meta['store_id']

    def _transaction(self, write: bool, fn: Callable[[str], Any]) -> Any:
        """``fn(store_id)`` in one transaction after node, schema and identity checks."""
        self._check_node()
        try:
            self.db.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
        except sqlite3.Error as exc:
            _raise_sqlite('binding store unavailable', exc)
        try:
            result = fn(self._check_store())
            self.db.execute('COMMIT')
        except sqlite3.Error as exc:
            self._rollback()
            _raise_sqlite('binding store unreadable', exc)
        except BaseException:
            self._rollback()
            raise
        return result

    def _rollback(self) -> None:
        try:
            self.db.execute('ROLLBACK')
        except sqlite3.Error:
            pass  # never replace the exception that is already propagating

    @staticmethod
    def _inject(fault_at: Optional[str], point: str) -> None:
        if fault_at == point:
            raise InjectedFault(f'binding_{point}')

    # -- validation -----------------------------------------------------------

    def _check_identity(self, binding: GenerationSourceBinding) -> bool:
        return (binding.scope == self.scope
                and binding.db_incarnation == self.db_incarnation
                and tuple((name, root) for name, root, _dev, _ino in binding.roots)
                == tuple(self.roots.items()))

    def _decode(self, generation: str, row: Tuple[Any, Any]) -> GenerationSourceBinding:
        column, text = row
        if not isinstance(text, str) or len(text) > MAX_RECORD_BYTES:
            _corrupt('invalid binding record')
        try:
            value = json.loads(text, parse_constant=_reject_constant)
        except (ValueError, RecursionError) as exc:
            _corrupt('unparseable binding record', exc)
        if not isinstance(value, dict) or _canonical(value) != text.encode('ascii', 'replace'):
            _corrupt('non-canonical binding record')
        try:
            binding = GenerationSourceBinding.from_record(value)
        except ValueError as exc:
            _corrupt('invalid binding record', exc)
        if binding.generation != generation:
            _corrupt('record key differs')
        if not self._check_identity(binding):
            _corrupt('record outside store identity')
        committed = generation_digest(generation)
        if committed is None or column != committed or binding_digest(binding) != committed:
            _corrupt('generation name does not commit to binding')
        return binding

    # -- public API -----------------------------------------------------------

    def get(self, scope: str, generation: str,
            db_incarnation: str) -> Optional[GenerationSourceBinding]:
        """Binding keyed by ``(scope, generation, db_incarnation)``, or ``None``.

        One primary-key lookup. An unbound (legacy) name fails closed.
        """
        if scope != self.scope or db_incarnation != self.db_incarnation:
            _raise_stale('binding key differs from store identity')
        if not isinstance(generation, str) or not generation:
            raise IndexContractError('invalid_identity')
        if generation_digest(generation) is None:
            _raise_stale('generation name is not bound')

        def lookup(_store_id: str) -> Optional[GenerationSourceBinding]:
            row = self.db.execute(_LOOKUP_SQL, (scope, generation, db_incarnation)).fetchone()
            return None if row is None else self._decode(generation, row)
        return self._transaction(False, lookup)

    def put(self, binding: GenerationSourceBinding, *,
            fault_at: Optional[str] = None) -> Dict[str, Any]:
        """Insert one immutable binding; returns after the FULL-sync commit.

        ``fault_at`` (tests only): ``'before_commit'`` raises after the INSERT,
        so the transaction rolls back; ``'after_commit'`` raises after the row
        is durable but before the caller sees the acknowledgement.
        """
        if not isinstance(binding, GenerationSourceBinding):
            raise ValueError('invalid generation source binding')
        digest = require_bound_generation(binding)
        if not self._check_identity(binding):
            _raise_stale('generation source binding does not match binding store')
        record = _canonical(binding.as_record()).decode('ascii')
        if len(record) > MAX_RECORD_BYTES:
            raise IndexContractError('binding_record_too_large')
        key = (binding.scope, binding.generation, binding.db_incarnation)

        def insert(_store_id: str) -> str:
            row = self.db.execute(_LOOKUP_SQL, key).fetchone()
            if row is not None:
                if self._decode(binding.generation, row) != binding:
                    raise IndexContractError('binding_conflict')
                return 'exists'
            self.db.execute(_INSERT_SQL, (*key, digest, record))
            self._inject(fault_at, 'before_commit')
            return 'stored'
        status = self._transaction(True, insert)
        self._inject(fault_at, 'after_commit')
        return {'status': status, 'generation': binding.generation, 'binding_digest': digest}

    def verify(self) -> Dict[str, Any]:
        """Audit only (O(records)): ``quick_check`` plus strict decode of every row."""
        def audit(store_id: str) -> Dict[str, Any]:
            if self.db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                _corrupt('quick_check failed')
            rows = self.db.execute('SELECT scope, generation, db_incarnation, binding_digest, '
                                   'record FROM bindings').fetchall()
            for scope, generation, incarnation, column, text in rows:
                if scope != self.scope or incarnation != self.db_incarnation:
                    _corrupt('record outside store identity')
                self._decode(generation, (column, text))
            return {'store_id': store_id, 'records': len(rows),
                    'generations': tuple(sorted(row[1] for row in rows))}
        return self._transaction(False, audit)


def _configure(db: sqlite3.Connection) -> None:
    db.execute('PRAGMA trusted_schema=OFF')
    db.execute('PRAGMA cell_size_check=ON')
    db.execute('PRAGMA synchronous=FULL')
    db.execute('PRAGMA fullfsync=ON')  # F_FULLFSYNC on macOS; no-op elsewhere
    if db.execute('PRAGMA synchronous').fetchone()[0] != 2:
        raise IndexContractError('binding_store_sync_unavailable')


def _check_header(db: sqlite3.Connection) -> None:
    if (db.execute('PRAGMA application_id').fetchone()[0] != APP_ID
            or db.execute('PRAGMA user_version').fetchone()[0] != SCHEMA_VERSION):
        _raise_stale('binding store format differs')
    if _schema_rows(db) != _EXPECTED_SCHEMA:
        _raise_stale('binding store schema differs')


# -- store-bound helpers ----------------------------------------------------------

def open_binding_store(path: Any, index: Any, catalog: Any, *,
                       create: bool = False) -> GenerationBindingStore:
    """Store bound to the catalog's scope/roots and the index's live incarnation."""
    return GenerationBindingStore(path, scope=catalog.scope,
                                  db_incarnation=_db_incarnation(index),
                                  roots=catalog.roots, create=create)


def _require_store_index(store: GenerationBindingStore, index: Any) -> None:
    if not isinstance(store, GenerationBindingStore):
        _raise_stale('generation binding store missing')
    if _db_incarnation(index) != store.db_incarnation:
        _raise_stale('database incarnation differs from binding store')


def persist_generation_binding(store: GenerationBindingStore, index: Any,
                               binding: GenerationSourceBinding, *,
                               fault_at: Optional[str] = None) -> Dict[str, Any]:
    """Persist-before-publish step: call any time before A-int publish.

    Publish only after this returns ``stored`` or ``exists``. Any exception
    means nothing may be published for this generation. ``publish_bound``
    enforces the ordering; a direct ``publish_from_catalog`` caller does not.
    ``sync_step`` persists right after capture, before the claim is queued,
    so a replayed claim always finds its binding.
    ``put`` checks names and store identity only, not live root ``(dev, ino)``;
    ``CandidateQuery`` checks those at query time.
    """
    _require_store_index(store, index)
    return store.put(binding, fault_at=fault_at)


def load_job_binding(store: GenerationBindingStore, index: Any,
                     job: Dict[str, Any]) -> Optional[GenerationSourceBinding]:
    """Stored binding for a queued claim's own generation name, else ``None``.

    ``None`` (unbound legacy name, nothing stored, manifest differs) means the
    claim can never publish on the bound path; the caller retires it.
    """
    _require_store_index(store, index)
    if (not isinstance(job, dict) or job.get('scope') != store.scope
            or generation_digest(job.get('gen')) is None):
        return None
    gen = job['gen']
    binding = store.get(store.scope, gen, store.db_incarnation)
    if binding is None or binding.source_manifest != job.get('source_manifest'):
        return None
    return binding


def same_bound_source(left: Optional[GenerationSourceBinding],
                      right: Optional[GenerationSourceBinding]) -> bool:
    """Both bindings serve the same bytes from the same live root nodes.

    Catalog scan id and raw digest are ignored: a rescan of unchanged bytes
    does not need a new generation. Manifest, incarnation and every root
    ``(collection, root, dev, ino)`` must be equal.
    """
    return (isinstance(left, GenerationSourceBinding)
            and isinstance(right, GenerationSourceBinding)
            and (left.scope, left.source_manifest, left.db_incarnation, left.roots)
            == (right.scope, right.source_manifest, right.db_incarnation, right.roots))


def stage_bound_streaming(index: Any, catalog: Any, binding: GenerationSourceBinding, *,
                          base: Optional[str], claim_digest: str, claim_epoch: int,
                          fault_after: Optional[int] = None) -> Any:
    """Bound ``stage_from_catalog`` without the whole-corpus document list.

    Same snapshot and binding checks as ``stage_from_catalog(binding=...)``
    (current COMPLETE snapshot, pending newer scan allowed, no stat hint).
    Every member is re-read no-follow and rehashed against the snapshot by
    ``catalog.iter_documents`` and handed one at a time to
    ``LayaIndexA.stage_stream``; nothing is reused because a hash matched.
    Still one A-int transaction over the whole corpus: a changed member, a
    newer scan publishing mid-iteration or a digest mismatch stages nothing.
    """
    if not isinstance(binding, GenerationSourceBinding):
        raise ValueError('invalid generation source binding')
    require_bound_generation(binding)
    snapshot = catalog.require_complete()
    exclusions = load_exclusions(index, catalog.scope)
    manifest = catalog.manifest(snapshot, exclusions, allow_pending=True)
    _require_binding_for(index, catalog, snapshot, binding, binding.generation, manifest)
    documents = catalog.iter_documents(snapshot, exclusions, allow_pending=True)
    try:
        return index.stage_stream(binding.generation, catalog.scope, documents, fault_after,
                                  base=base, source_manifest=manifest,
                                  claim_digest=claim_digest, claim_epoch=claim_epoch)
    finally:
        documents.close()


def publish_bound(index: Any, catalog: Any, store: GenerationBindingStore,
                  binding: GenerationSourceBinding, job: Dict[str, Any], *,
                  require_vector: bool = False) -> Any:
    """``publish_from_catalog(binding=...)`` only after the binding is durable.

    The stored row for ``binding.generation`` must equal ``binding`` and the
    claim job must carry the same scope, name and manifest; otherwise
    ``index_stale`` and nothing is published.
    """
    _require_store_index(store, index)
    if not isinstance(binding, GenerationSourceBinding):
        raise ValueError('invalid generation source binding')
    require_bound_generation(binding)
    if (not isinstance(job, dict) or job.get('scope') != binding.scope
            or job.get('gen') != binding.generation
            or job.get('source_manifest') != binding.source_manifest):
        _raise_stale('claim job does not match generation binding')
    if store.get(binding.scope, binding.generation, store.db_incarnation) != binding:
        _raise_stale('generation source binding not persisted before publish')
    return publish_from_catalog(index, catalog, job['base_gen'], binding.generation,
                                job['claim'], claim_digest=job['claim_digest'],
                                claim_epoch=job['claim_epoch'],
                                require_vector=require_vector, binding=binding)


def load_pinned_binding(index: Any, token: Any,
                        store: GenerationBindingStore) -> GenerationSourceBinding:
    """Binding of the generation the token pins; never of the current head."""
    _require_store_index(store, index)
    scope, gen, manifest, _epoch = _seal(index, token)
    if scope != store.scope:
        _raise_stale('view seal scope differs from binding store')
    if not isinstance(gen, str) or not gen:
        _raise_stale('malformed view seal')
    committed = generation_digest(gen)
    if committed is None:
        _raise_stale('generation name is not bound')
    binding = store.get(scope, gen, store.db_incarnation)
    if binding is None:
        _raise_stale('generation source binding missing')
    if binding_digest(binding) != committed:
        _raise_stale('generation name does not commit to binding')
    if binding.source_manifest != manifest:
        _raise_stale('view seal does not match generation source binding')
    return binding


def open_pinned_candidate_query(index: Any, token: Any, store: GenerationBindingStore, *,
                                max_file_bytes: int = DEFAULT_CANDIDATE_FILE_BYTES,
                                max_query_bytes: int = DEFAULT_CANDIDATE_QUERY_BYTES,
                                max_query_files: int = DEFAULT_CANDIDATE_QUERY_FILES
                                ) -> CandidateQuery:
    """``CandidateQuery`` for the pinned generation's stored binding, if safe.

    ``CandidateQuery`` re-checks seal, incarnation and live root identities
    on open and again at ``finish()``.
    """
    return CandidateQuery(index, token, load_pinned_binding(index, token, store),
                          max_file_bytes=max_file_bytes, max_query_bytes=max_query_bytes,
                          max_query_files=max_query_files)


def search_pinned_candidates(index: Any, token: Any, store: GenerationBindingStore,
                             fts_query: str, limit: int, *,
                             collection_names: Optional[Tuple[str, ...]] = None,
                             refill_factor: int = DEFAULT_REFILL_FACTOR,
                             max_file_bytes: int = DEFAULT_CANDIDATE_FILE_BYTES,
                             max_query_bytes: int = DEFAULT_CANDIDATE_QUERY_BYTES,
                             max_query_files: int = DEFAULT_CANDIDATE_QUERY_FILES):
    """``search_candidates_verified`` with the pinned generation's stored binding."""
    return search_candidates_verified(index, token, load_pinned_binding(index, token, store),
                                      fts_query, limit, collection_names=collection_names,
                                      refill_factor=refill_factor,
                                      max_file_bytes=max_file_bytes,
                                      max_query_bytes=max_query_bytes,
                                      max_query_files=max_query_files)

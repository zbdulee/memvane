"""Explicit A-int source/claim service for a future verified host selection.

This module does not select a project, install an engine, create a database,
or claim that a project-local route is installer authorization. The host must
verify the package, installer-owned active journal, project scope/root and DB
incarnation before it constructs this service.

``source_root`` is a v1 root string, or a Mapping of collection name to an
absolute resolved directory. Collection roots must be pairwise disjoint; the
whole binding is rejected before any database is opened.

Capacity path (optional, Mapping roots only): ``catalog_path`` and
``binding_store_path`` name an existing ``LayaSourceCatalog`` and
``GenerationBindingStore`` for the same scope, roots and DB incarnation. The
runtime never creates them. ``sync_step`` writes through them and
``query_bound`` reads the pinned generation's stored binding. Their modules
are imported only when these paths are given.
"""
from __future__ import annotations

from collections.abc import Mapping
import os
import re
from pathlib import Path
import sqlite3
import stat
from types import MappingProxyType

from laya_index_a import IndexContractError, LayaIndexA
from laya_index_claim_queue import ClaimQueue
from laya_index_source_port import (_COLLECTION, scan, scan_collections,
                                    search_collections_verified, search_verified)


_INCARNATION = re.compile(r"[0-9a-f]{32}\Z")
_MAX_COLLECTIONS = 16  # the scan_collections bound


# Capacity path stores beside the A-int DB. The installer owns them; no hook
# or worker creates them, and callers use the capacity path only when both
# exist.
CATALOG_SUFFIX = '.catalog.sqlite'
BINDING_STORE_SUFFIX = '.bindings.sqlite'


def capacity_store_paths(db_path):
    """``(catalog, binding store)`` beside ``db_path``, or None when neither exists.

    Both must be private regular files of this user. One without the other,
    a symlink or a shared mode fails closed.
    """
    db = Path(db_path)
    paths = (db.with_name(db.name + CATALOG_SUFFIX),
             db.with_name(db.name + BINDING_STORE_SUFFIX))
    present = []
    for path in paths:
        try:
            info = path.lstat()
        except FileNotFoundError:
            present.append(False)
            continue
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o077):
            raise IndexContractError('capacity_store_unsafe')
        present.append(True)
    if not any(present):
        return None
    if not all(present):
        raise IndexContractError('capacity_store_incomplete')
    return tuple(str(path) for path in paths)


def _collection_roots(roots):
    """Return a frozen sorted copy of valid disjoint roots, else None."""
    try:
        items = sorted(roots.items())
    except (TypeError, ValueError):
        return None
    if not 1 <= len(items) <= _MAX_COLLECTIONS:
        return None
    paths = []
    for name, root in items:
        if (not isinstance(name, str) or _COLLECTION.fullmatch(name) is None
                or not isinstance(root, str)):
            return None
        try:
            path = Path(root)
            if (not path.is_absolute() or not path.is_dir()
                    or str(path.resolve(strict=True)) != root):
                return None
        except (OSError, RuntimeError, TypeError, ValueError):
            return None
        paths.append(path)
    # Path ancestry, not a string prefix: '/' must overlap every other root.
    if any(left == right or left in right.parents or right in left.parents
           for i, left in enumerate(paths) for right in paths[i + 1:]):
        return None
    return MappingProxyType(dict(items))


class LayaIndexRuntime:
    """One explicitly bound scope; caller owns installation and routing proof."""

    def __init__(self, *, source_root: str | Mapping[str, str], scope: str,
                 db_path: str, queue_path: str, db_incarnation: str,
                 catalog_path: str | None = None,
                 binding_store_path: str | None = None):
        capacity = (catalog_path, binding_store_path)
        if capacity != (None, None) and (
                not isinstance(source_root, Mapping)
                or any(not isinstance(path, str) or not Path(path).is_absolute()
                       for path in (db_path, queue_path, *capacity))
                or len({db_path, queue_path, *capacity}) != 4):
            raise IndexContractError('invalid_runtime_binding')
        if isinstance(source_root, Mapping):
            source_root = _collection_roots(source_root)
            source_valid = source_root is not None
        else:
            try:
                source = Path(source_root)
                source_valid = (source.is_absolute() and source.is_dir()
                                and source.resolve(strict=True) == source)
            except (OSError, RuntimeError, TypeError, ValueError):
                source_valid = False
        if (not isinstance(scope, str) or _INCARNATION.fullmatch(scope) is None
                or not isinstance(source_root, (str, MappingProxyType))
                or not source_valid
                or not isinstance(db_path, str) or not Path(db_path).is_absolute()
                or not isinstance(queue_path, str) or not Path(queue_path).is_absolute()
                or db_path == queue_path or not isinstance(db_incarnation, str)
                or _INCARNATION.fullmatch(db_incarnation) is None):
            raise IndexContractError('invalid_runtime_binding')
        self.source_root = source_root
        self.scope = scope
        self.catalog = None
        self.binding_store = None
        # In-process sync_step memo and pins a busy A-int kept; never durable.
        self._snapshot_verdict = None
        self._unreleased_pins = []
        self.index = LayaIndexA(db_path)
        try:
            if self.index.incarnation() != db_incarnation:
                raise IndexContractError('database_incarnation_mismatch')
            self.queue = ClaimQueue.open(queue_path)
            try:
                if self.queue.binding() != db_incarnation:
                    raise IndexContractError('queue_index_binding_mismatch')
                if catalog_path is not None:
                    self._open_capacity(catalog_path, binding_store_path, db_incarnation)
            except BaseException:
                self.queue.close()
                raise
        except BaseException:
            self.index.close()
            raise

    def _open_capacity(self, catalog_path, binding_store_path, db_incarnation):
        from laya_index_capacity import GenerationBindingStore
        from laya_source_catalog import LayaSourceCatalog
        roots = dict(self.source_root)
        self.catalog = LayaSourceCatalog(catalog_path, roots, self.scope)
        try:
            self.binding_store = GenerationBindingStore(
                binding_store_path, scope=self.scope, db_incarnation=db_incarnation,
                roots=roots)
        except BaseException:
            self.catalog.close()
            self.catalog = None
            raise

    def close(self):
        try:
            if self.binding_store is not None:
                self.binding_store.close()
        finally:
            try:
                if self.catalog is not None:
                    self.catalog.close()
            finally:
                try:
                    self.queue.close()
                finally:
                    self.index.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def inspect(self):
        """Read-only logical index check; does not authorize migration or GC."""
        return self.index.inspect()

    def query(self, fts_query: str, limit: int):
        """Pin exactly one generation and serve bytes verified against source."""
        token, _gen = self.index.pin(self.scope)
        try:
            if isinstance(self.source_root, Mapping):
                return search_collections_verified(self.index, self.source_root,
                                                   token, fts_query, limit)
            return search_verified(self.index, self.source_root, token, fts_query, limit)
        finally:
            self.index.release(token)

    def _require_capacity(self):
        if self.catalog is None or self.binding_store is None:
            raise IndexContractError('capacity_not_configured')

    def request_scan(self):
        """Ask for one more catalog scan (coalesced); never touches the source."""
        self._require_capacity()
        return self.catalog.request_scan()

    def capacity_status(self):
        """Durable catalog state and scan backlog; not a freshness claim."""
        self._require_capacity()
        return self.catalog.status()

    def query_bound(self, fts_query: str, limit: int, *, collection_names=None,
                    max_file_bytes=None, max_query_bytes=None, max_query_files=None):
        """Lexical query of the pinned generation through its stored binding.

        Returns ``{'status', 'generation', 'rows', ...}``:

        * ``ok``: rows whose current bytes were re-read and hash to the
          indexed hash (``[]`` is a normal empty answer, including index lag);
        * ``pending``: zero rows, retry after sync. ``reason`` is
          ``no_published_generation``, ``candidates_unverified`` (every
          candidate dropped; ``detail`` has the counts), ``index_busy`` (an
          A-int writer, e.g. a ``sync_step`` stage holding the write lock for
          a whole corpus pass, outlasted the 2 s busy timeout) or
          ``binding_store_busy``;
        * ``stale``: zero rows, fail closed. ``reason`` is the diagnostic,
          e.g. a missing, unbound or unreadable binding, a replaced root, a
          recreated DB or an exclusion added since publish. Never served from
          A-int bodies.

        Pin and release each take the A-int write lock. If the release stays
        busy, verified rows are withheld (``ok`` becomes ``pending``/
        ``index_busy``) and the answer has ``pin_released: False``; the pin
        row is retried by later ``query_bound`` calls on this runtime only. A
        process exit leaves it in A-int, where it keeps its generation in the
        read-only ``gc_plan`` census and affects no answer.

        Ranking is the pinned generation's; it is not whole-corpus freshness.
        """
        self._require_capacity()
        from laya_index_capacity import (binding_store_busy, search_pinned_candidates,
                                         sqlite_busy)
        from laya_source_catalog import CatalogPending
        from laya_index_source_port import SourceStale
        budgets = {name: value for name, value in (
            ('max_file_bytes', max_file_bytes), ('max_query_bytes', max_query_bytes),
            ('max_query_files', max_query_files)) if value is not None}

        def answer(status, generation, rows=(), **extra):
            return {'status': status, 'generation': generation, 'rows': list(rows), **extra}

        try:
            token, gen = self.index.pin(self.scope)
        except IndexContractError as exc:
            if str(exc) == 'no_published_generation':
                return answer('pending', None, reason='no_published_generation')
            if str(exc) == 'index_stale':
                return answer('stale', None, reason='exclusion_epoch_changed')
            raise
        except sqlite3.OperationalError as exc:
            if sqlite_busy(exc):
                return answer('pending', None, reason='index_busy')
            raise

        def search():
            try:
                rows = search_pinned_candidates(self.index, token, self.binding_store,
                                                fts_query, limit,
                                                collection_names=collection_names,
                                                **budgets)
                return answer('ok', gen, rows)
            except CatalogPending as exc:
                return answer('pending', gen, reason=exc.reason, detail=exc.detail)
            except IndexContractError as exc:
                if binding_store_busy(exc):
                    return answer('pending', gen, reason='binding_store_busy')
                cause = exc.__cause__
                return answer('stale', gen, reason=(str(cause) if isinstance(cause, SourceStale)
                                                    else str(exc)))
            except sqlite3.OperationalError as exc:
                if sqlite_busy(exc):
                    return answer('pending', gen, reason='index_busy')
                raise

        try:
            result = search()
        except BaseException:
            try:
                self._release(token)
            except sqlite3.Error:
                pass  # never replace the exception that is already propagating
            raise
        if self._release(token):
            return result
        if result['status'] == 'ok':
            return answer('pending', gen, reason='index_busy', pin_released=False)
        return {**result, 'pin_released': False}

    def _release(self, token):
        """Release ``token``, then retry pins earlier calls could not release.

        ``False`` when A-int stayed busy and ``token`` is still pinned; it is
        kept for a later call. Any other release failure propagates.
        """
        from laya_index_capacity import sqlite_busy
        try:
            self.index.release(token)
        except sqlite3.OperationalError as exc:
            self._unreleased_pins.append(token)
            if sqlite_busy(exc):
                return False
            raise
        while self._unreleased_pins:
            try:
                self.index.release(self._unreleased_pins[0])
            except sqlite3.Error:
                break  # this call's own answer is already settled
            self._unreleased_pins.pop(0)
        return True

    def enqueue_claim(self, job: dict):
        """Retain an externally identified job only for the exact current source.

        A durable upstream owner must supply claim/gen/base/epoch identities.
        This method deliberately does not infer them from branch names or time.
        """
        if not isinstance(job, dict) or job.get('scope') != self.scope:
            raise IndexContractError('claim_scope_mismatch')
        source = (scan_collections(self.source_root, self.scope, self.index)
                  if isinstance(self.source_root, Mapping)
                  else scan(self.source_root, self.scope, self.index))
        if source.manifest != job.get('source_manifest'):
            raise IndexContractError('index_stale')
        return self.queue.enqueue(job)

    def process_claim(self, claim: str, *, recover: bool = False):
        """Selected writers must use sync_full with an owner and journal fence."""
        if recover:
            raise IndexContractError('owner_death_proof_required')
        raise IndexContractError('selected_sync_only')

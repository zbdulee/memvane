"""Explicit A-int source/claim service for a future verified host selection.

This module does not select a project, install an engine, create a database,
or claim that a project-local route is installer authorization. The host must
verify the package, installer-owned active journal, project scope/root and DB
incarnation before it constructs this service.
"""
from __future__ import annotations

import re
from pathlib import Path

from laya_index_a import IndexContractError, LayaIndexA
from laya_index_claim_queue import ClaimQueue
from laya_index_source_port import scan, search_verified


_INCARNATION = re.compile(r"[0-9a-f]{32}\Z")


class LayaIndexRuntime:
    """One explicitly bound scope; caller owns installation and routing proof."""

    def __init__(self, *, source_root: str, scope: str, db_path: str,
                 queue_path: str, db_incarnation: str):
        try:
            source = Path(source_root)
            source_valid = (source.is_absolute() and source.is_dir()
                            and source.resolve(strict=True) == source)
        except (OSError, RuntimeError, TypeError, ValueError):
            source_valid = False
        if (not isinstance(scope, str) or _INCARNATION.fullmatch(scope) is None
                or not isinstance(source_root, str) or not source_valid
                or not isinstance(db_path, str) or not Path(db_path).is_absolute()
                or not isinstance(queue_path, str) or not Path(queue_path).is_absolute()
                or db_path == queue_path or not isinstance(db_incarnation, str)
                or _INCARNATION.fullmatch(db_incarnation) is None):
            raise IndexContractError('invalid_runtime_binding')
        self.source_root = source_root
        self.scope = scope
        self.index = LayaIndexA(db_path)
        try:
            if self.index.incarnation() != db_incarnation:
                raise IndexContractError('database_incarnation_mismatch')
            self.queue = ClaimQueue.open(queue_path)
            try:
                if self.queue.binding() != db_incarnation:
                    raise IndexContractError('queue_index_binding_mismatch')
            except BaseException:
                self.queue.close()
                raise
        except BaseException:
            self.index.close()
            raise

    def close(self):
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
            return search_verified(self.index, self.source_root, token, fts_query, limit)
        finally:
            self.index.release(token)

    def enqueue_claim(self, job: dict):
        """Retain an externally identified job only for the exact current source.

        A durable upstream owner must supply claim/gen/base/epoch identities.
        This method deliberately does not infer them from branch names or time.
        """
        if not isinstance(job, dict) or job.get('scope') != self.scope:
            raise IndexContractError('claim_scope_mismatch')
        source = scan(self.source_root, self.scope, self.index)
        if source.manifest != job.get('source_manifest'):
            raise IndexContractError('index_stale')
        return self.queue.enqueue(job)

    def process_claim(self, claim: str, *, recover: bool = False):
        """Selected writers must use sync_full with an owner and journal fence."""
        if recover:
            raise IndexContractError('owner_death_proof_required')
        raise IndexContractError('selected_sync_only')

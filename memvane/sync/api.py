"""Public sync operations."""
from __future__ import annotations


def run(project, *, coordinator=None, report=None, start_service=None):
    """One sync pass; ``start_service(manifest, *, admit)`` replaces the warm embedding service."""
    from memvane.sync.product_sync_worker import run as implementation
    return implementation(project, coordinator=coordinator, report=report, start_service=start_service)


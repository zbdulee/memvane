"""Public sync operations."""
from __future__ import annotations


def run(project, *, coordinator=None, report=None):
    from memvane.sync.product_sync_worker import run as implementation
    return implementation(project, coordinator=coordinator, report=report)


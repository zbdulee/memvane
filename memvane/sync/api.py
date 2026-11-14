"""Public sync operations."""
from __future__ import annotations


def run(project, *, coordinator=None, report=None):
    from memvane.sync.product_sync_worker import run as implementation
    return implementation(project, coordinator=coordinator, report=report)


def schema3_sources(decision, handle, deadline, *, raw_scan=None):
    from memvane.sync.product_sync_worker import _schema3_sources as implementation
    return implementation(decision, handle, deadline, raw_scan=raw_scan)


def schema3_wiki_cards(root):
    from memvane.sync.product_sync_worker import _schema3_wiki_cards as implementation
    return implementation(root)

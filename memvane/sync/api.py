"""Public sync operations."""
from __future__ import annotations


def run(*args, **kwargs):
    from memvane.sync.product_sync_worker import run as implementation
    return implementation(*args, **kwargs)


def select_route(*args, **kwargs):
    from memvane.sync.product_sync_worker import select_route as implementation
    return implementation(*args, **kwargs)


def owned_selected_proof(*args, **kwargs):
    from memvane.sync.product_sync_worker import _owned_selected_proof as implementation
    return implementation(*args, **kwargs)


def selected(*args, **kwargs):
    from memvane.sync.product_sync_worker import _selected as implementation
    return implementation(*args, **kwargs)


def schema3_head_same(*args, **kwargs):
    from memvane.sync.product_sync_worker import _schema3_head_same as implementation
    return implementation(*args, **kwargs)


def schema3_rows_baseline(*args, **kwargs):
    from memvane.sync.product_sync_worker import _schema3_rows_baseline as implementation
    return implementation(*args, **kwargs)


def schema3_sources(*args, **kwargs):
    from memvane.sync.product_sync_worker import _schema3_sources as implementation
    return implementation(*args, **kwargs)


def schema3_wiki_cards(*args, **kwargs):
    from memvane.sync.product_sync_worker import _schema3_wiki_cards as implementation
    return implementation(*args, **kwargs)


def scan_collections(*args, **kwargs):
    from memvane.sync.source_scan import scan_collections as implementation
    return implementation(*args, **kwargs)


def sync_owner(*args, **kwargs):
    from memvane.sync.sync_owner_lock import sync_owner as implementation
    return implementation(*args, **kwargs)


def validate_sync_owner(*args, **kwargs):
    from memvane.sync.sync_owner_lock import validate_sync_owner as implementation
    return implementation(*args, **kwargs)

"""Public sync operations."""
from __future__ import annotations


def run(*args, **kwargs):
    from memvane.sync.product_sync_worker import run as implementation
    return implementation(*args, **kwargs)


def select_route(*args, **kwargs):
    from memvane.sync.product_sync_worker import select_route as implementation
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

"""Public sync operations."""
from __future__ import annotations


def run(*args, **kwargs):
    from memvane.sync.product_sync_worker import run as implementation
    return implementation(*args, **kwargs)




def schema3_sources(*args, **kwargs):
    from memvane.sync.product_sync_worker import _schema3_sources as implementation
    return implementation(*args, **kwargs)


def schema3_wiki_cards(*args, **kwargs):
    from memvane.sync.product_sync_worker import _schema3_wiki_cards as implementation
    return implementation(*args, **kwargs)

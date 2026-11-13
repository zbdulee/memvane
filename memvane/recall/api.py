"""Public recall rule operations."""
from __future__ import annotations


def fts_atom(*args, **kwargs):
    from memvane.recall.domain.rules import _fts_atom
    return _fts_atom(*args, **kwargs)


def hybrid_fts_queries(*args, **kwargs):
    from memvane.recall.domain.rules import _hybrid_fts_queries
    return _hybrid_fts_queries(*args, **kwargs)


def fuse_verified_lists(*args, **kwargs):
    from memvane.recall.domain.rules import _fuse_verified_lists
    return _fuse_verified_lists(*args, **kwargs)

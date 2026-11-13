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


def strip_ko_suffix(*args, **kwargs):
    from memvane.recall.keywords import strip_ko_suffix as implementation
    return implementation(*args, **kwargs)


def approved_profile_matches(*args, **kwargs):
    from memvane.recall.reader_dispatch import _approved_profile_matches as implementation
    return implementation(*args, **kwargs)


def approved_query_vector(*args, **kwargs):
    from memvane.recall.reader_dispatch import approved_query_vector as implementation
    return implementation(*args, **kwargs)


def query_endpoint(*args, **kwargs):
    from memvane.recall.reader_dispatch import query_endpoint as implementation
    return implementation(*args, **kwargs)


def serve_schema3_recall(*args, **kwargs):
    from memvane.recall.selected_recall import serve_schema3_recall as implementation
    return implementation(*args, **kwargs)


def serve_endpoint_recall(*args, **kwargs):
    from memvane.recall.selected_recall import serve_endpoint_recall as implementation
    return implementation(*args, **kwargs)

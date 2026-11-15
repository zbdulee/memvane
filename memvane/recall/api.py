"""Public recall rule operations."""
from __future__ import annotations

from memvane.recall.types import MAX_ROWS


def fts_atom(term):
    from memvane.recall.domain.rules import _fts_atom
    return _fts_atom(term)


def hybrid_fts_queries(prompt, lexical_patterns):
    from memvane.recall.domain.rules import _hybrid_fts_queries
    return _hybrid_fts_queries(prompt, lexical_patterns)


def fuse_verified_lists(lists, *, max_rows=MAX_ROWS):
    from memvane.recall.domain.rules import _fuse_verified_lists
    return _fuse_verified_lists(lists, max_rows=max_rows)


def strip_ko_suffix(token):
    from memvane.recall.keywords import strip_ko_suffix as implementation
    return implementation(token)


def approved_profile_matches(candidate, proof):
    from memvane.recall.reader_dispatch import _approved_profile_matches as implementation
    return implementation(candidate, proof)


def approved_query_vector(*, authority, snapshot, decision, provider, prompt, deadline):
    from memvane.recall.reader_dispatch import approved_query_vector as implementation
    return implementation(authority=authority, snapshot=snapshot, decision=decision, provider=provider,
                          prompt=prompt, deadline=deadline)


def query_endpoint(endpoint, *, checkout, prompt, query_vector, deadline, snapshot=None, decision=None,
                   provider=None, authority=None, linked=None):
    from memvane.recall.reader_dispatch import query_endpoint as implementation
    return implementation(endpoint, checkout=checkout, prompt=prompt, query_vector=query_vector,
                          deadline=deadline, snapshot=snapshot, decision=decision, provider=provider,
                          authority=authority, linked=linked)


def serve_endpoint_recall(host, payload, *, cwd, trace, route_decision=None, main_proof=None,
                          linked=None):
    from memvane.recall.selected_recall import serve_endpoint_recall as implementation
    return implementation(host, payload, cwd=cwd, trace=trace, route_decision=route_decision,
                          main_proof=main_proof, linked=linked)


def recall_scope():
    from memvane.recall.recall_scope import recall_scope as implementation
    return implementation()


def set_writer_allowed(allowed):
    from memvane.recall.recall_scope import set_writer_allowed as implementation
    return implementation(allowed)


def recall_epoch(proof):
    from memvane.recall.recall_scope import _recall_epoch as implementation
    return implementation(proof)


def recall_sync_wake(project_root):
    from memvane.recall.recall_scope import _recall_sync_wake as implementation
    return implementation(project_root)


def wake_provider(decision, proof, *, transition=False):
    from memvane.recall.recall_scope import _wake_provider as implementation
    return implementation(decision, proof, transition=transition)

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


def serve_schema3_recall(host, payload, *, endpoint, proof, decision, project, prompt, deadline,
                         main_proof, linked=None, _recall_epoch, _trace_fields, _RECALL_STATE, _fail,
                         _recall_sync_wake, _write_selection_log, _trace_final, host_payload,
                         _wake_provider, _proof_still_current, _serve_verified_rows):
    from memvane.recall.selected_recall import serve_schema3_recall as implementation
    return implementation(host, payload, endpoint=endpoint, proof=proof, decision=decision,
                          project=project, prompt=prompt, deadline=deadline, main_proof=main_proof,
                          linked=linked, _recall_epoch=_recall_epoch, _trace_fields=_trace_fields,
                          _RECALL_STATE=_RECALL_STATE, _fail=_fail, _recall_sync_wake=_recall_sync_wake,
                          _write_selection_log=_write_selection_log, _trace_final=_trace_final,
                          host_payload=host_payload, _wake_provider=_wake_provider,
                          _proof_still_current=_proof_still_current,
                          _serve_verified_rows=_serve_verified_rows)


def serve_endpoint_recall(host, payload, *, cwd, route_decision=None, main_proof=None, linked=None,
                          _recall_epoch, _trace_fields, _RECALL_STATE, _fail, _recall_sync_wake,
                          _write_selection_log, _trace_final, host_payload, _wake_provider,
                          _proof_still_current, _serve_verified_rows):
    from memvane.recall.selected_recall import serve_endpoint_recall as implementation
    return implementation(host, payload, cwd=cwd, route_decision=route_decision, main_proof=main_proof,
                          linked=linked, _recall_epoch=_recall_epoch, _trace_fields=_trace_fields,
                          _RECALL_STATE=_RECALL_STATE, _fail=_fail, _recall_sync_wake=_recall_sync_wake,
                          _write_selection_log=_write_selection_log, _trace_final=_trace_final,
                          host_payload=host_payload, _wake_provider=_wake_provider,
                          _proof_still_current=_proof_still_current,
                          _serve_verified_rows=_serve_verified_rows)

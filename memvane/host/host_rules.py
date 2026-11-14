"""Pure host trace and action decisions."""
from __future__ import annotations

def selection_final(cards):
    return 'selected' if cards else 'no_results_after_filter'


def trace_result(action, count, outcome, reason, final, notes):
    if action == 'recall':
        if outcome is None:
            outcome = 'served' if count else 'empty'
            reason = final or (notes[-1] if notes else 'no_context')
    elif outcome is None:
        outcome = 'ok'
        reason = final
    return outcome, reason


def is_session(action):
    return action in ('update', 'update-queued')


def discovery_grant_kind(has_roots, action):
    if not has_roots or action == 'index':
        return None
    return 'sync' if action == 'file-change-event' else 'session_start'


def check_compile_queue(has_collections, action):
    return has_collections and action not in ('index', 'file-change-event')


def gate_response(host):
    return {'action': 'continue'} if host == 'hermes' else {}

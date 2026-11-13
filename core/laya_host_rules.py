"""Pure host response selection and action decisions."""
from __future__ import annotations

def verified_pool(rows):
    return rows[:15]


def limit_selected(chosen, top_n):
    limit = min(top_n, 3)
    return chosen[:limit], limit


def selection_action(reason, chosen):
    if reason not in ('selected', 'ranked') or not chosen:
        if reason == 'selected':
            return 'selected_zero'
        if reason == 'no_candidates':
            return 'no_candidates'
        return 'empty'
    return 'serve'


def selector_action(reason):
    if reason in ('selected', 'no_candidates'):
        return 'accept'
    if reason == 'candidate_invalid':
        return 'invalid'
    return 'fallback'


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


def reader_reseal_candidate(code, has_route):
    return code == 'reader_endpoint_changed' and has_route


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


def drift_action(action):
    if action == 'recall':
        return 'recall'
    if is_session(action):
        return 'session'
    if action == 'gate':
        return 'gate'
    return 'defer'

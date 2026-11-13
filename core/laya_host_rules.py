"""Pure recall selection, event, and transition decisions."""
from __future__ import annotations

_UPGRADE_TRANSITIONS = frozenset((
    'selected_route_changed', 'selected_route_unavailable', 'selected_proof_changed',
    'incremental_base_changed', 'host_route_unverified', 'host_decision_changed'))


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


def is_upgrade_transition(code):
    return code in _UPGRADE_TRANSITIONS


def fence_reason(stalled):
    return 'bundle_upgrade_stalled' if stalled else 'bundle_upgrade_in_progress'

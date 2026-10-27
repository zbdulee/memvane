"""Query-time read coordinator for recall's live selection.

`select` is the single entry recall calls when live selection is on. It moves
the former inline block out of `recall.py` unchanged: state resolution,
shared/profile consent checks, the corpus snapshot, the pure
`selection_port` steps, the work estimate and the local selector, in the same
order and with the same exception mapping. Recall keeps the adaptive pool
plan, every log line, the request view pin and all capture/write paths.

Not read-only in the strict sense: `seam._private_state` may create the
private state directory, exactly as it did inline. That is a separate change.

Every learning name is imported inside `select` on each call, so tests that
replace module attributes (`live_select.choose`, `shared._policy`, ...) keep
working. `HookDeadlineExceeded` is a BaseException and is not caught.
"""
from __future__ import annotations

def select_published(pool, *, prompt, config, project_root,
                     wiki_names, scope_id, deadline=None):
    """Select verified Laya-index cards with the existing local Laya model.

    The index query supplies immutable, provenance-checked wiki rows. This
    coordinator never opens source files, queries QMD, or creates learning state.
    A missing/unavailable selector returns no cards instead of RRF topN.
    """
    from . import selection_port
    from .contracts import digest
    from .store import canonical
    from pathlib import Path
    import time
    out = {'final_results': [], 'reason': 'candidate_unavailable', 'resources': {},
           'candidate_count': 0}
    try:
        rows = selection_port.published_candidate_rows(pool, wiki_names, scope_id)
        out['candidate_count'] = len(rows)
        if not rows:
            out['reason'] = 'no_candidates'
            return out
        from .seam import _private_state
        from .live_select import choose
        state = _private_state(config, Path(project_root), create=False)
        pointer = 'active-checkpoint.json'
        shared = config.get('sharedLearning', {})
        if isinstance(shared, dict) and shared.get('enabled') is True:
            if shared.get('profile') == 'qmd-local-learning-profile-v1':
                from .learning_profile import selection_root
                from .candidate_apply import readable
                state = selection_root(config, project_root)
                if state is not None:
                    readable(state)
            elif shared.get('trialSelection') is True:
                from .shared import _safe_root, _policy, _current_config, trial_eligible
                state = _safe_root(config, project_root)
                if state is not None:
                    _policy(state)
                    if _current_config(state, project_root) is None:
                        state = None
                    elif not trial_eligible(state, deadline=deadline):
                        out['reason'] = 'shared_trial_not_eligible'
                        return out
                pointer = 'experimental-checkpoint.json'
        if state is None:
            out['reason'] = 'private_state_unavailable'
            return out
        fingerprint = digest(canonical([str(project_root), scope_id,
            [(row['id'], row['revision_sha256'], source['generation'])
             for row, source in zip(rows, pool)]]))
        if deadline is None:
            deadline = time.monotonic() + min(30., max(.2, float(config.get('queryTimeout', 5))))
        ids, reason, resources = choose(state, prompt, rows,
            corpus_fingerprint=fingerprint, deadline=deadline, pointer_name=pointer)
        out['reason'], out['resources'] = reason, resources
        if ids is not None:
            out['final_results'] = selection_port.apply_published_selection(pool, ids)
    except CAUGHT:
        out['reason'] = 'candidate_unavailable'
    return out

# The exact errors recall always mapped to a fallback reason.
CAUGHT = (OSError, ValueError, UnicodeError, KeyError, TypeError)
# ValueError messages that surface as their own reason.
PASSTHROUGH_REASONS = ('input_window_unavailable', 'responsiveness_deferred')


def select(pool, limit, top_n, *, config, card_root, qmd_paths, prompt,
           is_wiki_collection, resolve_path, shared_trial, shared_profile,
           live_plan, edit_view, rerank_target_seconds, deadline):
    """Live selection over the bounded `pool[:limit]`.

    Returns a dict: `final_results`, `reason`, `chose_zero`,
    `candidate_count`, `resources`, `corpus`, `model_rows`, `work_estimate`.
    Values reached before a failure are kept (e.g. the corpus snapshot or
    the work estimate), as recall's diagnostics always reported them.
    `deadline` is called at each use, like `hook_budget.deadline`.
    """
    out = {'final_results': pool[:min(top_n, 3)], 'reason': 'disabled',
           'chose_zero': False, 'candidate_count': min(len(pool), limit),
           'resources': {}, 'corpus': None, 'model_rows': [], 'work_estimate': {}}
    try:
        from context_learning.corpus import snapshot as corpus_snapshot
        from context_learning.live_select import choose
        from context_learning.seam import _private_state, _snapshot
        from context_learning import selection_port
        state = _private_state(config, card_root)
        shared_state = None
        if shared_trial:
            from context_learning.shared import _current_config, _policy, _safe_root
            shared_state = _safe_root(config, card_root)
            if shared_state is not None:
                _policy(shared_state)
                if shared_profile:
                    from context_learning.learning_profile import selection_root
                    shared_state = selection_root(config, card_root)
                elif _current_config(shared_state, card_root) is None:
                    shared_state = None
        current_corpus = out['corpus'] = corpus_snapshot(card_root, config, qmd_paths=qmd_paths)
        indexed_hashes = selection_port.indexed_revisions(current_corpus)
        if shared_trial and live_plan['requested'] == 0:
            out['reason'] = live_plan['reason']
        elif shared_trial and shared_state is None:
            out['reason'] = 'shared_state_unavailable'
        elif state is None and shared_state is None:
            out['reason'] = 'private_state_unavailable'
        elif (pool_reason := selection_port.pool_rejection(
                pool, limit, is_wiki_collection)) is not None:
            out['reason'] = pool_reason
        else:
            # Read-only port: all-or-nothing rows from the bounded pool.
            model_rows = out['model_rows'] = selection_port.candidate_rows(
                pool, limit, resolve_path=resolve_path,
                snapshot=_snapshot, indexed=indexed_hashes)
            if shared_trial:
                from context_learning.adaptive_pool import actual_work
                from context_learning.compact_input import derive
                bodies = [derive(row['source_text'], row['revision_sha256'])['body_text']
                          for row in model_rows]
                work_estimate = out['work_estimate'] = actual_work(
                    prompt, bodies, edit_view=edit_view,
                    rerank_target_seconds=rerank_target_seconds)
                if not work_estimate['all_pairs_fit_estimate']:
                    raise ValueError('input_window_unavailable')
                if edit_view and not work_estimate['within_rerank_target_estimate']:
                    # Preserve the complete captured QMD pool. Scoring a
                    # shorter subset would make applied inference differ
                    # from the gold/training candidate view.
                    raise ValueError('responsiveness_deferred')
            selection_kwargs = {
                'corpus_fingerprint': current_corpus['fingerprint'] if current_corpus else None,
                'deadline': deadline()}
            if shared_state is not None:
                if shared_profile:
                    from context_learning.candidate_apply import readable
                    readable(shared_state)
                else:
                    from context_learning.shared import trial_eligible
                    if not trial_eligible(shared_state, deadline=deadline()):
                        raise ValueError('shared_trial_not_eligible')
                    selection_kwargs['pointer_name'] = 'experimental-checkpoint.json'
            selected_ids, reason, resources = choose(
                shared_state if shared_state is not None else state,
                prompt, model_rows, **selection_kwargs)
            out['reason'], out['resources'] = reason, resources
            if selected_ids is not None:
                out['final_results'] = selection_port.apply_selection(pool, limit, selected_ids)
                out['chose_zero'] = not selected_ids
    except CAUGHT as exc:
        out['reason'] = str(exc) if str(exc) in PASSTHROUGH_REASONS else 'candidate_unavailable'
    if shared_trial and out['reason'] != 'selected':
        out['final_results'] = pool[:top_n]
    return out

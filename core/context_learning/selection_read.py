"""Query-time read coordinator for the selected Laya recall's live selection.

`select_published` is the entry the selected recall calls when live selection
is on: state resolution, shared/profile consent checks, the corpus snapshot,
the pure `selection_port` steps, the work estimate and the local selector.

Not read-only in the strict sense: `seam._private_state` may create the
private state directory.

Every learning name is imported inside the entry on each call, so tests that
replace module attributes (`live_select.choose`, `shared._policy`, ...) keep
working. `HookDeadlineExceeded` is a BaseException and is not caught.
"""
from __future__ import annotations


# The errors a published selection maps to a fallback reason.
CAUGHT = (OSError, ValueError, UnicodeError, KeyError, TypeError)


def selector_prepared(config, project_root):
    """Whether a Laya-route selector exists to consult; never creates state.

    An approved shared trial/profile, or
    a prepared local checkpoint. Without one the caller serves its ranked rows.
    """
    shared = config.get('sharedLearning', {}) if isinstance(config, dict) else {}
    if (isinstance(shared, dict) and shared.get('enabled') is True
            and (shared.get('trialSelection') is True
                 or shared.get('profile') == 'qmd-local-learning-profile-v1')):
        return True
    from .seam import local_checkpoint_prepared
    return local_checkpoint_prepared(config, project_root)


def select_published(pool, *, prompt, config, project_root,
                     wiki_names, scope_id, deadline=None):
    """Select verified Laya-index cards with the existing local Laya model.

    The index query supplies immutable, provenance-checked wiki rows. This
    coordinator never opens source files, queries QMD, or creates learning state.
    A missing/unavailable selector returns no cards and its reason; the Laya
    host then serves its ranked rows. An invalid pool is `candidate_invalid`.
    """
    from . import selection_port
    from .contracts import digest
    from .store import canonical
    from pathlib import Path
    import time
    out = {'final_results': [], 'reason': 'candidate_unavailable', 'resources': {},
           'candidate_count': 0}
    try:
        try:
            rows = selection_port.published_candidate_rows(pool, wiki_names, scope_id)
        except ValueError:
            out['reason'] = 'candidate_invalid'
            return out
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

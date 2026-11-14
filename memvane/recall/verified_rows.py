"""Card selection and the served answer of rows the index already verified."""
from __future__ import annotations

import sys

from memvane.recall.types import HostResponseError, RecallAnswer


def _fail(code: str):
    raise HostResponseError(code)


def _select_verified_cards(decision, settings, prompt, rows, trace):
    """Card selection after the index proved published wiki rows.

    Returns ``(rows, reason, fallback)``. With a prepared selector its answer
    is final (``selected``, including a real zero-card choice). Without one,
    or when the prepared selector cannot answer, the verified rows keep their
    hybrid rank (``ranked``) so search never empties on selector
    infrastructure; ``fallback`` names why. An invalid pool serves nothing.
    """
    from memvane.learning.api import select_published, selector_prepared
    from memvane.route.api import wiki_index_roots
    roots = wiki_index_roots(decision['collections'])
    names = tuple(sorted(roots))
    if not names:
        _fail('selected_wiki_required')
    project = decision['projectRoot']
    from memvane.recall.domain.rules import selector_action, verified_pool
    pool = verified_pool(rows)
    if not pool:
        return [], 'no_candidates', None
    try:
        from memvane.journal.api import inspect_journal
        from memvane.kernel.memvane_version import plugin_version, version_key
        selected = inspect_journal(decision['journalPath'])
        running = version_key(plugin_version())
        highest = version_key(selected['maxPluginVersion'])
        if (selected['state'] != 'selected' or selected['installId'] != decision['installId']
                or running is None or highest is None):
            raise ValueError('selector_version_unavailable')
        if running < highest:
            prepared, result = True, {'reason': 'older_plugin_version', 'final_results': []}
        else:
            prepared, result = selector_prepared(settings, project), None
    except Exception:
        # Learning state unreachable (moving, sealed old root): hybrid rank.
        prepared, result = True, {'reason': 'selector_unavailable', 'final_results': []}
    if prepared:
        if result is None:
            try:
                from memvane.resident.api import ensure_service
                result = select_published(
                    pool, prompt=prompt, config=settings, project_root=project,
                    wiki_names=names, scope_id=decision['scopeId'],
                    ensure_service=ensure_service)
            except Exception:
                result = {'reason': 'selector_unavailable', 'final_results': []}
        reason = result['reason']
        selection = selector_action(reason)
        if selection == 'accept':
            return result['final_results'], reason, None
        if selection == 'invalid':
            trace.note('laya_selector_candidate_invalid')
            print('[memvane] published candidates invalid; no context injected',
                  file=sys.stderr)
            return [], reason, None
        fallback = 'laya_selector_' + reason
        trace.note(fallback)
        print('[memvane] local selection unavailable (' + reason + '); serving hybrid rank',
              file=sys.stderr)
    else:
        fallback = 'laya_selector_not_prepared'
    from memvane.learning.api import published_candidate_rows
    try:
        # The same published-row contract the selector input must satisfy.
        published_candidate_rows(pool, names, decision['scopeId'])
    except ValueError:
        trace.note('laya_selector_candidate_invalid')
        print('[memvane] published candidates invalid; no context injected',
              file=sys.stderr)
        return [], 'candidate_invalid', None
    return pool, 'ranked', fallback


def _serve_verified_rows(decision, settings, prompt, rows, *, top_n, title_only, locations,
                         generation, still_current, vector_fallback, allow_mixed_generation,
                         trace, own_view=False):
    """Selector, trace and recheck of verified rows; returns the recall's answer.

    ``still_current(chosen)``
    is the store's own proof/view recheck after selection.
    """
    chosen, reason, fallback = _select_verified_cards(decision, settings, prompt, rows, trace)
    trace.fields(selection=reason, candidates=len(rows), generation=generation)
    from memvane.recall.domain.rules import limit_selected, selection_action
    chosen, answer_limit = limit_selected(chosen, top_n)
    # Capture sees the verified pool and what was served: the selector's
    # choice, or the ranked rows when no prepared selector answered.
    capture = (settings, rows[:15], chosen)
    result_action = selection_action(reason, chosen)
    if result_action != 'serve':
        if result_action == 'selected_zero':
            trace.note('laya_selector_selected_zero')
        elif result_action == 'no_candidates':
            # The reason of a recall with no candidate rows; vector fallbacks stay notes.
            trace.final('no_candidates')
        return RecallAnswer(decision, capture=capture, own_view=own_view)
    if not still_current(chosen):
        _fail('selected_proof_changed')
    chosen_paths = {row['path'] for row in chosen}
    return RecallAnswer(
        decision, rows=chosen, limit=answer_limit,
        ranking='laya_selection' if reason == 'selected' else 'hybrid_rrf',
        title_only_paths=title_only.intersection(chosen_paths),
        source_locations={path: spans for path, spans in locations.items()
                          if path in chosen_paths},
        allow_mixed_generation=allow_mixed_generation, selection_fallback=fallback,
        vector_fallback=vector_fallback, capture=capture, own_view=own_view)

"""Card selection and the served answer of rows the index already verified."""
from __future__ import annotations

import sys
import time

from memvane.recall.types import HostResponseError, RecallAnswer


def _fail(code: str):
    raise HostResponseError(code)


# The live selector refuses a prompt longer than this (``live_select.choose``).
EDIT_SELECTOR_PROMPT_CHARS = 2000

# Selector work measured by the inference worker (``adapter.predict_loaded``)
# and its client, renamed for the recall event. Numbers and the device only.
_SELECTOR_MEASURES = (('pairs', 'selectorPairs'), ('planned_pairs', 'selectorPlannedPairs'),
                      ('batches', 'selectorBatches'), ('truncated_pairs', 'selectorTruncatedPairs'),
                      ('max_batch_size', 'selectorBatchLimit'),
                      ('forward_seconds', 'selectorForwardSeconds'),
                      ('resident_wall_seconds', 'selectorResidentSeconds'),
                      ('queue_wait_seconds', 'selectorQueueSeconds'))


def _selector_measurement(trace, resources) -> None:
    """Record the selector's pair, batch and device measurement; never raises."""
    try:
        if not isinstance(resources, dict):
            return
        fields = {}
        for key, name in _SELECTOR_MEASURES:
            value = resources.get(key)
            if type(value) in (int, float) and value == value and value >= 0:
                fields[name] = value
        batches = fields.get('selectorBatches')
        if batches and 'selectorForwardSeconds' in fields:
            fields['selectorBatchMs'] = round(fields['selectorForwardSeconds'] * 1000 / batches, 3)
        device = resources.get('device')
        if isinstance(device, str) and device.split(':', 1)[0] in ('cpu', 'mps', 'cuda'):
            fields['selectorDevice'] = device.split(':', 1)[0]
        if fields:
            trace.fields(**fields)
    except Exception:
        pass


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
                from memvane.recall.recall_scope import edit_recall
                edit = edit_recall()
                options = {}
                if edit is not None:
                    # An edit recall asks the selector only until its own
                    # deadline, never while the service serves another
                    # request, and with the prompt length the live selector
                    # accepts (query fragments come with 1.0.19).
                    prompt = prompt[:EDIT_SELECTOR_PROMPT_CHARS]
                    options = {'deadline': edit['selector_deadline'], 'skip_busy': True}
                result = select_published(
                    pool, prompt=prompt, config=settings, project_root=project,
                    wiki_names=names, scope_id=decision['scopeId'],
                    ensure_service=ensure_service, **options)
            except Exception:
                result = {'reason': 'selector_unavailable', 'final_results': []}
        _selector_measurement(trace, result.get('resources'))
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
    started = time.monotonic()
    try:
        chosen, reason, fallback = _select_verified_cards(decision, settings, prompt, rows, trace)
    finally:
        trace.fields(selectorMs=round((time.monotonic() - started) * 1000, 3))
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
    started = time.monotonic()
    try:
        current = still_current(chosen)
    finally:
        trace.fields(stillCurrentMs=round((time.monotonic() - started) * 1000, 3))
    if not current:
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

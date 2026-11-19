"""Card selection and the served answer of rows the index already verified."""
from __future__ import annotations

import sys
import time

from memvane.recall.types import HostResponseError, RecallAnswer


def _fail(code: str):
    raise HostResponseError(code)


# The selector scores the SELECTOR_CANDIDATES best ranked cards of the
# verified pool (the capture keeps the whole pool). Each lane has a selector
# budget and a pair budget; past the budget the ranked rows are served.
# Values and their measurement: docs/continual-learning.md "Selector budget".
SELECTOR_CANDIDATES = 10
PROMPT_SELECTOR_SECONDS = 3.0
PROMPT_PAIR_BUDGET = 24
HOOK_RESERVE_SECONDS = 1.0
EDIT_PAIR_BUDGET = 91
# The live selector refuses a prompt over this many UTF-8 bytes.
SELECTOR_PROMPT_BYTES = 65536

# Selector work measured by the inference worker (``adapter.predict_loaded``),
# the service and its client, renamed for the recall event (no field name may
# contain "token": the event log drops it). Numbers and the device only.
_SELECTOR_MEASURES = (('pairs', 'selectorPairs'), ('planned_pairs', 'selectorPlannedPairs'),
                      ('batches', 'selectorBatches'), ('truncated_pairs', 'selectorTruncatedPairs'),
                      ('max_batch_size', 'selectorBatchLimit'),
                      ('max_padded_tokens', 'selectorPaddedLimit'),
                      ('forward_seconds', 'selectorForwardSeconds'),
                      ('resident_wall_seconds', 'selectorResidentSeconds'),
                      ('queue_wait_seconds', 'selectorQueueSeconds'),
                      ('client_identity_seconds', 'selectorClientIdentitySeconds'),
                      ('argv_seconds', 'selectorArgvSeconds'),
                      ('envelope_seconds', 'selectorEnvelopeSeconds'),
                      ('initial_validation_seconds', 'selectorValidationSeconds'),
                      ('identity_seconds', 'selectorIdentitySeconds'),
                      ('revalidation_seconds', 'selectorRevalidationSeconds'),
                      ('model_load_seconds', 'selectorLoadSeconds'),
                      ('tokenize_seconds', 'selectorPrepareSeconds'),
                      ('batch_prepare_seconds', 'selectorBatchPrepareSeconds'),
                      ('selector_wall_seconds', 'selectorWallSeconds'),
                      ('pair_len_p50', 'selectorPairLenP50'),
                      ('pair_len_p95', 'selectorPairLenP95'),
                      ('pair_len_max', 'selectorPairLenMax'),
                      ('padded_total', 'selectorPadded'),
                      ('query_chunks', 'selectorQueryChunks'),
                      ('card_chunks_max', 'selectorCardChunksMax'),
                      ('chunked_cards', 'selectorChunkedCards'),
                      ('dropped_pairs', 'selectorDroppedPairs'),
                      ('dropped_query', 'selectorDroppedQuery'),
                      ('dropped_card', 'selectorDroppedCard'),
                      ('cancelled_after_batches', 'selectorCancelledAfter'))


def _selector_prompt(prompt: str) -> str:
    """The prompt within the selector's byte limit: its head and its tail.

    The prompt lane's pair plan reads the first chunks and the last one, so
    an over-limit middle is never scored anyway.
    """
    data = prompt.encode('utf-8')
    if len(data) <= SELECTOR_PROMPT_BYTES:
        return prompt
    head = data[:SELECTOR_PROMPT_BYTES * 3 // 4].decode('utf-8', 'ignore')
    tail = data[-(SELECTOR_PROMPT_BYTES // 4 - 2):].decode('utf-8', 'ignore')
    return head + '\n' + tail


def _prompt_selector_seconds(settings) -> float:
    """``min(queryTimeout, 3 s)``; the timeout clamp of the selection read."""
    try:
        timeout = min(30., max(.2, float(settings.get('queryTimeout', 5))))
    except (AttributeError, TypeError, ValueError):
        timeout = 5.
    return min(PROMPT_SELECTOR_SECONDS, timeout)


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
                if edit is not None:
                    # An edit recall asks the selector only until its own
                    # deadline, never while the service serves another request.
                    lane, budget, deadline = 'edit', EDIT_PAIR_BUDGET, edit['selector_deadline']
                    options = {'skip_busy': True}
                else:
                    from memvane.kernel.hook_budget import deadline as hook_deadline
                    lane, budget, options = 'prompt', PROMPT_PAIR_BUDGET, {}
                    # Inside the armed hook deadline, one second kept for the answer.
                    deadline = min(time.monotonic() + _prompt_selector_seconds(settings),
                                   hook_deadline() - HOOK_RESERVE_SECONDS)
                candidates = pool[:SELECTOR_CANDIDATES]
                trace.fields(selectorLane=lane, selectorCandidates=len(candidates),
                             selectorBudgetMs=max(0, round((deadline - time.monotonic()) * 1000)))
                result = select_published(
                    candidates, prompt=_selector_prompt(prompt), config=settings,
                    project_root=project, wiki_names=names, scope_id=decision['scopeId'],
                    ensure_service=ensure_service, deadline=deadline, pair_budget=budget,
                    lane=lane, **options)
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

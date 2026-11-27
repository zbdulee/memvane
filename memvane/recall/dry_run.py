"""Dry run of the selected recall: the four stages of one prompt, with no side effect.

``dry_run_recall`` follows the prompt recall's route and reader rules
(``selected_recall``, ``reader_dispatch``) and keeps what the served answer
drops: the FTS lists with their bm25 scores, the semantic list with its
cosines, the RRF scores and each pool card's selector decision. It writes no
event or stderr line, records no sync or provider wake, starts no provider,
inference service or trainer, captures no learning case and bounds its work
by the caller's ``deadline`` alone (never the hook budget).
"""
from __future__ import annotations

from contextlib import ExitStack
import math
from pathlib import Path
import re
import time

from memvane.recall.types import DryRunRecallResult, HostResponseError, MAX_ROWS, RecallTrace

_CODE = re.compile(r'[a-z0-9_]{1,80}')
# The dry run's own reasons; every other reason is the selected recall's.
SELECTOR_DISABLED = 'selector_disabled'
SELECTOR_NOT_PREPARED = 'selector_not_prepared'
SELECTOR_NOT_WARM = 'selector_not_warm'
SEMANTIC_DISABLED = 'semantic_disabled'


def _fail(code: str):
    raise HostResponseError(code)


class _Collected(RecallTrace):
    """A trace port that keeps fields and notes for the result; it writes nothing."""

    def __init__(self):
        self.values, self.notes = {}, []

    def fields(self, **fields) -> None:
        self.values.update(fields)

    def note(self, reason: str) -> None:
        self.notes.append(reason)


def _code(exc) -> str:
    """A fixed refusal code for ``exc``; never its text when that is not a code."""
    for value in (str(exc), getattr(exc, 'reason', None)):
        if isinstance(value, str) and _CODE.fullmatch(value):
            return value
    return 'dry_run_failed'


def _ms(started) -> float:
    return round((time.monotonic() - started) * 1000, 3)


def dry_run_recall(target_root, prompt, *, deadline, use_semantic=True, use_selector=False):
    """The ``DryRunRecallResult`` of ``prompt`` in the checkout at ``target_root``.

    ``deadline`` is an absolute ``time.monotonic()`` bound. ``use_semantic``
    asks an already warm provider for the query vector; ``use_selector`` asks
    an already warm, admitted selector (cold: ``selector_not_warm`` or
    ``selector_not_prepared`` without asking). A refusal of the selected
    recall is the result's ``refused`` outcome with its fixed code.
    """
    started = time.monotonic()
    route = None
    try:
        if not isinstance(prompt, str) or not prompt.strip():
            _fail('invalid_query_prompt')
        if (type(deadline) not in (int, float) or not math.isfinite(deadline)
                or deadline <= time.monotonic()):
            _fail('invalid_deadline')
        if not isinstance(target_root, (str, Path)) or not Path(target_root).is_absolute():
            _fail('invalid_target_root')
        try:
            target = Path(target_root).resolve(strict=True)
        except OSError:
            _fail('invalid_target_root')
        from memvane.journal import api as journal
        decision = journal.select_route(str(target), action='recall', deadline=deadline)
        route = decision.get('state') if isinstance(decision, dict) else None
        if route == 'preview':
            return DryRunRecallResult(route='preview', outcome='empty', reason='initial_build_preview',
                                      timings={'totalMs': _ms(started)})
        if route == 'worktree':
            main, linked = decision.get('mainRoute'), decision
            if not isinstance(main, dict) or main.get('state') != 'candidate':
                _fail('worktree_route_invalid')
        elif route == 'candidate':
            main, linked = decision, None
        else:
            return DryRunRecallResult(route=str(route), outcome='empty', reason='route_not_selected',
                                      timings={'totalMs': _ms(started)})
        from memvane.recall.recall_scope import recall_scope, set_writer_allowed
        with recall_scope():
            # No sync wake from this recall scope, whatever a helper asks for.
            set_writer_allowed(False)
            result = _dry_run_selected(main, linked, target, prompt, deadline=deadline,
                                       use_semantic=use_semantic, use_selector=use_selector)
        if linked is not None and journal.select_route(
                str(target), action='recall', deadline=deadline) != decision:
            _fail('selected_route_changed')
        result['timings']['totalMs'] = _ms(started)
        return DryRunRecallResult(route=route, linked=linked is not None, **result)
    except Exception as exc:  # noqa: BLE001 - every refusal is the result's fixed code
        return DryRunRecallResult(route=None if route is None else str(route), outcome='refused',
                                  reason=_code(exc), timings={'totalMs': _ms(started)})


def _major_allowed(value) -> bool:
    from memvane.kernel.memvane_version import same_major
    try:
        return same_major(value['stateFormat'])
    except (OSError, KeyError, TypeError, ValueError):
        _fail('state_format_invalid')


def _empty(reason, *, notes=(), timings=None, **fields):
    return dict(outcome='empty', reason=reason, notes=list(notes),
                timings={} if timings is None else timings, **fields)


def _dry_run_selected(main, linked, target, prompt, *, deadline, use_semantic, use_selector):
    """The selected route's proof, reader endpoint and the four stages (``_serve_endpoint_recall``)."""
    from memvane.journal import api as journal
    from memvane.recall.domain.rules import reader_reseal_candidate
    from memvane.recall.recall_scope import _recall_epoch
    from memvane.recall.selected_recall import _RESETTLE_PENDING, _settings_resettle_pending
    if main.get('routeSchema') == 'memvane-route-v4' and not _major_allowed(main):
        return _empty('state_format_mismatch')
    proof = journal.verify_selected_route(main)
    if proof.get('productRouteAllowed') is not True:
        _fail('worktree_selection_unapproved' if linked is not None else 'selected_product_unavailable')
    if not _major_allowed(proof):
        return _empty('state_format_mismatch')
    project = Path(main['projectRoot'])
    if linked is None and target != project and project not in target.parents:
        _fail('reader_selection_unproved')
    root = Path(__file__).resolve().parents[2]
    with ExitStack() as stack:
        for attempt in range(2):
            try:
                endpoint = stack.enter_context(
                    journal.resolve_endpoint(project, action='recall', host_root=root))
                break
            except ValueError as exc:
                if (reader_reseal_candidate(str(exc), True)
                        and _settings_resettle_pending(main, project, root)):
                    return _empty(_RESETTLE_PENDING, notes=['laya_' + _RESETTLE_PENDING])
                if (attempt or str(exc) != 'reader_selection_changed'
                        or time.monotonic() >= deadline):
                    raise
        _record, _raw, reader_proof = journal.recheck_reader(endpoint)
        if reader_proof.get('readerDecision') != main:
            _fail('reader_selection_unproved')
        return _dry_run_schema3(endpoint, reader_proof, main, linked, project, prompt,
                                route_proof=proof, deadline=deadline, use_semantic=use_semantic,
                                use_selector=use_selector, stack=stack, epoch=_recall_epoch)


def _provider(proof, vector_off):
    """The reviewed provider of ``proof``, or None with its reason in ``vector_off``."""
    from memvane.kernel.errors import IndexContractError
    from memvane.provider.api import reviewed_provider
    if not isinstance(proof.get('providerManifest'), dict):
        _fail('vector_provider_missing')
    if proof.get('providerAvailable') is False:
        vector_off.append('provider_artifact_changed')
        return None
    try:
        return reviewed_provider(proof['providerManifest'])
    except IndexContractError as exc:
        vector_off.append('provider_manifest_' + (str(exc) if re.fullmatch(
            r'[a-z0-9_]{1,60}', str(exc)) else 'invalid'))
        return None


def _provider_state_code(proof) -> str:
    """The vector fallback of a provider that gave no vector, read from its owner state only.

    The prompt recall's ``_wake_provider`` decides the same codes and then
    wakes the owner; the dry run reads the state and wakes nothing. A
    terminal record of a non-transient failure is ``selected_provider_start_failed``.
    """
    from memvane.resident.api import read_owner_state
    from memvane.resident.types import TRANSIENT_FAILURES
    try:
        state = read_owner_state(proof['providerManifest'])
    except Exception as exc:
        raise HostResponseError('selected_provider_state_invalid') from exc
    if state is None:
        return 'selected_provider_preparing'
    if state['terminal'] and state['reason'] not in TRANSIENT_FAILURES:
        return 'selected_provider_start_failed'
    if state['reason'] == 'resource_deferred':
        return 'selected_provider_resource_deferred'
    if state['reason'] in ('startup_failed', 'worker_exited', 'unexpected'):
        return 'selected_provider_retry_wait'
    return 'selected_provider_preparing'


def _dry_run_schema3(endpoint, proof, decision, linked, project, prompt, *, route_proof, deadline,
                     use_semantic, use_selector, stack, epoch):
    """``serve_schema3_recall`` without its wakes, stderr lines or capture."""
    from memvane.settings import api as config
    from memvane.journal import api as reader
    from memvane.recall import reader_dispatch
    from memvane.recall.recall_scope import _proof_still_current
    epoch(proof)
    timings, notes = {}, []
    settings = config.load_project_config(decision['projectRoot'])
    selected_settings = decision['recallSettings']
    if any(settings.get(key) != value for key, value in selected_settings.items()):
        _fail('selected_recall_settings_changed')
    top_n = selected_settings.get('topN')
    patterns = selected_settings.get('lexicalPatterns')
    if (type(top_n) is not int or not 1 <= top_n <= MAX_ROWS
            or patterns not in ([], ['ep'])):
        _fail('selected_recall_settings_unsupported')
    vector_off: list[str] = []
    provider = _provider(proof, vector_off) if use_semantic else None
    vector = None
    if provider is not None and not vector_off:
        started, chunks = time.monotonic(), {}
        try:
            # Asks the already warm worker over its socket (``query_vectors``); it never starts one.
            vector = stack.enter_context(reader_dispatch.approved_query_vector(
                authority=endpoint, snapshot=None, decision=decision, provider=provider,
                prompt=prompt, deadline=deadline, info=chunks))
        finally:
            timings.update(queryEmbeddingMs=_ms(started), **chunks)
        if vector is None:
            vector_off.append('provider_unavailable')
    stages = {}
    for attempt in range(2):
        read = {}
        try:
            result = reader_dispatch.query_endpoint(
                endpoint, checkout=project, prompt=prompt, query_vector=vector, deadline=deadline,
                snapshot=None, decision=decision, provider=provider, authority=endpoint,
                linked=linked, timings=read, stages=stages)
            break
        except Exception as exc:
            reason = getattr(exc, 'reason', None)
            if reason == 'checkout_changed' and not attempt and time.monotonic() < deadline:
                continue
            if reason not in ('view_preparing', 'checkout_changed'):
                raise
            # The prompt recall asks the sync owner to prepare this view; the dry run does not.
            return _empty(reason, notes=['laya_view_preparing'] if reason == 'view_preparing' else (),
                          timings=_timings(timings, read))
        finally:
            timings = _timings(timings, read)
    status = result.semantic_status
    fallback = None
    if status == 'embedding_set_invalid':
        fallback = 'selected_embedding_set_invalid'
    elif not use_semantic:
        fallback = SEMANTIC_DISABLED
    elif provider is None:
        fallback = 'selected_' + vector_off[0]
    elif vector_off and linked is not None:
        fallback = 'selected_reader_endpoint_' + vector_off[0]
    elif vector_off:
        fallback = _provider_state_code(route_proof)
    elif vector is not None and status == 'lexical_only':
        fallback = 'selected_view_vectors_pending'
    if fallback is not None and fallback != SEMANTIC_DISABLED:
        notes.append('laya_vector_' + fallback)
    if timings.get('vectorKernelUnavailable'):
        notes.append('laya_vector_kernel_unavailable')
    lexical, semantic, fused = _stages(stages, result)
    fields = dict(generation=result.data_generation, lexical_lists=lexical, semantic_list=semantic,
                  fused_list=fused, semantic_status=status,
                  vector_covered_members=timings.get('vectorCoveredMembers'),
                  vector_eligible_members=timings.get('vectorEligibleMembers'),
                  fallback_reason=fallback)
    rows = list(result.rows)
    if not rows:
        reason = ('no_candidates_after_exclusion' if timings.get('sessionExcludedHits')
                  else 'no_candidates')
        return _empty(reason, notes=notes, timings=timings,
                      selector_result={'ran': False, 'reason': 'no_candidates', 'final': [],
                                       'decisions': []}, **fields)
    started = time.monotonic()
    try:
        selector = _selector_stage(decision, settings, prompt, rows, top_n=top_n,
                                   deadline=deadline, use_selector=use_selector)
    finally:
        timings['selectorMs'] = _ms(started)
    notes.extend(selector.pop('notes'))
    timings.update(selector.pop('measures'))
    reason = (selector['reason'] if selector['reason'] in ('selected', 'candidate_invalid', 'no_candidates')
              else 'ranked')
    if not selector['final']:
        return _empty('selected_zero' if reason == 'selected' else reason, notes=notes,
                      timings=timings, selector_result=selector, **fields)
    record, _raw, current = reader.recheck_reader(endpoint)
    if not _proof_still_current(current, proof):
        _fail('selected_proof_changed')
    with reader.pin_schema3(record, deadline=deadline, linked=linked, pin_view=False) as (
            _store, _handle, head):
        if head['gen_id'] != result.data_generation:
            _fail('selected_proof_changed')
    return dict(outcome='served', reason=reason, notes=notes, timings=timings,
                selector_result=selector, **fields)


def _timings(timings, read):
    """Stage times and counts of the read; the candidate path maps stay in the stage lists."""
    for key in ('semanticSources', 'semanticOnly', 'semanticCosine'):
        read.pop(key, None)
    merged = {**timings, **read}
    if 'vectorSource' in merged:
        merged['arenaHit'] = merged['vectorSource'] == 'arena'
    return merged


def _stages(stages, result):
    """The lexical, semantic and fused lists of one read; rows carry path and hash, never a body."""
    from memvane.recall.domain.rules import rrf_ranked
    queries, lanes = stages.get('queries') or [], stages.get('lexical') or []
    lexical = [{'query': queries[index] if len(queries) == len(lanes) else None,
                'rows': [{'path': row['path'], 'hash': row['hash'], 'score': row['score'],
                          'rank': rank} for rank, row in enumerate(lane, 1)]}
               for index, lane in enumerate(lanes)]
    nearest = stages.get('semantic')
    semantic = None if nearest is None else [
        {'path': row['path'], 'hash': row['hash'], 'cosine': round(row['score'], 6), 'rank': rank}
        for rank, row in enumerate(nearest, 1)]
    lists = list(lanes) + ([] if nearest is None else [list(nearest)])
    in_lexical = {row['path'] for lane in lanes for row in lane}
    in_semantic = {row['path'] for row in nearest or ()}
    fused = [{'path': row['path'], 'hash': row['hash'], 'rrfScore': round(score, 6), 'rank': rank,
              'lexical': row['path'] in in_lexical, 'semantic': row['path'] in in_semantic}
             for rank, (row, score) in enumerate(rrf_ranked(lists)[:len(result.rows)], 1)
             ] if any(lists) else []
    if [row['path'] for row in fused] != [row['path'] for row in result.rows]:
        _fail('dry_run_fusion_mismatch')
    return lexical, semantic, fused


def _reject_service_start(**_kwargs):
    """``ensure_service`` of the dry run's selector call: never starts the inference service."""
    raise PermissionError('dry_run_service_start_refused')


def _selector_cold(settings, project, deadline):
    """Why the selector cannot answer without a start, or None when it is warm and admitted.

    Prepared: an approved shared profile or a local checkpoint
    (``selector_prepared``). Warm: the per-user inference service already
    answers ``ready`` on its socket (``inference_version``); its state
    directory is only read, never created.
    """
    from memvane.learning.api import inference_version, selector_prepared
    from memvane.runtime.api import service_root
    try:
        if not selector_prepared(settings, project):
            return SELECTOR_NOT_PREPARED
        root = service_root()
        if not Path(root).is_dir():
            return SELECTOR_NOT_WARM
        answer = inference_version(root, deadline)
    except Exception:
        return SELECTOR_NOT_WARM
    if not isinstance(answer, dict) or answer.get('status') != 'ready':
        return SELECTOR_NOT_WARM
    return None


def _selector_stage(decision, settings, prompt, rows, *, top_n, deadline, use_selector):
    """``_select_verified_cards`` of the dry run: the final cards and each pool card's decision."""
    from memvane.learning.api import published_candidate_rows, select_published
    from memvane.recall.domain.rules import limit_selected, selector_action, verified_pool
    from memvane.recall.verified_rows import (
        PROMPT_PAIR_BUDGET, SELECTOR_CANDIDATES, _prompt_selector_seconds, _selector_measurement,
        _selector_prompt)
    from memvane.route.api import wiki_index_roots
    names = tuple(sorted(wiki_index_roots(decision['collections'])))
    if not names:
        _fail('selected_wiki_required')
    pool = verified_pool(rows)
    trace = _Collected()
    asked = pool[:SELECTOR_CANDIDATES]
    reason = SELECTOR_DISABLED
    result = None
    if use_selector:
        reason = _selector_version(decision) or _selector_cold(settings, decision['projectRoot'], deadline)
        if reason is None:
            selector_deadline = min(time.monotonic() + _prompt_selector_seconds(settings), deadline)
            try:
                result = select_published(
                    asked, prompt=_selector_prompt(prompt), config=settings,
                    project_root=decision['projectRoot'], wiki_names=names,
                    scope_id=decision['scopeId'], ensure_service=_reject_service_start,
                    deadline=selector_deadline, pair_budget=PROMPT_PAIR_BUDGET, lane='prompt',
                    skip_busy=True)
            except Exception:
                result = {'reason': 'selector_unavailable', 'final_results': []}
            _selector_measurement(trace, result.get('resources'))
            reason = result['reason']
    action = selector_action(reason) if result is not None else 'fallback'
    if action == 'accept':
        chosen = result['final_results']
        trace.note('dry_run_selector_answered')
    elif action == 'invalid':
        chosen = []
        trace.note('laya_selector_candidate_invalid')
    else:
        if reason != SELECTOR_DISABLED:
            trace.note('laya_selector_' + reason)
        try:
            published_candidate_rows(pool, names, decision['scopeId'])
            chosen = pool
        except ValueError:
            reason, chosen = 'candidate_invalid', []
            trace.note('laya_selector_candidate_invalid')
    final, _limit = limit_selected(chosen, top_n)
    served = {row['path'] for row in final}
    picked = {row['path'] for row in chosen}
    asked_paths = {row['path'] for row in asked}

    def decided(row):
        if row['path'] in served:
            return 'selected' if action == 'accept' else 'ranked_top'
        if action == 'accept':
            return 'not_selected' if row['path'] in asked_paths else 'outside_selector_candidates'
        if row['path'] in picked:
            return 'below_answer_limit'
        return reason
    notes = [note for note in trace.notes if note != 'dry_run_selector_answered']
    return {'ran': result is not None, 'reason': reason,
            'checkpoint': trace.values.get('selectorCheckpoint'),
            'final': [{'path': row['path'], 'hash': row['hash']} for row in final],
            'decisions': [{'path': row['path'], 'decision': decided(row)} for row in pool],
            'notes': notes, 'measures': trace.values}


def _selector_version(decision):
    """``older_plugin_version`` (or ``selector_unavailable``) as the selected recall decides it; else None."""
    try:
        from memvane.journal.api import inspect_journal
        from memvane.kernel.memvane_version import plugin_version, version_key
        selected = inspect_journal(decision['journalPath'])
        running = version_key(plugin_version())
        highest = version_key(selected['maxPluginVersion'])
        if (selected['state'] != 'selected' or selected['installId'] != decision['installId']
                or running is None or highest is None):
            return 'selector_unavailable'
        return 'older_plugin_version' if running < highest else None
    except Exception:
        return 'selector_unavailable'

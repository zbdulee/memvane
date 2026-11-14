"""Selected view recall: the journal-preferred reader endpoint and its schema3 view.

Recall decides here and returns a plain ``RecallAnswer``; the caller renders
it. Trace fields, notes and final reasons go to the caller's ``RecallTrace``.
"""
from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path
import re
import sys
import time

from memvane.recall.types import HostResponseError, MAX_ROWS, RecallAnswer


def _fail(code: str):
    raise HostResponseError(code)


def serve_schema3_recall(*, endpoint, proof, decision, project, prompt, deadline, main_proof,
                         trace, linked=None):
    """Recall from a journal-selected schema3 view; no v6 store or source file is opened.

    Settings, admission, provider and vector-fallback rules are the v6
    recall's. The query vector, when the sealed view has vectors and a warm
    provider answers, is bound to this endpoint by ``approved_query_vector``.

    ``linked`` reads the linked worktree's own view. A view with no
    published head yet (a first-seen worktree or branch) answers with no
    context and ``view_preparing``; a checkout that keeps changing under the
    read answers with no context and ``checkout_changed``. Neither falls
    back to another view.
    """
    from memvane.settings import api as config
    from memvane.journal import api as reader
    from memvane.recall import api as dispatch
    from memvane.kernel.errors import IndexContractError
    from memvane.provider.api import reviewed_provider
    from memvane.recall.recall_scope import (
        _proof_still_current, _recall_epoch, _recall_open, _recall_sync_wake, _wake_provider)
    from memvane.recall.verified_rows import _serve_verified_rows
    degraded = main_proof is None
    _recall_epoch(proof)
    trace.fields(store_format='schema3', reader_endpoint=endpoint.endpoint_id)
    if degraded:
        trace.fields(degraded='worktree' if linked is not None else 'reader_endpoint')
    own_view = linked is not None and _recall_open()
    settings = config.load_project_config(decision['projectRoot'])
    selected_settings = decision['recallSettings']
    if any(settings.get(key) != value for key, value in selected_settings.items()):
        _fail('selected_recall_settings_changed')
    top_n = selected_settings.get('topN')
    patterns = selected_settings.get('lexicalPatterns')
    if (type(top_n) is not int or not 1 <= top_n <= MAX_ROWS
            or patterns not in ([], ['ep'])):
        _fail('selected_recall_settings_unsupported')
    manifest = proof.get('providerManifest')
    if not isinstance(manifest, dict):
        _fail('vector_provider_missing')
    vector_off: list[str] = []
    if proof.get('providerAvailable') is False:
        provider = None
        vector_off.append('provider_artifact_changed')
    else:
        try:
            provider = reviewed_provider(manifest)
        except IndexContractError as exc:
            provider = None
            vector_off.append('provider_manifest_' + (str(exc) if re.fullmatch(
                r'[a-z0-9_]{1,60}', str(exc)) else 'invalid'))
    from memvane.resident.api import admit_query
    try:
        admit_query()
    except Exception:
        vector_off.append('resource_deferred')
    # A view sealed without vectors (a lexical-first publish whose fill is
    # still deferred) serves its lexical lists; the provider is not asked.
    unsealed = endpoint.record.get('vector_profile') is None
    with ExitStack() as stack:
        vector = None
        if provider is not None and not vector_off and not unsealed:
            vector = stack.enter_context(dispatch.approved_query_vector(
                authority=endpoint, snapshot=None, decision=decision, provider=provider,
                prompt=prompt, deadline=deadline))
            if vector is None and not vector_off:
                vector_off.append('provider_unavailable')
        for attempt in range(2):
            try:
                result = dispatch.query_endpoint(
                    endpoint, checkout=project, prompt=prompt, query_vector=vector,
                    deadline=deadline, snapshot=None, decision=decision, provider=provider,
                    authority=endpoint, linked=linked)
                break
            except Exception as exc:
                # Design 5.1/5.2: a cold view, or a checkout that moved twice
                # under the read, answers with no context and no other view.
                # (The view store's error carries ``reason``; this module
                # reaches the candidate store only through the reader adapter.)
                reason = getattr(exc, 'reason', None)
                if reason == 'checkout_changed' and not attempt and time.monotonic() < deadline:
                    continue
                if reason not in ('view_preparing', 'checkout_changed'):
                    raise
                if reason == 'view_preparing':
                    # The sync pass that prepares this view is asked for; it
                    # publishes the view's first generation only when complete.
                    if main_proof is not None:
                        try:
                            _recall_sync_wake(decision['projectRoot'])
                        except Exception:
                            pass
                    trace.note('laya_view_preparing')
                trace.final(reason)
                print(f'[memvane] {reason}; no context from this checkout\'s view yet',
                      file=sys.stderr)
                return RecallAnswer(decision, own_view=own_view)
    trace.fields(semantic=result.semantic_status)
    vector_fallback = None
    if provider is None:
        vector_fallback = 'selected_' + vector_off[0]
    elif vector_off and degraded:
        vector_fallback = 'selected_reader_endpoint_' + vector_off[0]
    elif vector_off:
        code = _wake_provider(decision, main_proof)
        vector_fallback = ('selected_provider_resource_deferred'
                           if vector_off[0] == 'resource_deferred' else code)
    elif unsealed or (vector is not None and result.semantic_status == 'lexical_only'):
        # The second case: a linked worktree view sealed lexical-first.
        vector_fallback = 'selected_view_vectors_pending'
    if vector_fallback is not None:
        trace.note('laya_vector_' + vector_fallback)
        print(f'[memvane] {vector_fallback}; serving lexical results', file=sys.stderr)

    def still_current(chosen):
        # Same install proof (epoch may only advance) and the same view head:
        # the served rows stay bound to the generation they were read from.
        record, _raw, current = reader.recheck_reader(endpoint)
        if not _proof_still_current(current, proof):
            return False
        with reader.pin_schema3(record, deadline=deadline, linked=linked,
                                 pin_view=False) as (_store, _handle, head):
            return head['gen_id'] == result.data_generation
    return _serve_verified_rows(
        decision, settings, prompt, list(result.rows), top_n=top_n,
        title_only=result.title_only, locations=dict(result.source_locations),
        generation=result.data_generation, still_current=still_current,
        vector_fallback=vector_fallback, allow_mixed_generation=False, trace=trace,
        own_view=own_view)


_RESETTLE_PENDING = 'reader_settings_resealing'

def _settings_resettle_pending(decision, project, root) -> bool:
    try:
        from memvane.journal import api as reader
        return (decision.get('state') == 'candidate' and
                reader.settings_resettle_pending(Path(decision['journalPath']).parent, project, root))
    except Exception:
        return False

def serve_endpoint_recall(host: str, payload: dict, *, cwd: str, trace, route_decision=None,
                          main_proof=None, linked=None) -> RecallAnswer:
    """Serve the journal-preferred reader endpoint (``_serve_endpoint_recall``).

    A writer killed mid-commit leaves a hot rollback journal. Route
    classification, which runs before the recall, rolls it back when it reads
    the store identity. The recall itself writes nothing: a hot journal it
    still meets refuses as ``reader_store_recovery_pending``, and the main
    checkout's recall then offers its sync wake, whose owner pass recovers it.
    """
    from memvane.recall.recall_scope import _recall_sync_wake
    try:
        return _serve_endpoint_recall(host, payload, cwd=cwd, trace=trace,
                                      route_decision=route_decision, main_proof=main_proof,
                                      linked=linked)
    except Exception as exc:
        if str(exc) == 'reader_store_recovery_pending' and main_proof is not None:
            try:
                _recall_sync_wake(route_decision['projectRoot'])
            except Exception:
                pass  # The refusal stands; a later wake still recovers it.
        raise

def _serve_endpoint_recall(host: str, payload: dict, *, cwd: str, trace, route_decision=None,
                           main_proof=None, linked=None) -> RecallAnswer:
    """Serve the journal-preferred reader endpoint.

    ``main_proof`` is the selected route proof. ``linked`` reads only the
    linked worktree view.
    """
    if main_proof is not None and route_decision is None:
        _fail('reader_selection_unproved')
    if host not in ('claude','codex','hermes') or not isinstance(payload,dict):
        _fail('invalid_hook_payload')
    requested = payload.get('cwd',cwd)
    prompt = payload.get('prompt')
    if (not isinstance(requested,str) or not Path(requested).is_absolute() or
            not isinstance(prompt,str) or not prompt.strip()):
        _fail('invalid_query_prompt')
    requested = Path(requested).resolve(strict=True)
    if not isinstance(route_decision, dict) or route_decision.get('state') != 'candidate':
        _fail('reader_selection_unproved')
    project = Path(route_decision['projectRoot'])
    if (project.resolve(strict=True) != project or
            (requested != project and project not in requested.parents)):
        _fail('reader_selection_unproved')
    from memvane.kernel import hook_budget
    from memvane.journal import api as lease
    from memvane.journal import api as reader
    from memvane.recall.recall_scope import _recall_sync_wake
    deadline = time.monotonic() + hook_budget.remaining()
    root = Path(__file__).resolve().parents[2]
    with ExitStack() as stack:
        for attempt in range(2):
            try:
                endpoint = stack.enter_context(lease.resolve_endpoint(project,action='recall',host_root=root))
                break
            except ValueError as exc:
                from memvane.recall.domain.rules import reader_reseal_candidate
                if (reader_reseal_candidate(str(exc), route_decision is not None) and
                        _settings_resettle_pending(route_decision, project, root)):
                    # Only the project's recall settings moved (skipPaths and
                    # the like): the sync owner re-seals the schema3 reader.
                    # Until then this recall answers without context, never
                    # from the stale record and never as an error.
                    if main_proof is not None:
                        try:
                            _recall_sync_wake(route_decision['projectRoot'])
                        except Exception:
                            pass
                    trace.note('laya_' + _RESETTLE_PENDING)
                    trace.final(_RESETTLE_PENDING)
                    print(f'[memvane] {_RESETTLE_PENDING}; no context until the index applies '
                          'the new recall settings', file=sys.stderr)
                    return RecallAnswer(route_decision)
                if (attempt or str(exc) != 'reader_selection_changed'
                        or time.monotonic() >= deadline):
                    raise
        _record,_att,proof = reader.recheck_reader(endpoint)
        decision = proof.get('readerDecision')
        if not isinstance(decision, dict) or decision != route_decision:
            _fail('reader_selection_unproved')
        return serve_schema3_recall(endpoint=endpoint, proof=proof, decision=decision,
                                    project=project, prompt=prompt, deadline=deadline,
                                    main_proof=main_proof, trace=trace, linked=linked)

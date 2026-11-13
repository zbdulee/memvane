"""Selected view recall execution with host callbacks supplied by the caller."""
from __future__ import annotations

from contextlib import ExitStack
import re
import sys
import time

from memvane.recall.types import MAX_ROWS

def serve_schema3_recall(host, payload, *, endpoint, proof, decision, project, prompt,
                         deadline, main_proof, linked=None, _recall_epoch, _trace_fields,
                         _RECALL_STATE, _fail, _recall_sync_wake, _write_selection_log,
                         _trace_final, host_payload, _wake_provider, _proof_still_current,
                         _serve_verified_rows):
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
    from memvane.settings import config
    from memvane.journal import api as reader
    from memvane.recall import api as dispatch
    from memvane.kernel.errors import IndexContractError
    from memvane.provider.api import reviewed_provider_class
    ReviewedLocalQmdProvider = reviewed_provider_class()
    degraded = main_proof is None
    _recall_epoch(proof)
    _trace_fields(store_format='schema3', reader_endpoint=endpoint.endpoint_id)
    if degraded:
        _trace_fields(degraded='worktree' if linked is not None else 'reader_endpoint')
    if linked is not None:
        state = _RECALL_STATE.get()
        if state is not None:
            state['ownView'] = True
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
            provider = ReviewedLocalQmdProvider(manifest)
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
                    _write_selection_log(decision['projectRoot'], 'laya_view_preparing', [])
                _trace_final(reason)
                print(f'[memvane] {reason}; no context from this checkout\'s view yet',
                      file=sys.stderr)
                return host_payload(host, '')
    _trace_fields(semantic=result.semantic_status)
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
        _write_selection_log(decision['projectRoot'], 'laya_vector_' + vector_fallback, [])
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
        host, decision, payload, settings, prompt, list(result.rows), top_n=top_n,
        title_only=result.title_only, locations=dict(result.source_locations),
        generation=result.data_generation, still_current=still_current,
        vector_fallback=vector_fallback, allow_mixed_generation=False)

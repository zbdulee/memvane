"""Off-hook, journal-bound full-scan sync for a selected Laya Index route.

Runtime wakes contain only a project path. Every attempt rereads the selected
route, installer journal, provider attestation and DB/queue incarnation. A
failed or deferred attempt leaves the wake for the runtime dispatcher to retry.
"""
from __future__ import annotations

from pathlib import Path
import sqlite3
import time

from context_learning.local_cycle import ResourceDeferred
from context_learning.resource_coordination import Coordinator
from context_learning.shared import resource_report
from laya_index_a import IndexContractError
from laya_index_host_route import select_route
from laya_index_runtime import LayaIndexRuntime
from laya_index_sync import prepare_candidate_batch_off_hook, sync_full
from laya_install_journal import (cas_mark_new_write, verify_selected_route,
                                  verify_worker_attestation)
from laya_local_embed_provider import ReviewedLocalQmdProvider
from laya_local_embed_service import start_owned_embed_service

# A conservative cold-start proxy, not a measured GGUF/native peak. The
# installed-candidate resource QA must measure and review this envelope.
EMBED_POLICY = {'maxRssMib': 2048, 'minAvailableMib': 768,
                'maxLoadPerCpu': 1, 'targetHeadroomFraction': .35}


def _higher_priority_wake(controller):
    """Yield between documents so older QMD index/update work can dispatch."""
    from runtime_work import WakeQueue
    try:
        rows = WakeQueue(controller.root).pending()
        return any(row['kind'] in ('index', 'update') and row['due'] <= time.time()
                   for row in rows)
    except (OSError, ValueError, sqlite3.Error):
        return True


def _selected(project):
    project = Path(project)
    if not project.is_absolute() or project.is_symlink() or not project.is_dir():
        raise IndexContractError('selected_project_unavailable')
    decision = select_route(str(project), action='update')
    if decision.get('state') != 'candidate' or decision.get('projectRoot') != str(project):
        raise IndexContractError('selected_route_unavailable')
    proof = verify_selected_route(decision)
    if proof.get('productRouteAllowed') is not True or not isinstance(proof.get('providerManifest'), dict):
        raise IndexContractError('selected_provider_unavailable')
    return decision, proof


def run(project, *, coordinator=None, report=None, start_service=start_owned_embed_service):
    """Run one selected sync; call only from the durable runtime wake worker."""
    decision, proof = _selected(project)
    provider = ReviewedLocalQmdProvider(proof['providerManifest'])
    profile = proof['vectorProfile']
    if (provider.profile['profile_id'] != profile['profileId'] or
            provider.profile['model_id'] != profile['modelId'] or
            provider.profile['embedding_fingerprint'] != profile['embeddingFingerprint'] or
            provider.profile['dimension'] != profile['dimension']):
        raise IndexContractError('selected_provider_profile_mismatch')
    controller = coordinator or Coordinator()
    observed = report if report is not None else resource_report(EMBED_POLICY, detailed=True)
    try:
        with controller.activity('provider', EMBED_POLICY, observed):
            # The hook's sealed stat receipt is a fast freshness fence. This
            # off-hook worker must rehash the full native closure before load.
            validated_manifest = verify_worker_attestation(decision)
            if validated_manifest != proof['providerManifest'] or verify_selected_route(decision) != proof:
                raise IndexContractError('selected_provider_proof_changed')
            def admit():
                # The service verifies its exact model/runtime files first.
                # Re-sample afterward, immediately before its model load.
                fresh = resource_report(EMBED_POLICY, detailed=True)
                return controller.check(EMBED_POLICY, fresh)['ready']

            service = start_service(validated_manifest, admit=admit)
            try:
                if verify_selected_route(decision) != proof:
                    raise IndexContractError('selected_proof_changed')
                class AdmittedProvider:
                    manifest = provider.manifest
                    profile = provider.profile

                    def embed_document_chunks(self, body, path):
                        if _higher_priority_wake(controller):
                            raise ResourceDeferred('laya_sync_yield_priority')
                        return provider.embed_document_chunks(body, path)

                with LayaIndexRuntime(source_root=decision['sourceRoot'],
                                      scope=decision['scopeId'], db_path=decision['dbPath'],
                                      queue_path=decision['queuePath'],
                                      db_incarnation=decision['dbIncarnation']) as runtime:
                    batch = prepare_candidate_batch_off_hook(runtime, AdmittedProvider())
                    if verify_selected_route(decision) != proof:
                        raise IndexContractError('selected_proof_changed')
                    if _higher_priority_wake(controller):
                        raise ResourceDeferred('laya_sync_yield_priority')

                    def before_publish():
                        nonlocal proof
                        if verify_selected_route(decision) != proof:
                            raise IndexContractError('selected_proof_changed')
                        marked = cas_mark_new_write(decision['journalPath'],
                                                    proof['selectionEpoch'])
                        if (marked.get('epoch') != proof['selectionEpoch'] + 1 or
                                marked.get('newWriteSeen') is not True):
                            raise IndexContractError('new_write_intent_unconfirmed')
                        fresh = verify_selected_route(decision)
                        if (fresh.get('selectionEpoch') != marked['epoch'] or
                                fresh.get('productRouteAllowed') is not True):
                            raise IndexContractError('new_write_proof_unconfirmed')
                        proof = fresh

                    result = sync_full(runtime, before_publish=before_publish,
                                       vector_batch=batch, require_vector=True)
                if verify_selected_route(decision) != proof:
                    raise IndexContractError('selected_proof_changed')
            finally:
                service.stop()
    except ResourceDeferred as exc:
        return {'state': 'deferred', 'reason': str(exc)}
    if result.get('state') not in ('indexed', 'unchanged', 'deferred'):
        raise IndexContractError('invalid_sync_result')
    return result

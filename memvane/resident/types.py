"""Resident service failure classifications."""
from __future__ import annotations

TRANSIENT_FAILURES = frozenset(('startup_failed', 'worker_exited'))
EMBED_POLICY = {'maxRssMib': 2048, 'minAvailableMib': 768,
                'maxLoadPerCpu': 1, 'targetHeadroomFraction': .35}

MOVED_PROOF = frozenset((
    'selected_route_changed', 'selected_proof_changed', 'worker_selection_changed',
    'selection_transition_pending', 'host_decision_changed', 'journal_changed',
    'worker_attestation_changed', 'production_attestation_changed',
))
INVALID_ATTESTATION = frozenset((
    'provider_artifact_hash_mismatch', 'provider_model_not_gguf',
    'provider_attestation_invalid', 'provider_closure_unapproved', 'provider_recipe_mismatch',
    'provider_artifact_path_invalid', 'provider_recipe_invalid', 'provider_manifest_invalid', 'invalid_vector_profile',
    'worker_attestation_invalid', 'provider_identity_changed', 'worker_provider_unapproved',
    'unsafe_production_attestation', 'production_attestation_invalid', 'production_attestation_noncanonical',
    'production_attestation_journal_mismatch',
))

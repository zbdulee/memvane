"""Public journal selection and worker proof operations."""
from __future__ import annotations


def service_worker_attestation(*args, **kwargs):
    from memvane.journal.application.service_proof import service_worker_attestation as implementation
    return implementation(*args, **kwargs)

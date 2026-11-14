"""Public resident service operations."""
from __future__ import annotations


def ensure_running(project, manifest, *, service_root=None, spawn=None):
    from memvane.resident.embed_resident import ensure_running as implementation
    return implementation(project, manifest, service_root=service_root, spawn=spawn)


def admit_query(*, coordinator=None, report=None):
    from memvane.resident.embed_resident import admit_query as implementation
    return implementation(coordinator=coordinator, report=report)


def read_owner_state(manifest):
    from memvane.resident.embed_resident import read_owner_state as implementation
    return implementation(manifest)


def failure_decision(manifest, *, own_lock=False):
    from memvane.resident.embed_resident import _failure_decision as implementation
    return implementation(manifest, own_lock=own_lock)


def probe(manifest):
    from memvane.resident.embed_resident import probe as implementation
    return implementation(manifest)


def attached_service():
    from memvane.resident.embed_resident import AttachedService as implementation
    return implementation()


def ensure_service(*, root=None, python=None, learning_root=None, spawn=None):
    from memvane.resident.resident_service import ensure_service as implementation
    return implementation(root=root, python=python, learning_root=learning_root, spawn=spawn)

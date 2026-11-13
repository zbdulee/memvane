"""Public resident service operations."""
from __future__ import annotations


def ensure_running(*args, **kwargs):
    from memvane.resident.embed_resident import ensure_running as implementation
    return implementation(*args, **kwargs)


def admit_query(*args, **kwargs):
    from memvane.resident.embed_resident import admit_query as implementation
    return implementation(*args, **kwargs)


def read_owner_state(*args, **kwargs):
    from memvane.resident.embed_resident import read_owner_state as implementation
    return implementation(*args, **kwargs)


def failure_decision(*args, **kwargs):
    from memvane.resident.embed_resident import _failure_decision as implementation
    return implementation(*args, **kwargs)


def probe(*args, **kwargs):
    from memvane.resident.embed_resident import probe as implementation
    return implementation(*args, **kwargs)


def attached_service(*args, **kwargs):
    from memvane.resident.embed_resident import AttachedService as implementation
    return implementation(*args, **kwargs)


def ensure_service(*args, **kwargs):
    from memvane.resident.resident_service import ensure_service as implementation
    return implementation(*args, **kwargs)

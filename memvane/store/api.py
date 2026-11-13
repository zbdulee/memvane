"""Public store value operations."""
from __future__ import annotations

from memvane.store.types import BlobStoreError, MAX_BODY, MAX_CHUNKS, SHA, UUID


def attestation(*args, **kwargs):
    from memvane.store.blob.blob_values import attestation as implementation
    return implementation(*args, **kwargs)


def compile_result_digest(*args, **kwargs):
    from memvane.store.blob.blob_values import compile_result_digest as implementation
    return implementation(*args, **kwargs)


def digest(*args, **kwargs):
    from memvane.store.blob.blob_values import digest as implementation
    return implementation(*args, **kwargs)


def fail(*args, **kwargs):
    from memvane.store.blob.blob_values import fail as implementation
    return implementation(*args, **kwargs)


def fingerprint(*args, **kwargs):
    from memvane.store.blob.blob_values import fingerprint as implementation
    return implementation(*args, **kwargs)


def hash_bytes(*args, **kwargs):
    from memvane.store.blob.blob_values import hash_bytes as implementation
    return implementation(*args, **kwargs)


def markdown_title(*args, **kwargs):
    from memvane.store.blob.blob_values import markdown_title as implementation
    return implementation(*args, **kwargs)


def profile(*args, **kwargs):
    from memvane.store.blob.blob_values import profile as implementation
    return implementation(*args, **kwargs)


def provenance_digests(*args, **kwargs):
    from memvane.store.blob.blob_values import provenance_digests as implementation
    return implementation(*args, **kwargs)


def relative(*args, **kwargs):
    from memvane.store.blob.blob_values import relative as implementation
    return implementation(*args, **kwargs)

"""Public store value operations."""
from __future__ import annotations

from memvane.store.types import BlobStoreError, CheckoutUnavailable, MAX_BODY, MAX_CHUNKS, SHA, UUID


def capture_matches(*args, **kwargs):
    from memvane.store.checkout.checkout_view import capture_matches as implementation
    return implementation(*args, **kwargs)


def capture_view(*args, **kwargs):
    from memvane.store.checkout.checkout_view import capture_view as implementation
    return implementation(*args, **kwargs)


def register_repository(*args, **kwargs):
    from memvane.store.checkout.checkout_view import register_repository as implementation
    return implementation(*args, **kwargs)


def register_worktree(*args, **kwargs):
    from memvane.store.checkout.checkout_view import register_worktree as implementation
    return implementation(*args, **kwargs)


def provider_spec(*args, **kwargs):
    from memvane.store.view.view_vectors import provider_spec as implementation
    return implementation(*args, **kwargs)


def qmd_first_pass(*args, **kwargs):
    from memvane.store.view.view_vectors import qmd_first_pass as implementation
    return implementation(*args, **kwargs)


def query_values(*args, **kwargs):
    from memvane.store.view.view_vectors import query_values as implementation
    return implementation(*args, **kwargs)


def register_provider_spec(*args, **kwargs):
    from memvane.store.view.view_vectors import register_provider_spec as implementation
    return implementation(*args, **kwargs)


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

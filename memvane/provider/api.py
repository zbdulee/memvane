"""Public provider operations."""
from __future__ import annotations

from memvane.provider.types import FILES_SHA, LLAMA_VERSION, RuntimeUnavailable


def collect(*args, **kwargs):
    from memvane.provider.embed_runtime import collect as implementation
    return implementation(*args, **kwargs)


def provision_new(*args, **kwargs):
    from memvane.provider.embed_runtime import provision_new as implementation
    return implementation(*args, **kwargs)


def sealed_files_sha256(*args, **kwargs):
    from memvane.provider.embed_runtime import sealed_files_sha256 as implementation
    return implementation(*args, **kwargs)


def select(*args, **kwargs):
    from memvane.provider.embed_runtime import select as implementation
    return implementation(*args, **kwargs)


def select_generation(*args, **kwargs):
    from memvane.provider.embed_runtime import select_generation as implementation
    return implementation(*args, **kwargs)


def verify_attested_files(*args, **kwargs):
    from memvane.provider.embed_runtime import verify_attested_files as implementation
    return implementation(*args, **kwargs)


def hash_file(*args, **kwargs):
    from memvane.provider.adapters.local_embed_provider import _hash_file as implementation
    return implementation(*args, **kwargs)


def recipe_fingerprint(*args, **kwargs):
    from memvane.provider.adapters.local_embed_provider import recipe_fingerprint as implementation
    return implementation(*args, **kwargs)


def private_parent(*args, **kwargs):
    from memvane.provider.adapters.local_embed_service import _private_parent as implementation
    return implementation(*args, **kwargs)


def remove_stale_socket(*args, **kwargs):
    from memvane.provider.adapters.local_embed_service import _remove_stale_socket as implementation
    return implementation(*args, **kwargs)


def bind_private(*args, **kwargs):
    from memvane.provider.adapters.local_embed_service import bind_private as implementation
    return implementation(*args, **kwargs)


def start_owned_embed_service(*args, **kwargs):
    from memvane.provider.adapters.local_embed_service import start_owned_embed_service as implementation
    return implementation(*args, **kwargs)


def profile_identity(*args, **kwargs):
    from memvane.provider.domain.profile import profile_identity as implementation
    return implementation(*args, **kwargs)


def reviewed_provider_class():
    from memvane.provider.adapters.local_embed_provider import ReviewedLocalQmdProvider
    return ReviewedLocalQmdProvider


def default_embed_model_uri():
    from memvane.provider.adapters.local_embed_provider import DEFAULT_EMBED_MODEL_URI
    return DEFAULT_EMBED_MODEL_URI

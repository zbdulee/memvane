"""Public provider operations."""
from __future__ import annotations
from memvane.provider.types import FILES_SHA, LLAMA_VERSION, RuntimeUnavailable


def closure_layout(closure):
    from memvane.provider.adapters.closure import closure_layout as implementation
    return implementation(closure)


def collect(home=None, *, approve=False):
    from memvane.provider.embed_runtime import collect as implementation
    return implementation(home, approve=approve)


def provision_new(home=None, *, approve_download=False, model_file=None, node=None, path_env=None):
    from memvane.provider.embed_runtime import provision_new as implementation
    return implementation(home, approve_download=approve_download, model_file=model_file, node=node,
                          path_env=path_env)


def sealed_files_sha256(path, *, port=None):
    fixture = fixture_port(port)
    if fixture is not None:
        return fixture.sealed_files_sha256(path)
    from memvane.provider.embed_runtime import sealed_files_sha256 as implementation
    return implementation(path)


def select(home=None):
    from memvane.provider.embed_runtime import select as implementation
    return implementation(home)


def select_generation(generation, home=None):
    from memvane.provider.embed_runtime import select_generation as implementation
    return implementation(generation, home)


def verify_attested_files(manifest, expected=None):
    from memvane.provider.embed_runtime import verify_attested_files as implementation
    return implementation(manifest, expected)


def hash_file(path):
    from memvane.provider.adapters.local_embed_provider import _hash_file as implementation
    return implementation(path)


def recipe_fingerprint(*, model_id, model_sha256, runtime_id, recipe_id, provider_sha256, worker_sha256,
                       node_sha256, module_entry_sha256, closure_sha256):
    from memvane.provider.adapters.local_embed_provider import recipe_fingerprint as implementation
    return implementation(model_id=model_id, model_sha256=model_sha256, runtime_id=runtime_id,
                          recipe_id=recipe_id, provider_sha256=provider_sha256,
                          worker_sha256=worker_sha256, node_sha256=node_sha256,
                          module_entry_sha256=module_entry_sha256, closure_sha256=closure_sha256)


def private_parent(path):
    from memvane.provider.adapters.local_embed_service import _private_parent as implementation
    return implementation(path)


def remove_stale_socket(path):
    from memvane.provider.adapters.local_embed_service import _remove_stale_socket as implementation
    return implementation(path)


def bind_private(server, path):
    from memvane.provider.adapters.local_embed_service import bind_private as implementation
    return implementation(server, path)


def start_owned_embed_service(manifest, *, admit, startup_timeout_s=15):
    from memvane.provider.adapters.local_embed_service import start_owned_embed_service as implementation
    return implementation(manifest, admit=admit, startup_timeout_s=startup_timeout_s)


def profile_identity(model_id, embedding_fingerprint, dimension):
    from memvane.provider.domain.profile import profile_identity as implementation
    return implementation(model_id, embedding_fingerprint, dimension)


def reviewed_provider(manifest, *, port=None):
    """A reviewed provider for ``manifest`` (a ``types.ReviewedProviderHandle``)."""
    fixture = fixture_port(port)
    if fixture is not None:
        return fixture.reviewed_provider(manifest)
    from memvane.provider.adapters.local_embed_provider import ReviewedLocalQmdProvider
    return ReviewedLocalQmdProvider(manifest)


def fixture_port(port=None):
    """``port``, else the test runtime root's fixture provider port, else None (``fixture_port``)."""
    from memvane.provider.fixture_port import resolve as implementation
    return implementation(port)


def is_reviewed_provider(value):
    """True when ``value`` is exactly a reviewed provider (no subclass, no stand-in).

    While the test runtime root holds a fixture spec (``fixture_port.active``)
    exactly its fixture handle also counts.
    """
    from memvane.provider.adapters.local_embed_provider import ReviewedLocalQmdProvider
    if type(value) is ReviewedLocalQmdProvider:
        return True
    from memvane.provider.fixture_port import active
    port = active()
    return port is not None and port.is_handle(value)


def query_embed_child_argv():
    """argv of the child embedding one query: ``{manifest, prompt, timeout, allowed}`` JSON on
    stdin, ``{"vector": [...]}`` or ``{"error": code}`` JSON on stdout."""
    from memvane.provider.query_embed_child import child_argv as implementation
    return implementation()


def default_embed_model_uri():
    from memvane.provider.adapters.local_embed_provider import DEFAULT_EMBED_MODEL_URI
    return DEFAULT_EMBED_MODEL_URI


def runtime_file_sha256(path):
    """The provider hasher's sha256 of one attested runtime file, cached by file identity
    in this process and, for a file in a Laya generation, in that runtime's digest cache."""
    from memvane.provider.adapters.local_embed_provider import _hash_file
    from memvane.provider.runtime_digests import cache_for, digest
    cache = cache_for(path)
    value = digest(path, _hash_file, cache, 'provider-sha256')
    cache.flush()
    return value

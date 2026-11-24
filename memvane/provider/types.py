"""Provider runtime values."""
from __future__ import annotations

from contextlib import contextmanager
import errno
from typing import Protocol

from memvane.kernel.errors import IndexContractError


class AttestationReadError(OSError):
    """This installation's evidence could not be read before launching a worker."""


@contextmanager
def attestation_reads():
    """Mark read errors at the proof boundary before the shared process starts."""
    try:
        yield
    except (OSError, ValueError) as exc:
        cause = exc if isinstance(exc, OSError) else exc.__cause__
        if isinstance(cause, OSError):
            if cause.errno == errno.ELOOP:
                raise IndexContractError('provider_attestation_invalid') from exc
            raise AttestationReadError(cause.errno, cause.strerror) from exc
        raise


class BundleEvidenceError(ValueError):
    def __init__(self, reason, detail=None):
        super().__init__(reason)
        self.detail = detail

LLAMA_VERSION = '3.18.1'

FILES_SHA = 'runtimeFilesSha256'
QUERY_IDENTITY_ERRORS = frozenset(('provider_identity_mismatch', 'provider_socket_changed'))

# The test runtime root's fixture provider port file (``fixture_port.resolve``).
FIXTURE_FILE = 'provider-fixture.json'

class RuntimeUnavailable(RuntimeError):
    pass


class ReviewedProviderHandle(Protocol):
    """Public members of a reviewed embedding provider (``api.reviewed_provider``).

    The provider holds a manifest verified against attested artifacts and asks
    the embedding service on its socket, so it crosses the domain boundary as a
    handle; it is also called directly (``provider(prompt, sealed_profile)``).
    Only these members may be used outside ``memvane.provider``; ``_`` members
    never. Signatures are the ``ReviewedLocalQmdProvider`` methods' own.
    """

    manifest: dict
    profile: dict

    def __call__(self, prompt, sealed_profile): ...

    def embed_document(self, body, title=None): ...

    def embed_document_chunks(self, body, path, *, timeout=120): ...

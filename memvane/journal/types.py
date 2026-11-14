"""Value types for installation journal proofs."""
from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Protocol


class JournalEvidenceError(ValueError):
    pass


class DispatchTargetError(ValueError):
    """A wake target that cannot be proved; ``code`` is the fixed dispatch reason."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)

MAX_RECORD_BYTES = 65536

V1_BUILD_SCHEMA = 'memvane-initial-build-v1'

V1_BUILD_FILE = 'build.json'

V1_BUILD_IDENTITY = frozenset(('projectRoot', 'stateRoot', 'installId', 'scopeId',
                               'storePath', 'storeNode', 'storeIncarnation', 'journalPath',
                               'routeSha256', 'attestationSha256'))


class CutoverBlocked(RuntimeError):
    pass


@dataclass(frozen=True, eq=False, init=False)
class VerifiedEndpoint:
    """An issued reader endpoint (``api.resolve_endpoint`` yields it).

    ``endpoint_id`` names the sealed endpoint record and ``record`` is that
    record, read-only. Only ``memvane.journal`` issues one.
    """
    endpoint_id: str
    record: MappingProxyType

    def __init__(self):
        raise ValueError('reader_endpoint_not_issued')


@dataclass(frozen=True, init=False)
class ReaderSelection:
    """The reader selection issued with an endpoint (``api.reader_selection``).

    ``epoch`` is the journal selection epoch and ``preferred_endpoint`` the
    selected endpoint id. A 1.0 selection has no ``previous_endpoint`` (None)
    and its ``transition_id`` is the install id.
    """
    epoch: int
    preferred_endpoint: str
    previous_endpoint: str | None
    transition_id: str

    def __init__(self):
        raise ValueError('reader_selection_not_issued')


class WriterHandle(Protocol):
    """Members other domains call on the writer ``api.build_writer_scope`` and
    ``api.selected_v1_writer_scope`` yield.

    The writer stands for a held sync owner, writer lifetime lock and journal
    admission, so it crosses the domain boundary as a handle. Only these
    members may be used outside ``memvane.journal``; signatures are
    ``WriterToken``'s own (the structure guard compares them).
    """

    def admission_proof(self): ...

    def store(self): ...

    def mark_new_write(self): ...

    def validate(self, *, deadline=None): ...


class WriterToken:
    """Operations of an issued writer; each validates the live admission first."""
    __slots__ = ()

    def __new__(cls):
        raise ValueError('normal_writer_not_issued')

    def __reduce__(self):
        raise TypeError('normal_writer_not_serializable')

    def admission_proof(self):
        """The owned admission proof after validating the live writer."""
        from memvane.journal import writer_scope
        return writer_scope.admission_proof(self)

    def store(self):
        """The bound format 4 store after validating the live writer."""
        from memvane.journal import writer_scope
        return writer_scope.writer_store(self)

    def mark_new_write(self):
        """Commit first-write intent and reprove the selected writer."""
        from memvane.journal import writer_scope
        return writer_scope.mark_new_write(self)

    def validate(self, *, deadline=None):
        """Validate current admission metadata, not a continuing content snapshot."""
        from memvane.journal import writer_scope
        return writer_scope.validate_normal_writer(self, deadline=deadline)


class BuildWriter(WriterToken):
    """A same-thread initial-build writer held under the existing owners."""
    __slots__ = ()


class SelectedWriter(WriterToken):
    __slots__ = ()

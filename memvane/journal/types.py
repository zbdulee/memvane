"""Value types for installation journal proofs."""
from __future__ import annotations


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


class _WriterToken:
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


class BuildWriter(_WriterToken):
    """A same-thread initial-build writer held under the existing owners."""
    __slots__ = ()


class SelectedWriter(_WriterToken):
    __slots__ = ()

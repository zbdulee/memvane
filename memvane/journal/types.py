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

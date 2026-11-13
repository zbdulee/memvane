"""Value types for installation journal proofs."""
from __future__ import annotations


class JournalEvidenceError(ValueError):
    pass

MAX_RECORD_BYTES = 65536

V1_BUILD_SCHEMA = 'memvane-initial-build-v1'

V1_BUILD_FILE = 'build.json'

V1_BUILD_IDENTITY = frozenset(('projectRoot', 'stateRoot', 'installId', 'scopeId',
                               'storePath', 'storeNode', 'storeIncarnation', 'journalPath',
                               'routeSha256', 'attestationSha256'))


class CutoverBlocked(RuntimeError):
    pass

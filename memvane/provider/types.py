"""Provider runtime values."""
from __future__ import annotations


class BundleEvidenceError(ValueError):
    def __init__(self, reason, detail=None):
        super().__init__(reason)
        self.detail = detail

LLAMA_VERSION = '3.18.1'

FILES_SHA = 'runtimeFilesSha256'

class RuntimeUnavailable(RuntimeError):
    pass

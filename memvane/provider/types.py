"""Provider runtime values."""
from __future__ import annotations

LLAMA_VERSION = '3.18.1'

FILES_SHA = 'runtimeFilesSha256'

class RuntimeUnavailable(RuntimeError):
    pass

"""Shared store values and failure types."""
from __future__ import annotations

import re

SHA = re.compile(r'[0-9a-f]{64}\Z')
UUID = re.compile(r'[0-9a-f]{32}\Z')
MAX_BODY = 4 * 1024 * 1024
MAX_CHUNKS = 1024


class BlobStoreError(ValueError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)

"""Shared store values and failure types."""
from __future__ import annotations

import re

SHA = re.compile(r'[0-9a-f]{64}\Z')
UUID = re.compile(r'[0-9a-f]{32}\Z')
MAX_BODY = 4 * 1024 * 1024
MAX_CHUNKS = 1024
WORK_EXCEEDED = 'vector_work_exceeded'
DEADLINE_EXCEEDED = 'vector_deadline_exceeded'
DEFERRED = {WORK_EXCEEDED: 'deferred_work_budget', DEADLINE_EXCEEDED: 'deferred_deadline',
            'fts_deadline_exceeded': 'deferred_deadline'}
FALLBACK_CHARS = (600, 90, 135)


class BlobStoreError(ValueError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


class CheckoutUnavailable(RuntimeError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)

MAX_MEMBERS = 20000
STAGE_ITEM_FIELDS = ('path', 'action', 'input_sha', 'blob_sha', 'trust_digest', 'metadata_json')

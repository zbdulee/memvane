"""Shared limits for the wiki workspace queues."""
from __future__ import annotations

from memvane.settings.types import CURRENT

COMPILE_DIR = CURRENT + "/compile"
SOURCE_QUEUE = "source-queue.jsonl"
VERIFY_QUEUE = "verify-queue.jsonl"
CARD_WRITE_LOCK = ".card-write.lock"
COMPILE_RUN_LOCK = ".compile-run.lock"

MAX_REQUEUE_COUNT = 3

LIFECYCLE_SECONDS = 300
STALE_SECONDS = 2 * LIFECYCLE_SECONDS
REQUEST_VERSION = 1
REQUEST_SUFFIX = "-wiki-lifecycle-request.json"
SERVED_SUFFIX = "-wiki-lifecycle-served.json"
TRIGGERS = frozenset(("session_start", "source_edit"))

DEFERRED_REASONS = frozenset(('owner_busy', 'writer_proof_deadline',
                              'reader_lifetime_busy', 'writer_proof_physical_busy'))

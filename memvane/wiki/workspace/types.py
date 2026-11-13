"""Shared limits for the wiki workspace queues."""
from __future__ import annotations

from memvane.settings.types import CURRENT

COMPILE_DIR = CURRENT + "/compile"
CANDIDATES = "candidates.jsonl"
SOURCE_QUEUE = "source-queue.jsonl"
TOMBSTONES = "tombstones.jsonl"
MANIFEST = "generated-manifest.jsonl"
MERGE_NEEDED = "merge-needed.jsonl"
VERIFY_QUEUE = "verify-queue.jsonl"
VERIFY_LOG = "verify-log.jsonl"
VERIFY_SKIPPED = "verify-skipped.jsonl"
VERIFY_DELETED = "verify-deleted.jsonl"
DEDUP_NEEDED = "dedup-needed.jsonl"
DEDUP_SKIPPED = "dedup-skipped.jsonl"
DEDUP_DELETED = "dedup-deleted.jsonl"
DISCARD_LEDGER = "discard-ledger.jsonl"
COMPILE_COOLDOWN = "cooldown"
LEGACY_VERIFY_COOLDOWN = "verify-cooldown"
VERIFY_ENGINE_COOLDOWN = "verify-engine-cooldown.json"
DEDUP_JUDGE_COOLDOWN = "dedup-judge-cooldown"
DEDUP_JUDGE_ENGINE_COOLDOWN = "dedup-judge-engine-cooldown.json"
CARD_WRITE_LOCK = ".card-write.lock"
COMPILE_RUN_LOCK = ".compile-run.lock"

MAX_REQUEUE_COUNT = 3
DEDUP_RESOLVE_IMPLICIT_TTL = 3600
CLAIMED_PID_BACKSTOP_SECS = 86400

LIFECYCLE_SECONDS = 300
STALE_SECONDS = 2 * LIFECYCLE_SECONDS
REQUEST_VERSION = 1
REQUEST_SUFFIX = "-wiki-lifecycle-request.json"
SERVED_SUFFIX = "-wiki-lifecycle-served.json"
TRIGGERS = frozenset(("session_start", "source_edit"))

DEFERRED_REASONS = frozenset(('owner_busy', 'writer_proof_deadline',
                              'reader_lifetime_busy', 'writer_proof_physical_busy'))

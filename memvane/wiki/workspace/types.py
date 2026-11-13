"""Shared limits for the wiki workspace queues."""

MAX_REQUEUE_COUNT = 3
DEDUP_RESOLVE_IMPLICIT_TTL = 3600
CLAIMED_PID_BACKSTOP_SECS = 86400

LIFECYCLE_SECONDS = 300
STALE_SECONDS = 2 * LIFECYCLE_SECONDS
REQUEST_VERSION = 1
REQUEST_SUFFIX = "-wiki-lifecycle-request.json"
SERVED_SUFFIX = "-wiki-lifecycle-served.json"
TRIGGERS = frozenset(("session_start", "source_edit"))

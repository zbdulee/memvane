"""Shared intake bounds and discovery errors."""
from __future__ import annotations

MAX_DEPTH = 32
RAW_SCAN_MAX_FILES = 4096
RAW_SCAN_MAX_BYTES = 64_000_000
QUEUE_SCAN_MAX_BYTES = 8 * 1024 * 1024
SESSION_START_TRIGGER = "session_start_source"
SESSION_START_ACCEPTED_TRIGGERS = (SESSION_START_TRIGGER,)


class DiscoveryBlocked(Exception):
    """Fail-closed reason code; never a path or source byte."""


class GrantUncertain(DiscoveryBlocked):
    """A failed write left an id that may still be active."""

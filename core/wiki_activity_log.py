"""Wiki card activity, recorded as ``wiki`` events in the install event log.

Card writes and deletions by the compiler, the verifier, dedup resolution and
pending-replacement publication append one ``wiki`` event (outcome = action,
path, title, reason) to ``<install state root>/events.jsonl`` through
``event_log``. A project without a verified selected route logs nothing.
The compile worker reads the recent ``wiki`` events back as the extractor
payload's ``wiki.logTail`` (``tail``); no other event kind reaches it.
"""
from __future__ import annotations

import event_log

TAIL_CHARS = event_log.WIKI_TAIL_CHARS
_UNRESOLVED = event_log._UNRESOLVED


def append(root, action: str, rel: str, title: str, bound=_UNRESOLVED, *,
           reason: str | None = None) -> bool:
    """Record one card write/delete. Returns whether the line was written."""
    return event_log.wiki(root, action, rel, title, reason=reason, bound=bound)


def tail(root, bound=_UNRESOLVED, limit: int = TAIL_CHARS) -> str:
    """Recent ``wiki`` events rendered as activity lines, or ""."""
    return event_log.wiki_tail(root, bound, limit)

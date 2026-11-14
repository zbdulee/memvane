"""Wiki card activity, recorded as ``wiki`` events in the install event log.

Card writes and deletions by the compiler, the verifier, dedup resolution and
pending-replacement publication append one ``wiki`` event (outcome = action,
path, title, reason) to ``<install state root>/events.jsonl`` through
``event_log``. A project without a verified selected route logs nothing.
The compile worker reads the recent ``wiki`` events back as the extractor
payload's ``wiki.logTail`` (``tail``); no other event kind reaches it.

``REASON_CODES`` lists, per diagnostic stage, the fixed reason codes a wiki
diagnostic event may carry. A stage owner adds its own entry; every code
matches ``event_log.code`` so the log keeps it verbatim.
"""
from __future__ import annotations

from pathlib import Path

from memvane.diagnostics import api as event_log
from memvane.diagnostics.types import MEASURE_REASONS

TAIL_CHARS = event_log.WIKI_TAIL_CHARS
from memvane.wiki.workspace.types import UNRESOLVED_BINDING as _UNRESOLVED

REASON_CODES = {
    'card_measure': MEASURE_REASONS,
}


def _binding(root, bound):
    if bound is not _UNRESOLVED:
        return bound
    try:
        from memvane.wiki.workspace import wiki_selected_laya
        return wiki_selected_laya.selected(Path(root).resolve())
    except Exception:  # noqa: BLE001
        return None


def append(root, action: str, rel: str, title: str, bound=_UNRESOLVED, *,
           reason: str | None = None) -> bool:
    """Record one card write/delete. Returns whether the line was written."""
    try:
        supplied = bound is not _UNRESOLVED
        bound = _binding(root, bound)
        if bound is None:
            return False
        state_root = event_log.state_root_of(bound[0].get('journalPath'))
        # Only a caller-supplied binding names the install on the event line.
        return event_log.wiki(state_root, action, rel, title, reason=reason,
                              install=event_log.install_of(bound[0]) if supplied else None)
    except Exception:  # noqa: BLE001
        return False


def tail(root, bound=_UNRESOLVED, limit: int = TAIL_CHARS) -> str:
    """Recent ``wiki`` events rendered as activity lines, or empty text."""
    try:
        bound = _binding(root, bound)
        return event_log.wiki_tail(event_log.state_root_of(bound[0].get('journalPath')), limit) if bound else ''
    except Exception:  # noqa: BLE001
        return ''

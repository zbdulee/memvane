"""Which unresolved source refresh events one compile attempt has already settled.

A ``pending_refresh`` event is resolved only when a card proves the edited
revision. A compile attempt can also end for good without such a card: the
writer keeps a hand-edited card (merge-needed), the verifier suppressed that
exact content, the source is gone, the extractor returned no candidate. The
event then stays unresolved, which is right for recall freshness, and the next
worker pass would enqueue and pay for the same attempt again, forever.

This module records that the attempt for one event and one source content is
over. Only the compile side reads it: recovery and the idle check skip a
settled event. The pending ledger itself is never written here and recall
keeps reading the event as unresolved.

The record is the event's ``settled`` state in ``wiki-pipeline.sqlite3``. It counts only while
its event id is the source's latest unresolved one and the source content is
still the settled one, so a new edit (a new event) and a change of bytes
without an event are both attempted again. An attempt that ended on an outage
(``EXPIRING_REASONS``) is settled for a day only. A missing or unreadable ledger means
"nothing settled": the cost is one more attempt, never a skipped one.
"""
from __future__ import annotations

from pathlib import Path
import re
import time

from memvane.wiki.pipeline import api as pipeline

_SHA = re.compile(r'[0-9a-f]{64}\Z')
_REASON_BYTES = 80
# Attempts that ended on an outage, not on the content: every CLI of a job with
# no known engine failed (not installed, could not run, timed out). Such a
# settlement holds for a day; then the event is attempted once more.
EXPIRING_REASONS = frozenset(('unknown_host_exhausted',))
EXPIRY_SECONDS = 24 * 3600


def _stored(root):
    """Settled events from the ledger; ``{}`` when it is absent or unreadable (one more attempt)."""
    root = Path(root).resolve()
    try:
        if not pipeline.pipeline_db_path(root).exists():
            return {}
        with pipeline.open_pipeline(root, readonly=True) as store:
            return store.settled_refreshes()
    except Exception:
        return {}


def _current_sha(root, source_path):
    """sha256 of the source as it is now, or None when it cannot be read as a source."""
    from memvane.wiki.card import api as wiki_freshness
    root = Path(root).resolve()
    try:
        target = (root / source_path).resolve()
        target.relative_to(root)
    except (OSError, ValueError):
        return None
    snapshot = wiki_freshness.snapshot_file(target)
    return snapshot.get('sha256') if isinstance(snapshot, dict) else None


def _expired(entry, now):
    """An outage settlement older than a day (or with no readable time) no longer holds."""
    if entry.get('reason') not in EXPIRING_REASONS:
        return False
    settled = entry.get('settledAt')
    if not isinstance(settled, (int, float)):
        return True
    return not 0 <= now - settled < EXPIRY_SECONDS


def _holds(root, pending, entry, now):
    return (isinstance(entry, dict) and entry.get('sourcePath') == pending.get('sourcePath')
            and not _expired(entry, now)
            and entry.get('sourceSha256') == _current_sha(root, pending['sourcePath']))


def unsettled(root, pendings):
    """The given unresolved events whose compile attempt is not settled for the current source content."""
    pendings = list(pendings)
    if not pendings:
        return []
    stored = _stored(root)
    if not stored:
        return pendings
    now = time.time()
    return [pending for pending in pendings
            if not _holds(root, pending, stored.get(pending.get('eventId')), now)]


def settle(root, pending, source_sha256, reason) -> bool:
    """Record that the attempt for ``pending`` and this source content is over.

    ``source_sha256`` is the content the attempt read, or None when the source
    could not be read. Only the source's newest unresolved event can be settled.
    """
    from memvane.wiki.card import api as wiki_freshness
    event = pending.get('eventId') if isinstance(pending, dict) else None
    source = pending.get('sourcePath') if isinstance(pending, dict) else None
    if (not isinstance(event, str) or not event or not isinstance(source, str) or not source
            or not (source_sha256 is None or (isinstance(source_sha256, str) and _SHA.fullmatch(source_sha256)))):
        return False
    open_events = {item.get('eventId'): item for item in wiki_freshness.unresolved_pending_refreshes(root)}
    if event not in open_events:
        return False
    text = str(reason or 'not_refreshed').encode('utf-8')[:_REASON_BYTES].decode('utf-8', 'ignore')
    try:
        with pipeline.open_pipeline(Path(root).resolve()) as store:
            return store.settle_event(open_events[event], source_sha256, text, time.time())
    except Exception:
        return False


def queued_events(public):
    """``(sourcePath, eventId)`` of every queued or claimed source job that carries its edit receipt."""
    from memvane.wiki.workspace import api as workspace
    result = set()
    try:
        rows = workspace.read_queue(public)
    except Exception:
        return result
    for _, row in rows:
        source, receipt = row.get('source'), row.get('pendingRefresh')
        if isinstance(source, dict) and isinstance(receipt, dict):
            name, event = source.get('path'), receipt.get('eventId')
            if isinstance(name, str) and isinstance(event, str):
                result.add((name, event))
    return result

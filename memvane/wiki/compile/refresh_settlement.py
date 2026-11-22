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

The record is one small JSON map beside the ledger. An entry counts only while
its event id is the source's latest unresolved one and the source content is
still the settled one, so a new edit (a new event) and a change of bytes
without an event are both attempted again. An attempt that ended on an outage
(``EXPIRING_REASONS``) is settled for a day only. A missing or unreadable file means
"nothing settled": the cost is one more attempt, never a skipped one.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
from pathlib import Path
import re

from memvane.kernel.atomic_rename import atomic_json
from memvane.settings.api import ledger

SETTLED_NAME = 'source-refresh-settled.json'
_SHA = re.compile(r'[0-9a-f]{64}\Z')
_MAX_BYTES = 4 * 1024 * 1024
_REASON_BYTES = 80
# Attempts that ended on an outage, not on the content: every CLI of a job with
# no known engine failed (not installed, could not run, timed out). Such a
# settlement holds for a day; then the event is attempted once more.
EXPIRING_REASONS = frozenset(('unknown_host_exhausted',))
EXPIRY_SECONDS = 24 * 3600


def _path(root):
    return ledger(Path(root).resolve(), SETTLED_NAME)


@contextmanager
def _locked(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_name(path.name + '.lock').open('a', encoding='utf-8') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        yield


def _read(path):
    """The stored map with only well-formed entries; ``{}`` when absent or unreadable."""
    try:
        if not path.is_file() or path.is_symlink() or path.stat().st_size > _MAX_BYTES:
            return {}
        value = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError, UnicodeError):
        return {}
    if not isinstance(value, dict):
        return {}
    return {event: entry for event, entry in value.items()
            if isinstance(event, str) and event and isinstance(entry, dict)
            and isinstance(entry.get('sourcePath'), str)
            and (entry.get('sourceSha256') is None
                 or (isinstance(entry.get('sourceSha256'), str) and _SHA.fullmatch(entry['sourceSha256'])))}


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
    try:
        settled = datetime.fromisoformat(str(entry.get('ts')).replace('Z', '+00:00'))
        age = (now - settled).total_seconds()
    except (ValueError, TypeError):
        return True
    return not 0 <= age < EXPIRY_SECONDS


def _holds(root, pending, entry, now):
    return (isinstance(entry, dict) and entry.get('sourcePath') == pending.get('sourcePath')
            and not _expired(entry, now)
            and entry.get('sourceSha256') == _current_sha(root, pending['sourcePath']))


def unsettled(root, pendings):
    """The given unresolved events whose compile attempt is not settled for the current source content."""
    pendings = list(pendings)
    if not pendings:
        return []
    path = _path(root)
    stored = _read(path) if path is not None else {}
    if not stored:
        return pendings
    now = datetime.now(timezone.utc)
    return [pending for pending in pendings
            if not _holds(root, pending, stored.get(pending.get('eventId')), now)]


def settle(root, pending, source_sha256, reason) -> bool:
    """Record that the attempt for ``pending`` and this source content is over.

    ``source_sha256`` is the content the attempt read, or None when the source
    could not be read. Entries of events that are no longer unresolved are
    dropped in the same write, so the file is bounded by the unresolved count.
    """
    from memvane.wiki.card import api as wiki_freshness
    event = pending.get('eventId') if isinstance(pending, dict) else None
    source = pending.get('sourcePath') if isinstance(pending, dict) else None
    if (not isinstance(event, str) or not event or not isinstance(source, str) or not source
            or not (source_sha256 is None or (isinstance(source_sha256, str) and _SHA.fullmatch(source_sha256)))):
        return False
    path = _path(root)
    if path is None:
        return False
    try:
        with _locked(path):
            open_events = {item.get('eventId') for item in wiki_freshness.unresolved_pending_refreshes(root)}
            if event not in open_events:
                return False
            stored = {key: value for key, value in _read(path).items() if key in open_events}
            stored[event] = {
                'sourcePath': source, 'sourceSha256': source_sha256,
                'reason': str(reason or 'not_refreshed').encode('utf-8')[:_REASON_BYTES].decode('utf-8', 'ignore'),
                'ts': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')}
            atomic_json(path, stored)
    except (OSError, ValueError):
        return False
    return True

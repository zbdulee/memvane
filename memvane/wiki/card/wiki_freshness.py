"""Current-source revision snapshots and fail-closed wiki freshness checks.

``sourceHash`` identifies a generated card.  It is deliberately not the source
content hash, so this module owns the separate source-revision comparison used
by the compile worker and recall guard.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from memvane.settings.api import compile_dir, ledger
from memvane.wiki.card.types import EDIT_INBOX, SOURCE_REFRESH_PENDING
from memvane.wiki.card import yaml_scalars

from memvane.wiki.card.types import FRESH, STALE, UNKNOWN
PENDING_REFRESH = "pending_refresh"
RESOLVED = "resolved"
PENDING_COMPACT_BYTES = 256 * 1024
PENDING_FIELDS = frozenset({"eventId", "ts", "sourcePath", "state", "engine"})
RESOLVED_FIELDS = frozenset({"pendingEventId", "ts", "state"})


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _pending_path(root: Path) -> Path | None:
    root = Path(root).resolve()
    public, folder = ledger(root, SOURCE_REFRESH_PENDING), compile_dir(root)
    return public if folder is not None and public == folder / SOURCE_REFRESH_PENDING else None


def _safe_source_path(value) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        return None
    normalized = path.as_posix()
    return normalized if normalized not in ("", ".") else None


def _pending_id(row: dict) -> str | None:
    if set(row) != PENDING_FIELDS:
        return None
    source = _safe_source_path(row.get("sourcePath"))
    event_id = row.get("eventId")
    ts = row.get("ts")
    engine = row.get("engine")
    if (
        source is None or not isinstance(event_id, str) or not event_id
        or not isinstance(ts, str) or not ts or not isinstance(engine, str) or not engine
    ):
        return None
    return event_id


def _resolved_id(row: dict) -> str | None:
    if set(row) != RESOLVED_FIELDS:
        return None
    event_id = row.get("pendingEventId")
    ts = row.get("ts")
    if not isinstance(event_id, str) or not event_id or not isinstance(ts, str) or not ts:
        return None
    return event_id


def _valid_event(row) -> bool:
    if not isinstance(row, dict):
        return False
    if row.get("state") == PENDING_REFRESH:
        return _pending_id(row) is not None
    if row.get("state") == RESOLVED:
        return _resolved_id(row) is not None
    return False


def _closed_json_object(pairs):
    """Reject duplicate JSON object keys instead of silently taking the last."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _valid_event_time(value) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


INBOX = EDIT_INBOX
INBOX_SCHEMA = "memvane-edit-inbox-v1"
INBOX_MAX_BYTES = 8 * 1024 * 1024
CONSUMING = ".consuming-"


def _inbox_path(root: Path) -> Path | None:
    public = _pending_path(root)
    return public.with_name(INBOX) if public is not None else None


def _inbox_lock(inbox: Path) -> Path:
    return inbox.with_name(inbox.name + ".lock")


def _inbox_event(raw: bytes) -> dict | None:
    try:
        row = json.loads(raw, object_pairs_hook=_closed_json_object)
    except (ValueError, RecursionError):
        return None
    if not isinstance(row, dict) or row.get("schema") != INBOX_SCHEMA or row.get("state") != PENDING_REFRESH:
        return None
    event = {key: row.get(key) for key in PENDING_FIELDS}
    if _pending_id(event) is None or not _valid_event_time(event["ts"]):
        return None
    return event


def _read_inbox_file(path: Path) -> tuple[list[dict], int]:
    """Events of one inbox file and its count of unusable lines; absent reads as empty."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return [], 0
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise OSError("unsafe edit inbox")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            data = stream.read(INBOX_MAX_BYTES + 1)
    finally:
        os.close(fd)
    events, malformed = [], 0
    for line in data.split(b"\n"):
        if not line.strip():
            continue
        event = _inbox_event(line)
        if event is None:
            malformed += 1
        else:
            events.append(event)
    return events, malformed


def _consuming(inbox: Path) -> list[Path]:
    return sorted(p for p in inbox.parent.glob(inbox.name + CONSUMING + "*") if p.is_file())


def _acquire_pending_lock(fd: int, timeout: float | None) -> None:
    if timeout is None:
        fcntl.flock(fd, fcntl.LOCK_EX)
        return
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise
            time.sleep(min(0.01, remaining))


def _lock_fd(inbox: Path) -> int:
    fd = os.open(_inbox_lock(inbox), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        os.close(fd)
        raise OSError("unsafe edit inbox lock")
    return fd


def record_pending_refresh(root: Path, source_path: str, engine: str, *,
                           compact: bool = True, lock_timeout: float | None = None,
                           error_out: list | None = None) -> dict | None:
    """Append one edit event to the hook inbox; the compile worker absorbs it into the ledger.

    Hooks never open ``wiki-pipeline.sqlite3``. ``compact`` is accepted for callers that
    predate the inbox and has no effect here.
    """
    from memvane.wiki.card.trust import incompatible_source
    if incompatible_source(source_path, report=True):
        if error_out is not None:
            error_out.append("failed")
        return None
    source = _safe_source_path(source_path)
    inbox = _inbox_path(root)
    if source is None or inbox is None:
        if error_out is not None:
            error_out.append("failed")
        return None
    event = {
        "eventId": uuid.uuid4().hex,
        "ts": _now_iso(),
        "sourcePath": source,
        "state": PENDING_REFRESH,
        "engine": engine if isinstance(engine, str) and engine.strip() else "unknown",
    }
    line = (json.dumps({**event, "schema": INBOX_SCHEMA}, ensure_ascii=False, sort_keys=True) + "\n").encode()
    try:
        inbox.parent.mkdir(parents=True, exist_ok=True)
        lock = _lock_fd(inbox)
        try:
            _acquire_pending_lock(lock, lock_timeout)
            fd = os.open(inbox, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                    raise OSError("unsafe edit inbox")
                if info.st_size + len(line) > INBOX_MAX_BYTES:
                    # Full until the worker absorbs it; the caller still wakes the worker.
                    raise BlockingIOError("edit inbox full")
                written = os.write(fd, line)
                if written != len(line):
                    os.ftruncate(fd, info.st_size)  # Never leave a torn line behind.
                    raise OSError("short edit inbox write")
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            os.close(lock)
    except BlockingIOError:
        if error_out is not None:
            error_out.append("busy")
        return None
    except (OSError, ValueError):
        if error_out is not None:
            error_out.append("failed")
        return None
    return event


def absorb_edit_inbox(root: Path) -> dict:
    """Move hook inbox events into ``refresh_events`` once; the caller holds ``.compile-run.lock``.

    The inbox is renamed aside under its lock, so later hook appends go to a fresh file.
    The aside files are inserted by ``eventId`` and their names recorded in one
    transaction, then deleted; a stop between the two re-inserts nothing on the next run.
    """
    inbox = _inbox_path(root)
    if inbox is None or not inbox.parent.is_dir():
        return {"absorbed": 0, "malformed": 0, "files": 0}
    lock = _lock_fd(inbox)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            size = os.lstat(inbox).st_size
        except FileNotFoundError:
            size = 0
        if size:
            aside = inbox.with_name(f"{inbox.name}{CONSUMING}{time.time_ns():020d}-{uuid.uuid4().hex[:8]}")
            os.rename(inbox, aside)
    finally:
        os.close(lock)
    files = _consuming(inbox)
    if not files:
        return {"absorbed": 0, "malformed": 0, "files": 0}
    events, malformed = [], 0
    for path in files:
        found, bad = _read_inbox_file(path)
        events.extend(found)
        malformed += bad
    from memvane.wiki.pipeline import api as pipeline
    with pipeline.open_pipeline(Path(root).resolve()) as store:
        added = store.absorb_refresh_events(events, [p.name for p in files])
        for path in files:
            path.unlink()
        store.prune_refreshes()
    return {"absorbed": added, "malformed": malformed, "files": len(files)}


def _events(root: Path) -> list[tuple[dict, bool]]:
    """``(event, resolved)`` oldest first: ledger rows and unabsorbed inbox files together."""
    from memvane.wiki.pipeline import api as pipeline
    root = Path(root).resolve()
    inbox = _inbox_path(root)
    if inbox is None:
        raise ValueError("unsafe compile directory")
    items, known = [], set()
    if pipeline.pipeline_db_path(root).exists():
        with pipeline.open_pipeline(root, readonly=True) as store:
            for row in store.refresh_rows():
                event = {"eventId": row["event_id"], "ts": row["ts"], "sourcePath": row["source_path"],
                         "state": PENDING_REFRESH, "engine": row["engine"]}
                items.append((event, row["state"] == "resolved"))
                known.add(row["event_id"])
    for path in [*_consuming(inbox), inbox]:
        for event in _read_inbox_file(path)[0]:
            if event["eventId"] not in known:
                known.add(event["eventId"])
                items.append((event, False))
    # Hook time decides which event is newest; arrival breaks a tie. A resolve can insert
    # a newer event into the ledger before an older one leaves the inbox.
    order = {id(item): index for index, item in enumerate(items)}
    return sorted(items, key=lambda item: (item[0]["ts"], order[id(item)]))


def _unresolved(root, strict, *, receipt=None):
    try:
        items = _events(root)
    except Exception:
        if strict:
            return None
        return []
    if receipt is not None and not any((event["eventId"], event["sourcePath"]) == receipt
                                       for event, _ in items):
        return []
    latest: dict[str, tuple[dict, bool]] = {}
    for event, resolved in items:
        latest[event["sourcePath"]] = (event, resolved)
    return [event for event, resolved in latest.values() if not resolved]


def unresolved_pending_refreshes(root: Path, *, compact: bool = False) -> list[dict]:
    """Each source's newest event when it is still unresolved. ``compact`` absorbs the inbox first
    and is only for the compile worker, which holds ``.compile-run.lock``."""
    if compact:
        try:
            absorb_edit_inbox(root)
        except Exception:
            pass  # Unabsorbed events are still read from the inbox below.
    return _unresolved(root, False) or []


def unresolved_pending_refreshes_strict(root: Path) -> list[dict] | None:
    return _unresolved(root, True)


def _ordered_latest_strict(
    rows: list[dict],
) -> dict[str, tuple[int, dict, tuple[int, dict] | None]] | None:
    """Fold only a causally valid ledger; reject ambiguous event histories."""
    seen: dict[str, tuple[int, dict]] = {}
    resolved_ids: set[str] = set()
    latest: dict[str, tuple[int, dict, tuple[int, dict] | None]] = {}
    for index, row in enumerate(rows):
        if row.get("state") == PENDING_REFRESH:
            event_id = _pending_id(row)
            if event_id is None or event_id in seen:
                return None
            seen[event_id] = (index, row)
            latest[row["sourcePath"]] = (index, row, None)
            continue
        event_id = _resolved_id(row)
        pending_item = seen.get(event_id) if event_id is not None else None
        if pending_item is None or event_id in resolved_ids or pending_item[0] >= index:
            return None
        source = pending_item[1]["sourcePath"]
        current = latest.get(source)
        if (
            current is None or current[2] is not None
            or current[1].get("eventId") != event_id
        ):
            return None
        resolved_ids.add(event_id)
        latest[source] = (current[0], current[1], (index, row))
    return latest


def latest_unresolved_pending(root: Path, source_path: str, *, event_id=None) -> dict | None:
    source = _safe_source_path(source_path)
    receipt = (event_id, source) if event_id is not None else None
    events = _unresolved(root, True, receipt=receipt) or []
    matches = [row for row in events if row['sourcePath'] == source]
    return matches[0] if len(matches) == 1 else None


def same_revision(left, right) -> bool:
    left_norm = yaml_scalars.normalize_source_revision(left)
    right_norm = yaml_scalars.normalize_source_revision(right)
    return left_norm is not None and left_norm == right_norm


def resolve_pending_refresh(
    root: Path, pending: dict, captured_revision: dict, compiled_revisions: list[dict],
    compiled_event_id: str | None = None,
) -> bool:
    """Resolve only the newest event of its source, and only for the bytes the writer captured."""
    pending_id = _pending_id(pending) if isinstance(pending, dict) else None
    captured = yaml_scalars.normalize_source_revision(captured_revision)
    compiled = [yaml_scalars.normalize_source_revision(item) for item in compiled_revisions]
    if compiled_event_id is None:
        compiled_event_id = pending_id
    if (
        pending_id is None or captured is None or _inbox_path(root) is None
        or compiled_event_id != pending_id
        or captured.get("path") != pending.get("sourcePath")
        or any(item is None for item in compiled)
        or captured not in compiled
    ):
        return False
    root = Path(root).resolve()
    source = (root / pending["sourcePath"]).resolve()
    try:
        source.relative_to(root)
    except ValueError:
        return False
    current_events = _unresolved(root, True)
    if current_events is None or pending not in current_events:
        return False
    current = snapshot_file(source)
    if current is None or current["sha256"] != captured["sha256"]:
        return False
    from memvane.wiki.pipeline import api as pipeline
    try:
        with pipeline.open_pipeline(root) as store:
            return store.resolve_pending_event(pending, time.time())
    except Exception:
        return False


def snapshot_bytes(path: Path) -> tuple[dict, bytes] | None:
    """Read one stable file image as ``(revision, bytes)`` or return ``None``.

    The two fstats are deliberately on the same opened descriptor.  Callers
    that need bounded source text must use these returned bytes, not re-open
    the path after receiving the revision.
    """
    try:
        with path.open("rb") as handle:
            before = os.fstat(handle.fileno())
            data = handle.read()
            after = os.fstat(handle.fileno())
    except OSError:
        return None
    if (before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_ino, after.st_size, after.st_mtime_ns,
    ):
        return None
    return {
        "sha256": hashlib.sha256(data).hexdigest(),
        "size": after.st_size,
        "mtimeNs": after.st_mtime_ns,
    }, data


def snapshot_file(path: Path) -> dict | None:
    """Return a stable source revision without retaining source bytes."""
    snapshot = snapshot_bytes(path)
    return snapshot[0] if snapshot is not None else None


def compare_revision(path: Path, expected: dict) -> tuple[str, str]:
    """Compare a previously captured source revision with a safe resolved path.

    Path containment is intentionally not duplicated here.  ``check_card``
    first asks ``wiki_card.resolve_existing_source`` for the project's shared
    allow-root decision, then passes its resolved result here.
    """
    if yaml_scalars.normalize_source_revision(expected) is None:
        return UNKNOWN, "invalid_source_revision"
    snapshot = snapshot_file(path)
    if snapshot is None:
        return UNKNOWN, "source_snapshot_failed"
    if snapshot["sha256"] != expected["sha256"]:
        return STALE, "content_hash_mismatch"
    return FRESH, ""


def _source_revisions(card: dict) -> tuple[list[dict] | None, str]:
    if not isinstance(card, dict):
        return None, "invalid_card"
    raw = card.get("sourceRevisions")
    if not isinstance(raw, list) or not raw:
        return None, "missing_source_revisions"
    revisions = []
    for entry in raw:
        if isinstance(entry, dict):
            revision = yaml_scalars.normalize_source_revision(entry)
        elif isinstance(entry, str):
            revision = yaml_scalars.parse_source_revision(entry)
        else:
            revision = None
        if revision is None:
            return None, "invalid_source_revision"
        revisions.append(revision)
    return revisions, ""


def check_card(card: dict, project_root: Path, allow_roots: list[Path]) -> dict:
    """Return the current freshness state for compiler-owned card provenance.

    ``unknown`` never authorizes recall: provenance problems, allow-root
    rejection, and read/stat/hash failures are all fail-closed.  A genuinely
    absent source is distinguishable and stale, so diagnostics can route it to
    the existing missing-source workflow.
    """
    revisions, problem = _source_revisions(card)
    if revisions is None:
        return {"state": UNKNOWN, "reason": problem}

    # Import lazily so recall can later import this module without a module-load
    # cycle.  This remains the one source containment decision for all users.
    from memvane.wiki.card import wiki_card

    for expected in revisions:
        resolved, reason = wiki_card.resolve_existing_source(
            expected["path"], project_root, allow_roots)
        if resolved is None:
            state = STALE if reason == "missing" else UNKNOWN
            return {"state": state, "reason": reason or "source_unavailable"}
        state, reason = compare_revision(resolved, expected)
        if state != FRESH:
            return {"state": state, "reason": reason}
    return {"state": FRESH, "checked": len(revisions)}

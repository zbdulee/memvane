#!/usr/bin/env python3
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
import uuid
from datetime import datetime, timezone
from pathlib import Path

import compile_paths as cp
import yaml_scalars

FRESH = "fresh"
STALE = "stale"
UNKNOWN = "unknown"
PENDING_REFRESH = "pending_refresh"
RESOLVED = "resolved"
PENDING_COMPACT_BYTES = 256 * 1024
PENDING_FIELDS = frozenset({"eventId", "ts", "sourcePath", "state", "engine"})
RESOLVED_FIELDS = frozenset({"pendingEventId", "ts", "state"})


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _pending_path(root: Path) -> Path | None:
    return cp.ledger(Path(root).resolve(), cp.SOURCE_REFRESH_PENDING)


def _pending_lock_path(path: Path) -> Path:
    return path.with_name(f"{path.name}.lock")


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


def _read_events_unlocked(path: Path, strict: bool = False) -> list[dict] | None:
    try:
        if not path.exists():
            return []
        text = path.read_text(encoding="utf-8", errors="strict")
    except (OSError, UnicodeError):
        return None if strict else []
    rows = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            if strict:
                row = json.loads(line, object_pairs_hook=_closed_json_object)
            else:
                row = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            if strict:
                return None
            continue
        if not _valid_event(row) or (strict and not _valid_event_time(row.get("ts"))):
            if strict:
                return None
            continue
        rows.append(row)
    return rows


def _append_event_unlocked(path: Path, event: dict) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        return False
    return True


def record_pending_refresh(root: Path, source_path: str, engine: str) -> dict | None:
    """Durably invalidate one source before its separate queue append."""
    source = _safe_source_path(source_path)
    path = _pending_path(root)
    if source is None or path is None:
        return None
    event = {
        "eventId": uuid.uuid4().hex,
        "ts": _now_iso(),
        "sourcePath": source,
        "state": PENDING_REFRESH,
        "engine": engine if isinstance(engine, str) and engine.strip() else "unknown",
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _pending_lock_path(path).open("a", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            if not _append_event_unlocked(path, event):
                return None
    except OSError:
        return None
    compact_pending_refreshes(root)
    return event


def read_pending_refreshes(root: Path) -> list[dict]:
    path = _pending_path(root)
    if path is None:
        return []
    rows = _read_events_unlocked(path)
    return rows if rows is not None else []


def unresolved_pending_refreshes(root: Path) -> list[dict]:
    """Return the latest unresolved pending event for each source."""
    latest = _ordered_latest(read_pending_refreshes(root))
    return [pending for _index, pending, resolved in latest.values() if resolved is None]


def unresolved_pending_refreshes_strict(root: Path) -> list[dict] | None:
    """Return unresolved events, or ``None`` when ledger state is unknowable.

    Absent and empty ledgers are known-empty. Unsafe ledger resolution,
    unreadable bytes, malformed JSON, duplicate keys, invalid closed-schema
    events, and invalid timestamps are unknown so a recall caller can fail
    closed without changing the permissive lifecycle recovery API.
    """
    try:
        path = _pending_path(root)
        if path is None:
            return None
        rows = _read_events_unlocked(path, strict=True)
    except (OSError, UnicodeError, RuntimeError):
        return None
    if rows is None:
        return None
    latest = _ordered_latest_strict(rows)
    if latest is None:
        return None
    return [pending for _index, pending, resolved in latest.values() if resolved is None]


def _ordered_latest(rows: list[dict]) -> dict[str, tuple[int, dict, tuple[int, dict] | None]]:
    """Fold ordered history; a resolve only applies to an earlier exact event id."""
    seen: dict[str, tuple[int, dict]] = {}
    latest: dict[str, tuple[int, dict, tuple[int, dict] | None]] = {}
    for index, row in enumerate(rows):
        if row.get("state") == PENDING_REFRESH:
            event_id = _pending_id(row)
            if event_id is None:
                continue
            seen[event_id] = (index, row)
            latest[row["sourcePath"]] = (index, row, None)
            continue
        event_id = _resolved_id(row)
        pending_item = seen.get(event_id) if event_id is not None else None
        if pending_item is None or pending_item[0] >= index:
            continue
        source = pending_item[1]["sourcePath"]
        current = latest.get(source)
        if current is not None and current[1].get("eventId") == event_id:
            latest[source] = (current[0], current[1], (index, row))
    return latest


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


def latest_unresolved_pending(root: Path, source_path: str) -> dict | None:
    source = _safe_source_path(source_path)
    if source is None:
        return None
    item = _ordered_latest(read_pending_refreshes(root)).get(source)
    if item is None or item[2] is not None:
        return None
    return dict(item[1])


def same_revision(left, right) -> bool:
    left_norm = yaml_scalars.normalize_source_revision(left)
    right_norm = yaml_scalars.normalize_source_revision(right)
    return left_norm is not None and left_norm == right_norm


def resolve_pending_refresh(
    root: Path, pending: dict, captured_revision: dict, compiled_revisions: list[dict],
    compiled_event_id: str | None = None,
) -> bool:
    """Resolve only the latest event with the captured bytes the writer returned."""
    pending_id = _pending_id(pending) if isinstance(pending, dict) else None
    captured = yaml_scalars.normalize_source_revision(captured_revision)
    compiled = [yaml_scalars.normalize_source_revision(item) for item in compiled_revisions]
    path = _pending_path(root)
    if compiled_event_id is None:
        compiled_event_id = pending_id
    if (
        pending_id is None or captured is None or path is None
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
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _pending_lock_path(path).open("a", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            rows = _read_events_unlocked(path, strict=True)
            if rows is None:
                return False
            latest = _ordered_latest(rows).get(pending["sourcePath"])
            if (
                latest is None or latest[2] is not None
                or latest[1].get("eventId") != pending_id
            ):
                return False
            current = snapshot_file(source)
            if current is None or current["sha256"] != captured["sha256"]:
                return False
            event = {
                "ts": _now_iso(),
                "state": RESOLVED,
                "pendingEventId": pending_id,
            }
            if not _append_event_unlocked(path, event):
                return False
    except OSError:
        return False
    compact_pending_refreshes(root)
    return True


def compact_pending_refreshes(root: Path, force: bool = False) -> bool:
    """Atomically retain each source's latest pending event and resolved pair."""
    path = _pending_path(root)
    if path is None or not path.exists():
        return False
    try:
        if not force and path.stat().st_size < PENDING_COMPACT_BYTES:
            return True
        with _pending_lock_path(path).open("a", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            rows = _read_events_unlocked(path, strict=True)
            if rows is None:
                return False
            kept: list[tuple[int, dict]] = []
            for pending_index, pending, resolved in _ordered_latest(rows).values():
                kept.append((pending_index, pending))
                if resolved is not None:
                    kept.append(resolved)
            kept.sort(key=lambda item: item[0])
            tmp = path.with_suffix(path.suffix + ".compact.tmp")
            with tmp.open("w", encoding="utf-8") as handle:
                for _index, row in kept:
                    handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
            try:
                dir_fd = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except OSError:
                pass
    except OSError:
        return False
    return True


CORRUPT_QUARANTINE_PREFIX = "source-refresh-pending.corrupt-"


def quarantine_corrupt_pending(root: Path) -> dict | None:
    """Rename an unparsable pending ledger aside so the lifecycle can restart.

    Corruption here is not an outside accident -- this module's own failure path
    produces it.  ``_append_event_unlocked`` issues one buffered ``write``; when
    that write is short and then fails (ENOSPC/EFBIG), the bytes already handed
    to the kernel stay durable while the caller is told the append failed.  A
    torn last line is the result (measured: 40 of 153 bytes, no newline).

    From there nothing recovers.  ``unresolved_pending_refreshes`` (permissive)
    still yields the earlier valid events, so ``recover_pending_refreshes``
    re-enqueues the source every worker run, while ``resolve_pending_refresh``
    reads strictly, refuses, and preserves the job -- one extractor call plus
    one verify call per run, forever (measured: 20 runs -> 20 + 20 calls).
    Recall is fail-closed at the same time: ``pending_refresh_cutoffs`` gets
    ``None`` and every card is reported ``unknown``.  ``compact_pending_refreshes``
    cannot clean up because it reads strictly too, and a fresh
    ``record_pending_refresh`` only appends after the torn line.

    Renaming (never deleting) keeps the bytes for post-mortem; this repository
    has repeatedly recorded deletions whose ledger held no body and so could not
    be explained afterwards.  The ledger is deliberately left ABSENT rather than
    recreated empty, so the next ``record_pending_refresh`` builds it normally.

    The cost is real and accepted: the pending marks captured at that moment are
    lost, so a card compiled from an already-edited source can look settled for
    one cycle.  That is a degradation, not a hole -- the authoritative freshness
    decision is the ``sourceRevisions`` SHA-256 comparison against the file on
    disk (``check_card``), which this never touches.

    Quarantining is idempotent: the renamed path no longer resolves as the
    ledger, so the next call sees an absent (= known-empty) ledger.
    """
    try:
        path = _pending_path(root)
        if path is None:
            return None
        with _pending_lock_path(path).open("a", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            if not path.exists():
                return None
            # The judgement is deliberately narrow, because a false positive
            # *is* the loss this function exists to bound.  Two conditions must
            # both hold: the bytes are readable, and the strict reader (reused,
            # never reimplemented) rejects their content.
            #
            # `_read_events_unlocked` folds "cannot read" and "read fine but
            # invalid" into the same `None`, so the read below splits them: an
            # OSError is a permission/transient filesystem problem and must
            # never cost a healthy ledger, while a UnicodeError is genuine byte
            # corruption.  Everything the strict reader accepts is left alone --
            # notably blank lines and valid pending/resolved pairs, which the
            # permissive/strict divergence survey showed are NOT corruption
            # (only torn lines, non-JSON, duplicate JSON keys, schema
            # violations, invalid/naive timestamps, duplicate event ids, and
            # causally impossible resolves are).
            try:
                path.read_text(encoding="utf-8", errors="strict")
            except UnicodeError:
                reason = "invalid_utf8"
            except OSError:
                return None
            else:
                if _read_events_unlocked(path, strict=True) is not None:
                    return None
                reason = "strict_reject"
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            root_path = Path(root).resolve()
            target = cp.ledger(root_path, "{}{}.jsonl".format(CORRUPT_QUARANTINE_PREFIX, stamp))
            if target is not None and target.exists():
                # Never overwrite an earlier quarantine; that would destroy the
                # very evidence this function preserves.
                target = cp.ledger(root_path, "{}{}-{}.jsonl".format(
                    CORRUPT_QUARANTINE_PREFIX, stamp, uuid.uuid4().hex[:8]))
            if target is None:
                return None
            os.replace(path, target)
    except OSError:
        return None
    return {"quarantinedTo": target.name, "reason": reason}


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
    import wiki_card

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


def _main() -> int:
    """Worker-side maintenance CLI.  Always exits 0 -- never kills the worker.

    `argparse` is imported here, not at module scope: recall imports this module
    on the blocking hook path and must not pay for a CLI it never runs.
    """
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--cwd", default=os.getcwd())
    parser.add_argument("--quarantine-corrupt-pending", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = None
    if args.quarantine_corrupt_pending:
        try:
            result = quarantine_corrupt_pending(Path(args.cwd))
        except Exception:
            result = None
    if args.json:
        print(json.dumps(result or {}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(_main())

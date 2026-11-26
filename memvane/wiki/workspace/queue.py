"""Queue claim and read: ledger queues (source, verify) and text queues (dedup pairs)."""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
import time
import uuid
from memvane.kernel.paths import now_iso

from memvane.kernel import cooldown as qmd_cooldown
from memvane.wiki.workspace import compile_paths as cp
from memvane.wiki.workspace.types import MAX_REQUEUE_COUNT, DEDUP_RESOLVE_IMPLICIT_TTL, CLAIMED_PID_BACKSTOP_SECS




def append_jsonl(path: Path, payload: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


def _is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    force_dead = os.environ.get("QMD_FORCE_DEAD_PIDS", "").split(",")
    if str(pid) in force_dead:
        return False
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except (OSError, ProcessLookupError):
        return False


def _write_discard_ledger(compile_dir: Path, queue_name: str, job: dict, requeue_count: int):
    # 이 원장은 claim_queue 경로에서 쓰여 config 컨텍스트가 없으므로 이름을 고정한다
    page_a = job.get("pageA")
    page_b = job.get("pageB")
    page_ab = f"{page_a} <-> {page_b}" if (page_a and page_b) else (page_a or page_b)
    target = job.get("targetPath") or job.get("path") or page_ab or "unknown"
    append_jsonl(compile_dir / cp.DISCARD_LEDGER, {
        "timestamp": now_iso(),
        "queue": queue_name,
        "targetPath": str(target)[:4096],
        "reason": "max_requeue_exceeded",
        "requeue_count": requeue_count,
    })


def reclaim_orphaned_claimed(path: Path, max_requeue: int = MAX_REQUEUE_COUNT):
    """Return dead claims of a text queue (dedup pairs) to the live file.

    Source and verify jobs live in the pipeline ledger, whose claim recovers
    them. MUST be called under _queue_lock_path(path) flock.
    """
    if not path.parent.exists():
        return
    prefix = f"{path.name}.claimed."
    current_pid = os.getpid()
    now = time.time()

    for child in list(path.parent.iterdir()):
        if not child.name.startswith(prefix):
            continue

        parts = child.name.split(".")
        file_pid = None
        if len(parts) >= 4:
            try:
                file_pid = int(parts[-2])
            except ValueError:
                pass

        if file_pid == current_pid:
            continue

        # A text queue keeps the historic PID-reuse mtime backstop.
        if file_pid and _is_pid_alive(file_pid):
            try:
                mtime = child.stat().st_mtime
                if not qmd_cooldown.window_elapsed(mtime, now, CLAIMED_PID_BACKSTOP_SECS):
                    continue
            except OSError:
                continue
        elif not file_pid:
            try:
                mtime = child.stat().st_mtime
                if not qmd_cooldown.window_elapsed(mtime, now, DEDUP_RESOLVE_IMPLICIT_TTL):
                    continue
            except OSError:
                continue

        try:
            rows = _text_rows(child.read_text(encoding='utf-8', errors='replace'))
        except OSError:
            continue

        remaining = []
        for line, data in rows:
            if data is None:
                remaining.append(line)
                continue
            try:
                count = int(data.get('_requeue_count', 0)) + 1
            except (TypeError, ValueError, OverflowError):
                count = 1
            if count <= max_requeue:
                remaining.append(json.dumps({**data, '_requeue_count': count}, ensure_ascii=False))
                continue
            _write_discard_ledger(path.parent, path.name, data, count)
        try:
            if remaining:
                with path.open('a', encoding='utf-8') as handle:
                    for line in remaining:
                        handle.write(line + '\n')
            child.unlink(missing_ok=True)
        except OSError:
            continue


def _text_rows(content):
    rows = []
    for line in content.splitlines():
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            parsed = None
        rows.append((line, parsed if isinstance(parsed, dict) else None))
    return rows


def read_queue(path: Path):
    from memvane.wiki.workspace import ledger_queue
    if isinstance(path, ledger_queue.LedgerClaim):
        return ledger_queue.LedgerRows(path)
    if path is not None and ledger_queue.kind(path) is not None:
        return ledger_queue.live(path)
    if path is None or not path.exists():
        return []
    try:
        return _text_rows(path.read_text(encoding='utf-8', errors='replace'))
    except OSError:
        return []


def claim_queue(path: Path, *, receipt_writer=None):
    from memvane.wiki.workspace import ledger_queue
    if ledger_queue.kind(path) is not None:
        return ledger_queue.claim(path, receipt_writer=receipt_writer)
    from memvane.wiki.workspace.queue_paths import queue_locked
    claimed = path.with_name(f"{path.name}.claimed.{os.getpid()}.{uuid.uuid4().hex}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with queue_locked(path):
        reclaim_orphaned_claimed(path)
        try:
            os.replace(path, claimed)
        except FileNotFoundError:
            return None
        try:
            os.utime(claimed, None)
        except OSError:
            pass
        path.touch(exist_ok=True)
    return claimed


def _parse_ts(value) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None

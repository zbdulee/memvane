"""Durable compile queue claim, read, and orphan recovery."""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
import time
import uuid
from memvane.kernel.paths import now_iso
from memvane.kernel.atomic_rename import fsync_dir
from memvane.wiki.workspace import queue_return

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


def _write_discard_ledger(compile_dir: Path, queue_name: str, job: dict, requeue_count: int, *, receipt=None):
    # 이 원장은 claim_queue 경로에서 쓰여 config 컨텍스트가 없으므로 이름을 고정한다
    ledger_path = compile_dir / cp.DISCARD_LEDGER
    source_obj = job.get("source")
    source_path = source_obj.get("path") if isinstance(source_obj, dict) else None
    card_obj = job.get("card")
    card_path = (card_obj.get("targetPath") or card_obj.get("path")) if isinstance(card_obj, dict) else None
    cand_obj = job.get("candidate")
    cand_path = (cand_obj.get("targetPath") or cand_obj.get("path")) if isinstance(cand_obj, dict) else None

    page_a = job.get("pageA")
    page_b = job.get("pageB")
    page_ab = f"{page_a} <-> {page_b}" if (page_a and page_b) else (page_a or page_b)

    target = (
        job.get("targetPath")
        or job.get("candidatePath")
        or job.get("path")
        or job.get("sourcePath")
        or source_path
        or card_path
        or cand_path
        or page_ab
        or "unknown"
    )
    payload = {
        "timestamp": now_iso(),
        "queue": queue_name,
        "targetPath": str(target)[:4096],
        "reason": "max_requeue_exceeded",
        "requeue_count": requeue_count,
    }
    if queue_name not in (cp.SOURCE_QUEUE, cp.VERIFY_QUEUE) and receipt is None:
        append_jsonl(ledger_path, payload)
        return
    if receipt is not None:
        payload['backfillReceipt'] = receipt
    # Only the atomic receipt can settle the cohort; audit durability is separate.
    with ledger_path.open('a', encoding='utf-8') as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + '\n')
        handle.flush()
        os.fsync(handle.fileno())
    fsync_dir(ledger_path.parent)


def reclaim_orphaned_claimed(path: Path, max_requeue: int = MAX_REQUEUE_COUNT, *, receipt_writer=None, private=False):
    """
    Return dead claims under the live queue lock. Source and verify rows at
    their retry limit stay private unless a frozen discard receipt settles them.
    MUST be called under _queue_lock_path(path) flock.
    """
    if not path.parent.exists():
        return
    native = path.name in (cp.SOURCE_QUEUE, cp.VERIFY_QUEUE) or receipt_writer is not None
    if native:
        queue_return.resume(path)
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

        # Only ancillary queues use the historic PID-reuse mtime backstop.
        if file_pid and _is_pid_alive(file_pid):
            # For source/verify, file age cannot disprove a live owner.
            if native:
                continue
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
            if native:
                if private:
                    private_folder = queue_return._dir(path, create=True)
                    moved = private_folder / ('native.claimed.%d.%s' % (os.getpid(), uuid.uuid4().hex))
                    queue_return._move_claim(child, moved)
                    child = moved
                rows = queue_return.ClaimRows(child)
            else:
                rows = _text_rows(child.read_text(encoding='utf-8', errors='replace'))
        except OSError:
            continue

        remaining, held = [], []
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
            receipt = None
            if path.name == cp.SOURCE_QUEUE or receipt_writer is not None:
                if receipt_writer is not None and data.get('partialCompile') is not True:
                    receipt = receipt_writer(data)
                if receipt is None:
                    held.append(line)
                    continue
            elif path.name == cp.VERIFY_QUEUE:
                held.append(line)
                continue
            _write_discard_ledger(path.parent, path.name, data, count, receipt=receipt)
        if native:
            queue_return.finish_locked(path, child, remaining, rows, held=queue_return.lines_bytes(held))
        else:
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
    if path is None or not path.exists():
        return []
    try:
        queue_name, separator, _owner = path.name.partition('.claimed.')
        if separator and queue_name not in (cp.MERGE_NEEDED, cp.DEDUP_NEEDED):
            return queue_return.ClaimRows(path)
        return _text_rows(path.read_text(encoding='utf-8', errors='replace'))
    except OSError:
        return []


def claim_queue(path: Path, *, receipt_writer=None, before_claim=None, private=False) -> Path | None:
    claimed = path.with_name(f"{path.name}.claimed.{os.getpid()}.{uuid.uuid4().hex}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with queue_return.legacy_card_lock(path), queue_return.queue_locked(path):
        if private:
            saved = queue_return.take_private(path, _is_pid_alive, include_live=False)
            if saved is None:
                from memvane.wiki.workspace.handoff.api import native_live
                live = native_live(path)
                if before_claim is not None and live is not None and live.exists() and live.stat().st_size:
                    before_claim(live)
                saved = queue_return.take_private(path, _is_pid_alive)
            if saved is not None:
                return saved
        if before_claim is not None:
            before_claim(path)
        live_existed = path.exists()
        reclaim_orphaned_claimed(path, receipt_writer=receipt_writer, private=private)
        native = path.name in (cp.SOURCE_QUEUE, cp.VERIFY_QUEUE) or receipt_writer is not None
        if native:
            saved = queue_return.take_private(path, _is_pid_alive)
            if saved is not None:
                return saved
            if not live_existed and path.exists() and path.stat().st_size == 0:
                return None
        try:
            if native:
                if private:
                    folder = queue_return._dir(path, create=True)
                    claimed = folder / ('native.claimed.%d.%s' % (os.getpid(), uuid.uuid4().hex))
                queue_return._move_claim(path, claimed, private=private)
            else:
                os.replace(path, claimed)
        except FileNotFoundError:
            return None
        if not native:
            try:
                os.utime(claimed, None)
            except OSError:
                pass
            path.touch(exist_ok=True)
            return claimed
        os.utime(claimed, None)
        queue_return.ensure_live(path)
    return claimed


def _parse_ts(value) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None

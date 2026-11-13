"""Durable compile queue claim, read, and orphan recovery."""
from __future__ import annotations

from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import time
import uuid

from memvane.kernel import cooldown as qmd_cooldown
from memvane.wiki.workspace import compile_paths as cp
from memvane.wiki.workspace.queue_paths import _queue_lock_path
from memvane.wiki.workspace.types import MAX_REQUEUE_COUNT, DEDUP_RESOLVE_IMPLICIT_TTL, CLAIMED_PID_BACKSTOP_SECS


def now_iso():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


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


def _write_discard_ledger(compile_dir: Path, queue_name: str, job: dict, requeue_count: int, *, receipt_writer=None):
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
    if queue_name == cp.SOURCE_QUEUE:
        if receipt_writer is None:
            raise ValueError('backfill_discard_authority_unavailable')
        receipt = receipt_writer(job)
        if receipt is not None:
            payload['backfillReceipt'] = receipt
    # Audit framing is not authoritative. A torn append cannot strand a cohort:
    # only the separate atomic private receipt may settle frozen source work.
    append_jsonl(ledger_path, payload)


def reclaim_orphaned_claimed(path: Path, max_requeue: int = MAX_REQUEUE_COUNT, *, receipt_writer=None):
    """
    고아 *.claimed.* 파일들을 감지하여 원본 큐(path)로 requeue 하거나,
    requeue 횟수가 max_requeue 초과시 폐기하고 원장에 남긴다.
    MUST be called under _queue_lock_path(path) flock.
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

        # 1차 판정: PID 검사. PID가 죽었으면 즉시 고아 회수 대상.
        # PID가 살아있더라도 PID 재사용(재부팅 후 root 데몬 등에 PID 겹침)에 대비해 2차 mtime backstop(≥24h)을 적용함.
        # 임계값 선택 근거: 짧게 잡으면 안 됨. 한 run은 extractor(batch.maxPerRun=10) + dedup judge + verify 호출로
        # 정상 동작 중 수십 분이 소요될 수 있으며, 짧은 임계는 살아있는 소유자의 배치를 탈취해 유료 CLI 이중 과금을 유발함.
        # 24h는 정상 run 최악 소요시간보다 압도적으로 큼.
        # 반면 file_pid가 없는 파일은 소유자가 없으므로 짧은 임계(DEDUP_RESOLVE_IMPLICIT_TTL=1h)를 적용함.
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
            raw_content = child.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        lines_to_requeue = []
        for line in raw_content.splitlines():
            if not line.strip():
                continue
            try:
                data = json.loads(line)
                if isinstance(data, dict):
                    try:
                        req_cnt = int(data.get("_requeue_count", 0)) + 1
                    except (TypeError, ValueError):
                        # 손상된 _requeue_count 값("3x" 등)은 절대 raise하지 않고 1로 수렴시켜
                        # 큐가 영구 wedge되는 것을 막고, 다음 회차에서 수렴하여 MAX_REQUEUE_COUNT 도달 시 정리를 보장함.
                        req_cnt = 1
                    if req_cnt > max_requeue:
                        _write_discard_ledger(path.parent, path.name, data, req_cnt, receipt_writer=receipt_writer)
                    else:
                        data["_requeue_count"] = req_cnt
                        lines_to_requeue.append(json.dumps(data, ensure_ascii=False))
                else:
                    lines_to_requeue.append(line)
            except json.JSONDecodeError:
                lines_to_requeue.append(line)

        # at-least-once append 후 unlink 성공을 전제로 MAX_REQUEUE_COUNT 상한이 유계됨.
        # I/O 실패(권한/ENOSPC/읽기전용) 시 claim_queue 밖으로 예외를 전파하지 않고 continue하여 다른 큐 claim을 방해하지 않음.
        if lines_to_requeue:
            try:
                with path.open("a", encoding="utf-8") as handle:
                    for rline in lines_to_requeue:
                        handle.write(rline if rline.endswith("\n") else rline + "\n")
            except OSError:
                continue

        try:
            child.unlink(missing_ok=True)
        except OSError:
            continue


def read_queue(path: Path):
    if not path.exists():
        return []
    rows = []
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []
    for line in content.splitlines():
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            rows.append((line, None))
            continue
        rows.append((line, parsed if isinstance(parsed, dict) else None))
    return rows


def claim_queue(path: Path, *, receipt_writer=None) -> Path | None:
    claimed = path.with_name(f"{path.name}.claimed.{os.getpid()}.{uuid.uuid4().hex}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(_queue_lock_path(path), "a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            reclaim_orphaned_claimed(path, receipt_writer=receipt_writer)
            if not path.exists():
                return None
            try:
                os.replace(path, claimed)
                # os.replace는 mtime을 유지하므로 오래된 backlog 큐 claim 시 claim 시각이 반영되지 않는 병리가 발생함.
                # os.utime으로 claimed 파일의 mtime을 현재 시각(claim 시각)으로 최신화한다.
                try:
                    os.utime(claimed, None)
                except OSError:
                    pass
                path.touch(exist_ok=True)
            except FileNotFoundError:
                return None
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    return claimed

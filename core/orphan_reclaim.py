#!/usr/bin/env python3
"""Orphan vector reclaim for the qmd index.

WHY (measured, roadmap step 8 -- docs/plans/2026-07-30-raw-index-crowding-measurement.md):
qmd 2.5.3's `removeCollection` (`dist/store.js:2265`) DELETEs `documents` and
`content` but never the vectors, so every collection removal leaves dead
embedding rows behind. On the live index that residue was 26,438 of 41,455 rows
(63.8%).

The damage is not disk. Dead vectors sit in the SAME candidate pool the daemon
draws its vec deep window (40) from, so they crowd out live documents: before the
step-8 cleanup a collection-filtered query could not surface a single document
from outside the global window, which is exactly why step 5 had to record vec as
`measurable: false`. Clearing 63.8% flipped both live projects to
`scoped_retrieval_proven`. Orphans are a RETRIEVAL problem.

And they keep accumulating -- not only from collection removals: re-embedding an
edited file orphans the old content hash's vectors. Measured 3 hours after the
step-8 cleanup: 1,112 orphans of 16,590 rows (6.7%) with no collection removed.

TRIGGERS (two, deliberately):
  1. post-remove -- update.sh writes a pending marker whenever a
     `qmd collection remove` actually succeeds. Orphans provably exist at that
     moment, so this run ignores the ratio threshold. Self-limiting: a removal is
     a one-shot event.
  2. opportunistic -- above-threshold residue with no removal (external
     `qmd collection remove`, edit churn). Threshold rationale in
     docs/settings.md; NOT every session, because `qmd cleanup` vacuums.

Runs from inside core/update.sh's background `qmd embed` subshell (strictly after
embed), never from a blocking hook: stdout stays silent unless --json is passed,
every failure is swallowed, and the exit code is always 0.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.resolve()))
import config as qmd_config
import cooldown as qmd_cooldown
import qmd_route
import sqlite_read

# `qmd cleanup` also clears the LLM cache and hard-deletes inactive document rows.
# Both are qmd's own maintenance semantics for this command; we do not hand-roll
# SQL for any of it, and in particular never touch the vec0 virtual table
# (`vectors_vec`) ourselves -- `cleanupOrphanedVectors` degrades gracefully when
# sqlite-vec is unavailable and we must inherit that.
CLEANUP_ARGV_TAIL = ("cleanup",)

# Orphan counting is READ-ONLY and on plain tables (`content_vectors` JOIN
# `documents`) -- the same pair qmd's own cleanupOrphanedVectors counts, and the
# independent cross-check that agreed with qmd's number in step 8. It is not a
# vec0 query.
ORPHAN_COUNT_SQL = """
SELECT COUNT(*) FROM content_vectors cv
WHERE NOT EXISTS (
  SELECT 1 FROM documents d WHERE d.hash = cv.hash AND d.active = 1
)
"""
TOTAL_COUNT_SQL = "SELECT COUNT(*) FROM content_vectors"


def cache_dir() -> Path:
    return Path(os.environ.get("QMD_CACHE_DIR", str(Path.home() / ".cache" / "qmd")))


def log_path() -> Path:
    return Path(os.environ.get(
        "QMD_ORPHAN_RECLAIM_LOG",
        str(cache_dir() / "orphan-reclaim.log"),
    ))


def log(message: str) -> None:
    try:
        path = log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"[{datetime.now(timezone.utc).isoformat()}] {message}\n")
    except OSError:
        pass


# --- markers -----------------------------------------------------------------
# All three markers are GLOBAL, not project-keyed: the qmd index is one file
# shared by every project, so "orphans exist" and "reclaim failed" are facts
# about the machine. update.sh writes pending_path() and reads failed_path();
# both sides must keep these names in sync (documented on both ends).


def pending_path() -> Path:
    return cache_dir() / "orphan-reclaim-pending"


def cooldown_path() -> Path:
    return cache_dir() / "orphan-reclaim-cooldown"


def failed_path() -> Path:
    return cache_dir() / "orphan-reclaim-failed"


def lock_dir() -> Path:
    """Same base as update.sh / index_worker.sh (`QMD_LOCK_BASE`, default
    `$TMPDIR/qmd-auto-context-locks-<user>` where <user> is `id -un`), but its OWN
    lock: this is single-flight for reclaim, not the writer/embed lock."""
    try:
        import pwd

        user = pwd.getpwuid(os.getuid()).pw_name
    except Exception:
        user = os.environ.get("USER") or str(os.getuid())
    base = Path(os.environ.get(
        "QMD_LOCK_BASE",
        str(Path(os.environ.get("TMPDIR", "/tmp")) / f"qmd-auto-context-locks-{user}"),
    ))
    return base / "qmd-orphan-reclaim.lock.d"


def mark_pending(reason: str = "collection_remove") -> bool:
    try:
        path = pending_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{reason}\n", encoding="utf-8")
        return True
    except OSError:
        return False


def clear_marker(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def cooldown_active(seconds: int) -> bool:
    """True while the last ATTEMPT is still inside the cooldown window.

    Attempt-based, not success-based, on purpose: if `cleanupOrphanedVectors`
    cannot delete anything (sqlite-vec unavailable -> it returns 0 by design) the
    orphan count stays above the threshold forever, and a success-based cooldown
    would vacuum on every single session -- the one thing this must not do.

    판정은 `cooldown.window_elapsed`가 SSOT다. 직접 `now - mtime`을 쓰면 **미래 mtime**
    (시계 되돌림·백업 복원)에서 나이가 음수가 되어 이 함수가 영구 True를 내고 **회수가
    영구 skip**된다 — 같은 병리가 dedup scan·sync lock·notice_once에도 있었다.
    """
    if seconds <= 0:
        return False
    try:
        mtime = cooldown_path().stat().st_mtime
    except OSError:
        return False  # absent marker means "never attempted" -> run
    return not qmd_cooldown.window_elapsed(mtime, time.time(), seconds)


def touch_cooldown() -> None:
    """Arm the cooldown BEFORE running cleanup.

    If the process is killed mid-vacuum the window is still armed, so a crash
    cannot turn into a per-session retry loop.
    """
    try:
        path = cooldown_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{datetime.now(timezone.utc).isoformat()}\n", encoding="utf-8")
    except OSError:
        pass


# --- index --------------------------------------------------------------------


def index_db_path(cwd=None) -> Path | None:
    """Read the exact index selected for the project being maintained."""
    try:
        candidate = Path(qmd_route.project_paths(cwd or os.getcwd())['INDEX_PATH'])
        return candidate if candidate.is_file() and not candidate.is_symlink() else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def count_orphans(db_path: Path) -> tuple[int, int] | None:
    """(orphan_rows, total_rows) or None when it cannot be determined.

    Read-only (shared WAL-aware reader + query_only), so a concurrent daemon is unaffected;
    measured 10ms on the live 16,590-row index.
    """
    try:
        with sqlite_read.connect(db_path, timeout=5.0) as conn:
            total = conn.execute(TOTAL_COUNT_SQL).fetchone()[0]
            orphans = conn.execute(ORPHAN_COUNT_SQL).fetchone()[0]
            return int(orphans), int(total)
    except (sqlite3.Error, OSError, ValueError):
        # No content_vectors table, locked/corrupt DB, or a concurrent write:
        # none can safely be interpreted as "reclaim now".
        return None


def resolve_qmd_bin(explicit: str = "") -> str:
    if explicit:
        return explicit
    env_bin = os.environ.get("QMD_BIN_RESOLVED") or os.environ.get("QMD_BIN")
    return env_bin or qmd_route.binary_info()['QMD_BIN']


def run_cleanup(qmd_bin: str, timeout: float, *, on_start=None) -> tuple[bool, str]:
    from runtime_work import qmd_operation
    from resource_deferred import ResourceDeferred
    observed=[]
    def execute(argv,**options):
        if on_start is not None:on_start()
        proc=subprocess.run(argv,capture_output=True,text=True,timeout=timeout,**options)
        observed.append(proc);return proc
    try:
        qmd_operation([qmd_bin,*CLEANUP_ARGV_TAIL],execute=execute)
        if not observed:raise ResourceDeferred('cleanup_resource_deferred',{})
        proc=observed[0]
    except FileNotFoundError:
        return False, "qmd_not_found"
    except subprocess.TimeoutExpired:
        return False, "timeout"
    except OSError as exc:
        return False, f"os_error:{exc.errno}"
    output = ((proc.stdout or "") + (proc.stderr or "")).strip().replace("\n", " | ")
    if proc.returncode != 0:
        return False, f"rc={proc.returncode} {output}"[:500]
    return True, output[:500]


def settings(cwd: str) -> dict:
    """orphanVectors settings for this project, or {} when the project is not
    opted in. `load_project_config` already applies the user-local optout marker
    and returns empty collections for unconfigured projects."""
    config = qmd_config.load_project_config(cwd)
    collections = config.get("collections")
    if not isinstance(collections, list) or not collections:
        return {}
    maintenance = config.get("maintenance") if isinstance(config.get("maintenance"), dict) else {}
    orphan = maintenance.get("orphanVectors")
    return orphan if isinstance(orphan, dict) else {}


def decide(cwd: str) -> dict:
    """Everything up to (but excluding) the cleanup call. Returns a result dict
    with `action` = skip|reclaim and `reason`/`trigger` for the log."""
    if os.environ.get("QMD_SANDBOX") or os.environ.get("GEMINI_SANDBOX"):
        return {"action": "skip", "reason": "sandbox"}
    if os.environ.get("QMD_ORPHAN_RECLAIM") == "off":
        return {"action": "skip", "reason": "disabled_env"}

    orphan_cfg = settings(cwd)
    if not orphan_cfg:
        return {"action": "skip", "reason": "no_collections"}
    if orphan_cfg.get("enabled") is False:
        return {"action": "skip", "reason": "disabled_config"}

    cooldown = int(orphan_cfg.get("cooldownSeconds") or 0)
    pending = pending_path().is_file()
    if cooldown_active(cooldown):
        # Applies to the post-remove trigger too: the pending marker survives, so
        # the removal's orphans are reclaimed on the next session rather than
        # letting a broken cleanup vacuum once per session.
        return {"action": "skip", "reason": "cooldown", "pending": pending}

    db_path = index_db_path(cwd)
    counts = count_orphans(db_path) if db_path else None
    result: dict = {"pending": pending}
    if counts is not None:
        orphans, total = counts
        ratio = (orphans / total) if total else 0.0
        result.update({"orphans": orphans, "total": total, "ratio": round(ratio, 4)})
    if pending:
        # A removal just created orphans; the ratio is not the question.
        result.update({"action": "reclaim", "reason": "post_remove", "trigger": "post_remove"})
        return result
    if counts is None:
        return {**result, "action": "skip", "reason": "orphans_unknown"}
    orphans, total = counts
    min_count = int(orphan_cfg.get("minCount") or 0)
    min_ratio = float(orphan_cfg.get("minRatio") or 0.0)
    ratio = (orphans / total) if total else 0.0
    if orphans < min_count or ratio < min_ratio:
        return {**result, "action": "skip", "reason": "below_threshold",
                "minCount": min_count, "minRatio": min_ratio}
    return {**result, "action": "reclaim", "reason": "threshold", "trigger": "threshold",
            "minCount": min_count, "minRatio": min_ratio}


def run(cwd: str, qmd_bin: str = "", timeout: float = 600.0) -> dict:
    result = decide(cwd)
    if result.get("action") != "reclaim":
        log(f"skip reason={result.get('reason')} "
            f"orphans={result.get('orphans')} total={result.get('total')} pending={result.get('pending')}")
        return result

    # Single-flight only. Serialization against our own `qmd embed` comes from the
    # call site (inside the embed subshell, strictly after embed). A concurrent
    # index_worker embed is handled by SQLite's busy timeout: measured, `qmd
    # cleanup` waits out an exclusive write transaction (5.1s) and then succeeds,
    # and it also succeeds with the daemon up and holding an open read
    # transaction -- so no daemon stop/reload is needed for VACUUM.
    lock = lock_dir()
    try:
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.mkdir()
    except FileExistsError:
        # **stale 회수가 없으면 이것이 영구 정지의 입구다**: 프로세스가 죽어 lock
        # 디렉터리가 남으면(SIGKILL·전원 차단·컨테이너 종료) 회수가 다시는 돌지 않고,
        # `qmd cleanup`은 사용자가 명령을 기억해야만 도는 상태로 되돌아간다. 저장소의
        # 다른 lock 5곳(`backend/keepalive.sh`, `backend/index_worker.sh`,
        # `core/backend_manager.sh`×3)이 전부 `find -mmin +10`으로 같은 처방을 갖고
        # 있으므로 같은 임계(`cooldown.LOCK_STALE_SECS`)를 파이썬으로 적용한다.
        #
        # 종점: 회수한 사실과 skip한 lock의 나이를 로그·`--json`에 남긴다 —
        # `lock_busy`만 있으면 "지금 다른 프로세스가 돈다"와 "영구히 막혔다"가
        # 구분되지 않아 진단이 불가능하다.
        reclaimed = False
        age = None
        try:
            mtime = lock.stat().st_mtime
            now = time.time()
            age = qmd_cooldown.age_seconds(mtime, now)  # 진단용 — 판정은 아래 한 곳이다
            if qmd_cooldown.window_elapsed(mtime, now, qmd_cooldown.LOCK_STALE_SECS):
                # rmdir 은 비어 있는 디렉터리만 지운다 — 우리 lock 은 파일을 담지 않으므로
                # 예상 밖 내용이 있으면 실패로 보호된다(env override 오설정 시 재귀 삭제 금지).
                lock.rmdir()
                lock.mkdir()
                reclaimed = True
        except OSError:
            reclaimed = False
        if not reclaimed:
            log(f"skip reason=lock_busy lock_age_secs={age}")
            return {**result, "action": "skip", "reason": "lock_busy",
                    "lockAgeSecs": age}
        log(f"lock stale reclaimed age_secs={age}")
        result["lockStaleReclaimed"] = True
    except OSError as exc:
        log(f"skip reason=lock_error errno={exc.errno}")
        return {**result, "action": "skip", "reason": "lock_error"}

    try:
        from resource_deferred import ResourceDeferred
        # Charge a real admitted attempt before VACUUM starts, preserving
        # crash/reboot cooldown without charging a resource refusal.
        try:ok, detail = run_cleanup(resolve_qmd_bin(qmd_bin), timeout,on_start=touch_cooldown)
        except ResourceDeferred:
            import runtime_work
            runtime_work.request('cleanup',str(Path(cwd).resolve()))
            log('deferred: shared compute or resource pressure; pending retained')
            return {**result,'action':'deferred','reason':'resource_deferred','ok':False}
        result["ok"] = ok
        result["detail"] = detail
        if ok:
            clear_marker(pending_path())
            clear_marker(failed_path())
            log(f"reclaimed trigger={result.get('trigger')} orphans={result.get('orphans')} "
                f"total={result.get('total')} detail={detail}")
        else:
            # Endpoint: the failure is recorded GLOBALLY so the next session's
            # synchronous update.sh path can surface it with notice_once. Without
            # this the index degrades silently -- the RC7 class this repo already
            # has a pattern for. The pending marker survives on purpose.
            try:
                path = failed_path()
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f"{detail}\n", encoding="utf-8")
            except OSError:
                pass
            log(f"FAILED trigger={result.get('trigger')} detail={detail}")
    finally:
        try:
            lock.rmdir()
        except OSError:
            pass
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cwd", default=os.getcwd())
    parser.add_argument("--qmd-bin", default="")
    parser.add_argument("--timeout", type=float, default=600.0)
    parser.add_argument("--json", action="store_true",
                        help="print the decision as one JSON line (tests/diagnostics)")
    parser.add_argument("--mark-pending", action="store_true",
                        help="record that a collection removal just created orphans")
    args = parser.parse_args()
    try:
        if args.mark_pending:
            ok = mark_pending()
            if not ok:
                # 삼킨 반환값이 아니다: 실패하면 post-remove 트리거 1회를 잃지만 임계
                # 트리거가 여전히 같은 orphan을 집으므로 회수 자체는 유실되지 않는다.
                # 대신 조용히 지나가지 않도록 로그에 남긴다.
                log("mark_pending FAILED (cache dir unwritable) — falling back to the threshold trigger")
            if args.json:
                print(json.dumps({"action": "mark_pending", "ok": ok}))
            return 0
        result = run(args.cwd, args.qmd_bin, args.timeout)
        if args.json:
            print(json.dumps(result, ensure_ascii=False))
    except Exception as exc:  # fail-open: never break the caller
        log(f"EXCEPTION: {exc!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

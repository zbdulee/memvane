#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.resolve()))
import compile_paths as cp
import config as qmd_config
import cooldown as qmd_cooldown
import dirty_queue
import resolve_paths as qmd_resolve_paths
import wiki_compile_enqueue as compile_enqueue


SKIP_DIRS = {".git", ".qmd", "node_modules", "__pycache__"}

# sync 경유 compile enqueue의 trigger 라벨. 편집 훅이 아니라 스냅샷 diff가 출처이므로
# `post_tool_source`로 기록하면 감사 로그에서 출처가 왜곡된다.
COMPILE_TRIGGER = "post_sync_source"
# `post_tool_source`만 켜 둔 기존 프로젝트에서도 git pull 구멍이 닫히도록 동등 grant로
# 인정한다. 그 trigger가 이미 표현하는 동의는 "소스 .md가 바뀌면 컴파일한다"이고, sync는
# 같은 사건을 다른 방법으로 탐지할 뿐이다(게다가 sync는 사용자가 직접 호출한다).
# 반대로 두 라벨 모두 없는 프로젝트(예: triggers=["manual"])는 그대로 skip된다.
COMPILE_ACCEPTED_TRIGGERS = (COMPILE_TRIGGER, compile_enqueue.POST_TOOL_TRIGGER)
# 한 번의 sync가 compile 큐에 넣을 수 있는 소스 파일 수 상한. compile worker에는 per-run
# 상한이 없어(`batch.maxItems`는 처리 시작 조건) 큐에 든 만큼 host CLI를 연속 spawn하고
# 그 토큰 비용은 사용자 계정으로 청구된다. 수백 파일 pull 뒤의 sync 한 번이 수 시간짜리
# 백그라운드 CLI 실행으로 번지지 않게 막는다. 상한을 넘긴 파일은 스냅샷을 진전시키지 않아
# 다음 sync가 다시 집어 간다(조용히 유실되지 않는다).
DEFAULT_COMPILE_MAX_FILES = 50


def emit_json(enabled, payload):
    if enabled:
        print(json.dumps(payload, ensure_ascii=False))


def state_dir():
    return Path(os.environ.get(
        "QMD_SYNC_STATE_DIR",
        str(Path.home() / ".config" / "qmd" / "sync-state"),
    ))


def project_key(project_root, config_path):
    raw = f"{project_root}\n{config_path or ''}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def state_path(project_root, config_path):
    return state_dir() / f"{project_key(project_root, config_path)}.json"


def read_state(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def write_state_atomic(path, snapshot):
    """스냅샷/커서 상태를 원자적으로 쓴다. **반환값이 없고 실패는 예외로 전파된다.**

    이 함수는 "삼킨 반환값" 목록에 오를 수 없다 — 확인할 값이 없고 실패가 조용하지 않다.
    호출부 분류는 전부 동일하다(**실패 방향이 안전**): 쓰기가 실패하면 상태가 전진하지
    않으므로 다음 회차가 같은 범위를 다시 처리한다. 손해는 재처리이고 유실이 아니다.
    sync에서는 예외가 `run()`을 뚫고 나가 skill(`set -euo pipefail`)에서 실패로 보이며,
    백그라운드 스캐너(`wiki_source_scan`·`wiki_dedup_scan`)에서는 각자의 `main()`이 잡아
    로그에 `EXCEPTION`으로 남긴다.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(snapshot, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(tmp, path)


def lock_path():
    return Path(os.environ.get("QMD_SYNC_LOCKDIR", "/tmp/qmd-sync.lock.d"))


def stale_lock_seconds():
    try:
        return max(1, int(os.environ.get("QMD_SYNC_LOCK_STALE_SECONDS", "3600")))
    except ValueError:
        return 3600


def pid_is_running(pid):
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def lock_is_stale(lock):
    pid_file = lock / "pid"
    try:
        raw_pid = pid_file.read_text(encoding="utf-8").strip()
        pid = int(raw_pid)
    except (OSError, ValueError):
        # pid를 못 읽으면 나이로 판정한다. 나이 계산은 `cooldown.window_elapsed`가 SSOT다 —
        # `now - mtime`을 그대로 쓰면 **미래 mtime**(시계 되돌림·백업 복원)에서 나이가
        # 음수가 되어 lock이 영원히 stale로 보이지 않고 **sync가 영구 sync_busy**가 된다
        # (pid 파일이 없는 lock = 정확히 비정상 종료로 남은 lock이라 이 분기가 회수 경로다).
        try:
            mtime = lock.stat().st_mtime
        except OSError:
            return False
        return qmd_cooldown.window_elapsed(mtime, time.time(), stale_lock_seconds())
    return not pid_is_running(pid)


def acquire_lock():
    lock = lock_path()
    try:
        os.mkdir(lock)
    except FileExistsError:
        if not lock_is_stale(lock):
            return None
        release_lock(lock)
        try:
            os.mkdir(lock)
        except FileExistsError:
            return None
    try:
        with open(lock / "pid", "w", encoding="utf-8") as handle:
            handle.write(str(os.getpid()))
    except OSError:
        release_lock(lock)
        return None
    return lock


def release_lock(lock):
    if not lock:
        return
    # 우리 락 구조는 디렉토리 안에 pid 파일만 둔다. shutil.rmtree로 통째 지우는 대신
    # pid 파일만 unlink 후 빈 디렉토리를 rmdir 한다. 예상 밖 내용이 있으면 rmdir이
    # 실패해(ENOTEMPTY) 보호된다 — env(QMD_SYNC_LOCKDIR) 오설정 시 재귀 삭제 방지.
    try:
        (lock / "pid").unlink()
    except OSError:
        pass
    try:
        os.rmdir(lock)
    except OSError:
        pass


def resolve_collection_roots(project_root, config):
    resolved = qmd_resolve_paths.resolve_paths(project_root, json.dumps(config))
    if resolved.get("refused"):
        return [], resolved.get("reason", "refused")
    entries = []
    root = Path(project_root).resolve()
    for entry in resolved.get("entries", []):
        name = entry.get("name")
        path = entry.get("path")
        if not isinstance(name, str) or not isinstance(path, str):
            continue
        p = Path(path).expanduser()
        abs_path = p.resolve() if p.is_absolute() else (root / p).resolve()
        entries.append((name, abs_path))
    return entries, None


def scan_files(root, *, hash_markdown=False):
    files = {}
    if not root.is_dir():
        return None
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in SKIP_DIRS]
        current = Path(dirpath)
        for filename in filenames:
            path = current / filename
            if not path.is_file() or path.is_symlink():
                continue
            try:
                stat = path.stat()
                rel = path.relative_to(root).as_posix()
            except OSError:
                continue
            meta = {"mtimeNs": stat.st_mtime_ns, "size": stat.st_size}
            if hash_markdown and path.suffix.lower() == '.md':
                digest = hashlib.sha256()
                try:
                    with path.open('rb') as handle:
                        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
                            digest.update(chunk)
                except OSError:
                    continue
                meta['sha256'] = digest.hexdigest()
            files[rel] = meta
    return files


def compare_collection(previous_files, current_files):
    """변경을 컬렉션 루트 기준 상대경로 **목록**으로 반환한다.

    예전에는 카운트만 반환했지만 compile source-queue enqueue가 파일 단위 diff를
    필요로 한다(dirty-queue는 여전히 컬렉션 단위). 호출부는 len()으로 카운트를 얻는다.
    """
    created, updated, deleted = [], [], []
    previous_files = previous_files or {}
    current_files = current_files or {}
    for rel, meta in current_files.items():
        old = previous_files.get(rel)
        if old is None:
            created.append(rel)
        elif (old.get("mtimeNs") != meta.get("mtimeNs") or old.get("size") != meta.get("size")
              or old.get('sha256') != meta.get('sha256')):
            updated.append(rel)
    for rel in previous_files:
        if rel not in current_files:
            deleted.append(rel)
    return sorted(created), sorted(updated), sorted(deleted)


def build_snapshot(project_root, config_path, roots, previous, *, hash_markdown=False):
    collections = {}
    warnings = []
    totals = {"created": 0, "updated": 0, "deleted": 0}
    changed = {}
    changed_files = {}
    previous_collections = (previous or {}).get("collections", {})

    for name, root in sorted(roots):
        files = scan_files(root, hash_markdown=hash_markdown)
        if files is None:
            warnings.append({"collection": name, "reason": "missing_root"})
            if name in previous_collections:
                collections[name] = previous_collections[name]
            continue
        previous_files = previous_collections.get(name, {}).get("files", {})
        created, updated, deleted = compare_collection(previous_files, files)
        totals["created"] += len(created)
        totals["updated"] += len(updated)
        totals["deleted"] += len(deleted)
        if created or updated or deleted:
            changed[name] = str(root)
            changed_files[name] = {
                "root": str(root),
                "created": created,
                "updated": updated,
                "deleted": deleted,
            }
        collections[name] = {"root": str(root), "files": files}

    snapshot = {
        "version": 1,
        "projectRoot": project_root,
        "configPath": config_path,
        "collections": collections,
    }
    return snapshot, totals, changed, changed_files, warnings


def compile_max_files():
    try:
        value = int(os.environ.get("QMD_SYNC_COMPILE_MAX", str(DEFAULT_COMPILE_MAX_FILES)))
    except ValueError:
        return DEFAULT_COMPILE_MAX_FILES
    return max(0, value)


def compile_engine(compile_cfg):
    """compile 레코드에 남길 engine 라벨.

    worker는 engine으로 extractor adapter를 고르므로(`resolve_extractor_argv`),
    QMD_ENGINE이 없는 수동 sync에서도 해석 가능한 값을 남겨야 한다 — 아니면 job이
    `missing_extractor`로 실패한다. builtin adapter가 호출하는 host CLI 바이너리 이름이
    engine 이름과 같아서(claude/codex/hermes) 설정에 적힌 builtins 순서대로 PATH를 훑는다.

    **`backends`만 설정한 프로젝트도 해석해야 한다.** builtins가 비고 backends에 명시 argv만
    적은 설정(라이브 service-engineering이 그 형태다)에서 예전에는 `unknown`을 돌려줬고,
    worker의 `resolve_extractor_argv`는 `backends["unknown"]`을 찾지 못해 그 프로젝트의
    수동 enqueue가 전부 `missing_extractor`로 죽었다 — QMD_ENGINE을 넣어 주는 hook 경로에서만
    우연히 살아 있던 분기다. backends 키도 같은 규칙(엔진 이름 == host CLI 이름)으로 훑는다.
    """
    extractor = compile_cfg.get("extractor") if isinstance(compile_cfg.get("extractor"), dict) else {}
    raw_builtins = extractor.get("builtins")
    builtins = [e for e in raw_builtins if isinstance(e, str)] if isinstance(raw_builtins, list) else []
    raw_backends = extractor.get("backends") if isinstance(extractor.get("backends"), dict) else {}
    backends = [e for e in raw_backends if isinstance(e, str)]
    env_engine = os.environ.get("QMD_ENGINE")
    if isinstance(env_engine, str) and (env_engine in builtins or env_engine in backends):
        return env_engine
    # builtins를 먼저 본다(기존 우선순위 유지). 그 다음이 명시 backends다.
    for engine in list(builtins) + [e for e in backends if e not in builtins]:
        if shutil.which(engine):
            return engine
    if builtins:
        return builtins[0]
    if backends:
        return backends[0]
    return env_engine if isinstance(env_engine, str) and env_engine else qmd_config.UNKNOWN_ENGINE


def compile_candidates(project_root, config, compile_cfg, changed_files, *, trigger=COMPILE_TRIGGER):
    """변경 파일 중 compile source가 될 수 있는 것만 (컬렉션, 상대경로, 레코드)로 추린다.

    per-file 게이팅(.md 확장자 / raw·session role / dot-prefix / DENIED_SOURCE_SEGMENTS)은
    `wiki_compile_enqueue._source_record`를 그대로 재사용한다. 삭제된 파일은 읽을 소스가
    없으므로 제외한다(카드 정리는 compile 경로의 책임이 아니다).
    """
    engine = compile_engine(compile_cfg)
    candidates = []
    for name in sorted(changed_files):
        entry = changed_files[name]
        root = Path(entry["root"])
        for rel in sorted(set(entry["created"]) | set(entry["updated"])):
            record = compile_enqueue._source_record(
                str(root / rel),
                project_root,
                project_root,
                config,
                engine,
                trigger=trigger,
            )
            if record is not None:
                candidates.append((name, rel, record))
    return candidates


def defer_snapshot_entries(snapshot, previous, deferred):
    """상한으로 미룬 파일은 스냅샷을 진전시키지 않아 다음 sync가 다시 집는다."""
    previous_collections = (previous or {}).get("collections", {})
    for name, rel in deferred:
        files = snapshot.get("collections", {}).get(name, {}).get("files")
        if not isinstance(files, dict):
            continue
        old = previous_collections.get(name, {}).get("files", {}).get(rel)
        if isinstance(old, dict):
            files[rel] = old
        else:
            files.pop(rel, None)


def enqueue_compile_sources(project_root, config, changed_files, snapshot, previous,
                            *, session_start=False):
    """sync가 감지한 변경 .md를 compile source-queue에도 넣는다.

    git pull·rebase·외부 도구 편집은 PostToolUse 훅을 타지 않아 wiki 카드가 조용히 낡는다.
    dirty-queue enqueue(컬렉션 단위)는 qmd 인덱스만 최신으로 만들 뿐이므로 이 경로가 없으면
    `wikiOnly` 프로젝트에서 낡은 카드가 유일한 recall 소스로 남는다.

    LLM은 호출하지 않는다 — 메타데이터만 append하고 실제 compile은 기존 backend manager
    kick 경로(편집 훅의 kick-wiki-compile, SessionStart의 --flush)가 처리한다.
    """
    accepted = ('session_start_source',) if session_start else COMPILE_ACCEPTED_TRIGGERS
    compile_cfg = compile_enqueue.compile_gate(config, accepted)
    if compile_cfg is None:
        return {"queued": 0, "deferred": 0, "reason": "compile_disabled"}
    queue_path = compile_enqueue._safe_queue_path(project_root, cp.rel(cp.SOURCE_QUEUE))
    if queue_path is None:
        return {"queued": 0, "deferred": 0, "reason": "queue_path_rejected"}
    candidates = compile_candidates(project_root, config, compile_cfg, changed_files,
        trigger='session_start_source' if session_start else COMPILE_TRIGGER)
    if not candidates:
        return {"queued": 0, "deferred": 0, "reason": "no_sources"}
    cap = compile_max_files()
    picked = candidates[:cap]
    deferred = [(name, rel) for name, rel, _ in candidates[cap:]]
    compile_enqueue._append_jsonl(queue_path, [record for _, _, record in picked])
    defer_snapshot_entries(snapshot, previous, deferred)
    return {
        "queued": len(picked),
        "deferred": len(deferred),
        "reason": "capped" if deferred else "queued",
    }


def run(cwd, *, json_output=False, dry_run=False, baseline_only=False,
        session_start=False):
    if os.environ.get("QMD_SANDBOX"):
        return 0
    import setup_guard
    if setup_guard.status(cwd)['status'] != 'ready':
        emit_json(json_output, {'ok': False, 'reason': 'setup_required'})
        return 1

    if session_start:
        found = qmd_config.find_project_config(cwd)
        if compile_enqueue.compile_gate(found['config'], ('session_start_source',)) is None:
            emit_json(json_output, {'ok': True, 'reason': 'session_start_compile_disabled',
                                    'compileQueued': 0})
            return 0
        import qmd_shared_store
        qmd_shared_store.select(os.environ.get('HOME'))
        shared = qmd_shared_store.root(os.environ.get('HOME'))
        os.environ['QMD_SYNC_STATE_DIR'] = str(shared / 'session-sync')
        os.environ['QMD_SYNC_LOCKDIR'] = str(shared / '.session-sync.lock.d')

    lock = acquire_lock()
    if lock is None:
        emit_json(json_output, {"ok": True, "reason": "sync_busy", "lockPath": str(lock_path())})
        return 0

    try:
        info = qmd_config.find_project_config(cwd)
        config = info["config"]
        project_root = info["projectRoot"]
        config_path = info["configPath"]
        out_state = state_path(project_root, config_path)

        roots, refused_reason = resolve_collection_roots(project_root, config)
        if refused_reason or not roots:
            emit_json(json_output, {
                "ok": True,
                "reason": "no_collections",
                "projectRoot": project_root,
                "created": 0,
                "updated": 0,
                "deleted": 0,
                "collectionsQueued": [],
                "statePath": str(out_state),
            })
            return 0

        previous = read_state(out_state)
        snapshot, totals, changed, changed_files, warnings = build_snapshot(
            project_root, config_path, roots, previous, hash_markdown=session_start
        )
        # 같은 diff가 두 소비자로 갈라진다. dirty 큐(=qmd 인덱싱)는 INDEXED_ROLES만,
        # compile source 큐는 `changed_files` 그대로(role `source` 포함)다 — 그것이
        # "인덱싱 안 되지만 카드는 만든다"는 role의 정의다. `collectionsQueued`는 dirty
        # 큐에 실제로 들어간 것만 보고한다(보고와 행동이 갈리면 sync 결과를 못 믿는다).
        sync_roles = qmd_config.role_map(config)
        indexed_changed = {
            name: root for name, root in changed.items()
            if qmd_config.is_indexed_collection(sync_roles, name)
        }
        queued = sorted(indexed_changed)
        compile_result = {"queued": 0, "deferred": 0, "reason": "not_attempted"}

        if baseline_only:
            write_state_atomic(out_state, snapshot)
            reason = "baseline"
            queued = []
        elif dry_run:
            reason = "dry_run"
        else:
            if changed:
                # role `source`만 바뀐 sync는 dirty 큐에 넣을 것이 없다. 그래도 compile
                # enqueue와 스냅샷 전진은 해야 한다 — 안 하면 그 변경이 매 sync마다
                # 재검출되고 compile이 같은 파일을 반복 큐잉한다(유료 호출 반복).
                if indexed_changed and not session_start:
                    dirty_queue.enqueue_project_collections(project_root, indexed_changed)
                # compile enqueue를 스냅샷 기록 "전"에 한다: 상한으로 미룬 파일의 스냅샷
                # 엔트리를 되돌려 다음 sync가 다시 집게 하기 때문이다.
                compile_result = enqueue_compile_sources(
                    project_root, config, changed_files, snapshot, previous,
                    session_start=session_start
                )
                # 스냅샷 전진 실패는 예외로 나간다(위 docstring). 그 경우 다음 sync가
                # 같은 변경을 다시 감지해 재enqueue하고, worker가 이미 컴파일한 뒤라면
                # 그 소스에 대한 유료 호출이 한 번 더 든다 — 재처리 방향이므로 유실은
                # 없지만 공짜도 아니라는 뜻이다(실패가 조용하지 않아야 하는 이유).
                write_state_atomic(out_state, snapshot)
                reason = "synced"
            else:
                reason = "unchanged"
                if not out_state.exists():
                    write_state_atomic(out_state, snapshot)

        emit_json(json_output, {
            "ok": True,
            "reason": reason,
            "projectRoot": project_root,
            "created": totals["created"],
            "updated": totals["updated"],
            "deleted": totals["deleted"],
            "collectionsQueued": queued,
            "compileQueued": compile_result["queued"],
            "compileDeferred": compile_result["deferred"],
            "compileReason": compile_result["reason"],
            "statePath": str(out_state),
            "warnings": warnings,
        })
        return 0
    finally:
        release_lock(lock)


def main():
    parser = argparse.ArgumentParser(description="Synchronize qmd dirty queue from filesystem state.")
    parser.add_argument("--cwd", default=os.getcwd())
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--baseline-only", action="store_true")
    parser.add_argument("--session-start", action="store_true")
    args = parser.parse_args()
    return run(args.cwd, json_output=args.json, dry_run=args.dry_run,
               baseline_only=args.baseline_only, session_start=args.session_start)


if __name__ == "__main__":
    sys.exit(main())

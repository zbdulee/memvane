#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
import stat
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.resolve()))
import config as qmd_config
import cooldown as qmd_cooldown
import dirty_queue
import file_change_plan
import resolve_paths as qmd_resolve_paths
import wiki_compile_enqueue as compile_enqueue

# Pure planning moved to file_change_plan; these names stay for existing callers.
compare_collection = file_change_plan.compare_collection
plan_collection = file_change_plan.plan_collection
defer_snapshot_entries = file_change_plan.defer_snapshot_entries


SKIP_DIRS = {".git", ".qmd", "node_modules", "__pycache__"}

def emit_json(enabled, payload):
    if enabled:
        print(json.dumps(payload, ensure_ascii=False))


def state_dir(explicit=None):
    return Path(explicit) if explicit is not None else Path(os.environ.get(
        "QMD_SYNC_STATE_DIR",
        str(Path.home() / ".config" / "qmd" / "sync-state"),
    ))


def project_key(project_root, config_path):
    raw = f"{project_root}\n{config_path or ''}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def state_path(project_root, config_path, *, state_root=None):
    return state_dir(state_root) / f"{project_key(project_root, config_path)}.json"


def read_state(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


EXIT_STATE_UNREADABLE = 3
EXIT_RECOVERY_REQUIRED = 4


class StateUnreadable(Exception):
    """An existing snapshot cannot safely serve as prior state or a baseline."""


def load_state(path):
    """Distinguish a missing snapshot from unreadable or malformed state.

    Other scan cursors use read_state's older empty-dict fallback. Sync must
    fail closed so a damaged cursor cannot requeue every source as created.
    """
    if path.is_symlink():
        raise StateUnreadable('symlink')
    try:
        with open(path, 'rb') as handle:
            raw = handle.read()
    except FileNotFoundError:
        if os.path.lexists(path):
            raise StateUnreadable('dangling_link') from None
        return None
    except OSError as error:
        raise StateUnreadable(f'read_error:{type(error).__name__}') from None
    try:
        data = json.loads(raw.decode('utf-8'))
    except UnicodeDecodeError:
        raise StateUnreadable('not_utf8') from None
    except ValueError:
        raise StateUnreadable('invalid_json') from None
    if not isinstance(data, dict):
        raise StateUnreadable('not_object')
    collections = data.get('collections')
    if not isinstance(collections, dict):
        raise StateUnreadable('bad_collections')
    for entry in collections.values():
        files = entry.get('files') if isinstance(entry, dict) else None
        if not isinstance(files, dict) or not all(isinstance(meta, dict) for meta in files.values()):
            raise StateUnreadable('bad_collection_entry')
    return data


def write_state_atomic(path, snapshot):
    """스냅샷/커서 상태를 원자적으로 쓴다. **반환값이 없고 실패는 예외로 전파된다.**

    이 함수는 "삼킨 반환값" 목록에 오를 수 없다 — 확인할 값이 없고 실패가 조용하지 않다.
    쓰기가 실패하면 상태가 전진하지 않는다. 수동 wiki compile sync는 별도 pending
    마커를 남겨 자동 재처리와 baseline 전진을 모두 막는다. 다른 호출부는 각자의
    재처리 계약을 따른다.
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


def pending_marker_path(path):
    return path.with_name(f'{path.name}.pending-compile')


def pending_marker_state(path, project_root, state_file):
    """Fail closed when a prior enqueue may have been claimed before snapshot ACK."""
    if not os.path.lexists(path):
        return 'absent'
    try:
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or info.st_mode & 0o077 or info.st_size > 4096):
            return 'invalid'
        data = json.loads(path.read_text(encoding='utf-8'))
        if data != {'schema': 1, 'projectRoot': project_root,
                    'statePath': str(state_file)}:
            return 'invalid'
        return 'valid'
    except (OSError, UnicodeError, ValueError):
        return 'invalid'


def write_pending_marker(path, project_root, state_file):
    """Durably mark enqueue intent before any queue write or worker claim."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({'schema': 1, 'projectRoot': project_root,
                          'statePath': str(state_file)}, sort_keys=True).encode('utf-8')
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                 getattr(os, 'O_NOFOLLOW', 0), 0o600)
    created = os.fstat(fd)
    try:
        if os.write(fd, payload) != len(payload):
            raise OSError('short_pending_marker_write')
        os.fsync(fd)
        os.close(fd)
        fd = None
        parent = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    except BaseException:
        if fd is not None:
            os.close(fd)
        # No enqueue has started if marker creation failed. Remove only this
        # inode; if cleanup itself fails, the residue still blocks all syncs.
        try:
            current = path.lstat()
            if (current.st_dev, current.st_ino) == (created.st_dev, created.st_ino):
                path.unlink()
                parent = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(parent)
                finally:
                    os.close(parent)
        except OSError:
            pass
        raise


def clear_pending_marker(path):
    path.unlink()
    parent = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(parent)
    finally:
        os.close(parent)


def lock_path(explicit=None):
    return Path(explicit) if explicit is not None else Path(os.environ.get("QMD_SYNC_LOCKDIR", "/tmp/qmd-sync.lock.d"))


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


def acquire_lock(lock_dir=None):
    lock = lock_path(lock_dir)
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


def scan_files(root, *, hash_markdown=False, return_unknown=False):
    files = {}
    unknown = set()
    try:
        root_info = root.stat()
    except FileNotFoundError:
        return None
    if not stat.S_ISDIR(root_info.st_mode):
        return None
    def walk_error(error):
        path = Path(error.filename) if error.filename else root
        if path == root:
            raise error
        if not isinstance(error, FileNotFoundError):
            unknown.add(path.relative_to(root).as_posix())
    for dirpath, dirnames, filenames in os.walk(root, onerror=walk_error):
        dirnames[:] = [name for name in dirnames if name not in SKIP_DIRS]
        current = Path(dirpath)
        for filename in filenames:
            path = current / filename
            rel = path.relative_to(root).as_posix()
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            except OSError:
                unknown.add(rel)
                continue
            if not stat.S_ISREG(info.st_mode):
                continue
            meta = {"mtimeNs": info.st_mtime_ns, "size": info.st_size}
            if hash_markdown and path.suffix.lower() == '.md':
                digest = hashlib.sha256()
                try:
                    with path.open('rb') as handle:
                        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
                            digest.update(chunk)
                except FileNotFoundError:
                    continue
                except OSError:
                    unknown.add(rel)
                    continue
                meta['sha256'] = digest.hexdigest()
            files[rel] = meta
    if not return_unknown and unknown:
        raise OSError('incomplete_collection_scan')
    return (files, unknown) if return_unknown else files


def build_snapshot(project_root, config_path, roots, previous, *, hash_markdown=False):
    collections = {}
    warnings = []
    entries = []
    previous_collections = (previous or {}).get("collections", {})

    # Scanning adapter: I/O and missing-root handling stay here; the change
    # set itself is computed by file_change_plan from these values alone.
    for name, root in sorted(roots):
        scan = scan_files(root, hash_markdown=hash_markdown, return_unknown=True)
        if scan is None:
            warnings.append({"collection": name, "reason": "missing_root"})
            if name in previous_collections:
                collections[name] = previous_collections[name]
            continue
        previous_files = previous_collections.get(name, {}).get("files", {})
        scanned_files, unknown = scan
        if unknown:
            warnings.append({"collection": name, "reason": "unreadable_paths",
                             "count": len(unknown)})
        entries.append((name, str(root), previous_files, scanned_files, unknown))

    # No role filter here: `changed` keeps every changed collection and run()
    # narrows it to indexed roles, as before.
    plan = file_change_plan.plan_change_set(entries)
    collections.update(plan["collections"])
    snapshot = {
        "version": 1,
        "projectRoot": project_root,
        "configPath": config_path,
        "collections": collections,
    }
    return snapshot, plan["totals"], plan["indexTargets"], plan["changedFiles"], warnings


def session_start_sync_off_reason(found):
    """Distinguish a deliberate false from an invalid value normalized off."""
    path = found.get('configPath')
    if not path:
        return 'session_start_sync_disabled'
    try:
        with open(path, 'r', encoding='utf-8') as handle:
            raw = json.load(handle)
    except (OSError, ValueError):
        return 'session_start_sync_disabled'
    return ('session_start_sync_invalid' if qmd_config.invalid_session_start_sync(raw)
            else 'session_start_sync_disabled')


def run(cwd, *, json_output=False, dry_run=False, baseline_only=False,
        session_start=False):
    if os.environ.get("QMD_SANDBOX"):
        return 0
    import setup_guard
    if setup_guard.status(cwd)['status'] != 'ready':
        emit_json(json_output, {'ok': False, 'reason': 'setup_required'})
        return 1

    state_root = lock_dir = None
    if session_start:
        found = qmd_config.find_project_config(cwd)
        if not found['config']['sessionStartSync']:
            reason = session_start_sync_off_reason(found)
            if reason == 'session_start_sync_invalid':
                print('[qmd] sessionStartSync 값이 true/false가 아니어서 SessionStart sync를 건너뜁니다',
                      file=sys.stderr)
            emit_json(json_output, {'ok': reason == 'session_start_sync_disabled',
                                    'reason': reason, 'compileQueued': 0})
            return 0
        import qmd_shared_store
        # select validates the existing pointer and prepared generation without
        # creating or switching either; sync only owns its own snapshot path.
        qmd_shared_store.select(os.environ.get('HOME'))
        shared = qmd_shared_store.root(os.environ.get('HOME'))
        state_root = shared / 'session-sync'
        lock_dir = shared / '.session-sync.lock.d'

    lock = acquire_lock(lock_dir)
    if lock is None:
        emit_json(json_output, {"ok": True, "reason": "sync_busy", "lockPath": str(lock_path(lock_dir))})
        return 0

    try:
        info = qmd_config.find_project_config(cwd)
        config = info["config"]
        project_root = info["projectRoot"]
        config_path = info["configPath"]
        out_state = state_path(project_root, config_path, state_root=state_root)
        marker = pending_marker_path(out_state)
        marker_state = pending_marker_state(marker, project_root, out_state)
        # Intent alone cannot prove which paid source jobs reached the queue or
        # were claimed. Even baseline-only must not advance past uncertain work.
        if marker_state != 'absent':
            emit_json(json_output, {'ok': False, 'reason': 'sync_recovery_required',
                                    'projectRoot': project_root, 'statePath': str(out_state),
                                    'pendingPath': str(marker)})
            return EXIT_RECOVERY_REQUIRED

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

        try:
            previous = load_state(out_state)
        except StateUnreadable as error:
            print(f'[qmd] sync state unreadable ({error}): {out_state}', file=sys.stderr)
            emit_json(json_output, {
                'ok': False,
                'reason': 'state_unreadable',
                'stateError': str(error),
                'projectRoot': project_root,
                'created': 0,
                'updated': 0,
                'deleted': 0,
                'collectionsQueued': [],
                'compileQueued': 0,
                'compileDeferred': 0,
                'compileReason': 'not_attempted',
                'statePath': str(out_state),
            })
            return EXIT_STATE_UNREADABLE
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

        # New default-on installs have no snapshot yet. QMD update/embed has
        # already completed before this call, so record a baseline without
        # dirtying every existing indexed file. Explicit SessionStart compile
        # consent retains its former first-run source behavior.
        had_state = previous is not None
        first_sync_baseline = (session_start and not had_state and
            compile_enqueue.compile_gate(config, ('session_start_source',)) is None)
        if baseline_only or first_sync_baseline:
            write_state_atomic(out_state, snapshot)
            reason = "baseline"
            queued = []
        elif dry_run:
            reason = "dry_run"
        else:
            if changed:
                # A worker can drain the source queue before a failed snapshot
                # is retried. A live-queue revision check alone cannot prove
                # whether that revision was already paid for. Leave this marker
                # on failure. Intent alone is insufficient to advance a baseline.
                guarded_compile = (not session_start and
                    compile_enqueue.compile_gate(config,
                        compile_enqueue.COMPILE_ACCEPTED_TRIGGERS) is not None)
                if guarded_compile:
                    write_pending_marker(marker, project_root, out_state)
                # role `source`만 바뀐 sync는 dirty 큐에 넣을 것이 없다. 그래도 compile
                # enqueue와 스냅샷 전진은 해야 한다 — 안 하면 그 변경이 매 sync마다
                # 재검출되고 compile이 같은 파일을 반복 큐잉한다(유료 호출 반복).
                # compile enqueue를 스냅샷 기록 "전"에 한다: 상한으로 미룬 파일의 스냅샷
                # 엔트리를 되돌려 다음 sync가 다시 집게 하기 때문이다. 순서는
                # file_change_plan.apply_change_set 한 곳이 소유한다.
                # 스냅샷 전진 실패는 예외로 나간다(write_state_atomic docstring). 그 경우
                # 다음 sync가 같은 변경을 다시 감지해 재enqueue하고, worker가 이미 컴파일한
                # 뒤라면 그 소스에 대한 유료 호출이 한 번 더 든다 — 재처리 방향이므로
                # 유실은 없지만 공짜도 아니라는 뜻이다(실패가 조용하지 않아야 하는 이유).
                skip_index = session_start and not had_state
                compile_result = file_change_plan.apply_change_set(
                    indexed_changed, changed_files, snapshot, previous,
                    enqueue_index=None if skip_index else (
                        lambda targets: dirty_queue.enqueue_project_collections(
                            project_root, targets)),
                    enqueue_wiki=lambda files: compile_enqueue.enqueue_changed_sources(
                        project_root, config, files, session_start=session_start),
                    persist=lambda value: write_state_atomic(out_state, value))
                if guarded_compile:
                    clear_pending_marker(marker)
                reason = "synced"
            else:
                reason = "unchanged"
                if previous is None:
                    write_state_atomic(out_state, snapshot)

        result = {
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
        }
        # 같은 revision 행이 아직 live 큐에 있어 건너뛴 재시도(예: enqueue 뒤 스냅샷 저장
        # 실패)는 compileQueued=0이지만 깨울 작업은 남아 있다. 그때만 내부 신호를 덧붙여
        # update.sh가 compile worker를 kick하게 한다. 그 밖의 출력은 바이트 그대로다.
        if compile_result.get("wakePending"):
            result["compileWakePending"] = True
        emit_json(json_output, result)
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

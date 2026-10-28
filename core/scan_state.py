"""Per-project scan cursors for the wiki lifecycle and dedup workers.

Each worker keeps one JSON cursor per project under ``state_dir()``. The
module imports only the standard library.
"""
import hashlib
import json
import os
from pathlib import Path

import memvane_paths


def state_dir(explicit=None):
    return Path(explicit) if explicit is not None else Path(os.environ.get(
        "QMD_SYNC_STATE_DIR",
        str(memvane_paths.auto_context_state() / "scan-state"),
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


def write_state_atomic(path, snapshot):
    """스냅샷/커서 상태를 원자적으로 쓴다. **반환값이 없고 실패는 예외로 전파된다.**

    이 함수는 "삼킨 반환값" 목록에 오를 수 없다 — 확인할 값이 없고 실패가 조용하지 않다.
    쓰기가 실패하면 상태가 전진하지 않는다. 수동 wiki compile sync는 별도 pending
    마커를 남겨 자동 재처리와 baseline 전진을 모두 막는다. 다른 호출부는 각자의
    재처리 계약을 따른다.
    sync에서는 예외가 `run()`을 뚫고 나가 skill(`set -euo pipefail`)에서 실패로 보이며,
    백그라운드 worker(`wiki_lifecycle`·`wiki_dedup_scan`)에서는 각자의 `main()`이 잡아
    로그에 `EXCEPTION`으로 남긴다.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(snapshot, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(tmp, path)

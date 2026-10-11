#!/usr/bin/env python3
"""Enqueue source markdown files for automatic wiki compile.

This hook-side command is intentionally silent. It records source metadata only;
it never stores source content or calls an extractor/LLM.
"""

import json
import fcntl
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.resolve()))
import compile_paths as cp
import config as qmd_config
import posttool
import wiki_freshness
from collection_match import select_collections


DEFAULT_ENGINE = qmd_config.UNKNOWN_ENGINE

# 편집 훅 경로의 trigger 라벨. 수동 sync 경로는 `post_sync_source`를 쓴다(core/sync.py).
POST_TOOL_TRIGGER = "post_tool_source"

# compile source가 절대 될 수 없는 segment. `.auto-context`는 이 플러그인의 관리 영역
# (wiki 카드·큐·로그)이라 소스로 승격하면 카드가 자기 자신을 재컴파일해 증식한다.
# 나머지는 collectionPaths에 등록될 일이 거의 없지만 방어적으로 막는다.
DENIED_SOURCE_SEGMENTS = frozenset({".auto-context", ".git", ".hg", ".svn", "node_modules"})


def _utc_now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _is_hidden_source_path(rel_path: str) -> bool:
    return any(part.startswith(".") for part in Path(rel_path).parts)


def _is_denied_source_path(rel_path: str) -> bool:
    return any(part in DENIED_SOURCE_SEGMENTS for part in Path(rel_path).parts)


def _engine(payload):
    value = payload.get("engine") or os.environ.get("QMD_ENGINE") or DEFAULT_ENGINE
    return value if isinstance(value, str) and value.strip() else DEFAULT_ENGINE


def _safe_queue_path(project_root, configured_path=None):
    """`.auto-context/compile/<name>` 아래 큐 파일. 밖을 가리키면 None.

    `configured_path`는 이제 **설정이 아니라 상수 이름**이다(`compile_paths.rel(...)`) —
    호출부가 verify 큐/ source 큐를 구분해 넘긴다. 검증은 그대로 유지한다: 파일 자체가
    compile 디렉터리 밖을 가리키는 symlink일 수 있다.
    """
    if not isinstance(configured_path, str) or not configured_path:
        configured_path = cp.rel(cp.SOURCE_QUEUE)
    rel = Path(configured_path)
    if rel.is_absolute() or ".." in rel.parts:
        return None
    if len(rel.parts) < 3 or rel.parts[0] != ".auto-context" or rel.parts[1] != "compile":
        return None
    root = Path(project_root).resolve()
    target = root / rel
    current = root
    for part in rel.parts[:-1]:
        current = current / part
        if current.exists() and current.is_symlink():
            return None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        resolved_parent = target.parent.resolve()
        resolved_parent.relative_to(root)
    except (OSError, ValueError):
        return None
    if target.exists() and target.is_symlink():
        return None
    return target


def _queue_lock_path(path):
    return path.with_name(f"{path.name}.lock")


def _append_jsonl(path, records):
    if not records:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = _queue_lock_path(path)
    with open(lock_path, "a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            with open(path, "a", encoding="utf-8") as handle:
                for record in records:
                    handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def compile_gate(config, accepted_triggers):
    """compile enqueue 공통 게이팅. 통과하면 compile 설정 블록, 아니면 None.

    편집 훅(`post_tool_source`)과 수동 sync(`post_sync_source`)가 같은 판정을 쓰도록
    한 곳에 둔다 — 게이팅을 두 곳에 복제하면 다음 편집에서 어긋난다.
    PostToolUse 전용 `events` 게이팅은 호출부(main)에 남긴다: sync는 hook 이벤트가 아니다.
    """
    if config.get("indexing") is not True:
        return None
    if not config.get("collections"):
        return None
    compile_cfg = config.get("compile") if isinstance(config.get("compile"), dict) else {}
    if not qmd_config.compile_active(compile_cfg):
        return None
    raw_triggers = compile_cfg.get("triggers")
    triggers = raw_triggers if isinstance(raw_triggers, list) else []
    if not any(trigger in triggers for trigger in accepted_triggers):
        return None
    return compile_cfg


def _source_record(path_value, cwd, project_root, config, engine, trigger=POST_TOOL_TRIGGER):
    source_path = Path(path_value)
    abs_path = source_path if source_path.is_absolute() else Path(cwd) / source_path
    try:
        resolved = abs_path.resolve()
    except OSError:
        return None
    if resolved.suffix.lower() != ".md":
        return None
    selected = select_collections([str(resolved)], project_root, config)
    if not selected:
        return None
    # select_collections returns at most one collection for one edited path.
    collection = next(iter(selected.keys()))
    # compile 입력 role: raw / session / source. `source`는 qmd에 등록되지 않는(=recall
    # 되지 않는) 컬렉션이지만 카드의 원천으로는 그대로 산다 — 그것이 role의 존재 이유다.
    # 판정은 config.COMPILE_SOURCE_ROLES 한 곳에서만 나온다(여집합 금지).
    roles = qmd_config.role_map(config)
    if not qmd_config.is_compile_source_collection(roles, collection):
        return None
    try:
        rel_path = resolved.relative_to(Path(project_root).resolve()).as_posix()
    except ValueError:
        return None
    if _is_denied_source_path(rel_path):
        return None
    # dot-prefix 정책은 유지하되 적용 범위를 "등록된 collection 루트 아래"로 좁힌다.
    # 루트 자체의 dot segment(예: `.nova/06_Sessions`)는 사용자가 collectionPaths에
    # 직접 적어 넣은 소스라 면제하고, 그 아래에서 새로 나타나는 dot segment
    # (`.claude`, `docs/.draft`, `.hidden.md`)는 그대로 배제한다.
    # 이 좁히기가 완화를 자동으로 봉쇄한다: collectionPaths가 `.`이면 면제되는
    # 접두부가 비어 있어 rel_path 전체가 여전히 dot 검사를 받는다.
    try:
        inner_rel = resolved.relative_to(Path(selected[collection]).resolve()).as_posix()
    except (OSError, ValueError):
        inner_rel = rel_path
    if _is_hidden_source_path(inner_rel):
        return None
    return {
        "ts": _utc_now(),
        "trigger": trigger,
        "engine": engine,
        "cwd": str(Path(project_root).resolve()),
        "source": {
            "kind": "file",
            "path": rel_path,
            "collection": collection,
        },
    }


def main():
    if os.environ.get("QMD_SANDBOX") or "--sandbox" in sys.argv:
        return 0
    raw = sys.stdin.read().strip()
    if not raw:
        return 0
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return 0
    if payload.get("hook_event_name") not in (None, "PostToolUse", "AfterTool"):
        return 0

    cwd = payload.get("cwd") or os.getcwd()
    found = qmd_config.find_project_config(cwd)
    config = found["config"]
    project_root = found["projectRoot"]
    paths = posttool.edited_paths(payload)
    from context_learning.synthetic_scope import invocation_in_generated_story
    if not invocation_in_generated_story(found, payload.get("cwd"), paths):
        return 0
    if not qmd_config.event_enabled(config, "postToolUse"):
        return 0
    compile_cfg = compile_gate(config, (POST_TOOL_TRIGGER,))
    if compile_cfg is None:
        return 0

    queue_path = _safe_queue_path(project_root, cp.rel(cp.SOURCE_QUEUE))
    if queue_path is None:
        return 0

    records = []
    seen = set()
    for edited_path in paths:
        if not isinstance(edited_path, str):
            continue
        record = _source_record(edited_path, cwd, project_root, config, _engine(payload))
        if record is None:
            continue
        key = (record["cwd"], record["source"]["path"], record["source"]["collection"])
        if key in seen:
            continue
        seen.add(key)
        records.append(record)
    queued = []
    for record in records:
        source = record.get("source") if isinstance(record.get("source"), dict) else {}
        pending = wiki_freshness.record_pending_refresh(
            Path(project_root), source.get("path", ""), record.get("engine", DEFAULT_ENGINE))
        if pending is None:
            continue
        record["pendingRefresh"] = {
            "eventId": pending["eventId"],
            "ts": pending["ts"],
            "engine": pending["engine"],
        }
        queued.append(record)
    _append_jsonl(queue_path, queued)
    return 0


if __name__ == "__main__":
    import hook_main
    sys.exit(hook_main.run(main))

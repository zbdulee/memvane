#!/usr/bin/env python3
"""Enqueue source markdown files for automatic wiki compile.

This hook-side command is intentionally silent. It records source metadata only;
it never stores source content or calls an extractor/LLM.
"""

import json
import fcntl
import hashlib
import os
import shutil
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.resolve()))
import compile_paths as cp
import config as qmd_config
import posttool
import wiki_freshness
import wiki_source_intent as intent
from collection_match import select_collections


DEFAULT_ENGINE = qmd_config.UNKNOWN_ENGINE

# 편집 훅 경로의 trigger 라벨. 수동 sync 경로는 `post_sync_source`를 쓴다(core/sync.py).
# 라벨 리터럴의 SSOT는 wiki_source_intent다(이 모듈의 이름은 기존 import 호환용 별칭).
POST_TOOL_TRIGGER = intent.POST_TOOL_TRIGGER

# compile source가 절대 될 수 없는 segment. `.auto-context`는 이 플러그인의 관리 영역
# (wiki 카드·큐·로그)이라 소스로 승격하면 카드가 자기 자신을 재컴파일해 증식한다.
# 나머지는 collectionPaths에 등록될 일이 거의 없지만 방어적으로 막는다.
DENIED_SOURCE_SEGMENTS = frozenset({".auto-context", ".git", ".hg", ".svn", "node_modules"})


# sync 경유 compile enqueue의 trigger 라벨. 편집 훅이 아니라 스냅샷 diff가 출처이므로
# `post_tool_source`로 기록하면 감사 로그에서 출처가 왜곡된다.
COMPILE_TRIGGER = intent.SYNC_TRIGGER
# `post_tool_source`만 켜 둔 기존 프로젝트에서도 git pull 구멍이 닫히도록 동등 grant로
# 인정한다. 그 trigger가 이미 표현하는 동의는 "소스 .md가 바뀌면 컴파일한다"이고, sync는
# 같은 사건을 다른 방법으로 탐지할 뿐이다(게다가 sync는 사용자가 직접 호출한다).
# 반대로 두 라벨 모두 없는 프로젝트(예: triggers=["manual"])는 그대로 skip된다.
COMPILE_ACCEPTED_TRIGGERS = intent.SYNC_ACCEPTED_TRIGGERS
# 한 번의 sync가 compile 큐에 넣을 수 있는 소스 파일 수 상한. compile worker에는 per-run
# 상한이 없어(`batch.maxItems`는 처리 시작 조건) 큐에 든 만큼 host CLI를 연속 spawn하고
# 그 토큰 비용은 사용자 계정으로 청구된다. 수백 파일 pull 뒤의 sync 한 번이 수 시간짜리
# 백그라운드 CLI 실행으로 번지지 않게 막는다. 상한을 넘긴 파일은 스냅샷을 진전시키지 않아
# 다음 sync가 다시 집어 간다(조용히 유실되지 않는다).
DEFAULT_COMPILE_MAX_FILES = 50
# queue lock 안에서 revision 키를 찾으려고 live 큐를 읽는 상한. worker가 오래 멈춰 큐가
# 이보다 커지면 키를 찾지 않고 기존처럼 append만 한다(중복은 가능, 유실은 없음).
QUEUE_SCAN_MAX_BYTES = 8 * 1024 * 1024


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
    for name, root, rel in intent.changed_source_paths(changed_files):
        record = _source_record(
            str(Path(root) / rel),
            project_root,
            project_root,
            config,
            engine,
            trigger=trigger,
        )
        if record is None:
            continue
        # hash 스캔(SessionStart·file-change)만 revision을 준다. 없는 행은 기존 bytes 그대로다.
        revision = intent.source_revision(changed_files, name, rel)
        if revision is not None:
            record[intent.REVISION_FIELD] = revision
        candidates.append((name, rel, record))
    return candidates


def _stable_revision(path):
    """Hash one eligible source before enqueue; reject a concurrent replacement."""
    before = path.stat()
    if not stat.S_ISREG(before.st_mode):
        raise OSError('sync_source_not_regular')
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        opened = os.fstat(handle.fileno())
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise OSError('sync_source_changed_during_hash')
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
        after = os.fstat(handle.fileno())
    named = path.stat()
    identity = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
    if identity(before) != identity(after) or identity(after) != identity(named):
        raise OSError('sync_source_changed_during_hash')
    return digest.hexdigest()


def enqueue_changed_sources(project_root, config, changed_files,
                            *, session_start=False):
    """sync가 감지한 변경 .md를 compile source-queue에도 넣는다.

    git pull·rebase·외부 도구 편집은 PostToolUse 훅을 타지 않아 wiki 카드가 조용히 낡는다.
    dirty-queue enqueue(컬렉션 단위)는 qmd 인덱스만 최신으로 만들 뿐이므로 이 경로가 없으면
    `wikiOnly` 프로젝트에서 낡은 카드가 유일한 recall 소스로 남는다.

    LLM은 호출하지 않는다 — 메타데이터만 append하고 실제 compile은 기존 backend manager
    kick 경로(편집 훅의 kick-wiki-compile, SessionStart의 --flush)가 처리한다.
    """
    accepted, trigger = intent.trigger_policy(session_start)
    compile_cfg = compile_gate(config, accepted)
    if compile_cfg is None:
        return intent.enqueue_result("compile_disabled")
    queue_path = _safe_queue_path(project_root, cp.rel(cp.SOURCE_QUEUE))
    if queue_path is None:
        return intent.enqueue_result("queue_path_rejected")
    candidates = compile_candidates(project_root, config, compile_cfg, changed_files,
        trigger=trigger)
    if not candidates:
        return intent.enqueue_result("no_sources")
    cap = compile_max_files()
    # Manual sync's stat-only scan has no revision. Hash only rows eligible for
    # this wake, so an enqueue-before-snapshot failure can recognize a still-live
    # same-revision row on retry without hashing every file in a large pull.
    if not session_start:
        for _, _, record in candidates[:cap]:
            if intent.revision_key(record) is None:
                path = Path(project_root) / record['source']['path']
                record[intent.REVISION_FIELD] = _stable_revision(path)
    # 상한 값(env)은 여기서 읽고 분할 규칙만 순수 계획에 맡긴다.
    # Unhashed legacy rows are still kept: an old job has no proof that its
    # source is the same revision, so never silently suppress it.
    if any(intent.revision_key(record) for _, _, record in candidates):
        plan = _append_unqueued(queue_path, candidates, cap)
    else:
        plan = intent.plan_enqueue(candidates, cap)
        _append_jsonl(queue_path, plan["records"])
    if plan["records"] or plan["result"].get("wakePending"):
        import wiki_selected_laya
        wiki_selected_laya.request_compile(Path(project_root),
            wiki_selected_laya.selected(Path(project_root)))
    return plan["result"]



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


def _write_jsonl(path, records):
    with open(path, "a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def _append_jsonl(path, records):
    if not records:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = _queue_lock_path(path)
    with open(lock_path, "a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            _write_jsonl(path, records)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _queued_revision_keys(path, limit=None):
    """Revision keys of rows still in the live queue (not claimed/drained ones).

    An unreadable queue, or one larger than `limit` bytes
    (`QUEUE_SCAN_MAX_BYTES`), yields no keys: at most `limit + 1` bytes are
    read, and the rows are appended as before the scan existed, so the failure
    mode is a duplicate, never a lost intent or an unbounded read under the lock.
    """
    limit = QUEUE_SCAN_MAX_BYTES if limit is None else limit
    try:
        with open(path, "rb") as handle:
            data = handle.read(limit + 1)
    except OSError:
        return set()
    if len(data) > limit:
        return set()
    keys = set()
    for line in data.decode("utf-8", errors="replace").splitlines():
        try:
            key = intent.revision_key(json.loads(line))
        except ValueError:
            continue
        if key is not None:
            keys.add(key)
    return keys


def _append_unqueued(path, candidates, cap):
    """Skip rows still queued under the same revision key, then cap and append.

    Everything happens under the one queue lock the worker's claim/requeue
    also takes, so a skip decision and the append see the same queue. Skipped
    candidates are neither queued nor deferred, and when all are skipped the
    result is the usual `no_sources`. They still use cap slots: they will be
    compiled by the same wake, so only `cap - skipped` fresh rows are appended
    and the rest is deferred (its snapshot entries stay behind for the next
    run). Otherwise a retry after a snapshot failure would add the tail and
    one wake would compile more than `cap` paid sources.

    A skipped row is still live work (e.g. a retry after the snapshot write
    failed), so the result gains internal `wakePending: True` and the caller
    can still kick the compile worker. It is absent when nothing was skipped
    and is not part of any status/sync output.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(_queue_lock_path(path), "a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            fresh = intent.without_queued(candidates, _queued_revision_keys(path))
            plan = intent.plan_enqueue(fresh, max(0, cap - (len(candidates) - len(fresh))))
            if plan["records"]:
                _write_jsonl(path, plan["records"])
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    if len(fresh) < len(candidates):
        plan["result"]["wakePending"] = True
    return plan


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
    record = {
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
    import wiki_selected_laya
    selected_route = wiki_selected_laya.selected(Path(project_root))
    if selected_route is not None:
        record['selectedRoute'] = wiki_selected_laya.route_identity(selected_route)
    return record


def enqueue_payload(payload):
    """Hook payload entry for hosts that already parsed and proved the event."""
    if not isinstance(payload, dict):
        return {"queued": 0, "reason": "invalid_payload"}
    if payload.get("hook_event_name") not in (None, "PostToolUse", "AfterTool"):
        return {"queued": 0, "reason": "invalid_event"}

    cwd = payload.get("cwd") or os.getcwd()
    found = qmd_config.find_project_config(cwd)
    config = found["config"]
    project_root = found["projectRoot"]
    paths = posttool.edited_paths(payload)
    from context_learning.synthetic_scope import invocation_in_generated_story
    if not invocation_in_generated_story(found, payload.get("cwd"), paths):
        return {"queued": 0, "reason": "outside_scope"}
    if not qmd_config.event_enabled(config, "postToolUse"):
        return {"queued": 0, "reason": "event_disabled"}
    compile_cfg = compile_gate(config, (POST_TOOL_TRIGGER,))
    if compile_cfg is None:
        return {"queued": 0, "reason": "compile_disabled"}

    queue_path = _safe_queue_path(project_root, cp.rel(cp.SOURCE_QUEUE))
    if queue_path is None:
        return {"queued": 0, "reason": "queue_path_rejected"}

    records = []
    seen = set()
    for edited_path in paths:
        if not isinstance(edited_path, str):
            continue
        record = _source_record(edited_path, cwd, project_root, config, _engine(payload))
        if record is None:
            continue
        key = intent.intent_identity(record)
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
    if queued:
        import wiki_selected_laya
        wiki_selected_laya.request_compile(Path(project_root),
            wiki_selected_laya.selected(Path(project_root)))
    return {"queued": len(queued), "reason": "queued" if queued else "no_sources"}


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
    enqueue_payload(payload)
    return 0


if __name__ == "__main__":
    import hook_main
    sys.exit(hook_main.run(main))

#!/usr/bin/env python3
"""Machine-review (auto-verify) worker for generated wiki cards.

Compile writes one ``verify_jobs`` row per review into ``wiki-pipeline.sqlite3``:
``kind: review`` for a first card it already published as ``generated``, and
``kind: replace`` whose ``pending_blob`` is the replacement of a visible card.
Each run recovers interrupted publishes and dead claims, claims one budget of
rows, and for each row: rechecks the target and source revision, pays for one
verdict through the host-CLI adapter pool (payload ``{"task": "verify"}``),
records it on the row, publishes, and closes the row.

  pass         -> the stamped image (status: verified + proof fields) replaces the
                  reviewed bytes by one exchange that keeps a concurrent human edit
  fail         -> the card stays ``generated``; a replacement image is dropped
  inconclusive -> the same as fail

The verifier never deletes a card. A row ends ``done`` with a reason, stays
``queued`` when nothing was decided (engine missing, cooling, timeout), or is
``dropped`` after a fourth dead claim. A verdict recorded before a crash is
reused, so a resumed row never pays twice for one verdict.

The source documents replayed against a card are the ones the row knows, not
the ones the model listed: candidate["sources"] is extractor output and
load_sources reads only the first MAX_SOURCES entries, so model-chosen decoys
could otherwise become the entire evidence base for a ``verified`` card.

A pass may also stamp ``sourceSpans``: line locations of the verifier's own exact
quotes, located in code against the revision-checked snapshot bytes
(source_spans.py). They are written with the status in one publish, carry no
quote text, and only locate cited excerpts; they are not per-claim proof.

Runs from wiki_compile_worker under ``.compile-run.lock``. Rows and logs never
store source bodies.
"""

from __future__ import annotations
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
import sqlite3
import stat
import sys
from pathlib import Path
import uuid

from memvane.kernel.atomic_rename import fsync_dir, canonical_json_bytes
from memvane.kernel.paths import now_iso
from memvane.wiki.verify.api import supersede_compile_intent as _supersede_compile_intent, compile_intent_id as _compile_intent_id
from memvane.wiki.workspace import api as workspace_files
from memvane.wiki.card import types as wiki_markers
from memvane.wiki.workspace import api as cp, types as ct
from memvane.settings import api as qmd_config, types as settings_types
from memvane.wiki.engines import api as engines
from memvane.wiki.card import api as wiki_freshness
from memvane.wiki.card import api as wsm
from memvane.wiki.card import api as source_spans
from memvane.wiki.card.api import card_sources_allowed, parse_frontmatter
from memvane.wiki.card import api as yaml_scalars
from memvane.wiki.workspace.api import read_queue
from memvane.wiki.verify.review_queue import candidate_matches_job
from memvane.wiki.verify.verification_stamp import render_verification_text
# 경로는 전부 `compile_paths` 상수 테이블에서 온다.
VERIFY_QUEUE_DEFAULT = cp.rel(ct.VERIFY_QUEUE)
VERDICT_VALUES = {"pass", "fail", "inconclusive"}
MAX_SOURCES = 3
# "호출은 됐지만 출력이 판정으로 쓸 수 없다" — 같은 입력에서 재현되는 설정 오류이므로
# 후보가 다 떨어지면 종점이 있다(defer_or_drop). transient(timeout·실행 실패)와 구분된다.
UNUSABLE_OUTPUT_REASONS = {"invalid_extractor_json", "invalid_verdict"}
# 검수 엔진 정책. 카드를 만든 엔진이 검수한다(교차 엔진 선택지는 settings로 고를 수 없다).
# 판정 행의 `crossEngine` 필드는 이 값을 그대로 남긴다.
CROSS_ENGINE_POLICY = "off"


def extractor_builtins(compile_cfg: dict) -> list[str]:
    raw = compile_cfg.get("extractor")
    extractor = raw if isinstance(raw, dict) else {}
    return [e for e in (extractor.get("builtins") or []) if isinstance(e, str) and e]


def plan_verify_attempts(
    compile_cfg: dict, vcfg: dict, producing: str, cooled=()
) -> tuple[list[dict], str, str]:
    """Verify attempt → ([{engine, argv, mode, key}], crossEngine policy, empty reason).

    The engine that wrote the card verifies it in a separate call (`mode: self`).
    `producing` must resolve to a per-engine argv from `compile.extractor.builtins` or
    `compile.verify.builtins`; the `"unknown"` sentinel (config.UNKNOWN_ENGINE, what
    enqueue writes when the host is unknown) and an unresolvable label defer with
    `producing_engine_unavailable` instead of paying another installed CLI.

    `cooled` are cooldown keys whose last paid call failed (non-127); a cooled producer
    defers with `engines_cooling` until its cooldown expires, so a failing engine is
    not re-billed every run.
    """
    builtins = extractor_builtins(compile_cfg)
    builtins += [e for e in (vcfg.get("builtins") or []) if isinstance(e, str) and e]
    primary = None
    if producing and producing != settings_types.UNKNOWN_ENGINE:
        primary = engines.resolve_extractor_argv(compile_cfg, producing, builtins=builtins)
    if primary is None:
        return [], CROSS_ENGINE_POLICY, "producing_engine_unavailable"
    if producing in set(cooled):
        return [], CROSS_ENGINE_POLICY, "engines_cooling"
    return ([{"engine": producing, "argv": primary,
              "mode": settings_types.VERIFIED_MODE_SELF, "key": producing}],
            CROSS_ENGINE_POLICY, "")


def verify_cfg_of(compile_cfg: dict) -> dict:
    raw = compile_cfg.get("verify")
    return raw if isinstance(raw, dict) else {}


# Verify cooldowns are per engine (``verify_engine`` rows), separate from compile:
# a preferred engine that keeps failing (an unauthenticated CLI) degrades to the
# next candidate instead of stopping every review of the project.
def cooling_engines(root: Path) -> set:
    from memvane.wiki.pipeline import api as pipeline
    return pipeline.cooling_keys(root, 'verify_engine')


def set_engine_cooldown(root: Path, key: str, seconds: int) -> bool:
    """이 후보를 seconds 동안 후보에서 제외한다. 반환값은 "기록이 남았는가"다.

    **호출자는 False를 반드시 표면화해야 한다**(working-rules 1) — 기록이 없으면 다음 run이
    같은 엔진을 다시 부르고 degrade가 일어나지 않는다.
    """
    from memvane.wiki.pipeline import api as pipeline
    return pipeline.set_cooldown(root, 'verify_engine', seconds, key)


def log_verdict(log, payload: dict) -> None:
    # Log rows only (working-rules 3): no decision reads the verify log.
    log.append(payload)


def reindex_wiki(root: Path, config: dict) -> None:
    collection, collection_path = cp.find_wiki_collection(config)
    if collection and collection_path:
        from memvane.wiki.workspace import api as wiki_selected_laya
        selected = wiki_selected_laya.selected(root)
        if selected is not None:
            wiki_selected_laya.request_sync(root, selected)


def wake_pending_retirement(root: Path) -> None:
    """Recheck a held deletion only after a successful replacement review."""
    from memvane.wiki.workspace import api as wiki_selected_laya
    selected = wiki_selected_laya.selected(root)
    if selected is None:
        return
    queue = cp.safe_queue_path(root, cp.rel(ct.SOURCE_QUEUE))
    if queue is None:
        return
    try:
        pending = read_queue(queue)
    except OSError:
        return
    if any(isinstance(job, dict) and job.get('sourceDelete') is True
           and job.get('sharedEnqueued') is True for _, job in pending):
        wiki_selected_laya.request_compile(root, selected)


def load_sources(
    root: Path, job: dict, max_chars: int, initial_sources: list[dict] | None = None
) -> list[dict]:
    """검증에 쓸 원문. **잡의 실제 소스가 모델 제공 항목에 밀려나지 않는다.**

    `job["sources"]`는 extractor(모델) 출력이 섞인 목록이고 여기는 앞에서 `MAX_SOURCES`
    개만 읽는다. 모델 목록이 앞에 오던 동안에는 모델이 decoy 3개를 내면 카드가 **실제
    원문 없이** 검증돼 `verified`가 됐다 — `recallVerifiedOnly` 기본값에서 그것은
    "인용 가능한 캐논"이므로 검증 전제 자체가 무력화된다.

    그래서 `job["authoritativeSources"]`(큐 잡이 아는 실제 소스, `wiki_compile`이 기록)를
    **먼저** 읽고 그 항목들은 `MAX_SOURCES` 상한으로 **자르지 않는다**. 모델 항목은 남는
    예산만 채운다. 0.x 잡·수동 compile 경로에는 이 키가 없어 기존 동작 그대로다.

    같은 경로는 한 번만 읽는다(권위 항목은 `sources`에도 들어 있다).
    """
    raw_auth = job.get("authoritativeSources")
    authoritative = raw_auth if isinstance(raw_auth, list) else []
    raw_model = job.get("sources")
    model = raw_model if isinstance(raw_model, list) else []
    loaded = list(initial_sources or [])
    seen: set[str] = {
        source.get("path") for source in loaded
        if isinstance(source, dict) and isinstance(source.get("path"), str)
    }
    # 권위 항목이 전부 앞에 오므로, 예산 소진 판정을 만나는 것은 모델 항목뿐이다.
    for src, forced in [(s, True) for s in authoritative] + [(s, False) for s in model]:
        if not forced and len(loaded) >= MAX_SOURCES:
            break
        if not isinstance(src, dict) or src.get("kind") != "file":
            continue
        rel = src.get("path")
        if not isinstance(rel, str) or not rel or rel in seen:
            continue
        path = (root / rel).resolve()
        try:
            path.relative_to(root)
        except ValueError:
            continue
        # 읽기 실패해도 같은 경로를 다시 시도하지 않는다(결과가 같고 I/O만 늘어난다).
        seen.add(rel)
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        loaded.append({
            "path": rel,
            "content": text[:max_chars],
            "truncated": len(text) > max_chars,
        })
    return loaded


def snapshot_revision_sources(
    root: Path, config: dict, job: dict, max_chars: int
) -> tuple[list[dict] | None, dict | None]:
    """Load job-owned sources from the exact snapshots whose hashes were checked.

    ``None, None`` is the narrow compatibility path for a queue row predating
    ``sourceRevisions``.  Once the field exists, invalid records, unsafe paths,
    failed snapshots, and hash mismatches all fail closed before a verifier call.
    """
    loaded, freshness, _evidence = snapshot_revision_evidence(root, config, job, max_chars)
    return loaded, freshness


def snapshot_revision_evidence(
    root: Path, config: dict, job: dict, max_chars: int
) -> tuple[list[dict] | None, dict | None, dict]:
    """``snapshot_revision_sources`` plus the full snapshot bytes per revision path.

    The third value feeds ``source_spans.derive_spans``: the same stable bytes
    whose hash matched the job revision, and how many characters of them the
    verifier was shown.  It is keyed only by worker-owned revision paths, so a
    model-chosen ``sourcePath`` cannot add evidence.  It never leaves the worker.
    """
    if "sourceRevisions" not in job:
        return None, None, {}
    raw_revisions = job.get("sourceRevisions")
    if not isinstance(raw_revisions, list) or not raw_revisions:
        return [], {"state": wiki_markers.UNKNOWN, "reason": "missing_source_revisions"}, {}

    from memvane.wiki.card import api as wiki_card

    allow_roots = wsm.allow_roots_of(config)
    loaded = []
    evidence: dict[str, dict] = {}
    for raw in raw_revisions:
        expected = yaml_scalars.normalize_source_revision(raw)
        if expected is None:
            return [], {"state": wiki_markers.UNKNOWN, "reason": "invalid_source_revision"}, {}
        resolved, reason = wiki_card.resolve_existing_source(expected["path"], root, allow_roots)
        if resolved is None:
            state = wiki_markers.STALE if reason == "missing" else wiki_markers.UNKNOWN
            return [], {"state": state, "reason": reason or "source_unavailable"}, {}
        snapshot = wiki_freshness.snapshot_bytes(resolved)
        if snapshot is None:
            return [], {"state": wiki_markers.UNKNOWN, "reason": "source_snapshot_failed"}, {}
        revision, data = snapshot
        if revision["sha256"] != expected["sha256"]:
            return [], {"state": wiki_markers.STALE, "reason": "content_hash_mismatch"}, {}
        bounded = engines.decode_source_bounded(data, max_chars)
        if bounded is None:
            return [], {"state": wiki_markers.UNKNOWN, "reason": "source_decode_failed"}, {}
        content, truncated = bounded
        loaded.append({
            "path": expected["path"],
            "content": content,
            "truncated": truncated,
        })
        # A path listed twice cannot name one unambiguous snapshot to locate in.
        item = source_spans.evidence_item(expected["path"], expected["sha256"], data, len(content))
        if expected["path"] in evidence or item is None:
            evidence[expected["path"]] = {}
        else:
            evidence[expected["path"]] = item
    return loaded, {"state": wiki_markers.FRESH, "checked": len(loaded)}, evidence


def base_record(job: dict) -> dict:
    """Before a verifier is chosen, identify only the producing engine."""
    return {"ts": now_iso(), "targetPath": job.get("targetPath", ""), "producedBy": job.get("engine", "")}


def _provenance_gate(job, checkout):
    """Fresh checkout identity only; source content was separately rechecked."""
    spec = job.get('sourceFingerprints')
    if checkout is None or not isinstance(spec, dict):
        return None, None
    try:
        capture = checkout.capture()
    except (OSError, RuntimeError, ValueError, sqlite3.Error):
        return None, 'provenance_checkout_unavailable'
    rows = spec.get('fingerprints')
    if (not isinstance(rows, list) or not rows
            or spec.get('viewId') != capture['viewId']
            or any(not isinstance(row, dict)
                   or row.get('worktree_id') != capture['worktreeId']
                   or row.get('branch_key') != capture['branchKey'] for row in rows)):
        return None, 'provenance_checkout_changed'
    return capture, None


def _write_compile_intent(checkout, result, verdict_digest, result_digest):
    from memvane.journal import api as ledger
    from memvane.journal.types import CutoverBlocked
    state = ledger.reader_state(Path(checkout.decision['journalPath']).parent)
    directory = state / 'provenance-intents'
    try:
        directory.mkdir(mode=0o700)
        fsync_dir(state)
    except FileExistsError:
        pass
    info = directory.lstat()
    if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700):
        raise ValueError('provenance_intent_directory_unsafe')
    identity = _compile_intent_id(result)
    path = directory / ('compile-' + identity + '.json')
    intent = {'kind': 'compile', **result, 'verdict_digest': verdict_digest,
              'result_digest': result_digest}
    try:
        ledger.ledger_write(path, intent, replace=False)
    except CutoverBlocked as exc:
        if str(exc) != 'cutover_record_exists' or ledger.ledger_private_file(path) != canonical_json_bytes(intent):
            raise
    return path


def defer_or_drop(
    root: Path, vcfg: dict, job: dict, attempt: dict, has_more: bool,
    provenance: dict, reason: str, log_path,
) -> tuple[bool, bool]:
    """유료 호출을 소진했지만 판정을 얻지 못한 경우의 유일한 처리 경로.

    어느 사유든 이 후보를 식히고(다음 run이 다음 후보로 degrade) 잡을 보존한다. 갈리는 것은
    **종점의 유무**다:
      - timeout·실행 실패는 진짜 transient(부하·네트워크·일시적 CLI 상태)라 기존대로 **항상
        보존**한다. 여기서 폐기하면 그 카드는 영원히 검수되지 않는다.
      - `invalid_extractor_json`/`invalid_verdict`(exit 0 + 쓰레기 stdout)는 같은 입력에서
        재현되는 **설정 오류**다. 남은 후보가 있으면 degrade하고, 후보가 다 떨어지면 폐기한다 —
        보존하면 cooldown 만료마다 전 후보를 다시 호출하는 **영구 과금 루프**가 되고, 그것은
        이 저장소가 억제 마커로 반복해서 닫아 온 클래스다. 폐기의 대가는 카드가 `generated`로
        남는 것뿐이다(삭제되지 않으며 소스를 고치면 재컴파일→재검증이 다시 열린다).
        0.x는 이 사유를 **첫 후보에서 즉시 폐기**했으므로 degrade가 순증이다.
        builtin adapter가 내는 쓰레기 stdout 은 `lib.emit_verdict`가 exit 1로 걸러 비127
        경로로 흐르지만, **근거 없는 `pass`는 그 검사를 통과하므로**(emit 은 verdict 값만
        본다) builtin 경로에서도 이 사유에 도달한다.

    cooldown 쓰기 실패는 **반드시 표면화한다**: 그 기록이 없으면 다음 run이 같은 후보를 다시
    부르고 degrade가 일어나지 않는다(= MAJOR 1 영구 정지의 재발). 조용히 넘기면 진단 불가다.
    """
    if reason == 'resource_deferred':
        log_verdict(log_path,{**base_record(job),**provenance,'result':'deferred','reason':reason})
        return False, True
    cooled_ok = set_engine_cooldown(root, attempt["key"], int(vcfg.get("cooldownSeconds", 600) or 600))
    terminal = reason in UNUSABLE_OUTPUT_REASONS and not has_more
    row = {
        **base_record(job), **provenance,
        "result": "skipped" if terminal else "deferred",
        "reason": reason, "cooledKey": attempt["key"],
    }
    if not cooled_ok:
        # 식힘 기록이 남지 않았다 → 다음 run이 같은 후보를 다시 부른다(정지 재발 가능).
        row["cooldownWriteFailed"] = True
    log_verdict(log_path, row)
    return (True, False) if terminal else (False, True)


def _card_locks(root: Path):
    @contextmanager
    def held():
        with cp.card_write_lock(root), workspace_files.mutation_lock(root):
            yield
    return held


def _wiki_root(root: Path, config: dict) -> Path:
    return (root / config.get("wikiPath", settings_types.WIKI_REL)).resolve()


def _live_target(root: Path, wiki_root: Path, rel) -> Path | None:
    """The row's visible card path, refused when it leaves the wiki or is hidden."""
    if not isinstance(rel, str) or not rel or Path(rel).is_absolute():
        return None
    live = root / rel
    if live.is_symlink():
        return None
    try:
        inside = live.resolve().relative_to(wiki_root)
    except ValueError:
        return None
    if any(part.startswith('.') for part in inside.parts):
        return None
    return live


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _live_sha(path: Path) -> str | None:
    try:
        return _sha(path.read_bytes())
    except FileNotFoundError:
        return None


def card_fields(text: str) -> tuple[dict, str, str]:
    """(frontmatter, status, managed block sourceHash) of card text."""
    meta, ok = parse_frontmatter(text)
    if not ok:
        meta = {}
    status = str(meta.get("status") or "").strip() or "generated"
    match = wiki_markers.AUTO_START_RE.search(text)
    return meta, status, (match.group(1) if match else "")


def source_freshness(root: Path, config: dict, job: dict) -> dict | None:
    """Check row-owned provenance; a row without ``sourceRevisions`` has none."""
    if "sourceRevisions" not in job:
        return None
    return yaml_scalars.check_card(
        {"sourceRevisions": job.get("sourceRevisions")}, root, yaml_scalars.allow_roots_of(config))


def _excluded(selected_route, text: str, job: dict) -> bool:
    """An old row may outlive a newly selected source-folder policy.

    Do not send an excluded raw source or a mixed-source card to a host model;
    the generated card stays recoverable and unverified.
    """
    if selected_route is None:
        return False
    from memvane.settings.api import excluded_source
    skips = selected_route[0]['recallSettings']['skipPaths']
    if not skips:
        return False
    revisions = job.get('sourceRevisions')
    sources = job.get('sources')
    normalized = ([yaml_scalars.normalize_source_revision(item) for item in revisions]
                  if isinstance(revisions, list) else [])
    return (not card_sources_allowed(text, skips) or not revisions
            or any(item is None or excluded_source(item['path'], skips) for item in normalized)
            or not isinstance(sources, list)
            or any(not isinstance(item, dict) or
                   (item.get('kind') == 'file' and
                    (not isinstance(item.get('path'), str) or excluded_source(item['path'], skips)))
                   for item in sources))


def _paid_verdict(root: Path, config: dict, compile_cfg: dict, vcfg: dict, job: dict,
                  text: str, log_path):
    """One paid verification of ``text``.

    Returns ``('verdict', recorded)`` or a closing ``(state, reason, processed)``.
    """
    max_chars = int(compile_cfg.get("maxSourceChars", 12000) or 12000)
    snapshot_sources, freshness, span_evidence = snapshot_revision_evidence(root, config, job, max_chars)
    if freshness is not None and freshness.get("state") != wiki_markers.FRESH:
        stale = freshness.get("state") == wiki_markers.STALE
        log_verdict(log_path, {**base_record(job), "result": "skipped" if stale else "deferred",
                               "reason": "source_changed_before_verify",
                               "freshnessReason": freshness.get("reason", "")})
        # A newer revision has its own refresh event and compile; this row is spent.
        return ("done", "stale_revision", True) if stale else ("queued", "source_unknown", False)
    sources = load_sources(root, job, max_chars, initial_sources=snapshot_sources)
    if not sources:
        # 원문 없이는 대조 불가 — generated로 남겨 미검수 배지가 유지되게 한다. 원문 소실
        # 카드의 보관 후 삭제는 SessionStart `wiki_lifecycle`이 기록된 `sourceRevisions`로 판정한다.
        log_verdict(log_path, {**base_record(job), "result": "skipped", "reason": "source_missing"})
        return "done", "source_missing", True

    producing = job.get("engine") if isinstance(job.get("engine"), str) else ""
    cooled = cooling_engines(root)
    attempts, cross_mode, empty_reason = plan_verify_attempts(compile_cfg, vcfg, producing, cooled)
    if not attempts:
        log_verdict(log_path, {**base_record(job), "result": "deferred", "reason": empty_reason,
                               "crossEngine": cross_mode, "enginesCooling": sorted(cooled)})
        # 생산 엔진 해석 불가·식힘 중은 설치/시간의 함수이므로 행을 보존한다.
        return "queued", empty_reason, False

    timeout = int(vcfg.get("timeout", 120) or 120)
    payload = {
        "task": "verify",
        "cwd": str(root),
        "engine": attempts[0]["engine"],
        "card": {"path": job["targetPath"], "content": text},
        "sources": sources,
        "timeout": timeout,
        "_qmd": {"reasoningEffort": {}},
    }
    # 실패 분류 경계 — (1) 한 run 안에서 같은 카드에 **유료 호출은 1회**다. 다음 후보로
    # 넘어가는 조건은 127(CLI 부재, 토큰 0)뿐이다. (2) run을 넘어가면 실패한 후보를 엔진
    # 단위로 식혀 다음 run의 planner가 다음 후보를 시도한다. 어느 경로도 verdict를 만들지
    # 않으므로 transient가 판정으로 흐르지 않는다.
    attempted: list[str] = []
    attempt = attempts[0]
    has_more = False
    parsed = reason = returncode = None
    for index, attempt in enumerate(attempts):
        payload["engine"] = attempt["engine"]
        payload["_qmd"]["reasoningEffort"] = {
            "requested": qmd_config.resolve_reasoning_effort(compile_cfg, attempt["engine"], "verify"),
            "capabilityDeclared": attempt["argv"] == engines.builtin_adapter_argv(attempt["engine"]),
        }
        attempted.append(attempt["engine"])
        parsed, reason, returncode = engines.run_extractor(attempt["argv"], payload, timeout, root)
        if returncode != 127:
            has_more = index + 1 < len(attempts)
            break
    if returncode == 127:
        log_verdict(log_path, {**base_record(job), "result": "deferred", "reason": "extractor_unavailable",
                               "crossEngine": cross_mode, "enginesAttempted": attempted,
                               "enginesCooling": sorted(cooled)})
        return "queued", "extractor_unavailable", False
    engine = attempt["engine"]
    verified_mode = attempt["mode"]
    # 실패 행에는 `attemptedMode`만 쓴다 — 실패한 시도의 mode는 달성한 것이 아니다.
    requested = payload["_qmd"]["reasoningEffort"]
    provenance = {
        "engine": engine,
        "attemptedMode": verified_mode,
        "crossEngine": cross_mode,
        "enginesAttempted": attempted,
        "enginesCooling": sorted(cooled),
        "_qmd": {"reasoningEffort": engines.reasoning_effort_audit(
            parsed.get("_qmd", {}).get("reasoningEffort")
            if isinstance(parsed, dict) and isinstance(parsed.get("_qmd"), dict) else {},
            requested.get("requested"),
            capability_declared=requested.get("capabilityDeclared") is True,
        )},
    }
    if reason:
        processed, preserve = defer_or_drop(root, vcfg, job, attempt, has_more, provenance, reason, log_path)
        return ("queued", reason, False) if preserve else ("done", reason, processed)
    verdict = parsed.get("verdict") if isinstance(parsed, dict) else None
    claims = parsed.get("claims") if isinstance(parsed, dict) and isinstance(parsed.get("claims"), list) else []
    # **`pass`는 근거를 요구한다.** `pass`가 `status: verified`의 유일한 입구이고 근거 0인
    # `pass`는 "무관한 요약이 trusted 카드가 된다"다. 퇴화한 출력은 판정이 아니라
    # `invalid_verdict`이며 카드를 건드리지 않는다. `supported`/`quote` 값까지는 보지 않는다
    # (프롬프트가 모델에 맡긴 규칙을 재구현하는 것이고 거짓말하는 adapter를 막지 못한다).
    if verdict not in VERDICT_VALUES or (
            verdict == "pass" and not any(isinstance(item, dict) and item for item in claims)):
        processed, preserve = defer_or_drop(root, vcfg, job, attempt, has_more, provenance,
                                            "invalid_verdict", log_path)
        return ("queued", "invalid_verdict", False) if preserve else ("done", "invalid_verdict", processed)
    reasons = parsed.get("reasons") if isinstance(parsed.get("reasons"), list) else []
    reasons = [str(item)[:200] for item in reasons[:5]]
    # 기존 pass claims의 정확한 인용만 코드로 위치시킨다(추가 유료 호출 없음).
    spans = source_spans.derive_spans(claims, span_evidence) if verdict == "pass" else []
    return "verdict", {"verdict": verdict, "claims": claims, "reasons": reasons, "spans": spans,
                       "engine": engine, "mode": verified_mode, "provenance": provenance}


def _publish_pass(root, config, job, live, wiki_root, text, expected, recorded, record, log_path, checkout,
                  remember=None):
    """Publish the stamped bytes; ``remember`` keeps them on the row so a resumed row
    publishes (or recognizes) the very same bytes."""
    engine, mode = recorded["engine"], recorded["mode"]
    bound = source_spans.bind_to_card(recorded["spans"], text) if recorded["spans"] else []
    rendered = recorded.get("rendered")
    if not isinstance(rendered, str):
        rendered = render_verification_text(text, "verified", engine, mode, yaml_scalars.card_body_hash(text),
                                            bound or None, datetime.now(timezone.utc))
        if rendered is None:
            log_verdict(log_path, {**record, "result": "stamp_failed", "stampStatus": "verified"})
            return "done", "stamp_render_failed", True
        if remember is not None:
            # 3번 기준: 실패하면 재개가 새 타임스탬프로 다시 렌더할 뿐이고 유료 호출은 없다.
            remember({**recorded, "rendered": rendered})
    intent_path = None
    if checkout is not None and isinstance(job.get('sourceFingerprints'), dict):
        capture, gate_reason = _provenance_gate(job, checkout)
        if capture is not None:
            from memvane.store import api as values
            result = dict(cardSha=_sha(rendered.encode('utf8')), viewId=capture['viewId'],
                          targetPath=live.resolve().relative_to(wiki_root).as_posix(),
                          expectedTargetHash=job.get('expectedTargetHash') if job.get('kind') == 'replace' else None,
                          sourceFingerprints=job['sourceFingerprints'], sourceSpans=bound,
                          verifiedMode=mode, verifyEngine=engine, producingEngine=job.get('engine', ''))
            digests = values.provenance_digests(
                {'verdict': 'pass', 'claims': recorded["claims"], 'reasons': recorded["reasons"]}, result)
            if digests is not None:
                try:
                    intent_path = _write_compile_intent(checkout, result, *digests)
                except (OSError, RuntimeError, ValueError):
                    log_verdict(log_path, {**record, 'result': 'deferred',
                                           'reason': 'provenance_intent_write_failed'})
                    return "queued", "provenance_intent_write_failed", False
            else:
                gate_reason = 'provenance_value_invalid'
        if gate_reason:
            log_verdict(log_path, {**base_record(job), 'result': 'skipped', 'reason': gate_reason})
    from memvane.wiki.pipeline import api as pipeline
    try:
        outcome = pipeline.publish_card(root, wiki_root, live.resolve().relative_to(wiki_root), rendered,
                                        expected_target_sha=expected,
                                        lock_context=_card_locks(root))
        state = outcome.state
    except (OSError, ValueError, sqlite3.Error):
        state = 'write_failed'
    if state != 'published':
        if intent_path is not None:
            _supersede_compile_intent(intent_path)
        if state in ('write_failed', 'aborted'):
            # The verdict is recorded on the row; the retry publishes without paying again.
            log_verdict(log_path, {**record, 'result': 'publish_pending', 'reason': state})
            return "queued", "publish_" + state, False
        log_verdict(log_path, {**record, "result": "skipped", "reason": "changed_during_verify"})
        return "done", "changed_during_verify", True
    reindex_wiki(root, config)
    wake_pending_retirement(root)
    log_verdict(log_path, {**record, "result": "verified", "sourceSpans": len(bound)})
    return "done", "verified", True


def process_row(root: Path, config: dict, compile_cfg: dict, vcfg: dict, store, token: str,
                row: dict, log_path, selected_route) -> tuple[str, str, bool]:
    """Settle one claimed row. Returns ``(state, reason, processed)`` for ``finish_verify_job``."""
    job = row['payload']
    if not card_sources_allowed(job, report=True):
        return "queued", "source_policy_incompatible", False
    wiki_root = _wiki_root(root, config)
    live = _live_target(root, wiki_root, job.get("targetPath"))
    if live is None:
        log_verdict(log_path, {**base_record(job), "result": "skipped", "reason": "unsafe_target_path"})
        return "done", "unsafe_target_path", True
    recorded = job.get('_verdict')
    if not (isinstance(recorded, dict) and recorded.get('verdict') in VERDICT_VALUES):
        recorded = None
    if (recorded is not None and isinstance(recorded.get('rendered'), str)
            and _live_sha(live) == _sha(recorded['rendered'].encode('utf-8'))):
        # The publish landed before the run ended; settle the row without another write.
        reindex_wiki(root, config)
        wake_pending_retirement(root)
        log_verdict(log_path, {**base_record(job), "result": "verified_replay"})
        return "done", "verified", True
    if job.get('kind') == 'replace':
        blob = store.job_blob('verify', row['jobId'])
        try:
            text = blob.decode('utf-8') if blob is not None else None
        except UnicodeError:
            text = None
        if text is None or not candidate_matches_job(text, job):
            log_verdict(log_path, {**base_record(job), "result": "skipped", "reason": "replacement_invalid"})
            return "done", "replacement_invalid", True
        expected = job.get('expectedTargetHash')
        if _live_sha(live) != expected:
            log_verdict(log_path, {**base_record(job), "result": "skipped", "reason": "changed_before_verify"})
            return "done", "changed_before_verify", True
    else:
        try:
            data = live.read_bytes()
            text = data.decode('utf-8')
        except FileNotFoundError:
            log_verdict(log_path, {**base_record(job), "result": "skipped", "reason": "card_missing"})
            return "done", "card_missing", True
        except UnicodeError:
            log_verdict(log_path, {**base_record(job), "result": "skipped", "reason": "not_generated"})
            return "done", "not_generated", True
        except OSError:
            return "queued", "card_unreadable", False
        expected = _sha(data)
        if job.get('expectedTargetHash') not in (None, expected):
            log_verdict(log_path, {**base_record(job), "result": "skipped", "reason": "changed_before_verify"})
            return "done", "changed_before_verify", True
    if not card_sources_allowed(text, report=True):
        return "queued", "source_policy_incompatible", False
    meta, status, block_hash = card_fields(text)
    if status != "generated" or meta.get("createdBy") not in wiki_markers.CREATED_BY_NAMES:
        log_verdict(log_path, {**base_record(job), "result": "skipped", "reason": "not_generated"})
        return "done", "not_generated", True
    job_hash = str(job.get("sourceHash") or "")
    if job_hash and block_hash and job_hash != block_hash:
        log_verdict(log_path, {**base_record(job), "result": "skipped", "reason": "stale_job"})
        return "done", "stale_job", True
    if _excluded(selected_route, text, job):
        log_verdict(log_path, {**base_record(job), 'result': 'skipped', 'reason': 'source_folder_excluded'})
        return "done", "source_folder_excluded", True
    if wiki_freshness.unresolved_pending_refreshes_strict(root) is None:
        return "queued", "refresh_unresolved", False
    checkout = None
    if selected_route is not None and isinstance(job.get('sourceFingerprints'), dict):
        from memvane.wiki.workspace import api as wiki_selected_laya
        try:
            checkout = wiki_selected_laya.schema3_checkout(selected_route)
        except (OSError, RuntimeError, ValueError, sqlite3.Error):
            log_verdict(log_path, {**base_record(job), 'result': 'skipped',
                                   'reason': 'provenance_checkout_unavailable'})
    if recorded is None:
        outcome = _paid_verdict(root, config, compile_cfg, vcfg, job, text, log_path)
        if outcome[0] != 'verdict':
            return outcome
        recorded = outcome[1]
        # 3번 기준: 기록이 실패해도 판정은 이번 run에 그대로 적용된다. 잃는 것은 게시 전 중단 시
        # 재사용뿐이고 그 재검수도 claim 횟수 상한(4번째 claim에서 dropped) 안에 머문다.
        store.record_verify_verdict(row['jobId'], token, recorded)
    freshness = source_freshness(root, config, job)
    if freshness is not None and freshness.get("state") != wiki_markers.FRESH:
        stale = freshness.get("state") == wiki_markers.STALE
        log_verdict(log_path, {**base_record(job), "result": "skipped" if stale else "deferred",
                               "reason": "source_changed_during_verify",
                               "freshnessReason": freshness.get("reason", "")})
        return ("done", "stale_revision", True) if stale else ("queued", "source_unknown", False)
    record = {**base_record(job), **recorded["provenance"], "verifiedMode": recorded["mode"],
              "verdict": recorded["verdict"], "claims": len(recorded["claims"]), "reasons": recorded["reasons"]}
    if recorded["verdict"] == "pass":
        return _publish_pass(root, config, job, live, wiki_root, text, expected, recorded, record,
                             log_path, checkout,
                             remember=lambda value: store.record_verify_verdict(row['jobId'], token, value))
    # fail·inconclusive: 카드를 지우지 않는다. 첫 카드는 generated로 남아 recallVerifiedOnly
    # 기본값 아래 recall에서 빠지고, 교체본은 행의 종결과 함께 버려진다.
    log_verdict(log_path, {**record, "result": "inconclusive" if recorded["verdict"] == "inconclusive" else "kept"})
    return "done", "kept", True


def run(
    root: Path, config: dict, compile_cfg: dict, produced_targets: list[str] | None = None
) -> dict:
    """Drain one bounded batch of verify rows.

    The caller checks compile eligibility. This run keeps the configured review
    budget and can expand it for just-produced targets up to VERIFY_PRODUCED_HARD_CAP.
    It takes those cards first and reserves one slot for an existing backlog.
    """
    result = {"processed": 0, "remaining": 0}
    vcfg = verify_cfg_of(compile_cfg)
    if not vcfg.get("enabled", True):
        return result
    from memvane.wiki.pipeline import api as pipeline
    from memvane.wiki.workspace import api as wiki_selected_laya
    selected_route = wiki_selected_laya.selected(root)
    log_path = pipeline.audit_log(root, 'verify_log')
    max_per_run = max(1, min(
        int(engines.budget_of(compile_cfg).get("verifyPerRun", 3) or 3), settings_types.MAX_VERIFY_PER_RUN
    ))
    produced = {t for t in (produced_targets or []) if isinstance(t, str) and t}
    # 생산량은 모델 출력에서 유도된 값이므로 상한을 씌운다(VERIFY_PRODUCED_HARD_CAP).
    budget = max(max_per_run, min(len(produced), settings_types.VERIFY_PRODUCED_HARD_CAP))
    try:
        pipeline.recover_publishes(root, _wiki_root(root, config), lock_context=_card_locks(root))
    except (OSError, ValueError, sqlite3.Error):
        return {**result, "remaining": 1, "reason": "publish_recovery_unavailable"}
    token = uuid.uuid4().hex
    returned = 0
    with pipeline.open_pipeline(root) as store:
        recovered = store.recover_dead_claims('verify')
        if recovered['exhausted']:
            for item in recovered['exhausted']:
                log_verdict(log_path, {**base_record(item['payload']), 'result': 'skipped',
                                       'reason': 'requeue_exhausted', 'attempts': item['attempts']})
            store.drop_jobs('verify', [item['jobId'] for item in recovered['exhausted']], 'requeue_exhausted')
        batch = store.claim_verify_batch(os.getpid(), token, budget=budget, produced=produced)
        try:
            for row in batch['claimed']:
                wiki_selected_laya.recheck(selected_route)
                state, reason, processed = process_row(root, config, compile_cfg, vcfg, store, token,
                                                       row, log_path, selected_route)
                if processed:
                    result["processed"] += 1
                if state == "queued":
                    returned += 1
                # 3번 기준: 실패하면 행이 claimed로 남고 아래 release가 queued로 되돌린다.
                store.finish_verify_job(row['jobId'], token, state, reason)
        finally:
            store.release_verify_claim(token)
    result["remaining"] = batch['remaining'] + returned
    result["budget"] = budget
    result["freshProcessed"] = batch['fresh']
    result["backlogProcessed"] = batch['backlog']
    if batch['superseded']:
        result["superseded"] = batch['superseded']
    # 모델 출력이 예산을 밀어올리려다 상한에 걸린 사실은 관측 가능해야 한다.
    if len(produced) > settings_types.VERIFY_PRODUCED_HARD_CAP:
        result["producedCapped"] = len(produced)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cwd", default=os.getcwd())
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if os.environ.get("QMD_SANDBOX") or "--sandbox" in sys.argv:
        return 0
    found = qmd_config.find_project_config(args.cwd)
    root = Path(found["projectRoot"]).resolve()
    config = found["config"]
    compile_cfg = config.get("compile") if isinstance(config.get("compile"), dict) else {}
    if config.get("indexing") is not True or not qmd_config.compile_active(compile_cfg):
        return 0
    result = run(root, config, compile_cfg)
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Machine-review (auto-verify) worker for generated wiki cards.

wiki_compile.py enqueues freshly written generated cards to verify-queue.jsonl.
This worker replays each card against its source documents through the same
host-CLI adapter pool used for extraction (payload {"task": "verify"}), but
**prefers an engine other than the one that wrote the card** (see
plan_verify_attempts / compile.verify.crossEngine), then:
  pass         -> status: verified (+ verifiedBy/verifiedAt/verifiedMode)
  fail         -> compile.verify.onFail: delete card (default) | contested | none
  inconclusive -> compile.verify.onInconclusive: delete (default) | contested | none

Deleting on `inconclusive` needs loop protection that `fail` does not: "the verifier
could not adjudicate" is far more likely to reproduce verbatim on the same source
than "the source contradicts the card", so delete -> recompile -> inconclusive ->
delete would re-bill the user's host-CLI account forever. Every inconclusive
deletion therefore records (sourcePath, bounded source body hash) in
verify-skipped.jsonl, and wiki_compile_worker.process_job skips those sources
*before* spawning an extractor. Same body-hash suppression pattern as
dedup-skipped.jsonl: unchanged content is never re-billed, changed content retries.
That marker is written BEFORE the card is unlinked and a failed write blocks the
delete (fail-closed), and run() refuses to spend a paid call at all when either
fail-closed ledger is unwritable (preflight_block_reason).

The source documents replayed against a card are the ones the QUEUE knows, not the
ones the model listed: candidate["sources"] is extractor output and load_sources
reads only the first MAX_SOURCES entries, so model-chosen decoys could otherwise
become the entire evidence base for a `verified` card.

Every deletion (fail and inconclusive alike) also appends one row to
verify-deleted.jsonl, the untrimmed audit ledger — verify-log.jsonl logs passes too and
so rotates its history away within days on an active project.

A pass may also stamp `sourceSpans`: line locations of the verifier's own exact
quotes, located in code against the revision-checked snapshot bytes
(source_spans.py). They are written with the status in one atomic write, carry
no quote text, and only locate cited excerpts — they are not per-claim proof.

Runs piggybacked from wiki_compile_worker.main() under the same per-cwd lock,
and doubles as a standalone CLI for tests/manual runs. Silent by default (hook
path); queue/log records never store source bodies.
"""

from __future__ import annotations
import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.resolve()))
import wiki_mutation_lock
import compile_paths as cp
import config as qmd_config
import wiki_compile as wc
import wiki_compile_worker as wcw
import wiki_freshness
import wiki_source_missing as wsm
import source_spans
import yaml_scalars
from dirty_queue import enqueue_project_collections
from wiki_compile_enqueue import _safe_queue_path

# 경로는 전부 `compile_paths` 상수 테이블에서 온다(설정 키 4개는 제거됐다 —
# `skippedPath`/`deletedPath` 오타가 유료 과금 루프의 입구였던 실패 클래스가 함께 사라진다).
VERIFY_QUEUE_DEFAULT = cp.rel(cp.VERIFY_QUEUE)
VERDICT_VALUES = {"pass", "fail", "inconclusive"}
MAX_SOURCES = 3
# "호출은 됐지만 출력이 판정으로 쓸 수 없다" — 같은 입력에서 재현되는 설정 오류이므로
# 후보가 다 떨어지면 종점이 있다(defer_or_drop). transient(timeout·실행 실패)와 구분된다.
UNUSABLE_OUTPUT_REASONS = {"invalid_extractor_json", "invalid_verdict"}
def extractor_builtins(compile_cfg: dict) -> list[str]:
    raw = compile_cfg.get("extractor")
    extractor = raw if isinstance(raw, dict) else {}
    return [e for e in (extractor.get("builtins") or []) if isinstance(e, str) and e]


def verify_engine_pool(compile_cfg: dict, vcfg: dict) -> list[str]:
    """Symbolic engines allowed to verify, in preference order.

    `compile.verify.builtins` when set, else inherited from the extractor pool
    (`compile.extractor.builtins` + explicitly configured `backends` keys — the shape
    both dogfood projects use). Only engine LABELS live here; argv resolution stays in
    wiki_compile_worker.resolve_extractor_argv so there is one rule, not two.

    This list does NOT have to contain the producing engine: under "prefer" that engine
    is always appended as the LAST candidate (plan_verify_attempts), so narrowing the
    pool — or typing an engine name wrong — can never take the self-verify fallback away.
    Replacing the fallback was measured to strand builtins-only projects (the
    `--enable-compile` default shape) on `missing_extractor`.
    """
    picked = [e for e in (vcfg.get("builtins") or []) if isinstance(e, str) and e]
    if not picked:
        # 상속 목록은 `wcw.extractor_engine_pool`이 SSOT다(dedup judge도 같은 함수를 쓴다).
        picked = wcw.extractor_engine_pool(compile_cfg)
    ordered = []
    for engine in picked:
        if engine not in ordered:
            ordered.append(engine)
    return ordered


def plan_verify_attempts(
    compile_cfg: dict, vcfg: dict, producing: str, cooled=()
) -> tuple[list[dict], str, str]:
    """Ordered verify attempts → ([{engine, argv, mode, key}], crossEngine mode, empty reason).

    Step 4 of the injection-quality roadmap: prefer an engine OTHER than the one that
    wrote the card. Self-review measurably degrades LLM judgment (Huang et al. ICLR'24)
    and the literature's stated mitigation is separating generator from judge; the live
    corpus was 655/688 verified cards self-reviewed by `claude`.

    Degrade is deliberate and asymmetric to the config:
      - "prefer" (default) puts the producing engine LAST instead of dropping it, so a
        single-CLI machine still verifies — marked `mode: self`. Requiring a second CLI
        would leave every card `generated`, and `recallVerifiedOnly` (default true)
        would then erase the whole wiki from recall with no human-review path to
        recover. A weaker-but-labelled check beats no wiki.
      - "require" drops it, and fails CLOSED when the producer cannot be attributed.
        Callers preserve the job instead of dropping it.
      - "off" is the 0.x path: producing engine only.

    `cooled` are cooldown keys whose last paid call failed (non-127); they are skipped so
    the NEXT run degrades to the next candidate. This is what keeps "one paid call per
    card per run" from turning into "verification never happens": a run never retries a
    failed engine, and a later run never retries it either until its cooldown expires.

    **A cross-engine claim requires an attributable producer.** `producing` must resolve
    to a per-engine argv in the current pool; the `"unknown"` sentinel
    (config.UNKNOWN_ENGINE, what enqueue writes when the host is unknown) and a label
    outside the pool both fail that test. In that case no candidate can be shown to
    differ from the producer, so every attempt is mode `unknown` and "require" refuses
    outright. (`extractor.argv`/`extractor.default` — the unattributable 0.x forms that
    used to land here — no longer exist in the schema; every argv now comes from a
    named engine, so provenance is always expressible.)
    """
    pool = verify_engine_pool(compile_cfg, vcfg)
    mode = vcfg.get("crossEngine", "prefer")
    if mode not in qmd_config.VERIFY_CROSS_ENGINE:
        mode = "prefer"
    cooled = set(cooled)

    # 생성 엔진이 builtin adapter로 카드를 만들었다면 그 adapter로 검수할 수 있어야 한다 —
    # verify.builtins가 좁혀져 있어도(또는 오타여도) self 폴백이 사라지면 안 된다.
    resolve_builtins = list(pool)
    if producing and producing in extractor_builtins(compile_cfg) and producing not in resolve_builtins:
        resolve_builtins.append(producing)
    producing_attributable = bool(
        producing and producing != qmd_config.UNKNOWN_ENGINE
        and wcw.resolve_extractor_argv(compile_cfg, producing, builtins=resolve_builtins) is not None
    )
    if mode == "require" and not producing_attributable:
        # 어느 후보도 "생성 엔진과 다르다"를 증명할 수 없다 → 약속을 지킬 수 없으므로 거부.
        return [], mode, "cross_engine_unavailable"

    # 순서 규칙은 dedup judge와 공용이다(`wcw.plan_engine_order`가 SSOT). 여기 남는 것은
    # verify 고유의 것들 — argv 해석, 식힘 건너뛰기, mode 라벨, 빈 계획의 사유다.
    order = wcw.plan_engine_order(pool, producing, mode, producing_attributable)

    attempts: list[dict] = []
    seen_argv: list[list[str]] = []
    skipped_cooling = 0
    for engine in order:
        primary = wcw.resolve_extractor_argv(compile_cfg, engine, builtins=resolve_builtins)
        if primary is None or primary in seen_argv:
            continue
        seen_argv.append(primary)
        if engine in cooled:
            skipped_cooling += 1
            continue
        if not producing_attributable:
            attempt_mode = qmd_config.VERIFIED_MODE_UNKNOWN
        elif engine == producing:
            attempt_mode = qmd_config.VERIFIED_MODE_SELF
        else:
            attempt_mode = qmd_config.VERIFIED_MODE_CROSS
        attempts.append({"engine": engine, "argv": primary, "mode": attempt_mode, "key": engine})
    if attempts:
        return attempts, mode, ""
    # 빈 결과의 사유를 구분한다: cooldown(일시적, 큐 보존) / require가 걸러냄(설정·설치
    # 상태, 큐 보존) / 설정 자체가 없음(기존 동작, 잡 폐기).
    if skipped_cooling:
        return [], mode, "engines_cooling"
    if mode == "require" and producing_attributable:
        return [], mode, "cross_engine_unavailable"
    return [], mode, "missing_extractor"


def verify_cfg_of(compile_cfg: dict) -> dict:
    raw = compile_cfg.get("verify")
    return raw if isinstance(raw, dict) else {}


def engine_cooldown_path(root: Path) -> Path:
    # compile cooldown과 분리(extractor 실패가 verify를 막지 않게) + **엔진 단위**로 분리.
    # 0.x의 전역 `verify-cooldown`을 대체한다: 전역이면 한 엔진의 non-127 실패가 그 프로젝트의
    # 모든 카드 검수를 막았고, 선호 엔진이 계속 같은 실패를 반복하는 상태(인증 안 된 CLI 등)에서
    # 다음 후보로 degrade하지 못해 **검수가 영구 정지**했다 — `recallVerifiedOnly` 기본값
    # 아래에서 그것은 wiki가 recall에서 사라지는 것과 같다.
    return root / cp.COMPILE_DIR / cp.VERIFY_ENGINE_COOLDOWN


# 엔진 단위 식힘 저장소의 구현은 `wcw`가 SSOT다(dedup judge가 자기 파일로 같은 헬퍼를
# 쓴다 — flock read-modify-write·만료 클램프·원자 쓰기 반환값 확인을 두 벌 두지 않는다).
# 여기 남는 것은 **경로**뿐이고, 그것이 verify와 dedup을 분리하는 유일한 차이다.
def load_engine_cooldowns(root: Path) -> dict:
    return wcw.load_engine_cooldowns(engine_cooldown_path(root))


def cooling_engines(root: Path) -> set:
    return wcw.cooling_engines(engine_cooldown_path(root))


def set_engine_cooldown(root: Path, key: str, seconds: int) -> bool:
    """이 후보를 seconds 동안 후보에서 제외한다. 반환값은 "기록이 남았는가"다.

    **호출자는 False를 반드시 표면화해야 한다.** 이 파일이 MAJOR 1(영구 정지) 수정의 복구
    메커니즘이다 — 기록이 없으면 다음 run이 같은 엔진을 다시 부르고 degrade가 일어나지 않아
    정지가 그대로 돌아온다. 조용히 실패하면 그 사실을 알 방법이 없다.
    """
    return wcw.set_engine_cooldown(engine_cooldown_path(root), key, seconds)


def log_verdict(log_path: Path, payload: dict) -> None:
    wcw.append_jsonl(log_path, payload)
    wc.trim_jsonl(log_path)


def verify_skipped_path(root: Path, vcfg: dict) -> Path:
    """inconclusive 삭제의 억제 마커 경로. **`verify_deleted_path`와 같이 폴백한다.**

    예전에는 `None`을 돌려줄 수 있었고 그것이 `verify_deleted_path`가 이미 닫은 것과 **같은
    과금 루프의 입구**였다: 원장이 **한 줄도 기록되지 않으면** unchanged source가
    재컴파일→재검수→재삭제를 반복하고 매 반복이 유료 호출이다. 그 입구를 만들던 설정 키
    (`compile.verify.skippedPath` — 오타 한 번이면 compile 디렉터리 밖을 가리켰다)는
    제거됐지만, 파일 자체가 밖을 가리키는 symlink면 `cp.ledger`가 여전히 None을 낸다.
    그때도 기본 경로로 떨어져 흔적을 남긴다(마커 없는 삭제보다 나은 실패다).
    """
    return cp.ledger(root, cp.VERIFY_SKIPPED) or (root / cp.COMPILE_DIR / cp.VERIFY_SKIPPED)


def verify_deleted_path(root: Path, vcfg: dict) -> Path:
    """삭제 전용 감사 원장 경로. **형제 호출자들과 같이 기본 경로로 폴백한다.**

    `candidate_path`·`log_path`는 폴백하는데 이것만 `None`을 돌려주던 것이 유료 무한 루프의
    입구였다: 경로가 안 잡히면 `record_verify_deletion`이 실패해 fail-closed 분기로 가고,
    cooldown도 억제 마커도 없어 **매 run 유료 호출 1회 + 잡 영구 잔존 + 카드 영구 비가시**가
    됐다(실측 3 run 연속). 설정 키(`compile.verify.deletedPath`)는 제거됐지만 symlink escape는
    남으므로 폴백도 남는다.
    """
    return cp.ledger(root, cp.VERIFY_DELETED) or (root / cp.COMPILE_DIR / cp.VERIFY_DELETED)


# 유료 호출 전 원장 쓰기 가능성 판정. 정의는 `wiki_compile`이 SSOT다 — dedup scan도 같은
# 판정을 쓰므로 여기서 재구현하면 게이트가 두 벌이 된다(같은 클래스가 그렇게 재발했다).
ledger_writable = wc.ledger_writable


def preflight_block_reason(root: Path, vcfg: dict) -> str:
    """유료 검수를 시작하기 전에 막아야 할 사유(없으면 빈 문자열).

    fail-closed 원장이 둘이다. **둘 다** 쓸 수 없으면 유료 호출을 하지 않는다:
      - `verify-deleted.jsonl` — 모든 기계 삭제의 감사 원장(fail·inconclusive 공통).
      - `verify-skipped.jsonl` — inconclusive 삭제의 억제 마커. 이것이 없으면 unchanged
        source가 재컴파일→재검수→재삭제를 반복하고 매 반복이 유료 호출이다.
    억제 마커는 `onInconclusive`가 실제로 삭제일 때만 필요하므로 그 경우에만 검사한다 —
    무조건 검사하면 `onInconclusive: none|contested` 프로젝트의 검수를 이유 없이 막는다.

    `run()`(큐 claim 전)과 `core/update.sh`의 SessionStart notice가 **같은 함수**를 부른다.
    판정을 두 곳에서 재구현하면 "막혔는데 아무도 안 알려주는" 상태가 다시 생긴다.
    """
    if not ledger_writable(verify_deleted_path(root, vcfg)):
        return "audit_ledger_unwritable"
    if vcfg.get("onInconclusive", "delete") == "delete" and not ledger_writable(
        verify_skipped_path(root, vcfg)
    ):
        return "suppression_ledger_unwritable"
    return ""


def load_verify_suppressions(path: Path | None) -> dict[str, str]:
    """sourcePath -> bounded body hash that produced an unadjudicable card.

    Append-only file read in order, so LAST record per source path wins (same
    semantics as wiki_dedup_scan.load_skip_suppressions). Malformed rows are
    ignored — fail-open means "compile it", i.e. spend tokens rather than lose
    a card, which is the safe direction for a cost guard that can't read itself.
    """
    suppressions: dict[str, str] = {}
    if path is None:
        return suppressions
    for row in wc.read_jsonl(path):
        source_path = row.get("sourcePath")
        body_hash = row.get("sourceBodyHash")
        if not (isinstance(source_path, str) and source_path and isinstance(body_hash, str) and body_hash):
            continue
        suppressions[source_path] = body_hash
    return suppressions


def record_machine_delete(root: Path, compile_cfg: dict, record: dict, verdict: str) -> None:
    """Mark the manifest so the next compile does not tombstone this target.

    wiki_compile's delete-detection reads "manifest says it existed + file gone" as a
    deliberate user deletion and tombstones the identity permanently. Without this
    row, a verify delete would suppress the card forever — contradicting the reason
    delete is preferable to contested (fix the source, get a fresh card). Fail-open:
    an unwritable manifest degrades to today's tombstone behaviour, never to a crash.
    """
    path = cp.ledger(root, cp.MANIFEST)
    if path is None:
        return
    # targetPath만 담아 same_generated_identity가 이 카드에만 매칭되게 한다
    # (sourceHash/canonicalKey를 넣으면 다른 카드까지 잘못 매칭될 수 있음).
    wc.append_jsonl(path, {
        "ts": wcw.now_iso(),
        "action": wc.MACHINE_DELETE_ACTION,
        "status": "deleted",
        "targetPath": record.get("targetPath", ""),
        "verdict": verdict,
        "engine": record.get("engine", ""),
    })
    wc.compact_manifest(path)


def record_verify_deletion(
    root: Path, vcfg: dict, sources: list[dict], record: dict, verdict: str
) -> bool:
    """Append the DURABLE audit row for a machine deletion (fail or inconclusive).

    Machine verification is the only gate that removes cards (verify.onFail /
    onInconclusive = delete, both default), so "why did this card go away" must stay
    answerable. verify-log.jsonl cannot answer it: it records every verdict including
    passes, so trim_jsonl drops the oldest half once it crosses LOG_MAX_BYTES — measured
    on a live project, pass traffic had already pushed three weeks of history out. The
    manifest's verify-deleted row survives compaction but carries no reasons and is
    replaced the moment the card regenerates.

    This file therefore holds deletions ONLY, which is what makes leaving it untrimmed
    affordable: one small row per deleted card (paths and bounded reasons, never bodies),
    not one per verdict. verify-skipped.jsonl overlaps for inconclusive but is keyed by
    source + body hash for billing-loop suppression; this ledger is keyed by card and
    covers fail too, which records no suppression by design.
    """
    path = verify_deleted_path(root, vcfg)
    # **예외도 False로 접는다.** `append_jsonl`이 OSError를 전파하면(경로가 디렉터리·ENOSPC)
    # standalone worker가 rc=1 traceback으로 죽고 `verify-log.jsonl`에 **한 줄도 남지 않아**
    # "흔적은 남는다"가 성립하지 않았다. 여기서 잡으면 호출자의 fail-closed 분기가
    # `delete_blocked` 줄을 남기고 잡을 보존한다(= 관측 가능 + 카드 보존).
    try:
        wc.append_jsonl(path, {
            "targetPath": record.get("targetPath", ""),
            "verdict": verdict,
            "engine": record.get("engine", ""),
            # 자기검증(self)이 내린 삭제는 교차검증이 내린 삭제보다 약한 근거다 — 삭제된
            # 카드를 사후에 판단하려면 누가 무엇을 검수했는지가 원장에 함께 있어야 한다.
            "verifiedMode": record.get("verifiedMode", ""),
            "producedBy": record.get("producedBy", ""),
            "reasons": record.get("reasons", []),
            "claims": record.get("claims", 0),
            "sourcePaths": [
                src.get("path") for src in sources
                if isinstance(src, dict) and isinstance(src.get("path"), str) and src.get("path")
            ],
            "deletedAt": wcw.now_iso(),
        })
    except OSError:
        return False
    return True


def record_verify_suppression(
    root: Path, vcfg: dict, sources: list[dict], record: dict, verdict: str
) -> tuple[int, bool]:
    """Mark every source of a deleted card so it is not recompiled unchanged.

    Returns `(rows written, all writes succeeded)`. **The second value must not be
    dropped.** `0` rows is legitimate (a card whose sources are all gone has nothing
    to suppress and cannot be recompiled from them either), so the count alone cannot
    tell "nothing to write" from "the write failed" — and the caller has to fail
    CLOSED on the latter, exactly as it does for the deletion ledger. Swallowing it
    deleted the card with no marker, so the unchanged source went through
    compile -> verify -> delete again on the next run, each pass a paid host-CLI call.

    Never trim_jsonl'd: dropping a row here reopens the billing loop it was written
    to close. Rows also carry verdict/reasons/targetPath, so this doubles as a
    source-keyed view of inconclusive deletions — but the card-keyed audit ledger for
    every machine deletion is verify-deleted.jsonl (see record_verify_deletion).

    Hashes are of the same maxSourceChars-bounded slice the extractor saw, so
    wiki_compile_worker's pre-extraction check compares like with like.
    """
    path = verify_skipped_path(root, vcfg)
    written = 0
    for src in sources:
        rel = src.get("path")
        content = src.get("content")
        if not (isinstance(rel, str) and rel and isinstance(content, str)):
            continue
        # **예외도 실패로 접는다**(삭제 감사 원장과 같은 이유): `append_jsonl`이 OSError를
        # 전파하면 standalone worker가 traceback으로 죽어 어디에도 흔적이 남지 않는다.
        try:
            wc.append_jsonl(path, {
                "sourcePath": rel,
                "sourceBodyHash": wc.source_body_hash(content),
                "targetPath": record.get("targetPath", ""),
                "verdict": verdict,
                "reasons": record.get("reasons", []),
                "engine": record.get("engine", ""),
                "deletedAt": wcw.now_iso(),
            })
        except OSError:
            return written, False
        written += 1
    return written, True


def reindex_wiki(root: Path, config: dict) -> None:
    collection, collection_path = wc.find_wiki_collection(config)
    if collection and collection_path:
        import wiki_selected_laya
        selected = wiki_selected_laya.selected(root)
        if selected is None:
            enqueue_project_collections(root, {collection: str((root / collection_path).resolve())})
        else:
            wiki_selected_laya.request_sync(root, selected)


def wake_pending_retirement(root: Path) -> None:
    """Recheck a held deletion only after a successful replacement review."""
    import wiki_selected_laya
    selected = wiki_selected_laya.selected(root)
    if selected is None:
        return
    queue = _safe_queue_path(root, cp.rel(cp.SOURCE_QUEUE))
    if queue is None:
        return
    try:
        pending = wcw.read_queue(queue)
    except OSError:
        return
    if any(isinstance(job, dict) and job.get('sourceDelete') is True
           and job.get('sharedEnqueued') is True for _, job in pending):
        wiki_selected_laya.request_compile(root, selected)


def pending_replacement(root: Path, wiki_root: Path, job: dict,
                        live_target: Path) -> tuple[Path, str] | None:
    """Accept only the compiler's hidden pending path for this exact target/job."""
    rel = job.get('pendingPath')
    if rel is None:
        return None
    job_hash = job.get('sourceHash')
    expected = job.get('expectedTargetHash')
    if (not isinstance(job_hash, str) or not re.fullmatch(r'[0-9a-f]{64}', job_hash)
            or not isinstance(expected, str) or not re.fullmatch(r'[0-9a-f]{64}', expected)
            or not isinstance(rel, str)):
        raise ValueError('pending_replacement_invalid')
    root = root.resolve()
    pending = live_target.with_name('.' + live_target.name + '.pending-' + job_hash[:16])
    if (pending.is_symlink() or pending.relative_to(root).as_posix() != rel
            or pending.parent != live_target.parent
            or not pending.is_relative_to(wiki_root)):
        raise ValueError('pending_replacement_invalid')
    return pending, expected


def publish_pending_replacement(root: Path, config: dict, job: dict,
                                pending: Path, live_target: Path,
                                expected: str) -> str:
    """Return published, stale or retry; only transient I/O stays queued."""
    try:
        pending_text, pending_meta, pending_status, pending_hash = card_state(pending)
        if (live_target.is_symlink() or pending.is_symlink()
                or pending_status != 'verified'
                or pending_hash != job.get('sourceHash')
                or not isinstance(pending_text, str)
                or pending_meta.get('verifiedBodyHash') !=
                    yaml_scalars.card_body_hash(pending_text)):
            return 'stale'
        try:
            old_bytes = live_target.read_bytes()
        except (FileNotFoundError, IsADirectoryError):
            return 'stale'
        if hashlib.sha256(old_bytes).hexdigest() != expected:
            return 'stale'
        # Once the reviewed candidate is published, the previous completed
        # card must remain recoverable just like a source-deletion retirement.
        import wiki_source_retire
        if not wiki_source_retire.archive_card(
                root, live_target, live_target.read_text(encoding='utf-8'), expected):
            return 'retry'
        os.replace(pending, live_target)
        directory = os.open(live_target.parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError:
        return 'retry'
    wiki_root = (root / config.get('wikiPath', '.auto-context/wiki')).resolve()
    wc.update_index(wiki_root, live_target, str(pending_meta.get('title') or live_target.stem))
    wc.append_log(wiki_root, 'updated', live_target,
                  str(pending_meta.get('title') or live_target.stem))
    reindex_wiki(root, config)
    return 'published'


def finish_stale_pending(root: Path, pending: Path, job: dict,
                         log_path: Path) -> tuple[bool, bool]:
    """Ack a superseded candidate without touching the current live card."""
    try:
        if pending.is_symlink():
            raise OSError('pending_symlink')
        pending.unlink(missing_ok=True)
    except OSError:
        log_verdict(log_path, {**base_record(job), 'result': 'deferred',
                               'reason': 'stale_pending_cleanup_failed'})
        return False, True
    log_verdict(log_path, {**base_record(job), 'result': 'stale_replacement'})
    wake_pending_retirement(root)
    return True, False


def card_state(target: Path) -> tuple[str | None, dict, str, str]:
    """(text, frontmatter, status, block sourceHash) — 읽기 실패 시 text None."""
    try:
        text = target.read_text(encoding="utf-8")
    except OSError:
        return None, {}, "", ""
    meta, ok = wc.parse_frontmatter(text)
    if not ok:
        meta = {}
    status = str(meta.get("status") or "").strip() or "generated"
    match = wc.AUTO_START_RE.search(text)
    return text, meta, status, (match.group(1) if match else "")


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
        return [], {"state": wiki_freshness.UNKNOWN, "reason": "missing_source_revisions"}, {}

    import recall

    allow_roots = wsm.allow_roots_of(config)
    loaded = []
    evidence: dict[str, dict] = {}
    for raw in raw_revisions:
        expected = yaml_scalars.normalize_source_revision(raw)
        if expected is None:
            return [], {"state": wiki_freshness.UNKNOWN, "reason": "invalid_source_revision"}, {}
        resolved, reason = recall.resolve_existing_source(expected["path"], root, allow_roots)
        if resolved is None:
            state = wiki_freshness.STALE if reason == "missing" else wiki_freshness.UNKNOWN
            return [], {"state": state, "reason": reason or "source_unavailable"}, {}
        snapshot = wiki_freshness.snapshot_bytes(resolved)
        if snapshot is None:
            return [], {"state": wiki_freshness.UNKNOWN, "reason": "source_snapshot_failed"}, {}
        revision, data = snapshot
        if revision["sha256"] != expected["sha256"]:
            return [], {"state": wiki_freshness.STALE, "reason": "content_hash_mismatch"}, {}
        bounded = wcw.decode_source_bounded(data, max_chars)
        if bounded is None:
            return [], {"state": wiki_freshness.UNKNOWN, "reason": "source_decode_failed"}, {}
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
    return loaded, {"state": wiki_freshness.FRESH, "checked": len(loaded)}, evidence


def base_record(job: dict) -> dict:
    """attempt 이전에도 쓸 수 있는 레코드 필드.

    `engine`은 여기에 두지 않는다 — 시도가 있기 전에는 "검수 엔진"이 없고, 같은 키가
    줄에 따라 생성 엔진/검수 엔진 두 의미를 가지면 집계가 오염된다. 생성 엔진은 항상
    `producedBy`, 실제로 판정을 낸 엔진은 항상 `engine`이다.
    """
    return {
        "ts": wcw.now_iso(),
        "targetPath": job.get("targetPath", ""),
        "producedBy": job.get("engine", ""),
    }


def source_freshness(root: Path, config: dict, job: dict) -> dict | None:
    """Check job-owned provenance, preserving legacy jobs without the field."""
    if "sourceRevisions" not in job:
        return None
    revisions = job.get("sourceRevisions")
    return wiki_freshness.check_card(
        {"sourceRevisions": revisions}, root, wsm.allow_roots_of(config))


def card_changed_during_verify(
    job_hash: str, fresh_meta: dict, fresh_status: str, fresh_hash: str,
    payload_text: str | None, fresh_text: str | None,
) -> bool:
    """검수 시작 이후 카드가 바뀌었는가 — 잠금 아래 재확인 규칙의 SSOT.

    **`fresh_hash`가 비어 있으면 "바뀌었다"로 본다.** 예전 조건은
    `job_hash and fresh_hash and job_hash != fresh_hash`라, auto 블록이 없어
    `card_state`가 `""`를 돌려주면 대조를 통째로 건너뛰었다 — 이 가드가 막으려는 바로 그
    상황에서 fail-open했다. 실해: 검수 LLM 호출(수십 초) 동안 카드가 `status: generated` +
    `createdBy: qmd-auto-context`만 남긴 **다른 본문**으로 교체되고 auto 블록이 빠지면
    (dedup consolidation·사람 편집이 정확히 그 모양이다) 옛 본문에 대한 verdict가 새 본문에
    적용된다. `pass`면 검증된 적 없는 내용에 `verified`가 찍히고, `fail`이면 방금 사람이 쓴
    본문이 삭제된다(삭제 원장에 본문이 없어 복구 불가).

    `markdown_page`가 auto 블록을 항상 쓰므로 정상 카드는 영향이 없고, `job_hash`가 빈
    잡(레거시·수동 경로)은 예전처럼 블록 해시 대조 없이 status/creator만 본다.

    **파일 전문 동등성이 이 가드의 두 번째 층이고, 위 세 축을 포함한다.** 왜 부분 대조로는
    부족한지가 이 층의 존재 이유다.
      - `job_hash` 는 auto 블록 **마커에 박힌** sourceHash 라(`AUTO_START_PATTERN`) 블록을
        통째로 재생성하는 compile 만 바꾼다. 사람이 블록 **안의 문장**을 고치면 마커는
        그대로이고 status·creator 도 그대로여서 세 축이 전부 통과한다.
      - frontmatter 의 `sourceRevisions` 만 교체돼도 세 축이 통과한다. 그런데 검수 근거의
        freshness 는 `source_freshness` 가 **잡의** `sourceRevisions` 로 보고, recall 은
        **카드의** frontmatter 로 본다 — 즉 원문 A 로 받은 verdict 가 원문 B 를 주장하는
        카드에 찍히고, recall 은 B 기준 freshness 를 통과시켜 그 카드를 캐논급으로 주입한다.
        본문만 대조하면 이 경로가 열린 채 남는다.
    그래서 축을 늘려 좇는 대신 **payload 로 보낸 파일 전문과 잠금 아래 다시 읽은 전문이
    같은가**를 묻는다. 스탬프는 이 판정 **뒤에** 일어나므로 아무것도 바뀌지 않았다면 두
    텍스트는 축자 동일해야 하고, 이 한 조건이 status·creator·마커·본문·provenance 전부를
    덮는다. 위 세 축은 남겨 둔다 — 각자 고정한 회귀가 있고(테스트가 그 자리를 못박는다)
    전문 대조가 언제 완화돼도 판정 granularity 가 남아야 한다.

    **어느 쪽이든 None 이면 "바뀌었다"로 본다** — `fresh_hash` 가 비었을 때와 같은 규칙이고
    같은 이유다(대조 불가를 통과로 읽으면 이 가드가 막으려는 상황에서 fail-open 한다).
    **두 인자에 기본값을 두지 않는다** — 기본값이 있으면 다음에 추가되는 호출부가 인자를
    빼고도 컴파일돼 대조를 조용히 건너뛴다. 대조의 요점이 "빠뜨릴 수 없게 하는 것"이라
    호출부가 명시하도록 강제한다.

    함수로 빼낸 이유는 테스트가 이 규칙을 **재구현하지 않게** 하기 위해서다 — 조건이
    호출부에 인라인이면 테스트는 같은 식을 다시 적을 수밖에 없고, 그러면 규칙이 바뀌어도
    둘이 나란히 틀린다.
    """
    return (
        fresh_status != "generated"
        or fresh_meta.get("createdBy") != "qmd-auto-context"
        or bool(job_hash and job_hash != fresh_hash)
        or payload_text is None
        or fresh_text is None
        or payload_text != fresh_text
    )


def apply_verify_verdict_locked(
    root: Path, config: dict, compile_cfg: dict, vcfg: dict, job: dict,
    target: Path, job_hash: str, payload_text: str | None,
    engine: str, verified_mode: str,
    provenance: dict, verdict: str, claims: list, reasons: list[str],
    sources: list[dict], log_path: Path, *, spans: list[dict],
) -> tuple[bool, bool]:
    """Re-read, decide, and mutate one card while CARD_WRITE_LOCK is held.

    ``spans`` were derived from the snapshot bytes before the lock.  They stay
    valid because the freshness re-check below compares the same revision
    ``sha256`` they are bound to; they are further narrowed to the card's own
    ``sourceRevisions`` so recall can bind them to what the card claims.
    """
    fresh_text, fresh_meta, fresh_status, fresh_hash = card_state(target)
    if card_changed_during_verify(job_hash, fresh_meta, fresh_status, fresh_hash,
                                  payload_text, fresh_text):
        log_verdict(log_path, {
            **base_record(job), **provenance,
            "result": "skipped", "reason": "changed_during_verify",
        })
        return True, False
    freshness = source_freshness(root, config, job)
    if freshness is not None and freshness.get("state") != wiki_freshness.FRESH:
        log_verdict(log_path, {
            **base_record(job), **provenance, "result": "deferred",
            "reason": "source_changed_during_verify",
            "freshnessReason": freshness.get("reason", ""),
        })
        return False, True

    record = {
        **base_record(job), **provenance, "verifiedMode": verified_mode,
        "verdict": verdict, "claims": len(claims), "reasons": reasons,
    }
    if verdict == "pass":
        # 지문은 **검증기에 보낸 전문**에서 파생한다. 가드가 전문 동등성을 통과시켰으므로
        # 디스크와 같은 값이지만, 출처를 "검증된 텍스트"로 고정해 둔다.
        # 줄 span 은 위치할 수 있는 인용만 담는다. 비면 키 없이 기존 파일 단위 verified 다.
        bound = source_spans.bind_to_card(spans, fresh_text) if spans else []
        if not wc.stamp_verification(target, "verified", engine, verified_mode,
                                     yaml_scalars.card_body_hash(payload_text),
                                     source_spans=bound or None):
            log_verdict(log_path, {**record, "result": "stamp_failed", "stampStatus": "verified"})
            return False, True
        pending = job.get('pendingPath')
        if pending is not None:
            live_target = root / job['targetPath']
            expected = job['expectedTargetHash']
            outcome = publish_pending_replacement(root, config, job, target,
                                                  live_target, expected)
            if outcome == 'stale':
                return finish_stale_pending(root, target, job, log_path)
            if outcome != 'published':
                log_verdict(log_path, {**record, 'result': 'publish_pending',
                                       'reason': 'write_failed'})
                return False, True
        if pending is None:
            reindex_wiki(root, config)
        wake_pending_retirement(root)
        log_verdict(log_path, {**record, "result": "verified", "sourceSpans": len(bound)})
        return True, False

    action = vcfg.get("onFail", "delete") if verdict == "fail" else vcfg.get("onInconclusive", "delete")
    pending_only = job.get('pendingPath') is not None
    applied = apply_negative_verdict(
        root, config, compile_cfg, vcfg, target, engine, verified_mode, action, verdict,
        sources, {**record, 'pendingOnly': True} if pending_only else record,
        log_path, yaml_scalars.card_body_hash(payload_text), pending_only=pending_only
    )
    return (True, False) if applied else (False, True)


def process_verify_job(
    root: Path, config: dict, compile_cfg: dict, vcfg: dict, job: dict, log_path: Path
) -> tuple[bool, bool]:
    """Return (processed, preserve_job) — wiki_compile_worker.process_job과 동일 계약."""
    import wiki_selected_laya
    selected_route = wiki_selected_laya.selected(root)
    rel = job.get("targetPath")
    if not isinstance(rel, str) or not rel:
        log_verdict(log_path, {**base_record(job), "result": "skipped", "reason": "missing_target_path"})
        return True, False
    wiki_root = (root / config.get("wikiPath", ".auto-context/wiki")).resolve()
    live_target = root / rel
    if live_target.is_symlink():
        log_verdict(log_path, {**base_record(job), 'result': 'skipped',
                               'reason': 'unsafe_target_path'})
        return True, False
    target = live_target.resolve()
    try:
        target.relative_to(wiki_root)
    except ValueError:
        log_verdict(log_path, {**base_record(job), "result": "skipped", "reason": "unsafe_target_path"})
        return True, False

    try:
        staged = pending_replacement(root, wiki_root, job, target)
    except ValueError:
        log_verdict(log_path, {**base_record(job), 'result': 'skipped',
                               'reason': 'pending_replacement_invalid'})
        return True, False
    if staged is not None:
        target, expected = staged
        if not target.exists():
            # A crash after atomic replacement but before queue ACK is a
            # completed replay. Otherwise the pending candidate is missing.
            live_text, live_meta, live_status, live_hash = card_state(live_target)
            if (live_text is not None and live_status == 'verified'
                    and live_hash == job.get('sourceHash')
                    and live_meta.get('verifiedBodyHash') ==
                        yaml_scalars.card_body_hash(live_text)):
                wc.update_index(wiki_root, live_target,
                                str(live_meta.get('title') or live_target.stem))
                reindex_wiki(root, config)
                wake_pending_retirement(root)
                log_verdict(log_path, {**base_record(job), 'result': 'verified_replay'})
                return True, False
            log_verdict(log_path, {**base_record(job), 'result': 'skipped',
                                   'reason': 'pending_replacement_missing'})
            return True, False
        if target.is_symlink():
            log_verdict(log_path, {**base_record(job), 'result': 'skipped',
                                   'reason': 'pending_replacement_unsafe'})
            return True, False
        staged_text, _, staged_status, staged_hash = card_state(target)
        if staged_status == 'verified' and staged_hash == job.get('sourceHash'):
            try:
                with cp.card_write_lock(root), wiki_mutation_lock.lock(root):
                    outcome = publish_pending_replacement(
                        root, config, job, target, live_target, expected)
                    if outcome == 'published':
                        wake_pending_retirement(root)
                        log_verdict(log_path, {**base_record(job), 'result': 'verified_replay'})
                        return True, False
                    if outcome == 'stale':
                        return finish_stale_pending(root, target, job, log_path)
            except OSError:
                pass
            return False, True

    text, meta, status, block_hash = card_state(target)
    if text is None:
        log_verdict(log_path, {**base_record(job), "result": "skipped", "reason": "card_missing"})
        return True, False
    if status != "generated" or meta.get("createdBy") != "qmd-auto-context":
        log_verdict(log_path, {**base_record(job), "result": "skipped", "reason": "not_generated"})
        return True, False
    job_hash = str(job.get("sourceHash") or "")
    if job_hash and block_hash and job_hash != block_hash:
        # 카드가 이 잡 이후 다시 컴파일됨 — 새 잡이 큐에 따로 있으므로 이 잡은 stale.
        log_verdict(log_path, {**base_record(job), "result": "skipped", "reason": "stale_job"})
        return True, False

    if selected_route is not None:
        # Old verify jobs may outlive a newly selected source-folder policy.
        # Do not send an excluded raw source or a mixed-source card to a host
        # model; keep its generated file recoverable and unverified.
        from laya_source_exclusion import card_sources_allowed, excluded_source
        skips = selected_route[0]['recallSettings']['skipPaths']
        if skips:
            job_revisions = job.get('sourceRevisions')
            sources = job.get('sources')
            normalized = [yaml_scalars.normalize_source_revision(item)
                          for item in job_revisions] if isinstance(job_revisions, list) else []
            unsafe = (not card_sources_allowed(text, skips) or not job_revisions or
                      any(item is None or excluded_source(item['path'], skips)
                          for item in normalized) or
                      not isinstance(sources, list) or
                      any(not isinstance(item, dict) or
                          (item.get('kind') == 'file' and
                           (not isinstance(item.get('path'), str) or
                            excluded_source(item['path'], skips)))
                          for item in sources))
        else:
            unsafe = False
        if unsafe:
            log_verdict(log_path, {**base_record(job), 'result': 'skipped',
                                   'reason': 'source_folder_excluded'})
            return True, False

    max_chars = int(compile_cfg.get("maxSourceChars", 12000) or 12000)
    snapshot_sources, freshness, span_evidence = snapshot_revision_evidence(
        root, config, job, max_chars)
    if freshness is not None and freshness.get("state") != wiki_freshness.FRESH:
        log_verdict(log_path, {
            **base_record(job), "result": "deferred", "reason": "source_changed_before_verify",
            "freshnessReason": freshness.get("reason", ""),
        })
        return False, True

    sources = load_sources(root, job, max_chars, initial_sources=snapshot_sources)
    if not sources:
        # 원문 없이는 대조 불가 — generated로 남겨 미검수 배지가 유지되게 한다. 카드는
        # 건드리지 않는다: 원문 소실 카드의 보관 후 삭제는 SessionStart `wiki_lifecycle`이
        # 기록된 `sourceRevisions`로 판정해 처리한다.
        log_verdict(log_path, {**base_record(job), "result": "skipped", "reason": "source_missing"})
        return True, False

    producing = job.get("engine") if isinstance(job.get("engine"), str) else ""
    cooled = cooling_engines(root)
    attempts, cross_mode, empty_reason = plan_verify_attempts(compile_cfg, vcfg, producing, cooled)
    if not attempts:
        log_verdict(log_path, {
            **base_record(job),
            "result": "deferred" if empty_reason != "missing_extractor" else "skipped",
            "reason": empty_reason, "crossEngine": cross_mode,
            "enginesCooling": sorted(cooled),
        })
        # 설정 자체가 없으면(missing_extractor) 기존대로 잡을 폐기한다. 나머지(식힘 중,
        # require가 후보를 걸러냄)는 설정/설치/시간의 함수이므로 잡을 보존한다 — 카드는
        # generated로 남고 삭제되지 않는다.
        return (True, False) if empty_reason == "missing_extractor" else (False, True)

    timeout = int(vcfg.get("timeout", 120) or 120)
    payload = {
        "task": "verify",
        "cwd": str(root),
        "engine": attempts[0]["engine"],
        "card": {"path": rel, "content": text},
        "sources": sources,
        "timeout": timeout,
        "_qmd": {"reasoningEffort": {
            "requested": qmd_config.resolve_reasoning_effort(compile_cfg, attempts[0]["engine"], "verify"),
            "capabilityDeclared": attempts[0]["argv"] == wcw._builtin_adapter_argv(attempts[0]["engine"]),
        }},
    }
    # 실패 분류 경계 — 두 요구를 동시에 만족시켜야 한다:
    #  (1) 한 run 안에서 같은 카드에 **유료 호출은 1회**다. 그래서 다음 후보로 넘어가는
    #      조건은 127(CLI 부재, host CLI를 실행하지도 못해 토큰 0)뿐이고, timeout·비127
    #      실패는 이미 호출한 것이라 그 run 에서 다른 엔진을 부르지 않는다.
    #  (2) 그러면서 **run 을 넘어가면 다음 후보로 degrade** 해야 한다. 실패한 후보를
    #      `set_engine_cooldown`으로 엔진 단위로 식혀 두면 다음 run 의 planner 가 그 후보를
    #      건너뛰고 다음 후보(최종적으로 생성 엔진)를 시도한다 — run 당 유료 호출은 여전히
    #      1회다. 전역 cooldown 이던 0.x 는 (1)만 지키고 (2)를 못 지켜, 선호 엔진이 계속
    #      실패하는 상태(인증 안 된 CLI 등)에서 검수가 영구 정지했다.
    # 어느 경로도 verdict 를 만들지 않으므로 transient 가 inconclusive(=삭제)로 흐를 수
    # 없다는 계약은 그대로다.
    attempted: list[str] = []
    attempt = attempts[0]
    has_more = False
    parsed = reason = returncode = None
    for index, attempt in enumerate(attempts):
        payload["engine"] = attempt["engine"]
        payload["_qmd"]["reasoningEffort"]["requested"] = qmd_config.resolve_reasoning_effort(
            compile_cfg, attempt["engine"], "verify"
        )
        payload["_qmd"]["reasoningEffort"]["capabilityDeclared"] = (
            attempt["argv"] == wcw._builtin_adapter_argv(attempt["engine"])
        )
        attempted.append(attempt["engine"])
        parsed, reason, returncode = wcw.run_extractor(attempt["argv"], payload, timeout, root)
        if returncode != 127:
            # 이 계획에 아직 시도하지 않은 후보가 남았는가 — 실패 처리의 degrade 판단에 쓴다.
            has_more = index + 1 < len(attempts)
            break
    if returncode == 127:
        # 과금 0인 경로지만 조용히 끝내지 않는다 — 어느 후보를 시도했는지 남지 않으면
        # "검수가 왜 안 되는가"를 사후에 규명할 수 없다.
        log_verdict(log_path, {
            **base_record(job), "result": "deferred", "reason": "extractor_unavailable",
            "crossEngine": cross_mode, "enginesAttempted": attempted,
            "enginesCooling": sorted(cooled),
        })
        return False, True  # 후보 전원 CLI 부재: 설치되면 재시도
    engine = attempt["engine"]
    verified_mode = attempt["mode"]
    # 실패 행에는 `attemptedMode`만 쓴다 — 실패한 시도의 mode는 "시도했던 것"이지
    # "달성한 것"이 아니고, `verifiedMode`가 실패 행에 남으면 노출 집계가 오독된다.
    provenance = {
        "engine": engine,
        "attemptedMode": verified_mode,
        "crossEngine": cross_mode,
        "enginesAttempted": attempted,
        # 식힘 중인 후보를 판정 줄에도 남긴다 — `self` 통과가 "다른 엔진이 없어서"인지
        # "다른 엔진이 실패해서 degrade한 것"인지 이 필드 없이는 구분되지 않는다.
        "enginesCooling": sorted(cooled),
        "_qmd": {"reasoningEffort": qmd_config.reasoning_effort_audit(
            parsed.get("_qmd", {}).get("reasoningEffort") if isinstance(parsed, dict) and isinstance(parsed.get("_qmd"), dict) else {},
            payload.get("_qmd", {}).get("reasoningEffort", {}).get("requested"),
            capability_declared=payload.get("_qmd", {}).get("reasoningEffort", {}).get("capabilityDeclared") is True,
        )},
    }
    if reason:
        return defer_or_drop(root, vcfg, job, attempt, has_more, provenance, reason, log_path)

    verdict = parsed.get("verdict") if isinstance(parsed, dict) else None
    if verdict not in VERDICT_VALUES:
        return defer_or_drop(root, vcfg, job, attempt, has_more, provenance, "invalid_verdict", log_path)
    reasons = parsed.get("reasons") if isinstance(parsed.get("reasons"), list) else []
    reasons = [str(item)[:200] for item in reasons[:5]]
    claims = parsed.get("claims") if isinstance(parsed.get("claims"), list) else []
    if verdict == "pass" and not any(isinstance(item, dict) and item for item in claims):
        # **`pass`는 근거를 요구한다.** `pass`가 `status: verified`의 유일한 입구이고
        # `verified`는 `recall.is_auto_trusted_card`를 통과해 캐논급으로 주입되므로, 근거 0인
        # `pass`는 곧 "무관한 요약이 trusted 카드가 된다"다(재현: 원문 `blue` · 카드 요약
        # `red` · verifier `{"verdict":"pass","claims":[]}` → `verified`). 자동 경로도 같은
        # 성질이지만 수동 경로는 caller 가 **어느 파일인가**까지 정하므로 노출이 넓다.
        #
        # **실패 방향이 중요하다 — `fail`/`inconclusive`로 강등하지 않는다.** 둘 다 기본
        # 동작이 카드 삭제(`onFail`/`onInconclusive` 기본 `delete`)인데 근거 없는 `pass`는
        # "원문과 모순"도 "판정 불가"도 아니고 **출력이 못 쓸 것**이다. `invalid_verdict`는
        # 카드를 지우지 않고 `generated`로 남긴다(= recall 비가시) — 안전한 방향이다.
        #
        # **어디까지 검사하는지와 그 근거는 둘로 갈린다.**
        #  - *비어 있지 않음*: 라이브 3프로젝트(ai-proxy·rccar·ktlo-check) `verify-log.jsonl`
        #    의 `pass` 319건이 전부 `claims > 0`이고 `pass + claims=0`은 0건이다 — 이 조건은
        #    실제 verifier 출력을 하나도 거부하지 않는다.
        #  - *항목이 비어 있지 않은 dict*: 라이브 근거가 없다(로그는 `len(claims)`만 남긴다).
        #    근거는 프롬프트 계약(`extractors/lib._VERIFY_PROMPT_TEMPLATE`의
        #    `{"claim","supported","quote","sourcePath"}`)과 `lib.emit_verdict`가 list 이기만
        #    하면 항목을 그대로 통과시킨다는 사실뿐이다. `[]`를 막고 `[null]`·`[""]`를 통과
        #    시키면 근거 요구가 이름만 남는다.
        #
        # **`supported`/`quote` 값까지는 보지 않는다.** (1) 그것은 프롬프트가 모델에 맡긴
        # verdict 규칙을 소비자에서 재구현하는 것이고, (2) **모양 검사는 퇴화한 출력을 거부할
        # 뿐 거짓말하는 adapter 를 막지 못한다** — `[{}]`를 만들 수 있는 adapter 는
        # `[{"claim":"x","supported":true,"quote":"y"}]`도 만든다. 그래서 경계를 "퇴화 거부"
        # 에서 멈춘다(더 깊은 검사는 방어를 늘리지 않고 카드 소실 위험만 늘린다).
        #
        # **`lib.emit_verdict`에서 exit 1로 막지 않는다.** 비127 종료는 transient 로 분류돼
        # 잡이 영구 보존되고 cooldown 만료마다 재호출된다(= 이 저장소가 억제 마커로 반복해서
        # 닫아 온 과금 루프). worker 쪽이 유일하게 옳은 층이고, 명시 argv/`backends` adapter
        # (lib 우회)도 여기서 함께 덮인다.
        return defer_or_drop(root, vcfg, job, attempt, has_more, provenance, "invalid_verdict", log_path)

    # 기존 pass claims 의 정확한 인용만 코드로 위치시킨다(추가 유료 호출·프롬프트 변경 없음).
    spans = source_spans.derive_spans(claims, span_evidence) if verdict == "pass" else []
    wiki_selected_laya.recheck(selected_route)
    try:
        with cp.card_write_lock(root), wiki_mutation_lock.lock(root):
            return apply_verify_verdict_locked(
                root, config, compile_cfg, vcfg, job, target, job_hash,
                # payload 로 보낸 파일 전문. 잠금 아래 다시 읽은 전문과 축자 대조해
                # 검수 중 카드 변경(본문·provenance·status 무엇이든)을 잡고, 통과하면
                # 이 텍스트에서 파생한 본문 지문이 카드에 찍힌다.
                text,
                engine, verified_mode, provenance, verdict, claims, reasons,
                sources, log_path, spans=spans,
            )
    except OSError:
        log_verdict(log_path, {
            **base_record(job), **provenance,
            "result": "deferred", "reason": "card_write_lock_unavailable",
        })
        return False, True


def defer_or_drop(
    root: Path, vcfg: dict, job: dict, attempt: dict, has_more: bool,
    provenance: dict, reason: str, log_path: Path,
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


def apply_negative_verdict(
    root: Path, config: dict, compile_cfg: dict, vcfg: dict, target: Path, engine: str,
    verified_mode: str, action: str, verdict: str, sources: list[dict], record: dict,
    log_path: Path, body_hash: str | None = None, *, pending_only: bool = False,
) -> bool:
    """Apply a fail/inconclusive verdict. Only inconclusive leaves a suppression marker.

    Returns whether the verdict was applied. `False` means the caller must preserve the
    job. Two things can block a delete, and both are ledgers that must exist before the
    card goes away: the durable deletion audit (`verify-deleted.jsonl`) and, for
    `inconclusive`, the billing-loop suppression marker (`verify-skipped.jsonl`).
    Machine verification is the one gate that removes cards.
    """
    if action == "delete":
        # 감사 레코드를 먼저 쓴다 — 여기서 죽으면 "설명 없는 삭제"가 아니라
        # "삭제되지 않은 카드에 대한 레코드 1줄"만 남는 쪽으로 실패해야 한다.
        # **반환값도 확인한다**(예외만 보면 안 된다): `False`면 원장에 한 줄도 남지 않으므로
        # 삭제를 **하지 않는다**. 미검수 카드가 하루 더 남는 것과 설명 없이 사라진 카드는
        # 등급이 다르다 — 후자는 복구도 사후 판단도 불가능하다(원장에 본문이 없다).
        # 잡을 보존해 다음 run이 다시 시도한다.
        if not record_verify_deletion(root, vcfg, sources, record, verdict):
            log_verdict(log_path, {**record, "result": "delete_blocked", "reason": "audit_ledger_unwritable"})
            return False
        # **억제 마커도 삭제 전에 쓰고 실패에 fail-closed한다.** fail은 판정이 결정적이라
        # 마커가 없어도 되지만, inconclusive("verifier가 판정 못함")는 같은 소스에서 재현될
        # 확률이 높아 마커 없이 지우면 재컴파일→재검수→재삭제가 반복되고 **매 반복이 유료
        # 호출**이다(재현: 안전영역 밖 `verify-skipped.jsonl` → 카드는 사라지고 삭제 원장만
        # 남은 채 unchanged source가 다시 처리됐다). 삭제 감사 원장에 이미 적용한 처방을
        # 여기에도 적용한다 — 삭제 전에 쓰고, 못 쓰면 지우지 않고 잡을 보존한다.
        # 순서는 감사 원장 → 억제 마커 → 삭제다. 억제 마커를 먼저 쓰면 감사 원장 실패 시
        # "지워지지도 않은 카드의 소스가 영구 억제"되어 그 소스가 다시는 컴파일되지 않는다.
        suppressed = 0
        if verdict == "inconclusive":
            suppressed, suppression_ok = record_verify_suppression(root, vcfg, sources, record, verdict)
            if not suppression_ok:
                log_verdict(log_path, {
                    **record, "result": "delete_blocked",
                    "reason": "suppression_ledger_unwritable", "suppressedSources": suppressed,
                })
                return False
        # tombstone은 세우지 않는다 — 소스가 고쳐지면 재컴파일→재검증이 다시 열려야 한다.
        # 제목은 **삭제 전에** 읽는다 — unlink 후에는 프론트매터를 더 못 읽는다.
        _, dead_meta, _, _ = card_state(target)
        dead_title = str(dead_meta.get("title") or target.stem)
        target.unlink(missing_ok=True)
        if pending_only:
            log_verdict(log_path, {**record, 'result': 'pending_deleted',
                                   'suppressedSources': suppressed})
            return True
        # 인덱스 줄도 같이 걷는다(사유는 wiki_compile.remove_from_index). 위 감사 원장·억제
        # 마커와 달리 여기는 fail-closed하지 않는다 — 인덱스는 캐논이 아니라 항법이고,
        # 실패해도 이 함수가 없던 시절의 어긋남으로 되돌아갈 뿐 새 손상은 아니다.
        wiki_root = (root / config.get("wikiPath", ".auto-context/wiki")).resolve()
        index_ok = wc.remove_from_index(wiki_root, target)
        wc.append_log(wiki_root, "deleted", target, dead_title)
        record_machine_delete(root, compile_cfg, record, verdict)
        reindex_wiki(root, config)
        log_verdict(log_path, {
            **record, "result": "deleted",
            "suppressedSources": suppressed, "indexOk": index_ok,
        })
        return True
    if action == "contested":
        # pass 경로와 같은 이유로 반환값을 확인한다. 여기서 실패하면 카드가 `generated`로
        # 남아 recall에서 빠지므로(비가시) 조용히 넘기면 안 된다. 이 함수는 (processed,
        # preserve)를 돌려주지 않으므로 실패 사실을 로그로만 표면화한다 — 다음 소스 변경이
        # 카드를 재생성하고 다시 검수 대상이 된다.
        if not wc.stamp_verification(target, "contested", engine, verified_mode, body_hash):
            log_verdict(log_path, {**record, "result": "stamp_failed", "stampStatus": "contested"})
            return False
        reindex_wiki(root, config)
        log_verdict(log_path, {**record, "result": "contested"})
        return True
    # none: 카드를 건드리지 않는다. inconclusive에서는 이것이 0.x 하위호환 경로 —
    # generated로 남아 recallVerifiedOnly 기본값 아래 recall에서 제외된다.
    log_verdict(log_path, {**record, "result": "inconclusive" if verdict == "inconclusive" else "kept"})
    return True


def _dedup_by_target(rows: list) -> list:
    """targetPath 기준 latest-wins — 같은 카드에 잡이 여러 개면 최신만 검증."""
    latest: dict = {}
    order: list = []
    for raw_line, job in rows:
        if job is None:
            continue
        key = job.get("targetPath", "")
        ts = wcw._parse_ts(job.get("ts")) or 0.0
        if key not in latest:
            order.append(key)
            latest[key] = (raw_line, job, ts)
        elif ts >= latest[key][2]:
            latest[key] = (raw_line, job, ts)
    return [(latest[key][0], latest[key][1]) for key in order]


def run(
    root: Path, config: dict, compile_cfg: dict, produced_targets: list[str] | None = None
) -> dict:
    """Drain (part of) the verify queue. Caller has already passed compile gating.

    `produced_targets` = card paths the CALLING run just enqueued. Two things depend
    on it and both were wrong before:

    **Budget.** A fixed `maxPerRun` makes the queue grow faster than it drains
    whenever a run compiles more cards than it verifies, and a `generated` card is
    invisible to recall under the default `compile.recallVerifiedOnly: true` — so
    editing ten documents left accurate cards unusable for several runs. The budget
    is therefore `max(maxPerRun, min(len(produced_targets), VERIFY_PRODUCED_HARD_CAP))`:
      - a human's explicit `maxPerRun` is always honoured (it is a human decision),
      - the part derived from **model output** is capped, because `produced` comes
        from `len(candidates)` and the model decides that number. Without the cap a
        single degenerate extractor response (40 cards) drove 200 paid verify calls
        in one run — the model deciding how much the user is billed.

    **Order.** Sizing the budget is not enough: the queue is FIFO, so `kept[:budget]`
    spent the enlarged budget on the OLDEST backlog and left this run's own cards
    `generated` — the exact outcome the enlargement was meant to prevent (measured:
    4 backlog + 2 new, budget 2 → the 2 backlog cards verified, both new ones not).
    This run's cards are processed first; leftover budget drains backlog, and one
    slot is always reserved for backlog so it cannot starve during a bulk period.
    """
    result = {"processed": 0, "remaining": 0}
    vcfg = verify_cfg_of(compile_cfg)
    import wiki_selected_laya
    selected_route = wiki_selected_laya.selected(root)
    if selected_route is not None:
        # Selected Laya keeps wiki bytes for recovery. The legacy default
        # machine-delete verdict is never inherited by an installed route.
        vcfg = {**vcfg, 'onFail': 'none', 'onInconclusive': 'none'}
    if not vcfg.get("enabled", True):
        return result
    queue = _safe_queue_path(root, VERIFY_QUEUE_DEFAULT)
    if queue is None or not queue.exists():
        return result
    log_path = cp.ledger(root, cp.VERIFY_LOG) or (root / cp.COMPILE_DIR / cp.VERIFY_LOG)

    # **유료 호출 전 preflight: fail-closed 원장에 쓸 수 없으면 검수하지 않는다.**
    # fail-closed 삭제(원장 없이는 지우지 않는다)는 유지하지만 그 실패에 종점이 없으면
    # 과금 루프가 된다 — 판정 1회(유료) → 원장 실패 → 잡 보존 → 다음 run 재판정(유료)…
    # 큐를 claim하기 **전에** 막으므로 host CLI 호출이 0회이고 잡·카드는 그대로다.
    # 대상은 삭제 감사 원장과 inconclusive 억제 마커 **둘 다**다(preflight_block_reason).
    # 사용자 표면화는 SessionStart(`core/update.sh`)가 같은 함수를 돌려 notice_once로 한다.
    blocked = preflight_block_reason(root, vcfg)
    if blocked:
        result["reason"] = blocked
        return result

    # 0.x 전역 cooldown 파일의 고아 정리 — 이제 읽지도 쓰지도 않으므로 남아 있으면 "검수가
    # 식힘 중"이라는 잘못된 인상만 준다. 후보 단위 파일로 대체됐다(engine_cooldown_path).
    (root / cp.COMPILE_DIR / cp.LEGACY_VERIFY_COOLDOWN).unlink(missing_ok=True)

    claimed = wcw.claim_queue(queue)
    if claimed is None:
        return result
    rows = wcw.read_queue(claimed)
    if not rows:
        claimed.unlink(missing_ok=True)
        queue.touch(exist_ok=True)
        return result

    malformed = [raw for raw, job in rows if job is None]
    kept = _dedup_by_target(rows)
    max_per_run = max(1, min(
        int(wcw.budget_of(compile_cfg).get("verifyPerRun", 3) or 3), qmd_config.MAX_VERIFY_PER_RUN
    ))
    produced = {t for t in (produced_targets or []) if isinstance(t, str) and t}
    budget = max(max_per_run, min(len(produced), qmd_config.VERIFY_PRODUCED_HARD_CAP))
    # 이 run이 만든 카드 먼저, 그 다음 backlog. 큐 순서를 바꾸는 것이 아니라 **처리 순서**만
    # 정한다(`kept`는 이미 `_dedup_by_target`가 targetPath별 latest-wins로 재구성한 목록이고
    # FIFO는 계약이 아니다). 처리되지 않은 줄은 전부 `remaining`으로 되돌아가므로 유실이 없다.
    fresh = [item for item in kept if item[1].get("targetPath") in produced]
    backlog = [item for item in kept if item[1].get("targetPath") not in produced]
    take_fresh = fresh[:budget]
    leftover = budget - len(take_fresh)
    if leftover == 0 and backlog:
        # **기아 방지 예약 1건.** 생산량이 예산을 다 먹는 run이 연속되면 backlog가 영원히
        # 밀린다. 유료 호출 1회를 더 쓰는 대신 backlog가 항상 전진한다(상한은 여전히 유계:
        # budget + 1).
        leftover = 1
    take_backlog = backlog[:leftover]
    to_process = take_fresh + take_backlog
    deferred = [raw for raw, _ in fresh[len(take_fresh):]] + [raw for raw, _ in backlog[len(take_backlog):]]
    remaining = deferred + malformed
    processed_count = 0
    try:
        for idx, (raw_line, job) in enumerate(to_process):
            try:
                wiki_selected_laya.recheck(selected_route)
                processed, preserve = process_verify_job(root, config, compile_cfg, vcfg, job, log_path)
            except Exception:
                remaining.append(raw_line)
                remaining.extend(line for line, _ in to_process[idx + 1:])
                raise
            if processed:
                processed_count += 1
            if preserve:
                remaining.append(raw_line)
    finally:
        wcw.requeue_lines(queue, remaining)
        claimed.unlink(missing_ok=True)
        queue.touch(exist_ok=True)
    result["processed"] = processed_count
    result["remaining"] = len(remaining)
    result["budget"] = budget
    result["freshProcessed"] = len(take_fresh)
    result["backlogProcessed"] = len(take_backlog)
    # 모델 출력이 예산을 밀어올리려다 상한에 걸린 사실은 관측 가능해야 한다 — 이 줄이 없으면
    # "카드를 40장 만들었는데 30장만 검수됐다"가 조용한 상태로 남는다.
    if len(produced) > qmd_config.VERIFY_PRODUCED_HARD_CAP:
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

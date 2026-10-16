#!/usr/bin/env python3
"""LLM body-vs-body duplicate judge shared by both dedup paths.

WHY this exists (measured, 2026-07-29): the daemon's `score` is NOT a similarity,
so no threshold on it can express "these two cards say the same thing". With
rerank=True qmd blends the RRF position into the score
(`blendedScore = w*(1/rrfRank) + (1-w)*rerankScore`, w=0.75 for rank<=3), so the
achievable score is bounded BY RANK: rank1 in [0.75,1.0], rank2 in [0.375,0.625],
rank3 in [0.25,0.5]. Measured on a real 125-card wiki the bands were rank1
{0.88,0.93} / rank2 {0.55,0.56} / rank3 [0.40,0.44] -- a spread of 0.01-0.04
within a rank against a 0.37 gap between ranks. Since a page's own body always
retrieves itself at rank 1, a true duplicate can only appear at rank>=2, where
`autoMergeThreshold`'s 0.9 default is mathematically unreachable and any value
that does fire degenerates into "queue whatever is at rank 2".

So: vector search is used for CANDIDATE RETRIEVAL only, and the verdict comes
from this judge, which hands both card bodies to the same host-CLI adapter pool
used for extraction and verification (payload {"task": "dedup"}).

The judge NEVER merges or deletes anything. A "duplicate" verdict only routes the
pair into a review queue for a human or the resolver agent to act on.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.resolve()))
import compile_paths as cp
import config as qmd_config
import cooldown as qmd_cooldown

VERDICT_VALUES = {"duplicate", "distinct", "unclear"}

# outcome values returned alongside a verdict:
#   "ok"          -> verdict is authoritative
#   "unavailable" -> no judge on this machine (no extractor configured, or CLI
#                    absent). PERMANENT for this run: callers fall back to their
#                    legacy score-threshold behavior instead of stalling.
#   "transient"   -> timeout / crash / cooldown. Callers must preserve work for a
#                    later run rather than deciding without a judgment.
OUTCOME_OK = "ok"
OUTCOME_UNAVAILABLE = "unavailable"
OUTCOME_TRANSIENT = "transient"

# 유료 호출은 났는데 **쓸 수 있는 출력이 아니었던** 사유. `wiki_verify_worker`의 동명 집합과
# 같은 의미이고 같은 처방을 받는다(`_unusable_output` ↔ verify `defer_or_drop`): 같은 입력에서
# 재현되는 설정·adapter 오류라, 남은 후보가 있으면 degrade하고 후보가 소진되면 **종점**을 만든다.
# timeout·실행 실패는 여기 들어가지 않는다(진짜 transient라 종점 없이 항상 보존).
UNUSABLE_OUTPUT_REASONS = {"invalid_extractor_json", "invalid_verdict"}

# 귀속 가능성은 **호출 경로의 성질**이지 이번 호출의 운이 아니다. 이 구분이 없으면
# `producing=""` 하나가 두 세계를 뭉친다 — (a) write-time인데 이번 카드의 생산 엔진을 못 읽음
# (사용자가 고친다 = require를 지킬 여지가 있다), (b) retroactive scan(생산자라는 사실이
# 애초에 존재하지 않는다 = require를 지킬 여지가 영원히 없다).
ATTRIBUTION_JOB = "job"    # 생산 엔진이 큐 잡에서 온 사실 (write-time gate)
ATTRIBUTION_NONE = "none"  # 카드 두 장, 둘째 생산자 미기록 (retroactive scan)

# `diagnostics()`가 남기는 필드. 값이 있는 것만 나간다.
DIAGNOSTIC_KEYS = (
    "outcome", "reason", "engine", "mode", "producedBy", "crossEngine", "crossEngineWaived",
    "enginesAttempted", "enginesCooling", "engineCooldown", "cooldownWriteFailed", "unusableOutput",
    "_qmd",
)
# degrade·차단이 실제로 일어났다는 신호. 이게 있으면 판정이 성공했어도 보고한다.
_NOTABLE_KEYS = ("crossEngineWaived", "engineCooldown", "cooldownWriteFailed", "unusableOutput")

DEFAULT_MAX_CHARS = 6000
DEFAULT_TIMEOUT = 120
DEFAULT_COOLDOWN_SECONDS = 600
DEFAULT_MAX_PAIRS_PER_SCAN = 8
DEFAULT_MAX_PAIRS_PER_COMPILE = 1


def judge_cfg_of(compile_cfg: dict) -> dict:
    semantic = compile_cfg.get("semanticDedup")
    semantic = semantic if isinstance(semantic, dict) else {}
    raw = semantic.get("judge")
    return raw if isinstance(raw, dict) else {}


def is_enabled(compile_cfg: dict) -> bool:
    # QMD_DEDUP_JUDGE=off is a process-wide kill switch (tests, opt-out debugging).
    if os.environ.get("QMD_DEDUP_JUDGE") == "off":
        return False
    return judge_cfg_of(compile_cfg).get("enabled", True) is not False


def _budget(compile_cfg: dict) -> dict:
    raw = compile_cfg.get("budget") if isinstance(compile_cfg, dict) else None
    return raw if isinstance(raw, dict) else {}


def max_pairs_per_scan(compile_cfg: dict) -> int:
    # 유료 호출 예산은 `compile.budget`이 SSOT다(예전 `semanticDedup.judge.maxPairsPerScan`).
    return max(0, _int(_budget(compile_cfg).get("dedupPairsPerScan"), DEFAULT_MAX_PAIRS_PER_SCAN))


def max_pairs_per_compile(compile_cfg: dict) -> int:
    return max(0, _int(_budget(compile_cfg).get("dedupPairsPerCompile"), DEFAULT_MAX_PAIRS_PER_COMPILE))


def _int(value, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def cross_engine_mode(compile_cfg: dict) -> str:
    mode = judge_cfg_of(compile_cfg).get("crossEngine", "prefer")
    return mode if mode in qmd_config.CROSS_ENGINE_MODES else "prefer"


def require_waived(compile_cfg: dict, attribution: str) -> bool:
    """`crossEngine: "require"`가 이 경로에 **적용되지 않는가**.

    `require`는 "생성 엔진이 **아닌** 엔진이 판정한다"는 약속이다. retroactive scan에는 그
    약속의 주체가 없다 — 이미 디스크에 있는 카드 두 장을 비교하고 둘째 카드의 생산자는 어디에도
    기록되지 않는다(라이브 실측: judge가 돈 12쌍 중 양쪽 생산자가 기록된 쌍 0). 즉 **구조적으로
    만족될 수 없는 조건**이고, 거기에 fail-closed를 걸면 그 경로의 판정이 통째로 사라진다
    (`is_available`→False → `wiki_dedup_scan`이 retrieval floor를 `autoMergeThreshold`로 올려
    실질적으로 아무것도 큐하지 않는다 = 이 모듈이 대체하려고 만들어진 그 무료 게이트로 회귀).
    그래서 이 경로에서는 require를 적용하지 않고 `prefer`와 같은 기존 순서를 쓴다.

    **조용히 넘기지 않는다**: 이 함수가 규칙의 한 곳이고, `judge_pair`가
    `crossEngineWaived`로, `wiki_dedup_scan`이 로그(요약 줄 + 판정 줄)로 그 사실을 남긴다.
    write-time gate(`ATTRIBUTION_JOB`)는 그대로 fail-closed다 — 거기서는 생산 엔진이 큐 잡의
    사실이라 require가 만족될 수 있고, 못 지키는 경우의 종점도 레거시 score 게이트로 존재한다.
    """
    return attribution == ATTRIBUTION_NONE and cross_engine_mode(compile_cfg) == "require"


def diagnostics(info: dict) -> dict:
    """판정이 안 나왔거나 degrade가 일어난 사실을 남길 최소 필드(없으면 빈 dict).

    **transient·식힘의 종점이다.** `_back_off`가 돌려주는 `engineCooldown`/
    `cooldownWriteFailed`를 읽는 소비자가 저장소에 하나도 없던 동안 dedup의 유료 실패는 어디에도
    기록되지 않아 "판정 실패"와 "unclear 판정"이 사후에 구분되지 않았고, 두 식힘 쓰기가 모두
    실패하면 write-time이 compile마다 같은 쌍을 재과금하는데 흔적이 0이었다. verify는 같은 사실을
    `verify-log.jsonl`의 `deferred` 행(+`cooldownWriteFailed`)으로 남긴다 — 같은 처방이다.

    후보를 **하나도 세우지 못한** 상시 상태(`judge_unavailable`/`missing_extractor`)는 degrade가
    아니라 이 머신의 설정이므로 빈 dict를 돌려준다 — 매 행에 남기면 잡음만 늘고 기존 행 모양이
    바뀐다.
    """
    notable = any(info.get(key) for key in _NOTABLE_KEYS)
    if info.get("outcome") == OUTCOME_OK and not notable and not info.get("_qmd"):
        return {}
    if not (info.get("enginesAttempted") or info.get("enginesCooling") or notable):
        return {}
    return {key: info[key] for key in DIAGNOSTIC_KEYS if info.get(key)}


def cooldown_path(root: Path) -> Path:
    # Separate from the compile and verify cooldowns: an extractor outage must not
    # silence dedup judging, and vice versa.
    return root / cp.COMPILE_DIR / cp.DEDUP_JUDGE_COOLDOWN


def engine_cooldown_path(root: Path) -> Path:
    """엔진 단위 식힘. **전역 `dedup-judge-cooldown`과 역할이 다르다**: 전역은 "judge 전체를
    잠시 쉰다"이고 이것은 "이 후보로는 다음 run에 degrade한다"다. 교차 엔진 우선을 켜면
    선호 후보가 계속 유료 실패하는 상태(인증 안 된 두 번째 CLI)가 생길 수 있고, 전역 식힘만
    있으면 만료마다 같은 후보를 다시 불러 **판정이 영구 정지**한다 — verify가 0.x 전역 식힘에서
    겪은 것과 같은 클래스라 같은 처방(엔진 단위 기록)을 쓴다. 파일은 verify와 분리한다.
    """
    return root / cp.COMPILE_DIR / cp.DEDUP_JUDGE_ENGINE_COOLDOWN


def cooldown_active(root: Path) -> bool:
    """판정은 `cooldown.expiry_active`가 SSOT다(compile cooldown과 같은 규칙).
    `now < float(파일내용)`만 보면 오염된 만료값 하나로 **판정이 영구 정지**한다 —
    전역 식힘이 후보 소진의 종점이므로 그 정지에는 복구 경로가 없다."""
    try:
        raw = cooldown_path(root).read_text(encoding="utf-8").strip()
    except OSError:
        return False
    return qmd_cooldown.expiry_active(raw, datetime.now(timezone.utc).timestamp())


def set_cooldown(root: Path, seconds: int) -> bool:
    """전역 식힘 기록. 반환값은 "기록이 남았는가"다 — 이것이 마지막 종점이므로 실패는
    호출자가 표면화한다(기록이 없으면 다음 run이 같은 후보를 다시 유료로 부른다)."""
    path = cooldown_path(root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # 쓰기도 읽기와 같은 상한으로 클램프한다(`cooldown.expiry_value`).
        expiry = qmd_cooldown.expiry_value(datetime.now(timezone.utc).timestamp(), seconds)
        path.write_text(f"{expiry}\n", encoding="utf-8")
        return True
    except OSError:
        return False


def judge_engine_pool(compile_cfg: dict) -> list[str]:
    """Engines that may judge, in preference order — host engine first when it is
    actually configured, then `wcw.extractor_engine_pool`.

    This is the whole 0.x `resolve_engine()` order minus its caller hint (the hint now
    marks the engine to AVOID, not the one to pick). Keeping it matters for the
    retroactive scan, which has no producer to prefer against, so this IS its choice:
    changing the order would make two hosts judge the same project with different
    engines. It also keeps the measured regression fixed — a project configuring
    `extractor.backends` WITHOUT `builtins` (the shape both dogfood projects use) must
    still resolve, or the judge reports itself permanently unavailable and every scan
    silently degrades to the legacy score gate it exists to replace.

    QMD_ENGINE is honored only when that engine is actually configured; an unconfigured
    host (gemini on a claude/codex-only project) must not resolve to a dead label.
    """
    import wiki_compile_worker as wcw

    pool = wcw.extractor_engine_pool(compile_cfg)
    env = os.environ.get("QMD_ENGINE", "")
    if env and env in pool:
        pool = [env] + [engine for engine in pool if engine != env]
    return pool


def plan_judge_attempts(
    compile_cfg: dict, producing: str = "", cooled=(), attribution: str = ATTRIBUTION_JOB
) -> tuple[list[dict], str, str]:
    """Ordered judge attempts → ([{engine, argv, mode, key}], crossEngine mode, empty reason).

    **Why this is not `wiki_verify_worker.plan_verify_attempts`.** Verify judges ONE card
    and only needs that card's producer, so "is this self-review" is decidable there.
    Dedup compares TWO cards, and the second card's producer is usually not recorded at
    all (measured: 0 of 12 judged pairs in the live project had both producers in
    `generated-manifest.jsonl`). So only the write-time gate — where the new candidate's
    producer is a fact assigned by the worker from the queue job, never model output —
    can claim anything about self-review, and it is the only path that passes `producing`.
    The retroactive scan passes `attribution=ATTRIBUTION_NONE`, lands in the unattributable
    branch, and keeps its previous engine order exactly.

    **`attribution` is a different question from `producing`.** An empty `producing` under
    `ATTRIBUTION_JOB` means "this card's producer could not be read" — a config/install state
    the user can fix, so `require` still fails CLOSED there (the caller then degrades to its
    legacy score gate). `ATTRIBUTION_NONE` means no producer exists as a fact on that path at
    all, so `require` can never be satisfied and gating on it deleted the entire retroactive
    verdict instead of gating anything. See `require_waived` for the rule and its endpoint.

    On top of that, verify reads `compile.verify.*`, labels attempts for `verifiedMode`,
    and maps an unsatisfiable `require` onto "preserve the queued job" — none of which
    exists here. What IS shared is the ORDER rule (`wcw.plan_engine_order`).

    `cooled` are cooldown keys whose last PAID call failed; skipping them is what makes
    the next run degrade to the next candidate instead of re-billing the same failure.
    """
    import wiki_compile_worker as wcw

    mode = cross_engine_mode(compile_cfg)
    # 귀속이 **경로 차원에서** 불가능하면 require를 적용하지 않는다(`require_waived` 참조).
    # 순서는 prefer와 같은 것으로 명시한다 — 지금은 `plan_engine_order`가 두 모드의
    # unattributable 분기에서 같은 값(`list(pool)`)을 내지만, 그 우연에 의존하지 않는다.
    enforce_require = mode == "require" and attribution != ATTRIBUTION_NONE
    order_mode = mode if (mode != "require" or enforce_require) else "prefer"
    cooled = set(cooled)
    # `extractor.argv`(모든 엔진 공용 단일 argv)·`extractor.default`(엔진 무관 폴백)는
    # 스키마에서 제거됐다 — 둘 다 엔진 귀속이 불가해 교차 주장을 할 수 없었고, 그 분기가
    # 여기와 `plan_verify_attempts`에 각각 있었다. 이제 모든 argv는 이름 있는 엔진에서 온다.
    pool = judge_engine_pool(compile_cfg)
    attributable = bool(
        producing and producing != qmd_config.UNKNOWN_ENGINE
        and wcw.resolve_extractor_argv(compile_cfg, producing) is not None
    )
    if enforce_require and not attributable:
        # 어느 후보도 "생성 엔진과 다르다"를 증명할 수 없다 → 약속을 지킬 수 없으므로 거부.
        return [], mode, "cross_engine_unavailable"

    order = wcw.plan_engine_order(pool, producing, order_mode, attributable)
    attempts: list[dict] = []
    seen_argv: list[list[str]] = []
    skipped_cooling = 0
    for engine in order:
        primary = wcw.resolve_extractor_argv(compile_cfg, engine)
        # 두 라벨이 **같은 argv**를 가리키면 뒤 라벨이 탈락하고, 남은 라벨이 교차를 주장한다.
        # 그대로 둔다(판정 2026-07-31): payload `engine`이 그 argv에 전달되므로 by-engine
        # dispatch wrapper 에서는 실제로 다른 엔진이 돌고, 같은 스크립트라면 틀리는 것은
        # provenance 라벨 하나이며 유료 호출 수·안전에는 영향이 없다. 무엇보다 이 argv 중복
        # 제거는 `plan_verify_attempts`와 **같은 규칙**이다 — 한쪽만 고치면 두 판정 경로가
        # 갈리고, 공용화한 것이 순서 규칙뿐이라 그 갈림을 잡아 줄 곳이 없다.
        if primary is None or primary in seen_argv:
            continue
        seen_argv.append(primary)
        if engine in cooled:
            skipped_cooling += 1
            continue
        if not attributable:
            attempt_mode = qmd_config.VERIFIED_MODE_UNKNOWN
        elif engine == producing:
            attempt_mode = qmd_config.VERIFIED_MODE_SELF
        else:
            attempt_mode = qmd_config.VERIFIED_MODE_CROSS
        attempts.append({"engine": engine, "argv": primary, "mode": attempt_mode, "key": engine})
    if attempts:
        return attempts, mode, ""
    if skipped_cooling:
        return [], mode, "engines_cooling"
    if enforce_require and attributable:
        return [], mode, "cross_engine_unavailable"
    return [], mode, "missing_extractor"


def is_available(compile_cfg: dict, engine: str = "", attribution: str = ATTRIBUTION_JOB) -> bool:
    """Config-only check that at least one judge candidate resolves.

    Callers use it to skip the whole judge path — including the daemon retrieval
    query — on machines with no host CLI configured, so behavior there stays
    byte-identical to the pre-judge code path. Cooldowns are deliberately NOT consulted:
    they are temporal state, and `judge_pair` maps them onto `transient`.

    Callers MUST pass the same `attribution` they will pass to `judge_pair`: this is the
    gate that decides whether the daemon retrieval query happens at all, so a mismatch
    would report "no judge" for a path that would in fact have judged (that mismatch is
    exactly how `require` silenced the retroactive scan).
    """
    if not is_enabled(compile_cfg):
        return False
    attempts, _, _ = plan_judge_attempts(compile_cfg, engine, attribution=attribution)
    return bool(attempts)


def judge_pair(
    root: Path,
    compile_cfg: dict,
    page_a: dict,
    page_b: dict,
    engine: str = "",
    attribution: str = ATTRIBUTION_JOB,
) -> tuple[str | None, dict]:
    """Ask the host CLI whether two card bodies record the same fact.

    page_a/page_b: {"path": <display path>, "content": <full card text>}
    `engine` is the PRODUCING engine of page_a when the caller knows it (write-time
    gate); it is now used to prefer a DIFFERENT judge, not to select this one.

    Returns (verdict_or_None, info) where info carries `outcome`, `engine` (who judged),
    `mode` (self / cross-engine / unknown), `producedBy`, `reason`, and the judge's
    `uniqueToA`/`uniqueToB` lists. verdict is None for every non-ok outcome -- callers
    must branch on outcome, never on truthiness.

    **Paid calls stay at exactly one per pair.** The candidate list is walked only while
    `run_extractor` returns 127 (host CLI absent — the binary never ran, so no tokens
    were billed); the first candidate that actually executes ends the walk.

    `attribution` says whether a producing engine exists as a fact on this path at all
    (`ATTRIBUTION_JOB` for the write-time gate, `ATTRIBUTION_NONE` for the retroactive
    scan). It gates whether `crossEngine: "require"` applies — see `require_waived`.
    """
    # Lazy import: wiki_compile_worker imports wiki_compile, which imports this
    # module -- a module-level import here would close the cycle.
    import wiki_compile_worker as wcw

    if not is_enabled(compile_cfg):
        return None, {"outcome": OUTCOME_UNAVAILABLE, "reason": "judge_disabled"}

    jcfg = judge_cfg_of(compile_cfg)
    producing = engine if isinstance(engine, str) else ""
    cooled = wcw.cooling_engines(engine_cooldown_path(root))
    attempts, mode, empty_reason = plan_judge_attempts(
        compile_cfg, producing, cooled, attribution=attribution
    )
    provenance = {
        "engine": "", "mode": qmd_config.VERIFIED_MODE_UNKNOWN, "producedBy": producing,
        # `crossEngine`은 **설정된 값**이다. 그 값이 이 경로에 적용되지 않았다면 그 사실을
        # 별도 필드로 남긴다 — 설정을 조용히 다른 값으로 보고하면(`off`) 무엇이 설정이고 무엇이
        # 코드 판단인지 사후에 구분할 수 없다.
        "crossEngine": mode, "enginesAttempted": [], "enginesCooling": sorted(cooled),
        **({"crossEngineWaived": "unattributable_path"}
           if require_waived(compile_cfg, attribution) else {}),
    }
    if not attempts:
        # 분류 경계: 식힘은 **시간**의 함수라 transient(작업 보존), 나머지(설정 없음,
        # require를 만족하는 후보 없음)는 이 머신의 상태라 unavailable(레거시 게이트로 degrade).
        # 이 경계를 잘못 두면 (a) 식힘을 unavailable로 보면 판정 없이 score 게이트로 조용히
        # 내려가고, (b) 설정 부재를 transient로 보면 scan이 매번 같은 자리에서 멈춘다.
        outcome = OUTCOME_TRANSIENT if empty_reason == "engines_cooling" else OUTCOME_UNAVAILABLE
        return None, {**provenance, "outcome": outcome, "reason": empty_reason}
    if cooldown_active(root):
        return None, {**provenance, "outcome": OUTCOME_TRANSIENT, "reason": "cooldown"}

    max_chars = max(500, _int(jcfg.get("maxCharsPerPage"), DEFAULT_MAX_CHARS))
    timeout = max(1, _int(jcfg.get("timeout"), DEFAULT_TIMEOUT))
    payload = {
        "task": "dedup",
        "cwd": str(root),
        "engine": attempts[0]["engine"],
        "pageA": {"path": str(page_a.get("path", "")), "content": str(page_a.get("content", ""))[:max_chars]},
        "pageB": {"path": str(page_b.get("path", "")), "content": str(page_b.get("content", ""))[:max_chars]},
        "timeout": timeout,
        "_qmd": {"reasoningEffort": {
            "requested": qmd_config.resolve_reasoning_effort(compile_cfg, attempts[0]["engine"], "semanticDedup"),
            "capabilityDeclared": attempts[0]["argv"] == wcw._builtin_adapter_argv(attempts[0]["engine"]),
        }},
    }

    attempted: list[str] = []
    attempt = attempts[0]
    has_more = False
    parsed = reason = returncode = None
    for index, candidate in enumerate(attempts):
        attempt = candidate
        payload["engine"] = candidate["engine"]
        payload["_qmd"]["reasoningEffort"]["requested"] = qmd_config.resolve_reasoning_effort(
            compile_cfg, candidate["engine"], "semanticDedup"
        )
        payload["_qmd"]["reasoningEffort"]["capabilityDeclared"] = (
            candidate["argv"] == wcw._builtin_adapter_argv(candidate["engine"])
        )
        attempted.append(candidate["engine"])
        parsed, reason, returncode = wcw.run_extractor(candidate["argv"], payload, timeout, root)
        if returncode != 127:
            has_more = index + 1 < len(attempts)
            break
    provenance = {
        **provenance,
        "engine": attempt["engine"], "mode": attempt["mode"], "enginesAttempted": attempted,
        "_qmd": {"reasoningEffort": qmd_config.reasoning_effort_audit(
            parsed.get("_qmd", {}).get("reasoningEffort") if isinstance(parsed, dict) and isinstance(parsed.get("_qmd"), dict) else {},
            payload.get("_qmd", {}).get("reasoningEffort", {}).get("requested"),
            capability_declared=payload.get("_qmd", {}).get("reasoningEffort", {}).get("capabilityDeclared") is True,
        )},
    }
    if returncode == 127:
        # 후보 전원 CLI 부재: 이 머신에서는 어떤 judge도 답하지 않는다(과금 0).
        return None, {**provenance, "outcome": OUTCOME_UNAVAILABLE, "reason": "cli_absent"}
    if reason == 'resource_deferred':
        return None, {**provenance,'outcome':OUTCOME_TRANSIENT,'reason':reason}
    if reason and reason not in UNUSABLE_OUTPUT_REASONS:
        # timeout·실행 실패 = 진짜 transient(부하·네트워크·일시적 CLI 상태). 판정이 없었으므로
        # 종점 없이 항상 작업을 보존한다 — verify가 같은 사유를 항상 보존하는 것과 같다.
        return None, {
            **provenance, "outcome": OUTCOME_TRANSIENT, "reason": reason,
            **_back_off(root, jcfg, attempt["key"], has_more),
        }

    verdict = parsed.get("verdict") if isinstance(parsed, dict) else None
    unusable = reason or ("" if verdict in VERDICT_VALUES else "invalid_verdict")
    if unusable:
        return _unusable_output(root, jcfg, provenance, attempt, has_more, unusable)

    def strings(key: str) -> list[str]:
        raw = parsed.get(key)
        return [str(item)[:300] for item in raw[:10]] if isinstance(raw, list) else []

    return verdict, {
        **provenance,
        "outcome": OUTCOME_OK,
        "verdict": verdict,
        "reason": str(parsed.get("reason") or "")[:500],
        "uniqueToA": strings("uniqueToA"),
        "uniqueToB": strings("uniqueToB"),
    }


def _unusable_output(
    root: Path, jcfg: dict, provenance: dict, attempt: dict, has_more: bool, reason: str
) -> tuple[str | None, dict]:
    """유료 호출을 소진했는데 **쓸 수 있는 출력이 아니었던** 경우. verify `defer_or_drop`와
    같은 처방이고 갈리는 것은 종점의 모양뿐이다(verify는 잡을 폐기, dedup은 쌍을 억제).

    `invalid_extractor_json`/`invalid_verdict`는 timeout과 달리 **같은 입력에서 재현되는**
    설정·adapter 오류다. 그래서:
      - 남은 후보가 있으면 그 후보만 식히고 `transient`로 돌려 작업을 보존한다 → 다음 run이
        **다음 후보로 degrade**한다(그 후보가 정상이면 판정을 얻는다).
      - 후보가 소진되면 **종점을 만든다**: `unclear`를 유효 판정(`OUTCOME_OK`)으로 돌려
        호출자가 억제 마커를 남기게 한다. 이것이 없던 동안 같은 쌍이 매 scan 재판정됐고
        (`dedup-skipped.jsonl`은 `distinct`만 기록했다) 매 반복이 사용자 계정 과금이었다 —
        이 저장소가 verify 원장으로 반복해서 닫아 온 클래스다.

    후보 소진 경로에서도 `_back_off`를 호출해 전역 식힘을 건다. 쓰레기를 내는 adapter는 다음
    쌍에도 같은 출력을 내므로, 식히지 않으면 그 한 번의 오설정이 이번 scan의 `maxPairsPerScan`
    예산을 전부 태운다(쌍 억제는 **같은** 쌍의 재과금만 막는다).
    """
    if has_more:
        return None, {
            **provenance, "outcome": OUTCOME_TRANSIENT, "reason": reason, "unusableOutput": True,
            **_back_off(root, jcfg, attempt["key"], True),
        }
    return "unclear", {
        **provenance, "outcome": OUTCOME_OK, "verdict": "unclear", "reason": reason,
        "unusableOutput": True,
        **_back_off(root, jcfg, attempt["key"], False),
    }


def _back_off(root: Path, jcfg: dict, key: str, has_more: bool) -> dict:
    """유료 실패의 **종점**. 같은 run에서 다른 엔진을 부르지 않으므로(이중 과금) 종점은
    다음 run이 무엇을 하느냐로 정해진다.

    남은 후보가 있으면 실패한 후보만 엔진 단위로 식혀 다음 run이 **다음 후보로 degrade**하게
    하고(최종적으로 생성 엔진 = 0.x 동작), 후보가 소진되면 judge 전체를 식힌다(전역 식힘 =
    기존 동작). 단일 CLI 머신은 후보가 하나뿐이라 항상 후자이므로 동작이 이전과 같다.

    엔진 식힘 기록이 실패하면 그것이 degrade의 유일한 메커니즘이므로 전역 식힘으로 떨어뜨려
    종점을 보장하고, 두 기록 모두 실패하면 사실을 `cooldownWriteFailed`로 표면화한다 —
    조용히 삼키면 다음 run이 같은 후보를 다시 유료로 부른다.
    """
    import wiki_compile_worker as wcw

    seconds = _int(jcfg.get("cooldownSeconds"), DEFAULT_COOLDOWN_SECONDS)
    if has_more and wcw.set_engine_cooldown(engine_cooldown_path(root), key, seconds):
        return {"engineCooldown": key}
    return {} if set_cooldown(root, seconds) else {"cooldownWriteFailed": True}


def read_card(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None

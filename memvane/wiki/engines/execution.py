"""Extractor engine selection, execution and cooldown."""
from __future__ import annotations

import fcntl
import json
from datetime import datetime, timezone
from pathlib import Path
import subprocess

from memvane.kernel import cooldown as qmd_cooldown
from memvane.kernel.atomic_rename import write_text_atomic
from memvane.kernel.resource_deferred import ResourceDeferred
from memvane.settings import config as qmd_config
from memvane.wiki.workspace.api import queue_lock_path as _queue_lock_path

BUILTIN_EXTRACTOR_ENGINES = {"claude", "codex", "hermes"}
MAX_ENGINE_COOLDOWN_SECS = qmd_cooldown.MAX_COOLDOWN_SECS
UNATTRIBUTED_KEY = "(unattributed)"

def _argv_list(value) -> list[str] | None:
    if isinstance(value, list) and value and all(isinstance(item, str) for item in value):
        return value
    return None

def _builtin_adapter_argv(engine: str) -> list[str] | None:
    if engine not in BUILTIN_EXTRACTOR_ENGINES:
        return None
    adapter = Path(__file__).resolve().parents[3] / "memvane/wiki/engines/extractors" / f"{engine}_adapter.py"
    if not adapter.is_file():
        return None
    from memvane.kernel.bundle_paths import entry_argv
    return entry_argv(f"extractor-{engine}")

def extractor_engine_pool(compile_cfg: dict) -> list[str]:
    """Symbolic engines this project can dispatch to, in preference order.

    `compile.extractor.builtins` first (declared order), then explicitly configured
    `backends` keys sorted (deterministic so two hosts scanning the same project pick
    the same engine). One definition of "which engines exist" — the verifier inherits
    it when `compile.verify.builtins` is empty and the dedup judge uses it directly.
    """
    raw = compile_cfg.get("extractor")
    extractor = raw if isinstance(raw, dict) else {}
    picked = [e for e in (extractor.get("builtins") or []) if isinstance(e, str) and e]
    backends = extractor.get("backends") if isinstance(extractor.get("backends"), dict) else {}
    picked += sorted(name for name in backends if isinstance(name, str) and name)
    ordered: list[str] = []
    for engine in picked:
        if engine not in ordered:
            ordered.append(engine)
    return ordered

def plan_engine_order(pool: list[str], producing: str, mode: str, attributable: bool) -> list[str]:
    """Candidate order for a judgment ABOUT work an engine produced. Shared rule.

    LLM self-review measurably degrades judgment (Huang et al. ICLR'24) and the stated
    mitigation is separating generator from judge, so the producing engine goes LAST
    (`prefer`) or is dropped (`require`). `off` is the 0.x path: producer only.

    `attributable` is the caller's proof that `producing` names a real, resolvable
    engine — without it no candidate can be SHOWN to differ from the producer, so the
    whole pool is offered and every attempt is labelled `unknown`.

    Only the ORDER lives here. Argv resolution (resolve_extractor_argv), cooldown
    skipping, mode labelling and the empty-plan reasons stay with each caller, because
    verify and dedup differ there: verify reads `compile.verify.*` and preserves its
    queue when `require` cannot be satisfied, while the dedup judge reads
    `compile.semanticDedup.judge.*` and must map an empty plan onto its own
    unavailable/transient outcome contract.
    """
    if mode == "off":
        # 생성 엔진이 귀속 불가면 그 라벨로는 argv가 안 나오므로 풀의 첫 후보로 폴백한다 —
        # 폐기하면 그 대상은 영원히 판정되지 않는다(mode는 unknown이라 거짓 주장도 아니다).
        return [producing] if attributable else pool[:1]
    if not attributable:
        return list(pool)
    others = [engine for engine in pool if engine != producing]
    return others if mode == "require" else others + [producing]

def load_engine_cooldowns(path: Path) -> dict:
    """{cooldown key: expiry epoch}. 읽기 실패는 fail-open(빈 dict) — 식힘 기록을 못 읽는
    것이 판정을 막는 이유가 되면 안 된다(최악은 한 번 더 시도하는 것이다).

    **만료값은 상한으로 클램프한다.** 손상되거나 주입된 값(`{"claude": 9e18}`)이 있으면 그
    후보가 영구히 후보에서 빠져 판정이 무한 연기된다. 상한을 넘는 항목은 식힘으로 보지
    않는다(다음 쓰기에서 정리된다).
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    ceiling = datetime.now(timezone.utc).timestamp() + MAX_ENGINE_COOLDOWN_SECS
    return {
        key: float(value) for key, value in raw.items()
        if isinstance(key, str) and isinstance(value, (int, float))
        and not isinstance(value, bool) and float(value) <= ceiling
    }

def cooling_engines(path: Path) -> set:
    # 활성 판정은 `cooldown.expiry_active` 한 벌을 쓴다(전역 cooldown과 같은 규칙).
    # `load_engine_cooldowns`의 ceiling 필터는 남겨 둔다 — 다음 쓰기에서 오염 항목을
    # 실제로 지우는 것이 그 필터이고, 이쪽은 판정만 한다.
    now = datetime.now(timezone.utc).timestamp()
    return {key for key, expiry in load_engine_cooldowns(path).items()
            if qmd_cooldown.expiry_active(expiry, now)}

def set_engine_cooldown(path: Path, key: str, seconds: int) -> bool:
    """이 후보를 seconds 동안 후보에서 제외한다. 반환값은 "기록이 남았는가"다.

    **호출자는 False를 반드시 표면화해야 한다** — 기록이 없으면 다음 run이 같은 엔진을 다시
    불러 degrade가 일어나지 않고, 실패가 반복되는 엔진에서 판정이 영구 정지한다(그것이
    엔진 단위 식힘이 존재하는 이유다).

    read-modify-write이므로 sidecar flock으로 직렬화하고, 만료 항목은 쓰기 때 정리해 파일을
    유계로 둔다. seconds는 상한으로 클램프한다.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(_queue_lock_path(path), "a", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                now = datetime.now(timezone.utc).timestamp()
                entries = {k: v for k, v in load_engine_cooldowns(path).items() if v > now}
                entries[key or UNATTRIBUTED_KEY] = now + min(max(0, seconds), MAX_ENGINE_COOLDOWN_SECS)
                # 원자적 쓰기(wiki_compile SSOT) — 실패해도 기존 식힘 기록이 잘리지 않는다.
                return write_text_atomic(path, json.dumps(entries, ensure_ascii=False, sort_keys=True) + "\n")
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    except OSError:
        return False

def resolve_extractor_argv(
    compile_cfg: dict, engine: str, builtins: list[str] | None = None
) -> list[str] | None:
    """engine → argv (없으면 None). Single rule for every adapter caller.

    `builtins` overrides which symbolic engines may resolve to a bundled adapter, so
    the verifier can consider an engine its own pool allows (compile.verify.builtins)
    without a second copy of this resolution living in wiki_verify_worker.

    **`extractor.dispatch` 게이트는 없다.** 예전에는 `dispatch == "by-engine"`일 때만
    backends/builtins를 봤고, 그 키를 안 적은 config에서는 여기가 항상 None을 돌려줘
    `process_job`이 잡을 `missing_extractor`로 **폐기**했다(requeue가 아니다).
    `extractor.argv`(모든 엔진 공용 단일 argv)와 `extractor.default`(엔진 무관 폴백)도
    함께 사라졌다 — 둘 다 엔진 귀속이 불가능해 교차검증 주장을 할 수 없었고,
    `backends[engine]`이 같은 일을 엔진 라벨을 지키면서 한다.
    """
    raw = compile_cfg.get("extractor")
    extractor = raw if isinstance(raw, dict) else {}
    backends = extractor.get("backends") if isinstance(extractor.get("backends"), dict) else {}
    primary = _argv_list(backends.get(engine))
    if builtins is None:
        builtins = extractor.get("builtins") if isinstance(extractor.get("builtins"), list) else []
    if primary is None and engine in {item for item in builtins if isinstance(item, str)}:
        primary = _builtin_adapter_argv(engine)
    return primary

def run_extractor(argv: list[str], payload: dict, timeout: int, root: Path) -> tuple[dict | None, str | None, int | None]:
    try:
        from memvane.runtime.api import provider_run
        proc = provider_run(
            argv,
            input=json.dumps(payload, ensure_ascii=False),
            text=True,
            capture_output=True,
            timeout=timeout,
            shell=False,
            cwd=str(root),
        )
    except ResourceDeferred:
        return None, "resource_deferred", 75
    except FileNotFoundError:
        # Binary genuinely absent → 127 sentinel lets the worker try `default`.
        return None, "extractor_failed", 127
    except OSError:
        # Present but unrunnable (PermissionError/ENOEXEC/ENOTDIR…) is a runtime/config
        # failure, NOT "CLI absent": return a non-127 code so it does NOT trigger fallback.
        return None, "extractor_failed", None
    except subprocess.TimeoutExpired:
        return None, "extractor_timeout", None
    if proc.stderr:
        # Extractor stderr can quote source or model text. Only the digest and
        # size of its bounded tail reach the install event log.
        from memvane.diagnostics import api as event_log
        tail = proc.stderr[-event_log.PROMPT_CHARS:]
        event_log.emit('compile.extractor', outcome='stderr', reason=None,
                       rc=proc.returncode, stderr=event_log.prompt_fields(tail))
    if proc.returncode == 75:
        return None, "resource_deferred", 75
    if proc.returncode != 0:
        return None, "extractor_failed", proc.returncode
    try:
        parsed = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None, "invalid_extractor_json", proc.returncode
    if not isinstance(parsed, dict):
        return None, "invalid_extractor_json", proc.returncode
    effort = payload.get("_qmd", {}).get("reasoningEffort", {}) if isinstance(payload.get("_qmd"), dict) else {}
    capability_declared = effort.get("capabilityDeclared") is True if isinstance(effort, dict) else False
    parsed["_qmd"] = {
        "reasoningEffort": qmd_config.reasoning_effort_audit(
            parsed.get("_qmd", {}).get("reasoningEffort") if isinstance(parsed.get("_qmd"), dict) else {},
            effort.get("requested") if isinstance(effort, dict) else None,
            capability_declared=capability_declared,
        )
    }
    return parsed, None, 0

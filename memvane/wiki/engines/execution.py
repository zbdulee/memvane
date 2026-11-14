"""Extractor engine selection, execution and cooldown."""
from __future__ import annotations

import fcntl
import json
from datetime import datetime, timezone
from pathlib import Path
import subprocess

from memvane.kernel import cooldown as qmd_cooldown
from memvane.kernel.resource_deferred import ResourceDeferred
from memvane.settings.types import REASONING_EFFORTS

BUILTIN_EXTRACTOR_ENGINES = {"claude", "codex", "hermes"}
MAX_ENGINE_COOLDOWN_SECS = qmd_cooldown.MAX_COOLDOWN_SECS
UNATTRIBUTED_KEY = "(unattributed)"


def reasoning_effort_audit(raw, requested=None, reason="capability_not_declared", capability_declared=True):
    """Return bounded, source-free effort audit metadata for persisted records."""
    requested = requested if requested in REASONING_EFFORTS else None
    if not capability_declared:
        return {"requested": requested, "applied": None, "status": "unsupported", "reason": str(reason)[:120]}
    value = raw if isinstance(raw, dict) else {}
    if value.get("requested") in REASONING_EFFORTS:
        requested = value["requested"]
    applied = value.get("applied") if value.get("applied") in REASONING_EFFORTS else None
    status = value.get("status") if value.get("status") in {"applied", "unsupported", "retried_without_effort"} else "unsupported"
    if value.get("reason"):
        reason = str(value["reason"])[:120]
    return {"requested": requested, "applied": applied, "status": status, "reason": str(reason)[:120]}


def _builtin_adapter_argv(engine: str) -> list[str] | None:
    if engine not in BUILTIN_EXTRACTOR_ENGINES:
        return None
    adapter = Path(__file__).resolve().parents[3] / "memvane/wiki/engines/extractors" / f"{engine}_adapter.py"
    if not adapter.is_file():
        return None
    from memvane.kernel.bundle_paths import entry_argv
    return entry_argv(f"extractor-{engine}")

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

def set_engine_cooldown(path: Path, key: str, seconds: int, *, _queue_lock_path, write_text_atomic) -> bool:
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
    """engine → bundled adapter argv (없으면 None). Single rule for every adapter caller.

    An engine resolves only when it is a declared builtin. `builtins` overrides the
    declared list (`compile.extractor.builtins`), so the verifier can add
    `compile.verify.builtins` without a second copy of this resolution.
    """
    if builtins is None:
        raw = compile_cfg.get("extractor")
        extractor = raw if isinstance(raw, dict) else {}
        builtins = extractor.get("builtins") if isinstance(extractor.get("builtins"), list) else []
    if engine in {item for item in builtins if isinstance(item, str)}:
        return _builtin_adapter_argv(engine)
    return None

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
        "reasoningEffort": reasoning_effort_audit(
            parsed.get("_qmd", {}).get("reasoningEffort") if isinstance(parsed.get("_qmd"), dict) else {},
            effort.get("requested") if isinstance(effort, dict) else None,
            capability_declared=capability_declared,
        )
    }
    return parsed, None, 0

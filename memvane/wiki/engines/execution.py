"""Extractor engine selection and execution."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess

from memvane.kernel.resource_deferred import ResourceDeferred
from memvane.settings.types import REASONING_EFFORTS

BUILTIN_EXTRACTOR_ENGINES = {"claude", "codex", "hermes"}


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

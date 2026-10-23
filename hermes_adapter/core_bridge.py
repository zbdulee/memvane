"""Bridge Hermes Agent plugin hooks to the existing qmd-auto-context core.

This module is intentionally thin: it adapts Hermes hook callback shapes to the
same JSON payloads used by Claude/Codex/Gemini dispatchers, then delegates to
core/recall.py, core/update.sh, core/posttool.py, core/index_enqueue.py,
core/wiki_compile_enqueue.py, and core/preflight_gate.py. qmd backend/config/
indexing/compile logic stays in core/.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 8.0
def _plugin_root() -> Path:
    override = os.environ.get("QMD_HERMES_PLUGIN_ROOT_FOR_TEST")
    if override:
        return Path(override).resolve()
    return Path(__file__).resolve().parents[1]


def _core_path(*parts: str) -> Path:
    return _plugin_root().joinpath(*parts)


def _cwd(cwd: Optional[str] = None, **kwargs: Any) -> str:
    candidate = (
        cwd
        or kwargs.get("cwd")
        or kwargs.get("working_directory")
        or os.environ.get("TERMINAL_CWD")
        or ""
    )
    if candidate:
        return str(candidate)
    try:
        return os.getcwd()
    except OSError:
        return str(Path.home())


def _env() -> Dict[str, str]:
    env = os.environ.copy()
    env["QMD_ENGINE"] = "hermes"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if not env.get("QMD_RECALL_LOG"):
        cache_dir = env.get("QMD_CACHE_DIR") or str(Path.home() / ".cache" / "qmd")
        env["QMD_RECALL_LOG"] = str(Path(cache_dir) / "qmd-hermes-hook.log")
    return env


def _is_noop_env() -> bool:
    env = os.environ
    return bool(
        env.get("QMD_SANDBOX")
        or env.get("HERMES_SANDBOX")
        or env.get("CLAUDE_SANDBOX")
        or env.get("CODEX_SANDBOX")
        or env.get("GEMINI_SANDBOX")
        or env.get("HERMES_HEADLESS") == "1"
        or env.get("CLAUDE_HEADLESS") == "1"
    )


def _setup_allowed(cwd: str) -> bool:
    try:
        # This safety check always uses the adapter's own installed core. The
        # test-only backend root override must not replace the guard itself.
        guard = Path(__file__).resolve().parent.parent / 'core' / 'setup_guard.py'
        result = subprocess.run([sys.executable, str(guard),
                                 'check', cwd], stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                timeout=2.0, env=_env(), check=False)
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _scope_allowed(action: str, payload: dict, raw_cwd: Any) -> bool:
    """Use the core pilot boundary before any Hermes backend side effect."""
    try:
        guard = Path(__file__).resolve().parent.parent / 'core' / 'synthetic_invocation_gate.py'
        event = {**payload, 'cwd': raw_cwd}
        result = subprocess.run([sys.executable, str(guard), action, '-', payload.get('cwd') or os.getcwd()],
                                input=json.dumps(event, ensure_ascii=False), text=True,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                timeout=2.0, env=_env(), check=False)
        return result.returncode == 0
    except (OSError, ValueError, TypeError, subprocess.TimeoutExpired):
        return False


def _laya_route_state(action: str, cwd: str) -> str:
    """Read the installed adapter's own selector before any QMD side effect."""
    selector = Path(__file__).resolve().parent.parent / "core" / "laya_index_host_route.py"
    try:
        result = subprocess.run(
            [sys.executable, str(selector), "classify", action, cwd],
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            timeout=2.0, env=_env(), check=False,
        )
        state = result.stdout.strip()
        return state if result.returncode == 0 and state in {"legacy", "candidate", "blocked"} else "blocked"
    except (OSError, subprocess.TimeoutExpired):
        return "blocked"


def _manager_path() -> str:
    return os.environ.get("QMD_BACKEND_MANAGER") or str(_core_path("core", "backend_manager.sh"))


def _run(
    argv: list[str],
    *,
    payload: Optional[dict] = None,
    timeout: float = DEFAULT_TIMEOUT,
    extra_env: Optional[Dict[str, str]] = None,
) -> subprocess.CompletedProcess[str]:
    stdin = json.dumps(payload or {}, ensure_ascii=False) if payload is not None else None
    env = _env()
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        argv,
        input=stdin,
        text=True,
        capture_output=True,
        timeout=timeout,
        env=env,
    )


def _run_quiet(argv: list[str], *, timeout: float = DEFAULT_TIMEOUT) -> None:
    try:
        subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            env=_env(),
            check=False,
        )
    except Exception as exc:  # pragma: no cover - defensive hook isolation
        logger.debug("qmd-auto-context manager command failed: %s", exc)


def _ensure_background() -> None:
    if os.environ.get("QMD_QUERY_FIXTURE"):
        return
    try:
        subprocess.Popen(
            ["bash", _manager_path(), "ensure"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=_env(),
        )
    except Exception as exc:  # pragma: no cover - defensive hook isolation
        logger.debug("qmd-auto-context background ensure failed: %s", exc)


def _json_stdout(proc: subprocess.CompletedProcess[str]) -> Optional[dict]:
    out = (proc.stdout or "").strip()
    if not out:
        return None
    try:
        parsed = json.loads(out)
    except json.JSONDecodeError:
        logger.debug("qmd-auto-context core returned non-json stdout: %r", out[:200])
        return None
    return parsed if isinstance(parsed, dict) else None


def _result_has_error(result: Any, status: Optional[str] = None) -> bool:
    if status and status not in {"ok", "success"}:
        return True
    if isinstance(result, str):
        try:
            parsed = json.loads(result)
        except (TypeError, ValueError):
            return False
        if isinstance(parsed, dict) and parsed.get("error"):
            return True

    return False


def _map_edit_tool(tool_name: str, args: Any) -> Optional[Tuple[str, Dict[str, Any]]]:
    if not isinstance(args, dict):
        return None
    if tool_name == "write_file":
        path = args.get("path")
        if not isinstance(path, str) or not path:
            return None
        return "Write", {
            "file_path": path,
            "content": args.get("content", "") if isinstance(args.get("content"), str) else "",
        }
    if tool_name == "patch":
        mode = args.get("mode") or "replace"
        if mode == "patch" or isinstance(args.get("patch"), str):
            patch = args.get("patch")
            if not isinstance(patch, str) or not patch:
                return None
            return "apply_patch", {"patch": patch}
        path = args.get("path")
        if not isinstance(path, str) or not path:
            return None
        tool_input: Dict[str, Any] = {"file_path": path}
        if isinstance(args.get("old_string"), str):
            tool_input["old_string"] = args["old_string"]
        if isinstance(args.get("new_string"), str):
            tool_input["new_string"] = args["new_string"]
        if args.get("replace_all") is not None:
            tool_input["replace_all"] = bool(args.get("replace_all"))
        return "Edit", tool_input
    return None


def recall_context(
    user_message: str = "",
    cwd: Optional[str] = None,
    **kwargs: Any,
) -> Optional[Dict[str, str]]:
    """Hermes pre_llm_call hook: inject qmd recall context into this turn."""
    selected_route = False
    try:
        if _is_noop_env():
            return None
        prompt = user_message or kwargs.get("message") or ""
        route_state = _laya_route_state("recall", _cwd(cwd, **kwargs))
        if route_state == "candidate":
            selected_route = True
            # The installed core rechecks the route and the installer-owned
            # journal before opening the selected DB. Never start QMD here.
            core = Path(__file__).resolve().parent.parent / "core"
            if str(core) not in sys.path:
                sys.path.insert(0, str(core))
            from laya_index_host_route import select_route
            from laya_host_response import serve_selected
            selected_cwd = _cwd(cwd, **kwargs)
            decision = select_route(selected_cwd, action="recall")
            if decision.get("state") != "candidate":
                return {"context": "[laya-index] selected route changed; QMD fallback refused"}
            return serve_selected("recall", "hermes", decision,
                                  {"prompt": prompt, "cwd": selected_cwd})
        if route_state != "legacy":
            return {"context": "[laya-index] selected route unavailable; QMD fallback refused"}
        if not isinstance(prompt, str) or not prompt.strip():
            return None
        if not _setup_allowed(_cwd(cwd, **kwargs)):
            return None
        payload = {
            "hook_event_name": "UserPromptSubmit",
            "prompt": prompt,
            "cwd": _cwd(cwd, **kwargs),
        }
        if not _scope_allowed('recall', payload, cwd or kwargs.get('cwd') or kwargs.get('working_directory')):
            return None
        _ensure_background()
        proc = _run([sys.executable, str(_core_path("core", "recall.py"))], payload=payload)
        parsed = _json_stdout(proc)
        if not parsed:
            return None
        context = (
            parsed.get("hookSpecificOutput", {})
            .get("additionalContext")
        )
        if isinstance(context, str) and context.strip():
            return {"context": context}
    except Exception as exc:  # pragma: no cover - hook isolation
        logger.debug("qmd-auto-context recall hook failed: %s", exc)
        # A selected route must make proof or search failure visible to the host.
        # Rechecking is read-only; even if the marker changes, no QMD path ran.
        if selected_route:
            return {"context": "[laya-index] selected proof or query failed; QMD fallback refused"}
    return None


def session_update(cwd: Optional[str] = None, **kwargs: Any) -> None:
    """Hermes on_session_start hook: run qmd update through core/update.sh.

    Hermes에는 session-start context 주입 채널이 없어(on_session_start 반환값
    미사용) update.sh의 stdout notice가 표면화되지 않는다. QMD_SUPPRESS_NOTICE=1로
    notice 출력·marker 기록을 모두 생략해, Hermes 실행이 TTL marker를 선점해서
    이후 Claude/Codex 세션의 이상 상태 알림을 삼키는 것을 방지한다.
    """
    selected_route = False
    try:
        if _is_noop_env():
            return None
        route_state = _laya_route_state("update", _cwd(cwd, **kwargs))
        if route_state == "candidate":
            selected_route = True
            core = Path(__file__).resolve().parent.parent / "core"
            if str(core) not in sys.path:
                sys.path.insert(0, str(core))
            from laya_index_host_route import select_route
            from laya_host_response import serve_selected
            selected_cwd = _cwd(cwd, **kwargs)
            serve_selected("update-queued", "hermes",
                           select_route(selected_cwd, action="update-queued"),
                           {"hook_event_name": "SessionStart", "cwd": selected_cwd})
            return None
        if route_state != "legacy":
            logger.warning("laya-index selected route unavailable; QMD fallback refused")
            return None
        if not _setup_allowed(_cwd(cwd, **kwargs)):
            return None
        payload = {"hook_event_name": "SessionStart", "cwd": _cwd(cwd, **kwargs)}
        if not _scope_allowed('update', payload, cwd or kwargs.get('cwd') or kwargs.get('working_directory')):
            return None
        manager = _manager_path()
        _run_quiet(["bash", manager, "ensure", "--wait"])
        _run_quiet(["bash", manager, "warm"])
        _run_quiet(["bash", manager, "rotate"])
        _run(
            ["bash", str(_core_path("core", "update.sh"))],
            payload=payload,
            timeout=30.0,
            extra_env={"QMD_SUPPRESS_NOTICE": "1"},
        )
    except Exception as exc:  # pragma: no cover - hook isolation
        logger.debug("qmd-auto-context session update failed: %s", exc)
        if selected_route:
            logger.warning("laya-index selected session sync failed; QMD fallback refused")


def pre_edit_gate(
    tool_name: str = "",
    args: Any = None,
    cwd: Optional[str] = None,
    **kwargs: Any,
) -> Optional[Dict[str, str]]:
    """Hermes pre_tool_call hook: translate qmd pending-project gate to block."""
    selected_route = False
    try:
        if _is_noop_env():
            return None
        mapped = _map_edit_tool(tool_name, args)
        if mapped is None:
            return None
        route_state = _laya_route_state("gate", _cwd(cwd, **kwargs))
        if route_state == "candidate":
            selected_route = True
            core = Path(__file__).resolve().parent.parent / "core"
            if str(core) not in sys.path:
                sys.path.insert(0, str(core))
            from laya_index_host_route import select_route
            from laya_host_response import serve_selected
            selected_cwd = _cwd(cwd, **kwargs)
            core_tool, tool_input = mapped
            decision = select_route(selected_cwd, action="gate")
            answer = serve_selected("gate", "hermes", decision, {
                "hook_event_name": "PreToolUse", "tool_name": core_tool,
                "tool_input": tool_input, "cwd": selected_cwd})
            if answer.get("action") == "continue":
                return None
            return {"action": "block", "message": "Laya Index gate failed; QMD fallback refused"}
        if route_state != "legacy":
            return {"action": "block", "message": "Laya Index route unavailable; QMD fallback refused"}
        if not _setup_allowed(_cwd(cwd, **kwargs)):
            return None
        core_tool, tool_input = mapped
        payload = {
            "hook_event_name": "PreToolUse",
            "tool_name": core_tool,
            "tool_input": tool_input,
            "cwd": _cwd(cwd, **kwargs),
        }
        if not _scope_allowed('gate', payload, cwd or kwargs.get('cwd') or kwargs.get('working_directory')):
            return None
        proc = _run([sys.executable, str(_core_path("core", "preflight_gate.py"))], payload=payload)
        parsed = _json_stdout(proc)
        if not parsed:
            return None
        hook_out = parsed.get("hookSpecificOutput", {})
        if hook_out.get("permissionDecision") == "deny":
            reason = hook_out.get("permissionDecisionReason") or "qmd-auto-context blocked this edit"
            return {"action": "block", "message": str(reason)}
    except Exception as exc:  # pragma: no cover - hook isolation
        logger.debug("qmd-auto-context pre-edit gate failed: %s", exc)
        if selected_route:
            return {"action": "block", "message": "Laya Index gate failed; QMD fallback refused"}
    return None


def post_edit_sync(
    tool_name: str = "",
    args: Any = None,
    result: Any = None,
    status: Optional[str] = None,
    cwd: Optional[str] = None,
    **kwargs: Any,
) -> None:
    """Hermes post_tool_call hook: run post-edit hint path and enqueue indexing.

    Hermes post_tool_call return values are observer-only, so posttool output is
    intentionally not returned or claimed as injected. Dirty-queue indexing is
    still performed via core/index_enqueue.py. Automatic wiki compile source
    queueing is observer-only as well and delegates to core/wiki_compile_enqueue.py.
    """
    selected_route = False
    try:
        if _is_noop_env():
            return None
        mapped = _map_edit_tool(tool_name, args)
        if mapped is None or _result_has_error(result, status):
            return
        route_state = _laya_route_state("posttool", _cwd(cwd, **kwargs))
        if route_state == "candidate":
            selected_route = True
            core = Path(__file__).resolve().parent.parent / "core"
            if str(core) not in sys.path:
                sys.path.insert(0, str(core))
            from laya_index_host_route import select_route
            from laya_host_response import serve_selected
            selected_cwd = _cwd(cwd, **kwargs)
            core_tool, tool_input = mapped
            serve_selected("posttool", "hermes",
                           select_route(selected_cwd, action="posttool"),
                           {"hook_event_name": "PostToolUse", "tool_name": core_tool,
                            "tool_input": tool_input, "cwd": selected_cwd})
            serve_selected("index", "hermes",
                           select_route(selected_cwd, action="index"),
                           {"hook_event_name": "PostToolUse", "tool_name": core_tool,
                            "tool_input": tool_input, "cwd": selected_cwd})
            return None
        if route_state != "legacy":
            logger.warning("laya-index selected route unavailable; QMD fallback refused")
            return None
        if not _setup_allowed(_cwd(cwd, **kwargs)):
            return None
        core_tool, tool_input = mapped
        payload = {
            "hook_event_name": "PostToolUse",
            "tool_name": core_tool,
            "tool_input": tool_input,
            "cwd": _cwd(cwd, **kwargs),
        }
        if not _scope_allowed('posttool', payload, cwd or kwargs.get('cwd') or kwargs.get('working_directory')):
            return None
        manager = _manager_path()
        payload_cwd = payload["cwd"]
        _run_quiet(["bash", manager, "ensure", "--wait"])
        _run([sys.executable, str(_core_path("core", "posttool.py"))], payload=payload)
        _run([sys.executable, str(_core_path("core", "index_enqueue.py"))], payload=payload)
        _run_quiet(["bash", manager, "kick-index"])
        _run([sys.executable, str(_core_path("core", "wiki_compile_enqueue.py"))], payload=payload)
        _run_quiet(["bash", manager, "kick-wiki-compile", str(payload_cwd)])
    except Exception as exc:  # pragma: no cover - hook isolation
        logger.debug("qmd-auto-context post-edit sync failed: %s", exc)
        if selected_route:
            logger.warning("laya-index selected post-edit action failed; QMD fallback refused")

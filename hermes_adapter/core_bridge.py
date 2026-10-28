"""Bridge Hermes Agent plugin hooks to the selected Laya route in core.

This module is intentionally thin: it adapts Hermes hook callback shapes to the
same JSON payloads used by Claude/Codex dispatchers and hands them to
core/laya_host_response.py. A project without a selected Laya route gets no
work; index, recall, compile and learning logic stays in core/.
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
        return state if result.returncode == 0 and state in {"legacy", "candidate", "blocked", "worktree"} else "blocked"
    except (OSError, subprocess.TimeoutExpired):
        return "blocked"


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
    """Hermes pre_llm_call hook: inject selected Laya recall context into this turn."""
    selected_route = False
    try:
        if _is_noop_env():
            return None
        prompt = user_message or kwargs.get("message") or ""
        route_state = _laya_route_state("recall", _cwd(cwd, **kwargs))
        if route_state in ("candidate", "worktree"):
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
            if decision.get("state") not in ("candidate", "worktree"):
                return {"context": "[memvane] selected route changed; request refused"}
            return serve_selected("recall", "hermes", decision,
                                  {"prompt": prompt, "cwd": selected_cwd})
        if route_state != "legacy":
            return {"context": "[memvane] selected route unavailable; request refused"}
        # Not onboarded: no recall.
    except Exception as exc:  # pragma: no cover - hook isolation
        logger.debug("memvane recall hook failed: %s", exc)
        # A selected route must make proof or search failure visible to the host.
        # Rechecking is read-only; even if the marker changes, no QMD path ran.
        if selected_route:
            return {"context": "[memvane] selected proof or query failed; request refused"}
    return None


def session_update(cwd: Optional[str] = None, **kwargs: Any) -> None:
    """Hermes on_session_start hook: queue the selected Laya route's sync wake."""
    selected_route = False
    try:
        if _is_noop_env():
            return None
        route_state = _laya_route_state("update", _cwd(cwd, **kwargs))
        if route_state in ("candidate", "worktree"):
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
            logger.warning("[memvane] selected route unavailable; request refused")
        # Not onboarded: no index work. Hermes has no SessionStart notice channel.
    except Exception as exc:  # pragma: no cover - hook isolation
        logger.debug("memvane session update failed: %s", exc)
        if selected_route:
            logger.warning("[memvane] selected session sync failed; request refused")


def pre_edit_gate(
    tool_name: str = "",
    args: Any = None,
    cwd: Optional[str] = None,
    **kwargs: Any,
) -> Optional[Dict[str, str]]:
    """Hermes pre_tool_call hook: translate the selected route's edit gate to block."""
    selected_route = False
    try:
        if _is_noop_env():
            return None
        mapped = _map_edit_tool(tool_name, args)
        if mapped is None:
            return None
        route_state = _laya_route_state("gate", _cwd(cwd, **kwargs))
        if route_state in ("candidate", "worktree"):
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
            return {"action": "block", "message": "memvane-db gate failed; edit blocked"}
        if route_state != "legacy":
            return {"action": "block", "message": "memvane-db route unavailable; edit blocked"}
        # Not onboarded: the edit is not gated.
    except Exception as exc:  # pragma: no cover - hook isolation
        logger.debug("memvane pre-edit gate failed: %s", exc)
        if selected_route:
            return {"action": "block", "message": "memvane-db gate failed; edit blocked"}
    return None


def post_edit_sync(
    tool_name: str = "",
    args: Any = None,
    result: Any = None,
    status: Optional[str] = None,
    cwd: Optional[str] = None,
    **kwargs: Any,
) -> None:
    """Hermes post_tool_call hook: the selected route's posttool and index wake.

    Hermes post_tool_call return values are observer-only, so nothing is
    returned or claimed as injected.
    """
    selected_route = False
    try:
        if _is_noop_env():
            return None
        mapped = _map_edit_tool(tool_name, args)
        if mapped is None or _result_has_error(result, status):
            return
        route_state = _laya_route_state("posttool", _cwd(cwd, **kwargs))
        if route_state in ("candidate", "worktree"):
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
            logger.warning("[memvane] selected route unavailable; request refused")
        # Not onboarded: no index or compile work.
    except Exception as exc:  # pragma: no cover - hook isolation
        logger.debug("memvane post-edit sync failed: %s", exc)
        if selected_route:
            logger.warning("[memvane] selected post-edit action failed; request refused")

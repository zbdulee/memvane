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
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# SessionStart notices (project folder move, folder conflict) per cwd. Hermes
# has no SessionStart context channel: the next recall turn carries them once.
_SESSION_NOTICES: Dict[str, str] = {}


def _remember_notice(cwd: str, notice: Any) -> None:
    if isinstance(notice, str) and notice:
        logger.warning(notice)
        _SESSION_NOTICES[cwd] = notice


def _with_session_notice(cwd: str, answer: Any) -> Any:
    notice = _SESSION_NOTICES.pop(cwd, None)
    if not notice:
        return answer
    if isinstance(answer, dict) and isinstance(answer.get("context"), str):
        return {**answer, "context": notice + "\n" + answer["context"]}
    if answer is None:
        return {"context": notice}
    return answer

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
        if action == "recall":
            # Only the hook classifier distinguishes an upgrade fence from an
            # ordinary blocked route. Its event file has the same bounded
            # payload contract as the command-hook dispatcher.
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8") as payload:
                json.dump({"cwd": cwd}, payload)
                payload.flush()
                result = subprocess.run(
                    [sys.executable, str(selector), "classify-hook", action,
                     payload.name, cwd],
                    stdin=subprocess.DEVNULL, capture_output=True, text=True,
                    # Recall classification may wait up to 1.5s for an
                    # upgrade fence, in addition to Python process startup.
                    timeout=3.0, env=_env(), check=False,
                )
            allowed = {"legacy", "candidate", "preview", "blocked", "worktree", "upgrading", "reader"}
        else:
            result = subprocess.run(
                [sys.executable, str(selector), "classify", action, cwd],
                stdin=subprocess.DEVNULL, capture_output=True, text=True,
                timeout=2.0, env=_env(), check=False,
            )
            allowed = {"legacy", "candidate", "blocked", "worktree"}
        state = result.stdout.strip()
        return state if result.returncode == 0 and state in allowed else "blocked"
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


def _log_recall_failure(decision: Any, exc: BaseException) -> None:
    """One host=hermes event for an unexpected selected-recall failure.

    Skipped when a core boundary already logged ``exc``. The diagnostic is
    captured only through the selected decision's own consent check; the
    prompt is never recorded. Never raises and never changes the answer.
    """
    if getattr(exc, "_memvane_event_logged", False) or not isinstance(decision, dict):
        return
    token = None
    try:
        import event_log
        import diagnostic_trace
        token = event_log.bind(decision=decision, host="hermes")
        reason = None
        try:
            from laya_host_response import _refusal_code
            reason = _refusal_code(exc)
        except Exception:
            reason = "unexpected_exception"
        try:
            diagnostic_id = diagnostic_trace.capture(
                decision, event="recall.selected", reason=reason, exc=exc)
        except Exception:
            diagnostic_id = None
        fields = {"diagnostic_id": diagnostic_id} if diagnostic_id else {}
        event_log.emit("recall.selected", outcome="failed", reason=reason,
                       decision=decision, host="hermes", **fields)
        try:
            exc._memvane_event_logged = True
        except Exception:
            pass
    except Exception:
        pass
    finally:
        if token is not None:
            try:
                import event_log
                event_log.unbind(token)
            except Exception:
                pass


def recall_context(
    user_message: str = "",
    cwd: Optional[str] = None,
    **kwargs: Any,
) -> Optional[Dict[str, str]]:
    """Hermes pre_llm_call hook: inject selected Laya recall context into this turn."""
    selected_route = False
    route_state = None
    decision = None
    try:
        if _is_noop_env():
            return None
        prompt = user_message or kwargs.get("message") or ""
        route_state = _laya_route_state("recall", _cwd(cwd, **kwargs))
        if route_state == 'reader':
            selected_route = True
            core = Path(__file__).resolve().parent.parent / 'core'
            if str(core) not in sys.path:
                sys.path.insert(0,str(core))
            from laya_host_response import serve_endpoint_recall
            selected_cwd = _cwd(cwd, **kwargs)
            return _with_session_notice(selected_cwd, serve_endpoint_recall(
                'hermes', {'prompt':prompt,'cwd':selected_cwd}, cwd=selected_cwd))
        if route_state == "upgrading":
            return None
        if route_state in ("candidate", "worktree", "preview"):
            selected_route = True
            # The installed core rechecks the route and the installer-owned
            # journal before opening the selected DB. Never start QMD here.
            core = Path(__file__).resolve().parent.parent / "core"
            if str(core) not in sys.path:
                sys.path.insert(0, str(core))
            from laya_index_host_route import select_route
            from laya_host_response import (serve_selected, serve_preview,
                                            _upgrade_recall_notice, _refusal_code)
            selected_cwd = _cwd(cwd, **kwargs)
            decision = select_route(selected_cwd, action="recall")
            if decision.get("state") != route_state:
                import laya_fresh_wiki_origin as fresh
                if fresh.upgrade_fenced(selected_cwd):
                    from laya_index_host_route import select_route_settled
                    hinted = select_route_settled(selected_cwd, action='recall')
                    if hinted.get('state') == 'reader':
                        from laya_host_response import serve_endpoint_recall
                        route_state = 'reader'
                        return _with_session_notice(selected_cwd, serve_endpoint_recall(
                            'hermes', {'prompt': prompt, 'cwd': selected_cwd}, cwd=selected_cwd))
                if _upgrade_recall_notice({'cwd': selected_cwd}, selected_cwd,
                                           'selected_route_changed') is not None:
                    return None
                return {"context": "[memvane] selected route changed; request refused"}
            serve = serve_preview if route_state == "preview" else serve_selected
            return _with_session_notice(selected_cwd, serve(
                "recall", "hermes", decision, {"prompt": prompt, "cwd": selected_cwd}))
        if route_state != "legacy":
            return _with_session_notice(_cwd(cwd, **kwargs), {
                "context": "[memvane] selected route unavailable; request refused"})
        # Not onboarded: no recall.
    except Exception as exc:  # pragma: no cover - hook isolation
        logger.debug("memvane recall hook failed: %s", exc)
        # A selected route must make proof or search failure visible to the host.
        # Rechecking is read-only; even if the marker changes, no QMD path ran.
        if selected_route:
            _log_recall_failure(decision, exc)
            try:
                from laya_host_response import _upgrade_recall_notice, _refusal_code
                selected_cwd = _cwd(cwd, **kwargs)
                if route_state != 'reader' and not getattr(exc, '_memvane_reader_attempted', False) and _upgrade_recall_notice({'cwd': selected_cwd}, selected_cwd,
                                           _refusal_code(exc)) is not None:
                    return None
            except Exception:
                # A broken core cannot escape the hook through its optional
                # upgrade diagnostic; unproved failures keep the refusal.
                pass
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
            answer = serve_selected("update-queued", "hermes",
                                    select_route(selected_cwd, action="update-queued"),
                                    {"hook_event_name": "SessionStart", "cwd": selected_cwd})
            if isinstance(answer, dict):
                _remember_notice(selected_cwd, answer.get("notice"))
            return None
        if route_state != "legacy":
            # A project folder that needs the owner or is moving to .memvane/
            # requests the upgrade worker here, as the command hooks do.
            notice = None
            try:
                core = Path(__file__).resolve().parent.parent / "core"
                if str(core) not in sys.path:
                    sys.path.insert(0, str(core))
                from laya_index_host_route import project_dir_notice_for
                notice = project_dir_notice_for(_cwd(cwd, **kwargs))
            except Exception as exc:  # pragma: no cover - hook isolation
                logger.debug("memvane project folder notice failed: %s", exc)
            if notice:
                _remember_notice(_cwd(cwd, **kwargs), notice)
            else:
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

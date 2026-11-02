"""Hermes Agent plugin registration for MemVane."""

from __future__ import annotations

from typing import Any, Optional

from .core_bridge import post_edit_sync, pre_edit_gate, recall_context, session_update


def _cwd_from_kwargs(kwargs: dict[str, Any]) -> Optional[str]:
    for key in ("cwd", "working_directory"):
        value = kwargs.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _on_pre_llm_call(user_message: str = "", **kwargs: Any):
    cwd = _cwd_from_kwargs(kwargs)
    kwargs.pop("cwd", None)
    return recall_context(user_message=user_message, cwd=cwd, **kwargs)


def _on_session_start(**kwargs: Any) -> None:
    cwd = _cwd_from_kwargs(kwargs)
    kwargs.pop("cwd", None)
    session_update(cwd=cwd, **kwargs)


def _on_pre_tool_call(tool_name: str = "", args: Any = None, **kwargs: Any):
    cwd = _cwd_from_kwargs(kwargs)
    kwargs.pop("cwd", None)
    return pre_edit_gate(tool_name=tool_name, args=args, cwd=cwd, **kwargs)


def _on_post_tool_call(
    tool_name: str = "",
    args: Any = None,
    result: Any = None,
    status: str | None = None,
    **kwargs: Any,
) -> None:
    cwd = _cwd_from_kwargs(kwargs)
    kwargs.pop("cwd", None)
    post_edit_sync(
        tool_name=tool_name,
        args=args,
        result=result,
        status=status,
        cwd=cwd,
        **kwargs,
    )


def register(ctx) -> None:
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
    ctx.register_hook("on_session_start", _on_session_start)
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
    ctx.register_hook("post_tool_call", _on_post_tool_call)

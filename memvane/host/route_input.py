"""Classify host hook input through the selected route."""
from __future__ import annotations

import json
from pathlib import Path
import re
import sys

from memvane.route.api import pairs as _pairs, read_regular as _read_regular
from memvane.journal.api import select_route
from memvane.route.types import MAX_HOOK_BYTES


def _decision(state, reason, **details):
    return {"state": state, "reason": reason, **details}


def _select_or_error(cwd, action):
    try:
        return select_route(cwd, action=action)
    except Exception:
        return _decision('error', 'route_lookup_failed')


def select_hook_payload(action: str, body: bytes, fallback_cwd: str) -> dict:
    """Classify bounded event bytes, including legacy malformed-input behavior."""
    try:
        if not isinstance(body, bytes) or len(body) > MAX_HOOK_BYTES:
            raise ValueError("invalid_hook_payload_size")
        try:
            payload = json.loads(body.decode("utf-8"), object_pairs_hook=_pairs)
        except json.JSONDecodeError:
            # Old hooks treat malformed/empty input as a graceful no-op. Only
            # preserve that path when its fallback project has no route marker.
            # A present marker is sticky even without a parseable event cwd.
            fallback = _select_or_error(fallback_cwd, action)
            return (fallback if fallback["state"] in ("legacy", "error") or fallback.get("majorMismatch") else
                    _decision("blocked", "unparseable_selected_hook_payload"))
        if not isinstance(payload, dict):
            raise ValueError("invalid_hook_payload")
        cwd = payload.get("cwd", fallback_cwd)
        if not isinstance(cwd, str) or not Path(cwd).is_absolute():
            raise ValueError("invalid_hook_cwd")
        return _select_or_error(cwd, action)
    except (OSError, ValueError, TypeError, UnicodeError, RuntimeError) as exc:
        return _decision("blocked", str(exc) or "invalid_hook_payload")


def select_hook_route(action: str, payload_file: str, fallback_cwd: str) -> dict:
    """Resolve a host event's own cwd before any hook work."""
    try:
        body = _read_regular(Path(payload_file), MAX_HOOK_BYTES)
        return select_hook_payload(action, body, fallback_cwd)
    except (OSError, ValueError, TypeError, UnicodeError, RuntimeError) as exc:
        return _decision("blocked", str(exc) or "invalid_hook_payload")


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    try:
        if len(args) == 3 and args[0] == "classify":
            decision = _select_or_error(args[2], args[1])
        elif len(args) == 4 and args[0] == "classify-hook":
            decision = select_hook_route(args[1], args[2], args[3])
        else:
            return 2
    except Exception:
        print("[memvane] route_lookup_failed; no hook work", file=sys.stderr)
        print("error")
        return 0
    if decision['state'] == 'error':
        print('[memvane] route_lookup_failed; no hook work', file=sys.stderr)
    if decision["state"] == "blocked" and decision.get("recallQuiet") is True and args[1] == "recall":
        # An onboarding or activation step stopped midway: the hook dispatcher
        # treats the recall like an unselected project, exit 0 without context.
        print("legacy")
        return 0
    if decision["state"] == "blocked" and decision.get("majorMismatch") in ("newer", "other"):
        # A pointer of another plugin major: no hook work and exit 0; the
        # SessionStart notice names the mismatch (onboarding_notice). Writers
        # and setup refuse through their own route proofs.
        print("legacy")
        return 0
    if decision['state'] == 'blocked':
        reason = decision.get('reason')
        if isinstance(reason, str) and re.fullmatch(r'[a-z0-9_]{1,80}', reason):
            print('[memvane] route_blocked: ' + reason, file=sys.stderr)
    print(decision["state"])
    return 0

"""Coalesced durable wiki lifecycle requests."""
from __future__ import annotations

from pathlib import Path
import time

from memvane.wiki.workspace import scan_state
from memvane.wiki.workspace.types import REQUEST_VERSION, REQUEST_SUFFIX, SERVED_SUFFIX, STALE_SECONDS, TRIGGERS


def _paths(root) -> tuple[Path, Path]:
    from memvane.settings import config
    found = config.find_project_config(str(root))
    project = str(Path(found["projectRoot"]).resolve())
    key = scan_state.project_key(project, found.get("configPath"))
    base = scan_state.state_dir()
    return base / (key + REQUEST_SUFFIX), base / (key + SERVED_SUFFIX)


def _stamp(state: dict) -> int:
    value = state.get("requestedAt")
    return value if type(value) is int and value > 0 else 0


def record(root, trigger: str) -> str:
    """Record one request: 'recorded', 'coalesced' or 'stale'.

    'coalesced' means a request is already pending and needs no new wake.
    'stale' means it has been pending past `STALE_SECONDS`; the caller offers
    the wake again in case the earlier one was dropped.
    """
    if trigger not in TRIGGERS:
        raise ValueError("invalid_lifecycle_trigger")
    request_path, served_path = _paths(root)
    current = _stamp(scan_state.read_state(request_path))
    if current > _stamp(scan_state.read_state(served_path)):
        return "stale" if time.time_ns() - current > STALE_SECONDS * 10**9 else "coalesced"
    scan_state.write_state_atomic(request_path, {
        "version": REQUEST_VERSION, "projectRoot": str(Path(root).resolve()),
        "requestedAt": max(time.time_ns(), current + 1), "trigger": trigger})
    return "recorded"


def claim(root) -> dict | None:
    """Mark the pending request served before running it; None when none is pending.

    A run that fails or times out is not retried from this request: the
    lifecycle is fail-open and the next trigger records a fresh one.
    """
    request_path, served_path = _paths(root)
    request = scan_state.read_state(request_path)
    stamp = _stamp(request)
    if stamp <= _stamp(scan_state.read_state(served_path)):
        return None
    trigger = request.get("trigger") if request.get("trigger") in TRIGGERS else None
    scan_state.write_state_atomic(served_path, {
        "version": REQUEST_VERSION, "projectRoot": str(Path(root).resolve()),
        "requestedAt": stamp, "trigger": trigger, "startedAt": time.time_ns()})
    return {"requestedAt": stamp, "trigger": trigger}


def request(decision: dict, trigger: str) -> str | None:
    """Hook side for a selected candidate route with a wiki collection.

    Writes the request before offering the wake, so a pass that picks the
    wake up always sees it. The offer never starts the owner: the caller's
    own sync wake follows and starts it. Returns the record outcome, or None
    when the route has no lifecycle.
    """
    if decision.get("state") != "candidate":
        return None
    collections = decision.get("collections")
    if not isinstance(collections, list) or not any(
            isinstance(item, dict) and item.get("role") == "wiki" for item in collections):
        return None
    root = decision["projectRoot"]
    outcome = record(root, trigger)
    if outcome != "coalesced":
        from memvane.runtime.api import request
        request("laya-compile", root, start=False)
    return outcome

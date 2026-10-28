"""Durable wiki lifecycle requests carried by the selected `laya-compile` wake.

A selected Laya hook (SessionStart, an edit inside the project, a successful
agent Bash) records one request per project and offers the project's existing
`laya-compile` wake. The compile pass of the sealed bundle
(`wiki_selected_laya.run`) serves a pending request first: it runs
`wiki_lifecycle.py` as a bounded child, then compiles what that run enqueued
in the same pass.

The lifecycle rides on `laya-compile` instead of a wake kind of its own. The
UID wake queue is shared with workers of every installed bundle, and a worker
without the kind in its table stops on the row and is respawned by its own
rewake.

Two files per project, one writer each, in the sync state dir:
`<key>-wiki-lifecycle-request.json` (hooks) and
`<key>-wiki-lifecycle-served.json` (the compile pass). A request is pending
while its `requestedAt` is newer than the served `requestedAt`. Triggers while
one is pending neither write nor wake: a burst of edits is served by one run.
A trigger during a run records a new request; its wake bump re-pends the
running row, so one follow-up pass serves it.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import time

import scan_state

REQUEST_VERSION = 1
REQUEST_SUFFIX = "-wiki-lifecycle-request.json"
SERVED_SUFFIX = "-wiki-lifecycle-served.json"
# Per-run caps, the git deadline and the card/byte limits already bound one run.
# This only stops a wedged child; the next trigger runs it again.
LIFECYCLE_SECONDS = 300
# A request pending this long gets its wake offered again.
STALE_SECONDS = 2 * LIFECYCLE_SECONDS
TRIGGERS = frozenset(("session_start", "source_edit"))


def _paths(root) -> tuple[Path, Path]:
    import config
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


def run_lifecycle(project: Path) -> dict:
    """One bounded lifecycle child for this project; fail-open."""
    import wiki_lifecycle
    argv = [sys.executable, "-B", str(Path(__file__).with_name("wiki_lifecycle.py")),
            "--cwd", str(project)]
    try:
        proc = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, check=False, timeout=LIFECYCLE_SECONDS,
                              env={**os.environ, "QMD_RUNTIME_DISPATCH": "1"})
    except (OSError, subprocess.TimeoutExpired) as exc:
        wiki_lifecycle.log(f"EXCEPTION: lifecycle child {type(exc).__name__}")
        return {"state": "failed", "reason": type(exc).__name__}
    if proc.returncode:
        wiki_lifecycle.log(f"EXCEPTION: lifecycle child exit {proc.returncode}")
        return {"state": "failed", "reason": "exit_" + str(proc.returncode)}
    return {"state": "ran"}


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
        import runtime_work
        runtime_work.request("laya-compile", root, start=False)
    return outcome

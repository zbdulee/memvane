"""Durable worker requests and the dispatcher step that starts the owned child.

``shared`` re-exports every name here and keeps the patch seams: a body reads
each name it shares with ``shared`` through that module when it runs.
"""
from __future__ import annotations

import fcntl
import os
import re
import secrets
import stat
import subprocess
from pathlib import Path


# A newer request replaces the file (new nonce). Only the full-worker lease
# holder (the dispatcher's step or the owned child) writes the state file, so
# a settled nonce never hides a request written while the child was running.
FULL_WORKER_SCHEMA = "qmd-shared-full-worker-request-v1"
FULL_WORKER_REQUEST = "full-worker-request.json"
FULL_WORKER_STATE = "full-worker-state.json"
FULL_WORKER_LOCK = "full-worker.lock"
# Starts that never recorded an outcome (crash, kill, reboot). Teacher calls
# keep their own uncertain-send receipts, so a restart never replays a send.
MAX_FULL_WORKER_STARTS = 3
# Training-only retries share FULL_WORKER_LOCK, so one owned learning child
# exists per shared root. Only the lease holder writes this one record.
TRAINING_WORKER_SCHEMA = "qmd-shared-training-worker-v1"
TRAINING_WORKER_STATE = "training-worker-state.json"
# Full-request outcome whose deferred training phase continues as a
# training-only request; its finished teacher phase is never rerun.
TRAINING_HANDOFF = "training_handoff"
_FULL_WORKER_JOB_KEYS = ("QMD_JOB_ID", "QMD_JOB_PARENT_ID", "QMD_JOB_EPISODE",
                         "QMD_JOB_QUEUED_AT", "QMD_JOB_MODEL_VERSION")


def _full_worker_request(root):
    path = root / _shared.FULL_WORKER_REQUEST
    if not path.exists() and not path.is_symlink():
        return None
    request = _shared.read_private_json(path)
    job = request.get("job") if isinstance(request, dict) else None
    if (not isinstance(job, dict) or request.get("schema") != _shared.FULL_WORKER_SCHEMA
            or not re.fullmatch(r"[0-9a-f]{32}", str(request.get("nonce")))
            or not isinstance(request.get("afterIndex"), bool)
            or set(job) != set(_shared._FULL_WORKER_JOB_KEYS)
            or any(not isinstance(v, str) or len(v) > 256 or "\x00" in v
                   for v in job.values())):
        raise ValueError("invalid_full_worker_request")
    return request


def _valid_worker_state(state, *, training=False):
    keys = {"nonce", "starts", "outcome", "acked"} | ({"schema", "revision"} if training else set())
    return (isinstance(state, dict) and set(state) == keys
            and isinstance(state["nonce"], str) and re.fullmatch(r"[0-9a-f]{32}", state["nonce"]) is not None
            and type(state["starts"]) is int and 0 <= state["starts"] <= _shared.MAX_FULL_WORKER_STARTS
            and (state["outcome"] is None or (isinstance(state["outcome"], str)
                                              and 0 < len(state["outcome"]) <= 80))
            and type(state["acked"]) is bool
            and (not training or (state["schema"] == _shared.TRAINING_WORKER_SCHEMA
                                  and type(state["revision"]) is int and state["revision"] >= 0)))


def _full_worker_state(root, nonce):
    path = root / _shared.FULL_WORKER_STATE
    if path.exists() or path.is_symlink():
        state = _shared.read_private_json(path)
        if not _shared._valid_worker_state(state):
            raise ValueError("invalid_full_worker_state")
        if state["nonce"] == nonce:
            return state
    return {"nonce": nonce, "starts": 0, "outcome": None, "acked": False}


def _training_worker_state(root):
    path = root / _shared.TRAINING_WORKER_STATE
    if not path.exists() and not path.is_symlink():
        return None
    state = _shared.read_private_json(path)
    if not _shared._valid_worker_state(state, training=True):
        raise ValueError("invalid_training_worker_state")
    return state


def _quarantine(root, name):
    """Move one unreadable private record aside, kept for review.

    Re-reading it on every wake would retry forever; a later valid request
    (a new hook launch or training wake) writes a fresh record instead.
    """
    try:
        os.replace(root / name, root / (name + ".invalid"))
    except FileNotFoundError:
        pass


def _spawn_owned(root, project, command, lease_fd, spawn, *, job, job_id):
    """Fixed argv only; the child inherits the lease FD for its whole life."""
    if command not in ("run-owned-worker", "run-owned-training"):
        raise ValueError("invalid_owned_worker_command")
    log = os.open(root / "worker.log", os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(log)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("unsafe_shared_log")
        # A whitelist, so a gold-only or test override never reaches the child.
        env = {k: os.environ[k] for k in ("PATH", "LANG", "TMPDIR", "HOME", "USER",
                                          "MEMVANE_TEST_RUNTIME_ROOT") if k in os.environ}
        # The host inventory reads only ~/.codex and refuses a redirected Codex
        # home for every host, so a redirected CODEX_HOME would stop Claude work
        # too. The default home passes through; a redirected one stays out and
        # only marks the Codex teacher to rest (hook capture also refuses it).
        codex_home = os.environ.get("CODEX_HOME")
        if codex_home and "HOME" in env:
            if Path(codex_home) == Path(env["HOME"]) / ".codex":
                env["CODEX_HOME"] = codex_home
            else:
                env[_shared.CODEX_HOME_REDIRECTED] = "1"
        env.update(PYTHONDONTWRITEBYTECODE="1", HF_HUB_OFFLINE="1",
                   TRANSFORMERS_OFFLINE="1", HF_DATASETS_OFFLINE="1", **job)
        from memvane.kernel.bundle_paths import entry_argv
        try:
            spawn(entry_argv("learning-shared", command, str(project), str(lease_fd)),
                  stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=env,
                  start_new_session=True, close_fds=True, pass_fds=(lease_fd,))
        except Exception:
            _shared.telemetry_emit(root / "job-telemetry.jsonl", "finished",
                               status="error", error_code="worker_spawn_failed",
                               job_id=job_id, op="shared_worker",
                               runtime_kind="local_worker")
            raise
    finally:
        os.close(log)


def _full_step(root, project, fd, spawn):
    """Advance one durable full (teacher+trainer) request without running it.

    ``running``/``started`` keep the wake; ``settled``, ``invalid_*`` and
    ``abandoned_after_restarts`` ACK it once; ``none`` means no unsettled
    request. A settled ``TRAINING_HANDOFF`` continues as a training-only retry.
    """
    try:
        request = _shared._full_worker_request(root)
    except (OSError, ValueError):
        _shared._quarantine(root, _shared.FULL_WORKER_REQUEST)
        return {"status": "invalid_request"}
    if request is None:
        return {"status": "none"}
    try:
        state = _shared._full_worker_state(root, request["nonce"])
    except (OSError, ValueError):
        # Unknown start count: never restart a teacher phase blindly. Queued
        # cases stay in the ledger; the next hook launch writes a new nonce.
        _shared._quarantine(root, _shared.FULL_WORKER_STATE)
        _shared._atomic_json(root / _shared.FULL_WORKER_STATE, {"nonce": request["nonce"], "starts": 0,
                                                "outcome": "abandoned_invalid_state", "acked": True})
        return {"status": "invalid_state"}
    if state["outcome"] is not None:
        if state["acked"]:
            return {"status": "none"}
        _shared._atomic_json(root / _shared.FULL_WORKER_STATE, {**state, "acked": True})
        return {"status": "settled", "outcome": state["outcome"]}
    if state["starts"] >= _shared.MAX_FULL_WORKER_STARTS:
        # Queued cases stay in the ledger; the next hook launch retries.
        _shared._atomic_json(root / _shared.FULL_WORKER_STATE, {**state, "outcome": "abandoned_after_restarts",
                                                "acked": True})
        return {"status": "abandoned_after_restarts"}
    # Counted before spawning: a crash between here and exit still counts.
    state = {**state, "starts": state["starts"] + 1}
    _shared._atomic_json(root / _shared.FULL_WORKER_STATE, state)
    _shared._spawn_owned(root, project, "run-owned-worker", fd, spawn,
                 job=request["job"], job_id=request["job"]["QMD_JOB_ID"])
    return {"status": "started", "mode": "full", "starts": state["starts"]}


def _training_step(root, project, revision, fd, spawn, *, handoff=False):
    """Advance the training-only retry for one wake revision.

    Runtime wake revisions are unique per queue, so a settled state answers
    exactly the revision that last started it. Any other revision (a wake
    re-put during or after that run) coalesces into one new request.
    """
    path = root / _shared.TRAINING_WORKER_STATE
    try:
        state = _shared._training_worker_state(root)
    except (OSError, ValueError):
        _shared._quarantine(root, _shared.TRAINING_WORKER_STATE)
        return {"status": "invalid_state", "mode": "training"}
    if state is None or state["outcome"] is not None:
        if state is not None and state["revision"] == revision and not handoff:
            if state["acked"]:
                return {"status": "none"}
            _shared._atomic_json(path, {**state, "acked": True})
            return {"status": "settled", "mode": "training", "outcome": state["outcome"]}
        # A replaced settled record needs no separate ACK: this request covers it.
        state = {"schema": _shared.TRAINING_WORKER_SCHEMA, "nonce": secrets.token_hex(16),
                 "revision": revision, "starts": 0, "outcome": None, "acked": False}
    elif state["starts"] >= _shared.MAX_FULL_WORKER_STARTS:
        _shared._atomic_json(path, {**state, "outcome": "abandoned_after_restarts", "acked": True})
        return {"status": "abandoned_after_restarts", "mode": "training"}
    state = {**state, "starts": state["starts"] + 1, "revision": revision}
    _shared._atomic_json(path, state)
    _shared._spawn_owned(root, project, "run-owned-training", fd, spawn,
                 job={}, job_id="training-worker:" + state["nonce"])
    return {"status": "started", "mode": "training", "starts": state["starts"]}


def learning_worker_step(root, project, revision, *, spawn=None):
    """Dispatcher step for one learning wake; never runs teacher or trainer.

    One lease covers both modes, so at most one owned learning child exists
    per shared root. A full (teacher+trainer) request goes first; otherwise
    the wake is a training-only retry. ``running``/``started`` keep the wake.
    """
    from memvane.runtime.api import coordination_lock as _lock
    root, project = _shared._validated_root(root), Path(project).resolve(strict=True)
    if type(revision) is not int or revision < 0:
        raise ValueError("invalid_runtime_revision")
    fd = _lock(root / _shared.FULL_WORKER_LOCK)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": "running"}
        spawn = spawn or subprocess.Popen
        full = _shared._full_step(root, project, fd, spawn)
        handoff = full.get("outcome") == _shared.TRAINING_HANDOFF
        if full["status"] != "none" and not handoff:
            return full
        return _shared._training_step(root, project, revision, fd, spawn, handoff=handoff)
    finally:
        os.close(fd)


# Bound last: ``shared`` imports this module while it initializes, and a body
# reads ``_shared`` only when it runs.
from . import shared as _shared  # noqa: E402

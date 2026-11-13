"""Small, best-effort job telemetry ledger; never writes prompts or source text.

The caller owns lifecycle transitions and supplies any process-group resource
summary it already measured. This module does not spawn a sampler or alter a
job's outcome when logging is unavailable. Use a private, existing directory
and a ``job-telemetry.jsonl`` file inside it.

CPU percent follows ``ps``: 100 means one core, so a process group may exceed
100. RSS is the sum of sampled process RSS, not unique physical memory.
"""
from __future__ import annotations

import fcntl
import json
import math
import os
from pathlib import Path
import re
import stat
import time
from collections.abc import Iterable, Mapping

SCHEMA = "qmd-job-telemetry-v1"
PHASES = frozenset(("queued", "started", "finished"))
STATUSES = frozenset(("success", "error", "cancelled", "skipped", "deferred", "unknown"))
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_LABEL = re.compile(r"[A-Za-z][A-Za-z0-9._:-]{0,127}\Z")
_METRIC_KEYS = ("cpu_avg_percent", "cpu_peak_percent", "rss_avg_mib", "rss_peak_mib")


def _label(value, *, identifier=False):
    rule = _ID if identifier else _LABEL
    return value if isinstance(value, str) and rule.fullmatch(value) else None


def _nonnegative(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return round(number, 3) if math.isfinite(number) and number >= 0 else None


def _work_count(value):
    return value if type(value) is int and 0 <= value <= 1000000 else None


def _resource_summary(resources):
    """Accept existing local-runner aliases; discard every other input key."""
    source = resources if isinstance(resources, Mapping) else {}
    samples = source.get("resource_samples", source.get("samples"))
    samples = samples if type(samples) is int and 0 <= samples <= 10000000 else None

    def metric(*names):
        for name in names:
            if name in source:
                return _nonnegative(source[name])
        return None

    values = {
        "cpu_avg_percent": metric("cpu_avg_percent", "avg_cpu_percent", "observed_avg_cpu_percent"),
        "cpu_peak_percent": metric("cpu_peak_percent", "peak_cpu_percent", "observed_peak_cpu_percent"),
        "rss_avg_mib": metric("rss_avg_mib", "avg_rss_mib"),
        "rss_peak_mib": metric("rss_peak_mib", "peak_rss_mib"),
    }
    for key, alias in (("rss_avg_mib", "observed_avg_rss_kib"),
                       ("rss_peak_mib", "observed_peak_rss_kib")):
        if values[key] is None and alias in source:
            kib = _nonnegative(source[alias])
            values[key] = round(kib / 1024, 3) if kib is not None else None
    if samples == 0:
        values = dict.fromkeys(_METRIC_KEYS)
    return {**values, "resource_samples": samples}


def _system_summary(value):
    source = value if isinstance(value, Mapping) else {}
    pressure = source.get("memory_pressure")
    pressure = pressure if isinstance(pressure, Mapping) else {}
    return {"available_mib": _nonnegative(source.get("available_mib")),
            "load_per_cpu": _nonnegative(source.get("load_per_cpu")),
            "swap_used_mib": _nonnegative(source.get("swap_used_mib")),
            "memory_free_percent": _nonnegative(pressure.get("system_free_percent")),
            "gpu_usage_percent": _nonnegative(source.get("gpu_usage_percent")),
            "gpu_probe": source.get("gpu") if source.get("gpu") in
            ("mps-possible", "unknown") else None}


def emit(path, phase, *, job_id, resources=None, **metadata) -> bool:
    """Append one private JSONL event, returning False on any logging failure.

    Only named, validated fields are persisted. Extra metadata, including a
    mistakenly supplied body, prompt, path, or secret, is silently discarded.
    ``ts`` is Unix seconds; ``queue_wait_ms`` and ``run_ms`` may be supplied
    from monotonic measurements. Absent or unmeasured values remain JSON null.
    """
    try:
        if phase not in PHASES or _label(job_id, identifier=True) is None:
            return False
        attempt = metadata.get("attempt", 1)
        if type(attempt) is not int or not 1 <= attempt <= 1000:
            return False
        parent_id = metadata.get("parent_id")
        if parent_id is not None and _label(parent_id, identifier=True) is None:
            return False
        episode = metadata.get("episode")
        if episode is not None and (type(episode) is not int or not 1 <= episode <= 1000000):
            return False
        stamp = metadata.get("ts", time.time())
        if isinstance(stamp, bool) or not isinstance(stamp, (int, float)) or not math.isfinite(stamp) or stamp < 0:
            return False
        status = _label(metadata.get("status")) if phase == "finished" else None
        if phase == "finished" and status not in STATUSES:
            status = "unknown"
        exit_code = metadata.get("exit_code") if phase == "finished" else None
        if type(exit_code) is not int or not -255 <= exit_code <= 255:
            exit_code = None
        summary = _resource_summary(resources)
        run_ms = _nonnegative(metadata.get("run_ms"))
        if run_ms is None and isinstance(resources, Mapping):
            seconds = _nonnegative(resources.get("elapsed_seconds"))
            run_ms = round(seconds * 1000, 3) if seconds is not None else None
        row = {
            "schema": SCHEMA, "phase": phase, "ts": round(float(stamp), 6),
            "job_id": job_id, "parent_id": parent_id, "attempt": attempt,
            "episode": episode, "op": _label(metadata.get("op")),
            "runtime_kind": _label(metadata.get("runtime_kind")),
            "model_version": _label(metadata.get("model_version"), identifier=True),
            "source_version": _label(metadata.get("source_version"), identifier=True),
            "destination_version": _label(metadata.get("destination_version"), identifier=True),
            "status": status, "error_code": _label(metadata.get("error_code")),
            "exit_code": exit_code,
            "queue_wait_ms": _nonnegative(metadata.get("queue_wait_ms")),
            "run_ms": run_ms,
            "registered_count": _work_count(metadata.get("registered_count")),
            "deferred_count": _work_count(metadata.get("deferred_count")),
            "skipped_count": _work_count(metadata.get("skipped_count")),
            "system_before": _system_summary(
                resources.get("before") if isinstance(resources, Mapping) else None),
            "system_after": _system_summary(
                resources.get("after") if isinstance(resources, Mapping) else None),
            **summary,
        }
        target = Path(path)
        if not target.is_absolute() or target.name != "job-telemetry.jsonl":
            return False
        parent = target.parent
        if parent.is_symlink() or not parent.is_dir():
            return False
        parent_stat = parent.stat()
        if parent_stat.st_uid != os.getuid() or parent_stat.st_mode & 0o077:
            return False
        raw = (json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()
        if len(raw) > 4096:
            return False
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(target, flags, 0o600)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o077 or info.st_nlink != 1):
                return False
            fcntl.flock(fd, fcntl.LOCK_EX)
            previous_size = os.fstat(fd).st_size
            try:
                if os.write(fd, raw) != len(raw):
                    raise OSError("short telemetry append")
                os.fsync(fd)
            except OSError:
                try:
                    os.ftruncate(fd, previous_size)
                except OSError:
                    pass
                return False
        finally:
            os.close(fd)
        return True
    except Exception:
        return False


def _valid_replay_row(row) -> bool:
    """Reject a corrupt line before it can change any attempt state."""
    if (not isinstance(row, dict) or row.get("schema") != SCHEMA
            or row.get("phase") not in PHASES
            or _label(row.get("job_id"), identifier=True) is None
            or type(row.get("attempt")) is not int
            or not 1 <= row["attempt"] <= 1000):
        return False
    stamp = row.get("ts")
    if (type(stamp) not in (int, float) or not math.isfinite(stamp)
            or stamp < 0):
        return False
    for name, identifier in (("parent_id", True), ("op", False),
                             ("runtime_kind", False), ("model_version", True),
                             ("source_version", True), ("destination_version", True),
                             ("error_code", False)):
        value = row.get(name)
        if value is not None and _label(value, identifier=identifier) is None:
            return False
    episode = row.get("episode")
    if episode is not None and (type(episode) is not int or not 1 <= episode <= 1000000):
        return False
    status = row.get("status")
    exit_code = row.get("exit_code")
    if row["phase"] == "finished":
        if not isinstance(status, str) or status not in STATUSES:
            return False
    elif status is not None or row.get("error_code") is not None or exit_code is not None:
        return False
    if exit_code is not None and (type(exit_code) is not int or not -255 <= exit_code <= 255):
        return False
    for name in (*_METRIC_KEYS, "queue_wait_ms", "run_ms"):
        value = row.get(name)
        if value is not None and _nonnegative(value) is None:
            return False
    samples = row.get("resource_samples")
    if samples is not None and (type(samples) is not int or not 0 <= samples <= 10000000):
        return False
    for name in ("system_before", "system_after"):
        value = row.get(name)
        if value is None:
            continue  # Earlier append-only rows used the same schema without probes.
        if not isinstance(value, dict) or set(value) != {
                "available_mib", "load_per_cpu", "swap_used_mib",
                "memory_free_percent", "gpu_usage_percent", "gpu_probe"}:
            return False
        if any(value[key] is not None and _nonnegative(value[key]) is None
               for key in ("available_mib", "load_per_cpu", "swap_used_mib",
                           "memory_free_percent", "gpu_usage_percent")):
            return False
        if value["gpu_probe"] not in (None, "mps-possible", "unknown"):
            return False
    return True


def replay(lines: Iterable[str]) -> list[dict]:
    """Summarize attempts; a missing finish stays incomplete, never success."""
    attempts = {}
    for line in lines:
        try:
            row = json.loads(line)
            if not _valid_replay_row(row):
                continue
            key = (row["job_id"], row["attempt"])
            current = attempts.setdefault(key, {
                "job_id": key[0], "attempt": key[1], "retry_count": key[1] - 1,
                "parent_id": None, "episode": None, "op": None,
                "runtime_kind": None, "model_version": None,
                "source_version": None, "destination_version": None,
                "queued_at": None, "started_at": None, "finished_at": None,
                "status": "incomplete", "error_code": None, "exit_code": None,
                "queue_wait_ms": None, "run_ms": None,
                "system_before": None, "system_after": None,
                **dict.fromkeys(_METRIC_KEYS), "resource_samples": None,
            })
            for name in ("parent_id", "episode", "op", "runtime_kind",
                         "model_version", "source_version", "destination_version"):
                if row.get(name) is not None:
                    current[name] = row[name]
            phase = row["phase"]
            stamp = float(row["ts"])
            if phase == "queued" and current["queued_at"] is None:
                current["queued_at"] = stamp
            elif phase == "started" and current["started_at"] is None:
                current["started_at"] = stamp
            elif phase == "finished":
                current["finished_at"] = stamp
                current["status"] = row["status"]
                current["error_code"] = row.get("error_code")
                current["exit_code"] = row.get("exit_code")
                for name in (*_METRIC_KEYS, "resource_samples",
                             "system_before", "system_after"):
                    current[name] = row.get(name)
            if row.get("queue_wait_ms") is not None:
                current["queue_wait_ms"] = _nonnegative(row["queue_wait_ms"])
            if row.get("run_ms") is not None:
                current["run_ms"] = _nonnegative(row["run_ms"])
        except (ValueError, TypeError, OverflowError):
            continue
    for current in attempts.values():
        if current["queue_wait_ms"] is None and current["queued_at"] is not None and current["started_at"] is not None:
            current["queue_wait_ms"] = _nonnegative((current["started_at"] - current["queued_at"]) * 1000)
        if current["run_ms"] is None and current["started_at"] is not None and current["finished_at"] is not None:
            current["run_ms"] = _nonnegative((current["finished_at"] - current["started_at"]) * 1000)
    return list(attempts.values())

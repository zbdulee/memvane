#!/usr/bin/env python3
"""Opt-in SessionStart launcher for one isolated, local due cycle.

The hook only launches a one-shot child. The child checks the durable schedule,
then uses the existing train/validation/evaluation and promotion gates. No
teacher transport, daemon, or global service is started here.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess
import sys

import config as qmd_config
import qmd_route
from context_learning.contracts import digest
from context_learning.dataset import read_private_json
from context_learning.local_cycle import LocalTrainer, VerifiedLayaTrainer, _atomic_json
from context_learning.schedule import run_due_cycle
from context_learning.seam import _private_state
from context_learning.store import canonical

POLICY = "auto-cycle.json"
RESULT = "last-auto-cycle.json"


def _policy(project: Path):
    found = qmd_config.find_project_config(str(project))
    cfg = found["config"]
    from context_learning.learning_profile import SCHEMA,present
    shared=cfg.get('sharedLearning',{})
    if shared.get('profile')==SCHEMA or (isinstance(shared.get('root'),str) and present(shared['root'])):
        # Selected profiles have a separate bounded local-only worker.
        return None
    if (found["configFormat"] != "auto-context-dir"
            or Path(found["projectRoot"]).resolve() != project
            or cfg.get("contextLearning", {}).get("autoCycle") is not True
            or cfg.get("indexing") is False):
        return None
    state = _private_state(cfg, project)
    if state is None:
        return None
    path = state / POLICY
    if not path.is_file() or path.is_symlink():
        return None
    data = read_private_json(path)
    required = {
            "schema", "versionId", "revisionsJson", "trainerArgvJson",
            "incumbentArtifact", "maxSeconds", "maxRssMib"}
    if (not isinstance(data, dict) or set(data) not in (required, required | {"dataMode"})
            or data["schema"] != "qmd-auto-cycle-policy-v1"
            or data.get("dataMode", "current") not in ("current", "historical_snapshot")
            or not isinstance(data["versionId"], str) or not data["versionId"]
            or any(not isinstance(data[k], str) or not Path(data[k]).is_absolute()
                   for k in ("revisionsJson", "trainerArgvJson", "incumbentArtifact"))
            or type(data["maxSeconds"]) is not int or not 1 <= data["maxSeconds"] <= 3600
            or type(data["maxRssMib"]) is not int or not 128 <= data["maxRssMib"] <= 65536):
        raise ValueError("invalid_auto_cycle_policy")
    return state, data


def run(project: Path) -> dict:
    import setup_guard
    if setup_guard.status(project)['status'] != 'ready':
        return {'status': 'setup_required'}
    item = _policy(project)
    if item is None:
        return {"status": "disabled"}
    state, policy = item
    data_mode = policy.get("dataMode", "current")
    expected_corpus = None
    if data_mode == "current":
        from context_learning.corpus import snapshot, current_evaluation_ready
        current = snapshot(project, qmd_config.find_project_config(str(project))["config"])
        if not current_evaluation_ready(state, policy["versionId"], current):
            summary = {"schema": "qmd-auto-cycle-result-v1", "status": "pending_review",
                       "versionId": policy["versionId"], "dataMode": data_mode,
                       "reason": "current_corpus_unavailable_or_changed"}
            _atomic_json(state / RESULT, summary)
            return summary
        expected_corpus = current["fingerprint"]
    previous_path = state / "learning-schedule.json"
    previous = read_private_json(previous_path) if previous_path.exists() else None
    anchor = previous.get("next_due") if isinstance(previous, dict) else "first"
    cycle_id = "auto-" + digest(canonical([policy["versionId"], anchor]))[:20]
    argv = read_private_json(policy["trainerArgvJson"])
    adapter = Path(__file__).with_name("context_learning") / "laya_adapter.py"
    trainer_type = (VerifiedLayaTrainer if isinstance(argv, list) and len(argv) == 3
                    and Path(argv[1]).resolve() == adapter.resolve() else LocalTrainer)
    trainer = trainer_type(argv, max_seconds=policy["maxSeconds"],
                           max_rss_mib=policy["maxRssMib"])
    def revisions():
        if expected_corpus is not None:
            from context_learning.corpus import snapshot
            latest = snapshot(project, qmd_config.find_project_config(str(project))["config"])
            if latest is None or latest["fingerprint"] != expected_corpus:
                raise ValueError("corpus_changed_during_cycle")
        return read_private_json(policy["revisionsJson"])
    result = run_due_cycle(state, cycle_id, policy["versionId"], revisions, trainer,
                           incumbent_artifact=policy["incumbentArtifact"])
    summary = {"schema": "qmd-auto-cycle-result-v1", "status": result["status"],
               "cycleId": cycle_id, "versionId": policy["versionId"],
               "dataMode": data_mode,
               "nextDue": result.get("next_due")}
    if result["status"] == "completed":
        summary["phase"] = result["cycle"]["phase"]
        summary["cadence"] = result["cadence"]
    elif result["status"] == "failed":
        summary["reason"] = result["reason"]
    _atomic_json(state / RESULT, summary)
    if summary['status']=='deferred_resource':
        import runtime_work
        runtime_work.request('auto',str(project))
    return summary


def launch(project: Path, *, data_mode: str | None = None) -> dict:
    import setup_guard
    if setup_guard.status(project)['status'] != 'ready':
        return {'status': 'setup_required'}
    item = _policy(project)
    if item is None:
        return {"status": "disabled"}
    state, policy = item
    if data_mode is not None and policy.get("dataMode", "current") != data_mode:
        return {"status": "deferred"}
    log_path = state / "auto-cycle.log"
    fd = os.open(log_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("unsafe_auto_cycle_log")
        env = {key: os.environ[key] for key in ("PATH", "LANG", "TMPDIR") if key in os.environ}
        # The synthetic legacy fixture has no v2 pointer; carry its explicit
        # test-only guard override into this deliberately scrubbed child env.
        if os.environ.get('QMD_SETUP_GUARD_FIXTURE') == '1':
            env['QMD_SETUP_GUARD_FIXTURE'] = '1'
        # The current-corpus gate must read the exact QMD index/config that the
        # preceding worker updated, including an isolated project override.
        route = qmd_route.project_paths(project)
        for key in qmd_route.PATH_KEYS:
            env[key] = route[key]
        env.update(PYTHONDONTWRITEBYTECODE="1", HF_HUB_OFFLINE="1",
                   TRANSFORMERS_OFFLINE="1", HF_DATASETS_OFFLINE="1")
        import runtime_work
        receipt=runtime_work.request('auto',str(project),start=False)
        if isinstance(receipt,dict) and receipt['status']=='deferred_queue_capacity':
            runtime_work.hook_wake();return receipt
        subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "run", str(project)],
                         stdin=subprocess.DEVNULL, stdout=fd, stderr=fd, env=env,
                         start_new_session=True, close_fds=True)
    finally:
        os.close(fd)
    return {"status": "launched"}


def main(argv):
    if len(argv) != 2 or argv[0] not in ("launch", "launch-historical", "launch-current", "run"):
        return 2
    try:
        project = Path(argv[1]).resolve(strict=True)
        mode = {"launch-historical": "historical_snapshot", "launch-current": "current"}.get(argv[0])
        result = run(project) if argv[0] == "run" else launch(project, data_mode=mode)
    except Exception:
        result = {"status": "failed", "reason": "auto_cycle_unavailable"}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)
    return 1 if result["status"] == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

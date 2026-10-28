#!/usr/bin/env python3
"""One isolated, local due cycle of opt-in automatic learning.

The runtime ``auto`` wake handler calls ``run``. It checks the durable
schedule, then uses the existing train/validation/evaluation and promotion
gates. No teacher transport, daemon, or global service is started here.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys

import config as qmd_config
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
    from context_learning.laya_learning import ready
    if not ready(project):
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


def main(argv):
    if len(argv) != 2 or argv[0] != "run":
        return 2
    try:
        project = Path(argv[1]).resolve(strict=True)
        result = run(project)
    except Exception:
        result = {"status": "failed", "reason": "auto_cycle_unavailable"}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)
    return 1 if result["status"] == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

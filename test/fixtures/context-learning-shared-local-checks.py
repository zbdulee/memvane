"""No provider/model call: exercise the shared durable queue and trial pointer."""
import json
import fcntl
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

from context_learning.contracts import digest, request
from context_learning.shared import _cadence, _db, _due_threshold, _policy, _reconcile_cases, _training_provenance, enqueue, observe_trial, rollback, run, status, sync_reviewed, train_due, trial_eligible
from context_learning.store import canonical, capture
from context_learning.seam import observe
from context_learning.safe_source import snapshot_beneath
from context_learning.offline import delete_case, save_label, review_digest
from context_learning.selection_gold import save_selection
from context_learning.laya_adapter import EXPERIMENT_INPUT_VIEW
from config import normalize_config


TRAINER_CALLS = []


class FakeTrainer:
    def call(self, mode, payload, result_path, *, cwd):
        TRAINER_CALLS.append(mode)
        assert mode in ("train", "predict")
        if mode == "train":
            assert payload.get("model_input_view") == EXPERIMENT_INPUT_VIEW
            assert payload["train"] and not any("project_key" in c for c in payload["train"])
            artifact = Path(payload["artifact_path"])
            artifact.write_text("trial")
            artifact.chmod(0o600)
            answer = {"schema_version": 1, "status": "trained"}
        else:
            assert all("selected_ids" not in c for c in payload["cases"])
            model = Path(payload["artifact_path"]).read_text()
            answer = {"schema_version": 1, "predictions": [
                {"request_id": c["request_id"], "input_sha256": c["input_sha256"],
                 "selected_ids": [c["candidates"][0]["id"]] if model == "trial" else []}
                for c in payload["cases"]]}
        path = Path(result_path)
        path.write_text(canonical(answer))
        path.chmod(0o600)
        return answer, {"peak_rss_mib": 1}


with tempfile.TemporaryDirectory(prefix="qmd-shared-", dir="/tmp") as name:
    base = Path(name).resolve()
    root = base / "shared"; root.mkdir(mode=0o700)
    a = base / "novel-a"; a.mkdir()
    b = base / "novel-b"; b.mkdir()
    private = base / "private"; private.mkdir(mode=0o700)
    consents = root / "consents"; consents.mkdir(mode=0o700)
    for project in (a, b):
        (project / ".auto-context" / "wiki").mkdir(parents=True)
        (project / ".auto-context" / "settings.json").write_text(canonical({
            "indexing": True, "collections": ["wiki"],
            "collectionPaths": {"wiki": ".auto-context/wiki"},
            "collectionRoles": {"wiki": "wiki"},
            "contextLearning": {"capture": True, "stateRoot": str(private)},
            "sharedLearning": {"enabled": True, "root": str(root)}}))
        consent = consents / (digest(str(project)) + ".json")
        consent.write_text(canonical({"schema": "qmd-shared-consent-v1",
            "projectRoot": str(project), "sharedRoot": str(root),
            "capture": True, "localTraining": True, "externalTeacher": False}))
        consent.chmod(0o600)
    original = base / "base.artifact"; original.write_text("base"); original.chmod(0o600)
    policy = {"schema": "qmd-shared-learning-v1", "enabled": True,
              "goldModel": "gpt-6-luna", "maxGoldCalls": 10,
              "batchMin": 5, "batchMax": 20, "targetExact": .95,
              "trainerArgv": [sys.executable, str(Path("core/context_learning/laya_adapter.py").resolve()), str(base)],
              "baseArtifact": str(original), "maxRssMib": 4096,
              "minAvailableMib": 128, "maxLoadPerCpu": 4,
              "safetySeconds": 900, "experimental": True}
    (root / "shared-policy.json").write_text(canonical(policy))
    (root / "shared-policy.json").chmod(0o600)
    assert _policy(root) == policy
    inspected = subprocess.run([sys.executable, "core/context_learning_shared.py",
        "status", str(root)], capture_output=True, text=True, check=False,
        env={**os.environ, 'HOME': str(base), 'PYTHONDONTWRITEBYTECODE': '1'})
    assert inspected.returncode == 0 and not inspected.stderr
    assert json.loads(inspected.stdout)["status"] == "ok"
    lock_fd = os.open(root / "shared-worker.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert run(root)["status"] == "busy"
    finally:
        os.close(lock_fd)
    source = "---\ntitle: Synthetic\n---\nAster has a blue bridge.\n"
    cid = "wiki/bridge.md"
    def metadata(card, seed):
        return {"mode": "eligible-returned-pool", "scope": "queried-phases-only",
            "phase_counts": {"primary": 1}, "per_phase_limit": 8,
            "eligible_ids": [card], "captured_ids": [card],
            "baseline_selected_ids": [card], "excluded": [],
            "snapshot_failures": [], "retrieval_truncated": False,
            "assessment_complete": True, "complete_within_returned_bound": True,
            "index_revision": digest(seed)}
    config = {"sharedLearning": {"enabled": True, "root": str(root)}}
    normalized = normalize_config({"contextLearning": {"capture": True, "stateRoot": str(base)},
        "sharedLearning": {"enabled": True, "root": str(root), "trialSelection": True,
                           "rerankTargetSeconds": 1.5}})
    assert normalized["contextLearning"]["candidateTopK"] == 15
    assert normalized["sharedLearning"]["trialSelection"] is True
    assert normalized["sharedLearning"]["rerankTargetSeconds"] == 1.5
    assert normalize_config({"sharedLearning": {"rerankTargetSeconds": float('inf')}})[
        "sharedLearning"]["rerankTargetSeconds"] == 1.0
    for i in range(5):
        project = a if i % 2 == 0 else b
        card = f"wiki/case-{i}.md"
        content = f"---\ntitle: Synthetic case {i}\n---\nUnique fact for case {i}.\n"
        (project / ".auto-context" / card).write_text(content)
        sample = request({"prompt": f"Synthetic bridge question {i}"}, [
            {"candidate_id": card, "revision_sha256": digest(content),
             "eligible": True, "excerpt": content}],
            host="codex", request_id=f"case-{i}", sampling=metadata(card, f"index-{i}"))
        local = private / digest(str(project)); local.mkdir(mode=0o700, exist_ok=True)
        assert capture(sample, enabled=True, state_dir=local, compact_sources={card: content}) == "stored"
        assert enqueue(config, project, sample, {card: content}) == "queued"
        assert enqueue(config, project, sample, {card: content}) == "duplicate"
        with _db(root) as db:
            db.execute("UPDATE cases SET status='gold',gold=? WHERE request_id=?",
                       (canonical({"selected_ids": [card], "input_sha256": digest(canonical(sample)),
                                   "review": "fixture_only"}), sample["request_id"]))
    # Repeated questions retain their split. A newly asked question may be
    # provisionally held out even if baseline retrieval focused a train card;
    # gold/candidate overlap is screened before evaluation.
    duplicate = request({"prompt": "Synthetic bridge question 0"}, [
        {"candidate_id": "wiki/case-0.md", "revision_sha256": digest("---\ntitle: Synthetic case 0\n---\nUnique fact for case 0.\n"),
         "eligible": True, "excerpt": "Unique fact for case 0."}],
        host="codex", request_id="family-duplicate", sampling=metadata("wiki/case-0.md", "index-0"))
    same_body = "---\ntitle: Synthetic case 0\n---\nUnique fact for case 0.\n"
    assert enqueue(config, a, duplicate, {"wiki/case-0.md": same_body}) == "queued"
    with _db(root) as db:
        assert db.execute("SELECT split FROM cases WHERE request_id='family-duplicate'").fetchone()[0] == "train"
        db.execute("DELETE FROM cases WHERE request_id='family-duplicate'")
    cross = request({"prompt": "Synthetic bridge question 4"}, [
        {"candidate_id": "wiki/case-0.md", "revision_sha256": digest(same_body),
         "eligible": True, "excerpt": same_body}],
        host="codex", request_id="family-cross", sampling=metadata("wiki/case-0.md", "index-cross"))
    assert enqueue(config, a, cross, {"wiki/case-0.md": same_body}) == "queued"
    with _db(root) as db:
        assert db.execute("SELECT split FROM cases WHERE request_id='family-cross'").fetchone()[0] == "holdout"
        db.execute("DELETE FROM cases WHERE request_id='family-cross'")
    before = {"exact": 10, "projects": {"a": {"exact": 5}}}
    after = {"exact": 9, "exact_rate": .95, "projects": {"a": {"exact": 5}}}
    assert _cadence(policy, 10, before, after) == (5, "aggregate_regression")
    assert _cadence(policy, 10, before, {"exact": 10, "exact_rate": .95,
        "projects": {"a": {"exact": 4}}}) == (5, "project_regression")
    assert _cadence(policy, 10, before, {"exact": 10, "exact_rate": .95,
        "projects": {"a": {"exact": 5}}}) == (20, "at_target")
    with patch("context_learning.shared.resource_report", return_value={
        "ready": False, "available_mib": 100, "load_per_cpu": 4, "gpu": "unknown"}):
        assert train_due(root, policy)["status"] == "deferred_resource"
    with patch("context_learning.shared.resource_report", return_value={
        "ready": True, "available_mib": 8192, "load_per_cpu": 0, "gpu": "mps-possible"}):
        from context_learning.local_cycle import _atomic_json
        ready = {"ready": True, "available_mib": 8192,
                 "load_per_cpu": 0, "gpu": "mps-possible"}
        with patch("context_learning.shared.resource_report",
                   side_effect=[ready, RuntimeError("synthetic_postflight_failure")]):
            interrupted_after_predict = train_due(root, policy,
                trainer_factory=lambda _: FakeTrainer())
        assert interrupted_after_predict["status"] == "failed"
        assert TRAINER_CALLS.count("train") == 1
        with _db(root) as db:
            assert db.execute("SELECT status FROM cycles").fetchone()[0] == "trained"
            original_evidence = json.loads(db.execute(
                "SELECT evidence FROM cycles").fetchone()[0])
            legacy_evidence = {k: v for k, v in original_evidence.items()
                               if k != "model_input_view"}
            db.execute("UPDATE cycles SET evidence=?", (canonical(legacy_evidence),))
        class LegacyResumeTrainer(FakeTrainer):
            def call(self, mode, payload, result_path, *, cwd):
                assert mode == "predict" and "model_input_view" not in payload
                return super().call(mode, payload, result_path, cwd=cwd)
        with patch("context_learning.shared.resource_report",
                   side_effect=[ready, RuntimeError("legacy_resume_after_predict")]):
            legacy_resume = train_due(root, policy,
                                      trainer_factory=lambda _: LegacyResumeTrainer())
        assert legacy_resume["status"] == "failed"
        assert TRAINER_CALLS.count("train") == 1
        with _db(root) as db:
            db.execute("UPDATE cycles SET evidence=?",
                       (canonical(original_evidence),))
        folder = root / "cycles" / interrupted_after_predict["cycle_id"]
        stale_pin = folder / "candidate.pin.json"
        _atomic_json(stale_pin, {"artifact_sha256": "0" * 64,
                                  "cases_sha256": "0" * 64})
        def interrupted_publish(path, value):
            _atomic_json(path, value)
            if Path(path).name == "experimental-checkpoint.json":
                raise RuntimeError("synthetic_post_publish_crash")
        with patch("context_learning.shared._atomic_json", side_effect=interrupted_publish):
            interrupted = train_due(root, policy, trainer_factory=lambda _: FakeTrainer())
        assert interrupted["status"] == "failed"
        assert TRAINER_CALLS.count("train") == 1
        assert TRAINER_CALLS.count("predict") >= 5  # legacy view and stale v2 cache rerun
        calls_after_crash = list(TRAINER_CALLS)
        completed = train_due(root, policy, trainer_factory=lambda _: FakeTrainer())
        assert TRAINER_CALLS == calls_after_crash
        assert completed["status"] == "complete", completed
        assert completed["metrics"]["train_questions"] == 4
        assert completed["metrics"]["holdout_questions"] == 1
        assert len(completed["metrics"]["candidate"]["projects"]) == 2
        assert sorted(x["questions"] for x in completed["metrics"]["candidate"]["projects"].values()) == [0, 1]
        from job_telemetry import replay
        train_events = (root / "job-telemetry.jsonl").read_text().splitlines()
        train_job = [row for row in replay(train_events)
                     if row["job_id"] == completed["cycle_id"] + ":train"]
        assert len(train_job) == 1 and train_job[0]["status"] == "success"
        assert train_job[0]["source_version"] == "base"
        assert train_job[0]["destination_version"] == completed["cycle_id"]
        assert train_job[0]["queue_wait_ms"] is not None
        assert train_due(root, policy, trainer_factory=lambda _: FakeTrainer())["status"] == "waiting"
    pointer = json.loads((root / "experimental-checkpoint.json").read_text())
    assert pointer["experimental"] is True and pointer["previous"]["artifact_path"] == str(original)
    assert pointer["schema_version"] == 1
    assert pointer["model_input_view"] == EXPERIMENT_INPUT_VIEW
    assert len(pointer["trainingProvenance"]["caseRefs"]) == 4
    assert len(pointer["sourceCases"]) == 5
    assert pointer["trainingProvenance"]["selectionFamilyKeys"]
    inherited = _training_provenance([], pointer)
    assert inherited == pointer["trainingProvenance"]
    checkpoint_path = root / "experimental-checkpoint.json"
    checkpoint_path.write_text(canonical({key: value for key, value in pointer.items()
                                          if key != "trainingProvenance"}))
    assert not trial_eligible(root)
    with patch("context_learning.shared.resource_report", return_value={"ready": True}):
        assert observe_trial(root, policy,
                             trainer_factory=lambda _: FakeTrainer())["status"] == "uncertain_training_provenance"
    checkpoint_path.write_text(canonical(pointer))
    assert trial_eligible(root)
    protected = a / ".auto-context/wiki/case-0.md"
    original_source = protected.read_bytes()
    protected.unlink()
    os.mkfifo(protected)
    began = time.monotonic()
    assert not trial_eligible(root) and time.monotonic() - began < 1
    protected.unlink(); protected.write_bytes(original_source)
    original_read = os.read
    changed_on_read = []
    def raced_read(fd, count):
        if not changed_on_read:
            protected.write_bytes(b"r" * len(original_source))
            changed_on_read.append(True)
        return original_read(fd, count)
    with patch("context_learning.safe_source.os.read", side_effect=raced_read):
        try:
            snapshot_beneath(a, ".auto-context/wiki/case-0.md")
        except ValueError as exc:
            assert str(exc) == "source_changed_during_read"
        else:
            raise AssertionError("source mutation during snapshot was accepted")
    protected.write_bytes(original_source)
    protected.write_bytes(b"x" * 65537)
    began = time.monotonic()
    assert not trial_eligible(root) and time.monotonic() - began < 1
    protected.write_bytes(original_source)
    protected.unlink(); protected.symlink_to(b / ".auto-context/wiki/case-1.md")
    assert not trial_eligible(root)
    protected.unlink(); protected.write_bytes(original_source)
    pointer_path = root / "experimental-checkpoint.json"
    pointer_path.write_text(canonical({**pointer,
        "sourceCases": pointer["sourceCases"] * 400}))
    # Deadline semantics must not depend on the OS minimum sleep resolution.
    # The original wall-clock sleep failed at this same line on the base SHA.
    simulated_clock = [0.0]
    def timed_active(*args, **kwargs):
        simulated_clock[0] += .0003
        return True
    with patch("context_learning.shared._case_active", side_effect=timed_active), patch(
            "context_learning.shared.time.monotonic", side_effect=lambda: simulated_clock[0]):
        assert not trial_eligible(root)
        assert trial_eligible(root, max_seconds=5)
    pointer_path.write_text(canonical(pointer))
    assert not trial_eligible(root, deadline=time.monotonic() - .001)
    assert trial_eligible(root)
    assert not (root / "active-checkpoint.json").exists()
    selector_policy = {"schema": "qmd-live-selector-v1",
        "trainerArgv": [sys.executable,
            str(Path("core/context_learning/laya_adapter.py").resolve()), str(base)],
        "timeoutSeconds": 10, "maxRssMib": 4096}
    (root / "live-selector.json").write_text(canonical(selector_policy))
    (root / "live-selector.json").chmod(0o600)
    class FakeSelector:
        def __init__(self, *args, **kwargs): pass
        def call(self, mode, payload, result_path, *, cwd):
            case = payload["cases"][0]
            return ({"schema_version": 1, "predictions": [{
                "request_id": case["request_id"], "input_sha256": case["input_sha256"],
                "selected_ids": [cid]}]}, {"elapsed_seconds": .01})
    from context_learning.live_select import choose
    with patch("context_learning.live_select.VerifiedLayaTrainer", FakeSelector):
        selected, reason, _ = choose(root, "What bridge?", [
            {"id": cid, "revision_sha256": digest(source), "source_text": source}],
            corpus_fingerprint=digest("synthetic corpus"),
            pointer_name="experimental-checkpoint.json")
    assert selected == [cid] and reason == "selected", (selected, reason)
    with sqlite3.connect(root / "shared-learning.sqlite3") as db:
        assert db.execute("SELECT COUNT(DISTINCT project_key) FROM cases").fetchone()[0] == 2
        assert db.execute("SELECT COUNT(*) FROM cycles WHERE status='complete'").fetchone()[0] == 1
    assert status(root)["cases"] == {"gold": 5}
    wiki = a / ".auto-context" / "wiki"; wiki.mkdir(parents=True, exist_ok=True)
    (wiki / "bridge.md").write_text(source)
    state_parent = private
    observed_config = {**config, "collectionPaths": {"wiki": ".auto-context/wiki"},
        "collectionRoles": {"wiki": "wiki"},
        "contextLearning": {"capture": True, "stateRoot": str(state_parent)}}
    os.environ["QMD_ENGINE"] = "codex"
    try:
        with patch("context_learning.shared.launch", return_value={"status": "fixture_no_worker"}):
            observed = observe({"prompt": "Where is the synthetic blue bridge?"},
                observed_config, a, [{"file": "qmd://wiki/bridge.md"}],
                phases=[{"name": "primary", "results": [{"file": "qmd://wiki/bridge.md"}],
                         "wiki_scoped": True, "returned_count": 1}],
                verdict=lambda hit, scoped: "eligible")
    finally:
        os.environ.pop("QMD_ENGINE", None)
    assert observed == "stored", observed
    state = state_parent / digest(str(a))
    with sqlite3.connect(state / "learning.sqlite3") as db:
        captured = json.loads(db.execute("SELECT body FROM samples ORDER BY rowid DESC LIMIT 1").fetchone()[0])
    assert status(root)["cases"] == {"gold": 5, "queued": 1}, status(root)["cases"]
    with _db(root) as db:
        stored = json.loads(db.execute("SELECT bodies FROM cases WHERE request_id=?", (captured["request_id"],)).fetchone()[0])
    assert stored[cid] == "Aster has a blue bridge.\n"
    rid = captured["request_id"]
    text = captured["candidates"][0]["excerpt"]["text"]
    label = {"schema_version": 2, "request_id": rid, "candidate_id": cid,
        "revision_sha256": digest(source), "abstain": False, "relevance": "irrelevant",
        "evidence": "", "rationale": {"kind": "absence", "text": "No red bridge fact is present.",
        "span": None, "scope": "provided-excerpt"}}
    input_sha = digest(canonical(captured))
    save_label(state, label, review={"reviewer_id": "synthetic-reviewer",
        "reference": "synthetic://bridge", "evidence_sha256": review_digest(label),
        "input_sha256": input_sha})
    save_selection(state, rid, [], input_sha256=input_sha,
                   reviewer_id="synthetic-reviewer", reference="synthetic://bridge")
    assert sync_reviewed(observed_config, a)["imported"] == 1
    assert status(root)["cases"] == {"gold": 6}
    with _db(root) as db:
        rows = db.execute("SELECT project_key,request_id,input_sha256,sample,bodies,gold,split,project_root,corpus_fingerprint,family_keys,trained_cycle FROM cases WHERE status='gold' AND use_state='active'").fetchall()
    assert _due_threshold(rows, 10, policy) == (5, "corpus_change")
    ids = [f"wiki/extra-{i}.md" for i in range(16)]
    expanded = {"mode": "eligible-returned-pool", "scope": "queried-phases-only",
        "phase_counts": {"primary": 8, "raw": 8}, "per_phase_limit": 8,
        "eligible_ids": ids, "captured_ids": ids, "baseline_selected_ids": ids[:3],
        "excluded": [], "snapshot_failures": [], "retrieval_truncated": False,
        "assessment_complete": True, "complete_within_returned_bound": True,
        "index_revision": digest("sixteen")}
    sixteen = request({"prompt": "Synthetic sixteen pool"}, [
        {"candidate_id": card, "revision_sha256": digest(source),
         "eligible": True, "excerpt": source} for card in ids],
        host="codex", request_id="sixteen", sampling=expanded)
    assert enqueue(observed_config, a, sixteen, {}) == "ineligible_pool_over_15"
    with _db(root) as db:
        db.execute("UPDATE cases SET split='holdout' WHERE request_id=?", (rid,))
    # Reproduce a post-training holdout whose final gold selects a fact
    # already used to train this checkpoint. Baseline focus missed the fact.
    trained_card = "wiki/case-0.md"
    trained_text = (a / ".auto-context" / trained_card).read_text()
    leaked_meta = metadata(trained_card, "post-trial-leak")
    leaked_meta["baseline_selected_ids"] = []
    leaked = request({"prompt": "What does the first synthetic signal say?"}, [
        {"candidate_id": trained_card, "revision_sha256": digest(trained_text),
         "eligible": True, "excerpt": trained_text}], host="codex",
         request_id="post-trial-leak", sampling=leaked_meta)
    local_a = private / digest(str(a))
    assert capture(leaked, enabled=True, state_dir=local_a,
                   compact_sources={trained_card: trained_text}) == "stored"
    assert enqueue(observed_config, a, leaked,
                   {trained_card: trained_text}) == "queued"
    with _db(root) as db:
        db.execute("UPDATE cases SET status='gold',split='holdout',gold=? WHERE request_id=?",
                   (canonical({"selected_ids": [trained_card],
                               "input_sha256": digest(canonical(leaked)),
                               "review": "fixture_only"}), leaked["request_id"]))
    initial_holdout_card = "wiki/case-4.md"
    initial_holdout_text = (a / ".auto-context" / initial_holdout_card).read_text()
    repeated_meta = metadata(initial_holdout_card, "post-trial-reused-holdout")
    repeated_meta["baseline_selected_ids"] = []
    repeated = request({"prompt": "Repeat the earlier fourth signal?"}, [
        {"candidate_id": initial_holdout_card,
         "revision_sha256": digest(initial_holdout_text),
         "eligible": True, "excerpt": initial_holdout_text}], host="codex",
         request_id="post-trial-reused-holdout", sampling=repeated_meta)
    assert capture(repeated, enabled=True, state_dir=local_a,
                   compact_sources={initial_holdout_card: initial_holdout_text}) == "stored"
    assert enqueue(observed_config, a, repeated,
                   {initial_holdout_card: initial_holdout_text}) == "queued"
    with _db(root) as db:
        db.execute("UPDATE cases SET status='gold',split='holdout',gold=? WHERE request_id=?",
                   (canonical({"selected_ids": [initial_holdout_card],
                               "input_sha256": digest(canonical(repeated)),
                               "review": "fixture_only"}), repeated["request_id"]))
    calls_before_observation = list(TRAINER_CALLS)
    with _db(root) as db:
        db.execute("UPDATE cases SET split='train' WHERE request_id=?", (rid,))
    with patch("context_learning.shared.resource_report", return_value={"ready": True}):
        only_leaked = observe_trial(root, policy, trainer_factory=lambda _: FakeTrainer())
    assert only_leaked["status"] == "insufficient_independent_holdout_families"
    assert only_leaked["independent_holdout"] == 0
    assert TRAINER_CALLS == calls_before_observation and pointer_path.exists()
    with _db(root) as db:
        db.execute("UPDATE cases SET split='holdout' WHERE request_id=?", (rid,))
    with patch("context_learning.shared.resource_report", return_value={
        "ready": True, "available_mib": 8192, "load_per_cpu": 0, "gpu": "mps-possible"}):
        uncertain = observe_trial(root, policy, trainer_factory=lambda _: FakeTrainer())
        restarted = observe_trial(root, policy, trainer_factory=lambda _: FakeTrainer())
    assert uncertain == restarted
    assert uncertain["status"] == "insufficient_independent_holdout_families"
    assert uncertain["independent_holdout"] == 1
    assert [item["reason"] for item in uncertain["excluded"]] == [
        "training_family_overlap", "selection_family_overlap"]
    assert TRAINER_CALLS == calls_before_observation
    assert pointer_path.exists()
    with _db(root) as db:
        assert db.execute("SELECT COUNT(*) FROM observations WHERE cycle_id=?",
                          (completed["cycle_id"],)).fetchone()[0] == 2
    # Tombstoning that contaminated local capture removes it from later
    # observations without deleting the retained historical snapshot.
    assert delete_case(local_a, leaked["request_id"]) == "deleted"
    with patch("context_learning.shared.resource_report", return_value={"ready": True}):
        after_delete = observe_trial(root, policy, trainer_factory=lambda _: FakeTrainer())
    assert after_delete["status"] == "insufficient_independent_holdout_families"
    with _db(root) as db:
        assert db.execute("SELECT use_state FROM cases WHERE request_id=?",
                          (leaked["request_id"],)).fetchone()[0] == "historical"
    fresh_card = "wiki/post-trial-independent.md"
    fresh_text = "---\ntitle: New synthetic signal\n---\nAn independent beacon is green.\n"
    (a / ".auto-context" / fresh_card).write_text(fresh_text)
    fresh = request({"prompt": "Which beacon is newly green?"}, [
        {"candidate_id": fresh_card, "revision_sha256": digest(fresh_text),
         "eligible": True, "excerpt": fresh_text}], host="codex",
         request_id="post-trial-independent", sampling=metadata(fresh_card, "post-trial-independent"))
    assert capture(fresh, enabled=True, state_dir=local_a,
                   compact_sources={fresh_card: fresh_text}) == "stored"
    assert enqueue(observed_config, a, fresh, {fresh_card: fresh_text}) == "queued"
    with _db(root) as db:
        db.execute("UPDATE cases SET status='gold',split='holdout',gold=? WHERE request_id=?",
                   (canonical({"selected_ids": [],
                               "input_sha256": digest(canonical(fresh)),
                               "review": "fixture_only"}), fresh["request_id"]))
    with patch("context_learning.shared.resource_report", return_value={
        "ready": True, "available_mib": 8192, "load_per_cpu": 0, "gpu": "mps-possible"}):
        observed_regression = observe_trial(root, policy, trainer_factory=lambda _: FakeTrainer())
    assert observed_regression["status"] == "regressed", observed_regression
    assert observed_regression["independent_holdout"] == 2
    assert len(observed_regression["excluded"]) == 1
    assert not (root / "experimental-checkpoint.json").exists()
    assert json.loads((root / "last-rollback.json").read_text())["cycle_id"] == completed["cycle_id"]
    assert rollback(root)["status"] == "no_experiment"
    # A removed owner grant or deleted project sample prevents later shared use.
    from context_learning.local_cycle import _atomic_json
    _atomic_json(root / "experimental-checkpoint.json", pointer)
    (consents / (digest(str(a)) + ".json")).unlink()
    assert not trial_eligible(root)
    calls_before_revoke = list(TRAINER_CALLS)
    with patch("context_learning.shared.resource_report", return_value={"ready": True}):
        revoked_observation = observe_trial(root, policy,
                                            trainer_factory=lambda _: FakeTrainer())
    assert revoked_observation["status"] == "source_revoked_before_observation"
    assert TRAINER_CALLS == calls_before_revoke
    with patch("context_learning.shared.resource_report", return_value={
        "ready": False, "available_mib": 0, "load_per_cpu": 4, "gpu": "unknown"}):
        assert run(root)["status"] == "deferred_resource"
    assert not (root / "experimental-checkpoint.json").exists()
    _reconcile_cases(root)
    assert status(root)["use_states"]["historical"] >= 1
    local_b = private / digest(str(b))
    assert delete_case(local_b, "case-1") == "deleted"
    assert _reconcile_cases(root) >= 1
    with _db(root) as db:
        assert db.execute("SELECT use_state FROM cases WHERE request_id='case-1'").fetchone()[0] == "historical"
        assert db.execute("SELECT sample FROM cases WHERE request_id='case-1'").fetchone()[0]
    print(json.dumps({"shared_queue": "passed", "local_trial": "passed",
                      "external_calls": 0}))

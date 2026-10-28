"""Repeated background cards, final-gold leakage and lexical cadence."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch

from context_learning.contracts import digest, request
from context_learning.shared import (_db, _due_threshold, _holdout_diagnostic,
                                     _evaluation_keys, _training_exposure_keys,
                                     _selection_exposure_keys, _screen_observation,
                                     _training_provenance,
                                     enqueue, observe_trial, status, train_due)
from context_learning.store import canonical, capture


def scenario(base, *, focus_common):
    base.mkdir()
    project = base / "project"; project.mkdir()
    wiki = project / ".auto-context/wiki"; wiki.mkdir(parents=True)
    private = base / "private"; private.mkdir(mode=0o700)
    root = base / "shared"; root.mkdir(mode=0o700)
    consents = root / "consents"; consents.mkdir(mode=0o700)
    (project / ".auto-context/settings.json").write_text(canonical({
        "indexing": True, "collections": ["wiki"],
        "collectionPaths": {"wiki": ".auto-context/wiki"},
        "collectionRoles": {"wiki": "wiki"},
        "contextLearning": {"capture": True, "stateRoot": str(private)},
        "sharedLearning": {"enabled": True, "root": str(root)}}))
    grant = consents / (digest(str(project)) + ".json")
    grant.write_text(canonical({"schema": "qmd-shared-consent-v1",
        "projectRoot": str(project), "sharedRoot": str(root),
        "capture": True, "localTraining": True, "externalTeacher": False}))
    grant.chmod(0o600)
    base_artifact = base / "base.artifact"; base_artifact.write_text("base"); base_artifact.chmod(0o600)
    policy = {"schema": "qmd-shared-learning-v1", "enabled": True,
        "goldModel": "gpt-6.1-sol", "maxGoldCalls": 10,
        "batchMin": 5, "batchMax": 20, "targetExact": .95,
        "trainerArgv": [sys.executable,
            str(Path("core/context_learning/laya_adapter.py").resolve()), str(base)],
        "baseArtifact": str(base_artifact), "maxRssMib": 4096,
        "minAvailableMib": 128, "maxLoadPerCpu": 4,
        "safetySeconds": 900, "experimental": True}
    policy_path = root / "shared-policy.json"
    policy_path.write_text(canonical(policy)); policy_path.chmod(0o600)
    overview = "---\ntitle: Shared background\n---\nThe observatory is open.\n"
    (wiki / "overview.md").write_text(overview)
    overview_id = "wiki/overview.md"
    local = private / digest(str(project)); local.mkdir(mode=0o700)
    for i in range(5):
        target_id = f"wiki/target-{i}.md"
        target = f"---\ntitle: Distinct target {i}\n---\nSignal {i} is violet.\n"
        (wiki / f"target-{i}.md").write_text(target)
        focused = overview_id if focus_common else target_id
        candidates = [{"candidate_id": target_id, "revision_sha256": digest(target),
                       "eligible": True, "excerpt": target}]
        if focus_common:
            candidates.append({"candidate_id": overview_id,
                               "revision_sha256": digest(overview),
                               "eligible": True, "excerpt": overview})
        sample = request({"prompt": f"Which signal is violet in sector {i}?"}, candidates, host="codex",
            request_id=f"case-{i}", sampling={
                "mode": "eligible-returned-pool", "scope": "queried-phases-only",
                "phase_counts": {"primary": len(candidates)}, "per_phase_limit": 8,
                "eligible_ids": [c["candidate_id"] for c in candidates],
                "captured_ids": [c["candidate_id"] for c in candidates],
                "baseline_selected_ids": [focused], "excluded": [],
                "snapshot_failures": [], "retrieval_truncated": False,
                "assessment_complete": True,
                "complete_within_returned_bound": True,
                "index_revision": digest("shared-index")})
        sources = {target_id: target}
        if focus_common:
            sources[overview_id] = overview
        assert capture(sample, enabled=True, state_dir=local,
                       compact_sources=sources) == "stored"
        assert enqueue({"sharedLearning": {"enabled": True, "root": str(root)}},
                       project, sample, sources) == "queued"
        with _db(root) as db:
            db.execute("UPDATE cases SET status='gold',gold=? WHERE request_id=?",
                       (canonical({"selected_ids": [target_id],
                                   "input_sha256": digest(canonical(sample)),
                                   "review": "synthetic_fixture"}), f"case-{i}"))
    return root, policy, overview_id


class FakeTrainer:
    calls = []

    def call(self, mode, payload, result_path, *, cwd):
        self.calls.append(mode)
        if mode == "train":
            Path(payload["artifact_path"]).write_text("synthetic-trained")
            Path(payload["artifact_path"]).chmod(0o600)
            answer = {"schema_version": 1, "status": "trained"}
        else:
            answer = {"schema_version": 1, "predictions": [
                {"request_id": case["request_id"],
                 "input_sha256": case["input_sha256"],
                 "selected_ids": [case["candidates"][0]["id"]]}
                for case in payload["cases"]]}
        Path(result_path).write_text(canonical(answer))
        Path(result_path).chmod(0o600)
        return answer, {"fixture": True}


def add_gold(root, start, count, *, contaminate=0):
    project = root.parent / "project"
    wiki = project / ".auto-context/wiki"
    private = root.parent / "private" / digest(str(project))
    old_train = "wiki/target-0.md"
    contaminated = []
    for i in range(start, start + count):
        # Deliberately reuse a trained card only in the contamination fixture.
        old_cards = [old_train] if contaminate else []
        target_id = f"wiki/target-{i}.md"
        target = f"---\ntitle: New target {i}\n---\nUnique new signal {i}.\n"
        (wiki / f"target-{i}.md").write_text(target)
        bodies = {target_id: target}
        bodies.update({cid: (project / ".auto-context" / cid).read_text()
                       for cid in old_cards})
        candidates = [{"candidate_id": cid,
                       "revision_sha256": digest(body), "eligible": True,
                       "excerpt": body} for cid, body in bodies.items()]
        sample = request({"prompt": f"Which new sector {i} signal applies?"},
            candidates, host="codex", request_id=f"case-{i}", sampling={
                "mode": "eligible-returned-pool", "scope": "queried-phases-only",
                "phase_counts": {"primary": 3}, "per_phase_limit": 8,
                "eligible_ids": list(bodies), "captured_ids": list(bodies),
                "baseline_selected_ids": [target_id], "excluded": [],
                "snapshot_failures": [], "retrieval_truncated": False,
                "assessment_complete": True,
                "complete_within_returned_bound": True,
                "index_revision": digest(f"index-{i}")})
        assert capture(sample, enabled=True, state_dir=private,
                       compact_sources=bodies) == "stored"
        assert enqueue({"sharedLearning": {"enabled": True, "root": str(root)}},
                       project, sample, bodies) == "queued"
        with _db(root) as db:
            split = db.execute("SELECT split FROM cases WHERE request_id=?",
                               (f"case-{i}",)).fetchone()[0]
            selected = target_id
            if split == "holdout" and len(contaminated) < contaminate:
                selected = old_train
                contaminated.append(f"case-{i}")
            db.execute("UPDATE cases SET status='gold',gold=? WHERE request_id=?",
                (canonical({"selected_ids": [selected],
                            "input_sha256": digest(canonical(sample)),
                            "review": "synthetic_fixture"}), f"case-{i}"))
    return contaminated


with tempfile.TemporaryDirectory(prefix="qmd-shared-family-", dir="/tmp") as name:
    base = Path(name).resolve()
    common_root, common_policy, _ = scenario(base / "common", focus_common=True)
    with patch("context_learning.shared.resource_report", return_value={"ready": True}):
        common_cycle = train_due(common_root, common_policy,
                                 trainer_factory=lambda _: FakeTrainer())
    assert common_cycle["status"] == "complete", common_cycle
    assert common_cycle["metrics"]["evaluation_scope"] == "unseen_query_family_seen_corpus"
    assert common_cycle["metrics"]["candidate_document_disjoint"]["status"] == "unavailable"
    repeated_root, _, _ = scenario(base / "repeated", focus_common=False)
    repeated_project = repeated_root.parent / "project"
    repeated_private = repeated_root.parent / "private" / digest(str(repeated_project))
    with _db(repeated_root) as db:
        held_sample, held_bodies = db.execute(
            "SELECT sample,bodies FROM cases WHERE request_id='case-4'").fetchone()
    held_bodies = {cid: (repeated_project / ".auto-context" / cid).read_text()
                   for cid in json.loads(held_bodies)}
    for repeat in range(2):
        sample = json.loads(held_sample)
        sample["request_id"] = f"repeat-holdout-{repeat}"
        repeat_capture = capture(sample, enabled=True, state_dir=repeated_private,
                                 compact_sources=held_bodies)
        assert repeat_capture == "stored", repeat_capture
        assert enqueue({"sharedLearning": {"enabled": True, "root": str(repeated_root)}},
                       repeated_project, sample, held_bodies) == "queued"
        with _db(repeated_root) as db:
            assert db.execute("SELECT split FROM cases WHERE request_id=?",
                              (sample["request_id"],)).fetchone()[0] == "holdout"
    for i in range(5, 10):
        candidate_id = f"wiki/unique-{i}.md"
        body = f"---\ntitle: Unique {i}\n---\nBeacon {i} is amber.\n"
        (repeated_project / ".auto-context" / candidate_id).write_text(body)
        sample = request({"prompt": f"Which beacon is amber in unique sector {i}?"},
            [{"candidate_id": candidate_id, "revision_sha256": digest(body),
              "eligible": True, "excerpt": body}], host="codex",
            request_id=f"unique-{i}", sampling={
                "mode": "eligible-returned-pool", "scope": "queried-phases-only",
                "phase_counts": {"primary": 1}, "per_phase_limit": 8,
                "eligible_ids": [candidate_id], "captured_ids": [candidate_id],
                "baseline_selected_ids": [candidate_id], "excluded": [],
                "snapshot_failures": [], "retrieval_truncated": False,
                "assessment_complete": True, "complete_within_returned_bound": True,
                "index_revision": digest(f"unique-index-{i}")})
        bodies = {candidate_id: body}
        assert capture(sample, enabled=True, state_dir=repeated_private,
                       compact_sources=bodies) == "stored"
        assert enqueue({"sharedLearning": {"enabled": True, "root": str(repeated_root)}},
                       repeated_project, sample, bodies) == "queued"
        with _db(repeated_root) as db:
            assigned_split = db.execute("SELECT split FROM cases WHERE request_id=?",
                                        (sample["request_id"],)).fetchone()[0]
        assert assigned_split == ("holdout" if i == 9 else "train")
    distinct_root, distinct_policy, overview_id = scenario(base / "distinct", focus_common=False)
    with _db(distinct_root) as db:
        rows = db.execute("SELECT project_key,request_id,input_sha256,sample,bodies,gold,split,project_root,corpus_fingerprint,family_keys,trained_cycle FROM cases ORDER BY request_id").fetchall()
    assert [r[6] for r in rows] == ["train"] * 4 + ["holdout"]
    assert _holdout_diagnostic(rows) is None
    # Final target in a train pool as a negative was invisible to the old
    # selected-fact check, but its body is passed to Laya training.
    exposed = list(rows[0])
    final_sample = json.loads(rows[4][3])
    final_candidate = final_sample["candidates"][0]
    exposed_sample = json.loads(exposed[3])
    exposed_sample["candidates"].append(final_candidate)
    exposed_bodies = json.loads(exposed[4])
    exposed_bodies.update(json.loads(rows[4][4]))
    exposed[3], exposed[4] = canonical(exposed_sample), canonical(exposed_bodies)
    assert not (_evaluation_keys(tuple(exposed)) & _evaluation_keys(rows[4]))
    assert _training_exposure_keys(tuple(exposed)) & _evaluation_keys(rows[4])
    assert _holdout_diagnostic([tuple(exposed), *rows[1:]]) is None
    # A final fact in an unselected validation candidate also reaches model
    # selection, although its question never trained model weights.
    final_id, final_body = "wiki/sealed-final.md", "Invented sealed final fact."
    final_candidate = {"candidate_id": final_id, "revision_sha256": digest(final_body)}
    validation = list(rows[4]); validation_sample = json.loads(validation[3])
    validation_sample["candidates"].append(final_candidate)
    validation_bodies = json.loads(validation[4]); validation_bodies[final_id] = final_body
    validation[3], validation[4] = canonical(validation_sample), canonical(validation_bodies)
    final_row = list(rows[4])
    final_sample = json.loads(final_row[3]); final_sample["candidates"] = [final_candidate]
    final_sample["prompt"]["text"] = "What is the sealed final fact?"
    final_row[3], final_row[4] = canonical(final_sample), canonical({final_id: final_body})
    final_row[5] = canonical({"selected_ids": [final_id]})
    assert not (_evaluation_keys(tuple(validation)) & _evaluation_keys(tuple(final_row)))
    assert _selection_exposure_keys(tuple(validation)) & _evaluation_keys(tuple(final_row))
    provenance = _training_provenance([*rows[:4], tuple(validation)], None)
    assert set(provenance["selectionFamilyKeys"]) & _evaluation_keys(tuple(final_row))
    # The next gold can select a new target while still seeing a previously
    # validated candidate as a negative. Its full pool is not fresh evidence.
    next_row = list(rows[4]); next_sample = json.loads(next_row[3])
    next_sample["prompt"]["text"] = "Which new independent beacon appears next?"
    new_id, new_body = "wiki/new-independent.md", "Invented new independent fact."
    next_sample["candidates"] = [
        {"candidate_id": new_id, "revision_sha256": digest(new_body)},
        final_candidate]
    next_bodies = {new_id: new_body, final_id: final_body}
    next_row[3], next_row[4] = canonical(next_sample), canonical(next_bodies)
    next_row[5] = canonical({"selected_ids": [new_id]})
    assert not (_evaluation_keys(tuple(next_row)) &
                set(provenance["selectionFamilyKeys"]))
    clean, excluded = _screen_observation(
        [tuple(next_row)], set(), selection_keys=provenance["selectionQueryFamilyKeys"])
    assert len(clean) == 1 and not excluded
    accepted, repeated = _screen_observation([tuple(validation), tuple(next_row)], set())
    assert len(accepted) == 2 and not repeated
    prior = list(rows[0]); prior[10] = "previous"
    incoming = list(rows[1]); changed = json.loads(incoming[3])
    changed["prompt"]["text"] = "Observe lunar orchard alignment"
    incoming[3] = canonical(changed); incoming[9] = prior[9]
    assert _due_threshold([tuple(prior), tuple(incoming)], 10, distinct_policy) == (
        5, "new_question_topic")
    # A later holdout containing even a negative trained card is excluded.
    contaminated_distinct = add_gold(distinct_root, 5, 5, contaminate=1)
    assert len(contaminated_distinct) == 1
    with patch("context_learning.shared.resource_report", return_value={"ready": True}):
        recovered = train_due(distinct_root, distinct_policy,
                              trainer_factory=lambda _: FakeTrainer())
    assert recovered["status"] == "complete", recovered
    assert recovered["metrics"]["fresh_holdout_questions"] >= 1
    assert any(item["reason"] == "training_family_overlap"
               for item in recovered["metrics"]["excluded_holdout"])
    # A fixed validation case may be reused on every cumulative cycle. Only
    # new independent families support the next cadence/quality observation.
    progressive_root, progressive_policy, _ = scenario(base / "progressive", focus_common=False)
    ready = {"ready": True}
    with patch("context_learning.shared.resource_report", return_value=ready):
        first = train_due(progressive_root, progressive_policy,
                          trainer_factory=lambda _: FakeTrainer())
    assert first["status"] == "complete", first
    assert first["metrics"]["fresh_holdout_questions"] == 1
    assert first["metrics"]["fixed_validation_questions"] == 0
    add_gold(progressive_root, 5, 10)
    with patch("context_learning.shared.resource_report", return_value=ready):
        second = train_due(progressive_root, progressive_policy,
                           trainer_factory=lambda _: FakeTrainer())
    assert second["status"] == "complete", second
    assert second["metrics"]["fresh_holdout_questions"] == 2
    assert second["metrics"]["fixed_validation_questions"] == 1
    second_pointer = json.loads((progressive_root / "experimental-checkpoint.json").read_text())
    assert len(second_pointer["validationCases"]) == 3
    restarted = subprocess.run([sys.executable, "core/context_learning_shared.py",
        "status", str(progressive_root)], capture_output=True, text=True, check=True,
        env={**os.environ, 'HOME': str(base), 'PYTHONDONTWRITEBYTECODE': '1'})
    assert json.loads(restarted.stdout)["status"] == "ok"
    # Repeated old train and validation facts are excluded as new evidence;
    # they cannot veto reuse of the exact pinned validation captures.
    contaminated = add_gold(progressive_root, 15, 7, contaminate=2)
    assert len(contaminated) == 2
    calls_before_wait = list(FakeTrainer.calls)
    with patch("context_learning.shared.resource_report", return_value=ready):
        waiting = train_due(progressive_root, progressive_policy,
                            trainer_factory=lambda _: FakeTrainer())
    assert waiting["status"] == "insufficient_independent_holdout_families", waiting
    assert waiting["reason"] == "insufficient_fresh_holdout"
    assert waiting["fresh"] == 1 and len(waiting["excluded"]) == 2, waiting
    assert {entry["reason"] for entry in waiting["excluded"]} == {
        "training_family_overlap"}
    assert FakeTrainer.calls == calls_before_wait
    add_gold(progressive_root, 30, 5)
    with patch("context_learning.shared.resource_report", return_value=ready):
        observed = observe_trial(progressive_root, progressive_policy,
                                 trainer_factory=lambda _: FakeTrainer())
        repeated_observation = observe_trial(progressive_root, progressive_policy,
                                             trainer_factory=lambda _: FakeTrainer())
    assert observed == repeated_observation
    assert observed["status"] == "observed" and observed["independent_holdout"] == 2
    assert len(observed["excluded"]) == 2
    with patch("context_learning.shared.resource_report", return_value=ready):
        third = train_due(progressive_root, progressive_policy,
                          trainer_factory=lambda _: FakeTrainer())
    assert third["status"] == "complete", third
    assert third["metrics"]["fresh_holdout_questions"] == 2
    assert third["metrics"]["fixed_validation_questions"] == 3
    assert len(third["metrics"]["excluded_holdout"]) == 2
    assert third["metrics"]["evaluation_scope"] == "unseen_query_family_seen_corpus"
    assert third["metrics"]["fresh_candidate"]["questions"] == 2
    assert len(json.loads((progressive_root / "experimental-checkpoint.json").read_text())[
        "validationCases"]) == 5
    with patch("context_learning.shared.resource_report", return_value=ready):
        assert train_due(progressive_root, progressive_policy,
                         trainer_factory=lambda _: FakeTrainer())["status"] == "waiting"
    print(json.dumps({"independent_holdout": True, "no_false_quality": True}))

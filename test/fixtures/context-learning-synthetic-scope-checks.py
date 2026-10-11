"""No CLI call: exact synthetic transport scope and ordinary-project denial."""
import hashlib
import json
import subprocess
from pathlib import Path
import secrets
import sys
import tempfile
from unittest.mock import patch

from context_learning.compact_input import derive
from context_learning.contracts import digest, request
from context_learning.synthetic_scope import allowed_case, policy_for
import context_learning.synthetic_scope as synthetic_scope
import context_learning.synthetic_experiment as experiment_module
from context_learning.synthetic_experiment import PLANNED_BUNDLE_NAME, SHARED_NAME, PRIVATE_NAME
from context_learning.synthetic_teacher import _invoke_codex, process_pending
from context_learning.shared import _db, enqueue
from context_learning.store import canonical, capture


def rejected(fn):
    try:
        fn()
    except ValueError:
        return True
    raise AssertionError("synthetic scope unexpectedly allowed")


with tempfile.TemporaryDirectory(prefix="qmd-parent-approval-", dir="/tmp") as approval_name, tempfile.TemporaryDirectory(prefix="qmd-v2-shared-synthetic-check-", dir="/tmp") as name:
    novel_root = Path(name).resolve()
    synthetic_scope.NOVEL_ROOT = novel_root
    bundle = novel_root
    approval_dir = Path(approval_name).resolve()
    approval_dir.chmod(0o700)
    approval_path = approval_dir / "current.json"
    synthetic_scope.APPROVAL_FILE = approval_path
    project = bundle / PLANNED_BUNDLE_NAME; project.mkdir(mode=0o700)
    shared = bundle / SHARED_NAME; shared.mkdir(mode=0o700)
    private = bundle / PRIVATE_NAME; private.mkdir(mode=0o700)
    ordinary = bundle / "ordinary-project"; ordinary.mkdir(mode=0o700)
    source = "---\ntitle: Synthetic bridge\n---\nThe synthetic bridge is blue.\n"
    cid = "wiki/synthetic.md"
    consents = shared / "consents"; consents.mkdir(mode=0o700)
    for enrolled in (project, ordinary):
        wiki = enrolled / ".auto-context" / "wiki"
        wiki.mkdir(parents=True)
        (wiki / "synthetic.md").write_text(source)
        (enrolled / ".auto-context" / "settings.json").write_text(canonical({
            "indexing": True, "collections": ["wiki"],
            "collectionPaths": {"wiki": ".auto-context/wiki"},
            "collectionRoles": {"wiki": "wiki"},
            "contextLearning": {"capture": True, "stateRoot": str(private)},
            "sharedLearning": {"enabled": True, "root": str(shared)}}))
        consent = consents / (digest(str(enrolled)) + ".json")
        consent.write_text(canonical({"schema": "qmd-shared-consent-v1",
            "projectRoot": str(enrolled), "sharedRoot": str(shared),
            "capture": True, "localTraining": True, "externalTeacher": False}))
        consent.chmod(0o600)
    marker = {"schema": "qmd-synthetic-project-v1", "projectRoot": str(project),
              "createdUtc": "2026-10-07T00:00:00Z", "nonce": secrets.token_hex(24),
              "origin": "newly-generated-synthetic-only",
              "allowedReceiver": "codex-cli", "allowedPurpose": "selection-gold",
              "maxGoldCalls": 10, "model": "gpt-6.1-sol"}
    marker_path = project / ".qmd-synthetic-project.json"
    marker_path.write_text(canonical(marker)); marker_path.chmod(0o600)
    provenance = {"schema": "qmd-synthetic-provenance-v1",
                  "projectRoot": str(project),
                  "generator": "newly-generated-synthetic-only",
                  "nonce": marker["nonce"],
                  "sources": {cid: digest(source)},
                  "questions": {
                      "synthetic-only-1": digest("What color is the synthetic bridge?"),
                      "synthetic-only-2": digest("Which synthetic bridge is blue?"),
                      "synthetic-only-3": digest("Synthetic uncertain prompt")}}
    provenance_path = project / "synthetic-provenance.json"
    provenance_path.write_text(canonical(provenance)); provenance_path.chmod(0o600)
    policy = {"schema": "qmd-synthetic-teacher-v1", "enabled": True,
              "projectRoot": str(project),
              "markerSha256": hashlib.sha256(marker_path.read_bytes()).hexdigest(),
              "provenanceSha256": hashlib.sha256(provenance_path.read_bytes()).hexdigest(),
              "receiver": "codex-cli", "purpose": "selection-gold",
              "model": "gpt-6.1-sol", "maxCalls": 10,
              "maxInputBytes": 131072, "maxOutputBytes": 32768,
              "reservedUsd": 5, "reserveUsdPerCall": .5}
    policy_path = shared / "synthetic-teacher-policy.json"
    policy_path.write_text(canonical(policy)); policy_path.chmod(0o600)
    assert rejected(lambda: policy_for(shared, project))  # Missing experiment control denies egress.
    # Exercise the older policy validator independently; current production
    # transport additionally requires the bounded experiment control.
    old_mode = patch.object(experiment_module, "load", return_value=None)
    old_mode.start()
    def approve():
        approval_path.write_text(canonical({
            "schema": "qmd-synthetic-parent-approval-v1", "enabled": True,
            "projectRoot": str(project), "sharedRoot": str(shared),
            "generationManifestSha256": policy["provenanceSha256"],
            "markerSha256": policy["markerSha256"],
            "receiver": "codex-cli", "purpose": "selection-gold",
            "model": "gpt-6.1-sol"}))
        approval_path.chmod(0o600)
    approve()
    with tempfile.TemporaryDirectory(prefix="qmd-v2-shared-synthetic-forged-", dir="/tmp") as forged_name:
        forged_bundle = Path(forged_name).resolve()
        forged_project = forged_bundle / "project"; forged_project.mkdir(mode=0o700)
        forged_shared = forged_bundle / "shared-state"; forged_shared.mkdir(mode=0o700)
        forged_marker = {**marker, "projectRoot": str(forged_project)}
        forged_marker_path = forged_project / ".qmd-synthetic-project.json"
        forged_marker_path.write_text(canonical(forged_marker)); forged_marker_path.chmod(0o600)
        forged_manifest = {**provenance, "projectRoot": str(forged_project)}
        forged_manifest_path = forged_project / "synthetic-provenance.json"
        forged_manifest_path.write_text(canonical(forged_manifest)); forged_manifest_path.chmod(0o600)
        forged_policy = {**policy, "projectRoot": str(forged_project),
            "markerSha256": hashlib.sha256(forged_marker_path.read_bytes()).hexdigest(),
            "provenanceSha256": hashlib.sha256(forged_manifest_path.read_bytes()).hexdigest()}
        forged_policy_path = forged_shared / "synthetic-teacher-policy.json"
        forged_policy_path.write_text(canonical(forged_policy)); forged_policy_path.chmod(0o600)
        assert rejected(lambda: policy_for(forged_shared, forged_project))
    meta = {"mode": "eligible-returned-pool", "scope": "queried-phases-only",
            "phase_counts": {"primary": 1}, "per_phase_limit": 8,
            "eligible_ids": [cid], "captured_ids": [cid],
            "baseline_selected_ids": [cid], "excluded": [],
            "snapshot_failures": [], "retrieval_truncated": False,
            "assessment_complete": True, "complete_within_returned_bound": True,
            "index_revision": digest("synthetic-index")}
    sample = request({"prompt": "What color is the synthetic bridge?"}, [
        {"candidate_id": cid, "revision_sha256": digest(source),
         "eligible": True, "excerpt": source}],
        host="codex", request_id="synthetic-only-1", sampling=meta)
    bodies = {cid: derive(source, digest(source))["body_text"]}
    assert allowed_case(shared, project, sample, bodies)[0] == policy
    assert rejected(lambda: allowed_case(shared, ordinary, sample, bodies))
    other = bundle / "other"; other.mkdir(mode=0o700)
    assert rejected(lambda: allowed_case(other, project, sample, bodies))
    assert rejected(lambda: allowed_case(shared, project, sample, {cid: "x" * 16385}))
    wrong = dict(policy, receiver="arbitrary-service")
    policy_path.write_text(canonical(wrong))
    assert rejected(lambda: policy_for(shared, project))
    policy_path.write_text(canonical(policy))
    marker_path.write_text(canonical({**marker, "origin": "preexisting-private-project"}))
    assert rejected(lambda: policy_for(shared, project))
    marker_path.write_text(canonical(marker))
    provenance_path.write_text(canonical({**provenance, "generator": "imported-private-text"}))
    assert rejected(lambda: policy_for(shared, project))
    provenance_path.write_text(canonical(provenance))
    (project / ".auto-context" / cid).write_text("changed source")
    assert rejected(lambda: allowed_case(shared, project, sample, bodies))
    (project / ".auto-context" / cid).write_text(source)
    worker_policy = {"schema": "qmd-shared-learning-v1", "enabled": True,
        "goldModel": "gpt-6.1-sol", "maxGoldCalls": 10,
        "batchMin": 5, "batchMax": 20, "targetExact": .95,
        "trainerArgv": [sys.executable, str(Path("core/context_learning/laya_adapter.py").resolve()), str(bundle)],
        "baseArtifact": str(bundle / "base.artifact"), "maxRssMib": 4096,
        "minAvailableMib": 128, "maxLoadPerCpu": 4,
        "safetySeconds": 900, "experimental": True}
    (shared / "shared-policy.json").write_text(canonical(worker_policy))
    (shared / "shared-policy.json").chmod(0o600)
    metadata = dict(meta, index_revision=digest("synthetic index 2"))
    made = request({"prompt": "Which synthetic bridge is blue?"}, [
        {"candidate_id": cid, "revision_sha256": digest(source),
         "eligible": True, "excerpt": source}],
        host="codex", request_id="synthetic-only-2", sampling=metadata)
    ordinary_case = request({"prompt": "Ordinary project must not transfer"}, [
        {"candidate_id": cid, "revision_sha256": digest(source),
         "eligible": True, "excerpt": source}],
        host="codex", request_id="ordinary-1", sampling=metadata)
    shared_cfg = {"sharedLearning": {"enabled": True, "root": str(shared)}}
    for enrolled, case in ((project, made), (ordinary, ordinary_case)):
        state = private / digest(str(enrolled)); state.mkdir(mode=0o700)
        assert capture(case, enabled=True, state_dir=state,
                       compact_sources={cid: source}) == "stored"
    assert enqueue(shared_cfg, project, made, {cid: source}) == "queued"
    assert enqueue(shared_cfg, ordinary, ordinary_case, {cid: source}) == "queued"
    # A grant withdrawn after reservation but before subprocess launch must
    # stop the exact egress attempt.
    grant = consents / (digest(str(project)) + ".json")
    grant_bytes = grant.read_bytes()
    checks = []
    def withdraw_at_boundary(root, target, case, body, **kwargs):
        checks.append(1)
        if len(checks) == 2:
            grant.unlink()
        return allowed_case(root, target, case, body, **kwargs)
    with patch("context_learning.synthetic_teacher.allowed_case",
               side_effect=withdraw_at_boundary), patch(
                   "context_learning.synthetic_teacher.shutil.which",
                   return_value="/bin/true"), patch(
                   "context_learning.synthetic_teacher.subprocess.Popen",
                   side_effect=AssertionError("egress_occurred")):
        assert rejected(lambda: _invoke_codex(made, {cid: bodies[cid]}, policy,
                                              shared, worker_policy))
    grant.write_bytes(grant_bytes); grant.chmod(0o600)
    assert len(checks) == 2
    # Withdrawal after Popen terminates the owned process group and cannot
    # turn its eventual output into gold. The child here is local sleep only.
    spawned = []
    real_popen = subprocess.Popen
    def spawn_sleep(_args, **kwargs):
        child = real_popen(["/bin/sleep", "10"], **kwargs)
        spawned.append(child)
        grant.unlink()
        return child
    with patch("context_learning.synthetic_teacher.shutil.which", return_value="/bin/sleep"), patch(
            "context_learning.synthetic_teacher.subprocess.Popen", side_effect=spawn_sleep):
        assert rejected(lambda: _invoke_codex(made, {cid: bodies[cid]}, policy,
                                              shared, worker_policy))
    assert len(spawned) == 1 and spawned[0].poll() is not None
    grant.write_bytes(grant_bytes); grant.chmod(0o600)
    calls = []
    def synthetic_answer(sample, bodies, scoped, root, worker):
        calls.append(sample["request_id"])
        return {"selected_ids": [sample["candidates"][0]["candidate_id"]],
                "usage": {}, "elapsed_seconds": .1, "peak_rss_mib": 10}
    ready = {"ready": True, "load_per_cpu": 0, "available_mib": 8192, "gpu": "cpu"}
    with patch("context_learning.synthetic_teacher.resource_report", return_value=ready):
        assert process_pending(shared, project, worker_policy,
                               invoke=synthetic_answer,
                               request_ids=('not-listed',)) == {'status': 'drained', 'outcomes': []}
        assert calls == []
        processed = process_pending(shared, project, worker_policy,
                                    invoke=synthetic_answer,
                                    request_ids=('synthetic-only-2',))
        assert processed["outcomes"] == ["first_done", "gold"]
        assert process_pending(shared, project, worker_policy, invoke=synthetic_answer)["outcomes"] == []
    assert calls == ["synthetic-only-2", "synthetic-only-2"]
    assert rejected(lambda: process_pending(shared, ordinary, worker_policy, invoke=synthetic_answer))
    with _db(shared) as db:
        rows = dict(db.execute("SELECT request_id,status FROM cases"))
        assert rows == {"synthetic-only-2": "gold", "ordinary-1": "queued"}
        assert json.loads(db.execute("SELECT value FROM meta WHERE key='gold_calls_used'").fetchone()[0]) == 2
    failed = request({"prompt": "Synthetic uncertain prompt"}, [
        {"candidate_id": cid, "revision_sha256": digest(source),
         "eligible": True, "excerpt": source}],
        host="codex", request_id="synthetic-only-3", sampling=metadata)
    state = private / digest(str(project))
    assert capture(failed, enabled=True, state_dir=state,
                   compact_sources={cid: source}) == "stored"
    assert enqueue(shared_cfg, project, failed, {cid: source}) == "queued"
    def uncertain(*_):
        raise ValueError("synthetic uncertain call")
    with patch("context_learning.synthetic_teacher.resource_report", return_value=ready):
        assert process_pending(shared, project, worker_policy, invoke=uncertain)["outcomes"] == ["pending_review"]
        assert process_pending(shared, project, worker_policy, invoke=synthetic_answer)["outcomes"] == []
    with _db(shared) as db:
        assert db.execute("SELECT status FROM cases WHERE request_id='synthetic-only-3'").fetchone()[0] == "pending_review"
        assert json.loads(db.execute("SELECT value FROM meta WHERE key='gold_calls_used'").fetchone()[0]) == 3
    cid2 = "wiki/second.md"
    source2 = "---\ntitle: Synthetic second\n---\nThe synthetic tower is amber.\n"
    (project / ".auto-context" / cid2).write_text(source2)
    old = request({"prompt": "Stale synthetic source?"}, [
        {"candidate_id": cid, "revision_sha256": digest(source),
         "eligible": True, "excerpt": source}],
        host="codex", request_id="synthetic-only-4", sampling=metadata)
    fresh_meta = {**metadata, "eligible_ids": [cid2], "captured_ids": [cid2],
                  "baseline_selected_ids": [cid2],
                  "index_revision": digest("synthetic second index")}
    fresh = request({"prompt": "What color is the synthetic tower?"}, [
        {"candidate_id": cid2, "revision_sha256": digest(source2),
         "eligible": True, "excerpt": source2}],
        host="codex", request_id="synthetic-only-5", sampling=fresh_meta)
    provenance["sources"][cid2] = digest(source2)
    provenance["questions"].update({
        old["request_id"]: old["prompt"]["sha256"],
        fresh["request_id"]: fresh["prompt"]["sha256"]})
    provenance_path.write_text(canonical(provenance))
    policy["provenanceSha256"] = hashlib.sha256(provenance_path.read_bytes()).hexdigest()
    policy_path.write_text(canonical(policy))
    assert rejected(lambda: policy_for(shared, project))  # Parent pin is stale.
    approve()
    assert capture(old, enabled=True, state_dir=state,
                   compact_sources={cid: source}) == "stored"
    assert capture(fresh, enabled=True, state_dir=state,
                   compact_sources={cid2: source2}) == "stored"
    assert enqueue(shared_cfg, project, old, {cid: source}) == "queued"
    assert enqueue(shared_cfg, project, fresh, {cid2: source2}) == "queued"
    (project / ".auto-context" / cid).write_text("deleted original revision")
    with patch("context_learning.synthetic_teacher.resource_report", return_value=ready):
        continued = process_pending(shared, project, worker_policy,
                                    invoke=synthetic_answer)
    assert continued["outcomes"] == ["historical", "first_done", "gold"], continued
    with _db(shared) as db:
        assert db.execute("SELECT use_state FROM cases WHERE request_id='synthetic-only-4'").fetchone()[0] == "historical"
        db.execute("UPDATE cases SET status='first_running' WHERE request_id='synthetic-only-5'")
    with patch("context_learning.synthetic_teacher.resource_report", return_value=ready):
        resumed = process_pending(shared, project, worker_policy,
                                  invoke=synthetic_answer)
    assert resumed["outcomes"] == ["pending_review"], resumed
    with _db(shared) as db:
        assert db.execute("SELECT status FROM cases WHERE request_id='synthetic-only-5'").fetchone()[0] == "pending_review"
        assert json.loads(db.execute("SELECT value FROM meta WHERE key='gold_calls_used'").fetchone()[0]) == 5
    old_mode.stop()
    assert rejected(lambda: policy_for(shared, project))
    print(json.dumps({"synthetic_allowed": True, "ordinary_denied": True,
                      "tamper_denied": True, "external_calls": 0}))

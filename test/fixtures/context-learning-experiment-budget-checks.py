"""Entire synthetic path with mocked Codex and Laya, never a provider call."""
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

from context_learning.contracts import digest
from context_learning.seam import observe
from context_learning.shared import _db, _policy, train_due
from context_learning.store import canonical
from context_learning.synthetic_experiment import (
    CONTROL_NAME, DISK_LIMIT, EXPERIMENT, FINAL_IDS, GOLD_LIMIT, LOG_LIMIT,
    TOTAL_LIMIT, TRAIN_LIMIT, disk_bytes, evaluate_final, snapshot)
from context_learning.synthetic_generator import source_sha256
from context_learning.synthetic_launcher import _narrative_counts, launch_one
import context_learning.synthetic_launcher as launcher_module
from context_learning.synthetic_teacher import process_pending
import context_learning.synthetic_scope as scope
import context_learning.synthetic_experiment as experiment_module
from context_learning.synthetic_scope import COLLECTION_PATHS, WIKI_COLLECTION


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(path, value):
    path.write_text(canonical(value))
    path.chmod(0o600)


def denied(call, reason):
    try:
        call()
    except ValueError as error:
        assert str(error) == reason, (error, reason)
    else:
        raise AssertionError("unexpectedly allowed")


denied(lambda: _narrative_counts("마법" * 2499), "synthetic_narrative_too_short")
denied(lambda: _narrative_counts("# 제목\n" + "마법" * 2500),
       "synthetic_narrative_format_invalid")
assert _narrative_counts("마법" * 2500) == {
    "with_whitespace": 5000, "without_whitespace": 5000,
    "hangul_syllables": 5000}
assert _narrative_counts("가" * 10000)["without_whitespace"] == 10000
assert len(launcher_module._fact_body("가" * 5000).encode()) > 4096

plugin_source = Path(launcher_module.__file__).resolve().parents[2]
plugin_version = json.loads((plugin_source / ".codex-plugin/plugin.json").read_text())["version"]
plugin_inventory = {"installed": [
    {"pluginId": "qmd-auto-context@current-local", "name": "qmd-auto-context",
     "version": plugin_version, "source": {"source": "local", "path": str(plugin_source)},
     "installed": True, "enabled": True},
    {"pluginId": "qmd-auto-context@old-local", "name": "qmd-auto-context",
     "version": "older", "source": {"source": "local", "path": str(plugin_source)},
     "installed": True, "enabled": True},
    {"pluginId": "browser@local", "name": "browser", "version": "1",
     "installed": True, "enabled": True}]}
with patch.object(launcher_module.subprocess, "run", return_value=SimpleNamespace(
        returncode=0, stdout=json.dumps(plugin_inventory))):
    assert launcher_module._plugin_overrides() == [
        'plugins."qmd-auto-context@old-local".enabled=false',
        'plugins."browser@local".enabled=false']
    with tempfile.TemporaryDirectory(prefix="qmd-plugin-integrity-", dir="/tmp") as tmp:
        copy = Path(tmp)
        for relative in ("core/context_learning/synthetic_launcher.py",
                         "core/context_learning/synthetic_scope.py",
                         "core/recall.py", "hooks/run-hook",
                         ".codex-plugin/plugin.json", "hooks/hooks-codex.json"):
            target = copy / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(plugin_source / relative, target)
        inventory = json.loads(json.dumps(plugin_inventory))
        inventory["installed"][0]["source"]["path"] = str(copy)
        with patch.object(launcher_module.subprocess, "run", return_value=SimpleNamespace(
                returncode=0, stdout=json.dumps(inventory))):
            assert launcher_module._plugin_overrides()
            for relative in (".codex-plugin/plugin.json", "hooks/hooks-codex.json"):
                target = copy / relative
                original = target.read_bytes()
                target.write_bytes(original + b" ")
                denied(launcher_module._plugin_overrides,
                       "synthetic_plugin_source_changed")
                target.write_bytes(original)

# A trusted plugin does not imply that unrelated project/user MCP servers are
# safe for the synthetic CLI. Every enabled server must be disabled before the
# call; no server configuration or credential is copied into argv or evidence.
inventory = [{"name": "private-server", "enabled": True},
             {"name": "already-off", "enabled": False}]
all_off = [{"name": row["name"], "enabled": False} for row in inventory]
with patch.object(launcher_module.subprocess, "run", side_effect=[
        SimpleNamespace(returncode=0, stdout=json.dumps(inventory)),
        SimpleNamespace(returncode=0, stdout=json.dumps(all_off))]) as probe:
    overrides = launcher_module._mcp_overrides(Path("/tmp"),
                                                ["plugins.old.enabled=false"])
    assert overrides == ['mcp_servers.private-server={command="/usr/bin/false",enabled=false}']
    assert probe.call_args_list[1].args[0][-2:] == ["-c", overrides[0]]
with patch.object(launcher_module.subprocess, "run", side_effect=[
        SimpleNamespace(returncode=0, stdout=json.dumps(inventory)),
        SimpleNamespace(returncode=0, stdout=json.dumps(inventory))]):
    denied(lambda: launcher_module._mcp_overrides(Path("/tmp"), []),
           "synthetic_mcp_disable_unverified")
with patch.object(launcher_module.subprocess, "run", return_value=SimpleNamespace(
        returncode=0, stdout=json.dumps([{"name": "unsafe.name", "enabled": True}]))):
    denied(lambda: launcher_module._mcp_overrides(Path("/tmp"), []),
           "synthetic_mcp_inventory_invalid")

# Codex app/connector tools are distinct from configured MCP servers. The
# synthetic invocation needs the qmd plugin hook, but no app/browser tools.
with patch.object(launcher_module, "_plugin_overrides", return_value=[]), patch.object(
        launcher_module, "_mcp_overrides", return_value=[]):
    query_argv = launcher_module._query_argv(Path("/tmp"))
for flag in ("features.apps=false", "features.browser_use=false",
             "features.in_app_browser=false", "features.computer_use=false"):
    assert flag in query_argv
    assert flag in launcher_module.codex_adapter.argv()


with tempfile.TemporaryDirectory(prefix="qmd-v2-shared-synthetic-test-", dir="/tmp") as temp, tempfile.TemporaryDirectory(prefix="qmd-experiment-approval-", dir="/tmp") as approvals:
    novel_root = Path(temp).resolve()
    os.environ["HOME"] = str(novel_root)
    scope.NOVEL_ROOT = novel_root
    project = novel_root
    story = project / experiment_module.PLANNED_BUNDLE_NAME; story.mkdir(mode=0o700)
    root = project / experiment_module.SHARED_NAME; root.mkdir(mode=0o700)
    private = project / experiment_module.PRIVATE_NAME; private.mkdir(mode=0o700)
    wiki = story / ".auto-context/wiki"; wiki.mkdir(parents=True)
    (project / ".auto-context/wiki").mkdir(parents=True)
    paths = scope.project_collection_paths(project)
    scope.APPROVAL_FILE = Path(approvals) / "current.json"
    config = {"indexing": True, "collections": list(paths),
              "collectionPaths": paths,
              "allowRoots": [], "injectSourcePathsPerCard": 0,
              "recallStrategy": "wikiOnly",
              "wikiPath": paths[WIKI_COLLECTION],
              "collectionRoles": {name: "wiki" if name in (WIKI_COLLECTION, scope.PROJECT_WIKI_COLLECTION) else "raw"
                                  for name in paths},
              "compile": {"mode": "off"},
              "contextLearning": {"capture": True, "stateRoot": str(private)},
              "sharedLearning": {"enabled": True, "root": str(root),
                                 "trialSelection": True}}
    save(project / ".auto-context/settings.json", config)
    # This budget fixture has no qmd runtime. Test the real fail-closed gate
    # before substituting a selected runtime for the mocked CLI sequence.
    denied(lambda: scope._experiment_config(root, project),
           "synthetic_project_index_not_selected")
    import setup_guard
    import qmd_route
    index_path = project / "synthetic-selected-index.sqlite"
    with sqlite3.connect(index_path) as index:
        index.execute("CREATE TABLE documents (collection TEXT,path TEXT,hash TEXT,active INTEGER)")
    patch("setup_guard.status", return_value={"status": "ready", "project": str(project)}).start()
    patch("qmd_route.project_paths", return_value={"selected": True,
           "INDEX_PATH": str(index_path)}).start()
    consents = root / "consents"; consents.mkdir(mode=0o700)
    save(consents / (digest(str(project)) + ".json"),
         {"schema": "qmd-shared-consent-v1", "projectRoot": str(project),
          "sharedRoot": str(root), "capture": True, "localTraining": True,
          "externalTeacher": False})
    control = {"schema": "qmd-synthetic-experiment-v1", "id": EXPERIMENT,
               "projectRoot": str(project), "sharedRoot": str(root),
               "maxTotalCalls": TOTAL_LIMIT, "maxGoldCalls": GOLD_LIMIT,
               "maxTrainCycles": TRAIN_LIMIT, "maxDiskBytes": DISK_LIMIT,
               "maxLogBytes": LOG_LIMIT, "finalCaseIds": FINAL_IDS}
    control_path = root / CONTROL_NAME; save(control_path, control)
    nonce = secrets.token_hex(24)
    marker_path = story / ".qmd-synthetic-project.json"
    save(marker_path, {"schema": "qmd-synthetic-project-v1",
        "projectRoot": str(project), "createdUtc": "2026-10-08T00:00:00Z",
        "nonce": nonce, "origin": "newly-generated-synthetic-only",
        "allowedReceiver": "codex-cli", "allowedPurpose": "selection-gold",
        "maxGoldCalls": GOLD_LIMIT, "model": "gpt-6.1-sol"})
    spec_path = story / "synthetic-generator.json"
    save(spec_path, {"schema": "qmd-synthetic-generator-v1",
        "projectRoot": str(project), "nonce": nonce, "seed": secrets.token_hex(32),
        "generatorSha256": source_sha256(), "model": "gpt-6-luna"})
    previous_generation = story / ".auto-context/qmd-index-generations/old"
    previous_generation.mkdir(parents=True)
    with (previous_generation / "inactive.gguf").open("wb") as old_model:
        old_model.truncate(DISK_LIMIT + 1)
    assert disk_bytes(root) < DISK_LIMIT
    teacher = {"schema": "qmd-synthetic-teacher-v1", "enabled": True,
        "projectRoot": str(project), "markerSha256": sha(marker_path),
        "provenanceSha256": sha(spec_path), "receiver": "codex-cli",
        "purpose": "selection-gold", "model": "gpt-6.1-sol",
        "maxCalls": GOLD_LIMIT, "maxInputBytes": 131072,
        "maxOutputBytes": 32768, "reservedUsd": 40,
        "reserveUsdPerCall": .1}
    save(root / "synthetic-teacher-policy.json", teacher)
    base = root / "base.artifact"; base.write_text("base"); base.chmod(0o600)
    worker_policy = {"schema": "qmd-shared-learning-v1", "enabled": True,
        "goldModel": "gpt-6.1-sol", "maxGoldCalls": GOLD_LIMIT,
        "batchMin": 5, "batchMax": 20, "targetExact": .95,
        "trainerArgv": [sys.executable,
            str(Path("core/context_learning/laya_adapter.py").resolve()), str(project)],
        "baseArtifact": str(base), "maxRssMib": 4096,
        "minAvailableMib": 128, "maxLoadPerCpu": 4,
        "safetySeconds": 900, "experimental": True}
    save(root / "shared-policy.json", worker_policy)
    grant = {"schema": "qmd-synthetic-parent-approval-v2", "enabled": True,
        "projectRoot": str(project), "sharedRoot": str(root),
        "generatorSpecSha256": sha(spec_path), "markerSha256": sha(marker_path),
        "launcherSha256": sha(Path(launcher_module.__file__)),
        "sharedPolicySha256": sha(root / "shared-policy.json"),
        "teacherPolicySha256": sha(root / "synthetic-teacher-policy.json"),
        "receiver": "codex-cli", "purpose": "selection-gold",
        "model": "gpt-6.1-sol", "writingModel": "gpt-6-luna",
        "experimentId": EXPERIMENT, "controlSha256": sha(control_path),
        "approvedThroughEpisode": 100, "approvedTotalCalls": TOTAL_LIMIT,
        "approvedGoldCalls": GOLD_LIMIT, "approvedTrainCycles": TRAIN_LIMIT}
    save(scope.APPROVAL_FILE, grant)
    # Only the exact synthetic experiment accepts explicit null count limits.
    # The legacy bounded fixture below still exercises its original limits.
    policy_files = (control_path, marker_path, root / "synthetic-teacher-policy.json",
                    root / "shared-policy.json", scope.APPROVAL_FILE)
    old_policy_bytes = {path: path.read_bytes() for path in policy_files}
    save(control_path, {**control, "maxTotalCalls": None,
                        "maxGoldCalls": None, "maxTrainCycles": None})
    denied(lambda: experiment_module.approval(root, project, *experiment_module.load(root, project)),
           "parent_experiment_approval_mismatch")
    save(marker_path, {**json.loads(marker_path.read_text()), "maxGoldCalls": None})
    save(root / "synthetic-teacher-policy.json",
         {**teacher, "markerSha256": sha(marker_path),
          "maxCalls": None, "reservedUsd": None,
          "reserveUsdPerCall": None})
    save(root / "shared-policy.json", {**worker_policy, "maxGoldCalls": None})
    uncapped_grant = {**grant, "controlSha256": sha(control_path),
                      "markerSha256": sha(marker_path),
                      "teacherPolicySha256": sha(root / "synthetic-teacher-policy.json"),
                      "sharedPolicySha256": sha(root / "shared-policy.json"),
                      "approvedTotalCalls": None, "approvedGoldCalls": None,
                      "approvedTrainCycles": None}
    save(scope.APPROVAL_FILE, uncapped_grant)
    loaded = experiment_module.load(root, project)
    assert experiment_module.approval(root, project, *loaded) == uncapped_grant
    assert scope.policy_for(root, project)["maxCalls"] is None
    with patch.object(scope, "_generated_collections_only",
                      side_effect=ValueError("partial_generated_files")):
        denied(lambda: scope.policy_for(root, project), "partial_generated_files")
        assert scope.policy_for(root, project,
                                for_continuation_repair=True)["maxCalls"] is None
    save(root / "synthetic-teacher-policy.json",
         {**json.loads((root / "synthetic-teacher-policy.json").read_text()),
          "enabled": False})
    denied(lambda: scope.policy_for(root, project,
                                    for_continuation_repair=True),
           "invalid_synthetic_policy")
    save(root / "synthetic-teacher-policy.json",
         {**teacher, "markerSha256": sha(marker_path),
          "maxCalls": None, "reservedUsd": None,
          "reserveUsdPerCall": None})
    assert _policy(root)["maxGoldCalls"] is None
    assert snapshot(root)["limits"]["total_calls"] is None
    with _db(root) as db:
        assert experiment_module._limits(db, uncapped_grant) == (5, None, None, None)
    with patch.object(experiment_module, "_counts", return_value={"writing": 800, "gold": 400}):
        assert experiment_module.reserve_manual(root, project, "planning", 1)["status"] == "reserved"
        with _db(root) as db, patch.object(experiment_module, "mapped_episode", return_value=(1, 1)):
            experiment_module.reserve_gold(db, root, project, *loaded,
                                           scope.policy_for(root, project),
                                           "synthetic-count-check", "first_running")
    with _db(root) as db:
        db.execute("DELETE FROM experiment_calls")
        db.executemany("INSERT INTO experiment_cycles VALUES (?,?)",
                       [(f"old-{n}", 1.0) for n in range(15)])
        experiment_module.reserve_train(db, root, project, "uncapped-next")
        assert db.execute("SELECT COUNT(*) FROM experiment_cycles").fetchone()[0] == 16
        db.execute("DELETE FROM experiment_cycles")
    assert process_pending(root, project, {**worker_policy, "maxGoldCalls": None},
                           invoke=lambda *_: None)["status"] == "drained"
    for path, old in old_policy_bytes.items():
        path.write_bytes(old)
    assert scope.policy_for(root, project)["maxCalls"] == GOLD_LIMIT
    edit_events = []
    launcher_module._dispatch_edit_hook = lambda _project, relative, *, deleted=False: edit_events.append((relative, deleted))
    launcher_module._indexed_revision = lambda *_: True
    # This legacy budget fixture pre-seeds verified fictional plans without
    # counting planning calls. The separate planning fixture covers their real
    # reservation, order and edit-hook path.
    patch.object(launcher_module, "_planning_ready", return_value=None).start()
    for slot, relative in enumerate(("01_Settings/world.md",
                                     "02_Characters/cast.md",
                                     "03_Plot/arc.md"), start=1):
        path = story / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"가상의 계획 단계 {slot}를 기록한다. " * 12 + "\n")
        with _db(root) as db:
            db.execute("INSERT INTO experiment_generated (kind,episode,slot,relative_path,sha256,prior_sha256,recorded_at) VALUES (?,?,?,?,?,?,?)",
                       ("planning", 1, slot, relative, sha(path), None, 1.0))
    launcher_module._plugin_overrides = lambda: ['plugins."old-local".enabled=false']
    fake_calls = []
    def fake_codex(prompt, cwd, kind, *, check):
        check()
        episode = int(re.search(r"episode (\d{3})", prompt).group(1))
        fake_calls.append((kind, episode))
        if kind == "writing":
            return canonical({"prose": f"Invented episode {episode:03d}. " + "마법" * 2500,
                              "setting": f"# Setting {episode:03d}\n" + "가상의 세계 규칙을 기록한다. " * 12,
                              "characters": f"# Characters {episode:03d}\n" + "가상의 인물 관계를 기록한다. " * 12,
                              "plot": f"# Plot {episode:03d}\n" + "가상의 줄거리 흐름을 기록한다. " * 12,
                              "temporary": "# Omen\nA temporary fictional omen.\n" if episode == 1 else None})
        if kind in ("wiki_create", "wiki_review"):
            return canonical({label: f"Episode {episode:03d} {label} fact is unique {episode:03d}-{label}."
                    for label in ("main", "final")})
        label = "main" if "main fact" in prompt else "final"
        hit = {"file": f"{WIKI_COLLECTION}/ep{episode:03d}-{label}.md", "score": 1.0}
        phase = {"name": "primary", "results": [hit],
                 "wiki_scoped": False, "returned_count": 1}
        selected = [hit]
        if episode in (3, 5) and label == "main":
            # A real retrieval miss: only an older generated card was returned.
            old = {"file": f"{WIKI_COLLECTION}/ep001-main.md", "score": .9}
            phase = {**phase, "results": [old], "returned_count": 1}
            selected = [old]
        os.environ["QMD_ENGINE"] = "codex"
        if episode == 6 and label == "main":
            rogue = wiki / "ep006-rogue.md"
            rogue.write_text("---\ntitle: Untracked\n---\nUntracked fictional card.\n")
            extra = {"file": f"{WIKI_COLLECTION}/ep006-rogue.md", "score": .5}
            forged = {**phase, "results": [hit, extra], "returned_count": 2}
            with patch("context_learning.shared.launch", return_value={"status": "mocked"}):
                assert observe({"prompt": prompt}, config, project, [hit],
                    phases=[forged], verdict=lambda *_: "eligible") == "stored"
            local_status = private / digest(str(project)) / "shared-last-status.json"
            assert json.loads(local_status.read_text())["status"] == "synthetic_source_not_generated"
            rogue.unlink()
        with patch("context_learning.shared.launch", return_value={"status": "mocked"}):
            observed = observe({"prompt": prompt}, config, project, selected,
                phases=[phase], verdict=lambda *_: "eligible")
            assert observed == "stored", observed
        if episode == 1 and label == "main":
            local_status = private / digest(str(project)) / "shared-last-status.json"
            assert json.loads(local_status.read_text())["status"] == "queued", local_status.read_text()
            with patch("context_learning.synthetic_teacher.resource_report",
                       return_value={"ready": True}):
                early = process_pending(root, project, worker_policy,
                                        invoke=lambda *_: (_ for _ in ()).throw(
                                            AssertionError("gold before query completion")))
            assert early["status"] == "deferred_query_launch", early
        return "Synthetic query complete."
    def episode(number, slots=(1, 2)):
        for kind in ("writing", "wiki_create", "wiki_review"):
            result = launch_one(root, project, kind, number, invoke=fake_codex)
            assert result["status"] in ("complete", "already_reserved")
            if result["status"] == "already_reserved":
                with _db(root) as db:
                    assert db.execute("SELECT state FROM experiment_invocations WHERE call_key=?",
                                      (result["call_key"],)).fetchone()[0] == "complete"
        if number == 1:
            assert edit_events[:2] == [
                ("01_Settings/temporary-omen.md", False),
                ("04_Manuscript/ep001.md", False),
            ], edit_events[:2]
        if number == 1:
            import recall
            main_meta = recall.read_wiki_meta(
                {"file": f"{WIKI_COLLECTION}/ep001-main.md"}, config, str(project), 600)
            final_meta = recall.read_wiki_meta(
                {"file": f"{WIKI_COLLECTION}/ep001-final.md"}, config, str(project), 600)
            assert main_meta["trusted"] and main_meta["bodyHashState"] == "match", main_meta
            assert not final_meta["trusted"] and final_meta["status"] == "generated"
            with patch.object(launcher_module, "_indexed_revision", return_value=False):
                assert launch_one(root, project, "query", 1, 1,
                                  invoke=fake_codex)["status"] == "deferred_index"
            with _db(root) as db:
                assert db.execute("SELECT COUNT(*) FROM experiment_calls WHERE kind='query'").fetchone()[0] == 0
        for slot in slots:
            if number == 1 and slot == 2:
                # Simulate a crash after verified bytes reached disk, before
                # the generated-file ledger transaction was committed.
                with _db(root) as db:
                    reviewed, source_sha, materialized = db.execute(
                        "SELECT reviewed,source_sha256,materialized_sha256 "
                        "FROM experiment_pending_final WHERE episode=1").fetchone()
                assert reviewed and materialized is None
                final_path = wiki / "ep001-final.md"
                final_path.write_text(launcher_module._fact_card(
                    story, 1, "final", reviewed, source_sha, verified=True))
                with sqlite3.connect(index_path) as index:
                    index.execute("INSERT INTO documents VALUES (?,?,?,1)",
                                  (WIKI_COLLECTION, "ep001-final.md", sha(final_path)))
                denied(lambda: scope.policy_for(root, project),
                       "synthetic_collection_unproven")
                assert scope.policy_for(root, project, recovery_episode=1)
            with patch("context_learning.shared.launch", return_value={"status": "mocked"}):
                result = launch_one(root, project, "query", number, slot,
                                    invoke=fake_codex)
            assert result["status"] in ("complete", "already_reserved")
            if number == 1 and slot == 2:
                assert scope.policy_for(root, project)
                with _db(root) as db:
                    assert db.execute("SELECT materialized_sha256 FROM experiment_pending_final "
                                      "WHERE episode=1").fetchone()[0] == sha(final_path)
            if result["status"] == "already_reserved":
                with _db(root) as db:
                    assert db.execute("SELECT state FROM experiment_invocations WHERE call_key=?",
                                      (result["call_key"],)).fetchone()[0] == "complete"
        if number == 1 and 2 in slots:
            import recall
            final_meta = recall.read_wiki_meta(
                {"file": f"{WIKI_COLLECTION}/ep001-final.md"}, config, str(project), 600)
            assert final_meta["trusted"] and final_meta["bodyHashState"] == "match"
    with sqlite3.connect(index_path) as index:
        index.execute("INSERT INTO documents VALUES (?,?,?,1)",
                      (scope.PROJECT_WIKI_COLLECTION, "private-story.md", "f" * 64))
    denied(lambda: scope.policy_for(root, project),
           "synthetic_index_contains_foreign_document")
    with sqlite3.connect(index_path) as index:
        index.execute("DELETE FROM documents")
        index.execute("INSERT INTO documents VALUES (?,?,?,1)",
                      (WIKI_COLLECTION, "unproven.md", "f" * 64))
    denied(lambda: scope.policy_for(root, project),
           "synthetic_index_contains_unproven_document")
    with sqlite3.connect(index_path) as index:
        index.execute("DELETE FROM documents")
    denied(lambda: launch_one(root, project, "writing", 6, invoke=fake_codex),
           "experiment_batch_not_approved")
    for number in range(1, 6):
        episode(number)
    # The old hook stored q5 locally but its then-strict shared binder left
    # the completed CLI invocation uncertain. Rebind that same UUID and pool
    # using a receipt; no second mock provider invocation is permitted.
    with _db(root) as db:
        q5 = db.execute("SELECT t.request_id,t.prompt_sha256 FROM experiment_query_tickets t "
                        "WHERE t.episode=5 AND t.slot=1").fetchone()
        db.execute("DELETE FROM cases WHERE request_id=?", (q5[0],))
        db.execute("UPDATE experiment_query_tickets SET request_id=NULL,input_sha256=NULL,"
                   "candidate_sha256=NULL WHERE episode=5 AND slot=1")
        db.execute("UPDATE experiment_invocations SET state='uncertain',output_sha256=NULL,"
                   "detail='query_capture_missing' WHERE call_key='query:005:1'")
    save(private / digest(str(project)) / "shared-last-status.json",
         {"request_id_hash": hashlib.sha256(q5[0].encode()).hexdigest(),
          "status": "experiment_target_source_missing"})
    evidence = root / "attempt-evidence"; evidence.mkdir(mode=0o700, exist_ok=True)
    receipt = evidence / "query_005_1.json"
    save(receipt, {"callKey": "query:005:1", "inputSha256": q5[1],
                   "turnCompleted": True, "turnFailed": False,
                   "finalMessagePresent": True, "exitCode": 0})
    before_recovery_calls = list(fake_calls)
    with patch.object(experiment_module, "Q5_CAPTURE_RECEIPT_SHA256", sha(receipt)), \
            patch.object(experiment_module, "Q5_CAPTURE_REQUEST_ID", q5[0]):
        checked = experiment_module.recover_q5_capture(root, project,
                                                        validate_only=True)
        assert checked["status"] == "ready_for_receipt_recovery"
        assert checked["expected_target_in_pool"] is False
        assert experiment_module.recover_q5_capture(root, project)["status"] == "recovered"
        assert experiment_module.recover_q5_capture(root, project)["status"] == "already_recovered"
        with _db(root) as db:
            db.execute("UPDATE experiment_query_tickets SET request_id=? "
                       "WHERE call_key='query:005:1'", ("wrong-capture-uuid",))
        denied(lambda: experiment_module.recover_q5_capture(root, project),
               "experiment_recovery_request_changed")
        with _db(root) as db:
            db.execute("UPDATE experiment_query_tickets SET request_id=? "
                       "WHERE call_key='query:005:1'", (q5[0],))
            db.execute("UPDATE experiment_invocations SET state='running' "
                       "WHERE call_key='query:005:1'")
        denied(lambda: experiment_module.recover_q5_capture(root, project),
               "experiment_recovery_case_invalid")
        with _db(root) as db:
            db.execute("UPDATE experiment_invocations SET state='uncertain',"
                       "detail='query_capture_missing' WHERE call_key='query:005:1'")
        original_bound = launcher_module._bound_query
        def raced_binding(db, *args):
            original_bound(db, *args)
            db.execute("UPDATE experiment_invocations SET state='running' "
                       "WHERE call_key='query:005:1'")
        with patch.object(launcher_module, "_bound_query", side_effect=raced_binding):
            denied(lambda: experiment_module.recover_q5_capture(root, project),
                   "experiment_recovery_state_changed")
        with _db(root) as db:
            assert db.execute("SELECT state FROM experiment_invocations "
                              "WHERE call_key='query:005:1'").fetchone() == ("uncertain",)
            db.execute("UPDATE experiment_invocations SET state='complete' "
                       "WHERE call_key='query:005:1'")
    assert fake_calls == before_recovery_calls
    foreign_card = wiki / "foreign-verified.md"
    foreign_card.write_text("---\nstatus: verified\ncreatedBy: qmd-auto-context\n---\nInvented foreign card.\n")
    denied(lambda: scope.policy_for(root, project), "synthetic_collection_unproven")
    foreign_card.unlink()
    foreign_source = project / "invented-outside.txt"
    foreign_source.write_text("Invented outside text.\n")
    foreign_card.symlink_to(foreign_source)
    denied(lambda: scope.policy_for(root, project), "synthetic_collection_linked")
    foreign_card.unlink()
    assert (story / "01_Settings/world.md").is_file()
    assert (story / "02_Characters/cast.md").is_file()
    assert (story / "03_Plot/arc.md").is_file()
    assert (story / "04_Manuscript/ep001.md").is_file()
    assert not (story / "01_Settings/temporary-omen.md").exists()
    assert ("01_Settings/temporary-omen.md", False) in edit_events
    assert ("01_Settings/temporary-omen.md", True) in edit_events
    assert len(snapshot(root)["episode_lengths"]) == 5
    assert all(row["hangul_syllables"] == 5000
               for row in snapshot(root)["episode_lengths"])
    with _db(root) as db:
        rows = db.execute("SELECT request_id,sample FROM cases ORDER BY captured_at").fetchall()
        assert len(rows) == 10
        assert all(re.fullmatch(r"[0-9a-f-]{36}", rid) for rid, _ in rows)
        assert len({rid for rid, _ in rows}) == 10
        assert db.execute("SELECT COUNT(*) FROM experiment_query_tickets WHERE request_id IS NOT NULL").fetchone()[0] == 10
        missed = db.execute("SELECT c.sample,c.split FROM cases c JOIN experiment_query_tickets t USING(request_id) WHERE t.episode=3 AND t.slot=1").fetchone()
        missed_sample = json.loads(missed[0])
        assert missed_sample["sampling"]["baseline_selected_ids"] == [f"{WIKI_COLLECTION}/ep001-main.md"]
        assert f"{WIKI_COLLECTION}/ep003-main.md" not in [
            card["candidate_id"] for card in missed_sample["candidates"]]
        assert snapshot(root)["retrieval"]["expected_target_missing"] == 2
    # A normal capture without a launcher ticket stays local and cannot enter
    # the experiment queue, even if it retrieves a generated source.
    unrelated = {"file": f"{WIKI_COLLECTION}/ep001-main.md", "score": 1.0}
    phase = {"name": "primary", "results": [unrelated],
             "wiki_scoped": False, "returned_count": 1}
    with patch("context_learning.shared.launch", return_value={"status": "mocked"}):
        assert observe({"prompt": "Unreserved synthetic question about episode 001"},
            config, project, [unrelated], phases=[phase],
            verdict=lambda *_: "eligible") == "stored"
    local = private / digest(str(project)) / "shared-last-status.json"
    assert json.loads(local.read_text())["status"] == "experiment_query_ticket_missing_or_ambiguous"
    with _db(root) as db:
        assert db.execute("SELECT COUNT(*) FROM cases").fetchone()[0] == 10
    assert launch_one(root, project, "query", 1, 1,
                      invoke=fake_codex)["status"] == "already_reserved"
    assert len(fake_calls) == 25
    def fake_gold(sample, *_):
        missed_target = (all(target not in [
            card["candidate_id"].split("/")[-1] for card in sample["candidates"]]
            for target in ("ep003-main.md", "ep005-main.md"))
            and any(f"episode {number:03d}" in sample["prompt"]["text"]
                    for number in (3, 5)))
        return {"selected_ids": [] if missed_target else
                [sample["candidates"][0]["candidate_id"]],
                "usage": {}, "elapsed_seconds": .1, "peak_rss_mib": 1}
    with patch("context_learning.synthetic_teacher.resource_report",
               return_value={"ready": True}):
        result = process_pending(root, project, worker_policy, invoke=fake_gold)
    assert result["status"] == "drained", result
    assert snapshot(root)["calls"]["gold"] == 20
    with _db(root) as db:
        assert db.execute(
            "SELECT stage,transport,COUNT(*) FROM gold_attempt_receipts "
            "GROUP BY stage,transport").fetchall() == [("committed", "injected", 20)]
        assert db.execute("SELECT gold FROM cases c JOIN experiment_query_tickets t "
                          "USING(request_id) WHERE t.episode=3 AND t.slot=1").fetchone()[0]
        assert json.loads(db.execute("SELECT gold FROM cases c JOIN experiment_query_tickets t "
                                     "USING(request_id) WHERE t.episode=3 AND t.slot=1").fetchone()[0])["selected_ids"] == []
        assert all(len(set(split for (split,) in db.execute(
            "SELECT c.split FROM cases c JOIN experiment_query_tickets t "
            "USING(request_id) WHERE t.episode=?", (episode,)))) == 1
                   for episode in range(1, 6))
    class FakeTrainer:
        def call(self, mode, payload, result_path, *, cwd):
            if mode == "train":
                Path(payload["artifact_path"]).write_text("candidate")
                Path(payload["artifact_path"]).chmod(0o600)
                result = {"schema_version": 1, "status": "trained"}
            else:
                result = {"schema_version": 1, "predictions": [
                    {"request_id": case["request_id"], "input_sha256": case["input_sha256"],
                     "selected_ids": [case["candidates"][0]["id"]]}
                    for case in payload["cases"]]}
                if (os.environ.get("QMD_FINAL_MUTATION") == "1"
                        and Path(cwd).name == "final-evaluation"):
                    with _db(root) as db:
                        first = db.execute("SELECT project_key,request_id,gold FROM cases WHERE split='final' AND status='gold' ORDER BY request_id LIMIT 1").fetchone()
                        changed_gold = dict(json.loads(first[2]), selected_ids=[])
                        db.execute("UPDATE cases SET gold=? WHERE project_key=? AND request_id=?",
                                   (canonical(changed_gold), first[0], first[1]))
            save(Path(result_path), result)
            return result, {"fixture": True}
    with patch("context_learning.shared.resource_report", return_value={"ready": True}):
        first = train_due(root, worker_policy,
                          trainer_factory=lambda _: FakeTrainer())
    assert first["status"] == "complete", first
    assert snapshot(root)["approved_episode"] == 5
    # One approval pins the generator and both runtime policies for all 100.
    policy_path = root / "shared-policy.json"
    original_policy = policy_path.read_bytes()
    policy_path.write_bytes(original_policy + b" ")
    denied(lambda: launch_one(root, project, "writing", 6, invoke=fake_codex),
           "parent_experiment_approval_mismatch")
    policy_path.write_bytes(original_policy)
    project_config = project / ".auto-context/settings.json"
    original_config = project_config.read_bytes()
    save(project_config, {**config, "collectionPaths": {
        **paths, WIKI_COLLECTION: "../private-novel"}})
    denied(lambda: launch_one(root, project, "writing", 6, invoke=fake_codex),
           "synthetic_project_config_mismatch")
    project_config.write_bytes(original_config)
    for unsafe in ({"allowRoots": [str(project)]}, {"injectSourcePathsPerCard": 3}):
        save(project_config, {**config, **unsafe})
        denied(lambda: launch_one(root, project, "writing", 6, invoke=fake_codex),
               "synthetic_project_config_mismatch")
        project_config.write_bytes(original_config)
    # Pilot evidence unlocks the rest without rewriting the owner approval.
    episode(6, (1,))
    assert snapshot(root)["approved_episode"] == 100
    # A model response without the actual hook capture is a billed uncertain
    # attempt. Only an explicit, newly reserved retry may recover the slot.
    with patch("context_learning.shared.launch", return_value={"status": "mocked"}):
        denied(lambda: launch_one(root, project, "query", 6, 2,
                                  invoke=lambda *_a, **_k: "No hook ran."),
               "query_capture_missing")
        assert launch_one(root, project, "query", 6, 2,
                          invoke=fake_codex)["status"] == "already_reserved"
        denied(lambda: launch_one(root, project, "query", 6, 2, recovery=2,
                                  invoke=fake_codex), "experiment_recovery_not_eligible")
        assert launch_one(root, project, "query", 6, 2, recovery=1,
                          invoke=fake_codex)["status"] == "complete"
        assert launch_one(root, project, "query", 6, 2, recovery=1,
                          invoke=fake_codex)["status"] == "already_reserved"
    with _db(root) as db:
        prior = db.execute("SELECT i.state,t.request_id,i.detail FROM experiment_invocations i JOIN experiment_query_tickets t USING(call_key) WHERE i.call_key='query:006:2'").fetchone()
        recovered = db.execute("SELECT i.state,t.request_id FROM experiment_invocations i JOIN experiment_query_tickets t USING(call_key) WHERE i.call_key='query:006:2:retry1'").fetchone()
        assert prior == ("uncertain", None, "query_capture_missing"), prior
        assert recovered[0] == "complete" and re.fullmatch(r"[0-9a-f-]{36}", recovered[1])
    denied(lambda: launch_one(root, project, "query", 2, 1, recovery=1,
                              invoke=fake_codex), "experiment_recovery_not_eligible")
    for number in range(7, 16):
        episode(number, (1,))
    with patch("context_learning.synthetic_teacher.resource_report",
               return_value={"ready": True}):
        assert process_pending(root, project, worker_policy,
                               invoke=fake_gold)["status"] == "drained"
    with patch("context_learning.shared.resource_report", return_value={"ready": True}):
        later = train_due(root, worker_policy,
                          trainer_factory=lambda _: FakeTrainer())
    assert later["status"] == "complete", later
    assert snapshot(root)["train_reservations"] == 2
    # Exercise the actual pre-reserved Popen path with a fake provider process.
    spawned = []
    class FakeProcess:
        def __init__(self, args, *, cwd, env, stdin, stdout, **_kwargs):
            spawned.append((args, cwd, stdin.read().decode()))
            self.pid = 123456
            self.returncode = 0
            stdout.write((canonical({"type": "item.completed", "item": {
                "type": "agent_message", "text": canonical({
                    "prose": "Invented episode 016. " + "마법" * 2500,
                    "setting": "# Setting\n" + "가상의 세계 규칙을 기록한다. " * 12,
                    "characters": "# Cast\n" + "가상의 인물 관계를 기록한다. " * 12,
                    "plot": "# Arc\n" + "가상의 줄거리 흐름을 기록한다. " * 12,
                    "temporary": None})}}) + "\n").encode())
            stdout.write((canonical({"type": "turn.completed", "usage": {
                "input_tokens": 10, "output_tokens": 8}}) + "\n").encode())
            stdout.flush()
        def poll(self):
            return 0
    with patch("context_learning.synthetic_launcher.subprocess.Popen", FakeProcess), patch(
            "context_learning.synthetic_launcher._mcp_overrides", return_value=[]), patch(
            "context_learning.shared.resource_report",
            return_value={"ready": True, "memory_pressure": None,
                          "swap_used_mib": 0, "gpu_usage_percent": None}):
        assert launch_one(root, project, "writing", 16)["status"] == "complete"
        assert launch_one(root, project, "writing", 16)["status"] == "already_reserved"
    assert len(spawned) == 1 and spawned[0][1] == story
    assert "--model" in spawned[0][0] and "gpt-6-luna" in spawned[0][0]
    assert 'plugins."old-local".enabled=false' in spawned[0][0]
    # An interrupted subprocess consumes its reservation and cannot retry.
    def interrupted(*_args, **_kwargs):
        raise ValueError("mock_provider_interrupted")
    denied(lambda: launch_one(root, project, "query", 7, 2,
                              invoke=interrupted), "mock_provider_interrupted")
    assert launch_one(root, project, "query", 7, 2,
                      invoke=fake_codex)["status"] == "already_reserved"
    with _db(root) as db:
        assert db.execute("SELECT state FROM experiment_invocations WHERE call_key='query:007:2'").fetchone()[0] == "uncertain"
    # Complete the remaining invented episodes in bounded local cycles. The
    # final q2 cases accumulate while later q1 cycles still train.
    for start, stop in ((16, 26), (26, 36), (36, 46), (46, 56),
                        (56, 66), (66, 76), (76, 86), (86, 101)):
        for number in range(start, stop):
            episode(number, (1, 2) if number >= 81 else (1,))
        with patch("context_learning.synthetic_teacher.resource_report",
                   return_value={"ready": True}):
            assert process_pending(root, project, worker_policy,
                                   invoke=fake_gold)["status"] == "drained"
        with patch("context_learning.shared.resource_report", return_value={"ready": True}):
            cycle = train_due(root, worker_policy,
                              trainer_factory=lambda _: FakeTrainer())
        assert cycle["status"] == "complete", cycle
    assert snapshot(root)["final_gold"] == 20
    # A final question can select a new fact while carrying a trained card
    # as a negative. Reject that pool before reserving another training cycle.
    with _db(root) as db:
        trained_sample, trained_bodies = db.execute(
            "SELECT sample,bodies FROM cases WHERE split='train' AND status='gold' "
            "ORDER BY captured_at LIMIT 1").fetchone()
        final_request, original_sample, original_bodies = db.execute(
            "SELECT request_id,sample,bodies FROM cases WHERE split='final' "
            "AND status='gold' ORDER BY captured_at LIMIT 1").fetchone()
        old_candidate = json.loads(trained_sample)["candidates"][0]
        changed_sample = json.loads(original_sample)
        changed_sample["candidates"].append(old_candidate)
        changed_bodies = json.loads(original_bodies)
        changed_bodies[old_candidate["candidate_id"]] = json.loads(trained_bodies)[
            old_candidate["candidate_id"]]
        db.execute("UPDATE cases SET sample=?,bodies=? WHERE request_id=?",
                   (canonical(changed_sample), canonical(changed_bodies), final_request))
        experiment_module.reserve_train(db, root, project,
                                        "negative-final-candidate-proof")
        db.execute("UPDATE cases SET sample=?,bodies=? WHERE request_id=?",
                   (original_sample, original_bodies, final_request))
    with _db(root) as db:
        row = db.execute("SELECT split,status FROM cases c JOIN experiment_query_tickets t USING(request_id) WHERE t.episode=81 AND t.slot=2").fetchone()
        assert row == ("final", "gold"), row
    with patch("context_learning.shared.resource_report",
               return_value={"ready": True}):
        if os.environ.get("QMD_FINAL_MUTATION") == "1":
            denied(lambda: evaluate_final(root, trainer_factory=lambda _: FakeTrainer()),
                   "experiment_final_changed_during_evaluation")
            assert snapshot(root)["final_evaluation"] == "uncertain"
            with _db(root) as db:
                assert db.execute("SELECT result FROM experiment_final WHERE id=1").fetchone() == (None,)
        else:
            final = evaluate_final(root, trainer_factory=lambda _: FakeTrainer())
            assert final["status"] == "complete" and final["questions"] == 20, final
            assert evaluate_final(root, trainer_factory=lambda _: (_ for _ in ()).throw(
                AssertionError("final evaluation repeated"))) == final
            with _db(root) as db:
                first = db.execute("SELECT project_key,request_id,gold FROM cases WHERE split='final' AND status='gold' ORDER BY request_id LIMIT 1").fetchone()
                db.execute("UPDATE cases SET gold=? WHERE project_key=? AND request_id=?",
                           (canonical(dict(json.loads(first[2]), selected_ids=[])), first[0], first[1]))
            assert snapshot(root)["final_evaluation"] == "invalidated"
            assert evaluate_final(root)["status"] == "final_result_invalidated"
    denied(lambda: launch_one(root, project, "writing", 16,
                              invoke=fake_codex), "experiment_final_sealed")
    print(json.dumps({"pilot_budget": True, "batch_restart": True,
                      "final_sealed": True, "real_hook_uuid": True,
                      "late_cycle": True, "final_gold_mutation": True,
                      "external_calls": 0}))

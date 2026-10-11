"""Counted synthetic retry after an uncertain call; no provider or story data."""
import json
import hashlib
import io
import sqlite3
from pathlib import Path
import tempfile
from contextlib import redirect_stdout
from unittest.mock import patch

from context_learning import codex_adapter
from context_learning import synthetic_experiment as experiment
from context_learning import synthetic_generator
from context_learning import synthetic_launcher as launcher
from context_learning import synthetic_scope as scope
from context_learning.synthetic_scope import WIKI_COLLECTION
from context_learning.shared import _db
import context_learning_synthetic_experiment as entry


def refused(fn, reason):
    try:
        fn()
    except ValueError as exc:
        assert str(exc) == reason, (str(exc), reason)
    else:
        raise AssertionError("unexpectedly accepted")


assert launcher._writing_prompt("pilot", 6, 0) == "pilot"
assert launcher._writing_prompt("pilot", 6, 1) == "pilot"
assert launcher._writing_prompt("pilot", 6, 2) != "pilot"
assert launcher._writing_prompt("future", 7, 0).startswith("future")
assert launcher._writing_prompt("twelve", 12, 0) != "twelve"
assert launcher._writing_prompt("twelve", 12, 1) != launcher._writing_prompt(
    "twelve", 12, 0)
with tempfile.TemporaryDirectory(prefix="qmd-short-draft-") as tmp:
    root = Path(tmp); root.chmod(0o700)
    (root / "attempt-evidence").mkdir(mode=0o700)
    draft = '{"prose":"synthetic draft"}'
    digest = launcher._retain_short_draft(root, "writing:006:1:retry2", draft)
    path = root / "attempt-evidence/writing_006_1_retry2.draft.json"
    assert path.read_text() == draft
    assert digest == hashlib.sha256(draft.encode()).hexdigest()
    assert not path.stat().st_mode & 0o077
    refused(lambda: launcher._retain_short_draft(
        root, "writing:006:1:retry2", "replacement"), "synthetic_draft_exists")
    malformed = '{"prose":"line one\nline two"}'
    raw_sha = launcher._retain_writing_response(
        root, "writing:012:1:retry1", malformed)
    raw = root / "attempt-evidence/writing_012_1_retry1.raw-response.txt"
    assert raw.read_text() == malformed
    assert raw_sha == hashlib.sha256(malformed.encode()).hexdigest()
    assert not raw.stat().st_mode & 0o077
    try:
        launcher._retain_writing_response(root, "writing:012:1:retry1", "replacement")
    except FileExistsError:
        pass
    else:
        raise AssertionError("raw response was overwritten")
with tempfile.TemporaryDirectory(prefix="qmd-continuation-draft-") as tmp:
    root = Path(tmp); root.chmod(0o700)
    folder = root / "attempt-evidence"; folder.mkdir(mode=0o700)
    draft = {"prose": "가" * 4782, "setting": "가상 배경", "characters": "가상 인물",
             "plot": "가상 사건", "temporary": None}
    path = folder / "writing_008_1.draft.json"
    path.write_text(json.dumps(draft)); path.chmod(0o600)
    with patch.object(launcher, "WRITING_008_DRAFT_SHA256",
                      hashlib.sha256(path.read_bytes()).hexdigest()):
        loaded = launcher._eighth_draft(root)
        assert loaded == draft
        assert "<synthetic-draft>" in launcher._continuation_prompt(loaded)
        combined = launcher._merge_eighth_continuation(
            root, json.dumps({"continuation": "나" * 500}))
        assert combined["prose"] == draft["prose"] + "\n\n" + "나" * 500
        assert combined["setting"] == draft["setting"]
        assert launcher._narrative_counts(combined["prose"])["without_whitespace"] == 5282
        refused(lambda: launcher._merge_eighth_continuation(
            root, '{"prose":"overwrite"}'), "synthetic_continuation_response_invalid")
        path.write_text(json.dumps({**draft, "prose": "다" * 4782}))
        refused(lambda: launcher._eighth_draft(root), "synthetic_continuation_draft_changed")


long_korean_plan = "가" * 10000  # 30,000 UTF-8 bytes; no prose-length rule.
assert launcher._planning_response_status(json.dumps({"content": long_korean_plan}))[0] == {
    "predicate": "ok", "contentBytes": 30000, "contentType": "str",
    "hasContentKey": True, "extraKeyCount": 0}
assert launcher._planning_response_status('{"content":"valid","extra":1}')[0]["predicate"] == "wrong_keys"
assert launcher._planning_response_status('{"other":"missing"}')[0]["predicate"] == "wrong_keys"
assert launcher._planning_response_status('{"content":null}')[0]["predicate"] == "content_not_string"
assert launcher._planning_response_status('{"content":"  "}')[0]["predicate"] == "content_blank"
assert launcher._planning_response_status('{"content":"{"}')[0]["predicate"] == "content_no_document"
assert launcher._planning_response_status('{"content":"가상의 세계."}')[0]["predicate"] == "ok"
with tempfile.TemporaryDirectory(prefix="qmd-malformed-planning-source-") as tmp:
    story = Path(tmp)
    world = story / "01_Settings/world.md"
    world.parent.mkdir()
    world.write_text("{")
    class RecordedSource:
        def execute(self, *_args):
            return self
        def fetchone(self):
            return (hashlib.sha256(b"{").hexdigest(),)
    refused(lambda: launcher._source(RecordedSource(), story, "01_Settings/world.md"),
            "synthetic_planning_content_invalid")
with tempfile.TemporaryDirectory(prefix="qmd-audited-world-repair-") as tmp:
    project = Path(tmp) / "novel"; project.mkdir()
    story = experiment.story_for(project); story.mkdir()
    root = experiment.shared_for(project); root.mkdir(mode=0o700)
    world = story / "01_Settings/world.md"; world.parent.mkdir()
    world.write_text("{")
    attempt = root / "attempt-evidence"; attempt.mkdir(mode=0o700)
    (attempt / "planning_001_1_retry3.response.txt").write_text('{"content":"{"}')
    (attempt / "planning_001_1_retry3.response.txt").chmod(0o600)
    with _db(root) as db:
        db.execute("INSERT INTO experiment_calls VALUES (?,?,?,?)",
                   (launcher.WORLD_REPAIR_CALL, "planning", 1, 1.0))
        db.execute("INSERT INTO experiment_invocations VALUES (?,?,?,?,?)",
                   (launcher.WORLD_REPAIR_CALL, "a" * 64, "complete",
                    launcher.WORLD_REPAIR_OUTPUT, "{}"))
        db.execute("INSERT INTO experiment_generated "
                   "(kind,episode,slot,relative_path,sha256,prior_sha256,recorded_at) "
                   "VALUES (?,?,?,?,?,?,?)",
                   ("planning", 1, 1, launcher.WORLD_REPAIR_RELATIVE,
                    launcher.WORLD_REPAIR_PRIOR, None, 1.0))
    repaired = "가상의 도서관에서는 잊힌 주문을 기록하고 사서는 그 기록을 지킨다."
    edits = []
    with (patch.object(launcher, "load", return_value=({"maxTotalCalls": None}, "control-sha")),
          patch.object(launcher, "policy_for", return_value={"provenanceSha256": "f" * 64}),
          patch.object(launcher, "generator_spec", return_value=({"seed": "a" * 64}, "f" * 64)),
          patch.object(launcher, "approval", return_value={}),
          patch.object(launcher, "check_disk"),
          patch.object(launcher, "_dispatch_edit_hook", side_effect=lambda _s, rel: edits.append(rel)),
          patch.object(experiment, "_limits", return_value=(5, None, None, None))):
        refused(lambda: launcher.launch_one(root, project, "planning", 1, 2,
                                            invoke=lambda *_a, **_k: "never"),
                "synthetic_planning_content_invalid")
        result = launcher.repair_planning_world(
            root, project, invoke=lambda _prompt, _story, _kind, *, check:
            (check(), json.dumps({"content": repaired}))[1])
        assert result["status"] == "complete"
        refused(lambda: launcher.repair_planning_world(root, project,
                                                        invoke=lambda *_a, **_k: "never"),
                "synthetic_repair_source_changed")
    assert world.read_text() == repaired
    assert (root / "repair-evidence/world-before-planning_repair_001_1.md").read_text() == "{"
    assert edits == ["01_Settings/world.md"]
    with _db(root) as db:
        history = db.execute("SELECT entry_id,sha256,prior_sha256 FROM experiment_generated "
                             "ORDER BY entry_id").fetchall()
        assert len(history) == 2 and history[0][1] == launcher.WORLD_REPAIR_PRIOR
        assert history[1][2] == history[0][1] and history[1][1] == result["new_sha256"]
        audit = db.execute("SELECT old_entry_id,state,new_entry_id,desired_sha256 "
                           "FROM experiment_repairs WHERE call_key=?",
                           (launcher.WORLD_REPAIR_KEY,)).fetchone()
        assert audit == (history[0][0], "complete", history[1][0], history[1][1])
        assert db.execute("SELECT state,output_sha256 FROM experiment_invocations "
                          "WHERE call_key=?", (launcher.WORLD_REPAIR_CALL,)).fetchone() == (
                              "complete", launcher.WORLD_REPAIR_OUTPUT)
        assert db.execute("SELECT COUNT(*) FROM experiment_calls WHERE kind='planning_repair'").fetchone() == (1,)
        db.execute("UPDATE experiment_repairs SET detail='hook_pending' WHERE call_key=?",
                   (launcher.WORLD_REPAIR_KEY,))
    with (patch.object(launcher, "load", return_value=({}, "control-sha")),
          patch.object(launcher, "approval", return_value={}),
          patch.object(launcher, "_dispatch_edit_hook", side_effect=lambda _s, rel: edits.append(rel))):
        assert launcher.reconcile_planning_world(root, project)["status"] == "complete"
    assert edits == ["01_Settings/world.md", "01_Settings/world.md"]
    with _db(root) as db:
        assert db.execute("SELECT detail FROM experiment_repairs WHERE call_key=?",
                          (launcher.WORLD_REPAIR_KEY,)).fetchone() == (None,)
        assert db.execute("SELECT COUNT(*) FROM experiment_generated").fetchone() == (2,)
with tempfile.TemporaryDirectory(prefix="qmd-world-repair-fails-closed-") as tmp:
    project = Path(tmp) / "novel"; project.mkdir()
    story = experiment.story_for(project); story.mkdir()
    root = experiment.shared_for(project); root.mkdir(mode=0o700)
    world = story / "01_Settings/world.md"; world.parent.mkdir()
    world.write_text("{")
    attempt = root / "attempt-evidence"; attempt.mkdir(mode=0o700)
    response_artifact = attempt / "planning_001_1_retry3.response.txt"
    response_artifact.write_text('{"content":"{"}'); response_artifact.chmod(0o600)
    with _db(root) as db:
        db.execute("INSERT INTO experiment_calls VALUES (?,?,?,?)",
                   (launcher.WORLD_REPAIR_CALL, "planning", 1, 1.0))
        db.execute("INSERT INTO experiment_invocations VALUES (?,?,?,?,?)",
                   (launcher.WORLD_REPAIR_CALL, "a" * 64, "complete",
                    launcher.WORLD_REPAIR_OUTPUT, "{}"))
        db.execute("INSERT INTO experiment_generated "
                   "(kind,episode,slot,relative_path,sha256,prior_sha256,recorded_at) "
                   "VALUES (?,?,?,?,?,?,?)",
                   ("planning", 1, 1, launcher.WORLD_REPAIR_RELATIVE,
                    launcher.WORLD_REPAIR_PRIOR, None, 1.0))
    with (patch.object(launcher, "load", return_value=({"maxTotalCalls": None}, "control-sha")),
          patch.object(launcher, "policy_for", return_value={"provenanceSha256": "f" * 64}),
          patch.object(launcher, "generator_spec", return_value=({"seed": "a" * 64}, "f" * 64)),
          patch.object(launcher, "approval", return_value={}),
          patch.object(launcher, "check_disk"),
          patch.object(experiment, "_limits", return_value=(5, None, None, None))):
        refused(lambda: launcher.repair_planning_world(
            root, project, invoke=lambda *_a, **_k: '{"content":"{"}'),
            "synthetic_planning_response_invalid")
        refused(lambda: launcher.repair_planning_world(
            root, project, invoke=lambda *_a, **_k: "never"),
            "synthetic_repair_already_reserved")
    assert world.read_text() == "{"
    assert (root / "repair-evidence/world-before-planning_repair_001_1.md").read_text() == "{"
    with _db(root) as db:
        assert db.execute("SELECT state FROM experiment_repairs WHERE call_key=?",
                          (launcher.WORLD_REPAIR_KEY,)).fetchone() == ("failed",)
        assert db.execute("SELECT COUNT(*) FROM experiment_generated").fetchone() == (1,)
with tempfile.TemporaryDirectory(prefix="qmd-world-repair-replay-") as tmp:
    project = Path(tmp) / "novel"; project.mkdir()
    story = experiment.story_for(project); story.mkdir()
    root = experiment.shared_for(project); root.mkdir(mode=0o700)
    world = story / "01_Settings/world.md"; world.parent.mkdir()
    world.write_text("{")
    body = "가상의 도서관에는 token=FICTIONAL-EXAMPLE 이라는 기록 규칙이 있다."
    response = json.dumps({"content": body}, ensure_ascii=False)
    backup = root / "repair-evidence"; backup.mkdir(mode=0o700)
    (backup / "world-before-planning_repair_001_1.md").write_text("{")
    stdout = root / "fake-events.jsonl"; stderr = root / "fake-stderr.txt"
    stdout.write_text("\n".join(json.dumps(event) for event in (
        {"type": "item.completed", "item": {"type": "agent_message", "text": response}},
        {"type": "turn.completed", "usage": {"input_tokens": 8, "output_tokens": 12}})) + "\n")
    stderr.write_text("")
    launcher._record_attempt(root, launcher.WORLD_REPAIR_KEY, "synthetic prompt",
                             stdout, stderr, 0)
    attempt = root / "attempt-evidence"
    receipt = attempt / "planning_repair_001_1.recovery.json"
    assert receipt.read_text() == response and receipt.stat().st_mode & 0o077 == 0
    diagnostic = (attempt / "planning_repair_001_1.response.txt").read_text()
    assert "FICTIONAL-EXAMPLE" not in diagnostic and "[REDACTED]" in diagnostic
    with _db(root) as db:
        db.execute("INSERT INTO experiment_calls VALUES (?,?,?,?)",
                   (launcher.WORLD_REPAIR_KEY, "planning_repair", 1, 1.0))
        db.execute("INSERT INTO experiment_invocations VALUES (?,?,?,NULL,NULL)",
                   (launcher.WORLD_REPAIR_KEY, "a" * 64, "running"))
        old_id = db.execute("INSERT INTO experiment_generated "
                            "(kind,episode,slot,relative_path,sha256,prior_sha256,recorded_at) "
                            "VALUES (?,?,?,?,?,?,?)", ("planning", 1, 1,
                            launcher.WORLD_REPAIR_RELATIVE, launcher.WORLD_REPAIR_PRIOR,
                            None, 1.0)).lastrowid
        db.execute("INSERT INTO experiment_repairs "
                   "(call_key,old_entry_id,old_sha256,backup_path,state,desired_sha256,"
                   "response_sha256,resources,reserved_at) VALUES (?,?,?,?,?,?,?,?,?)",
                   (launcher.WORLD_REPAIR_KEY, old_id, launcher.WORLD_REPAIR_PRIOR,
                    "repair-evidence/world-before-planning_repair_001_1.md", "applying",
                    hashlib.sha256(body.encode()).hexdigest(),
                    hashlib.sha256(response.encode()).hexdigest(), "{}", 1.0))
    with (patch.object(launcher, "load", return_value=({}, "control-sha")),
          patch.object(launcher, "approval", return_value={}),
          patch.object(launcher, "_dispatch_edit_hook", return_value=None)):
        replay = launcher.reconcile_planning_world(root, project)
    assert replay["status"] == "complete" and world.read_text() == body
    with _db(root) as db:
        assert db.execute("SELECT state,new_entry_id FROM experiment_repairs WHERE call_key=?",
                          (launcher.WORLD_REPAIR_KEY,)).fetchone()[0] == "complete"
        assert db.execute("SELECT COUNT(*) FROM experiment_generated").fetchone() == (2,)
assert "UTF-8 bytes" not in launcher._planning_prompt({"seed": "a" * 64}, 1, {})
with tempfile.TemporaryDirectory(prefix="qmd-long-planning-document-") as tmp:
    root = Path(tmp)
    launcher._write_generated(root, "world.md", long_korean_plan)
    assert (root / "world.md").read_text() == long_korean_plan
spec = {"seed": "a" * 64}
writing_prompt = synthetic_generator.prompt(spec, "writing", 1, 1)
assert "5,000 non-whitespace characters" in writing_prompt
assert "22,000" not in writing_prompt and "1,800" not in writing_prompt
wiki_prompt = synthetic_generator.prompt(spec, "wiki_create", 1, 1, writing="invented")
assert "roughly 600 characters" in wiki_prompt


quota_event = {"type": "item.completed", "item": {
    "type": "error", "message": "Rate limit reached. Retry after reset."}}
refused(lambda: codex_adapter.inspect_event(quota_event), "provider_quota_waiting")
refused(lambda: codex_adapter.inspect_event({"type": "error",
    "message": "network connection timed out"}), "provider_transport_waiting")
refused(lambda: codex_adapter.inspect_event({"type": "item.completed",
    "item": {"type": "error", "message": "Unknown model requested"}}),
    "provider_model_unavailable")
refused(lambda: codex_adapter.inspect_event({"type": "item.completed",
    "item": {"type": "error", "message": "Unexpected private detail"}}),
    "provider_error_item")

for old_sha, detail, permitted in (
        (experiment.PREPLAN_WRITING_INPUT_SHA256, "provider_error_item", True),
        (experiment.PREPLAN_WRITING_INPUT_SHA256, "provider_quota_waiting", False),
        ("a" * 64, "provider_error_item", False)):
    with tempfile.TemporaryDirectory(prefix="qmd-preplan-writing-recovery-") as tmp:
        project = Path(tmp) / "project"; project.mkdir()
        root = Path(tmp) / "shared"; root.mkdir(mode=0o700)
        with (patch.object(experiment, "load", return_value=({}, "control-sha")),
              patch.object(experiment, "approval", return_value={}),
              patch.object(experiment, "_limits", return_value=(5, 45, 20, 1)),
              patch.object(experiment, "check_disk")):
            first = experiment.reserve_manual(root, project, "writing", 1)
            with _db(root) as db:
                db.execute("INSERT INTO experiment_invocations VALUES (?,?,?,NULL,?)",
                           (first["call_key"], old_sha, "uncertain", detail))
            refused(lambda: experiment.reserve_manual(
                root, project, "writing", 1, recovery=1, input_sha256="b" * 64),
                "experiment_recovery_input_changed")
            if permitted:
                assert experiment.reserve_manual(
                    root, project, "writing", 1, recovery=1,
                    input_sha256=experiment.POSTPLAN_WRITING_INPUT_SHA256) == {
                        "status": "reserved", "call_key": "writing:001:1:retry1"}
                with _db(root) as db:
                    assert db.execute("SELECT state FROM experiment_invocations "
                                      "WHERE call_key=?", (first["call_key"],)).fetchone() == ("uncertain",)
            else:
                refused(lambda: experiment.reserve_manual(
                    root, project, "writing", 1, recovery=1,
                    input_sha256=experiment.POSTPLAN_WRITING_INPUT_SHA256),
                    "experiment_recovery_input_changed")

with tempfile.TemporaryDirectory(prefix="qmd-sixth-writing-recovery-") as tmp:
    project = Path(tmp) / "project"; project.mkdir()
    root = Path(tmp) / "shared"; root.mkdir(mode=0o700)
    receipt = root / "attempt-evidence/writing_006_1_retry1.json"
    receipt.parent.mkdir(mode=0o700)
    receipt.write_text('{"synthetic":"short completed response"}')
    receipt.chmod(0o600)
    prior = {"01_Settings/world.md": "1" * 64,
             "02_Characters/cast.md": "2" * 64,
             "03_Plot/arc.md": "3" * 64}
    with _db(root) as db:
        for relative, sha in prior.items():
            db.execute("INSERT INTO experiment_generated "
                       "(kind,episode,slot,relative_path,sha256,prior_sha256,recorded_at) "
                       "VALUES (?,?,?,?,?,?,?)",
                       ("writing", 5, 1, relative, sha, None, 1.0))
        for key, episode, state, detail, input_sha in (
                ("writing:005:1", 5, "complete", None, "5" * 64),
                ("writing:006:1", 6, "uncertain", "synthetic_narrative_too_short",
                 experiment.WRITING_006_OLD_PROMPT_SHA256),
                ("writing:006:1:retry1", 6, "uncertain",
                 "synthetic_narrative_too_short",
                 experiment.WRITING_006_OLD_PROMPT_SHA256)):
            db.execute("INSERT INTO experiment_calls VALUES (?,?,?,?)",
                       (key, "writing", episode, 1.0))
            db.execute("INSERT INTO experiment_invocations VALUES (?,?,?,?,?)",
                       (key, input_sha, state, None, detail))
    with (patch.object(experiment, "load", return_value=({}, "control-sha")),
          patch.object(experiment, "approval", return_value={}),
          patch.object(experiment, "_limits", return_value=(100, None, None, None)),
          patch.object(experiment, "check_disk")):
        attempt = lambda sha: experiment.reserve_manual(
            root, project, "writing", 6, 1, recovery=2,
            input_sha256=sha, prior_docs_sha=prior)
        refused(lambda: attempt("f" * 64), "experiment_recovery_input_changed")
        refused(lambda: attempt(experiment.WRITING_006_REVISED_PROMPT_SHA256),
                "experiment_recovery_receipt_changed")
        with patch.object(experiment, "WRITING_006_RETRY1_RECEIPT_SHA256",
                          hashlib.sha256(receipt.read_bytes()).hexdigest()):
            assert attempt(experiment.WRITING_006_REVISED_PROMPT_SHA256) == {
                "status": "reserved", "call_key": "writing:006:1:retry2"}
            assert attempt(experiment.WRITING_006_REVISED_PROMPT_SHA256) == {
                "status": "already_reserved", "call_key": "writing:006:1:retry2"}
        with _db(root) as db:
            assert db.execute("SELECT COUNT(*) FROM experiment_calls WHERE "
                              "call_key='writing:006:1:retry2'").fetchone() == (1,)
            db.execute("INSERT INTO experiment_invocations VALUES (?,?,?,?,?)",
                       ("writing:006:1:retry2",
                        experiment.WRITING_006_REVISED_PROMPT_SHA256,
                        "uncertain", None, "synthetic_narrative_too_short"))
        refused(lambda: experiment.reserve_manual(
            root, project, "writing", 6, 1, recovery=3,
            input_sha256=experiment.WRITING_006_REVISED_PROMPT_SHA256,
            prior_docs_sha=prior), "experiment_writing_same_prompt_short")
        with _db(root) as db:
            assert db.execute("SELECT COUNT(*) FROM experiment_calls WHERE "
                              "call_key='writing:006:1:retry3'").fetchone() == (0,)

with tempfile.TemporaryDirectory(prefix="qmd-eighth-continuation-recovery-") as tmp:
    project = Path(tmp) / "project"; project.mkdir()
    root = Path(tmp) / "shared"; root.mkdir(mode=0o700)
    folder = root / "attempt-evidence"; folder.mkdir(mode=0o700)
    receipt = folder / "writing_008_1.json"
    draft = folder / "writing_008_1.draft.json"
    receipt.write_text('{"synthetic":"receipt"}'); receipt.chmod(0o600)
    draft.write_text(json.dumps({"prose": "가" * 4782, "setting": "가상 배경",
                                 "characters": "가상 인물", "plot": "가상 사건",
                                 "temporary": None})); draft.chmod(0o600)
    prior = {"01_Settings/world.md": "1" * 64,
             "02_Characters/cast.md": "2" * 64,
             "03_Plot/arc.md": "3" * 64}
    with _db(root) as db:
        for relative, sha in prior.items():
            db.execute("INSERT INTO experiment_generated "
                       "(kind,episode,slot,relative_path,sha256,prior_sha256,recorded_at) "
                       "VALUES (?,?,?,?,?,?,?)",
                       ("writing", 7, 1, relative, sha, None, 1.0))
        for key, episode, state, detail, sha in (
                ("writing:007:1", 7, "complete", None, "7" * 64),
                ("writing:008:1", 8, "uncertain", "synthetic_narrative_too_short",
                 experiment.WRITING_008_OLD_PROMPT_SHA256)):
            db.execute("INSERT INTO experiment_calls VALUES (?,?,?,?)",
                       (key, "writing", episode, 1.0))
            db.execute("INSERT INTO experiment_invocations VALUES (?,?,?,?,?)",
                       (key, sha, state, None, detail))
    with (patch.object(experiment, "load", return_value=({}, "control-sha")),
          patch.object(experiment, "approval", return_value={}),
          patch.object(experiment, "_limits", return_value=(100, None, None, None)),
          patch.object(experiment, "check_disk"),
          patch.object(experiment, "WRITING_008_RECEIPT_SHA256",
                       hashlib.sha256(receipt.read_bytes()).hexdigest()),
          patch.object(experiment, "WRITING_008_DRAFT_SHA256",
                       hashlib.sha256(draft.read_bytes()).hexdigest())):
        attempt = lambda sha: experiment.reserve_manual(
            root, project, "writing", 8, 1, recovery=1,
            input_sha256=sha, prior_docs_sha=prior)
        refused(lambda: attempt(experiment.WRITING_008_OLD_PROMPT_SHA256),
                "experiment_writing_same_prompt_short")
        receipt.write_text('changed')
        refused(lambda: attempt(experiment.WRITING_008_CONTINUATION_PROMPT_SHA256),
                "experiment_recovery_receipt_changed")
        receipt.write_text('{"synthetic":"receipt"}')
        draft.write_text('changed')
        refused(lambda: attempt(experiment.WRITING_008_CONTINUATION_PROMPT_SHA256),
                "experiment_recovery_draft_changed")
        draft.write_text(json.dumps({"prose": "가" * 4782, "setting": "가상 배경",
                                     "characters": "가상 인물", "plot": "가상 사건",
                                     "temporary": None}))
        assert attempt(experiment.WRITING_008_CONTINUATION_PROMPT_SHA256) == {
            "status": "reserved", "call_key": "writing:008:1:retry1"}
        assert attempt(experiment.WRITING_008_CONTINUATION_PROMPT_SHA256) == {
            "status": "already_reserved", "call_key": "writing:008:1:retry1"}
        with _db(root) as db:
            assert db.execute("SELECT COUNT(*) FROM experiment_calls WHERE "
                              "call_key='writing:008:1:retry1'").fetchone() == (1,)

with tempfile.TemporaryDirectory(prefix="qmd-twelfth-json-recovery-") as tmp:
    project = Path(tmp) / "project"; project.mkdir()
    root = Path(tmp) / "shared"; root.mkdir(mode=0o700)
    folder = root / "attempt-evidence"; folder.mkdir(mode=0o700)
    receipt = folder / "writing_012_1.json"
    receipt.write_text('{"synthetic":"malformed-json-receipt"}'); receipt.chmod(0o600)
    prior = {"01_Settings/world.md": "1" * 64,
             "02_Characters/cast.md": "2" * 64,
             "03_Plot/arc.md": "3" * 64}
    with _db(root) as db:
        for relative, sha in prior.items():
            db.execute("INSERT INTO experiment_generated "
                       "(kind,episode,slot,relative_path,sha256,prior_sha256,recorded_at) "
                       "VALUES (?,?,?,?,?,?,?)", ("writing", 11, 1, relative, sha, None, 1.0))
        for key, episode, state, detail, sha in (
                ("writing:011:1", 11, "complete", None, "1" * 64),
                ("writing:012:1", 12, "uncertain", experiment.WRITING_012_OLD_DETAIL,
                 experiment.WRITING_012_OLD_PROMPT_SHA256)):
            db.execute("INSERT INTO experiment_calls VALUES (?,?,?,?)",
                       (key, "writing", episode, 1.0))
            db.execute("INSERT INTO experiment_invocations VALUES (?,?,?,?,?)",
                       (key, sha, state, None, detail))
    with (patch.object(experiment, "load", return_value=({}, "control-sha")),
          patch.object(experiment, "approval", return_value={}),
          patch.object(experiment, "_limits", return_value=(100, None, None, None)),
          patch.object(experiment, "check_disk"),
          patch.object(experiment, "WRITING_012_RECEIPT_SHA256",
                       hashlib.sha256(receipt.read_bytes()).hexdigest())):
        attempt = lambda sha: experiment.reserve_manual(
            root, project, "writing", 12, 1, recovery=1,
            input_sha256=sha, prior_docs_sha=prior)
        refused(lambda: attempt(experiment.WRITING_012_OLD_PROMPT_SHA256),
                "experiment_writing_same_prompt_invalid_json")
        refused(lambda: attempt("f" * 64), "experiment_recovery_input_changed")
        receipt.write_text("changed")
        refused(lambda: attempt(experiment.WRITING_012_REVISED_PROMPT_SHA256),
                "experiment_recovery_receipt_changed")
        receipt.write_text('{"synthetic":"malformed-json-receipt"}')
        assert attempt(experiment.WRITING_012_REVISED_PROMPT_SHA256) == {
            "status": "reserved", "call_key": "writing:012:1:retry1"}
        assert attempt(experiment.WRITING_012_REVISED_PROMPT_SHA256) == {
            "status": "already_reserved", "call_key": "writing:012:1:retry1"}
        with _db(root) as db:
            assert db.execute("SELECT COUNT(*) FROM experiment_calls WHERE "
                              "call_key='writing:012:1:retry1'").fetchone() == (1,)

for fail_hook in (None, 2, 4):
    with tempfile.TemporaryDirectory(prefix="qmd-eighth-continuation-end-to-end-") as tmp:
        project = Path(tmp) / "novel"; project.mkdir()
        story = experiment.story_for(project); story.mkdir()
        root = experiment.shared_for(project); root.mkdir(mode=0o700)
        folder = root / "attempt-evidence"; folder.mkdir(mode=0o700)
        draft_value = {"prose": "가" * 4782, "setting": "가상 새 배경",
                       "characters": "가상 새 인물", "plot": "가상 새 사건",
                       "temporary": None}
        draft_path = folder / "writing_008_1.draft.json"
        draft_path.write_text(json.dumps(draft_value)); draft_path.chmod(0o600)
        receipt_path = folder / "writing_008_1.json"
        receipt_path.write_text('{"synthetic":"receipt"}'); receipt_path.chmod(0o600)
        old = {"01_Settings/world.md": "가상의 옛 배경",
               "02_Characters/cast.md": "가상의 옛 인물",
               "03_Plot/arc.md": "가상의 옛 사건"}
        with _db(root) as db:
            for relative, body in old.items():
                path = story / relative
                path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                path.write_text(body); path.chmod(0o600)
                db.execute("INSERT INTO experiment_generated "
                           "(kind,episode,slot,relative_path,sha256,prior_sha256,recorded_at) "
                           "VALUES (?,?,?,?,?,?,?)",
                           ("writing", 7, 1, relative,
                            hashlib.sha256(body.encode()).hexdigest(), None, 1.0))
            for key, episode, state, detail, sha in (
                    ("writing:007:1", 7, "complete", None, "7" * 64),
                    ("writing:008:1", 8, "uncertain", "synthetic_narrative_too_short",
                     experiment.WRITING_008_OLD_PROMPT_SHA256)):
                db.execute("INSERT INTO experiment_calls VALUES (?,?,?,?)",
                           (key, "writing", episode, 1.0))
                db.execute("INSERT INTO experiment_invocations VALUES (?,?,?,?,?)",
                           (key, sha, state, None, detail))
        prompt_sha = hashlib.sha256(
            launcher._continuation_prompt(draft_value).encode()).hexdigest()
        edits = []
        def hook(_story, relative, **kw):
            edits.append((relative, kw.get("deleted", False)))
            if len(edits) == fail_hook:
                raise ValueError("synthetic_test_hook_failure")
        calls = []
        def fake_invoke(prompt, *_args, **_kwargs):
            calls.append(prompt)
            return json.dumps({"continuation": "나" * 350})
        with (patch.object(launcher, "load", return_value=({}, "control-sha")),
              patch.object(scope, "_synthetic_bundle", return_value=(root, project)),
              patch.object(launcher, "policy_for", return_value={"provenanceSha256": "f" * 64}),
              patch.object(launcher, "generator_spec",
                           return_value=({"seed": "a" * 64}, "f" * 64)),
              patch.object(launcher, "approval", return_value={}),
              patch.object(launcher, "check_disk"),
              patch.object(launcher, "_dispatch_edit_hook", side_effect=hook),
              patch.object(launcher, "WRITING_008_DRAFT_SHA256",
                           hashlib.sha256(draft_path.read_bytes()).hexdigest()),
              patch.object(experiment, "load", return_value=({}, "control-sha")),
              patch.object(experiment, "approval", return_value={}),
              patch.object(experiment, "_limits", return_value=(100, None, None, None)),
              patch.object(experiment, "check_disk"),
              patch.object(experiment, "WRITING_008_CONTINUATION_PROMPT_SHA256", prompt_sha),
              patch.object(experiment, "WRITING_008_RECEIPT_SHA256",
                           hashlib.sha256(receipt_path.read_bytes()).hexdigest()),
              patch.object(experiment, "WRITING_008_DRAFT_SHA256",
                           hashlib.sha256(draft_path.read_bytes()).hexdigest())):
            if fail_hook is None:
                result = launcher.launch_one(root, project, "writing", 8, 1,
                                             recovery=1, invoke=fake_invoke)
                assert result["status"] == "complete"
                assert (story / "04_Manuscript/ep008.md").read_text() == (
                    draft_value["prose"] + "\n\n" + "나" * 350)
                assert [(story / relative).read_text() for relative in old] == [
                    draft_value["setting"], draft_value["characters"], draft_value["plot"]]
                with _db(root) as db:
                    assert db.execute("SELECT COUNT(*) FROM experiment_generated WHERE "
                                      "kind='writing' AND episode=8").fetchone() == (4,)
            else:
                refused(lambda: launcher.launch_one(root, project, "writing", 8, 1,
                                                    recovery=1, invoke=fake_invoke),
                        "synthetic_test_hook_failure")
                assert [(story / relative).read_text() for relative in old] == list(old.values())
                assert not (story / "04_Manuscript/ep008.md").exists()
                with _db(root) as db:
                    assert db.execute("SELECT COUNT(*) FROM experiment_generated WHERE "
                                      "kind='writing' AND episode=8").fetchone() == (0,)
                    assert db.execute("SELECT state FROM experiment_invocations WHERE "
                                      "call_key='writing:008:1:retry1'").fetchone() == ("uncertain",)
                if fail_hook == 4:
                    assert edits[-1] == ("04_Manuscript/ep008.md", True)
            assert len(calls) == 1
            assert (folder / "writing_008_1_retry1.complete.json").is_file()
            assert (folder / "writing_008_1_retry1.output-backup.json").is_file()
            completed = json.loads((folder / "writing_008_1_retry1.complete.json").read_text())
            assert json.loads(completed["response"]) == {"continuation": "나" * 350}
            assert hashlib.sha256(completed["response"].encode()).hexdigest() == (
                completed["responseSha256"])
            assert not (folder / "writing_008_1_retry1.complete.json").stat().st_mode & 0o077
            assert hashlib.sha256(draft_path.read_bytes()).hexdigest() == (
                experiment.WRITING_008_DRAFT_SHA256)
            duplicate = launcher.launch_one(
                root, project, "writing", 8, 1, recovery=1,
                invoke=lambda *_a, **_k: (_ for _ in ()).throw(
                    AssertionError("duplicate external call")))
            assert duplicate["status"] == "already_reserved"

with tempfile.TemporaryDirectory(prefix="qmd-experiment-resume-") as tmp:
    project = Path(tmp) / "project"; project.mkdir()
    root = Path(tmp) / "shared"; root.mkdir(mode=0o700)
    with (patch.object(experiment, "load", return_value=({}, "control-sha")),
          patch.object(experiment, "approval", return_value={}),
          patch.object(experiment, "_limits", return_value=(5, 45, 20, 1)),
          patch.object(experiment, "check_disk")):
        first = experiment.reserve_manual(root, project, "writing", 1)
        assert first == {"status": "reserved", "call_key": "writing:001:1"}
        with _db(root) as db:
            db.execute("INSERT INTO experiment_invocations VALUES (?,?,?,NULL,?)",
                       (first["call_key"], "a" * 64, "uncertain", "provider_quota_waiting"))
        refused(lambda: experiment.reserve_manual(root, project, "writing", 1, recovery=1,
                                                   input_sha256="b" * 64),
                "experiment_recovery_input_changed")
        refused(lambda: experiment.reserve_manual(root, project, "writing", 1, recovery=1),
                "experiment_recovery_input_changed")
        retry = experiment.reserve_manual(root, project, "writing", 1, recovery=1,
                                          input_sha256="a" * 64)
        assert retry == {"status": "reserved", "call_key": "writing:001:1:retry1"}
        assert experiment.reserve_manual(root, project, "writing", 1, recovery=1,
                                         input_sha256="a" * 64) == {
            "status": "already_reserved", "call_key": retry["call_key"]}
        refused(lambda: experiment.reserve_manual(root, project, "writing", 1, recovery=2,
                                                  input_sha256="a" * 64),
                "experiment_recovery_not_eligible")
        with _db(root) as db:
            db.execute("INSERT INTO experiment_invocations VALUES (?,?,?,NULL,NULL)",
                       (retry["call_key"], "a" * 64, "complete"))
        refused(lambda: experiment.reserve_manual(root, project, "writing", 1, recovery=2,
                                                  input_sha256="a" * 64),
                "experiment_recovery_not_eligible")
        with _db(root) as db:
            db.execute("UPDATE experiment_invocations SET state='uncertain' WHERE call_key=?",
                       (retry["call_key"],))
            db.execute("INSERT INTO experiment_generated (kind,episode,slot,relative_path,sha256,prior_sha256,recorded_at) VALUES (?,?,?,?,?,?,?)",
                       ("writing", 1, 1, "04_Manuscript/ep001.md", "a" * 64, None, 1.0))
        refused(lambda: experiment.reserve_manual(root, project, "writing", 1, recovery=2,
                                                  input_sha256="a" * 64),
                "experiment_recovery_outputs_present")
        with _db(root) as db:
            assert db.execute("SELECT COUNT(*) FROM experiment_calls").fetchone()[0] == 2
            assert db.execute("SELECT COUNT(*) FROM experiment_generated").fetchone()[0] == 1

with tempfile.TemporaryDirectory(prefix="qmd-experiment-order-") as tmp:
    project = Path(tmp) / "project"; project.mkdir()
    root = Path(tmp) / "shared"; root.mkdir(mode=0o700)
    base = {"01_Settings/world.md": "1" * 64,
            "02_Characters/cast.md": "2" * 64,
            "03_Plot/arc.md": "3" * 64,
            "01_Settings/temporary-omen.md": "4" * 64}
    with (patch.object(experiment, "load", return_value=({}, "control-sha")),
          patch.object(experiment, "approval", return_value={}),
          patch.object(experiment, "_limits", return_value=(5, 45, 20, 1)),
          patch.object(experiment, "check_disk")):
        first = experiment.reserve_manual(root, project, "writing", 1)
        refused(lambda: experiment.reserve_manual(root, project, "writing", 2,
                                                  prior_docs_sha=base),
                "experiment_episode_order_invalid")
        with _db(root) as db:
            db.execute("INSERT INTO experiment_invocations VALUES (?,?,?,NULL,NULL)",
                       (first["call_key"], "a" * 64, "complete"))
            for relative, sha in base.items():
                kind, slot = (("writing", 1) if relative == "01_Settings/temporary-omen.md"
                              else ("planning", {"01_Settings/world.md": 1,
                                                  "02_Characters/cast.md": 2,
                                                  "03_Plot/arc.md": 3}[relative]))
                db.execute("INSERT INTO experiment_generated (kind,episode,slot,relative_path,sha256,prior_sha256,recorded_at) VALUES (?,?,?,?,?,?,?)",
                           (kind, 1, slot, relative, sha, None, 1.0))
        second = experiment.reserve_manual(root, project, "writing", 2,
                                           prior_docs_sha=base)
        assert second["status"] == "reserved"
        refused(lambda: experiment.reserve_manual(root, project, "writing", 3,
                                                  prior_docs_sha={k: v for k, v in base.items()
                                                                  if k != "01_Settings/temporary-omen.md"}),
                "experiment_episode_order_invalid")
        with _db(root) as db:
            db.execute("INSERT INTO experiment_invocations VALUES (?,?,?,NULL,NULL)",
                       (second["call_key"], "b" * 64, "complete"))
        old = {k: v for k, v in base.items() if k != "01_Settings/temporary-omen.md"}
        refused(lambda: experiment.reserve_manual(root, project, "writing", 3,
                                                  prior_docs_sha=old),
                "experiment_episode_order_invalid")
        newer = {key: str(index) * 64 for index, key in enumerate(old, start=5)}
        with _db(root) as db:
            for relative, sha in newer.items():
                db.execute("INSERT INTO experiment_generated (kind,episode,slot,relative_path,sha256,prior_sha256,recorded_at) VALUES (?,?,?,?,?,?,?)",
                           ("writing", 2, 1, relative, sha, old[relative], 2.0))
        third = experiment.reserve_manual(root, project, "writing", 3,
                                          prior_docs_sha=newer)
        assert third["status"] == "reserved"
        with _db(root) as db:
            assert db.execute("SELECT COUNT(*) FROM experiment_calls").fetchone()[0] == 3

with tempfile.TemporaryDirectory(prefix="qmd-planning-quota-") as tmp:
    project = Path(tmp) / "project"; project.mkdir()
    root = Path(tmp) / "shared"; root.mkdir(mode=0o700)
    with (patch.object(experiment, "load", return_value=({}, "control-sha")),
          patch.object(experiment, "approval", return_value={}),
          patch.object(experiment, "_limits", return_value=(5, 45, 20, 1)),
          patch.object(experiment, "check_disk")):
        first = experiment.reserve_manual(root, project, "planning", 1, 1)
        with _db(root) as db:
            db.execute("INSERT INTO experiment_invocations VALUES (?,?,?,NULL,?)",
                       (first["call_key"], "e" * 64, "uncertain", "provider_quota_waiting"))
        refused(lambda: experiment.reserve_manual(root, project, "planning", 1, 1,
                                                   recovery=1, input_sha256="f" * 64),
                "experiment_recovery_input_changed")
        resumed = experiment.reserve_manual(root, project, "planning", 1, 1,
                                            recovery=1, input_sha256="e" * 64)
        assert resumed == {"status": "reserved", "call_key": "planning:001:1:retry1"}
        with _db(root) as db:
            assert db.execute("SELECT COUNT(*) FROM experiment_calls").fetchone()[0] == 2

with tempfile.TemporaryDirectory(prefix="qmd-planning-contract-retry-") as tmp:
    project = Path(tmp) / "project"; project.mkdir()
    root = Path(tmp) / "shared"; root.mkdir(mode=0o700)
    with (patch.object(experiment, "load", return_value=({}, "control-sha")),
          patch.object(experiment, "approval", return_value={}),
          patch.object(experiment, "_limits", return_value=(5, 45, 20, 1)),
          patch.object(experiment, "check_disk")):
        for key, digest, detail in (("planning:001:1", "a" * 64, "invalid_shared_trainer"),
                                    ("planning:001:1:retry1", "a" * 64, "provider_error_item"),
                                    ("planning:001:1:retry2", "a" * 64,
                                     "synthetic_planning_response_invalid")):
            with _db(root) as db:
                db.execute("INSERT INTO experiment_calls VALUES (?,?,?,?)",
                           (key, "planning", 1, 1.0))
                db.execute("INSERT INTO experiment_invocations VALUES (?,?,?,NULL,?)",
                           (key, digest, "uncertain", detail))
        changed = experiment.reserve_manual(root, project, "planning", 1, 1,
                                            recovery=3, input_sha256="b" * 64)
        assert changed == {"status": "reserved", "call_key": "planning:001:1:retry3"}

with tempfile.TemporaryDirectory(prefix="qmd-planning-only-") as tmp:
    project = Path(tmp) / "novel"; project.mkdir()
    story = experiment.story_for(project); story.mkdir()
    root = experiment.shared_for(project); root.mkdir(mode=0o700)
    edits = []
    policy = {"provenanceSha256": "f" * 64}
    content = ("가상의 도서관 세계 규칙을 기록한다. " * 12,
               "가상의 사서와 견습생 관계를 기록한다. " * 12,
               "가상의 다섯 화 줄거리 흐름을 기록한다. " * 12)
    with (patch.object(launcher, "load", return_value=({}, "control-sha")),
          patch.object(scope, "_synthetic_bundle", return_value=(root, project)),
          patch.object(launcher, "policy_for", return_value=policy),
          patch.object(launcher, "generator_spec", return_value=({"seed": "a" * 64}, "f" * 64)),
          patch.object(launcher, "approval", return_value={}),
          patch.object(launcher, "check_disk"),
          patch.object(launcher, "_dispatch_edit_hook", side_effect=lambda _s, r, **_k: edits.append(r)),
          patch.object(experiment, "load", return_value=({}, "control-sha")),
          patch.object(experiment, "approval", return_value={}),
          patch.object(experiment, "_limits", return_value=(5, 45, 20, 1)),
          patch.object(experiment, "check_disk")):
        refused(lambda: launcher.launch_one(root, project, "writing", 1,
                                             invoke=lambda *_a, **_k: "never"),
                "synthetic_planning_incomplete")
        refused(lambda: launcher.launch_one(root, project, "planning", 1, 2,
                                             invoke=lambda *_a, **_k: "never"),
                "synthetic_generated_input_missing")
        for slot, body in enumerate(content, start=1):
            result = launcher.launch_one(root, project, "planning", 1, slot,
                                         invoke=lambda prompt, *_a, body=body, **_k:
                                         json.dumps({"content": body}))
            assert result == {"status": "complete", "call_key": f"planning:001:{slot}",
                              "worker": None}
            if slot < 3:
                refused(lambda: launcher.launch_one(root, project, "writing", 1,
                                                     invoke=lambda *_a, **_k: "never"),
                        "synthetic_planning_incomplete")
        assert edits == ["01_Settings/world.md", "02_Characters/cast.md", "03_Plot/arc.md"]
        assert all((story / relative).is_file() for relative in edits)
        assert not (story / "04_Manuscript/ep001.md").exists()
        with _db(root) as db:
            assert db.execute("SELECT kind,COUNT(*) FROM experiment_calls GROUP BY kind").fetchall() == [("planning", 3)]
            launcher._planning_ready(db, story)
        assert launcher.launch_one(root, project, "planning", 1,
                                   invoke=lambda *_a, **_k: (_ for _ in ()).throw(
                                       AssertionError("duplicate external call")))["status"] == "already_reserved"
        observed_prompts = []
        def prose_response(prompt, *_args, **_kwargs):
            observed_prompts.append(prompt)
            return json.dumps({"prose": "가" * 5000, "setting": "discarded setting",
                               "characters": "discarded cast", "plot": "discarded plot",
                               "temporary": "Invented temporary omen."})
        assert launcher.launch_one(root, project, "writing", 1,
                                   invoke=prose_response)["status"] == "complete"
        assert all(body in observed_prompts[0] for body in content)
        assert [(story / relative).read_text() for relative in edits[:3]] == list(content)
        assert edits[3:] == ["01_Settings/temporary-omen.md",
                             "04_Manuscript/ep001.md"]
        with _db(root) as db:
            plan_rows = db.execute("SELECT kind,relative_path FROM experiment_generated "
                                   "WHERE relative_path IN (?,?,?) ORDER BY entry_id",
                                   tuple(edits[:3])).fetchall()
            assert plan_rows == [("planning", relative) for relative in edits[:3]]

with tempfile.TemporaryDirectory(prefix="qmd-attempt-evidence-") as tmp:
    root = Path(tmp)
    stdout = root / "events.jsonl"
    stderr = root / "stderr.txt"
    stdout.write_text(json.dumps({"type": "item.completed", "item": {
        "type": "error", "message": "Rate limit. Authorization: Bearer PRIVATE-CREDENTIAL"}}) + "\n")
    stderr.write_text("Bearer PRIVATE-CREDENTIAL.\n")
    launcher._record_attempt(root, "planning:001:1", "synthetic prompt", stdout,
                             stderr, 1)
    saved = (root / "attempt-evidence/planning_001_1.json").read_text()
    assert "PRIVATE-CREDENTIAL" not in saved
    evidence = json.loads(saved)
    assert evidence["providerFailureClasses"] == ["provider_quota_waiting"]
    assert evidence["errorDetails"][0]["messageCategory"] == "rate_limit"
    assert evidence["stdout"]["bytes"] == stdout.stat().st_size
    assert evidence["stderr"]["bytes"] == stderr.stat().st_size
    stdout.write_text(json.dumps({"type": "item.started", "item": {
        "type": "command_execution", "command": "echo PRIVATE-CREDENTIAL"}}) + "\n")
    launcher._record_attempt(root, "query:001:1", "synthetic prompt", stdout,
                             stderr, 0)
    action_saved = (root / "attempt-evidence/query_001_1.json").read_text()
    assert "PRIVATE-CREDENTIAL" not in action_saved
    action = json.loads(action_saved)
    assert action["itemTypes"] == {"command_execution": 1}
    assert action["actionShapes"] == [{
        "eventType": "item.started", "itemType": "command_execution",
        "fieldCount": 2, "hasCommand": True, "hasName": False,
        "commandBytes": len("echo PRIVATE-CREDENTIAL"),
        "commandSha256": hashlib.sha256(b"echo PRIVATE-CREDENTIAL").hexdigest()}]
    stdout.write_text(json.dumps({"type": "item.started", "item": {
        "type": "privatecredential", "secretvalue": "PRIVATE-CREDENTIAL",
        "command": "\ud800"}}) + "\n")
    launcher._record_attempt(root, "query:001:2", "synthetic prompt", stdout,
                             stderr, 0)
    guarded_saved = (root / "attempt-evidence/query_001_2.json").read_text()
    assert "PRIVATE-CREDENTIAL" not in guarded_saved
    assert "privatecredential" not in guarded_saved
    assert "secretvalue" not in guarded_saved
    guarded = json.loads(guarded_saved)
    assert guarded["itemTypes"] == {"other": 1}
    assert guarded["actionShapes"] == [{
        "eventType": "item.started", "itemType": "other", "fieldCount": 3,
        "hasCommand": True, "hasName": False, "commandBytes": None,
        "commandSha256": None}]
    stdout.write_text(json.dumps({"type": "item.completed", "item": {
        "type": "error", "code": "hook_command_failed",
        "message": "Hook command failed; api_key=PRIVATE-CREDENTIAL"}}) + "\n")
    launcher._record_attempt(root, "planning:001:3", "synthetic prompt", stdout,
                             stderr, 1)
    generic = (root / "attempt-evidence/planning_001_3.json").read_text()
    assert "PRIVATE-CREDENTIAL" not in generic
    assert json.loads(generic)["providerFailureClasses"] == ["provider_error_item"]
    detail = json.loads(generic)["errorDetails"][0]
    assert {key: detail[key] for key in ("eventType", "itemType", "code",
                                        "errorType", "messageCategory")} == {
        "eventType": "item.completed", "itemType": "error",
        "code": "hook_command_failed", "errorType": None,
        "messageCategory": "hook_failure"}
    novel_message = "Nova meets the hidden guardian at dawn"
    stdout.write_text(json.dumps({"type": "item.completed", "item": {
        "type": "error", "message": novel_message}}) + "\n")
    stderr.write_text(novel_message)
    launcher._record_attempt(root, "planning:001:1:retry2", "synthetic prompt", stdout,
                             stderr, 1)
    novel_evidence = (root / "attempt-evidence/planning_001_1_retry2.json").read_text()
    assert json.loads(novel_evidence)["errorDetails"][0]["redactedMessage"] is None
    assert json.loads(novel_evidence)["errorDetails"][0]["messageCategory"] is None
    stdout.write_text(json.dumps({"type": "item.completed", "item": {
        "type": "error", "code": "aliceprivatepass",
        "message": novel_message}}) + "\n")
    launcher._record_attempt(root, "planning:001:1:retry3", "synthetic prompt", stdout,
                             stderr, 1)
    unknown_code = (root / "attempt-evidence/planning_001_1_retry3.json").read_text()
    assert "aliceprivatepass" not in unknown_code
    assert json.loads(unknown_code)["errorDetails"][0]["code"] is None
    stdout.write_text("\n".join(json.dumps(event) for event in (
        {"type": "item.completed", "item": {"type": "error", "code": "MCP_TIMEOUT",
                                              "message": "Tool failed. Bearer PRIVATE-CREDENTIAL"}},
        {"type": "turn.completed", "usage": {"input_tokens": 12, "output_tokens": 3},
         "finish_reason": "stopped"})) + "\n")
    launcher._record_attempt(root, "planning_repair:001:1", "synthetic prompt", stdout,
                             stderr, 0)
    diagnostic = json.loads((root / "attempt-evidence/planning_repair_001_1.json").read_text())
    assert diagnostic["errorDetails"][0]["code"] == "MCP_TIMEOUT"
    assert "PRIVATE-CREDENTIAL" not in diagnostic["errorDetails"][0]["redactedMessage"]
    assert diagnostic["usage"] == {"input_tokens": 12, "output_tokens": 3}
    assert diagnostic["finalReason"] == "stopped"
    stdout.write_text("\n".join(json.dumps(event) for event in (
        {"type": "item.completed", "item": {"type": "error", "message": "Hook failed"}},
        {"type": "item.completed", "item": {"type": "agent_message",
                                               "text": json.dumps({"content": long_korean_plan})}},
        {"type": "turn.completed", "usage": {"input_tokens": 10,
                                               "output_tokens": 5}})) + "\n")
    assert launcher._completed_response(stdout) == json.dumps({"content": long_korean_plan})
    launcher._record_attempt(root, "planning:001:2", "synthetic prompt", stdout,
                             stderr, 0)
    completed = json.loads((root / "attempt-evidence/planning_001_2.json").read_text())
    assert completed["turnCompleted"] and completed["finalMessagePresent"]
    assert not completed["turnFailed"]
    artifact = root / "attempt-evidence" / completed["planningResponseArtifact"]
    assert json.loads(artifact.read_text()) == {"content": long_korean_plan}
    assert completed["planningValidation"]["predicate"] == "ok"
    assert completed["usage"] == {"input_tokens": 10, "output_tokens": 5}
    stdout.write_text("\n".join(json.dumps(event) for event in (
        {"type": "item.completed", "item": {"type": "agent_message",
                                               "text": json.dumps({"content": long_korean_plan})}},
        {"type": "turn.completed", "usage": {}})) + "\n")
    launcher._record_attempt(root, "planning:001:3:retry1", "synthetic prompt",
                             stdout, stderr, 0)
    large = json.loads((root / "attempt-evidence/planning_001_3_retry1.json").read_text())
    assert large["planningValidation"]["contentBytes"] == 30000
    assert json.loads((root / "attempt-evidence" / large["planningResponseArtifact"]).read_text()) == {"content": long_korean_plan}
    stdout.write_text("\n".join(json.dumps(event) for event in (
        {"type": "item.completed", "item": {"type": "agent_message",
                                               "text": '{"content":"Bearer PRIVATE-CREDENTIAL"}'}},
        {"type": "turn.completed", "usage": {}})) + "\n")
    launcher._record_attempt(root, "planning:001:2:retry1", "synthetic prompt",
                             stdout, stderr, 0)
    redacted = json.loads((root / "attempt-evidence/planning_001_2_retry1.json").read_text())
    assert "PRIVATE-CREDENTIAL" not in (
        root / "attempt-evidence" / redacted["planningResponseArtifact"]).read_text()
    stdout.write_text("\n".join(json.dumps(event) for event in (
        {"type": "item.completed", "item": {"type": "error", "message": "Hook failed"}},
        {"type": "turn.completed", "usage": {"input_tokens": 10,
                                               "output_tokens": 0}})) + "\n")
    refused(lambda: launcher._completed_response(stdout),
            "synthetic_cli_error_item_without_response")
    stdout.write_text("\n".join(json.dumps(event) for event in (
        {"type": "item.completed", "item": {"type": "agent_message",
                                               "text": '{"content":"untrusted"}'}},
        {"type": "turn.failed", "message": "server error"})) + "\n")
    refused(lambda: launcher._completed_response(stdout), "provider_error")

with tempfile.TemporaryDirectory(prefix="qmd-planning-output-schema-") as tmp:
    project = Path(tmp) / "novel"; project.mkdir()
    story = experiment.story_for(project); story.mkdir()
    root = experiment.shared_for(project); root.mkdir(mode=0o700)
    seen = []
    class SchemaProcess:
        def __init__(self, args, *, stdout, **_kwargs):
            assert args[-1] == "-" and "--output-schema" in args
            schema_file = Path(args[args.index("--output-schema") + 1])
            schema = json.loads(schema_file.read_text())
            assert schema == {"type": "object", "properties": {"content": {"type": "string"}},
                              "required": ["content"], "additionalProperties": False}
            seen.append(schema_file)
            self.pid = 123456
            self.returncode = 0
            stdout.write((json.dumps({"type": "item.completed", "item": {
                "type": "error", "message": "Synthetic hook tool error"}}) + "\n").encode())
            stdout.write((json.dumps({"type": "item.completed", "item": {
                "type": "agent_message", "text": json.dumps({"content": long_korean_plan})}}) + "\n").encode())
            stdout.write(b'{"type":"turn.completed","usage":{}}\n')
            stdout.flush()
        def poll(self):
            return 0
    with (patch("context_learning.shared._policy", return_value={"maxRssMib": 4096}),
          patch("context_learning.shared.resource_report", return_value={"ready": True}),
          patch.object(launcher, "_query_argv", return_value=["codex", "exec", "--json", "-"]),
          patch.object(launcher.subprocess, "Popen", SchemaProcess)):
        result = launcher._invoke("synthetic planning prompt", story, "planning",
                                  check=lambda: None, call_key="planning:001:1")
    assert result["response"] == json.dumps({"content": long_korean_plan})
    assert len(seen) == 1
    recorded = json.loads((root / "attempt-evidence/planning_001_1.json").read_text())
    assert recorded["turnCompleted"] and recorded["finalMessagePresent"]
    assert recorded["planningValidation"]["predicate"] == "ok"

for episode in (11, 12):
    with tempfile.TemporaryDirectory(prefix="qmd-writing-output-schema-") as tmp:
        project = Path(tmp) / "novel"; project.mkdir()
        story = experiment.story_for(project); story.mkdir()
        root = experiment.shared_for(project); root.mkdir(mode=0o700)
        malformed = '{"prose":"line one\nline two","setting":"s",' + (
            '"characters":"c","plot":"p","temporary":null}')
        class WritingSchemaProcess:
            def __init__(self, args, *, stdout, **_kwargs):
                assert args[-1] == "-"
                assert ("--output-schema" in args) == (episode >= 12)
                if episode >= 12:
                    schema = json.loads(Path(args[args.index("--output-schema") + 1]).read_text())
                    assert schema == {"type": "object", "properties": {
                        "prose": {"type": "string"}, "setting": {"type": "string"},
                        "characters": {"type": "string"}, "plot": {"type": "string"},
                        "temporary": {"type": "null"}},
                        "required": ["prose", "setting", "characters", "plot", "temporary"],
                        "additionalProperties": False}
                self.pid = 123456
                self.returncode = 0
                for event in ({"type": "item.completed", "item": {
                        "type": "agent_message", "text": malformed}},
                              {"type": "turn.completed", "usage": {}}):
                    stdout.write((json.dumps(event) + "\n").encode())
                stdout.flush()
            def poll(self):
                return 0
        key = f"writing:{episode:03d}:1"
        with (patch("context_learning.shared._policy", return_value={"maxRssMib": 4096}),
              patch("context_learning.shared.resource_report", return_value={"ready": True}),
              patch.object(launcher, "_query_argv",
                           return_value=["codex", "exec", "--json", "-"]),
              patch.object(launcher.subprocess, "Popen", WritingSchemaProcess)):
            result = launcher._invoke("synthetic writing prompt", story, "writing",
                                      check=lambda: None, call_key=key)
        assert result["response"] == malformed
        raw = root / "attempt-evidence" / (key.replace(":", "_") + ".raw-response.txt")
        assert raw.read_text() == malformed
        assert hashlib.sha256(raw.read_bytes()).hexdigest() == hashlib.sha256(
            malformed.encode()).hexdigest()
        assert not raw.stat().st_mode & 0o077
        try:
            json.loads(raw.read_text())
        except json.JSONDecodeError:
            pass
        else:
            raise AssertionError("malformed model output unexpectedly parsed")

with patch.object(entry, "launch_one", return_value={"status": "complete"}) as launch:
    with redirect_stdout(io.StringIO()):
        assert entry.main(["recover", "/tmp/root", "/tmp/project", "writing", "1", "1", "2"]) == 0
    assert launch.call_args.kwargs == {"recovery": 2}

# An actual hook capture may bind before a later unexpected provider tool
# action makes the launch uncertain. Retain that case as history, never gold
# it, and allow only the exact pinned one-time q3 recovery. No provider runs.
with tempfile.TemporaryDirectory(prefix="qmd-bound-action-recovery-") as tmp:
    project = Path(tmp) / "novel"; project.mkdir()
    root = project / experiment.SHARED_NAME; root.mkdir(mode=0o700)
    receipt = root / "attempt-evidence/query_003_1_retry3.json"
    receipt.parent.mkdir(mode=0o700)
    receipt.write_text('{"synthetic":"unexpected_action_item"}')
    receipt.chmod(0o600)
    rid = "00000000-0000-4000-8000-000000000003"
    previous = "query:003:1:retry3"
    source_id = f"{WIKI_COLLECTION}/ep003-main.md"
    capture_sha = hashlib.sha256(b"{}").hexdigest()
    with _db(root) as db:
        db.execute("INSERT INTO experiment_calls VALUES (?,?,?,?)",
                   (previous, "query", 3, 1.0))
        db.execute("INSERT INTO experiment_invocations VALUES (?,?,?,?,?)",
                   (previous, "a" * 64, "uncertain", None, "unexpected_action_item"))
        db.execute("INSERT INTO experiment_query_tickets "
                   "(call_key,episode,slot,prompt_sha256,source_id,source_sha256,"
                   "request_id,input_sha256,candidate_sha256) VALUES (?,?,?,?,?,?,?,?,?)",
                   (previous, 3, 1, "a" * 64, source_id, "b" * 64,
                    rid, capture_sha, "d" * 64))
        db.execute("INSERT INTO cases (project_key,project_root,request_id,"
                   "input_sha256,sample,bodies,corpus_fingerprint,captured_at,"
                   "split,family_keys,use_state,status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                   ("project", str(project), rid, capture_sha, "{}", "{}", "index",
                    1.0, "train", "[]", "active", "queued"))
    with (patch.object(experiment, "load", return_value=({}, "control-sha")),
          patch.object(experiment, "approval", return_value={}),
          patch.object(experiment, "_limits", return_value=(5, None, None, None)),
          patch.object(experiment, "check_disk")):
        retry = lambda: experiment.reserve_manual(
            root, project, "query", 3, 1, recovery=4,
            input_sha256="a" * 64, source_id=source_id,
            source_sha256="b" * 64)
        refused(retry, "experiment_recovery_receipt_changed")
        with _db(root) as db:
            assert db.execute("SELECT use_state FROM cases WHERE request_id=?",
                              (rid,)).fetchone() == ("active",)
            db.execute("UPDATE cases SET status='gold' WHERE request_id=?", (rid,))
        with patch.object(experiment, "ACTION_RECOVERY_RECEIPT_SHA256",
                          hashlib.sha256(receipt.read_bytes()).hexdigest()):
            refused(retry, "experiment_recovery_not_eligible")
            with _db(root) as db:
                db.execute("UPDATE cases SET status='queued' WHERE request_id=?", (rid,))
                db.execute("UPDATE experiment_query_tickets SET request_id=NULL "
                           "WHERE call_key=?", (previous,))
            refused(retry, "experiment_recovery_not_eligible")
            with _db(root) as db:
                db.execute("UPDATE experiment_query_tickets SET request_id=? "
                           "WHERE call_key=?", (rid, previous))
            # A SessionStart worker must defer the uncertain bound capture.
            from context_learning import synthetic_teacher
            with (patch.object(synthetic_teacher, "policy_for", return_value={
                    "maxCalls": None, "model": "gpt-6.1-sol"}),
                  patch.object(scope, "_synthetic_bundle", return_value=(root, project)),
                  patch.object(synthetic_teacher, "resource_report",
                               return_value={"ready": True}),
                  patch.object(experiment, "load", return_value=({}, "control-sha"))):
                deferred = synthetic_teacher.process_pending(
                    root, project, {"maxGoldCalls": None,
                                    "goldModel": "gpt-6.1-sol"},
                    invoke=lambda *_args, **_kwargs: None)
            assert deferred["status"] == "deferred_query_launch"
            with _db(root) as db:
                assert db.execute("SELECT status,use_state FROM cases WHERE request_id=?",
                                  (rid,)).fetchone() == ("queued", "active")
                # Older worker versions could already have made this exact
                # pending-review transition before the recovery patch landed.
                db.execute("UPDATE cases SET status='pending_review',error='other' "
                           "WHERE request_id=?", (rid,))
            refused(retry, "experiment_recovery_not_eligible")
            with _db(root) as db:
                db.execute("UPDATE cases SET error='synthetic_scope_invalid' "
                           "WHERE request_id=?", (rid,))
            refused(lambda: experiment.reserve_manual(
                root, project, "query", 3, 1, recovery=4,
                input_sha256="a" * 64, source_id=source_id,
                source_sha256="c" * 64), "experiment_recovery_source_changed")
            with _db(root) as db:
                assert db.execute("SELECT use_state FROM cases WHERE request_id=?",
                                  (rid,)).fetchone() == ("active",)
                assert db.execute("SELECT 1 FROM experiment_calls WHERE call_key='query:003:1:retry4'").fetchone() is None
            reserved = retry()
            assert reserved == {"status": "reserved", "call_key": "query:003:1:retry4"}
            assert retry() == {"status": "resumable", "call_key": "query:003:1:retry4"}
            with _db(root) as db:
                assert db.execute("SELECT state FROM experiment_invocations WHERE call_key='query:003:1:retry4'").fetchone() == ("reserved",)
                assert db.execute("SELECT request_id FROM experiment_query_tickets WHERE call_key='query:003:1:retry4'").fetchone() == (None,)
            refused(lambda: launcher._start_reserved_action(
                root, "query:003:1:retry4", "a" * 64,
                lambda: (_ for _ in ()).throw(ValueError("preflight_unavailable"))),
                "preflight_unavailable")
            with _db(root) as db:
                assert db.execute("SELECT state FROM experiment_invocations WHERE call_key='query:003:1:retry4'").fetchone() == ("reserved",)
            assert launcher._start_reserved_action(
                root, "query:003:1:retry4", "a" * 64, lambda: None)
            assert not launcher._start_reserved_action(
                root, "query:003:1:retry4", "a" * 64, lambda: None)
            assert retry() == {"status": "already_reserved", "call_key": "query:003:1:retry4"}
        refused(lambda: experiment.reserve_manual(
            root, project, "query", 3, 1, recovery=5,
            input_sha256="a" * 64), "invalid_experiment_call")
    with _db(root) as db:
        assert db.execute("SELECT use_state,error,status FROM cases WHERE request_id=?",
                          (rid,)).fetchone() == ("historical", "uncertain_action_item", "pending_review")
        assert db.execute("SELECT request_id FROM experiment_query_tickets WHERE call_key=?",
                          (previous,)).fetchone() == (rid,)

with tempfile.TemporaryDirectory(prefix="qmd-measured-action-failure-") as tmp:
    project = Path(tmp) / "novel"; project.mkdir()
    story = experiment.story_for(project); story.mkdir()
    root = experiment.shared_for(project); root.mkdir(mode=0o700)
    class ActionProcess:
        def __init__(self, _args, *, stdout, **_kwargs):
            self.pid = 123456
            self.returncode = 0
            for item in ({"type": "item.completed", "item": {"type": "mcp_tool_call"}},
                         {"type": "item.completed", "item": {"type": "agent_message",
                             "text": "synthetic answer"}},
                         {"type": "turn.completed", "usage": {}}):
                stdout.write((json.dumps(item) + "\n").encode())
            stdout.flush()
        def poll(self):
            return 0
    with (patch("context_learning.shared._policy", return_value={"maxRssMib": 4096}),
          patch("context_learning.shared.resource_report", return_value={"ready": True}),
          patch.object(launcher, "_query_argv", return_value=["codex", "exec", "--json", "-"]),
          patch.object(launcher.subprocess, "Popen", ActionProcess)):
        try:
            launcher._invoke("synthetic query", story, "query", check=lambda: None,
                             call_key="query:003:1:retry4")
        except ValueError as exc:
            assert str(exc) == "unexpected_action_item"
            assert exc.resources["elapsed_seconds"] >= 0
            assert exc.resources["before"] == {"ready": True}
        else:
            raise AssertionError("unexpected action accepted")

with tempfile.TemporaryDirectory(prefix="qmd-fifteenth-continuation-") as tmp:
    project = Path(tmp) / "novel"; project.mkdir()
    story = experiment.story_for(project); story.mkdir()
    root = experiment.shared_for(project); root.mkdir(mode=0o700)
    folder = root / "attempt-evidence"; folder.mkdir(mode=0o700)
    draft_value = {"prose": "가" * 4929, "setting": "가상 새 배경",
                   "characters": "가상 새 인물", "plot": "가상 새 사건",
                   "temporary": None}
    draft = folder / "writing_015_1.draft.json"
    draft.write_text(json.dumps(draft_value)); draft.chmod(0o600)
    raw = folder / "writing_015_1.raw-response.txt"
    raw.write_bytes(draft.read_bytes()); raw.chmod(0o600)
    receipt = folder / "writing_015_1.json"
    receipt.write_text('{"synthetic":"short-receipt"}'); receipt.chmod(0o600)
    old = {"01_Settings/world.md": "가상의 옛 배경",
           "02_Characters/cast.md": "가상의 옛 인물",
           "03_Plot/arc.md": "가상의 옛 사건"}
    with _db(root) as db:
        for relative, body in old.items():
            path = story / relative
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            path.write_text(body); path.chmod(0o600)
            db.execute("INSERT INTO experiment_generated "
                       "(kind,episode,slot,relative_path,sha256,prior_sha256,recorded_at) "
                       "VALUES (?,?,?,?,?,?,?)",
                       ("writing", 14, 1, relative,
                        hashlib.sha256(body.encode()).hexdigest(), None, 1.0))
        for key, episode, state, detail, sha in (
                ("writing:014:1", 14, "complete", None, "7" * 64),
                ("writing:015:1", 15, "uncertain", "synthetic_narrative_too_short",
                 experiment.WRITING_015_OLD_PROMPT_SHA256)):
            db.execute("INSERT INTO experiment_calls VALUES (?,?,?,?)",
                       (key, "writing", episode, 1.0))
            db.execute("INSERT INTO experiment_invocations VALUES (?,?,?,?,?)",
                       (key, sha, state, None, detail))
    prompt_sha = hashlib.sha256(launcher._continuation_prompt(draft_value, 15).encode()).hexdigest()
    edits = []
    with (patch.object(launcher, "load", return_value=({}, "control-sha")),
          patch.object(scope, "_synthetic_bundle", return_value=(root, project)),
          patch.object(launcher, "policy_for", return_value={"provenanceSha256": "f" * 64}),
          patch.object(launcher, "generator_spec", return_value=({"seed": "a" * 64}, "f" * 64)),
          patch.object(launcher, "approval", return_value={}),
          patch.object(launcher, "check_disk"),
          patch.object(launcher, "_dispatch_edit_hook",
                       side_effect=lambda _story, relative, **_kw: edits.append(relative)),
          patch.object(launcher, "WRITING_015_DRAFT_SHA256",
                       hashlib.sha256(draft.read_bytes()).hexdigest()),
          patch.object(experiment, "load", return_value=({}, "control-sha")),
          patch.object(experiment, "approval", return_value={}),
          patch.object(experiment, "_limits", return_value=(100, None, None, None)),
          patch.object(experiment, "check_disk"),
          patch.object(experiment, "WRITING_015_OLD_PROMPT_SHA256", "a" * 64),
          patch.object(experiment, "WRITING_015_CONTINUATION_PROMPT_SHA256", prompt_sha),
          patch.object(experiment, "WRITING_015_RECEIPT_SHA256",
                       hashlib.sha256(receipt.read_bytes()).hexdigest()),
          patch.object(experiment, "WRITING_015_DRAFT_SHA256",
                       hashlib.sha256(draft.read_bytes()).hexdigest())):
        with _db(root) as db:
            db.execute("UPDATE experiment_invocations SET input_sha256=? WHERE call_key='writing:015:1'",
                       ("a" * 64,))
        attempt = lambda sha: experiment.reserve_manual(
            root, project, "writing", 15, 1, recovery=1,
            input_sha256=sha,
            prior_docs_sha={relative: hashlib.sha256(body.encode()).hexdigest()
                            for relative, body in old.items()})
        refused(lambda: attempt("a" * 64), "experiment_writing_same_prompt_short")
        raw.write_text("changed")
        refused(lambda: attempt(prompt_sha), "experiment_recovery_draft_changed")
        raw.write_bytes(draft.read_bytes())
        receipt.write_text("changed")
        refused(lambda: attempt(prompt_sha), "experiment_recovery_receipt_changed")
        receipt.write_text('{"synthetic":"short-receipt"}')
        assert launcher.launch_one(root, project, "writing", 15, 1, recovery=1,
                                   invoke=lambda *_a, **_kw: json.dumps(
                                       {"continuation": "나" * 200})) == {
                                           "status": "complete", "call_key": "writing:015:1:retry1",
                                           "worker": None}
        assert (story / "04_Manuscript/ep015.md").read_text() == (
            draft_value["prose"] + "\n\n" + "나" * 200)
        assert [(story / relative).read_text() for relative in old] == [
            draft_value["setting"], draft_value["characters"], draft_value["plot"]]
        assert len(edits) == 4
        assert (folder / "writing_015_1_retry1.complete.json").is_file()
        assert (folder / "writing_015_1_retry1.output-backup.json").is_file()
        assert launcher.launch_one(root, project, "writing", 15, 1, recovery=1,
                                   invoke=lambda *_a, **_kw: (_ for _ in ()).throw(
                                       AssertionError("duplicate external call")))["status"] == "already_reserved"

with tempfile.TemporaryDirectory(prefix="qmd-fifteenth-continuation-schema-") as tmp:
    project = Path(tmp) / "novel"; project.mkdir()
    story = experiment.story_for(project); story.mkdir()
    root = experiment.shared_for(project); root.mkdir(mode=0o700)
    class ContinuationSchemaProcess:
        def __init__(self, args, *, stdout, **_kwargs):
            assert args[-1] == "-" and "--output-schema" in args
            schema = json.loads(Path(args[args.index("--output-schema") + 1]).read_text())
            assert schema == {"type": "object", "properties": {
                "continuation": {"type": "string"}},
                "required": ["continuation"], "additionalProperties": False}
            self.pid = 123456
            self.returncode = 0
            for event in ({"type": "item.completed", "item": {
                    "type": "agent_message", "text": '{"continuation":"가상 후속 장면"}'}},
                          {"type": "turn.completed", "usage": {}}):
                stdout.write((json.dumps(event) + "\n").encode())
            stdout.flush()
        def poll(self):
            return 0
    with (patch("context_learning.shared._policy", return_value={"maxRssMib": 4096}),
          patch("context_learning.shared.resource_report", return_value={"ready": True}),
          patch.object(launcher, "_query_argv", return_value=["codex", "exec", "--json", "-"]),
          patch.object(launcher.subprocess, "Popen", ContinuationSchemaProcess)):
        result = launcher._invoke("synthetic continuation", story, "writing",
                                  check=lambda: None, call_key="writing:015:1:retry1")
    assert json.loads(result["response"]) == {"continuation": "가상 후속 장면"}
    assert (root / "attempt-evidence/writing_015_1_retry1.raw-response.txt").is_file()

with tempfile.TemporaryDirectory(prefix="qmd-continuation-rejected-root-") as tmp:
    approved = Path(tmp) / "approved-novel"; approved.mkdir()
    foreign = Path(tmp) / "other-novel"; foreign.mkdir()
    foreign_root = foreign / experiment.SHARED_NAME; foreign_root.mkdir(mode=0o700)
    with patch.object(scope, "NOVEL_ROOT", approved):
        refused(lambda: launcher.launch_one(
            foreign_root, foreign, "writing", 15, 1, recovery=1,
            invoke=lambda *_a, **_k: "unexpected"), "outside_synthetic_bundle")
    assert not (foreign_root / "continuation-reconcile.lock").exists()

for completed_files in range(5):
    with tempfile.TemporaryDirectory(prefix="qmd-continuation-crash-reconcile-") as tmp:
        project = Path(tmp) / "novel"; project.mkdir()
        (project / ".auto-context/wiki").mkdir(parents=True)
        story = experiment.story_for(project); story.mkdir()
        root = experiment.shared_for(project); root.mkdir(mode=0o700)
        folder = root / "attempt-evidence"; folder.mkdir(mode=0o700)
        draft_value = {"prose": "가" * 4929, "setting": "새 배경",
                       "characters": "새 인물", "plot": "새 사건", "temporary": None}
        draft = folder / "writing_015_1.draft.json"
        draft.write_text(json.dumps(draft_value)); draft.chmod(0o600)
        raw = folder / "writing_015_1.raw-response.txt"
        raw.write_bytes(draft.read_bytes()); raw.chmod(0o600)
        first = folder / "writing_015_1.json"
        first.write_text('{"synthetic":"receipt"}'); first.chmod(0o600)
        prompt_sha = hashlib.sha256(launcher._continuation_prompt(draft_value, 15).encode()).hexdigest()
        retry = folder / "writing_015_1_retry1.json"
        retry.write_text(json.dumps({"callKey": "writing:015:1:retry1",
            "inputSha256": prompt_sha, "turnCompleted": True, "turnFailed": False,
            "finalMessagePresent": True, "exitCode": 0})); retry.chmod(0o600)
        response = json.dumps({"continuation": "나" * 200})
        retry_raw = folder / "writing_015_1_retry1.raw-response.txt"
        retry_raw.write_text(response); retry_raw.chmod(0o600)
        combined = dict(draft_value)
        combined["prose"] += "\n\n" + "나" * 200
        complete = folder / "writing_015_1_retry1.complete.json"
        complete.write_text(json.dumps({"schema": "qmd-synthetic-continuation-v1",
            "callKey": "writing:015:1:retry1", "responseSha256": hashlib.sha256(response.encode()).hexdigest(),
            "response": response, "combined": combined})); complete.chmod(0o600)
        outputs = [("01_Settings/world.md", "옛 배경", combined["setting"]),
                   ("02_Characters/cast.md", "옛 인물", combined["characters"]),
                   ("03_Plot/arc.md", "옛 사건", combined["plot"]),
                   ("04_Manuscript/ep015.md", None, combined["prose"])]
        entries = []
        with _db(root) as db:
            db.execute("INSERT INTO experiment_calls VALUES (?,?,?,?)",
                       ("writing:015:1:retry1", "writing", 15, 1.0))
            db.execute("INSERT INTO experiment_invocations VALUES (?,?,?,?,?)",
                       ("writing:015:1:retry1", prompt_sha, "running", None, None))
            for index, (relative, old, new) in enumerate(outputs):
                path = story / relative; path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                if index < 3:
                    path.write_text(old); path.chmod(0o600)
                    db.execute("INSERT INTO experiment_generated "
                               "(kind,episode,slot,relative_path,sha256,prior_sha256,recorded_at) "
                               "VALUES (?,?,?,?,?,?,?)",
                               ("writing", 14, 1, relative, hashlib.sha256(old.encode()).hexdigest(), None, 1.0))
                entries.append({"relative": relative,
                                "previousSha256": hashlib.sha256(old.encode()).hexdigest() if old else None,
                                "previousBody": old,
                                "newSha256": hashlib.sha256(new.encode()).hexdigest()})
                if index < completed_files:
                    path.write_text(new); path.chmod(0o600)
        backup = folder / "writing_015_1_retry1.output-backup.json"
        backup.write_text(json.dumps({"schema": "qmd-synthetic-output-backup-v1",
                                      "callKey": "writing:015:1:retry1", "entries": entries}))
        backup.chmod(0o600)
        index_path = project / "synthetic-index.sqlite3"
        with sqlite3.connect(index_path) as index:
            index.execute("CREATE TABLE documents (collection TEXT,path TEXT,hash TEXT,active INTEGER)")
        edits = []
        with (patch.object(launcher, "load", return_value=({}, "control-sha")),
              patch.object(scope, "_synthetic_bundle", return_value=(root, project)),
              patch.object(launcher, "policy_for", return_value={}),
              patch.object(launcher, "approval", return_value={}),
              patch("qmd_route.project_paths", return_value={
                  "selected": True, "INDEX_PATH": str(index_path)}),
              patch.object(launcher, "_dispatch_edit_hook",
                           side_effect=lambda _story, relative: edits.append(relative)),
              patch.object(launcher, "WRITING_015_DRAFT_SHA256",
                           hashlib.sha256(draft.read_bytes()).hexdigest()),
              patch.object(experiment, "WRITING_015_DRAFT_SHA256",
                           hashlib.sha256(draft.read_bytes()).hexdigest()),
              patch.object(experiment, "WRITING_015_RECEIPT_SHA256",
                           hashlib.sha256(first.read_bytes()).hexdigest()),
              patch.object(experiment, "WRITING_015_CONTINUATION_PROMPT_SHA256", prompt_sha)):
            if completed_files == 2:
                (story / outputs[2][0]).write_text("conflicting change")
                refused(lambda: launcher.reconcile_continuation(root, project, 15),
                        "synthetic_continuation_output_conflict")
                (story / outputs[2][0]).write_text(outputs[2][1])
                with _db(root) as db:
                    assert db.execute("SELECT COUNT(*) FROM experiment_generated WHERE "
                                      "kind='writing' AND episode=15").fetchone() == (0,)
                retry_raw.write_text("changed")
                refused(lambda: launcher.reconcile_continuation(root, project, 15),
                        "synthetic_continuation_response_changed")
                retry_raw.write_text(response)
                rogue = story / "04_Manuscript/rogue.md"
                rogue.write_text("unproven synthetic extra")
                refused(lambda: launcher.reconcile_continuation(root, project, 15),
                        "synthetic_collection_unproven")
                rogue.unlink()
                with sqlite3.connect(index_path) as index:
                    index.execute("INSERT INTO documents VALUES (?,?,?,1)",
                                  (next(iter(scope.COLLECTION_PATHS)), "world.md", "f" * 64))
                refused(lambda: launcher.reconcile_continuation(root, project, 15),
                        "synthetic_index_contains_unproven_document")
                with sqlite3.connect(index_path) as index:
                    index.execute("DELETE FROM documents")
            assert launcher.reconcile_continuation(root, project, 15) == {
                "status": "reconciled", "call_key": "writing:015:1:retry1"}
            assert len(edits) == 4
            assert launcher.reconcile_continuation(root, project, 15)["status"] == "already_complete"
        assert [(story / relative).read_text() for relative, _, _ in outputs] == [
            new for _, _, new in outputs]
        with _db(root) as db:
            assert db.execute("SELECT COUNT(*) FROM experiment_generated WHERE "
                              "kind='writing' AND episode=15").fetchone() == (4,)
            assert db.execute("SELECT state,output_sha256 FROM experiment_invocations "
                              "WHERE call_key='writing:015:1:retry1'").fetchone() == (
                                  "complete", hashlib.sha256(response.encode()).hexdigest())

print(json.dumps({"status": "ok"}))

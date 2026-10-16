"""Durable scope and invocation ledger for the reviewed synthetic experiment.

This ledger only restricts work. It never grants external transport: the
synthetic teacher still requires its exact source and owner approval checks.
The synthetic launcher reserves in SQLite immediately before every Codex call.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time

from .dataset import read_private_json
from .safe_source import snapshot_beneath
from .store import canonical

CONTROL_NAME = "synthetic-experiment.json"
EXPERIMENT = "qmd-fantasy-100-20261008"
PLANNED_BUNDLE_NAME = "마지막 마법의 도서관"
SHARED_NAME = ".qmd-fantasy-shared-20261008"
PRIVATE_NAME = ".qmd-fantasy-private-20261008"
FINAL_IDS = [f"ep{episode:03d}-q2" for episode in range(81, 101)]
TOTAL_LIMIT = 780  # Historical bounded-policy compatibility.
GOLD_LIMIT = 320
TRAIN_LIMIT = 14
DISK_LIMIT = 2 * 1024 ** 3
LOG_LIMIT = 20 * 1024 ** 2
KIND_LIMITS = {"planning": 6, "writing": 100, "wiki_create": 100,
               "wiki_review": 100, "query": 160, "gold": GOLD_LIMIT}
PILOT_LIMITS = {"episode": 5, "total": 45, "gold": 20, "train": 1}
# One historical provider-error attempt preceded the verified three-document
# plan. Its old input cannot be replayed; only this exact newly pinned input
# may consume the explicit first retry. The old row remains in the ledger.
PREPLAN_WRITING_INPUT_SHA256 = "a8eeacbe7f464c7bf4bbb0ec2252b4221ab21903b99b273054669f62d3ce71aa"
POSTPLAN_WRITING_INPUT_SHA256 = "f69a377bfc5517dc03552c4219bac361f55403d4d74cfc75c56fbeb6099f1530"
# One captured q3 retry subsequently attempted an unexpected provider action.
# Only this private redacted receipt may be retired before one extra retry.
ACTION_RECOVERY_RECEIPT_SHA256 = "4b2d92525acfc0db2ea3a9b152cbaf2a8fb02b34a057e7d282502c85e975af50"
# Exact already-executed q5 receipt. This permits binding its persisted hook
# capture without issuing another external query invocation.
Q5_CAPTURE_RECEIPT_SHA256 = "889b523218090de0e2ed4bb4151872f6c4d5501f837f017c205177a66641caad"
Q5_CAPTURE_REQUEST_ID = "6da7f1db-0f07-4010-a25b-363e18589315"
# Two episode-6 Luna responses completed but missed the reviewed manuscript
# minimum. They produced no story file. Permit one changed length-allocation
# prompt only when the second attempt's immutable private receipt still
# matches; keep both counted attempts and their output hashes as history.
WRITING_006_OLD_PROMPT_SHA256 = "144c644813d9c6a224efdce49c58ac8c20b32ce6ce0221ad9abf57ab95cf42be"
WRITING_006_REVISED_PROMPT_SHA256 = "2f0571f941f3b577fb2c9b5a7d090653f112aa2c2ecba23bf2fcf724989e0c7a"
WRITING_006_RETRY1_RECEIPT_SHA256 = "e0130933f87ca82682922202081950a397aa510b163316e538417a7232c87d00"
# Episode 8's first length-guided response was a valid, retained synthetic
# draft of 4,782 non-whitespace prose characters. Permit one counted
# continuation only for these exact private draft and receipt bytes.
WRITING_008_OLD_PROMPT_SHA256 = "1a0329a92757183fc3bcda19ad9fd817cc4109df3b8d1dd71f6c65f5ba977aad"
WRITING_008_DRAFT_SHA256 = "6ddf26cd505621028beffec4167d0a9d68a82d092dd2772f1984bd0453adc314"
WRITING_008_RECEIPT_SHA256 = "7edf1a28c4d37aa328fdbde83b2eefe85c4f2ad845f546983a8a2c30d9d43877"
WRITING_008_CONTINUATION_PROMPT_SHA256 = "7356d136dd571b8da4be688775397e6639066e6c8c9ffc094604206a9ddcca89"
# Episode 12 returned a completed but malformed JSON message. The response
# was not retained by v38, so only one new schema-constrained prompt may run
# after this exact receipt; the failed call remains counted and uncertain.
WRITING_012_OLD_PROMPT_SHA256 = "5d1637d896f1e849c72b2d8df349ae75af21e79a2f06f6346f454fe627761ad3"
WRITING_012_RECEIPT_SHA256 = "367aef03552dfc624a0c66dc3b8e140d2e283debcd0b9cbd4f5b67350c387456"
WRITING_012_OLD_DETAIL = "Invalid control character at: line 1 column 440 (char 439)"
WRITING_012_REVISED_PROMPT_SHA256 = "f64c6ddb5a1b4c90ab15d207caef6b1626378c9cb4f643904e80704f3f805eff"
# Episode 15's schema-valid response was 4,929 non-whitespace characters.
# Preserve its exact private bytes and permit one counted continuation only.
WRITING_015_OLD_PROMPT_SHA256 = "f15b2d62463545a8cbc0bec325e01c94aa8140066940b808d6f4de23f8eb9239"
WRITING_015_DRAFT_SHA256 = "a037a67a43759862124bbac95efdb01eb729bd7b1f7186a95a6e85f171842908"
WRITING_015_RECEIPT_SHA256 = "dc1c1b96ce9b57c706c38cf6d9355cf9071302438c4410fb672bc03cf7af18bb"
WRITING_015_CONTINUATION_PROMPT_SHA256 = "7ba4e6466da36cd9f76510b0ddc486208114f2bd10a0cbb128b9c073a3d542fe"
GENERATOR_NAME = "synthetic-generator.json"


def project_for(root):
    return Path(root).parent


def story_for(project):
    return Path(project) / PLANNED_BUNDLE_NAME


def shared_for(project):
    return Path(project) / SHARED_NAME


def private_for(project):
    return Path(project) / PRIVATE_NAME


def load(root, project):
    """Return the exact experimental control, or None for ordinary mode."""
    root, project = Path(root).resolve(), Path(project).resolve()
    path = root / CONTROL_NAME
    if not path.exists():
        if root.name == SHARED_NAME:
            raise ValueError("missing_synthetic_experiment_control")
        return None
    try:
        value = read_private_json(path)
        raw = snapshot_beneath(root, CONTROL_NAME)
    except (OSError, ValueError) as exc:
        raise ValueError("invalid_synthetic_experiment_control") from exc
    if canonical(value).encode() != raw:
        raise ValueError("synthetic_experiment_control_changed")
    expected = {"schema": "qmd-synthetic-experiment-v1", "id": EXPERIMENT,
                "projectRoot": str(project), "sharedRoot": str(root),
                "maxTotalCalls": TOTAL_LIMIT, "maxGoldCalls": GOLD_LIMIT,
                "maxTrainCycles": TRAIN_LIMIT, "maxDiskBytes": DISK_LIMIT,
                "maxLogBytes": LOG_LIMIT, "finalCaseIds": FINAL_IDS}
    from . import synthetic_scope
    if not isinstance(value, dict):
        raise ValueError("invalid_synthetic_experiment_control")
    uncapped = dict(expected, maxTotalCalls=None, maxGoldCalls=None,
                    maxTrainCycles=None)
    if (value not in (expected, uncapped) or root != shared_for(project)
            or project != synthetic_scope.NOVEL_ROOT.resolve(strict=True)
            or not story_for(project).is_dir()):
        raise ValueError("invalid_synthetic_experiment_control")
    return value, hashlib.sha256(raw).hexdigest()


def approval(root, project, control, control_sha, *, policy=None):
    """Owner-held immutable generator and bounded experiment approval."""
    from . import synthetic_scope
    path = Path(synthetic_scope.APPROVAL_FILE)
    parent = path.parent
    if (parent.is_symlink() or not parent.is_dir()
            or parent == project or project in parent.parents
            or parent == root or root in parent.parents):
        raise ValueError("missing_parent_experiment_approval")
    info = parent.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("unsafe_parent_experiment_approval")
    try:
        value = read_private_json(path)
    except (OSError, ValueError) as exc:
        raise ValueError("missing_parent_experiment_approval") from exc
    keys = {"schema", "enabled", "projectRoot", "sharedRoot",
            "generatorSpecSha256", "markerSha256", "launcherSha256",
            "sharedPolicySha256", "teacherPolicySha256", "receiver",
            "purpose", "model", "writingModel", "experimentId", "controlSha256",
            "approvedThroughEpisode", "approvedTotalCalls",
            "approvedGoldCalls", "approvedTrainCycles"}
    if (not isinstance(value, dict) or set(value) != keys
            or value["schema"] != "qmd-synthetic-parent-approval-v2"
            or value["enabled"] is not True
            or value["projectRoot"] != str(project)
            or value["sharedRoot"] != str(root)
            or value["experimentId"] != control["id"]
            or value["controlSha256"] != control_sha
            or value["receiver"] != "codex-cli"
            or value["purpose"] != "selection-gold"
            or value["model"] != "gpt-6.1-sol"
            or value["writingModel"] != "gpt-6-luna"
            or not isinstance(value["generatorSpecSha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", value["generatorSpecSha256"])
            or not isinstance(value["markerSha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", value["markerSha256"])
            or any(not isinstance(value[name], str) or not re.fullmatch(r"[0-9a-f]{64}", value[name])
                   for name in ("launcherSha256", "sharedPolicySha256", "teacherPolicySha256"))):
        raise ValueError("parent_experiment_approval_mismatch")
    episode = value["approvedThroughEpisode"]
    uncapped = control["maxTotalCalls"] is None
    counts = (value["approvedTotalCalls"], value["approvedGoldCalls"],
              value["approvedTrainCycles"])
    if (type(episode) is not int or episode not in (5, 100)
            or (uncapped and counts != (None, None, None))
            or (not uncapped and (
                any(type(count) is not int or count < 0 or count > limit
                    for count, limit in zip(counts, (TOTAL_LIMIT, GOLD_LIMIT, TRAIN_LIMIT)))
                or (episode == 5 and any(count > limit for count, limit in
                                         zip(counts, (45, 20, 1))))
                or (episode == 100 and counts != (TOTAL_LIMIT, GOLD_LIMIT, TRAIN_LIMIT))))):
        raise ValueError("invalid_experiment_batch_approval")
    if policy is not None and (value["generatorSpecSha256"] != policy["provenanceSha256"]
                               or value["markerSha256"] != policy["markerSha256"]):
        raise ValueError("parent_experiment_approval_mismatch")
    if value["generatorSpecSha256"] != generator_spec(project)[1]:
        raise ValueError("parent_experiment_approval_mismatch")
    from . import synthetic_launcher
    pins = {"launcherSha256": hashlib.sha256(Path(synthetic_launcher.__file__).read_bytes()).hexdigest(),
            "sharedPolicySha256": hashlib.sha256(snapshot_beneath(root, "shared-policy.json")).hexdigest(),
            "teacherPolicySha256": hashlib.sha256(snapshot_beneath(root, "synthetic-teacher-policy.json")).hexdigest()}
    if any(value[key] != expected for key, expected in pins.items()):
        raise ValueError("parent_experiment_approval_mismatch")
    return value


def generator_spec(project):
    """Pin the deterministic prompt generator, its seed, and project identity."""
    from .dataset import read_private_json
    from .synthetic_generator import source_sha256
    project = Path(project).resolve(strict=True)
    story = story_for(project)
    path = story / GENERATOR_NAME
    value = read_private_json(path)
    raw = snapshot_beneath(story, GENERATOR_NAME)
    if (canonical(value).encode() != raw or not isinstance(value, dict)
            or set(value) != {"schema", "projectRoot", "nonce", "seed",
                                  "generatorSha256", "model"}
            or value["schema"] != "qmd-synthetic-generator-v1"
            or value["projectRoot"] != str(project)
            or value["model"] != "gpt-6-luna"
            or not isinstance(value["nonce"], str)
            or not re.fullmatch(r"[0-9a-f]{48}", value["nonce"])
            or not isinstance(value["seed"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", value["seed"])
            or value["generatorSha256"] != source_sha256()):
        raise ValueError("synthetic_generator_mismatch")
    return value, hashlib.sha256(raw).hexdigest()


def _meta(db, key, fallback=0):
    row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else fallback


def _advance(db, grant):
    old = _meta(db, "experiment_approved_episode")
    new = 5 if grant["approvedThroughEpisode"] == 5 or not _pilot_verified(db) else 100
    if old > new:
        raise ValueError("experiment_batch_revoked")
    if new != old:
        db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)",
                   ("experiment_approved_episode", canonical(new)))
    return new


def _pilot_verified(db):
    counts = dict(db.execute("SELECT kind,COUNT(*) FROM experiment_invocations JOIN experiment_calls USING(call_key) WHERE episode<=5 AND state='complete' GROUP BY kind"))
    return (all(counts.get(kind, 0) >= 5 for kind in ("writing", "wiki_create", "wiki_review"))
            and counts.get("query", 0) >= 10
            and db.execute("SELECT COUNT(*) FROM experiment_calls WHERE kind='gold' AND episode<=5").fetchone()[0] >= 20
            and db.execute("SELECT COUNT(*) FROM experiment_query_tickets JOIN cases USING(request_id) WHERE episode<=5 AND cases.status='gold' AND cases.use_state='active'").fetchone()[0] >= 10
            and db.execute("SELECT COUNT(*) FROM cycles JOIN experiment_cycles USING(cycle_id) WHERE cycles.status='complete'").fetchone()[0] >= 1)


def _limits(db, grant):
    episode = _advance(db, grant)
    if grant["approvedTotalCalls"] is None:
        return episode, None, None, None
    return (episode,
            min(grant["approvedTotalCalls"], PILOT_LIMITS["total"] if episode == 5 else TOTAL_LIMIT),
            min(grant["approvedGoldCalls"], PILOT_LIMITS["gold"] if episode == 5 else GOLD_LIMIT),
            min(grant["approvedTrainCycles"], PILOT_LIMITS["train"] if episode == 5 else TRAIN_LIMIT))


def disk_bytes(root):
    total = 0
    project = project_for(root)
    story = story_for(project)
    # Count experiment-owned output and state. Older inactive QMD generations
    # from preparation remain preserved under story/.auto-context but are not
    # produced by this experiment and must not exhaust its output ceiling.
    from .synthetic_scope import COLLECTION_PATHS
    for name in (".qmd-synthetic-project.json", GENERATOR_NAME):
        path = story / name
        if path.exists():
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("experiment_disk_symlink")
            total += info.st_size
    subtrees = [Path(root), private_for(project)]
    for relative in COLLECTION_PATHS.values():
        path = story / relative
        if path.exists():
            subtrees.append(path)
    for subtree in subtrees:
        if not subtree.is_dir() or subtree.is_symlink():
            raise ValueError("experiment_disk_tree_invalid")
        for base, dirs, files in os.walk(subtree, followlinks=False):
            for name in dirs + files:
                path = Path(base) / name
                info = path.lstat()
                if stat.S_ISLNK(info.st_mode):
                    raise ValueError("experiment_disk_symlink")
                if stat.S_ISREG(info.st_mode):
                    total += info.st_size
    return total


def check_disk(root, control):
    if disk_bytes(root) >= control["maxDiskBytes"]:
        raise ValueError("experiment_disk_budget_exhausted")
    log = Path(root) / "worker.log"
    if log.exists() and (log.is_symlink() or log.stat().st_size >= control["maxLogBytes"]):
        raise ValueError("experiment_log_budget_exhausted")


def _counts(db):
    return dict(db.execute("SELECT kind,COUNT(*) FROM experiment_calls GROUP BY kind"))


def _episode(request_id):
    match = re.fullmatch(r"ep(\d{3})-q[12]", request_id)
    if not match or not 1 <= int(match.group(1)) <= 100:
        raise ValueError("invalid_experiment_request_id")
    return int(match.group(1))


def mapped_episode(db, request_id):
    row = db.execute("SELECT episode,slot FROM experiment_query_tickets WHERE request_id=? AND input_sha256 IS NOT NULL",
                     (request_id,)).fetchone()
    if row is None:
        # A completed host edit has its own one-use provenance, not a forged
        # experiment query invocation. Slot zero can never reserve sealed final.
        row = db.execute("SELECT episode,0 FROM edit_tickets WHERE request_id=? AND input_sha256 IS NOT NULL",
                         (request_id,)).fetchone()
    if row is None:
        raise ValueError("experiment_capture_not_mapped")
    return row


def bind_capture(db, root, project, control, control_sha, sample, bodies,
                 input_sha256, *, commit, recovery_call_key=None):
    """Claim exactly one pre-launched query ticket in the enqueue transaction."""
    grant = approval(root, project, control, control_sha)
    if recovery_call_key is not None:
        if (recovery_call_key != "query:005:1"
                or sample["request_id"] != Q5_CAPTURE_REQUEST_ID
                or hashlib.sha256(snapshot_beneath(
                    root, "attempt-evidence/query_005_1.json")).hexdigest()
                != Q5_CAPTURE_RECEIPT_SHA256):
            raise ValueError("experiment_recovery_receipt_invalid")
    effective, _, _, _ = _limits(db, grant)
    prompt_sha = sample["prompt"]["sha256"]
    tickets = db.execute(
        "SELECT t.call_key,t.episode,t.slot,t.source_id,t.source_sha256 "
        "FROM experiment_query_tickets t JOIN experiment_invocations i USING(call_key) "
        "WHERE t.prompt_sha256=? AND t.request_id IS NULL AND "
        "(i.state IN ('running','complete') OR (i.state='uncertain' AND "
        "i.detail='query_capture_missing' AND t.call_key=?))",
        (prompt_sha, recovery_call_key or "")).fetchall()
    if len(tickets) != 1:
        raise ValueError("experiment_query_ticket_missing_or_ambiguous")
    call_key, episode, slot, source_id, source_sha = tickets[0]
    if episode > effective or sample["host"] != "codex":
        raise ValueError("experiment_batch_not_approved")
    # The intended target can be absent from real retrieval. Verify it was
    # generated, reviewed, and indexed, but never add it to the captured pool.
    from .synthetic_launcher import _indexed_revision
    from .synthetic_scope import generated_path
    target_path = generated_path(source_id)
    review_kind = "wiki_review" if slot == 1 else "wiki_review_final"
    target = db.execute("SELECT sha256 FROM experiment_generated WHERE "
                        "kind=? AND relative_path=? ORDER BY entry_id DESC LIMIT 1",
                        (review_kind, str(target_path))).fetchone()
    if target != (source_sha,) or not _indexed_revision(story_for(project), source_id, source_sha):
        raise ValueError("experiment_target_source_unverified")
    from .compact_input import derive
    for candidate in sample["candidates"]:
        cid, revision = candidate["candidate_id"], candidate["revision_sha256"]
        if not isinstance(cid, str):
            raise ValueError("synthetic_source_not_generated")
        relative = generated_path(cid)
        row = db.execute("SELECT sha256 FROM experiment_generated WHERE relative_path=? ORDER BY entry_id DESC LIMIT 1",
                         (str(relative),)).fetchone()
        if row is None or row[0] != revision:
            raise ValueError("synthetic_source_not_generated")
        source = snapshot_beneath(story_for(project), relative)
        if hashlib.sha256(source).hexdigest() != revision:
            raise ValueError("synthetic_source_changed")
        if bodies.get(cid) != derive(source.decode("utf8"), revision)["body_text"]:
            raise ValueError("synthetic_capture_not_from_source")
    candidate_sha = hashlib.sha256(canonical([
        [c["candidate_id"], c["revision_sha256"]] for c in sample["candidates"]]).encode()).hexdigest()
    if commit:
        changed = db.execute("UPDATE experiment_query_tickets SET request_id=?,input_sha256=?,candidate_sha256=? WHERE call_key=? AND request_id IS NULL",
                             (sample["request_id"], input_sha256, candidate_sha, call_key)).rowcount
        if changed != 1:
            raise ValueError("experiment_query_ticket_reused")
    return episode, slot, source_id, source_sha


def reserve_gold(db, root, project, control, control_sha, policy, request_id, phase):
    grant = approval(root, project, control, control_sha, policy=policy)
    episode, _ = mapped_episode(db, request_id)
    if phase not in ("first_running", "second_running"):
        raise ValueError("invalid_experiment_gold_phase")
    effective, total_limit, gold_limit, _ = _limits(db, grant)
    check_disk(root, control)
    if episode > effective:
        raise ValueError("experiment_batch_not_approved")
    key = f"gold:{request_id}:{phase}"
    if db.execute("SELECT 1 FROM experiment_calls WHERE call_key=?", (key,)).fetchone():
        raise ValueError("experiment_call_already_reserved")
    counts = _counts(db)
    total, gold = sum(counts.values()), counts.get("gold", 0)
    if ((total_limit is not None and total >= total_limit)
            or (gold_limit is not None and gold >= gold_limit)):
        raise ValueError("experiment_call_budget_exhausted")
    db.execute("INSERT INTO experiment_calls VALUES (?,?,?,?)",
               (key, "gold", episode, time.time()))


def reserve_train(db, root, project, cycle_id):
    loaded = load(root, project)
    if loaded is None:
        if _meta(db, "experiment_approved_episode"):
            raise ValueError("missing_synthetic_experiment_control")
        return
    control, sha = loaded
    grant = approval(root, project, control, sha)
    _, _, _, train_limit = _limits(db, grant)
    check_disk(root, control)
    if db.execute("SELECT 1 FROM experiment_final WHERE id=1").fetchone():
        raise ValueError("experiment_model_frozen_for_final")
    final_rows = db.execute(
        "SELECT c.request_id,c.split,q.episode,q.slot,e.episode "
        "FROM cases c LEFT JOIN experiment_query_tickets q ON "
        "q.request_id=c.request_id LEFT JOIN edit_tickets e ON "
        "e.request_id=c.request_id AND e.project_root=c.project_root "
        "AND e.input_sha256=c.input_sha256 WHERE c.project_root=?",
        (str(project),)).fetchall()
    if any((q_episode is None) == (edit_episode is None) or
           (((q_episode >= 81 and slot == 2) if q_episode is not None else False)
            != (split == "final"))
           for _, split, q_episode, slot, edit_episode in final_rows):
        raise ValueError("experiment_final_reserve_changed")
    from .shared import _query_family_keys
    gold_rows = db.execute("SELECT project_key,request_id,input_sha256,sample,bodies,gold,split,project_root,corpus_fingerprint,family_keys,trained_cycle FROM cases WHERE project_root=? AND status='gold' AND use_state='active'",
                           (str(project),)).fetchall()
    final_keys = set().union(*(_query_family_keys(row) for row in gold_rows if row[6] == "final"))
    iterative_keys = set().union(*(_query_family_keys(row) for row in gold_rows if row[6] != "final"))
    if final_keys & iterative_keys:
        raise ValueError("experiment_final_family_overlap")
    existing = db.execute("SELECT COUNT(*) FROM experiment_cycles").fetchone()[0]
    if (grant["approvedTrainCycles"] is not None
            and existing > grant["approvedTrainCycles"]):
        raise ValueError("experiment_train_approval_revoked")
    if db.execute("SELECT 1 FROM experiment_cycles WHERE cycle_id=?", (cycle_id,)).fetchone():
        return
    if train_limit is not None and existing >= train_limit:
        raise ValueError("experiment_train_budget_exhausted")
    db.execute("INSERT INTO experiment_cycles VALUES (?,?)",
               (cycle_id, time.time()))


def reserve_manual(root, project, kind, episode, slot=1, *, recovery=0,
                   input_sha256=None, prior_docs_sha=None,
                   source_id=None, source_sha256=None):
    loaded = load(root, project)
    if loaded is None:
        raise ValueError("missing_synthetic_experiment_control")
    control, sha = loaded
    grant = approval(root, project, control, sha)
    from .synthetic_scope import WIKI_COLLECTION
    action_recovery = (kind == "query" and episode == 3 and slot == 1
                       and recovery == 4)
    if (kind not in ("planning", "writing", "wiki_create", "wiki_review", "query")
            or type(episode) is not int or not 1 <= episode <= 100
            or type(slot) is not int or not 1 <= slot <= (3 if kind == "planning" else 2 if kind == "query" else 1)
            or (kind == "planning" and episode != 1)
            or type(recovery) is not int or not 0 <= recovery <= (4 if action_recovery else 3)):
        raise ValueError("invalid_experiment_call")
    from .shared import _db
    with _db(root) as db:
        effective, total_limit, _, _ = _limits(db, grant)
        check_disk(root, control)
        if episode > effective:
            raise ValueError("experiment_batch_not_approved")
        base = f"{kind}:{episode:03d}:{slot}"
        key = base if not recovery else f"{base}:retry{recovery}"
        if action_recovery and (source_id != f"{WIKI_COLLECTION}/ep003-main.md"
                                or not isinstance(source_sha256, str)
                                or not re.fullmatch(r"[0-9a-f]{64}", source_sha256)):
            raise ValueError("experiment_recovery_source_invalid")
        if db.execute("SELECT 1 FROM experiment_calls WHERE call_key=?", (key,)).fetchone():
            if action_recovery:
                existing = db.execute(
                    "SELECT i.input_sha256,i.state,t.episode,t.slot,"
                    "t.prompt_sha256,t.source_id,t.source_sha256,t.request_id "
                    "FROM experiment_invocations i JOIN experiment_query_tickets t "
                    "USING(call_key) WHERE i.call_key=?", (key,)).fetchone()
                if existing == (input_sha256, "reserved", episode, slot,
                                input_sha256, source_id, source_sha256, None):
                    receipt = snapshot_beneath(
                        root, "attempt-evidence/query_003_1_retry3.json")
                    old = db.execute(
                        "SELECT i.state,i.output_sha256,i.input_sha256,i.detail,"
                        "t.request_id,t.source_id,t.source_sha256 "
                        "FROM experiment_invocations i JOIN experiment_query_tickets t "
                        "USING(call_key) WHERE i.call_key=?",
                        (f"{base}:retry3",)).fetchone()
                    case = db.execute(
                        "SELECT status,use_state,error,trained_cycle,first,second,gold "
                        "FROM cases WHERE request_id=? AND project_root=?",
                        (old[4], str(project))).fetchone() if old else None
                    if (old and old[4] is not None
                            and hashlib.sha256(receipt).hexdigest() == ACTION_RECOVERY_RECEIPT_SHA256
                            and old == ("uncertain", None, input_sha256,
                                        "unexpected_action_item", old[4],
                                        source_id, source_sha256)
                            and case in (("queued", "historical", "uncertain_action_item",
                                          None, None, None, None),
                                         ("pending_review", "historical",
                                          "uncertain_action_item", None, None,
                                          None, None))
                            and not db.execute(
                                "SELECT 1 FROM experiment_calls WHERE call_key LIKE ? LIMIT 1",
                                (f"gold:{old[4]}:%",)).fetchone()):
                        return {"status": "resumable", "call_key": key}
            return {"status": "already_reserved", "call_key": key}
        if kind == "planning" and slot > 1:
            required = ({"01_Settings/world.md": 1} if slot == 2 else
                        {"01_Settings/world.md": 1, "02_Characters/cast.md": 2})
            prior_key = f"planning:001:{slot - 1}"
            prior_call = db.execute(
                "SELECT 1 FROM experiment_invocations WHERE "
                "(call_key=? OR call_key LIKE ?) AND state='complete' LIMIT 1",
                (prior_key, prior_key + ":retry%" )).fetchone()
            if (not prior_call or not isinstance(prior_docs_sha, dict)
                    or set(prior_docs_sha) != set(required)):
                raise ValueError("experiment_planning_order_invalid")
            for relative, producer_slot in required.items():
                sha = prior_docs_sha[relative]
                latest = db.execute(
                    "SELECT kind,episode,slot,sha256 FROM experiment_generated "
                    "WHERE relative_path=? ORDER BY entry_id DESC LIMIT 1",
                    (relative,)).fetchone()
                if (not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha)
                        or latest != ("planning", 1, producer_slot, sha)):
                    raise ValueError("experiment_planning_order_invalid")
        if kind == "writing" and episode > 1:
            previous_complete = db.execute(
                "SELECT 1 FROM experiment_invocations JOIN experiment_calls USING(call_key) "
                "WHERE kind='writing' AND episode=? AND state='complete' LIMIT 1",
                (episode - 1,)).fetchone()
            required = {"01_Settings/world.md", "02_Characters/cast.md",
                        "03_Plot/arc.md"}
            if episode == 2:
                required.add("01_Settings/temporary-omen.md")
            if (not previous_complete or not isinstance(prior_docs_sha, dict)
                    or set(prior_docs_sha) != required):
                raise ValueError("experiment_episode_order_invalid")
            for relative, sha in prior_docs_sha.items():
                latest = db.execute(
                    "SELECT kind,episode,slot,sha256 FROM experiment_generated "
                    "WHERE relative_path=? ORDER BY entry_id DESC LIMIT 1",
                    (relative,)).fetchone()
                if episode == 2 and relative != "01_Settings/temporary-omen.md":
                    expected = ("planning", 1, {
                        "01_Settings/world.md": 1,
                        "02_Characters/cast.md": 2,
                        "03_Plot/arc.md": 3}[relative], sha)
                else:
                    expected = ("writing", episode - 1, 1, sha)
                if (not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha)
                        or latest != expected):
                    raise ValueError("experiment_episode_order_invalid")
        if recovery:
            previous = base if recovery == 1 else f"{base}:retry{recovery - 1}"
            old = db.execute("SELECT state,output_sha256,input_sha256,detail FROM experiment_invocations WHERE call_key=?",
                             (previous,)).fetchone()
            if old is None or old[:2] != ("uncertain", None):
                raise ValueError("experiment_recovery_not_eligible")
            revised_plan = (kind == "planning" and episode == 1 and slot == 1
                            and recovery == 3
                            and old[3] == "synthetic_planning_response_invalid")
            revised_first_writing = (
                kind == "writing" and episode == 1 and slot == 1
                and recovery == 1 and old[3] == "provider_error_item"
                and old[2] == PREPLAN_WRITING_INPUT_SHA256
                and input_sha256 == POSTPLAN_WRITING_INPUT_SHA256)
            revised_sixth_writing = (
                kind == "writing" and episode == 6 and slot == 1
                and recovery == 2
                and old == ("uncertain", None, WRITING_006_OLD_PROMPT_SHA256,
                            "synthetic_narrative_too_short")
                and input_sha256 == WRITING_006_REVISED_PROMPT_SHA256)
            if revised_sixth_writing:
                receipt = snapshot_beneath(
                    root, "attempt-evidence/writing_006_1_retry1.json")
                if hashlib.sha256(receipt).hexdigest() != WRITING_006_RETRY1_RECEIPT_SHA256:
                    raise ValueError("experiment_recovery_receipt_changed")
            continued_eighth_writing = (
                kind == "writing" and episode == 8 and slot == 1
                and recovery == 1
                and old == ("uncertain", None, WRITING_008_OLD_PROMPT_SHA256,
                            "synthetic_narrative_too_short")
                and input_sha256 == WRITING_008_CONTINUATION_PROMPT_SHA256)
            if continued_eighth_writing:
                receipt = snapshot_beneath(root, "attempt-evidence/writing_008_1.json")
                draft = snapshot_beneath(root, "attempt-evidence/writing_008_1.draft.json")
                if hashlib.sha256(receipt).hexdigest() != WRITING_008_RECEIPT_SHA256:
                    raise ValueError("experiment_recovery_receipt_changed")
                if hashlib.sha256(draft).hexdigest() != WRITING_008_DRAFT_SHA256:
                    raise ValueError("experiment_recovery_draft_changed")
            revised_twelfth_writing = (
                kind == "writing" and episode == 12 and slot == 1
                and recovery == 1
                and old == ("uncertain", None, WRITING_012_OLD_PROMPT_SHA256,
                            WRITING_012_OLD_DETAIL)
                and input_sha256 == WRITING_012_REVISED_PROMPT_SHA256)
            if revised_twelfth_writing:
                receipt = snapshot_beneath(root, "attempt-evidence/writing_012_1.json")
                if hashlib.sha256(receipt).hexdigest() != WRITING_012_RECEIPT_SHA256:
                    raise ValueError("experiment_recovery_receipt_changed")
            continued_fifteenth_writing = (
                kind == "writing" and episode == 15 and slot == 1
                and recovery == 1
                and old == ("uncertain", None, WRITING_015_OLD_PROMPT_SHA256,
                            "synthetic_narrative_too_short")
                and input_sha256 == WRITING_015_CONTINUATION_PROMPT_SHA256)
            if continued_fifteenth_writing:
                receipt = snapshot_beneath(root, "attempt-evidence/writing_015_1.json")
                draft = snapshot_beneath(root, "attempt-evidence/writing_015_1.draft.json")
                raw = snapshot_beneath(root, "attempt-evidence/writing_015_1.raw-response.txt")
                if hashlib.sha256(receipt).hexdigest() != WRITING_015_RECEIPT_SHA256:
                    raise ValueError("experiment_recovery_receipt_changed")
                if (hashlib.sha256(draft).hexdigest() != WRITING_015_DRAFT_SHA256
                        or raw != draft):
                    raise ValueError("experiment_recovery_draft_changed")
            if (kind == "writing" and old[3] in (
                    "synthetic_narrative_too_short", WRITING_012_OLD_DETAIL)
                    and old[2] == input_sha256):
                raise ValueError("experiment_writing_same_prompt_short"
                                 if old[3] == "synthetic_narrative_too_short" else
                                 "experiment_writing_same_prompt_invalid_json")
            if (not isinstance(input_sha256, str)
                    or not re.fullmatch(r"[0-9a-f]{64}", input_sha256)
                    or (old[2] != input_sha256
                        and not revised_plan and not revised_first_writing
                        and not revised_sixth_writing
                        and not continued_eighth_writing
                        and not revised_twelfth_writing
                        and not continued_fifteenth_writing)):
                raise ValueError("experiment_recovery_input_changed")
            if kind == "query":
                ticket = db.execute("SELECT request_id FROM experiment_query_tickets WHERE call_key=?",
                                    (previous,)).fetchone()
                if action_recovery and (ticket is None or ticket == (None,)):
                    raise ValueError("experiment_recovery_not_eligible")
                if ticket != (None,):
                    # The exact reviewed q3 retry3 captured an eligible case,
                    # then the provider attempted an unexpected MCP action.
                    # Keep the snapshot and ticket as history, but never gold
                    # or train that uncertain attempt. Permit one new counted
                    # retry only after its pinned receipt is verified.
                    if not action_recovery or old[3] != "unexpected_action_item":
                        raise ValueError("experiment_recovery_not_eligible")
                    receipt = snapshot_beneath(
                        root, "attempt-evidence/query_003_1_retry3.json")
                    if hashlib.sha256(receipt).hexdigest() != ACTION_RECOVERY_RECEIPT_SHA256:
                        raise ValueError("experiment_recovery_receipt_changed")
                    rid = ticket[0]
                    case = db.execute(
                        "SELECT status,use_state,trained_cycle,first,second,gold,error "
                        "FROM cases WHERE request_id=? AND project_root=?",
                        (rid, str(project))).fetchone()
                    if (case not in (("queued", "active", None, None, None, None, None),
                                     ("pending_review", "active", None, None, None,
                                      None, "synthetic_scope_invalid"))
                            or db.execute("SELECT 1 FROM experiment_calls WHERE call_key LIKE ? LIMIT 1",
                                          (f"gold:{rid}:%",)).fetchone()):
                        raise ValueError("experiment_recovery_not_eligible")
                    if db.execute("UPDATE cases SET use_state='historical',"
                                  "error='uncertain_action_item' WHERE request_id=? "
                                  "AND project_root=? AND use_state='active'",
                                  (rid, str(project))).rowcount != 1:
                        raise ValueError("experiment_recovery_not_eligible")
            elif db.execute("SELECT 1 FROM experiment_generated WHERE kind=? AND episode=? AND slot=? LIMIT 1",
                            (kind, episode, slot)).fetchone():
                # A response may have reached disk before the call became
                # uncertain. Never repeat a generation with side effects.
                raise ValueError("experiment_recovery_outputs_present")
        counts = _counts(db)
        if total_limit is not None and (sum(counts.values()) >= total_limit
                                        or counts.get(kind, 0) >= KIND_LIMITS[kind]):
            raise ValueError("experiment_call_budget_exhausted")
        db.execute("INSERT INTO experiment_calls VALUES (?,?,?,?)",
                   (key, kind, episode, time.time()))
        if action_recovery:
            previous_source = db.execute(
                "SELECT source_id,source_sha256 FROM experiment_query_tickets "
                "WHERE call_key=?", (previous,)).fetchone()
            if previous_source != (source_id, source_sha256):
                raise ValueError("experiment_recovery_source_changed")
            db.execute("INSERT INTO experiment_invocations VALUES (?,?,?,NULL,NULL)",
                       (key, input_sha256, "reserved"))
            db.execute("INSERT INTO experiment_query_tickets "
                       "(call_key,episode,slot,prompt_sha256,source_id,source_sha256) "
                       "VALUES (?,?,?,?,?,?)",
                       (key, episode, slot, input_sha256, source_id, source_sha256))
    return {"status": "reserved", "call_key": key}


def snapshot(root):
    from .shared import _db
    loaded = load(root, project_for(root))
    with _db(root) as db:
        result = {"calls": _counts(db),
                  "invocations": dict(db.execute("SELECT state,COUNT(*) FROM experiment_invocations GROUP BY state")),
                  "uncertain_calls": [{"call_key": key, "reason": detail} for key, detail in
                                      db.execute("SELECT call_key,detail FROM experiment_invocations WHERE state='uncertain' ORDER BY call_key")],
                  "approved_episode": _meta(db, "experiment_approved_episode"),
                  "train_reservations": db.execute("SELECT COUNT(*) FROM experiment_cycles").fetchone()[0],
                  "final_gold": db.execute("SELECT COUNT(*) FROM cases WHERE split='final' AND status='gold' AND use_state='active'").fetchone()[0]}
        captured = db.execute(
            "SELECT t.source_id,t.source_sha256,c.sample FROM experiment_query_tickets t "
            "JOIN cases c ON c.request_id=t.request_id AND c.project_root=? "
            "WHERE c.use_state='active'", (str(project_for(root)),)).fetchall()
        present = sum(any(candidate["candidate_id"] == source_id and
                          candidate["revision_sha256"] == source_sha
                          for candidate in json.loads(sample)["candidates"])
                      for source_id, source_sha, sample in captured)
        query_attempts = db.execute(
            "SELECT COUNT(*) FROM experiment_invocations i JOIN experiment_calls c "
            "USING(call_key) WHERE c.kind='query'").fetchone()[0]
        result["retrieval"] = {"captured_queries": len(captured),
                               "query_attempts": query_attempts,
                               "attempts_outside_active_capture":
                                   max(query_attempts - len(captured), 0),
                               "expected_target_present": present,
                               "expected_target_missing": len(captured) - present,
                               "conditional_expected_target_in_pool_rate":
                                   present / len(captured) if captured else None}
        result["episode_lengths"] = [
            {"episode": episode, **json.loads(detail)["narrative_characters"]}
            for episode, detail in db.execute(
                "SELECT c.episode,i.detail FROM experiment_calls c "
                "JOIN experiment_invocations i USING(call_key) "
                "WHERE c.kind='writing' AND i.state='complete' ORDER BY c.episode")
            if detail and "narrative_characters" in json.loads(detail)]
        sealed = db.execute("SELECT status,cases_sha256 FROM experiment_final WHERE id=1").fetchone()
        if sealed and sealed[0] == "complete":
            _, mapped, current_sha = _final_state(db, project_for(root))
            if current_sha != sealed[1] or sorted(mapped) != [(n, 2) for n in range(81, 101)]:
                sealed = ("invalidated", sealed[1])
        result["final_evaluation"] = sealed[0] if sealed else None
    if loaded is not None:
        control, sha = loaded
        result["limits"] = {"total_calls": control["maxTotalCalls"],
                            "gold_calls": control["maxGoldCalls"],
                            "train_cycles": control["maxTrainCycles"],
                            "disk_bytes": control["maxDiskBytes"]}
        result["disk_bytes"] = disk_bytes(root)
        try:
            grant = approval(root, project_for(root), control, sha)
            result["approved"] = {"episode": grant["approvedThroughEpisode"],
                                  "total_calls": grant["approvedTotalCalls"],
                                  "gold_calls": grant["approvedGoldCalls"],
                                  "train_cycles": grant["approvedTrainCycles"]}
        except (OSError, ValueError):
            result["approved"] = None
    return result


def recover_q5_capture(root, project, *, validate_only=False):
    """Bind one previously captured q5 retrieval miss from immutable evidence."""
    from .shared import _db, _current_config, enqueue
    from .seam import _private_state
    from .compact_input import derive
    from .synthetic_scope import generated_path
    root, project = Path(root).resolve(strict=True), Path(project).resolve(strict=True)
    if project != project_for(root):
        raise ValueError("experiment_recovery_project_invalid")
    control, control_sha = load(root, project)
    approval(root, project, control, control_sha)
    call_key = "query:005:1"
    receipt = snapshot_beneath(root, "attempt-evidence/query_005_1.json")
    if hashlib.sha256(receipt).hexdigest() != Q5_CAPTURE_RECEIPT_SHA256:
        raise ValueError("experiment_recovery_receipt_changed")
    proof = json.loads(receipt)
    if (proof.get("callKey") != call_key or proof.get("turnCompleted") is not True
            or proof.get("turnFailed") is not False or proof.get("finalMessagePresent") is not True
            or proof.get("exitCode") != 0):
        raise ValueError("experiment_recovery_receipt_invalid")
    cfg = _current_config(root, project)
    private = _private_state(cfg, project) if cfg else None
    if private is None:
        raise ValueError("experiment_recovery_consent_missing")
    from .history_activation import read_database
    shared_path = root / "shared-learning.sqlite3"
    if shared_path.is_symlink() or not shared_path.is_file():
        raise ValueError("experiment_recovery_database_missing")
    with read_database(shared_path) as db:
        ticket = db.execute(
            "SELECT t.prompt_sha256,t.source_id,t.source_sha256,t.request_id,"
            "i.state,i.detail,i.input_sha256 FROM experiment_query_tickets t "
            "JOIN experiment_invocations i USING(call_key) WHERE t.call_key=?",
            (call_key,)).fetchone()
        if ticket is None or ticket[0] != proof.get("inputSha256"):
            raise ValueError("experiment_recovery_ticket_changed")
        if ticket[3] is not None:
            if ticket[3] != Q5_CAPTURE_REQUEST_ID:
                raise ValueError("experiment_recovery_request_changed")
            case = db.execute("SELECT status,use_state FROM cases WHERE request_id=? "
                              "AND project_root=?", (ticket[3], str(project))).fetchone()
            if ticket[4] == "complete" and case and case[1] == "active":
                return {"status": "already_recovered", "request_id": ticket[3]}
            if (not case or case[1] != "active"
                    or ticket[4:7] != ("uncertain", "query_capture_missing", ticket[0])):
                raise ValueError("experiment_recovery_case_invalid")
        elif ticket[4:7] != ("uncertain", "query_capture_missing", ticket[0]):
            raise ValueError("experiment_recovery_not_eligible")
    db_path = private / "learning.sqlite3"
    if db_path.is_symlink() or not db_path.is_file():
        raise ValueError("experiment_recovery_capture_missing")
    with read_database(db_path) as db:
        matches = [(rid, body) for rid, body in db.execute("SELECT request_id,body FROM samples")
                   if json.loads(body)["prompt"]["sha256"] == ticket[0]]
        if len(matches) != 1:
            raise ValueError("experiment_recovery_capture_ambiguous")
        rid, body = matches[0]
        if rid != Q5_CAPTURE_REQUEST_ID:
            raise ValueError("experiment_recovery_request_changed")
        if (db.execute("SELECT 1 FROM tombstones WHERE request_id=?", (rid,)).fetchone()
                or hashlib.sha256(rid.encode()).hexdigest() != json.loads(
                    snapshot_beneath(private, "shared-last-status.json")).get("request_id_hash")):
            raise ValueError("experiment_recovery_capture_status_invalid")
        status = json.loads(snapshot_beneath(private, "shared-last-status.json"))
        if status.get("status") != "experiment_target_source_missing":
            raise ValueError("experiment_recovery_capture_status_invalid")
        sample = json.loads(body)
        sources = {}
        for candidate in sample["candidates"]:
            cid, revision = candidate["candidate_id"], candidate["revision_sha256"]
            source = snapshot_beneath(story_for(project), generated_path(cid))
            if hashlib.sha256(source).hexdigest() != revision:
                raise ValueError("experiment_recovery_source_changed")
            content = source.decode("utf8")
            compact = db.execute("SELECT body FROM compact_inputs WHERE request_id=? "
                                 "AND candidate_id=?", (rid, cid)).fetchone()
            if compact is None or compact[0] != canonical(derive(content, revision)):
                raise ValueError("experiment_recovery_compact_changed")
            sources[cid] = content
    if validate_only:
        return {"status": "ready_for_receipt_recovery", "request_id": rid,
                "candidate_count": len(sample["candidates"]),
                "expected_target_in_pool": any(c["candidate_id"] == ticket[1] and
                                               c["revision_sha256"] == ticket[2]
                                               for c in sample["candidates"])}
    if ticket[3] is None:
        outcome = enqueue(cfg, project, sample, sources,
                          recovery_call_key=call_key)
        if outcome != "queued":
            raise ValueError("experiment_recovery_enqueue_" + outcome[:40])
    from .synthetic_launcher import _bound_query
    with _db(root) as db:
        row = db.execute("SELECT request_id FROM experiment_query_tickets WHERE call_key=?",
                         (call_key,)).fetchone()
        if row != (rid,):
            raise ValueError("experiment_recovery_binding_invalid")
        _bound_query(db, root, project, call_key, sample["prompt"]["text"],
                     ticket[1], ticket[2])
        changed = db.execute("UPDATE experiment_invocations SET state='complete',detail=? "
                             "WHERE call_key=? AND state='uncertain' "
                             "AND detail='query_capture_missing'",
                             (canonical({"recoveredCaptureReceiptSha256": Q5_CAPTURE_RECEIPT_SHA256}),
                              call_key)).rowcount
        if changed != 1:
            raise ValueError("experiment_recovery_state_changed")
    return {"status": "recovered", "request_id": rid,
            "expected_target_in_pool": any(c["candidate_id"] == ticket[1] and
                                           c["revision_sha256"] == ticket[2]
                                           for c in sample["candidates"])}


def _final_state(db, project):
    rows = db.execute("SELECT project_key,request_id,input_sha256,sample,bodies,gold,split,project_root,corpus_fingerprint,family_keys,trained_cycle FROM cases WHERE project_root=? AND split='final' AND status='gold' AND use_state='active' ORDER BY captured_at,request_id",
                      (str(project),)).fetchall()
    mapped = db.execute("SELECT t.episode,t.slot FROM experiment_query_tickets t JOIN cases c USING(request_id) WHERE c.project_root=? AND c.split='final' AND c.status='gold' AND c.use_state='active'",
                        (str(project),)).fetchall()
    cases_sha = hashlib.sha256(canonical([(r[0], r[1], r[2], r[5]) for r in rows]).encode()).hexdigest()
    return rows, mapped, cases_sha


def _final_query_families(rows):
    """Reject repeated sealed questions or answer facts among final cases."""
    from .shared import _query_family_keys
    keys = set()
    for row in rows:
        incoming = _query_family_keys(row)
        if incoming & keys:
            raise ValueError("experiment_final_family_overlap")
        keys.update(incoming)
    return keys


def evaluate_final(root, *, trainer_factory=None):
    """One sealed local prediction of the frozen checkpoint on twenty final cases."""
    from .shared import (_db, _case, _case_active, _candidate_exposure_keys, _policy, _score,
                         _historical_trainer_policy,
                         _predict, _reconcile_cases, _valid_training_provenance,
                         resource_report, trial_eligible)
    from .local_cycle import VerifiedLayaTrainer, _hash
    root = Path(root).resolve(strict=True)
    project = project_for(root)
    control, sha = load(root, project)
    grant = approval(root, project, control, sha)
    policy = _policy(root)
    if (root.name == SHARED_NAME and (
            policy.get('_runtime_authorization_v2') is True or
            (root / 'runtime-execution-authorization.json').exists())):
        # The stage-only v2 grant is not authority to unseal the historical
        # final set. Keep its one-shot ledger and pointer unchanged.
        return {'status': 'runtime_approval_required'}
    # A historical trainer reference authorizes reading old labels only. Do
    # not reserve the one-shot final attempt before executable runtime consent.
    if _historical_trainer_policy(policy):
        return {"status": "runtime_approval_required"}
    adapter = Path(__file__).with_name("laya_adapter.py").resolve()
    if Path(policy["trainerArgv"][1]).resolve() != adapter:
        raise ValueError("invalid_shared_trainer")
    if not resource_report(policy)["ready"]:
        return {"status": "deferred_resource"}
    _reconcile_cases(root)
    pointer_path = root / "experimental-checkpoint.json"
    pointer = read_private_json(pointer_path)
    if (not _valid_training_provenance(pointer) or not trial_eligible(root, max_seconds=5)
            or not pointer.get("experimental")
            or _hash(pointer["artifact_path"]) != pointer["artifact_sha256"]):
        raise ValueError("experiment_final_pointer_invalid")
    with _db(root) as db:
        existing = db.execute("SELECT status,result,pointer_sha256,cases_sha256 FROM experiment_final WHERE id=1").fetchone()
        if existing:
            if existing[0] == "complete":
                latest, mapped, latest_sha = _final_state(db, project)
                pointer_sha = hashlib.sha256(canonical(pointer).encode()).hexdigest()
                if (pointer_sha != existing[2] or latest_sha != existing[3]
                        or len(latest) != 20
                        or sorted(mapped) != [(n, 2) for n in range(81, 101)]):
                    db.execute("UPDATE experiment_final SET status='invalidated' WHERE id=1 AND status='complete'")
                    return {"status": "final_result_invalidated"}
                return json.loads(existing[1])
            return {"status": "final_result_invalidated" if existing[0] == "invalidated" else "final_attempt_uncertain"}
        effective, _, _, _ = _limits(db, grant)
        if effective != 100:
            raise ValueError("experiment_final_not_unlocked")
        completed = dict(db.execute("SELECT kind,COUNT(*) FROM experiment_invocations JOIN experiment_calls USING(call_key) WHERE state='complete' GROUP BY kind"))
        q1_gold = db.execute("SELECT COUNT(*) FROM experiment_query_tickets t JOIN experiment_invocations i USING(call_key) JOIN cases c USING(request_id) WHERE t.slot=1 AND i.state='complete' AND c.status='gold' AND c.use_state='active'").fetchone()[0]
        if (any(completed.get(kind, 0) != 100 for kind in
                ("writing", "wiki_create", "wiki_review")) or q1_gold != 100):
            raise ValueError("experiment_episodes_incomplete")
        if db.execute("SELECT COUNT(*) FROM cases WHERE status='gold' AND use_state='active' AND split!='final' AND trained_cycle IS NULL").fetchone()[0]:
            raise ValueError("experiment_iterative_training_incomplete")
        rows, mapped, cases_sha = _final_state(db, project)
        if (len(rows) != 20 or sorted(mapped) != [(n, 2) for n in range(81, 101)]
                or any(not _case_active(root, (row[7], row[1], row[3])) for row in rows)):
            raise ValueError("experiment_final_cases_incomplete")
        final_keys = _final_query_families(rows)
        provenance = pointer["trainingProvenance"]
        prior_keys = set(provenance.get("queryFamilyKeys", provenance["familyKeys"]))
        prior_keys.update(provenance.get("selectionQueryFamilyKeys", provenance["selectionFamilyKeys"]))
        if final_keys & prior_keys:
            raise ValueError("experiment_final_family_overlap")
        pointer_sha = hashlib.sha256(canonical(pointer).encode()).hexdigest()
        db.execute("INSERT INTO experiment_final VALUES (1,'running',?,?,NULL)",
                   (pointer_sha, cases_sha))
    folder = root / "final-evaluation"
    folder.mkdir(mode=0o700, exist_ok=True)
    try:
        if trainer_factory is None:
            trainer = VerifiedLayaTrainer(policy["trainerArgv"], max_seconds=policy["safetySeconds"],
                            max_rss_mib=policy["maxRssMib"],
                            pressure_probe=lambda **options: resource_report(policy, admission=False,**options), require_monitor=True)
        else:
            trainer = trainer_factory(policy)
        cases = [_case(row[:6]) for row in rows]
        score, resources = _predict(trainer, folder, Path(pointer["artifact_path"]),
                                    cases, "sealed-final",
                                    input_view=pointer.get("model_input_view"))
        corpus_seen = set(provenance["familyKeys"]) | set(provenance["selectionFamilyKeys"])
        strict_rows = []
        strict_overlap_families = 0
        for row in rows:
            keys = _candidate_exposure_keys(row)
            overlap = keys & corpus_seen
            if overlap:
                strict_overlap_families += len(overlap)
            else:
                strict_rows.append(row)
            corpus_seen.update(keys)
        strict_overlap = len(rows) - len(strict_rows)
        strict_metric = {"status": "available" if strict_rows else "unavailable",
                         "questions": len(strict_rows), "overlap_questions": strict_overlap,
                         "overlap_families": strict_overlap_families}
        if strict_rows:
            strict_cases = [_case(row[:6]) for row in strict_rows]
            ids = {case["request_id"] for case in strict_cases}
            predictions = read_private_json(folder / "sealed-final.json")["predictions"]
            strict_metric["score"] = _score(
                strict_cases,
                {"predictions": [prediction for prediction in predictions
                                 if prediction["request_id"] in ids]})
        else:
            strict_metric["reason"] = "no_disjoint_candidate_pool"
        result = {"status": "complete", "schema": "qmd-synthetic-final-v1",
                  "evaluation_scope": "sealed_unseen_query_family_seen_corpus",
                  "candidate_document_disjoint": strict_metric,
                  "pointer_sha256": pointer_sha,
                  "cases_sha256": cases_sha, "questions": score["questions"],
                  "exact": score["exact"], "exact_rate": score["exact_rate"],
                  "resources": resources}
        with _db(root) as db:
            db.execute("BEGIN IMMEDIATE")
            latest, mapped, latest_sha = _final_state(db, project)
            if (len(latest) != 20 or sorted(mapped) != [(n, 2) for n in range(81, 101)]
                    or latest_sha != cases_sha
                    or hashlib.sha256(canonical(read_private_json(pointer_path)).encode()).hexdigest() != pointer_sha
                    or _hash(pointer["artifact_path"]) != pointer["artifact_sha256"]
                    or not trial_eligible(root, max_seconds=5)
                    or any(not _case_active(root, (row[7], row[1], row[3])) for row in latest)):
                raise ValueError("experiment_final_changed_during_evaluation")
            changed = db.execute("UPDATE experiment_final SET status='complete',result=? WHERE id=1 AND status='running' AND pointer_sha256=? AND cases_sha256=?",
                                 (canonical(result), pointer_sha, cases_sha)).rowcount
            if changed != 1:
                raise ValueError("experiment_final_seal_changed")
        return result
    except Exception:
        with _db(root) as db:
            db.execute("UPDATE experiment_final SET status='uncertain' WHERE id=1 AND status='running'")
        raise

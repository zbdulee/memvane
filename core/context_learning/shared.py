"""Explicitly enrolled projects share one durable experimental learning queue.

The normal project learning database and its strict promotion gate are untouched.
This queue accepts immutable capture-time bodies, and only a detached one-shot
worker may call the teacher or local trainer. Uncertain external calls never
retry automatically.
"""
from __future__ import annotations

import fcntl
from contextlib import closing, contextmanager, ExitStack
from contextvars import ContextVar
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
import sys
import threading
import time
import secrets
import job_telemetry

from .compact_input import derive
from .contracts import digest, validate_label, validate_request
from .dataset import read_private_json
from .local_cycle import ResourceDeferred, LocalTrainer, VerifiedLayaTrainer, _atomic_json, _hash, _safe_file
from .store import canonical
from .safe_source import snapshot_beneath

POLICY_NAME = "shared-policy.json"
DATABASE_NAME = "shared-learning.sqlite3"
SCHEMA = "qmd-shared-learning-v1"
STATUSES = ("awaiting_local", "queued", "first_running", "first_done", "second_running",
            "gold", "pending_review", "failed")
RETRYABLE_SCOPE_SETTLEMENT = ("synthetic_collection_unproven",
                              "synthetic_index_edit_not_settled",
                              "synthetic_index_contains_unproven_document")
V45_ADAPTER_SHA256 = 'd36ab90d7b0b89a7725e66bab3c4761fc2c26d6cbbfc0631d71ca839af9996c4'
REVIEWED_ADAPTER_SHA256 = '1509e6833ba8b2ca23d6e7811173d268c09b16005a2b3d9f9f2838c9ae23bf5e'


def _v45_adapter_path():
    return (Path.home() / '.codex/plugins/cache/qmd-shared-v21-local/'
            'qmd-auto-context/0.35.2-dev.20261009-fantasy-pilot-v45/'
            'core/context_learning/laya_adapter.py')


def _historical_trainer_policy(policy):
    argv = policy.get('trainerArgv') if isinstance(policy, dict) else None
    return (isinstance(argv, list) and len(argv) == 3
            and isinstance(argv[1], str)
            and Path(argv[1]) == _v45_adapter_path())


def _historical_v45_adapter_reference(path, current_adapter):
    """Recognize one approved policy reference without authorizing execution.

    An installer may remove the old cache directory. The exact historical
    string remains part of the approved policy, while all executable trainer
    paths stay gated to this installed version elsewhere.
    """
    old = _v45_adapter_path()
    path = Path(path)
    if path != old or hashlib.sha256(current_adapter.read_bytes()).hexdigest() != REVIEWED_ADAPTER_SHA256:
        return False
    if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
        return False
    if path.exists():
        info = path.stat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or hashlib.sha256(path.read_bytes()).hexdigest() != V45_ADAPTER_SHA256):
            return False
    return True


def inference_argv(policy, operation):
    """Resolve a reviewed historical reference for edit inference only.

    The approved policy retains the v45 path as immutable provenance. Its
    missing cache entry must never be recreated, and this resolver must never
    supply an executable for training, observation, or final evaluation.
    """
    if operation not in ('segment', 'predict'):
        raise ValueError('invalid_shared_inference_operation')
    argv = policy.get('trainerArgv') if isinstance(policy, dict) else None
    current = Path(__file__).with_name('laya_adapter.py')
    if (not isinstance(argv, list) or len(argv) != 3
            or any(not isinstance(value, str) or not Path(value).is_absolute()
                   for value in argv)):
        raise ValueError('invalid_shared_inference_command')
    from .laya_adapter import EDIT_INPUT_VIEW, _read_json
    if policy.get('modelInputView') != EDIT_INPUT_VIEW:
        raise ValueError('historical_inference_view_unverified')
    try:
        config = _read_json(Path(argv[2]) / 'rl_agent_config.json')
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise ValueError('historical_inference_view_unverified') from exc
    if (not isinstance(config, dict)
            or config.get('encoder') != 'jhu-clsp/mmBERT-base'
            or (config.get('max_len'), config.get('head_max_len')) != (1024, 256)):
        raise ValueError('historical_inference_view_unverified')
    try:
        info = current.lstat()
        trusted_current = (stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                           and info.st_nlink == 1 and
                           hashlib.sha256(current.read_bytes()).hexdigest()
                           == REVIEWED_ADAPTER_SHA256)
    except OSError:
        trusted_current = False
    if not trusted_current:
        raise ValueError('historical_inference_adapter_unverified')
    if Path(argv[1]) == current.resolve():
        return list(argv)
    if (not _historical_trainer_policy(policy)
            or not _historical_v45_adapter_reference(argv[1], current)):
        raise ValueError('historical_inference_adapter_unverified')
    return [argv[0], str(current), argv[2]]


def _gold_only_request_ids():
    try:
        ids = json.loads(os.environ.get('QMD_SHARED_GOLD_REQUEST_IDS', ''))
    except (ValueError, TypeError):
        return None
    if (not isinstance(ids, list) or not 1 <= len(ids) <= 8
            or len(set(str(x) for x in ids)) != len(ids)
            or any(not isinstance(x, str) or not re.fullmatch(
                r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', x)
                   for x in ids)):
        return None
    return ids


def _safe_root(config, project):
    opts = config.get("sharedLearning", {})
    if not isinstance(opts, dict) or opts.get("enabled") is not True:
        return None
    raw = opts.get("root")
    if not isinstance(raw, str) or not Path(raw).is_absolute():
        return None
    root = Path(raw)
    if not root.is_dir() or any(p.is_symlink() for p in (root, *root.parents)):
        return None
    root = root.resolve(strict=True)
    project = Path(project).resolve(strict=True)
    if root == project:
        return None
    # The shared store may sit in a hidden project subdirectory, provided no
    # configured collection can index it. This also rejects a collection that
    # indexes the whole project root.
    for base in config.get("collectionPaths", {}).values():
        if not isinstance(base, str):
            return None
        collection_root = (project / base).resolve()
        if root == collection_root or collection_root in root.parents:
            return None
    info = root.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        return None
    return root


def _validated_root(raw):
    root = Path(raw)
    if not root.is_absolute() or not root.is_dir() or any(p.is_symlink() for p in (root, *root.parents)):
        raise ValueError("unsafe_shared_root")
    info = root.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("unsafe_shared_root")
    return root.resolve(strict=True)


def _policy(root):
    root = _validated_root(root)
    path = root / POLICY_NAME
    if path.is_symlink() or not path.is_file():
        raise ValueError("missing_shared_policy")
    value = read_private_json(path)
    keys = {"schema", "enabled", "goldModel", "maxGoldCalls", "batchMin",
            "batchMax", "targetExact", "trainerArgv", "baseArtifact",
            "maxRssMib", "minAvailableMib", "maxLoadPerCpu",
            "safetySeconds", "experimental"}
    if not isinstance(value, dict) or not keys <= set(value) or set(value) - keys - {'modelInputView', 'adaptiveMemory', 'targetHeadroomFraction'} or value["schema"] != SCHEMA or value["enabled"] is not True:
        raise ValueError("invalid_shared_policy")
    if ('targetHeadroomFraction' in value and
            (type(value['targetHeadroomFraction']) not in (int,float) or
             not .1<=value['targetHeadroomFraction']<=.5)):
        raise ValueError('invalid_shared_headroom')
    if not isinstance(value["goldModel"], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", value["goldModel"]):
        raise ValueError("invalid_gold_model")
    uncapped_gold = value["maxGoldCalls"] is None
    if uncapped_gold:
        from .synthetic_experiment import load, project_for
        experiment = load(root, project_for(root))
        if experiment is None or experiment[0]["maxGoldCalls"] is not None:
            raise ValueError("invalid_shared_budget")
    for key, minimum, maximum in (("batchMin", 2, 100),
                                  ("batchMax", 2, 500), ("maxRssMib", 512, 65536),
                                  ("minAvailableMib", 128, 65536), ("safetySeconds", 60, 3600)):
        if type(value[key]) is not int or not minimum <= value[key] <= maximum:
            raise ValueError("invalid_shared_budget")
    if not uncapped_gold and (type(value["maxGoldCalls"]) is not int
                              or not 2 <= value["maxGoldCalls"] <= 1000):
        if value["maxGoldCalls"] != 0:
            raise ValueError("invalid_shared_budget")
        # Zero is reserved for an exact teacher-free QA transaction. The
        # installing phase is needed while profile.activate calls _policy;
        # runtime use still requires the activated phase in synthetic_local_only.
        import project_learning_setup
        path = root / project_learning_setup.NAME
        if path.is_symlink() or not path.is_file():
            raise ValueError("invalid_shared_budget")
        setup = read_private_json(path)
        plan = setup.get("plan") if isinstance(setup, dict) else None
        if (not isinstance(plan, dict)
                or setup.get("phase") not in ("installing", "activated")
                or plan.get("mode") != project_learning_setup.SYNTHETIC_STAGED
                or setup.get("acceptedSha256") != project_learning_setup.proposal_sha256(plan)
                or plan.get("policy") != value
                or plan.get("teacherConsent") is not None
                or (root / "project-teacher-consent.json").exists()):
            raise ValueError("invalid_shared_budget")
    if value["batchMax"] < value["batchMin"] or type(value["targetExact"]) not in (int, float) or not .5 <= value["targetExact"] <= 1:
        raise ValueError("invalid_shared_cadence")
    if type(value["maxLoadPerCpu"]) not in (int, float) or not .1 <= value["maxLoadPerCpu"] <= 4:
        raise ValueError("invalid_shared_load")
    argv = value["trainerArgv"]
    adapter = Path(__file__).with_name("laya_adapter.py").resolve()
    general_grant=None
    from . import project_scope
    if project_scope.present(root):
        from .runtime_authorization import active
        general_grant=active(root,value,adapter,reviewed_adapter_sha256=REVIEWED_ADAPTER_SHA256)
    if (not isinstance(argv, list) or len(argv) != 3
            or any(not isinstance(x, str) or not Path(x).is_absolute() for x in argv)
            or (Path(argv[1]).resolve() != adapter
                and not general_grant
                and not _historical_v45_adapter_reference(argv[1], adapter))):
        raise ValueError("invalid_shared_trainer")
    if 'adaptiveMemory' in value:
        from .memory_policy import validate
        validate(value['adaptiveMemory'])
    if 'modelInputView' in value:
        from .laya_adapter import EDIT_INPUT_VIEW, _read_json
        if value['modelInputView'] != EDIT_INPUT_VIEW:
            raise ValueError('invalid_shared_model_input_view')
        try:
            config = _read_json(Path(argv[2]) / 'rl_agent_config.json')
        except (OSError, ValueError, KeyError, IndexError, TypeError) as exc:
            raise ValueError('invalid_shared_multilingual_model') from exc
        if (config.get('encoder') != 'jhu-clsp/mmBERT-base'
                or (config.get('max_len'), config.get('head_max_len')) != (1024, 256)):
            raise ValueError('invalid_shared_multilingual_model')
    if not isinstance(value["baseArtifact"], str) or not Path(value["baseArtifact"]).is_absolute() or value["experimental"] is not True:
        raise ValueError("invalid_shared_artifact")
    # Historical isolated/derived preflights run inside read_only_validation
    # and bind the immutable on-disk argv. They must not inherit a separate
    # execution approval used by the normal automatic worker.
    if _READ_ONLY_VALIDATION.get():
        return value
    from .runtime_authorization import active
    grant = general_grant or active(root, value, adapter,
                   reviewed_adapter_sha256=REVIEWED_ADAPTER_SHA256)
    if grant is None:
        return value
    return {**value, 'trainerArgv': [value['trainerArgv'][0], str(adapter),
                                    value['trainerArgv'][2]],
            '_runtime_authorization_v2': True}


@contextmanager
def _db(root, *, home=None):
    if _READ_ONLY_VALIDATION.get():
        from .history_activation import writer_access
        with writer_access(Path(root)/DATABASE_NAME,home=home) as path:
            if path.is_symlink() or not path.is_file():raise ValueError('unsafe_shared_database')
            info=path.stat()
            if info.st_uid!=os.getuid() or info.st_nlink!=1 or info.st_mode&0o077:raise ValueError('unsafe_shared_database')
            with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=1)) as db:
                db.execute('PRAGMA query_only=ON');yield db
        return
    from .history_activation import writer_access
    with writer_access(Path(root) / DATABASE_NAME, home=home) as path:
        with _db_at(path) as db:
            yield db


_READ_ONLY_VALIDATION = ContextVar('qmd_shared_read_only_validation', default=False)


@contextmanager
def read_only_validation():
    """Make nested synthetic validators query the existing DB without DDL."""
    token = _READ_ONLY_VALIDATION.set(True)
    try:
        yield
    finally:
        _READ_ONLY_VALIDATION.reset(token)


@contextmanager
def _db_at(path):
    if path.is_symlink():
        raise ValueError("unsafe_shared_database")
    if path.exists():
        info = path.stat()
        if info.st_uid != os.getuid() or info.st_nlink != 1 or info.st_mode & 0o077:
            raise ValueError("unsafe_shared_database")
    new = not path.exists()
    db = sqlite3.connect(path, timeout=1)
    if new:
        os.chmod(path, 0o600)
    if db.execute('PRAGMA user_version').fetchone()[0] == 2:
        db.execute('PRAGMA foreign_keys=ON')
    db.execute("PRAGMA busy_timeout=1000")
    db.execute("PRAGMA secure_delete=ON")
    db.execute("CREATE TABLE IF NOT EXISTS cases (project_key TEXT NOT NULL, project_root TEXT NOT NULL, request_id TEXT NOT NULL, input_sha256 TEXT NOT NULL, sample TEXT NOT NULL, bodies TEXT NOT NULL, corpus_fingerprint TEXT NOT NULL, captured_at REAL NOT NULL, split TEXT NOT NULL, family_keys TEXT NOT NULL, use_state TEXT NOT NULL, status TEXT NOT NULL, first TEXT, second TEXT, gold TEXT, error TEXT, trained_cycle TEXT, PRIMARY KEY(project_key,request_id))")
    db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS cycles (cycle_id TEXT PRIMARY KEY, status TEXT NOT NULL, evidence TEXT NOT NULL, started_at REAL NOT NULL, finished_at REAL)")
    db.execute("CREATE TABLE IF NOT EXISTS observations (cycle_id TEXT NOT NULL, evidence_sha256 TEXT NOT NULL, body TEXT NOT NULL, PRIMARY KEY(cycle_id,evidence_sha256))")
    db.execute("CREATE TABLE IF NOT EXISTS families (family_key TEXT PRIMARY KEY, split TEXT NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS experiment_calls (call_key TEXT PRIMARY KEY, kind TEXT NOT NULL, episode INTEGER NOT NULL, reserved_at REAL NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS experiment_cycles (cycle_id TEXT PRIMARY KEY, reserved_at REAL NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS experiment_invocations (call_key TEXT PRIMARY KEY, input_sha256 TEXT NOT NULL, state TEXT NOT NULL, output_sha256 TEXT, detail TEXT, FOREIGN KEY(call_key) REFERENCES experiment_calls(call_key))")
    db.execute("CREATE TABLE IF NOT EXISTS experiment_generated (entry_id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, episode INTEGER NOT NULL, slot INTEGER NOT NULL, relative_path TEXT NOT NULL, sha256 TEXT NOT NULL, prior_sha256 TEXT, recorded_at REAL NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS experiment_repairs (call_key TEXT PRIMARY KEY, old_entry_id INTEGER NOT NULL, old_sha256 TEXT NOT NULL, backup_path TEXT NOT NULL, state TEXT NOT NULL, desired_sha256 TEXT, response_sha256 TEXT, resources TEXT, new_entry_id INTEGER, detail TEXT, reserved_at REAL NOT NULL, completed_at REAL)")
    db.execute("CREATE TABLE IF NOT EXISTS experiment_pending_final (episode INTEGER PRIMARY KEY, draft TEXT NOT NULL, reviewed TEXT, source_sha256 TEXT NOT NULL, materialized_sha256 TEXT)")
    db.execute("CREATE TABLE IF NOT EXISTS experiment_query_tickets (call_key TEXT PRIMARY KEY, episode INTEGER NOT NULL, slot INTEGER NOT NULL, prompt_sha256 TEXT NOT NULL, source_id TEXT NOT NULL, source_sha256 TEXT NOT NULL, request_id TEXT UNIQUE, input_sha256 TEXT, candidate_sha256 TEXT, FOREIGN KEY(call_key) REFERENCES experiment_calls(call_key))")
    db.execute("CREATE TABLE IF NOT EXISTS experiment_final (id INTEGER PRIMARY KEY CHECK(id=1), status TEXT NOT NULL, pointer_sha256 TEXT NOT NULL, cases_sha256 TEXT NOT NULL, result TEXT)")
    db.execute("CREATE TABLE IF NOT EXISTS edit_tickets (ticket_id TEXT PRIMARY KEY, project_root TEXT NOT NULL, story_root TEXT NOT NULL, relative_path TEXT NOT NULL, episode INTEGER NOT NULL, approval_sha256 TEXT NOT NULL, parent_approval_sha256 TEXT NOT NULL, before_sha256 TEXT, after_sha256 TEXT, document_sha256 TEXT NOT NULL, source_side TEXT NOT NULL, source_start INTEGER NOT NULL, segment_start INTEGER NOT NULL, segment_end INTEGER NOT NULL, segment_sha256 TEXT NOT NULL, event_json TEXT NOT NULL, session_id TEXT NOT NULL, turn_id TEXT NOT NULL, tool_use_id TEXT NOT NULL, operation TEXT NOT NULL, created_at REAL NOT NULL, expires_at REAL NOT NULL, request_id TEXT UNIQUE, input_sha256 TEXT, candidate_sha256 TEXT)")
    db.execute("CREATE TABLE IF NOT EXISTS source_card_versions (registration_id TEXT PRIMARY KEY, project_root TEXT NOT NULL, story_root TEXT NOT NULL, source_path TEXT NOT NULL, source_sha256 TEXT NOT NULL, source_bytes BLOB NOT NULL, source_ticket_id TEXT NOT NULL UNIQUE, source_ticket_sha256 TEXT NOT NULL, card_path TEXT NOT NULL, card_sha256 TEXT NOT NULL, card_bytes BLOB NOT NULL, card_ticket_id TEXT NOT NULL UNIQUE, card_ticket_sha256 TEXT NOT NULL, generation_method TEXT NOT NULL, index_ack TEXT NOT NULL, registered_at REAL NOT NULL, UNIQUE(card_path,card_sha256))")
    db.execute("CREATE TABLE IF NOT EXISTS topical_worker_versions (registration_id TEXT PRIMARY KEY, project_root TEXT NOT NULL, story_root TEXT NOT NULL, card_path TEXT NOT NULL, card_sha256 TEXT NOT NULL, card_bytes BLOB NOT NULL, generation_id TEXT NOT NULL, card_id TEXT NOT NULL, source_snapshots TEXT NOT NULL, evidence_sha256 TEXT NOT NULL, attestation_sha256 TEXT NOT NULL, publication_sha256 TEXT NOT NULL, scope_policy_sha256 TEXT NOT NULL, provider_receipts_sha256 TEXT NOT NULL, index_ack TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('active','retired')), registered_at REAL NOT NULL, UNIQUE(card_path,card_sha256))")
    db.execute("CREATE TABLE IF NOT EXISTS synthetic_source_inventory (relative_path TEXT PRIMARY KEY, sha256 TEXT, recorded_at REAL NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS synthetic_source_events (event_id TEXT PRIMARY KEY, relative_path TEXT NOT NULL, operation TEXT NOT NULL, before_sha256 TEXT, after_sha256 TEXT, state TEXT NOT NULL, recorded_at REAL NOT NULL, processed_at REAL)")
    db.execute("CREATE TABLE IF NOT EXISTS synthetic_retired_cards (relative_path TEXT NOT NULL, sha256 TEXT NOT NULL, archive_relative TEXT NOT NULL, source_event_id TEXT NOT NULL, source_refs TEXT NOT NULL, state TEXT NOT NULL, recorded_at REAL NOT NULL, PRIMARY KEY(relative_path,sha256))")
    try:
        with db:
            yield db
    finally:
        db.close()


def _project_key(project):
    return hashlib.sha256(str(Path(project).resolve()).encode()).hexdigest()


def _consent(root, project):
    """Owner-private grant is independent of repository-controlled settings."""
    root, project = Path(root).resolve(), Path(project).resolve()
    path = root / "consents" / (_project_key(project) + ".json")
    if path.is_symlink() or not path.is_file():
        return False
    try:
        value = read_private_json(path)
        return value == {"schema": "qmd-shared-consent-v1",
                         "projectRoot": str(project), "sharedRoot": str(root),
                         "capture": True, "localTraining": True,
                         "externalTeacher": False}
    except (ValueError, OSError):
        return False


def _current_config(root, project):
    if not _consent(root, project):
        return None
    try:
        import config as qmd_config
        found = qmd_config.find_project_config(str(project))
        cfg = found["config"]
        shared = cfg.get("sharedLearning", {})
        if (found["configFormat"] != "auto-context-dir"
                or Path(found["projectRoot"]).resolve() != Path(project).resolve()
                or shared.get("enabled") is not True
                or Path(shared.get("root") or "").resolve() != Path(root).resolve()
                or cfg.get("contextLearning", {}).get("capture") is not True
                or cfg.get("indexing") is False):
            return None
        return cfg
    except (OSError, ValueError):
        return None


def _case_active(root, row, *, cfg=None, private_db=None, source_cache=None,
                 deadline=None, ticket_validated=False,
                 create_private_state=True):
    """Read-only continuing-consent, tombstone and source-revision check."""
    if deadline is not None and time.monotonic() >= deadline:
        return False
    project, rid, sample_json = Path(row[0]), row[1], row[2]
    cfg = _current_config(root, project) if cfg is None else cfg
    if cfg is None:
        return False
    try:
        project = project.resolve(strict=True)
        sample = json.loads(sample_json)
        if sample.get('schema_version') == 4 and not ticket_validated:
            from .edit_ticket import validate_case
            from .history_activation import read_database
            shared_db = root / DATABASE_NAME
            if shared_db.is_symlink() or not shared_db.is_file():
                return False
            with read_database(shared_db) as provenance_db:
                validate_case(provenance_db, root, project, sample, {})
        if private_db is None:
            from .seam import _private_state
            from .history_activation import read_database
            state = _private_state(cfg, project, create=create_private_state)
            if state is None:
                return False
            db_path = state / "learning.sqlite3"
            if db_path.is_symlink() or not db_path.is_file():
                return False
            with read_database(db_path) as db:
                return _case_active(root, row, cfg=cfg, private_db=db,
                                    source_cache=source_cache, deadline=deadline,
                                    ticket_validated=True)
        if private_db.execute("SELECT 1 FROM tombstones WHERE request_id=?", (rid,)).fetchone():
            return False
        local = private_db.execute("SELECT body FROM samples WHERE request_id=?", (rid,)).fetchone()
        if local is None or digest(local[0]) != digest(sample_json):
            return False
        for candidate in sample["candidates"]:
            if deadline is not None and time.monotonic() >= deadline:
                return False
            collection, sep, relative = candidate["candidate_id"].partition("/")
            base = cfg.get("collectionPaths", {}).get(collection)
            if not sep or not isinstance(base, str):
                return False
            source_path = Path(base) / relative
            cache_key = (str(project), str(source_path), candidate["revision_sha256"])
            if source_cache is not None and cache_key in source_cache:
                good = source_cache[cache_key]
            else:
                content = snapshot_beneath(project, source_path, deadline=deadline)
                good = hashlib.sha256(content).hexdigest() == candidate["revision_sha256"]
                if source_cache is not None:
                    source_cache[cache_key] = good
            if not good:
                return False
        return True
    except (OSError, ValueError, KeyError, TypeError, UnicodeError, sqlite3.Error):
        return False


def _family_keys(project_key, sample, bodies):
    prompt = " ".join(sample["prompt"]["text"].casefold().split())
    keys = {"question:" + digest(prompt)}
    # A recurring background card may share a provisional split. The full
    # captured pool must pass the independent validation screen later.
    focused = set(sample["sampling"]["baseline_selected_ids"])
    for candidate in sample["candidates"]:
        cid = candidate["candidate_id"]
        if cid not in focused:
            continue
        keys.add("document:" + digest(canonical([project_key, cid])))
        keys.add("revision:" + candidate["revision_sha256"])
        keys.add("body:" + digest(bodies[cid]))
    return sorted(keys)


def _rows_for_model_view(rows, input_view):
    from .laya_adapter import EDIT_INPUT_VIEW
    if input_view == EDIT_INPUT_VIEW:
        return [row for row in rows if json.loads(row[3]).get('schema_version') == 4]
    return rows


def _synthetic_family_keys(project_key, episode, source_id, *, final=False):
    keys = {"target:" + digest(canonical([project_key, source_id]))}
    # An edit ticket uses episode=0 when its verified source path is not an
    # epNNN.md manuscript. Zero is an absence marker, not a shared source:
    # grouping every independent Settings edit under episode 0 fabricates a
    # common family. The target key above already pins all revisions of the
    # same edit path; the full candidate pool remains screened separately.
    if not final and not (episode == 0 and source_id.startswith("edit:")):
        keys.add("episode:" + digest(canonical([project_key, episode])))
    return keys


def _sync_synthetic_families(db, project_key):
    """Add pinned episode/target groups without changing old split labels."""
    rows = db.execute(
        "SELECT c.request_id,c.split,c.family_keys,t.episode,t.slot,t.source_id "
        "FROM cases c JOIN experiment_query_tickets t USING(request_id) "
        "WHERE c.project_key=?", (project_key,)).fetchall()
    pending = []
    proposed = {}
    for rid, split, encoded, episode, slot, source_id in rows:
        keys = _synthetic_family_keys(project_key, episode, source_id,
                                      final=split == "final")
        for family in keys:
            if family in proposed and proposed[family] != split:
                return False
            proposed[family] = split
            old = db.execute("SELECT split FROM families WHERE family_key=?",
                             (family,)).fetchone()
            if old and old[0] != split:
                return False
        pending.append((rid, split, encoded, keys))
    for rid, split, encoded, keys in pending:
        for family in keys:
            db.execute("INSERT OR IGNORE INTO families VALUES (?,?)", (family, split))
        stored = set(json.loads(encoded))
        if not keys <= stored:
            db.execute("UPDATE cases SET family_keys=? WHERE project_key=? AND request_id=?",
                       (canonical(sorted(stored | keys)), project_key, rid))
    return True


def _gold_fact_keys(row):
    project_key, sample_json, bodies_json, gold_json = row
    sample, bodies, gold = json.loads(sample_json), json.loads(bodies_json), json.loads(gold_json)
    selected = set(gold["selected_ids"])
    keys = set()
    for candidate in sample["candidates"]:
        cid = candidate["candidate_id"]
        if cid in selected:
            keys.update(("document:" + digest(canonical([project_key, cid])),
                         "revision:" + candidate["revision_sha256"],
                         "body:" + digest(bodies[cid])))
    return keys


def _evaluation_keys(row):
    sample = json.loads(row[3])
    prompt = " ".join(sample["prompt"]["text"].casefold().split())
    return {"question:" + digest(prompt)} | _gold_fact_keys(
        (row[0], row[3], row[4], row[5]))


def _query_family_keys(row):
    """Families whose gold labels must stay on one side of validation."""
    keys = _evaluation_keys(row)
    if len(row) > 9:
        keys.update(key for key in json.loads(row[9])
                    if key.startswith(("episode:", "target:")))
    return keys


def _candidate_exposure_keys(row):
    """Preserve every seen candidate for the separate corpus-disjoint metric."""
    sample, bodies = json.loads(row[3]), json.loads(row[4])
    keys = _evaluation_keys(row)
    for candidate in sample["candidates"]:
        cid = candidate["candidate_id"]
        keys.update(("document:" + digest(canonical([row[0], cid])),
                     "revision:" + candidate["revision_sha256"],
                     "body:" + digest(bodies[cid])))
    return keys


_training_exposure_keys = _candidate_exposure_keys
_selection_exposure_keys = _candidate_exposure_keys


def _case_reference(row):
    return {"projectRoot": row[7], "requestId": row[1],
            "inputSha256": row[2]}


def _training_provenance(rows, previous, *, validation_rows=None):
    inherited = previous.get("trainingProvenance") if previous and previous.get("experimental") else None
    if previous and previous.get("experimental") and not _valid_training_provenance(previous):
        raise ValueError("missing_inherited_training_provenance")
    refs = {canonical(ref): ref for ref in inherited["caseRefs"]} if inherited else {}
    keys = set(inherited["familyKeys"]) if inherited else set()
    selection_keys = set(inherited["selectionFamilyKeys"]) if inherited else set()
    query_keys = set(inherited.get("queryFamilyKeys", inherited["familyKeys"])) if inherited else set()
    selection_query_keys = set(inherited.get(
        "selectionQueryFamilyKeys", inherited["selectionFamilyKeys"])) if inherited else set()
    for row in rows:
        if row[6] == "train":
            ref = _case_reference(row)
            refs[canonical(ref)] = ref
            keys.update(_training_exposure_keys(row))
            query_keys.update(_query_family_keys(row))
    for row in (validation_rows if validation_rows is not None else
                [r for r in rows if r[6] == "holdout"]):
        selection_keys.update(_selection_exposure_keys(row))
        selection_query_keys.update(_query_family_keys(row))
    if not refs or not keys or len(refs) > 2000:
        raise ValueError("training_provenance_budget")
    return {"schema_version": 3, "caseRefs": [refs[key] for key in sorted(refs)],
            "familyKeys": sorted(keys),
            "selectionFamilyKeys": sorted(selection_keys),
            "queryFamilyKeys": sorted(query_keys),
            "selectionQueryFamilyKeys": sorted(selection_query_keys)}


def _valid_training_provenance(pointer):
    value = pointer.get("trainingProvenance")
    sources = pointer.get("sourceCases")
    validation = pointer.get("validationCases")
    if (not isinstance(value, dict) or value.get("schema_version") not in (2, 3)
            or set(value) != ({"schema_version", "caseRefs", "familyKeys", "selectionFamilyKeys"}
                              | ({"queryFamilyKeys", "selectionQueryFamilyKeys"}
                                 if value["schema_version"] == 3 else set()))
            or not isinstance(value["caseRefs"], list)
            or not value["caseRefs"] or len(value["caseRefs"]) > 2000
            or not isinstance(value["familyKeys"], list) or not value["familyKeys"]
            or len(value["familyKeys"]) > 10000
            or not isinstance(value["selectionFamilyKeys"], list)
            or len(value["selectionFamilyKeys"]) > 10000
            or (value["schema_version"] == 3 and any(
                not isinstance(value[name], list) or len(value[name]) > 10000
                for name in ("queryFamilyKeys", "selectionQueryFamilyKeys")))
            or not isinstance(sources, list) or not isinstance(validation, list)
            or not validation or len(validation) > 2000):
        return False
    try:
        refs = {canonical(ref) for ref in value["caseRefs"] if isinstance(ref, dict)
                and set(ref) == {"projectRoot", "requestId", "inputSha256"}}
        source_refs = {canonical(ref) for ref in sources if isinstance(ref, dict)
                       and set(ref) == {"projectRoot", "requestId", "inputSha256"}}
        validation_refs = {canonical(ref) for ref in validation if isinstance(ref, dict)
                           and set(ref) == {"projectRoot", "requestId", "inputSha256"}}
        return (len(refs) == len(value["caseRefs"]) and refs <= source_refs
                and len(validation_refs) == len(validation) and validation_refs <= source_refs
                and len(set(value["familyKeys"])) == len(value["familyKeys"])
                and len(set(value["selectionFamilyKeys"])) == len(value["selectionFamilyKeys"])
                and (value["schema_version"] == 2 or all(
                    len(set(value[name])) == len(value[name])
                    for name in ("queryFamilyKeys", "selectionQueryFamilyKeys")))
                and all(isinstance(key, str) and key.startswith(
                    ("question:", "episode:", "target:", "document:", "revision:", "body:"))
                    for key in value["familyKeys"] + value["selectionFamilyKeys"]
                    + value.get("queryFamilyKeys", []) + value.get("selectionQueryFamilyKeys", [])))
    except (TypeError, ValueError):
        return False


def _holdout_diagnostic(rows, inherited_keys=()):
    train = [r for r in rows if r[6] == "train"]
    holdout = [r for r in rows if r[6] == "holdout"]
    if not train or not holdout:
        return {"reason": "missing_split", "train": len(train),
                "holdout": len(holdout)}
    train_keys = set(inherited_keys) | set().union(*(_query_family_keys(r) for r in train))
    holdout_keys = set().union(*(_query_family_keys(r) for r in holdout))
    overlap = train_keys & holdout_keys
    if overlap:
        return {"reason": "shared_query_family", "train": len(train),
                "holdout": len(holdout), "overlap_families": len(overlap)}
    return None


def _cycle_holdouts(rows, previous):
    """Reuse pinned validation; admit new families only as fresh evidence."""
    training_keys = (set(previous["trainingProvenance"].get(
        "queryFamilyKeys", previous["trainingProvenance"]["familyKeys"]))
                     if previous else set())
    train = [row for row in rows if row[6] == "train"]
    holdout = [row for row in rows if row[6] == "holdout"]
    if not train or not holdout:
        return [], [], [], {"reason": "missing_split", "train": len(train),
                            "holdout": len(holdout)}
    training_keys.update(*(_query_family_keys(row) for row in train))
    if previous:
        pinned = {canonical(ref) for ref in previous["validationCases"]}
        reused = [row for row in holdout if canonical(_case_reference(row)) in pinned]
        if len(reused) != len(pinned):
            return [], [], [], {"reason": "missing_fixed_validation",
                                "expected": len(pinned), "available": len(reused)}
        incoming = [row for row in holdout if canonical(_case_reference(row)) not in pinned]
        reused_keys = set().union(*(_query_family_keys(row) for row in reused))
        if training_keys & reused_keys:
            return [], [], [], {"reason": "shared_query_family", "train": len(train),
                                "holdout": len(reused),
                                "overlap_families": len(training_keys & reused_keys)}
        selection_keys = (set().union(*(_query_family_keys(row) for row in reused)) |
                          set(previous["trainingProvenance"].get(
                              "selectionQueryFamilyKeys",
                              previous["trainingProvenance"]["selectionFamilyKeys"])))
    else:
        # A contaminated holdout must not enter validation, but it must not
        # veto a later independent holdout forever. Screen each candidate and
        # retain the exclusions in the cycle evidence.
        reused, incoming = [], holdout
        selection_keys = set()
    fresh, excluded = _screen_observation(incoming, training_keys,
                                         selection_keys=selection_keys)
    required = 2 if previous else 1
    if len(fresh) < required:
        if not previous and not fresh and any(
                item['reason'] == 'training_family_overlap' for item in excluded):
            return [], [], excluded, {"reason": "shared_query_family",
                "train": len(train), "holdout": len(holdout),
                "overlap_families": sum(item['reason'] == 'training_family_overlap'
                                        for item in excluded), "excluded": excluded}
        return [], [], excluded, {"reason": "insufficient_fresh_holdout",
            "fresh": len(fresh), "required": required, "reused": len(reused),
            "excluded": excluded}
    return reused + fresh, fresh, excluded, None


def _screen_observation(rows, training_keys, *, selection_keys=()):
    """Keep only query and answer families unseen by training or selection."""
    accepted, excluded, observed_keys = [], [], set()
    training_keys = set(training_keys)
    selection_keys = set(selection_keys)
    for row in rows:
        keys = _query_family_keys(row)
        if keys & training_keys:
            reason = "training_family_overlap"
        elif keys & selection_keys:
            reason = "selection_family_overlap"
        elif keys & observed_keys:
            reason = "repeated_holdout_family"
        else:
            accepted.append(row)
            observed_keys.update(keys)
            continue
        excluded.append({"case_hash": digest(canonical(_case_reference(row)))[:16],
                         "reason": reason})
    return accepted, excluded


def _candidate_disjoint_metric(folder, baseline_label, trial_label, rows,
                               *, training_keys=(), selection_keys=()):
    """Secondary corpus-disjoint score; never controls the query-family gate."""
    seen = set(training_keys) | set(selection_keys)
    clean = []
    overlap_questions = overlap_families = 0
    for row in rows:
        keys = _candidate_exposure_keys(row)
        overlap = keys & seen
        if overlap:
            overlap_questions += 1
            overlap_families += len(overlap)
        else:
            clean.append(row)
        seen.update(keys)
    report = {"status": "available" if clean else "unavailable",
              "questions": len(clean), "overlap_questions": overlap_questions,
              "overlap_families": overlap_families}
    if not clean:
        report["reason"] = "no_disjoint_candidate_pool"
        return report
    cases = [_case(row[:6]) for row in clean]
    ids = {case["request_id"] for case in cases}
    for name, label in (("incumbent", baseline_label), ("candidate", trial_label)):
        full = read_private_json(folder / (label + ".json"))
        selected = {"predictions": [prediction for prediction in full["predictions"]
                                    if prediction["request_id"] in ids]}
        report[name] = _score(cases, selected)
    return report


def _reconcile_cases(root):
    with _db(root) as db:
        rows = db.execute("SELECT project_key,request_id,project_root,sample FROM cases WHERE use_state='active' AND status!='awaiting_local'").fetchall()
    expired = [(key, rid) for key, rid, project, sample in rows
               if not _case_active(root, (project, rid, sample))]
    if expired:
        with _db(root) as db:
            for key, rid in expired:
                db.execute("UPDATE cases SET use_state='historical',error='consent_or_source_changed' WHERE project_key=? AND request_id=? AND use_state='active'",
                           (key, rid))
    return len(expired)


def recover_awaiting_edits(root, project, *, request_ids=None):
    """Crash recovery after ticket binding but before private capture/activation."""
    from .edit_ticket import TTL
    with _db(root) as db:
        rows = db.execute("SELECT project_key,request_id,project_root,sample,captured_at FROM cases WHERE project_root=? AND status='awaiting_local' AND use_state='active'",
                          (str(project),)).fetchall()
    if request_ids is not None:
        selected = set(request_ids)
        rows = [row for row in rows if row[1] in selected]
    for key, rid, project_root, sample, captured_at in rows:
        if _case_active(root, (project_root, rid, sample)):
            with _db(root) as db:
                db.execute("UPDATE cases SET status='queued' WHERE project_key=? AND request_id=? AND status='awaiting_local' AND use_state='active'",
                           (key, rid))
        elif time.time() - captured_at >= TTL:
            with _db(root) as db:
                db.execute("UPDATE cases SET use_state='historical',error='local_capture_not_completed' WHERE project_key=? AND request_id=? AND status='awaiting_local' AND use_state='active'",
                           (key, rid))


def recover_scope_deferred_edits(root, project, *, request_ids=None):
    """Retry only proven pre-egress edit cases after global QMD settlement.

    The caller must have passed policy_for on the current synthetic bundle.
    A case with any reserved gold call remains pending for manual review.
    """
    root, project = Path(root), Path(project)
    with _db(root) as db:
        rows = db.execute(
            "SELECT c.project_key,c.request_id,c.project_root,c.sample,c.error "
            "FROM cases c JOIN edit_tickets t ON t.request_id=c.request_id "
            "AND t.project_root=c.project_root AND t.input_sha256=c.input_sha256 "
            "WHERE c.project_root=? AND c.use_state='active' "
            "AND c.status='pending_review' AND c.error IN (?,?,?) "
            "AND c.first IS NULL AND c.second IS NULL AND c.gold IS NULL "
            "AND NOT EXISTS (SELECT 1 FROM experiment_calls e WHERE "
            "e.call_key IN ('gold:'||c.request_id||':first_running', "
            "'gold:'||c.request_id||':second_running'))",
            (str(project), *RETRYABLE_SCOPE_SETTLEMENT)).fetchall()
    if request_ids is not None:
        selected = set(request_ids)
        rows = [row for row in rows if row[1] in selected]
    recovered = 0
    for key, rid, owner, sample, reason in rows:
        if not _case_active(root, (owner, rid, sample)):
            continue
        with _db(root) as db:
            recovered += db.execute(
                "UPDATE cases SET status='queued',error=NULL WHERE project_key=? "
                "AND request_id=? AND use_state='active' AND status='pending_review' "
                "AND error=? AND first IS NULL AND second IS NULL AND gold IS NULL "
                "AND NOT EXISTS (SELECT 1 FROM experiment_calls e WHERE "
                "e.call_key IN ('gold:'||cases.request_id||':first_running', "
                "'gold:'||cases.request_id||':second_running'))",
                (key, rid, reason)).rowcount
    return recovered


def trial_eligible(root, *, deadline=None, max_seconds=.2):
    """Verify continuing grants; prompt checks keep a short deadline."""
    path = Path(root) / "experimental-checkpoint.json"
    if not path.is_file() or path.is_symlink():
        return False
    try:
        budget = min(max(float(max_seconds), .001), 5.)
        deadline = min(time.monotonic() + budget, deadline) if deadline is not None else time.monotonic() + budget
        # A 100-episode experiment can pin more than 512 KiB of exact source
        # references while remaining far below the 16 MiB private JSON cap.
        if path.stat().st_size > 2 * 1024 * 1024 or time.monotonic() >= deadline:
            return False
        pointer = read_private_json(path)
        sources = pointer.get("sourceCases")
        from .laya_adapter import EXPERIMENT_INPUT_VIEW, EDIT_INPUT_VIEW
        if (pointer.get("schema_version") != 1 or not pointer.get("experimental")
                or pointer.get("model_input_view") not in (None, EXPERIMENT_INPUT_VIEW,
                                                           EDIT_INPUT_VIEW)
                or not isinstance(sources, list) or not sources or len(sources) > 2000
                or not _valid_training_provenance(pointer)):
            return False
        db_path = Path(root) / DATABASE_NAME
        if db_path.is_symlink() or not db_path.is_file():
            return False
        with ExitStack() as stack:
            from .history_activation import read_database
            db = stack.enter_context(read_database(db_path))
            db.execute("PRAGMA busy_timeout=20")
            projects, private_dbs, source_cache = {}, {}, {}
            for source in sources:
                if time.monotonic() >= deadline:
                    return False
                if not isinstance(source, dict) or set(source) != {"projectRoot", "requestId", "inputSha256"}:
                    return False
                project = source["projectRoot"]
                if project not in projects:
                    cfg = _current_config(root, project)
                    if cfg is None:
                        return False
                    from .seam import _private_state
                    state = _private_state(cfg, Path(project).resolve(strict=True))
                    if state is None:
                        return False
                    local_path = state / "learning.sqlite3"
                    if local_path.is_symlink() or not local_path.is_file():
                        return False
                    projects[project] = cfg
                    private_dbs[project] = stack.enter_context(read_database(local_path))
                row = db.execute("SELECT input_sha256,sample,use_state,status FROM cases WHERE project_key=? AND request_id=?",
                                 (_project_key(project), source["requestId"])).fetchone()
                if (row is None or row[0] != source["inputSha256"] or row[2] != "active"
                        or row[3] != "gold"
                        or not _case_active(root, (project, source["requestId"], row[1]),
                                            cfg=projects[project], private_db=private_dbs[project],
                                            source_cache=source_cache, deadline=deadline)):
                    return False
            return time.monotonic() < deadline and all(_consent(root, p) for p in projects)
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
        return False


def enqueue(config, project, sample, compact_sources, *, recovery_call_key=None):
    """Copy an exact eligible capture once; no current source reread is needed."""
    root = _safe_root(config, project)
    if root is None:
        return "disabled"
    worker_policy = _policy(root)
    if not _consent(root, project):
        return "missing_shared_consent"
    validate_request(sample)
    if (sample["host"] not in ("codex", "claude") or sample["prompt"]["truncated"]
            or sample["schema_version"] < 2
            or not sample["sampling"]["complete_within_returned_bound"]
            or not sample["candidates"]):
        return "ineligible"
    # The older returned-pool contract keeps its original shared admission.
    # Only the explicit candidate_limit contract can request the wider pool.
    if len(sample["candidates"]) > (30 if sample['schema_version'] in (3, 4) else 15):
        return ("ineligible_pool_over_30" if sample['schema_version'] in (3, 4)
                else "ineligible_pool_over_15")
    bodies = {}
    for candidate in sample["candidates"]:
        cid = candidate["candidate_id"]
        if cid not in compact_sources:
            return "incomplete_body"
        bodies[cid] = derive(compact_sources[cid], candidate["revision_sha256"])["body_text"]
    key = _project_key(project)
    families = _family_keys(key, sample, bodies)
    from .synthetic_experiment import load as experiment_control, bind_capture
    try:
        experiment = experiment_control(root, project)
    except ValueError:
        return "experiment_control_invalid"
    if (root / "synthetic-experiment.json").exists() and experiment is None:
        return "experiment_project_mismatch"
    edit = sample['schema_version'] == 4
    if edit and experiment is None:
        return 'edit_experiment_required'
    encoded = canonical(sample)
    parent_call_key = None
    with _db(root) as db:
        old = db.execute("SELECT input_sha256 FROM cases WHERE project_key=? AND request_id=?",
                         (key, sample["request_id"])).fetchone()
        if old:
            return "duplicate" if old[0] == digest(encoded) else "conflict"
        ticket = None
        if experiment is not None:
            try:
                if edit:
                    from .edit_ticket import bind as bind_edit
                    ticket = bind_edit(db, root, project, sample, bodies,
                                       digest(encoded), commit=False)
                else:
                    ticket = bind_capture(db, root, project, *experiment, sample, bodies,
                                          digest(encoded), commit=False,
                                          recovery_call_key=recovery_call_key)
            except ValueError as exc:
                return str(exc)[:80]
        final_reserved = ticket is not None and ticket[0] >= 81 and ticket[1] == 2
        if experiment is not None:
            if not _sync_synthetic_families(db, key):
                return "family_split_conflict"
            families = sorted(set(families) | _synthetic_family_keys(
                key, ticket[0], ticket[2], final=final_reserved))
        if (db.execute("SELECT COUNT(*) FROM cases").fetchone()[0] >= 2000
                or (root / DATABASE_NAME).stat().st_size > 512 * 1024 * 1024):
            return "queue_full"
        # A target/episode can have several paraphrased questions. Their
        # provisional split follows the same pinned family, even if a common
        # background card appears in every retrieved pool.
        split_families = [family for family in families if family.startswith(
            ('question:', 'episode:', 'target:'))]
        if sum(family.startswith('question:') for family in split_families) != 1:
            return "invalid_question_family"
        assigned = {row[0] for row in db.execute(
            "SELECT split FROM families WHERE family_key IN (" +
            ",".join("?" for _ in split_families) + ")", split_families)}
        if len(assigned) > 1:
            return "family_split_conflict"
        ordinal = db.execute("SELECT COUNT(DISTINCT family_key) FROM families WHERE family_key LIKE 'question:%'").fetchone()[0]
        # Count gold holdouts that the actual validation screen could use.
        # Repeated questions and facts exposed to training do not fill the
        # quota. Reserve one slot for each still-running holdout question so
        # an asynchronous gold worker does not overschedule validation.
        gold_rows = db.execute(
            "SELECT project_key,request_id,input_sha256,sample,bodies,gold,split,"
            "project_root,corpus_fingerprint,family_keys,trained_cycle FROM cases "
            "WHERE status='gold' AND use_state='active' AND split!='final' "
            "ORDER BY captured_at,request_id").fetchall()
        train_keys = set().union(*(_query_family_keys(row)
                                   for row in gold_rows if row[6] == 'train'))
        screened, _ = _screen_observation(
            (row for row in gold_rows if row[6] == 'holdout'), train_keys)
        accepted_questions = {key for row in screened for key in _evaluation_keys(row)
                              if key.startswith('question:')}
        pending_questions = {key for (encoded_families,) in db.execute(
            "SELECT family_keys FROM cases WHERE split='holdout' AND use_state='active' "
            "AND status IN ('queued','first_running','first_done','second_running')")
            for key in json.loads(encoded_families) if key.startswith('question:')}
        active_holdouts = len(screened) + len(pending_questions - accepted_questions)
        if final_reserved and assigned and assigned != {"final"}:
            return "final_family_conflict"
        if not final_reserved and "final" in assigned:
            return "final_family_conflict"
        # A scheduled fifth question may inherit a train family from a
        # baseline distractor. Do not relabel it or leak that family into
        # validation; assign the next genuinely unassigned question to the
        # missing holdout slot instead of waiting until question ten.
        holdout_due = active_holdouts < (ordinal + 1) // 5
        split = ("final" if final_reserved else next(iter(assigned)) if assigned
                 else "holdout" if holdout_due else "train")
        for family in split_families:
            db.execute("INSERT OR IGNORE INTO families VALUES (?,?)", (family, split))
        db.execute("INSERT INTO cases (project_key,project_root,request_id,input_sha256,sample,bodies,corpus_fingerprint,captured_at,split,family_keys,use_state,status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                   (key, str(Path(project).resolve()), sample["request_id"], digest(encoded),
                    encoded, canonical(bodies), sample["sampling"]["index_revision"],
                    time.time(), split, canonical(families), "active",
                    'awaiting_local' if edit else "queued"))
        if ticket is not None:
            if edit:
                bind_edit(db, root, project, sample, bodies,
                          digest(encoded), commit=True)
                parent_call_key = sample['edit_event']['ticket_id']
            else:
                bind_capture(db, root, project, *experiment, sample, bodies,
                             digest(encoded), commit=True,
                             recovery_call_key=recovery_call_key)
                parent_call_key = db.execute(
                    "SELECT call_key FROM experiment_query_tickets WHERE request_id=?",
                    (sample["request_id"],)).fetchone()[0]
        from .history_activation import record_case_mapping
        record_case_mapping(db, project, sample)
    if ticket is not None and not edit:
        for phase in ("first", "second"):
            job_telemetry.emit(root / "job-telemetry.jsonl", "queued",
                               job_id=f"gold:{sample['request_id']}:{phase}",
                               parent_id=parent_call_key, episode=ticket[0],
                               op=f"gold_{phase}", runtime_kind="remote_cli",
                               model_version=worker_policy["goldModel"])
    return 'awaiting_local' if edit else "queued"


def activate_edit_case(config, project, sample, *, captured):
    """Expose a bound edit to the gold worker only after private capture."""
    root = _safe_root(config, project)
    if root is None or sample.get('schema_version') != 4:
        return 'edit_activation_unavailable'
    key, rid = _project_key(project), sample['request_id']
    encoded = canonical(sample)
    if not captured:
        with _db(root) as db:
            db.execute("UPDATE cases SET use_state='historical',error='local_capture_failed' WHERE project_key=? AND request_id=? AND input_sha256=? AND status='awaiting_local'",
                       (key, rid, digest(encoded)))
        return 'local_capture_failed'
    if not _case_active(root, (str(Path(project).resolve()), rid, encoded)):
        return 'edit_activation_unavailable'
    try:
        with _db(root) as db:
            if sample['edit_event'].get('deferred_capture') is True:
                from .deferred_edit import finalize_before_gold
                finalize_before_gold(db, Path(project).resolve(strict=True),
                                     sample, digest(encoded))
            changed = db.execute("UPDATE cases SET status='queued' WHERE project_key=? AND request_id=? AND input_sha256=? AND status='awaiting_local' AND use_state='active'",
                                 (key, rid, digest(encoded))).rowcount
            if changed != 1:
                raise ValueError('edit_activation_conflict')
    except (ValueError, OSError, sqlite3.Error):
        return 'edit_activation_conflict'
    return 'queued'


def resource_report(policy, *, detailed=False, admission=True,deadline=None):
    """OS adapter observations plus explicit configured memory policy."""
    from resource_platform import adapter_for,normal_pressure,number
    from .bounded_calibration import pressure_ready,verdict
    from .memory_policy import assess
    from .resource_coordination import observation_fresh
    started=time.monotonic()
    end=min(started+2,deadline) if deadline is not None else started+2
    report=adapter_for(sys.platform).observe(path_factory=Path,run=subprocess.run,
        clock=time.monotonic,cpu_count=os.cpu_count,loadavg=getattr(os,'getloadavg',None),
        deadline=end,detailed=detailed)
    available,load=report['available_mib'],report['load_per_cpu']
    report['ready']=(number(load) and load<=policy['maxLoadPerCpu']
        and number(available) and available>=policy['minAvailableMib']
        and pressure_ready(report,policy))
    if 'calibration' in policy:report['calibration_admission']=verdict(report,policy)
    if 'adaptiveMemory' in policy:
        result=assess(report,policy,admission=admission)
        report.update(ready=result['ready'],memory_admission=result)
        if result['training_limits'] is not None:report['training_limits']=result['training_limits']
    report['observation_status']='fresh' if observation_fresh(report) else 'stale'
    report['ready']=report['ready'] and report['observation_status']=='fresh' and time.monotonic()<end
    return report


def _meta(db, key, default):
    row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else default


def _put_meta(db, key, value):
    db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, canonical(value)))


def _queue_summary(db, table, namespace):
    """Namespace-scoped aggregates without opening captures or creating tables."""
    if table not in ('learning_jobs', 'learning_evaluations'):
        raise ValueError('queue_summary_table_invalid')
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
        return {}
    columns={row[1] for row in db.execute(f'PRAGMA table_info({table})')}
    if not {'namespace','status','reason','retry_at'} <= columns:
        return {'migration_required':True}
    counts=dict(db.execute(f'SELECT status,COUNT(*) FROM {table} WHERE namespace=? GROUP BY status',
                           (namespace,)))
    reasons=[{'status':state,'reason':reason,'count':count} for state,reason,count in db.execute(
        f"SELECT status,reason,COUNT(*) FROM {table} WHERE namespace=? "
        "AND status NOT IN ('staged','completed') GROUP BY status,reason ORDER BY status,reason",
        (namespace,))]
    next_retry=db.execute(f"SELECT MIN(retry_at) FROM {table} WHERE namespace=? "
                          "AND status='deferred' AND retry_at>0",(namespace,)).fetchone()[0]
    return {'counts':counts,'reasons':reasons,'next_retry':next_retry}


def status(root):
    """Read durable counts without opening any captured body."""
    with _db(root) as db:
        counts = dict(db.execute("SELECT status,COUNT(*) FROM cases GROUP BY status"))
        use_states = dict(db.execute("SELECT use_state,COUNT(*) FROM cases GROUP BY use_state"))
        latest = db.execute("SELECT cycle_id,status,evidence FROM cycles ORDER BY started_at DESC LIMIT 1").fetchone()
        namespace=digest(str(Path(root).resolve()))
        learning_queue=_queue_summary(db,'learning_jobs',namespace)
        quality_queue=_queue_summary(db,'learning_evaluations',namespace)
        learning_jobs=learning_queue.get('counts',{})
        return {"status": "ok", "cases": counts, "use_states": use_states,"learning_jobs":learning_jobs,
                "learning_queue":learning_queue,"quality_queue":quality_queue,
                "gold_calls_used": _meta(db, "gold_calls_used", 0),
                "next_batch": _meta(db, "next_batch", None),
                "holdout_diagnostic": _meta(db, "holdout_diagnostic", None),
                "latest_cycle": {"id": latest[0], "status": latest[1],
                                 "evidence": json.loads(latest[2])} if latest else None}


def sync_reviewed(config, project):
    """Import only independently reviewed local gold from an enrolled project."""
    root = _safe_root(config, project)
    if root is None:
        return {"status": "disabled"}
    from .seam import _private_state
    from .store import database
    state = _private_state(config, Path(project).resolve())
    if state is None:
        return {"status": "missing_project_state"}
    key = _project_key(project)
    imported = 0
    with _db(root) as shared_db, database(state) as project_db:
        if not project_db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='selection_gold'").fetchone():
            return {"status": "waiting_review"}
        rows = shared_db.execute("SELECT request_id,input_sha256,sample FROM cases WHERE project_key=? AND status='queued' AND use_state='active'",
                                 (key,)).fetchall()
        for rid, expected, sample_json in rows:
            if not _case_active(root, (str(project), rid, sample_json)):
                shared_db.execute("UPDATE cases SET use_state='historical',error='consent_or_source_changed' WHERE project_key=? AND request_id=?",
                                  (key, rid))
                continue
            source = project_db.execute("SELECT body FROM samples WHERE request_id=?", (rid,)).fetchone()
            selected = project_db.execute("SELECT body,review FROM selection_gold WHERE request_id=?", (rid,)).fetchone()
            if not source or digest(source[0]) != expected or not selected or not selected[1]:
                continue
            sample, selection, review = json.loads(source[0]), json.loads(selected[0]), json.loads(selected[1])
            if selection["input_sha256"] != expected or review["selection_sha256"] != digest(selected[0]):
                continue
            labels = project_db.execute("SELECT candidate_id,body,review FROM labels WHERE request_id=?", (rid,)).fetchall()
            if len(labels) != len(sample["candidates"]) or any(not item[2] for item in labels):
                continue
            from .offline import review_digest
            try:
                checked = {}
                for cid, body, review_body in labels:
                    label = validate_label(json.loads(body), sample)
                    review_record = json.loads(review_body)
                    if (review_record.get("evidence_sha256") != review_digest(label)
                            or (label["schema_version"] == 2
                                and review_record.get("input_sha256") != expected)
                            or label["abstain"]):
                        raise ValueError("invalid_reviewed_label")
                    checked[cid] = label
                if set(checked) != {c["candidate_id"] for c in sample["candidates"]}:
                    raise ValueError("incomplete_reviewed_labels")
            except (ValueError, TypeError, KeyError):
                continue
            relevant = {cid for cid, label in checked.items() if label["relevance"] != "irrelevant"}
            if not set(selection["selected_ids"]) <= relevant:
                continue
            gold = {"selected_ids": selection["selected_ids"], "input_sha256": expected,
                    "review": "explicit_project_reviews"}
            shared_db.execute("UPDATE cases SET status='gold',gold=? WHERE project_key=? AND request_id=? AND status='queued'",
                              (canonical(gold), key, rid))
            imported += 1
    return {"status": "synced", "imported": imported}


def _case(row):
    project_key, rid, input_sha, sample_json, bodies_json, gold_json = row
    sample, bodies, gold = json.loads(sample_json), json.loads(bodies_json), json.loads(gold_json)
    if digest(sample_json) != input_sha or gold["input_sha256"] != input_sha:
        raise ValueError("shared_case_changed")
    ids = [c["candidate_id"] for c in sample["candidates"]]
    if not isinstance(gold["selected_ids"], list) or len(gold["selected_ids"]) > 3 or not set(gold["selected_ids"]) <= set(ids):
        raise ValueError("invalid_shared_gold")
    return {"request_id": project_key[:12] + ":" + rid, "input_sha256": input_sha,
            "prompt": sample["prompt"]["text"],
            "candidates": [{"id": c["candidate_id"], "input_kind": "full_compact_wiki_body",
                            "input_text": bodies[c["candidate_id"]]} for c in sample["candidates"]],
            "selected_ids": gold["selected_ids"], "project_key": project_key}


def _score(cases, predictions):
    by_id = {p["request_id"]: p for p in predictions["predictions"]}
    if len(by_id) != len(cases):
        raise ValueError("incomplete_shared_prediction")
    per_project = {}
    total = correct = 0
    needed=retrieved=irrelevant=empty=empty_correct=0
    for case in cases:
        prediction = by_id.pop(case["request_id"])
        if prediction["input_sha256"] != case["input_sha256"]:
            raise ValueError("shared_prediction_changed")
        selected = prediction["selected_ids"]
        if not isinstance(selected, list) or len(selected) > 3 or not set(selected) <= {c["id"] for c in case["candidates"]}:
            raise ValueError("invalid_shared_prediction")
        good = set(selected) == set(case["selected_ids"])
        gold=set(case['selected_ids']);chosen=set(selected)
        needed+=len(gold);retrieved+=len(gold&chosen);irrelevant+=len(chosen-gold)
        if not gold:empty+=1;empty_correct+=int(not chosen)
        group = per_project.setdefault(case["project_key"], {"questions": 0, "exact": 0})
        group["questions"] += 1
        group["exact"] += good
        total += 1
        correct += good
    if by_id:
        raise ValueError("unexpected_shared_prediction")
    return {"questions": total, "exact": correct, "exact_rate": correct / total,
            "projects": per_project,'necessary_recall':retrieved/needed if needed else 1,
            'irrelevant_injection_rate':irrelevant/total,'none_exact_rate':empty_correct/empty if empty else 1}


def _predict(trainer, folder, artifact, cases, label, *, input_view=None,cache_epoch=None):
    bare = [{k: v for k, v in case.items() if k not in ("selected_ids", "project_key")}
            for case in cases]
    output = folder / (label + ".json")
    pin_path = folder / (label + ".pin.json")
    pin = {"artifact_sha256": _hash(artifact), "cases_sha256": digest(canonical(bare)),
           "model_input_view": input_view}
    if cache_epoch is not None:pin['evaluation_epoch']=cache_epoch
    if output.exists() and pin_path.exists() and read_private_json(pin_path) == pin:
        answer, resources = read_private_json(output), {"resumed": True}
    else:
        # An interrupted or earlier attempt may leave a result for other
        # weights. Never reuse it on the strength of its filename alone.
        output.unlink(missing_ok=True)
        pin_path.unlink(missing_ok=True)
        request = {"schema_version": 1, "artifact_path": str(artifact), "cases": bare}
        if input_view is not None:
            request["model_input_view"] = input_view
        answer, resources = trainer.call("predict", request, output, cwd=folder)
        _atomic_json(pin_path, pin)
    return _score(cases, answer), resources


def _publish_evaluated(root, cycle_id, evidence, rows):
    if evidence.get('authorizationSha256') is not None:
        # A staged automatic cohort can never silently enter this legacy
        # publication path, including after its grant expires or is removed.
        raise ValueError('automatic_cohort_publication_not_authorized')
    if (root / "synthetic-experiment.json").exists():
        from .synthetic_experiment import load as experiment_control, check_disk
        from .synthetic_experiment import project_for
        control, _ = experiment_control(root, project_for(root))
        check_disk(root, control)
    expected = digest(canonical([(r[0], r[1], r[2], r[5]) for r in rows]))
    if evidence["input_sha256"] != expected:
        raise ValueError("shared_queue_changed_during_cycle")
    if any(not _case_active(root, (r[7], r[1], r[3])) for r in rows):
        raise ValueError("shared_case_revoked_during_cycle")
    path = root / "experimental-checkpoint.json"
    current = read_private_json(path) if path.exists() else None
    record = evidence["pointer"]
    previous = evidence["prior_pointer"]
    validation, fresh, excluded, diagnostic = _cycle_holdouts(
        rows, evidence.get("selection_fence", previous))
    validation_refs = sorted((canonical(_case_reference(row)) for row in validation))
    if (not _valid_training_provenance(record) or diagnostic
            or validation_refs != sorted(canonical(ref) for ref in record["validationCases"])
            or len(fresh) != evidence["metrics"]["fresh_holdout_questions"]
            or excluded != evidence["metrics"]["excluded_holdout"]):
        raise ValueError("shared_evaluation_provenance_invalid")
    retention_reason = _regression_reason(
        evidence["metrics"]["fresh_incumbent"],
        evidence["metrics"]["fresh_candidate"])
    if current != evidence["prior_pointer"] and not (
            retention_reason is None and current == record):
        raise ValueError("shared_pointer_changed")
    if retention_reason is None and current != record:
        _atomic_json(path, record)
    metrics = {**evidence["metrics"], "applied": retention_reason is None,
               "retention_reason": retention_reason}
    terminal = "complete" if retention_reason is None else "retained"
    with _db(root) as db:
        latest = _rows_for_model_view(db.execute("SELECT project_key,request_id,input_sha256,sample,bodies,gold,split,project_root FROM cases WHERE status='gold' AND use_state='active' AND split!='final' ORDER BY captured_at,request_id").fetchall(), record.get('model_input_view'))
        if digest(canonical([(r[0], r[1], r[2], r[5]) for r in latest])) != expected:
            raise ValueError("shared_queue_changed_during_cycle")
        terminal_evidence = (canonical(metrics) if terminal == "complete" else
                             canonical({**evidence, "metrics": metrics}))
        db.execute("UPDATE cycles SET status=?,evidence=?,finished_at=? WHERE cycle_id=? AND status='evaluated'",
                   (terminal, terminal_evidence, time.time(), cycle_id))
        for case in rows:
            db.execute("UPDATE cases SET trained_cycle=? WHERE project_key=? AND request_id=? AND status='gold' AND use_state='active' AND trained_cycle IS NULL",
                       (cycle_id, case[0], case[1]))
        _put_meta(db, "next_batch", evidence["metrics"]["next_batch"])
        _put_meta(db, "holdout_diagnostic", None)
    return {"status": terminal, "cycle_id": cycle_id, "metrics": metrics}


def _regression_reason(incumbent, candidate):
    if candidate["exact"] < incumbent["exact"]:
        return "aggregate_regression"
    if any(candidate["projects"].get(key, {}).get("exact", 0) < group["exact"]
           for key, group in incumbent["projects"].items()):
        return "project_regression"
    return None


def _selection_fence(db, current):
    """Carry reviewed holdouts forward after retaining an incumbent head.

    A rejected artifact is never an inference pointer, but the validation
    families it saw must not become fresh holdouts in the next cycle.
    """
    row = db.execute("SELECT evidence,finished_at FROM cycles WHERE status='retained' "
                     "ORDER BY finished_at DESC LIMIT 1").fetchone()
    if row is None:
        return current
    if current is not None:
        applied = db.execute("SELECT finished_at FROM cycles WHERE cycle_id=? "
                             "AND status='complete'", (current.get('cycle_id'),)).fetchone()
        if applied is not None and applied[0] is not None and applied[0] > row[1]:
            return current
    try:
        retained = json.loads(row[0])
        pointer = retained['pointer']
        if (retained['prior_pointer'] != current or
                not _valid_training_provenance(pointer) or
                retained['metrics']['applied'] is not False):
            raise ValueError('shared_retained_fence_invalid')
        return pointer
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError('shared_retained_fence_invalid') from exc


def _cadence(policy, threshold, incumbent, candidate):
    """Use relative holdout quality as well as the absolute pacing target."""
    reason = _regression_reason(incumbent, candidate)
    if reason is None and candidate["exact_rate"] < policy["targetExact"]:
        reason = "below_target"
    elif reason is None:
        reason = "at_target"
    proposed = threshold * 2 if reason == "at_target" else threshold // 2
    return min(policy["batchMax"], max(policy["batchMin"], proposed)), reason


def _due_threshold(rows, threshold, policy):
    """Fresh document/revision families advance a review, but still need N cases."""
    prior = [row for row in rows if row[10] is not None]
    incoming = [row for row in rows if row[10] is None]
    if not prior or not incoming:
        return threshold, "count"
    known = set().union(*(set(json.loads(row[9])) for row in prior))
    known_corpora = {row[8] for row in prior}
    for row in incoming:
        keys = json.loads(row[9])
        if row[8] not in known_corpora:
            return min(threshold, policy["batchMin"]), "corpus_change"
        if any(key.startswith(("document:", "revision:", "body:")) and key not in known
               for key in keys):
            return min(threshold, policy["batchMin"]), "document_change"
    # Cheap subject-shift signal for questions about unchanged documents.
    # It cannot establish semantic novelty; low lexical overlap only changes
    # scheduling and never counts as evidence of better model quality.
    stop = {"what", "which", "where", "when", "who", "why", "how", "does",
            "about", "from", "with", "this", "that", "the", "and", "for"}
    def topic(row):
        prompt = json.loads(row[3])["prompt"]["text"].casefold()
        return {word for word in re.findall(r"[^\W_]{2,}", prompt) if word not in stop}
    previous = [topic(row) for row in prior]
    for row in incoming:
        current = topic(row)
        if (len(current) >= 2 and previous
                and max((len(current & old) / len(current | old) if old else 0)
                        for old in previous) < .2):
            return min(threshold, policy["batchMin"]), "new_question_topic"
    return threshold, "count"


def train_due(root, policy, *, trainer_factory=None):
    """One globally serialized, local-only cumulative cycle."""
    if _historical_trainer_policy(policy):
        return {"status": "runtime_approval_required"}
    if policy.get('_runtime_authorization_v2') is True:
        from .runtime_authorization import require_operation
        from .automatic_staging import train_due_staged
        try:
            grant = require_operation(root, policy, 'local_train')
        except ResourceDeferred as exc:
            return {'status':'deferred_resource','reason':str(exc)}
        except ValueError:
            return {'status': 'runtime_approval_required'}
        return train_due_staged(root, policy, grant,
                                trainer_factory=trainer_factory)
    report = resource_report(policy, detailed=True)
    if not report["ready"]:
        from .local_cycle import record_resource_defer
        record_resource_defer(root, report)
        return {"status": "deferred_resource", "resources": report}
    _reconcile_cases(root)
    pointer_path = root / "experimental-checkpoint.json"
    current = read_private_json(pointer_path) if pointer_path.exists() else None
    from .laya_adapter import EXPERIMENT_INPUT_VIEW, EDIT_INPUT_VIEW, model_identity
    input_view = current.get("model_input_view") if current else policy.get(
        'modelInputView', EXPERIMENT_INPUT_VIEW)
    if (policy.get('modelInputView') == EDIT_INPUT_VIEW and current is not None
            and input_view != EDIT_INPUT_VIEW):
        return {'status': 'multilingual_migration_required'}
    if (input_view == EDIT_INPUT_VIEW and
            policy.get('modelInputView') != EDIT_INPUT_VIEW):
        return {'status': 'multilingual_policy_revoked'}
    if current and (not _valid_training_provenance(current) or not trial_eligible(root, max_seconds=5)):
        return {"status": "uncertain_training_provenance"}
    with _db(root) as db:
        all_rows = db.execute("SELECT project_key,request_id,input_sha256,sample,bodies,gold,split,project_root,corpus_fingerprint,family_keys,trained_cycle FROM cases WHERE status='gold' AND use_state='active' AND split!='final' ORDER BY captured_at,request_id").fetchall()
        if any(json.loads(row[3]).get('schema_version') == 4 for row in all_rows):
            if (policy.get('modelInputView') != EDIT_INPUT_VIEW or
                    input_view != EDIT_INPUT_VIEW):
                return {'status': 'multilingual_migration_required'}
        rows = _rows_for_model_view(all_rows, input_view)
        new_count = sum(row[10] is None for row in rows)
        threshold = _meta(db, "next_batch", policy["batchMin"])
        due_threshold, trigger = _due_threshold(rows, threshold, policy)
        if new_count < due_threshold or len(rows) < policy["batchMin"]:
            return {"status": "waiting", "new_gold": new_count,
                    "threshold": due_threshold, "cadence_trigger": trigger}
        evidence = digest(canonical([(r[0], r[1], r[2], r[5]) for r in rows]))
        cycle_id = ("shared-" + digest(canonical([
            evidence, input_view, model_identity(policy['trainerArgv'][2])]))[:20]
            if input_view == EDIT_INPUT_VIEW else "shared-" + evidence[:20])
        old = db.execute("SELECT status,evidence FROM cycles WHERE cycle_id=?", (cycle_id,)).fetchone()
        if old and old[0] in ("complete", "retained"):
            return {"status": "already_complete" if old[0] == "complete"
                    else "already_retained", "cycle_id": cycle_id}
        # A cycle created by the original adapter has no view marker. Its
        # already-trained head must resume with the legacy 512-token view.
        # A merely started or failed cycle has no committed head, so a
        # pre-upgrade row can retry under the requested experimental view.
        if old and old[0] in ("trained", "evaluated"):
            old_evidence = json.loads(old[1])
            old_pointer = old_evidence.get("pointer") if old[0] == "evaluated" else None
            input_view = (old_pointer or old_evidence).get("model_input_view")
        elif old and old[0] in ("training", "failed", "deferred"):
            input_view = json.loads(old[1]).get("model_input_view", input_view)
        if old and old[0] == "evaluated":
            evaluated = json.loads(old[1])
            previous = evaluated["prior_pointer"]
            _, _, _, diagnostic = _cycle_holdouts(
                rows, evaluated.get("selection_fence", previous))
            if diagnostic:
                _put_meta(db, "holdout_diagnostic", diagnostic)
                return {"status": "insufficient_independent_holdout_families",
                        **diagnostic}
            if (root / "synthetic-experiment.json").exists() or _meta(db, "experiment_approved_episode", 0):
                from .synthetic_experiment import reserve_train
                try:
                    from .synthetic_experiment import project_for
                    reserve_train(db, root, project_for(root), cycle_id)
                except ValueError as exc:
                    return {"status": "experiment_paused", "reason": str(exc)[:80]}
            return _publish_evaluated(root, cycle_id, evaluated, rows)
        if current and current.get("cycle_id") == cycle_id:
            return {"status": "uncertain_training_provenance"}
        try:
            selection_fence = _selection_fence(db, current)
        except ValueError:
            return {"status": "uncertain_training_provenance",
                    "reason": "shared_retained_fence_invalid"}
        validation_rows, fresh_rows, excluded, diagnostic = _cycle_holdouts(
            rows, selection_fence)
        if diagnostic:
            _put_meta(db, "holdout_diagnostic", diagnostic)
            return {"status": "insufficient_independent_holdout_families",
                    **diagnostic}
        train = [_case(row[:6]) for row in rows if row[6] == "train"]
        holdout = [_case(row[:6]) for row in validation_rows]
        fresh_holdout = [_case(row[:6]) for row in fresh_rows]
        try:
            training_provenance = _training_provenance(
                rows, selection_fence, validation_rows=validation_rows)
            refs = ({canonical(ref): ref for ref in selection_fence["sourceCases"]}
                    if selection_fence else {})
            for row in rows:
                if row[6] != "train" and row not in validation_rows:
                    continue
                ref = _case_reference(row)
                refs[canonical(ref)] = ref
            if len(refs) > 2000:
                raise ValueError("source_provenance_budget")
            source_refs = [refs[key] for key in sorted(refs)]
        except (ValueError, KeyError, TypeError) as exc:
            return {"status": "uncertain_training_provenance", "reason": str(exc)[:80]}
        if (root / "synthetic-experiment.json").exists() or _meta(db, "experiment_approved_episode", 0):
            from .synthetic_experiment import reserve_train
            try:
                from .synthetic_experiment import project_for
                reserve_train(db, root, project_for(root), cycle_id)
            except ValueError as exc:
                return {"status": "experiment_paused", "reason": str(exc)[:80]}
        if old is None:
            db.execute("INSERT INTO cycles VALUES (?,?,?,?,NULL)",
                       (cycle_id, "training", canonical({"input_sha256": evidence,
                        "model_input_view": input_view}), time.time()))
    cycles = root / "cycles"
    cycles.mkdir(mode=0o700, exist_ok=True)
    folder = cycles / cycle_id
    folder.mkdir(mode=0o700, exist_ok=True)
    artifact = folder / "candidate.artifact"
    incumbent = Path(current["artifact_path"]) if current else Path(policy["baseArtifact"])
    if current and _hash(incumbent) != current["artifact_sha256"]:
        raise ValueError("shared_incumbent_changed")
    if trainer_factory is None:
        adapter = Path(__file__).with_name("laya_adapter.py")
        if Path(policy["trainerArgv"][1]).resolve() != adapter.resolve():
            raise ValueError("invalid_shared_trainer")
        trainer = VerifiedLayaTrainer(policy["trainerArgv"], max_seconds=policy["safetySeconds"],
                        max_rss_mib=policy["maxRssMib"],
                        pressure_probe=lambda **options: resource_report(policy, admission=False,**options), require_monitor=True)
    else:
        trainer = trainer_factory(policy)
    try:
        if old and old[0] == "trained":
            trained_evidence = json.loads(old[1])
            if (not artifact.exists() or _hash(artifact) != trained_evidence["artifact_sha256"]
                    or _hash(incumbent) != trained_evidence["incumbent_sha256"]):
                raise ValueError("shared_trained_artifact_changed")
            train_resources = {"resumed": True}
        else:
            # A killed local trainer can leave an incomplete artifact. Its
            # output is never trusted until the trained phase records its hash.
            train_meta = {"job_id": cycle_id + ":train", "parent_id": cycle_id,
                          "op": "laya_train", "runtime_kind": "local_trainer",
                          "model_version": "laya",
                          "source_version": current["cycle_id"] if current else "base",
                          "destination_version": cycle_id}
            queued_at = time.monotonic()
            job_telemetry.emit(root / "job-telemetry.jsonl", "queued", **train_meta)
            train_resources = None
            train_status = "error"
            started_at = queued_at
            try:
                artifact.unlink(missing_ok=True)
                (folder / "train.json").unlink(missing_ok=True)
                started_at = time.monotonic()
                job_telemetry.emit(root / "job-telemetry.jsonl", "started",
                                   queue_wait_ms=(started_at - queued_at) * 1000,
                                   **train_meta)
                train_request = {"schema_version": 1,
                    "train": [{k: v for k, v in case.items() if k != "project_key"} for case in train],
                    "artifact_path": str(artifact), "manifest_sha256": evidence}
                if input_view is not None:
                    train_request["model_input_view"] = input_view
                answer, train_resources = trainer.call("train", train_request,
                                                       folder / "train.json", cwd=folder)
                if answer != {"schema_version": 1, "status": "trained"}:
                    raise ValueError("invalid_shared_train_result")
                with _db(root) as db:
                    db.execute("UPDATE cycles SET status='trained',evidence=? WHERE cycle_id=? AND status IN ('training','failed','deferred')",
                               (canonical({"input_sha256": evidence, "artifact_sha256": _hash(artifact),
                                           "incumbent_sha256": _hash(incumbent),
                                           "model_input_view": input_view}), cycle_id))
                train_status = "success"
            finally:
                try:
                    after_report = resource_report(policy, detailed=True)
                except Exception:
                    after_report = {}
                job_telemetry.emit(root / "job-telemetry.jsonl", "finished",
                                   status=train_status,
                                   error_code="train_failed" if train_status == "error" else None,
                                   exit_code=getattr(trainer, "last_exit_code", None),
                                   resources={**(train_resources or getattr(
                                       trainer, "last_resources", None) or {}),
                                              "before": report, "after": after_report},
                                   run_ms=(time.monotonic() - started_at) * 1000,
                                   **train_meta)
        before, before_resources = _predict(trainer, folder, incumbent, holdout,
                                            "incumbent", input_view=input_view)
        after, after_resources = _predict(trainer, folder, artifact, holdout,
                                          "candidate", input_view=input_view)
        if len(fresh_holdout) == len(holdout):
            fresh_before, fresh_after = before, after
            fresh_resources = {"incumbent": before_resources, "candidate": after_resources}
        else:
            fresh_before, fresh_before_resources = _predict(
                trainer, folder, incumbent, fresh_holdout, "fresh-incumbent",
                input_view=input_view)
            fresh_after, fresh_after_resources = _predict(
                trainer, folder, artifact, fresh_holdout, "fresh-candidate",
                input_view=input_view)
            fresh_resources = {"incumbent": fresh_before_resources,
                               "candidate": fresh_after_resources}
        for project_key in {case["project_key"] for case in train + holdout}:
            before["projects"].setdefault(project_key, {"questions": 0, "exact": 0})
            after["projects"].setdefault(project_key, {"questions": 0, "exact": 0})
        with _db(root) as db:
            latest = _rows_for_model_view(db.execute("SELECT project_key,request_id,input_sha256,sample FROM cases WHERE status='gold' AND use_state='active' AND split!='final'").fetchall(), input_view)
            if len(latest) != len(rows):
                raise ValueError("shared_queue_changed_during_cycle")
            if pointer_path.exists() and read_private_json(pointer_path) != current:
                raise ValueError("shared_pointer_changed")
            record = {"schema_version": 1, "schema": SCHEMA,
                      "cycle_id": cycle_id, "artifact_path": str(artifact),
                      "artifact_sha256": _hash(artifact), "previous": current or
                      {"artifact_path": str(incumbent), "artifact_sha256": _hash(incumbent)},
                      "experimental": True,
                      "sourceCases": source_refs,
                      "validationCases": [_case_reference(row) for row in validation_rows],
                      "trainingProvenance": training_provenance,
                      "model_input_view": input_view}
            next_batch, cadence_reason = _cadence(
                policy, threshold, fresh_before, fresh_after)
            strict_metric = _candidate_disjoint_metric(
                folder, "fresh-incumbent" if len(fresh_holdout) != len(holdout) else "incumbent",
                "fresh-candidate" if len(fresh_holdout) != len(holdout) else "candidate",
                fresh_rows,
                training_keys=(set(selection_fence["trainingProvenance"]["familyKeys"])
                               if selection_fence else set()) | set().union(*(
                                   _candidate_exposure_keys(row) for row in rows
                                   if row[6] == "train")),
                selection_keys=(set(selection_fence["trainingProvenance"]["selectionFamilyKeys"])
                                if selection_fence else set()))
            metrics = {"incumbent": before, "candidate": after,
                       "fresh_incumbent": fresh_before, "fresh_candidate": fresh_after,
                       "train_questions": len(train), "holdout_questions": len(holdout),
                       "fixed_validation_questions": len(holdout) - len(fresh_holdout),
                       "fresh_holdout_questions": len(fresh_holdout),
                       "excluded_holdout": excluded,
                       "evaluation_scope": "unseen_query_family_seen_corpus",
                       "model_input_view": input_view,
                       "candidate_document_disjoint": strict_metric,
                       "resources": {"train": train_resources, "incumbent": before_resources,
                                     "candidate": after_resources, "fresh": fresh_resources,
                                     "preflight": report,
                                     "postflight": resource_report(policy, detailed=True)},
                       "trial": True, "next_batch": next_batch,
                       "cadence_reason": cadence_reason,
                       "cadence_trigger": trigger, "due_threshold": due_threshold}
            evaluated = {"input_sha256": evidence, "prior_pointer": current,
                         "pointer": record, "metrics": metrics,
                         "model_input_view": input_view,
                         "selection_fence": selection_fence}
            db.execute("UPDATE cycles SET status='evaluated',evidence=? WHERE cycle_id=? AND status='trained'",
                       (canonical(evaluated), cycle_id))
        return _publish_evaluated(root, cycle_id, evaluated, rows)
    except Exception as exc:
        from .local_cycle import record_resource_defer
        deferred = isinstance(exc, ResourceDeferred)
        if deferred:
            record_resource_defer(root, exc.report, reason=str(exc))
        with _db(root) as db:
            # A trained artifact stays pinned across downstream failures.
            # Evaluated publication also resumes without a second training.
            db.execute("UPDATE cycles SET status=?,evidence=?,finished_at=? WHERE cycle_id=? AND status IN ('training','failed','deferred')",
                       ("deferred" if deferred else "failed", canonical({"reason": str(exc)[:80],
                                   "model_input_view": input_view}), time.time(), cycle_id))
        return {"status": "deferred_resource" if deferred else "failed", "reason": str(exc)[:80], "cycle_id": cycle_id}


def rollback(root):
    path = root / "experimental-checkpoint.json"
    if not path.exists():
        return {"status": "no_experiment"}
    current = read_private_json(path)
    previous = current.get("previous")
    if not isinstance(previous, dict) or _hash(previous["artifact_path"]) != previous["artifact_sha256"]:
        raise ValueError("invalid_rollback_target")
    if previous.get("experimental"):
        _atomic_json(path, previous)
    else:
        path.unlink()
    _atomic_json(root / "last-rollback.json",
                 {"schema": SCHEMA, "cycle_id": current["cycle_id"],
                  "previous_sha256": previous["artifact_sha256"], "at": time.time()})
    return {"status": "rolled_back", "cycle_id": current["cycle_id"]}


def prepare_multilingual_trial(root, *, expected_old_pointer_sha256):
    """Archive the English trial exactly before the first edit-model cycle.

    This explicit maintenance operation never changes the global active model.
    New training remains gated on reviewed schema-v4 edit gold and pressure.
    """
    import fcntl
    from .laya_adapter import EDIT_INPUT_VIEW, _head_path, model_identity
    root = _validated_root(root)
    policy = _policy(root)
    if policy.get('modelInputView') != EDIT_INPUT_VIEW:
        raise ValueError('multilingual_policy_required')
    base_sha = model_identity(policy['trainerArgv'][2])
    _head_path(_safe_file(policy['baseArtifact']), base_sha, EDIT_INPUT_VIEW)
    pointer = root / 'experimental-checkpoint.json'
    archive = root / 'english-rollback-checkpoint.json'
    marker = root / 'multilingual-migration.json'
    # The learning worker holds this same lease across teacher, training and
    # publication. Migrating its input view while that worker is live could
    # otherwise detach the checkpoint it is about to publish.
    lock = os.open(root / 'shared-worker.lock',
                   os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(lock)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError('unsafe_shared_lock')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError('multilingual_worker_busy') from exc
        if marker.exists():
            value = read_private_json(marker)
            if value.get('newModelSha256') != base_sha:
                raise ValueError('multilingual_migration_changed')
            if pointer.exists() and read_private_json(pointer).get('model_input_view') != EDIT_INPUT_VIEW:
                raise ValueError('multilingual_migration_pointer_reappeared')
            return {'status': 'already_prepared', 'old_pointer_sha256':
                    value.get('oldPointerSha256')}
        if pointer.is_symlink() or archive.is_symlink():
            raise ValueError('multilingual_migration_unsafe_pointer')
        old_bytes = (archive.read_bytes() if archive.exists() else
                     pointer.read_bytes() if pointer.exists() else None)
        old_hash = hashlib.sha256(old_bytes).hexdigest() if old_bytes is not None else None
        if old_hash != expected_old_pointer_sha256:
            raise ValueError('multilingual_migration_pointer_changed')
        if old_bytes is not None:
            old = read_private_json(archive if archive.exists() else pointer)
            if old.get('model_input_view') == EDIT_INPUT_VIEW:
                raise ValueError('multilingual_already_active')
            if not archive.exists():
                fd = os.open(archive, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                             0o600)
                with os.fdopen(fd, 'wb') as output:
                    output.write(old_bytes)
                    output.flush()
                    os.fsync(output.fileno())
            if pointer.exists():
                if pointer.read_bytes() != old_bytes:
                    raise ValueError('multilingual_migration_pointer_changed')
                pointer.unlink()
        _atomic_json(marker, {'schema': 'qmd-multilingual-trial-migration-v1',
                              'oldPointerSha256': old_hash,
                              'newModelSha256': base_sha, 'preparedAt': time.time()})
        return {'status': 'prepared', 'old_pointer_sha256': old_hash,
                'new_model_sha256': base_sha}
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        os.close(lock)


def observe_trial(root, policy, *, trainer_factory=None):
    """Compare the trial on later held-out captures, then undo clear regression."""
    if _historical_trainer_policy(policy):
        return {"status": "runtime_approval_required"}
    if policy.get('_runtime_authorization_v2') is True:
        # A v2 stage-only grant does not permit observation or rollback of a
        # previously published trial through this direct legacy entrypoint.
        return {"status": "runtime_approval_required"}
    path = root / "experimental-checkpoint.json"
    if not path.exists():
        return {"status": "no_experiment"}
    _reconcile_cases(root)
    preflight = resource_report(policy, detailed=True)
    if not preflight["ready"]:
        return {"status": "deferred_resource"}
    current = read_private_json(path)
    if not current.get("experimental") or _hash(current["artifact_path"]) != current["artifact_sha256"]:
        raise ValueError("invalid_experimental_pointer")
    if not _valid_training_provenance(current):
        return {"status": "uncertain_training_provenance"}
    if not trial_eligible(root, max_seconds=5):
        return {"status": "source_revoked_before_observation"}
    previous = current["previous"]
    if _hash(previous["artifact_path"]) != previous["artifact_sha256"]:
        raise ValueError("invalid_previous_pointer")
    provenance_sha = digest(canonical(current["trainingProvenance"]))
    with _db(root) as db:
        rows = _rows_for_model_view(db.execute("SELECT project_key,request_id,input_sha256,sample,bodies,gold,split,project_root,corpus_fingerprint,family_keys,trained_cycle FROM cases WHERE status='gold' AND use_state='active' AND trained_cycle IS NULL AND split='holdout' ORDER BY captured_at,request_id").fetchall(), current.get('model_input_view'))
        if not rows:
            return {"status": "waiting_new_holdout"}
        rows_sha = digest(canonical([(row[0], row[1], row[2], row[5]) for row in rows]))
        fingerprint = digest(canonical([provenance_sha, rows_sha]))
        provenance = current["trainingProvenance"]
        clean, excluded = _screen_observation(rows,
            provenance.get("queryFamilyKeys", provenance["familyKeys"]),
            selection_keys=provenance.get(
                "selectionQueryFamilyKeys", provenance["selectionFamilyKeys"]))
        old = db.execute("SELECT body FROM observations WHERE cycle_id=? AND evidence_sha256=?",
                         (current["cycle_id"], fingerprint)).fetchone()
        if old:
            prior = json.loads(old[0])
            if (prior.get("training_provenance_sha256") != provenance_sha
                    or prior.get("holdout_sha256") != rows_sha):
                return {"status": "unverified_previous_observation"}
            if prior["status"] == "regressed":
                rollback(root)
            return prior
        if len(clean) < 2:
            report = {"status": "insufficient_independent_holdout_families",
                      "cycle_id": current["cycle_id"], "independent_holdout": len(clean),
                      "required_holdout": 2, "excluded": excluded,
                      "training_provenance_sha256": provenance_sha,
                      "holdout_sha256": rows_sha}
            db.execute("INSERT OR IGNORE INTO observations VALUES (?,?,?)",
                       (current["cycle_id"], fingerprint, canonical(report)))
            return report
    cases = [_case(row[:6]) for row in clean]
    if trainer_factory is None:
        adapter = Path(__file__).with_name("laya_adapter.py")
        klass = VerifiedLayaTrainer if Path(policy["trainerArgv"][1]).resolve() == adapter.resolve() else LocalTrainer
        trainer = klass(policy["trainerArgv"], max_seconds=policy["safetySeconds"],
                        max_rss_mib=policy["maxRssMib"],
                        pressure_probe=lambda **options: resource_report(policy, admission=False,**options), require_monitor=True)
    else:
        trainer = trainer_factory(policy)
    folder = root / "observations" / (current["cycle_id"] + "-" + fingerprint[:16])
    folder.mkdir(parents=True, mode=0o700, exist_ok=True)
    input_view = current.get("model_input_view")
    before, before_resources = _predict(trainer, folder, Path(previous["artifact_path"]),
                                        cases, "previous", input_view=input_view)
    after, after_resources = _predict(trainer, folder, Path(current["artifact_path"]),
                                      cases, "trial", input_view=input_view)
    if (not trial_eligible(root, max_seconds=5)
            or any(not _case_active(root, (row[7], row[1], row[3])) for row in clean)):
        return {"status": "source_revoked_during_observation"}
    regressed = after["exact_rate"] < before["exact_rate"]
    strict_metric = _candidate_disjoint_metric(
        folder, "previous", "trial", clean,
        training_keys=provenance["familyKeys"],
        selection_keys=provenance["selectionFamilyKeys"])
    report = {"status": "regressed" if regressed else "observed", "cycle_id": current["cycle_id"],
              "previous": before, "trial": after,
              "evaluation_scope": "unseen_query_family_seen_corpus",
              "candidate_document_disjoint": strict_metric,
              "independent_holdout": len(clean), "excluded": excluded,
              "training_provenance_sha256": provenance_sha,
              "holdout_sha256": rows_sha,
              "resources": {"previous": before_resources, "trial": after_resources,
                            "preflight": preflight,
                            "postflight": resource_report(policy, detailed=True)}}
    with _db(root) as db:
        if read_private_json(path) != current:
            raise ValueError("shared_pointer_changed")
        latest = _rows_for_model_view(db.execute("SELECT project_key,request_id,input_sha256,sample,bodies,gold,split,project_root FROM cases WHERE status='gold' AND use_state='active' AND trained_cycle IS NULL AND split='holdout' ORDER BY captured_at,request_id").fetchall(), input_view)
        if digest(canonical([(row[0], row[1], row[2], row[5]) for row in latest])) != rows_sha:
            return {"status": "holdout_changed_during_observation"}
        db.execute("INSERT OR IGNORE INTO observations VALUES (?,?,?)",
                   (current["cycle_id"], fingerprint, canonical(report)))
    if regressed:
        if not trial_eligible(root, max_seconds=5):
            uncertain = {"status": "source_revoked_during_observation",
                         "cycle_id": current["cycle_id"],
                         "training_provenance_sha256": provenance_sha,
                         "holdout_sha256": rows_sha}
            with _db(root) as db:
                db.execute("UPDATE observations SET body=? WHERE cycle_id=? AND evidence_sha256=?",
                           (canonical(uncertain), current["cycle_id"], fingerprint))
            return uncertain
        rollback(root)
    return report


class _SelfMeter:
    """Sample only this detached worker; CLI and Laya children have own meters."""

    def __init__(self):
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.cpu_total = self.rss_total = self.peak_cpu = self.peak_rss = 0.0
        self.samples = 0

    def _sample(self):
        try:
            result = subprocess.run(
                ["/bin/ps", "-p", str(os.getpid()), "-o", "%cpu=,rss="],
                capture_output=True, text=True, timeout=1, check=False)
            if result.returncode != 0:
                return
            cpu_raw, rss_raw = result.stdout.strip().split()
            cpu, rss = float(cpu_raw), int(rss_raw) / 1024
            self.cpu_total += cpu; self.rss_total += rss
            self.peak_cpu = max(self.peak_cpu, cpu)
            self.peak_rss = max(self.peak_rss, rss)
            self.samples += 1
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return

    def _loop(self):
        while not self.stop_event.is_set():
            self._sample()
            self.stop_event.wait(0.5)

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.thread.join(timeout=2)
        return {"avg_cpu_percent": round(self.cpu_total / self.samples, 2)
                if self.samples else None,
                "peak_cpu_percent": round(self.peak_cpu, 2) if self.samples else None,
                "avg_rss_mib": round(self.rss_total / self.samples, 2)
                if self.samples else None,
                "peak_rss_mib": round(self.peak_rss, 2) if self.samples else None,
                "resource_samples": self.samples}


def run(root, *, project=None, sync_config=None, wait_for_lock=False):
    """Meter one detached worker invocation, including any pre-lock work."""
    from . import project_scope
    if project_scope.present(root):
        if os.environ.get('QMD_SHARED_GOLD_ONLY') == '1':
            return {'status':'project_requires_normal_worker'}
        from .project_worker import run as run_project_worker
        return run_project_worker(root,project or project_scope.owner(root),sync_config)
    if os.environ.get('QMD_SHARED_GOLD_ONLY') == '1':
        # The generic sync can reconcile unrelated cases before the worker
        # applies its exact request-ID filter. Gold collection never runs it.
        if project is None or _gold_only_request_ids() is None:
            return {'status': 'gold_allowlist_required'}
        sync_config = None
    root = Path(root)
    from .learning_profile import present,SCHEMA
    marker=(sync_config or {}).get('sharedLearning',{}).get('profile')==SCHEMA
    if present(root) or marker:
        if os.environ.get('QMD_SHARED_GOLD_ONLY')=='1':return {'status':'profile_forbids_teacher_worker'}
        if project is None:
            from .learning_profile import NAME
            project=Path(read_private_json(root/NAME)['proposal']['projectRoot'])
        return run_training_pending(root,project)
    if _policy(root).get('_runtime_authorization_v2') is True:
        # The generic local-review import scans every historical queued row.
        # A v2 normal worker admits fresh ticket cases inside _run_impl only.
        sync_config = None
    telemetry_job = os.environ.get("QMD_JOB_ID")
    telemetry_path = root / "job-telemetry.jsonl"
    telemetry_meta = {"job_id": telemetry_job,
                      "parent_id": os.environ.get("QMD_JOB_PARENT_ID") or None,
                      "episode": int(os.environ["QMD_JOB_EPISODE"])
                      if os.environ.get("QMD_JOB_EPISODE", "").isdigit() else None,
                      "op": "shared_worker", "runtime_kind": "local_worker",
                      "model_version": os.environ.get("QMD_JOB_MODEL_VERSION") or "base"}
    worker_started = time.monotonic()
    meter = _SelfMeter() if telemetry_job else None
    worker_status = "error"
    before_report = {}
    if telemetry_job:
        try:
            queued_at = float(os.environ.get("QMD_JOB_QUEUED_AT", ""))
            queue_wait = max(0.0, (time.time() - queued_at) * 1000)
        except ValueError:
            queue_wait = None
        job_telemetry.emit(telemetry_path, "started", queue_wait_ms=queue_wait,
                           **telemetry_meta)
        meter.start()
    try:
        if telemetry_job:
            try:
                before_report = resource_report(_policy(root), detailed=True)
            except Exception:
                before_report = {}
        # A historical v45 trainer path is a readable policy reference, not
        # executable approval. Check it before generic sync can reconcile
        # queued rows, while retaining the normal worker telemetry envelope.
        if sync_config is not None and _historical_trainer_policy(_policy(root)):
            result = {'status': 'runtime_approval_required'}
        else:
            synced = sync_reviewed(sync_config, project) if sync_config is not None else None
            result = _run_impl(root, project=project, wait_for_lock=wait_for_lock)
            if synced is not None:
                result = {**result, "synced": synced}
        worker_status = ("error" if result["status"] == "failed" else
                         "skipped" if result["status"] == "busy" else
                         "deferred" if result["status"] in (
                             "experiment_paused", "deferred_resource",
                             "runtime_approval_required", "gold_allowlist_required",
                             "insufficient_independent_holdout_families",
                             "uncertain_training_provenance") else "success")
        return result
    finally:
        if telemetry_job:
            resources = meter.stop()
            try:
                after_report = resource_report(_policy(root), detailed=True)
            except Exception:
                after_report = {}
            job_telemetry.emit(telemetry_path, "finished", status=worker_status,
                               error_code="worker_failed" if worker_status == "error" else None,
                               resources={**resources, "before": before_report,
                                          "after": after_report},
                               run_ms=(time.monotonic() - worker_started) * 1000,
                               **telemetry_meta)


def _run_impl(root, *, project=None, wait_for_lock=False):
    root = Path(root)
    policy = _policy(root)
    from .synthetic_experiment import SHARED_NAME, project_for
    gold_only = os.environ.get('QMD_SHARED_GOLD_ONLY') == '1'
    if _historical_trainer_policy(policy) and not gold_only:
        return {'status': 'runtime_approval_required'}
    automatic_grant = None
    if not gold_only and policy.get('_runtime_authorization_v2') is True:
        from .runtime_authorization import require_operation
        try:
            automatic_grant = require_operation(root, policy, 'gold')
        except ValueError:
            return {'status': 'runtime_approval_required'}
    gold_request_ids = _gold_only_request_ids() if gold_only else None
    if gold_only:
        if project is None or gold_request_ids is None:
            return {'status': 'gold_allowlist_required'}
    if (root / "synthetic-experiment.json").exists() or root.name == SHARED_NAME:
        from .synthetic_experiment import load as experiment_control, check_disk
        try:
            control, _ = experiment_control(root, project_for(root))
            check_disk(root, control)
        except ValueError as exc:
            return {"status": "experiment_paused", "reason": str(exc)[:80]}
    lock = root / "shared-worker.lock"
    waiter_fd = None
    if wait_for_lock:
        # Only one detached post-index process may wait behind a long trainer.
        # Once it owns the worker lock, release this gate so a later ACK can
        # enqueue the next wake without accumulating blocked processes.
        waiter_fd = os.open(root / "post-index-waiter.lock",
                            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        waiter_info = os.fstat(waiter_fd)
        if (not stat.S_ISREG(waiter_info.st_mode)
                or waiter_info.st_uid != os.getuid()
                or waiter_info.st_mode & 0o077):
            os.close(waiter_fd)
            raise ValueError("unsafe_post_index_waiter_lock")
        try:
            fcntl.flock(waiter_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(waiter_fd)
            return {"status": "busy"}
    fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("unsafe_shared_lock")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX |
                        (0 if wait_for_lock else fcntl.LOCK_NB))
        except BlockingIOError:
            return {"status": "busy"}
        if waiter_fd is not None:
            os.close(waiter_fd)
            waiter_fd = None
        teacher = {"status": "disabled"}
        if project is not None:
            try:
                from .synthetic_scope import policy_for, _synthetic_bundle
                _synthetic_bundle(root, project)
                if not gold_only:
                    if automatic_grant is None:
                        recover_awaiting_edits(root, project)
                    else:
                        from .automatic_cohort import admitted
                        with _db(root) as db:
                            ids = [row[1] for _, row in admitted(db, project,
                                automatic_grant, statuses=('awaiting_local',))]
                        recover_awaiting_edits(root, project, request_ids=ids)
                policy_for(root, project)
                if not gold_only:
                    if automatic_grant is None:
                        recover_scope_deferred_edits(root, project)
                    else:
                        from .automatic_cohort import admitted
                        with _db(root) as db:
                            ids = [row[1] for _, row in admitted(db, project,
                                automatic_grant, statuses=('pending_review',))]
                        recover_scope_deferred_edits(root, project,
                                                     request_ids=ids)
            except ValueError as exc:
                teacher = {"status": "denied", "reason": str(exc)[:80]}
            else:
                from .synthetic_teacher import process_pending
                if gold_only:
                    teacher = process_pending(root, project, policy,
                                              request_ids=gold_request_ids)
                else:
                    teacher = process_pending(root, project, policy,
                                              automatic_grant=automatic_grant)
        # An operator-scoped collection session may finish authenticated gold
        # without crossing the automatic checkpoint/observation boundary.
        # The scope checks and one-use gold receipts above remain mandatory.
        if gold_only:
            return {'status': 'gold_only', 'teacher': teacher,
                    'observation': {'status': 'skipped_gold_only'},
                    'cycle': {'status': 'skipped_gold_only'}}
        if automatic_grant is None:
            seen = set()
            while (root / "experimental-checkpoint.json").exists() and not trial_eligible(root, max_seconds=5):
                pointer = read_private_json(root / "experimental-checkpoint.json")
                if pointer.get("cycle_id") in seen:
                    raise ValueError("shared_rollback_cycle")
                seen.add(pointer.get("cycle_id"))
                rollback(root)
        observation = ({'status': 'skipped_staged_authorization'}
                       if automatic_grant is not None else
                       observe_trial(root, policy))
        cycle = train_due(root, policy)
        return {"status": cycle["status"], "observation": observation,
                "teacher": teacher, "cycle": cycle}
    finally:
        if waiter_fd is not None:
            os.close(waiter_fd)
        os.close(fd)


def _reconcile_source_cards(root, project):
    """Best-effort admission; failures never silently authorize a candidate."""
    try:
        from .source_card_provenance import reconcile_pending
        result = reconcile_pending(root, project)
        if not result['eligible'] and not result.get('skipped'):
            return result
        status = ('success' if result['registered'] and not result['deferred']
                  and not result.get('skipped') else 'deferred')
        error = None if status == 'success' else 'source_card_pending'
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error) as exc:
        result = {'registered': [], 'deferred': [], 'error': str(exc)[:80]}
        status, error = 'error', 'source_card_reconcile_failed'
    job_telemetry.emit(Path(root) / 'job-telemetry.jsonl', 'finished',
        job_id='source-card:' + secrets.token_hex(12),
        op='source_card_registration', runtime_kind='local_worker',
        status=status, error_code=error,
        registered_count=len(result['registered']),
        deferred_count=len(result['deferred']),
        skipped_count=result.get('skipped', 0))
    return result


def launch(config, project, *, parent_id=None, episode=None,
           after_index=False):
    root = _safe_root(config, project)
    if root is None:
        return {"status": "disabled"}
    from . import project_scope
    general_project=project_scope.present(root)
    if general_project:
        from .project_worker import setup_ready
        if not setup_ready(root):return {'status':'setup_in_progress'}
        flags=config.get('contextLearning',{})
        if flags.get('capture') is not True or flags.get('autoCycle') is not True:
            return {'status':'disabled'}
        project_scope.require(root,_policy(root),'local_train')
        import project_learning_setup
        if project_learning_setup.synthetic_local_only(root,project,_policy(root)):
            # Editing/capture must not start the expressly manual QA job.
            return {'status':'manual_synthetic_training_required'}
    # A source/card edit has its own one-use hook tickets. SessionStart also
    # retries admission if the prior index worker died after its QMD ACK.
    from .learning_profile import present,SCHEMA
    selected_profile=present(root) or config.get('sharedLearning',{}).get('profile')==SCHEMA
    if not selected_profile and not general_project:_reconcile_source_cards(root, project)
    policy = _policy(root)
    if _historical_trainer_policy(policy) and os.environ.get("QMD_SHARED_GOLD_ONLY") != "1":
        return {"status": "runtime_approval_required"}
    # A bounded gold collection session verifies its exact ticket allowlist
    # after the durable index ACK, then calls the gold-only worker explicitly.
    # Neither the immediate edit path nor the post-index wake may launch a
    # background worker that could consume another pending case or train.
    if os.environ.get("QMD_SHARED_GOLD_ONLY") == "1":
        return {"status": "deferred_gold_only_manual_review"}
    from .synthetic_experiment import SHARED_NAME
    if (root / "synthetic-experiment.json").exists() or root.name == SHARED_NAME:
        from .synthetic_experiment import load as experiment_control, check_disk
        try:
            control, _ = experiment_control(root, project)
            check_disk(root, control)
        except ValueError as exc:
            return {"status": "experiment_paused", "reason": str(exc)[:80]}
    # A selected profile's normal worker is training/evaluation only. Let the
    # durable wake own its start, admission and retries instead of spawning a
    # competing detached trainer directly from the hook. General project and
    # legacy source workers still need their capture/teacher phase below.
    if selected_profile and not general_project:
        import runtime_work
        receipt=runtime_work.request('learning',str(Path(project).resolve()))
        return receipt or {'status':'deferred_resource'}
    # General project and legacy source workers also run the capture/teacher
    # phase. The hook only persists one coalescing full-worker request plus
    # its durable wake; the runtime dispatcher starts the owned child through
    # learning_worker_step, so a crash or reboot cannot drop the teacher phase.
    job_id = "worker:" + secrets.token_hex(12)
    queued_at = time.time()
    pointer_path = root / "experimental-checkpoint.json"
    version = (read_private_json(pointer_path).get("cycle_id", "base")
               if pointer_path.exists() else "base")
    metadata = {"job_id": job_id, "parent_id": parent_id,
                "episode": episode, "op": "shared_worker",
                "runtime_kind": "local_worker", "model_version": version}
    job_telemetry.emit(root / "job-telemetry.jsonl", "queued",
                       ts=queued_at, **metadata)
    _atomic_json(root / FULL_WORKER_REQUEST, {
        "schema": FULL_WORKER_SCHEMA, "nonce": secrets.token_hex(16),
        "afterIndex": bool(after_index),
        "job": {"QMD_JOB_ID": job_id, "QMD_JOB_PARENT_ID": parent_id or "",
                "QMD_JOB_EPISODE": str(episode) if episode is not None else "",
                "QMD_JOB_QUEUED_AT": str(queued_at),
                "QMD_JOB_MODEL_VERSION": str(version)}})
    import runtime_work
    receipt = runtime_work.request('learning', str(Path(project).resolve()))
    return receipt or {'status': 'deferred_resource'}


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
    path = root / FULL_WORKER_REQUEST
    if not path.exists() and not path.is_symlink():
        return None
    request = read_private_json(path)
    job = request.get("job") if isinstance(request, dict) else None
    if (not isinstance(job, dict) or request.get("schema") != FULL_WORKER_SCHEMA
            or not re.fullmatch(r"[0-9a-f]{32}", str(request.get("nonce")))
            or not isinstance(request.get("afterIndex"), bool)
            or set(job) != set(_FULL_WORKER_JOB_KEYS)
            or any(not isinstance(v, str) or len(v) > 256 or "\x00" in v
                   for v in job.values())):
        raise ValueError("invalid_full_worker_request")
    return request


def _valid_worker_state(state, *, training=False):
    keys = {"nonce", "starts", "outcome", "acked"} | ({"schema", "revision"} if training else set())
    return (isinstance(state, dict) and set(state) == keys
            and isinstance(state["nonce"], str) and re.fullmatch(r"[0-9a-f]{32}", state["nonce"]) is not None
            and type(state["starts"]) is int and 0 <= state["starts"] <= MAX_FULL_WORKER_STARTS
            and (state["outcome"] is None or (isinstance(state["outcome"], str)
                                              and 0 < len(state["outcome"]) <= 80))
            and type(state["acked"]) is bool
            and (not training or (state["schema"] == TRAINING_WORKER_SCHEMA
                                  and type(state["revision"]) is int and state["revision"] >= 0)))


def _full_worker_state(root, nonce):
    path = root / FULL_WORKER_STATE
    if path.exists() or path.is_symlink():
        state = read_private_json(path)
        if not _valid_worker_state(state):
            raise ValueError("invalid_full_worker_state")
        if state["nonce"] == nonce:
            return state
    return {"nonce": nonce, "starts": 0, "outcome": None, "acked": False}


def _training_worker_state(root):
    path = root / TRAINING_WORKER_STATE
    if not path.exists() and not path.is_symlink():
        return None
    state = read_private_json(path)
    if not _valid_worker_state(state, training=True):
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
        env = {k: os.environ[k] for k in ("PATH", "LANG", "TMPDIR", "HOME", "USER") if k in os.environ}
        env.update(PYTHONDONTWRITEBYTECODE="1", HF_HUB_OFFLINE="1",
                   TRANSFORMERS_OFFLINE="1", HF_DATASETS_OFFLINE="1", **job)
        entry = Path(__file__).resolve().parents[1] / "context_learning_shared.py"
        try:
            spawn([sys.executable, "-B", str(entry), command, str(project), str(lease_fd)],
                  stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=env,
                  start_new_session=True, close_fds=True, pass_fds=(lease_fd,))
        except Exception:
            job_telemetry.emit(root / "job-telemetry.jsonl", "finished",
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
        request = _full_worker_request(root)
    except (OSError, ValueError):
        _quarantine(root, FULL_WORKER_REQUEST)
        return {"status": "invalid_request"}
    if request is None:
        return {"status": "none"}
    try:
        state = _full_worker_state(root, request["nonce"])
    except (OSError, ValueError):
        # Unknown start count: never restart a teacher phase blindly. Queued
        # cases stay in the ledger; the next hook launch writes a new nonce.
        _quarantine(root, FULL_WORKER_STATE)
        _atomic_json(root / FULL_WORKER_STATE, {"nonce": request["nonce"], "starts": 0,
                                                "outcome": "abandoned_invalid_state", "acked": True})
        return {"status": "invalid_state"}
    if state["outcome"] is not None:
        if state["acked"]:
            return {"status": "none"}
        _atomic_json(root / FULL_WORKER_STATE, {**state, "acked": True})
        return {"status": "settled", "outcome": state["outcome"]}
    if state["starts"] >= MAX_FULL_WORKER_STARTS:
        # Queued cases stay in the ledger; the next hook launch retries.
        _atomic_json(root / FULL_WORKER_STATE, {**state, "outcome": "abandoned_after_restarts",
                                                "acked": True})
        return {"status": "abandoned_after_restarts"}
    # Counted before spawning: a crash between here and exit still counts.
    state = {**state, "starts": state["starts"] + 1}
    _atomic_json(root / FULL_WORKER_STATE, state)
    _spawn_owned(root, project, "run-owned-worker", fd, spawn,
                 job=request["job"], job_id=request["job"]["QMD_JOB_ID"])
    return {"status": "started", "mode": "full", "starts": state["starts"]}


def _training_step(root, project, revision, fd, spawn, *, handoff=False):
    """Advance the training-only retry for one wake revision.

    Runtime wake revisions are unique per queue, so a settled state answers
    exactly the revision that last started it. Any other revision (a wake
    re-put during or after that run) coalesces into one new request.
    """
    path = root / TRAINING_WORKER_STATE
    try:
        state = _training_worker_state(root)
    except (OSError, ValueError):
        _quarantine(root, TRAINING_WORKER_STATE)
        return {"status": "invalid_state", "mode": "training"}
    if state is None or state["outcome"] is not None:
        if state is not None and state["revision"] == revision and not handoff:
            if state["acked"]:
                return {"status": "none"}
            _atomic_json(path, {**state, "acked": True})
            return {"status": "settled", "mode": "training", "outcome": state["outcome"]}
        # A replaced settled record needs no separate ACK: this request covers it.
        state = {"schema": TRAINING_WORKER_SCHEMA, "nonce": secrets.token_hex(16),
                 "revision": revision, "starts": 0, "outcome": None, "acked": False}
    elif state["starts"] >= MAX_FULL_WORKER_STARTS:
        _atomic_json(path, {**state, "outcome": "abandoned_after_restarts", "acked": True})
        return {"status": "abandoned_after_restarts", "mode": "training"}
    state = {**state, "starts": state["starts"] + 1, "revision": revision}
    _atomic_json(path, state)
    _spawn_owned(root, project, "run-owned-training", fd, spawn,
                 job={}, job_id="training-worker:" + state["nonce"])
    return {"status": "started", "mode": "training", "starts": state["starts"]}


def learning_worker_step(root, project, revision, *, spawn=None):
    """Dispatcher step for one learning wake; never runs teacher or trainer.

    One lease covers both modes, so at most one owned learning child exists
    per shared root. A full (teacher+trainer) request goes first; otherwise
    the wake is a training-only retry. ``running``/``started`` keep the wake.
    """
    from .resource_coordination import _lock
    root, project = _validated_root(root), Path(project).resolve(strict=True)
    if type(revision) is not int or revision < 0:
        raise ValueError("invalid_runtime_revision")
    fd = _lock(root / FULL_WORKER_LOCK)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": "running"}
        spawn = spawn or subprocess.Popen
        full = _full_step(root, project, fd, spawn)
        handoff = full.get("outcome") == TRAINING_HANDOFF
        if full["status"] != "none" and not handoff:
            return full
        return _training_step(root, project, revision, fd, spawn, handoff=handoff)
    finally:
        os.close(fd)


def _owned_root(project):
    import config
    project = Path(project).resolve(strict=True)
    cfg = config.find_project_config(str(project))["config"]
    return project, cfg, _safe_root(cfg, project)


def _check_owned_lease(root, fd):
    info, expected = os.fstat(fd), (root / FULL_WORKER_LOCK).stat()
    if (info.st_dev, info.st_ino) != (expected.st_dev, expected.st_ino):
        raise ValueError("full_worker_lease_changed")


def _settle_owned(root, name, state, result, project, *, handoff=False):
    """Record one child outcome under the lease; retries keep a durable wake."""
    import runtime_wake_policy
    status = result.get("status") if isinstance(result, dict) else None
    status = status[:80] if isinstance(status, str) and status else "failed"
    retry = status in runtime_wake_policy.RETRY
    # Only a retryable result hands off; a finished cycle settles normally.
    handoff = handoff and retry
    if retry and not handoff:
        # Completed without a crash: the restart budget starts over, and
        # the request stays pending for the next admitted dispatch.
        _atomic_json(root / name, {**state, "starts": 0, "outcome": None, "acked": False})
    else:
        _atomic_json(root / name, {**state, "outcome": TRAINING_HANDOFF if handoff else status,
                                   "acked": False})
    if not retry:
        return result
    import runtime_work
    receipt = runtime_work.request('learning', str(project))
    lost = isinstance(receipt, dict) and receipt.get("status") == "deferred_queue_capacity"
    return {**result, "learning_retry": {"status": "wake_dropped_until_next_hook" if lost else
                                         "training_only_handoff" if handoff else
                                         "durable_wake_pending"}}


def _run_owned_worker(project, lease_fd):
    """Child body: full worker under the inherited lease, then settle once."""
    project, cfg, root = _owned_root(project)
    if root is None:
        return {"status": "disabled"}
    fd = int(lease_fd)
    _check_owned_lease(root, fd)
    try:
        # Same open file description as the spawner's lock: succeeds only for
        # the inherited owner, never for a stray process with a fresh open.
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        request = _full_worker_request(root)
        if request is None:
            return {"status": "idle"}
        try:
            result = run(root, project=project, sync_config=cfg,
                         wait_for_lock=request["afterIndex"])
        except ResourceDeferred as exc:
            result = {"status": "deferred_resource", "reason": str(exc)[:80]}
        except Exception as exc:
            result = {"status": "failed", "reason": str(exc)[:80]}
        state = _full_worker_state(root, request["nonce"])
        # A deferral after the teacher phase finished (a cycle result exists)
        # retries training only, so the teacher phase is never entered again.
        return _settle_owned(root, FULL_WORKER_STATE, state, result, project,
                             handoff=isinstance(result, dict) and "cycle" in result)
    finally:
        os.close(fd)


def _run_owned_training(project, lease_fd):
    """Child body: local training/evaluation retry only, then settle once.

    Never calls ``run``: no teacher, observation, capture or enrollment.
    Pressure yields inside the trainer at its committed group boundary and
    returns ``deferred_resource``; the durable wake then waits for runtime
    admission before a fresh child resumes from the committed cursor.
    """
    import setup_guard
    project, _, root = _owned_root(project)
    if root is None:
        return {"status": "disabled"}
    fd = int(lease_fd)
    _check_owned_lease(root, fd)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state = _training_worker_state(root)
        if state is None or state["outcome"] is not None:
            return {"status": "idle"}
        if setup_guard.status(project)["status"] != "ready":
            result = {"status": "setup_in_progress"}
        else:
            try:
                result = run_training_pending(root, project)
            except ResourceDeferred as exc:
                result = {"status": "deferred_resource", "reason": str(exc)[:80]}
            except Exception as exc:
                result = {"status": "failed", "reason": str(exc)[:80]}
        return _settle_owned(root, TRAINING_WORKER_STATE, state, result, project)
    finally:
        os.close(fd)


def wake_after_index(project):
    """Wake one exact synthetic owner after its QMD claim is ACKed.

    The detached worker waits on the shared OS lock, so an earlier hook
    worker cannot consume the wake before the index catches up.
    """
    from .synthetic_scope import NOVEL_ROOT, _synthetic_bundle, policy_for
    from .synthetic_experiment import shared_for
    import config
    requested = Path(project)
    if not requested.is_absolute() or requested.is_symlink():
        return {"status": "ignored"}
    try:
        owner = requested.resolve(strict=True)
        cfg=config.find_project_config(str(owner))['config']
        enrolled=_safe_root(cfg,owner)
        from . import project_scope
        if enrolled is not None and project_scope.present(enrolled):
            project_scope.require(enrolled,_policy(enrolled),'local_train')
            return launch(cfg,owner,after_index=True)
        if owner != NOVEL_ROOT.resolve(strict=True):
            return {"status": "ignored"}
        root = shared_for(owner)
        _synthetic_bundle(root, owner)
        cfg = config.find_project_config(str(owner))["config"]
        if _safe_root(cfg, owner) != root:
            return {"status": "ignored"}
        _reconcile_source_cards(root, owner)
        worker_policy = _policy(root)
        if os.environ.get('QMD_SHARED_GOLD_ONLY') == '1':
            return {"status": "deferred_gold_only_manual_review"}
        if _historical_trainer_policy(worker_policy):
            return {"status": "runtime_approval_required"}
        grant = None
        if worker_policy.get('_runtime_authorization_v2') is True:
            from .runtime_authorization import require_operation
            grant = require_operation(root, worker_policy, 'gold')
        # An ACK may be the first event after PostToolUse crashed between
        # private capture and activation. Recovery verifies the exact local
        # sample and source before promoting awaiting_local to queued.
        if grant is None:
            recover_awaiting_edits(root, owner)
        else:
            from .automatic_cohort import admitted
            with _db(root) as db:
                ids = [row[1] for _, row in admitted(db, owner, grant,
                    statuses=('awaiting_local',))]
            recover_awaiting_edits(root, owner, request_ids=ids)
        policy_for(root, owner)
        if grant is None:
            recover_scope_deferred_edits(root, owner)
        else:
            from .automatic_cohort import admitted
            with _db(root) as db:
                ids = [row[1] for _, row in admitted(db, owner, grant,
                    statuses=('pending_review',))]
            recover_scope_deferred_edits(root, owner, request_ids=ids)
        with _db(root) as db:
            if grant is not None:
                from .automatic_cohort import admitted
                pending = [(row[1], row[3]) for _, row in admitted(db, owner,
                    grant, statuses=('queued', 'first_done'))]
            else:
                pending = db.execute(
                    "SELECT c.request_id,c.sample FROM cases c JOIN edit_tickets t "
                    "ON t.request_id=c.request_id WHERE c.project_root=? "
                    "AND t.project_root=? AND c.use_state='active' "
                    "AND c.status IN ('queued','first_done')",
                    (str(owner), str(owner))).fetchall()
        if not any(_case_active(root, (str(owner), *row)) for row in pending):
            return {"status": "ignored"}
        return launch(cfg, owner, after_index=True)
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
        return {"status": "ignored"}


def run_training_pending(root,project):
    """Retry local training only, never teacher/observation or enrollment."""
    import config
    from .runtime_authorization import require_operation
    from .resource_coordination import _lock
    root,project=Path(root),Path(project)
    cfg=config.find_project_config(str(project))['config']
    if _safe_root(cfg,project)!=root:return {'status':'disabled'}
    policy=_policy(root)
    if policy.get('_runtime_authorization_v2') is not True:return {'status':'disabled'}
    import project_learning_setup
    if project_learning_setup.synthetic_local_only(root,project,policy):
        budget=project_learning_setup.synthetic_budget_status(root,project,policy)
        if budget['status']!='ready':return budget
    from . import project_scope
    if project_scope.present(root):
        from .project_worker import setup_ready
        flags=cfg.get('contextLearning',{})
        if flags.get('capture') is not True or flags.get('autoCycle') is not True:
            return {'status':'disabled'}
        if not setup_ready(root):return {'status':'setup_in_progress'}
        if not project_learning_setup.synthetic_local_only(root,project,policy):
            from .general_teacher import read_weekly_quota
            try:read_weekly_quota()
            except ResourceDeferred as exc:return {'status':'deferred_resource','reason':str(exc)}
            except ValueError as exc:
                return {'status':'deferred_quota','reason':str(exc) if str(exc) in (
                    'weekly_quota_checkpoint','weekly_quota_unavailable') else 'weekly_quota_unavailable'}
    from . import learning_profile
    selected_profile=learning_profile.present(root) or cfg.get('sharedLearning',{}).get('profile')==learning_profile.SCHEMA
    profile=None
    if selected_profile:
        try:profile=learning_profile.require(root,policy,'quality_evaluate')
        except ResourceDeferred as exc:return {'status':'deferred_resource','reason':str(exc)}
        except (OSError,ValueError,KeyError,TypeError):return {'status':'runtime_approval_required'}
    fd=_lock(root/'shared-worker.lock')
    try:
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return {'status':'busy'}
        if selected_profile:
            from . import candidate_apply
            path=root/candidate_apply.JOURNAL
            if path.exists() or path.is_symlink():
                intent=candidate_apply.read_journal(root)
                if intent['phase'] not in ('activated','rejected'):
                    if not profile['autoApply']:return {'status':'apply_blocked','reason':'profile_apply_disabled'}
                    return _profile_apply(root,policy,profile,{'status':'already_staged_evaluated','cycle_id':intent['cycle_id']})
                if intent['phase']=='activated':
                    candidate_apply.readable(root);candidate_apply._finish_cycle(root,intent['cycle_id'])
        # Existing fixtures without a shared ledger have no evaluation work.
        if (root/DATABASE_NAME).exists():
            from .evaluation_queue import EvaluationQueue
            from .automatic_staging import evaluate_pending
            evaluations=EvaluationQueue(root).pending()
            if evaluations:
                result=evaluate_pending(root,policy,evaluations[0]['cycle_id'])
                return _profile_apply(root,policy,profile,result)
            if selected_profile:
                with _db(root) as db:
                    from .learning_queue import LearningQueue
                    LearningQueue(root).schema(db)
                    stage=db.execute("SELECT c.cycle_id FROM cycles c JOIN learning_jobs j ON j.job_id=c.cycle_id LEFT JOIN learning_evaluations e ON e.cycle_id=c.cycle_id AND e.namespace=j.namespace WHERE j.namespace=? AND c.status IN ('staged','staged_evaluated') AND (e.status IS NULL OR e.status!='blocked') AND (e.retry_at IS NULL OR e.retry_at<=?) ORDER BY c.started_at DESC LIMIT 1",(EvaluationQueue(root).scope,time.time())).fetchone()
                if stage:
                    result=evaluate_pending(root,policy,stage[0])
                    if (result['status'] not in ('staged_waiting_quality_cohort','already_staged_evaluated')
                            or (result['status']=='already_staged_evaluated' and profile['autoApply'])):
                        return _profile_apply(root,policy,profile,result)
                    if not profile['localTrain']:return result
        if profile and not profile['localTrain']:return {'status':'disabled'}
        try:
            require_operation(root,policy,'local_train')
            if selected_profile:learning_profile.require(root,policy,'local_train')
        except ResourceDeferred as exc:return {'status':'deferred_resource','reason':str(exc)}
        except ValueError:return {'status':'runtime_approval_required'}
        return _profile_apply(root,policy,profile,train_due(root,policy))
    finally:os.close(fd)


def _profile_apply(root,policy,profile,result):
    if profile and profile['autoApply'] and result['status'] in ('staged_evaluated','already_staged_evaluated'):
        from .candidate_apply import apply_candidate
        answer=apply_candidate(root,policy,result['cycle_id'])
        if (root/DATABASE_NAME).exists():
            from .evaluation_queue import EvaluationQueue
            queue=EvaluationQueue(root);job=queue.get('evaluate:'+result['cycle_id'])
            if job is not None:
                if answer['status']=='apply_deferred':queue.defer(job['id'],'apply_resource')
                elif answer['status'] in ('applied','already_applied'):queue.finish(job['id'],'completed')
                elif answer['status']=='apply_blocked':queue.finish(job['id'],'blocked')
        return answer
    return result


def main(argv):
    owned = {"run-owned-worker": _run_owned_worker, "run-owned-training": _run_owned_training}
    if argv[:1] and argv[0] in owned and len(argv) == 3:
        try:
            result = owned[argv[0]](argv[1], argv[2])
        except Exception as exc:
            result = {"status": "failed", "reason": str(exc)[:80]}
        print(canonical(result), flush=True)
        return 1 if result["status"] == "failed" else 0
    if len(argv) != 2 or argv[0] not in ("run", "run-project", "run-project-after-index",
                                          "start-synthetic-training",
                                          "launch-project", "wake-after-index",
                                          "status", "rollback"):
        return 2
    try:
        if argv[0] == "start-synthetic-training":
            import config,project_learning_setup,runtime_work
            project=Path(argv[1]).resolve(strict=True)
            cfg=config.find_project_config(str(project))['config']
            root=_safe_root(cfg,project)
            if root is None or not project_learning_setup.synthetic_local_only(root,project,_policy(root)):
                raise ValueError('synthetic_training_setup_required')
            budget=project_learning_setup.start_synthetic_budget(root,project,_policy(root))
            result=(runtime_work.request('learning',str(project)) or {'status':'deferred_resource'}) if budget['status']=='ready' else budget
        elif argv[0] == "wake-after-index":
            result = wake_after_index(argv[1])
        elif argv[0] == "launch-project":
            import config
            project = Path(argv[1]).resolve(strict=True)
            cfg = config.find_project_config(str(project))["config"]
            result = launch(cfg, project)
        elif argv[0] in ("run-project", "run-project-after-index"):
            import config
            project = Path(argv[1]).resolve(strict=True)
            cfg = config.find_project_config(str(project))["config"]
            root = _safe_root(cfg, project)
            if root is None:
                result = {"status": "disabled"}
            else:
                result = run(root, project=project, sync_config=cfg,
                             wait_for_lock=argv[0] == "run-project-after-index")
                if (result['status'] in ('deferred_resource','staged_evaluation_deferred','apply_deferred')
                        and os.environ.get('QMD_SHARED_GOLD_ONLY')!='1'
                        and _policy(root).get('_runtime_authorization_v2') is True):
                    import runtime_work
                    runtime_work.request('learning',str(project))
                    result={**result,'learning_retry':{'status':'durable_wake_pending'}}
        else:
            root = Path(argv[1]).resolve(strict=True)
            result = {"run": run, "status": status, "rollback": rollback}[argv[0]](root)
    except Exception as exc:
        result = {"status": "failed", "reason": str(exc)[:80]}
    print(canonical(result), flush=True)
    return 1 if result["status"] == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

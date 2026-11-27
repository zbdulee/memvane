"""Explicitly enrolled projects share one durable experimental learning queue.

The normal project learning database and its strict promotion gate are untouched.
This queue accepts immutable capture-time bodies, and only a detached one-shot
worker may call the teacher or local trainer. Uncertain external calls never
retry automatically.
"""
from __future__ import annotations

# The role modules (``shared_*``) read these names through this module, and tests
# patch them here; an import used only by a role module stays for that reason.
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
from memvane.diagnostics.api import telemetry_emit
from memvane.kernel.errors import _query_only_uri

from .compact_input import derive
from .contracts import digest, validate_label, validate_request
from .dataset import read_private_json
from .local_cycle import ResourceDeferred, LocalTrainer, VerifiedLayaTrainer, _atomic_json, _hash, _safe_file
from .store import canonical
from .safe_source import snapshot_beneath

POLICY_NAME = "shared-policy.json"
DATABASE_NAME = "shared-learning.sqlite3"
# Set by the owned-worker spawn when the session redirected CODEX_HOME.
CODEX_HOME_REDIRECTED = "MEMVANE_CODEX_HOME_REDIRECTED"
SCHEMA = "qmd-shared-learning-v1"
STATUSES = ("awaiting_local", "queued", "first_running", "first_done", "second_running",
            "gold", "pending_review", "failed")
REVIEWED_ADAPTER_SHA256 = '1509e6833ba8b2ca23d6e7811173d268c09b16005a2b3d9f9f2838c9ae23bf5e'


def inference_argv(policy, operation):
    """Resolve the reviewed installed adapter for edit inference only."""
    if operation not in ('segment', 'predict'):
        raise ValueError('invalid_shared_inference_operation')
    argv = policy.get('trainerArgv') if isinstance(policy, dict) else None
    current = Path(__file__).with_name('adapter.py')
    if (not isinstance(argv, list) or len(argv) != 3
            or any(not isinstance(value, str) or not Path(value).is_absolute()
                   for value in argv)):
        raise ValueError('invalid_shared_inference_command')
    from .adapter import EDIT_INPUT_VIEW, _read_json
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
    if Path(argv[1]) != current.resolve():
        raise ValueError('historical_inference_adapter_unverified')
    return list(argv)

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


def _policy(root, *, capture=False):
    """The validated shared policy, bound to its execution grant when one exists.

    ``capture`` (``enqueue`` only) passes the default-teacher enable fence
    (``project_scope_teacher_enable_pending``): new captures keep queueing
    while the transaction is pending (plan §5-6). That policy carries no
    grant and runs nothing; every other caller is still refused.
    """
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
    if value["maxGoldCalls"] is None:
        raise ValueError("invalid_shared_budget")
    for key, minimum, maximum in (("batchMin", 2, 100),
                                  ("batchMax", 2, 500), ("maxRssMib", 512, 65536),
                                  ("minAvailableMib", 128, 65536), ("safetySeconds", 60, 3600)):
        if type(value[key]) is not int or not minimum <= value[key] <= maximum:
            raise ValueError("invalid_shared_budget")
    if (type(value["maxGoldCalls"]) is not int
            or not 2 <= value["maxGoldCalls"] <= 1000):
        if value["maxGoldCalls"] != 0:
            raise ValueError("invalid_shared_budget")
        # Zero is reserved for an exact teacher-free QA transaction. The
        # installing phase is needed while profile.activate calls _policy;
        # runtime use still requires the activated phase in synthetic_local_only.
        from memvane.learning import project_learning_setup
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
    adapter = Path(__file__).with_name("adapter.py").resolve()
    general_grant=None
    fenced=False
    from . import project_scope
    if project_scope.present(root):
        from .runtime_authorization import active
        try:
            general_grant=active(root,value,adapter,reviewed_adapter_sha256=REVIEWED_ADAPTER_SHA256)
        except ValueError as exc:
            if not capture or str(exc)!='project_scope_teacher_enable_pending':raise
            # Capture runs no trainer: only the argv shape is checked under the fence.
            fenced=True
    if (not isinstance(argv, list) or len(argv) != 3
            or any(not isinstance(x, str) or not Path(x).is_absolute() for x in argv)
            or (Path(argv[1]).resolve() != adapter
                and not general_grant and not fenced)):
        raise ValueError("invalid_shared_trainer")
    if 'adaptiveMemory' in value:
        from memvane.runtime.api import memory_validate as validate
        validate(value['adaptiveMemory'])
    if 'modelInputView' in value:
        from .adapter import EDIT_INPUT_VIEW, _read_json
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
    if _READ_ONLY_VALIDATION.get() or fenced:
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
    path = Path(root) / DATABASE_NAME
    if _READ_ONLY_VALIDATION.get():
        if path.is_symlink() or not path.is_file():raise ValueError('unsafe_shared_database')
        info=path.stat()
        if info.st_uid!=os.getuid() or info.st_nlink!=1 or info.st_mode&0o077:raise ValueError('unsafe_shared_database')
        with closing(sqlite3.connect(_query_only_uri(path),uri=True,timeout=1)) as db:
            db.execute('PRAGMA query_only=ON');yield db
        return
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
        # Exactly the default-local (False) or default-teacher (True) shape;
        # bool type checks keep 0/1 from comparing equal to False/True.
        return (isinstance(value, dict)
                and all(type(value.get(key)) is bool
                        for key in ("capture", "localTraining", "externalTeacher"))
                and value == {"schema": "qmd-shared-consent-v1",
                              "projectRoot": str(project), "sharedRoot": str(root),
                              "capture": True, "localTraining": True,
                              "externalTeacher": value["externalTeacher"]})
    except (ValueError, OSError):
        return False


def _current_config(root, project):
    if not _consent(root, project):
        return None
    try:
        from memvane.settings import api as qmd_config
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


# Role modules. Every moved name stays an attribute of this module: callers and
# patches address ``shared``, and the moved bodies read their shared names here.
from .shared_cases import (  # noqa: E402
    _case_active, _family_keys, _rows_for_model_view, _gold_fact_keys, _evaluation_keys,
    _query_family_keys, _candidate_exposure_keys, _training_exposure_keys, _selection_exposure_keys,
    _case_reference, _training_provenance, _valid_training_provenance, _holdout_diagnostic,
    _cycle_holdouts, _screen_observation, _candidate_disjoint_metric, _reconcile_cases,
    trial_eligible)
from .shared_queue import (  # noqa: E402
    CASE_LIMIT, enqueue, _queue_summary, status, sync_reviewed, _case, _score, _predict,
    _publish_evaluated)
from .shared_train import (  # noqa: E402
    _regression_reason, _selection_fence, _cadence, _due_threshold, train_due, rollback,
    observe_trial)
from .shared_run import (  # noqa: E402
    _SelfMeter, run, _run_impl, launch, wake_after_index, run_training_pending)
from .shared_dispatch import (  # noqa: E402
    FULL_WORKER_SCHEMA, FULL_WORKER_REQUEST, FULL_WORKER_STATE, FULL_WORKER_LOCK,
    MAX_FULL_WORKER_STARTS, TRAINING_WORKER_SCHEMA, TRAINING_WORKER_STATE, TRAINING_HANDOFF,
    _FULL_WORKER_JOB_KEYS, _full_worker_request, _valid_worker_state, _full_worker_state,
    _training_worker_state, _quarantine, _spawn_owned, _full_step, _training_step,
    learning_worker_step)


def resource_report(policy, *, detailed=False, admission=True, deadline=None):
    from memvane.runtime.api import resource_report as report
    return report(policy, detailed=detailed, admission=admission, deadline=deadline)

def _meta(db, key, default):
    row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else default


def _put_meta(db, key, value):
    db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, canonical(value)))


def _owned_root(project):
    from memvane.settings import api as config
    project = Path(project).resolve(strict=True)
    cfg = config.find_project_config(str(project))["config"]
    return project, cfg, _safe_root(cfg, project)


def _check_owned_lease(root, fd):
    info, expected = os.fstat(fd), (root / FULL_WORKER_LOCK).stat()
    if (info.st_dev, info.st_ino) != (expected.st_dev, expected.st_ino):
        raise ValueError("full_worker_lease_changed")


def _settle_owned(root, name, state, result, project, *, handoff=False):
    """Record one child outcome under the lease; retries keep a durable wake."""
    from memvane.runtime.types import RETRY
    status = result.get("status") if isinstance(result, dict) else None
    status = status[:80] if isinstance(status, str) and status else "failed"
    from .worker_retry import again  # The same retry result too many times in a row settles the request.
    retry = again(root, project, status, status in RETRY)
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
    from memvane.runtime import api as runtime_work
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
    from .learning import ready
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
        if not ready(project):
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


def _profile_apply(root,policy,profile,result):
    if profile and profile['autoApply'] and result['status'] in ('staged_evaluated','already_staged_evaluated'):
        from .candidate_apply import apply_candidate
        answer=apply_candidate(root,policy,result['cycle_id'])
        if (root/DATABASE_NAME).exists():
            from .candidate_close import close_judged
            from .evaluation_queue import EvaluationQueue
            queue=EvaluationQueue(root);job=queue.get('evaluate:'+result['cycle_id'])
            # Both profiles: a judged candidate is closed so no later pass applies it again.
            if close_judged(root,result['cycle_id'],answer,queue) is None and job is not None:
                if answer['status']=='apply_deferred':queue.defer(job['id'],'apply_resource')
                elif answer['status'] in ('applied','already_applied'):queue.finish(job['id'],'completed')
                elif answer['status']=='apply_blocked':
                    from .candidate_close import unapplied
                    unapplied(root,result['cycle_id'],answer,queue,job)
        return answer
    return result


TEACHER_COMMANDS = ("teacher-off", "teacher-on", "restore-teacher-enable", "restore-previous-model")


def _teacher_command(argv):
    """Setup skill commands: ``teacher-off|teacher-on --project P``, ``restore-teacher-enable|restore-previous-model --project P [--plan]``."""
    plan = "--plan" in argv[1:]
    rest = [item for item in argv[1:] if item != "--plan"]
    if len(rest) != 2 or rest[0] != "--project" or (plan and not argv[0].startswith("restore-")):
        raise ValueError("teacher_setup_action_invalid")
    if argv[0].startswith("restore-"):
        from .candidate_rollback import restore_command
        return restore_command(argv[0], rest[1], plan_only=plan)
    from .teacher_state import switch_project
    return switch_project(rest[1], argv[0] == "teacher-on")


def main(argv):
    if argv[:1] and argv[0] in TEACHER_COMMANDS:
        try:
            result = _teacher_command(argv)
        except Exception as exc:
            result = {"status": "failed", "reason": str(exc)[:80]}
        print(canonical(result), flush=True)
        return 1 if result["status"] == "failed" else 0
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
            from memvane.settings import api as config
            from memvane.learning import project_learning_setup
            from memvane.runtime import api as runtime_work
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
            from memvane.settings import api as config
            project = Path(argv[1]).resolve(strict=True)
            cfg = config.find_project_config(str(project))["config"]
            result = launch(cfg, project)
        elif argv[0] in ("run-project", "run-project-after-index"):
            from memvane.settings import api as config
            project = Path(argv[1]).resolve(strict=True)
            cfg = config.find_project_config(str(project))["config"]
            root = _safe_root(cfg, project)
            if root is None:
                result = {"status": "disabled"}
            else:
                result = run(root, project=project, sync_config=cfg,
                             wait_for_lock=argv[0] == "run-project-after-index")
                if (result['status'] in ('deferred_resource','staged_evaluation_deferred','apply_deferred')
                        and _policy(root).get('_runtime_authorization_v2') is True):
                    from memvane.runtime import api as runtime_work
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

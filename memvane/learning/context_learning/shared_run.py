"""Worker runs: the metered detached run, the hook launch and the training-only pass.

``shared`` re-exports every name here and keeps the patch seams: a body reads
each name it shares with ``shared`` through that module when it runs.
"""
from __future__ import annotations

import fcntl
import os
import secrets
import sqlite3
import stat
import subprocess
import threading
import time
from pathlib import Path


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
        from .project_worker import run as run_project_worker
        return run_project_worker(root,project or project_scope.owner(root),sync_config)
    root = Path(root)
    from .learning_profile import present,SCHEMA
    marker=(sync_config or {}).get('sharedLearning',{}).get('profile')==SCHEMA
    if present(root) or marker:
        if project is None:
            from .learning_profile import NAME
            project=Path(_shared.read_private_json(root/NAME)['proposal']['projectRoot'])
        return _shared.run_training_pending(root,project)
    if _shared._policy(root).get('_runtime_authorization_v2') is True:
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
    meter = _shared._SelfMeter() if telemetry_job else None
    worker_status = "error"
    before_report = {}
    if telemetry_job:
        try:
            queued_at = float(os.environ.get("QMD_JOB_QUEUED_AT", ""))
            queue_wait = max(0.0, (time.time() - queued_at) * 1000)
        except ValueError:
            queue_wait = None
        _shared.telemetry_emit(telemetry_path, "started", queue_wait_ms=queue_wait,
                           **telemetry_meta)
        meter.start()
    try:
        if telemetry_job:
            try:
                before_report = _shared.resource_report(_shared._policy(root), detailed=True)
            except Exception:
                before_report = {}
        synced = _shared.sync_reviewed(sync_config, project) if sync_config is not None else None
        result = _shared._run_impl(root, project=project, wait_for_lock=wait_for_lock)
        if synced is not None:
            result = {**result, 'synced': synced}
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
                after_report = _shared.resource_report(_shared._policy(root), detailed=True)
            except Exception:
                after_report = {}
            _shared.telemetry_emit(telemetry_path, "finished", status=worker_status,
                               error_code="worker_failed" if worker_status == "error" else None,
                               resources={**resources, "before": before_report,
                                          "after": after_report},
                               run_ms=(time.monotonic() - worker_started) * 1000,
                               **telemetry_meta)


def _run_impl(root, *, project=None, wait_for_lock=False):
    root = Path(root)
    policy = _shared._policy(root)
    automatic_grant = None
    if policy.get('_runtime_authorization_v2') is True:
        from .runtime_authorization import require_operation
        try:
            automatic_grant = require_operation(root, policy, 'gold')
        except ValueError:
            return {'status': 'runtime_approval_required'}
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
        # The shared worker has no teacher of its own; consented project
        # gold runs in the project worker (`general_teacher`).
        teacher = {"status": "disabled"}
        if automatic_grant is None:
            seen = set()
            while (root / "experimental-checkpoint.json").exists() and not _shared.trial_eligible(root, max_seconds=5):
                pointer = _shared.read_private_json(root / "experimental-checkpoint.json")
                if pointer.get("cycle_id") in seen:
                    raise ValueError("shared_rollback_cycle")
                seen.add(pointer.get("cycle_id"))
                _shared.rollback(root)
        observation = ({'status': 'skipped_staged_authorization'}
                       if automatic_grant is not None else
                       _shared.observe_trial(root, policy))
        cycle = _shared.train_due(root, policy)
        return {"status": cycle["status"], "observation": observation,
                "teacher": teacher, "cycle": cycle}
    finally:
        if waiter_fd is not None:
            os.close(waiter_fd)
        os.close(fd)


def launch(config, project, *, parent_id=None, episode=None,
           after_index=False):
    root = _shared._safe_root(config, project)
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
        try:granted=_shared._policy(root)
        except ValueError as exc:
            # The enable fence: a capture queued under it waits; no worker starts.
            if str(exc)=='project_scope_teacher_enable_pending':return {'status':'teacher_enable_pending'}
            raise
        project_scope.require(root,granted,'local_train')
        from memvane.learning import project_learning_setup
        if project_learning_setup.synthetic_local_only(root,project,_shared._policy(root)):
            # Editing/capture must not start the expressly manual QA job.
            return {'status':'manual_synthetic_training_required'}
    from .learning_profile import present,SCHEMA
    selected_profile=present(root) or config.get('sharedLearning',{}).get('profile')==SCHEMA
    policy = _shared._policy(root)
    # A selected profile's normal worker is training/evaluation only. Let the
    # durable wake own its start, admission and retries instead of spawning a
    # competing detached trainer directly from the hook. General project and
    # legacy source workers still need their capture/teacher phase below.
    if selected_profile and not general_project:
        from memvane.runtime import api as runtime_work
        receipt=runtime_work.request('learning',str(Path(project).resolve()))
        return receipt or {'status':'deferred_resource'}
    # General project and legacy source workers also run the capture/teacher
    # phase. The hook only persists one coalescing full-worker request plus
    # its durable wake; the runtime dispatcher starts the owned child through
    # learning_worker_step, so a crash or reboot cannot drop the teacher phase.
    job_id = "worker:" + secrets.token_hex(12)
    queued_at = time.time()
    pointer_path = root / "experimental-checkpoint.json"
    version = (_shared.read_private_json(pointer_path).get("cycle_id", "base")
               if pointer_path.exists() else "base")
    metadata = {"job_id": job_id, "parent_id": parent_id,
                "episode": episode, "op": "shared_worker",
                "runtime_kind": "local_worker", "model_version": version}
    _shared.telemetry_emit(root / "job-telemetry.jsonl", "queued",
                       ts=queued_at, **metadata)
    _shared._atomic_json(root / _shared.FULL_WORKER_REQUEST, {
        "schema": _shared.FULL_WORKER_SCHEMA, "nonce": secrets.token_hex(16),
        "afterIndex": bool(after_index),
        "job": {"QMD_JOB_ID": job_id, "QMD_JOB_PARENT_ID": parent_id or "",
                "QMD_JOB_EPISODE": str(episode) if episode is not None else "",
                "QMD_JOB_QUEUED_AT": str(queued_at),
                "QMD_JOB_MODEL_VERSION": str(version)}})
    from memvane.runtime import api as runtime_work
    receipt = runtime_work.request('learning', str(Path(project).resolve()))
    return receipt or {'status': 'deferred_resource'}


def wake_after_index(project):
    """Wake an enrolled project's learning worker after its index publish.

    The detached worker waits on the shared OS lock, so an earlier hook
    worker cannot consume the wake before the index catches up.
    """
    from memvane.settings import api as config
    requested = Path(project)
    if not requested.is_absolute() or requested.is_symlink():
        return {"status": "ignored"}
    try:
        owner = requested.resolve(strict=True)
        cfg=config.find_project_config(str(owner))['config']
        enrolled=_shared._safe_root(cfg,owner)
        from . import project_scope
        if enrolled is None or not project_scope.present(enrolled):
            return {"status": "ignored"}
        project_scope.require(enrolled,_shared._policy(enrolled),'local_train')
        return _shared.launch(cfg,owner,after_index=True)
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
        return {"status": "ignored"}


def run_training_pending(root,project):
    """Retry local training only, never teacher/observation or enrollment."""
    from memvane.settings import api as config
    from .runtime_authorization import require_operation
    from memvane.runtime.api import coordination_lock as _lock
    root,project=Path(root),Path(project)
    cfg=config.find_project_config(str(project))['config']
    if _shared._safe_root(cfg,project)!=root:return {'status':'disabled'}
    policy=_shared._policy(root)
    if policy.get('_runtime_authorization_v2') is not True:return {'status':'disabled'}
    from memvane.learning import project_learning_setup
    from memvane.learning.default_enrollment import profile_kind
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
        # Only an explicit profile's teacher spends the Codex weekly quota.
        if profile_kind(root,project,policy)=='explicit':
            from .general_teacher import read_weekly_quota
            try:read_weekly_quota()
            except _shared.ResourceDeferred as exc:return {'status':'deferred_resource','reason':str(exc)}
            except ValueError as exc:
                return {'status':'deferred_quota','reason':str(exc) if str(exc) in (
                    'weekly_quota_checkpoint','weekly_quota_unavailable') else 'weekly_quota_unavailable'}
    from . import learning_profile
    selected_profile=learning_profile.present(root) or cfg.get('sharedLearning',{}).get('profile')==learning_profile.SCHEMA
    profile=None
    if selected_profile:
        try:profile=learning_profile.require(root,policy,'quality_evaluate')
        except _shared.ResourceDeferred as exc:return {'status':'deferred_resource','reason':str(exc)}
        except (OSError,ValueError,KeyError,TypeError):return {'status':'runtime_approval_required'}
    fd=_lock(root/'shared-worker.lock')
    try:
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return {'status':'busy'}
        if selected_profile:
            from . import candidate_apply,candidate_rollback;candidate_rollback.resume(root)  # An owner's restore that stopped.
            path=root/candidate_apply.JOURNAL
            if path.exists() or path.is_symlink():
                intent=candidate_apply.read_journal(root)
                # Resumed first only once its pointer is written; before that nothing is committed.
                if (intent['phase'] not in ('activated','rejected','intent') and (intent['phase']!='prepared'
                        or _shared.read_private_json(root/'active-checkpoint.json')!=intent.get('old_pointer'))):
                    if not profile['autoApply']:return {'status':'apply_blocked','reason':'profile_apply_disabled'}
                    return _shared._profile_apply(root,policy,profile,{'status':'already_staged_evaluated','cycle_id':intent['cycle_id']})
                if intent['phase']=='activated':
                    candidate_apply.readable(root);candidate_apply._finish_cycle(root,intent['cycle_id'])
            if (root/_shared.DATABASE_NAME).exists():
                # Before the staged selection: carry or close cycles of an earlier approval.
                from .candidate_close import supersede
                try:supersede(root,policy,project)
                except _shared.ResourceDeferred as exc:return {'status':'deferred_resource','reason':str(exc)}
        # Existing fixtures without a shared ledger have no evaluation work.
        if (root/_shared.DATABASE_NAME).exists():
            from .evaluation_queue import EvaluationQueue
            from .automatic_staging import evaluate_pending
            evaluations=EvaluationQueue(root).pending()
            if evaluations:
                result=evaluate_pending(root,policy,evaluations[0]['cycle_id'])
                return _shared._profile_apply(root,policy,profile,result)
            if selected_profile:
                with _shared._db(root) as db:
                    from .learning_queue import LearningQueue
                    LearningQueue(root).schema(db)
                    stage=db.execute("SELECT c.cycle_id FROM cycles c JOIN learning_jobs j ON j.job_id=c.cycle_id LEFT JOIN learning_evaluations e ON e.cycle_id=c.cycle_id AND e.namespace=j.namespace WHERE j.namespace=? AND c.status IN ('staged','staged_evaluated') AND (e.status IS NULL OR e.status!='blocked') AND (e.retry_at IS NULL OR e.retry_at<=?) ORDER BY c.started_at DESC LIMIT 1",(EvaluationQueue(root).scope,time.time())).fetchone()
                if stage:
                    result=evaluate_pending(root,policy,stage[0])
                    if (result['status'] not in ('staged_waiting_quality_cohort','already_staged_evaluated')
                            or (result['status']=='already_staged_evaluated' and profile['autoApply'])):
                        return _shared._profile_apply(root,policy,profile,result)
                    if not profile['localTrain']:return result
        if profile and not profile['localTrain']:return {'status':'disabled'}
        try:
            require_operation(root,policy,'local_train')
            if selected_profile:learning_profile.require(root,policy,'local_train')
        except _shared.ResourceDeferred as exc:return {'status':'deferred_resource','reason':str(exc)}
        except ValueError:return {'status':'runtime_approval_required'}
        return _shared._profile_apply(root,policy,profile,_shared.train_due(root,policy))
    finally:os.close(fd)


# Bound last: ``shared`` imports this module while it initializes, and a body
# reads ``_shared`` only when it runs.
from . import shared as _shared  # noqa: E402

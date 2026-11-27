"""Training cadence, one training cycle, its rollback and the trial observation.

``shared`` re-exports every name here and keeps the patch seams: a body reads
each name it shares with ``shared`` through that module when it runs.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path


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
                not _shared._valid_training_provenance(pointer) or
                retained['metrics']['applied'] is not False):
            raise ValueError('shared_retained_fence_invalid')
        return pointer
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError('shared_retained_fence_invalid') from exc


def _cadence(policy, threshold, incumbent, candidate):
    """Use relative holdout quality as well as the absolute pacing target."""
    reason = _shared._regression_reason(incumbent, candidate)
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
    if policy.get('_runtime_authorization_v2') is True:
        from .runtime_authorization import require_operation
        from .automatic_staging import train_due_staged
        try:
            grant = require_operation(root, policy, 'local_train')
        except _shared.ResourceDeferred as exc:
            return {'status':'deferred_resource','reason':str(exc)}
        except ValueError:
            return {'status': 'runtime_approval_required'}
        return train_due_staged(root, policy, grant,
                                trainer_factory=trainer_factory)
    report = _shared.resource_report(policy, detailed=True)
    if not report["ready"]:
        from .local_cycle import record_resource_defer
        record_resource_defer(root, report)
        return {"status": "deferred_resource", "resources": report}
    _shared._reconcile_cases(root)
    pointer_path = root / "experimental-checkpoint.json"
    current = _shared.read_private_json(pointer_path) if pointer_path.exists() else None
    from .adapter import EXPERIMENT_INPUT_VIEW, EDIT_INPUT_VIEW, model_identity
    input_view = current.get("model_input_view") if current else policy.get(
        'modelInputView', EXPERIMENT_INPUT_VIEW)
    if (policy.get('modelInputView') == EDIT_INPUT_VIEW and current is not None
            and input_view != EDIT_INPUT_VIEW):
        return {'status': 'multilingual_migration_required'}
    if (input_view == EDIT_INPUT_VIEW and
            policy.get('modelInputView') != EDIT_INPUT_VIEW):
        return {'status': 'multilingual_policy_revoked'}
    if current and (not _shared._valid_training_provenance(current) or not _shared.trial_eligible(root, max_seconds=5)):
        return {"status": "uncertain_training_provenance"}
    with _shared._db(root) as db:
        all_rows = db.execute("SELECT project_key,request_id,input_sha256,sample,bodies,gold,split,project_root,corpus_fingerprint,family_keys,trained_cycle FROM cases WHERE status='gold' AND use_state='active' AND split!='final' ORDER BY captured_at,request_id").fetchall()
        if any(json.loads(row[3]).get('schema_version') == 4 for row in all_rows):
            if (policy.get('modelInputView') != EDIT_INPUT_VIEW or
                    input_view != EDIT_INPUT_VIEW):
                return {'status': 'multilingual_migration_required'}
        rows = _shared._rows_for_model_view(all_rows, input_view)
        new_count = sum(row[10] is None for row in rows)
        threshold = _shared._meta(db, "next_batch", policy["batchMin"])
        due_threshold, trigger = _shared._due_threshold(rows, threshold, policy)
        if new_count < due_threshold or len(rows) < policy["batchMin"]:
            return {"status": "waiting", "new_gold": new_count,
                    "threshold": due_threshold, "cadence_trigger": trigger}
        evidence = _shared.digest(_shared.canonical([(r[0], r[1], r[2], r[5]) for r in rows]))
        cycle_id = ("shared-" + _shared.digest(_shared.canonical([
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
            _, _, _, diagnostic = _shared._cycle_holdouts(
                rows, evaluated.get("selection_fence", previous))
            if diagnostic:
                _shared._put_meta(db, "holdout_diagnostic", diagnostic)
                return {"status": "insufficient_independent_holdout_families",
                        **diagnostic}
            return _shared._publish_evaluated(root, cycle_id, evaluated, rows)
        if current and current.get("cycle_id") == cycle_id:
            return {"status": "uncertain_training_provenance"}
        try:
            selection_fence = _shared._selection_fence(db, current)
        except ValueError:
            return {"status": "uncertain_training_provenance",
                    "reason": "shared_retained_fence_invalid"}
        validation_rows, fresh_rows, excluded, diagnostic = _shared._cycle_holdouts(
            rows, selection_fence)
        if diagnostic:
            _shared._put_meta(db, "holdout_diagnostic", diagnostic)
            return {"status": "insufficient_independent_holdout_families",
                    **diagnostic}
        train = [_shared._case(row[:6]) for row in rows if row[6] == "train"]
        holdout = [_shared._case(row[:6]) for row in validation_rows]
        fresh_holdout = [_shared._case(row[:6]) for row in fresh_rows]
        try:
            training_provenance = _shared._training_provenance(
                rows, selection_fence, validation_rows=validation_rows)
            refs = ({_shared.canonical(ref): ref for ref in selection_fence["sourceCases"]}
                    if selection_fence else {})
            for row in rows:
                if row[6] != "train" and row not in validation_rows:
                    continue
                ref = _shared._case_reference(row)
                refs[_shared.canonical(ref)] = ref
            if len(refs) > 2000:
                raise ValueError("source_provenance_budget")
            source_refs = [refs[key] for key in sorted(refs)]
        except (ValueError, KeyError, TypeError) as exc:
            return {"status": "uncertain_training_provenance", "reason": str(exc)[:80]}
        if old is None:
            db.execute("INSERT INTO cycles VALUES (?,?,?,?,NULL)",
                       (cycle_id, "training", _shared.canonical({"input_sha256": evidence,
                        "model_input_view": input_view}), time.time()))
    cycles = root / "cycles"
    cycles.mkdir(mode=0o700, exist_ok=True)
    folder = cycles / cycle_id
    folder.mkdir(mode=0o700, exist_ok=True)
    artifact = folder / "candidate.artifact"
    incumbent = Path(current["artifact_path"]) if current else Path(policy["baseArtifact"])
    if current and _shared._hash(incumbent) != current["artifact_sha256"]:
        raise ValueError("shared_incumbent_changed")
    if trainer_factory is None:
        adapter = Path(_shared.__file__).with_name("adapter.py")
        if Path(policy["trainerArgv"][1]).resolve() != adapter.resolve():
            raise ValueError("invalid_shared_trainer")
        trainer = _shared.VerifiedLayaTrainer(policy["trainerArgv"], max_seconds=policy["safetySeconds"],
                        max_rss_mib=policy["maxRssMib"],
                        pressure_probe=lambda **options: _shared.resource_report(policy, admission=False,**options), require_monitor=True)
    else:
        trainer = trainer_factory(policy)
    try:
        if old and old[0] == "trained":
            trained_evidence = json.loads(old[1])
            if (not artifact.exists() or _shared._hash(artifact) != trained_evidence["artifact_sha256"]
                    or _shared._hash(incumbent) != trained_evidence["incumbent_sha256"]):
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
            _shared.telemetry_emit(root / "job-telemetry.jsonl", "queued", **train_meta)
            train_resources = None
            train_status = "error"
            started_at = queued_at
            try:
                artifact.unlink(missing_ok=True)
                (folder / "train.json").unlink(missing_ok=True)
                started_at = time.monotonic()
                _shared.telemetry_emit(root / "job-telemetry.jsonl", "started",
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
                with _shared._db(root) as db:
                    db.execute("UPDATE cycles SET status='trained',evidence=? WHERE cycle_id=? AND status IN ('training','failed','deferred')",
                               (_shared.canonical({"input_sha256": evidence, "artifact_sha256": _shared._hash(artifact),
                                           "incumbent_sha256": _shared._hash(incumbent),
                                           "model_input_view": input_view}), cycle_id))
                train_status = "success"
            finally:
                try:
                    after_report = _shared.resource_report(policy, detailed=True)
                except Exception:
                    after_report = {}
                _shared.telemetry_emit(root / "job-telemetry.jsonl", "finished",
                                   status=train_status,
                                   error_code="train_failed" if train_status == "error" else None,
                                   exit_code=getattr(trainer, "last_exit_code", None),
                                   resources={**(train_resources or getattr(
                                       trainer, "last_resources", None) or {}),
                                              "before": report, "after": after_report},
                                   run_ms=(time.monotonic() - started_at) * 1000,
                                   **train_meta)
        before, before_resources = _shared._predict(trainer, folder, incumbent, holdout,
                                            "incumbent", input_view=input_view)
        after, after_resources = _shared._predict(trainer, folder, artifact, holdout,
                                          "candidate", input_view=input_view)
        if len(fresh_holdout) == len(holdout):
            fresh_before, fresh_after = before, after
            fresh_resources = {"incumbent": before_resources, "candidate": after_resources}
        else:
            fresh_before, fresh_before_resources = _shared._predict(
                trainer, folder, incumbent, fresh_holdout, "fresh-incumbent",
                input_view=input_view)
            fresh_after, fresh_after_resources = _shared._predict(
                trainer, folder, artifact, fresh_holdout, "fresh-candidate",
                input_view=input_view)
            fresh_resources = {"incumbent": fresh_before_resources,
                               "candidate": fresh_after_resources}
        for project_key in {case["project_key"] for case in train + holdout}:
            before["projects"].setdefault(project_key, {"questions": 0, "exact": 0})
            after["projects"].setdefault(project_key, {"questions": 0, "exact": 0})
        with _shared._db(root) as db:
            latest = _shared._rows_for_model_view(db.execute("SELECT project_key,request_id,input_sha256,sample FROM cases WHERE status='gold' AND use_state='active' AND split!='final'").fetchall(), input_view)
            if len(latest) != len(rows):
                raise ValueError("shared_queue_changed_during_cycle")
            if pointer_path.exists() and _shared.read_private_json(pointer_path) != current:
                raise ValueError("shared_pointer_changed")
            record = {"schema_version": 1, "schema": _shared.SCHEMA,
                      "cycle_id": cycle_id, "artifact_path": str(artifact),
                      "artifact_sha256": _shared._hash(artifact), "previous": current or
                      {"artifact_path": str(incumbent), "artifact_sha256": _shared._hash(incumbent)},
                      "experimental": True,
                      "sourceCases": source_refs,
                      "validationCases": [_shared._case_reference(row) for row in validation_rows],
                      "trainingProvenance": training_provenance,
                      "model_input_view": input_view}
            next_batch, cadence_reason = _shared._cadence(
                policy, threshold, fresh_before, fresh_after)
            strict_metric = _shared._candidate_disjoint_metric(
                folder, "fresh-incumbent" if len(fresh_holdout) != len(holdout) else "incumbent",
                "fresh-candidate" if len(fresh_holdout) != len(holdout) else "candidate",
                fresh_rows,
                training_keys=(set(selection_fence["trainingProvenance"]["familyKeys"])
                               if selection_fence else set()) | set().union(*(
                                   _shared._candidate_exposure_keys(row) for row in rows
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
                                     "postflight": _shared.resource_report(policy, detailed=True)},
                       "trial": True, "next_batch": next_batch,
                       "cadence_reason": cadence_reason,
                       "cadence_trigger": trigger, "due_threshold": due_threshold}
            evaluated = {"input_sha256": evidence, "prior_pointer": current,
                         "pointer": record, "metrics": metrics,
                         "model_input_view": input_view,
                         "selection_fence": selection_fence}
            db.execute("UPDATE cycles SET status='evaluated',evidence=? WHERE cycle_id=? AND status='trained'",
                       (_shared.canonical(evaluated), cycle_id))
        return _shared._publish_evaluated(root, cycle_id, evaluated, rows)
    except Exception as exc:
        from .local_cycle import record_resource_defer
        deferred = isinstance(exc, _shared.ResourceDeferred)
        if deferred:
            record_resource_defer(root, exc.report, reason=str(exc))
        with _shared._db(root) as db:
            # A trained artifact stays pinned across downstream failures.
            # Evaluated publication also resumes without a second training.
            db.execute("UPDATE cycles SET status=?,evidence=?,finished_at=? WHERE cycle_id=? AND status IN ('training','failed','deferred')",
                       ("deferred" if deferred else "failed", _shared.canonical({"reason": str(exc)[:80],
                                   "model_input_view": input_view}), time.time(), cycle_id))
        return {"status": "deferred_resource" if deferred else "failed", "reason": str(exc)[:80], "cycle_id": cycle_id}


def rollback(root):
    path = root / "experimental-checkpoint.json"
    if not path.exists():
        return {"status": "no_experiment"}
    current = _shared.read_private_json(path)
    previous = current.get("previous")
    if not isinstance(previous, dict) or _shared._hash(previous["artifact_path"]) != previous["artifact_sha256"]:
        raise ValueError("invalid_rollback_target")
    if previous.get("experimental"):
        _shared._atomic_json(path, previous)
    else:
        path.unlink()
    _shared._atomic_json(root / "last-rollback.json",
                 {"schema": _shared.SCHEMA, "cycle_id": current["cycle_id"],
                  "previous_sha256": previous["artifact_sha256"], "at": time.time()})
    return {"status": "rolled_back", "cycle_id": current["cycle_id"]}




def observe_trial(root, policy, *, trainer_factory=None):
    """Compare the trial on later held-out captures, then undo clear regression."""
    if policy.get('_runtime_authorization_v2') is True:
        # A v2 stage-only grant does not permit observation or rollback of a
        # previously published trial through this direct legacy entrypoint.
        return {"status": "runtime_approval_required"}
    path = root / "experimental-checkpoint.json"
    if not path.exists():
        return {"status": "no_experiment"}
    _shared._reconcile_cases(root)
    preflight = _shared.resource_report(policy, detailed=True)
    if not preflight["ready"]:
        return {"status": "deferred_resource"}
    current = _shared.read_private_json(path)
    if not current.get("experimental") or _shared._hash(current["artifact_path"]) != current["artifact_sha256"]:
        raise ValueError("invalid_experimental_pointer")
    if not _shared._valid_training_provenance(current):
        return {"status": "uncertain_training_provenance"}
    if not _shared.trial_eligible(root, max_seconds=5):
        return {"status": "source_revoked_before_observation"}
    previous = current["previous"]
    if _shared._hash(previous["artifact_path"]) != previous["artifact_sha256"]:
        raise ValueError("invalid_previous_pointer")
    provenance_sha = _shared.digest(_shared.canonical(current["trainingProvenance"]))
    with _shared._db(root) as db:
        rows = _shared._rows_for_model_view(db.execute("SELECT project_key,request_id,input_sha256,sample,bodies,gold,split,project_root,corpus_fingerprint,family_keys,trained_cycle FROM cases WHERE status='gold' AND use_state='active' AND trained_cycle IS NULL AND split='holdout' ORDER BY captured_at,request_id").fetchall(), current.get('model_input_view'))
        if not rows:
            return {"status": "waiting_new_holdout"}
        rows_sha = _shared.digest(_shared.canonical([(row[0], row[1], row[2], row[5]) for row in rows]))
        fingerprint = _shared.digest(_shared.canonical([provenance_sha, rows_sha]))
        provenance = current["trainingProvenance"]
        clean, excluded = _shared._screen_observation(rows,
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
                _shared.rollback(root)
            return prior
        if len(clean) < 2:
            report = {"status": "insufficient_independent_holdout_families",
                      "cycle_id": current["cycle_id"], "independent_holdout": len(clean),
                      "required_holdout": 2, "excluded": excluded,
                      "training_provenance_sha256": provenance_sha,
                      "holdout_sha256": rows_sha}
            db.execute("INSERT OR IGNORE INTO observations VALUES (?,?,?)",
                       (current["cycle_id"], fingerprint, _shared.canonical(report)))
            return report
    cases = [_shared._case(row[:6]) for row in clean]
    if trainer_factory is None:
        adapter = Path(_shared.__file__).with_name("adapter.py")
        klass = _shared.VerifiedLayaTrainer if Path(policy["trainerArgv"][1]).resolve() == adapter.resolve() else _shared.LocalTrainer
        trainer = klass(policy["trainerArgv"], max_seconds=policy["safetySeconds"],
                        max_rss_mib=policy["maxRssMib"],
                        pressure_probe=lambda **options: _shared.resource_report(policy, admission=False,**options), require_monitor=True)
    else:
        trainer = trainer_factory(policy)
    folder = root / "observations" / (current["cycle_id"] + "-" + fingerprint[:16])
    folder.mkdir(parents=True, mode=0o700, exist_ok=True)
    input_view = current.get("model_input_view")
    before, before_resources = _shared._predict(trainer, folder, Path(previous["artifact_path"]),
                                        cases, "previous", input_view=input_view)
    after, after_resources = _shared._predict(trainer, folder, Path(current["artifact_path"]),
                                      cases, "trial", input_view=input_view)
    if (not _shared.trial_eligible(root, max_seconds=5)
            or any(not _shared._case_active(root, (row[7], row[1], row[3])) for row in clean)):
        return {"status": "source_revoked_during_observation"}
    regressed = after["exact_rate"] < before["exact_rate"]
    strict_metric = _shared._candidate_disjoint_metric(
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
                            "postflight": _shared.resource_report(policy, detailed=True)}}
    with _shared._db(root) as db:
        if _shared.read_private_json(path) != current:
            raise ValueError("shared_pointer_changed")
        latest = _shared._rows_for_model_view(db.execute("SELECT project_key,request_id,input_sha256,sample,bodies,gold,split,project_root FROM cases WHERE status='gold' AND use_state='active' AND trained_cycle IS NULL AND split='holdout' ORDER BY captured_at,request_id").fetchall(), input_view)
        if _shared.digest(_shared.canonical([(row[0], row[1], row[2], row[5]) for row in latest])) != rows_sha:
            return {"status": "holdout_changed_during_observation"}
        db.execute("INSERT OR IGNORE INTO observations VALUES (?,?,?)",
                   (current["cycle_id"], fingerprint, _shared.canonical(report)))
    if regressed:
        if not _shared.trial_eligible(root, max_seconds=5):
            uncertain = {"status": "source_revoked_during_observation",
                         "cycle_id": current["cycle_id"],
                         "training_provenance_sha256": provenance_sha,
                         "holdout_sha256": rows_sha}
            with _shared._db(root) as db:
                db.execute("UPDATE observations SET body=? WHERE cycle_id=? AND evidence_sha256=?",
                           (_shared.canonical(uncertain), current["cycle_id"], fingerprint))
            return uncertain
        _shared.rollback(root)
    return report


# Bound last: ``shared`` imports this module while it initializes, and a body
# reads ``_shared`` only when it runs.
from . import shared as _shared  # noqa: E402

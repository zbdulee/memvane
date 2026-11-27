"""Capture queue, its status and the scoring of one candidate on queued cases.

``shared`` re-exports every name here and keeps the patch seams: a body reads
each name it shares with ``shared`` through that module when it runs.
"""
from __future__ import annotations

import json
import time
from pathlib import Path


CASE_LIMIT = 2000  # Case rows of any status; capture stops here (``queue_full``).


def enqueue(config, project, sample, compact_sources):
    """Copy an exact eligible capture once; no current source reread is needed."""
    root = _shared._safe_root(config, project)
    if root is None:
        return "disabled"
    _shared._policy(root, capture=True)  # A malformed shared policy refuses the write.
    if not _shared._consent(root, project):
        return "missing_shared_consent"
    _shared.validate_request(sample)
    if sample["schema_version"] == 4:
        return "edit_capture_unsupported"
    # Production capture stores no prompt case (``seam._origin_gate``); v5
    # edit recall cases share this admission with a 15-card pool.
    if (sample["host"] not in ("codex", "claude") or sample["prompt"]["truncated"]
            or sample["schema_version"] < 2
            or not sample["sampling"]["complete_within_returned_bound"]
            or not sample["candidates"]):
        return "ineligible"
    # The older returned-pool contract keeps its original shared admission.
    # Only the explicit candidate_limit contract can request the wider pool.
    if len(sample["candidates"]) > (30 if sample['schema_version'] == 3 else 15):
        return ("ineligible_pool_over_30" if sample['schema_version'] == 3
                else "ineligible_pool_over_15")
    bodies = {}
    for candidate in sample["candidates"]:
        cid = candidate["candidate_id"]
        if cid not in compact_sources:
            return "incomplete_body"
        bodies[cid] = _shared.derive(compact_sources[cid], candidate["revision_sha256"])["body_text"]
    key = _shared._project_key(project)
    families = _shared._family_keys(key, sample, bodies)
    encoded = _shared.canonical(sample)
    with _shared._db(root) as db:
        old = db.execute("SELECT input_sha256 FROM cases WHERE project_key=? AND request_id=?",
                         (key, sample["request_id"])).fetchone()
        if old:
            return "duplicate" if old[0] == _shared.digest(encoded) else "conflict"
        if (db.execute("SELECT COUNT(*) FROM cases").fetchone()[0] >= _shared.CASE_LIMIT
                or (root / _shared.DATABASE_NAME).stat().st_size > 512 * 1024 * 1024):
            return "queue_full"
        # A target can have several paraphrased questions. Their provisional
        # split follows the same pinned family, even if a common background
        # card appears in every retrieved pool.
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
        train_keys = set().union(*(_shared._query_family_keys(row)
                                   for row in gold_rows if row[6] == 'train'))
        screened, _ = _shared._screen_observation(
            (row for row in gold_rows if row[6] == 'holdout'), train_keys)
        accepted_questions = {key for row in screened for key in _shared._evaluation_keys(row)
                              if key.startswith('question:')}
        pending_questions = {key for (encoded_families,) in db.execute(
            "SELECT family_keys FROM cases WHERE split='holdout' AND use_state='active' "
            "AND status IN ('queued','first_running','first_done','second_running')")
            for key in json.loads(encoded_families) if key.startswith('question:')}
        active_holdouts = len(screened) + len(pending_questions - accepted_questions)
        if "final" in assigned:
            return "final_family_conflict"
        # A scheduled fifth question may inherit a train family from a
        # baseline distractor. Do not relabel it or leak that family into
        # validation; assign the next genuinely unassigned question to the
        # missing holdout slot instead of waiting until question ten.
        holdout_due = active_holdouts < (ordinal + 1) // 5
        split = (next(iter(assigned)) if assigned
                 else "holdout" if holdout_due else "train")
        for family in split_families:
            db.execute("INSERT OR IGNORE INTO families VALUES (?,?)", (family, split))
        db.execute("INSERT INTO cases (project_key,project_root,request_id,input_sha256,sample,bodies,corpus_fingerprint,captured_at,split,family_keys,use_state,status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                   (key, str(Path(project).resolve()), sample["request_id"], _shared.digest(encoded),
                    encoded, _shared.canonical(bodies), sample["sampling"]["index_revision"],
                    time.time(), split, _shared.canonical(families), "active", "queued"))
    return "queued"


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
    with _shared._db(root) as db:
        counts = dict(db.execute("SELECT status,COUNT(*) FROM cases GROUP BY status"))
        use_states = dict(db.execute("SELECT use_state,COUNT(*) FROM cases GROUP BY use_state"))
        latest = db.execute("SELECT cycle_id,status,evidence FROM cycles ORDER BY started_at DESC LIMIT 1").fetchone()
        namespace=_shared.digest(str(Path(root).resolve()))
        learning_queue=_shared._queue_summary(db,'learning_jobs',namespace)
        quality_queue=_shared._queue_summary(db,'learning_evaluations',namespace)
        learning_jobs=learning_queue.get('counts',{})
        reasons = {}
        for error, count in db.execute("SELECT error,COUNT(*) FROM cases WHERE status='pending_review' GROUP BY error"):
            code = error
            if isinstance(error, str) and error.startswith('{'):
                try:code = json.loads(error).get('code', error)
                except (ValueError, AttributeError):pass
            code = code if isinstance(code, str) and code else 'unspecified'
            reasons[code] = reasons.get(code, 0) + count
        return {"status": "ok", "cases": counts, "use_states": use_states,"learning_jobs":learning_jobs,
                "pending_review_reasons": reasons,
                "learning_queue":learning_queue,"quality_queue":quality_queue,
                "gold_calls_used": _shared._meta(db, "gold_calls_used", 0),
                "next_batch": _shared._meta(db, "next_batch", None),
                "holdout_diagnostic": _shared._meta(db, "holdout_diagnostic", None),
                "latest_cycle": {"id": latest[0], "status": latest[1],
                                 "evidence": json.loads(latest[2])} if latest else None}


def sync_reviewed(config, project):
    """Import only independently reviewed local gold from an enrolled project."""
    root = _shared._safe_root(config, project)
    if root is None:
        return {"status": "disabled"}
    from .seam import _private_state
    from .store import database
    state = _private_state(config, Path(project).resolve())
    if state is None:
        return {"status": "missing_project_state"}
    key = _shared._project_key(project)
    imported = 0
    with _shared._db(root) as shared_db, database(state) as project_db:
        if not project_db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='selection_gold'").fetchone():
            return {"status": "waiting_review"}
        rows = shared_db.execute("SELECT request_id,input_sha256,sample FROM cases WHERE project_key=? AND status='queued' AND use_state='active'",
                                 (key,)).fetchall()
        for rid, expected, sample_json in rows:
            if not _shared._case_active(root, (str(project), rid, sample_json)):
                shared_db.execute("UPDATE cases SET use_state='historical',error='consent_or_source_changed' WHERE project_key=? AND request_id=?",
                                  (key, rid))
                continue
            source = project_db.execute("SELECT body FROM samples WHERE request_id=?", (rid,)).fetchone()
            selected = project_db.execute("SELECT body,review FROM selection_gold WHERE request_id=?", (rid,)).fetchone()
            if not source or _shared.digest(source[0]) != expected or not selected or not selected[1]:
                continue
            sample, selection, review = json.loads(source[0]), json.loads(selected[0]), json.loads(selected[1])
            if selection["input_sha256"] != expected or review["selection_sha256"] != _shared.digest(selected[0]):
                continue
            labels = project_db.execute("SELECT candidate_id,body,review FROM labels WHERE request_id=?", (rid,)).fetchall()
            if len(labels) != len(sample["candidates"]) or any(not item[2] for item in labels):
                continue
            from .offline import review_digest
            try:
                checked = {}
                for cid, body, review_body in labels:
                    label = _shared.validate_label(json.loads(body), sample)
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
                              (_shared.canonical(gold), key, rid))
            imported += 1
    return {"status": "synced", "imported": imported}


def _case(row):
    project_key, rid, input_sha, sample_json, bodies_json, gold_json = row
    sample, bodies, gold = json.loads(sample_json), json.loads(bodies_json), json.loads(gold_json)
    if _shared.digest(sample_json) != input_sha or gold["input_sha256"] != input_sha:
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
    pin = {"artifact_sha256": _shared._hash(artifact), "cases_sha256": _shared.digest(_shared.canonical(bare)),
           "model_input_view": input_view}
    if cache_epoch is not None:pin['evaluation_epoch']=cache_epoch
    if output.exists() and pin_path.exists() and _shared.read_private_json(pin_path) == pin:
        answer, resources = _shared.read_private_json(output), {"resumed": True}
    else:
        # An interrupted or earlier attempt may leave a result for other
        # weights. Never reuse it on the strength of its filename alone.
        output.unlink(missing_ok=True)
        pin_path.unlink(missing_ok=True)
        request = {"schema_version": 1, "artifact_path": str(artifact), "cases": bare}
        if input_view is not None:
            request["model_input_view"] = input_view
        answer, resources = trainer.call("predict", request, output, cwd=folder)
        _shared._atomic_json(pin_path, pin)
    return _shared._score(cases, answer), resources


def _publish_evaluated(root, cycle_id, evidence, rows):
    if evidence.get('authorizationSha256') is not None:
        # A staged automatic cohort can never silently enter this legacy
        # publication path, including after its grant expires or is removed.
        raise ValueError('automatic_cohort_publication_not_authorized')
    expected = _shared.digest(_shared.canonical([(r[0], r[1], r[2], r[5]) for r in rows]))
    if evidence["input_sha256"] != expected:
        raise ValueError("shared_queue_changed_during_cycle")
    if any(not _shared._case_active(root, (r[7], r[1], r[3])) for r in rows):
        raise ValueError("shared_case_revoked_during_cycle")
    path = root / "experimental-checkpoint.json"
    current = _shared.read_private_json(path) if path.exists() else None
    record = evidence["pointer"]
    previous = evidence["prior_pointer"]
    validation, fresh, excluded, diagnostic = _shared._cycle_holdouts(
        rows, evidence.get("selection_fence", previous))
    validation_refs = sorted((_shared.canonical(_shared._case_reference(row)) for row in validation))
    if (not _shared._valid_training_provenance(record) or diagnostic
            or validation_refs != sorted(_shared.canonical(ref) for ref in record["validationCases"])
            or len(fresh) != evidence["metrics"]["fresh_holdout_questions"]
            or excluded != evidence["metrics"]["excluded_holdout"]):
        raise ValueError("shared_evaluation_provenance_invalid")
    retention_reason = _shared._regression_reason(
        evidence["metrics"]["fresh_incumbent"],
        evidence["metrics"]["fresh_candidate"])
    if current != evidence["prior_pointer"] and not (
            retention_reason is None and current == record):
        raise ValueError("shared_pointer_changed")
    if retention_reason is None and current != record:
        _shared._atomic_json(path, record)
    metrics = {**evidence["metrics"], "applied": retention_reason is None,
               "retention_reason": retention_reason}
    terminal = "complete" if retention_reason is None else "retained"
    with _shared._db(root) as db:
        latest = _shared._rows_for_model_view(db.execute("SELECT project_key,request_id,input_sha256,sample,bodies,gold,split,project_root FROM cases WHERE status='gold' AND use_state='active' AND split!='final' ORDER BY captured_at,request_id").fetchall(), record.get('model_input_view'))
        if _shared.digest(_shared.canonical([(r[0], r[1], r[2], r[5]) for r in latest])) != expected:
            raise ValueError("shared_queue_changed_during_cycle")
        terminal_evidence = (_shared.canonical(metrics) if terminal == "complete" else
                             _shared.canonical({**evidence, "metrics": metrics}))
        db.execute("UPDATE cycles SET status=?,evidence=?,finished_at=? WHERE cycle_id=? AND status='evaluated'",
                   (terminal, terminal_evidence, time.time(), cycle_id))
        for case in rows:
            db.execute("UPDATE cases SET trained_cycle=? WHERE project_key=? AND request_id=? AND status='gold' AND use_state='active' AND trained_cycle IS NULL",
                       (cycle_id, case[0], case[1]))
        _shared._put_meta(db, "next_batch", evidence["metrics"]["next_batch"])
        _shared._put_meta(db, "holdout_diagnostic", None)
    return {"status": terminal, "cycle_id": cycle_id, "metrics": metrics}


# Bound last: ``shared`` imports this module while it initializes, and a body
# reads ``_shared`` only when it runs.
from . import shared as _shared  # noqa: E402

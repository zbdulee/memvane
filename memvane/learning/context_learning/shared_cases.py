"""Case admission, training provenance and holdout selection of the shared queue.

``shared`` re-exports every name here and keeps the patch seams: a body reads
each name it shares with ``shared`` through that module when it runs.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from contextlib import ExitStack
from pathlib import Path


def _case_active(root, row, *, cfg=None, private_db=None, source_cache=None,
                 deadline=None, ticket_validated=False,
                 create_private_state=True):
    """Read-only continuing-consent, tombstone and source-revision check.

    A v5 edit case skips the source-revision part; its other checks stay.
    """
    if deadline is not None and time.monotonic() >= deadline:
        return False
    project, rid, sample_json = Path(row[0]), row[1], row[2]
    cfg = _shared._current_config(root, project) if cfg is None else cfg
    if cfg is None:
        return False
    try:
        project = project.resolve(strict=True)
        sample = json.loads(sample_json)
        if sample.get('schema_version') == 4:
            # Edit captures came from the retired host-edit ticket flow.
            return False
        if private_db is None:
            from .seam import _private_state
            from .learning_db import read_database
            state = _private_state(cfg, project, create=create_private_state)
            if state is None:
                return False
            db_path = state / "learning.sqlite3"
            if db_path.is_symlink() or not db_path.is_file():
                return False
            with read_database(db_path) as db:
                return _shared._case_active(root, row, cfg=cfg, private_db=db,
                                    source_cache=source_cache, deadline=deadline,
                                    ticket_validated=True)
        if private_db.execute("SELECT 1 FROM tombstones WHERE request_id=?", (rid,)).fetchone():
            return False
        local = private_db.execute("SELECT body FROM samples WHERE request_id=?", (rid,)).fetchone()
        if local is None or _shared.digest(local[0]) != _shared.digest(sample_json):
            return False
        if sample.get('schema_version') == 5 and sample.get('context_origin') == 'tool_edit':
            # Decision 12: the teacher judges an edit case from its immutable
            # snapshot (``cases.bodies``), so a regenerated card keeps it active.
            return True
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
                content = _shared.snapshot_beneath(project, source_path, deadline=deadline)
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
    keys = {"question:" + _shared.digest(prompt)}
    # A recurring background card may share a provisional split. The full
    # captured pool must pass the independent validation screen later.
    focused = set(sample["sampling"]["baseline_selected_ids"])
    for candidate in sample["candidates"]:
        cid = candidate["candidate_id"]
        if cid not in focused:
            continue
        keys.add("document:" + _shared.digest(_shared.canonical([project_key, cid])))
        keys.add("revision:" + candidate["revision_sha256"])
        keys.add("body:" + _shared.digest(bodies[cid]))
    return sorted(keys)


def _rows_for_model_view(rows, input_view):
    from .adapter import EDIT_INPUT_VIEW
    if input_view == EDIT_INPUT_VIEW:
        return [row for row in rows if json.loads(row[3]).get('schema_version') == 4]
    return rows


def _gold_fact_keys(row):
    project_key, sample_json, bodies_json, gold_json = row
    sample, bodies, gold = json.loads(sample_json), json.loads(bodies_json), json.loads(gold_json)
    selected = set(gold["selected_ids"])
    keys = set()
    for candidate in sample["candidates"]:
        cid = candidate["candidate_id"]
        if cid in selected:
            keys.update(("document:" + _shared.digest(_shared.canonical([project_key, cid])),
                         "revision:" + candidate["revision_sha256"],
                         "body:" + _shared.digest(bodies[cid])))
    return keys


def _evaluation_keys(row):
    sample = json.loads(row[3])
    prompt = " ".join(sample["prompt"]["text"].casefold().split())
    return {"question:" + _shared.digest(prompt)} | _shared._gold_fact_keys(
        (row[0], row[3], row[4], row[5]))


def _query_family_keys(row):
    """Families whose gold labels must stay on one side of validation."""
    keys = _shared._evaluation_keys(row)
    if len(row) > 9:
        keys.update(key for key in json.loads(row[9])
                    if key.startswith(("episode:", "target:")))
    return keys


def _candidate_exposure_keys(row):
    """Preserve every seen candidate for the separate corpus-disjoint metric."""
    sample, bodies = json.loads(row[3]), json.loads(row[4])
    keys = _shared._evaluation_keys(row)
    for candidate in sample["candidates"]:
        cid = candidate["candidate_id"]
        keys.update(("document:" + _shared.digest(_shared.canonical([row[0], cid])),
                     "revision:" + candidate["revision_sha256"],
                     "body:" + _shared.digest(bodies[cid])))
    return keys


_training_exposure_keys = _candidate_exposure_keys
_selection_exposure_keys = _candidate_exposure_keys


def _case_reference(row):
    return {"projectRoot": row[7], "requestId": row[1],
            "inputSha256": row[2]}


def _training_provenance(rows, previous, *, validation_rows=None):
    inherited = previous.get("trainingProvenance") if previous and previous.get("experimental") else None
    if previous and previous.get("experimental") and not _shared._valid_training_provenance(previous):
        raise ValueError("missing_inherited_training_provenance")
    refs = {_shared.canonical(ref): ref for ref in inherited["caseRefs"]} if inherited else {}
    keys = set(inherited["familyKeys"]) if inherited else set()
    selection_keys = set(inherited["selectionFamilyKeys"]) if inherited else set()
    query_keys = set(inherited.get("queryFamilyKeys", inherited["familyKeys"])) if inherited else set()
    selection_query_keys = set(inherited.get(
        "selectionQueryFamilyKeys", inherited["selectionFamilyKeys"])) if inherited else set()
    for row in rows:
        if row[6] == "train":
            ref = _shared._case_reference(row)
            refs[_shared.canonical(ref)] = ref
            keys.update(_shared._training_exposure_keys(row))
            query_keys.update(_shared._query_family_keys(row))
    for row in (validation_rows if validation_rows is not None else
                [r for r in rows if r[6] == "holdout"]):
        selection_keys.update(_shared._selection_exposure_keys(row))
        selection_query_keys.update(_shared._query_family_keys(row))
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
        refs = {_shared.canonical(ref) for ref in value["caseRefs"] if isinstance(ref, dict)
                and set(ref) == {"projectRoot", "requestId", "inputSha256"}}
        source_refs = {_shared.canonical(ref) for ref in sources if isinstance(ref, dict)
                       and set(ref) == {"projectRoot", "requestId", "inputSha256"}}
        validation_refs = {_shared.canonical(ref) for ref in validation if isinstance(ref, dict)
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
    train_keys = set(inherited_keys) | set().union(*(_shared._query_family_keys(r) for r in train))
    holdout_keys = set().union(*(_shared._query_family_keys(r) for r in holdout))
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
    training_keys.update(*(_shared._query_family_keys(row) for row in train))
    if previous:
        pinned = {_shared.canonical(ref) for ref in previous["validationCases"]}
        reused = [row for row in holdout if _shared.canonical(_shared._case_reference(row)) in pinned]
        if len(reused) != len(pinned):
            return [], [], [], {"reason": "missing_fixed_validation",
                                "expected": len(pinned), "available": len(reused)}
        incoming = [row for row in holdout if _shared.canonical(_shared._case_reference(row)) not in pinned]
        reused_keys = set().union(*(_shared._query_family_keys(row) for row in reused))
        if training_keys & reused_keys:
            return [], [], [], {"reason": "shared_query_family", "train": len(train),
                                "holdout": len(reused),
                                "overlap_families": len(training_keys & reused_keys)}
        selection_keys = (set().union(*(_shared._query_family_keys(row) for row in reused)) |
                          set(previous["trainingProvenance"].get(
                              "selectionQueryFamilyKeys",
                              previous["trainingProvenance"]["selectionFamilyKeys"])))
    else:
        # A contaminated holdout must not enter validation, but it must not
        # veto a later independent holdout forever. Screen each candidate and
        # retain the exclusions in the cycle evidence.
        reused, incoming = [], holdout
        selection_keys = set()
    fresh, excluded = _shared._screen_observation(incoming, training_keys,
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
        keys = _shared._query_family_keys(row)
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
        excluded.append({"case_hash": _shared.digest(_shared.canonical(_shared._case_reference(row)))[:16],
                         "reason": reason})
    return accepted, excluded


def _candidate_disjoint_metric(folder, baseline_label, trial_label, rows,
                               *, training_keys=(), selection_keys=()):
    """Secondary corpus-disjoint score; never controls the query-family gate."""
    seen = set(training_keys) | set(selection_keys)
    clean = []
    overlap_questions = overlap_families = 0
    for row in rows:
        keys = _shared._candidate_exposure_keys(row)
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
    cases = [_shared._case(row[:6]) for row in clean]
    ids = {case["request_id"] for case in cases}
    for name, label in (("incumbent", baseline_label), ("candidate", trial_label)):
        full = _shared.read_private_json(folder / (label + ".json"))
        selected = {"predictions": [prediction for prediction in full["predictions"]
                                    if prediction["request_id"] in ids]}
        report[name] = _shared._score(cases, selected)
    return report


def _reconcile_cases(root):
    with _shared._db(root) as db:
        rows = db.execute("SELECT project_key,request_id,project_root,sample FROM cases WHERE use_state='active' AND status!='awaiting_local'").fetchall()
    expired = [(key, rid) for key, rid, project, sample in rows
               if not _shared._case_active(root, (project, rid, sample))]
    if expired:
        with _shared._db(root) as db:
            for key, rid in expired:
                db.execute("UPDATE cases SET use_state='historical',error='consent_or_source_changed' WHERE project_key=? AND request_id=? AND use_state='active'",
                           (key, rid))
    return len(expired)


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
        pointer = _shared.read_private_json(path)
        sources = pointer.get("sourceCases")
        from .adapter import EXPERIMENT_INPUT_VIEW, EDIT_INPUT_VIEW
        if (pointer.get("schema_version") != 1 or not pointer.get("experimental")
                or pointer.get("model_input_view") not in (None, EXPERIMENT_INPUT_VIEW,
                                                           EDIT_INPUT_VIEW)
                or not isinstance(sources, list) or not sources or len(sources) > 2000
                or not _shared._valid_training_provenance(pointer)):
            return False
        db_path = Path(root) / _shared.DATABASE_NAME
        if db_path.is_symlink() or not db_path.is_file():
            return False
        with ExitStack() as stack:
            from .learning_db import read_database
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
                    cfg = _shared._current_config(root, project)
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
                                 (_shared._project_key(project), source["requestId"])).fetchone()
                if (row is None or row[0] != source["inputSha256"] or row[2] != "active"
                        or row[3] != "gold"
                        or not _shared._case_active(root, (project, source["requestId"], row[1]),
                                            cfg=projects[project], private_db=private_dbs[project],
                                            source_cache=source_cache, deadline=deadline)):
                    return False
            return time.monotonic() < deadline and all(_shared._consent(root, p) for p in projects)
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
        return False


# Bound last: ``shared`` imports this module while it initializes, and a body
# reads ``_shared`` only when it runs.
from . import shared as _shared  # noqa: E402

"""Only the newly created synthetic project may enter automatic Codex gold.

General shared-learning enrollment never grants external transport. This
separate owner-private policy pins the exact synthetic project, marker bytes,
receiver, purpose, model and budget before any subprocess can be started.
"""
from __future__ import annotations

import hashlib
from contextlib import closing
import json
import os
from pathlib import Path
import re
import stat

from .compact_input import derive
from .contracts import validate_request
from .dataset import read_private_json
from .store import canonical
from .safe_source import snapshot_beneath

POLICY_NAME = "synthetic-teacher-policy.json"
PROVENANCE_NAME = "synthetic-provenance.json"
SCHEMA = "qmd-synthetic-teacher-v1"
APPROVAL_FILE = Path.home() / ".config/laya/synthetic-approval/current.json"
NOVEL_ROOT = Path.home() / "work/novel"
COLLECTION_PATHS = {
    "fantasy-20261008-settings": "01_Settings",
    "fantasy-20261008-characters": "02_Characters",
    "fantasy-20261008-plot": "03_Plot",
    "fantasy-20261008-manuscript": "04_Manuscript",
    "fantasy-20261008-wiki": ".auto-context/wiki",
}
WIKI_COLLECTION = "fantasy-20261008-wiki"
PROJECT_WIKI_COLLECTION = "novel-20261008-wiki"


def project_collection_paths(project):
    from .synthetic_experiment import PLANNED_BUNDLE_NAME
    return {PROJECT_WIKI_COLLECTION: ".auto-context/wiki",
            **{name: f"{PLANNED_BUNDLE_NAME}/{relative}"
               for name, relative in COLLECTION_PATHS.items()}}


def invocation_in_generated_story(found, cwd, targets=None):
    """Limit the root-owned pilot's hook invocations to its generated story.

    The root config is needed for the shared QMD owner, but inherited config
    must not make a sibling directory a search or edit scope. Ordinary projects
    (including separately configured stories under the novel root) retain their
    existing behavior. An edit with multiple or missing targets fails closed.
    """
    from .synthetic_experiment import story_for
    try:
        root = Path(found["projectRoot"]).resolve(strict=False)
        config = found["config"]
        novel = NOVEL_ROOT.resolve(strict=False)
        collections = config.get("collections", [])
        if (root != novel or not isinstance(collections, list)
                or not set(project_collection_paths(root)).intersection(collections)):
            return True
        story = story_for(root).resolve(strict=True)
        if not isinstance(cwd, str) or not Path(cwd).is_absolute():
            return False
        workdir = Path(os.path.abspath(cwd))
        if not _inside_story(workdir, story):
            return False
        if targets is None:
            return True
        if not isinstance(targets, (list, tuple)) or not targets:
            return False
        for target in targets:
            if not isinstance(target, str) or not target:
                return False
            candidate = Path(target)
            if not candidate.is_absolute():
                candidate = workdir / candidate
            if not _inside_story(Path(os.path.abspath(candidate)), story):
                return False
        return True
    except (OSError, ValueError, KeyError, TypeError):
        return False


def _inside_story(path, story):
    try:
        path.relative_to(story)
        path.resolve(strict=False).relative_to(story)
        return True
    except (OSError, ValueError):
        return False


def generated_path(candidate_id):
    collection, sep, relative = candidate_id.partition("/")
    if (not sep or collection not in COLLECTION_PATHS or not relative
            or any(part in ("", ".", "..") or part.startswith(".")
                   for part in Path(relative).parts)
            or Path(relative).is_absolute()):
        raise ValueError("synthetic_source_not_generated")
    return Path(COLLECTION_PATHS[collection]) / relative


def _experiment_config(root, project):
    import config as qmd_config
    from .synthetic_experiment import private_for
    from .synthetic_experiment import story_for
    found = qmd_config.find_project_config(str(story_for(project)))
    cfg = found["config"]
    paths = project_collection_paths(project)
    roles = {name: ("wiki" if name in (PROJECT_WIKI_COLLECTION, WIKI_COLLECTION)
                    else "raw") for name in paths}
    if (found["projectRoot"] != str(project)
            or found.get("configPath") != str(project / ".auto-context/settings.json")
            or found.get("configFormat") != "auto-context-dir"
            or cfg.get("indexing") is not True
            or cfg.get("collections") != list(paths)
            or cfg.get("collectionPaths") != paths
            or cfg.get("collectionRoles") != roles
            or cfg.get("recallStrategy") != "wikiOnly"
            or cfg.get("wikiPath") != paths[WIKI_COLLECTION]
            or {key: cfg.get("sharedLearning", {}).get(key)
                for key in ("enabled", "root", "trialSelection")}
            != {"enabled": True, "root": str(root), "trialSelection": True}
            or not isinstance(cfg.get("sharedLearning", {}).get("rerankTargetSeconds"),
                              (int, float))
            or not .2 <= cfg["sharedLearning"]["rerankTargetSeconds"] <= 10
            or cfg.get("contextLearning", {}).get("capture") is not True
            or cfg.get("contextLearning", {}).get("stateRoot") != str(private_for(project))
            or cfg.get("compile", {}).get("mode") != "off"
            or cfg.get("injectSourcePathsPerCard") != 0
            or cfg.get("allowRoots") != []):
        raise ValueError("synthetic_project_config_mismatch")
    # The experiment's raw story collections lived in an isolated QMD index.
    # The Laya index holds wiki collections only, so no index can prove them.
    raise ValueError("synthetic_project_index_not_selected")


def _active_edit_revisions(db, root, project, story):
    """Exact raw-file exceptions proved by a bound, locally captured edit.

    This never authorizes candidate bodies. Candidate egress still requires a
    generated wiki revision and its current source in allowed_case().
    """
    from .shared import _current_config
    from .seam import _private_state
    from .edit_ticket import validate_case
    if db.execute("SELECT 1 FROM edit_tickets WHERE project_root=? AND "
                  "request_id IS NOT NULL LIMIT 1", (str(project),)).fetchone() is None:
        return {}
    cfg = _current_config(root, project)
    state = _private_state(cfg, Path(project)) if cfg is not None else None
    local_path = state / 'learning.sqlite3' if state is not None else None
    if local_path is None or local_path.is_symlink() or not local_path.is_file():
        return {}
    revisions = {}
    from .history_activation import read_database
    with read_database(local_path) as local:
        for path, before, after, rid, sample_json, bodies_json in db.execute(
                "SELECT t.relative_path,t.before_sha256,t.after_sha256,c.request_id,"
                "c.sample,c.bodies FROM edit_tickets t JOIN cases c ON "
                "c.request_id=t.request_id WHERE c.project_root=? AND "
                "c.use_state='active' AND c.status!='awaiting_local'",
                (str(project),)):
            parts = Path(path).parts
            if not parts or parts[0] not in ('01_Settings', '02_Characters',
                                             '03_Plot', '04_Manuscript'):
                continue
            captured = local.execute('SELECT body FROM samples WHERE request_id=?',
                                     (rid,)).fetchone()
            if (captured != (sample_json,) or local.execute(
                    'SELECT 1 FROM tombstones WHERE request_id=?', (rid,)).fetchone()):
                continue
            try:
                sample = json.loads(sample_json)
                if sample.get('schema_version') != 4:
                    continue
                validate_case(db, root, project, sample, json.loads(bodies_json))
            except (ValueError, OSError, KeyError, TypeError, UnicodeError):
                continue
            if path in revisions and revisions[path] != (before, after):
                raise ValueError('synthetic_edit_revision_ambiguous')
            revisions[path] = (before, after)
    return revisions


def _generated_collections_only(root, project, *, recovery_episode=None,
                                partial_revisions=None):
    """No unproven card or linked file may be indexed before a Codex call."""
    from .shared import _db
    from .synthetic_experiment import story_for
    story = story_for(project)
    root_wiki = project / ".auto-context/wiki"
    if (root_wiki.is_symlink() or not root_wiki.is_dir()
            or any(root_wiki.iterdir())):
        raise ValueError("synthetic_project_wiki_not_empty")
    recovery = {}
    with _db(root) as db:
        edits = _active_edit_revisions(db, root, project, story)
        latest = {}
        for kind, relative, sha in db.execute(
                "SELECT kind,relative_path,sha256 FROM experiment_generated ORDER BY entry_id"):
            latest[relative] = (kind, sha)
        retired = {(relative, sha): archive for relative, sha, archive in db.execute(
            "SELECT relative_path,sha256,archive_relative FROM synthetic_retired_cards "
            "WHERE state='queued'")}
        from .source_card_provenance import latest_paths, registered_revision
        registered = latest_paths(db)
        for relative, (kind, sha) in registered.items():
            if kind in ('host_wiki_card', 'topical_worker_card'):
                archive = retired.get((relative, sha))
                if archive is not None and hashlib.sha256(
                        snapshot_beneath(root, archive)).hexdigest() == sha:
                    continue
                cid = WIKI_COLLECTION + '/' + relative[len('.auto-context/wiki/'):]
                if not registered_revision(db, root, project, cid, sha,
                                           require_index=False):
                    raise ValueError('synthetic_registered_source_invalid')
        latest.update(registered)
        deleted = {(relative, before) for relative, before in db.execute(
            "SELECT relative_path,before_sha256 FROM synthetic_source_events "
            "WHERE operation='delete' AND after_sha256 IS NULL AND state='retired'")}
        if recovery_episode is not None:
            if type(recovery_episode) is not int or not 1 <= recovery_episode <= 100:
                raise ValueError("synthetic_recovery_invalid")
            row = db.execute(
                "SELECT reviewed,source_sha256,materialized_sha256 FROM experiment_pending_final WHERE episode=?",
                (recovery_episode,)).fetchone()
            q1 = db.execute(
                "SELECT i.state FROM experiment_query_tickets t JOIN experiment_invocations i USING(call_key) WHERE t.episode=? AND t.slot=1 ORDER BY i.rowid DESC LIMIT 1",
                (recovery_episode,)).fetchone()
            review = db.execute(
                "SELECT i.state FROM experiment_calls c JOIN experiment_invocations i USING(call_key) WHERE c.kind='wiki_review' AND c.episode=?",
                (recovery_episode,)).fetchone()
            relative = f".auto-context/wiki/ep{recovery_episode:03d}-final.md"
            manuscript = f"04_Manuscript/ep{recovery_episode:03d}.md"
            if (row and row[0] and row[2] is None and q1 == ("complete",)
                    and review == ("complete",)
                    and latest.get(manuscript, (None, None))[1] == row[1]
                    and latest.get(relative, (None,))[0] == "wiki_create"):
                from .synthetic_launcher import _fact_card
                content = _fact_card(story, recovery_episode, "final", row[0],
                                     row[1], verified=True)
                recovery[relative] = hashlib.sha256(content.encode()).hexdigest()
    seen = set()
    partial_revisions = partial_revisions or {}
    for base in COLLECTION_PATHS.values():
        folder = story / base
        if folder.is_symlink():
            raise ValueError("synthetic_collection_linked")
        if not folder.exists():
            continue
        if not folder.is_dir():
            raise ValueError("synthetic_collection_unproven")
        for directory, dirs, files in os.walk(folder, followlinks=False):
            for name in dirs + files:
                path = Path(directory) / name
                info = path.lstat()
                if (stat.S_ISLNK(info.st_mode) or info.st_uid != os.getuid()
                        or (name in dirs and not stat.S_ISDIR(info.st_mode))
                        or (name in files and (not stat.S_ISREG(info.st_mode)
                                               or info.st_nlink != 1))):
                    raise ValueError("synthetic_collection_linked")
                if name in dirs:
                    continue
                relative = path.relative_to(story).as_posix()
                expected = latest.get(relative)
                actual_sha = hashlib.sha256(snapshot_beneath(story, relative)).hexdigest()
                if (actual_sha not in partial_revisions.get(relative, ())
                        and actual_sha != edits.get(relative, (None, None))[1]
                        and (expected is None or expected[0] == "writing_delete"
                             or actual_sha not in (expected[1], recovery.get(relative)))):
                    raise ValueError("synthetic_collection_unproven")
                seen.add(relative)
    for relative, (kind, sha) in latest.items():
        if (kind == 'writing_delete' or relative in seen
                or edits.get(relative, (None, 'not-edited'))[1] is None):
            continue
        archive = retired.get((relative, sha))
        if (relative, sha) in deleted and not relative.startswith('.auto-context/wiki/'):
            continue
        if (not relative.startswith('.auto-context/wiki/') or archive is None
                or hashlib.sha256(snapshot_beneath(root, archive)).hexdigest() != sha):
            raise ValueError("synthetic_collection_missing_generated_file")
    return recovery, edits


def _indexed_collections_only(root, project, *, recovery=None,
                              partial_revisions=None, edits=None):
    """Reject before any external CLI call: no index proves the story collections.

    The experiment's raw story collections were proved against an isolated
    QMD index. The Laya index holds wiki collections only.
    """
    raise ValueError("synthetic_project_index_not_selected")


def _parent_approval(root, project, policy):
    """An operator-held pin outside the project and shared queue is mandatory."""
    from .synthetic_experiment import load as experiment_control, approval as experiment_approval
    experiment = experiment_control(root, project)
    if experiment is not None:
        experiment_approval(root, project, *experiment, policy=policy)
        return
    path = Path(APPROVAL_FILE)
    parent = path.parent
    if (parent.is_symlink() or not parent.is_dir()
            or parent == project or project in parent.parents
            or parent == root or root in parent.parents):
        raise ValueError("missing_parent_experiment_approval")
    info = parent.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("unsafe_parent_experiment_approval")
    try:
        approval = read_private_json(path)
    except (OSError, ValueError) as exc:
        raise ValueError("missing_parent_experiment_approval") from exc
    if approval != {"schema": "qmd-synthetic-parent-approval-v1",
                    "enabled": True, "projectRoot": str(project),
                    "sharedRoot": str(root),
                    "generationManifestSha256": policy["provenanceSha256"],
                    "markerSha256": policy["markerSha256"],
                    "receiver": policy["receiver"], "purpose": policy["purpose"],
                    "model": policy["model"]}:
        raise ValueError("parent_experiment_approval_mismatch")


def _pinned_project_json(project, filename):
    path = project / filename
    value = read_private_json(path)
    raw = snapshot_beneath(project, filename)
    if canonical(value).encode() != raw:
        raise ValueError("synthetic_manifest_changed")
    return value, raw


def _path(root):
    path = Path(root) / POLICY_NAME
    if path.is_symlink() or not path.is_file():
        raise ValueError("synthetic_teacher_not_enabled")
    return path


def _synthetic_bundle(root, project):
    requested_root, requested_project = Path(root), Path(project)
    if any(part.is_symlink() for path in (requested_root, requested_project)
           for part in (path, *path.parents)):
        raise ValueError("outside_synthetic_bundle")
    root, project = requested_root.resolve(strict=True), requested_project.resolve(strict=True)
    from .synthetic_experiment import PLANNED_BUNDLE_NAME, SHARED_NAME, PRIVATE_NAME
    novel = NOVEL_ROOT.resolve(strict=True)
    story = novel / PLANNED_BUNDLE_NAME
    if (project not in (novel, story) or root != novel / SHARED_NAME
            or any(p.is_symlink() for p in (NOVEL_ROOT, project, story, root))):
        raise ValueError("outside_synthetic_bundle")
    parent_info = novel.stat()
    if parent_info.st_uid != os.getuid() or not novel.is_dir():
        raise ValueError("unsafe_synthetic_bundle")
    for path in (story, root, novel / PRIVATE_NAME):
        if not path.is_dir() or path.is_symlink():
            raise ValueError("unsafe_synthetic_bundle")
        info = path.stat()
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("unsafe_synthetic_bundle")
    return root, project


def policy_for(root, project, *, recovery_episode=None,
               for_continuation_repair=False, for_inflight_verification=False):
    root, project = _synthetic_bundle(root, project)
    from .synthetic_experiment import load as experiment_control
    experiment = experiment_control(root, project)
    if experiment is not None and project != NOVEL_ROOT.resolve(strict=True):
        raise ValueError("synthetic_project_root_mismatch")
    value = read_private_json(_path(root))
    keys = {"schema", "enabled", "projectRoot", "markerSha256",
            "provenanceSha256", "receiver",
            "purpose", "model", "maxCalls", "maxInputBytes",
            "maxOutputBytes", "reservedUsd", "reserveUsdPerCall"}
    if not isinstance(value, dict) or set(value) != keys or value["schema"] != SCHEMA or value["enabled"] is not True:
        raise ValueError("invalid_synthetic_policy")
    if (value["projectRoot"] != str(project) or value["receiver"] != "codex-cli"
            or value["purpose"] != "selection-gold" or value["model"] != "gpt-6.1-sol"):
        raise ValueError("synthetic_scope_mismatch")
    max_calls = 320 if experiment is not None else 100
    uncapped = experiment is not None and experiment[0]["maxGoldCalls"] is None
    if ((uncapped and any(value[key] is not None for key in
                          ("maxCalls", "reservedUsd", "reserveUsdPerCall")))
            or (not uncapped and (
                type(value["maxCalls"]) is not int
                or not 2 <= value["maxCalls"] <= max_calls
                or (experiment is not None and value["maxCalls"] != 320)
                or type(value["reservedUsd"]) not in (int, float)
                or not 0 < value["reservedUsd"] <= 50
                or type(value["reserveUsdPerCall"]) not in (int, float)
                or not 0 < value["reserveUsdPerCall"] <= 1
                or value["reserveUsdPerCall"] * value["maxCalls"] > value["reservedUsd"]))
            or type(value["maxInputBytes"]) is not int or not 1024 <= value["maxInputBytes"] <= 131072
            or type(value["maxOutputBytes"]) is not int or not 1024 <= value["maxOutputBytes"] <= 32768):
        raise ValueError("synthetic_budget_invalid")
    from .synthetic_experiment import story_for
    source_root = story_for(project) if experiment is not None else project
    marker, marker_bytes = _pinned_project_json(source_root, ".qmd-synthetic-project.json")
    if (hashlib.sha256(marker_bytes).hexdigest() != value["markerSha256"]
            or not isinstance(marker, dict)
            or set(marker) != {"schema", "projectRoot", "createdUtc", "nonce",
                               "origin", "allowedReceiver", "allowedPurpose",
                               "maxGoldCalls", "model"}
            or marker["schema"] != "qmd-synthetic-project-v1"
            or marker["projectRoot"] != str(project)
            or marker["origin"] != "newly-generated-synthetic-only"
            or marker["allowedReceiver"] != value["receiver"]
            or marker["allowedPurpose"] != value["purpose"]
            or marker["model"] != value["model"]
            or marker["maxGoldCalls"] != value["maxCalls"]
            or not isinstance(marker["nonce"], str)
            or not re.fullmatch(r"[0-9a-f]{48}", marker["nonce"])):
        raise ValueError("synthetic_marker_mismatch")
    if experiment is not None:
        _experiment_config(root, project)
        if for_continuation_repair or for_inflight_verification:
            # The four files can be half-applied after SIGKILL. Validate the
            # identity, consent, marker and generator below, but defer the
            # normal generated/index consistency gate until reconciliation.
            # After a gold call, another approved edit may also be settling;
            # the exact captured candidate and edit are checked below.
            if recovery_episode is not None:
                raise ValueError("synthetic_repair_policy_scope_invalid")
        else:
            recovery, edits = _generated_collections_only(
                root, project, recovery_episode=recovery_episode)
            _indexed_collections_only(root, project, recovery=recovery,
                                      edits=edits)
        from .synthetic_experiment import generator_spec
        spec, spec_sha = generator_spec(project)
        if spec_sha != value["provenanceSha256"] or spec["nonce"] != marker["nonce"]:
            raise ValueError("synthetic_generator_mismatch")
    elif for_continuation_repair:
        raise ValueError("synthetic_repair_policy_scope_invalid")
    else:
        provenance, provenance_bytes = _pinned_project_json(source_root, PROVENANCE_NAME)
        if (hashlib.sha256(provenance_bytes).hexdigest() != value["provenanceSha256"]
            or not isinstance(provenance, dict)
            or set(provenance) != {"schema", "projectRoot", "generator", "nonce",
                                    "sources", "questions"}
            or provenance["schema"] != "qmd-synthetic-provenance-v1"
            or provenance["projectRoot"] != str(project)
            or provenance["generator"] != "newly-generated-synthetic-only"
            or provenance["nonce"] != marker["nonce"]
            or not isinstance(provenance["sources"], dict)
            or not 1 <= len(provenance["sources"]) <= 200
            or not isinstance(provenance["questions"], dict)
            or not 1 <= len(provenance["questions"]) <= 200
            or any(not isinstance(cid, str)
                   or not re.fullmatch(r"wiki/[a-z0-9][a-z0-9/_-]*\.md", cid)
                   or not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha)
                   for cid, sha in provenance["sources"].items())
            or any(not isinstance(rid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", rid)
                   or not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha)
                   for rid, sha in provenance["questions"].items())):
            raise ValueError("synthetic_provenance_mismatch")
    _parent_approval(root, project, value)
    return value


def allowed_case(root, case_project, sample, bodies, *, inflight=False):
    policy = policy_for(root, case_project,
                        for_inflight_verification=inflight)
    project = Path(case_project).resolve(strict=True)
    from .synthetic_experiment import load as experiment_control
    experiment = experiment_control(root, project)
    from .synthetic_experiment import story_for
    source_root = story_for(project) if experiment is not None else project
    if experiment is None:
        provenance, provenance_bytes = _pinned_project_json(project, PROVENANCE_NAME)
        if hashlib.sha256(provenance_bytes).hexdigest() != policy["provenanceSha256"]:
            raise ValueError("synthetic_provenance_changed")
    validate_request(sample)
    if (sample["schema_version"] < 2 or sample["prompt"]["truncated"]
            or not sample["sampling"]["complete_within_returned_bound"]
            or sample["host"] not in ("codex", "claude")
            or not isinstance(bodies, dict)
            or set(bodies) != {c["candidate_id"] for c in sample["candidates"]}
            or any(not isinstance(v, str) or not v.strip() or len(v.encode()) > 16384
                   for v in bodies.values())):
        raise ValueError("synthetic_case_ineligible")
    if experiment is not None:
        from .shared import _db
        from .synthetic_experiment import mapped_episode
        with _db(root) as db:
            if sample['schema_version'] == 4:
                from .edit_ticket import validate_case
                validate_case(db, root, project, sample, bodies)
            else:
                mapped_episode(db, sample["request_id"])
                row = db.execute("SELECT t.prompt_sha256,t.input_sha256,t.candidate_sha256,t.source_id,t.source_sha256,i.state FROM experiment_query_tickets t JOIN experiment_invocations i USING(call_key) WHERE t.request_id=?",
                                 (sample["request_id"],)).fetchone()
                candidate_sha = hashlib.sha256(canonical([
                    [c["candidate_id"], c["revision_sha256"]] for c in sample["candidates"]]).encode()).hexdigest()
                if (row is None or row[0] != sample["prompt"]["sha256"]
                        or row[1] != hashlib.sha256(canonical(sample).encode()).hexdigest()
                        or row[2] != candidate_sha
                        or row[5] != "complete"):
                    raise ValueError("synthetic_capture_not_mapped")
            generated = dict(db.execute("SELECT relative_path,sha256 FROM experiment_generated ORDER BY entry_id"))
            registered = {}
            if sample['schema_version'] == 4:
                from .source_card_provenance import registered_revision
                registered = {candidate['candidate_id']: registered_revision(
                    db, root, project, candidate['candidate_id'],
                    candidate['revision_sha256']) for candidate in sample['candidates']}
    elif provenance["questions"].get(sample["request_id"]) != sample["prompt"]["sha256"]:
        raise ValueError("synthetic_question_not_allowlisted")
    for candidate in sample["candidates"]:
        cid, revision = candidate["candidate_id"], candidate["revision_sha256"]
        if experiment is not None:
            if (generated.get(str(generated_path(cid))) != revision
                    and not registered.get(cid, False)):
                raise ValueError("synthetic_source_not_generated")
        elif provenance["sources"].get(cid) != revision:
            raise ValueError("synthetic_source_not_allowlisted")
        if experiment is not None:
            relative = generated_path(cid)
        else:
            if not cid.startswith("wiki/"):
                raise ValueError("synthetic_source_not_wiki")
            relative = Path(".auto-context") / cid
        source = snapshot_beneath(source_root, relative)
        if hashlib.sha256(source).hexdigest() != revision:
            raise ValueError("synthetic_source_changed")
        try:
            expected_body = derive(source.decode("utf8"), revision)["body_text"]
        except UnicodeError as exc:
            raise ValueError("synthetic_source_invalid") from exc
        if bodies[cid] != expected_body:
            raise ValueError("synthetic_capture_not_from_source")
    payload = canonical({"question": sample["prompt"]["text"],
                         "candidates": [{"id": c["candidate_id"],
                                         "revision": c["revision_sha256"],
                                         "body": bodies[c["candidate_id"]]}
                                        for c in sample["candidates"]]})
    if len(payload.encode()) > policy["maxInputBytes"]:
        raise ValueError("synthetic_input_budget")
    return policy, payload

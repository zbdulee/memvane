#!/usr/bin/env python3
"""Explicit, opt-in rewrite of a project's settings to the minimal model.

The minimal model keeps only ``name`` and ``skipPaths`` in
``.auto-context/settings.json``; ``config.expand_minimal_settings`` derives
the rest. ``plan`` is read-only. ``apply`` rewrites the file only when the
derived collection identity (name, role, canonical root) and wiki path equal
the current effective ones and the identity a selected route binds. A
difference refuses with ``identityDiff``; collections are never renamed.

Installed state that binds the current settings bytes:

- a selected (active) Laya route: the route record, ``reviewed-route.json``,
  the install journal's selected route digest, the selection ledger and the
  selection marker are rebound to the new bytes by ``laya_settings_rebind``
  under the bundle-upgrade locks, with an intent for resume or rollback.
  The derived raw collection may differ from the bound one (a new name, or
  its root expanded to an ancestor such as the project root) while the wiki
  collection is equal: the route, ledger and raw discovery cursor move with
  it. Wiki cards and live compile queues that cite the old raw name would
  stop resolving, so such a rename refuses with
  ``raw_collection_bound_by_wiki_state``.
  While it is between steps, recall serves the published generation
  read-only. QMD-side records that only the legacy QMD path reads (a finished
  setup journal, a shared-store approval) are not rewritten; the proposal
  lists them in ``rebind.staleQmdBindings``.
- a route that is not a verified active selection, a selection marker
  without a route, an unselected initial build (its reviewed build proposal
  binds the settings digest), an unfinished setup journal, or a shared QMD
  store registration/approval of a project without a selected route: refused.

Non-identity fields that change (recall tuning, compile budget, custom
extractor backends, learning flags) are reported in ``changedFields`` and do
not refuse the plan; accepting the proposal digest accepts them.

``name`` (``--name``) replaces a legacy ``name`` that does not derive the
bound collections, for example a non-ASCII title. It must be an ASCII slug
that derives exactly the current collection names.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import datetime as _dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402

SCHEMA = "qmd-minimal-settings-plan-v1"
MAX_SETTINGS_BYTES = 2 * 1024 * 1024
ROUTE_RECORD = "laya-index-route.json"
SELECTION_MARKER = "laya-index-selection.json"
SETUP_JOURNALS = ("install-update-journal.json", "shared-install-journal.json")
FINISHED_SETUP_PHASES = ("activated", "rolled_back", "completed")
_IGNORED_FIELDS = frozenset({"settingsModel"})


class MigrationRefused(Exception):
    def __init__(self, reason, **detail):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


def _present(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _read_settings(project: Path):
    settings = project / config.CONFIG_DIR_NAME / config.SETTINGS_FILE_NAME
    auto = settings.parent
    if auto.is_symlink() or not auto.is_dir() or settings.is_symlink():
        raise MigrationRefused("unsafe_settings_path")
    found = config.find_project_config(str(project))
    if (found.get("configFormat") != "auto-context-dir"
            or found.get("projectRoot") != str(project)
            or found.get("configPath") != str(settings)):
        raise MigrationRefused("canonical_settings_required",
                               configFormat=found.get("configFormat"))
    fd = os.open(settings, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_SETTINGS_BYTES:
            raise MigrationRefused("unsafe_settings_path")
        data = os.read(fd, MAX_SETTINGS_BYTES + 1)
    finally:
        os.close(fd)
    try:
        raw = json.loads(data.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        raise MigrationRefused("invalid_settings_json") from None
    if not isinstance(raw, dict):
        raise MigrationRefused("invalid_settings_json")
    return settings, data, raw


def _canonical(project: Path, relative: str) -> str:
    path = Path(relative).expanduser()
    if not path.is_absolute():
        path = project / path
    return str(path.resolve(strict=False))


def collection_identity(project: Path, cfg: dict) -> dict:
    """Effective collection identity: sorted (name, role, sourceRoot) and wiki root.

    The path match is resolve_paths' first-matching ``collectionPaths`` pattern
    (default "."), without its risky-root refusal, so two configs are never
    compared as equal merely because both resolved to nothing.
    """
    import fnmatch
    roles = config.role_map(cfg)
    paths = cfg.get("collectionPaths") if isinstance(cfg.get("collectionPaths"), dict) else {}
    rows = []
    for name in cfg.get("collections") or []:
        matched = "."
        for pattern, value in paths.items():
            if isinstance(pattern, str) and isinstance(value, str) and fnmatch.fnmatch(name, pattern):
                matched = value
                break
        rows.append({"name": name, "role": config.collection_role(roles, name),
                     "sourceRoot": _canonical(project, matched)})
    wiki = cfg.get("wikiPath")
    return {"collections": sorted(rows, key=lambda row: row["name"]),
            "wikiRoot": _canonical(project, wiki) if isinstance(wiki, str) and wiki else None}


def identity_diff(current: dict, derived: dict) -> list:
    """Per-collection differences between two identities, then the wiki root."""
    left = {row["name"]: row for row in current.get("collections") or []}
    right = {row["name"]: row for row in derived.get("collections") or []}
    diff = []
    for name in sorted(set(left) | set(right), key=str):
        if name not in left or name not in right:
            diff.append({"collection": name, "current": left.get(name),
                         "derived": right.get(name)})
            continue
        for field in ("role", "sourceRoot"):
            if left[name].get(field) != right[name].get(field):
                diff.append({"collection": name, "field": field,
                             "current": left[name].get(field),
                             "derived": right[name].get(field)})
    if "wikiRoot" in current and current.get("wikiRoot") != derived.get("wikiRoot"):
        diff.append({"field": "wikiRoot", "current": current.get("wikiRoot"),
                     "derived": derived.get("wikiRoot")})
    return diff


def minimal_settings_for(raw: dict, name_override=None) -> dict:
    name = raw.get("name") if name_override is None else name_override
    if name_override is not None and (not isinstance(name_override, str)
                                      or config.settings_slug(name_override) != name_override):
        raise MigrationRefused("name_override_invalid", name=name_override)
    if not isinstance(name, str) or not config.settings_slug(name):
        raise MigrationRefused("name_required")
    skip = raw.get("skipPaths", [])
    if (not isinstance(skip, list)
            or any(not isinstance(item, str) or not item for item in skip)):
        raise MigrationRefused("invalid_skip_paths")
    return {"name": name, "skipPaths": list(skip)}


def payload_bytes(minimal: dict) -> bytes:
    return (json.dumps(minimal, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _route_collections(record_path: Path):
    """Collections a route record binds, or None when it cannot be read."""
    from laya_index_host_route import MAX_RECORD_BYTES, _pairs, _read_regular
    try:
        record = json.loads(_read_regular(record_path, MAX_RECORD_BYTES).decode("utf-8"),
                            object_pairs_hook=_pairs)
    except (OSError, ValueError, UnicodeError):
        return None
    if not isinstance(record, dict):
        return None
    if isinstance(record.get("collections"), list):
        rows = record["collections"]
    elif isinstance(record.get("collectionName"), str):
        rows = [{"name": record["collectionName"], "role": "raw",
                 "sourceRoot": record.get("sourceRoot")}]
    else:
        return None
    if any(not isinstance(row, dict) for row in rows):
        return None
    return sorted(({"name": row.get("name"), "role": row.get("role"),
                    "sourceRoot": row.get("sourceRoot")} for row in rows),
                  key=lambda row: str(row["name"]))


def _shared_store_binding(project: Path):
    import qmd_shared_store as store
    base = store.root()
    if not _present(base):
        return None
    try:
        registry = store._registry(store._unlinked_root())
        if any(isinstance(row, dict) and row.get("scope") == str(project)
               for row in registry.values()):
            return "settings_bound_by_shared_store"
        try:
            selected = store.select()
        except ValueError as exc:
            if str(exc) != "plugin_qmd_store_not_ready":
                raise
            return None
        if str(project) in (selected.get("approvedProjects") or {}):
            return "settings_bound_by_shared_store"
        pending, _grant = store._legacy_enrollment(base, selected)
        if pending == str(project):
            return "settings_bound_by_shared_store"
    except Exception as exc:  # unreadable store: cannot prove the bytes are unbound
        return "binding_probe_failed:shared_store:%s" % (str(exc) or type(exc).__name__)
    return None


def _setup_journal_phase(path: Path):
    try:
        if path.is_symlink() or path.stat().st_size > 65536:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value.get("phase") if isinstance(value, dict) else None


def _qmd_bindings(project: Path):
    """QMD-side records of a selected project: (stale names, refusal or None).

    A selected route never enters the QMD setup or shared-store path, so a
    finished setup journal or a shared-store approval keeps the old digest
    without affecting search. An unfinished setup transition still refuses.
    """
    auto = project / config.CONFIG_DIR_NAME
    stale = []
    for name in SETUP_JOURNALS:
        path = auto / name
        if not _present(path):
            continue
        if _setup_journal_phase(path) not in FINISHED_SETUP_PHASES:
            return stale, "settings_bound_by_unfinished_setup_journal"
        stale.append(name)
    reason = _shared_store_binding(project)
    if reason == "settings_bound_by_shared_store":
        stale.append("shared_store_approval")
    elif reason:
        return stale, reason
    return stale, None


def _probe_failed(kind, exc):
    return "binding_probe_failed:%s:%s" % (kind, str(exc) or type(exc).__name__)


def _initial_build(project: Path):
    import laya_initial_build
    return laya_initial_build.lookup(str(project))


def raw_only_move(bound: list, derived: list):
    """(old raw, new raw) when only the raw collection differs and its root expands."""
    from laya_install_journal import raw_collection_rebind
    if not raw_collection_rebind(bound, derived):
        return None
    old = next(row for row in bound if row["role"] == "raw")
    new = next(row for row in derived if row["role"] == "raw")
    old_root, new_root = Path(old["sourceRoot"]), Path(new["sourceRoot"])
    if old_root != new_root and new_root not in old_root.parents:
        return None
    return old, new


_FRONTMATTER_LIMIT = 1 << 20
_LIVE_QUEUES = ("source-queue.jsonl", "verify-queue.jsonl")


def _cites(text: str, name: str) -> bool:
    pattern = r"""["']?collection["']?\s*:\s*["']?%s["']?\s*[,}\n]""" % re.escape(name)
    return re.search(pattern, text) is not None


def raw_name_citations(project: Path, wiki_root: str, name: str) -> dict:
    """Wiki cards (frontmatter) and live compile queue rows that cite raw ``name``.

    Card provenance (``sources``/``sourceRevisions``) names the raw collection;
    recall and the compiler resolve it through the selected collections, so a
    renamed raw collection would reject every such card.
    """
    cards = []
    root = Path(wiki_root)
    for directory, dirs, files in os.walk(root):
        dirs[:] = sorted(item for item in dirs if not (Path(directory) / item).is_symlink())
        for file in sorted(files):
            path = Path(directory) / file
            if not file.endswith(".md") or path.is_symlink():
                continue
            with open(path, "rb") as handle:
                head = handle.read(_FRONTMATTER_LIMIT).decode("utf-8", "replace")
            head = head.replace("\r\n", "\n")
            end = head.find("\n---\n", 4) if head.startswith("---\n") else -1
            if end >= 0 and _cites(head[4:end] + "\n", name):
                cards.append(path.relative_to(root).as_posix())
    queued = 0
    compile_dir = project / config.CONFIG_DIR_NAME / "compile"
    for file in _LIVE_QUEUES:
        path = compile_dir / file
        if path.is_file() and not path.is_symlink():
            queued += sum(1 for line in path.read_text(encoding="utf-8", errors="replace")
                          .splitlines() if _cites(line, name))
    return {"cards": cards, "queued": queued}


def _selected_binding(project: Path, derived: dict, payload: bytes, route: Path):
    bound = _route_collections(route)
    if bound is None:
        return "settings_bound_by_laya_route", {"bindingPath": str(route)}, None
    new_collections = None
    if bound != derived["collections"]:
        move = raw_only_move(bound, derived["collections"])
        if move is None:
            return "bound_collection_identity_changed", {
                "bindingPath": str(route), "boundCollections": bound,
                "identityDiff": identity_diff({"collections": bound}, derived)}, None
        old, new = move
        if old["name"] != new["name"]:
            wiki = next(row for row in bound if row["role"] == "wiki")
            try:
                cited = raw_name_citations(project, wiki["sourceRoot"], old["name"])
            except OSError as exc:
                return _probe_failed("wiki_citations", exc), {}, None
            if cited["cards"] or cited["queued"]:
                return "raw_collection_bound_by_wiki_state", {
                    "bindingPath": str(route), "boundCollections": bound,
                    "identityDiff": identity_diff({"collections": bound}, derived),
                    "rawCollection": old["name"], "citingCards": len(cited["cards"]),
                    "citingCardSample": cited["cards"][:5],
                    "queuedRecords": cited["queued"]}, None
        new_collections = derived["collections"]
    import laya_settings_rebind
    try:
        summary, _exact = laya_settings_rebind.inspect(project, payload, new_collections)
    except laya_settings_rebind.RebindRefused as exc:
        return exc.reason, {"bindingPath": str(route), **exc.detail}, None
    except Exception as exc:
        return _probe_failed("laya_route", exc), {"bindingPath": str(route)}, None
    try:
        build = _initial_build(project)
    except Exception as exc:
        return _probe_failed("initial_build", exc), {}, None
    # The selected install's own build record is not read after selection
    # (the bundle upgrade leaves it as well); a build in another state root is.
    if (build is not None and build.get("phase") != "retired" and
            build.get("identity", {}).get("stateRoot") != summary["stateRoot"]):
        return "settings_bound_by_initial_build", {
            "stateRoot": build.get("identity", {}).get("stateRoot")}, None
    stale, reason = _qmd_bindings(project)
    if reason:
        return reason, {}, None
    return None, {}, {**summary, "staleQmdBindings": stale}


def settings_binding(project: Path, derived: dict, payload: bytes):
    """(refusal, detail, rebind) for the installed state binding the settings bytes.

    ``rebind`` is the selected-route rebind summary when that state can move
    to ``payload``; the refusal is None then.
    """
    auto = project / config.CONFIG_DIR_NAME
    route = auto / ROUTE_RECORD
    if _present(route):
        return _selected_binding(project, derived, payload, route)
    if _present(auto / SELECTION_MARKER):
        return "settings_bound_by_laya_route", {
            "bindingPath": str(auto / SELECTION_MARKER)}, None
    try:
        build = _initial_build(project)
    except Exception as exc:
        return _probe_failed("initial_build", exc), {}, None
    if build is not None:
        # The reviewed build proposal and the pending journal route bind the
        # settings digest. Activate (or abandon) the build, then migrate.
        detail = {"stateRoot": build.get("identity", {}).get("stateRoot"),
                  "next": "activate_or_abandon_index_build_then_migrate"}
        bound = build.get("identity", {}).get("collections")
        if isinstance(bound, list) and all(isinstance(row, dict) for row in bound):
            bound = sorted(({"name": row.get("name"), "role": row.get("role"),
                             "sourceRoot": row.get("sourceRoot")} for row in bound),
                           key=lambda row: str(row["name"]))
            detail["boundCollections"] = bound
            if bound != derived["collections"]:
                detail["identityDiff"] = identity_diff({"collections": bound}, derived)
                return "bound_collection_identity_changed", detail, None
        return "settings_bound_by_initial_build", detail, None
    for name in SETUP_JOURNALS:
        if _present(auto / name):
            return "settings_bound_by_setup_journal", {"bindingPath": str(auto / name)}, None
    reason = _shared_store_binding(project)
    if reason:
        return reason, {}, None
    return None, {}, None


def _changed_fields(current: dict, derived: dict) -> list:
    keys = (set(current) | set(derived)) - _IGNORED_FIELDS
    return sorted(key for key in keys if current.get(key) != derived.get(key))


def _pending_rebind(project: Path):
    import laya_settings_rebind
    try:
        return laya_settings_rebind.pending_for_project(project)
    except laya_settings_rebind.RebindRefused as exc:
        raise MigrationRefused(exc.reason, **exc.detail) from None


def plan(project_root, name=None) -> dict:
    """Read-only proposal.

    ``status`` is ``review_required``, ``already_minimal``, ``rebind_pending``
    (an interrupted selected-route rebind; apply resumes or rolls it back) or
    ``refused``.
    """
    project = Path(project_root)
    try:
        if (not project.is_absolute() or project.is_symlink()
                or project.resolve(strict=True) != project):
            raise MigrationRefused("invalid_project_root")
        settings, data, raw = _read_settings(project)
        digest = hashlib.sha256(data).hexdigest()
        pending = _pending_rebind(project)
        if pending is not None:
            state, intent = pending
            return {"status": "rebind_pending", "projectRoot": str(project),
                    "stateRoot": str(state), "settingsSha256": digest,
                    "proposalSha256": intent["proposalSha256"], "changesApplied": 0,
                    "next": ("migrate-minimal-settings --approve-settings-rewrite "
                             "--accepted-settings-proposal-sha256 <proposalSha256> "
                             "resumes; add --rollback-settings-rebind to restore "
                             "the previous settings")}
        if config.is_minimal_settings(raw):
            return {"status": "already_minimal", "projectRoot": str(project),
                    "settingsSha256": digest, "changesApplied": 0}
        current_cfg = config.normalize_config(raw)
        if current_cfg.get("indexing") is not True:
            # minimal settings cannot say "off"; the local optout marker does.
            raise MigrationRefused("legacy_indexing_not_enabled")
        minimal = minimal_settings_for(raw, name)
        derived_cfg = config.normalize_config(minimal)
        current = collection_identity(project, current_cfg)
        derived = collection_identity(project, derived_cfg)
        if name is not None and (sorted(row["name"] for row in derived["collections"]) !=
                                 sorted(row["name"] for row in current["collections"])):
            raise MigrationRefused("name_override_mismatch", name=name,
                                   currentCollections=sorted(
                                       row["name"] for row in current["collections"]),
                                   derivedCollections=sorted(
                                       row["name"] for row in derived["collections"]))
        payload = payload_bytes(minimal)
        # Bindings first: a bound route or build reports the identity it binds.
        reason, detail, rebind = settings_binding(project, derived, payload)
        if reason:
            raise MigrationRefused(reason, derivedIdentity=derived, **detail)
        moved = rebind is not None and "rawCollection" in rebind
        if current != derived and not (
                moved and current.get("wikiRoot") == derived.get("wikiRoot")
                and current["collections"] == _route_collections(
                    project / config.CONFIG_DIR_NAME / ROUTE_RECORD)):
            raise MigrationRefused("collection_identity_changed",
                                   currentIdentity=current, derivedIdentity=derived,
                                   identityDiff=identity_diff(current, derived))
        proposal = {"schema": SCHEMA, "projectRoot": str(project),
                    "settingsPath": str(settings), "settingsSha256": digest,
                    "minimalSettings": minimal, "identity": derived,
                    "changedFields": _changed_fields(current_cfg, derived_cfg),
                    **({"rebind": rebind} if rebind is not None else {})}
        proposal_sha = hashlib.sha256(json.dumps(
            proposal, sort_keys=True, ensure_ascii=False,
            separators=(",", ":")).encode("utf-8")).hexdigest()
        return {"status": "review_required", "proposalSha256": proposal_sha,
                "changesApplied": 0, **proposal}
    except MigrationRefused as exc:
        return {"status": "refused", "reason": exc.reason, "projectRoot": str(project),
                "changesApplied": 0, **exc.detail}
    except OSError as exc:
        return {"status": "refused", "reason": "settings_read_error",
                "error": str(exc), "projectRoot": str(project), "changesApplied": 0}


@contextmanager
def _setup_lock(auto: Path):
    lock_path = auto / ".install-update.lock"
    try:
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError:
        raise MigrationRefused("unsafe_setup_lock") from None
    try:
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise MigrationRefused("unsafe_setup_lock")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise MigrationRefused("managed_setup_in_progress") from None
        yield
    finally:
        os.close(fd)


def _write_new(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)


def _backup(auto: Path, original: bytes) -> Path:
    stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = auto / ("settings.json.pre-minimal-%s" % stamp)
    _write_new(backup, original)
    return backup


def _rewrite_unbound(auto: Path, current: dict) -> dict:
    """The settings file alone: nothing installed binds its bytes."""
    settings = Path(current["settingsPath"])
    original = settings.read_bytes()
    if hashlib.sha256(original).hexdigest() != current["settingsSha256"]:
        raise MigrationRefused("settings_changed_during_migration")
    backup = _backup(auto, original)
    payload = payload_bytes(current["minimalSettings"])
    expected = config.normalize_config(current["minimalSettings"])
    fd, tmp_name = tempfile.mkstemp(dir=str(auto), prefix="settings.json.",
                                    suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if config.normalize_config(json.loads(tmp.read_text(encoding="utf-8"))) != expected:
            raise MigrationRefused("verification_failed")
        if hashlib.sha256(settings.read_bytes()).hexdigest() != current["settingsSha256"]:
            raise MigrationRefused("settings_changed_during_migration")
        os.replace(tmp, settings)
    finally:
        if tmp.exists():
            tmp.unlink()
    final = json.loads(settings.read_text(encoding="utf-8"))
    if config.normalize_config(final) != expected:
        raise MigrationRefused("verification_failed", backupPath=str(backup))
    return {"backupPath": str(backup), "settingsSha256": hashlib.sha256(payload).hexdigest()}


def _rewrite_selected(project: Path, auto: Path, current: dict) -> dict:
    """Settings plus every selected-route binding, through a recoverable intent."""
    import laya_settings_rebind as rebind
    settings = Path(current["settingsPath"])
    original = settings.read_bytes()
    if hashlib.sha256(original).hexdigest() != current["settingsSha256"]:
        raise MigrationRefused("settings_changed_during_migration")
    payload = payload_bytes(current["minimalSettings"])
    moved = current["rebind"].get("rawCollection") is not None
    intent = rebind.begin(project, current["proposalSha256"], payload,
                          current["identity"]["collections"] if moved else None)
    # The intent holds the exact old bytes too; the backup is the user's copy.
    backup = _backup(auto, original)
    receipt = rebind.forward(intent)
    final = json.loads(settings.read_text(encoding="utf-8"))
    if config.normalize_config(final) != config.normalize_config(current["minimalSettings"]):
        raise MigrationRefused("verification_failed", backupPath=str(backup))
    return {"backupPath": str(backup), "settingsSha256": hashlib.sha256(payload).hexdigest(),
            "rebind": {**current["rebind"], "receiptPath": receipt}}


def _resume(project: Path, pending, accepted_proposal_sha256, rollback) -> dict:
    import laya_settings_rebind as rebind
    import wiki_mutation_lock
    state, intent = pending
    if accepted_proposal_sha256 != intent["proposalSha256"]:
        raise MigrationRefused("proposal_acceptance_required",
                               proposalSha256=intent["proposalSha256"])
    auto = project / config.CONFIG_DIR_NAME
    with rebind.locks(project, state), _setup_lock(auto), wiki_mutation_lock.lock(project):
        intent = rebind.load_intent(state)
        if intent is None or intent["proposalSha256"] != accepted_proposal_sha256:
            raise MigrationRefused("settings_rebind_intent_changed")
        if rollback:
            receipt = rebind.rollback(intent)
            status = "rolled_back"
        else:
            receipt = rebind.forward(intent)
            status = "migrated"
    settings = auto / config.SETTINGS_FILE_NAME
    return {"status": status, "projectRoot": str(project), "settingsPath": str(settings),
            "settingsSha256": hashlib.sha256(settings.read_bytes()).hexdigest(),
            "rebind": {"stateRoot": str(state), "receiptPath": receipt},
            "changesApplied": 1}


def apply(project_root, accepted_proposal_sha256, *, rollback=False, name=None) -> dict:
    """Rewrite settings to the accepted proposal; the original bytes are kept as a backup.

    An interrupted selected-route rebind resumes with the same accepted
    proposal digest, or with ``rollback`` returns to the previous settings.
    """
    import wiki_mutation_lock
    project = Path(project_root)
    try:
        first = plan(project, name)
        if first["status"] == "rebind_pending":
            return _resume(project, _pending_rebind(project), accepted_proposal_sha256,
                           rollback)
        if rollback:
            raise MigrationRefused("settings_rebind_not_pending")
        if first["status"] != "review_required":
            return first
        auto = project / config.CONFIG_DIR_NAME
        if "rebind" in first:
            import laya_settings_rebind as rebind
            outer = rebind.locks(project, first["rebind"]["stateRoot"])
        else:
            from contextlib import nullcontext
            outer = nullcontext()
        with outer, _setup_lock(auto), wiki_mutation_lock.lock(project):
            current = plan(project, name)
            if current["status"] != "review_required":
                return current
            if (not isinstance(accepted_proposal_sha256, str)
                    or current["proposalSha256"] != accepted_proposal_sha256):
                raise MigrationRefused("proposal_acceptance_required",
                                       proposalSha256=current["proposalSha256"])
            if ("rebind" in current) != ("rebind" in first):
                raise MigrationRefused("settings_binding_changed_during_migration")
            done = (_rewrite_selected(project, auto, current) if "rebind" in current
                    else _rewrite_unbound(auto, current))
            return {"status": "migrated", "projectRoot": str(project),
                    "settingsPath": current["settingsPath"],
                    "previousSettingsSha256": current["settingsSha256"],
                    "changedFields": current["changedFields"], "changesApplied": 1,
                    **done}
    except MigrationRefused as exc:
        return {"status": "refused", "reason": exc.reason, "projectRoot": str(project),
                "changesApplied": 0, **exc.detail}
    except OSError as exc:
        return {"status": "refused", "reason": "write_error", "error": str(exc),
                "projectRoot": str(project), "changesApplied": 0}
    except Exception as exc:
        import laya_settings_rebind as rebind
        if isinstance(exc, rebind.RebindRefused):
            return {"status": "refused", "reason": exc.reason, "projectRoot": str(project),
                    "changesApplied": 0, **exc.detail}
        # A failed rebind step leaves its intent: plan reports rebind_pending.
        return {"status": "refused", "reason": "settings_rebind_step_failed",
                "error": str(exc) or type(exc).__name__, "projectRoot": str(project),
                "changesApplied": 0}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("action", choices=("plan", "apply"))
    parser.add_argument("--project", required=True)
    parser.add_argument("--accepted-proposal-sha256")
    parser.add_argument("--rollback-settings-rebind", action="store_true")
    parser.add_argument("--name")
    args = parser.parse_args(argv)
    project = str(Path(args.project).resolve())
    result = plan(project, args.name) if args.action == "plan" else apply(
        project, args.accepted_proposal_sha256, rollback=args.rollback_settings_rebind,
        name=args.name)
    print(json.dumps(result, sort_keys=True, ensure_ascii=False))
    return 0 if result["status"] in ("review_required", "already_minimal", "migrated",
                                     "rebind_pending", "rolled_back") else 1


if __name__ == "__main__":
    raise SystemExit(main())

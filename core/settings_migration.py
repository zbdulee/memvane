#!/usr/bin/env python3
"""Explicit, opt-in rewrite of a project's settings to the minimal model.

The minimal model keeps only ``name`` and ``skipPaths`` in
``.auto-context/settings.json``; ``config.expand_minimal_settings`` derives
the rest. ``plan`` is read-only. ``apply`` rewrites the file only when

- the derived collection identity (name, role, canonical root) and wiki path
  equal the current effective ones, and
- no installed state binds the current settings bytes: a laya-db route
  record or selection marker, a registered initial build, a setup journal, or
  a shared QMD store registration/approval. Every one of those compares the
  settings SHA-256 on later hooks or setup steps, so a rewrite would block
  them. Rebinding them is not done here.

Non-identity fields that change (recall tuning, compile budget, custom
extractor backends, learning flags) are reported in ``changedFields`` and do
not refuse the plan; accepting the proposal digest accepts them.
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


def minimal_settings_for(raw: dict) -> dict:
    name = raw.get("name")
    if not isinstance(name, str) or not config.settings_slug(name):
        raise MigrationRefused("name_required")
    skip = raw.get("skipPaths", [])
    if (not isinstance(skip, list)
            or any(not isinstance(item, str) or not item for item in skip)):
        raise MigrationRefused("invalid_skip_paths")
    return {"name": name, "skipPaths": list(skip)}


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


def _identity_reason(bound, derived, *, changed, same):
    return changed if bound != derived["collections"] else same


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


def settings_binding(project: Path, derived: dict):
    """The first installed state that binds the current settings bytes, or None."""
    auto = project / config.CONFIG_DIR_NAME
    for name in (ROUTE_RECORD, SELECTION_MARKER):
        path = auto / name
        if _present(path):
            bound = _route_collections(path) if name == ROUTE_RECORD else None
            if bound is None:
                return "settings_bound_by_laya_route", {"bindingPath": str(path)}
            return _identity_reason(bound, derived,
                                    changed="bound_collection_identity_changed",
                                    same="settings_bytes_bound_by_laya_route"), {
                "bindingPath": str(path), "boundCollections": bound}
    try:
        import laya_initial_build
        build = laya_initial_build.lookup(str(project))
    except Exception as exc:
        return "binding_probe_failed:initial_build:%s" % (str(exc) or type(exc).__name__), {}
    if build is not None:
        bound = build.get("identity", {}).get("collections")
        detail = {"stateRoot": build.get("identity", {}).get("stateRoot")}
        if isinstance(bound, list) and all(isinstance(row, dict) for row in bound):
            bound = sorted(({"name": row.get("name"), "role": row.get("role"),
                             "sourceRoot": row.get("sourceRoot")} for row in bound),
                           key=lambda row: str(row["name"]))
            detail["boundCollections"] = bound
            return _identity_reason(bound, derived,
                                    changed="bound_collection_identity_changed",
                                    same="settings_bound_by_initial_build"), detail
        return "settings_bound_by_initial_build", detail
    for name in SETUP_JOURNALS:
        if _present(auto / name):
            return "settings_bound_by_setup_journal", {"bindingPath": str(auto / name)}
    reason = _shared_store_binding(project)
    if reason:
        return reason, {}
    return None, {}


def _changed_fields(current: dict, derived: dict) -> list:
    keys = (set(current) | set(derived)) - _IGNORED_FIELDS
    return sorted(key for key in keys if current.get(key) != derived.get(key))


def plan(project_root) -> dict:
    """Read-only proposal. ``status`` is ``review_required``, ``already_minimal`` or ``refused``."""
    project = Path(project_root)
    try:
        if (not project.is_absolute() or project.is_symlink()
                or project.resolve(strict=True) != project):
            raise MigrationRefused("invalid_project_root")
        settings, data, raw = _read_settings(project)
        digest = hashlib.sha256(data).hexdigest()
        if config.is_minimal_settings(raw):
            return {"status": "already_minimal", "projectRoot": str(project),
                    "settingsSha256": digest, "changesApplied": 0}
        current_cfg = config.normalize_config(raw)
        if current_cfg.get("indexing") is not True:
            # minimal settings cannot say "off"; the local optout marker does.
            raise MigrationRefused("legacy_indexing_not_enabled")
        minimal = minimal_settings_for(raw)
        derived_cfg = config.normalize_config(minimal)
        current = collection_identity(project, current_cfg)
        derived = collection_identity(project, derived_cfg)
        # Bindings first: a bound route reports the identity it binds.
        reason, detail = settings_binding(project, derived)
        if reason:
            raise MigrationRefused(reason, derivedIdentity=derived, **detail)
        if current != derived:
            raise MigrationRefused("collection_identity_changed",
                                   currentIdentity=current, derivedIdentity=derived)
        proposal = {"schema": SCHEMA, "projectRoot": str(project),
                    "settingsPath": str(settings), "settingsSha256": digest,
                    "minimalSettings": minimal, "identity": derived,
                    "changedFields": _changed_fields(current_cfg, derived_cfg)}
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


def apply(project_root, accepted_proposal_sha256) -> dict:
    """Rewrite settings to the accepted proposal; the original bytes are kept as a backup."""
    import wiki_mutation_lock
    project = Path(project_root)
    first = plan(project)
    if first["status"] != "review_required":
        return first
    auto = project / config.CONFIG_DIR_NAME
    try:
        with _setup_lock(auto), wiki_mutation_lock.lock(project):
            current = plan(project)
            if current["status"] != "review_required":
                return current
            if (not isinstance(accepted_proposal_sha256, str)
                    or current["proposalSha256"] != accepted_proposal_sha256):
                raise MigrationRefused("proposal_acceptance_required",
                                       proposalSha256=current["proposalSha256"])
            settings = Path(current["settingsPath"])
            original = settings.read_bytes()
            if hashlib.sha256(original).hexdigest() != current["settingsSha256"]:
                raise MigrationRefused("settings_changed_during_migration")
            stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            backup = auto / ("settings.json.pre-minimal-%s" % stamp)
            _write_new(backup, original)
            payload = (json.dumps(current["minimalSettings"], ensure_ascii=False, indent=2)
                       + "\n").encode("utf-8")
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
            return {"status": "migrated", "projectRoot": str(project),
                    "settingsPath": str(settings), "backupPath": str(backup),
                    "previousSettingsSha256": current["settingsSha256"],
                    "settingsSha256": hashlib.sha256(payload).hexdigest(),
                    "changedFields": current["changedFields"], "changesApplied": 1}
    except MigrationRefused as exc:
        return {"status": "refused", "reason": exc.reason, "projectRoot": str(project),
                "changesApplied": 0, **exc.detail}
    except OSError as exc:
        return {"status": "refused", "reason": "write_error", "error": str(exc),
                "projectRoot": str(project), "changesApplied": 0}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("action", choices=("plan", "apply"))
    parser.add_argument("--project", required=True)
    parser.add_argument("--accepted-proposal-sha256")
    args = parser.parse_args(argv)
    project = str(Path(args.project).resolve())
    result = plan(project) if args.action == "plan" else apply(
        project, args.accepted_proposal_sha256)
    print(json.dumps(result, sort_keys=True, ensure_ascii=False))
    return 0 if result["status"] in ("review_required", "already_minimal", "migrated") else 1


if __name__ == "__main__":
    raise SystemExit(main())

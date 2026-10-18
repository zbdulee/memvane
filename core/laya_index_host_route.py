#!/usr/bin/env python3
"""Read-only host boundary for a reviewed Laya Index route candidate.

Absence of the route record preserves the existing QMD path. Presence is
sticky: malformed, stale, or unverifiable records block the hook instead of
falling back to QMD. This module validates only the route record and settings
binding. The product runtime must independently verify the database incarnation
and package identity before serving or writing anything.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import unicodedata

import config

SCHEMA = "laya-index-route-v1"
RECORD = "laya-index-route.json"
QMD_SELECTION_MARKER = "laya-index-selection.json"
MAX_RECORD_BYTES = 8192
MAX_SETTINGS_BYTES = 2 * 1024 * 1024
FIELDS = frozenset({"schema", "status", "projectRoot", "settingsSha256",
                    "dbPath", "queuePath", "dbIncarnation", "scopeId", "collectionName",
                    "sourceRoot", "pluginRoot", "journalPath", "installId",
                    "generation", "bundleManifestSha256",
                    "ownershipManifestSha256", "vectorProfile"})
VECTOR_PROFILE_FIELDS = frozenset({"profileId", "modelId", "embeddingFingerprint",
                                   "dimension", "metric", "approvedProviderId",
                                   "providerArtifactSha256", "providerRelativePath"})
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
INCARNATION = re.compile(r"[0-9a-f]{32}\Z")
PACKAGE_GENERATION = re.compile(r"g-[0-9a-f]{32}\Z")
COLLECTION_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
PROVIDER_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}\Z")
ROUTED_ACTIONS = frozenset({"recall", "update", "update-queued", "gate", "posttool",
                            "index", "compile", "file-change-event",
                            "topical-event", "topical-stop", "topical-reconcile"})
MAX_HOOK_BYTES = 2 * 1024 * 1024


def _decision(state: str, reason: str, **details: str) -> dict:
    return {"state": state, "reason": reason, **details}


def _canonical_absolute(value: object) -> bool:
    if not isinstance(value, str) or not value or "\x00" in value:
        return False
    path = Path(value)
    return path.is_absolute() and str(path.resolve(strict=False)) == value


def _pairs(items: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate_route_key")
        result[key] = value
    return result


def _read_regular(path: Path, limit: int) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            raise ValueError("unsafe_route_file")
        if before.st_uid != os.getuid() or before.st_mode & stat.S_IWOTH:
            raise ValueError("unsafe_route_file_owner")
        data = os.read(fd, limit + 1)
        after = os.fstat(fd)
        if (len(data) > limit or len(data) != before.st_size
                or (before.st_dev, before.st_ino, before.st_mtime_ns, before.st_size)
                != (after.st_dev, after.st_ino, after.st_mtime_ns, after.st_size)):
            raise ValueError("route_file_changed")
        return data
    finally:
        os.close(fd)


def _vector_profile(value: object) -> dict:
    """Validate a route's exact provider/profile identity, not its approval."""
    if not isinstance(value, dict) or set(value) != VECTOR_PROFILE_FIELDS:
        raise ValueError("invalid_vector_profile")
    model = value["modelId"]
    fingerprint = value["embeddingFingerprint"]
    dimension = value["dimension"]
    relative = value["providerRelativePath"]
    if (not isinstance(model, str) or not 1 <= len(model) <= 512
            or any(unicodedata.category(char) in {"Cc", "Cf", "Cs", "Zl", "Zp"}
                   for char in model)
            or not isinstance(fingerprint, str) or not SHA256.fullmatch(fingerprint)
            or type(dimension) is not int or not 1 <= dimension <= 2048
            or value["metric"] != "cosine"
            or not isinstance(value["approvedProviderId"], str)
            or not PROVIDER_ID.fullmatch(value["approvedProviderId"])
            or not isinstance(value["providerArtifactSha256"], str)
            or not SHA256.fullmatch(value["providerArtifactSha256"])
            or not isinstance(relative, str) or not relative.startswith("core/")
            or not relative.endswith(".py")
            or "\\" in relative or any(part in ("", ".", "..") for part in relative.split("/"))):
        raise ValueError("invalid_vector_profile")
    canonical = json.dumps([model, fingerprint, dimension, "cosine"],
                           sort_keys=True, ensure_ascii=False,
                           separators=(",", ":")).encode("utf-8")
    if value["profileId"] != hashlib.sha256(canonical).hexdigest():
        raise ValueError("vector_profile_id_mismatch")
    return value


def _selected_settings_supported(raw: dict, cfg: dict, collection: str) -> bool:
    """The first selected recall route supports one flat raw collection."""
    roles = raw.get('collectionRoles', {})
    if (raw.get('recallStrategy') != 'flat' or
            not isinstance(roles, dict) or
            any(key != collection or value != 'raw' for key, value in roles.items()) or
            config.role_map(cfg).get(collection, 'raw') != 'raw' or
            raw.get('skipPaths', []) != [] or cfg.get('skipPaths') != [] or
            raw.get('lexicalPatterns', []) not in ([], ['ep']) or
            cfg.get('lexicalPatterns') not in ([], ['ep']) or
            raw.get('allowRoots', []) != []):
        return False
    for key in ('minScore', 'rawFallbackMinScore'):
        value = raw.get(key, 0)
        if (type(value) not in (int, float) or not math.isfinite(value) or
                value != 0 or cfg.get(key) != 0):
            return False
    top_n = raw.get('topN', 3)
    return type(top_n) is int and 1 <= top_n <= 5 and cfg.get('topN') == top_n


def _candidate_paths(cwd: Path):
    # Search all ancestors for a *blocking* marker. Ordinary QMD config search
    # stops at HOME (or cwd outside HOME), but a nested cwd outside HOME must
    # not miss an already-selected route at its project root and enter QMD.
    # Candidate activation still requires config's exact project-root binding.
    for directory in (cwd, *cwd.parents):
        auto = directory / ".auto-context"
        path = auto / RECORD
        try:
            path.lstat()
        except FileNotFoundError:
            continue
        yield directory, auto, path


def select_route(cwd: str, *, action: str = "recall") -> dict:
    """Classify a hook without creating a directory, database, or runtime.

    Candidate means only that the route record and source settings agree. It
    does not mean that an active product service is ready. The caller must use
    the runtime's own proof check; its failure may never select legacy QMD.
    """
    if action not in ROUTED_ACTIONS:
        return _decision("blocked", "unsupported_route_action")
    try:
        requested = Path(cwd)
        if not requested.is_absolute():
            return _decision("blocked", "invalid_cwd")
        current = requested.resolve(strict=False)
        records = list(_candidate_paths(current))
        markers = []
        for directory in (current, *current.parents):
            marker = directory / '.auto-context' / QMD_SELECTION_MARKER
            if marker.exists() or marker.is_symlink():
                markers.append((directory, marker))
    except (OSError, RuntimeError, ValueError):
        return _decision("blocked", "route_lookup_failed")
    if len(markers) > 1:
        return _decision("blocked", "ambiguous_selection_markers")
    if markers:
        # A QMD-origin transition keeps this marker even after route removal.
        # The installer journal, not absence of a route file, authorizes QMD
        # restoration. Any uncertain cross-file state stops the hook.
        from laya_qmd_cutover import classify_marker
        marker_root, marker_path = markers[0]
        selection = classify_marker(marker_root, marker_path,
                                    marker_path.parent / RECORD)
        if selection == 'blocked':
            return _decision("blocked", "qmd_selection_uncertain")
        if selection == 'legacy':
            if records:
                return _decision("blocked", "qmd_route_unexpected")
            return _decision("legacy", "qmd_selection_verified")
        if (selection != 'candidate' or len(records) != 1 or
                records[0][0] != marker_root):
            return _decision("blocked", "qmd_route_unverified")
    elif not records:
        return _decision("legacy", "no_route_record")
    if not requested.is_dir():
        return _decision("blocked", "route_cwd_unavailable")
    # A nested/stale route must not be ignored just because an ancestor has
    # ordinary QMD settings or a second route record.
    if len(records) != 1:
        return _decision("blocked", "ambiguous_route_records")
    root, auto, path = records[0]
    try:
        if auto.is_symlink() or path.is_symlink() or not auto.is_dir():
            raise ValueError("unsafe_route_path")
        found = config.find_project_config(str(current))
        if (found.get("configFormat") != "auto-context-dir"
                or found.get("projectRoot") != str(root)
                or found.get("configPath") != str(root / ".auto-context" / "settings.json")):
            raise ValueError("route_settings_not_selected")
        settings = auto / "settings.json"
        if settings.is_symlink():
            raise ValueError("unsafe_route_settings")
        raw = _read_regular(path, MAX_RECORD_BYTES)
        record = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs)
        if not isinstance(record, dict) or set(record) != FIELDS:
            raise ValueError("invalid_route_fields")
        if record["schema"] != SCHEMA or record["status"] != "active":
            raise ValueError("route_not_active")
        if record["projectRoot"] != str(root) or not root.is_absolute():
            raise ValueError("route_project_mismatch")
        if not isinstance(record["settingsSha256"], str) or not SHA256.fullmatch(record["settingsSha256"]):
            raise ValueError("invalid_settings_digest")
        if not isinstance(record["bundleManifestSha256"], str) or not SHA256.fullmatch(record["bundleManifestSha256"]):
            raise ValueError("invalid_bundle_manifest_digest")
        if not isinstance(record["ownershipManifestSha256"], str) or not SHA256.fullmatch(record["ownershipManifestSha256"]):
            raise ValueError("invalid_ownership_manifest_digest")
        if not isinstance(record["installId"], str) or not INCARNATION.fullmatch(record["installId"]):
            raise ValueError("invalid_install_id")
        if not isinstance(record["generation"], str) or not PACKAGE_GENERATION.fullmatch(record["generation"]):
            raise ValueError("invalid_package_generation")
        if not isinstance(record["dbIncarnation"], str) or not INCARNATION.fullmatch(record["dbIncarnation"]):
            raise ValueError("invalid_db_incarnation")
        plugin_root = record["pluginRoot"]
        if not _canonical_absolute(plugin_root):
            raise ValueError("invalid_plugin_root")
        journal_path = record["journalPath"]
        if (not _canonical_absolute(journal_path)
                or journal_path != str(Path(plugin_root).parent / "install-journal.sqlite")):
            raise ValueError("invalid_journal_path")
        db_path = record["dbPath"]
        queue_path = record["queuePath"]
        if not _canonical_absolute(db_path) or Path(db_path) == root:
            raise ValueError("invalid_db_path")
        if not _canonical_absolute(queue_path) or queue_path == db_path:
            raise ValueError("invalid_queue_path")
        settings_bytes = _read_regular(settings, MAX_SETTINGS_BYTES)
        settings_digest = hashlib.sha256(settings_bytes).hexdigest()
        if settings_digest != record["settingsSha256"]:
            raise ValueError("route_settings_changed")
        raw_settings = json.loads(settings_bytes.decode("utf-8"), object_pairs_hook=_pairs)
        if not isinstance(raw_settings, dict):
            raise ValueError("invalid_route_settings")
        cfg = config.normalize_config(raw_settings)
        if cfg.get("indexing") is not True:
            raise ValueError("route_indexing_not_selected")
        raw_compile = raw_settings.get("compile", {})
        if (not isinstance(raw_compile, dict)
                or raw_compile.get("mode", "off") != "off"
                or raw_compile.get("enabled") is True
                or raw_compile.get("autoWrite") is True
                or config.compile_active(cfg.get("compile"))):
            raise ValueError("selected_compile_unsupported")
        # The first product candidate has exactly one explicit indexed source
        # collection. Neither a branch name nor a wildcard mapping is allowed
        # to choose its scope or source directory implicitly.
        names = cfg.get("collections")
        if not isinstance(names, list) or len(names) != 1 or not isinstance(names[0], str):
            raise ValueError("single_collection_required")
        name = names[0]
        if not COLLECTION_NAME.fullmatch(name):
            raise ValueError("unsupported_collection_name")
        if not _selected_settings_supported(raw_settings, cfg, name):
            raise ValueError("selected_recall_settings_unsupported")
        if record["collectionName"] != name or not config.is_indexed_collection(config.role_map(cfg), name):
            raise ValueError("route_collection_mismatch")
        mappings = cfg.get("collectionPaths")
        if not isinstance(mappings, dict) or set(mappings) != {name}:
            raise ValueError("explicit_collection_path_required")
        relative = mappings[name]
        if (not isinstance(relative, str) or not relative or Path(relative).is_absolute()
                or ".." in Path(relative).parts):
            raise ValueError("invalid_collection_path")
        source = root / relative
        if (source.is_symlink() or not source.is_dir()
                or any(parent.is_symlink() for parent in (source, *source.parents) if parent != root and root in parent.parents)):
            raise ValueError("unsafe_collection_source")
        canonical_source = source.resolve(strict=True)
        if root != canonical_source and root not in canonical_source.parents:
            raise ValueError("collection_source_outside_project")
        if record["sourceRoot"] != str(canonical_source):
            raise ValueError("route_source_root_mismatch")
        # The first selected route has no topical compiler/derived-card lane.
        # Host topical hooks still fire, but become proved no-ops only when no
        # project or source topical pilot is present.
        for base in (root, canonical_source):
            for marker in (".qmd-topical-sandbox", ".qmd-topical-v2-project",
                           ".topical-reconcile-hook.json"):
                candidate = base / marker
                if candidate.exists() or candidate.is_symlink():
                    raise ValueError("selected_topical_unsupported")
        if not isinstance(record["scopeId"], str) or not INCARNATION.fullmatch(record["scopeId"]):
            raise ValueError("invalid_scope_id")
        vector_profile = _vector_profile(record["vectorProfile"])
        return _decision("candidate", "route_record_valid", projectRoot=str(root),
                         dbPath=db_path, queuePath=queue_path,
                         dbIncarnation=record["dbIncarnation"],
                         scopeId=record["scopeId"], collectionName=name,
                         sourceRoot=str(canonical_source),
                         pluginRoot=plugin_root, journalPath=journal_path,
                         installId=record["installId"],
                         generation=record["generation"],
                         bundleManifestSha256=record["bundleManifestSha256"],
                         ownershipManifestSha256=record["ownershipManifestSha256"],
                         vectorProfile=vector_profile)
    except (OSError, RuntimeError, ValueError, TypeError, UnicodeError, KeyError) as exc:
        return _decision("blocked", str(exc) or "invalid_route_record")


def select_hook_payload(action: str, body: bytes, fallback_cwd: str) -> dict:
    """Classify bounded event bytes, including legacy malformed-input behavior."""
    try:
        if not isinstance(body, bytes) or len(body) > MAX_HOOK_BYTES:
            raise ValueError("invalid_hook_payload_size")
        try:
            payload = json.loads(body.decode("utf-8"), object_pairs_hook=_pairs)
        except json.JSONDecodeError:
            # Old hooks treat malformed/empty input as a graceful no-op. Only
            # preserve that path when its fallback project has no route marker.
            # A present marker is sticky even without a parseable event cwd.
            fallback = select_route(fallback_cwd, action=action)
            return (fallback if fallback["state"] == "legacy" else
                    _decision("blocked", "unparseable_selected_hook_payload"))
        if not isinstance(payload, dict):
            raise ValueError("invalid_hook_payload")
        cwd = payload.get("cwd", fallback_cwd)
        if not isinstance(cwd, str) or not Path(cwd).is_absolute():
            raise ValueError("invalid_hook_cwd")
        return select_route(cwd, action=action)
    except (OSError, ValueError, TypeError, UnicodeError, RuntimeError) as exc:
        return _decision("blocked", str(exc) or "invalid_hook_payload")


def select_hook_route(action: str, payload_file: str, fallback_cwd: str) -> dict:
    """Resolve a host event's own cwd before its QMD setup/backend work."""
    try:
        body = _read_regular(Path(payload_file), MAX_HOOK_BYTES)
        return select_hook_payload(action, body, fallback_cwd)
    except (OSError, ValueError, TypeError, UnicodeError, RuntimeError) as exc:
        return _decision("blocked", str(exc) or "invalid_hook_payload")


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) == 3 and args[0] == "classify":
        decision = select_route(args[2], action=args[1])
    elif len(args) == 4 and args[0] == "classify-hook":
        decision = select_hook_route(args[1], args[2], args[3])
    else:
        return 2
    print(decision["state"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

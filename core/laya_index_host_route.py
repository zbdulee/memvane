#!/usr/bin/env python3
"""Read-only host boundary for a reviewed memvane-db route candidate.

Absence of the route record preserves the existing QMD path. Presence is
sticky: malformed, stale, or unverifiable records block the hook instead of
falling back to QMD. This module validates only the route record and settings
binding. The product runtime must independently verify the database incarnation
and package identity before serving or writing anything.

The route's ``settingsSha256`` binds the resolved settings identity
(``settings_identity_digest``: project root plus each collection's name, role
and source root, which include the wiki root). Editing ``skipPaths`` keeps that
identity; renaming a collection changes it and blocks. A route that binds any
other digest (such as the settings file bytes) blocks as
``route_settings_changed``. A QMD-origin selection marker blocks as
``qmd_origin_detach_required``.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import shlex
import stat
import sys
import unicodedata

import config
import project_dir

SCHEMA = "laya-index-route-v1"
SCHEMA_V2 = "laya-index-route-v2"
# Same fields as v2. Every route this release writes uses v3: a release before
# the MemVane rename reads only v1/v2, so it classifies a v3 route as blocked
# and can neither serve nor move (downgrade) an install this release owns.
SCHEMA_V3 = "memvane-route-v3"
RECORD = "laya-index-route.json"
QMD_SELECTION_MARKER = "laya-index-selection.json"
MAX_RECORD_BYTES = 8192
MAX_SETTINGS_BYTES = 2 * 1024 * 1024
FIELDS = frozenset({"schema", "status", "projectRoot", "settingsSha256",
                    "dbPath", "queuePath", "dbIncarnation", "scopeId", "collectionName",
                    "sourceRoot", "pluginRoot", "journalPath", "installId",
                    "generation", "bundleManifestSha256",
                    "ownershipManifestSha256", "vectorProfile"})
FIELDS_V2 = (FIELDS - {"collectionName", "sourceRoot"}) | {"collections"}
FIELDS_V3 = FIELDS_V2
COLLECTION_FIELDS = frozenset({"name", "role", "sourceRoot"})
VECTOR_PROFILE_FIELDS = frozenset({"profileId", "modelId", "embeddingFingerprint",
                                   "dimension", "metric", "approvedProviderId",
                                   "providerArtifactSha256", "providerRelativePath"})
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
INCARNATION = re.compile(r"[0-9a-f]{32}\Z")
PACKAGE_GENERATION = re.compile(r"g-[0-9a-f]{32}\Z")
COLLECTION_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
PROVIDER_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}\Z")
# A write action on a project whose `.memvane/` lost its settings to an older
# branch checkout (``project_dir.layout`` ``split``).
PROJECT_DIR_SPLIT = "project_dir_split"
ROUTED_ACTIONS = frozenset({"recall", "update", "update-queued", "gate", "posttool",
                            "index", "compile", "file-change-event"})
MAX_HOOK_BYTES = 2 * 1024 * 1024
SETTINGS_IDENTITY_SCHEMA = "laya-settings-identity-v1"


def written_schema(schema: str) -> str:
    """The schema a rewritten route carries: v2 and v3 become v3, v1 stays."""
    return SCHEMA_V3 if schema in (SCHEMA_V2, SCHEMA_V3) else schema


def settings_identity_digest(project_root, collections) -> str:
    """Digest of the settings identity a selected route binds.

    ``collections`` are the route's ``{name, role, sourceRoot}`` entries as
    resolved from the project settings; their order does not matter.
    """
    rows = sorted(({"name": item["name"], "role": item["role"],
                    "sourceRoot": item["sourceRoot"]} for item in collections),
                  key=lambda item: item["name"])
    canonical = json.dumps({"schema": SETTINGS_IDENTITY_SCHEMA,
                            "projectRoot": str(project_root),
                            "collections": rows},
                           sort_keys=True, ensure_ascii=False,
                           separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def route_collections(record: dict) -> list:
    """The ``{name, role, sourceRoot}`` collections a v1 or v2 route binds."""
    if record.get("schema") == SCHEMA:
        return [{"name": record["collectionName"], "role": "raw",
                 "sourceRoot": record["sourceRoot"]}]
    return record["collections"]


def wiki_index_roots(collections):
    """The selected v2 index contains wiki cards, never standalone raw files.

    Keep the full route's raw root for source-revision and locator checks.
    This mapping alone is passed to the index, catalog, and vector runtime.
    """
    if (not isinstance(collections, list) or len(collections) != 2 or
            any(not isinstance(item, dict) or
                    set(item) != COLLECTION_FIELDS for item in collections)):
        raise ValueError('selected_collections_invalid')
    roles = [item['role'] for item in collections]
    if sorted(roles) != ['raw', 'wiki']:
        raise ValueError('selected_collection_roles_invalid')
    wiki = next(item for item in collections if item['role'] == 'wiki')
    if (not isinstance(wiki['name'], str) or
            not COLLECTION_NAME.fullmatch(wiki['name']) or
            not _canonical_absolute(wiki['sourceRoot'])):
        raise ValueError('selected_wiki_root_invalid')
    return {wiki['name']: wiki['sourceRoot']}


def selected_collection_roots(project_root, collections):
    """Validate both source identities; only the wiki root is indexed.

    The legacy raw project root may contain the plugin's hidden wiki folder.
    The raw scanner skips that hidden folder, while selected memvane-db indexes the
    explicitly named wiki root. Other overlapping roots remain unsafe.
    """
    wiki = wiki_index_roots(collections)
    if len({item['name'] for item in collections}) != len(collections):
        raise ValueError('selected_collection_names_duplicate')
    roots = {}
    for item in collections:
        value = item['sourceRoot']
        if not _canonical_absolute(value) or not (Path(value).is_dir() or
                                                  _split_wiki(Path(project_root), value)):
            raise ValueError('selected_collection_root_invalid')
        roots[item['name']] = value
    raw_root = Path(next(item['sourceRoot'] for item in collections
                         if item['role'] == 'raw'))
    wiki_root = Path(next(iter(wiki.values())))
    if raw_root == wiki_root or raw_root in wiki_root.parents or wiki_root in raw_root.parents:
        project = Path(project_root)
        if (raw_root != project or
                wiki_root not in tuple(project / name / project_dir.WIKI
                                       for name in project_dir.NAMES)):
            raise ValueError('selected_collection_roots_overlap')
    return roots


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


def _multi_settings_supported(raw: dict, cfg: dict, names: tuple[str, ...]) -> bool:
    """Bind legacy QMD settings; selected Laya search is always wiki-only."""
    roles = raw.get('collectionRoles')
    strategy = raw.get('recallStrategy', 'hierarchical')
    skip_paths = raw.get('skipPaths', [])
    if (not isinstance(roles, dict) or set(roles) != set(names)
            or sorted(roles.values()) != ['raw', 'wiki']
            or strategy not in ('hierarchical', 'wikiOnly')
            or cfg.get('recallStrategy') != strategy
            or not isinstance(skip_paths, list)
            or any(not isinstance(path, str) or not path for path in skip_paths)
            or cfg.get('skipPaths') != skip_paths
            or raw.get('allowRoots', []) != [] or cfg.get('allowRoots') != []
            or raw.get('lexicalPatterns', []) not in ([], ['ep'])
            or cfg.get('lexicalPatterns') not in ([], ['ep'])):
        return False
    for key in ('minScore', 'rawFallbackMinScore'):
        value = raw.get(key, 0)
        if (type(value) not in (int, float) or not math.isfinite(value)
                or value != 0 or cfg.get(key) != 0):
            return False
    top_n = raw.get('topN', 3)
    return type(top_n) is int and 1 <= top_n <= 5 and cfg.get('topN') == top_n


def _multi_compile_supported(raw: dict, cfg: dict, root: Path,
                             wiki_root: str) -> bool:
    raw_compile = raw.get('compile', {})
    compile_cfg = cfg.get('compile')
    if (not isinstance(raw_compile, dict) or not isinstance(compile_cfg, dict)
            or 'enabled' in raw_compile or 'autoWrite' in raw_compile):
        return False
    mode = raw_compile.get('mode', 'off')
    if mode == 'off':
        return not config.compile_active(compile_cfg)
    if mode != 'auto-wiki' or compile_cfg.get('mode') != 'auto-wiki':
        return False
    # session_start_source is its own explicit consent; it is never widened
    # from post_tool_source/post_sync_source.
    active = {'post_tool_source', 'post_sync_source', 'session_start_source'}
    triggers = raw_compile.get('triggers')
    if (not isinstance(triggers, list) or triggers != compile_cfg.get('triggers')
            or not set(triggers) & active
            or any(trigger not in active | {'manual'} for trigger in triggers)
            or compile_cfg.get('recallVerifiedOnly') is not True
            or compile_cfg.get('verify', {}).get('enabled') is not True):
        return False
    extractor = compile_cfg.get('extractor', {})
    if (not isinstance(extractor, dict) or
            not (extractor.get('builtins') or extractor.get('backends'))):
        return False
    wiki_path = cfg.get('wikiPath')
    if not isinstance(wiki_path, str) or not wiki_path:
        return False
    try:
        return _source_for_collection(root, wiki_path) == wiki_root
    except (OSError, RuntimeError, ValueError):
        return False


def _split_wiki(root: Path, value: object) -> bool:
    """``<root>/.memvane/wiki`` gone under an older branch checkout (``split``).

    The selected identity still names it: search keeps the published index and
    the edit gate keeps working while every writer holds (``project_dir_split``).
    """
    folder = Path(root) / project_dir.CURRENT
    return (value in (project_dir.CURRENT + "/" + project_dir.WIKI,
                      str(folder / project_dir.WIKI)) and
            not os.path.lexists(folder / project_dir.WIKI) and
            project_dir._real_dir(folder) and project_dir.layout(root) == "split")


def _source_for_collection(root: Path, relative: object) -> str:
    if (not isinstance(relative, str) or not relative or Path(relative).is_absolute()
            or ".." in Path(relative).parts):
        raise ValueError("invalid_collection_path")
    if _split_wiki(root, relative):
        return str(root / relative)
    source = root / relative
    if (source.is_symlink() or not source.is_dir()
            or any(parent.is_symlink() for parent in (source, *source.parents)
                   if parent != root and root in parent.parents)):
        raise ValueError("unsafe_collection_source")
    canonical = source.resolve(strict=True)
    if root != canonical and root not in canonical.parents:
        raise ValueError("collection_source_outside_project")
    return str(canonical)


def _derived_collections(record: dict, root: Path, settings_bytes: bytes) -> list:
    """The ``{name, role, sourceRoot}`` set the settings file resolves to."""
    raw = json.loads(settings_bytes.decode("utf-8"), object_pairs_hook=_pairs)
    if not isinstance(raw, dict):
        raise ValueError("invalid_route_settings")
    folder = project_dir.active_name(root)
    cfg = config.normalize_config(config.expand_minimal_settings(raw, folder), folder)
    names, mappings = cfg.get("collections"), cfg.get("collectionPaths")
    if (not isinstance(names, list) or not isinstance(mappings, dict) or
            any(not isinstance(name, str) for name in names) or
            set(mappings) != set(names)):
        raise ValueError("explicit_collection_path_required")
    roles = config.role_map(cfg)
    return [{"name": name,
             "role": "raw" if record["schema"] == SCHEMA else roles.get(name),
             "sourceRoot": _source_for_collection(root, mappings[name])}
            for name in sorted(names)]


def _candidate_paths(cwd: Path):
    # Search all ancestors for a *blocking* marker. Ordinary QMD config search
    # stops at HOME (or cwd outside HOME), but a nested cwd outside HOME must
    # not miss an already-selected route at its project root and enter QMD.
    # Candidate activation still requires config's exact project-root binding.
    # The record is read from `.memvane/` first, then the `.auto-context/`
    # fallback of a project not moved yet (``project_dir.runtime_file``).
    for directory in (cwd, *cwd.parents):
        path = project_dir.runtime_file(directory, RECORD)
        try:
            path.lstat()
        except (FileNotFoundError, NotADirectoryError):
            continue
        yield directory, path.parent, path


def _classify_record(root: Path, auto: Path, path: Path, current: Path) -> dict:
    """Apply the settings and route checks to the visible route record."""
    try:
        if auto.is_symlink() or path.is_symlink() or not auto.is_dir():
            raise ValueError("unsafe_route_path")
        found = config.find_project_config(str(current))
        settings = project_dir.settings_path(root)
        if (found.get("configFormat") != "auto-context-dir"
                or found.get("projectRoot") != str(root) or settings is None
                or found.get("configPath") != str(settings)):
            raise ValueError("route_settings_not_selected")
        if settings.is_symlink():
            raise ValueError("unsafe_route_settings")
        raw = _read_regular(path, MAX_RECORD_BYTES)
        record = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs)
        if (not isinstance(record, dict) or
                (record.get('schema'), set(record)) not in
                ((SCHEMA, FIELDS), (SCHEMA_V2, FIELDS_V2), (SCHEMA_V3, FIELDS_V3))):
            raise ValueError("invalid_route_fields")
        if record["status"] != "active":
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
        if not _canonical_absolute(journal_path):
            raise ValueError("invalid_journal_path")
        if record['schema'] == SCHEMA:
            if journal_path != str(Path(plugin_root).parent / "install-journal.sqlite"):
                raise ValueError("invalid_journal_path")
        else:
            private = Path(journal_path).parent
            info = private.lstat()
            if (Path(journal_path).name != 'install-journal.sqlite' or
                    private.is_symlink() or not private.is_dir() or
                    info.st_uid != os.getuid() or
                    stat.S_IMODE(info.st_mode) & 0o077):
                raise ValueError('unsafe_private_journal_root')
        db_path = record["dbPath"]
        queue_path = record["queuePath"]
        if not _canonical_absolute(db_path) or Path(db_path) == root:
            raise ValueError("invalid_db_path")
        if not _canonical_absolute(queue_path) or queue_path == db_path:
            raise ValueError("invalid_queue_path")
        settings_bytes = _read_regular(settings, MAX_SETTINGS_BYTES)
        try:
            derived = _derived_collections(record, root, settings_bytes)
        except (ValueError, TypeError, KeyError, UnicodeError):
            derived = None
        # Settings that no longer resolve to the bound identity are a
        # settings change, whatever would have failed first.
        if (derived is None or record["settingsSha256"] !=
                settings_identity_digest(root, derived)):
            raise ValueError("route_settings_changed")
        raw_settings = json.loads(settings_bytes.decode("utf-8"), object_pairs_hook=_pairs)
        if not isinstance(raw_settings, dict):
            raise ValueError("invalid_route_settings")
        # A minimal {name, skipPaths} file is checked as its derived legacy
        # shape; any other file is checked exactly as written.
        folder = project_dir.active_name(root)
        raw_settings = config.expand_minimal_settings(raw_settings, folder)
        cfg = config.normalize_config(raw_settings, folder)
        if cfg.get("indexing") is not True:
            raise ValueError("route_indexing_not_selected")
        raw_compile = raw_settings.get("compile", {})
        if record['schema'] == SCHEMA and (not isinstance(raw_compile, dict)
                or raw_compile.get("mode", "off") != "off"
                or raw_compile.get("enabled") is True
                or raw_compile.get("autoWrite") is True
                or config.compile_active(cfg.get("compile"))):
            raise ValueError("selected_compile_unsupported")
        names = cfg.get("collections")
        if (not isinstance(names, list) or not names or
                any(not isinstance(name, str) or not COLLECTION_NAME.fullmatch(name)
                    for name in names) or len(set(names)) != len(names)):
            raise ValueError("unsupported_collection_names")
        mappings = cfg.get("collectionPaths")
        if not isinstance(mappings, dict) or set(mappings) != set(names):
            raise ValueError("explicit_collection_path_required")
        selected_collections = []
        if record['schema'] == SCHEMA:
            if len(names) != 1:
                raise ValueError("single_collection_required")
            name = names[0]
            if not _selected_settings_supported(raw_settings, cfg, name):
                raise ValueError("selected_recall_settings_unsupported")
            if (record["collectionName"] != name or
                    not config.is_indexed_collection(config.role_map(cfg), name)):
                raise ValueError("route_collection_mismatch")
            canonical_source = _source_for_collection(root, mappings[name])
            if record["sourceRoot"] != canonical_source:
                raise ValueError("route_source_root_mismatch")
            selected_collections = [{'name': name, 'role': 'raw',
                                     'sourceRoot': canonical_source}]
        else:
            ordered = tuple(sorted(names))
            if (len(ordered) != 2 or
                    not _multi_settings_supported(raw_settings, cfg, ordered)):
                raise ValueError('selected_recall_settings_unsupported')
            declared = record['collections']
            if (not isinstance(declared, list) or len(declared) != 2 or
                    any(not isinstance(item, dict) or set(item) != COLLECTION_FIELDS
                        for item in declared)):
                raise ValueError('route_collections_invalid')
            roles = config.role_map(cfg)
            for name, item in zip(ordered, declared):
                source = _source_for_collection(root, mappings[name])
                if (item != {'name': name, 'role': roles.get(name),
                             'sourceRoot': source} or
                        item['role'] not in ('raw', 'wiki')):
                    raise ValueError('route_collection_mismatch')
                selected_collections.append(item)
            selected_collection_roots(root, selected_collections)
            wiki_root = next(item['sourceRoot'] for item in selected_collections
                             if item['role'] == 'wiki')
            if not _multi_compile_supported(raw_settings, cfg, root, wiki_root):
                raise ValueError('selected_compile_unsupported')
        if not isinstance(record["scopeId"], str) or not INCARNATION.fullmatch(record["scopeId"]):
            raise ValueError("invalid_scope_id")
        vector_profile = _vector_profile(record["vectorProfile"])
        details = dict(projectRoot=str(root),
                         dbPath=db_path, queuePath=queue_path,
                         dbIncarnation=record["dbIncarnation"],
                         scopeId=record["scopeId"],
                         pluginRoot=plugin_root, journalPath=journal_path,
                         installId=record["installId"],
                         generation=record["generation"],
                         bundleManifestSha256=record["bundleManifestSha256"],
                         ownershipManifestSha256=record["ownershipManifestSha256"],
                         vectorProfile=vector_profile)
        if record['schema'] == SCHEMA:
            details.update(collectionName=name, sourceRoot=canonical_source)
        else:
            details.update(
                collections=selected_collections,
                collectionRoots={item['name']: item['sourceRoot']
                                 for item in selected_collections},
                recallSettings={key: cfg[key] for key in
                                ('recallStrategy', 'minScore', 'rawFallbackMinScore',
                                 'topN', 'skipPaths', 'lexicalPatterns')})
        return _decision("candidate", "route_record_valid", **details)
    except (OSError, RuntimeError, ValueError, TypeError, UnicodeError, KeyError) as exc:
        return _decision("blocked", str(exc) or "invalid_route_record")



def _preview_route(current: Path, requested: Path, action: str) -> dict | None:
    """Recognize only an installer-owned, inactive fresh-wiki build."""
    if action != 'recall':
        return None
    try:
        found = config.find_project_config(str(current))
        if found.get('configFormat') != 'auto-context-dir':
            return None
        root = Path(found['projectRoot'])
        if (not requested.is_dir() or not root.is_absolute()
                or str(root.resolve(strict=True)) != str(root)
                or (current != root and root not in current.parents)
                or found.get('configPath') != str(project_dir.settings_path(root))):
            return _decision('blocked', 'initial_preview_project_mismatch')
        import laya_initial_build
        build = laya_initial_build.lookup(str(root))
        if build is None:
            return None
        identity = build['identity']
        if build['phase'] not in ('registered', 'building', 'sealed'):
            return _decision('blocked', 'initial_preview_build_unavailable')
        state = Path(identity['stateRoot'])
        route_path = state / 'reviewed-route.json'
        if (identity['routeFile'] != str(route_path)
                or identity['journalPath'] != str(state / 'install-journal.sqlite')
                or identity['projectRoot'] != str(root)):
            return _decision('blocked', 'initial_preview_identity_mismatch')
        raw = _read_regular(route_path, MAX_RECORD_BYTES)
        if hashlib.sha256(raw).hexdigest() != identity['routeSha256']:
            return _decision('blocked', 'initial_preview_route_changed')
        candidate = _classify_record(root, project_dir.folder(root), route_path, current)
        if candidate['state'] != 'candidate':
            return candidate
        expected = {'projectRoot': identity['projectRoot'],
                    'dbPath': identity['candidateDb'], 'queuePath': identity['queueDb'],
                    'dbIncarnation': identity['dbIncarnation'],
                    'scopeId': identity['scopeId'], 'installId': identity['installId'],
                    'journalPath': identity['journalPath'],
                    'collections': identity['collections']}
        if (any(candidate.get(key) != value for key, value in expected.items())
                or candidate['vectorProfile']['profileId'] != identity['vectorProfileId']):
            return _decision('blocked', 'initial_preview_route_mismatch')
        return {**candidate, 'state': 'preview', 'reason': 'initial_build_preview',
                'previewStateRoot': str(state),
                'previewRouteSha256': identity['routeSha256']}
    except (OSError, RuntimeError, ValueError, TypeError, UnicodeError, KeyError):
        return _decision('blocked', 'initial_preview_proof_unavailable')


def verify_preview_route(decision: dict) -> dict:
    """Reprove a pending build and the currently executing sealed bundle."""
    if not isinstance(decision, dict) or decision.get('state') != 'preview':
        raise ValueError('initial_preview_unselected')
    live = select_route(decision.get('projectRoot', ''), action='recall')
    if live != decision:
        raise ValueError('initial_preview_changed')
    import laya_initial_build
    build = laya_initial_build.lookup(decision['projectRoot'])
    if (build is None or build['identity']['stateRoot'] != decision['previewStateRoot']
            or build['phase'] not in ('registered', 'building', 'sealed')):
        raise ValueError('initial_preview_build_changed')
    from laya_install_journal import inspect_journal, preflight_product_candidate
    journal = inspect_journal(decision['journalPath'])
    proof = preflight_product_candidate(decision['journalPath'],
                                        require_executing=True)
    pending = journal['pending']
    if (journal['state'] != 'candidate_ready' or journal['selected'] is not None
            or journal['newWriteSeen'] is not False
            or Path(decision['journalPath']).parent != Path(decision['previewStateRoot'])
            or journal['pendingRouteSha256'] != decision['previewRouteSha256']
            or proof['pendingRouteSha256'] != decision['previewRouteSha256']
            or journal['scopeId'] != decision['scopeId']
            or journal['projectRoot'] != decision['projectRoot']
            or not isinstance(pending, dict)
            or any(pending.get(key) != decision.get(key) for key in
                   ('pluginRoot', 'installId', 'generation',
                    'bundleManifestSha256', 'ownershipManifestSha256'))
            or pending.get('vectorProfile') != decision['vectorProfile']):
        raise ValueError('initial_preview_journal_mismatch')
    return {'journalEpoch': journal['epoch'], 'buildPhase': build['phase'], **proof,
            'vectorProfile': decision['vectorProfile']}


def select_route(cwd: str, *, action: str = "recall") -> dict:
    """Classify a hook without creating a directory, database, or runtime.

    Candidate means only that the route record and source settings agree. It
    does not mean that an active product service is ready. The caller must use
    the runtime's own proof check; its failure may never select legacy QMD.

    A cwd inside a linked git worktree of a project whose main checkout has a
    verified selected route classifies as ``worktree`` (see
    ``_worktree_route``). The host serves the main checkout's published
    generation read-only; every other caller treats it like ``blocked``.
    """
    decision = _select_route(cwd, action=action)
    if decision.get("state") in ("blocked", "legacy") and action in ROUTED_ACTIONS:
        worktree = _worktree_route(cwd, action)
        if worktree is not None:
            return worktree
    return decision


def _settings_identity(project: Path) -> tuple:
    """The derived project identity a worktree must share with its main checkout.

    The folder name is normalized: a worktree on a branch that still has
    ``.auto-context/`` and a main checkout on ``.memvane/`` (or the reverse)
    derive the same identity.
    """
    settings = project_dir.settings_path(project)
    if settings is None:
        raise ValueError("route_settings_not_selected")
    raw = json.loads(_read_regular(settings, MAX_SETTINGS_BYTES).decode("utf-8"),
                     object_pairs_hook=_pairs)
    if not isinstance(raw, dict):
        raise ValueError("invalid_route_settings")
    folder = project_dir.CURRENT
    cfg = config.normalize_config(config.expand_minimal_settings(raw, folder), folder)
    paths = cfg.get("collectionPaths")
    return (cfg.get("name"), cfg.get("indexing"), tuple(cfg.get("collections") or ()),
            tuple(sorted(paths.items())) if isinstance(paths, dict) else None,
            tuple(sorted(config.role_map(cfg).items())), cfg.get("wikiPath"))


def _worktree_route(cwd: str, action: str) -> dict | None:
    """Read-only route of a linked git worktree onto its main checkout.

    Route records and selection markers are untracked runtime files, so a
    worktree has the tracked settings and wiki but no route of its own. The
    worktree is served from the main checkout only when all of these hold:
    git reports a linked worktree whose common git dir is the main checkout's
    git dir; no route record or selection marker exists between the cwd and the
    worktree root; the worktree project sits at the same relative path as the
    main project; the main project classifies exactly ``candidate``; and both
    settings derive the same name, collections, paths, roles and wiki path.
    Otherwise None, and the caller keeps today's classification.
    """
    try:
        requested = Path(cwd)
        if not requested.is_absolute():
            return None
        current = requested.resolve(strict=False)
        if not current.is_dir():
            return None
        import git_branch_safety
        linked = git_branch_safety.linked_worktree(current)
        if linked is None:
            return None
        worktree, main = Path(linked["worktreeRoot"]), Path(linked["mainRoot"])
        if current != worktree and worktree not in current.parents:
            return None
        for directory in (current, *current.parents):
            for name in (RECORD, QMD_SELECTION_MARKER):
                if any(os.path.lexists(directory / folder / name)
                       for folder in project_dir.NAMES):
                    return None
            if directory == worktree:
                break
        found = config.find_project_config(str(current))
        if found.get("configFormat") != "auto-context-dir":
            return None
        project = Path(found["projectRoot"])
        relative = project.relative_to(worktree)
        main_project = main / relative
        if main_project.resolve(strict=True) != main_project:
            return None
        main_decision = _select_route(str(main_project), action=action)
        if main_decision.get("state") == "blocked":
            # The main checkout's state or project folder may be moving: the
            # worktree waits the same window as the main checkout's own hooks.
            main_decision = _settle_moves(str(main_project), action, main_decision)
        if (main_decision.get("state") != "candidate"
                or main_decision.get("projectRoot") != str(main_project)):
            return None
        if _settings_identity(project) != _settings_identity(main_project):
            return None
    except (OSError, RuntimeError, ValueError, TypeError, UnicodeError, KeyError):
        return None
    return {"state": "worktree", "reason": "linked_worktree_read_only",
            "worktreeRoot": str(worktree), "worktreeProjectRoot": str(project),
            "gitCommonDir": linked["commonDir"], "mainRoute": main_decision}


def _select_route(cwd: str, *, action: str = "recall") -> dict:
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
            marker = project_dir.runtime_file(directory, QMD_SELECTION_MARKER)
            if marker.exists() or marker.is_symlink():
                markers.append((directory, marker))
    except (OSError, RuntimeError, ValueError):
        return _decision("blocked", "route_lookup_failed")
    if len(markers) > 1:
        return _decision("blocked", "ambiguous_selection_markers")
    if markers:
        # The marker stays through restoration. Only its durable ledger can
        # classify an absent route as legacy.
        from laya_ledger_common import (QMD_MARKER_SCHEMA,
                                        QMD_ORIGIN_DETACH_REQUIRED, _read_json)
        from laya_fresh_wiki_origin import (MARKER_SCHEMA as FRESH_MARKER_SCHEMA,
                                            classify_marker as classify_fresh)
        marker_root, marker_path = markers[0]
        try:
            marker_record = _read_json(marker_path)
            schema = marker_record.get('schema') if isinstance(marker_record, dict) else None
        except (OSError, RuntimeError, ValueError, TypeError):
            schema = None
        if schema == QMD_MARKER_SCHEMA:
            return _decision("blocked", QMD_ORIGIN_DETACH_REQUIRED)
        selection = (classify_fresh(marker_root, marker_path, marker_path.parent / RECORD)
                     if schema == FRESH_MARKER_SCHEMA else 'blocked')
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
        preview = _preview_route(current, requested, action)
        return preview if preview is not None else _decision("legacy", "no_route_record")
    if not requested.is_dir():
        return _decision("blocked", "route_cwd_unavailable")
    # A nested/stale route must not be ignored just because an ancestor has
    # ordinary QMD settings or a second route record.
    if len(records) != 1:
        return _decision("blocked", "ambiguous_route_records")
    root, auto, path = records[0]
    if action not in ("recall", "gate") and project_dir.layout(root) == "split":
        # An older branch is checked out over a moved project: its
        # `.auto-context/` is not indexed and a missing or partial
        # `.memvane/wiki` is never published as a deletion. Search keeps the
        # published index and the edit gate keeps allowing edits; every index
        # and wiki writer holds.
        return _decision("blocked", PROJECT_DIR_SPLIT)
    return _classify_record(root, auto, path, current)


def select_route_settled(cwd: str, *, action: str = "recall") -> dict:
    """``select_route``, waiting briefly while the install's state moves.

    Recall and SessionStart only: a route that is blocked while its project
    names a fresh state move is re-read for up to ``laya_state_move.
    WAIT_SECONDS`` (the window between the rename and the route write). Other
    actions, and a blocked route without a move, return at once.
    """
    decision = select_route(cwd, action=action)
    if decision.get("state") != "blocked":
        return decision
    return _settle_moves(cwd, action, decision)


def _settle_moves(cwd: str, action: str, decision: dict) -> dict:
    """Wait out a state-root move, a project folder move (``.memvane/``) or,
    for a recall, a bundle upgrade's fence."""
    for name in ("laya_state_move", "laya_project_dir_move", "laya_fresh_wiki_origin"):
        if decision.get("state") != "blocked":
            return decision
        try:
            module = __import__(name)
            decision = module.settle_decision(cwd, action, decision, select_route)
        except Exception:
            pass
    return decision


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
        return select_route_settled(cwd, action=action)
    except (OSError, ValueError, TypeError, UnicodeError, RuntimeError) as exc:
        return _decision("blocked", str(exc) or "invalid_hook_payload")


def select_hook_route(action: str, payload_file: str, fallback_cwd: str) -> dict:
    """Resolve a host event's own cwd before any hook work."""
    try:
        body = _read_regular(Path(payload_file), MAX_HOOK_BYTES)
        return select_hook_payload(action, body, fallback_cwd)
    except (OSError, ValueError, TypeError, UnicodeError, RuntimeError) as exc:
        return _decision("blocked", str(exc) or "invalid_hook_payload")


# Onboarding notices stay in Korean like the setup wrappers' notices. The
# first line is for the user and names no path; the second carries the exact
# answer commands as JSON so any path survives (quotes, backticks, newlines).
ONBOARDING_NOTICE = ('[memvane] 이 프로젝트는 아직 MemVane 온보딩 전입니다. setup skill로 '
                     '온보딩하거나, 이번 세션만 건너뛰기·다시 묻지 않기를 원하면 말해 주세요.')
ONBOARDING_NOTICE_NO_SESSION = ('[memvane] 이 프로젝트는 아직 MemVane 온보딩 전입니다. setup '
                                'skill로 온보딩하거나, 다시 묻지 않기를 원하면 말해 주세요.')
ONBOARDING_ANSWER_PREFIX = '[memvane] onboarding-answer '
BUILD_NOTICE = ('[memvane] {root}: 초기 MemVane 색인을 준비 중입니다. '
                '빌드가 끝나면 setup skill로 활성화하세요.')


def onboarding_notice(payload_file: str, fallback_cwd: str) -> str | None:
    """One SessionStart line for a project with settings but no selected route.

    The caller has already classified the event ``legacy``. A directory
    without ``.memvane/settings.json`` (or the ``.auto-context/`` fallback)
    is not opted in and stays silent.
    """
    cwd = fallback_cwd
    session = None
    try:
        payload = json.loads(_read_regular(Path(payload_file), MAX_HOOK_BYTES).decode("utf-8"))
        if isinstance(payload, dict) and isinstance(payload.get("cwd"), str):
            cwd = payload["cwd"]
        if isinstance(payload, dict):
            session = payload.get("session_id") or payload.get("sessionId")
    except (OSError, ValueError, TypeError, UnicodeError):
        pass
    if not Path(cwd).is_absolute():
        return None
    found = config.find_project_config(cwd)
    if found.get("configFormat") != "auto-context-dir":
        return None
    root = found.get("projectRoot") or cwd
    try:
        from laya_initial_build import lookup
        building = lookup(Path(root)) is not None
    except Exception:
        building = False
    if building:
        # A host plugin update (or an in-place re-extraction of the bound
        # bundle) moves the inactive or bound build in a detached worker.
        try:
            import laya_bundle_upgrade
            laya_bundle_upgrade.request_for_project(root)
        except Exception:
            pass
        # A build the user started is progress, not a question.
        return BUILD_NOTICE.format(root=root)
    try:
        import onboarding_choice
        session = onboarding_choice.valid_session(session)
        if onboarding_choice.suppressed(root, session):
            return None
    except Exception:
        # A fault in the recorded answer shows the notice, never hides it.
        session = None
    return onboarding_lines(str(root), session)


def onboarding_lines(root: str, session: str | None) -> str:
    """The user notice line plus the agent's answer commands as one JSON line."""
    entry = str(Path(__file__).resolve().parent / "setup_entry.py")
    base = ["python3", "-B", entry]

    def command(*args):
        argv = [*base, *args]
        return {"argv": argv, "shell": shlex.join(argv)}

    answer = {"projectRoot": root,
              "use": "run the matching command only when the user declines onboarding"}
    if session:
        answer["skip"] = command("onboarding-skip", "--project", root, "--session", session)
    answer["never"] = command("onboarding-never", "--project", root)
    notice = ONBOARDING_NOTICE if session else ONBOARDING_NOTICE_NO_SESSION
    return notice + "\n" + ONBOARDING_ANSWER_PREFIX + json.dumps(answer, ensure_ascii=False)


UPGRADE_NOTICE = ('[memvane] plugin bundle upgrade in progress; search resumes '
                  'when it finishes.')
UPGRADE_STALLED_NOTICE = ('[memvane] plugin bundle upgrade stopped mid-way; a background '
                          'run is asked to finish it. Until then prompts get no '
                          'MemVane context.')


def project_dir_notice(payload_file: str, fallback_cwd: str) -> str | None:
    """One SessionStart line for a blocked project whose folders need attention.

    ``split`` (an older branch checked out over ``.memvane/``) and
    ``conflict`` name what the owner does; ``adopt`` (git brought the
    renamed folder, the runtime files are still in ``.auto-context/``) and a
    move in progress request the upgrade worker. Never waits.
    """
    cwd = fallback_cwd
    try:
        payload = json.loads(_read_regular(Path(payload_file), MAX_HOOK_BYTES).decode("utf-8"))
        if isinstance(payload, dict) and isinstance(payload.get("cwd"), str):
            cwd = payload["cwd"]
    except (OSError, ValueError, TypeError, UnicodeError):
        pass
    return project_dir_notice_for(cwd)


def project_dir_notice_for(cwd: str) -> str | None:
    """``project_dir_notice`` for an event cwd (Hermes calls it directly)."""
    if not isinstance(cwd, str) or not Path(cwd).is_absolute():
        return None
    try:
        import laya_fresh_wiki_origin as fresh
        if fresh.upgrade_in_progress(cwd) or fresh.upgrade_stalled(cwd):
            # A fenced upgrade (live or stopped) asks the worker to finish it.
            fresh.request_recovery(cwd)
            return (UPGRADE_STALLED_NOTICE if fresh.upgrade_stalled(cwd)
                    else UPGRADE_NOTICE)
    except Exception:
        pass
    try:
        import laya_project_dir_move as dir_move
        found = config.find_project_config(cwd)
        root = Path(found.get("projectRoot") or cwd)
        line = dir_move.layout_notice(root)
        if line is not None:
            return line
        # Only a move blocks the route here; any other block stays a refusal.
        if (project_dir.layout(root) != "adopt" and
                dir_move.read_pointer(root) is None):
            return None
        record = project_dir.runtime_file(root, RECORD)
        if not record.is_file():
            return None
        route = json.loads(_read_regular(record, MAX_RECORD_BYTES).decode("utf-8"))
        import laya_bundle_upgrade
        return laya_bundle_upgrade.project_dir_notice(str(root), route["journalPath"])
    except Exception:
        return None


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) == 3 and args[0] == "classify":
        decision = select_route_settled(args[2], action=args[1])
    elif len(args) == 4 and args[0] == "classify-hook":
        decision = select_hook_route(args[1], args[2], args[3])
    elif len(args) == 3 and args[0] in ("onboarding-notice", "project-dir-notice"):
        notice = (onboarding_notice if args[0] == "onboarding-notice"
                  else project_dir_notice)(args[1], args[2])
        if notice:
            print(notice)
        return 0
    else:
        return 2
    if (args[0] == "classify-hook" and args[1] == "recall" and
            decision["state"] == "blocked" and
            decision.get("reason") in ("bundle_upgrade_in_progress",
                                       "bundle_upgrade_stalled")):
        # The dispatcher answers this recall without context, not an error.
        print("upgrading")
        return 0
    print(decision["state"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

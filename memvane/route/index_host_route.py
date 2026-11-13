#!/usr/bin/env python3
"""Validate route record fields, settings identity, and collection roots.

The journal classifies and selects routes. The product runtime independently
verifies the selected database and provider identity before serving or writing.

The route's ``settingsSha256`` binds the resolved settings identity
(``settings_identity_digest``: project root plus each collection's name, role
and source root, which include the wiki root). Editing ``skipPaths`` keeps that
identity; renaming a collection changes it and blocks. A route that binds any
other digest (such as the settings file bytes) blocks as
``route_settings_changed``.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import unicodedata

from memvane.settings import api as config
from memvane.settings import api as project_dir, types as project_types
from memvane.route.types import (FIELDS, FIELDS_V2, FIELDS_V3, FIELDS_V4,
                                 MAX_RECORD_BYTES, MAX_SETTINGS_BYTES,
                                 QMD_SELECTION_MARKER, RECORD, SCHEMA, SCHEMA_V2,
                                 SCHEMA_V3, SCHEMA_V4, SELECTION_SCHEMA_V1,
                                 SETTINGS_IDENTITY_SCHEMA, SHA256, INCARNATION, COLLECTION_FIELDS, COLLECTION_NAME)
VECTOR_PROFILE_FIELDS = frozenset({"profileId", "modelId", "embeddingFingerprint",
                                   "dimension", "metric", "approvedProviderId",
                                   "providerArtifactSha256", "providerRelativePath"})
PROVIDER_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}\Z")


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
        if not _canonical_absolute(value) or not Path(value).is_dir():
            raise ValueError('selected_collection_root_invalid')
        roots[item['name']] = value
    raw_root = Path(next(item['sourceRoot'] for item in collections
                         if item['role'] == 'raw'))
    wiki_root = Path(next(iter(wiki.values())))
    if raw_root == wiki_root or raw_root in wiki_root.parents or wiki_root in raw_root.parents:
        project = Path(project_root)
        if (raw_root != project or
                wiki_root not in tuple(project / name / project_types.WIKI
                                       for name in project_types.NAMES)):
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
            or not isinstance(relative, str) or not relative.startswith("memvane/provider/adapters/")
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




def _source_for_collection(root: Path, relative: object) -> str:
    if (not isinstance(relative, str) or not relative or Path(relative).is_absolute()
            or ".." in Path(relative).parts):
        raise ValueError("invalid_collection_path")
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
    if not config.is_minimal_settings(raw):
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


def _classify_v4(root: Path, settings: Path, record: dict, *, inspect_store=True) -> dict:
    """Classify a local single-store route without granting runtime authority."""
    from memvane.kernel import memvane_paths
    import sqlite3
    from contextlib import closing
    from memvane.kernel.memvane_version import same_major

    if (set(record) != FIELDS_V4 or record['status'] != 'active'
            or type(record['installId']) is not str
            or not INCARNATION.fullmatch(record['installId'])
            or type(record['scopeId']) is not str
            or not INCARNATION.fullmatch(record['scopeId'])
            or type(record['storeIncarnation']) is not str
            or not INCARNATION.fullmatch(record['storeIncarnation'])
            or type(record['stateFormat']) is not int or record['stateFormat'] < 1):
        raise ValueError('invalid_route_fields')
    state = memvane_paths.install_root(record['installId'])
    store = state / 'store.sqlite'
    if record['stateRoot'] != str(state) or record['storePath'] != str(store):
        return _decision('legacy', 'foreign_install')
    observed = {}
    for path, field, directory in ((state, 'stateNode', True), (store, 'storeNode', False)):
        node = record[field]
        if (not isinstance(node, list) or len(node) != 2
                or any(type(n) is not int or n < 0 for n in node)):
            raise ValueError('invalid_route_node')
        try:
            info = path.lstat()
        except FileNotFoundError:
            return _decision('legacy', 'foreign_install')
        if (stat.S_ISLNK(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077
                or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
                or (not directory and info.st_nlink != 1)):
            raise ValueError('unsafe_route_store')
        if node[1] != info.st_ino:
            return _decision('legacy', 'foreign_install')
        if path.resolve(strict=True) != path:
            raise ValueError('unsafe_route_store')
        observed[field] = (info.st_dev, info.st_ino)
    identity = None
    matching_major = same_major(record['stateFormat'])
    if matching_major and inspect_store:
        try:
            with closing(sqlite3.connect(store.as_uri() + '?mode=ro', uri=True, timeout=2)) as db:
                db.execute('PRAGMA query_only=ON')
                identity = db.execute('SELECT incarnation,scope_id FROM store_meta WHERE singleton=1').fetchone()
        except sqlite3.Error as exc:
            raise ValueError('route_store_unavailable') from exc
    for path, field in ((state, 'stateNode'), (store, 'storeNode')):
        info = path.lstat()
        if (info.st_dev, info.st_ino) != observed[field]:
            raise ValueError('route_store_changed')
    if matching_major and inspect_store and identity != (record['storeIncarnation'], record['scopeId']):
        return _decision('legacy', 'foreign_install')
    if record['projectRoot'] != str(root):
        raise ValueError('route_project_mismatch')
    settings_bytes = _read_regular(settings, MAX_SETTINGS_BYTES)
    derived = _derived_collections(record, root, settings_bytes)
    if (record['collections'] != derived or record['settingsSha256'] !=
            settings_identity_digest(root, derived)):
        raise ValueError('route_settings_changed')
    selected_collection_roots(root, derived)
    raw = json.loads(settings_bytes.decode('utf-8'), object_pairs_hook=_pairs)
    folder = project_dir.active_name(root)
    cfg = config.normalize_config(config.expand_minimal_settings(raw, folder), folder)
    vector_profile = _vector_profile(record['vectorProfile'])
    return _decision('candidate', 'route_record_valid', routeSchema=SCHEMA_V4, projectRoot=str(root),
                     stateRoot=str(state), storePath=str(store),
                     storeIncarnation=record['storeIncarnation'],
                     stateFormat=record['stateFormat'], scopeId=record['scopeId'],
                     installId=record['installId'], journalPath=str(state / 'journal.sqlite'),
                     collections=derived, vectorProfile=vector_profile,
                     collectionRoots={item['name']: item['sourceRoot'] for item in derived},
                     recallSettings={key: cfg[key] for key in
                                     ('recallStrategy', 'minScore', 'rawFallbackMinScore',
                                      'topN', 'skipPaths', 'lexicalPatterns')})

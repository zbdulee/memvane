"""Journal-owned read-only route selection for host and worker requests."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat

from memvane.settings import config
from memvane.settings import api as project_dir, types as project_types
from memvane.route.api import (canonical_absolute as _canonical_absolute,
    candidate_paths as _candidate_paths, classify_v4 as _classify_v4,
    decision as _decision, derived_collections as _derived_collections,
    multi_compile_supported as _multi_compile_supported,
    multi_settings_supported as _multi_settings_supported, pairs as _pairs,
    read_regular as _read_regular, selected_settings_supported as _selected_settings_supported,
    source_for_collection as _source_for_collection, vector_profile as _vector_profile,
    selected_collection_roots, settings_identity_digest)
from memvane.route.types import (COLLECTION_FIELDS, COLLECTION_NAME, FIELDS, FIELDS_V2, FIELDS_V3, INCARNATION,
    MAX_RECORD_BYTES, MAX_SETTINGS_BYTES, PACKAGE_GENERATION, QMD_SELECTION_MARKER,
    RECORD, ROUTED_ACTIONS, SCHEMA, SCHEMA_V2, SCHEMA_V3, SCHEMA_V4,
    SELECTION_SCHEMA_V1, SHA256)


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
        if isinstance(record, dict) and record.get('schema') == SCHEMA_V4:
            return _classify_v4(root, settings, record)
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
            if journal_path != str(Path(plugin_root).parent / "journal.sqlite"):
                raise ValueError("invalid_journal_path")
        else:
            private = Path(journal_path).parent
            info = private.lstat()
            if (Path(journal_path).name != 'journal.sqlite' or
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
        from memvane.install import initial_build as laya_initial_build
        build = laya_initial_build.lookup(str(root))
        if build is None:
            return None
        identity = build['identity']
        if build.get('schema') == laya_initial_build.V1_BUILD_SCHEMA:
            if build['phase'] not in ('registered', 'building', 'ready'):
                return _decision('blocked', 'initial_preview_build_unavailable')
            state = Path(identity['stateRoot'])
            raw = _read_regular(state / 'route.json', MAX_RECORD_BYTES)
            if hashlib.sha256(raw).hexdigest() != identity['routeSha256']:
                return _decision('blocked', 'initial_preview_route_changed')
            route = json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs)
            candidate = _classify_v4(root, project_dir.settings_path(root), route)
            if candidate['state'] != 'candidate':
                return candidate
            if any(candidate.get(key) != identity[key] for key in
                   ('projectRoot', 'stateRoot', 'storePath', 'storeIncarnation',
                    'scopeId', 'installId', 'journalPath')):
                return _decision('blocked', 'initial_preview_route_mismatch')
            return {**candidate, 'state': 'preview', 'reason': 'initial_build_preview'}
        return _decision('blocked', 'initial_preview_build_unavailable')
    except (OSError, RuntimeError, ValueError, TypeError, UnicodeError, KeyError):
        return _decision('blocked', 'initial_preview_proof_unavailable')


def select_route(cwd: str, *, action: str = "recall", linked: bool = True) -> dict:
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
    if linked and decision.get("state") in ("blocked", "legacy") and action in ROUTED_ACTIONS:
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
    if not config.is_minimal_settings(raw):
        raise ValueError("invalid_route_settings")
    folder = project_types.CURRENT
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
        from memvane.kernel import paths as git_branch_safety
        linked = git_branch_safety.linked_worktree(current)
        if linked is None:
            return None
        worktree, main = Path(linked["worktreeRoot"]), Path(linked["mainRoot"])
        if current != worktree and worktree not in current.parents:
            return None
        for directory in (current, *current.parents):
            for name in (RECORD, QMD_SELECTION_MARKER):
                if any(os.path.lexists(directory / folder / name)
                       for folder in project_types.NAMES):
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
        if _settings_identity(project) != _settings_identity(main_project):
            return None
        if main_decision.get('state') == 'blocked':
            return _decision('blocked', 'linked_worktree_main_blocked')
        if (main_decision.get("state") != 'candidate'
                or main_decision.get("projectRoot") != str(main_project)):
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
        from memvane.journal.api import ledger_read_json as _read_json
        marker_root, marker_path = markers[0]
        try:
            marker_record = _read_json(marker_path)
            schema = marker_record.get('schema') if isinstance(marker_record, dict) else None
        except (OSError, RuntimeError, ValueError, TypeError):
            schema = None
        if schema == SELECTION_SCHEMA_V1:
            try:
                if (set(marker_record) != {'schema', 'projectRoot', 'installId', 'routeSha256'}
                        or type(marker_record['projectRoot']) is not str
                        or type(marker_record['installId']) is not str
                        or not INCARNATION.fullmatch(marker_record['installId'])
                        or type(marker_record['routeSha256']) is not str
                        or not SHA256.fullmatch(marker_record['routeSha256'])):
                    raise ValueError('selection_marker_invalid')
                if not records:
                    from memvane.kernel import memvane_paths
                    try:
                        memvane_paths.install_root(marker_record['installId']).lstat()
                    except FileNotFoundError:
                        return _decision('legacy', 'foreign_install')
                    raise ValueError('selection_route_missing')
                if len(records) != 1 or records[0][0] != marker_root:
                    raise ValueError('selection_marker_invalid')
                route_bytes = _read_regular(records[0][2], MAX_RECORD_BYTES)
                route = json.loads(route_bytes.decode('utf-8'), object_pairs_hook=_pairs)
                if (not isinstance(route, dict) or route.get('schema') != SCHEMA_V4
                        or route.get('installId') != marker_record['installId']
                        or route.get('projectRoot') != marker_record['projectRoot']
                        or hashlib.sha256(route_bytes).hexdigest() != marker_record['routeSha256']):
                    raise ValueError('selection_marker_invalid')
                return _classify_record(*records[0], current)
            except (OSError, RuntimeError, ValueError, TypeError, UnicodeError, KeyError):
                return _decision('blocked', 'selection_marker_invalid')
        return _decision('blocked', 'selection_marker_invalid')
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
    result = _classify_record(root, auto, path, current)
    if result.get('routeSchema') == SCHEMA_V4 and not markers:
        return _decision('blocked', 'selection_marker_missing')
    return result

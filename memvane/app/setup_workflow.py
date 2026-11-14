#!/usr/bin/env python3
"""Plan, prepare, finalize and activate one private MemVane store."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
import time

from memvane.settings import api as project_dir, types as project_types
from memvane.install.types import InstallBlocked
from memvane.install.api import install_lock as _install_lock
from memvane.install.api import _initial_build as initial_build
from memvane.install.api import _ledger

from memvane.journal.api import inspect_journal


_REVIEWED_MODEL_BASENAMES = frozenset((
    'embeddinggemma-300M-Q8_0.gguf',
    'hf_ggml-org_embeddinggemma-300M-Q8_0.gguf',
))


def _fail(reason):
    raise InstallBlocked(reason)


def _private_parent(path):
    parent = Path(path)
    if not parent.is_absolute() or parent.is_symlink() or not parent.is_dir():
        _fail('unsafe_install_parent')
    info = parent.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        _fail('unsafe_install_parent')
    return parent.resolve(strict=True)


def _prepare_state_root(path, install_id):
    """Create only a fresh per-install directory under the local state base."""
    root = Path(path)
    from memvane.kernel.memvane_execution_guard import require_cached_plugin_for_live_write
    require_cached_plugin_for_live_write(root)
    from memvane.kernel import memvane_paths
    if root.exists() or root.is_symlink():
        result = _private_parent(root)
        if result.parent == memvane_paths.installs_base():
            memvane_paths.ensure_state_layout()
        return result
    base = memvane_paths.installs_base()
    if (not root.is_absolute() or root.parent != base or
            type(install_id) is not str or not re.fullmatch(r'[0-9a-f]{32}', install_id) or
            root.name != install_id):
        _fail('unsupported_state_root_creation')
    cursor = Path.home()
    for name in (*memvane_paths.STATE_PARTS, 'installs', install_id):
        cursor = cursor / name
        if not cursor.exists() and not cursor.is_symlink():
            os.mkdir(cursor, 0o700)
        info = cursor.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or
                stat.S_IMODE(info.st_mode) & 0o022):
            _fail('unsafe_state_root_ancestor')
    memvane_paths.ensure_state_layout()
    return _private_parent(root)


def _read_private(path, limit):
    source = Path(path)
    if not source.is_absolute() or source.is_symlink():
        _fail('unsafe_install_input')
    fd = os.open(source, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or
                before.st_nlink != 1 or stat.S_IMODE(before.st_mode) & 0o077 or
                before.st_size > limit):
            _fail('unsafe_install_input')
        raw = os.read(fd, limit + 1)
        after = os.fstat(fd)
        if (len(raw) != before.st_size or
                (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) !=
                (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)):
            _fail('install_input_changed')
        return raw
    finally:
        os.close(fd)


def _write_or_verify_private(path, raw):
    """Create a private record with exactly ``raw``, or verify the one there.

    The bytes are written beside the record and published with a no-replace
    rename, so an interrupted write leaves no record and its rerun writes it.
    """
    from memvane.kernel.atomic_rename import write_new_file
    target = Path(path)
    if not target.exists() and not target.is_symlink():
        try:
            write_new_file(target, raw)
            return
        except FileExistsError:
            pass
    if _read_private(target, 8192) != raw:
        _fail('private_route_requires_recovery')


# Origin planning reads settings only; the initial build scans the wiki.
def plan_fresh_wiki_origin(project_root, *, _bound_marker=False):
    """Review a new wiki-only Laya generation from minimal project settings.

    The canonical ``.memvane/settings.json`` must be minimal
    (``{name, skipPaths}``); any other shape refuses as
    ``minimal_settings_required``. The settings file is never rewritten; the
    installer builds a separate private generation from the canonical wiki
    root. No QMD database is read, copied, or selected. A project route or
    selection marker refuses (``project_selection_already_exists``) unless
    it names an install that does not exist on this machine; the plan then
    lists those pointers (``replacesForeignPointers``) and prepare removes them.
    """
    from memvane.settings import api as config
    from memvane.route.api import (multi_settings_supported as _multi_settings_supported,
                                       multi_compile_supported as _multi_compile_supported,
                                       selected_collection_roots,
                                       source_for_collection as _source_for_collection,
                                       read_regular as _read_regular)
    from memvane.route.types import MAX_SETTINGS_BYTES, RECORD, QMD_SELECTION_MARKER
    project = Path(project_root)
    if (not project.is_absolute() or project.is_symlink() or
            project.resolve(strict=True) != project):
        _fail('invalid_project_root')
    auto = project_dir.folder(project)
    foreign = None
    if ((auto / RECORD).exists() or (auto / RECORD).is_symlink() or
            (not _bound_marker and ((auto / QMD_SELECTION_MARKER).exists() or
                                    (auto / QMD_SELECTION_MARKER).is_symlink()))):
        from memvane.app.setup_activation import foreign_pointers
        foreign = foreign_pointers(project)
        if not foreign:
            _fail('project_selection_already_exists')
    found = config.find_project_config(str(project))
    settings = auto / 'settings.json'
    if (found.get('configPath') != str(settings) or
            found.get('projectRoot') != str(project) or settings.is_symlink()):
        _fail('canonical_settings_required')
    raw = _read_regular(settings, MAX_SETTINGS_BYTES)
    settings_sha = hashlib.sha256(raw).hexdigest()
    parsed = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique)
    # A new selection starts only from minimal ``{name, skipPaths}`` settings.
    if not config.is_minimal_settings(parsed):
        _fail('minimal_settings_required')
    source_settings = config.expand_minimal_settings(parsed, project_dir.active_name(project))
    cfg = config.load_project_config(str(project))
    names = tuple(sorted(cfg.get('collections', ())))
    roles = config.role_map(cfg)
    if (cfg.get('indexing') is not True or len(names) != 2 or
            sorted(roles.get(name) for name in names) != ['raw', 'wiki'] or
            set(cfg.get('collectionPaths', {})) != set(names) or
            not _multi_settings_supported(source_settings, cfg, names)):
        _fail('unsupported_selected_settings')
    collections = [{'name': name, 'role': roles[name],
                    'sourceRoot': _source_for_collection(
                        project, cfg['collectionPaths'][name])}
                   for name in names]
    try:
        selected_collection_roots(project, collections)
    except ValueError as exc:
        raise InstallBlocked(str(exc)) from exc
    raw_root = Path(next(item['sourceRoot'] for item in collections
                         if item['role'] == 'raw'))
    wiki_root = Path(next(item['sourceRoot'] for item in collections
                          if item['role'] == 'wiki'))
    if (raw_root != project or wiki_root != project_dir.folder(project) / project_types.WIKI
            or not _multi_compile_supported(source_settings, cfg, project,
                                             str(wiki_root))):
        _fail('fresh_wiki_origin_settings_unsupported')
    info = project.stat()
    seed = json.dumps(['fresh-wiki-only', str(project), info.st_dev,
                       info.st_ino, settings_sha], separators=(',', ':')).encode()
    plan = {'schema': 'laya-fresh-wiki-origin-plan-v1', 'mode': 'fresh_wiki_only',
            'projectRoot': str(project), 'settingsSha256': settings_sha,
            'scopeId': hashlib.sha256(seed).hexdigest()[:32],
            'collections': collections, 'sourceInventory': 'durable_wiki_catalog_scan'}
    if foreign:
        # prepare removes exactly these pointer bytes before it registers the build.
        plan['replacesForeignPointers'] = foreign
    digest = hashlib.sha256(json.dumps(plan, sort_keys=True,
        ensure_ascii=False, separators=(',', ':')).encode('utf-8')).hexdigest()
    return {'status': 'review_required', 'proposalSha256': digest, **plan}


def _settings_identity(plan):
    """A new route binds the resolved settings identity, not the file bytes;
    the reviewed plan proposal keeps binding the exact bytes."""
    from memvane.route.api import settings_identity_digest
    return settings_identity_digest(plan['projectRoot'], plan['collections'])


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _fail('duplicate_settings_key')
        result[key] = value
    return result


_PROVIDER_FILE_MAX = 4 * 1024 * 1024


def _provider_file_sha(path):
    """sha256 of one reviewed provider file: a regular, singly linked, bounded inode read once."""
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > _PROVIDER_FILE_MAX:
        raise ValueError('unsafe_bundle_file')
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_CLOEXEC', 0))
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise ValueError('bundle_file_changed')
        raw = os.read(fd, _PROVIDER_FILE_MAX + 1)
        ident = lambda s: (s.st_dev, s.st_ino, s.st_nlink, s.st_mode, s.st_size,
                           s.st_mtime_ns, s.st_ctime_ns)
        if (len(raw) > _PROVIDER_FILE_MAX or ident(before) != ident(os.fstat(fd))
                or ident(before) != ident(path.lstat())):
            raise ValueError('bundle_file_changed')
    finally:
        os.close(fd)
    return hashlib.sha256(raw).hexdigest()


def review_local_provider(*, plugin_root, state_root,
                          model_path=None, dimension=None, runtime_generation=None):
    """Read-only exact local artifact proposal for human/release review.

    The executable inventory is the named Laya generation when supplied,
    otherwise the active runtime: its node, node-llama-cpp entry, every packaged
    native binary of the pinned Metal build and its GGUF model. The runtime
    is fully re-hashed; a missing or changed file refuses. A ``model_path``
    argument, when given, must name the runtime's own model file. Review
    neither asserts that native execution works nor issues approval: the
    caller must accept the returned digest, and prepare repeats every check.
    """
    import platform
    from memvane.provider import api as laya_embed_runtime
    from memvane.provider.api import profile_identity
    from memvane.provider.api import default_embed_model_uri, hash_file as _hash_file, recipe_fingerprint
    DEFAULT_EMBED_MODEL_URI = default_embed_model_uri()
    if platform.system() != 'Darwin' or platform.machine() != 'arm64':
        _fail('reviewed_native_platform_unsupported')
    root, state = map(Path, (plugin_root, state_root))
    if (not all(path.is_absolute() and not path.is_symlink()
                for path in (root, state)) or
            str(state.resolve(strict=False)) != str(state)):
        _fail('provider_review_paths_invalid')
    # Only the v1 install namespace may be created by prepare.
    from memvane.kernel import memvane_paths
    try:
        socket_path = memvane_paths.install_socket(state)
    except ValueError as exc:
        _fail(str(exc))
    if (not state.exists() and not state.is_symlink() and
            (state.parent != memvane_paths.installs_base() or
             not re.fullmatch(r'[0-9a-f]{32}', state.name))):
        _fail('unsupported_state_root_creation')
    provider_files = ('memvane/provider/adapters/local_embed_provider.py',
                      'memvane/provider/adapters/laya_local_embed_worker.mjs')
    artifacts = {}
    for name in provider_files:
        artifacts[name] = _provider_file_sha(root / name)
    try:
        runtime = (laya_embed_runtime.select() if runtime_generation is None else
                   laya_embed_runtime.select_generation(runtime_generation))
    except laya_embed_runtime.RuntimeUnavailable as exc:
        _fail('provider_review_' + str(exc))
    module_pkg = Path(runtime['modulePackage'])
    native_pkg = Path(runtime['nativePackage'])
    for package_file in (module_pkg / 'package.json', native_pkg / 'package.json'):
        item = json.loads(_read_private_or_package(package_file))
        if not isinstance(item, dict) or item.get('version') != laya_embed_runtime.LLAMA_VERSION:
            _fail('provider_review_package_version_changed')
    closure = runtime['dependencyClosure']
    if (not any(row['path'].endswith('.node') for row in closure) or
            not any(row['path'].endswith('.dylib') for row in closure) or
            len(closure) > 4096):
        _fail('provider_review_native_closure_incomplete')
    if type(dimension) is not int or not 1 <= dimension <= 2048:
        _fail('provider_review_dimension_invalid')
    if model_path is not None and model_path != runtime['modelPath']:
        _fail('provider_review_model_not_laya_runtime')
    model_path = runtime['modelPath']
    if Path(model_path).name not in _REVIEWED_MODEL_BASENAMES:
        _fail('provider_review_model_identity_invalid')
    node, module = runtime['nodePath'], runtime['moduleEntryPath']
    # Same bytes the runtime sealed; re-read through the provider's own hasher.
    model_sha = _hash_file(model_path)
    node_sha, module_sha = _hash_file(node), _hash_file(module)
    if (model_sha, node_sha, module_sha) != (runtime['modelSha256'], runtime['nodeSha256'],
                                             runtime['moduleEntrySha256']):
        _fail('provider_review_laya_embed_runtime_corrupt')
    closure_sha = hashlib.sha256(json.dumps(
        closure, sort_keys=True, ensure_ascii=False, separators=(',', ':')
    ).encode('utf-8')).hexdigest()
    provider_sha = artifacts['memvane/provider/adapters/local_embed_provider.py']
    worker_sha = artifacts['memvane/provider/adapters/laya_local_embed_worker.mjs']
    model_id = DEFAULT_EMBED_MODEL_URI
    runtime_id = runtime['runtimeId']
    fingerprint = recipe_fingerprint(
        model_id=model_id, model_sha256=model_sha,
        runtime_id=runtime_id,
        recipe_id='qmd-regex-token-chunks-candidate-v1',
        provider_sha256=provider_sha, worker_sha256=worker_sha,
        node_sha256=node_sha, module_entry_sha256=module_sha,
        closure_sha256=closure_sha)
    approved_id = 'local-qmd-' + hashlib.sha256(
        (provider_sha + worker_sha + closure_sha).encode()).hexdigest()[:24]
    profile = {'profileId': profile_identity(model_id, fingerprint, dimension),
               'modelId': model_id, 'embeddingFingerprint': fingerprint,
               'dimension': dimension, 'metric': 'cosine',
               'approvedProviderId': approved_id,
               'providerArtifactSha256': provider_sha,
               'providerRelativePath': 'memvane/provider/adapters/local_embed_provider.py'}
    manifest = {'approvedProviderId': approved_id,
                'providerArtifactSha256': provider_sha,
                'providerRelativePath': profile['providerRelativePath'],
                'providerPath': str(root / profile['providerRelativePath']),
                'workerPath': str(root / 'memvane/provider/adapters/laya_local_embed_worker.mjs'),
                'workerSha256': worker_sha,
                'serviceSocketPath': str(socket_path),
                'nodePath': str(node), 'nodeSha256': node_sha,
                'moduleEntryPath': str(module), 'moduleEntrySha256': module_sha,
                'modelPath': str(model_path), 'modelSha256': model_sha,
                'runtimeId': runtime_id,
                'modelId': model_id, 'embeddingFingerprint': fingerprint,
                'dimension': dimension,
                'recipeId': 'qmd-regex-token-chunks-candidate-v1',
                'dependencyClosureApproved': True,
                'dependencyClosureSha256': closure_sha}
    proposal = {'schema': 'laya-local-provider-review-v2',
                'stateRoot': str(state),
                'embedRuntimeGeneration': runtime['generation'],
                'reviewedProvider': {'vectorProfile': profile,
                                     'providerManifest': manifest,
                                     'dependencyClosure': closure}}
    return {'status': 'review_required',
            'proposalSha256': hashlib.sha256(json.dumps(
                proposal, sort_keys=True, ensure_ascii=False, separators=(',', ':')
            ).encode('utf-8')).hexdigest(),
            **proposal}


def _read_private_or_package(path):
    path = Path(path)
    if not path.is_absolute() or path.is_symlink() or path.stat().st_size > 65536:
        _fail('provider_review_metadata_invalid')
    return path.read_bytes()


def prepare_fresh_wiki_origin(*, project_root, plugin_root, state_root,
                              expected_proposal_sha256, install_id,
                              accepted_provider_sha256,
                              model_path=None, dimension=None):
    """Register an inactive wiki-only build; do not touch QMD or activate.

    The provider is the active Laya embedding runtime; ``model_path`` is
    optional and, when given, must be that runtime's model file.
    """
    if dimension is None:
        _fail('fresh_wiki_model_proof_required')
    if (not isinstance(expected_proposal_sha256, str) or
            not re.fullmatch(r'[0-9a-f]{64}', expected_proposal_sha256) or
            not isinstance(install_id, str) or
            not re.fullmatch(r'[0-9a-f]{32}', install_id) or
            not isinstance(accepted_provider_sha256, str) or
            not re.fullmatch(r'[0-9a-f]{64}', accepted_provider_sha256)):
        _fail('invalid_origin_preparation_request')
    state = _prepare_state_root(state_root, install_id)
    journal = state / 'journal.sqlite'
    from memvane.kernel import memvane_paths
    try:
        memvane_paths.install_socket(state, create=True)
    except ValueError as exc:
        _fail(str(exc))
    with _install_lock(state):
        runtime = state / 'runtime'
        if not runtime.exists() and not runtime.is_symlink():
            os.mkdir(runtime, 0o700)
        _private_parent(runtime)
        plan = plan_fresh_wiki_origin(project_root)
        if plan['proposalSha256'] != expected_proposal_sha256:
            _fail('reviewed_origin_plan_changed')
        _require_raw_scan_within_bounds(plan)
        review = review_local_provider(
            plugin_root=plugin_root, state_root=state,
            model_path=model_path, dimension=dimension)
        if review['proposalSha256'] != accepted_provider_sha256:
            _fail('reviewed_provider_proposal_changed')
        from memvane.app.setup_activation import retire_foreign_pointers
        retire_foreign_pointers(plan)
        result = _register_initial_build(
            plan=plan, reviewed_provider=review['reviewedProvider'],
            state=state, journal=journal,
            install_id=install_id)
        return result


def _require_raw_scan_within_bounds(plan):
    """Refuse before any build a raw tree the sync's bounded raw scan cannot read.

    The initial build and every later sync scan the raw roots with the same
    file and byte bounds; over them a generation names no sources and
    finalize could never prove the raw snapshot.
    """
    from memvane.kernel.errors import IndexContractError
    from memvane.wiki.intake.api import scan_collections
    from memvane.wiki.intake.types import RAW_SCAN_MAX_BYTES, RAW_SCAN_MAX_FILES
    roots = {item['name']: item['sourceRoot'] for item in plan['collections']
             if item['role'] == 'raw'}
    try:
        scan_collections(roots, plan['scopeId'], None, max_files=RAW_SCAN_MAX_FILES,
                         max_bytes=RAW_SCAN_MAX_BYTES)
    except (IndexContractError, ValueError, OSError) as exc:
        detail = str(exc.__cause__ or exc)
        bounded = detail in ('too many files', 'too many entries', 'too deep') or detail.startswith('oversize:')
        raise InstallBlocked('raw_scan_limit_exceeded' if bounded else 'raw_scan_unavailable') from exc


def _request_build_wake(project_root):
    """Queue the selected-project wake kind; the worker finds the build."""
    try:
        from memvane.runtime import api as runtime_work
        queued = runtime_work.request('laya-sync', str(project_root))
    except (OSError, ValueError) as exc:
        return {'status': 'wake_unavailable', 'reason': type(exc).__name__}
    return queued if queued is not None else {'status': 'wake_skipped_in_fixture'}


def _create_v1_store(project_root, state_root, scope_id):
    """Create one new format-4 store under the installer's lock."""
    from memvane.kernel.memvane_execution_guard import require_cached_plugin_for_live_write
    from memvane.store import api as store_api
    state = _private_parent(state_root)
    require_cached_plugin_for_live_write(state)
    project = _checked_project(project_root)
    path = state / 'store.sqlite'
    if path.exists() or path.is_symlink():
        from contextlib import closing
        from memvane.kernel.errors import _query_only_uri
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o077):
            _fail('initial_store_unsafe')
        with closing(sqlite3.connect(_query_only_uri(path), uri=True)) as db:
            db.execute('PRAGMA query_only=ON')
            meta = store_api.stored_header(db)
            rows = store_api.stored_worktrees(db, project_root=str(project), records=True)
        if meta is None or len(rows) != 1:
            _fail('initial_store_identity_changed')
        repository = json.loads(meta[0], object_pairs_hook=_unique)
        worktree = json.loads(rows[0][0], object_pairs_hook=_unique)
        binding = store_api.observe_binding(path, repository=repository, scope_id=scope_id,
                                            worktree_id=worktree['worktreeId'])
    else:
        binding = store_api.observe_binding(path, scope_id=scope_id, create_root=str(project))
        repository, worktree = binding['repository'], binding['worktree']
    incarnation, policy = binding['incarnation'], binding['policy_digest']
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o077):
        _fail('initial_store_unsafe')
    return {'storePath': str(path), 'storeNode': _live_node(path),
            'storeIncarnation': incarnation, 'repository': repository,
            'worktree': worktree, 'settingsPolicyDigest': policy}
def _live_node(path):
    info = Path(path).lstat()
    return [info.st_dev, info.st_ino]


def _keep_recorded_devices(route, path):
    """Reuse a staged route's ``[st_dev, st_ino]`` where the inode still matches.

    APFS may renumber devices at boot, so a rerun compares the recorded nodes
    by inode and keeps the recorded bytes; any other difference still refuses.
    """
    from memvane.kernel.persistent_identity import persistent_node_equal
    if not os.path.lexists(path):
        return
    try:
        staged = json.loads(_read_private(path, 8192).decode('utf-8'), object_pairs_hook=_unique)
    except (UnicodeError, ValueError):
        return  # A torn route keeps refusing in ``_write_or_verify_private``.
    if not isinstance(staged, dict):
        return
    for field in ('stateNode', 'storeNode'):
        if persistent_node_equal(staged.get(field), route[field]):
            route[field] = list(staged[field])


def _create_v1_route(plan, state_root, install_id, store, vector_profile):
    """Stage a route bound to a validated local store and project settings."""
    from memvane.kernel.memvane_execution_guard import require_cached_plugin_for_live_write
    from memvane.kernel.memvane_version import same_major
    from memvane.route.types import SCHEMA_V4
    from memvane.route.api import classify_v4 as _classify_v4
    from memvane.journal.api import route_record
    state = _private_parent(state_root)
    require_cached_plugin_for_live_write(state)
    if not same_major(1):
        _fail('state_format_mismatch')
    project = _checked_project(plan['projectRoot'])
    settings = project_dir.settings_path(project)
    if settings is None:
        _fail('origin_settings_unavailable')
    route = {'schema': SCHEMA_V4, 'status': 'active', 'projectRoot': str(project),
             'settingsSha256': _settings_identity(plan), 'stateRoot': str(state),
             'stateNode': _live_node(state),
             'storePath': store['storePath'], 'storeNode': store['storeNode'],
             'storeIncarnation': store['storeIncarnation'], 'installId': install_id,
             'scopeId': plan['scopeId'], 'stateFormat': 1,
             'collections': plan['collections'], 'vectorProfile': vector_profile}
    _keep_recorded_devices(route, state / 'route.json')
    raw = json.dumps(route, sort_keys=True, ensure_ascii=False,
                     separators=(',', ':')).encode('utf-8')
    route_record(raw, state / 'journal.sqlite')
    if _classify_v4(project, settings, route).get('state') != 'candidate':
        _fail('origin_route_unproved')
    _write_or_verify_private(state / 'route.json', raw)
    return raw


def _register_initial_build(*, plan, reviewed_provider, state, journal, install_id):
    """Register the reviewed single-store build under the existing install lock."""
    from memvane.install.api import _initial_build as initial_build
    from memvane.journal.api import prepare_v1_journal
    existing = initial_build.lookup(plan['projectRoot'])
    if existing is not None and (existing['identity']['stateRoot'] != str(state)
                                 or existing['identity']['installId'] != install_id):
        _fail('initial_build_already_registered')
    store = _create_v1_store(plan['projectRoot'], state, plan['scopeId'])
    route_raw = _create_v1_route(plan, state, install_id, store,
                                 reviewed_provider['vectorProfile'])
    staged, row = prepare_v1_journal(journal, reviewed_provider, route_raw)
    route_sha = hashlib.sha256(route_raw).hexdigest()
    store = {**store, 'storeNode': json.loads(route_raw.decode('utf-8'))['storeNode']}
    if (row['state'] != 'prepared' or row['routeSha256'] != route_sha
            or row['attestationSha256'] != staged['attestationSha256']
            or row['installId'] != install_id or row['scopeId'] != plan['scopeId']
            or row['projectRoot'] != plan['projectRoot']
            or row['storePath'] != store['storePath']
            or row['storeInode'] != store['storeNode'][1]
            or row['storeIncarnation'] != store['storeIncarnation']):
        _fail('initial_build_journal_changed')
    identity = {'projectRoot': plan['projectRoot'], 'stateRoot': str(state),
                'installId': install_id, 'scopeId': plan['scopeId'],
                'storePath': store['storePath'], 'storeNode': store['storeNode'],
                'storeIncarnation': store['storeIncarnation'], 'journalPath': str(journal),
                'routeSha256': route_sha, 'attestationSha256': staged['attestationSha256']}
    from memvane.journal.api import prepare_reader_lifetime
    prepare_reader_lifetime(install_id)
    with initial_build.owner(state) as owned:
        if not owned:
            _fail('initial_build_busy')
        record = initial_build.create_v1(identity)
        pointer = initial_build.register_project(identity)
    return {'status': 'initial_build_registered',
            'proposalSha256': plan['proposalSha256'], 'journalPath': str(journal),
            'pendingRouteSha256': route_sha, 'storePath': store['storePath'],
            'routeFile': str(state / 'route.json'),
            'buildRecord': str(state / initial_build.V1_BUILD_FILE),
            'buildPointer': pointer, 'buildPhase': record['phase'],
            'wake': _request_build_wake(plan['projectRoot'])}


def initial_build_status(state_root):
    """Read the single-store build's durable phase."""
    from memvane.install.api import _initial_build as initial_build
    state = _private_parent(state_root)
    record = initial_build.read_v1(state)
    if record is None:
        _fail('initial_build_missing')
    return {'status': 'initial_build', 'phase': record['phase'],
            'reason': record['reason'], 'epoch': record['epoch']}


def request_initial_build_rescan(state_root):
    """Request another bounded build pass through the existing sync wake.

    Only a build that was never selected rescans: once the project pointers
    or a selected journal exist, refreshing the index belongs to the sync
    owner, and a ``building`` record would fail every selected proof.
    """
    from memvane.install.api import _initial_build as initial_build
    state = _private_parent(state_root)
    with _install_lock(state), initial_build.owner(state) as owned:
        if not owned:
            _fail('initial_build_busy')
        record = initial_build.read_v1(state)
        if record is None:
            _fail('initial_build_missing')
        _require_unselected(record['identity'])
        record = initial_build.advance_v1(record, phase='building')
    return {'status': 'rescan_requested', 'phase': record['phase'],
            'wake': _request_build_wake(record['identity']['projectRoot'])}


def _project_pointers_present(project_root):
    return any(os.path.lexists(project_dir.runtime_file(Path(project_root), name))
               for name in ('route.json', 'selection.json'))


def _require_unselected(identity):
    """Refuse once activation began: journal past ``prepared`` or a project pointer."""
    row = inspect_journal(identity['journalPath'])
    if row['state'] != 'prepared' or _project_pointers_present(identity['projectRoot']):
        _fail('initial_build_already_selected')
    return row


def finalize_initial_build(*args, **kwargs):
    from memvane.app.setup_activation import finalize_initial_build as implementation
    return implementation(*args, **kwargs)


def _checked_project(project_root):
    project = Path(project_root)
    if (not project.is_absolute() or project.is_symlink() or
            project.resolve(strict=True) != project):
        _fail('invalid_project_root')
    return project


def activate_origin_selection(*args, **kwargs):
    from memvane.app.setup_activation import activate_origin_selection as implementation
    return implementation(*args, **kwargs)


def recover_origin_selection(*args, **kwargs):
    from memvane.app.setup_activation import recover_origin_selection as implementation
    return implementation(*args, **kwargs)


def restore_origin_selection(*args, **kwargs):
    from memvane.app.setup_activation import restore_origin_selection as implementation
    return implementation(*args, **kwargs)


def _installed_root():
    return Path(__file__).resolve(strict=True).parents[2]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    fresh_plan = sub.add_parser('plan-fresh-wiki',
                                help='review a new wiki-only generation')
    fresh_plan.add_argument('--project-root', required=True)
    review = sub.add_parser('review-provider',
                            help='inventory exact local Node/model/native artifacts')
    review.add_argument('--state-root', required=True)
    review.add_argument('--model-file')
    review.add_argument('--dimension', type=int)
    fresh_prepare = sub.add_parser('prepare-fresh-wiki',
                                   help='register an inactive new wiki-only build')
    for key in ('project-root', 'state-root', 'proposal-sha256', 'install-id',
                'accepted-provider-sha256'):
        fresh_prepare.add_argument('--' + key, required=True)
    fresh_prepare.add_argument('--model-file')
    fresh_prepare.add_argument('--dimension', type=int)
    status = sub.add_parser('status', help='inspect the inactive or active install journal')
    status.add_argument('--state-root', required=True)
    build_status = sub.add_parser('build-status', help='inspect a registered initial build')
    build_status.add_argument('--state-root', required=True)
    rescan = sub.add_parser('build-rescan',
                            help='request a scan of a registered initial build')
    rescan.add_argument('--state-root', required=True)
    finalize = sub.add_parser('finalize-build',
                              help='bind a sealed, fresh initial build; never activates')
    finalize.add_argument('--project-root', required=True)
    finalize.add_argument('--state-root', required=True)
    for name in ('activate', 'recover'):
        command = sub.add_parser(name)
        command.add_argument('--project-root', required=True)
        command.add_argument('--state-root', required=True)
        command.add_argument('--route-file', required=True)
        if name == 'activate':
            command.add_argument('--approve-activation', action='store_true')
    restore = sub.add_parser('restore',
                             help='withdraw the Laya selection before its first selected write')
    restore.add_argument('--project-root', required=True)
    restore.add_argument('--state-root', required=True)
    restore.add_argument('--approve-restore', action='store_true')
    args = parser.parse_args(argv)
    if args.action not in ('plan-fresh-wiki', 'review-provider', 'status', 'build-status'):
        from memvane.kernel.memvane_execution_guard import require_cached_plugin_for_live_write
        require_cached_plugin_for_live_write()
    root = _installed_root()
    state_root = (_private_parent(args.state_root)
                  if args.action in ('status', 'activate', 'recover') else None)
    journal = state_root / 'journal.sqlite' if state_root else None
    if args.action == 'plan-fresh-wiki':
        result = plan_fresh_wiki_origin(args.project_root)
    elif args.action == 'build-status':
        result = initial_build_status(args.state_root)
    elif args.action == 'build-rescan':
        result = request_initial_build_rescan(args.state_root)
    elif args.action == 'finalize-build':
        result = finalize_initial_build(project_root=args.project_root,
                                        state_root=args.state_root)
    elif args.action == 'review-provider':
        result = review_local_provider(
            plugin_root=root, state_root=args.state_root,
            model_path=args.model_file, dimension=args.dimension)
    elif args.action == 'prepare-fresh-wiki':
        result = prepare_fresh_wiki_origin(
            project_root=args.project_root, plugin_root=root, state_root=args.state_root,
            expected_proposal_sha256=args.proposal_sha256,
            install_id=args.install_id,
            accepted_provider_sha256=args.accepted_provider_sha256,
            model_path=args.model_file, dimension=args.dimension)
    elif args.action == 'status':
        result = {'journal': inspect_journal(journal)}
    elif args.action == 'restore':
        if not args.approve_restore:
            _fail('restore_approval_required')
        result = restore_origin_selection(project_root=args.project_root,
                                          state_root=args.state_root)
    else:
        project = _checked_project(args.project_root)
        route = _read_private(args.route_file, 8192)
        with _install_lock(state_root):
            if args.action == 'activate':
                if not args.approve_activation:
                    _fail('activation_approval_required')
                result = activate_origin_selection(project, journal, route)
            else:
                result = recover_origin_selection(project, journal, route)
    print(json.dumps(result, sort_keys=True, ensure_ascii=False))
    if args.action == 'restore' and result.get('status') in ('blocked', 'recovery_required'):
        return 2
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (InstallBlocked, OSError, ValueError, RuntimeError) as exc:
        print(json.dumps({'status': 'blocked', 'reason': str(exc)},
                         sort_keys=True), file=sys.stderr)
        sys.exit(2)

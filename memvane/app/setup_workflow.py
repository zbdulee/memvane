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
    target = Path(path)
    if target.exists() or target.is_symlink():
        if _read_private(target, 8192) != raw:
            _fail('private_route_requires_recovery')
        return
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                 getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        # Retain a partial path for explicit recovery; never unlink on error.
        raise
    directory = os.open(target.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


# Origin planning reads settings only; the initial build scans the wiki.
def plan_fresh_wiki_origin(project_root, *, _bound_marker=False):
    """Review a new wiki-only Laya generation from minimal project settings.

    The canonical ``.memvane/settings.json`` must be minimal
    (``{name, skipPaths}``); any other shape refuses as
    ``minimal_settings_required``. The settings file is never rewritten; the
    installer builds a separate private generation from the canonical wiki
    root. No QMD database is read, copied, or selected.
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
    if ((auto / RECORD).exists() or (auto / RECORD).is_symlink() or
            (not _bound_marker and ((auto / QMD_SELECTION_MARKER).exists() or
                                    (auto / QMD_SELECTION_MARKER).is_symlink()))):
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
    from memvane.provider.api import _bundle as product_bundle
    _read, MAX_FILE, _canonical = (product_bundle._read, product_bundle.MAX_FILE,
                                   product_bundle._canonical)
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
        installed, _ = _read(root / name, MAX_FILE)
        artifacts[name] = hashlib.sha256(installed).hexdigest()
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
    closure_sha = hashlib.sha256(_canonical(closure)).hexdigest()
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
            'proposalSha256': hashlib.sha256(_canonical(proposal)).hexdigest(),
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
        review = review_local_provider(
            plugin_root=plugin_root, state_root=state,
            model_path=model_path, dimension=dimension)
        if review['proposalSha256'] != accepted_provider_sha256:
            _fail('reviewed_provider_proposal_changed')
        result = _register_initial_build(
            plan=plan, reviewed_provider=review['reviewedProvider'],
            state=state, journal=journal,
            install_id=install_id)
        return result


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
    from memvane.store import api as checkout
    from memvane.store.api import view_store_class
    ViewStore = view_store_class()
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
            meta = db.execute('SELECT repository_json FROM store_meta WHERE singleton=1').fetchone()
            rows = db.execute('SELECT record_json FROM worktrees WHERE project_root=?',
                              (str(project),)).fetchall()
        if meta is None or len(rows) != 1:
            _fail('initial_store_identity_changed')
        repository = json.loads(meta[0], object_pairs_hook=_unique)
        worktree = json.loads(rows[0][0], object_pairs_hook=_unique)
        store = ViewStore.open(path, repository=repository, scope_id=scope_id)
    else:
        repository = checkout.register_repository(str(project))
        worktree = checkout.register_worktree(str(project), repository)
        store = ViewStore.create(path, project_root=str(project), repository=repository,
                                 worktree=worktree, scope_id=scope_id)
    try:
        store._observe_worktree(worktree['worktreeId'])
        incarnation = store._incarnation
        policy = store._policy
    finally:
        store.close()
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o077):
        _fail('initial_store_unsafe')
    return {'storePath': str(path), 'storeNode': [info.st_dev, info.st_ino],
            'storeIncarnation': incarnation, 'repository': repository,
            'worktree': worktree, 'settingsPolicyDigest': policy}
def _create_v1_route(plan, state_root, install_id, store, vector_profile):
    """Stage a route bound to a validated local store and project settings."""
    from memvane.kernel.memvane_execution_guard import require_cached_plugin_for_live_write
    from memvane.kernel.memvane_version import same_major
    from memvane.route.types import SCHEMA_V4
    from memvane.route.api import classify_v4 as _classify_v4
    from memvane.journal.api import _api_route as _route
    state = _private_parent(state_root)
    require_cached_plugin_for_live_write(state)
    if not same_major(1):
        _fail('state_format_mismatch')
    project = _checked_project(plan['projectRoot'])
    settings = project_dir.settings_path(project)
    if settings is None:
        _fail('origin_settings_unavailable')
    state_info = state.lstat()
    route = {'schema': SCHEMA_V4, 'status': 'active', 'projectRoot': str(project),
             'settingsSha256': _settings_identity(plan), 'stateRoot': str(state),
             'stateNode': [state_info.st_dev, state_info.st_ino],
             'storePath': store['storePath'], 'storeNode': store['storeNode'],
             'storeIncarnation': store['storeIncarnation'], 'installId': install_id,
             'scopeId': plan['scopeId'], 'stateFormat': 1,
             'collections': plan['collections'], 'vectorProfile': vector_profile}
    raw = json.dumps(route, sort_keys=True, ensure_ascii=False,
                     separators=(',', ':')).encode('utf-8')
    _route(raw, state / 'journal.sqlite')
    if _classify_v4(project, settings, route).get('state') != 'candidate':
        _fail('origin_route_unproved')
    _write_or_verify_private(state / 'route.json', raw)
    return raw


def _register_initial_build(*, plan, reviewed_provider, state, journal, install_id):
    """Register the reviewed single-store build under the existing install lock."""
    from memvane.install.api import _initial_build as initial_build
    from memvane.journal.api import _api_stage_v1_attestation as stage_v1_attestation, _api_create_v1_journal as create_v1_journal
    existing = initial_build.lookup(plan['projectRoot'])
    if existing is not None and (existing['identity']['stateRoot'] != str(state)
                                 or existing['identity']['installId'] != install_id):
        _fail('initial_build_already_registered')
    store = _create_v1_store(plan['projectRoot'], state, plan['scopeId'])
    route_raw = _create_v1_route(plan, state, install_id, store,
                                 reviewed_provider['vectorProfile'])
    staged = stage_v1_attestation(journal, reviewed_provider)
    if journal.exists() or journal.is_symlink():
        row = inspect_journal(journal)
    else:
        row = create_v1_journal(journal, route_raw, staged['attestationSha256'])
    route_sha = hashlib.sha256(route_raw).hexdigest()
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
    from memvane.journal.api import _api_prepare_lifetime as _prepare_lifetime
    _prepare_lifetime(install_id)
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
    """Request another bounded build pass through the existing sync wake."""
    from memvane.install.api import _initial_build as initial_build
    state = _private_parent(state_root)
    with _install_lock(state), initial_build.owner(state) as owned:
        if not owned:
            _fail('initial_build_busy')
        record = initial_build.read_v1(state)
        if record is None:
            _fail('initial_build_missing')
        record = initial_build.advance_v1(record, phase='building')
    return {'status': 'rescan_requested', 'phase': record['phase'],
            'wake': _request_build_wake(record['identity']['projectRoot'])}


def finalize_initial_build(*, project_root, state_root):
    """Seal the ready single-store head in the prepared journal and ledger."""
    from memvane.install.api import _initial_build as build
    from memvane.journal.api import _install_journal as journal
    from memvane.journal.api import _reader as reader
    from memvane.app.api import _runtime_dispatch as dispatch
    from memvane.sync import api as sync
    from memvane.store import api as values
    from memvane.store import api as view_values, types as store_types
    from memvane.wiki.workspace.api import file_coordination as workspace_files
    from contextlib import closing
    from memvane.store.api import regular as _regular
    _read_json, _write = _ledger._read_json, _ledger._write
    from memvane.store.api import view_store_class, skip_patterns as _skip_patterns
    ViewStore = view_store_class()
    from memvane.store.api import provider_spec
    from memvane.runtime.api import sync_owner
    state = _private_parent(state_root)
    project = _checked_project(project_root)
    with _install_lock(state), build.owner(state) as owned:
        if not owned:
            _fail('initial_build_busy')
        record = build.read_v1(state)
        if record is None or record['identity']['projectRoot'] != str(project):
            _fail('initial_build_project_mismatch')
        if record['phase'] != 'ready':
            return {'status': 'initial_build_pending', 'phase': record['phase']}
        identity = record['identity']
        with sync_owner(Path(identity['storePath']), wait=False) as owner:
            if owner is None:
                _fail('owner_busy')
            with workspace_files.mutation_lock.lock(project):
                dispatch.resolve('laya-sync', str(project))
                row = journal.inspect_journal(identity['journalPath'])
                attestation, digest = journal._attestation(Path(identity['journalPath']))
                if digest != identity['attestationSha256'] or row['state'] != 'prepared':
                    _fail('initial_build_journal_changed')
                manifest = journal._v1_provider_manifest(attestation, row['vectorProfile'])
                spec = provider_spec(manifest, row['vectorProfile'])
                if spec is None:
                    _fail('initial_build_vector_spec_unavailable')
                decision = journal.select_route(str(project), action='recall')
                if decision.get('state') != 'preview':
                    _fail('initial_build_route_changed')
                from memvane.kernel.errors import _query_only_uri
                with closing(sqlite3.connect(_query_only_uri(Path(identity['storePath'])), uri=True)) as db:
                    db.execute('PRAGMA query_only=ON')
                    meta = db.execute('SELECT repository_json FROM store_meta WHERE singleton=1').fetchone()
                    trees = db.execute('SELECT worktree_id FROM worktrees WHERE project_root=?',
                                       (str(project),)).fetchall()
                if meta is None or len(trees) != 1:
                    _fail('initial_build_store_changed')
                repository = json.loads(meta[0], object_pairs_hook=_unique)
                worktree_id = trees[0][0]
                store = ViewStore.open_reader(identity['storePath'], repository=repository,
                                              scope_id=identity['scopeId'])
                try:
                    if store._incarnation != identity['storeIncarnation']:
                        _fail('initial_build_store_changed')
                    handle = store.capture_view(worktree_id)
                    pin = store.pin_view(handle)
                    try:
                        head = store.head(handle)
                        vector = store._pinned(pin)['vector']
                        if (head['gen_id'] is None or vector is None
                                or vector['recipe_id'] != spec['recipe_id']
                                or vector['profile_id'] != spec['profile_id']):
                            _fail('initial_build_vectors_pending')
                        with store._session() as db:
                            tree = db.execute('SELECT * FROM worktrees WHERE worktree_id=?',
                                              (worktree_id,)).fetchone()
                            baseline = sync.schema3_rows_baseline(store, db, handle, head)
                            head_row = db.execute('SELECT ordinal FROM s_heads WHERE view_id=?',
                                                  (handle.view_id,)).fetchone()
                        if tree is None or head_row is None:
                            _fail('initial_build_store_changed')
                        found, _skipped = sync.schema3_wiki_cards(tree['wiki_root'])
                        skips = _skip_patterns(dict(tree))
                        paths = [p for p in found if not view_values.skipped(p, skips)]
                        current = {}
                        for path in paths:
                            raw, _node, _root = _regular(tree['wiki_root'], path, store_types.MAX_BODY)
                            current[path] = values.hash_bytes(raw)
                        if current != {p: member['blob_sha'] for p, member in baseline['old'].items()}:
                            _fail('initial_build_out_of_date')
                        scan = {}
                        observed = sync.schema3_sources(decision, handle, time.monotonic() + 45,
                                                         raw_scan=scan)
                        if 'snapshot' not in scan or not sync.schema3_head_same(
                                store, head, baseline, observed, handle):
                            _fail('initial_build_out_of_date')
                        if store.head(handle) != head:
                            _fail('initial_build_head_changed')
                        store_evidence = reader._schema3_store(state, project,
                            {'scopeId': identity['scopeId']}, decision,
                            identity['storePath'], worktree_id)
                        sealed = dict(install_id=identity['installId'], project=str(project),
                            project_node=reader._node(project), state=str(state),
                            state_node=reader._node(state), scope=identity['scopeId'],
                            attestation_sha256=digest, profile=row['vectorProfile']['profileId'],
                            settings=reader._sha(reader._raw(decision['recallSettings'])),
                            roots=decision['collections'], store_format='schema3',
                            capability=reader.SCHEMA3_CAPABILITY, checkout_policy=reader.SCHEMA3_POLICY,
                            vector_profile=spec['profile_id'], vector_recipe=spec['recipe_id'],
                            head_floor={'view_id': head['view_id'], 'epoch': head['epoch'],
                                        'ordinal': head_row[0]},
                            creation_epoch=(row['reader']['creation_epoch'] if row['reader'] else row['epoch'] + 1),
                            **store_evidence)
                        if not reader._schema3_shape(sealed):
                            _fail('initial_build_reader_invalid')
                    finally:
                        store.release(pin)
                finally:
                    store.close()
                path, db = journal._open(identity['journalPath'], write=True)
                with closing(db):
                    db.execute('BEGIN IMMEDIATE')
                    try:
                        current_row = journal._inspect_v1_row(db)
                        if current_row != {k: v for k, v in row.items() if k != 'journalPath'}:
                            _fail('initial_build_journal_changed')
                        if current_row['reader'] is None:
                            db.execute('INSERT INTO reader(id,record) VALUES(1,?)', (journal._canon(sealed),))
                            changed = db.execute("UPDATE selection SET epoch=epoch+1,vector_recipe=? "
                                "WHERE id=1 AND epoch=? AND state='prepared'",
                                (spec['recipe_id'], row['epoch'])).rowcount
                            if changed != 1:
                                _fail('initial_build_journal_changed')
                        elif current_row['reader'] != sealed:
                            _fail('initial_build_reader_changed')
                        journal._inspect_v1_row(db)
                        db.commit()
                    except BaseException:
                        db.rollback()
                        raise
                ledger_path = state / 'ledger.json'
                ledger = {'schema': 'memvane-install-ledger-v1', 'epoch': 1,
                          'phase': 'prepared', 'identity': identity}
                if ledger_path.exists() or ledger_path.is_symlink():
                    if _read_json(ledger_path) != ledger:
                        _fail('initial_build_ledger_changed')
                else:
                    _write(ledger_path, ledger, replace=False)
                return {'status': 'initial_build_finalized', 'phase': 'prepared',
                        'journalPath': str(path), 'routeFile': str(state / 'route.json'),
                        'generation': head['gen_id']}


def _checked_project(project_root):
    project = Path(project_root)
    if (not project.is_absolute() or project.is_symlink() or
            project.resolve(strict=True) != project):
        _fail('invalid_project_root')
    return project


def activate_origin_selection(project_root, journal_path, route_bytes):
    """Publish or recover the proved v1 selection under the install lock."""
    from memvane.install.api import _initial_build as build
    from memvane.journal.api import _install_journal as journal_module
    from memvane.journal.api import _reader as reader
    from memvane.sync import api as sync
    from memvane.wiki.intake import api as discovery
    from memvane.settings import api as project_dir
    from memvane.wiki.workspace.api import file_coordination as workspace_files
    from contextlib import closing
    from memvane.route.api import classify_v4 as _classify_v4
    from memvane.route.types import SELECTION_SCHEMA_V1
    _read_json, _write = _ledger._read_json, _ledger._write
    from memvane.journal.api import _api_reader_lifetime as reader_lifetime
    from memvane.runtime.api import sync_owner
    project = _checked_project(project_root)
    journal = Path(journal_path)
    state = _private_parent(journal.parent)
    if journal != state / 'journal.sqlite':
        _fail('initial_build_journal_changed')
    route, digest = journal_module._route(route_bytes, journal)
    with build.owner(state) as owned:
        if not owned:
            _fail('initial_build_busy')
        record = build.read_v1(state)
        if record is None or record['phase'] != 'ready':
            _fail('initial_build_not_ready')
        identity = record['identity']
        if (identity['projectRoot'] != str(project) or identity['journalPath'] != str(journal)
                or identity['routeSha256'] != digest
                or _read_private(state / 'route.json', 8192) != route_bytes):
            _fail('initial_build_route_changed')
        with sync_owner(Path(identity['storePath']), wait=False) as owner:
            if owner is None:
                _fail('owner_busy')
            with workspace_files.mutation_lock.lock(project), reader_lifetime(identity['installId'], exclusive=False):
                row = journal_module.inspect_journal(journal)
                ledger_path = state / 'ledger.json'
                ledger = _read_json(ledger_path)
                if (not isinstance(ledger, dict) or set(ledger) != {'schema', 'epoch', 'phase', 'identity'}
                        or ledger['schema'] != 'memvane-install-ledger-v1'
                        or ledger['identity'] != identity
                        or type(ledger['epoch']) is not int or ledger['epoch'] < 1
                        or ledger['phase'] not in ('prepared', 'selected')
                        or row['state'] not in ('prepared', 'selected')
                        or (ledger['phase'] == 'selected' and row['state'] != 'selected')
                        or row['reader'] is None or row['routeSha256'] != digest
                        or row['attestationSha256'] != identity['attestationSha256']
                        or row['projectInode'] != project.stat().st_ino
                        or row['storeInode'] != identity['storeNode'][1]
                        or any(row[key] != identity[key] for key in
                               ('projectRoot', 'installId', 'scopeId', 'storePath', 'storeIncarnation'))):
                    _fail('initial_build_ledger_changed')
                attestation, att_sha = journal_module._attestation(journal)
                if att_sha != identity['attestationSha256']:
                    _fail('worker_attestation_changed')
                journal_module._v1_provider_manifest(attestation, row['vectorProfile'])
                decision = _classify_v4(project, project_dir.settings_path(project), route)
                if decision.get('state') != 'candidate':
                    _fail('initial_build_route_changed')
                saved = row['reader']
                with reader._schema3_pin(saved, deadline=time.monotonic() + 45, pin_view=False) as (store, handle, head):
                    floor = saved['head_floor']
                    if (floor is None or head['view_id'] != floor['view_id']
                            or head['epoch'] != floor['epoch']):
                        _fail('initial_build_head_changed')
                    with store._session() as db:
                        baseline = sync.schema3_rows_baseline(store, db, handle, head)
                    scan = {}
                    observed = sync.schema3_sources(decision, handle, time.monotonic() + 45,
                                                     raw_scan=scan)
                    if 'snapshot' not in scan or not sync.schema3_head_same(
                            store, head, baseline, observed, handle):
                        _fail('initial_build_out_of_date')
                    discovery.seed_source_snapshot(decision, scan['snapshot'], generation=head['gen_id'])
                    if store.head(handle) != head:
                        _fail('initial_build_head_changed')
                marker = {'schema': SELECTION_SCHEMA_V1, 'projectRoot': str(project),
                          'installId': identity['installId'], 'routeSha256': digest}
                marker_raw = json.dumps(marker, sort_keys=True, ensure_ascii=False,
                                        separators=(',', ':')).encode('utf-8')
                _write_or_verify_private(project_dir.runtime_file(project, 'selection.json'), marker_raw)
                _write_or_verify_private(project_dir.runtime_file(project, 'route.json'), route_bytes)
                path, db = journal_module._open(journal, write=True)
                with closing(db):
                    db.execute('BEGIN IMMEDIATE')
                    try:
                        current = journal_module._inspect_v1_row(db)
                        if current != {k: v for k, v in row.items() if k != 'journalPath'}:
                            _fail('initial_build_journal_changed')
                        if current['state'] == 'prepared':
                            if db.execute("UPDATE selection SET state='selected',epoch=epoch+1 "
                                          "WHERE id=1 AND epoch=? AND state='prepared'", (row['epoch'],)).rowcount != 1:
                                _fail('initial_build_journal_changed')
                        journal_module._inspect_v1_row(db)
                        db.commit()
                    except BaseException:
                        db.rollback()
                        raise
                if ledger['phase'] == 'prepared':
                    ledger = {**ledger, 'phase': 'selected', 'epoch': ledger['epoch'] + 1}
                    _write(ledger_path, ledger, replace=True)
                proof = journal_module.verify_selected_route(decision)
                try:
                    from memvane.learning.api import request_selector_maintenance
                    request_selector_maintenance(decision, force=True, owner=owner)
                except (OSError, ValueError, RuntimeError):
                    pass  # Selection is complete; a later SessionStart can retry.
                return {'status': 'selected', 'phase': 'selected', 'journalPath': str(path),
                        'selectionEpoch': proof['selectionEpoch'],
                        'syncWake': _request_build_wake(str(project)),
                        'gitignoreEntries': ['.memvane/route.json', '.memvane/selection.json']}


def recover_origin_selection(project_root, journal_path, route_bytes):
    """Finish the same partially published or withdrawn v1 selection."""
    _read_json = _ledger._read_json
    state = Path(journal_path).parent
    ledger = _read_json(state / 'ledger.json')
    if ledger.get('phase') in ('restoring', 'restored'):
        return _restore_v1_locked(_checked_project(project_root), state)
    return activate_origin_selection(project_root, journal_path, route_bytes)


def restore_origin_selection(*, project_root, state_root):
    """Withdraw the selected route only before its first selected write."""
    project = _checked_project(project_root)
    state = _private_parent(state_root)
    with _install_lock(state):
        return _restore_v1_locked(project, state)
def _restore_v1_locked(project, state):
    from memvane.install.api import _initial_build as build
    from memvane.journal.api import _install_journal as journal
    from contextlib import closing
    from memvane.route.types import SELECTION_SCHEMA_V1
    _read_json, _write, _fsync_dir = _ledger._read_json, _ledger._write, _ledger._fsync_dir
    from memvane.journal.api import _api_reader_lifetime as reader_lifetime
    from memvane.runtime.api import sync_owner
    from memvane.wiki.workspace.api import file_coordination as workspace_files
    with build.owner(state) as owned:
        if not owned:
            _fail('initial_build_busy')
        record = build.read_v1(state)
        if record is None or record['identity']['projectRoot'] != str(project):
            _fail('initial_build_project_mismatch')
        identity = record['identity']
        ledger_path = state / 'ledger.json'
        ledger = _read_json(ledger_path)
        if (not isinstance(ledger, dict) or set(ledger) != {'schema', 'epoch', 'phase', 'identity'}
                or ledger['schema'] != 'memvane-install-ledger-v1' or ledger['identity'] != identity
                or type(ledger['epoch']) is not int or ledger['epoch'] < 1
                or ledger['phase'] not in ('prepared', 'selected', 'restoring', 'restored')):
            _fail('initial_build_ledger_changed')
        path = state / 'journal.sqlite'
        route_raw = _read_private(state / 'route.json', 8192)
        if hashlib.sha256(route_raw).hexdigest() != identity['routeSha256']:
            _fail('initial_build_route_changed')
        with sync_owner(Path(identity['storePath']), wait=False) as owner:
            if owner is None:
                _fail('owner_busy')
            with workspace_files.mutation_lock.lock(project), reader_lifetime(identity['installId'], exclusive=True):
                row = journal.inspect_journal(path)
                if (row['routeSha256'] != identity['routeSha256']
                        or row['attestationSha256'] != identity['attestationSha256']
                        or any(row[k] != identity[k] for k in
                               ('projectRoot', 'installId', 'scopeId', 'storePath', 'storeIncarnation'))):
                    _fail('initial_build_journal_changed')
                if row['newWriteSeen']:
                    _fail('rollback_forward_only')
                project_paths = [project_dir.runtime_file(project, name)
                                 for name in ('route.json', 'selection.json')]
                present = any(item.exists() or item.is_symlink() for item in project_paths)
                if ledger['phase'] == 'prepared' and row['state'] == 'prepared' and not present:
                    return {'status': 'not_selected', 'changesApplied': 0}
                if ((ledger['phase'] == 'selected' and row['state'] != 'selected')
                        or (ledger['phase'] == 'restored' and row['state'] != 'restored')
                        or (ledger['phase'] == 'restoring' and row['state'] not in ('prepared', 'selected', 'restored'))):
                    _fail('restore_state_invalid')
                if ledger['phase'] in ('prepared', 'selected'):
                    ledger = {**ledger, 'phase': 'restoring', 'epoch': ledger['epoch'] + 1}
                    _write(ledger_path, ledger, replace=True)
                if row['state'] in ('prepared', 'selected'):
                    _, db = journal._open(path, write=True)
                    with closing(db):
                        db.execute('BEGIN IMMEDIATE')
                        try:
                            current = journal._inspect_v1_row(db)
                            if current != {k: v for k, v in row.items() if k != 'journalPath'}:
                                _fail('initial_build_journal_changed')
                            if db.execute("UPDATE selection SET state='restored',epoch=epoch+1 "
                                          "WHERE id=1 AND state=? AND new_write_seen=0 AND epoch=?",
                                          (row['state'], row['epoch'])).rowcount != 1:
                                _fail('restore_state_invalid')
                            journal._inspect_v1_row(db)
                            db.commit()
                        except BaseException:
                            db.rollback()
                            raise
                marker = {'schema': SELECTION_SCHEMA_V1, 'projectRoot': str(project),
                          'installId': identity['installId'], 'routeSha256': identity['routeSha256']}
                marker_raw = json.dumps(marker, sort_keys=True, ensure_ascii=False,
                                        separators=(',', ':')).encode('utf-8')
                for name, expected in (('route.json', route_raw), ('selection.json', marker_raw)):
                    target = project_dir.runtime_file(project, name)
                    if not target.exists() and not target.is_symlink():
                        continue
                    before = target.lstat()
                    if _read_private(target, 8192) != expected or not os.path.samestat(before, target.lstat()):
                        _fail('restore_project_pointer_changed')
                    target.unlink()
                    _fsync_dir(target.parent)
                build.retire_project_pointer(identity)
                if ledger['phase'] != 'restored':
                    ledger = {**ledger, 'phase': 'restored', 'epoch': ledger['epoch'] + 1}
                    _write(ledger_path, ledger, replace=True)
    return {'status': 'restored', 'changesApplied': 1}


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

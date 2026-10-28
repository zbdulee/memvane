#!/usr/bin/env python3
"""Installer-only Laya onboarding: plan, provider review, build, bind, activate.

The host must install and load the exact Git candidate first. Staging seals
that *installed* root; it never installs a plugin or changes a project route.
A new project is planned from its minimal settings (``plan-fresh-wiki``),
registered as a durable inactive initial build (``laya_initial_build``) that
the runtime ``laya-sync`` wake advances in bounded steps, bound by an explicit
``finalize-build`` (``laya_fresh_wiki_origin`` ledger and project marker) and
activated by a separate ``activate``, which then queues one selected
``laya-sync`` wake. An explicit ``retire-origin`` withdraws a bound build that
was never selected (deleting nothing) so a fresh install with its own state
root can be prepared. An explicit ``restore`` withdraws the route before its
first selected write. A project still bound to a QMD-origin ledger
(``qmd-cutover.json``) is refused as ``qmd_origin_detach_required``.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager, nullcontext
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
import tempfile

from laya_install_journal import (create_journal, inspect_journal,
                                  preflight_product_candidate,
                                  stage_production_attestation)
from laya_product_bundle import (adopt_host_loaded_plugin,
                                 inspect_full_plugin_candidate,
                                 PRODUCTION_ATTESTATION_SCHEMA,
                                 validate_production_provider_attestation)


class InstallBlocked(RuntimeError):
    pass


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


def _state_bases():
    """State bases an existing install may live under (current, pre-rename)."""
    import memvane_paths
    return (memvane_paths.state_base(), memvane_paths.legacy_state_base())


def _prepare_state_root(path, install_id):
    """Create only a fresh per-install directory under the local state base."""
    root = Path(path)
    if root.exists() or root.is_symlink():
        return _private_parent(root)
    import memvane_paths
    base = memvane_paths.state_base()
    if (not root.is_absolute() or root.parent != base or
            type(install_id) is not str or not re.fullmatch(r'[0-9a-f]{32}', install_id) or
            root.name != install_id):
        _fail('unsupported_state_root_creation')
    cursor = Path.home()
    for name in (*memvane_paths.STATE_PARTS, install_id):
        cursor = cursor / name
        if not cursor.exists() and not cursor.is_symlink():
            os.mkdir(cursor, 0o700)
        info = cursor.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or
                stat.S_IMODE(info.st_mode) & 0o022):
            _fail('unsafe_state_root_ancestor')
    return _private_parent(root)


@contextmanager
def _install_lock(parent):
    target = parent / 'laya-install.lock'
    fd = os.open(target, os.O_RDWR | os.O_CREAT |
                 getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
                info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o077):
            _fail('unsafe_install_lock')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise InstallBlocked('install_busy') from exc
        yield
    finally:
        os.close(fd)


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


def _json_input(path):
    raw = _read_private(path, 1024 * 1024)
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                _fail('duplicate_install_input_key')
            result[key] = value
        return result
    def nonfinite(_value):
        _fail('nonfinite_install_input')
    return json.loads(raw.decode('utf-8'), object_pairs_hook=unique,
                      parse_constant=nonfinite)


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


# One synchronous port scan (laya_index_source_port defaults). This is the
# work bound of the in-process origin path only, never a corpus admission: a
# source above it is planned as a background initial build instead.
def plan_fresh_wiki_origin(project_root, *, _bound_marker=False):
    """Review a new wiki-only Laya generation from minimal project settings.

    The canonical ``.auto-context/settings.json`` must be minimal
    (``{name, skipPaths}``); any other shape refuses as
    ``minimal_settings_required``. The settings file is never rewritten; the
    installer builds a separate private generation from the canonical wiki
    root. No QMD database is read, copied, or selected.
    """
    import config
    from laya_index_host_route import (_multi_settings_supported,
                                       _multi_compile_supported,
                                       selected_collection_roots,
                                       _source_for_collection, _read_regular,
                                       MAX_SETTINGS_BYTES, RECORD,
                                       QMD_SELECTION_MARKER)
    project = Path(project_root)
    if (not project.is_absolute() or project.is_symlink() or
            project.resolve(strict=True) != project):
        _fail('invalid_project_root')
    auto = project / '.auto-context'
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
    source_settings = config.expand_minimal_settings(parsed)
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
    if (raw_root != project or wiki_root != project / '.auto-context' / 'wiki'
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
    from laya_index_host_route import settings_identity_digest
    return settings_identity_digest(plan['projectRoot'], plan['collections'])


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _fail('duplicate_settings_key')
        result[key] = value
    return result


def review_local_provider(*, source_root, plugin_root, state_root, commit_id,
                          model_path=None, dimension=None):
    """Read-only exact local artifact proposal for human/release review.

    The executable inventory is the active Laya embedding runtime
    (``laya_embed_runtime``): its node, node-llama-cpp entry, every packaged
    native binary of the pinned Metal build and its GGUF model. The runtime
    is fully re-hashed; a missing or changed file refuses. A ``model_path``
    argument, when given, must name the runtime's own model file. Review
    neither asserts that native execution works nor issues approval: the
    caller must accept the returned digest, and prepare repeats every check.
    """
    import platform
    import laya_embed_runtime
    from laya_index_vector import profile_identity
    from laya_local_embed_provider import (DEFAULT_EMBED_MODEL_URI,
                                           _hash_file, recipe_fingerprint)
    from laya_product_bundle import (_git, _git_blob_oid, _read, COMMIT,
                                     MAX_FILE, _canonical)
    if platform.system() != 'Darwin' or platform.machine() != 'arm64':
        _fail('reviewed_native_platform_unsupported')
    source, root, state = map(Path, (source_root, plugin_root, state_root))
    if (not all(path.is_absolute() and not path.is_symlink()
                for path in (source, root, state)) or
            source.resolve(strict=True) == root.resolve(strict=True) or
            type(commit_id) is not str or not COMMIT.fullmatch(commit_id) or
            str(state.resolve(strict=False)) != str(state) or
            len(str(state / 'runtime' / 'laya-embed.sock').encode()) > 100):
        _fail('provider_review_paths_invalid')
    # prepare creates only ~/.local/state/memvane/<install id>; refuse before a digest.
    import memvane_paths
    if (not state.exists() and not state.is_symlink() and
            (state.parent != memvane_paths.state_base() or
             not re.fullmatch(r'[0-9a-f]{32}', state.name))):
        _fail('unsupported_state_root_creation')
    if (_git(source, 'rev-parse', 'HEAD').decode().strip() != commit_id or
            _git(source, 'rev-parse', '--show-toplevel').decode().strip() != str(source)):
        _fail('provider_review_commit_changed')
    object_format = _git(source, 'rev-parse', '--show-object-format').decode().strip()
    artifacts = {}
    for name in ('core/laya_local_embed_provider.py',
                 'core/laya_local_embed_worker.mjs'):
        original, _ = _read(source / name, MAX_FILE)
        installed, _ = _read(root / name, MAX_FILE)
        committed = _git(source, 'rev-parse', f'{commit_id}:{name}').decode().strip()
        if original != installed or _git_blob_oid(original, object_format) != committed:
            _fail('provider_review_installed_blob_changed')
        artifacts[name] = hashlib.sha256(installed).hexdigest()
    try:
        runtime = laya_embed_runtime.select()
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
    provider_sha = artifacts['core/laya_local_embed_provider.py']
    worker_sha = artifacts['core/laya_local_embed_worker.mjs']
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
               'providerRelativePath': 'core/laya_local_embed_provider.py'}
    manifest = {'approvedProviderId': approved_id,
                'providerArtifactSha256': provider_sha,
                'providerRelativePath': profile['providerRelativePath'],
                'providerPath': str(root / profile['providerRelativePath']),
                'workerPath': str(root / 'core/laya_local_embed_worker.mjs'),
                'workerSha256': worker_sha,
                'serviceSocketPath': str(state / 'runtime/laya-embed.sock'),
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
                'sourceCommit': commit_id, 'pluginRoot': str(root),
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


def _unfinished_catalog(path):
    """True for a catalog file left with no tables by an interrupted create.

    LayaSourceCatalog creates the file first and commits its schema later, so
    a crash in between leaves a file it refuses to open. A valid catalog
    always has tables, so an empty schema can only be that leftover.
    """
    import sqlite3
    from contextlib import closing
    try:
        with closing(sqlite3.connect(f'file:{path}?mode=ro', uri=True)) as db:
            return db.execute("SELECT COUNT(*) FROM sqlite_master "
                              "WHERE type='table'").fetchone()[0] == 0
    except sqlite3.Error:
        return False


def prepare_capacity_stores(db_path, roots, scope, db_incarnation):
    """Create or verify the capacity catalog and binding store beside ``db_path``.

    Their presence switches the selected worker and host recall to the
    capacity path (``laya_index_runtime.capacity_store_paths``). Idempotent:
    an existing store is opened, which checks its scope, roots and
    incarnation, and a mismatch fails as ``capacity_store_mismatch`` (for
    example after the candidate DB was rebuilt); nothing is deleted then. A
    catalog left with no schema by an interrupted create is removed and
    created again. Both end as private regular files.
    """
    from contextlib import closing
    from laya_index_a import IndexContractError
    from laya_index_capacity import GenerationBindingStore
    from laya_index_runtime import (BINDING_STORE_SUFFIX, CATALOG_SUFFIX,
                                    capacity_store_paths)
    from laya_source_catalog import LayaSourceCatalog
    db = Path(db_path)
    catalog = db.with_name(db.name + CATALOG_SUFFIX)
    store = db.with_name(db.name + BINDING_STORE_SUFFIX)
    roots = dict(roots)
    if (os.path.lexists(catalog) and not catalog.is_symlink() and catalog.is_file()
            and _unfinished_catalog(catalog)):
        for suffix in ('', '-wal', '-shm', '-journal'):
            Path(str(catalog) + suffix).unlink(missing_ok=True)
    try:
        with closing(LayaSourceCatalog(catalog, roots, scope,
                                       create=not os.path.lexists(catalog))):
            pass
        with closing(GenerationBindingStore(store, scope=scope,
                                            db_incarnation=db_incarnation, roots=roots,
                                            create=not os.path.lexists(store))):
            pass
    except IndexContractError:
        _fail('capacity_store_mismatch')
    for path in (catalog, store):
        if not path.is_symlink() and path.is_file():
            os.chmod(path, 0o600)
    paths = capacity_store_paths(db)
    if paths != (str(catalog), str(store)):
        _fail('capacity_store_unavailable')
    return paths


def _publish_claim_queue(queue, incarnation):
    """Publish a complete, closed claim queue without clobbering a final path.

    ClaimQueue.create initializes SQLite in place after its exclusive open, so
    process death there would leave a partial final queue. Build privately and
    hard-link only a verified single-file DB, as the QMD candidate does. A
    killed private stage is retained under its own name and never reused.
    """
    from contextlib import closing
    from laya_index_claim_queue import ClaimQueue

    with tempfile.TemporaryDirectory(prefix='.laya-claims-', dir=queue.parent) as work:
        built = Path(work) / 'claims.sqlite'
        with closing(ClaimQueue.create(built, index_incarnation=incarnation)):
            pass
        with closing(ClaimQueue.open(built)) as opened:
            if opened.binding() != incarnation:
                _fail('queue_stage_binding_mismatch')
        # ClaimQueue.open restores WAL mode. Switch the closed private DB back
        # to a self-contained rollback journal before publishing it: a fresh
        # read-only opener cannot read a WAL-mode header without sidecars.
        with closing(sqlite3.connect(built)) as db:
            mode = db.execute('PRAGMA journal_mode=DELETE').fetchone()
            if mode is None or mode[0].lower() != 'delete':
                _fail('queue_stage_not_self_contained')
        # Closing the last WAL connection checkpoints it; a nonempty sidecar
        # could still carry rows, so never discard one. Only this private
        # stage reaches here.
        for suffix in ('-wal', '-journal'):
            sidecar = Path(str(built) + suffix)
            if sidecar.exists() and sidecar.stat().st_size:
                _fail('queue_stage_not_self_contained')
        for suffix in ('-wal', '-shm', '-journal'):
            Path(str(built) + suffix).unlink(missing_ok=True)
        with built.open('rb') as stream:
            os.fsync(stream.fileno())
        # Preserve the final-path guard from ClaimQueue.create. A stale WAL
        # beside a missing main file must never attach to the fresh queue.
        if any(Path(str(queue) + suffix).exists() or
               Path(str(queue) + suffix).is_symlink()
               for suffix in ('-wal', '-shm', '-journal')):
            _fail('partial_queue_requires_recovery')
        try:
            os.link(built, queue)
        except FileExistsError as exc:
            raise InstallBlocked('partial_queue_requires_recovery') from exc
        # Drop the private name at once; cutover and sync owner fences
        # require the published queue to be a single-link file.
        built.unlink()
        directory = os.open(queue.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


def _finish_interrupted_claim_queue_link(queue, incarnation):
    """Drop only our verified private hard link after an interrupted publish."""
    from contextlib import closing
    from laya_index_claim_queue import ClaimQueue

    final = queue.lstat()
    if final.st_nlink == 1:
        return
    if not stat.S_ISREG(final.st_mode) or final.st_nlink != 2:
        _fail('partial_queue_requires_recovery')
    with closing(ClaimQueue.open(queue)) as opened:
        if opened.binding() != incarnation:
            _fail('partial_queue_requires_recovery')
    for parent in queue.parent.iterdir():
        if not parent.name.startswith('.laya-claims-'):
            continue
        info = parent.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o700):
            continue
        built = parent / 'claims.sqlite'
        try:
            staged = built.lstat()
        except FileNotFoundError:
            continue
        if (stat.S_ISREG(staged.st_mode) and staged.st_uid == os.getuid()
                and staged.st_dev == final.st_dev and staged.st_ino == final.st_ino
                and staged.st_nlink == 2):
            built.unlink()
            directory = os.open(queue.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            return
    _fail('partial_queue_requires_recovery')


def prepare_fresh_wiki_origin(*, project_root, source_root, plugin_root, state_root,
                              commit_id, expected_proposal_sha256, install_id,
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
    if Path(source_root).resolve(strict=True) == Path(plugin_root).resolve(strict=True):
        _fail('source_checkout_cannot_be_installed_root')
    state = _prepare_state_root(state_root, install_id)
    journal = state / 'install-journal.sqlite'
    socket = state / 'runtime' / 'laya-embed.sock'
    if len(str(socket).encode('utf-8')) > 100:
        _fail('install_socket_path_too_long')
    with _install_lock(state):
        plan = plan_fresh_wiki_origin(project_root)
        if plan['proposalSha256'] != expected_proposal_sha256:
            _fail('reviewed_origin_plan_changed')
        review = review_local_provider(
            source_root=source_root, plugin_root=plugin_root,
            state_root=state, commit_id=commit_id,
            model_path=model_path, dimension=dimension)
        if review['proposalSha256'] != accepted_provider_sha256:
            _fail('reviewed_provider_proposal_changed')
        return _register_initial_build(
            plan=plan, reviewed_provider=review['reviewedProvider'],
            provider_proposal_sha256=accepted_provider_sha256,
            source_root=source_root, plugin_root=plugin_root, state=state,
            journal=journal, socket=socket, commit_id=commit_id,
            install_id=install_id)


def _adopt_and_attest(*, source_root, plugin_root, commit_id, reviewed_provider,
                      state, journal, socket):
    """Adopt the host-loaded exact bundle and stage its provider attestation."""
    proof = adopt_host_loaded_plugin(source_root, plugin_root,
                                     commit_id=commit_id,
                                     vector_profile=reviewed_provider['vectorProfile'])
    profile = proof['vectorProfile']
    manifest = reviewed_provider['providerManifest']
    if manifest.get('serviceSocketPath') != str(socket):
        _fail('provider_socket_not_bound_to_install')
    runtime = state / 'runtime'
    if not runtime.exists() and not runtime.is_symlink():
        os.mkdir(runtime, 0o700)
    _private_parent(runtime)
    attestation = {'schema': PRODUCTION_ATTESTATION_SCHEMA,
                   'sourceCommit': proof['sourceCommit'],
                   'bundleManifestSha256': proof['bundleManifestSha256'],
                   'ownershipManifestSha256': proof['ownershipManifestSha256'],
                   'journalPath': str(journal), 'vectorProfile': profile,
                   'providerManifest': manifest,
                   'dependencyClosure': reviewed_provider['dependencyClosure']}
    validate_production_provider_attestation(profile, proof, attestation)
    stage_production_attestation(journal, proof, manifest,
                                 reviewed_provider['dependencyClosure'])
    return proof, profile, manifest


BUILD_CANDIDATE = 'laya-build.sqlite'
BUILD_QUEUE = 'laya-build-claims.sqlite'


def _empty_or_bound_candidate(path, scope):
    """Open the private build DB; only bound generations may exist in it."""
    from contextlib import closing
    from laya_index_a import LayaIndexA
    from laya_index_capacity import generation_digest
    with closing(LayaIndexA(path)) as index:
        incarnation = index.incarnation()
        index.db.execute('BEGIN')
        try:
            rows = index.db.execute('SELECT gen,scope FROM generations').fetchall()
            index.db.rollback()
        except BaseException:
            index.db.rollback()
            raise
    if any(row_scope != scope or generation_digest(gen) is None for gen, row_scope in rows):
        _fail('partial_candidate_requires_recovery')
    return incarnation


def _request_build_wake(project_root):
    """Queue the selected-project wake kind; the worker finds the build."""
    try:
        import runtime_work
        queued = runtime_work.request('laya-sync', str(project_root))
    except (OSError, ValueError) as exc:
        return {'status': 'wake_unavailable', 'reason': type(exc).__name__}
    return queued if queued is not None else {'status': 'wake_skipped_in_fixture'}


def _register_initial_build(*, plan, reviewed_provider, provider_proposal_sha256,
                            source_root, plugin_root, state, journal, socket,
                            commit_id, install_id):
    """Create every store of an inactive initial build and register it once.

    Under the install lock, in order, each step verified-or-created so an
    interrupted run is resumed by repeating it: exact bundle adoption and
    attestation; an empty private A-int DB (no-clobber); its claim queue
    (privately built, hard-linked); the source catalog and binding store
    bound to that DB incarnation; the reviewed route bytes; the
    ``candidate_ready`` journal; the build record; the project pointer; and
    one ``laya-sync`` wake. Nothing reads source bytes, loads a model,
    copies QMD or writes a project file. The route is published only by a
    later explicit activation.
    """
    import laya_initial_build as initial_build
    from laya_index_a import LayaIndexA
    from laya_index_host_route import SCHEMA_V3
    roots = {item['name']: item['sourceRoot'] for item in plan['collections']}
    proof, profile, _manifest = _adopt_and_attest(
        source_root=source_root, plugin_root=plugin_root, commit_id=commit_id,
        reviewed_provider=reviewed_provider,
        state=state, journal=journal, socket=socket)
    candidate = state / BUILD_CANDIDATE
    queue = state / BUILD_QUEUE
    if not candidate.exists() and not candidate.is_symlink():
        LayaIndexA(candidate, create=True).close()
    incarnation = _empty_or_bound_candidate(candidate, plan['scopeId'])
    if queue.exists() or queue.is_symlink():
        from contextlib import closing
        from laya_index_claim_queue import ClaimQueue
        _finish_interrupted_claim_queue_link(queue, incarnation)
        with closing(ClaimQueue.open(queue)) as opened:
            if opened.binding() != incarnation:
                _fail('partial_queue_requires_recovery')
    else:
        _publish_claim_queue(queue, incarnation)
    from laya_index_host_route import wiki_index_roots
    catalog, store = prepare_capacity_stores(candidate, wiki_index_roots(plan['collections']),
                                             plan['scopeId'], incarnation)
    # The route's generation is the install identity token; the bound index
    # generation it will serve is chosen by the build and bound at finalize.
    generation = 'g-' + hashlib.sha256(
        ('laya-origin-generation:' + install_id + ':' +
         plan['proposalSha256']).encode()).hexdigest()[:32]
    route = {'schema': SCHEMA_V3, 'status': 'active',
             'projectRoot': plan['projectRoot'],
             'settingsSha256': _settings_identity(plan),
             'dbPath': str(candidate), 'queuePath': str(queue),
             'dbIncarnation': incarnation, 'scopeId': plan['scopeId'],
             'collections': plan['collections'],
             'pluginRoot': str(plugin_root), 'journalPath': str(journal),
             'installId': install_id, 'generation': generation,
             'bundleManifestSha256': proof['bundleManifestSha256'],
             'ownershipManifestSha256': proof['ownershipManifestSha256'],
             'vectorProfile': profile}
    route_bytes = json.dumps(route, sort_keys=True, ensure_ascii=False,
                             separators=(',', ':')).encode('utf-8')
    if len(route_bytes) > 8192:
        _fail('origin_route_exceeds_limit')
    route_file = state / 'reviewed-route.json'
    _write_or_verify_private(route_file, route_bytes)
    staged = stage_host_loaded_candidate(
        source_root=source_root, plugin_root=plugin_root,
        state_root=state, commit_id=commit_id, route_bytes=route_bytes,
        reviewed_provider=reviewed_provider, _already_locked=True)
    identity = {'projectRoot': plan['projectRoot'], 'stateRoot': str(state),
                'installId': install_id, 'scopeId': plan['scopeId'],
                'collections': plan['collections'], 'candidateDb': str(candidate),
                'queueDb': str(queue), 'catalogDb': catalog, 'bindingStore': store,
                'dbIncarnation': incarnation, 'journalPath': str(journal),
                'routeFile': str(route_file),
                'routeSha256': hashlib.sha256(route_bytes).hexdigest(),
                'attestationSha256': staged['attestationSha256'],
                'vectorProfileId': profile['profileId'],
                'proposalSha256': plan['proposalSha256'],
                'providerProposalSha256': provider_proposal_sha256,
                'originMode': 'fresh_wiki_only'}
    if identity['routeSha256'] != staged['pendingRouteSha256']:
        _fail('staged_route_changed')
    record = initial_build.create_or_verify(identity)
    pointer = initial_build.register_project(identity)
    return {'status': 'initial_build_registered',
            'proposalSha256': plan['proposalSha256'],
            'journalPath': str(journal),
            'pendingRouteSha256': staged['pendingRouteSha256'],
            'candidateDb': str(candidate), 'routeFile': str(route_file),
            'buildRecord': str(initial_build.record_path(state)),
            'buildPointer': pointer, 'buildPhase': record['phase'],
            'wake': _request_build_wake(plan['projectRoot'])}


def initial_build_status(state_root):
    """Read-only build record plus the catalog's durable scan backlog."""
    import laya_initial_build as initial_build
    from contextlib import closing
    from laya_source_catalog import LayaSourceCatalog
    from laya_index_host_route import wiki_index_roots
    state = _private_parent(state_root)
    record = initial_build.read(state)
    if record is None:
        _fail('initial_build_missing')
    identity = record['identity']
    roots = wiki_index_roots(identity['collections'])
    with closing(LayaSourceCatalog(identity['catalogDb'], roots, identity['scopeId'],
                                   wiki_only=True)) as catalog:
        scan = catalog.status()
    return {'status': 'initial_build', 'phase': record['phase'],
            'progress': record['progress'], 'blocked': record['blocked'],
            'sealed': record['sealed'], 'freshness': record['freshness'],
            'freshnessSatisfied': initial_build.freshness_satisfied(record),
            'catalog': scan}


def request_initial_build_rescan(state_root):
    """Explicitly request a scan (also clears a source block) and wake the build."""
    import laya_initial_build as initial_build
    from contextlib import closing
    from laya_source_catalog import LayaSourceCatalog
    from laya_index_host_route import wiki_index_roots
    state = _private_parent(state_root)
    with _install_lock(state), initial_build.owner(state) as owned:
        if not owned:
            _fail('initial_build_busy')
        record = initial_build.read(state)
        if record is None:
            _fail('initial_build_missing')
        if record['phase'] == 'upgrading':
            _fail('inactive_bundle_upgrade_pending')
        identity = record['identity']
        roots = wiki_index_roots(identity['collections'])
        with closing(LayaSourceCatalog(identity['catalogDb'], roots,
                                       identity['scopeId'], wiki_only=True)) as catalog:
            record = initial_build.request_freshness(record, catalog)
    return {'status': 'rescan_requested', 'phase': record['phase'],
            'freshness': record['freshness'],
            'wake': _request_build_wake(identity['projectRoot'])}


def finalize_initial_build(*, project_root, state_root):
    """Bind a sealed, fresh initial build to its selection ledger; never activates.

    Two calls. The first finds no satisfied freshness, requests a scan that
    starts now and wakes the build (``freshness_pending``). Once the build
    has re-sealed from a scan started after that request, the next call
    re-checks the reviewed plan, the inactive journal and attestation and
    the live sealed evidence, then binds through ``laya_fresh_wiki_origin``
    (ledger ``prepared``, then the project marker). Unknown freshness, a
    pending or blocked scan, or any changed evidence refuses.

    Repeating it is safe. After an interruption between the ledger, the
    marker and the build record, it re-proves the same binding, completes
    what is missing and records ``bound``. Once the ledger moved on it only
    reports the phase.
    """
    import laya_initial_build as initial_build
    state = _private_parent(state_root)
    with _install_lock(state), initial_build.owner(state) as owned:
        if not owned:
            _fail('initial_build_busy')
        record = initial_build.read(state)
        if record is None:
            _fail('initial_build_missing')
        if record['phase'] == 'upgrading':
            _fail('inactive_bundle_upgrade_pending')
        return _finalize_fresh_wiki_locked(record, project_root, state)


def _finalize_fresh_wiki_locked(record, project_root, state):
    """Bind a fresh wiki build without inventing a QMD source or backup."""
    import laya_initial_build as initial_build
    import laya_fresh_wiki_origin as fresh
    from contextlib import closing
    from laya_index_host_route import wiki_index_roots
    from laya_index_runtime import LayaIndexRuntime
    from laya_install_journal import inspect_journal
    from laya_source_catalog import LayaSourceCatalog
    identity = record['identity']
    if identity['projectRoot'] != str(project_root):
        _fail('initial_build_project_mismatch')
    if record['phase'] == 'retired':
        _fail('initial_build_retired')
    if record['phase'] == 'blocked':
        return {'status': 'initial_build_blocked', 'blocked': record['blocked']}
    ledger_path = state / fresh.LEDGER
    cutover = fresh._load(ledger_path) if ledger_path.exists() or ledger_path.is_symlink() else None
    if cutover is not None and (record['sealed'] is None or
            cutover['indexGeneration'] != record['sealed']['generation'] or
            cutover['candidateDb'] != identity['candidateDb']):
        _fail('fresh_wiki_selection_already_exists')
    if cutover is not None and cutover['phase'] != 'prepared':
        if record['phase'] != 'bound':
            record = initial_build.advance(record, phase='bound')
        return {'status': 'cutover_' + cutover['phase'],
                'cutoverPhase': cutover['phase'],
                'journalPath': identity['journalPath'],
                'generation': cutover['indexGeneration'],
                'buildPhase': record['phase']}
    if record['phase'] == 'bound' and cutover is None:
        _fail('initial_build_bound_without_cutover')
    roots = wiki_index_roots(identity['collections'])
    if cutover is None and not initial_build.freshness_satisfied(record):
        if record['phase'] == 'building' and record['freshness'] is not None:
            return {'status': 'freshness_pending', 'phase': record['phase'],
                    'progress': record['progress'],
                    'wake': _request_build_wake(identity['projectRoot'])}
        with closing(LayaSourceCatalog(identity['catalogDb'], roots,
                                       identity['scopeId'], wiki_only=True)) as catalog:
            record = initial_build.request_freshness(record, catalog)
        return {'status': 'freshness_pending', 'phase': record['phase'],
                'freshness': record['freshness'],
                'wake': _request_build_wake(identity['projectRoot'])}
    plan = plan_fresh_wiki_origin(project_root, _bound_marker=cutover is not None)
    if plan['proposalSha256'] != identity['proposalSha256']:
        _fail('reviewed_origin_plan_changed')
    journal = inspect_journal(identity['journalPath'])
    if (journal['state'] != 'candidate_ready' or
            journal['pendingRouteSha256'] != identity['routeSha256'] or
            journal['newWriteSeen']):
        _fail('initial_build_journal_changed')
    approval = preflight_product_candidate(identity['journalPath'])
    if approval['attestationSha256'] != identity['attestationSha256']:
        _fail('initial_build_attestation_changed')
    sealed = record['sealed']
    with LayaIndexRuntime(source_root=roots, scope=identity['scopeId'],
                          db_path=identity['candidateDb'],
                          queue_path=identity['queueDb'],
                          db_incarnation=identity['dbIncarnation'],
                          catalog_path=identity['catalogDb'],
                          binding_store_path=identity['bindingStore']) as runtime:
        live = initial_build.evidence(runtime, sealed['generation'],
                                      identity['vectorProfileId'])
    if live != sealed:
        _fail('initial_build_candidate_changed')
    route_bytes = _read_private(identity['routeFile'], 8192)
    if hashlib.sha256(route_bytes).hexdigest() != identity['routeSha256']:
        _fail('initial_build_route_changed')
    row = fresh.bind(project_root=Path(project_root),
                     journal_path=identity['journalPath'],
                     route_bytes=route_bytes, sealed=sealed, identity=identity)
    if record['phase'] != 'bound':
        record = initial_build.advance(record, phase='bound')
    return {'status': 'cutover_' + row['phase'], 'cutoverPhase': row['phase'],
            'journalPath': identity['journalPath'],
            'candidateDb': identity['candidateDb'],
            'routeFile': identity['routeFile'],
            'generation': sealed['generation'], 'buildPhase': record['phase']}


def retire_drifted_origin(*, project_root, state_root):
    """Retire a bound, never selected initial build; select nothing.

    This explicit step makes room for a fresh install when a bound build
    can no longer be activated. Under the install lock and the build owner
    the selection ledger moves ``prepared`` -> ``retired`` and its own project
    marker is withdrawn (``laya_fresh_wiki_origin.retire``); the project's
    wake pointer of this build is then archived and released
    (``retire_project_pointer``). The old state root keeps the candidate,
    queue, catalog, binding store, journal, ledger, route and build record.
    The next install is a new ``review-provider`` and ``prepare-origin`` with
    a new install id and state root.
    """
    import laya_initial_build as initial_build
    import laya_fresh_wiki_origin as fresh
    project = _checked_project(project_root)
    state = _private_parent(state_root)
    with _install_lock(state), initial_build.owner(state) as owned:
        if not owned:
            _fail('initial_build_busy')
        record = initial_build.read(state)
        if record is None:
            _fail('initial_build_missing')
        identity = record['identity']
        journal = state / 'install-journal.sqlite'
        if (identity['projectRoot'] != str(project) or
                identity['stateRoot'] != str(state) or
                identity['journalPath'] != str(journal)):
            _fail('initial_build_project_mismatch')
        if record['phase'] != 'bound':
            _fail('fresh_wiki_build_not_bound')
        row = fresh.retire(project, journal)
        pointer = initial_build.retire_project_pointer(identity)
        return {'status': 'fresh_wiki_binding_retired', 'changesApplied': 1,
                'stateRoot': str(state), 'buildPointer': pointer,
                'cutoverPhase': row['phase'],
                'nextRequires': 'new_install_id_and_state_root'}


UNUSED_RETIREMENT = 'unused-build-retirement'
UNUSED_RECEIPT_SCHEMA = 'laya-unused-build-retirement-v1'
INACTIVE_RETIREMENT = 'inactive-build-retirement'
INACTIVE_RECEIPT_SCHEMA = 'laya-inactive-build-retirement-v1'
# Tables whose rows would mean the build already read source or wrote vectors.
# A missing table counts as empty; a store that cannot be read refuses.
_UNUSED_ZERO_TABLES = (
    ('candidateDb', ('generations', 'content', 'membership', 'head', 'candidate_seals',
                     'receipts', 'pins', 'gen_index', 'vector_profiles', 'vector_chunks',
                     'vector_chunk_sets', 'gen_vector_rows', 'gen_vector_seals',
                     'exclusions', 'content_fts', 'content_fts_idx',
                     'content_fts_content', 'content_fts_docsize')),
    ('queueDb', ('jobs',)),
    ('catalogDb', ('scans', 'active', 'current', 'scan_dirs', 'scan_files',
                   'hash_ready', 'pending_fill', 'vector_fill')),
    ('bindingStore', ('bindings',)),
)
_UNUSED_METADATA_TABLES = {
    'candidateDb': {'schema_meta', 'content_fts_data', 'content_fts_config'},
    'queueDb': {'queue_meta'},
    'catalogDb': {'meta'},
    'bindingStore': {'meta'},
}


def _ro_sqlite(path):
    import sqlite3
    source = Path(path)
    if not source.is_absolute() or source.is_symlink() or not source.is_file():
        _fail('unused_build_store_unavailable')
    # A WAL-mode database with no sidecars cannot always be opened mode=ro by
    # the system SQLite. With no WAL present, immutable reads the complete
    # single-file snapshot without creating sidecars; a present WAL must be
    # read through SQLite's normal read-only WAL path or fail closed.
    wal = Path(str(source) + '-wal')
    shm = Path(str(source) + '-shm')
    mode = '?mode=ro' if wal.exists() or shm.exists() else '?immutable=1'
    return sqlite3.connect(source.as_uri() + mode, uri=True, timeout=.5)


def _unused_store_proof(identity, *, allow_work=False):
    """Read-only, WAL-aware proof of store rows and active claims.

    A present WAL is read through SQLite's normal read-only path; a store
    without sidecars is read as an immutable single-file snapshot. Any SQLite
    error or integrity failure refuses: unreadable is never read as empty.
    """
    import sqlite3
    from contextlib import closing
    proof = {}
    for key, tables in _UNUSED_ZERO_TABLES:
        try:
            with closing(_ro_sqlite(identity[key])) as db:
                db.execute('BEGIN')
                if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                    _fail('unused_build_store_check_failed')
                present = {row[0] for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%'")}
                dynamic = set()
                if allow_work and key == 'candidateDb':
                    from laya_index_a import _gen_fts_ddl
                    # A partly built generation owns an FTS5 table and its
                    # shadow tables. Verify the exact allocated set and DDL;
                    # unrelated schema drift still refuses. Do not require a
                    # fixed user_version here: the snapshot/replay check below
                    # must also catch a changed SQLite header.
                    ordinals = [row[0] for row in db.execute('SELECT ord FROM gen_index')]
                    if any(type(n) is not int or n <= 0 for n in ordinals):
                        _fail('unused_build_store_schema_unrecognized')
                    dynamic = {name for name in present if re.fullmatch(
                        r'gen_fts_[1-9][0-9]*(?:_(?:data|idx|content|docsize|config))?',
                        name)}
                    expected = {f'gen_fts_{n}{suffix}' for n in ordinals for suffix in
                                ('', '_data', '_idx', '_content', '_docsize', '_config')}
                    if dynamic != expected or any(db.execute(
                            'SELECT sql FROM sqlite_master WHERE type=? AND name=?',
                            ('table', f'gen_fts_{n}')).fetchone() != (_gen_fts_ddl(f'gen_fts_{n}'),)
                            for n in ordinals):
                        _fail('unused_build_store_schema_unrecognized')
                if present - set(tables) - _UNUSED_METADATA_TABLES[key] - dynamic:
                    _fail('unused_build_store_schema_unrecognized')
                counts = {name: db.execute('SELECT COUNT(*) FROM "%s"' % name).fetchone()[0]
                          for name in tables if name in present}
                if allow_work and key == 'queueDb' and 'jobs' in present:
                    active = db.execute("SELECT COUNT(*) FROM jobs WHERE state IN ('pending','leased')").fetchone()[0]
                    if active:
                        _fail('inactive_build_claim_active')
                db.execute('COMMIT')
        except sqlite3.Error:
            _fail('unused_build_store_unreadable')
        if not allow_work and any(counts.values()):
            _fail('unused_build_has_work')
        wal = Path(identity[key] + '-wal')
        if allow_work:
            try:
                source_info = Path(identity[key]).stat()
                wal_info = wal.stat() if wal.exists() else None
            except OSError:
                _fail('unused_build_store_unavailable')
            proof[key] = {'counts': counts,
                          'walBytes': wal_info.st_size if wal_info else 0,
                          'sourceIdentity': [source_info.st_dev, source_info.st_ino,
                                             source_info.st_size, source_info.st_mtime_ns,
                                             source_info.st_ctime_ns],
                          'walIdentity': ([wal_info.st_dev, wal_info.st_ino,
                                           wal_info.st_size, wal_info.st_mtime_ns,
                                           wal_info.st_ctime_ns] if wal_info else None)}
        else:
            proof[key] = {'counts': counts,
                          'walBytes': wal.stat().st_size if wal.exists() else 0}
    return proof


def _sha256_file(path):
    """Streaming digest of one owned, private, single-link regular file."""
    source = Path(path)
    if source.is_symlink():
        _fail('unsafe_install_input')
    fd = os.open(source, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
                info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o077):
            _fail('unsafe_install_input')
        digest = hashlib.sha256()
        while True:
            block = os.read(fd, 1 << 20)
            if not block:
                return digest.hexdigest()
            digest.update(block)
    finally:
        os.close(fd)


def _snapshot_sqlite(source, target):
    """Create one self-contained SQLite snapshot, verified by quick_check."""
    import sqlite3
    from contextlib import closing
    try:
        with closing(_ro_sqlite(source)) as src:
            # VACUUM INTO reads a consistent SQLite snapshot and produces a
            # standalone file. backup() of WAL-mode stores leaves a -shm
            # sidecar on this host, which cannot be sealed as one file.
            src.execute('VACUUM main INTO ?', (str(target),))
        if Path(str(target) + '-wal').exists() or Path(str(target) + '-shm').exists():
            _fail('unused_build_backup_failed')
        with closing(sqlite3.connect(Path(target).as_uri() + '?immutable=1', uri=True)) as check:
            ok = check.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
    except sqlite3.Error:
        _fail('unused_build_backup_failed')
    if not ok:
        _fail('unused_build_backup_failed')
    os.chmod(target, 0o600)
    return _sha256_file(target)


def _receipt_matches(final, identity, install_id, commit_id, *, current_bytes,
                     schema=UNUSED_RECEIPT_SCHEMA):
    """True only for a complete receipt of exactly this build; else refuse.

    ``current_bytes`` are the live record/pointer bytes to compare with the
    receipt (None to skip: after retirement the record has legitimately moved).
    """
    try:
        receipt = json.loads(_read_private(final / 'receipt.json', 65536))
    except (OSError, ValueError, InstallBlocked):
        _fail('unused_build_receipt_invalid')
    files = receipt.get('files') if isinstance(receipt, dict) else None
    if (not isinstance(files, dict) or receipt.get('schema') != schema or
            receipt.get('installId') != install_id or receipt.get('commitId') != commit_id or
            receipt.get('projectRoot') != identity['projectRoot'] or
            receipt.get('stateRoot') != identity['stateRoot'] or
            set(files) != {'journal.sqlite', 'candidate.sqlite', 'queue.sqlite',
                           'catalog.sqlite', 'binding.sqlite', 'initial-build.json',
                           'build-pointer.json', 'reviewed-route.json'}):
        _fail('unused_build_receipt_mismatch')
    for name, digest in files.items():
        if _sha256_file(final / name) != digest:
            _fail('unused_build_receipt_mismatch')
    if current_bytes is not None and any(
            hashlib.sha256(current_bytes[name]).hexdigest() != files[name]
            for name in current_bytes):
        _fail('unused_build_receipt_mismatch')
    return receipt


def abandon_unused_initial_build(*, project_root, state_root, install_id, commit_id,
                                 wake_revision=None, queue=None):
    """Retire a registered initial build that never started; deletes nothing.

    For an owned synthetic-QA install whose build is still ``registered``
    (unsealed, no progress/freshness/block), journal ``candidate_ready`` with
    no selected route or first write, and whose stores hold no source or
    vector row, this ends the build so a fresh install can register. Under
    the install lock and then the build owner (the order finalize/rescan and
    the wake cancel use), each step verified-or-resumed by a repeat:

    1. Prove identity and emptiness (``runtime_wake_cancel.ownership_proof``:
       fixture markers, canonical pointer, journal, project still plain QMD;
       the caller's install id and commit; the reviewed route bytes; a
       read-only WAL-aware zero-row proof of every store).
    2. Write a verified private receipt directory in the state root first:
       online SQLite backups of the journal and the four stores plus the
       exact record, pointer and route bytes (no-clobber, atomic rename).
    3. A pending ``laya-sync`` wake is cancelled by the official CAS
       (``runtime_wake_cancel.cancel_locked``) under these same locks. A wake
       row needs its ``wake_revision``; a running or newer one refuses before
       any record/pointer change. Nothing but the project's own selected
       route or an install step enqueues this kind, and both are excluded
       (QMD selected; install lock held), so none can reappear.
    4. CAS the record to terminal ``retired`` (a worker that knows it ACKs,
       an older one fails closed on the unknown phase), then release the
       project pointer (``retire_project_pointer``, archived first).

    Old state files, QMD, sources and backups all remain. Sealed, bound,
    building, blocked, selected or first-write builds are never touched.
    """
    return _abandon_initial_build(project_root=project_root, state_root=state_root,
                                  install_id=install_id, commit_id=commit_id,
                                  wake_revision=wake_revision, queue=queue,
                                  allow_started=False)


def abandon_inactive_initial_build(*, project_root, state_root, install_id, commit_id,
                                   wake_revision=None, queue=None):
    """Archive an unselected QA candidate, including a partially built one.

    All private stores are backed up and retained. A selected route, first
    write, bound cutover, active claim, running wake, or changed owner refuses.
    This is separate from the registered-only unused-build contract.
    """
    return _abandon_initial_build(project_root=project_root, state_root=state_root,
                                  install_id=install_id, commit_id=commit_id,
                                  wake_revision=wake_revision, queue=queue,
                                  allow_started=True)


def _abandon_initial_build(*, project_root, state_root, install_id, commit_id,
                           wake_revision, queue, allow_started):
    import laya_initial_build as initial_build
    import runtime_wake_cancel as wake_cancel
    from laya_install_journal import inspect_journal
    project = _checked_project(project_root)
    state = _private_parent(state_root)
    if (state != Path(state_root) or type(install_id) is not str or
            not re.fullmatch(r'[0-9a-f]{32}', install_id) or state.name != install_id or
            state.parent not in _state_bases()):
        _fail('unused_build_state_not_canonical')
    if type(commit_id) is not str or not re.fullmatch(r'[0-9a-f]{40}', commit_id):
        _fail('unused_build_commit_invalid')
    if wake_revision is not None and (type(wake_revision) is not int or wake_revision <= 0):
        _fail('unused_build_wake_revision_invalid')
    import time
    final = state / (INACTIVE_RETIREMENT if allow_started else UNUSED_RETIREMENT)
    other = state / (UNUSED_RETIREMENT if allow_started else INACTIVE_RETIREMENT)
    if other.exists() or other.is_symlink():
        _fail('unused_build_conflicting_receipt')
    receipt_schema = INACTIVE_RECEIPT_SCHEMA if allow_started else UNUSED_RECEIPT_SCHEMA
    if final.is_symlink():
        _fail('unused_build_receipt_invalid')
    with _install_lock(state), initial_build.owner(state) as owned:
        if not owned:
            _fail('initial_build_busy')
        record = initial_build.read(state)
        if record is None:
            _fail('initial_build_missing')
        identity = record['identity']
        journal = state / 'install-journal.sqlite'
        if (identity['projectRoot'] != str(project) or identity['stateRoot'] != str(state) or
                identity['journalPath'] != str(journal) or identity['installId'] != install_id):
            _fail('unused_build_identity_mismatch')
        if record['phase'] == 'retired':
            # Replay after the record moved: finish the pointer, nothing else.
            receipt = _receipt_matches(final, identity, install_id, commit_id,
                                       current_bytes=None, schema=receipt_schema)
            return {'status': 'inactive_build_retired' if allow_started else 'unused_build_retired',
                    'replayed': True,
                    'receipt': str(final / 'receipt.json'),
                    'buildPointer': initial_build.retire_project_pointer(identity),
                    'buildPhase': 'retired', 'recordEpoch': record['epoch'],
                    'receiptRecordEpoch': receipt.get('recordEpoch'),
                    'changesApplied': 0, 'nextRequires': 'new_install_id_and_state_root'}
        if record['phase'] != 'registered' and (not allow_started or record['phase'] not in
                                                ('building', 'blocked')):
            _fail('unused_build_not_registered')
        if not allow_started and any(record[key] is not None for key in
                                     ('progress', 'sealed', 'freshness', 'blocked')):
            _fail('unused_build_has_progress')
        try:
            ownership = wake_cancel.ownership_proof(str(project))
        except wake_cancel.CancelRefused as exc:
            _fail('unused_build_' + str(exc))
        if (ownership['installId'] != install_id or ownership['stateRoot'] != str(state) or
                initial_build.read(state) != record):
            _fail('unused_build_identity_mismatch')
        pointer_path = initial_build.registered_pointer_path(str(project))
        if pointer_path is None:
            _fail('unused_build_pointer_mismatch')
        pointer_bytes = _read_private(pointer_path, 65536)
        expected_pointer = {'schema': initial_build.POINTER_SCHEMA,
                            'projectRoot': identity['projectRoot'],
                            'stateRoot': identity['stateRoot'],
                            'installId': identity['installId']}
        if json.loads(pointer_bytes) != expected_pointer:
            _fail('unused_build_pointer_mismatch')
        pending = inspect_journal(journal)
        entry = pending['pending'] if isinstance(pending['pending'], dict) else {}
        if (pending['state'] != 'candidate_ready' or pending['selected'] is not None or
                pending['newWriteSeen'] or pending['projectRoot'] != str(project) or
                pending['scopeId'] != identity['scopeId'] or
                pending['pendingRouteSha256'] != identity['routeSha256'] or
                (not allow_started and pending['providerAttestationSha256'] is not None) or
                (not allow_started and pending['providerFileIdentitySha256'] is not None) or
                entry.get('installId') != install_id or entry.get('sourceCommit') != commit_id):
            _fail('unused_build_journal_changed')
        if allow_started:
            from laya_ledger_common import refuse_qmd_origin
            from laya_fresh_wiki_origin import LEDGER as FRESH_LEDGER
            refuse_qmd_origin(state)
            if (state / FRESH_LEDGER).exists():
                _fail('inactive_build_cutover_started')
        route_bytes = _read_private(identity['routeFile'], 8192)
        if hashlib.sha256(route_bytes).hexdigest() != identity['routeSha256']:
            _fail('initial_build_route_changed')
        stores = _unused_store_proof(identity, allow_work=True) if allow_started else _unused_store_proof(identity)
        record_bytes = _read_private(initial_build.record_path(state),
                                     initial_build.MAX_RECORD_BYTES)
        live = {'initial-build.json': record_bytes, 'build-pointer.json': pointer_bytes,
                'reviewed-route.json': route_bytes}
        wake_status = wake_cancel.status(str(project), queue=queue)
        wake = wake_status['wake']
        if wake is None and wake_status.get('overflow'):
            _fail('unused_build_wake_overflow_present')
        if wake is not None and wake_revision is None:
            _fail('unused_build_wake_revision_required')
        if not final.exists():
            temp = Path(tempfile.mkdtemp(prefix='.unused-retire-', dir=state))
            try:
                files = {'initial-build.json': record_bytes, 'build-pointer.json': pointer_bytes,
                         'reviewed-route.json': route_bytes}
                for name, raw in files.items():
                    fd = os.open(temp / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(fd, 'wb') as out:
                        out.write(raw)
                        out.flush()
                        os.fsync(out.fileno())
                digests = {name: hashlib.sha256(raw).hexdigest() for name, raw in files.items()}
                for name, source in (('journal.sqlite', journal),
                                     ('candidate.sqlite', identity['candidateDb']),
                                     ('queue.sqlite', identity['queueDb']),
                                     ('catalog.sqlite', identity['catalogDb']),
                                     ('binding.sqlite', identity['bindingStore'])):
                    digests[name] = _snapshot_sqlite(source, temp / name)
                receipt = {'schema': receipt_schema, 'installId': install_id,
                           'commitId': commit_id, 'projectRoot': identity['projectRoot'],
                           'stateRoot': identity['stateRoot'], 'recordEpoch': record['epoch'],
                           'journalEpoch': pending['epoch'], 'stores': stores,
                           'wakeRevision': wake_revision, 'files': digests,
                           'createdNs': time.time_ns()}
                fd = os.open(temp / 'receipt.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, 'wb') as out:
                    out.write(json.dumps(receipt, sort_keys=True, separators=(',', ':')).encode())
                    out.flush()
                    os.fsync(out.fileno())
                os.rename(temp, final)
                fd = os.open(state, os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            except BaseException:
                import shutil
                shutil.rmtree(temp, ignore_errors=True)
                raise
        # A pre-existing receipt (interrupted run) must be this exact build's.
        receipt = _receipt_matches(final, identity, install_id, commit_id,
                                   current_bytes=live, schema=receipt_schema)
        if allow_started and receipt.get('stores') != stores:
            _fail('unused_build_store_changed')
        cancelled = None
        if wake is not None:
            import sqlite3
            try:
                cancelled = wake_cancel.cancel_locked(str(project), wake_revision, str(final),
                                                      ownership, queue=queue)
            except wake_cancel.CancelRefused as exc:
                _fail('unused_build_wake_' + str(exc))
            except sqlite3.OperationalError:
                _fail('unused_build_wake_queue_busy')
        # Last look before the first mutation of the record: nothing moved.
        current_stores = (_unused_store_proof(identity, allow_work=True) if allow_started
                          else _unused_store_proof(identity))
        try:
            current_ownership = wake_cancel.ownership_proof(str(project))
        except wake_cancel.CancelRefused:
            _fail('unused_build_changed')
        if (initial_build.read(state) != record or current_stores != stores or
                inspect_journal(journal) != pending or
                current_ownership['sha256'] != ownership['sha256']):
            _fail('unused_build_changed')
        if allow_started:
            refuse_qmd_origin(state)
            if (state / FRESH_LEDGER).exists():
                _fail('inactive_build_cutover_started')
        clear = {'sealed': None, 'progress': None, 'freshness': None, 'blocked': None}
        record = initial_build.advance(record, phase='retired', **clear) if allow_started else initial_build.advance(record, phase='retired')
        pointer = initial_build.retire_project_pointer(identity)
    return {'status': 'inactive_build_retired' if allow_started else 'unused_build_retired',
            'replayed': False,
            'receipt': str(final / 'receipt.json'), 'stateRoot': str(state),
            'wake': None if cancelled is None else
            {key: cancelled[key] for key in ('status', 'revision') if key in cancelled},
            'buildPointer': pointer, 'buildPhase': record['phase'],
            'changesApplied': 1, 'nextRequires': 'new_install_id_and_state_root',
            'next': ['plan-fresh-wiki-origin', 'review-index-provider',
                     'prepare-fresh-wiki-origin']}


def retired_build_pointer(project_root):
    """Read-only: the retired build still holding this project's wake pointer.

    An interrupted abandon can retire the build record yet leave the
    pointer, which then refuses a fresh install as
    ``initial_build_already_registered``. Only a pointer naming a ``retired``
    build qualifies; an active or prepared install is never named. Returns
    the next action, or None.
    """
    import laya_initial_build as initial_build
    try:
        record = initial_build.lookup(str(_checked_project(project_root)))
        if record is None or record['phase'] != 'retired':
            return None
        identity = record['identity']
        state = Path(identity['stateRoot'])
        if identity['journalPath'] != str(state / 'install-journal.sqlite'):
            return None
    except Exception:  # a hint only; the real refusal stays the caller's
        return None
    # An interrupted abandon may have archived a partial candidate.
    action = ('abandon-inactive-index-build' if
              (state / INACTIVE_RETIREMENT / 'receipt.json').is_file() else
              'abandon-index-build')
    return {'nextAction': action, 'stateRoot': str(state),
            'reason': 'retired_build_pointer_registered'}


def _checked_project(project_root):
    project = Path(project_root)
    if (not project.is_absolute() or project.is_symlink() or
            project.resolve(strict=True) != project):
        _fail('invalid_project_root')
    return project


def _require_fresh_bound_generation(row, state):
    """A selection activates only the generation sealed after a freshness request.

    The build record must be ``bound`` to exactly the ledger's generation and
    sealed evidence, from a scan started at or after the recorded request.
    """
    import laya_initial_build as initial_build
    from laya_ledger_common import BOUND_EVIDENCE
    record = initial_build.read(state)
    if record is None:
        _fail('initial_build_missing')
    identity, sealed, freshness = record['identity'], record['sealed'], record['freshness']
    if (identity['journalPath'] != row['journalPath'] or
            identity['projectRoot'] != row['projectRoot'] or
            identity['candidateDb'] != row['candidateDb']):
        _fail('initial_build_project_mismatch')
    if record['phase'] != 'bound':
        _fail('initial_build_bind_unsettled')
    if (sealed is None or freshness is None or
            sealed['scanStartedNs'] < freshness['requestedNs'] or
            any(sealed[theirs] != row[ours] for ours, theirs in BOUND_EVIDENCE)):
        _fail('activation_requires_fresh_generation')


def activate_origin_selection(project_root, journal_path, route_bytes):
    """Explicitly activate a prepared selection, then wake the selected sync.

    The caller holds the install lock. The bound generation must be the one
    sealed after the freshness request (``_require_fresh_bound_generation``);
    ``laya_fresh_wiki_origin.activate`` then publishes the route. Once active,
    one ``laya-sync`` wake is queued so the selected worker catches up any
    source edit made after the sealed snapshot; its outcome is reported,
    never retried here.
    """
    import laya_fresh_wiki_origin as fresh
    from laya_ledger_common import refuse_qmd_origin
    project = _checked_project(project_root)
    journal = Path(journal_path)
    refuse_qmd_origin(journal.parent)
    if not (journal.parent / fresh.LEDGER).exists():
        _fail('cutover_ledger_missing')
    row = fresh._load(journal.parent / fresh.LEDGER)
    _require_fresh_bound_generation(row, journal.parent)
    from laya_journal_v4 import LEGACY_HERMES, legacy_hermes_present
    if legacy_hermes_present():
        _fail(LEGACY_HERMES)
    row = fresh.activate(project, journal, route_bytes)
    return {**row, 'syncWake': _request_build_wake(str(project))}


def recover_origin_selection(project_root, journal_path, route_bytes):
    """Settle an interrupted selection; an ``active`` result wakes the selected sync."""
    import laya_fresh_wiki_origin as fresh
    from laya_ledger_common import refuse_qmd_origin
    project = _checked_project(project_root)
    journal = Path(journal_path)
    refuse_qmd_origin(journal.parent)
    if not (journal.parent / fresh.LEDGER).exists():
        _fail('cutover_ledger_missing')
    row = fresh.recover(project, journal, route_bytes)
    if row['phase'] == 'active':
        return {**row, 'syncWake': _request_build_wake(str(project))}
    return row


def restore_origin_selection(*, project_root, state_root):
    """Withdraw a selected route before its first selected write.

    The user-visible restore. Under the install lock it reads the ledger
    beside the private journal and requires the same project, journal and
    reviewed route bytes. ``active`` restores only while the journal has seen
    no selected write; ``switching``/``restoring`` settle through recovery;
    ``prepared`` never left the previous path and changes nothing. After a
    selected write the route is forward-only. The candidate stores, the
    ledger and the marker are kept; only the exact route file this selection
    published is withdrawn.
    """
    import laya_fresh_wiki_origin as fresh
    from laya_ledger_common import refuse_qmd_origin
    project = _checked_project(project_root)
    state = _private_parent(state_root)
    journal = state / 'install-journal.sqlite'
    refuse_qmd_origin(state)
    if not (state / fresh.LEDGER).exists():
        _fail('cutover_ledger_missing')
    return _restore_fresh_wiki_selection(project, state, journal)


def _restore_fresh_wiki_selection(project, state, journal):
    import laya_fresh_wiki_origin as fresh
    auto = project / '.auto-context'
    def observed():
        return {'cutoverPhase': fresh._load(state / fresh.LEDGER)['phase'],
                'journalState': inspect_journal(journal)['state'],
                'selection': fresh.classify_marker(project, auto / fresh.MARKER,
                                                    auto / fresh.ROUTE)}
    with _install_lock(state):
        row = fresh._load(state / fresh.LEDGER)
        if row['phase'] == 'retired':
            return {'status': 'binding_retired', 'cutoverPhase': 'retired',
                    'journalState': inspect_journal(journal)['state'],
                    'changesApplied': 0}
        route_bytes = _read_private(state / 'reviewed-route.json', 8192)
        if (row['projectRoot'] != str(project) or row['journalPath'] != str(journal) or
                hashlib.sha256(route_bytes).hexdigest() != row['routeSha256']):
            _fail('restore_cutover_mismatch')
        before = observed()
        if tuple(before.values()) in (('prepared', 'candidate_ready', 'legacy'),
                                      ('restored', 'restored_origin', 'legacy')):
            return {'status': 'legacy_selected', **before, 'changesApplied': 0}
        try:
            if row['phase'] == 'active':
                fresh.restore(project, journal)
            else:
                fresh.recover(project, journal, route_bytes)
        except fresh.FreshWikiBlocked as exc:
            after = observed()
            if str(exc) == 'fresh_wiki_rollback_forward_only':
                return {'status': 'blocked', 'reason': 'rollback_forward_only',
                        **after, 'changesApplied': int(after != before)}
            if after == before:
                raise
            return {'status': 'recovery_required', 'reason': str(exc),
                    **after, 'changesApplied': 1}
        after = observed()
    if tuple(after.values()) in (('prepared', 'candidate_ready', 'legacy'),
                                 ('restored', 'restored_origin', 'legacy')):
        from laya_embed_resident import wait_for_release
        stopped = wait_for_release(state / 'runtime' / 'laya-embed.sock')
        return {'status': 'legacy_restored', **after,
                'changesApplied': int(after != before),
                **({} if stopped else {'embedServiceCleanup': 'pending'})}
    return {'status': 'recovery_required', 'reason': 'restore_unsettled',
            **after, 'changesApplied': int(after != before)}


def stage_host_loaded_candidate(*, source_root, plugin_root, state_root, commit_id,
                                route_bytes, reviewed_provider,
                                _already_locked=False):
    """Seal an installed commit and stage its attested, inactive journal.

    The final route is constructed by the onboarding owner. The caller
    must not call this with the source checkout as ``plugin_root``. No QMD
    file, model or project route is written by staging.
    """
    root = Path(plugin_root)
    if not isinstance(route_bytes, bytes):
        _fail('invalid_route_bytes')
    try:
        route_hint = json.loads(route_bytes.decode('utf-8'))
    except (UnicodeError, ValueError) as exc:
        raise InstallBlocked('invalid_route_bytes') from exc
    if not isinstance(route_hint, dict):
        _fail('invalid_route_bytes')
    parent = _prepare_state_root(state_root, route_hint.get('installId'))
    if len(str(parent / 'runtime' / 'laya-embed.sock').encode('utf-8')) > 100:
        _fail('install_socket_path_too_long')
    if (not root.is_absolute() or root.is_symlink() or
            Path(source_root).resolve(strict=True) == root.resolve(strict=True) or
            route_hint.get('journalPath') != str(parent / 'install-journal.sqlite') or
            route_hint.get('pluginRoot') != str(root) or
            not isinstance(reviewed_provider, dict) or
            set(reviewed_provider) != {'vectorProfile', 'providerManifest',
                                       'dependencyClosure'}):
        _fail('invalid_install_request')
    journal = parent / 'install-journal.sqlite'
    with (nullcontext() if _already_locked else _install_lock(parent)):
        proof = adopt_host_loaded_plugin(source_root, root, commit_id=commit_id,
                                          vector_profile=reviewed_provider['vectorProfile'])
        runtime = parent / 'runtime'
        if not runtime.exists() and not runtime.is_symlink():
            os.mkdir(runtime, 0o700)
        runtime_info = runtime.lstat()
        if (not stat.S_ISDIR(runtime_info.st_mode) or
                runtime_info.st_uid != os.getuid() or
                stat.S_IMODE(runtime_info.st_mode) & 0o077):
            _fail('unsafe_runtime_directory')
        receipt = stage_production_attestation(
            journal, proof, reviewed_provider['providerManifest'],
            reviewed_provider['dependencyClosure'])
        if journal.exists() or journal.is_symlink():
            observed = inspect_journal(journal)
            if (observed['state'] != 'candidate_ready' or
                    observed['pending'] is None or
                    observed['pending']['pluginRoot'] != str(root) or
                    observed['pending']['sourceCommit'] != commit_id or
                    observed['pending']['bundleManifestSha256'] !=
                    proof['bundleManifestSha256'] or
                    observed['pendingRouteSha256'] !=
                    hashlib.sha256(route_bytes).hexdigest() or
                    observed['providerAttestationSha256'] is not None):
                _fail('existing_install_journal_requires_review')
        else:
            observed = create_journal(journal, proof, route_bytes)
        preflight = preflight_product_candidate(journal)
        if preflight['attestationSha256'] != receipt['attestationSha256']:
            _fail('staged_attestation_changed')
        return {'status': 'candidate_ready', 'pluginRoot': str(root),
                'sourceCommit': commit_id, 'journalPath': str(journal),
                'bundleManifestSha256': proof['bundleManifestSha256'],
                'ownershipManifestSha256': proof['ownershipManifestSha256'],
                'attestationSha256': receipt['attestationSha256'],
                'pendingRouteSha256': preflight['pendingRouteSha256']}


def _installed_root():
    return Path(__file__).resolve(strict=True).parent.parent


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    stage = sub.add_parser('stage', help='seal an already host-installed candidate')
    stage.add_argument('--source-root', required=True)
    stage.add_argument('--commit', required=True)
    stage.add_argument('--state-root', required=True)
    stage.add_argument('--route-file', required=True)
    stage.add_argument('--provider-review-file', required=True)
    fresh_plan = sub.add_parser('plan-fresh-wiki',
                                help='review a new wiki-only generation')
    fresh_plan.add_argument('--project-root', required=True)
    review = sub.add_parser('review-provider',
                            help='inventory exact local Node/model/native artifacts')
    review.add_argument('--source-root', required=True)
    review.add_argument('--state-root', required=True)
    review.add_argument('--commit', required=True)
    review.add_argument('--model-file')
    review.add_argument('--dimension', type=int)
    fresh_prepare = sub.add_parser('prepare-fresh-wiki',
                                   help='register an inactive new wiki-only build')
    for key in ('project-root', 'source-root', 'state-root', 'commit',
                'proposal-sha256', 'install-id', 'accepted-provider-sha256'):
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
    retire = sub.add_parser('retire-origin',
                            help='retire a bound, never selected build')
    retire.add_argument('--project-root', required=True)
    retire.add_argument('--state-root', required=True)
    retire.add_argument('--approve-retire', action='store_true')
    args = parser.parse_args(argv)
    root = _installed_root()
    state_root = (_private_parent(args.state_root)
                  if args.action in ('status', 'activate', 'recover') else None)
    journal = state_root / 'install-journal.sqlite' if state_root else None
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
            source_root=args.source_root, plugin_root=root,
            state_root=args.state_root, commit_id=args.commit,
            model_path=args.model_file, dimension=args.dimension)
    elif args.action == 'prepare-fresh-wiki':
        result = prepare_fresh_wiki_origin(
            project_root=args.project_root, source_root=args.source_root,
            plugin_root=root, state_root=args.state_root, commit_id=args.commit,
            expected_proposal_sha256=args.proposal_sha256,
            install_id=args.install_id,
            accepted_provider_sha256=args.accepted_provider_sha256,
            model_path=args.model_file, dimension=args.dimension)
    elif args.action == 'stage':
        result = stage_host_loaded_candidate(
            source_root=args.source_root, plugin_root=root,
            state_root=args.state_root, commit_id=args.commit,
            route_bytes=_read_private(args.route_file, 8192),
            reviewed_provider=_json_input(args.provider_review_file))
    elif args.action == 'status':
        proof = inspect_full_plugin_candidate(root)
        result = {'bundle': proof, 'journal': inspect_journal(journal)}
    elif args.action == 'restore':
        if not args.approve_restore:
            _fail('restore_approval_required')
        result = restore_origin_selection(project_root=args.project_root,
                                          state_root=args.state_root)
    elif args.action == 'retire-origin':
        if not args.approve_retire:
            _fail('retire_approval_required')
        result = retire_drifted_origin(project_root=args.project_root,
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

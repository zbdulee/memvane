#!/usr/bin/env python3
"""Installer-only Laya candidate staging and QMD cutover entrypoint.

The host must install and load the exact Git candidate first. Staging seals
that *installed* root; it never installs a plugin or changes a project route.
An already prepared QMD cutover ledger is mandatory before activation.
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
from laya_qmd_cutover import activate_qmd_origin, recover_qmd_origin


class InstallBlocked(RuntimeError):
    pass


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
    if root.exists() or root.is_symlink():
        return _private_parent(root)
    base = Path.home() / '.local' / 'state' / 'laya'
    if (not root.is_absolute() or root.parent != base or
            type(install_id) is not str or not re.fullmatch(r'[0-9a-f]{32}', install_id) or
            root.name != install_id):
        _fail('unsupported_state_root_creation')
    cursor = Path.home()
    for name in ('.local', 'state', 'laya', install_id):
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


MAX_INITIAL_FILES = 200
MAX_INITIAL_SOURCE_BYTES = 5_000_000


def plan_qmd_origin(project_root):
    """Read the owned QMD source and both current collection trees for review.

    This plan contains no manuscript bodies. It refuses an unindexed source,
    changed QMD title recipe, an unowned QMD database or active route. No
    model, backup, candidate or plugin cache is touched.
    """
    import config
    import qmd_route
    from laya_index_host_route import (_multi_settings_supported,
                                       _multi_compile_supported,
                                       _source_for_collection,
                                       RECORD, QMD_SELECTION_MARKER)
    from laya_index_source_port import scan_collections
    from laya_index_vector import qmd_markdown_title
    from laya_qmd_migration import QMD_253, inspect_qmd_collections

    project = Path(project_root)
    if (not project.is_absolute() or project.is_symlink() or
            project.resolve(strict=True) != project):
        _fail('invalid_project_root')
    auto = project / '.auto-context'
    if ((auto / RECORD).exists() or (auto / RECORD).is_symlink() or
            (auto / QMD_SELECTION_MARKER).exists() or
            (auto / QMD_SELECTION_MARKER).is_symlink()):
        _fail('project_selection_already_exists')
    found = config.find_project_config(str(project))
    settings = auto / 'settings.json'
    if (found.get('configPath') != str(settings) or
            found.get('projectRoot') != str(project) or settings.is_symlink()):
        _fail('canonical_settings_required')
    from laya_index_host_route import _read_regular, MAX_SETTINGS_BYTES
    raw = _read_regular(settings, MAX_SETTINGS_BYTES)
    settings_sha = hashlib.sha256(raw).hexdigest()
    source_settings = json.loads(raw.decode('utf-8'), object_pairs_hook=lambda pairs: _unique(pairs))
    cfg = config.load_project_config(str(project))
    names = tuple(sorted(cfg.get('collections', ())))
    roles = config.role_map(cfg)
    if (cfg.get('indexing') is not True or len(names) != 2 or
            sorted(roles.get(name) for name in names) != ['raw', 'wiki'] or
            set(cfg.get('collectionPaths', {})) != set(names) or
            not _multi_settings_supported(source_settings, cfg, names)):
        _fail('unsupported_selected_settings')
    roots = {name: _source_for_collection(project, cfg['collectionPaths'][name])
             for name in names}
    if (roots[names[0]].startswith(roots[names[1]] + '/') or
            roots[names[1]].startswith(roots[names[0]] + '/')):
        _fail('overlapping_collection_roots')
    wiki_root = next(roots[name] for name in names if roles[name] == 'wiki')
    if not _multi_compile_supported(source_settings, cfg, project, wiki_root):
        _fail('unsupported_selected_compile_settings')
    runtime = qmd_route.binary_info()
    selected = qmd_route.project_paths(str(project), reject_conflicts=True)
    if runtime.get('managed') is not True or selected.get('selected') is not True:
        _fail('plugin_owned_qmd_required')
    source_db = selected['INDEX_PATH']
    inventory = inspect_qmd_collections(source_db, QMD_253, names)
    if any(inventory['collection_config'][name][0] != roots[name]
           for name in names):
        _fail('qmd_collection_root_changed')
    stat_project = project.stat()
    scope_seed = json.dumps([str(project), stat_project.st_dev,
                             stat_project.st_ino, settings_sha],
                            separators=(',', ':')).encode('utf-8')
    scope = hashlib.sha256(scope_seed).hexdigest()[:32]
    class InitialIndex:
        def list_exclusions(self, _scope):
            return []
    scan = scan_collections(roots, scope, InitialIndex())
    if (len(scan.files) > MAX_INITIAL_FILES or
            sum(len(file.data) for file in scan.files) > MAX_INITIAL_SOURCE_BYTES or
            any(not file.eligible for file in scan.files)):
        _fail('initial_source_exceeds_reviewed_bounds')
    current = {file.path: file for file in scan.files}
    active = {path: (title, sha, body) for path, title, sha, flag, body
              in inventory['documents'] if flag}
    if set(current) - set(active):
        _fail('qmd_current_source_unindexed')
    for path, file in current.items():
        title, sha, body = active[path]
        if (file.sha256 != sha or file.text != body or
                title != qmd_markdown_title(file.text, path)):
            _fail('qmd_current_source_changed')
    plan = {'schema': 'laya-qmd-origin-plan-v1', 'projectRoot': str(project),
            'settingsSha256': settings_sha, 'sourceDb': source_db,
            'sourceFormat': QMD_253, 'sourceDigest': inventory['digest'],
            'scopeId': scope, 'sourceManifest': scan.manifest,
            'collections': [{'name': name, 'role': roles[name],
                             'sourceRoot': roots[name]} for name in names],
            'sourceFiles': [[file.path, file.sha256] for file in scan.files],
            'deletedSinceQmd': len(active) - len(current),
            'qmdStoreGeneration': selected.get('generation'),
            'qmdManagedRuntime': runtime['QMD_PACKAGE_BIN']}
    digest = hashlib.sha256(json.dumps(plan, sort_keys=True,
        ensure_ascii=False, separators=(',', ':')).encode('utf-8')).hexdigest()
    return {'status': 'review_required', 'proposalSha256': digest, **plan}


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

    Inventory covers every packaged native binary for the pinned Metal build.
    It neither asserts that native execution works nor issues approval: the
    caller must accept the returned digest, and prepare repeats every check.
    """
    import platform
    import qmd_runtime
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
    runtime = qmd_runtime.select()
    proof = runtime['proof']
    if proof.get('version') != '2.5.3':
        _fail('provider_review_qmd_version_unsupported')
    qmd = Path(proof['qmd'])
    package = qmd.parent.parent
    node = Path(proof['node'])
    module_pkg = package / 'node_modules' / 'node-llama-cpp'
    native_pkg = package / 'node_modules' / '@node-llama-cpp' / 'mac-arm64-metal'
    module = module_pkg / 'dist' / 'index.js'
    for package_file, expected in ((package / 'package.json', '2.5.3'),
                                   (module_pkg / 'package.json', '3.18.1'),
                                   (native_pkg / 'package.json', '3.18.1')):
        item = json.loads(_read_private_or_package(package_file))
        if not isinstance(item, dict) or item.get('version') != expected:
            _fail('provider_review_package_version_changed')
    native_files = []
    for directory, dirs, files in os.walk(native_pkg, followlinks=False):
        for name in dirs + files:
            if (Path(directory) / name).is_symlink():
                _fail('provider_review_native_symlink')
        native_files.extend(Path(directory) / name for name in files
                            if Path(name).suffix in ('.node', '.dylib', '.so'))
    if (not any(path.suffix == '.node' for path in native_files) or
            not any(path.suffix == '.dylib' for path in native_files) or
            len(native_files) > 4096):
        _fail('provider_review_native_closure_incomplete')
    closure = [{'path': str(path), 'sha256': _hash_file(str(path)),
                'bytes': path.stat().st_size} for path in sorted(native_files)]
    if model_path is None:
        import qmd_shared_store
        import shared_qmd_setup
        selected = qmd_shared_store.select()
        metadata = Path(selected['generation']) / 'bootstrap-proof.json'
        model_proof = json.loads(_read_private_or_package(metadata))
        if (not isinstance(model_proof, dict) or
                model_proof.get('schema') != shared_qmd_setup.SCHEMA or
                model_proof.get('searchProved') is not True):
            _fail('provider_review_model_proof_invalid')
        model_path = model_proof.get('modelPath')
        dimension = model_proof.get('dimension')
        expected_model_hash = model_proof.get('modelSha256')
    else:
        expected_model_hash = None
    if type(dimension) is not int or not 1 <= dimension <= 2048:
        _fail('provider_review_dimension_invalid')
    if (not isinstance(model_path, str) or
            Path(model_path).name != 'embeddinggemma-300M-Q8_0.gguf'):
        _fail('provider_review_model_identity_invalid')
    model_sha = _hash_file(str(model_path))
    if expected_model_hash is not None and model_sha != expected_model_hash:
        _fail('provider_review_model_changed')
    node_sha, module_sha = _hash_file(str(node)), _hash_file(str(module))
    closure_sha = hashlib.sha256(_canonical(closure)).hexdigest()
    provider_sha = artifacts['core/laya_local_embed_provider.py']
    worker_sha = artifacts['core/laya_local_embed_worker.mjs']
    model_id = DEFAULT_EMBED_MODEL_URI
    fingerprint = recipe_fingerprint(
        model_id=model_id, model_sha256=model_sha,
        runtime_id='qmd-2.5.3-node-llama-cpp-3.18.1',
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
                'runtimeId': 'qmd-2.5.3-node-llama-cpp-3.18.1',
                'modelId': model_id, 'embeddingFingerprint': fingerprint,
                'dimension': dimension,
                'recipeId': 'qmd-regex-token-chunks-candidate-v1',
                'dependencyClosureApproved': True,
                'dependencyClosureSha256': closure_sha}
    proposal = {'schema': 'laya-local-provider-review-v1',
                'sourceCommit': commit_id, 'pluginRoot': str(root),
                'stateRoot': str(state), 'qmdRuntimeGeneration': runtime['generation'],
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


def _plan_scan(plan):
    """Reproduce the reviewed source snapshot without opening the model."""
    from laya_index_source_port import scan_collections
    roots = {item['name']: item['sourceRoot'] for item in plan['collections']}
    class FreshIndex:
        def list_exclusions(self, _scope):
            return []
    scan = scan_collections(roots, plan['scopeId'], FreshIndex(),
                            max_files=MAX_INITIAL_FILES,
                            max_bytes=MAX_INITIAL_SOURCE_BYTES)
    if (scan.manifest != plan['sourceManifest'] or
            [[item.path, item.sha256] for item in scan.files] != plan['sourceFiles'] or
            any(not item.eligible for item in scan.files)):
        _fail('reviewed_source_changed')
    return scan


def _candidate_prepared(path, generation, scope, manifest, source_digest,
                        profile_id, files, claim):
    """Read an interrupted, already sealed candidate without replacing it."""
    from contextlib import closing
    from laya_index_a import LayaIndexA
    from laya_index_vector import verify_generation_seal
    with closing(LayaIndexA(path)) as index:
        index.db.execute('BEGIN')
        try:
            seal = index.db.execute(
                'SELECT source_manifest,claim_digest,claim_epoch FROM candidate_seals WHERE gen=?',
                (generation,)).fetchone()
            state = index.db.execute('SELECT state,scope FROM generations WHERE gen=?',
                                     (generation,)).fetchone()
            members = index.db.execute(
                'SELECT path,hash,eligible FROM membership WHERE gen=? ORDER BY path',
                (generation,)).fetchall()
            head = index.db.execute('SELECT gen FROM head WHERE scope=?',
                                    (scope,)).fetchone()
            receipt = index.db.execute(
                'SELECT scope,gen,source_manifest,claim_digest,claim_epoch FROM receipts WHERE claim=?',
                (claim,)).fetchone()
            vector = verify_generation_seal(index, generation)
            index.db.rollback()
        except BaseException:
            index.db.rollback()
            raise
        if (seal != (manifest, source_digest, 0) or
                state not in (('prepared', scope), ('published', scope)) or
                members != [(f.path, f.sha256, 1) for f in files]
                or vector['profile_id'] != profile_id):
            _fail('partial_candidate_requires_recovery')
        if state[0] == 'published' and (
                head != (generation,) or receipt !=
                (scope, generation, manifest, source_digest, 0)):
            _fail('partial_candidate_requires_recovery')
        if state[0] == 'prepared' and (head is not None or receipt is not None):
            _fail('partial_candidate_requires_recovery')
        return index.incarnation()


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


def prepare_owned_qmd_origin(*, project_root, source_root, plugin_root, state_root, commit_id,
                             expected_proposal_sha256, install_id,
                             accepted_provider_sha256, user_qmd_roots,
                             model_path=None, dimension=None,
                             coordinator=None, start_service=None,
                             provider_factory=None):
    """Prepare a bounded, privately backed up two-collection cutover.

    This is an off-hook installer operation. It leaves QMD selected and never
    activates the product route. Existing candidate bytes are verified and
    reused; inconsistent partial state is retained for explicit recovery.
    """
    from contextlib import closing
    from context_learning.local_cycle import ResourceDeferred
    from context_learning.resource_coordination import Coordinator
    from context_learning.shared import resource_report
    from laya_index_a import LayaIndexA
    from laya_index_claim_queue import ClaimQueue
    from laya_index_host_route import SCHEMA_V2
    from laya_index_vector import MAX_CANDIDATES, qmd_markdown_title
    from laya_local_embed_provider import ReviewedLocalQmdProvider
    from laya_local_embed_service import start_owned_embed_service
    from laya_package import _user_qmd_inventory
    from laya_product_sync_worker import EMBED_POLICY
    from laya_qmd_cutover import bind_qmd_origin
    from laya_qmd_migration import (backup_qmd_collections,
                                    inspect_qmd_collections,
                                    prepare_qmd_candidate,
                                    verify_restore_collections)

    if (not isinstance(expected_proposal_sha256, str) or
            not re.fullmatch(r'[0-9a-f]{64}', expected_proposal_sha256) or
            not isinstance(install_id, str) or
            not re.fullmatch(r'[0-9a-f]{32}', install_id) or
            not isinstance(accepted_provider_sha256, str) or
            not re.fullmatch(r'[0-9a-f]{64}', accepted_provider_sha256) or
            not isinstance(user_qmd_roots, list) or not user_qmd_roots):
        _fail('invalid_origin_preparation_request')
    if Path(source_root).resolve(strict=True) == Path(plugin_root).resolve(strict=True):
        _fail('source_checkout_cannot_be_installed_root')
    state = _prepare_state_root(state_root, install_id)
    journal = state / 'install-journal.sqlite'
    socket = state / 'runtime' / 'laya-embed.sock'
    if len(str(socket).encode('utf-8')) > 100:
        _fail('install_socket_path_too_long')
    with _install_lock(state):
        plan = plan_qmd_origin(project_root)
        if plan['proposalSha256'] != expected_proposal_sha256:
            _fail('reviewed_origin_plan_changed')
        review = review_local_provider(
            source_root=source_root, plugin_root=plugin_root,
            state_root=state, commit_id=commit_id,
            model_path=model_path, dimension=dimension)
        if review['proposalSha256'] != accepted_provider_sha256:
            _fail('reviewed_provider_proposal_changed')
        reviewed_provider = review['reviewedProvider']
        names = tuple(item['name'] for item in plan['collections'])
        roots = {item['name']: item['sourceRoot'] for item in plan['collections']}
        scan = _plan_scan(plan)
        proof = adopt_host_loaded_plugin(source_root, plugin_root,
                                         commit_id=commit_id,
                                         vector_profile=reviewed_provider['vectorProfile'])
        _user_qmd_inventory(Path(plugin_root), user_qmd_roots)
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

        # A no-clobber whole-DB backup precedes any model load. Interrupted
        # backup/candidate bytes are retained and checked on retry.
        backup = state / 'qmd-origin-backup.sqlite'
        if backup.exists() or backup.is_symlink():
            verify_restore_collections(backup, plan['sourceFormat'], names,
                                       plan['sourceDigest'])
        else:
            backup_qmd_collections(plan['sourceDb'], plan['sourceFormat'],
                                   names, backup)
            verify_restore_collections(backup, plan['sourceFormat'], names,
                                       plan['sourceDigest'])
        candidate = state / 'laya-candidate.sqlite'
        queue = state / 'laya-claims.sqlite'
        generation = 'g-' + hashlib.sha256(
            ('laya-origin-generation:' + install_id + ':' +
             expected_proposal_sha256).encode()).hexdigest()[:32]
        claim = 'origin-' + hashlib.sha256(
            ('laya-origin-claim:' + generation).encode()).hexdigest()
        if not candidate.exists() and not candidate.is_symlink():
            provider = (provider_factory or ReviewedLocalQmdProvider)(manifest)
            if (provider.profile['profile_id'] != profile['profileId'] or
                    provider.profile['model_id'] != profile['modelId'] or
                    provider.profile['embedding_fingerprint'] !=
                    profile['embeddingFingerprint'] or
                    provider.profile['dimension'] != profile['dimension']):
                _fail('reviewed_provider_profile_mismatch')
            controller = coordinator or Coordinator()
            observed = resource_report(EMBED_POLICY, detailed=True)
            with controller.activity('provider', EMBED_POLICY, observed):
                def admit():
                    fresh = resource_report(EMBED_POLICY, detailed=True)
                    return controller.check(EMBED_POLICY, fresh)['ready']
                service = (start_service or start_owned_embed_service)(manifest, admit=admit)
                try:
                    chunks = []
                    seen = set()
                    for file in scan.files:
                        fresh = resource_report(EMBED_POLICY, detailed=True)
                        if not controller.check(EMBED_POLICY, fresh)['ready']:
                            raise ResourceDeferred('laya_origin_resource_pressure')
                        title = qmd_markdown_title(file.text, file.path)
                        key = (file.sha256, title)
                        if key in seen:
                            continue
                        seen.add(key)
                        rows = provider.embed_document_chunks(file.text, file.path)
                        if not rows or len(rows) + len(chunks) > MAX_CANDIDATES:
                            _fail('origin_vector_budget_exceeded')
                        chunks.extend({'hash': key[0], 'title': key[1],
                                       'seq': row['seq'], 'pos': row['pos'],
                                       'vector': row['vector']} for row in rows)
                finally:
                    service.stop()
            fresh_plan = plan_qmd_origin(project_root)
            if fresh_plan['proposalSha256'] != expected_proposal_sha256:
                _fail('origin_changed_during_embedding')
            prepare_qmd_candidate(
                backup, plan['sourceFormat'], names,
                {file.path: file.text for file in scan.files}, candidate,
                plan['scopeId'], generation, migration_allowed=True,
                expected_collection_root=roots,
                expected_source_digest=plan['sourceDigest'],
                vector_batch={'scope': plan['scopeId'],
                              'source_manifest': scan.manifest,
                              'qmd_source_digest': plan['sourceDigest'],
                              'model_id': profile['modelId'],
                              'embedding_fingerprint': profile['embeddingFingerprint'],
                              'dimension': profile['dimension'], 'chunks': chunks},
                provider_approved=True, current_source_db=plan['sourceDb'],
                scope_collections=names)
        incarnation = _candidate_prepared(
            candidate, generation, plan['scopeId'], scan.manifest,
            plan['sourceDigest'], profile['profileId'], scan.files, claim)
        if queue.exists() or queue.is_symlink():
            _finish_interrupted_claim_queue_link(queue, incarnation)
            with closing(ClaimQueue.open(queue)) as opened:
                if opened.binding() != incarnation:
                    _fail('partial_queue_requires_recovery')
        else:
            _publish_claim_queue(queue, incarnation)
        if plan_qmd_origin(project_root)['proposalSha256'] != expected_proposal_sha256:
            _fail('origin_changed_before_bind')
        route = {'schema': SCHEMA_V2, 'status': 'active',
                 'projectRoot': plan['projectRoot'],
                 'settingsSha256': plan['settingsSha256'],
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
            reviewed_provider=reviewed_provider,
            user_qmd_roots=user_qmd_roots, _already_locked=True)
        row = bind_qmd_origin(
            project_root=project_root, journal_path=journal,
            route_bytes=route_bytes, source_db=plan['sourceDb'],
            backup_db=backup, source_format=plan['sourceFormat'],
            collection=names, source_digest=plan['sourceDigest'],
            source_root=roots, candidate_db=candidate, queue_db=queue,
            scope=plan['scopeId'], index_generation=generation,
            source_manifest=scan.manifest,
            vector_profile_id=profile['profileId'], claim=claim)
        return {'status': 'cutover_prepared',
                'proposalSha256': expected_proposal_sha256,
                'journalPath': str(journal),
                'pendingRouteSha256': staged['pendingRouteSha256'],
                'cutoverPhase': row['phase'],
                'sourceBackup': str(backup),
                'candidateDb': str(candidate),
                'routeFile': str(route_file)}


def stage_host_loaded_candidate(*, source_root, plugin_root, state_root, commit_id,
                                route_bytes, reviewed_provider, user_qmd_roots,
                                _already_locked=False):
    """Seal an installed commit and stage its attested, inactive journal.

    The final route is constructed by the QMD migration owner. The caller
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
                                       'dependencyClosure'} or
            not isinstance(user_qmd_roots, list)):
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
            observed = create_journal(journal, proof, route_bytes,
                                      user_qmd_roots=user_qmd_roots)
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
    stage.add_argument('--user-qmd-roots-file', required=True)
    plan = sub.add_parser('plan-origin', help='inspect an owned two-collection QMD origin')
    plan.add_argument('--project-root', required=True)
    review = sub.add_parser('review-provider',
                            help='inventory exact local Node/model/native artifacts')
    review.add_argument('--source-root', required=True)
    review.add_argument('--state-root', required=True)
    review.add_argument('--commit', required=True)
    review.add_argument('--model-file')
    review.add_argument('--dimension', type=int)
    prepare = sub.add_parser('prepare-origin',
                             help='prepare a reviewed bounded QMD-origin cutover')
    prepare.add_argument('--project-root', required=True)
    prepare.add_argument('--source-root', required=True)
    prepare.add_argument('--state-root', required=True)
    prepare.add_argument('--commit', required=True)
    prepare.add_argument('--proposal-sha256', required=True)
    prepare.add_argument('--install-id', required=True)
    prepare.add_argument('--accepted-provider-sha256', required=True)
    prepare.add_argument('--model-file')
    prepare.add_argument('--dimension', type=int)
    prepare.add_argument('--user-qmd-root', action='append', required=True)
    status = sub.add_parser('status', help='inspect the inactive or active install journal')
    status.add_argument('--state-root', required=True)
    for name in ('activate', 'recover'):
        command = sub.add_parser(name)
        command.add_argument('--project-root', required=True)
        command.add_argument('--state-root', required=True)
        command.add_argument('--route-file', required=True)
        if name == 'activate':
            command.add_argument('--approve-activation', action='store_true')
    args = parser.parse_args(argv)
    root = _installed_root()
    state_root = (_private_parent(args.state_root)
                  if args.action in ('status', 'activate', 'recover') else None)
    journal = state_root / 'install-journal.sqlite' if state_root else None
    if args.action == 'plan-origin':
        result = plan_qmd_origin(args.project_root)
    elif args.action == 'review-provider':
        result = review_local_provider(
            source_root=args.source_root, plugin_root=root,
            state_root=args.state_root, commit_id=args.commit,
            model_path=args.model_file, dimension=args.dimension)
    elif args.action == 'prepare-origin':
        result = prepare_owned_qmd_origin(
            project_root=args.project_root, source_root=args.source_root,
            plugin_root=root, state_root=args.state_root, commit_id=args.commit,
            expected_proposal_sha256=args.proposal_sha256,
            install_id=args.install_id,
            accepted_provider_sha256=args.accepted_provider_sha256,
            model_path=args.model_file, dimension=args.dimension,
            user_qmd_roots=args.user_qmd_root)
    elif args.action == 'stage':
        result = stage_host_loaded_candidate(
            source_root=args.source_root, plugin_root=root,
            state_root=args.state_root, commit_id=args.commit,
            route_bytes=_read_private(args.route_file, 8192),
            reviewed_provider=_json_input(args.provider_review_file),
            user_qmd_roots=_json_input(args.user_qmd_roots_file))
    elif args.action == 'status':
        proof = inspect_full_plugin_candidate(root)
        result = {'bundle': proof, 'journal': inspect_journal(journal)}
    else:
        project = Path(args.project_root)
        if (not project.is_absolute() or project.is_symlink() or
                project.resolve(strict=True) != project):
            _fail('invalid_project_root')
        route = _read_private(args.route_file, 8192)
        with _install_lock(state_root):
            if args.action == 'activate':
                if not args.approve_activation:
                    _fail('activation_approval_required')
                result = activate_qmd_origin(project, journal, route)
            else:
                result = recover_qmd_origin(project, journal, route)
    print(json.dumps(result, sort_keys=True, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (InstallBlocked, OSError, ValueError, RuntimeError) as exc:
        print(json.dumps({'status': 'blocked', 'reason': str(exc)},
                         sort_keys=True), file=sys.stderr)
        sys.exit(2)

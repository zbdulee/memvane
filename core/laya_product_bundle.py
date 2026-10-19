"""Private, exact-commit Laya plugin candidate; no installation or activation.

The fixed file list is the executable Python dependency closure of the current
Laya route and runtime. The surrounding host adapters remain plugin-owned and
are checked by their own installation boundary. A Git commit is verified at
packaging time, not inferred from a caller-supplied revision string.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import unicodedata


SCHEMA = 'laya-product-bundle-v1'
OWNER_SCHEMA = 'laya-product-ownership-v1'
FULL_SCHEMA = 'laya-full-plugin-bundle-v1'
FULL_OWNER_SCHEMA = 'laya-full-plugin-ownership-v1'
PLUGIN_PREFIXES = ('core/', 'hooks/', 'hermes_adapter/', 'backend/',
                   'skills/', 'scripts/', 'agents/', 'locks/',
                   '.claude-plugin/', '.codex-plugin/', '.agents/plugins/')
PLUGIN_ROOT_FILES = frozenset(('__init__.py', 'plugin.yaml', 'plugin.json',
                               'package.json', 'LICENSE', 'README.md'))
ARTIFACTS = (
    'config.py',
    'laya_index_a.py',
    'laya_index_vector.py',
    'laya_local_embed_provider.py',
    'laya_index_source_port.py',
    'laya_index_claim_queue.py',
    'laya_index_flow.py',
    'laya_index_runtime.py',
    'laya_index_sync.py',
    'laya_source_catalog.py',
    'laya_index_capacity.py',
    'laya_vector_capacity.py',
    'laya_index_host_route.py',
    'laya_qmd_cutover.py',
    'laya_qmd_migration.py',
    'laya_package.py',
    'laya_install_binding.py',
    'laya_backup_preflight.py',
    'laya_product_bundle.py',
    'laya_install_journal.py',
)
MAX_FILE = 4 * 1024 * 1024
MAX_JSON = 1024 * 1024
SHA = re.compile(r'[0-9a-f]{64}\Z')
COMMIT = re.compile(r'[0-9a-f]{40,64}\Z')
PROVIDER_ID = re.compile(r'[a-z0-9][a-z0-9._-]{0,127}\Z')
VECTOR_FIELDS = frozenset(('profileId', 'modelId', 'embeddingFingerprint',
                           'dimension', 'metric', 'approvedProviderId',
                           'providerArtifactSha256', 'providerRelativePath'))
PRODUCTION_ATTESTATION_SCHEMA = 'laya-production-provider-attestation-v2'
PRODUCTION_ATTESTATION_FIELDS = frozenset((
    'schema', 'sourceCommit', 'bundleManifestSha256',
    'ownershipManifestSha256', 'journalPath', 'vectorProfile', 'providerManifest',
    'dependencyClosure'))
PROVIDER_MANIFEST_FIELDS = frozenset((
    'approvedProviderId', 'providerArtifactSha256', 'providerRelativePath',
    'providerPath', 'workerPath', 'workerSha256',
    'nodePath', 'nodeSha256', 'moduleEntryPath',
    'moduleEntrySha256', 'modelPath', 'modelSha256', 'runtimeId', 'modelId',
    'embeddingFingerprint', 'dimension', 'recipeId',
    'dependencyClosureApproved', 'dependencyClosureSha256',
    'serviceSocketPath'))
FLAGS = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_CLOEXEC', 0)


class BundleEvidenceError(ValueError):
    pass


def _fail(reason):
    raise BundleEvidenceError(reason)


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(',', ':')).encode('utf-8')


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _git_blob_oid(raw, object_format):
    if object_format not in ('sha1', 'sha256'):
        _fail('unsupported_git_object_format')
    header = f'blob {len(raw)}\0'.encode('ascii')
    return hashlib.new(object_format, header + raw).hexdigest()


def validate_vector_profile(profile, artifacts):
    """Validate an immutable declaration against exact bundled provider bytes."""
    if profile is None:
        return None
    if not isinstance(profile, dict) or set(profile) != VECTOR_FIELDS:
        _fail('invalid_vector_profile')
    model, fingerprint, dimension, metric = (profile['modelId'],
                                             profile['embeddingFingerprint'],
                                             profile['dimension'], profile['metric'])
    if (type(model) is not str or not 1 <= len(model) <= 512 or
            any(unicodedata.category(char) in ('Cc', 'Cf', 'Cs', 'Zl', 'Zp')
                for char in model) or
            type(fingerprint) is not str or not SHA.fullmatch(fingerprint) or
            type(dimension) is not int or not 1 <= dimension <= 2048 or
            metric != 'cosine'):
        _fail('invalid_vector_profile')
    derived = _sha(_canonical([model, fingerprint, dimension, metric]))
    relative = profile['providerRelativePath']
    provider_id = profile['approvedProviderId']
    if (type(profile['profileId']) is not str or profile['profileId'] != derived or
            type(provider_id) is not str or not PROVIDER_ID.fullmatch(provider_id) or
            type(relative) is not str or not relative.startswith('core/') or
            not relative.endswith('.py') or
            Path(relative).is_absolute() or '\\' in relative or
            any(part in ('', '.', '..') for part in relative.split('/')) or
            type(profile['providerArtifactSha256']) is not str or
            not SHA.fullmatch(profile['providerArtifactSha256']) or
            relative not in artifacts or
            not isinstance(artifacts[relative], dict) or
            artifacts[relative].get('sha256') != profile['providerArtifactSha256']):
        _fail('provider_artifact_mismatch')
    return dict(profile)


def _attested_file(path, expected_sha, expected_bytes=None):
    """Verify external executable/model bytes without following any symlink."""
    if type(path) is not str or not Path(path).is_absolute() or '\x00' in path:
        _fail('invalid_provider_artifact_path')
    candidate = Path(path)
    if str(candidate.resolve(strict=False)) != path:
        _fail('invalid_provider_artifact_path')
    cursor = Path('/')
    try:
        for part in candidate.parts[1:]:
            cursor /= part
            if cursor.is_symlink():
                _fail('invalid_provider_artifact_path')
        before = candidate.lstat()
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or
                type(expected_sha) is not str or not SHA.fullmatch(expected_sha) or
                (expected_bytes is not None and
                 (type(expected_bytes) is not int or expected_bytes < 0 or
                  before.st_size != expected_bytes))):
            _fail('invalid_provider_artifact')
        digest = hashlib.sha256()
        fd = os.open(candidate, FLAGS)
        try:
            opened = os.fstat(fd)
            while block := os.read(fd, 1024 * 1024):
                digest.update(block)
            after = os.fstat(fd)
        finally:
            os.close(fd)
        ident = lambda s: (s.st_dev, s.st_ino, s.st_nlink, s.st_mode,
                           s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        if (ident(before) != ident(opened) or ident(before) != ident(after) or
                ident(before) != ident(candidate.lstat()) or
                digest.hexdigest() != expected_sha):
            _fail('provider_artifact_changed')
        return before.st_size
    except (OSError, RuntimeError, ValueError) as exc:
        if isinstance(exc, BundleEvidenceError):
            raise
        raise BundleEvidenceError('provider_artifact_unavailable') from exc


def validate_production_provider_attestation(profile, bundle_proof, attestation):
    """Check a *journal-bound* external review record; this is not authority.

    The installer must secure the record outside the plugin, bind its digest
    into the selection journal, and compare that digest on every route check.
    No declaration in a route or bundled source can authorize itself.
    """
    if (type(bundle_proof) is not dict or
            bundle_proof.get('status') != 'exact_commit_full_plugin_candidate_consistent' or
            type(attestation) is not dict or
            set(attestation) != PRODUCTION_ATTESTATION_FIELDS or
            attestation.get('schema') != PRODUCTION_ATTESTATION_SCHEMA or
            type(profile) is not dict or profile != bundle_proof.get('vectorProfile') or
            attestation.get('vectorProfile') != profile or
            any(attestation.get(key) != bundle_proof.get(key) for key in
                ('sourceCommit', 'bundleManifestSha256',
                 'ownershipManifestSha256'))):
        _fail('production_attestation_identity_mismatch')
    root = Path(bundle_proof.get('pluginRoot', ''))
    if not root.is_absolute() or root.is_symlink():
        _fail('production_attestation_identity_mismatch')
    if inspect_full_plugin_candidate(root) != bundle_proof:
        _fail('production_bundle_changed')
    provider = attestation['providerManifest']
    if type(provider) is not dict or set(provider) != PROVIDER_MANIFEST_FIELDS:
        _fail('production_provider_manifest_invalid')
    relative = profile['providerRelativePath']
    expected_provider_path = str(root / relative)
    if (provider['providerPath'] != expected_provider_path or
            provider['workerPath'] != str(root / 'core' / 'laya_local_embed_worker.mjs') or
            provider['providerRelativePath'] != relative or
            provider['providerArtifactSha256'] != profile['providerArtifactSha256'] or
            provider['approvedProviderId'] != profile['approvedProviderId'] or
            provider['modelId'] != profile['modelId'] or
            provider['embeddingFingerprint'] != profile['embeddingFingerprint'] or
            provider['dimension'] != profile['dimension'] or
            provider['dependencyClosureApproved'] is not True):
        _fail('production_provider_profile_mismatch')
    journal_path = Path(attestation['journalPath']) if type(attestation['journalPath']) is str else Path('.')
    if (not journal_path.is_absolute() or journal_path.name != 'install-journal.sqlite' or
            journal_path.is_symlink() or
            str(journal_path.resolve(strict=False)) != str(journal_path)):
        _fail('production_journal_path_invalid')
    state_dir = journal_path.parent
    try:
        state_info = state_dir.lstat()
        if (not stat.S_ISDIR(state_info.st_mode) or
                state_info.st_uid != os.getuid() or
                stat.S_IMODE(state_info.st_mode) & 0o077):
            _fail('production_journal_owner_invalid')
    except OSError as exc:
        raise BundleEvidenceError('production_journal_owner_invalid') from exc
    runtime_dir = state_dir / 'runtime'
    socket_path = str(runtime_dir / 'laya-embed.sock')
    try:
        runtime_info = runtime_dir.lstat()
        if (provider['serviceSocketPath'] != socket_path or
                len(socket_path.encode('utf-8')) > 100 or
                os.path.normpath(socket_path) != socket_path or
                str(Path(socket_path).resolve(strict=False)) != socket_path or
                not stat.S_ISDIR(runtime_info.st_mode) or
                runtime_info.st_uid != os.getuid() or
                stat.S_IMODE(runtime_info.st_mode) & 0o077):
            _fail('production_service_owner_invalid')
        if Path(socket_path).exists() or Path(socket_path).is_symlink():
            socket_info = Path(socket_path).lstat()
            if (not stat.S_ISSOCK(socket_info.st_mode) or
                    socket_info.st_uid != os.getuid() or
                    stat.S_IMODE(socket_info.st_mode) & 0o077):
                _fail('production_service_owner_invalid')
    except (OSError, RuntimeError, TypeError) as exc:
        raise BundleEvidenceError('production_service_owner_invalid') from exc
    _attested_file(expected_provider_path, profile['providerArtifactSha256'])
    _attested_file(provider['workerPath'], provider['workerSha256'])
    for path_key, digest_key in (('nodePath', 'nodeSha256'),
                                 ('moduleEntryPath', 'moduleEntrySha256'),
                                 ('modelPath', 'modelSha256')):
        _attested_file(provider[path_key], provider[digest_key])
    closure = attestation['dependencyClosure']
    if (type(closure) is not list or not closure or len(closure) > 4096 or
            any(type(row) is not dict or set(row) != {'path', 'sha256', 'bytes'}
                or type(row['path']) is not str
                for row in closure)):
        _fail('production_dependency_closure_invalid')
    paths = [row['path'] for row in closure]
    if paths != sorted(set(paths)):
        _fail('production_dependency_closure_invalid')
    for row in closure:
        _attested_file(row['path'], row['sha256'], row['bytes'])
    if _sha(_canonical(closure)) != provider['dependencyClosureSha256']:
        _fail('production_dependency_closure_mismatch')
    from laya_local_embed_provider import recipe_fingerprint
    from laya_index_vector import profile_identity
    try:
        fingerprint = recipe_fingerprint(
            model_id=provider['modelId'], model_sha256=provider['modelSha256'],
            runtime_id=provider['runtimeId'], recipe_id=provider['recipeId'],
            provider_sha256=provider['providerArtifactSha256'],
            worker_sha256=provider['workerSha256'],
            node_sha256=provider['nodeSha256'],
            module_entry_sha256=provider['moduleEntrySha256'],
            closure_sha256=provider['dependencyClosureSha256'])
        expected_profile = profile_identity(provider['modelId'], fingerprint,
                                            provider['dimension'])
    except (ValueError, KeyError, TypeError) as exc:
        raise BundleEvidenceError('production_provider_recipe_invalid') from exc
    if (fingerprint != profile['embeddingFingerprint'] or
            expected_profile != profile['profileId']):
        _fail('production_provider_recipe_mismatch')
    return dict(provider)


def production_provider_approved(profile, bundle_proof, attestation=None, *,
                                 journal_attestation_sha256=None):
    """Require a reviewed installer-owned record sealed by the journal.

    This local install boundary excludes arbitrary tampering by the same user.
    The journal must store the reviewed digest before activation and never
    derive its expected value from changed on-disk attestation bytes later.
    """
    try:
        if (type(journal_attestation_sha256) is not str or
                not SHA.fullmatch(journal_attestation_sha256) or
                type(attestation) is not dict or
                _sha(_canonical(attestation)) != journal_attestation_sha256):
            return False
        validate_production_provider_attestation(profile, bundle_proof, attestation)
        return True
    except (BundleEvidenceError, TypeError, ValueError, KeyError):
        return False


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            _fail('duplicate_bundle_key')
        result[key] = value
    return result


def _nonfinite(_value):
    _fail('nonfinite_bundle_number')


def _read(path, limit):
    try:
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > limit:
            _fail('unsafe_bundle_file')
        fd = os.open(path, FLAGS)
        try:
            opened = os.fstat(fd)
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                _fail('bundle_file_changed')
            raw = os.read(fd, limit + 1)
            after = os.fstat(fd)
            now = path.lstat()
            ident = lambda s: (s.st_dev, s.st_ino, s.st_nlink, s.st_mode, s.st_size,
                               s.st_mtime_ns, s.st_ctime_ns)
            if len(raw) > limit or ident(before) != ident(after) or ident(before) != ident(now):
                _fail('bundle_file_changed')
            return raw, {'sha256': _sha(raw), 'bytes': len(raw), 'dev': before.st_dev,
                         'ino': before.st_ino, 'nlink': before.st_nlink,
                         'mode': stat.S_IMODE(before.st_mode),
                         'mtimeNs': before.st_mtime_ns, 'ctimeNs': before.st_ctime_ns}
        finally:
            os.close(fd)
    except OSError as exc:
        raise BundleEvidenceError('bundle_file_unavailable') from exc


def _json(path):
    raw, identity = _read(path, MAX_JSON)
    try:
        return json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs,
                          parse_constant=_nonfinite), identity
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise BundleEvidenceError('invalid_bundle_json') from exc


def _private(root):
    try:
        path = Path(root)
        if not path.is_absolute() or path.is_symlink() or not path.is_dir():
            _fail('unsafe_bundle_root')
        canonical = path.resolve(strict=True)
        for directory in (canonical, canonical / 'core'):
            info = directory.lstat()
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or
                    stat.S_IMODE(info.st_mode) & 0o022):
                _fail('unsafe_bundle_owner')
        return canonical
    except (OSError, RuntimeError, TypeError) as exc:
        raise BundleEvidenceError('bundle_root_unavailable') from exc


def _git(root, *args):
    try:
        done = subprocess.run(('git', '-C', str(root), *args), check=True,
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              timeout=15)
        return done.stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise BundleEvidenceError('source_commit_unavailable') from exc


def _write(path, raw, *, mode=0o600):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                 getattr(os, 'O_NOFOLLOW', 0), mode)
    with os.fdopen(fd, 'wb') as out:
        out.write(raw)
        out.flush()
        os.fsync(out.fileno())


def prepare_product_candidate(source_root, engine_root, *, commit_id):
    """Copy only exact committed bytes into a new private candidate directory."""
    source = Path(source_root)
    target = Path(engine_root)
    if (not source.is_absolute() or source.is_symlink() or not source.is_dir() or
            type(commit_id) is not str or not COMMIT.fullmatch(commit_id) or
            not target.is_absolute() or target.exists() or target.is_symlink() or
            not target.parent.is_dir() or target.parent.is_symlink()):
        _fail('invalid_bundle_request')
    source = source.resolve(strict=True)
    if (_git(source, 'rev-parse', '--show-toplevel').decode().strip() != str(source) or
            _git(source, 'rev-parse', 'HEAD').decode().strip() != commit_id):
        _fail('source_commit_mismatch')
    inputs = {}
    for name in ARTIFACTS:
        raw, _ = _read(source / 'core' / name, MAX_FILE)
        committed = _git(source, 'show', f'{commit_id}:core/{name}')
        if raw != committed:
            _fail('source_not_exact_commit')
        inputs[name] = raw
    os.mkdir(target, 0o700)
    os.mkdir(target / 'core', 0o700)
    entries = {}
    for name, raw in inputs.items():
        _write(target / 'core' / name, raw)
        _, entries[name] = _read(target / 'core' / name, MAX_FILE)
    manifest = {'schema': SCHEMA, 'kind': 'exact_commit_python_candidate',
                'sourceCommit': commit_id, 'exactCommitVerifiedAtPackaging': True,
                'artifacts': {name: {'sha256': row['sha256'], 'bytes': row['bytes']}
                              for name, row in entries.items()}}
    _write(target / 'manifest.json', _canonical(manifest))
    manifest_sha = _sha(_canonical(manifest))
    ownership = {'schema': OWNER_SCHEMA, 'engineRoot': str(target.resolve(strict=True)),
                 'bundleManifestSha256': manifest_sha,
                 'entries': entries}
    _write(target / 'ownership-manifest.json', _canonical(ownership))
    for directory in (target / 'core', target):
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    return inspect_product_candidate(target)


def inspect_product_candidate(engine_root):
    """Recheck file bytes and owner inode evidence without selecting a route."""
    root = _private(engine_root)
    if (set(os.listdir(root)) != {'core', 'manifest.json', 'ownership-manifest.json'} or
            set(os.listdir(root / 'core')) != set(ARTIFACTS)):
        _fail('unexpected_bundle_entry')
    manifest, manifest_file = _json(root / 'manifest.json')
    ownership, ownership_file = _json(root / 'ownership-manifest.json')
    if (not isinstance(manifest, dict) or manifest.get('schema') != SCHEMA or
            manifest.get('kind') != 'exact_commit_python_candidate' or
            manifest.get('exactCommitVerifiedAtPackaging') is not True or
            type(manifest.get('sourceCommit')) is not str or
            not COMMIT.fullmatch(manifest['sourceCommit']) or
            not isinstance(manifest.get('artifacts'), dict) or
            set(manifest['artifacts']) != set(ARTIFACTS) or
            not isinstance(ownership, dict) or ownership.get('schema') != OWNER_SCHEMA or
            ownership.get('engineRoot') != str(root) or
            ownership.get('bundleManifestSha256') != manifest_file['sha256'] or
            not isinstance(ownership.get('entries'), dict) or
            set(ownership['entries']) != set(ARTIFACTS)):
        _fail('bundle_manifest_mismatch')
    for name in ARTIFACTS:
        raw, live = _read(root / 'core' / name, MAX_FILE)
        expected = manifest['artifacts'][name]
        owner = ownership['entries'][name]
        if (not isinstance(expected, dict) or type(expected.get('sha256')) is not str or
                not SHA.fullmatch(expected['sha256']) or type(expected.get('bytes')) is not int or
                expected != {'sha256': live['sha256'], 'bytes': len(raw)} or
                not isinstance(owner, dict) or
                any(type(owner.get(key)) is not int for key in
                    ('bytes', 'dev', 'ino', 'nlink', 'mode', 'mtimeNs', 'ctimeNs')) or
                type(owner.get('sha256')) is not str or owner != live):
            _fail('bundle_file_identity_changed')
    return {'status': 'exact_commit_candidate_consistent', 'engineRoot': str(root),
            'sourceCommit': manifest['sourceCommit'],
            'bundleManifestSha256': manifest_file['sha256'],
            'ownershipManifestSha256': ownership_file['sha256'],
            'includedModules': list(ARTIFACTS), 'exactCommitVerifiedAtPackaging': True,
            'productRouteAllowed': False}


def _plugin_paths(source, commit_id):
    names = _git(source, 'ls-tree', '-r', '-z', commit_id).split(b'\0')
    try:
        paths = {}
        for entry in names:
            if not entry:
                continue
            meta, raw_name = entry.split(b'\t', 1)
            mode, kind, oid = meta.split(b' ', 2)
            name = raw_name.decode('utf-8')
            if kind != b'blob' or mode not in (b'100644', b'100755'):
                _fail('unsupported_plugin_file_mode')
            paths[name] = {'mode': 0o700 if mode == b'100755' else 0o600,
                           'oid': oid.decode('ascii')}
    except UnicodeError as exc:
        raise BundleEvidenceError('invalid_commit_path') from exc
    selected = sorted(paths)
    if (not selected or any(name.startswith('/') or '..' in Path(name).parts or
                            '\\' in name or '.git' in Path(name).parts or
                            '.laya-product' in Path(name).parts or
                            Path(name).parts[:1] == ('.in_use',) for name in selected) or
            not {'hooks/run-hook', 'core/laya_index_runtime.py',
                 'core/laya_index_vector.py',
                 'core/laya_local_embed_provider.py',
                 'core/laya_local_embed_service.py',
                 'core/laya_local_embed_worker.mjs',
                 'core/laya_qmd_chunking.mjs',
                 'core/laya_index_host_route.py', 'core/laya_host_response.py',
                 'core/laya_qmd_cutover.py', 'core/laya_qmd_migration.py',
                 'core/laya_product_sync_worker.py',
                 'core/laya_index_sync.py',
                 'core/laya_source_catalog.py', 'core/laya_index_capacity.py',
                 'core/laya_vector_capacity.py',
                 'core/laya_product_install.py',
                 'core/laya_product_bundle.py', 'core/laya_install_journal.py',
                 'hermes_adapter/core_bridge.py', 'plugin.yaml',
                 'LICENSE', 'README.md', 'locks/qmd-2.5.3/package.json',
                 'locks/qmd-2.5.3/package-lock.json',
                 '.claude-plugin/plugin.json', '.codex-plugin/plugin.json'}.issubset(selected)):
        _fail('incomplete_plugin_commit')
    return {name: paths[name] for name in selected}


def _skip_host_in_use(root, current, dirs):
    """Accept only the host's bounded root PID markers, never plugin code."""
    if current != root or '.in_use' not in dirs:
        return
    marker_dir = root / '.in_use'
    try:
        directory = marker_dir.lstat()
        if (not stat.S_ISDIR(directory.st_mode) or
                directory.st_uid != os.getuid() or
                directory.st_dev != root.stat().st_dev or
                stat.S_IMODE(directory.st_mode) & 0o022):
            _fail('unsafe_host_in_use_metadata')
        entries = list(os.scandir(marker_dir))
        if len(entries) > 64:
            _fail('unsafe_host_in_use_metadata')
        for entry in entries:
            if (not re.fullmatch(r'[1-9][0-9]{0,15}', entry.name, re.ASCII)):
                _fail('unsafe_host_in_use_metadata')
            try:
                info = entry.stat(follow_symlinks=False)
            except FileNotFoundError:
                continue  # The host may remove a PID marker while we inspect.
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
                    info.st_dev != directory.st_dev or info.st_nlink != 1 or
                    info.st_size > 4096 or stat.S_IMODE(info.st_mode) & 0o133):
                _fail('unsafe_host_in_use_metadata')
    except FileNotFoundError:
        dirs.remove('.in_use')  # The host removed the transient directory.
        return
    except OSError as exc:
        raise BundleEvidenceError('unsafe_host_in_use_metadata') from exc
    dirs.remove('.in_use')


def adopt_host_loaded_plugin(source_root, plugin_root, *, commit_id,
                             vector_profile=None):
    """Seal an already installed, exact-commit host plugin in place.

    This only adopts a private, owner-controlled host cache. It does not copy
    executable files, change modes, register a host plugin, or select a route.
    The caller must separately observe that the host loaded ``plugin_root``.
    Existing metadata and any unexpected installed file fail closed.
    """
    source, installed = Path(source_root), Path(plugin_root)
    if (not source.is_absolute() or source.is_symlink() or not source.is_dir() or
            type(commit_id) is not str or not COMMIT.fullmatch(commit_id) or
            not installed.is_absolute() or installed.is_symlink() or
            not installed.is_dir() or installed == source):
        _fail('invalid_installed_candidate_request')
    source = source.resolve(strict=True)
    installed = _private(installed)
    metadata = installed / '.laya-product'
    if metadata.exists() or metadata.is_symlink():
        proof = inspect_full_plugin_candidate(installed)
        if (proof['sourceCommit'] != commit_id or
                proof['vectorProfile'] != vector_profile):
            _fail('installed_candidate_already_owned')
        return proof
    if (_git(source, 'rev-parse', '--show-toplevel').decode().strip() != str(source) or
            _git(source, 'rev-parse', 'HEAD').decode().strip() != commit_id):
        _fail('source_commit_mismatch')
    selected = _plugin_paths(source, commit_id)
    object_format = _git(source, 'rev-parse', '--show-object-format').decode().strip()
    actual = set()
    for directory, dirs, files in os.walk(installed, followlinks=False):
        current = Path(directory)
        info = current.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or
                stat.S_IMODE(info.st_mode) & 0o022 or
                any((current / child).is_symlink() for child in dirs)):
            _fail('unsafe_installed_directory')
        _skip_host_in_use(installed, current, dirs)
        if current == installed and '.git' in dirs:
            dirs.remove('.git')  # Host marketplace clones may retain Git metadata.
        for name in files:
            actual.add(str((current / name).relative_to(installed)))
    if actual != set(selected):
        _fail('installed_file_set_not_exact_commit')
    entries = {}
    for name, committed in selected.items():
        source_raw, source_info = _read(source / name, MAX_FILE)
        installed_raw, installed_info = _read(installed / name, MAX_FILE)
        if (source_info['mode'] & 0o111 != (0o111 if committed['mode'] == 0o700 else 0) or
                _git_blob_oid(source_raw, object_format) != committed['oid']):
            _fail('source_not_exact_commit')
        if (installed_info['dev'] != installed.stat().st_dev or
                (installed_info['mode'] & 0o111) !=
                (0o111 if committed['mode'] == 0o700 else 0) or
                installed_info['mode'] & 0o022 or
                installed_raw != source_raw or
                (installed / name).stat().st_uid != os.getuid()):
            _fail('installed_blob_not_exact_commit')
        entries[name] = installed_info
    artifacts = {name: {'sha256': row['sha256'], 'bytes': row['bytes'],
                        'mode': row['mode']} for name, row in entries.items()}
    profile = validate_vector_profile(vector_profile, artifacts)
    os.mkdir(metadata, 0o700)
    manifest = {'schema': FULL_SCHEMA, 'kind': 'exact_commit_full_plugin_candidate',
                'sourceCommit': commit_id, 'exactCommitVerifiedAtPackaging': True,
                'artifacts': artifacts, 'vectorProfile': profile}
    _write(metadata / 'manifest.json', _canonical(manifest))
    owner = {'schema': FULL_OWNER_SCHEMA, 'pluginRoot': str(installed),
             'bundleManifestSha256': _sha(_canonical(manifest)), 'entries': entries}
    _write(metadata / 'ownership-manifest.json', _canonical(owner))
    fd = os.open(metadata, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    fd = os.open(installed, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    return inspect_full_plugin_candidate(installed)


def prepare_full_plugin_candidate(source_root, plugin_root, *, commit_id,
                                  vector_profile=None):
    """Copy the exact committed Git tree and its host entrypoints, no installs.

    Every tracked blob, including documentation and provenance, is included
    so release QA can compare the installed tree to the candidate tree. .git
    and untracked/ignored files are not copied. A dirty tracked file is rejected.
    """
    source, target = Path(source_root), Path(plugin_root)
    if (not source.is_absolute() or source.is_symlink() or not source.is_dir() or
            type(commit_id) is not str or not COMMIT.fullmatch(commit_id) or
            not target.is_absolute() or target.exists() or target.is_symlink() or
            not target.parent.is_dir() or target.parent.is_symlink()):
        _fail('invalid_bundle_request')
    source = source.resolve(strict=True)
    if (_git(source, 'rev-parse', '--show-toplevel').decode().strip() != str(source) or
            _git(source, 'rev-parse', 'HEAD').decode().strip() != commit_id):
        _fail('source_commit_mismatch')
    selected = _plugin_paths(source, commit_id)
    object_format = _git(source, 'rev-parse', '--show-object-format').decode().strip()
    inputs = {}
    for name in selected:
        parent = source
        for component in Path(name).parts[:-1]:
            parent = parent / component
            if parent.is_symlink() or not parent.is_dir():
                _fail('unsafe_source_parent')
        raw, source_identity = _read(source / name, MAX_FILE)
        if bool(source_identity['mode'] & 0o111) != (selected[name]['mode'] == 0o700):
            _fail('source_mode_not_exact_commit')
        if _git_blob_oid(raw, object_format) != selected[name]['oid']:
            _fail('source_not_exact_commit')
        inputs[name] = raw
    validate_vector_profile(vector_profile,
                            {name: {'sha256': _sha(raw)} for name, raw in inputs.items()})
    os.mkdir(target, 0o700)
    entries = {}
    for name, raw in inputs.items():
        dest = target / name
        parent = target
        for component in Path(name).parts[:-1]:
            parent = parent / component
            if not parent.exists():
                os.mkdir(parent, 0o700)
        _write(dest, raw, mode=selected[name]['mode'])
        _, entries[name] = _read(dest, MAX_FILE)
    artifacts = {name: {'sha256': row['sha256'], 'bytes': row['bytes'],
                        'mode': row['mode']} for name, row in entries.items()}
    profile = validate_vector_profile(vector_profile, artifacts)
    metadata = target / '.laya-product'
    metadata.mkdir(mode=0o700)
    manifest = {'schema': FULL_SCHEMA, 'kind': 'exact_commit_full_plugin_candidate',
                'sourceCommit': commit_id, 'exactCommitVerifiedAtPackaging': True,
                'artifacts': artifacts, 'vectorProfile': profile}
    _write(metadata / 'manifest.json', _canonical(manifest))
    manifest_sha = _sha(_canonical(manifest))
    owner = {'schema': FULL_OWNER_SCHEMA, 'pluginRoot': str(target.resolve(strict=True)),
             'bundleManifestSha256': manifest_sha, 'entries': entries}
    _write(metadata / 'ownership-manifest.json', _canonical(owner))
    for directory, _dirs, _files in os.walk(target, topdown=False):
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    return inspect_full_plugin_candidate(target)


def inspect_full_plugin_candidate(plugin_root):
    """Recheck exact installed file set, bytes, inode and owner metadata."""
    root = _private(plugin_root)
    meta = root / '.laya-product'
    if (meta.is_symlink() or not meta.is_dir() or meta.stat().st_uid != os.getuid() or
            stat.S_IMODE(meta.stat().st_mode) & 0o077 or
            set(os.listdir(meta)) != {'manifest.json', 'ownership-manifest.json'}):
        _fail('unsafe_bundle_metadata')
    manifest, manifest_file = _json(meta / 'manifest.json')
    owner, owner_file = _json(meta / 'ownership-manifest.json')
    if (not isinstance(manifest, dict) or manifest.get('schema') != FULL_SCHEMA or
            manifest.get('kind') != 'exact_commit_full_plugin_candidate' or
            manifest.get('exactCommitVerifiedAtPackaging') is not True or
            type(manifest.get('sourceCommit')) is not str or
            not COMMIT.fullmatch(manifest['sourceCommit']) or
            not isinstance(manifest.get('artifacts'), dict) or
            not isinstance(owner, dict) or owner.get('schema') != FULL_OWNER_SCHEMA or
            owner.get('pluginRoot') != str(root) or
            owner.get('bundleManifestSha256') != manifest_file['sha256'] or
            not isinstance(owner.get('entries'), dict) or
            set(owner['entries']) != set(manifest['artifacts'])):
        _fail('bundle_manifest_mismatch')
    expected_files = set(manifest['artifacts'])
    profile = validate_vector_profile(manifest.get('vectorProfile'),
                                      manifest['artifacts'])
    if (not {'hooks/run-hook', 'core/laya_index_runtime.py',
             'core/laya_index_vector.py',
             'core/laya_local_embed_provider.py',
             'core/laya_local_embed_service.py',
             'core/laya_local_embed_worker.mjs',
             'core/laya_qmd_chunking.mjs',
             'core/laya_index_host_route.py', 'core/laya_host_response.py',
             'core/laya_qmd_cutover.py', 'core/laya_qmd_migration.py',
             'core/laya_product_sync_worker.py',
             'core/laya_index_sync.py',
             'core/laya_source_catalog.py', 'core/laya_index_capacity.py',
             'core/laya_vector_capacity.py',
             'core/laya_product_install.py',
             'core/laya_product_bundle.py', 'core/laya_install_journal.py',
             'hermes_adapter/core_bridge.py', 'plugin.yaml',
             'LICENSE', 'README.md', 'locks/qmd-2.5.3/package.json',
             'locks/qmd-2.5.3/package-lock.json',
             '.claude-plugin/plugin.json', '.codex-plugin/plugin.json'}.issubset(expected_files) or
            any(Path(name).is_absolute() or '..' in Path(name).parts or
                '.git' in Path(name).parts or '.laya-product' in Path(name).parts or
                Path(name).parts[:1] == ('.in_use',)
                for name in expected_files)):
        _fail('bundle_file_set_invalid')
    seen_files = set()
    for directory, dirs, files in os.walk(root, followlinks=False):
        current = Path(directory)
        info = current.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or
                stat.S_IMODE(info.st_mode) & 0o022):
            _fail('unsafe_bundle_directory')
        for dirname in dirs:
            if (current / dirname).is_symlink():
                _fail('unsafe_bundle_directory')
        _skip_host_in_use(root, current, dirs)
        if current == root and '.git' in dirs:
            dirs.remove('.git')
        for filename in files:
            relative = str((current / filename).relative_to(root))
            if not relative.startswith('.laya-product/'):
                seen_files.add(relative)
    if seen_files != expected_files:
        _fail('unexpected_bundle_entry')
    for name in expected_files:
        raw, live = _read(root / name, MAX_FILE)
        expected, owned = manifest['artifacts'][name], owner['entries'][name]
        if (not isinstance(expected, dict) or
                expected != {'sha256': live['sha256'], 'bytes': len(raw),
                             'mode': live['mode']} or
                type(expected.get('sha256')) is not str or
                type(expected.get('bytes')) is not int or
                type(expected.get('mode')) is not int or
                not isinstance(owned, dict) or
                any(type(owned.get(key)) is not int for key in
                    ('bytes', 'dev', 'ino', 'nlink', 'mode', 'mtimeNs', 'ctimeNs')) or
                type(owned.get('sha256')) is not str or owned != live):
            _fail('bundle_file_identity_changed')
    return {'status': 'exact_commit_full_plugin_candidate_consistent',
            'pluginRoot': str(root), 'sourceCommit': manifest['sourceCommit'],
            'bundleManifestSha256': manifest_file['sha256'],
            'ownershipManifestSha256': owner_file['sha256'],
            'includedFiles': sorted(expected_files),
            'vectorProfile': profile,
            'exactCommitVerifiedAtPackaging': True, 'productRouteAllowed': False}

"""Source bundle and read-only active-artifact evidence for memvane-db.

This module never selects an engine, replaces an installed file, or retires QMD.
The bundle is a private, no-clobber *candidate*. An installation receipt must be
issued independently by a future installer; a manifest or matching hash alone
cannot establish ownership or authorize cleanup of a user's QMD.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat


SCHEMA = 'laya-source-bundle-v1'
INSTALL_SCHEMA = 'laya-install-receipt-v1'
OWNERSHIP_SCHEMA = 'laya-ownership-manifest-v1'
ARTIFACTS = (
    'core/laya_index_a.py',
    'core/laya_index_source_port.py',
    'core/source_text.py',
    'core/laya_index_claim_queue.py',
    'core/laya_index_flow.py',
    # The bound flow path imports these lazily.
    'core/laya_index_capacity.py',
    'core/laya_source_catalog.py',
)
MAX_SOURCE_BYTES = 4 * 1024 * 1024
SHA256 = re.compile(r'[0-9a-f]{64}\Z')
COMMIT = re.compile(r'[0-9a-f]{40,64}\Z')
_FLAGS = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_CLOEXEC', 0)


class PackageEvidenceError(ValueError):
    pass


def _fail(reason):
    raise PackageEvidenceError(reason)


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(',', ':')).encode('utf-8')


def _sha(body):
    return hashlib.sha256(body).hexdigest()


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _fail('duplicate_evidence_key')
        result[key] = value
    return result


def _nonfinite(_value):
    _fail('nonfinite_evidence_number')


def _root(path, *, private=False):
    try:
        path = Path(path)
        if not path.is_absolute() or path.is_symlink() or not path.is_dir():
            _fail('unsafe_root')
        root = path.resolve(strict=True)
        if private:
            info = root.stat()
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
                _fail('unsafe_candidate_permissions')
            core = root / 'core'
            if core.is_symlink() or not core.is_dir():
                _fail('unsafe_artifact_parent')
            info = core.stat()
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
                _fail('unsafe_candidate_permissions')
        return root
    except (OSError, RuntimeError, TypeError) as exc:
        raise PackageEvidenceError('root_unavailable') from exc


def _inside(root, relative):
    if relative not in ARTIFACTS and relative != 'manifest.json':
        _fail('unexpected_artifact')
    path = root / relative
    parent = root
    for part in Path(relative).parts[:-1]:
        parent = parent / part
        if parent.is_symlink() or not parent.is_dir():
            _fail('unsafe_artifact_parent')
    return path


def _read(path, limit, *, single_link=True):
    try:
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode) or (single_link and before.st_nlink != 1):
            _fail('unsafe_artifact_file')
        fd = os.open(path, _FLAGS)
        try:
            opened = os.fstat(fd)
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                _fail('artifact_changed')
            if opened.st_size > limit:
                _fail('artifact_too_large')
            chunks, size = [], 0
            while True:
                part = os.read(fd, min(1024 * 1024, limit + 1 - size))
                if not part:
                    break
                size += len(part)
                if size > limit:
                    _fail('artifact_too_large')
                chunks.append(part)
            after = os.fstat(fd)
            now = path.lstat()
            identity = lambda s: (s.st_dev, s.st_ino, s.st_nlink, s.st_size,
                                  s.st_mtime_ns, s.st_ctime_ns)
            if identity(before) != identity(after) or identity(before) != identity(now):
                _fail('artifact_changed')
            return b''.join(chunks), before
        finally:
            os.close(fd)
    except (FileNotFoundError, IsADirectoryError, OSError) as exc:
        raise PackageEvidenceError('artifact_unavailable') from exc


def _load_json(path, limit=1024 * 1024):
    body, _ = _read(path, limit)
    try:
        return json.loads(body.decode('utf-8'), object_pairs_hook=_unique_pairs,
                          parse_constant=_nonfinite), _sha(body)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise PackageEvidenceError('invalid_evidence_json') from exc


def prepare_candidate(source_root, destination, *, source_commit=None,
                      source_dirty=True):
    """Copy exactly four Python modules into a new private staging directory.

    The source checkout can be dirty; source_commit is only a caller claim, not
    an exact-commit attestation. A partial directory after an error is retained
    and never resumed or silently replaced. No dependencies or models run.
    """
    source = _root(source_root)
    destination = Path(destination)
    if (not destination.is_absolute() or destination.exists() or
            destination.is_symlink() or not destination.parent.is_dir() or
            destination.parent.is_symlink()):
        _fail('unsafe_candidate_destination')
    if source_commit is not None and (not isinstance(source_commit, str) or
                                      not COMMIT.fullmatch(source_commit)):
        _fail('invalid_source_commit_claim')
    if type(source_dirty) is not bool:
        _fail('invalid_source_dirty_claim')
    # Read all inputs before creating a destination. No checkout file is edited.
    artifacts = {}
    for relative in ARTIFACTS:
        body, _ = _read(_inside(source, relative), MAX_SOURCE_BYTES)
        artifacts[relative] = body
    os.mkdir(destination, 0o700)  # exclusive; never replaces an installed root
    (destination / 'core').mkdir(mode=0o700)
    records = {}
    for relative, body in artifacts.items():
        path = destination / relative
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                     getattr(os, 'O_NOFOLLOW', 0), 0o600)
        with os.fdopen(fd, 'wb') as out:
            out.write(body)
            out.flush()
            os.fsync(out.fileno())
        records[relative] = {'sha256': _sha(body), 'bytes': len(body)}
    manifest = {'schema': SCHEMA, 'kind': 'source_only_python_candidate',
                'artifacts': records, 'sourceCommitClaim': source_commit,
                'sourceDirtyClaim': source_dirty,
                'exactCommitVerified': False, 'installationStateAtPackaging': 'uninstalled'}
    fd = os.open(destination / 'manifest.json', os.O_WRONLY | os.O_CREAT |
                 os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(fd, 'wb') as out:
        out.write(_canonical(manifest))
        out.flush()
        os.fsync(out.fileno())
    for directory in (destination / 'core', destination):
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    return inspect_candidate(destination)


def inspect_candidate(bundle_root, *, _allow_receipt=False):
    """Verify current bytes; never infer that a matching artifact is installed."""
    root = _root(bundle_root, private=True)
    expected_root = {'core', 'manifest.json'}
    if _allow_receipt:
        expected_root.update(('install-receipt.json', 'ownership-manifest.json'))
    if (set(os.listdir(root)) != expected_root or
            set(os.listdir(root / 'core')) != {Path(p).name for p in ARTIFACTS}):
        _fail('unexpected_candidate_file')
    manifest, manifest_sha = _load_json(_inside(root, 'manifest.json'))
    if (not isinstance(manifest, dict) or manifest.get('schema') != SCHEMA or
            manifest.get('kind') != 'source_only_python_candidate' or
            not isinstance(manifest.get('artifacts'), dict) or
            set(manifest['artifacts']) != set(ARTIFACTS) or
            manifest.get('exactCommitVerified') is not False or
            manifest.get('installationStateAtPackaging') != 'uninstalled'):
        _fail('invalid_candidate_manifest')
    files = {}
    for relative in ARTIFACTS:
        row = manifest['artifacts'][relative]
        if (not isinstance(row, dict) or type(row.get('sha256')) is not str or
                not SHA256.fullmatch(row['sha256'])
                or type(row.get('bytes')) is not int or row['bytes'] < 0):
            _fail('invalid_artifact_entry')
        body, info = _read(_inside(root, relative), MAX_SOURCE_BYTES)
        if len(body) != row['bytes'] or _sha(body) != row['sha256']:
            _fail('artifact_hash_changed')
        files[relative] = {'path': str(root / relative), 'sha256': row['sha256'],
                           'bytes': len(body), 'dev': info.st_dev,
                           'ino': info.st_ino, 'nlink': info.st_nlink,
                           'mtimeNs': info.st_mtime_ns, 'ctimeNs': info.st_ctime_ns}
    return {'status': 'source_bundle_consistent', 'root': str(root),
            'manifestSha256': manifest_sha, 'files': files,
            'exactCommitVerified': False, 'currentInstallation': 'unverified'}


def _user_qmd_inventory(plugin_root, roots):
    if not isinstance(roots, (list, tuple)) or not roots:
        _fail('missing_user_qmd_inventory')
    records = []
    plugin_identity = plugin_root.stat()
    for supplied in roots:
        try:
            path = Path(supplied)
            if not path.is_absolute():
                _fail('invalid_user_qmd_root')
            canonical = path.resolve(strict=True)
            if not canonical.is_dir():
                _fail('invalid_user_qmd_root')
            foreign = canonical.stat()
            # Compare object identities, including aliased parent chains. A
            # shared filesystem root is irrelevant; containment is not.
            plugin_ancestors = {(p.stat().st_dev, p.stat().st_ino)
                                for p in (plugin_root, *plugin_root.parents)}
            foreign_ancestors = {(p.stat().st_dev, p.stat().st_ino)
                                 for p in (canonical, *canonical.parents)}
            if ((plugin_identity.st_dev, plugin_identity.st_ino) in foreign_ancestors or
                    (foreign.st_dev, foreign.st_ino) in plugin_ancestors):
                _fail('user_qmd_root_overlap')
            records.append({'root': str(canonical), 'dev': foreign.st_dev,
                            'ino': foreign.st_ino})
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            if isinstance(exc, PackageEvidenceError):
                raise
            raise PackageEvidenceError('invalid_user_qmd_root') from exc
    if len({(r['dev'], r['ino']) for r in records}) != len(records):
        _fail('duplicate_user_qmd_root')
    return sorted(records, key=lambda r: r['root'])


def verified_active_artifact(plugin_root, install_receipt_path,
                             ownership_manifest_path, active_record, *, user_qmd_roots):
    """Verify an externally selected install receipt without defining selection.

    The caller must supply a trusted active record from its own selection
    journal. This checks agreement and current file identity, not authenticity
    of that journal, run-time safety, or permission to retire another runtime.
    """
    root = _root(plugin_root, private=True)
    user_inventory = _user_qmd_inventory(root, user_qmd_roots)
    candidate = inspect_candidate(root, _allow_receipt=True)
    receipt_path = Path(install_receipt_path)
    ownership_path = Path(ownership_manifest_path)
    if (receipt_path.is_symlink() or ownership_path.is_symlink() or
            receipt_path.resolve() != root / 'install-receipt.json' or
            ownership_path.resolve() != root / 'ownership-manifest.json'):
        _fail('install_receipt_outside_root')
    receipt, _ = _load_json(receipt_path)
    ownership, ownership_sha = _load_json(ownership_path)
    if (not isinstance(active_record, dict) or not isinstance(receipt, dict) or
            not isinstance(ownership, dict)):
        _fail('invalid_active_receipt')
    install_id = receipt.get('installId')
    generation = receipt.get('generation')
    if (receipt.get('schema') != INSTALL_SCHEMA or
            type(install_id) is not str or not re.fullmatch(r'[0-9a-f]{32}', install_id) or
            type(generation) is not str or not re.fullmatch(r'g-[0-9a-f]{32}', generation) or
            type(receipt.get('bundleManifestSha256')) is not str or
            type(receipt.get('ownershipManifestSha256')) is not str or
            receipt.get('pluginRoot') != str(root) or
            receipt.get('bundleManifestSha256') != candidate['manifestSha256'] or
            receipt.get('ownershipManifestSha256') != ownership_sha or
            any(receipt.get(k) != active_record.get(k) for k in
                ('installId', 'generation', 'bundleManifestSha256', 'ownershipManifestSha256')) or
            ownership.get('schema') != OWNERSHIP_SCHEMA or
            ownership.get('installId') != install_id or
            ownership.get('generation') != generation or
            ownership.get('pluginRoot') != str(root) or
            not isinstance(ownership.get('entries'), dict) or
            set(ownership['entries']) != set(ARTIFACTS)):
        _fail('install_receipt_mismatch')
    for relative, live in candidate['files'].items():
        expected = ownership['entries'][relative]
        if (not isinstance(expected, dict) or expected.get('ownerStatus') != 'confirmed' or
                expected.get('role') != 'engine_module' or
                expected.get('exclusive') is not True or expected.get('shared') is not False or
                expected.get('references') != [generation] or
                any(type(expected.get(key)) is not int for key in
                    ('bytes', 'dev', 'ino', 'nlink', 'mtimeNs', 'ctimeNs')) or
                type(expected.get('sha256')) is not str or
                any(expected.get(key) != live[key]
                    for key in ('sha256', 'bytes', 'dev', 'ino', 'nlink', 'mtimeNs', 'ctimeNs'))):
            _fail('installed_file_identity_changed')
    return {'status': 'active_receipt_consistent', 'pluginRoot': str(root),
            'generation': generation, 'installId': install_id,
            'bundleManifestSha256': candidate['manifestSha256'],
            'ownershipManifestSha256': ownership_sha,
            'userQmdInventory': user_inventory,
            'candidateKind': 'source_only_python_candidate',
            'productComplete': False,
            'entry': candidate['files']['core/laya_index_a.py']['path'],
            'limitations': 'caller must authenticate selection journal and recheck before use'}

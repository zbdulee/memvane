"""Explicit read-only inference evidence; separate from training compatibility.

Only prepare() publishes evidence. Prediction never calls prepare, train or
smoke. Full content hashes establish identity; stat snapshots only detect
changes within an owner-controlled generation between verified uses.
"""
from __future__ import annotations
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import stat
import sys
import sysconfig
import time

from .dataset import read_private_json
from .store import canonical

RECEIPT = 'laya-inference-proof.json'
BINDING = 'laya-inference-binding.json'
MANIFEST = 'laya-inference-manifest.json'
SCHEMA = 'qmd-laya-inference-proof-v1'
# The last entry versions the prediction pairing (laya_adapter.PREDICTION_PAIRING):
# a receipt prepared before it fails admission and is prepared again.
CHECKS = ['read_only_load', 'synthetic_forward', 'fixed_selection_mapping',
          'prediction_leading_body_prefix_v1']


def file_sha(path):
    from runtime_admission import hash_file
    return hash_file(path)


def _sha(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def code_identity():
    return _sha({str(p.relative_to(Path(__file__).parent.parent)): file_sha(p)
                 for p in sorted(Path(__file__).parent.parent.rglob('*.py'))})


def _private_state(state):
    from .inference_service import namespace
    return namespace(state)


def runtime_roots():
    """Only a closed venv; external/editable imports require separate review."""
    if not sys.flags.isolated:
        raise ValueError('inference_isolated_entrypoint_required')
    if sys.prefix == sys.base_prefix:
        raise ValueError('inference_runtime_not_isolated')
    cfg = Path(sys.prefix)/'pyvenv.cfg'
    if 'include-system-site-packages = false' not in cfg.read_text().lower():
        raise ValueError('inference_external_library_scope')
    paths = [Path(sysconfig.get_path('purelib')), Path(sysconfig.get_path('platlib')),
             Path(sys.base_prefix)/'lib', Path(sys.executable).resolve(), cfg]
    roots = sorted({p.resolve() for p in paths})
    # Capture any zip stdlib and executable symlink routing as well.
    for p in sys.path:
        if p and p.endswith('.zip') and Path(p).is_file(): roots.append(Path(p).resolve())
    result = []
    for p in sorted(set(roots)):
        if not any(parent in p.parents for parent in result): result.append(p)
    code = Path(__file__).parent.parent.resolve()
    for entry in sys.path:
        p = Path(entry).resolve()
        if not (p == code or any(p == root or root in p.parents for root in result)
                or (p.suffix == '.zip' and Path(sys.base_prefix) in p.parents)):
            raise ValueError('inference_external_library_scope')
    return result


def _module_scope(roots):
    code = Path(__file__).parent.parent.resolve()
    for name, module in tuple(sys.modules.items()):
        origin = getattr(module, '__file__', None)
        if module is sys.modules.get('__main__') or not origin: continue
        # PyTorch exposes two virtual modules whose relative __file__ is a
        # placeholder, not an external import. Bind only the exact instances
        # and implementation types supplied by the manifest-covered modules.
        proxies = {'torch.ops': ('torch._ops','_Ops','ops','_ops.py'),
                   'torch.classes': ('torch._classes','_Classes','classes','_classes.py')}
        if name in proxies:
            parent_name, type_name, instance_name, placeholder = proxies[name]
            parent = sys.modules.get(parent_name)
            values = vars(parent) if parent is not None else {}
            torch_values = vars(sys.modules['torch']) if 'torch' in sys.modules else {}
            if (origin != placeholder or type(module) is not values.get(type_name)
                    or module is not values.get(instance_name)
                    or module is not torch_values.get(instance_name)):
                raise ValueError('inference_external_library_scope')
            origin = values.get('__file__')
            if not origin: raise ValueError('inference_external_library_scope')
        p = Path(origin).resolve()
        if not (p == code or code in p.parents or any(p == r or r in p.parents for r in roots)):
            raise ValueError('inference_external_library_scope')


def device_identity():
    import torch
    return {'os': platform.system(), 'release': platform.release(),
            'machine': platform.machine(), 'host': platform.node(), 'kernel': platform.version(), 'backend': 'mps' if torch.backends.mps.is_available() else 'cpu',
            'torch': torch.__version__, 'executable': sys.executable,
            'prefix': sys.prefix, 'base_prefix': sys.base_prefix,
            'resolved_executable': str(Path(sys.executable).resolve())}


def _stamp(path, *, allow_link=False):
    info = path.lstat()
    if (not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode) or (allow_link and stat.S_ISLNK(info.st_mode)))
            or info.st_uid not in (0, os.getuid()) or (not stat.S_ISLNK(info.st_mode) and info.st_mode & 0o022)):
        raise ValueError('inference_identity_changed')
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
            info.st_ctime_ns, info.st_mode, info.st_uid, info.st_nlink]


def snapshot(roots, *, content=False):
    """Include bytecode/metadata/additions; one stat per warm entry.

    DirEntry avoids repeated lstat/is_dir/is_symlink calls for each file.
    Hash reads additionally re-stat to reject changes while reading content.
    """
    result = {}
    def visit(path, info):
        mode = info.st_mode
        if (not (stat.S_ISREG(mode) or stat.S_ISDIR(mode) or stat.S_ISLNK(mode))
                or info.st_uid not in (0,os.getuid())
                or (not stat.S_ISLNK(mode) and mode & 0o022)):
            raise ValueError('inference_identity_changed')
        stamp = [info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,
                 info.st_ctime_ns,mode,info.st_uid,info.st_nlink]
        if stat.S_ISLNK(mode):
            resolved = Path(path).resolve(strict=True)
            if not any(resolved == Path(root) or Path(root) in resolved.parents for root in roots):
                raise ValueError('inference_identity_changed')
            result[path] = [stamp, 'symlink:'+os.readlink(path)]
        elif stat.S_ISDIR(mode):
            result[path] = [stamp, None]
            with os.scandir(path) as entries:
                for entry in entries: visit(entry.path,entry.stat(follow_symlinks=False))
        else:
            digest = file_sha(path) if content else None
            if content and _stamp(Path(path)) != stamp:
                raise ValueError('inference_identity_changed')
            result[path] = [stamp, digest]
    for root in roots:
        path = str(root)
        visit(path,os.lstat(path))
    return result


def _stamps(manifest):
    return {key: [value[0], value[1] if isinstance(value[1],str) and value[1].startswith('symlink:') else None]
            for key, value in manifest.items()}


def _write(path, row):
    from .local_cycle import _atomic_json
    _atomic_json(path, row)


def _record(state, argv, *, view, verified, manifest=None, before_publish=None):
    """Internal publication after explicit synthetic verification, never predict."""
    state = Path(state)
    roots = [*runtime_roots(), Path(argv[2]).resolve()]
    manifest = snapshot(roots, content=True) if manifest is None else manifest
    if snapshot(roots) != _stamps(manifest): raise ValueError('inference_identity_changed')
    receipt = {'schema': SCHEMA, 'createdAt': time.time(), 'namespace': _private_state(state),
        'executable': argv[0], 'model_dir': argv[2], 'view': view,
        'verified': verified, 'code_sha256': code_identity(),
        'device': device_identity(), 'roots': [str(p) for p in roots],
        'manifest_sha256': _sha(manifest), 'checks': CHECKS,
        'policy_sha256': _sha(read_private_json(state/'live-selector.json')),
        'approval_sha256': _sha(read_private_json(state/'laya-synthetic-attestation.json')),
        'checkpoint_sha256': _sha(read_private_json(state/'active-checkpoint.json'))}
    if before_publish is not None: before_publish()
    _write(state/MANIFEST, manifest)
    _write(state/RECEIPT, receipt)
    # Binding makes a missing/revoked inference receipt fail closed, rather
    # than reverting to legacy training-proof eligibility.
    _write(state/BINDING, {'schema': SCHEMA, 'receipt_sha256': _sha(receipt)})
    return receipt


def has_evidence(state):
    return any((Path(state)/name).exists() or (Path(state)/name).is_symlink()
               for name in (BINDING, RECEIPT, MANIFEST))


def admit(state, argv, view):
    """Cheap client gate. No model hashing, imports or subprocesses."""
    state = Path(state)
    if not has_evidence(state): return None
    try:
        binding = read_private_json(state/BINDING)
        receipt = read_private_json(state/RECEIPT)
        if (binding != {'schema': SCHEMA, 'receipt_sha256': _sha(receipt)}
                or set(receipt) != {'schema','createdAt','namespace','executable','model_dir','view',
                    'verified','code_sha256','device','roots','manifest_sha256','checks',
                    'policy_sha256','approval_sha256','checkpoint_sha256'}
                or receipt['schema'] != SCHEMA or receipt['checks'] != CHECKS
                or type(receipt['createdAt']) not in (int,float)
                or not math.isfinite(receipt['createdAt']) or receipt['createdAt'] > time.time()+1
                or receipt['namespace'] != _private_state(state)
                or receipt['executable'] != argv[0] or receipt['model_dir'] != argv[2]
                or receipt['view'] != view or receipt['code_sha256'] != code_identity()
                or receipt['verified']['adapter_sha256'] != hashlib.sha256(Path(argv[1]).read_bytes()).hexdigest()
                or receipt['policy_sha256'] != _sha(read_private_json(state/'live-selector.json'))
                or receipt['approval_sha256'] != _sha(read_private_json(state/'laya-synthetic-attestation.json'))
                or receipt['checkpoint_sha256'] != _sha(read_private_json(state/'active-checkpoint.json'))):
            raise ValueError('inference_identity_changed')
        from . import resident
        active = read_private_json(state/'active-checkpoint.json')
        if resident._hash(resident._safe_file(active['artifact_path'])) != active['artifact_sha256']:
            raise ValueError('inference_identity_changed')
        return receipt
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ValueError('inference_identity_changed') from exc


class IdentityCache:
    def __init__(self):
        self.code_at_start = code_identity()
        self.generation = None
        self.content = {}
        self.receipts = {}
        self.last_timing = {}

    def verify(self, state, argv, view):
        started = time.perf_counter()
        receipt = admit(state, argv, view)
        if receipt is None: raise ValueError('inference_identity_required')
        if receipt['code_sha256'] != self.code_at_start:
            raise ValueError('inference_identity_changed')
        roots = [*runtime_roots(), Path(argv[2]).resolve()]
        _module_scope(runtime_roots())
        if (receipt['roots'] != [str(p) for p in roots] or argv[0] != sys.executable
                or receipt['device'] != device_identity()):
            raise ValueError('inference_identity_changed')
        key = receipt['manifest_sha256']
        if self.generation is not None and self.generation != key:
            # Imported libraries/loaded objects cannot be refreshed by hashing
            # new files in the same process. Require a newly started worker.
            raise ValueError('inference_identity_changed')
        manifest_path = Path(state)/MANIFEST
        manifest_stamp = _stamp(manifest_path)
        seen = self.receipts.get(str(manifest_path))
        if seen is not None and seen != manifest_stamp: raise ValueError('inference_identity_changed')
        if seen is None:
            if _sha(read_private_json(manifest_path)) != key: raise ValueError('inference_identity_changed')
            self.receipts[str(manifest_path)] = manifest_stamp
        manifest = self.content.get(key)
        hit = manifest is not None
        if manifest is None:
            manifest = read_private_json(Path(state)/MANIFEST)
            if _sha(manifest) != key: raise ValueError('inference_identity_changed')
            current = snapshot(roots, content=True)
            if current != manifest: raise ValueError('inference_identity_changed')
            # Failed changes never adopt a new identity implicitly.
            self.content[key] = manifest
            self.generation = key
        elif snapshot(roots) != _stamps(manifest):
            self.content.pop(key, None)
            raise ValueError('inference_identity_changed')
        # Manifest deletion/replacement/revocation remains visible on warm
        # calls too. Hashing its receipt is not a model/library rehash.
        path = Path(state)/MANIFEST
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError('inference_identity_changed')
        # Full receipt/approval/scope are re-read at each admission above.
        self.last_timing = {'identity_seconds': round(time.perf_counter()-started,6),
                            'identity_cache_hit': hit, 'metadata_subprocesses': 0}
        return {**receipt['verified'], 'runtime_identity_sha256': _sha([
            receipt['verified']['runtime_identity_sha256'], key,
            receipt['device'], receipt['code_sha256']])}


def prepare(state, argv, *, max_seconds=30, max_rss_mib=4096):
    """Explicit approved inference-only preparation in the policy interpreter.

    Requires an unchanged, currently valid existing compatibility/approval
    anchor. Does not extend or rewrite that training receipt. No expiry is
    inferred for the separately verified immutable inference generation.
    """
    from .local_cycle import VerifiedLayaTrainer
    from .inference_service import _load, _forward, _rss_mib, _policy, resident
    from .laya_adapter import LEGACY_INPUT_VIEW
    state = Path(state)
    deadline = time.monotonic()+max_seconds
    _private_state(state)
    raw = read_private_json(state/'live-selector.json')
    _, normalized = _policy(state, 'active-checkpoint.json', deadline=deadline)
    if normalized['trainerArgv'] != argv or argv[0] != sys.executable:
        raise ValueError('inference_prepare_scope_mismatch')
    trainer = VerifiedLayaTrainer(argv, max_seconds=max_seconds, max_rss_mib=max_rss_mib,
                                  proof_cache_dir=state, deadline=deadline)
    verified = trainer.validate_prediction(cwd=state)
    active = read_private_json(state/'active-checkpoint.json')
    view = active.get('model_input_view', LEGACY_INPUT_VIEW)
    artifact = resident._safe_file(active['artifact_path'])
    if file_sha(artifact) != active['artifact_sha256']: raise ValueError('inference_identity_changed')
    roots = [*runtime_roots(), Path(argv[2]).resolve()]
    before = snapshot(roots, content=True)
    _module_scope(runtime_roots())
    code_before, device_before = code_identity(), device_identity()
    anchors = [read_private_json(state/name) for name in
               ('live-selector.json','active-checkpoint.json','laya-synthetic-attestation.json')]
    case = {'request_id':'inference-prepare-synthetic','prompt':'What is the synthetic Cobalt beacon marker?',
            'candidates':[{'id':'synthetic/cobalt','revision_sha256':'0'*64,
                'input_text':'The imaginary Cobalt beacon marker is COBALT-271.', 'input_kind':'full_compact_wiki_body'},
                # Far over any model budget: proves the versioned leading-prefix pairing.
                {'id':'synthetic/overflow','revision_sha256':'1'*64,
                'input_text':'# Synthetic overflow\n' + 'synthetic overflow ' * 700, 'input_kind':'full_compact_wiki_body'}]}
    case['input_sha256'] = _sha(case)
    request = {'schema_version':1,'artifact_path':str(artifact),'cases':[case]}
    if 'model_input_view' in active: request['model_input_view'] = view
    job = {'request':request, 'model_dir':argv[2],
           'family':(verified['runtime_identity_sha256'],verified['base_model_sha256'],view)}
    limits, _ = resident._admission(max_rss_mib, already_running=False)
    if limits is None or _rss_mib() > max_rss_mib or time.monotonic() >= deadline:
        raise ValueError('inference_prepare_resource_deferred')
    loaded = _load(job)
    if time.monotonic() >= deadline or _rss_mib() > max_rss_mib:
        raise ValueError('inference_prepare_resource_deferred')
    _module_scope(runtime_roots())
    timing = {}
    _forward(loaded, job, limits=limits, timing=timing, cancelled=lambda:time.monotonic() >= deadline)
    if timing.get('truncated_pairs') != 1:
        raise ValueError('inference_prepare_truncation_not_exercised')
    trainer._prediction_proof()  # Expiry/revocation during preparation is not renewed.
    if (time.monotonic() >= deadline or _rss_mib() > max_rss_mib or snapshot(roots) != _stamps(before) or code_identity() != code_before
            or device_identity() != device_before or anchors != [read_private_json(state/name) for name in
            ('live-selector.json','active-checkpoint.json','laya-synthetic-attestation.json')]
            or file_sha(artifact) != active['artifact_sha256']):
        raise ValueError('inference_identity_changed')
    def before_publish():
        _module_scope(runtime_roots())
        if (snapshot(roots) != _stamps(before) or code_identity() != code_before
                or device_identity() != device_before or anchors != [read_private_json(state/name) for name in
                    ('live-selector.json','active-checkpoint.json','laya-synthetic-attestation.json')]
                or file_sha(artifact) != active['artifact_sha256']):
            raise ValueError('inference_identity_changed')
        if trainer._prediction_proof() != anchors[2] or time.monotonic() >= deadline:
            raise ValueError('inference_identity_changed')
    return _record(state, argv, view=view, verified=verified, manifest=before, before_publish=before_publish)

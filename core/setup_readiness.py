"""Read-only component readiness; preparing models does not enable learning.

The hook preflight reads only small local metadata. Explicit setup checks reuse
existing compatibility attestations and revalidate versions/bundle hashes; they
never install software or run a new inference/training smoke. The detached
SessionStart worker alone re-attests the selected generation when the shipped
adapter is the only drift; it never installs, downloads or selects a generation.
"""
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import time

from context_learning import runtime_setup as runtime
from context_learning.dataset import read_private_json
from context_learning.laya_adapter import MODEL_FILES, EDIT_INPUT_TOKENS

ADAPTER = Path(__file__).parent / 'context_learning/laya_adapter.py'


def _small_json(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 65536:
        raise ValueError('invalid_model_metadata')
    value = json.loads(path.read_text())
    if not isinstance(value, dict): raise ValueError('invalid_model_metadata')
    return value


def laya_preflight():
    """Cheap, never affirmative readiness; suitable for a local setup notice."""
    result = {'status': 'setup_required', 'ready': False, 'required': True,
              'verification': 'preflight_only', 'targetVersion': runtime.LAYA_PIN,
              'targetPython': runtime.PYTHON_PIN, 'reason': None}
    try:
        root = runtime.default_managed_root()
        if root.exists() or root.is_symlink():
            info = root.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError('unsafe_managed_root')
        pointer = root / 'active.json'
        if not pointer.exists() and not pointer.is_symlink():
            raise ValueError('managed_runtime_not_prepared')
        if pointer.is_symlink() or not pointer.is_file() or pointer.stat().st_size > 65536:
            raise ValueError('invalid_managed_pointer')
        try: active = read_private_json(pointer)
        except (OSError, ValueError): raise ValueError('invalid_managed_pointer')
        if not isinstance(active, dict): raise ValueError('invalid_managed_pointer')
        attestation = active.get('attestation')
        if (not isinstance(attestation, dict) or attestation.get('schema_version') != 1
                or attestation.get('data_kind') != 'synthetic_compact_wiki'
                or attestation.get('checks') != dict.fromkeys(runtime.CHECKS, True)):
            raise ValueError('managed_compatibility_required')
        exe = Path(active['executable'])
        if not exe.is_absolute() or not exe.resolve().is_relative_to(root.resolve()):
            raise ValueError('external_interpreter_dependency')
        if not exe.is_file() or not os.access(exe, os.X_OK):
            raise ValueError('missing_interpreter')
        model = Path(active['model_dir'])
        if not model.is_absolute() or model.is_symlink() or not model.is_dir():
            raise ValueError('local_model_required')
        for name in MODEL_FILES:
            if (model/name).is_symlink() or not (model/name).is_file():
                raise ValueError('local_model_required')
        cfg = _small_json(model/'rl_agent_config.json')
        encoder = _small_json(model/'encoder/config.json')
        tokenizer = _small_json(model/'tokenizer/tokenizer_config.json')
        if (cfg.get('encoder') != 'jhu-clsp/mmBERT-base'
                or (cfg.get('max_len'), cfg.get('head_max_len')) != (1024, 256)
                or any(type(value) is not int or value < EDIT_INPUT_TOKENS for value in
                       (encoder.get('max_position_embeddings'), tokenizer.get('model_max_length')))):
            raise ValueError('edit_model_required')
        # Checked last: this reason means every other local input still matches.
        attested_adapter = Path(active.get('adapter_path') or '')
        if (attestation.get('adapter_sha256') != runtime._sha_file(ADAPTER)
                or not attested_adapter.is_absolute() or attested_adapter.is_symlink()
                or not attested_adapter.is_file()):
            raise ValueError('current_adapter_required')
        result['status'] = 'prepared_unverified'
    except (OSError, ValueError, KeyError, TypeError) as exc:
        reason = str(exc) if isinstance(exc, ValueError) else 'invalid_managed_pointer'
        result['reason'] = 'laya_' + reason
    return result


def _plugin_version():
    try:
        return json.loads((Path(__file__).resolve().parent.parent / 'package.json').read_text())['version']
    except (OSError, ValueError, KeyError, TypeError):
        return None


def adapter_refresh_blocked(queue):
    """True when the worker will not re-attest on its own: a recorded failure
    for this exact adapter/runtime, or a newer host already owns the proof."""
    from context_learning.runtime_installer import (_version_key, adapter_owner_version,
                                                    adapter_refresh_marker)
    try:
        root = runtime.default_managed_root()
        active = read_private_json(root / 'active.json')
        recorded = _version_key(adapter_owner_version(root, active))
        current = _version_key(_plugin_version())
        if recorded is not None and current is not None and current < recorded:
            return True
        marker = adapter_refresh_marker(queue, active, runtime._sha_file(ADAPTER))
    except (OSError, ValueError, KeyError, TypeError):
        return True
    return marker.exists() or marker.is_symlink()


def refresh_adapter_attestation(queue, *, reattest=None):
    """Re-attest adapter-only drift of the selected Laya generation.

    Resource pressure defers to a later SessionStart; a failed smoke is
    recorded once (under the activation lock) so the setup notice returns
    instead of retrying every session.
    """
    from resource_deferred import ResourceDeferred
    if laya_preflight()['reason'] != 'laya_current_adapter_required':
        return {'status': 'not_required'}
    if reattest is None:
        from context_learning.runtime_installer import reattest_active as reattest
    try:
        return reattest(runtime.default_managed_root(), ADAPTER.resolve(),
                        marker_dir=Path(queue), plugin_version=_plugin_version())
    except ResourceDeferred as exc:
        return {'status': 'deferred', 'reason': str(exc)}
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        reason = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        return {'status': 'failed', 'reason': reason}


def _preparation_stamp():
    pointer = runtime.default_managed_root() / 'active.json'
    active = read_private_json(pointer)
    model = Path(active['model_dir'])
    paths = [pointer, ADAPTER, Path(active['adapter_path']), Path(active['executable']).resolve(),
             *(model/name for name in MODEL_FILES)]
    return active, [(str(path), (info.st_dev, info.st_ino, info.st_size,
                                info.st_mtime_ns, info.st_ctime_ns))
                    for path in paths for info in [path.stat()]]


def laya_readiness():
    result = laya_preflight()
    result['verification'] = 'existing_attestation_revalidated'
    # Missing/invalid metadata is enough to reject readiness without executing
    # an interpreter. A valid preflight alone cannot claim model integrity.
    if result['reason'] is not None: return result
    try:
        before = _preparation_stamp()
        selected = runtime.choose_runtime()
        if selected.get('status') != 'selected':
            result.update(status='setup_required', reason='laya_' + selected.get('status', 'runtime_check_failed'))
        else:
            # Re-read small inputs after the full version/hash checks. A new
            # pointer or metadata publication cannot inherit the old result.
            after = laya_preflight()
            if after['reason'] is not None: return after
            if _preparation_stamp() != before:
                result.update(status='setup_required', reason='laya_preparation_changed_during_check')
                return result
            result.update(status='laya_ready', ready=True)
    except (OSError, ValueError, KeyError, TypeError):
        result.update(status='setup_required', reason='laya_runtime_check_failed')
    return result


def components(qmd_ready, laya=None):
    laya = laya_readiness() if laya is None else laya
    overall = bool(qmd_ready and laya['ready'])
    return {'qmdReady': bool(qmd_ready), 'layaReady': laya['ready'], 'overallReady': overall,
            'componentReadiness': {
                'qmd': {'status': 'qmd_ready' if qmd_ready else 'setup_required', 'ready': bool(qmd_ready)},
                'laya': laya,
                'overall': {'status': 'overall_ready' if overall else 'setup_required', 'ready': overall}}}


def qmd_model_reason():
    """Explicit common-store model check; no QMD invocation or vector writes."""
    import qmd_shared_store as store
    import shared_qmd_setup as bootstrap
    try:
        selected = store.select()
        proof_path = store._private_file(Path(selected['generation'])/'bootstrap-proof.json')
        if proof_path.stat().st_size > 65536:
            return 'qmd_embedding_model_proof_required'
        proof = json.loads(proof_path.read_text())
        if not isinstance(proof, dict) or proof.get('schema') != bootstrap.SCHEMA:
            return 'qmd_embedding_model_proof_required'
        current = bootstrap._model(proof.get('modelPath'))
        if current != {'path': proof['modelPath'], 'sha256': proof['modelSha256']}:
            return 'qmd_embedding_model_changed_requires_review'
        return None
    except ValueError as exc:
        return 'qmd_local_embedding_model_required' if str(exc) == 'local_embedding_model_required' else 'qmd_embedding_model_proof_required'
    except (OSError, KeyError, TypeError):
        return 'qmd_embedding_model_proof_required'

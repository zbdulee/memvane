"""Explicit local Laya choice for a complete, source-fresh wiki candidate pool.

The caller keeps the baseline choice for every unavailable/invalid result. The
owner-only policy names the repository's Laya adapter and a local interpreter;
no teacher transport or network configuration is inherited by the child.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import time

from .compact_input import derive_for_selection
from .contracts import digest
from .dataset import read_private_json
from .local_cycle import VerifiedLayaTrainer, _safe_file
from .store import canonical


def _file_hash(path):
    checksum = hashlib.sha256()
    with Path(path).open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            checksum.update(block)
    return checksum.hexdigest()


def choose(state_dir, prompt, rows, *, corpus_fingerprint, deadline=None,
           pointer_name='active-checkpoint.json'):
    """Return (selected IDs or None, reason, bounded resource observation).

    A real 0-card prediction is []. None means use the preexisting QMD topN.
    Rows contain current complete file bytes, never a QMD excerpt.
    """
    if not isinstance(corpus_fingerprint, str) or len(corpus_fingerprint) != 64:
        return None, 'corpus_unavailable', {}
    if (not isinstance(prompt, str) or not prompt.strip() or len(prompt.encode()) > 65536
            or not isinstance(rows, list) or not 1 <= len(rows) <= 30):
        return None, 'invalid_pool', {}
    started = time.perf_counter()
    state = Path(state_dir)
    try:
        policy = read_private_json(state / 'live-selector.json')
        if (not isinstance(policy, dict) or set(policy) != {
                'schema', 'trainerArgv', 'timeoutSeconds', 'maxRssMib'}
                or policy['schema'] != 'qmd-live-selector-v1'
                or not isinstance(policy['trainerArgv'], list)
                or len(policy['trainerArgv']) != 3
                or any(not isinstance(x, str) or not Path(x).is_absolute()
                       for x in policy['trainerArgv'])
                or type(policy['timeoutSeconds']) is not int
                or not 1 <= policy['timeoutSeconds'] <= 30
                or type(policy['maxRssMib']) is not int
                or not 128 <= policy['maxRssMib'] <= 8192):
            return None, 'invalid_policy', {}
        adapter = Path(__file__).with_name('laya_adapter.py').resolve()
        from .adapter_binding import BindingTimeout, attested_adapter, inference_argv
        try:
            argv = inference_argv(policy['trainerArgv'], adapter, deadline=deadline,
                                  attested_adapter_sha256=attested_adapter(state))
        except BindingTimeout:
            return None, 'timeout', {}
        except ValueError as exc:
            return None, ('adapter_anchor_missing' if str(exc) == 'adapter_anchor_missing'
                          else 'unsupported_adapter'), {}
        policy = {**policy, 'trainerArgv': argv}
        if pointer_name not in ('active-checkpoint.json', 'experimental-checkpoint.json'):
            return None, 'invalid_pointer', {}
        active = read_private_json(state / pointer_name)
        if not isinstance(active, dict) or active.get('schema_version') != 1:
            return None, 'no_active_checkpoint', {}
        if pointer_name=='active-checkpoint.json':
            from .candidate_apply import readable
            readable(state)
            if active.get('application') is not None:
                from .learning_profile import NAME,selection_enabled
                profile=read_private_json(state/NAME)['proposal']
                if not selection_enabled(state,Path(profile['projectRoot'])):return None,'profile_selection_disabled',{}
        from .laya_adapter import EDIT_INPUT_VIEW
        if (pointer_name == 'experimental-checkpoint.json'
                and active.get('model_input_view') == EDIT_INPUT_VIEW):
            # The ordinary live selector still pins the preserved English
            # base. Edit-view heads are trained against the separately
            # validated multilingual shared policy; mixing bases must fail.
            from .shared import _policy as shared_policy, inference_argv
            shared = shared_policy(state)
            if shared.get('modelInputView') != EDIT_INPUT_VIEW:
                return None, 'invalid_shared_model_input_view', {}
            policy = {**policy, 'trainerArgv': inference_argv(shared, 'predict'),
                      'maxRssMib': shared['maxRssMib']}
        if (active.get('model_input_view') != EDIT_INPUT_VIEW and len(prompt) > 2000):
            return None, 'invalid_pool', {}
        artifact = _safe_file(active.get('artifact_path'))
        if _file_hash(artifact) != active.get('artifact_sha256'):
            return None, 'checkpoint_changed', {}
        candidates = []
        seen = set()
        for row in rows:
            if (not isinstance(row, dict) or set(row) != {
                    'id', 'revision_sha256', 'source_text'}
                    or not isinstance(row['id'], str) or not row['id']
                    or row['id'] in seen):
                return None, 'invalid_pool', {}
            seen.add(row['id'])
            compact = derive_for_selection(row['source_text'], row['revision_sha256'])
            candidates.append({'id': row['id'], 'revision_sha256': row['revision_sha256'],
                               'input_text': compact['body_text'],
                               'input_kind': compact['input_kind']})
        case = {'request_id': 'live-' + digest(canonical([prompt, [r['id'] for r in rows]]))[:24],
                'prompt': prompt, 'candidates': candidates}
        case['input_sha256'] = digest(canonical(case))
        remaining = deadline - time.monotonic() if deadline is not None else policy['timeoutSeconds']
        if remaining < 1:
            return None, 'timeout', {}
        request = {'schema_version': 1,
                   'artifact_path': str(artifact), 'cases': [case]}
        if active.get('model_input_view') is not None:
            request['model_input_view'] = active['model_input_view']
        # Validate before creating a shared process or socket. No prediction
        # can mint training evidence, even when capture/autoCycle are disabled.
        from .inference_identity import admit
        from .laya_adapter import LEGACY_INPUT_VIEW
        admission_started = time.perf_counter()
        inference_receipt = admit(state, policy['trainerArgv'],
                                  active.get('model_input_view', LEGACY_INPUT_VIEW))
        if inference_receipt is None:
            trainer = VerifiedLayaTrainer(policy['trainerArgv'],
                max_seconds=min(policy['timeoutSeconds'], max(1, int(remaining))),
                max_rss_mib=policy['maxRssMib'], proof_cache_dir=state,
                deadline=deadline)
            trainer.validate_prediction(cwd=state)
        client_identity_seconds = time.perf_counter() - admission_started
        from .inference_service import predict as service_predict
        service_deadline = min(deadline, time.monotonic() + policy['timeoutSeconds']) if deadline is not None else (
            time.monotonic() + policy['timeoutSeconds'])
        result, resources = service_predict(state, policy, request,
            active['artifact_sha256'], deadline=service_deadline,
            pointer_name=pointer_name)
        expected = {'schema_version': 1, 'predictions': [{
            'request_id': case['request_id'], 'input_sha256': case['input_sha256'],
            'selected_ids': result.get('predictions', [{}])[0].get('selected_ids')}]}
        if result != expected:
            return None, 'invalid_prediction', resources
        selected = result['predictions'][0]['selected_ids']
        if (not isinstance(selected, list) or len(selected) > 3
                or len(set(selected)) != len(selected) or
                any(not isinstance(x, str) or x not in seen for x in selected)):
            return None, 'invalid_prediction', resources
        return selected, 'selected', {
            **resources, 'client_identity_seconds': round(client_identity_seconds, 6), 'model_version': active.get('cycle_id'),
            'model_input_view': active.get('model_input_view'),
            'selector_wall_seconds': round(time.perf_counter() - started, 6)}
    except (OSError, ValueError, KeyError, TypeError, IndexError, json.JSONDecodeError) as exc:
        reason = str(exc)
        if reason == 'trainer_timeout':
            reason = 'timeout'
        elif reason == 'trainer_inference_attestation_required' or reason.startswith('inference_identity_'):
            reason = 'inference_attestation_required'
        elif reason == 'trainer_memory_budget':
            reason = 'memory_budget'
        elif reason.startswith('trainer_failed:'):
            reason = reason.split(':', 1)[1]
        elif reason.startswith('resident_'):
            reason = reason[:80]
        elif reason in ('trainer_failed', 'invalid_trainer_executable',
                        'missing_local_model_dir', 'laya_runtime_not_verified',
                        'laya_runtime_or_source_changed') or reason.startswith('missing_local_model_file:'):
            reason = 'runtime_unavailable'
        else:
            reason = 'selector_unavailable'
        timing = getattr(exc, 'timing', {})
        return None, reason, {
            **(timing if isinstance(timing, dict) else {}),
            'selector_wall_seconds': round(time.perf_counter() - started, 6)}

"""Owner-approved, output-only training on a frozen derived real-edit split.

The live manifest remains embargoed until a separately reviewed freeze removes
that flag. No normal hook invokes this module and no pointer is written here.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import stat
import time

from . import derived_split, isolated_train
from .dataset import read_private_json
from .laya_adapter import EDIT_INPUT_VIEW
from .store import canonical


SCHEMA = 'qmd-isolated-derived-runtime-approval-v1'
APPROVAL_RELATIVE = '.config/laya/isolated-derived-approval/current.json'
APPROVAL_KEYS = {
    'schema', 'enabled', 'operation', 'projectRoot', 'sharedRoot',
    'manifestPath', 'manifestSha256', 'planPath', 'auditPath', 'screenPath',
    'sharedPolicySha256', 'parentApprovalSha256', 'realEditApprovalSha256',
    'approvedAdapterPath', 'approvedAdapterSha256', 'runnerSha256',
    'splitReaderSha256', 'derivedRunnerSha256', 'pythonPath',
    'pythonExecutableSha256', 'modelPath', 'modelIdentitySha256',
    'modelInputView', 'baseArtifactSha256', 'trainPayloadSha256',
    'developmentPayloadSha256', 'omittedActiveGoldCaseIds',
    'outputDirectory', 'publishPointer',
    'expiresAt',
}


def _approval():
    path = Path.home() / APPROVAL_RELATIVE
    if not path.parent.exists():
        raise ValueError('derived_runtime_approval_missing')
    isolated_train._owner_directory(path.parent)
    if path.is_symlink() or not path.is_file():
        raise ValueError('derived_runtime_approval_missing')
    info = path.stat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_nlink != 1 or info.st_mode & 0o077):
        raise ValueError('derived_runtime_approval_unsafe')
    value = read_private_json(path)
    if (not isinstance(value, dict) or set(value) != APPROVAL_KEYS
            or value['schema'] != SCHEMA or value['enabled'] is not True
            or value['operation'] != 'train'
            or value['publishPointer'] is not False
            or type(value['expiresAt']) not in (int, float)
            or not time.time() < value['expiresAt'] <= time.time() + 7 * 86400):
        raise ValueError('derived_runtime_approval_invalid')
    for key in ('manifestSha256', 'sharedPolicySha256', 'parentApprovalSha256',
                'realEditApprovalSha256', 'approvedAdapterSha256', 'runnerSha256',
                'splitReaderSha256', 'derivedRunnerSha256',
                'pythonExecutableSha256', 'modelIdentitySha256',
                'baseArtifactSha256', 'trainPayloadSha256',
                'developmentPayloadSha256'):
        if not isinstance(value[key], str) or not re.fullmatch('[0-9a-f]{64}', value[key]):
            raise ValueError('derived_runtime_approval_invalid')
    for key in ('manifestPath', 'planPath', 'auditPath', 'screenPath',
                'projectRoot', 'sharedRoot', 'approvedAdapterPath',
                'pythonPath', 'modelPath', 'outputDirectory'):
        if not isinstance(value[key], str) or not Path(value[key]).is_absolute():
            raise ValueError('derived_runtime_approval_invalid')
    omitted = value['omittedActiveGoldCaseIds']
    if (not isinstance(omitted, list) or omitted != sorted(set(omitted))
            or not all(isinstance(rid, str) for rid in omitted)):
        raise ValueError('derived_runtime_approval_invalid')
    return path, value


def _check_payload_pins(payload, approval):
    if (payload['train_payload_sha256'] != approval['trainPayloadSha256']
            or payload['development_payload_sha256'] != approval['developmentPayloadSha256']
            or payload['omitted_active_gold_case_ids'] != approval['omittedActiveGoldCaseIds']):
        raise ValueError('derived_payload_mapping_mismatch')


def preflight(project, *, require_output_missing=True):
    """Read-only full provenance and payload check; embargo always wins."""
    from .shared import _policy, read_only_validation
    from .synthetic_experiment import approval as parent_approval, load, shared_for, story_for
    from .synthetic_scope import APPROVAL_FILE, _synthetic_bundle, policy_for
    from edit_context import _approved

    project = Path(project).resolve(strict=True)
    root = shared_for(project)
    with read_only_validation():
        approval_path, value = _approval()
        story = story_for(project).resolve(strict=True)
        _synthetic_bundle(root, project)
        policy = _policy(root)
        scope = policy_for(root, project)
        parent_approval(root, project, *load(root, project), policy=scope)
        _approved(project, story)
        adapter = Path(isolated_train.__file__).with_name('laya_adapter.py').resolve()
        python = Path(policy['trainerArgv'][0])
        model = Path(policy['trainerArgv'][2])
        checks = {
            'project': value['projectRoot'] == str(project),
            'root': value['sharedRoot'] == str(root),
            'policy': value['sharedPolicySha256'] == isolated_train._sha(root / 'shared-policy.json'),
            'parent': value['parentApprovalSha256'] == isolated_train._sha(APPROVAL_FILE),
            'edit': value['realEditApprovalSha256'] == isolated_train._sha(
                Path.home() / '.config/laya/real-edit-approval/current.json'),
            'adapter_path': value['approvedAdapterPath'] == str(adapter),
            'adapter_sha': value['approvedAdapterSha256'] == isolated_train._sha(adapter),
            'runner_sha': value['runnerSha256'] == isolated_train._sha(isolated_train.__file__),
            'split_reader_sha': value['splitReaderSha256'] == isolated_train._sha(derived_split.__file__),
            'derived_runner_sha': value['derivedRunnerSha256'] == isolated_train._sha(__file__),
            'python_path': value['pythonPath'] == str(python),
            'python_executable': python.is_file() and os.access(python, os.X_OK),
            'python_sha': value['pythonExecutableSha256'] == isolated_train._sha(
                python.resolve(strict=True)),
            'model_path': value['modelPath'] == str(model),
            'model_identity': value['modelIdentitySha256'] == isolated_train.isolated_model_identity(model),
            'input_view': value['modelInputView'] == policy['modelInputView'] == EDIT_INPUT_VIEW,
            'base': value['baseArtifactSha256'] == isolated_train._sha(policy['baseArtifact']),
        }
        for name, valid in checks.items():
            if not valid:
                raise ValueError(f'derived_runtime_mapping_mismatch:{name}')
        pointers = isolated_train._pointer_snapshot(root, project)
        if pointers[str(root / 'experimental-checkpoint.json')] is not None:
            raise ValueError('derived_experimental_pointer_present')
        if any(sha == 'unsafe' for sha in pointers.values()):
            raise ValueError('derived_pointer_unsafe')
        history_pointer = Path.home() / 'Library/Application Support/qmd-auto-context/learning/history-active.json'
        if pointers[str(history_pointer)] is not None:
            raise ValueError('derived_history_activation_unsupported')
        payload = derived_split.prepare(root, project, story,
            value['manifestPath'], value['manifestSha256'], {
                'plan': value['planPath'], 'audit': value['auditPath'],
                'screen': value['screenPath']})
        if payload['manifest']['training_embargo'] is not False:
            raise ValueError('derived_training_embargoed')
        _check_payload_pins(payload, value)
        parent = Path.home() / isolated_train.OUTPUT_RELATIVE
        isolated_train._owner_directory(parent)
        output = Path(value['outputDirectory'])
        if (output.parent != parent or output.is_symlink()
                or not re.fullmatch('[a-z0-9][a-z0-9-]{0,79}', output.name)
                or require_output_missing and output.exists()):
            raise ValueError('derived_output_path_invalid')
        if not require_output_missing:
            isolated_train._owner_directory(output)
    evidence = {'schema': SCHEMA, 'manifestSha256': value['manifestSha256'],
                'trainPayloadSha256': value['trainPayloadSha256'],
                'developmentPayloadSha256': value['developmentPayloadSha256'],
                'omittedActiveGoldCaseIds': value['omittedActiveGoldCaseIds'],
                'modelIdentitySha256': value['modelIdentitySha256'],
                'modelInputView': EDIT_INPUT_VIEW}
    return {'project': project, 'root': root, 'policy': policy, 'approval': value,
            'approval_sha256': isolated_train._sha(approval_path),
            'output': output, 'pointers': pointers,
            'evidence_sha256': hashlib.sha256(canonical(evidence).encode()).hexdigest(),
            'train': payload['train'], 'holdout': payload['development'],
            'manifest_sha256': value['manifestSha256']}


def run(project):
    plan = preflight(project)
    return isolated_train._run_plan(plan, lambda: preflight(
        project, require_output_missing=False), validation_label='development')

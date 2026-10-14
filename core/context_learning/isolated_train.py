"""One explicitly approved, output-only multilingual training run.

This is deliberately separate from shared.train_due: it cannot publish a
trial, reconcile case membership, change an approval pin, or dispatch gold.
The owner-held runtime mapping is not created by this module.
"""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import time

from .dataset import read_private_json
from .laya_adapter import EDIT_INPUT_VIEW, model_identity
from .local_cycle import VerifiedLayaTrainer, _atomic_json
from .store import canonical


SCHEMA = 'qmd-isolated-multilingual-runtime-approval-v1'
APPROVAL_RELATIVE = '.config/qmd/isolated-train-approval/current.json'
OUTPUT_RELATIVE = '.local/share/qmd-auto-context/isolated-runs'
_CASE_KEYS = {'requestId', 'inputSha256', 'goldSha256', 'evidenceSha256',
              'ticketSha256'}
_APPROVAL_KEYS = {
    'schema', 'enabled', 'operation', 'projectRoot', 'sharedRoot',
    'sharedPolicySha256', 'parentApprovalSha256', 'realEditApprovalSha256',
    'historicalTrainerPath', 'historicalTrainerSha256',
    'approvedAdapterPath', 'approvedAdapterSha256', 'runnerSha256',
    'pythonPath', 'pythonExecutableSha256', 'modelPath',
    'modelIdentitySha256', 'modelInputView', 'baseArtifactSha256',
    'trainCases', 'holdoutCases', 'excludedCases',
    'trainPayloadSha256', 'holdoutPayloadSha256', 'outputDirectory',
    'publishPointer', 'expiresAt',
}


def _sha(path):
    checksum = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            checksum.update(block)
    return checksum.hexdigest()


def isolated_model_identity(model_dir):
    """Pin every file the local model/tokenizer directory loaders can see.

    The historical adapter identity remains unchanged because existing local
    artifacts use it. This stronger identity is an additional isolated-run
    admission pin; it includes additions as well as modifications beneath the
    two directories passed to Transformers and Laya.
    """
    root = Path(model_dir)
    if root.is_symlink():
        raise ValueError('isolated_model_path_unsafe')
    base = model_identity(root)
    entries = {}
    for name in ('encoder', 'tokenizer'):
        directory = root / name
        if directory.is_symlink() or not directory.is_dir():
            raise ValueError('isolated_model_path_unsafe')
        for path in sorted(directory.rglob('*')):
            relative = path.relative_to(root).as_posix()
            info = path.lstat()
            if stat.S_ISDIR(info.st_mode):
                entries[relative + '/'] = None
            elif stat.S_ISLNK(info.st_mode):
                # Hugging Face snapshots commonly link each file to a shared
                # blob. Pin both the effective bytes and the link destination;
                # never traverse a directory link into an unbounded tree.
                target = path.resolve(strict=True)
                if not target.is_file():
                    raise ValueError('isolated_model_path_unsafe')
                entries[relative] = {'sha256': _sha(path),
                                     'linkTarget': os.readlink(path)}
            elif stat.S_ISREG(info.st_mode):
                entries[relative] = {'sha256': _sha(path)}
            else:
                raise ValueError('isolated_model_path_unsafe')
    return hashlib.sha256(canonical({'schema': 'qmd-isolated-model-inputs-v1',
        'legacyModelSha256': base, 'loaderEntries': entries}).encode()).hexdigest()


def _train_cases_payload(cases):
    return [{k: v for k, v in case.items() if k != 'project_key'}
            for case in cases]


def _predict_cases_payload(cases):
    return [{k: v for k, v in case.items()
             if k not in ('selected_ids', 'project_key')} for case in cases]


def _owner_directory(path):
    path = Path(path)
    if path.is_symlink() or not path.is_dir():
        raise ValueError('isolated_owner_directory_missing')
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError('isolated_owner_directory_unsafe')


def _approval_file():
    path = Path.home() / APPROVAL_RELATIVE
    _owner_directory(path.parent)
    if path.is_symlink() or not path.is_file():
        raise ValueError('isolated_runtime_approval_missing')
    info = path.stat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_nlink != 1 or info.st_mode & 0o077):
        raise ValueError('isolated_runtime_approval_unsafe')
    return path


def _pointer_snapshot(root, project):
    # No pointer is ever a writer target here. Detect an unexpected concurrent
    # publication before accepting prediction/score evidence.
    paths = (root / 'experimental-checkpoint.json',
             root / 'active-checkpoint.json',
             project / '.auto-context/active-checkpoint.json',
             Path.home() / 'Library/Application Support/qmd-auto-context/learning/history-active.json')
    return {str(path): _sha(path) if path.is_file() and not path.is_symlink()
            else 'unsafe' if path.is_symlink() else None for path in paths}


def _case_ref(row):
    return {'requestId': row[1], 'inputSha256': row[2],
            'goldSha256': hashlib.sha256(row[5].encode()).hexdigest(),
            # The split and family keys control holdout independence even
            # when captured sample and gold bytes stay unchanged.
            'evidenceSha256': hashlib.sha256(canonical([
                row[0], row[6], row[7], row[8], json.loads(row[9]), row[10]
            ]).encode()).hexdigest(),
            'ticketSha256': hashlib.sha256(canonical(row[11]).encode()).hexdigest()}


def _case_list(value, expected_count):
    if (not isinstance(value, list) or len(value) != expected_count
            or any(not isinstance(row, dict) or set(row) != _CASE_KEYS
                   or not isinstance(row['requestId'], str)
                   or any(not isinstance(row[key], str)
                          or not re.fullmatch('[0-9a-f]{64}', row[key])
                          for key in ('inputSha256', 'goldSha256',
                                      'evidenceSha256', 'ticketSha256')) for row in value)
            or len({row['requestId'] for row in value}) != len(value)
            or value != sorted(value, key=lambda row: row['requestId'])):
        raise ValueError('isolated_case_approval_invalid')


def _mapping(root, project, policy, *, require_output_missing):
    from . import shared
    from .synthetic_scope import APPROVAL_FILE
    from .synthetic_experiment import story_for
    from edit_context import _approved

    path = _approval_file()
    value = read_private_json(path)
    if (not isinstance(value, dict) or set(value) != _APPROVAL_KEYS
            or value['schema'] != SCHEMA or value['enabled'] is not True
            or value['operation'] != 'train' or value['publishPointer'] is not False
            or type(value['expiresAt']) not in (int, float)
            or not time.time() < value['expiresAt'] <= time.time() + 7 * 86400):
        raise ValueError('isolated_runtime_approval_invalid')
    for key, count in (('trainCases', 4), ('holdoutCases', 1), ('excludedCases', 1)):
        _case_list(value[key], count)
    ids = [row['requestId'] for key in ('trainCases', 'holdoutCases', 'excludedCases')
           for row in value[key]]
    if len(set(ids)) != 6:
        raise ValueError('isolated_case_approval_invalid')

    adapter = Path(__file__).with_name('laya_adapter.py').resolve()
    python = Path(policy['trainerArgv'][0])
    model = Path(policy['trainerArgv'][2])
    checks = {
        'historical_policy': shared._historical_trainer_policy(policy),
        'historical_path': value['historicalTrainerPath'] == policy['trainerArgv'][1],
        'historical_sha': value['historicalTrainerSha256'] == shared.V45_ADAPTER_SHA256,
        'adapter_path': value['approvedAdapterPath'] == str(adapter),
        'adapter_sha': value['approvedAdapterSha256'] == _sha(adapter),
        'runner_sha': value['runnerSha256'] == _sha(__file__),
        'python_path': value['pythonPath'] == str(python),
        'python_executable': python.is_file() and os.access(python, os.X_OK),
        'python_sha': value['pythonExecutableSha256'] == _sha(python.resolve(strict=True)),
        'model_path': value['modelPath'] == str(model),
        'model_identity': value['modelIdentitySha256'] == isolated_model_identity(model),
        'input_view': value['modelInputView'] == policy['modelInputView'] == EDIT_INPUT_VIEW,
        'base_sha': value['baseArtifactSha256'] == _sha(policy['baseArtifact']),
        'policy_sha': value['sharedPolicySha256'] == _sha(root / 'shared-policy.json'),
        'parent_sha': value['parentApprovalSha256'] == _sha(APPROVAL_FILE),
        'edit_sha': value['realEditApprovalSha256'] == _sha(
            Path.home() / '.config/qmd/real-edit-approval/current.json'),
        'project_root': value['projectRoot'] == str(project),
        'shared_root': value['sharedRoot'] == str(root),
    }
    for name, valid in checks.items():
        if not valid:
            raise ValueError(f'isolated_runtime_mapping_mismatch:{name}')
    # Check the existing capture-time approval chain without rotating it.
    _approved(project, story_for(project))

    parent = Path.home() / OUTPUT_RELATIVE
    _owner_directory(parent)
    output = Path(value['outputDirectory'])
    if (not output.is_absolute() or output.parent != parent
            or not re.fullmatch('[a-z0-9][a-z0-9-]{0,79}', output.name)
            or output.is_symlink()
            or require_output_missing and output.exists()):
        raise ValueError('isolated_output_path_invalid')
    if not require_output_missing:
        _owner_directory(output)
    return value, path, output


def _eligible_rows(root, project, scope):
    from .shared import (_case_active, _case_reference, _cycle_holdouts,
                         _case, DATABASE_NAME)
    from .synthetic_scope import allowed_case
    from .edit_ticket import validate_case

    db_path = root / DATABASE_NAME
    if db_path.is_symlink() or not db_path.is_file():
        raise ValueError('isolated_database_missing')
    with closing(sqlite3.connect(f'file:{db_path}?mode=ro', uri=True, timeout=1)) as db:
        rows = db.execute(
            "SELECT project_key,request_id,input_sha256,sample,bodies,gold,split,"
            "project_root,corpus_fingerprint,family_keys,trained_cycle FROM cases "
            "WHERE status='gold' AND use_state='active' "
            "AND json_extract(sample,'$.schema_version')=4 "
            "ORDER BY request_id").fetchall()
        if len(rows) != 6 or any(row[7] != str(project) for row in rows):
            raise ValueError('isolated_extra_or_missing_gold_case')
        _, clean, excluded, diagnostic = _cycle_holdouts(rows, None)
        train = [row for row in rows if row[6] == 'train']
        other_holdout = [row for row in rows if row[6] == 'holdout'
                         and row[1] not in {item[1] for item in clean}]
        if (len(train) != 4 or len(clean) != 1 or len(other_holdout) != 1
                or len(excluded) != 1 or diagnostic is not None
                or excluded[0]['reason'] != 'training_family_overlap'):
            raise ValueError('isolated_holdout_not_independent')
        tickets = {}
        for row in rows:
            sample = json.loads(row[3]); bodies = json.loads(row[4])
            ticket = validate_case(db, root, project, sample, bodies)
            if (sample.get('schema_version') != 4
                    or not _case_active(root, (str(project), row[1], row[3]))
                    or allowed_case(root, project, sample, bodies)[0] != scope
                    or ticket is None):
                raise ValueError('isolated_case_provenance_changed')
            before = db.execute('SELECT before_sha256 FROM edit_tickets WHERE ticket_id=?',
                                (sample['edit_event']['ticket_id'],)).fetchone()
            if before is None:
                raise ValueError('isolated_case_provenance_changed')
            tickets[row[1]] = {'validated_ticket': list(ticket),
                               'before_sha256': before[0],
                               'edit_event': sample['edit_event']}
            _case(row[:6])  # Recheck complete gold and candidate body contract.
        return tuple([(*row, tickets[row[1]]) for row in group]
                     for group in (train, clean, other_holdout))


def preflight(project, *, require_output_missing=True):
    """Read-only validation; never authorizes training without owner mapping."""
    from .shared import _policy, read_only_validation
    from .synthetic_experiment import approval as parent_approval, load, shared_for
    from .synthetic_scope import _synthetic_bundle, policy_for

    project = Path(project).resolve(strict=True)
    root = shared_for(project)
    with read_only_validation():
        _synthetic_bundle(root, project)
        policy = _policy(root)
        scope = policy_for(root, project)
        parent_approval(root, project, *load(root, project), policy=scope)
        value, approval_path, output = _mapping(
            root, project, policy, require_output_missing=require_output_missing)
        pointers = _pointer_snapshot(root, project)
        if pointers[str(root / 'experimental-checkpoint.json')] is not None:
            raise ValueError('isolated_experimental_pointer_present')
        if any(sha == 'unsafe' for sha in pointers.values()):
            raise ValueError('isolated_pointer_unsafe')
        # This output-only path reads the legacy shared DB. An activated
        # history may select another database, so fail before mixing epochs.
        history_pointer = Path.home() / 'Library/Application Support/qmd-auto-context/learning/history-active.json'
        if pointers[str(history_pointer)] is not None:
            raise ValueError('isolated_history_activation_unsupported')
        train, holdout, excluded = _eligible_rows(root, project, scope)
        for key, rows in (('trainCases', train), ('holdoutCases', holdout),
                          ('excludedCases', excluded)):
            actual = sorted((_case_ref(row) for row in rows), key=lambda row: row['requestId'])
            if actual != value[key]:
                raise ValueError('isolated_case_mapping_mismatch')
        from .shared import _case
        train_cases = [{**_case(row[:6]), 'capture_provenance': row[11]}
                       for row in train]
        holdout_cases = [{**_case(row[:6]), 'capture_provenance': row[11]}
                         for row in holdout]
        payload_hashes = {
            'trainPayloadSha256': hashlib.sha256(canonical(
                _train_cases_payload(train_cases)).encode()).hexdigest(),
            'holdoutPayloadSha256': hashlib.sha256(canonical(
                _predict_cases_payload(holdout_cases)).encode()).hexdigest(),
        }
        if any(value[key] != actual for key, actual in payload_hashes.items()):
            raise ValueError('isolated_payload_mapping_mismatch')
    pinned = {'train': value['trainCases'], 'holdout': value['holdoutCases'],
              'excluded': value['excludedCases'], **payload_hashes,
              'modelIdentitySha256': value['modelIdentitySha256'],
              'modelInputView': EDIT_INPUT_VIEW}
    evidence_sha = hashlib.sha256(canonical(pinned).encode()).hexdigest()
    return {'project': project, 'root': root, 'policy': policy, 'approval': value,
            'approval_sha256': _sha(approval_path), 'output': output,
            'pointers': pointers, 'evidence_sha256': evidence_sha,
            'train': train_cases, 'holdout': holdout_cases}


def _make_trainer(plan):
    from .shared import resource_report
    policy = plan['policy']
    argv = [policy['trainerArgv'][0], plan['approval']['approvedAdapterPath'],
            policy['trainerArgv'][2]]
    return VerifiedLayaTrainer(argv, max_seconds=policy['safetySeconds'],
        max_rss_mib=policy['maxRssMib'], proof_cache_dir=plan['output'],
        pressure_probe=lambda: resource_report(policy, admission=False), require_monitor=True,
        isolated_temp_dir=plan['output'])


def _step_loss_trace(answer, expected_pairs):
    """Require a complete, ordered numeric trace before accepting a derived run."""
    if (not isinstance(answer, dict) or set(answer) !=
            {'schema_version', 'status', 'step_losses'}
            or answer['schema_version'] != 1 or answer['status'] != 'trained'
            or not isinstance(answer['step_losses'], list)
            or not answer['step_losses']):
        raise ValueError('isolated_train_loss_trace_invalid')
    total = 0
    for index, row in enumerate(answer['step_losses'], 1):
        if (not isinstance(row, dict) or set(row) !=
                {'step', 'pairs', 'cross_entropy', 'wall_seconds'}
                or type(row['step']) is not int or row['step'] != index
                or type(row['pairs']) is not int or not 1 <= row['pairs'] <= 2
                or type(row['cross_entropy']) not in (int, float)
                or not math.isfinite(row['cross_entropy'])
                or row['cross_entropy'] < 0
                or type(row['wall_seconds']) not in (int, float)
                or not math.isfinite(row['wall_seconds'])
                or row['wall_seconds'] < 0):
            raise ValueError('isolated_train_loss_trace_invalid')
        total += row['pairs']
    if total != expected_pairs:
        raise ValueError('isolated_train_loss_trace_invalid')
    return answer['step_losses']


def run(project):
    """Write only inside the newly approved isolated output directory."""
    return _run_plan(preflight(project), lambda: preflight(
        project, require_output_missing=False))


def _run_plan(plan, revalidate, *, validation_label='holdout'):
    """Shared output-only runner; each admission path revalidates its own pins."""
    from .shared import _predict, _score, resource_report

    before = resource_report(plan['policy'], detailed=True)
    if not before['ready']:
        from .local_cycle import ResourceDeferred
        # Output-only approval does not authorize a shared-root journal write.
        raise ResourceDeferred('isolated_resource_deferred', before)
    output = plan['output']
    output.mkdir(mode=0o700, exist_ok=False)
    started = time.perf_counter()
    stages = {}

    def still_current():
        fresh = revalidate()
        if (fresh['evidence_sha256'] != plan['evidence_sha256']
                or fresh['approval_sha256'] != plan['approval_sha256']
                or fresh['pointers'] != plan['pointers']):
            raise ValueError('isolated_inputs_changed')

    try:
        still_current()
        trainer = _make_trainer(plan)
        base = Path(plan['policy']['baseArtifact'])
        base_sha = _sha(base)
        train, holdout = plan['train'], plan['holdout']
        if base_sha != plan['approval']['baseArtifactSha256']:
            raise ValueError('isolated_base_changed')
        def predict(artifact, cases, label):
            began = time.perf_counter()
            score, resources = _predict(trainer, output, artifact, cases, label,
                                        input_view=EDIT_INPUT_VIEW)
            stages[label] = {'wall_seconds': round(time.perf_counter()-began, 3),
                             'resources': resources, 'score': score}

        predict(base, train, 'train-base')
        predict(base, holdout, validation_label + '-base')
        still_current()
        artifact = output / 'candidate.artifact'
        payload = {'schema_version': 1,
                   'train': _train_cases_payload(train),
                   'artifact_path': str(artifact),
                   'manifest_sha256': plan.get('manifest_sha256', plan['evidence_sha256']),
                   'model_input_view': EDIT_INPUT_VIEW}
        derived = plan.get('manifest_sha256') is not None
        if derived:
            payload['record_step_losses'] = True
        began = time.perf_counter()
        answer, resources = trainer.call('train', payload, output / 'train.json',
                                         cwd=output)
        stages['train'] = {'wall_seconds': round(time.perf_counter()-began, 3),
                           'resources': resources}
        if derived:
            stages['train']['step_losses'] = _step_loss_trace(answer,
                sum(len(case['candidates']) for case in train))
        elif answer != {'schema_version': 1, 'status': 'trained'}:
            raise ValueError('isolated_train_result_invalid')
        if not artifact.is_file():
            raise ValueError('isolated_train_result_invalid')
        still_current()
        predict(artifact, train, 'train-candidate')
        predict(artifact, holdout, validation_label + '-candidate')
        still_current()
        result = {'schema': 'qmd-isolated-multilingual-result-v1',
                  'status': 'isolated_complete',
                  'approval_sha256': plan['approval_sha256'],
                  'evidence_sha256': plan['evidence_sha256'],
                  'source_manifest_sha256': plan.get('manifest_sha256'),
                  'model_input_view': EDIT_INPUT_VIEW,
                  'base_artifact_sha256': base_sha,
                  'candidate_artifact_sha256': _sha(artifact),
                  'wall_seconds': round(time.perf_counter()-started, 3),
                  'resources_before': before,
                  'resources_after': resource_report(plan['policy'], detailed=True),
                  'stages': stages, 'published_pointer': False}
        _atomic_json(output / 'result.json', result)
        return {'status': 'isolated_complete', 'output_directory': str(output),
                'candidate_artifact_sha256': result['candidate_artifact_sha256'],
                'evidence_sha256': plan['evidence_sha256']}
    except Exception as exc:
        from .local_cycle import ResourceDeferred
        _atomic_json(output / 'failure.json', {'schema':
            'qmd-isolated-multilingual-failure-v1',
            'status': 'deferred_resource' if isinstance(exc, ResourceDeferred) else 'failed',
            'completionAck': False,
            'reason': str(exc)[:80], 'evidence_sha256': plan['evidence_sha256']})
        raise

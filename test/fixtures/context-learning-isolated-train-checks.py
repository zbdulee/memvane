"""Disposable six-case approval test; no model, provider, or live state."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from unittest.mock import patch
from contextlib import redirect_stdout
from io import StringIO

from context_learning import isolated_train, shared, synthetic_scope
from context_learning.laya_adapter import EDIT_INPUT_VIEW, MODEL_FILES
from context_learning.store import canonical
from context_learning.contracts import digest
from context_learning import runtime_setup


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def denied(call, code):
    try:
        call()
    except ValueError as exc:
        assert str(exc) == code, (str(exc), code)
    else:
        raise AssertionError('unapproved operation reached training')


trace = [{'step': 1, 'pairs': 2, 'cross_entropy': 0.7, 'wall_seconds': 0.12},
         {'step': 2, 'pairs': 1, 'cross_entropy': 0.4, 'wall_seconds': 0.09}]
assert isolated_train._step_loss_trace(
    {'schema_version': 1, 'status': 'trained', 'step_losses': trace}, 3) == trace
denied(lambda: isolated_train._step_loss_trace(
    {'schema_version': 1, 'status': 'trained', 'step_losses': trace}, 4),
    'isolated_train_loss_trace_invalid')
denied(lambda: isolated_train._step_loss_trace(
    {'schema_version': 1, 'status': 'trained', 'step_losses':
     [{**trace[0], 'cross_entropy': float('nan')}, trace[1]]}, 3),
    'isolated_train_loss_trace_invalid')


with tempfile.TemporaryDirectory(prefix='qmd-isolated-train-', dir='/tmp') as tmp:
    home = Path(tmp).resolve()
    project = home / 'story'
    root = home / 'shared'
    project.mkdir(); root.mkdir()
    approval_dir = home / '.config/laya/isolated-train-approval'
    approval_dir.mkdir(parents=True)
    approval_dir.chmod(0o700)
    output_parent = home / isolated_train.OUTPUT_RELATIVE
    output_parent.mkdir(parents=True)
    output_parent.chmod(0o700)
    parent_approval = home / 'parent-approval.json'
    parent_approval.write_text('{}')
    real_edit = home / '.config/laya/real-edit-approval/current.json'
    real_edit.parent.mkdir(parents=True)
    real_edit.write_text('{}')
    policy_file = root / 'shared-policy.json'
    policy_file.write_text('{}')
    base = root / 'base.artifact'
    base.write_bytes(b'base model fixture')
    base.chmod(0o600)
    model = home / 'cached-mmbert'
    model.mkdir()
    (model / 'rl_agent_config.json').write_text(json.dumps({
        'encoder': 'jhu-clsp/mmBERT-base', 'max_len': 1024, 'head_max_len': 256}))
    for name in MODEL_FILES:
        path = model / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists(): path.write_bytes(('synthetic ' + name).encode())
    special_tokens = model / 'tokenizer/special_tokens_map.json'
    special_blob = home / 'cached-tokenizer-blob.json'
    special_blob.write_text('{"mask_token":"[MASK]"}')
    special_tokens.symlink_to(special_blob)
    old = (home / '.codex/plugins/cache/qmd-shared-v21-local/qmd-auto-context/'
           '0.35.2-dev.20261009-fantasy-pilot-v45/core/context_learning/laya_adapter.py')
    policy = {'trainerArgv': [sys.executable, str(old), str(model)],
              'modelInputView': EDIT_INPUT_VIEW, 'baseArtifact': str(base),
              'safetySeconds': 300, 'maxRssMib': 4096}
    adapter = Path(isolated_train.__file__).with_name('laya_adapter.py')
    # Only this disposable fixture approves the candidate adapter's exact bytes.
    # Production REVIEWED_ADAPTER_SHA256 and installed grants stay unchanged.
    shared.REVIEWED_ADAPTER_SHA256 = sha(adapter)
    assert sha(adapter) == shared.REVIEWED_ADAPTER_SHA256

    rows = []
    for slot in range(6):
        split = 'train' if slot < 4 else 'holdout'
        candidate_id = 'shared-fact' if slot in (0, 4) else f'fact-{slot}'
        body = 'shared fact' if slot in (0, 4) else f'unique fact {slot}'
        sample = {'schema_version': 4, 'prompt': {'text': f'Question {slot}'},
                  'edit_event': {'ticket_id': f'{slot:032x}'},
                  'candidates': [{'candidate_id': candidate_id,
                                  'revision_sha256': hashlib.sha256(body.encode()).hexdigest()}]}
        sample_json = canonical(sample)
        input_sha = digest(sample_json)
        gold = canonical({'input_sha256': input_sha,
                          'selected_ids': [] if slot == 5 else [candidate_id]})
        rows.append(('project-key', f'request-{slot}', input_sha, sample_json,
                     canonical({candidate_id: body}), gold, split, str(project),
                     'corpus-fingerprint', '[]', None, 'gold', 'active', f'{slot:02d}'))
    db_path = root / shared.DATABASE_NAME
    with sqlite3.connect(db_path) as db:
        db.execute('CREATE TABLE cases (project_key TEXT,request_id TEXT,input_sha256 TEXT,'
                   'sample TEXT,bodies TEXT,gold TEXT,split TEXT,project_root TEXT,'
                   'corpus_fingerprint TEXT,family_keys TEXT,trained_cycle TEXT,'
                   'status TEXT,use_state TEXT,captured_at TEXT)')
        db.execute('CREATE TABLE edit_tickets (ticket_id TEXT,before_sha256 TEXT)')
        db.executemany('INSERT INTO cases VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)', rows)
        db.executemany('INSERT INTO edit_tickets VALUES (?,?)',
                       [(f'{slot:032x}', f'{slot:064x}') for slot in range(6)])
    db_path.chmod(0o600)
    db_before = sha(db_path)
    with shared.read_only_validation(), shared._db(root) as read_db:
        assert read_db.execute('SELECT COUNT(*) FROM cases').fetchone()[0] == 6
        try:
            read_db.execute('CREATE TABLE forbidden_output(value TEXT)')
        except sqlite3.OperationalError:
            pass
        else:
            raise AssertionError('read-only validation wrote into shared DB')
    assert sha(db_path) == db_before
    eligible = shared._cycle_holdouts(rows, None)
    assert len(eligible[1]) == 1 and eligible[1][0][1] == 'request-5'
    assert len(eligible[2]) == 1 and eligible[2][0]['reason'] == 'training_family_overlap'

    def with_ticket(row):
        event = json.loads(row[3])['edit_event']
        ticket = {'validated_ticket': ['validated'],
                  'before_sha256': f'{int(event["ticket_id"], 16):064x}',
                  'edit_event': event}
        return (*row[:11], ticket)

    def refs(rows):
        return sorted((isolated_train._case_ref(with_ticket(row)) for row in rows),
                      key=lambda row: row['requestId'])

    def payloads(rows):
        cases = [{**shared._case(row[:6]), 'capture_provenance': with_ticket(row)[11]}
                 for row in sorted(rows, key=lambda row: row[1])]
        return cases

    def payload_sha(cases):
        return hashlib.sha256(canonical(cases).encode()).hexdigest()

    approval = {
        'schema': isolated_train.SCHEMA, 'enabled': True, 'operation': 'train',
        'projectRoot': str(project), 'sharedRoot': str(root),
        'sharedPolicySha256': sha(policy_file),
        'parentApprovalSha256': sha(parent_approval),
        'realEditApprovalSha256': sha(real_edit),
        'historicalTrainerPath': str(old),
        'historicalTrainerSha256': shared.V45_ADAPTER_SHA256,
        'approvedAdapterPath': str(adapter),
        'approvedAdapterSha256': sha(adapter),
        'runnerSha256': sha(isolated_train.__file__),
        'pythonPath': sys.executable,
        'pythonExecutableSha256': sha(Path(sys.executable).resolve()),
        'modelPath': str(model),
        'modelIdentitySha256': isolated_train.isolated_model_identity(model),
        'modelInputView': EDIT_INPUT_VIEW,
        'baseArtifactSha256': sha(base),
        'trainCases': refs(rows[:4]), 'holdoutCases': refs(rows[5:]),
        'excludedCases': refs(rows[4:5]),
        'trainPayloadSha256': payload_sha(isolated_train._train_cases_payload(payloads(rows[:4]))),
        'holdoutPayloadSha256': payload_sha(isolated_train._predict_cases_payload(payloads(rows[5:]))),
        'outputDirectory': str(output_parent / 'approved-run'),
        'publishPointer': False, 'expiresAt': isolated_train.time.time() + 3600,
    }
    approval_path = approval_dir / 'current.json'
    def save(value):
        approval_path.write_text(canonical(value))
        approval_path.chmod(0o600)
    save(approval)

    trainer_calls = []
    class FakeTrainer:
        def call(self, operation, request, output, *, cwd):
            trainer_calls.append(operation)
            assert Path(cwd) == output_parent / 'approved-run'
            assert all('capture_provenance' in case for case in
                       (request['train'] if operation == 'train' else request['cases']))
            ordered = request['train'] if operation == 'train' else request['cases']
            assert [case['request_id'] for case in ordered] == sorted(
                case['request_id'] for case in ordered)
            if operation == 'train':
                Path(request['artifact_path']).write_bytes(b'candidate checkpoint')
                Path(request['artifact_path']).chmod(0o600)
                answer = {'schema_version': 1, 'status': 'trained'}
            else:
                answer = {'predictions': [
                    {'request_id': case['request_id'],
                     'input_sha256': case['input_sha256'],
                     'selected_ids': []} for case in request['cases']]}
            Path(output).write_text(canonical(answer))
            return answer, {'fake': True}

    patches = [
        patch.object(Path, 'home', return_value=home),
        patch.object(synthetic_scope, 'APPROVAL_FILE', parent_approval),
        patch('context_learning.synthetic_experiment.shared_for', return_value=root),
        patch('context_learning.synthetic_experiment.story_for', return_value=project),
        patch('context_learning.synthetic_experiment.load', return_value=('control','state')),
        patch('context_learning.synthetic_experiment.approval', return_value=None),
        patch('context_learning.synthetic_scope._synthetic_bundle', return_value=None),
        patch('context_learning.synthetic_scope.policy_for', return_value='scope'),
        patch('context_learning.synthetic_scope.allowed_case', return_value=('scope', None)),
        patch('edit_context._approved', return_value=None),
        patch('context_learning.edit_ticket.validate_case', return_value=('validated',)),
        patch.object(shared, '_policy', return_value=policy),
        patch.object(shared, '_case_active', return_value=True),
        patch.object(shared, 'resource_report', return_value={'ready': True}),
        patch.object(isolated_train, '_make_trainer', return_value=FakeTrainer()),
    ]
    from contextlib import ExitStack
    with ExitStack() as stack:
        for item in patches: stack.enter_context(item)
        denied(lambda: (approval_path.unlink(), isolated_train.preflight(project)),
               'isolated_runtime_approval_missing')
        save(approval)
        assert shared._historical_trainer_policy(policy), 'historical path'
        assert approval['approvedAdapterPath'] == str(adapter.resolve()), 'adapter path'
        assert isolated_train._sha(adapter) == shared.REVIEWED_ADAPTER_SHA256, 'adapter hash'
        assert approval['pythonExecutableSha256'] == isolated_train._sha(
            Path(policy['trainerArgv'][0]).resolve(strict=True)), 'python executable'
        assert approval['modelIdentitySha256'] == isolated_train.isolated_model_identity(model), 'model'
        assert approval['sharedPolicySha256'] == isolated_train._sha(root / 'shared-policy.json'), 'policy'
        assert approval['parentApprovalSha256'] == isolated_train._sha(synthetic_scope.APPROVAL_FILE), 'parent'
        checks = {
            'historicalSha': approval['historicalTrainerSha256'] == shared.V45_ADAPTER_SHA256,
            'adapterSha': approval['approvedAdapterSha256'] == shared.REVIEWED_ADAPTER_SHA256,
            'runnerSha': approval['runnerSha256'] == isolated_train._sha(isolated_train.__file__),
            'pythonPath': approval['pythonPath'] == str(Path(policy['trainerArgv'][0])),
            'pythonFile': Path(policy['trainerArgv'][0]).is_file(),
            'pythonExec': os.access(policy['trainerArgv'][0], os.X_OK),
            'modelPath': approval['modelPath'] == str(Path(policy['trainerArgv'][2])),
            'inputView': approval['modelInputView'] == EDIT_INPUT_VIEW,
            'baseSha': approval['baseArtifactSha256'] == isolated_train._sha(policy['baseArtifact']),
            'realEditSha': approval['realEditApprovalSha256'] == isolated_train._sha(real_edit),
            'projectRoot': approval['projectRoot'] == str(project),
            'sharedRoot': approval['sharedRoot'] == str(root),
        }
        assert all(checks.values()), checks
        from context_learning.dataset import read_private_json
        loaded = read_private_json(approval_path)
        assert loaded == approval, (loaded, approval)
        assert loaded['historicalTrainerPath'] == policy['trainerArgv'][1]
        assert loaded['approvedAdapterPath'] == str(adapter.resolve())
        assert loaded['pythonExecutableSha256'] == isolated_train._sha(Path(sys.executable).resolve(strict=True))
        assert loaded['modelIdentitySha256'] == isolated_train.isolated_model_identity(model)
        assert loaded['modelInputView'] == policy['modelInputView']
        assert loaded['realEditApprovalSha256'] == isolated_train._sha(
            Path.home() / '.config/laya/real-edit-approval/current.json')
        assert isolated_train.preflight(project)['evidence_sha256']
        mutations = [
            ('operation', 'predict', 'isolated_runtime_approval_invalid'),
            ('publishPointer', True, 'isolated_runtime_approval_invalid'),
            ('approvedAdapterSha256', '0' * 64, 'isolated_runtime_mapping_mismatch:adapter_sha'),
            ('pythonPath', '/bin/false', 'isolated_runtime_mapping_mismatch:python_path'),
            ('modelIdentitySha256', '0' * 64, 'isolated_runtime_mapping_mismatch:model_identity'),
            ('runnerSha256', '0' * 64, 'isolated_runtime_mapping_mismatch:runner_sha'),
            ('trainCases', approval['trainCases'] + approval['holdoutCases'],
             'isolated_case_approval_invalid'),
            ('baseArtifactSha256', '0' * 64, 'isolated_runtime_mapping_mismatch:base_sha'),
            ('outputDirectory', str(root / 'experimental-checkpoint.json'),
             'isolated_output_path_invalid'),
        ]
        for key, changed, reason in mutations:
            save({**approval, key: changed})
            denied(lambda: isolated_train.preflight(project), reason)
        save(approval)
        original_tokens = special_tokens.read_bytes()
        special_tokens.write_text('{"mask_token":"[NEW_MASK]"}')
        denied(lambda: isolated_train.preflight(project),
               'isolated_runtime_mapping_mismatch:model_identity')
        special_tokens.write_bytes(original_tokens)
        added_tokenizer = model / 'tokenizer/added_tokens.json'
        added_tokenizer.write_text('{}')
        denied(lambda: isolated_train.preflight(project),
               'isolated_runtime_mapping_mismatch:model_identity')
        added_tokenizer.unlink()
        assert isolated_train.preflight(project)['evidence_sha256']
        with sqlite3.connect(db_path) as db:
            db.execute("UPDATE cases SET captured_at='99' WHERE request_id='request-0'")
        # Capture timestamps are not part of the training order or payload.
        assert isolated_train.preflight(project)['evidence_sha256']
        with sqlite3.connect(db_path) as db:
            db.execute("UPDATE cases SET captured_at='00' WHERE request_id='request-0'")
            original_bodies = db.execute(
                "SELECT bodies FROM cases WHERE request_id='request-1'").fetchone()[0]
            changed_bodies = json.loads(original_bodies)
            changed_bodies['fact-1'] = 'Changed synthetic candidate body'
            db.execute("UPDATE cases SET bodies=? WHERE request_id='request-1'",
                       (canonical(changed_bodies),))
        denied(lambda: isolated_train.preflight(project),
               'isolated_payload_mapping_mismatch')
        with sqlite3.connect(db_path) as db:
            db.execute("UPDATE cases SET bodies=? WHERE request_id='request-1'",
                       (original_bodies,))
        with sqlite3.connect(db_path) as db:
            db.execute("UPDATE cases SET family_keys=? WHERE request_id='request-1'",
                       (canonical(['target:changed']),))
        denied(lambda: isolated_train.preflight(project), 'isolated_case_mapping_mismatch')
        with sqlite3.connect(db_path) as db:
            db.execute("UPDATE cases SET family_keys='[]' WHERE request_id='request-1'")
            db.execute("UPDATE cases SET project_key='changed' WHERE request_id='request-1'")
        denied(lambda: isolated_train.preflight(project), 'isolated_case_mapping_mismatch')
        with sqlite3.connect(db_path) as db:
            db.execute("UPDATE cases SET project_key='project-key' WHERE request_id='request-1'")
        history_pointer = (home / 'Library/Application Support/qmd-auto-context/'
                           'learning/history-active.json')
        history_pointer.parent.mkdir(parents=True)
        history_pointer.write_text('{}')
        denied(lambda: isolated_train.preflight(project),
               'isolated_history_activation_unsupported')
        history_pointer.unlink()
        db_before = sha(db_path)
        from types import SimpleNamespace
        scratch = output_parent / 'probe-scratch'
        scratch.mkdir(mode=0o700)
        observed = []
        def probe_subprocess(argv, **kwargs):
            observed.append((kwargs['cwd'], kwargs['env']['TMPDIR']))
            return SimpleNamespace(returncode=1, stdout=b'', stderr=b'')
        with patch.object(runtime_setup.subprocess, 'run', side_effect=probe_subprocess):
            denied(lambda: runtime_setup._run_probe(sys.executable, temp_dir=scratch),
                   'runtime_probe_failed')
        assert len(observed) == 1 and Path(observed[0][0]).parent == scratch
        assert observed[0][1] == str(scratch)
        assert not trainer_calls
        import context_learning_isolated as isolated_cli
        stream = StringIO()
        with redirect_stdout(stream):
            assert isolated_cli.main(['preflight', str(project)]) == 0
        ready = json.loads(stream.getvalue())
        assert ready['status'] == 'ready' and ready['train_cases'] == 4
        assert ready['independent_holdout_cases'] == 1
        stream = StringIO()
        with redirect_stdout(stream):
            assert isolated_cli.main(['run', str(project)]) == 0
        outcome = json.loads(stream.getvalue())
        assert outcome['status'] == 'isolated_complete'
        assert trainer_calls == ['predict', 'predict', 'train', 'predict', 'predict']
        assert sha(db_path) == db_before
        assert not (root / 'experimental-checkpoint.json').exists()
        assert (output_parent / 'approved-run/result.json').is_file()
        denied(lambda: isolated_train.preflight(project), 'isolated_output_path_invalid')
    print(json.dumps({'validated': True, 'rejectedMutations': len(mutations) + 1,
                      'outputOnly': True}))

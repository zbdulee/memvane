"""Synthetic control-flow guard; no model, QMD, or provider process is called."""
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from unittest.mock import patch

from context_learning.shared import (
    _run_impl, _v45_adapter_path, launch, run, wake_after_index,
)


with tempfile.TemporaryDirectory(prefix='qmd-gold-only-') as name:
    root = Path(name).resolve() / 'shared'
    root.mkdir(mode=0o700)
    project = Path(name) / 'synthetic-project'
    project.mkdir()
    request_ids = ['12345678-1234-1234-1234-123456789abc']
    current_policy = {'checked': True, 'trainerArgv': ['/x', '/x', '/x']}
    historical_policy = {'checked': True, 'trainerArgv': [
        '/x', str(_v45_adapter_path()), '/x']}
    def forbidden(*_args, **_kwargs):
        raise AssertionError('checkpoint path reached')
    with patch.dict(os.environ, {'QMD_SHARED_GOLD_ONLY': '1',
                                 'QMD_SHARED_GOLD_REQUEST_IDS': json.dumps(request_ids)}), \
            patch('context_learning.shared._policy', return_value=current_policy), \
            patch('context_learning.synthetic_scope._synthetic_bundle') as bundle, \
            patch('context_learning.synthetic_scope.policy_for') as scope, \
            patch('context_learning.shared.recover_awaiting_edits'), \
            patch('context_learning.shared.recover_scope_deferred_edits'), \
            patch('context_learning.synthetic_teacher.process_pending',
                  return_value={'status': 'drained'}) as teacher, \
            patch('context_learning.shared.observe_trial', side_effect=forbidden), \
            patch('context_learning.shared.train_due', side_effect=forbidden), \
            patch('context_learning.shared.rollback', side_effect=forbidden):
        result = _run_impl(root, project=project)
    assert result == {'status': 'gold_only', 'teacher': {'status': 'drained'},
                      'observation': {'status': 'skipped_gold_only'},
                      'cycle': {'status': 'skipped_gold_only'}}
    bundle.assert_called_once_with(root, project)
    scope.assert_called_once_with(root, project)
    teacher.assert_called_once_with(root, project, current_policy,
                                    request_ids=request_ids)
    with patch.dict(os.environ, {'QMD_SHARED_GOLD_ONLY': '1',
                                 'QMD_SHARED_GOLD_REQUEST_IDS': json.dumps(request_ids)}), \
            patch('context_learning.shared._policy', return_value=current_policy), \
            patch('context_learning.synthetic_scope._synthetic_bundle'), \
            patch('context_learning.synthetic_scope.policy_for',
                  side_effect=ValueError('scope_unproven')), \
            patch('context_learning.shared.recover_awaiting_edits'), \
            patch('context_learning.shared.recover_scope_deferred_edits'), \
            patch('context_learning.synthetic_teacher.process_pending') as denied_teacher, \
            patch('context_learning.shared.observe_trial', side_effect=forbidden), \
            patch('context_learning.shared.train_due', side_effect=forbidden):
        denied = _run_impl(root, project=project)
    assert denied['teacher'] == {'status': 'denied', 'reason': 'scope_unproven'}
    denied_teacher.assert_not_called()
    assert not (root / 'experimental-checkpoint.json').exists()

    with patch.dict(os.environ, {'QMD_SHARED_GOLD_ONLY': '1',
                                 'QMD_SHARED_GOLD_REQUEST_IDS': ''}), \
            patch('context_learning.shared._policy', return_value=current_policy), \
            patch('context_learning.synthetic_teacher.process_pending') as no_ids_teacher:
        assert _run_impl(root, project=project) == {'status': 'gold_allowlist_required'}
    no_ids_teacher.assert_not_called()

    unrelated = root / 'unrelated-queued.sqlite3'
    with sqlite3.connect(unrelated) as db:
        db.execute('CREATE TABLE cases (request_id TEXT, status TEXT)')
        db.execute('INSERT INTO cases VALUES (?,?)', ('unrelated-case', 'queued'))
    unchanged = unrelated.read_bytes()
    with patch.dict(os.environ, {'QMD_SHARED_GOLD_ONLY': '1',
                                 'QMD_SHARED_GOLD_REQUEST_IDS': '',
                                 'QMD_JOB_ID': ''}), \
            patch('context_learning.shared.sync_reviewed') as sync, \
            patch('context_learning.shared._run_impl') as worker:
        assert run(root, project=project, sync_config={'enabled': True}) == {
            'status': 'gold_allowlist_required'}
        sync.assert_not_called()
        worker.assert_not_called()
    with patch.dict(os.environ, {'QMD_SHARED_GOLD_ONLY': '1',
                                 'QMD_SHARED_GOLD_REQUEST_IDS': json.dumps(request_ids),
                                 'QMD_JOB_ID': ''}), \
            patch('context_learning.shared._policy', return_value=current_policy), \
            patch('context_learning.shared.sync_reviewed') as sync, \
            patch('context_learning.shared._run_impl',
                  return_value={'status': 'gold_only'}) as worker:
        assert run(root, project=project, sync_config={'enabled': True}) == {
            'status': 'gold_only'}
        sync.assert_not_called()
        worker.assert_called_once_with(root, project=project, wait_for_lock=False)
    assert unrelated.read_bytes() == unchanged
    with sqlite3.connect(unrelated) as db:
        assert db.execute('SELECT status FROM cases WHERE request_id=?',
                          ('unrelated-case',)).fetchone() == ('queued',)

    # The real hook would launch a detached worker immediately and again after
    # index ACK. Gold-only collection must require a reviewed exact allowlist.
    with patch.dict(os.environ, {'QMD_SHARED_GOLD_ONLY': '1',
                                 'QMD_SHARED_GOLD_REQUEST_IDS': json.dumps(request_ids)}), \
            patch('context_learning.shared._safe_root', return_value=root), \
            patch('context_learning.shared._policy', return_value=current_policy), \
            patch('context_learning.shared.subprocess.Popen') as child, \
            patch('context_learning.shared.job_telemetry.emit'):
        assert launch({}, project) == {'status': 'deferred_gold_only_manual_review'}
        child.assert_not_called()
        assert launch({}, project, after_index=True) == {
            'status': 'deferred_gold_only_manual_review'}
        child.assert_not_called()
    # The hook persists a full-worker request and its durable wake; only the
    # runtime dispatcher starts the owned child (see learning_worker_step).
    with patch.dict(os.environ, {'QMD_SHARED_GOLD_ONLY': ''}), \
            patch('context_learning.shared._safe_root', return_value=root), \
            patch('context_learning.shared._policy', return_value=current_policy), \
            patch('context_learning.shared.subprocess.Popen') as child, \
            patch('runtime_work.request', return_value={'status': 'queued'}) as wake, \
            patch('context_learning.shared.job_telemetry.emit'):
        assert launch({}, project) == {'status': 'queued'}
        child.assert_not_called()
        wake.assert_called_once_with('learning', str(project.resolve()))
        assert json.loads((root / 'full-worker-request.json').read_text())['afterIndex'] is False
    # The owned child gets a whitelisted environment: no gold-only override.
    from context_learning.shared import learning_worker_step
    with patch.dict(os.environ, {'QMD_SHARED_GOLD_ONLY': '1'}), \
            patch('context_learning.shared.subprocess.Popen') as child:
        assert learning_worker_step(root, project, 1)['status'] == 'started'
        assert 'QMD_SHARED_GOLD_ONLY' not in child.call_args.kwargs['env']
        assert child.call_args.args[0][3] == 'run-owned-worker'
    (root / 'full-worker-request.json').unlink()
    (root / 'full-worker-state.json').unlink()
    # A selected profile has no capture/teacher phase in its normal worker.
    # Only its durable training wake may start that worker.
    with patch.dict(os.environ, {'QMD_SHARED_GOLD_ONLY': ''}), \
            patch('context_learning.shared._safe_root', return_value=root), \
            patch('context_learning.shared._policy', return_value=current_policy), \
            patch('context_learning.learning_profile.present', return_value=True), \
            patch('context_learning.shared.subprocess.Popen') as child, \
            patch('runtime_work.request', return_value={'status':'queued'}) as wake:
        assert launch({}, project) == {'status':'queued'}
        wake.assert_called_once_with('learning',str(project.resolve()))
        child.assert_not_called()
    with patch.dict(os.environ, {'QMD_SHARED_GOLD_ONLY': ''}), \
            patch('context_learning.shared._safe_root', return_value=root), \
            patch('context_learning.shared._policy', return_value=historical_policy), \
            patch('context_learning.shared.subprocess.Popen') as child, \
            patch('context_learning.synthetic_teacher.process_pending') as teacher:
        assert launch({}, project) == {'status': 'runtime_approval_required'}
        assert _run_impl(root, project=project) == {'status': 'runtime_approval_required'}
        child.assert_not_called()
        teacher.assert_not_called()
    with patch('context_learning.synthetic_scope.NOVEL_ROOT', project), \
            patch('context_learning.synthetic_scope._synthetic_bundle'), \
            patch('context_learning.synthetic_scope.policy_for') as scope, \
            patch('context_learning.synthetic_experiment.shared_for', return_value=root), \
            patch('config.find_project_config', return_value={'config': {}}), \
            patch('context_learning.shared._safe_root', return_value=root), \
            patch('context_learning.shared.recover_awaiting_edits') as awaiting, \
            patch('context_learning.shared.recover_scope_deferred_edits') as deferred, \
            patch('context_learning.shared.launch') as child:
        with patch.dict(os.environ, {'QMD_SHARED_GOLD_ONLY': '1'}), \
                patch('context_learning.shared._policy', return_value=current_policy):
            assert wake_after_index(project) == {
                'status': 'deferred_gold_only_manual_review'}
        with patch.dict(os.environ, {'QMD_SHARED_GOLD_ONLY': ''}), \
                patch('context_learning.shared._policy', return_value=historical_policy):
            assert wake_after_index(project) == {'status': 'runtime_approval_required'}
        awaiting.assert_not_called()
        deferred.assert_not_called()
        scope.assert_not_called()
        child.assert_not_called()
    print(json.dumps({'provenanceChecked': True, 'teacherCalled': True,
                      'checkpointSkipped': True, 'manualGoldGuard': True}))

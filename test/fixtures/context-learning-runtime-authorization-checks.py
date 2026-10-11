"""Disposable execution approval checks; never touches a live policy."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'core'))

from context_learning import runtime_authorization as auth
from context_learning import shared
from context_learning import synthetic_experiment, synthetic_scope
from context_learning.laya_adapter import EDIT_INPUT_VIEW


def save_private(path, data):
    path.write_text(json.dumps(data, sort_keys=True))
    path.chmod(0o600)


with tempfile.TemporaryDirectory(prefix='qmd-runtime-auth-', dir='/tmp') as tmp:
    base = Path(tmp).resolve()
    project = base / 'novel'; project.mkdir(mode=0o700)
    story = project / 'story'; story.mkdir(mode=0o700)
    root = project / 'shared'; root.mkdir(mode=0o700)
    adapter = base / 'reviewed.py'; adapter.write_text('print("reviewed")\n')
    peer_adapter = base / 'peer-reviewed.py'
    peer_adapter.write_bytes(adapter.read_bytes())
    python = base / 'python'; python.write_text('runtime\n')
    model = base / 'model'; model.mkdir(mode=0o700)
    save_private(model / 'rl_agent_config.json',
                 {'encoder': 'jhu-clsp/mmBERT-base', 'max_len': 1024,
                  'head_max_len': 256})
    historical = base / 'historical.py'
    policy = {'schema': shared.SCHEMA, 'enabled': True,
              'trainerArgv': [str(python), str(historical), str(model)],
              'modelInputView': EDIT_INPUT_VIEW, 'goldModel': 'gpt-6.1-sol',
              'maxGoldCalls': 20, 'batchMin': 5, 'batchMax': 20,
              'targetExact': .95, 'baseArtifact': str(base / 'base.artifact'),
              'maxRssMib': 4096, 'minAvailableMib': 128,
              'maxLoadPerCpu': 4, 'safetySeconds': 900,
              'experimental': True}
    save_private(root / 'shared-policy.json', policy)
    save_private(root / 'synthetic-teacher-policy.json', {'consent': 'scoped'})
    with shared._db_at(root / 'shared-learning.sqlite3'):
        pass
    current_sha = hashlib.sha256(adapter.read_bytes()).hexdigest()
    consent = {'receiver': 'codex-cli', 'purpose': 'selection-gold',
               'model': 'gpt-6.1-sol'}
    start = time.time_ns() // 1_000_000 + 60_000
    expiry = start + 24 * 60 * 60 * 1000
    cohort = {'schema': auth.COHORT_SCHEMA, 'id': 'fresh-edit-cohort-v1',
              'notBeforeUnixMs': start,
              'trainEditPrefixes': [
                  '01_Settings/qmd-prospective/tr-world-lantern/',
                  '02_Characters/qmd-prospective/tr-character-keeper/',
                  '03_Plot/qmd-prospective/tr-plot-bridge/',
                  '01_Settings/qmd-prospective/tr-support-catalog/',
                  '01_Settings/qmd-prospective/tr-world-echo/'],
              'validationEditPrefixes': [
                  '01_Settings/qmd-prospective/va-world-tide/',
                  '02_Characters/qmd-prospective/va-character-mapmaker/',
                  '03_Plot/qmd-prospective/va-plot-archive/',
                  '01_Settings/qmd-prospective/va-support-seal/',
                  '02_Characters/qmd-prospective/va-character-healer/'],
              'minTrainCases': 5, 'minQualityTrainCases': 100,
              'minTrainFamilies': 5, 'minValidationCases': 50,
              'minValidationFamilies': 5,
              'minEmptyValidation': 10, 'baselineRawPaths': []}
    with patch.object(synthetic_scope, 'NOVEL_ROOT', project), patch.object(
            synthetic_scope, 'policy_for', return_value=consent), patch.object(
            synthetic_experiment, 'shared_for', return_value=root), patch.object(
            synthetic_experiment, 'story_for', return_value=story), patch.object(
            synthetic_experiment, 'project_for', return_value=project), patch.object(
            auth, '_approved_adapter_paths', return_value={
                'codex': adapter, 'claude': peer_adapter}):
        old_path = cohort['trainEditPrefixes'][0] + 'old-ticketed.md'
        deleted_path = cohort['trainEditPrefixes'][0] + 'old-deleted.md'
        with shared._db_at(root / 'shared-learning.sqlite3') as db:
            db.execute("INSERT INTO edit_tickets (ticket_id,project_root,"
                "story_root,relative_path,episode,approval_sha256,"
                "parent_approval_sha256,document_sha256,source_side,"
                "source_start,segment_start,segment_end,segment_sha256,"
                "event_json,session_id,turn_id,tool_use_id,operation,"
                "created_at,expires_at) VALUES (" +
                ','.join('?' for _ in range(20)) + ')',
                ('old-ticket', str(project), str(story), old_path, 0,
                 'a', 'b', 'c', 'after', 0, 0, 1, 'd', '{}', 's', 't',
                 'u', 'write', start / 1000 - 120, start / 1000 - 60))
            db.execute("INSERT INTO synthetic_source_events VALUES (?,?,?,?,?,?,?,?)",
                ('old-deleted-event', deleted_path, 'delete', 'a', None,
                 'processed', start / 1000 - 120, start / 1000 - 120))
        assert auth.baseline_raw_paths(story, cohort, root) == sorted(
            [old_path, deleted_path])
        with shared._db_at(root / 'shared-learning.sqlite3') as db:
            db.execute("DELETE FROM edit_tickets WHERE ticket_id='old-ticket'")
            db.execute("DELETE FROM synthetic_source_events WHERE event_id='old-deleted-event'")
        assert auth.effective_argv(root, policy, adapter,
            reviewed_adapter_sha256=current_sha) == policy['trainerArgv']
        old_cohort = {**cohort, 'notBeforeUnixMs': start - 120_000}
        old_proposal = auth.proposal(root, project, policy, adapter,
            reviewed_adapter_sha256=current_sha, cohort=old_cohort,
            expires_at_unix_ms=expiry)
        try:
            auth.activate(root, project, policy, adapter,
                auth.proposal_sha256(old_proposal),
                reviewed_adapter_sha256=current_sha, cohort=old_cohort,
                expires_at_unix_ms=expiry)
        except ValueError as exc:
            assert 'cutoff_not_fresh' in str(exc)
        else:
            raise AssertionError('old cohort cutoff accepted')
        proposal = auth.proposal(root, project, policy, adapter,
                                 reviewed_adapter_sha256=current_sha,
                                 cohort=cohort, expires_at_unix_ms=expiry)
        assert proposal['operations'] == ['gold', 'local_train',
                                           'quality_evaluate']
        plan_sha = auth.proposal_sha256(proposal)
        try:
            auth.activate(root, project, policy, adapter, '0' * 64,
                          reviewed_adapter_sha256=current_sha,
                          cohort=cohort, expires_at_unix_ms=expiry)
        except ValueError as exc:
            assert 'plan_changed' in str(exc)
        else:
            raise AssertionError('wrong plan accepted')
        assert not (root / auth.NAME).exists()
        existing = story / cohort['trainEditPrefixes'][0] / 'existing.md'
        existing.parent.mkdir(parents=True)
        existing.write_text('pre-cutoff synthetic file')
        try:
            auth.activate(root, project, policy, adapter, plan_sha,
                          reviewed_adapter_sha256=current_sha,
                          cohort=cohort, expires_at_unix_ms=expiry)
        except ValueError as exc:
            assert str(exc) == 'runtime_authorization_baseline_changed'
        else:
            raise AssertionError('changed baseline accepted')
        existing.unlink()
        assert auth.activate(root, project, policy, adapter, plan_sha,
                             reviewed_adapter_sha256=current_sha,
                             cohort=cohort,
                             expires_at_unix_ms=expiry)['status'] == 'activated'
        assert auth.activate(root, project, policy, adapter, plan_sha,
                             reviewed_adapter_sha256=current_sha,
                             cohort=cohort,
                             expires_at_unix_ms=expiry)['status'] == 'already_active'
        assert auth.effective_argv(root, policy, adapter,
            reviewed_adapter_sha256=current_sha) == [str(python), str(adapter), str(model)]
        assert auth.effective_argv(root, policy, peer_adapter,
            reviewed_adapter_sha256=current_sha) == [str(python), str(peer_adapter), str(model)]
        with patch.object(auth.time, 'time_ns', return_value=expiry * 1_000_000):
            assert auth.active(root, policy, adapter,
                reviewed_adapter_sha256=current_sha) is None
            assert auth.effective_argv(root, policy, adapter,
                reviewed_adapter_sha256=current_sha) == policy['trainerArgv']
        auth_file = root / auth.NAME
        auth_file.unlink()
        assert auth.active(root, policy, adapter,
            reviewed_adapter_sha256=current_sha) is None
        save_private(auth_file, proposal)
        # The isolated/derived preflight reads the immutable policy inside
        # read_only_validation, even when normal execution is authorized.
        with patch.object(shared, '_historical_v45_adapter_reference',
                          return_value=True), shared.read_only_validation():
            assert shared._policy(root)['trainerArgv'] == policy['trainerArgv']
        assert (root / auth.NAME).stat().st_mode & 0o077 == 0
        assert json.loads((root / 'shared-policy.json').read_text()) == policy
        save_private(root / 'shared-policy.json', {**policy, 'goldModel': 'other'})
        try:
            auth.effective_argv(root, policy, adapter,
                reviewed_adapter_sha256=current_sha)
        except ValueError as exc:
            assert 'stale_or_invalid' in str(exc)
        else:
            raise AssertionError('stale approval accepted')
        save_private(root / 'shared-policy.json', policy)
        peer_adapter.write_text('print("changed peer")\n')
        try:
            auth.effective_argv(root, policy, adapter,
                reviewed_adapter_sha256=current_sha)
        except ValueError as exc:
            assert 'adapter_invalid' in str(exc)
        else:
            raise AssertionError('changed peer adapter accepted')
        peer_adapter.write_bytes(adapter.read_bytes())
        adapter.write_text('print("changed")\n')
        try:
            auth.effective_argv(root, policy, adapter,
                reviewed_adapter_sha256=current_sha)
        except ValueError as exc:
            assert 'adapter_invalid' in str(exc)
        else:
            raise AssertionError('changed adapter accepted')
    print(json.dumps({'plan_bound': True, 'idempotent': True,
                      'stale_denied': True, 'scope': 'single-synthetic-story'}))

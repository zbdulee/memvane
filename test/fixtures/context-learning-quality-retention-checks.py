"""Disposable local trainer: regressions retain pointer and holdout history."""
import json
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'core'))

from context_learning.contracts import digest, request
from context_learning.shared import _db, _selection_fence, train_due
from context_learning.store import canonical


def add_cases(root, start, *, project_split=False):
    with _db(root) as db:
        for offset in range(5):
            ordinal = start + offset
            owner = 'b' * 64 if project_split and offset == 4 else 'a' * 64
            cid = f'wiki/synthetic-{ordinal}.md'
            body = f'Synthetic isolated fact {ordinal}.\n'
            sample = request({'prompt': f'Which isolated fact is number {ordinal}?'},
                [{'candidate_id': cid, 'revision_sha256': digest(body),
                  'eligible': True, 'excerpt': body}],
                host='codex', request_id=f'fixture-{ordinal}')
            encoded = canonical(sample)
            input_sha = digest(encoded)
            gold = canonical({'selected_ids': [cid], 'input_sha256': input_sha,
                              'review': 'fixture_only'})
            db.execute('INSERT INTO cases (project_key,project_root,request_id,'
                       'input_sha256,sample,bodies,corpus_fingerprint,captured_at,'
                       'split,family_keys,use_state,status,gold) '
                       'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (owner, f'/tmp/synthetic-owner-{owner[0]}', sample['request_id'],
                        input_sha, encoded, canonical({cid: body}), 'synthetic-corpus',
                        time.time() + ordinal / 1000,
                        'holdout' if offset >= 3 else 'train', '[]', 'active',
                        'gold', gold))


class FakeTrainer:
    calls = []

    def __init__(self, scenario):
        self.scenario = scenario

    def call(self, mode, payload, result_path, *, cwd):
        self.calls.append(mode)
        if mode == 'train':
            artifact = Path(payload['artifact_path'])
            artifact.write_text('candidate')
            artifact.chmod(0o600)
            answer = {'schema_version': 1, 'status': 'trained'}
        else:
            model = Path(payload['artifact_path']).read_text()
            predictions = []
            for case in payload['cases']:
                if self.scenario == 'aggregate':
                    correct = model == 'base'
                elif self.scenario == 'improved':
                    correct = model == 'candidate'
                else:
                    owner_b = case['request_id'].startswith('b' * 12)
                    correct = owner_b if model == 'candidate' else not owner_b
                predictions.append({'request_id': case['request_id'],
                                    'input_sha256': case['input_sha256'],
                                    'selected_ids': ([case['candidates'][0]['id']]
                                                     if correct else [])})
            answer = {'schema_version': 1, 'predictions': predictions}
        Path(result_path).write_text(canonical(answer))
        Path(result_path).chmod(0o600)
        return answer, {'peak_rss_mib': 1}


with tempfile.TemporaryDirectory(prefix='qmd-quality-retention-', dir='/tmp') as tmp:
    outcomes = {}
    for scenario in ('aggregate', 'project', 'improved'):
        root = Path(tmp) / scenario
        root.mkdir(mode=0o700)
        base = root / 'base.artifact'
        base.write_text('base')
        base.chmod(0o600)
        policy = {'batchMin': 5, 'batchMax': 20, 'targetExact': .95,
                  'baseArtifact': str(base), 'trainerArgv': [sys.executable,
                      str(Path(__file__).resolve().parents[2] / 'core/context_learning/laya_adapter.py'),
                      str(root)], 'safetySeconds': 900, 'maxRssMib': 4096}
        add_cases(root, 0, project_split=scenario == 'project')
        FakeTrainer.calls = []
        with patch('context_learning.shared.resource_report',
                   return_value={'ready': True}), patch(
                'context_learning.shared._reconcile_cases', return_value=0), patch(
                'context_learning.shared._case_active', return_value=True), patch(
                'context_learning.shared.trial_eligible', return_value=True):
            result = train_due(root, policy,
                trainer_factory=lambda _: FakeTrainer(scenario))
            expected = 'complete' if scenario == 'improved' else 'retained'
            assert result['status'] == expected, result
            pointer = root / 'experimental-checkpoint.json'
            assert pointer.exists() == (scenario == 'improved')
            assert result['metrics']['applied'] == (scenario == 'improved')
            if scenario != 'improved':
                assert result['metrics']['retention_reason'] == (
                    'aggregate_regression' if scenario == 'aggregate'
                    else 'project_regression'), result
            with _db(root) as db:
                assert db.execute('SELECT status FROM cycles').fetchone()[0] == expected
                assert db.execute('SELECT COUNT(*) FROM cases WHERE trained_cycle IS NOT NULL').fetchone()[0] == 5
                if scenario != 'improved':
                    assert _selection_fence(db, None)['cycle_id'] == result['cycle_id']
            prior_calls = list(FakeTrainer.calls)
            repeat = train_due(root, policy,
                trainer_factory=lambda _: FakeTrainer(scenario))
            assert repeat['status'] in ('waiting', 'already_retained',
                                        'already_complete'), (scenario, repeat)
            assert FakeTrainer.calls == prior_calls
            if scenario == 'aggregate':
                add_cases(root, 5)
                with _db(root) as db:
                    fence = _selection_fence(db, None)
                    assert len(fence['validationCases']) == 2
                again = train_due(root, policy,
                    trainer_factory=lambda _: FakeTrainer(scenario))
                assert again['status'] == 'retained', again
                assert again['metrics']['fresh_holdout_questions'] == 2
                assert again['metrics']['fixed_validation_questions'] == 2
                assert not pointer.exists()
                # A later improvement may publish its pointer, then crash
                # before the terminal DB commit. Resume must bypass the older
                # retained fence and finish without retraining.
                add_cases(root, 10)
                from context_learning.local_cycle import _atomic_json as atomic
                crashed = [False]
                def interrupt_after_pointer(path, value):
                    atomic(path, value)
                    if Path(path).name == 'experimental-checkpoint.json' and not crashed[0]:
                        crashed[0] = True
                        raise RuntimeError('fixture_post_pointer_crash')
                with patch('context_learning.shared._atomic_json',
                           side_effect=interrupt_after_pointer):
                    interrupted = train_due(root, policy,
                        trainer_factory=lambda _: FakeTrainer('improved'))
                assert interrupted['status'] == 'failed', interrupted
                assert pointer.exists()
                calls_after_crash = list(FakeTrainer.calls)
                resumed = train_due(root, policy,
                    trainer_factory=lambda _: FakeTrainer('improved'))
                assert resumed['status'] == 'complete', resumed
                assert FakeTrainer.calls == calls_after_crash
                assert resumed['metrics']['fresh_holdout_questions'] == 2
        outcomes[scenario] = result['status']
    print(json.dumps(outcomes, sort_keys=True))

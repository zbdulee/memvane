"""Synthetic selector protocol: full body, active pointer, 0-card and failure fallback."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, 'core')
from context_learning import live_select
from context_learning.resident import ResidentFailure
from context_learning.contracts import digest
from context_learning.laya_adapter import EXPERIMENT_INPUT_VIEW, EDIT_INPUT_VIEW

with tempfile.TemporaryDirectory(prefix='qmd-live-selector-') as temporary:
    state = Path(temporary).resolve()
    state.chmod(0o700)
    artifact = state / 'head.artifact'
    artifact.write_text('synthetic local head')
    artifact.chmod(0o600)
    def private(path, value):
        path.write_text(json.dumps(value))
        path.chmod(0o600)
    private(state / 'active-checkpoint.json', {
        'schema_version': 1, 'artifact_path': str(artifact),
        'artifact_sha256': hashlib.sha256(artifact.read_bytes()).hexdigest()})
    adapter = Path('core/context_learning/laya_adapter.py').resolve()
    private(state / 'live-selector.json', {
        'schema': 'qmd-live-selector-v1',
        'trainerArgv': [sys.executable, str(adapter), str(state)],
        'timeoutSeconds': 5, 'maxRssMib': 512})
    source = '---\ntitle: Synthetic\nstatus: verified\n---\nFull synthetic body line.\n'
    rows = [{'id': 'wiki/card.md', 'revision_sha256': digest(source),
             'source_text': source}]
    observed = []
    observed_views = []
    class StubTrainer:
        def __init__(self, argv, **kwargs):
            assert argv[1] == str(adapter)
        def call(self, mode, request, output, *, cwd):
            assert mode == 'predict'
            case = request['cases'][0]
            observed.append(case['candidates'][0]['input_text'])
            observed_views.append(request.get('model_input_view'))
            if behavior == 'timeout': raise ValueError('trainer_timeout')
            selected = ['wiki/card.md'] if behavior == 'select' else [] if behavior == 'zero' else ['unknown']
            return {'schema_version': 1, 'predictions': [{
                'request_id': case['request_id'], 'input_sha256': case['input_sha256'],
                'selected_ids': selected}]}, {'elapsed_seconds': .1}
    live_select.VerifiedLayaTrainer = StubTrainer
    for behavior, expected, reason in (
            ('select', ['wiki/card.md'], 'selected'),
            ('zero', [], 'selected'),
            ('invalid', None, 'invalid_prediction'),
            ('timeout', None, 'timeout')):
        ids, actual, _ = live_select.choose(state, 'Synthetic user request', rows,
                                           corpus_fingerprint='a' * 64)
        assert ids == expected and actual == reason, (ids, actual)
    assert observed and all(body == 'Full synthetic body line.\n' for body in observed)
    assert observed_views == [None] * len(observed_views)
    private(state / 'experimental-checkpoint.json', {
        'schema_version': 1, 'artifact_path': str(artifact),
        'artifact_sha256': hashlib.sha256(artifact.read_bytes()).hexdigest(),
        'cycle_id': 'shared-synthetic-cycle',
        'model_input_view': EXPERIMENT_INPUT_VIEW})
    behavior = 'select'
    trial_ids, trial_reason, trial_resources = live_select.choose(state, 'Synthetic user request', rows,
        corpus_fingerprint='a' * 64,
        pointer_name='experimental-checkpoint.json')
    assert trial_ids == ['wiki/card.md'] and trial_reason == 'selected'
    assert trial_resources['model_version'] == 'shared-synthetic-cycle'
    assert trial_resources['model_input_view'] == EXPERIMENT_INPUT_VIEW
    assert observed_views[-1] == EXPERIMENT_INPUT_VIEW
    private(state / 'experimental-checkpoint.json', {
        'schema_version': 1, 'artifact_path': str(artifact),
        'artifact_sha256': hashlib.sha256(artifact.read_bytes()).hexdigest(),
        'cycle_id': 'multilingual-edit-cycle',
        'model_input_view': EDIT_INPUT_VIEW})
    multilingual_dir = state / 'multilingual-model'
    multilingual_dir.mkdir()
    (multilingual_dir / 'rl_agent_config.json').write_text(json.dumps({
        'encoder': 'jhu-clsp/mmBERT-base', 'max_len': 1024,
        'head_max_len': 256}))
    def resident_stub(_state, policy, request, artifact_sha, *, deadline):
        assert policy['trainerArgv'][1] == str(adapter)
        assert policy['trainerArgv'][2] == str(multilingual_dir)
        assert policy['maxRssMib'] == 4096
        assert request['model_input_view'] == EDIT_INPUT_VIEW
        assert artifact_sha == hashlib.sha256(artifact.read_bytes()).hexdigest()
        case = request['cases'][0]
        return {'schema_version': 1, 'predictions': [{
            'request_id': case['request_id'], 'input_sha256': case['input_sha256'],
            'selected_ids': ['wiki/card.md']}]}, {'rerank_wall_seconds': .2}
    with patch('context_learning.shared._policy', return_value={
            'modelInputView': EDIT_INPUT_VIEW,
            'trainerArgv': [sys.executable, str(adapter), str(multilingual_dir)],
            'maxRssMib': 4096}), patch(
            'context_learning.resident.predict', side_effect=resident_stub):
        edit_ids, edit_reason, edit_resources = live_select.choose(
            state, 'Synthetic edit content', rows,
            corpus_fingerprint='a' * 64,
            pointer_name='experimental-checkpoint.json')
        assert edit_ids == ['wiki/card.md'] and edit_reason == 'selected'
        assert edit_resources['model_input_view'] == EDIT_INPUT_VIEW
    from context_learning.shared import _v45_adapter_path
    with patch.object(Path, 'home', return_value=state):
        historical = str(_v45_adapter_path())
        assert not Path(historical).exists()
        with patch('context_learning.shared._policy', return_value={
                'modelInputView': EDIT_INPUT_VIEW,
                'trainerArgv': [sys.executable, historical, str(multilingual_dir)],
                'maxRssMib': 4096}), patch(
                'context_learning.resident.predict', side_effect=resident_stub):
            edit_ids, edit_reason, _ = live_select.choose(
                state, 'Synthetic edit content', rows,
                corpus_fingerprint='a' * 64,
                pointer_name='experimental-checkpoint.json')
            assert edit_ids == ['wiki/card.md'] and edit_reason == 'selected'
    with patch('context_learning.shared._policy', return_value={
            'modelInputView': EDIT_INPUT_VIEW,
            'trainerArgv': [sys.executable, str(adapter), str(multilingual_dir)],
            'maxRssMib': 4096}), patch(
            'context_learning.resident.predict', side_effect=ResidentFailure(
                'input_budget_exceeded', {'queue_wait_seconds': .123})):
        missed, reason, metrics = live_select.choose(state,
            'Synthetic edit content', rows, corpus_fingerprint='a' * 64,
            pointer_name='experimental-checkpoint.json')
        assert missed is None and reason == 'resident_input_budget_exceeded'
        assert metrics['queue_wait_seconds'] == .123
        assert metrics['selector_wall_seconds'] >= 0
    artifact.write_text('changed artifact')
    assert live_select.choose(state, 'Synthetic user request', rows,
                              corpus_fingerprint='a' * 64)[1] == 'checkpoint_changed'
    assert live_select.choose(state, 'Synthetic user request', rows,
                              corpus_fingerprint=None)[1] == 'corpus_unavailable'
    print(json.dumps({'fullBodyInput': True, 'rankedSelection': True,
                      'semanticZero': True, 'invalidOutputFallback': True,
                      'timeoutFallback': True, 'checkpointPin': True,
                      'corpusGate': True, 'externalCalls': 0}))

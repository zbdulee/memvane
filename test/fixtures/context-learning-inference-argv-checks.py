"""Only the reviewed installed adapter is used for edit inference."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

from memvane.learning.context_learning import shared
from memvane.learning.context_learning.adapter import EDIT_INPUT_VIEW


def rejected(callback, code):
    try:
        callback()
    except ValueError as exc:
        assert str(exc) == code, str(exc)
    else:
        raise AssertionError('unverified inference reached execution')


reviewed = Path(__file__).resolve().parents[2] / 'memvane/learning/context_learning/adapter.py'
candidate_sha = hashlib.sha256(reviewed.read_bytes()).hexdigest()
# Candidate approval is local to this disposable fixture, never the production pin.
with tempfile.TemporaryDirectory(prefix='qmd-inference-argv-', dir=reviewed.parents[2]) as name:
    root = Path(name)
    installed = root / 'installed/context_learning'
    installed.mkdir(parents=True)
    adapter = installed / 'adapter.py'
    adapter.write_bytes(reviewed.read_bytes())
    model = root / 'multilingual-model'
    model.mkdir()
    config = model / 'rl_agent_config.json'
    accepted_config = {'encoder': 'jhu-clsp/mmBERT-base',
                       'max_len': 1024, 'head_max_len': 256}
    config.write_text(json.dumps(accepted_config))
    observed = []

    class SegmentTrainer:
        def __init__(self, argv, **kwargs):
            observed.append(list(argv))

        def call(self, mode, request, output, *, cwd):
            assert mode == 'segment'
            text = request['text']
            sha = hashlib.sha256(text.encode()).hexdigest()
            return {'schema_version': 1, 'source_sha256': sha,
                    'segments': [{'index': 0, 'count': 1, 'start': 0,
                                  'end': len(text), 'text': text, 'sha256': sha}]}, {}

    with patch.object(Path, 'home', return_value=root), \
            patch.object(shared, '__file__', str(installed / 'shared.py')), \
            patch('context_learning.local_cycle.LocalTrainer', SegmentTrainer), \
            patch('hook_budget.remaining', return_value=8):
        old = root / 'retired' / 'adapter.py'
        policy = {'trainerArgv': [sys.executable, str(adapter), str(model)],
                  'modelInputView': EDIT_INPUT_VIEW, 'maxRssMib': 512}
        frozen = json.dumps(policy, sort_keys=True)
        rejected(lambda: shared.inference_argv(policy, 'predict'),
                 'historical_inference_adapter_unverified')
        with patch.object(shared, 'REVIEWED_ADAPTER_SHA256', candidate_sha):
            expected = [sys.executable, str(adapter), str(model)]
            assert shared.inference_argv(policy, 'segment') == expected
            assert shared.inference_argv(policy, 'predict') == expected
            rejected(lambda: shared.inference_argv({**policy, 'trainerArgv':
                     [sys.executable, str(old), str(model)]}, 'predict'),
                     'historical_inference_adapter_unverified')
            assert shared.inference_argv({**policy, 'trainerArgv': expected},
                                         'segment') == expected
            assert json.dumps(policy, sort_keys=True) == frozen
            rejected(lambda: shared.inference_argv(policy, 'train'),
                     'invalid_shared_inference_operation')
            rejected(lambda: shared.inference_argv(policy, 'smoke'),
                     'invalid_shared_inference_operation')
            rejected(lambda: shared.inference_argv({**policy,
                     'modelInputView': 'full-candidate-1024-v1'}, 'segment'),
                     'historical_inference_view_unverified')
            rejected(lambda: shared.inference_argv({**policy,
                     'trainerArgv': [sys.executable, str(old.with_name('other.py')),
                                     str(model)]}, 'segment'),
                     'historical_inference_adapter_unverified')
            alias = installed / 'alias.py'
            alias.symlink_to(adapter)
            rejected(lambda: shared.inference_argv({**policy,
                     'trainerArgv': [sys.executable, str(alias), str(model)]}, 'predict'),
                     'historical_inference_adapter_unverified')
            config.write_text(json.dumps({**accepted_config, 'encoder': 'English-only'}))
            rejected(lambda: shared.inference_argv(policy, 'segment'),
                     'historical_inference_view_unverified')
            config.write_text(json.dumps(accepted_config))
            adapter.write_text('tampered')
            rejected(lambda: shared.inference_argv(policy, 'predict'),
                     'historical_inference_adapter_unverified')
            adapter.unlink()

print(json.dumps({'retiredAdapterRejected': True,
                  'currentAdapterHashAndViewPinned': True,
                  'invalidAdaptersRejectedBeforeExecution': True,
                  'trainingNotResolved': True}))

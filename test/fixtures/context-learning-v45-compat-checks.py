"""Historical policy reference is data, never an executable upgrade shortcut."""
import json
from pathlib import Path
import sqlite3
import tempfile
from unittest.mock import patch, Mock

from context_learning import shared
from context_learning.synthetic_experiment import evaluate_final
from context_learning.shared import (
    REVIEWED_ADAPTER_SHA256, _historical_v45_adapter_reference,
    _v45_adapter_path, observe_trial, train_due,
)

current = Path(__file__).resolve().parents[2] / 'core/context_learning/laya_adapter.py'
candidate_sha = __import__('hashlib').sha256(current.read_bytes()).hexdigest()
# Production authority stays pinned; this fixture separately reviews candidate bytes.
with tempfile.TemporaryDirectory(prefix='qmd-v45-compat-',
                                 dir=current.parents[2]) as name:
    home = Path(name)
    with patch.object(Path, 'home', return_value=home):
        old = _v45_adapter_path()
        assert not _historical_v45_adapter_reference(old, current)
        with patch.object(shared, 'REVIEWED_ADAPTER_SHA256', candidate_sha):
            assert _historical_v45_adapter_reference(old, current)
            assert not _historical_v45_adapter_reference(old.with_name('other.py'), current)
            old.parent.mkdir(parents=True)
            old.write_bytes(current.read_bytes())
            assert not _historical_v45_adapter_reference(old, current)
            old.write_text('tampered')
            assert not _historical_v45_adapter_reference(old, current)
            old.unlink()
            policy = {'trainerArgv': ['/runtime/python', str(old), '/cached/model']}
            assert train_due(home, policy) == {'status': 'runtime_approval_required'}
            assert observe_trial(home, policy) == {'status': 'runtime_approval_required'}
            final_db = home / 'final-state.sqlite3'
            with sqlite3.connect(final_db) as db:
                db.execute('CREATE TABLE experiment_final (id INTEGER, status TEXT)')
            unchanged = final_db.read_bytes()
            trainer = Mock(side_effect=AssertionError('trainer executed'))
            with patch('context_learning.synthetic_experiment.project_for', return_value=home), \
                    patch('context_learning.synthetic_experiment.load',
                          return_value=({}, 'approved-control')), \
                    patch('context_learning.synthetic_experiment.approval', return_value={}), \
                    patch('context_learning.shared._policy', return_value=policy), \
                    patch('context_learning.shared._db',
                          side_effect=AssertionError('final reservation reached')):
                assert evaluate_final(home, trainer_factory=trainer) == {
                    'status': 'runtime_approval_required'}
            trainer.assert_not_called()
            assert final_db.read_bytes() == unchanged
print(json.dumps({'missingOldCacheAcceptedForRead': True,
                  'tamperedOldCacheRejected': True,
                  'trainingAndObservationBlocked': True,
                  'finalReservationBlocked': True}))

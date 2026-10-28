"""Historical policy reference is data, never an executable upgrade shortcut."""
import json
from pathlib import Path
import tempfile
from unittest.mock import patch

from context_learning import shared
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
print(json.dumps({'missingOldCacheAcceptedForRead': True,
                  'tamperedOldCacheRejected': True,
                  'trainingAndObservationBlocked': True}))

"""Synthetic-QA memory ceiling: the user-approved 3,584 MiB RSS bound (no model, no files).

The measured-forward profile rule is fixedMib = max(ceil(1.5*peak), peak+512),
perPairMib = 128, fixedMib+perPairMib <= SYNTHETIC_MAX_RSS_MIB. Only the
guard-derived peak is patched; every other check in _measured_memory runs.
"""
import json
import math
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path.cwd() / 'core'))
import project_learning_setup as setup

assert setup.SYNTHETIC_MAX_RSS_MIB == 3584
evidence = {'schema': 'qmd-synthetic-forward-calibration-v1'}
digest = setup.proposal_sha256(evidence)


def profile(peak):
    return {'fixedMib': max(math.ceil(peak * 1.5), peak + 512), 'perPairMib': 128,
            'estimateSource': 'reviewed-forward-envelope:' + digest}


def check(peak):
    with patch.object(setup, '_guard_calibration', return_value={'peakRssKib': peak * 1024}):
        try:
            setup._measured_memory(profile(peak), evidence, root=None, project=None,
                                   model_identity_sha='0' * 64, runtime_sha='0' * 64,
                                   adapter_sha='0' * 64)
            return 'accepted'
        except ValueError as exc:
            return str(exc)


results = {'measured2033': check(2033), 'boundary2304': check(2304), 'over2305': check(2305)}
assert results == {'measured2033': 'accepted', 'boundary2304': 'accepted',
                   'over2305': 'setup_synthetic_measured_profile_required'}, results
# A profile that is not the reviewed formula stays rejected even under the ceiling.
with patch.object(setup, '_guard_calibration', return_value={'peakRssKib': 2033 * 1024}):
    try:
        setup._measured_memory({**profile(2033), 'fixedMib': 2900}, evidence, root=None, project=None,
                               model_identity_sha='0' * 64, runtime_sha='0' * 64, adapter_sha='0' * 64)
        raise AssertionError('non-formula profile accepted')
    except ValueError as exc:
        assert str(exc) == 'setup_synthetic_measured_profile_required'
# Other synthetic bounds are unchanged in source.
source = (Path.cwd() / 'core/project_learning_setup.py').read_text()
for fragment in ("'minAvailableMib':768,'maxLoadPerCpu':.75}",
                 "minAvailableMib=768,maxLoadPerCpu=.75,maxGoldCalls=0,",
                 "batchMax=100,adaptiveMemory=profile,targetHeadroomFraction=.35",
                 "profile.get('reserveFraction')!=.35", "'timeoutSeconds':30,"):
    assert fragment in source, fragment
results['otherBoundsUnchanged'] = True
print(json.dumps(results, sort_keys=True))

"""One bounded OS observation with the configured admission policy applied."""
import os
from pathlib import Path
import subprocess
import sys
import time


def report(policy, *, detailed=False, admission=True, deadline=None):
    from memvane.runtime.resource_platform import adapter_for, normal_pressure, number
    from memvane.runtime.bounded_calibration import pressure_ready, verdict as calibration_verdict
    from memvane.runtime.memory_policy import assess as memory_assess
    from memvane.runtime.resource_coordination import observation_fresh
    started = time.monotonic()
    end = min(started + 2, deadline) if deadline is not None else started + 2
    result = adapter_for(sys.platform).observe(
        path_factory=Path, run=subprocess.run, clock=time.monotonic,
        cpu_count=os.cpu_count, loadavg=getattr(os, 'getloadavg', None),
        deadline=end, detailed=detailed)
    available, load = result['available_mib'], result['load_per_cpu']
    result['ready'] = (number(load) and load <= policy['maxLoadPerCpu']
        and number(available) and available >= policy['minAvailableMib']
        and pressure_ready(result, policy))
    if 'calibration' in policy:
        result['calibration_admission'] = calibration_verdict(result, policy)
    if 'adaptiveMemory' in policy:
        decision = memory_assess(result, policy, admission=admission)
        result.update(ready=decision['ready'], memory_admission=decision)
        if decision['training_limits'] is not None:
            result['training_limits'] = decision['training_limits']
    result['observation_status'] = ('fresh' if observation_fresh(result)
                                    else 'stale')
    result['ready'] = (result['ready'] and result['observation_status'] == 'fresh'
                       and time.monotonic() < end)
    return result

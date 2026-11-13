"""Explicit bounded calibration guards; never auto-enable learning or approve a profile.

Unknown footprint is measured under remaining-reserve/RSS/MPS monitors, rather
than requiring an already available profile. Normal training policy is untouched.
"""
import math


def _number(value, *, positive=False):
    return (type(value) in (int, float) and math.isfinite(value)
            and (value > 0 if positive else value >= 0))


def policy(report, *, reserve_fraction, absolute_floor_mib,
           max_rss_mib, max_load_per_cpu):
    if not isinstance(report,dict) or not isinstance(report.get('query_status'),dict):
        raise ValueError('calibration_policy_unavailable')
    total = report.get('total_mib')
    if (not _number(total, positive=True)
            or report.get('query_status', {}).get('total') != 'ok'
            or not _number(reserve_fraction) or not .10 <= reserve_fraction <= .50
            or type(max_rss_mib) is not int or not 128 <= max_rss_mib <= 65536
            or not _number(max_load_per_cpu, positive=True)
            or absolute_floor_mib is not None and
               (type(absolute_floor_mib) is not int or not 128 <= absolute_floor_mib <= 65536)):
        raise ValueError('calibration_policy_unavailable')
    return {'minAvailableMib':max(absolute_floor_mib or 0, math.ceil(total*reserve_fraction)),
            'maxRssMib':max_rss_mib, 'maxLoadPerCpu':max_load_per_cpu,
            'reserveFraction':reserve_fraction, 'totalMib':total,
            'absoluteFloorMib':absolute_floor_mib,
            'purpose':'explicit_calibration_only'}


def check(report, policy):
    result = {'ready':False,'reason':'calibration_measurement_unavailable',
              'reserve_mib':policy['minAvailableMib']}
    if not isinstance(report,dict):return result
    status = report.get('query_status', {})
    if (not isinstance(status,dict)
            or any(status.get(k) != 'ok' for k in ('load','available','total','memory_pressure'))):
        return result
    pressure = report.get('memory_pressure')
    free = pressure.get('system_free_percent') if isinstance(pressure,dict) else None
    available, load = report.get('available_mib'), report.get('load_per_cpu')
    if (not _number(free) or free > 100 or not _number(available)
            or not _number(load) or report.get('total_mib') != policy['totalMib']):
        return result
    if free < 10:
        result['reason']='calibration_os_pressure'; return result
    if available < policy['minAvailableMib'] or load > policy['maxLoadPerCpu']:
        result['reason']='calibration_host_reserve_or_load'; return result
    # Available is measured after current allocations. Adding the whole loaded
    # model to its required remaining reserve would charge those allocations twice.
    result.update(ready=True,reason='calibration_running_reserve_ready')
    return result


def groups(items, *, include_combined=False):
    """Measure both singleton shapes, including the longer/negative pair."""
    if not isinstance(items,list) or len(items)!=2 or type(include_combined) is not bool:
        raise ValueError('calibration_requires_two_pairs')
    return [[items[0]],[items[1]]] + ([list(items)] if include_combined else [])


def mps_snapshot(torch):
    empty={'mpsCurrentBytes':None,'mpsDriverBytes':None,'mpsRecommendedBytes':None}
    if torch is None:return empty
    try:
        available=torch.backends.mps.is_available()
        if type(available) is not bool:raise ValueError('unknown_mps_availability')
        if not available:return empty
        torch.mps.synchronize()
        current=torch.mps.current_allocated_memory()
        driver=torch.mps.driver_allocated_memory()
        recommended=torch.mps.recommended_max_memory()
        if (any(type(x) is not int or x<0 for x in (current,driver))
                or type(recommended) is not int or recommended<=0):
            raise ValueError('unknown_mps_measurement')
        return dict(mpsCurrentBytes=current,mpsDriverBytes=driver,mpsRecommendedBytes=recommended)
    except Exception as error:
        raise ValueError('training_memory_unavailable') from error


def prediction_cases(training_cases):
    """Preserve synthetic input only; never send labels to the held-out reader."""
    keys=('request_id','input_sha256','prompt','candidates')
    if (not isinstance(training_cases,list) or not training_cases
            or any(not isinstance(c,dict) or not set(keys)<=set(c) for c in training_cases)):
        raise ValueError('invalid_calibration_cases')
    return [{k:c[k] for k in keys} for c in training_cases]

"""Optional additive memory admission. Estimates are never measured facts."""
import math
import re


def validate(value):
    required = {'device', 'fixedMib', 'perPairMib', 'estimateSource',
                'modelIdentitySha256'}
    if (not isinstance(value, dict) or not required <= set(value)
            or set(value) - required - {'reserveFraction'}):
        raise ValueError('invalid_adaptive_memory')
    fraction = value.get('reserveFraction', .10)
    if (type(fraction) not in (int, float) or not math.isfinite(fraction)
            or not .10 <= fraction <= .50
            or value['device'] not in ('cpu', 'mps')
            or any(type(value[k]) is not int or not 1 <= value[k] <= 65536
                   for k in ('fixedMib', 'perPairMib'))
            or not isinstance(value['estimateSource'], str)
            or not 1 <= len(value['estimateSource']) <= 256
            or not isinstance(value['modelIdentitySha256'], str)
            or not re.fullmatch('[0-9a-f]{64}', value['modelIdentitySha256'])):
        raise ValueError('invalid_adaptive_memory')
    return {**value, 'reserveFraction': fraction}


def assess(report, policy, *, admission=True):
    """Keep explicit absolute floors; never silently fall back from unknowns."""
    result = {'ready': False, 'reason': 'invalid_adaptive_memory',
              'estimate_kind': 'configured_estimate_not_measurement',
              'training_limits': None}
    try:
        profile = validate(policy['adaptiveMemory'])
    except (KeyError, ValueError):
        return result
    total, available = report.get('total_mib'), report.get('available_mib')
    status = report.get('query_status', {})
    if status.get('memory_pressure') != 'ok':
        result['reason'] = 'memory_pressure_' + status.get('memory_pressure', 'unavailable')
        return result
    pressure = report.get('memory_pressure')
    if not isinstance(pressure, dict) or 'system_free_percent' not in pressure:
        result['reason'] = 'memory_pressure_unrecognized'
        return result
    if pressure['system_free_percent'] < 10:
        result['reason'] = 'os_memory_pressure'
        return result
    if (type(total) not in (int, float) or not math.isfinite(total) or total <= 0
            or type(available) not in (int, float) or not math.isfinite(available)):
        result['reason'] = 'memory_capacity_unavailable'
        return result
    reserve = max(policy['minAvailableMib'], math.ceil(total * profile['reserveFraction']))
    result.update(reserve_mib=reserve, profile=profile)
    if not report['ready'] or available < reserve:
        result['reason'] = 'host_reserve_or_load'
        return result
    # Preflight charges peak+reserve. Once admitted, monitor remaining reserve
    # and OS pressure; charging that whole peak again would double-reserve it.
    for size in (2, 1):
        peak = profile['fixedMib'] + profile['perPairMib'] * size
        if peak <= policy['maxRssMib'] and available >= reserve + peak:
            result.update(ready=True, reason='estimated_fit', estimated_peak_mib=peak,
                training_limits={'microbatchSize': size, 'effectiveBatchSize': 2,
                    'device': profile['device'],
                    'modelIdentitySha256': profile['modelIdentitySha256'],
                    'reserveFraction': profile['reserveFraction']})
            return result
    if not admission:
        result.update(ready=True, reason='running_reserve_ready')
        return result
    result['reason'] = 'minimum_microbatch_unavailable'
    return result

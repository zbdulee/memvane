"""Optional additive memory admission. Estimates are never measured facts."""
import math
import re
from resource_platform import normal_pressure,number
from .bounded_calibration import pressure_ready


def validate(value):
    required = {'device', 'fixedMib', 'perPairMib', 'estimateSource',
                'modelIdentitySha256'}
    if (not isinstance(value, dict) or not required <= set(value)
            or set(value) - required - {'reserveFraction','systemReserveMib','reserveSource',
                'uncertainty','maxVerifiedMicrobatchSize','warmAdditionalMib','warmEstimateSource'}):
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
    for amount,source,allow_zero in (('systemReserveMib','reserveSource',False),
                                    ('warmAdditionalMib','warmEstimateSource',True)):
        if amount in value or source in value:
            if (type(value.get(amount))is not int or not int(not allow_zero)<=value[amount]<=65536
                    or not isinstance(value.get(source),str) or not 1<=len(value[source])<=256):
                raise ValueError('invalid_adaptive_memory')
    if (type(value.get('maxVerifiedMicrobatchSize',1))is not int
            or value.get('maxVerifiedMicrobatchSize',1) not in (1,2)
            or 'uncertainty' in value and
               (not isinstance(value['uncertainty'],str) or not 1<=len(value['uncertainty'])<=512)):
        raise ValueError('invalid_adaptive_memory')
    return {**value, 'reserveFraction': fraction}


def system_reserve(policy,total):
    """Preserve declared floors; fixed sourced reserves avoid a RAM ratio.

    Legacy adaptive reserveFraction remains compatibility policy, explicitly
    labeled as an uncalibrated ratio. It is not the soft35% expansion target.
    """
    floor=policy['minAvailableMib']
    if type(floor)is not int or floor<0:raise ValueError('invalid_system_reserve')
    if 'adaptiveMemory' not in policy:return floor,'configured_absolute_floor'
    profile=validate(policy['adaptiveMemory'])
    if 'systemReserveMib' in profile:
        return max(floor,profile['systemReserveMib']),profile['reserveSource']
    return max(floor,math.ceil(total*profile['reserveFraction'])),'legacy_configured_ratio_not_calibrated'


def assess(report, policy, *, admission=True,resident_credit=0,phase='cold',now=None):
    """Keep explicit absolute floors; never silently fall back from unknowns."""
    result = {'ready': False, 'reason': 'invalid_adaptive_memory',
              'estimate_kind': 'configured_estimate_not_measurement',
              'training_limits': None}
    try:
        profile = validate(policy['adaptiveMemory'])
    except (KeyError, ValueError):
        return result
    total,available=report.get('total_mib'),report.get('available_mib')
    if not pressure_ready(report,policy,now=now):result['reason']='os_memory_pressure_unknown_or_high';return result
    if not number(total) or total<=0 or not number(available):
        result['reason'] = 'memory_capacity_unavailable'
        return result
    if phase not in ('cold','loaded_self','resume_new_process') or not number(resident_credit):
        result['reason']='memory_phase_or_credit_unknown';return result
    if profile['device']=='mps' and 'platform' in report:
        observed=report['platform']
        # Live adapters always describe topology. This additive host-RAM model
        # cannot budget unknown/separate VRAM; metadata absence is supported
        # only for the legacy injected reports, not as an OS observation.
        if (not isinstance(observed,dict) or observed.get('os')!='darwin'
                or observed.get('scope')!='host' or observed.get('memory_model')!='unified'
                or observed.get('support') not in ('partial','supported')):
            result['reason']='memory_backend_unsupported';return result
    try:reserve,source=system_reserve(policy,total)
    except (KeyError,ValueError):result['reason']='invalid_system_reserve';return result
    peak_limit=policy.get('maxRssMib')
    if not number(peak_limit) or peak_limit<=0:
        result['reason']='memory_budget_unknown';return result
    target_fraction=policy.get('targetHeadroomFraction',.35)
    if not number(target_fraction) or target_fraction>1:
        result['reason']='invalid_headroom_target';return result
    target=math.ceil(total*target_fraction)
    credit=min(resident_credit,profile['fixedMib']) if phase=='loaded_self' else 0
    result.update(reserve_mib=reserve,reserve_source=source,profile=profile,phase=phase,
        uncertainty=profile.get('uncertainty','not_quantified; configured estimate only'),
        estimate_source=profile['estimateSource'],credited_owned_mib=credit,
        target_headroom_mib=target,target_role='soft_expansion_goal',target_met=False)
    def requirement(size):
        peak=profile['fixedMib']+profile['perPairMib']*size
        if phase=='loaded_self' and credit>0 and 'warmAdditionalMib' in profile:
            extra=profile['warmAdditionalMib']+profile['perPairMib']*(size-1)
            basis=profile['warmEstimateSource']
        else:extra=max(0,peak-credit);basis='cold_envelope_less_measured_fixed_credit' if credit else 'cold_envelope'
        return peak,extra,basis
    peak,extra,basis=requirement(1)
    result.update(estimated_peak_mib=peak,required_additional_mib=extra,additional_basis=basis)
    if report.get('ready') is not True or available < reserve:
        result['reason'] = 'host_reserve_or_load'
        return result
    # First fit the minimum supported shape. A ratio is never its start veto.
    for size in (2,1) if profile.get('maxVerifiedMicrobatchSize',1)==2 else (1,):
        peak,extra,basis=requirement(size)
        fits=available>=reserve+extra if admission else available>=reserve
        expanded=(size==1 or available-extra>=target)
        if peak <= peak_limit and fits and expanded:
            result.update(ready=True, reason='estimated_fit', estimated_peak_mib=peak,
                required_additional_mib=extra,additional_basis=basis,target_met=available-extra>=target,
                training_limits={'microbatchSize': size, 'effectiveBatchSize': 2,
                    'device': profile['device'],
                    'modelIdentitySha256': profile['modelIdentitySha256'],
                    'reserveFraction': profile['reserveFraction']})
            return result
    result['reason'] = 'minimum_microbatch_unavailable'
    return result

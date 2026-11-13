"""Pure, conservative scheduling and promotion decisions for offline cycles.

The 95% target changes the next *attempt* interval; it never authorizes a
checkpoint. Promotion uses fixed held-out metrics and an incumbent comparison.
"""
import math

TARGET = .95
MIN_INTERVAL_HOURS = 24
MAX_INTERVAL_HOURS = 14 * 24


def _rate(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError('invalid_rate')
    return float(value)


def wilson_lower(successes, total, z=1.96):
    if type(successes) is not int or type(total) is not int or not 0 <= successes <= total:
        raise ValueError('invalid_counts')
    if total == 0:
        return None
    p = successes / total
    d = 1 + z*z/total
    return (p + z*z/(2*total) - z*math.sqrt((p*(1-p)+z*z/(4*total))/total))/d


def selection_score(cases):
    """Each case has exact reviewed 0–3 IDs and an actual emitted 0–3 choice."""
    if not isinstance(cases, list) or len(cases) > 10000:
        raise ValueError('invalid_cases')
    exact = none_total = none_correct = 0
    families = set()
    for case in cases:
        if not isinstance(case, dict) or set(case) != {'gold', 'selected', 'family'}:
            raise ValueError('invalid_case')
        gold, selected = case['gold'], case['selected']
        if any(not isinstance(v, list) or len(v) > 3 or len(set(v)) != len(v) or any(not isinstance(x, str) or not x for x in v) for v in (gold, selected)):
            raise ValueError('invalid_choice')
        family = case['family']
        if not isinstance(family, str) or not family.strip():
            raise ValueError('invalid_family')
        families.add(family)
        exact += set(gold) == set(selected)
        if not gold:
            none_total += 1
            none_correct += not selected
    total = len(cases)
    return {'questions': total, 'families': len(families), 'none_questions': none_total,
            'exact': exact, 'exact_rate': exact/total if total else None,
            'wilson_lower': wilson_lower(exact, total),
            'none_exact_rate': none_correct/none_total if none_total else None}


def next_interval(previous_hours, score, *, new_family=False, source_churn=False, recent_failure=False):
    if type(previous_hours) not in (int, float) or not math.isfinite(previous_hours) or not MIN_INTERVAL_HOURS <= previous_hours <= MAX_INTERVAL_HOURS:
        raise ValueError('invalid_previous_interval')
    if not isinstance(score, dict) or not {'questions', 'families', 'none_questions', 'exact_rate', 'wilson_lower'} <= set(score):
        raise ValueError('invalid_score')
    if any(type(x) is not bool for x in (new_family, source_churn, recent_failure)):
        raise ValueError('invalid_change_signal')
    enough = score['questions'] >= 50 and score['families'] >= 5 and score['none_questions'] >= 10
    if any(type(score[k]) is not int or score[k] < 0 for k in ('questions', 'families', 'none_questions')) or score['families'] > score['questions'] or score['none_questions'] > score['questions']:
        raise ValueError('invalid_score_counts')
    if enough and (score['exact_rate'] is None or score['wilson_lower'] is None):
        raise ValueError('missing_held_out_rate')
    if score['exact_rate'] is not None: _rate(score['exact_rate'])
    if score['wilson_lower'] is not None: _rate(score['wilson_lower'])
    if new_family or source_churn or recent_failure:
        proposed = previous_hours / 2
        reason = 'new_or_changed_evidence'
    elif not enough:
        proposed = min(previous_hours, 72)
        reason = 'insufficient_held_out_evidence'
    else:
        # At n=50 even 50/50 has a 95% Wilson lower bound of only .9287.
        # Normalize by the best attainable lower bound at this same sample
        # size. This is a pacing signal, never a claim that true quality is
        # statistically above TARGET or a checkpoint activation gate.
        attainable = wilson_lower(score['questions'], score['questions'])
        quality = min(score['exact_rate'], score['wilson_lower'] / attainable)
        if quality >= TARGET:
            proposed = previous_hours * 2
            reason = 'target_supported'
        elif quality > .8:
            # Smooth increase as held-out exact choice approaches 95%; the
            # square keeps early improvements conservative.
            proposed = previous_hours * (1 + ((quality - .8) / .2) ** 2)
            reason = 'approaching_target'
        else:
            proposed = previous_hours / 2
            reason = 'below_target'
    return {'hours': max(MIN_INTERVAL_HOURS, min(MAX_INTERVAL_HOURS, proposed)),
            'reason': reason, 'target': TARGET, 'eligible_evidence': enough,
            'activation_decision': 'none'}


def promotion_decision(incumbent, challenger, *, train_questions, validation_score, stale=False):
    """Fixed evaluation gate; caller must pin split and hashes before training."""
    if type(train_questions) is not int or train_questions < 0 or type(stale) is not bool:
        raise ValueError('invalid_training_state')
    if not isinstance(validation_score, dict) or not {'questions', 'families', 'none_questions'} <= set(validation_score):
        raise ValueError('invalid_validation_score')
    if stale:
        return {'promote': False, 'reason': 'stale_source'}
    if train_questions < 100 or validation_score['questions'] < 50 or validation_score['families'] < 5 or validation_score['none_questions'] < 10:
        return {'promote': False, 'reason': 'insufficient_separated_data'}
    if not isinstance(incumbent, dict) or not isinstance(challenger, dict) or set(incumbent) != {'exact_rate', 'necessary_recall', 'irrelevant_injection_rate', 'none_exact_rate'} or set(challenger) != set(incumbent):
        raise ValueError('invalid_comparison')
    for metric in incumbent: _rate(incumbent[metric]); _rate(challenger[metric])
    if challenger['exact_rate'] <= incumbent['exact_rate']:
        return {'promote': False, 'reason': 'no_exact_choice_gain'}
    for metric in ('necessary_recall', 'none_exact_rate'):
        if challenger[metric] < incumbent[metric]:
            return {'promote': False, 'reason': metric+'_regressed'}
    if challenger['irrelevant_injection_rate'] > incumbent['irrelevant_injection_rate']:
        return {'promote': False, 'reason': 'irrelevant_injection_regressed'}
    return {'promote': True, 'reason': 'held_out_improvement', 'target_used_as_activation_threshold': False}

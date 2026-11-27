"""The training admission's peak estimate from the trainer's measured footprint.

Without a measurement the coordinator admits a training or an evaluation on the
policy's RSS ceiling (``maxRssMib``, 4,096 MiB) plus its reserve. A finished
training measures the process group's peak RSS; ``record`` keeps the largest
one per trainer command in the namespace's ``trainer-footprint.json`` (a cache
outside the 1.x contract: missing or unreadable means no measurement). From
then on ``admission_view`` admits on the reviewed envelope of that peak,
``max(ceil(1.5 * peak), peak + 512)`` and never below ``MIN_ENVELOPE_MIB``,
capped by the ceiling. Only the coordinator's admission when a training or an
evaluation starts takes this view. The ceiling still bounds the running trainer,
and the request's ``learning_control`` keeps the configured policy: it is part of
the training request's identity (``train-intent.json``), and its per-step
admissions credit the memory the loaded model already holds.
"""
from __future__ import annotations

import math
from pathlib import Path
import time

NAME = 'trainer-footprint.json'
SCHEMA = 'memvane-trainer-footprint-v1'
MIN_ENVELOPE_MIB = 1024
# A peak needs this many RSS samples to stand for a training run.
MIN_SAMPLES = 2


def envelope(peak_mib):
    """The admission estimate of one measured peak (MiB)."""
    return max(MIN_ENVELOPE_MIB, math.ceil(peak_mib * 1.5), math.ceil(peak_mib) + 512)


def _key(policy):
    from .contracts import digest
    from .store import canonical
    return digest(canonical(list(policy['trainerArgv'])))


def _peak(root, policy):
    """The recorded peak (MiB) of this policy's trainer command, or None."""
    from .dataset import read_private_json
    try:
        row = read_private_json(Path(root) / NAME)
        peak = row['trainers'][_key(policy)]['peakMib']
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None
    return peak if type(peak) is int and 0 < peak <= 65536 else None


def record(root, policy, resources):
    """Keep a finished training's measured peak RSS; never raises.

    Ignored on failure: the admission keeps the policy's ceiling, the estimate it had
    before any measurement.
    """
    try:
        kib, samples = resources.get('observed_peak_rss_kib'), resources.get('resource_samples')
        if type(kib) is not int or kib <= 0 or type(samples) is not int or samples < MIN_SAMPLES:
            return
        peak = math.ceil(kib / 1024)
        if not 0 < peak <= 65536:
            return
        from .dataset import read_private_json
        from .local_cycle import _atomic_json
        path = Path(root) / NAME
        try:
            row = read_private_json(path)
            trainers = row['trainers'] if row.get('schema') == SCHEMA and isinstance(row.get('trainers'), dict) else {}
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            trainers = {}
        key = _key(policy)
        known = _peak(root, policy)
        if known is not None and known >= peak:
            return
        trainers = {**trainers, key: {'peakMib': peak, 'recordedAt': time.time()}}
        _atomic_json(path, {'schema': SCHEMA, 'trainers': trainers})
    except Exception:  # noqa: BLE001 - see the docstring
        return


def admission_view(root, policy):
    """``policy`` with its admission peak (``maxRssMib``) at the measured envelope when one is known.

    A policy with an explicit memory profile (``adaptiveMemory``) keeps its own estimate.
    """
    ceiling = policy.get('maxRssMib') if isinstance(policy, dict) else None
    if type(ceiling) is not int or 'adaptiveMemory' in policy or not isinstance(policy.get('trainerArgv'), list):
        return policy
    peak = _peak(root, policy)
    if peak is None:
        return policy
    return {**policy, 'maxRssMib': min(ceiling, envelope(peak))}

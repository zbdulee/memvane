"""Fixed-key numeric phase timing for selector warm-up and maintenance events.

Only these keys cross into a failure or a diagnostic event, and only as
finite non-negative numbers: never a message, path, source text or a bool.
A phase that was not measured (for example the server phases of a request
the client stopped waiting for) is absent, never reported as zero.
"""
from __future__ import annotations

import math

SECONDS = ('startup_wait_seconds', 'client_validation_seconds', 'initial_validation_seconds',
           'validation_seconds', 'identity_seconds', 'revalidation_seconds', 'model_load_seconds',
           'cached_model_load_seconds', 'forward_seconds', 'queue_wait_seconds',
           'resident_wall_seconds', 'warm_wait_seconds', 'drain_wait_seconds')
KEYS = frozenset(SECONDS + ('attempts',))
_CHAIN = 8


def _seconds(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        return None
    return round(float(value), 6)


def clean(timing):
    """The closed numeric subset of ``timing`` (anything else is dropped)."""
    if not isinstance(timing, dict):
        return {}
    row = {}
    for key in SECONDS:
        value = _seconds(timing.get(key))
        if value is not None:
            row[key] = value
    attempts = timing.get('attempts')
    if type(attempts) is int and attempts >= 0:
        row['attempts'] = attempts
    return row


def collect(exc):
    """Clean timing carried by ``exc`` and its causes; the outermost value wins."""
    chain, seen = [], set()
    while exc is not None and id(exc) not in seen and len(chain) < _CHAIN:
        seen.add(id(exc))
        chain.append(exc)
        exc = exc.__cause__ if exc.__cause__ is not None else exc.__context__
    row = {}
    for item in reversed(chain):
        row.update(clean(getattr(item, 'timing', None)))
    return row

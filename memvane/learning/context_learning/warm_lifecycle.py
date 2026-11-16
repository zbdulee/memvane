"""Bounded selector warm-up: one startup window, at most one drain and one retry.

The first attempt waits for the hosted service on its own startup window and
then sends with a fresh request budget. Only when that request runs out of
time (a cold load may outlive the 30-second wire window and stay loaded), the
current hosted service is given a bounded drain until it reports idle, and
exactly one ordinary request (no second startup window) follows. Every other
failure propagates at once. Worst case: startup + 2 x warm + drain.
"""
from __future__ import annotations

import time

from .inference_timing import clean

EVENT = 'learning.selector.warm.phase'


def emit(outcome, reason=None, timing=None, *, phase='warm'):
    """A phase event with a fixed reason code and numeric fields only.

    No state root or decision is named: the maintenance caller binds the
    selected decision around its execution.
    """
    try:
        from memvane.diagnostics import api as diagnostics
        diagnostics.emit(EVENT, outcome=outcome, reason=reason, stage=phase, **clean(timing))
    except Exception:
        pass


def reason(exc):
    """A fixed code for ``exc``: a service failure code, else ``warm_failed``."""
    from memvane.runtime.types import ServiceFailure
    code = exc.code if isinstance(exc, ServiceFailure) else None
    if isinstance(code, str) and 0 < len(code) <= 64 and code.replace('_', '').isalnum():
        return code
    return 'warm_failed'


def _drained(python, deadline):
    """False only if the same hosted service is still busy at ``deadline``.

    Nothing answering, or a different service (signature, python, or not
    hosted), has nothing of this warm-up to drain: the retry's own readiness
    check handles it.
    """
    from . import inference_service as service
    while True:
        try:
            answer = service._version(service.service_root(), deadline)
        except (OSError, ValueError):
            return True
        if not (service._current(answer) and answer.get('hosted') is True
                and answer.get('python') == python):
            return True
        if service._idle(answer):
            return True
        left = deadline - time.monotonic()
        if left <= 0:
            return False
        time.sleep(min(.05, left))


def attempt(predict, state, policy, request, artifact_sha, *, startup_seconds, warm_seconds,
            drain_seconds, measured=None):
    """Clean timing of a successful warm prediction; a failure carries it too."""
    from memvane.runtime.types import ServiceFailure
    began = time.monotonic()
    fixed = {**clean(measured), 'attempts': 1}

    def finish(row):
        row = clean(row)
        elapsed = time.monotonic() - began
        startup = fixed.get('startup_wait_seconds', row.get('startup_wait_seconds', 0))
        warm_wait = max(0, elapsed - startup - fixed.get('drain_wait_seconds', 0))
        return clean({**row, **fixed, 'warm_wait_seconds': round(warm_wait, 6)})
    try:
        _, row = predict(state, policy, request, artifact_sha,
                         deadline=time.monotonic() + warm_seconds, startup_seconds=startup_seconds)
        return finish(row)
    except ServiceFailure as exc:
        exc.timing = finish(exc.timing)
        if exc.code != 'deadline_expired' or getattr(exc, 'phase', None) != 'warm':
            raise
        first = exc
    if 'startup_wait_seconds' in first.timing:
        fixed['startup_wait_seconds'] = first.timing['startup_wait_seconds']
    drain_began = time.monotonic()
    idle = _drained(policy['trainerArgv'][0], drain_began + drain_seconds)
    fixed['drain_wait_seconds'] = round(time.monotonic()-drain_began, 6)
    if not idle:
        error = ServiceFailure('deadline_expired', timing=finish(first.timing))
        error.phase = 'drain'
        raise error from first
    fixed['attempts'] = 2
    try:
        _, row = predict(state, policy, request, artifact_sha, deadline=time.monotonic() + warm_seconds)
    except ServiceFailure as exc:
        # Second-attempt phases only: the first attempt's are not this one's.
        exc.timing = finish(exc.timing)
        raise
    return finish(row)

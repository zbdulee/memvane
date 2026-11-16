"""Client side of one inference request: the worker's answer and the warm-up path.

Imported lazily by ``inference_service`` (never by the hosted worker), so
service attributes are read at call time and fixtures that patch them apply.
"""
from __future__ import annotations

import math
import socket
import time

from memvane.runtime.types import ServiceFailure
from .inference_timing import clean


def answer(response, started, measured=None):
    """(prediction, timing) of a worker answer.

    The wire is unchanged: exactly ``{status, prediction, timing}``. A failure
    carries only the closed numeric timing (``inference_timing.clean``).
    """
    from .inference_service import _CODES
    def wall():
        return {**(measured or {}), 'resident_wall_seconds': round(time.perf_counter()-started, 6)}
    if not isinstance(response, dict) or set(response) != {'status', 'prediction', 'timing'}:
        raise ServiceFailure('worker_error', timing=clean(wall()))
    if response['status'] != 'ok':
        status = response['status']
        code = status if isinstance(status, str) and status in _CODES else 'worker_error'
        if code == 'inference_attestation_required':
            raise ValueError('trainer_inference_attestation_required')
        server = response['timing'] if isinstance(response['timing'], dict) else {}
        raise ServiceFailure(code, timing=clean({**server, **wall()}))
    timing = response['timing']
    if not isinstance(timing, dict):
        raise ServiceFailure('worker_error', timing=clean(wall()))
    return response['prediction'], {**timing, **wall()}


def predict_after_startup(state, policy, request, artifact_sha, *, deadline, pointer_name,
                          ensure_service, startup_seconds):
    """Warm-up only: readiness on its own deadline, then a fresh request budget.

    The request budget (at most the wire's 30 seconds) is taken from
    ``deadline`` before startup; the original absolute deadline is not used
    afterwards, so a slow start cannot consume the request's window. The
    startup and inference leases each use that phase's own deadline.
    """
    from . import inference_service as service
    from memvane.runtime.api import coordinator as Coordinator
    if (type(startup_seconds) not in (int, float) or not math.isfinite(startup_seconds)
            or startup_seconds <= 0):
        raise ValueError('startup_seconds_invalid')
    budget = min(30.0, deadline - time.monotonic())
    if not budget > 0:
        raise ServiceFailure('deadline_expired')
    measured = {}
    started = time.perf_counter()
    phase = 'startup'
    try:
        scope = service.namespace(state)
        root = service.service_root(create=True)
        began = time.monotonic()
        ready_by = began + startup_seconds
        from .warm_lifecycle import emit
        emit('started', phase='startup')
        try:
            with Coordinator(root).inference(ready_by, scope):
                service._ensure(root, policy['trainerArgv'][0], ready_by, ensure_service=ensure_service)
        finally:
            measured['startup_wait_seconds'] = round(time.monotonic()-began, 6)
        emit('completed', timing=measured, phase='startup')
        phase = 'warm'
        fresh = time.monotonic() + budget
        emit('started', phase='warm')
        with Coordinator(root).inference(fresh, scope):
            envelope = service._make_envelope(state, request, artifact_sha, fresh, pointer_name)
            response = service._exchange(root, envelope, fresh)
    except ServiceFailure as exc:
        exc.timing = clean({**exc.timing, **_numeric(measured, started)})
        exc.phase = phase
        raise
    except PermissionError as exc:
        error = ServiceFailure('permission_denied', timing=_numeric(measured, started))
        error.phase = phase
        raise error from exc
    except socket.timeout as exc:
        error = ServiceFailure('deadline_expired', timing=_numeric(measured, started))
        error.phase = phase
        raise error from exc
    except ValueError as exc:
        if str(exc) == 'resident_handshake_timeout':
            error = ServiceFailure('deadline_expired', timing=_numeric(measured, started))
            error.phase = phase
            raise error from exc
        exc.timing = _numeric(measured, started)
        exc.phase = phase
        raise
    try:
        return answer(response, started, measured)
    except ServiceFailure as exc:
        exc.phase = 'warm'
        raise


def _numeric(measured, started):
    return clean({**measured, 'resident_wall_seconds': round(time.perf_counter()-started, 6)})

"""The inference service's idle step: a selector model with no request for a while is released.

The service process stays up and keeps its socket; only the loaded model goes.
The next request loads it again like a first request (its admission and its
first-load timing). An application's prepared generation that waits for its
activation is never released.
"""
from __future__ import annotations

import sys
import time

# About fifteen minutes: a working session keeps the model, a machine left alone gets its memory back.
IDLE_RELEASE_SECONDS = 900


def release_idle(engine, now=None):
    """Release ``engine``'s model after ``IDLE_RELEASE_SECONDS`` without a request; whether it did.

    Called by the service's scoring thread, the engine's only user, between requests.
    An engine without the request clock (a test double) keeps its model.
    """
    last = getattr(engine, 'last_used', None)
    if type(last) not in (int, float):
        return False
    now = time.monotonic() if now is None else now
    pending = engine.pending
    waiting = pending is not None and now < pending[-1]  # A prepared generation before its deadline.
    if engine.active is None:
        if pending is None or waiting:
            return False
        engine.pending = None
        free_memory()
        return True
    if waiting or now - last < IDLE_RELEASE_SECONDS:
        return False
    engine.active = engine.footprint_mib = engine.cached_load = engine.pending = None
    free_memory()
    return True


def free_memory():
    """Return a released model's memory: the collector, then the device allocator's cache."""
    import gc
    gc.collect()
    torch = sys.modules.get('torch')
    for backend in ('mps', 'cuda'):
        try:
            module = getattr(torch, backend, None)
            if module is not None and (backend != 'cuda' or module.is_available()):
                module.empty_cache()
        except Exception:  # noqa: BLE001 - a backend that cannot empty its cache keeps it
            pass

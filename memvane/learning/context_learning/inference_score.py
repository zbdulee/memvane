"""One ordinary inference request inside the hosted worker's scoring thread.

Imported by ``inference_service._serve`` (which may run as ``__main__``), so
it never imports ``inference_service``; the failure classifier is passed in.
"""
from __future__ import annotations

import time

from .inference_timing import clean


def infer(engine, coordinator, validate, envelope, deadline, queue_wait, *, cancelled, failure):
    """The reply row for one request.

    Success keeps the existing timing dict. A failure replies with its fixed
    code and only the closed numeric phase timing measured so far (validation,
    revalidation, queue wait, partial load/forward), never exception text.
    """
    timing = {}
    measured = {'queue_wait_seconds': queue_wait}
    revalidation = [0.0]
    job = None

    def revalidate():
        began = time.perf_counter()
        try:
            return validate(envelope)
        finally:
            revalidation[0] += time.perf_counter()-began
    try:
        with coordinator.inference(deadline, str(envelope.get('namespace', ''))), \
                coordinator.inference_compute(deadline):
            began = time.perf_counter()
            try:
                # Only this initial validation may stand on a recent runtime
                # sweep; the revalidations below never do.
                from .inference_identity import reuse_window
                with reuse_window():
                    job = validate(envelope)
            finally:
                measured['initial_validation_seconds'] = round(time.perf_counter()-began, 6)
            result, timing = engine.run(job, cancelled=cancelled, revalidate=revalidate, timing=timing)
    except Exception as exc:
        measured['revalidation_seconds'] = round(revalidation[0], 6)
        identity = job.get('identity_timing') if isinstance(job, dict) else None
        return {'status': failure(exc), 'prediction': None,
                'timing': clean({**timing, **(identity if isinstance(identity, dict) else {}), **measured})}
    timing.update(job.get('identity_timing', {}))
    timing['initial_validation_seconds'] = measured['initial_validation_seconds']
    timing['revalidation_seconds'] = round(revalidation[0], 6)
    timing['queue_wait_seconds'] = queue_wait
    return {'status': 'ok', 'prediction': result, 'timing': timing}

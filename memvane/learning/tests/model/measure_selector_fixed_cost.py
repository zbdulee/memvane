"""Opt-in measurement of the selector's per-request work outside the forward (M-1).

Run with the Laya runtime interpreter in isolated mode under the development
guard's model-test kind (the model directory is stat-swept, never loaded)::

    npm run dev:guard -- --kind model-test -- env QMD_LAYA_MODEL=<model> \\
      <laya python> -I -B memvane/learning/tests/model/measure_selector_fixed_cost.py

Prints one JSON report of seconds per call (first and warm median) for the
identity work one request repeats: the code identity hash, the client wire
signature, the runtime root resolution, the runtime stat sweep, the loaded
module scope check and the device identity.
"""
import json
import os
from pathlib import Path
import statistics
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

from memvane.learning.context_learning import inference_identity as identity  # noqa: E402
from memvane.learning.context_learning import inference_service as service  # noqa: E402

REPEATS = 5


def _timed(call):
    started = time.perf_counter()
    value = call()
    return time.perf_counter() - started, value


def _series(call, *, reset=None):
    first, value = _timed(call)
    warm = []
    for _ in range(REPEATS):
        if reset is not None:
            reset()
        warm.append(_timed(call)[0])
    return {'first_s': round(first, 4), 'warm_median_s': round(statistics.median(warm), 4),
            'warm_max_s': round(max(warm), 4)}, value


def main():
    model_dir = Path(os.environ['QMD_LAYA_MODEL']).resolve()
    report = {}
    report['code_identity'], _ = _series(identity.code_identity)
    report['code_files'] = len(list(Path(identity.__file__).resolve().parents[2].rglob('*.py')))
    report['wire_signature'], _ = _series(service._signature.__wrapped__)
    report['runtime_roots'], roots = _series(identity.runtime_roots)
    roots = [*roots, model_dir]
    report['snapshot_sweep'], swept = _series(lambda: identity.snapshot(roots))
    report['snapshot_entries'] = len(swept)
    import torch  # noqa: F401
    import transformers  # noqa: F401
    import laya.common  # noqa: F401
    report['module_scope'], _ = _series(lambda: identity._module_scope(identity.runtime_roots()))
    report['device_identity'], _ = _series(identity.device_identity)
    report['python'] = sys.version.split()[0]
    print(json.dumps(report))


if __name__ == '__main__':
    main()

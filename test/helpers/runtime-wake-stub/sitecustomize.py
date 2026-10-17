"""Test-only: record runtime wake requests instead of the UID-home queue.

runtime_work.WakeQueue resolves its root from the OS account home, not HOME,
so an isolated HOME alone still reaches the live runtime-wakes.sqlite3. Load
only through a test child's PYTHONPATH; product entry points never import it.
"""
import json
import os

import runtime_work


def _request(kind, target='', *, start=True):
    with open(os.environ['QMD_TEST_RUNTIME_WAKE_LOG'], 'a', encoding='utf-8') as handle:
        handle.write(json.dumps({'kind': kind, 'target': target, 'start': start}) + '\n')
    return {'status': 'queued'}


runtime_work.request = _request
runtime_work.hook_wake = lambda: None

"""Small process-boundary adapters preserving existing CLI exit behavior."""
from __future__ import annotations

import json
import sqlite3
import sys


def runtime_work():
    from memvane.runtime.runtime_work import main
    try:
        return main(sys.argv[1:])
    except (OSError, ValueError, sqlite3.Error):
        return 75


def host_ack():
    from memvane.kernel.bundle_paths import plugin_root
    from memvane.learning.context_learning.history_lifecycle import acknowledge_host
    if len(sys.argv) != 2:
        return 2
    acknowledge_host(sys.argv[1], plugin_root())
    return 0


def selector_maintenance():
    from memvane.learning.selector_maintenance import run
    if len(sys.argv) == 3 and sys.argv[2] in ('force', 'newer'):
        try:
            run(sys.argv[1], force=sys.argv[2] == 'force')
        except Exception:
            pass
    return 0


def learning_shared():
    from memvane.learning.context_learning.history_lifecycle import worker_lease
    from memvane.learning.context_learning.shared import main
    with worker_lease():
        return main(sys.argv[1:])


def registry():
    from memvane.registry.memvane_registry import snapshot
    if sys.argv[1:] != ['status']:
        print('usage: memvane_registry.py status', file=sys.stderr)
        return 2
    print(json.dumps(snapshot(reconcile=False), sort_keys=True, ensure_ascii=False))
    return 0

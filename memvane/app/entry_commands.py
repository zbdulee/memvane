"""Small process-boundary adapters preserving existing CLI exit behavior."""
from __future__ import annotations

import json
import sqlite3
import sys


def runtime_work_main(argv):
    if argv[:1]==['cancel-build-wake'] and len(argv)==7 and argv[1::2]==['--project','--revision','--backup-dir']:
        # Official per-UID queue only; ownership is proven from private build state.
        from memvane.install.api import cancel_wake
        try:revision=int(argv[4])
        except ValueError:revision=None
        result=cancel_wake(argv[2],revision,argv[6])
        print(json.dumps(result,sort_keys=True,default=str));return 0 if result['status']!='refused' else 3
    if argv[:1]==['build-wake-status'] and len(argv)==3 and argv[1]=='--project':
        from memvane.install.api import wake_status
        print(json.dumps(wake_status(argv[2]),sort_keys=True,default=str));return 0
    from memvane.runtime.api import runtime_main as main
    from memvane.app.wiring import dispatch
    return main(argv, execute=dispatch)


def runtime_work():
    try:
        return runtime_work_main(sys.argv[1:])
    except (OSError, ValueError, sqlite3.Error):
        return 75


def host_ack():
    from memvane.kernel.bundle_paths import plugin_root
    from memvane.learning.api import acknowledge_host
    if len(sys.argv) != 2:
        return 2
    acknowledge_host(sys.argv[1], plugin_root())
    return 0


def selector_maintenance():
    from memvane.learning.api import run_selector_maintenance as run
    if len(sys.argv) == 3 and sys.argv[2] in ('force', 'newer'):
        try:
            run(sys.argv[1], force=sys.argv[2] == 'force')
        except Exception:
            pass
    return 0


def learning_shared():
    from memvane.learning.api import worker_lease
    from memvane.learning.api import learning_main as main
    with worker_lease():
        return main(sys.argv[1:])


def registry():
    from memvane.registry.api import snapshot
    if sys.argv[1:] != ['status']:
        print('usage: memvane_registry.py status', file=sys.stderr)
        return 2
    print(json.dumps(snapshot(reconcile=False), sort_keys=True, ensure_ascii=False))
    return 0

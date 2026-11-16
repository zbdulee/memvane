"""Small process-boundary adapters preserving existing CLI exit behavior."""
from __future__ import annotations

import json
import sqlite3
import sys


def runtime_work_main(argv):
    if argv[:1]==['build-wake-status'] and len(argv)==3 and argv[1]=='--project':
        from memvane.install.api import wake_status
        print(json.dumps(wake_status(argv[2]),sort_keys=True,default=str));return 0
    from memvane.runtime.api import runtime_main as main
    from memvane.app.wiring import dispatch, drop_event, stall_event
    return main(argv, execute=dispatch, on_drop=drop_event, on_stall=stall_event)


def runtime_work():
    try:
        return runtime_work_main(sys.argv[1:])
    except (OSError, ValueError, sqlite3.Error):
        return 75


def selector_maintenance():
    from memvane.learning.api import run_selector_maintenance as run
    from memvane.learning.api import bind_service_starter
    from memvane.resident.api import ensure_service
    bind_service_starter(ensure_service)
    if len(sys.argv) == 3 and sys.argv[2] in ('force', 'newer'):
        try:
            run(sys.argv[1], force=sys.argv[2] == 'force')
        except Exception:
            import os
            import traceback
            traceback.print_exc()
            try:
                sys.stderr.flush()
                size = os.fstat(2).st_size
                tail = os.pread(2, min(size, 8192), max(0, size - 8192)).decode('utf8', 'replace')
                from memvane.journal.api import select_route
                from memvane.diagnostics.api import capture
                from memvane.settings.api import diagnostic_enabled
                decision = select_route(sys.argv[1], action='update')
                capture(decision, event='learning.selector.maintenance', reason='worker_stderr',
                        child_stderr=tail, consent=diagnostic_enabled)
            except Exception:
                pass
    return 0


def learning_shared():
    from memvane.learning.api import bind_service_starter
    from memvane.learning.api import learning_main as main
    from memvane.resident.api import ensure_service
    bind_service_starter(ensure_service)
    return main(sys.argv[1:])


def registry():
    from memvane.registry.api import snapshot
    if sys.argv[1:] != ['status']:
        print('usage: memvane_registry.py status', file=sys.stderr)
        return 2
    print(json.dumps(snapshot(reconcile=False), sort_keys=True, ensure_ascii=False))
    return 0

"""Small process-boundary adapters preserving existing CLI exit behavior."""
from __future__ import annotations

import json
import sqlite3
import sys


def runtime_work_main(argv):
    if argv[:1]==['build-wake-status'] and len(argv)==3 and argv[1]=='--project':
        from memvane.install.api import wake_status
        print(json.dumps(wake_status(argv[2]),sort_keys=True,default=str));return 0
    if argv[:1] == ['snapshot-readonly'] and len(argv) in (1, 2):
        from memvane.runtime.api import runtime_main as main
        captured = []
        code = main(argv, execute=captured.append)
        if code == 0 and captured:
            print(json.dumps(captured[0], sort_keys=True, ensure_ascii=False))
        return code
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


LEDGER_VIEWS = ('dedup-pairs', 'dedup-skip', 'verify-log', 'audit')


def wiki_ledger():
    """Read-only view of a project's ``wiki-pipeline.sqlite3`` (public output keys, release.md).

    ``dedup-pairs`` lists queued pairs with the ``index`` the resolver's ``--index`` takes.
    A project without a ledger prints an empty list.
    """
    import argparse
    from memvane.settings import api as settings
    from memvane.wiki.pipeline import api as pipeline
    from memvane.wiki.pipeline.types import AUDIT_KINDS
    parser = argparse.ArgumentParser(prog='wiki-ledger')
    parser.add_argument('view', choices=LEDGER_VIEWS)
    parser.add_argument('kind', nargs='?', choices=AUDIT_KINDS)
    parser.add_argument('--cwd', default='.')
    parser.add_argument('--limit', type=int, default=50, help='newest log rows (verify-log, audit)')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(sys.argv[1:])
    if (args.view == 'audit') != (args.kind is not None):
        parser.error('audit takes exactly one kind; other views take none')
    found = settings.find_project_config(args.cwd)
    root = found['projectRoot']
    try:
        if args.view == 'dedup-pairs':
            rows = [{'index': i, 'pageA': row['pageA'], 'pageB': row['pageB'], 'score': row['score']}
                    for i, row in enumerate(pipeline.dedup_queue(root))]
        elif args.view == 'dedup-skip':
            rows = []
            for key, hashes in sorted(pipeline.suppressions(root, 'dedup').items()):
                page_a, _, page_b = key.partition('\n')
                rows.append({'pageA': page_a, 'pageB': page_b, 'hashes': hashes})
        else:
            kind = 'verify_log' if args.view == 'verify-log' else args.kind
            limit = max(0, args.limit)
            rows = pipeline.log_rows(root, kind)[-limit:] if limit else []
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(json.dumps({'error': 'ledger_unreadable', 'detail': type(exc).__name__}), file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, sort_keys=True))
    else:
        for row in rows:
            print(json.dumps(row, ensure_ascii=False, sort_keys=True))
    return 0

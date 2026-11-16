"""Synthetic GC subprocess paused immediately before commit."""

GC_HELD_CHILD = r'''
import json, os, sys, time
from pathlib import Path
args = json.loads(sys.argv[1])
from memvane.store import api as views
store = views.open_store(args['db'], repository=args['repository'], scope_id=args['scope'])
handle = store.capture_view(args['worktree_id'])
def before_commit(sql):
    if sql.strip().upper() == 'COMMIT':
        Path(args['ready']).touch()
        end = time.monotonic() + 20
        while not Path(args['release']).exists() and time.monotonic() < end:
            time.sleep(.01)
        if not Path(args['release']).exists():
            os._exit(19)
store._db.set_trace_callback(before_commit)
store.collect_view_rows(handle, limit=64)
store.close()
'''

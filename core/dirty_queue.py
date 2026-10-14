#!/usr/bin/env python3
import fcntl
import install_deadline
import os
from pathlib import Path

from dirty_queue_claim import queue_lock,_private_regular


def queue_path():
    legacy=Path.home()/'.config/qmd/dirty-queue'
    fixture=os.environ.get('QMD_SETUP_GUARD_FIXTURE')=='1'
    default=legacy if fixture else Path.home()/'Library/Application Support/qmd-auto-context/backend/dirty-queue'
    selected=Path(os.environ.get('QMD_DIRTY_QUEUE') or default)
    if not fixture and selected.resolve()==legacy.resolve():raise ValueError('legacy_dirty_queue_alias_forbidden')
    return selected


def enqueue_collections(selected, *, project_root=None):
    if not selected:
        return
    q = queue_path()
    q.parent.mkdir(mode=0o700,parents=True, exist_ok=True)
    lines = [
        f"{name}\t{selected[name]}" + (f"\t{project_root}" if project_root else "") + "\n"
        for name in sorted(selected)
    ]
    # The stable sidecar lock also covers the worker's atomic ACK replacement.
    # Locking the queue inode alone would let an already-open enqueuer append to
    # an unlinked old inode after replacement and silently lose that edit.
    with queue_lock(q):
        fd=os.open(q,os.O_WRONLY|os.O_APPEND|os.O_CREAT|os.O_NOFOLLOW,0o600)
        try:
            _private_regular(q)
            if os.fstat(fd).st_ino!=q.stat().st_ino:raise ValueError('dirty_queue_changed_before_enqueue')
        except Exception:
            os.close(fd);raise
        with os.fdopen(fd,"a",encoding="utf-8") as f:
            install_deadline.flock(f.fileno(), fcntl.LOCK_EX)
            f.writelines(lines)
            f.flush(); os.fsync(f.fileno())
            install_deadline.flock(f.fileno(), fcntl.LOCK_UN)


def enqueue_project_collections(project_root, selected):
    """Retain selected-index ownership even for external collection roots."""
    if not selected:
        return
    import qmd_route
    root = Path(project_root).resolve()
    route = qmd_route.project_paths(root)
    enqueue_collections(selected, project_root=str(root) if route['selected'] else None)

if __name__=='__main__':
    import sys
    if sys.argv[1:]!=['path']:raise SystemExit(2)
    print(queue_path())

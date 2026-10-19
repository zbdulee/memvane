"""Persistent wake hints for existing workers; no model/service installation.

Content queues own ACK/checkpoints. One bounded UID-global waiter holds only its
dispatch lease while sleeping, never a writer, trainer or compute lease.
"""
from contextlib import contextmanager
import fcntl
import json
import math
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import time

from resource_deferred import ResourceDeferred
# Neutral lease primitives: importing this module never loads context_learning.
from runtime_lease import atomic_json as _atomic_json,coordination_lock as _lock,private_state as _private_state
import runtime_wake_policy as wake_policy
# Re-exported: drain's default executor and external callers keep this object.
from runtime_handlers import dispatch

POLICY={'maxRssMib':512,'minAvailableMib':768,'maxLoadPerCpu':1,
        'targetHeadroomFraction':.35}
# Re-exported names; the decision SSOT is runtime_wake_policy.
KINDS=wake_policy.KINDS
RETRY=wake_policy.RETRY
MAX_WAKES=wake_policy.MAX_WAKES


def Coordinator(*args,**kwargs):
    # Call-time shim: the real coordinator and its root stay in context_learning.
    # Module attribute, so tests can keep patching runtime_work.Coordinator.
    from context_learning.resource_coordination import Coordinator as coordinator
    return coordinator(*args,**kwargs)


def default_root():
    from context_learning.resource_coordination import default_root as root
    return root()


def sample():
    from context_learning.shared import resource_report
    return resource_report(POLICY,detailed=True,admission=False)


@contextmanager
def foreground(namespace,*,coordinator=None):
    # Advertising demand does not acquire compute.lock or block QMD retrieval.
    with (coordinator or Coordinator()).inference(time.monotonic()+.1,namespace):yield


def guarded_run(argv,*,coordinator=None,execute=None,**options):
    """Admit one fixed caller-owned command while preserving its index route."""
    execute=execute or subprocess.run
    if os.environ.get('QMD_SETUP_GUARD_FIXTURE')=='1':
        return execute(argv,**options)
    controller=coordinator or Coordinator()
    try:report=sample()
    except (OSError,ValueError,TypeError,KeyError):report=None
    with controller.step(POLICY,report,with_fd=True) as (_,fd):
        from runtime_cpu import lease,environment
        with lease(controller.root) as budget:
            env=environment({**os.environ,**(options.pop('env',None) or {})})
            fds=(*options.pop('pass_fds',()),fd,*budget.fds)
            return execute(argv,env=env,pass_fds=fds,**options)


def qmd_operation(argv,*,coordinator=None,execute=None):
    """One QMD command is its indivisible checkpoint boundary; never SIGSTOP."""
    try:
        return guarded_run(argv,coordinator=coordinator,execute=execute,
            stdin=subprocess.DEVNULL,check=False).returncode
    except ResourceDeferred:return 75


class WakeQueue:
    def __init__(self,root=None):
        self.root=_private_state(Path(root) if root is not None else default_root())
        self.path=self.root/'runtime-wakes.sqlite3'

    @contextmanager
    def connect(self):
        fd=os.open(self.path,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        try:
            info=os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid()
                    or info.st_mode&0o077 or info.st_nlink!=1):raise ValueError('unsafe_runtime_queue')
        finally:os.close(fd)
        db=sqlite3.connect(self.path,timeout=.2);db.row_factory=sqlite3.Row
        try:
            db.execute('PRAGMA synchronous=FULL')
            db.execute('CREATE TABLE IF NOT EXISTS wakes (id TEXT PRIMARY KEY,kind TEXT NOT NULL,target TEXT NOT NULL,revision INTEGER NOT NULL,status TEXT NOT NULL,due REAL NOT NULL,attempts INTEGER NOT NULL,reason TEXT)')
            with db:yield db
        finally:db.close()

    def put(self,kind,target,*,now=None):
        wake_policy.validate_wake(kind,target)
        key=wake_policy.wake_key(kind,target)
        now=time.time() if now is None else now
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            exists=db.execute('SELECT 1 FROM wakes WHERE id=?',(key,)).fetchone()
            limit=wake_policy.capacity_limit(kind,MAX_WAKES)
            if not exists and db.execute('SELECT COUNT(*) FROM wakes').fetchone()[0]>=limit:
                _atomic_json(self.root/'runtime-wake-overflow.json',{'status':'deferred_queue_capacity',
                    'kind':kind,'key':key,'at':now,'limit':MAX_WAKES})
                return None
            db.execute("INSERT INTO wakes VALUES (?,?,?,1,'pending',?,0,NULL) ON CONFLICT(id) DO UPDATE SET revision=revision+1",(key,kind,target,now))
        return key

    def pending(self):
        with self.connect() as db:return [dict(row) for row in db.execute('SELECT * FROM wakes ORDER BY due,id')]


def drain(queue,*,execute=dispatch,ready=None,clock=time.monotonic,wall=time.time,
          sleep=time.sleep,max_seconds=60,max_runs=8,lease_fd=None):
    if not 0<max_seconds<=60 or not 1<=max_runs<=8:raise ValueError('invalid_runtime_budget')
    fd=_lock(queue.root/'runtime-worker.lock') if lease_fd is None else lease_fd
    try:
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return {'status':'busy','runs':0}
        with queue.connect() as db:db.execute("UPDATE wakes SET status='pending' WHERE status='running'")
        controller=Coordinator(queue.root);deadline=clock()+max_seconds;runs=0
        def admitted(row):
            if not wake_policy.needs_compute_admission(row['kind']):
                return True
            try:report=sample()
            except (OSError,ValueError,TypeError,KeyError):report=None
            return controller.check(POLICY,report)['ready']
        injected_ready=ready
        while runs<max_runs and clock()<deadline:
            rows=queue.pending()
            if not rows:return {'status':'drained','runs':runs}
            due=wake_policy.due_rows(rows,wall())
            row=wake_policy.next_row(due) if due else None
            try:can_run=row is not None and (injected_ready() if injected_ready is not None else admitted(row))
            except (OSError,ValueError,TypeError,KeyError,ResourceDeferred):can_run=False
            if not can_run:
                sleep(wake_policy.idle_wait(deadline-clock()));continue
            with queue.connect() as db:
                changed=db.execute("UPDATE wakes SET status='running' WHERE id=? AND revision=? AND status='pending'",(row['id'],row['revision'])).rowcount
            if not changed:continue
            try:done=execute(row);error=None
            except Exception as exc:done=False;error=exc
            runs+=1
            with queue.connect() as db:
                step=wake_policy.settle(row['attempts'],done=done,error=error,now=wall())
                if step['ack']:
                    # Revision-scoped ACK: a wake re-put while running survives, re-pended.
                    db.execute('DELETE FROM wakes WHERE id=? AND revision=?',(row['id'],row['revision']))
                    db.execute("UPDATE wakes SET status='pending',due=? WHERE id=?",(step['due'],row['id']))
                else:
                    db.execute("UPDATE wakes SET status='pending',due=?,attempts=attempts+1,reason=? WHERE id=?",(step['due'],step['reason'],row['id']))
        return {'status':'pending' if queue.pending() else 'drained','runs':runs}
    finally:os.close(fd)


def wake():
    """Pass the dispatch lease across spawn, avoiding one child per session."""
    queue=WakeQueue()
    if not queue.path.exists() or not queue.pending():return False
    fd=_lock(queue.root/'runtime-worker.lock')
    try:
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return False
        subprocess.Popen([sys.executable,'-B',str(Path(__file__).resolve()),'worker',str(fd)],
            stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
            close_fds=True,pass_fds=(fd,),start_new_session=True)
        return True
    finally:os.close(fd)


def request(kind,target='',*,start=True):
    # Fixture runs must never create runtime files/children in the real UID home.
    if os.environ.get('QMD_SETUP_GUARD_FIXTURE')=='1' or os.environ.get('QMD_SANDBOX'):return
    key=WakeQueue().put(kind,target)
    if start and os.environ.get('QMD_RUNTIME_DISPATCH')!='1':wake()
    return {'status':'queued' if key is not None else 'deferred_queue_capacity'}


def _drain_and_rewake(queue,fd):
    """Keep a pending durable wake live after this bounded owner exits.

    drain closes its passed lease before returning. The successor is spawned
    only if the queue still has rows and the same kernel lock can be acquired;
    another owner or a concurrent request can win safely. Checking even after
    a drained result closes the enqueue-versus-last-read race.
    """
    try:return drain(queue,lease_fd=fd)
    finally:wake()


def hook_wake():
    if os.environ.get('QMD_SANDBOX') or os.environ.get('QMD_SETUP_GUARD_FIXTURE')=='1':return
    wake()


def main(argv):
    if argv[:1]==['qmd'] and len(argv)>2:return qmd_operation(argv[1:])
    if argv==['wake']:hook_wake();return 0
    if argv==['index-pending']:
        from dirty_queue import queue_path
        path=queue_path()
        if path.is_file() and path.stat().st_size:request('index')
        return 0
    if argv[:1]==['worker'] and len(argv)==2:
        queue=WakeQueue();fd=int(argv[1]);info=os.fstat(fd);expected=(queue.root/'runtime-worker.lock').stat()
        if (info.st_dev,info.st_ino)!=(expected.st_dev,expected.st_ino):raise ValueError('runtime_lease_changed')
        _drain_and_rewake(queue,fd);return 0
    return 2


if __name__=='__main__':
    try:raise SystemExit(main(sys.argv[1:]))
    except (OSError,ValueError,sqlite3.Error):raise SystemExit(75)

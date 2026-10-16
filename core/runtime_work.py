"""Persistent wake hints for existing workers; no model/service installation.

Content queues own ACK/checkpoints. One bounded UID-global waiter holds only its
dispatch lease while sleeping, never a writer, trainer or compute lease.
"""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import time

from context_learning.resource_coordination import Coordinator,default_root,_lock,decision
from context_learning.local_cycle import ResourceDeferred
from context_learning.local_cycle import _atomic_json
from context_learning import resident

POLICY={'maxRssMib':512,'minAvailableMib':768,'maxLoadPerCpu':1,
        'targetHeadroomFraction':.35}
KINDS={'index':0,'update':1,'cleanup':2,'learning':3,'auto':3}
RETRY={'busy','deferred_resource','staged_evaluation_deferred','apply_deferred','setup_required','setup_in_progress'}
MAX_WAKES=256


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
        self.root=resident._private_state(Path(root) if root is not None else default_root())
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
        if kind not in KINDS or not isinstance(target,str) or len(target.encode())>4096:raise ValueError('invalid_runtime_wake')
        if kind=='index':
            if target:raise ValueError('invalid_runtime_wake')
        elif kind=='update':
            if len(target)!=32 or any(c not in '0123456789abcdef' for c in target):raise ValueError('invalid_runtime_wake')
        elif not Path(target).is_absolute() or '\x00' in target:raise ValueError('invalid_runtime_wake')
        key=hashlib.sha256((kind+'\0'+target).encode()).hexdigest()
        now=time.time() if now is None else now
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            exists=db.execute('SELECT 1 FROM wakes WHERE id=?',(key,)).fetchone()
            limit=MAX_WAKES if kind=='index' else MAX_WAKES-1
            if not exists and db.execute('SELECT COUNT(*) FROM wakes').fetchone()[0]>=limit:
                _atomic_json(self.root/'runtime-wake-overflow.json',{'status':'deferred_queue_capacity',
                    'kind':kind,'key':key,'at':now,'limit':MAX_WAKES})
                return None
            db.execute("INSERT INTO wakes VALUES (?,?,?,1,'pending',?,0,NULL) ON CONFLICT(id) DO UPDATE SET revision=revision+1",(key,kind,target,now))
        return key

    def pending(self):
        with self.connect() as db:return [dict(row) for row in db.execute('SELECT * FROM wakes ORDER BY due,id')]


def dispatch(row):
    """Fixed handlers revalidate setup/ownership/authorization themselves."""
    if row['kind']=='index':
        result=subprocess.run(['bash',str(Path(__file__).resolve().parents[1]/'backend/index_worker.sh')],
            stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
            env={**os.environ,'QMD_RUNTIME_DISPATCH':'1'},check=False)
        from dirty_queue import queue_path
        path=queue_path()
        return result.returncode==0 and (not path.exists() or path.stat().st_size==0)
    if row['kind']=='update':
        import update_hook_queue
        update_hook_queue.worker(row['target'])
        return not (update_hook_queue._private_queue()/(row['target']+'.job.json')).exists()
    if row['kind']=='learning':
        import config
        import setup_guard
        from context_learning import shared
        project=Path(row['target'])
        if not project.is_dir():return True
        if setup_guard.status(project)['status']!='ready':return False
        cfg=config.find_project_config(str(project))['config'];root=shared._safe_root(cfg,project)
        if root is None:return True
        # Resource retries must not launch a teacher or create fresh captures.
        from context_learning.history_lifecycle import worker_lease
        with worker_lease():
            if setup_guard.status(project)['status']!='ready':return False
            result=shared.run_training_pending(root,project)
        return result['status'] not in RETRY
    if row['kind']=='auto':
        import context_learning_auto
        result=context_learning_auto.run(Path(row['target']))
        return result['status'] not in RETRY
    if row['kind']=='cleanup':
        import setup_guard,qmd_route
        project=Path(row['target'])
        if not project.is_dir():return True
        if setup_guard.status(project)['status']!='ready':return False
        route=qmd_route.project_paths(project)
        result=subprocess.run([sys.executable,'-B',str(Path(__file__).with_name('orphan_reclaim.py')),
            '--cwd',str(project),'--json'],stdin=subprocess.DEVNULL,capture_output=True,text=True,
            env={**os.environ,**{key:route[key] for key in qmd_route.PATH_KEYS},'QMD_RUNTIME_DISPATCH':'1'},check=False)
        if result.returncode:return False
        answer=json.loads(result.stdout)
        return answer.get('reason')!='resource_deferred'
    raise ValueError('invalid_runtime_handler')


def drain(queue,*,execute=dispatch,ready=None,clock=time.monotonic,wall=time.time,
          sleep=time.sleep,max_seconds=60,max_runs=8,lease_fd=None):
    if not 0<max_seconds<=60 or not 1<=max_runs<=8:raise ValueError('invalid_runtime_budget')
    fd=_lock(queue.root/'runtime-worker.lock') if lease_fd is None else lease_fd
    try:
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return {'status':'busy','runs':0}
        with queue.connect() as db:db.execute("UPDATE wakes SET status='pending' WHERE status='running'")
        controller=Coordinator(queue.root);deadline=clock()+max_seconds;runs=0
        def admitted():
            try:report=sample()
            except (OSError,ValueError,TypeError,KeyError):report=None
            return controller.check(POLICY,report)['ready']
        ready=ready or admitted
        while runs<max_runs and clock()<deadline:
            rows=queue.pending()
            if not rows:return {'status':'drained','runs':runs}
            due=[r for r in rows if r['due']<=wall()]
            try:can_run=bool(due) and ready()
            except (OSError,ValueError,TypeError,KeyError,ResourceDeferred):can_run=False
            if not can_run:
                sleep(min(5,max(0,deadline-clock())));continue
            row=min(due,key=lambda r:(KINDS[r['kind']],r['due'],r['id']))
            with queue.connect() as db:
                changed=db.execute("UPDATE wakes SET status='running' WHERE id=? AND revision=? AND status='pending'",(row['id'],row['revision'])).rowcount
            if not changed:continue
            try:done=execute(row);reason='pending'
            except Exception as exc:done=False;reason=type(exc).__name__
            runs+=1
            with queue.connect() as db:
                if done:
                    db.execute('DELETE FROM wakes WHERE id=? AND revision=?',(row['id'],row['revision']))
                    db.execute("UPDATE wakes SET status='pending',due=? WHERE id=?",(wall(),row['id']))
                else:
                    delay=min(300,5*2**min(row['attempts'],6))
                    db.execute("UPDATE wakes SET status='pending',due=?,attempts=attempts+1,reason=? WHERE id=?",(wall()+delay,reason,row['id']))
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
        drain(queue,lease_fd=fd);return 0
    return 2


if __name__=='__main__':
    try:raise SystemExit(main(sys.argv[1:]))
    except (OSError,ValueError,sqlite3.Error):raise SystemExit(75)

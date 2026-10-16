"""One model-free child tied to the existing shared daemon's lifecycle pipe."""
import fcntl
import os
from pathlib import Path
import select
import sqlite3
import subprocess
import sys
import time

from context_learning.resource_coordination import Coordinator,_lock
from context_learning.local_cycle import ResourceDeferred
import runtime_work

INTERVAL=5.
IDLE_INTERVAL=30.


def parent_alive(fd):
    try:return not select.select([fd],[],[],0)[0] or bool(os.read(fd,1))
    except OSError:return False


def run(queue,*,coordinator=None,probe=runtime_work.sample,wake=runtime_work.wake,
        alive=lambda:True,wait=time.sleep,wall=time.time,lease_fd=None):
    controller=coordinator or Coordinator(queue.root)
    fd=_lock(queue.root/'runtime-monitor.lock') if lease_fd is None else lease_fd
    samples=wakes=0
    try:
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return {'status':'busy','samples':0,'wakes':0}
        while alive():
            interval=INTERVAL
            try:
                rows=queue.pending()
                if not rows:interval=IDLE_INTERVAL
                else:
                    report=probe();samples+=1
                    ready=controller.check(runtime_work.POLICY,report)
                    if ready['ready'] and any(row['due']<=wall() for row in rows):
                        wake();wakes+=1
            except (OSError,ValueError,sqlite3.Error):pass  # retain canonical work
            wait(interval)
        return {'status':'parent_closed','samples':samples,'wakes':wakes}
    finally:os.close(fd)


class Handle:
    def __init__(self,process,write):self.process=process;self.write=write
    def close(self):
        if self.write is not None:os.close(self.write);self.write=None
        try:self.process.wait(timeout=2.5)
        except subprocess.TimeoutExpired:
            from context_learning.training_watch import stop_owned
            stop_owned(self.process)


class Supervisor:
    """Cheap daemon-thread supervision; sampling runs in the separate child."""
    def __init__(self,root,*,factory=None,clock=time.monotonic):
        self.root=Path(root);self.factory=factory or start;self.clock=clock
        self.handle=None;self.next_start=0.;self.status='not_started'
    def tick(self):
        if self.handle is not None:
            if self.handle.process.poll() is None:return
            self.handle.close();self.handle=None;self.status='exited'
        if self.clock()<self.next_start:return
        self.next_start=self.clock()+30
        try:
            self.handle=self.factory(self.root)
            self.status='running' if self.handle is not None else 'busy_or_disabled'
        except (OSError,ValueError):self.status='unavailable'
    def close(self):
        if self.handle is not None:self.handle.close();self.handle=None
        self.status='closed'


def start(root,*,spawn=None):
    if os.environ.get('QMD_SETUP_GUARD_FIXTURE')=='1':return None
    spawn=spawn or subprocess.Popen;root=Path(root)
    fd=_lock(root/'runtime-monitor.lock');read=write=None
    try:
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return None
        read,write=os.pipe()
        process=spawn([sys.executable,'-B',str(Path(__file__).resolve()),str(root),str(fd),str(read)],
            stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
            close_fds=True,pass_fds=(fd,read),start_new_session=True)
        result=Handle(process,write);write=None;return result
    finally:
        os.close(fd)
        if read is not None:os.close(read)
        if write is not None:os.close(write)


def main(args):
    if len(args)!=3:return 2
    root=Coordinator().root
    if Path(args[0])!=root:return 2
    lease,read=map(int,args[1:]);actual=os.fstat(lease);expected=(root/'runtime-monitor.lock').stat()
    if (actual.st_dev,actual.st_ino)!=(expected.st_dev,expected.st_ino):return 2
    try:
        run(runtime_work.WakeQueue(root),lease_fd=lease,alive=lambda:parent_alive(read),
            wait=lambda seconds:select.select([read],[],[],seconds))
    finally:os.close(read)
    return 0


if __name__=='__main__':raise SystemExit(main(sys.argv[1:]))

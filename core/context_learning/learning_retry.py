"""One bounded training-only waiter; pending work survives its exit/reboot."""
import fcntl
import os
import time
from .learning_queue import LearningQueue
from .resource_coordination import _lock


def wait_pending(root,execute,*,max_seconds=60,max_runs=2,clock=time.monotonic,
                 wall=time.time,sleep=time.sleep):
    if not 0<max_seconds<=60 or not 1<=max_runs<=2:raise ValueError('invalid_learning_retry_budget')
    fd=_lock(root/'learning-retry.lock')
    try:
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return {'status':'retry_waiter_exists','retryScheduled':False}
        queue=LearningQueue(root);deadline=clock()+max_seconds;runs=0
        while runs<max_runs:
            jobs=queue.pending(now=float('inf'))
            if not jobs:return {'status':'drained','runs':runs,'retryScheduled':False}
            at=min(job['retry_at'] for job in jobs)
            while at>wall():
                left=deadline-clock()
                if left<=0:return {'status':'pending','next_retry':at,'runs':runs,'retryScheduled':False}
                sleep(min(1,at-wall(),left))
            if clock()>=deadline:return {'status':'pending','next_retry':at,'runs':runs,'retryScheduled':False}
            result=execute();runs+=1
            if result['status'] in ('runtime_approval_required','disabled','learning_job_blocked','staged_source_revoked'):
                return {'status':result['status'],'runs':runs,'retryScheduled':False}
        jobs=queue.pending(now=float('inf'))
        return {'status':'pending' if jobs else 'drained','runs':runs,'retryScheduled':False}
    finally:os.close(fd)

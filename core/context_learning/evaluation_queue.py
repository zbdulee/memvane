"""Durable quality-only work; contains hashes, never source bodies."""
from contextlib import contextmanager
from pathlib import Path
import fcntl,os,time
from .learning_queue import LearningQueue
from .resource_coordination import _lock

class EvaluationQueue:
    def __init__(self,root,*,connect=None):
        self.base=LearningQueue(root,connect=connect);self.root=Path(root);self.connect=self.base.connect

    def schema(self,db):
        db.execute('CREATE TABLE IF NOT EXISTS learning_evaluations (job_id TEXT PRIMARY KEY, cycle_id TEXT NOT NULL, stage_sha TEXT NOT NULL, validation_sha TEXT NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL, failures INTEGER NOT NULL, retry_at REAL NOT NULL, reason TEXT)')
        if not db.in_transaction:db.execute('BEGIN IMMEDIATE')

    def get(self,job_id,*,db=None):
        if db is None:
            with self.connect() as db:return self.get(job_id,db=db)
        self.schema(db)
        row=db.execute('SELECT * FROM learning_evaluations WHERE job_id=?',(job_id,)).fetchone()
        return dict(zip(('id','cycle_id','stage_sha','validation_sha','status','attempts','failures','retry_at','reason'),row)) if row else None

    def ensure(self,cycle_id,stage_sha,validation_sha):
        job_id='evaluate:'+cycle_id
        with self.connect() as db:
            old=self.get(job_id,db=db)
            if old is None:
                db.execute("INSERT INTO learning_evaluations VALUES (?,?,?,?,'queued',0,0,0,NULL)",(job_id,cycle_id,stage_sha,validation_sha))
            elif old['stage_sha']!=stage_sha:raise ValueError('evaluation_stage_changed')
            elif old['status']=='waiting_quality' and old['validation_sha']!=validation_sha:
                db.execute("UPDATE learning_evaluations SET status='queued',retry_at=0,validation_sha=?,reason=NULL WHERE job_id=?",(validation_sha,job_id))
            return self.get(job_id,db=db)

    def finish(self,job_id,status):
        if status not in ('completed','waiting_quality','blocked'):raise ValueError('evaluation_status_invalid')
        with self.connect() as db:
            self.schema(db);db.execute('UPDATE learning_evaluations SET status=?,retry_at=0 WHERE job_id=?',(status,job_id))

    def refresh(self,cycle_id):
        with self.connect() as db:
            self.schema(db)
            db.execute("UPDATE learning_evaluations SET status='queued',retry_at=0 WHERE cycle_id=? AND status IN ('completed','waiting_quality')",(cycle_id,))

    def defer(self,job_id,reason,*,failure=False,now=None):
        now=time.time() if now is None else now
        with self.connect() as db:
            old=self.get(job_id,db=db);failures=old['failures']+int(failure)
            db.execute('UPDATE learning_evaluations SET status=?,failures=?,retry_at=?,reason=? WHERE job_id=?',
                ('blocked' if failures>=3 else 'deferred',failures,now+min(300,30*2**min(old['attempts'],4)),reason,job_id))

    @contextmanager
    def claim(self,job_id,*,now=None):
        now=time.time() if now is None else now
        fd=_lock(self.root/'evaluation-claim.lock');claimed=False
        try:
            try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:yield None;return
            with self.connect() as db:
                job=self.get(job_id,db=db)
                if job is None or job['status'] not in ('queued','running','deferred') or job['retry_at']>now:lease=None
                else:
                    db.execute("UPDATE learning_evaluations SET status='running',attempts=attempts+1 WHERE job_id=?",(job_id,));claimed=True;lease=fd
            yield lease
        finally:
            if claimed:
                with self.connect() as db:
                    db.execute("UPDATE learning_evaluations SET status='deferred',retry_at=?,reason='interrupted' WHERE job_id=? AND status='running'",(now,job_id))
            os.close(fd)

    def pending(self,*,now=None):
        now=time.time() if now is None else now
        with self.connect() as db:
            self.schema(db)
            return [self.get(row[0],db=db) for row in db.execute("SELECT job_id FROM learning_evaluations WHERE status IN ('queued','running','deferred') AND retry_at<=? ORDER BY rowid",(now,)).fetchall()]

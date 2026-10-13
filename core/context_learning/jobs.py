"""Private request leases plus POSIX locks; no worker/daemon or automatic retry."""
import fcntl
import json
import os
from pathlib import Path
import secrets
import sqlite3
import stat
import time

from .contracts import digest
from .store import canonical, database


# The cancel probe runs every 0.1s inside a paid teacher call. Another
# process's short transaction (job-status, cancel-job) must not abort that
# call: the probe waits briefly, treats a lock as "not cancelled yet", and
# re-raises only once the lock has persisted past CANCEL_PROBE_GRACE.
CANCEL_PROBE_TIMEOUT = 0.25
CANCEL_PROBE_GRACE = 5.0


def _lock_contention(exc):
    return isinstance(exc, sqlite3.OperationalError) and str(exc) in (
        'database is locked', 'database table is locked')


def _table(db):
    db.execute('CREATE TABLE IF NOT EXISTS teacher_jobs (request_id TEXT PRIMARY KEY, owner TEXT NOT NULL, owner_pid INTEGER NOT NULL, input_sha256 TEXT NOT NULL, expires REAL NOT NULL, status TEXT NOT NULL, cancel_requested INTEGER NOT NULL DEFAULT 0)')


class JobLease:
    """Lock prevents duplicate live owners even when metadata expires.

    Crashed owners release the OS lock; their metadata must expire before reuse.
    Lock files are never unlinked while reusable, avoiding lock-inode races.
    """
    def __init__(self, state_dir, request_id, *, lease_seconds=60):
        if not isinstance(request_id, str) or not request_id or len(request_id)>128:
            raise ValueError('invalid_request_id')
        if type(lease_seconds) not in (int,float) or not 0 < lease_seconds <= 60:
            raise ValueError('invalid_lease')
        self.state_dir=state_dir;self.request_id=request_id
        self.seconds=lease_seconds;self.owner=secrets.token_hex(16)
        self.fd=None;self.acquired=False;self.result_status='failed'
        self.next_check=0;self.busy_since=None

    def __enter__(self):
        try:
            with database(self.state_dir) as db:
                if not db.execute('SELECT 1 FROM samples WHERE request_id=?',(self.request_id,)).fetchone():
                    raise ValueError('unknown_or_deleted_request')
            path=Path(self.state_dir)/('teacher-job-'+digest(self.request_id)+'.lock')
            self.fd=os.open(path,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
            info=os.fstat(self.fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_nlink!=1 or info.st_mode & 0o077:
                raise ValueError('unsafe_job_lock')
            try:fcntl.flock(self.fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:raise ValueError('job_busy')
            with database(self.state_dir) as db:
                _table(db)
                sample=db.execute('SELECT body FROM samples WHERE request_id=?',(self.request_id,)).fetchone()
                if sample is None or db.execute('SELECT 1 FROM tombstones WHERE request_id=?',(self.request_id,)).fetchone():
                    raise ValueError('unknown_or_deleted_request')
                row=db.execute('SELECT status,expires FROM teacher_jobs WHERE request_id=?',(self.request_id,)).fetchone()
                if row and row[0]=='running' and row[1]>time.time():
                    raise ValueError('job_busy')
                db.execute('INSERT OR REPLACE INTO teacher_jobs VALUES (?,?,?,?,?,?,0)',
                    (self.request_id,self.owner,os.getpid(),digest(canonical(json.loads(sample[0]))),time.time()+self.seconds,'running'))
            self.acquired=True
            return self
        except BaseException:
            if self.fd is not None:os.close(self.fd);self.fd=None
            raise

    def cancelled(self):
        now=time.monotonic()
        if now<self.next_check:return False
        self.next_check=now+.1
        try:
            with database(self.state_dir,timeout=CANCEL_PROBE_TIMEOUT) as db:
                row=db.execute('SELECT owner,status,expires,cancel_requested FROM teacher_jobs WHERE request_id=?',(self.request_id,)).fetchone()
                deleted=db.execute('SELECT 1 FROM tombstones WHERE request_id=?',(self.request_id,)).fetchone()
        except sqlite3.OperationalError as exc:
            if not _lock_contention(exc):raise
            if self.busy_since is None:self.busy_since=now
            if now-self.busy_since>=CANCEL_PROBE_GRACE:raise
            return False
        self.busy_since=None
        return bool(deleted or not row or row[0]!=self.owner or row[1]!='running' or row[2]<=time.time() or row[3])

    def __exit__(self, kind, value, traceback):
        try:
            if self.acquired:
                with database(self.state_dir) as db:
                    db.execute('UPDATE teacher_jobs SET status=?,expires=? WHERE request_id=? AND owner=?',
                               (self.result_status if kind is None else 'failed',time.time(),self.request_id,self.owner))
        finally:
            if self.fd is not None:os.close(self.fd);self.fd=None


def job_status(state_dir, request_id):
    with database(state_dir) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='teacher_jobs'").fetchone():return {'status':'not_found'}
        row=db.execute('SELECT status,expires,cancel_requested,input_sha256,owner_pid FROM teacher_jobs WHERE request_id=?',(request_id,)).fetchone()
        if not row:return {'status':'not_found'}
        return {'status':row[0],'expires_at':row[1],'expired':row[1]<=time.time(),
                'cancel_requested':bool(row[2]),'input_sha256':row[3],'owner_pid':row[4]}


def cancel_job(state_dir, request_id):
    with database(state_dir) as db:
        _table(db)
        changed=db.execute('UPDATE teacher_jobs SET cancel_requested=1 WHERE request_id=? AND status=? AND expires>?',
                           (request_id,'running',time.time())).rowcount
        return {'status':'cancel_requested' if changed else 'not_running'}


def label_job(state_dir, request_id, *, enabled=False, host=None, **options):
    """Explicit job API. Cached complete provisional results avoid paid duplicates."""
    if enabled is not True:return {'status':'disabled','cli_invocations':0}
    result=None
    try:
        external_cancel=options.pop('cancel',None)
        if external_cancel is not None and not callable(external_cancel):raise ValueError('invalid_cancellation')
        with JobLease(state_dir,request_id) as lease:
            with database(state_dir) as db:
                sample=json.loads(db.execute('SELECT body FROM samples WHERE request_id=?',(request_id,)).fetchone()[0])
                if host not in ('claude','codex') or host!=sample['host']:
                    raise ValueError('host_mismatch')
                rows=db.execute('SELECT body,review FROM labels WHERE request_id=?',(request_id,)).fetchall()
                if any(row[1] is not None for row in rows):raise ValueError('reviewed_label_exists')
            if len(rows)==len(sample['candidates']):
                from .teacher import validate_response
                validate_response(canonical({'labels':[json.loads(r[0]) for r in rows]}).encode(),sample)
                if lease.cancelled() or (external_cancel is not None and external_cancel()):raise ValueError('cancelled')
                result={'status':'completed','cli_invocations':0,'stored_labels':len(rows),
                        'label_status':'provisional','cached':True}
            else:
                from .transport import label_request
                result=label_request(state_dir,request_id,enabled=True,host=host,
                    cancel=lambda:lease.cancelled() or (external_cancel is not None and external_cancel()),**options)
            lease.result_status='cancelled' if result.get('reason')=='cancelled' else result['status']
            return result
    except ValueError as exc:
        if result is not None:
            return dict(result,job_metadata_status='unavailable')
        safe={'invalid_request_id','invalid_lease','unknown_or_deleted_request','unsafe_job_lock','job_busy','host_mismatch','reviewed_label_exists','cancelled','invalid_cancellation'}
        return {'status':'failed','reason':str(exc) if str(exc) in safe else 'job_rejected','cli_invocations':0}
    except Exception:
        if result is not None:
            return dict(result,job_metadata_status='unavailable')
        return {'status':'failed','reason':'job_unavailable','cli_invocations':0}

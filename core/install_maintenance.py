"""Bounded maintenance retry. No lock deletion, process signals, or direct ACK.

Queue provenance is checked under its stable sidecar lease. Native worker owns
the processing lease and ACK; rc0 alone never proves a completed claim.
"""
import fcntl,os,random,stat,time,subprocess
from pathlib import Path
import dirty_queue_claim as claims

class QueueBusy(Exception):pass
class WorkerFailure(RuntimeError):pass
class UnstableSourceSnapshot(Exception):pass

def _owned_rows(raw,expected):
 for line in raw.splitlines(keepends=True):
  if not line.endswith(b'\n'):raise ValueError('queue_owner_line_invalid')
  fields=line[:-1].decode('utf8').split('\t')
  if len(fields)!=3 or not fields[0] or tuple(fields[1:])!=expected.get(fields[0]):
   raise ValueError('queue_owner_unapproved')

def snapshot(queue,expected):
 queue=Path(queue);q,lock,claim_path=claims._paths(queue)
 if not q.parent.is_dir() or q.parent.is_symlink():raise ValueError('unsafe_dirty_queue_parent')
 fd=os.open(lock,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
 try:
  claims._private_regular(lock)
  try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
  except BlockingIOError:raise QueueBusy('queue_sidecar_busy') from None
  if q.exists():claims._private_regular(q)
  raw=q.read_bytes() if q.exists() else b''
  if len(raw)>1048576:raise ValueError('queue_too_large')
  _owned_rows(raw,expected)
  claim=claims._read_claim(claim_path)
  if claim:
   value,data=claim;_owned_rows(data,expected)
   if not q.exists():raise ValueError('dirty_queue_claim_changed')
   if value['phase']=='processing' and (q.stat().st_ino!=value['inode'] or not raw.startswith(data)):
    raise ValueError('dirty_queue_claim_prefix_changed')
   if value['phase']=='done' and q.stat().st_ino==value['inode'] and not raw.startswith(data):
    raise ValueError('dirty_queue_claim_prefix_changed')
  return {'pending':bool(raw or claim),'queueLines':len(raw.splitlines()),
          'claimPhase':claim[0]['phase'] if claim else None,'claimSha256':claim[0]['sha256'] if claim else None}
 finally:os.close(fd)

def drain(queue,expected,*,run,lease,deadline,clock=time.monotonic,sleep=time.sleep,jitter=random.uniform,event=lambda *_:None):
 attempts=0;waits=0;reason='deadline';last=None
 while clock()<deadline:
  try:
   last=snapshot(queue,expected)
   holder=lease()  # Unknown/denied identity is a failure, never a dead writer.
   if holder:reason='live_'+holder['busy'];event('waiting',holder)
   elif not last['pending']:
    return {'status':'drained','exitCode':0,'attempts':attempts,'waits':waits,'queue':last}
   else:
    # A holder can win the race after this check. Native single-flight and
    # post-run queue verification, including rc0, close that boundary.
    attempts+=1;event('worker',{'attempt':attempts,'claim':last})
    rc=run(deadline)
    if rc is None:reason='worker_still_running'
    elif rc not in (0,75):raise WorkerFailure('worker_rc_'+str(rc))
    else:reason='worker_busy' if rc==75 else 'unacked_or_concurrent_work'
    last=snapshot(queue,expected)
    if rc==0 and not last['pending'] and not lease():
     return {'status':'drained','exitCode':0,'attempts':attempts,'waits':waits,'queue':last}
  except QueueBusy:reason='queue_sidecar_busy'
  remaining=deadline-clock()
  if remaining<=0:break
  pause=min(remaining,15.,(2.**min(waits,4))*jitter(.8,1.2))
  waits+=1;event('deferred-wait',{'reason':reason,'seconds':pause});sleep(pause)
 return {'status':'deferred','exitCode':75,'reason':reason,'attempts':attempts,'waits':waits,'queue':last}

def stable(snapshot_sources,drain_once,requeue,verify,*,passes=3,deadline=float('inf'),clock=time.monotonic,sleep=time.sleep):
 for number in range(passes):
  if clock()>=deadline:break
  try:before=snapshot_sources()
  except UnstableSourceSnapshot:
   if number+1<passes:sleep(min(1.,max(0.,deadline-clock())))
   continue
  result=drain_once()
  if isinstance(result,dict) and result.get('status')=='deferred':return result
  verified=verify()
  if isinstance(verified,dict) and verified.get('status')=='deferred':return verified
  try:unchanged=before==snapshot_sources()
  except UnstableSourceSnapshot:unchanged=False
  if unchanged:return {'status':'stable','exitCode':0,'passes':number+1,'drain':result}
  # Retain changed approved work even when the bounded retry is exhausted.
  if clock()<deadline:requeue()
 return {'status':'deferred','exitCode':75,'reason':'sources_changed_during_resume','passes':passes}

def locks_status(paths,*,alive):
 """Inspect markers without reclaiming; release/construction races wait once."""
 for kind,path in paths:
  try:row=_lock_status(kind,path,alive)
  except FileNotFoundError:return {'busy':kind,'pid':None}
  if row:return row
 return None

def _lock_status(kind,path,alive):
 path=Path(path)
 if not path.exists() and not path.is_symlink():return None
 info=path.lstat()
 if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o022:
  raise ValueError('unsafe_worker_lock')
 entries={f.name for f in path.iterdir()}
 if not entries:return {'busy':kind,'pid':None}  # construction window
 if entries!={'pid'}:raise ValueError('unsafe_worker_lock_contents')
 fd=os.open(path/'pid',os.O_RDONLY|os.O_NOFOLLOW)
 try:
  row=os.fstat(fd)
  if not stat.S_ISREG(row.st_mode) or row.st_uid!=os.getuid() or row.st_nlink!=1 or row.st_mode&0o022 or row.st_size>24:
   raise ValueError('unsafe_worker_lock_pid')
  raw=os.read(fd,25).decode().strip()
 finally:os.close(fd)
 if not raw.isdigit() or int(raw)<=1:raise ValueError('unsafe_worker_lock_pid')
 if alive(int(raw)) is not False:return {'busy':kind,'pid':int(raw)}
 return None

def run_process(args,*,cwd,env,output,deadline,clock=time.monotonic,sleep=time.sleep):
 """Do not kill an index writer when the controller deadline expires."""
 fd=os.open(output,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
 with os.fdopen(fd,'wb') as log:
  child=subprocess.Popen(args,cwd=cwd,env=env,stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,close_fds=True,start_new_session=True)
  while child.poll() is None and clock()<deadline:
   sleep(min(.2,max(0.,deadline-clock())))
  return {'returncode':child.poll(),'pid':child.pid,'log':str(output)}

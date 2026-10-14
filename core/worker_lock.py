"""Only proven-dead, same-owner/inode pid markers are recoverable.
Permission denial, construction, live or replaced holders remain untouched.
"""
import fcntl,json,os,stat,sys,uuid
from contextlib import contextmanager
from pathlib import Path
from qmd_endpoint import alive as process_alive

def _directory(path):
 st=path.lstat()
 if not stat.S_ISDIR(st.st_mode) or st.st_uid!=os.getuid() or st.st_mode&0o022:raise ValueError('unsafe_worker_lock_directory')
 return st

def published(path,pidfd,dirfd):
 path=Path(path);directory=_directory(path);held=os.fstat(dirfd);marker=(path/'pid').lstat();opened=os.fstat(pidfd)
 if ((directory.st_dev,directory.st_ino)!=(held.st_dev,held.st_ino)
     or (marker.st_dev,marker.st_ino)!=(opened.st_dev,opened.st_ino)):
  return False
 if not stat.S_ISREG(marker.st_mode) or marker.st_uid!=os.getuid() or marker.st_nlink!=1 or marker.st_mode&0o022:raise ValueError('unsafe_worker_publication')
 raw=(path/'pid').read_text().strip()
 return raw.isdigit() and int(raw)>1

@contextmanager
def _lease(path):
 path=Path(path)
 if not path.is_absolute():raise ValueError('absolute_worker_lock_required')
 _directory(path.parent)
 fd=os.open(str(path)+'.reclaim.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
 try:
  st=os.fstat(fd)
  if not stat.S_ISREG(st.st_mode) or st.st_uid!=os.getuid() or st.st_nlink!=1 or st.st_mode&0o077:raise ValueError('unsafe_worker_reclaim_lease')
  fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
  yield
 finally:os.close(fd)

def release(path,pidfd,dirfd,rekick=False):
 with _lease(path):return _release_locked(path,pidfd,dirfd,rekick)

def _release_locked(path,pidfd,dirfd,rekick=False):
 # A late publisher writes to its pre-opened marker, never a new pathname.
 # Only those retained directory/marker inodes authorize child cleanup.
 path=Path(path);directory=_directory(path);held=os.fstat(dirfd);marker=(path/'pid').lstat();opened=os.fstat(pidfd)
 if ((directory.st_dev,directory.st_ino)!=(held.st_dev,held.st_ino)
     or (marker.st_dev,marker.st_ino)!=(opened.st_dev,opened.st_ino)):return False
 if not stat.S_ISREG(marker.st_mode) or marker.st_uid!=os.getuid() or marker.st_nlink!=1 or marker.st_mode&0o022:raise ValueError('unsafe_worker_publication')
 allowed={'pid','rekick'} if rekick else {'pid'}
 if {p.name for p in path.iterdir()}-allowed:raise ValueError('unsafe_worker_release_contents')
 extra=path/'rekick'
 if extra.exists() or extra.is_symlink():
  st=extra.lstat()
  if not rekick or not stat.S_ISREG(st.st_mode) or st.st_uid!=os.getuid() or st.st_nlink!=1 or st.st_mode&0o022:raise ValueError('unsafe_worker_rekick')
 # Move the owned directory before removing its marker. A read-only parent
 # then preserves the PID instead of leaving an ownerless construction lock.
 retired=path.with_name(path.name+'.released-'+uuid.uuid4().hex)
 path.rename(retired)
 moved=_directory(retired)
 if (moved.st_dev,moved.st_ino)!=(held.st_dev,held.st_ino):return False
 if rekick:(retired/'rekick').unlink(missing_ok=True)
 (retired/'pid').unlink();retired.rmdir();return True

def release_pid(path,pid):
 path=Path(path);dirfd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW);pidfd=None
 try:
  pidfd=os.open(path/'pid',os.O_RDONLY|os.O_NOFOLLOW)
  if os.read(pidfd,25).decode().strip()!=str(pid):return False
  return release(path,pidfd,dirfd)
 finally:
  if pidfd is not None:os.close(pidfd)
  os.close(dirfd)

def reclaim(path,*,alive=process_alive):
 path=Path(path)
 if not path.is_absolute():raise ValueError('absolute_worker_lock_required')
 _directory(path.parent)
 fd=os.open(str(path)+'.reclaim.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
 try:
  st=os.fstat(fd)
  if not stat.S_ISREG(st.st_mode) or st.st_uid!=os.getuid() or st.st_nlink!=1 or st.st_mode&0o077:raise ValueError('unsafe_worker_reclaim_lease')
  try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
  except BlockingIOError:return {'status':'busy','reason':'reclaim_lease_busy'}
  try:identity=_directory(path)
  except FileNotFoundError:return {'status':'absent'}
  names={p.name for p in path.iterdir()}
  if not names:return {'status':'busy','reason':'construction_window'}
  if names!={'pid'}:raise ValueError('unsafe_worker_lock_contents')
  pidfile=path/'pid';pidfd=os.open(pidfile,os.O_RDONLY|os.O_NOFOLLOW)
  try:
   pidinfo=os.fstat(pidfd)
   if not stat.S_ISREG(pidinfo.st_mode) or pidinfo.st_uid!=os.getuid() or pidinfo.st_nlink!=1 or pidinfo.st_size>24 or pidinfo.st_mode&0o022:raise ValueError('unsafe_worker_lock_pid')
   raw=os.read(pidfd,25)
   token=raw.decode().strip()
   if not token.isdigit() or int(token)<=1:return {'status':'busy','reason':'unknown_holder'}
   try:running=alive(int(token))
   except (PermissionError,ValueError):return {'status':'busy','reason':'inspection_denied'}
   if running is not False:return {'status':'busy','reason':'live_or_unknown_holder'}
   current=_directory(path);marker=pidfile.lstat()
   if ((current.st_dev,current.st_ino)!=(identity.st_dev,identity.st_ino)
       or (marker.st_dev,marker.st_ino)!=(pidinfo.st_dev,pidinfo.st_ino)
       or os.fstat(pidfd).st_nlink!=1 or pidfile.read_bytes()!=raw):return {'status':'busy','reason':'holder_replaced'}
   dirfd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
   try:
    current=os.fstat(dirfd)
    if (current.st_dev,current.st_ino)!=(identity.st_dev,identity.st_ino) or not _release_locked(path,pidfd,dirfd):
     return {'status':'busy','reason':'holder_replaced'}
   finally:os.close(dirfd)
   parent=os.open(path.parent,os.O_RDONLY)
   try:os.fsync(parent)
   finally:os.close(parent)
   return {'status':'reclaimed'}
  finally:os.close(pidfd)
 except FileNotFoundError:return {'status':'busy','reason':'holder_transition'}
 finally:os.close(fd)

def main(args):
 if len(args)==3 and args[0]=='release-pid':
  try:ok=release_pid(args[1],int(args[2]))
  except (OSError,ValueError):return 75
  return 0 if ok else 75
 if len(args)==4 and args[0] in ('published','release','release-rekick'):
  try:
   if args[0]=='published':ok=published(args[1],int(args[2]),int(args[3]))
   else:ok=release(args[1],int(args[2]),int(args[3]),args[0]=='release-rekick')
  except (OSError,ValueError):return 75
  return 0 if ok else 75
 if len(args)!=2 or args[0]!='reclaim':return 2
 try:row=reclaim(args[1])
 except (OSError,ValueError) as e:print(json.dumps({'status':'unsafe','reason':str(e)}));return 1
 print(json.dumps(row));return 0 if row['status'] in ('reclaimed','absent') else 75
if __name__=='__main__':raise SystemExit(main(sys.argv[1:]))

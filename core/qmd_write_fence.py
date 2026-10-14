"""Bounded reservation of the same writer/embed locks used by QMD workers.

Never signals a holder. Unknown/denied/live holders are retained; only an
exact dead-holder pid-only lock may be reclaimed. Lock order matches workers.
"""
from contextlib import contextmanager
import getpass,os,stat,time,math
from pathlib import Path
from qmd_endpoint import alive
import install_deadline

def paths():
    base=Path(os.environ.get('QMD_LOCK_BASE') or Path(os.environ.get('TMPDIR') or '/tmp')/('qmd-auto-context-locks-'+getpass.getuser()))
    return [Path(os.environ.get('QMD_WRITER_LOCKDIR') or base/'qmd-update.lock.d'),
            Path(os.environ.get('QMD_EMBED_LOCKDIR') or base/'qmd-embed.lock.d')]

def safe_dir(path):
    info=path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o022:
        raise ValueError('unsafe_qmd_writer_fence')

def dead_lock(path):
    import worker_lock
    row=worker_lock.reclaim(path,alive=alive)
    if row.get('reason')=='inspection_denied':raise ValueError('qmd_endpoint_identity_unavailable')
    return row['status'] in ('reclaimed','absent')

@contextmanager
def hold(seconds=30):
    if not isinstance(seconds,(int,float)) or not math.isfinite(seconds) or not 0<=seconds<=120:raise ValueError('invalid_qmd_writer_wait')
    alive(os.getpid())  # EPERM is a capability denial, never a dead writer.
    held=[];identities={};deadline=install_deadline.wait_deadline(seconds)
    try:
        for path in paths():
            path.parent.mkdir(mode=0o700,parents=True,exist_ok=True);safe_dir(path.parent)
            while True:
                try:path.mkdir(mode=0o700)
                except FileExistsError:
                    try:
                        if dead_lock(path):continue
                    except FileNotFoundError:continue
                    remaining=deadline-time.monotonic()
                    if remaining<=0:raise TimeoutError('qmd_writer_fence_busy')
                    time.sleep(min(.05,remaining));continue
                held.append(path)
                identities[path]=path.stat().st_ino
                fd=os.open(path/'pid',os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
                with os.fdopen(fd,'w') as output:output.write(str(os.getpid()));output.flush();os.fsync(output.fileno())
                break
        def verify():
            for path in held:
                safe_dir(path)
                if path.stat().st_ino!=identities[path] or (path/'pid').read_text().strip()!=str(os.getpid()):
                    raise ValueError('qmd_writer_fence_lost')
        verify();yield verify
    finally:
        for path in reversed(held):
            try:
                if path.stat().st_ino==identities.get(path) and not any(path.iterdir()):
                    path.rmdir();continue
                if path.stat().st_ino==identities.get(path) and (path/'pid').read_text().strip()==str(os.getpid()):
                    (path/'pid').unlink();path.rmdir()
            except FileNotFoundError:pass

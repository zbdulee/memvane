"""Share update.sh/index_worker.sh's writer lock for scoped direct QMD writes."""
from contextlib import contextmanager
import errno
import os
from pathlib import Path
import pwd
import stat
import tempfile
import time


def _lock_path():
    supplied = os.environ.get('QMD_WRITER_LOCKDIR')
    if supplied:
        return Path(supplied)
    base = os.environ.get('QMD_LOCK_BASE')
    if not base:
        base = str(Path(os.environ.get('TMPDIR') or tempfile.gettempdir()) /
                   ('qmd-auto-context-locks-' + pwd.getpwuid(os.getuid()).pw_name))
    return Path(base) / 'qmd-update.lock.d'


def _reclaim_dead(path):
    """Match the shell writer's dead-owner recovery without stealing a live lock."""
    if path.is_symlink() or not path.is_dir():
        return False
    info = path.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o022:
        return False
    pid_path = path / 'pid'
    if not pid_path.exists():
        # The owner may be between mkdir and pid creation.
        time.sleep(.2)
    if pid_path.is_symlink():
        return False
    try:
        pid_info = pid_path.stat()
        if (not stat.S_ISREG(pid_info.st_mode) or pid_info.st_uid != os.getuid()
                or pid_info.st_mode & 0o022 or pid_info.st_size > 32):
            return False
        holder = pid_path.read_text().strip()
        if not holder.isdecimal() or int(holder) <= 0:
            return False
        try:
            os.kill(int(holder), 0)
            return False
        except OSError as exc:
            if exc.errno != errno.ESRCH:
                return False
        if path.stat().st_ino != info.st_ino or pid_path.stat().st_ino != pid_info.st_ino:
            return False
        pid_path.unlink()
    except FileNotFoundError:
        if path.stat().st_ino != info.st_ino:
            return False
    try:
        path.rmdir()
        return True
    except OSError:
        return False


@contextmanager
def acquire():
    path = _lock_path()
    parent = path.parent
    if not parent.is_absolute() or parent.is_symlink():
        raise ValueError('qmd_writer_lock_unsafe')
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = parent.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise ValueError('qmd_writer_lock_unsafe')
    try:
        path.mkdir(mode=0o700)
    except FileExistsError as exc:
        if not _reclaim_dead(path):
            raise ValueError('qmd_writer_busy') from exc
        try:
            path.mkdir(mode=0o700)
        except FileExistsError as again:
            raise ValueError('qmd_writer_busy') from again
    inode = path.stat().st_ino
    try:
        pid = path / 'pid'
        fd = os.open(pid, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as out:
            out.write(str(os.getpid()) + '\n')
        yield
    finally:
        if path.is_dir() and not path.is_symlink() and path.stat().st_ino == inode:
            (path / 'pid').unlink(missing_ok=True)
            path.rmdir()

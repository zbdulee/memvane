"""Bounded child-only rollback preimage inspection; never opens source SQLite."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import sqlite3
import stat
import sys

LIMIT = 16 * 1024 * 1024
NAMES = ('probe.sqlite', 'probe.sqlite-journal', 'probe.sqlite-wal', 'probe.sqlite-shm')


def need(value, code):
    if not value:
        raise ValueError(code)


def private(info, directory=False):
    need((stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
         and info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == (0o700 if directory else 0o600)
         and (directory or info.st_nlink == 1), 'legacy_probe_unsafe')


def signature(info):
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def pair(path):
    need(path.is_absolute() and path.resolve(strict=True) == path, 'legacy_probe_path_invalid')
    private(path.parent.lstat(), True)
    need(not os.path.lexists(path.parent/'.reader-initial-v5'), 'legacy_probe_initial_intent')
    fds = []
    try:
        for name in (path, Path(str(path)+'-journal')):
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            fds.append(fd)
            private(os.fstat(fd))
        before = [os.fstat(fd) for fd in fds]
        need(sum(info.st_size for info in before) <= LIMIT, 'legacy_probe_too_large')
        bodies = [os.pread(fd, info.st_size+1, 0) for fd, info in zip(fds, before)]
        need(all(len(body) == info.st_size for body, info in zip(bodies, before)), 'legacy_probe_changed')
        for fd, info, name in zip(fds, before, (path, Path(str(path)+'-journal'))):
            need(signature(os.fstat(fd)) == signature(info)
                 and os.path.samestat(info, name.lstat()), 'legacy_probe_changed')
        receipt = [dict(node=signature(info), sha256=hashlib.sha256(body).hexdigest())
                   for info, body in zip(before, bodies)]
        return bodies, receipt
    finally:
        for fd in reversed(fds):
            os.close(fd)


def clean(directory):
    need(set(os.listdir(directory)) <= {'.owner.json', *NAMES}, 'legacy_probe_unknown_scratch')
    for name in NAMES:
        path = directory/name
        if os.path.lexists(path):
            private(path.lstat())
            path.unlink()


def probe(path, checkpoint=False):
    # This fresh process closes source FDs before creating any SQLite handle.
    bodies, receipt = pair(path)
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from laya_package import sync_owner_directory
    root = sync_owner_directory(create=True)
    lock = os.open(root/'legacy-journal-probe.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        private(os.fstat(lock))
        need(os.path.samestat(os.fstat(lock), (root/'legacy-journal-probe.lock').lstat()), 'legacy_probe_lock_changed')
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        directory = root/'legacy-journal-probe'
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            pass
        private(directory.lstat(), True)
        info = directory.lstat()
        expected = dict(schema='memvane-legacy-journal-probe-v1', uid=os.getuid(), dev=info.st_dev, ino=info.st_ino)
        owner = directory/'.owner.json'
        if not os.path.lexists(owner):
            need(not os.listdir(directory), 'legacy_probe_unowned_scratch')
            fd = os.open(owner, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            try:
                raw = json.dumps(expected, sort_keys=True).encode()
                need(os.write(fd, raw) == len(raw), 'legacy_probe_owner_write')
                os.fsync(fd)
            finally:
                os.close(fd)
        private(owner.lstat())
        need(owner.stat().st_size <= 1024 and json.loads(owner.read_text()) == expected,
             'legacy_probe_unowned_scratch')
        clean(directory)
        try:
            for name, body in zip(NAMES, bodies):
                fd = os.open(directory/name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                try:
                    need(os.write(fd, body) == len(body), 'legacy_probe_short_copy')
                finally:
                    os.close(fd)
            if checkpoint:
                need(os.environ.get('QMD_SETUP_GUARD_FIXTURE') == '1', 'legacy_probe_checkpoint_denied')
                print(json.dumps(dict(stage='private-copy-ready', scratch=str(directory))), flush=True)
                need(sys.stdin.readline() == 'continue\n', 'legacy_probe_checkpoint_interrupted')
            import laya_install_journal as J
            db = sqlite3.connect((directory/NAMES[0]).as_uri()+'?mode=rw', uri=True, timeout=.5)
            try:
                need(J._schema_version(db) in (3, 4), 'legacy_probe_schema_invalid')
                row = J._row(db)
            finally:
                db.close()
            _bodies, after = pair(path)
            need(after == receipt, 'legacy_probe_changed')
            return dict(row=row, receipt=receipt)
        finally:
            clean(directory)
    finally:
        os.close(lock)


def main():
    signal.alarm(5)
    mode, path = sys.argv[1], Path(sys.argv[2])
    if mode == 'check':
        expected = json.loads(sys.stdin.read(8193))
        need(pair(path)[1] == expected, 'legacy_probe_changed')
        result = dict(checked=True)
    else:
        need(mode == 'probe', 'legacy_probe_mode_invalid')
        result = probe(path, '--checkpoint-after-copy' in sys.argv[3:])
    print(json.dumps(result, sort_keys=True), flush=True)


if __name__ == '__main__':
    main()

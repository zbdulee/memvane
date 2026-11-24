"""Locate the native edit ledger without exposing a recoverable job to released workers."""
import os
import stat
from tempfile import NamedTemporaryFile

from memvane.kernel.atomic_rename import canonical_json_bytes, fsync_dir, private_state


MAX_BYTES = 8 * 1024 * 1024


def private_path(public, *, create=False):
    folder = public.parent
    if create:
        folder.mkdir(parents=True, exist_ok=True)
    for part in ('.handoff', 'v1', 'refresh'):
        parent, folder = folder, folder / part
        if create:
            try:
                folder.mkdir(mode=0o700)
            except FileExistsError:
                pass
            fsync_dir(parent)
        elif not os.path.lexists(folder):
            return None
        private_state(folder)
    path = folder / 'events.jsonl'
    if os.path.lexists(path):
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
                info.st_nlink != 1 or info.st_size > MAX_BYTES):
            raise OSError('unsafe native refresh ledger')
    return path


def paths(public):
    if public is None:
        raise ValueError('unsafe public refresh ledger')
    private = private_path(public)
    return (public, private) if private is not None else (public,)


def append_private(path, event):
    """Leave the old event sequence intact if a write runs out of space."""
    try:
        with path.open('rb') as stream:
            before = stream.read(MAX_BYTES + 1)
    except FileNotFoundError:
        before = b''
    raw = canonical_json_bytes(event) + b'\n'
    if len(before) + len(raw) > MAX_BYTES or (before and not before.endswith(b'\n')):
        return False
    with NamedTemporaryFile(dir=path.parent, prefix='.edit-', delete=False) as stream:
        tmp = stream.name
        try:
            stream.write(before)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
            os.replace(tmp, path)
            fsync_dir(path.parent)
        finally:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
    return True

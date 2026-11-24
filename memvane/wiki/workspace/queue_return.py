"""Durable claim returns. All mutations run under the existing live queue lock."""
from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import uuid
from contextlib import contextmanager, nullcontext

from memvane.kernel.atomic_rename import canonical_json_bytes, fsync_dir, rename_noreplace, write_new_file


class ClaimRows(list):
    """Rows plus the exact file prefix the worker was offered."""

    def __init__(self, path):
        super().__init__()
        raw, info = _read_file(path)
        self.inode = info.st_ino
        self.size = len(raw)
        self.digest = hashlib.sha256(raw).hexdigest()
        self.line_ranges = []
        start = 0
        for line in raw.split(b'\n')[:-1]:
            length = len(line) + 1
            offset, start = start, start + length
            try:
                text = line.decode('utf-8')
            except UnicodeError:
                continue  # Exact undecodable bytes remain private at return.
            if not text.strip():
                continue
            try:
                job = json.loads(text)
            except (ValueError, RecursionError):
                job = None
            self.line_ranges.append((offset, length))
            self.append((text, job if isinstance(job, dict) else None))


    def finish(self, queue, claim, remaining):
        with queue_locked(queue):
            finish_locked(queue, claim, remaining, self)

    def privatize(self, queue, claim):
        return _privatize_claim(queue, claim, self)

    def finish_verified(self, queue, claim, remaining, cleanup):
        return _finish_verified(queue, claim, self, remaining, cleanup)


def _read_file(path, *, sync_private=False):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as src:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or not os.path.samestat(info, path.lstat())):
            raise OSError('unsafe queue file')
        if sync_private:
            if info.st_nlink != 1:
                raise OSError('unsafe private claim')
            if stat.S_IMODE(info.st_mode) != 0o600:
                os.fchmod(fd, 0o600)
            os.fsync(fd)
        return src.read(), info


def _raw(path):
    return _read_file(path)[0]


def _dir(queue, create=False):
    path = queue.parent
    for name in ('.handoff', 'v1', 'returns', hashlib.sha256(os.fsencode(queue.name)).hexdigest()[:24]):
        parent, path = path, path / name
        if create:
            try:
                path.mkdir(mode=0o700)
            except FileExistsError:
                pass
            # A previous creator may have stopped before syncing this name.
            fsync_dir(parent)
        try:
            info = path.lstat()
        except FileNotFoundError:
            return None
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise OSError('unsafe private queue directory')
    return path


def _token():
    return uuid.uuid4().hex


def _sync_move(src, dst):
    os.replace(src, dst)
    fsync_dir(dst.parent)
    if src.parent != dst.parent:
        fsync_dir(src.parent)


@contextmanager
def _opened(name, flags, *, dir_fd=None):
    fd = os.open(name, flags, dir_fd=dir_fd)
    try:
        yield fd
    finally:
        os.close(fd)


def _move_claim(src, dst, *, private=True):
    directory = os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY
    with _opened(src.parent, directory) as source_parent, _opened(dst.parent, directory) as target_parent:
        with _opened(src.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=source_parent) as fd:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid()
                    or not os.path.samestat(info, src.lstat())):
                raise OSError('unsafe claim to move')
            rename_noreplace(source_parent, src.name, dst.name, target_parent_fd=target_parent)
            if private:
                os.fchmod(fd, 0o600)
            os.fsync(fd)
            os.fsync(target_parent)
            if src.parent != dst.parent:
                os.fsync(source_parent)


def _exact_file(path, data):
    try:
        write_new_file(path, data)
    except FileExistsError:
        if _raw(path) != data:
            raise OSError('private queue image differs')
        fsync_dir(path.parent)


def _write_state(folder, state):
    temp = folder / ('tmp-' + _token())
    image = dict(state, checksum=hashlib.sha256(canonical_json_bytes(state)).hexdigest())
    write_new_file(temp, canonical_json_bytes(image))
    _sync_move(temp, folder / 'work')


def _encoded(raw):
    return base64.b64encode(raw).decode('ascii')


def _decoded(text):
    return base64.b64decode(text, validate=True)


def _snapshot(path, rows):
    raw = _raw(path)
    if (not isinstance(rows, ClaimRows) or path.lstat().st_ino != rows.inode or len(raw) < rows.size
            or hashlib.sha256(raw[:rows.size]).hexdigest() != rows.digest):
        raise OSError('queue claim prefix changed')
    return raw


def _split_tail(raw, size):
    """Only complete UTF-8 lines enter a released queue; preserve all other bytes."""
    boundary = raw.rfind(b'\n', 0, size) + 1
    public, retained = [], []
    for line in raw.split(b'\n')[:-1]:
        try:
            line.decode('utf-8')
        except UnicodeError:
            retained.append(line + b'\n')
    for line in raw[boundary:].split(b'\n')[:-1]:
        try:
            line.decode('utf-8')
        except UnicodeError:
            continue
        public.append(line + b'\n')
    retained.append(raw[raw.rfind(b'\n') + 1:])
    return b''.join(public), b''.join(retained)


def lines_bytes(lines):
    return b''.join((s if s.endswith('\n') else s + '\n').encode('utf-8') for s in lines)


def _load(folder, queue):
    state = json.loads(_raw(folder / 'work'))
    checksum = state.pop('checksum', None)
    if checksum != hashlib.sha256(canonical_json_bytes(state)).hexdigest():
        raise OSError('invalid claim return checksum')
    if (state.get('version') != 1 or state.get('queue') != queue.name
            or not re.fullmatch('[a-f0-9]{32}', state.get('id', ''))
            or not re.fullmatch('[a-f0-9]{32}', state.get('swap', ''))
            or Path(state['claim']).name != state['claim']
            or '.claimed.' not in state['claim']
            or state['phase'] not in ('return', 'prepared', 'published')):
        raise OSError('unknown claim return')
    return state


def ensure_live(queue):
    try:
        fd = os.open(queue, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        if not stat.S_ISREG(queue.lstat().st_mode):
            raise OSError('unsafe live queue')
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    fsync_dir(queue.parent)


def _live_bytes(queue):
    try:
        return _raw(queue)
    except FileNotFoundError:
        return b''


def resume(queue):
    folder = _dir(queue)
    if folder is None or not os.path.lexists(folder / 'work'):
        return
    state = _load(folder, queue)
    nonce = state['id']
    claim = (folder if state['private'] else queue.parent) / state['claim']
    raw_path = folder / ('raw-' + nonce)
    if os.path.lexists(raw_path) and os.path.lexists(claim):
        raise OSError('conflicting claim return names')
    if not os.path.lexists(raw_path) and state['phase'] != 'published':
        if not os.path.lexists(claim):
            # A released owner already recovered the still-public claim.
            (folder / 'work').unlink()
            fsync_dir(folder)
            return
        raw = _raw(claim)
        if (claim.lstat().st_ino != state['inode'] or len(raw) < state['length']
                or hashlib.sha256(raw[:state['length']]).hexdigest() != state['digest']):
            raise OSError('queue return source changed')
        _move_claim(claim, raw_path)
    if state['phase'] != 'published':
        # A stop during the move can precede either parent fsync.
        fsync_dir(folder)
        fsync_dir(claim.parent)
        original, info = _read_file(raw_path, sync_private=True)
        size = state['length']
        if (info.st_ino != state['inode'] or len(original) < size
                or hashlib.sha256(original[:size]).hexdigest() != state['digest']):
            raise OSError('queue return source changed')
        tail, torn = _split_tail(original, state['read'])
        publish = _decoded(state['return'])
        hold = _decoded(state['hold'])
        if state['private']:
            hold += publish + tail
            publish = b''
        else:
            publish += tail
        if hold:
            _exact_file(folder / ('ready-' + nonce), hold)
        if torn:
            _exact_file(folder / ('fragment-' + nonce), torn)
        if publish:
            prepared = state['phase'] == 'prepared'
            swap = queue.parent / ('.qr-' + state['swap'])
            if not prepared or os.path.lexists(swap):
                current = _live_bytes(queue)
                # Keep a torn live suffix separately before adding complete rows.
                cut = current.rfind(b'\n') + 1
                if cut != len(current):
                    suffix = current[cut:]
                    _exact_file(folder / ('fragment-' + nonce + '-' + hashlib.sha256(suffix).hexdigest()), suffix)
                image = current[:cut] + publish
                if prepared and _raw(swap) != image:
                    # The reserved witness remains named throughout the rebuild.
                    # Only its final move onto live consumes this name.
                    temp = queue.parent / ('.qr-' + _token())
                    write_new_file(temp, image)
                    _sync_move(temp, swap)
                if not prepared:
                    # Before the prepared record, even an identical existing
                    # name lacks publication authority. Never consume it.
                    state['swap'] = _token()
                    swap = queue.parent / ('.qr-' + state['swap'])
                    write_new_file(swap, image)
                    state['phase'] = 'prepared'
                    _write_state(folder, state)
                _sync_move(swap, queue)
            fsync_dir(queue.parent)
        state['phase'] = 'published'
        _write_state(folder, state)
    if os.path.lexists(raw_path):
        if raw_path.lstat().st_ino != state['inode']:
            raise OSError('queue raw identity changed')
        raw_path.unlink()
    fsync_dir(folder)
    ensure_live(queue)
    (folder / 'work').unlink()
    fsync_dir(folder)


def finish_locked(queue, claim, remaining, rows, *, held=b''):
    resume(queue)
    original = _snapshot(claim, rows)
    tail, torn = _split_tail(original, rows.size)
    if not remaining and not held and not tail and not torn:
        claim.unlink()
        fsync_dir(claim.parent)
        ensure_live(queue)
        return
    folder = _dir(queue, create=True)
    state = dict(version=1, queue=queue.name, id=_token(), phase='return', swap=_token(),
                 claim=claim.name, private=claim.parent == folder, inode=rows.inode,
                 length=len(original), digest=hashlib.sha256(original).hexdigest(), read=rows.size,
                 hold=_encoded(held))
    state['return'] = _encoded(lines_bytes(remaining))
    _write_state(folder, state)
    resume(queue)


def legacy_card_lock(queue):
    from memvane.wiki.workspace import compile_paths as cp
    from memvane.settings import types as project_types
    if queue.name == cp.VERIFY_QUEUE and queue.parent.parent.name in project_types.NAMES:
        return cp.card_write_lock(queue.parent.parent.parent)
    return nullcontext()


@contextmanager
def queue_locked(queue):
    from memvane.wiki.workspace.queue_paths import _queue_lock_path
    with _queue_lock_path(queue).open('a') as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield


def _privatize_claim(queue, claim, rows):
    with queue_locked(queue):
        _snapshot(claim, rows)
        folder = _dir(queue, create=True)
        if claim.parent == folder:
            if not re.fullmatch(r'native\.claimed\.%d\.[a-f0-9]{32}' % os.getpid(), claim.name):
                raise OSError('not own private claim')
            return claim
        if claim.parent != queue.parent or not claim.name.startswith(queue.name + '.claimed.' + str(os.getpid()) + '.'):
            raise OSError('not own source claim')
        private = folder / ('native.claimed.%d.%s' % (os.getpid(), _token()))
        _move_claim(claim, private)
        return private


def _finish_verified(queue, claim, rows, remaining, cleanup):
    """Under the caller's card lock, check all remaining references before ACK."""
    with queue_locked(queue):
        resume(queue)
        original = _snapshot(claim, rows)
        tail, torn = _split_tail(original, rows.size)
        folder = _dir(queue)
        other = [p for p in queue.parent.glob(queue.name + '.claimed.*') if p != claim]
        if queue.exists():
            other.append(queue)
        if folder is not None:
            other.extend(p for p in folder.iterdir()
                         if p != claim and p.name.startswith(('native.claimed.', 'ready-', 'fragment-')))
        extra = cleanup(lines_bytes(remaining) + tail, other, bool(torn))
        finish_locked(queue, claim, list(remaining) + extra, rows)


def take_private(queue, alive):
    folder = _dir(queue)
    if folder is None:
        return None
    for child in sorted(folder.iterdir()):
        if re.fullmatch('ready-[a-f0-9]{32}', child.name):
            nonce = child.name[6:]
        else:
            match = re.fullmatch(r'native\.claimed\.(\d+)\.([a-f0-9]{32})', child.name)
            if not match or alive(int(match[1])):
                continue
            nonce = match[2]
        dest = folder / ('native.claimed.%d.%s' % (os.getpid(), nonce))
        _move_claim(child, dest)
        return dest
    return None


def queue_recovery_pending(queue):
    folder = _dir(queue)
    return folder is not None and any(p.name == 'work' or p.name.startswith(('ready-', 'native.claimed.'))
                                      for p in folder.iterdir())

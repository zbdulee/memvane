"""UID-wide sync owner and explicit borrowed ownership."""
from __future__ import annotations
from contextlib import ExitStack, contextmanager
import fcntl
import os
from pathlib import Path
import stat
import threading
import time
from laya_store_primitives import IndexContractError


def _file_identity(path: Path):
    try:
        info = path.lstat()
    except OSError as exc:
        raise IndexContractError('sync_owner_lock_unavailable') from exc
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_nlink != 1 or info.st_mode & 0o022):
        raise IndexContractError('sync_owner_lock_unavailable')
    return info.st_dev, info.st_ino


_sync_leases = {}
# Explicit close ownership: losing a Python reference must not release an
# exclusion still needed by an outstanding native DB handle.
_sync_pins = {}


class SyncLease:
    """An issued, lexical, same-thread sync owner; not a mover permission."""
    __slots__ = ()

    def __new__(cls):
        raise IndexContractError('sync_owner_not_issued')

    def __reduce__(self):
        raise TypeError('sync_owner_not_serializable')

    def pin(self):
        record = _lease_record(self)
        _check_sync_lease(record)
        fds = []
        try:
            for fd in (record['anchor'][2], record['fd']):
                fds.append(os.dup(fd))
            _check_sync_lease(record)
            pin = object.__new__(_SyncPin)
            _sync_pins[pin] = (fds, record['pid'], record['thread'])
            return pin
        except BaseException:
            for fd in fds:
                os.close(fd)
            raise


class _SyncPin:
    """Retain kernel exclusion until explicit close, without owner authority."""
    __slots__ = ()

    def __new__(cls):
        raise IndexContractError('sync_pin_not_issued')

    def __reduce__(self):
        raise TypeError('sync_pin_not_serializable')

    def close(self):
        record = _sync_pins.get(self)
        if record is None:
            return
        fds, pid, thread = record
        if pid != os.getpid() or thread is not threading.current_thread():
            raise IndexContractError('sync_owner_not_issued')
        while fds:
            os.close(fds[-1])
            fds.pop()
        del _sync_pins[self]


def _lease_record(claim):
    if type(claim) is not SyncLease or claim not in _sync_leases:
        raise IndexContractError('sync_owner_not_issued')
    record = _sync_leases[claim]
    if record['pid'] != os.getpid() or record['thread'] is not threading.current_thread():
        raise IndexContractError('sync_owner_not_issued')
    return record


def _check_held_sync_file(path, fd, node, *, held=True):
    opened = os.fstat(fd)
    if (not stat.S_ISREG(opened.st_mode) or opened.st_uid != os.getuid()
            or opened.st_nlink != 1 or stat.S_IMODE(opened.st_mode) != 0o600
            or (opened.st_dev, opened.st_ino) != node
            or _file_identity(path) != node
            or stat.S_IMODE(path.lstat().st_mode) != 0o600):
        raise IndexContractError('sync_owner_lock_unavailable')
    if not held:
        return
    # Never reacquire a missing lock on its issuing open description.
    probe = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
                    | getattr(os, 'O_CLOEXEC', 0) | os.O_NONBLOCK)
    try:
        info = os.fstat(probe)
        if (info.st_dev, info.st_ino) != node:
            raise IndexContractError('sync_owner_lock_unavailable')
        try:
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pass
        else:
            raise IndexContractError('sync_owner_lock_unavailable')
    finally:
        os.close(probe)


def _check_sync_anchor(anchor):
    from laya_package import sync_owner_directory
    parent, parent_node, fd, node = anchor
    if (sync_owner_directory(create=False) != parent
            or _directory_node(parent) != parent_node):
        raise IndexContractError('sync_owner_lock_unavailable')
    _check_held_sync_file(parent / 'owner.lock', fd, node)


def _directory_node(path):
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077):
        raise IndexContractError('sync_owner_lock_unavailable')
    return info.st_dev, info.st_ino


@contextmanager
def _sync_anchor():
    """Stable UID exclusion, outside every movable project/state directory.

    All sync owners share it. Acquire inside provider admission, before the
    queue-adjacent legacy lock. Arbitrary same-UID modification of this private
    coordination namespace is not an issued-API security boundary.
    """
    from laya_package import sync_owner_directory
    fd = None
    try:
        try:
            parent = sync_owner_directory(create=True)
            parent_node = _directory_node(parent)
            path = parent / 'owner.lock'
            fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW
                         | os.O_CLOEXEC | os.O_NONBLOCK, 0o600)
            info = os.fstat(fd)
            node = (info.st_dev, info.st_ino)
            _check_held_sync_file(path, fd, node, held=False)
            acquired = True
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                acquired = False
            anchor = (parent, parent_node, fd, node)
            if acquired:
                _check_sync_anchor(anchor)
        except OSError as exc:
            raise IndexContractError('sync_owner_lock_unavailable') from exc
        if not acquired:
            yield None
            return
        # Preserve the caller's native exception; only our filesystem errors
        # describe unavailable owner evidence.
        yield anchor
        try:
            _check_sync_anchor(anchor)
        except OSError as exc:
            raise IndexContractError('sync_owner_lock_unavailable') from exc
    finally:
        if fd is not None:
            os.close(fd)


def _check_sync_lease(record, queue_path=None):
    try:
        _check_sync_anchor(record['anchor'])
        current = (record['queue'] if record['recheck'] is None
                   else Path(record['recheck']()))
        if (not current.is_absolute() or current.is_symlink()
                or (queue_path is not None and Path(queue_path) != current)):
            raise IndexContractError('sync_owner_lock_unavailable')
        lock_path = current.parent / 'sync.lock'
        _check_held_sync_file(lock_path, record['fd'], record['lock_node'])
        if _file_identity(current) != record['queue_node']:
            raise IndexContractError('sync_owner_lock_unavailable')
    except OSError as exc:
        raise IndexContractError('sync_owner_lock_unavailable') from exc


def validate_sync_owner(claim, queue_path):
    """Validate only an active issued lease, without promoting bools or pins."""
    _check_sync_lease(_lease_record(claim), queue_path)


# Off-hook owners (sync worker, shadow driver steps) wait this long for the
# per-UID owner and queue locks before reporting busy; the resident service
# holds them for its periodic owner re-proof. Hooks never wait (wait=False).
SYNC_OWNER_WAIT_SECONDS = 3.0
_SYNC_OWNER_POLL_SECONDS = 0.05


@contextmanager
def sync_owner(queue_path: Path, *, recheck_queue=None, wait=False, deadline=None):
    """Issue a lexical owner after stable UID and install exclusions.

    ``wait=True`` retries a busy owner or queue lock for at most
    ``SYNC_OWNER_WAIT_SECONDS`` (never past ``deadline``), then yields None.
    """
    queue_path = Path(queue_path)
    if not queue_path.is_absolute() or queue_path.is_symlink():
        raise IndexContractError('sync_owner_lock_unavailable')
    if type(wait) is not bool or (deadline is not None and (
            type(deadline) not in (int, float) or deadline != deadline)):
        raise IndexContractError('sync_owner_lock_unavailable')
    _file_identity(queue_path)
    until = time.monotonic() + SYNC_OWNER_WAIT_SECONDS if wait else 0
    if deadline is not None:
        until = min(until, deadline)
    while True:
        with ExitStack() as held:
            anchor = held.enter_context(_sync_anchor())
            claim = None if anchor is None else held.enter_context(
                _queue_sync_owner(queue_path, recheck_queue=recheck_queue, anchor=anchor))
            if claim is not None or time.monotonic() + _SYNC_OWNER_POLL_SECONDS > until:
                yield claim
                return
        time.sleep(_SYNC_OWNER_POLL_SECONDS)


@contextmanager
def _queue_sync_owner(queue_path: Path, *, recheck_queue, anchor):
    """Hold install exclusion and issue a lexical, same-thread owner or None.

    All selected sync writers use the install's ``sync.lock`` from before
    admission until after receipt/ACK or retry release. The file is
    never unlinked, including on failure; unlinking would split the lock domain.
    The supplied store node and open kernel lock must remain unchanged.
    """
    queue_path = Path(queue_path)
    if not queue_path.is_absolute() or queue_path.is_symlink():
        raise IndexContractError('sync_owner_lock_unavailable')
    queue_identity = _file_identity(queue_path)
    lock_path = queue_path.parent / 'sync.lock'
    flags = os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0)
    flags |= getattr(os, 'O_CLOEXEC', 0)
    try:
        fd = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise IndexContractError('sync_owner_lock_unavailable') from exc
    try:
        opened = os.fstat(fd)
        if (_file_identity(lock_path) != (opened.st_dev, opened.st_ino)
                or stat.S_IMODE(opened.st_mode) != 0o600):
            raise IndexContractError('sync_owner_lock_unavailable')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield None
            return
        record = dict(fd=fd, pid=os.getpid(), thread=threading.current_thread(),
                      queue=queue_path, queue_node=queue_identity,
                      lock_node=(opened.st_dev, opened.st_ino), recheck=recheck_queue,
                      anchor=anchor)
        _check_sync_lease(record)
        claim = object.__new__(SyncLease)
        _sync_leases[claim] = record
        try:
            yield claim
            _check_sync_lease(record)
        finally:
            del _sync_leases[claim]
    finally:
        # Do not LOCK_UN: explicit pins share this open description and keep
        # exclusion after lexical authority is revoked.
        os.close(fd)


@contextmanager
def _owner_lock(queue_path: Path, *, recheck_queue=None, wait=False, deadline=None):
    """Legacy exact-bool adapter; no implicit reentry into a held owner."""
    with sync_owner(queue_path, recheck_queue=recheck_queue, wait=wait, deadline=deadline) as claim:
        yield claim is not None


@contextmanager
def borrow_sync_owner(queue_path: Path, claim=None):
    """Reuse only an explicitly supplied live owner; otherwise acquire normally."""
    if claim is None:
        with _owner_lock(queue_path) as owned:
            yield owned
    else:
        validate_sync_owner(claim, queue_path)
        pin = claim.pin()
        try:
            yield True
            validate_sync_owner(claim, queue_path)
        finally:
            # Revocation stops new writes, but cannot expose an in-flight
            # claim's retry cleanup to another owner before this call unwinds.
            pin.close()



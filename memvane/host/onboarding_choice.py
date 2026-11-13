"""The user's answer to the SessionStart onboarding notice, per project.

A project with ``.memvane/settings.json`` but no selected route gets one
onboarding line at SessionStart. The user can skip it for the current session
or decline it for the project; an explicit onboarding request turns it back on.

The answer lives outside the repository, in the per-user state base::

    ~/.local/state/memvane/v1/onboarding/<sha256(canonical project root)>.json

The canonical root is the resolved path with the file system's own spelling
(``F_GETPATH`` on macOS), so case aliases such as ``/users`` and ``/Users``
share one record. A session skip expires after ``SKIP_RETENTION_MS``.

No install state root exists before onboarding (the install ID is minted by
setup), and a file in the project would be committed and silence teammates.
The record only hides the notice: it never selects a route or starts index,
wiki or learning work. Reading it never creates anything, and an unreadable
record reads as "ask" so a fault shows the notice instead of hiding it.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX
    fcntl = None
import secrets

sys.dont_write_bytecode = True

import time

from memvane.settings import config
from memvane.kernel import memvane_paths
from memvane.kernel.paths import canonical

SCHEMA = 'memvane-onboarding-choice-v1'
MAX_BYTES = 128 * 1024
MAX_SESSIONS = 512
LOCK_TIMEOUT_SECONDS = 2.0
SKIP_RETENTION_MS = 30 * 24 * 60 * 60 * 1000
# Same shape event_log accepts; the id is also quoted into the notice command.
_SESSION = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z')


def choice_dir(home=None) -> Path:
    return memvane_paths.state_base(home) / 'onboarding'




def project_root(cwd) -> Path | None:
    """The settings-owning project root for ``cwd``, or None without settings."""
    if not isinstance(cwd, str) or not Path(cwd).is_absolute():
        return None
    found = config.find_project_config(cwd)
    if found.get('configFormat') != 'auto-context-dir':
        return None
    return canonical(found.get('projectRoot') or cwd)


def record_path(root, home=None) -> Path:
    key = hashlib.sha256(str(canonical(root)).encode()).hexdigest()
    return choice_dir(home) / (key + '.json')


def valid_session(value) -> str | None:
    return value if isinstance(value, str) and _SESSION.fullmatch(value) else None


def _read(path: Path, root: Path, *, home=None) -> dict | None:
    try:
        with _choice_directory(home) as (directory, directory_fd, check_directory):
            if path.parent != directory:
                raise ValueError('onboarding_choice_unreadable')
            check_directory()
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                         dir_fd=directory_fd)
            try:
                info = os.fstat(fd)
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                        or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o077
                        or info.st_size > MAX_BYTES):
                    raise ValueError('onboarding_choice_unreadable')
                data = os.read(fd, MAX_BYTES + 1)
                if len(data) > MAX_BYTES:
                    raise ValueError('onboarding_choice_unreadable')
                check_directory()
            finally:
                os.close(fd)
    except FileNotFoundError:
        return None
    value = json.loads(data.decode('utf-8'))
    if (not isinstance(value, dict) or value.get('schema') != SCHEMA or
            value.get('projectRoot') != str(root) or
            not isinstance(value.get('never'), bool) or
            not isinstance(value.get('skippedSessions'), list) or
            not all(_valid_skip(item) for item in value['skippedSessions'])):
        raise ValueError('onboarding_choice_unreadable')
    return value


def _valid_skip(item) -> bool:
    return (isinstance(item, dict) and set(item) == {'session', 'at'} and
            valid_session(item['session']) is not None and
            type(item['at']) is int and item['at'] >= 0)


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _live_skips(value: dict, now: int) -> list:
    return [item for item in value['skippedSessions'] if now - item['at'] <= SKIP_RETENTION_MS]


def suppressed(root, session=None, home=None) -> bool:
    """True when the user declined the project or skipped this session.

    Fails open: any read or shape fault keeps the notice.
    """
    try:
        root = canonical(root)
        value = _read(record_path(root, home), root, home=home)
    except Exception:
        return False
    if value is None:
        return False
    if value['never']:
        return True
    session = valid_session(session)
    return session is not None and any(
        item['session'] == session for item in _live_skips(value, _now_ms()))


@contextmanager
def _choice_directory(home=None, *, create=False):
    """Open the lexical HOME-to-state chain without following any symlink.

    Existing ancestors are fully checked before the first mkdir or chmod.
    Every mutation is relative to retained, owner-checked directory FDs.
    """
    home = Path(home) if home is not None else Path.home()
    if (not home.is_absolute() or home == Path(home.anchor) or '..' in home.parts
            or not hasattr(os, 'O_NOFOLLOW')):
        raise ValueError('onboarding_choice_dir_unsafe')
    directory = choice_dir(home)
    parts = directory.parts[1:]
    home_end = len(home.parts) - 2
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptors, links = [], []
    try:
        current = os.open('/', flags)
        descriptors.append(current)
        def validate(info, owned):
            if (not stat.S_ISDIR(info.st_mode) or
                    (owned and (info.st_uid != os.getuid() or info.st_mode & 0o022))):
                raise ValueError('onboarding_choice_dir_unsafe')
        def check():
            for parent, name, child, node, owned in links:
                opened = os.fstat(child)
                named = os.stat(name, dir_fd=parent, follow_symlinks=False)
                validate(opened, owned)
                validate(named, owned)
                if ((opened.st_dev, opened.st_ino) != node or
                        (named.st_dev, named.st_ino) != node):
                    raise ValueError('onboarding_choice_dir_changed')
        missing = len(parts)
        for position, name in enumerate(parts):
            try:
                child = os.open(name, flags, dir_fd=current)
            except FileNotFoundError:
                if position <= home_end or not create:
                    raise
                missing = position
                break
            descriptors.append(child)
            info = os.fstat(child)
            owned = position >= home_end
            validate(info, owned)
            links.append((current, name, child, (info.st_dev, info.st_ino), owned))
            current = child
        check()
        for name in parts[missing:]:
            check()
            try:
                os.mkdir(name, 0o700, dir_fd=current)
            except FileExistsError:
                pass
            child = os.open(name, flags, dir_fd=current)
            descriptors.append(child)
            info = os.fstat(child)
            validate(info, True)
            links.append((current, name, child, (info.st_dev, info.st_ino), True))
            current = child
        check()
        if create:
            os.fchmod(current, 0o700)
            check()
        yield directory, current, check
    finally:
        for fd in reversed(descriptors):
            os.close(fd)


def _private_dir(home=None) -> Path:
    with _choice_directory(home, create=True) as (directory, _fd, _check):
        return directory


@contextmanager
def _choice_lock(root: Path, home=None):
    """Serialize writers on one persistent, private canonical-project inode."""
    if fcntl is None or not hasattr(os, 'O_NOFOLLOW'):
        raise ValueError('onboarding_choice_lock_unavailable')
    with _choice_directory(home, create=True) as (directory, directory_fd, check_directory):
        fd = None
        locked = False
        try:
            name = record_path(root, home).with_suffix('.lock').name
            flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
            check_directory()
            try:
                fd = os.open(name, flags | os.O_CREAT | os.O_EXCL, 0o600,
                             dir_fd=directory_fd)
                os.fchmod(fd, 0o600)
            except FileExistsError:
                fd = os.open(name, flags, dir_fd=directory_fd)
            def check():
                check_directory()
                opened = os.fstat(fd)
                named = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                if (not stat.S_ISREG(opened.st_mode) or opened.st_uid != os.getuid()
                        or stat.S_IMODE(opened.st_mode) != 0o600 or opened.st_nlink != 1
                        or not stat.S_ISREG(named.st_mode)
                        or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)):
                    raise ValueError('onboarding_choice_lock_changed')
            check()
            deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locked = True
                    break
                except (BlockingIOError, InterruptedError):
                    check()
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise ValueError('onboarding_choice_lock_busy')
                    time.sleep(min(0.01, remaining))
            check()
            yield check
        finally:
            if fd is not None:
                try:
                    if locked:
                        fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    os.close(fd)


def _write(root: Path, value: dict, home=None, *, check_locked=None) -> None:
    data = (json.dumps(value, ensure_ascii=False, sort_keys=True) + '\n').encode()
    if len(data) > MAX_BYTES:
        raise ValueError('onboarding_choice_too_large')
    if check_locked is not None:
        check_locked()
    with _choice_directory(home, create=True) as (_directory, directory_fd, check_directory):
        name = '.write-' + secrets.token_hex(16)
        check_directory()
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=directory_fd)
        try:
            with os.fdopen(fd, 'wb') as out:
                os.fchmod(out.fileno(), 0o600)
                out.write(data)
                out.flush()
                os.fsync(out.fileno())
            if check_locked is not None:
                check_locked()
            check_directory()
            os.replace(name, record_path(root, home).name,
                       src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        finally:
            try:
                os.unlink(name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass


def _current(root: Path, home=None) -> dict:
    try:
        value = _read(record_path(root, home), root, home=home)
    except Exception:
        value = None
    return value or {'schema': SCHEMA, 'projectRoot': str(root), 'never': False,
                     'skippedSessions': []}


def _require_root(project) -> Path:
    root = project_root(project)
    if root is None:
        raise ValueError('onboarding_settings_required')
    return root


def _result(status, root, home=None, **extra):
    return {'status': status, 'projectRoot': str(root),
            'recordPath': str(record_path(root, home)), 'changesApplied': extra.pop('changes', 0),
            **extra}


def skip(project, session, home=None) -> dict:
    """Hide the notice for one host session of this project."""
    session = valid_session(session)
    if session is None:
        raise ValueError('onboarding_skip_session_required')
    root = _require_root(project)
    with _choice_lock(root, home) as check:
        value = _current(root, home)
        now = _now_ms()
        kept = [item for item in _live_skips(value, now) if item['session'] != session]
        value['skippedSessions'] = (kept + [{'session': session, 'at': now}])[-MAX_SESSIONS:]
        _write(root, value, home, check_locked=check)
    return _result('onboarding_notice_skipped', root, home, session=session, changes=1)


def never(project, home=None) -> dict:
    """Stop the automatic notice for this project until an explicit resume."""
    root = _require_root(project)
    with _choice_lock(root, home) as check:
        value = _current(root, home)
        value['never'] = True
        _write(root, value, home, check_locked=check)
    return _result('onboarding_notice_never', root, home, changes=1)


def resume(project, home=None) -> dict:
    """Forget a skip or decline so the notice shows again."""
    root = _require_root(project)
    removed = 0
    with _choice_lock(root, home) as check:
        with _choice_directory(home) as (_directory, directory_fd, check_directory):
            check()
            check_directory()
            try:
                os.unlink(record_path(root, home).name, dir_fd=directory_fd)
                removed = 1
            except FileNotFoundError:
                pass
    return _result('onboarding_notice_enabled', root, home, changes=removed)


def status(project, home=None) -> dict:
    root = _require_root(project)
    try:
        value = _read(record_path(root, home), root, home=home)
    except Exception:
        return _result('onboarding_notice_status', root, home, choice='ask',
                       reason='onboarding_choice_unreadable')
    if value is None:
        return _result('onboarding_notice_status', root, home, choice='ask')
    return _result('onboarding_notice_status', root, home,
                   choice='never' if value['never'] else 'ask',
                   skippedSessions=len(value['skippedSessions']))

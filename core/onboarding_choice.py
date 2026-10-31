"""The user's answer to the SessionStart onboarding notice, per project.

A project with ``.memvane/settings.json`` but no selected route gets one
onboarding line at SessionStart. The user can skip it for the current session
or decline it for the project; an explicit onboarding request turns it back on.

The answer lives outside the repository, in the per-user state base::

    ~/.local/state/memvane/onboarding/<sha256(canonical project root)>.json

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
import tempfile

sys.dont_write_bytecode = True

import time

import config
import memvane_paths

SCHEMA = 'memvane-onboarding-choice-v1'
MAX_BYTES = 16384
MAX_SESSIONS = 512
SKIP_RETENTION_MS = 30 * 24 * 60 * 60 * 1000
# Same shape event_log accepts; the id is also quoted into the notice command.
_SESSION = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z')


def choice_dir(home=None) -> Path:
    return memvane_paths.state_base(home) / 'onboarding'


def canonical(path) -> Path:
    """``path`` resolved, in the file system's spelling where it differs."""
    resolved = Path(path).resolve()
    getpath = getattr(fcntl, 'F_GETPATH', None) if fcntl is not None else None
    if getpath is not None:
        try:
            fd = os.open(resolved, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        except OSError:
            return Path(os.path.normcase(str(resolved)))
        try:
            # MAXPATHLEN on macOS; fcntl caps the buffer at 1024 bytes.
            raw = fcntl.fcntl(fd, getpath, bytes(1024))
        except (OSError, ValueError):
            raw = None
        finally:
            os.close(fd)
        if raw:
            spelled = os.fsdecode(raw.split(b'\0', 1)[0])
            if spelled and os.path.isabs(spelled):
                return Path(spelled)
    return Path(os.path.normcase(str(resolved)))


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


def _read(path: Path, root: Path) -> dict | None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
            raise ValueError('onboarding_choice_unreadable')
        data = os.read(fd, MAX_BYTES + 1)
    finally:
        os.close(fd)
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
        value = _read(record_path(root, home), root)
    except Exception:
        return False
    if value is None:
        return False
    if value['never']:
        return True
    session = valid_session(session)
    return session is not None and any(
        item['session'] == session for item in _live_skips(value, _now_ms()))


def _private_dir(home=None) -> Path:
    base = memvane_paths.state_base(home)
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory = choice_dir(home)
    try:
        directory.mkdir(mode=0o700)
    except FileExistsError:
        pass
    info = os.lstat(directory)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError('onboarding_choice_dir_unsafe')
    os.chmod(directory, 0o700)
    return directory


def _write(root: Path, value: dict, home=None) -> None:
    directory = _private_dir(home)
    path = record_path(root, home)
    data = (json.dumps(value, ensure_ascii=False, sort_keys=True) + '\n').encode()
    fd, name = tempfile.mkstemp(prefix='.write-', dir=directory)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'wb') as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _current(root: Path, home=None) -> dict:
    try:
        value = _read(record_path(root, home), root)
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
    value = _current(root, home)
    now = _now_ms()
    kept = [item for item in _live_skips(value, now) if item['session'] != session]
    value['skippedSessions'] = (kept + [{'session': session, 'at': now}])[-MAX_SESSIONS:]
    _write(root, value, home)
    return _result('onboarding_notice_skipped', root, home, session=session, changes=1)


def never(project, home=None) -> dict:
    """Stop the automatic notice for this project until an explicit resume."""
    root = _require_root(project)
    value = _current(root, home)
    value['never'] = True
    _write(root, value, home)
    return _result('onboarding_notice_never', root, home, changes=1)


def resume(project, home=None) -> dict:
    """Forget a skip or decline so the notice shows again."""
    root = _require_root(project)
    removed = 0
    try:
        os.unlink(record_path(root, home))
        removed = 1
    except FileNotFoundError:
        pass
    return _result('onboarding_notice_enabled', root, home, changes=removed)


def status(project, home=None) -> dict:
    root = _require_root(project)
    try:
        value = _read(record_path(root, home), root)
    except Exception:
        return _result('onboarding_notice_status', root, home, choice='ask',
                       reason='onboarding_choice_unreadable')
    if value is None:
        return _result('onboarding_notice_status', root, home, choice='ask')
    return _result('onboarding_notice_status', root, home,
                   choice='never' if value['never'] else 'ask',
                   skippedSessions=len(value['skippedSessions']))

"""Wiki compile and maintenance activity log, kept in the install state root.

A project wiki holds only cards. Card writes and deletions by the compiler,
the verifier, dedup resolution and pending-replacement publication append one
line to ``<install state root>/wiki-activity.log`` instead:

    - <ISO-8601 UTC> <action> <wiki-relative path> - <title>

The state root is the directory of the verified route's install journal, the
same root that holds ``bundle-auto-upgrade.log``. A project without a verified
selected route has no state root and logs nothing; nothing is written under a
per-user directory or the project.

The state root is reached one component at a time from ``/`` with
``O_NOFOLLOW|O_DIRECTORY``: no component may be a symlink, every ancestor is
owned by the user or root and is not group/world-writable unless sticky, and
the state root itself is owned by the user with no group/other bits. The log
file is opened relative to that directory fd, never by path. The file is capped
at ``LOG_LIMIT`` bytes; past the cap it rotates to ``.1`` (one older generation
kept). Logging never fails a wiki write: every error is swallowed. The compile
worker reads the last ``TAIL_CHARS`` characters as the extractor payload's
``wiki.logTail``.
"""
from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import stat

LOG_NAME = 'wiki-activity.log'
ROTATED_NAME = LOG_NAME + '.1'
LOG_LIMIT = 256 * 1024
TAIL_CHARS = 8000
_NOFOLLOW = getattr(os, 'O_NOFOLLOW', 0)
_DIRECTORY = getattr(os, 'O_DIRECTORY', 0)
_CLOEXEC = getattr(os, 'O_CLOEXEC', 0)

_UNRESOLVED = object()


def _resolve_bound(root: Path):
    import wiki_selected_laya
    return wiki_selected_laya.selected(root)


def state_dir(root, bound=_UNRESOLVED) -> Path | None:
    """The install state root for ``root``'s verified route, or None.

    ``bound`` is the ``wiki_selected_laya.selected`` result the caller already
    holds. Omitted, it is resolved here; a route that cannot be proven yields
    None (nothing is logged rather than logged into an unproven directory).
    """
    try:
        if bound is _UNRESOLVED:
            bound = _resolve_bound(Path(root).resolve())
        if bound is None:
            return None
        journal = bound[0].get('journalPath')
        if not isinstance(journal, str) or not os.path.isabs(journal):
            return None
        return Path(journal).parent
    except Exception:  # noqa: BLE001 - logging never fails a wiki write
        return None


def _open_state_dir(path: Path) -> int:
    """A directory fd for ``path`` reached without following any symlink."""
    uid = os.getuid()
    parts = Path(path).parts
    if (len(parts) < 2 or parts[0] != os.sep
            or any(part in ('', '.', '..') for part in parts[1:])):
        raise ValueError('wiki_activity_dir_unsafe')
    fd = os.open(os.sep, os.O_RDONLY | _DIRECTORY | _CLOEXEC)
    try:
        for index, part in enumerate(parts[1:], start=1):
            child = os.open(part, os.O_RDONLY | _DIRECTORY | _NOFOLLOW | _CLOEXEC, dir_fd=fd)
            os.close(fd)
            fd = child
            info = os.fstat(fd)
            mode = stat.S_IMODE(info.st_mode)
            if not stat.S_ISDIR(info.st_mode):
                raise ValueError('wiki_activity_dir_unsafe')
            if index == len(parts) - 1:
                if info.st_uid != uid or mode & 0o077:
                    raise ValueError('wiki_activity_dir_unsafe')
            elif (info.st_uid not in (uid, 0)
                    or (mode & 0o022 and not mode & stat.S_ISVTX)):
                raise ValueError('wiki_activity_dir_unsafe')
        return fd
    except BaseException:
        os.close(fd)
        raise


def log_path(root, bound=_UNRESOLVED) -> Path | None:
    """Where the activity log lives when its state root passes the checks."""
    directory = state_dir(root, bound)
    if directory is None:
        return None
    try:
        os.close(_open_state_dir(directory))
    except (OSError, ValueError):
        return None
    return directory / LOG_NAME


def append(root, action: str, rel: str, title: str, bound=_UNRESOLVED) -> bool:
    """Append one event line. Returns whether the line was written."""
    directory = state_dir(root, bound)
    if directory is None:
        return False
    title = ' '.join(str(title).split())
    stamp = datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')
    line = f'- {stamp} {action} {rel} - {title}\n'.encode('utf-8')
    try:
        dfd = _open_state_dir(directory)
    except (OSError, ValueError):
        return False
    try:
        try:
            info = os.stat(LOG_NAME, dir_fd=dfd, follow_symlinks=False)
        except FileNotFoundError:
            info = None
        if info is not None:
            if not stat.S_ISREG(info.st_mode):
                return False
            if info.st_size > LOG_LIMIT:
                os.replace(LOG_NAME, ROTATED_NAME, src_dir_fd=dfd, dst_dir_fd=dfd)
        fd = os.open(LOG_NAME, os.O_WRONLY | os.O_CREAT | os.O_APPEND | _NOFOLLOW | _CLOEXEC,
                     0o600, dir_fd=dfd)
        try:
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode) or opened.st_uid != os.getuid():
                return False
            os.write(fd, line)
        finally:
            os.close(fd)
        return True
    except (OSError, ValueError):
        return False
    finally:
        os.close(dfd)


def tail(root, bound=_UNRESOLVED, limit: int = TAIL_CHARS) -> str:
    """The last ``limit`` characters of the log, or "" when there is none."""
    directory = state_dir(root, bound)
    if directory is None:
        return ''
    try:
        dfd = _open_state_dir(directory)
    except (OSError, ValueError):
        return ''
    try:
        fd = os.open(LOG_NAME, os.O_RDONLY | _NOFOLLOW | _CLOEXEC, dir_fd=dfd)
    except OSError:
        os.close(dfd)
        return ''
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return ''
        # UTF-8 is at most 4 bytes per character.
        window = min(info.st_size, limit * 4)
        os.lseek(fd, info.st_size - window, os.SEEK_SET)
        data = b''
        while len(data) < window:
            chunk = os.read(fd, window - len(data))
            if not chunk:
                break
            data += chunk
    except OSError:
        return ''
    finally:
        os.close(fd)
        os.close(dfd)
    return data.decode('utf-8', errors='ignore')[-limit:]

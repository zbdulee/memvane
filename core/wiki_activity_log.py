"""Wiki compile and maintenance activity log, kept outside the project.

A project wiki holds only cards. Card writes and deletions by the compiler,
the verifier, dedup resolution and pending-replacement publication append one
line here instead:

    - <ISO-8601 UTC> <action> <wiki-relative path> - <title>

Location:
  * selected project: ``<install state root>/wiki-activity.log``. The state
    root is the directory of the verified route's install journal, the same
    root that holds ``bundle-auto-upgrade.log``.
  * unselected project (synthetic fixtures): the per-user auto-context state,
    ``wiki-activity/<project key>.log``.

The file is capped at ``LOG_LIMIT`` bytes; past the cap it rotates to ``.1``
(one older generation kept). Logging never fails a wiki write: every error is
swallowed. The compile worker reads the last ``TAIL_CHARS`` characters as the
extractor payload's ``wiki.logTail``.
"""
from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import stat

import memvane_paths
import scan_state

LOG_NAME = 'wiki-activity.log'
FALLBACK_DIR = 'wiki-activity'
LOG_LIMIT = 256 * 1024
TAIL_CHARS = 8000
_NOFOLLOW = getattr(os, 'O_NOFOLLOW', 0)

_UNRESOLVED = object()


def _private_dir(path: Path) -> Path:
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077):
        raise ValueError('wiki_activity_dir_unsafe')
    return path


def _resolve_bound(root: Path):
    import wiki_selected_laya
    return wiki_selected_laya.selected(root)


def log_path(root, bound=_UNRESOLVED, *, create: bool = False) -> Path | None:
    """The activity log for ``root``; None when no safe location exists.

    ``bound`` is the ``wiki_selected_laya.selected`` result the caller already
    holds. Omitted, it is resolved here; a route that cannot be proven yields
    None (nothing is logged rather than logged into an unproven directory).
    """
    root = Path(root).resolve()
    try:
        if bound is _UNRESOLVED:
            bound = _resolve_bound(root)
        if bound is not None:
            journal = bound[0].get('journalPath')
            if not isinstance(journal, str) or not os.path.isabs(journal):
                return None
            return _private_dir(Path(journal).parent) / LOG_NAME
        base = memvane_paths.auto_context_state() / FALLBACK_DIR
        if create:
            base.mkdir(mode=0o700, parents=True, exist_ok=True)
        if base.exists():
            _private_dir(base)
        return base / f'{scan_state.project_key(str(root), None)}.log'
    except Exception:  # noqa: BLE001 - logging never fails a wiki write
        return None


def append(root, action: str, rel: str, title: str, bound=_UNRESOLVED) -> bool:
    """Append one event line. Returns whether the line was written."""
    path = log_path(root, bound, create=True)
    if path is None:
        return False
    title = ' '.join(str(title).split())
    stamp = datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')
    line = f'- {stamp} {action} {rel} - {title}\n'
    try:
        if path.is_symlink():
            return False
        if path.exists() and path.stat().st_size > LOG_LIMIT:
            os.replace(path, path.with_name(path.name + '.1'))
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | _NOFOLLOW, 0o600)
        with os.fdopen(fd, 'a', encoding='utf-8') as out:
            out.write(line)
        return True
    except (OSError, ValueError):
        return False


def tail(root, bound=_UNRESOLVED, limit: int = TAIL_CHARS) -> str:
    """The last ``limit`` characters of the log, or "" when there is none."""
    path = log_path(root, bound)
    if path is None:
        return ''
    try:
        fd = os.open(path, os.O_RDONLY | _NOFOLLOW)
    except OSError:
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
    return data.decode('utf-8', errors='ignore')[-limit:]

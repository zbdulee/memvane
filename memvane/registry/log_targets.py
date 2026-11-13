"""Owner diagnostic-target registry (diagnostic logging Task 4).

The registry names the installs whose diagnostic logs the owner wants to read
together. It stores nothing but a canonical project root and the install ID
that was bound to it at registration time; it never holds event content.

File: ``$XDG_CONFIG_HOME/memvane/log-targets.json`` (``~/.config`` when
unset). Each line is a bounded UTF-8 JSON object::

    {"version": 1, "op": "upsert"|"remove", "root": "/abs", "install_id": "..."}

Safety contract:

* Directories are opened with ``O_NOFOLLOW|O_DIRECTORY``, except root-owned
  system symlinks above the private config directory;
  ancestors must be owned by the user or root and not group/world writable
  (unless sticky); the ``memvane`` directory must be user-owned with no group
  or other bits. The file is opened relative to that fd with ``O_NOFOLLOW``
  and must be a regular, user-owned, ``0600``, single-link file.
* Writers take a non-blocking exclusive ``flock`` on the registry inode itself
  (no side lock file, no inode replacement) and re-check that the directory
  entry still names the locked inode. A held lock is ``registry_busy``.
* Only a writer, under that lock, trims an incomplete final record (no
  trailing newline). Any malformed complete line, or an oversize file, is
  ``registry_corrupt`` and nothing is written.
* Reads re-verify each target against the current route: the install ID must
  still match, else ``stale_route``.

All failures are fixed refusal strings; no exception text or
event body is returned.
"""
from __future__ import annotations

import errno
import fcntl
import json
import os
from pathlib import Path
import re
import stat

MAX_TARGETS = 64
MAX_REQUESTS = 256
MAX_LINE_BYTES = 8192
MAX_REGISTRY_BYTES = 1 << 20
VERSION = 1

REFUSALS = ('missing', 'not_onboarded', 'stale_route', 'limit_exceeded',
            'unsafe_registry', 'registry_corrupt', 'registry_busy',
            'registry_missing', 'registry_incomplete')

_NAME = 'log-targets.json'
_DIRNAME = 'memvane'
_KEYS = frozenset(('version', 'op', 'root', 'install_id'))
_INSTALL = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}')
_NOFOLLOW = getattr(os, 'O_NOFOLLOW', 0)
_DIRECTORY = getattr(os, 'O_DIRECTORY', 0)
_CLOEXEC = getattr(os, 'O_CLOEXEC', 0)
_NONBLOCK = getattr(os, 'O_NONBLOCK', 0)


class Refusal(Exception):
    """A fixed, content-free refusal reason."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


# --------------------------------------------------------------------- paths

def _config_home() -> Path:
    value = os.environ.get('XDG_CONFIG_HOME')
    if value and os.path.isabs(value):
        return Path(value)
    return Path(os.path.expanduser('~')) / '.config'


def registry_path() -> Path:
    return _config_home() / _DIRNAME / _NAME


# -------------------------------------------------------------------- routes

def route_for(root: Path):
    """The current selected route for ``root``, via the existing route API.

    Returns ``{'install_id', 'state_root', 'onboarded'}`` or ``None`` when no
    install is bound. Reads route state only; creates nothing.
    """
    try:
        from memvane.diagnostics import api as event_log
        from memvane.journal import api as laya_index_host_route
        decision = laya_index_host_route.select_route(str(root), action='recall')
    except Exception:
        return None
    if not isinstance(decision, dict) or decision.get('state') == 'worktree':
        return None
    if decision.get('state') == 'legacy':
        return {'install_id': None, 'state_root': None, 'onboarded': False}
    if (decision.get('state') not in ('candidate', 'preview')
            or decision.get('projectRoot') != str(root)):
        return None
    install = event_log.install_of(decision)
    state_root = event_log.state_root_for_decision(decision)
    if install is None or state_root is None:
        return None
    return {'install_id': install, 'state_root': state_root,
            'onboarded': True}


def _route(root: str):
    try:
        route = route_for(Path(root))
    except Exception:
        return None
    if not isinstance(route, dict):
        return None
    if route.get('onboarded') is False and route.get('install_id') is None:
        return route
    install = route.get('install_id')
    if not isinstance(install, str) or not _INSTALL.fullmatch(install):
        return None
    return route


def _canonical(requested):
    """``(root, None)`` or ``(None, 'missing')``; no filesystem mutation."""
    if (not isinstance(requested, str) or not requested or '\x00' in requested
            or len(requested.encode('utf-8', 'surrogateescape')) > 4096):
        return None, 'missing'
    try:
        root = os.path.realpath(os.path.abspath(requested))
        if not stat.S_ISDIR(os.stat(root).st_mode):
            return None, 'missing'
    except (OSError, ValueError):
        return None, 'missing'
    return root, None


# ------------------------------------------------------------ safe file I/O

def _check_ancestor(fd: int, uid: int) -> None:
    info = os.fstat(fd)
    mode = stat.S_IMODE(info.st_mode)
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid not in (uid, 0)
            or (mode & 0o022 and not mode & stat.S_ISVTX)):
        raise Refusal('unsafe_registry')


def _system_link(part: str, fd: int, flags: int, is_last: bool, exc: OSError) -> int:
    """Follow only a root-owned symlink above the config dir (e.g. macOS /var).

    The containing directory already passed the ancestor check, so only root
    can retarget such a link. User-owned links, and any link at the
    ``memvane`` directory itself, are refused.
    """
    if is_last or exc.errno not in (errno.ELOOP, errno.ENOTDIR):
        raise Refusal('unsafe_registry') from exc
    try:
        link = os.stat(part, dir_fd=fd, follow_symlinks=False)
    except OSError as err:
        raise Refusal('unsafe_registry') from err
    if not stat.S_ISLNK(link.st_mode) or link.st_uid != 0:
        raise Refusal('unsafe_registry') from exc
    try:
        return os.open(part, flags & ~_NOFOLLOW, dir_fd=fd)
    except OSError as err:
        raise Refusal('unsafe_registry') from err


def _open_dir(create: bool):
    """A no-follow fd for the ``memvane`` config directory, or None if absent."""
    uid = os.getuid()
    parts = registry_path().parent.parts
    if (len(parts) < 2 or parts[0] != os.sep
            or any(p in ('', '.', '..') for p in parts[1:])):
        raise Refusal('unsafe_registry')
    fd = os.open(os.sep, os.O_RDONLY | _DIRECTORY | _CLOEXEC)
    try:
        last = len(parts) - 1
        for index, part in enumerate(parts[1:], start=1):
            flags = os.O_RDONLY | _DIRECTORY | _NOFOLLOW | _CLOEXEC
            try:
                child = os.open(part, flags, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    os.close(fd)
                    return None
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
                child = os.open(part, flags, dir_fd=fd)
            except OSError as exc:
                child = _system_link(part, fd, flags, index == last, exc)
            os.close(fd)
            fd = child
            if index == last:
                info = os.fstat(fd)
                if (not stat.S_ISDIR(info.st_mode) or info.st_uid != uid
                        or stat.S_IMODE(info.st_mode) & 0o077):
                    raise Refusal('unsafe_registry')
            else:
                _check_ancestor(fd, uid)
        return fd
    except Refusal:
        os.close(fd)
        raise
    except OSError as exc:
        os.close(fd)
        raise Refusal('unsafe_registry') from exc


def _check_file(fd: int, dfd: int) -> None:
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
        raise Refusal('unsafe_registry')
    try:
        entry = os.stat(_NAME, dir_fd=dfd, follow_symlinks=False)
    except OSError as exc:
        raise Refusal('unsafe_registry') from exc
    if (entry.st_dev, entry.st_ino) != (info.st_dev, info.st_ino):
        raise Refusal('unsafe_registry')


def _open_file(dfd: int, write: bool):
    base = _NOFOLLOW | _CLOEXEC | _NONBLOCK
    flags = (os.O_RDWR | os.O_APPEND if write else os.O_RDONLY) | base
    try:
        try:
            fd = os.open(_NAME, flags, dir_fd=dfd)
        except FileNotFoundError:
            if not write:
                return None
            fd = os.open(_NAME, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=dfd)
            os.fchmod(fd, 0o600)
    except OSError as exc:
        raise Refusal('unsafe_registry') from exc
    try:
        _check_file(fd, dfd)
        try:
            fcntl.flock(fd, (fcntl.LOCK_EX if write else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                raise Refusal('registry_busy') from exc
            raise Refusal('unsafe_registry') from exc
        # The entry may have been swapped between open and lock.
        _check_file(fd, dfd)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _read_all(fd: int) -> bytes:
    size = os.fstat(fd).st_size
    if size > MAX_REGISTRY_BYTES:
        raise Refusal('registry_corrupt')
    chunks, total = [], 0
    while True:
        chunk = os.pread(fd, 65536, total)
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > MAX_REGISTRY_BYTES:
            raise Refusal('registry_corrupt')
    return b''.join(chunks)


def _row(line: bytes):
    if not line or len(line) > MAX_LINE_BYTES:
        return None
    try:
        row = json.loads(line.decode('utf-8'))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(row, dict) or set(row) != _KEYS:
        return None
    root, install = row['root'], row['install_id']
    if (row['version'] != VERSION or type(row['version']) is not int
            or row['op'] not in ('upsert', 'remove')
            or not isinstance(root, str) or not os.path.isabs(root)
            or os.path.normpath(root) != root or '\x00' in root
            or not isinstance(install, str) or not _INSTALL.fullmatch(install)):
        return None
    return row


def _parse(data: bytes):
    """``(state, complete_len)``: root -> install, in first-registration order."""
    end = data.rfind(b'\n') + 1
    state: dict[str, str] = {}
    for line in data[:end].split(b'\n')[:-1]:
        row = _row(line)
        if row is None:
            raise Refusal('registry_corrupt')
        if row['op'] == 'upsert':
            state[row['root']] = row['install_id']
        else:
            state.pop(row['root'], None)
    if len(state) > MAX_TARGETS:
        raise Refusal('registry_corrupt')
    return state, end


def _line(root: str, install: str) -> bytes:
    return json.dumps({'version': VERSION, 'op': 'upsert', 'root': root,
                       'install_id': install},
                      ensure_ascii=True, separators=(',', ':')).encode('ascii') + b'\n'


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError(errno.EIO, 'short write')
        view = view[written:]
    os.fsync(fd)


# ------------------------------------------------------------------- public

def register(projects) -> dict:
    """Register projects; one result row per request, in request order."""
    if not isinstance(projects, (list, tuple)):
        return {'results': [], 'partial': True}
    requests = list(projects)
    results = []
    for index, requested in enumerate(requests):
        row = {'requested': requested if isinstance(requested, str) else None,
               'root': None, 'install_id': None, 'status': None}
        if index >= MAX_REQUESTS:
            row['status'] = 'limit_exceeded'
        results.append(row)

    dfd = fd = None
    try:
        try:
            dfd = _open_dir(create=True)
            fd = _open_file(dfd, write=True)
            data = _read_all(fd)
            state, complete = _parse(data)
        except Refusal as refusal:
            for row in results:
                row['status'] = row['status'] or refusal.reason
            return {'results': results, 'partial': True}

        pending: list[int] = []
        buffer = bytearray()
        for index, row in enumerate(results):
            if row['status']:
                continue
            root, reason = _canonical(row['requested'])
            if reason:
                row['status'] = reason
                continue
            row['root'] = root
            route = _route(root)
            if route is None:
                row['status'] = 'missing'
                continue
            install = route['install_id']
            row['install_id'] = install
            if not route.get('onboarded'):
                row['status'] = 'not_onboarded'
                continue
            if state.get(root) == install:
                row['status'] = 'duplicate'
                continue
            if any(key != root and value == install for key, value in state.items()):
                row['status'] = 'duplicate'
                continue
            if root not in state and len(state) >= MAX_TARGETS:
                row['status'] = 'limit_exceeded'
                continue
            line = _line(root, install)
            if len(line) > MAX_LINE_BYTES:
                row['status'] = 'missing'
                continue
            state[root] = install
            buffer += line
            pending.append(index)
            row['status'] = 'registered'

        try:
            if complete != len(data):
                # Interrupted append: drop only the incomplete tail, in place.
                os.ftruncate(fd, complete)
                os.fsync(fd)
            if buffer:
                if complete + len(buffer) > MAX_REGISTRY_BYTES:
                    raise Refusal('registry_corrupt')
                _write_all(fd, bytes(buffer))
        except (OSError, Refusal) as exc:
            reason = exc.reason if isinstance(exc, Refusal) else 'unsafe_registry'
            for index in pending:
                results[index]['status'] = reason
    finally:
        if fd is not None:
            os.close(fd)  # releases the flock
        if dfd is not None:
            os.close(dfd)
    partial = any(r['status'] not in ('registered', 'duplicate') for r in results)
    return {'results': results, 'partial': partial}


def resolve() -> list:
    """Every registered target re-verified against its current route."""
    dfd = fd = None
    try:
        try:
            dfd = _open_dir(create=False)
            if dfd is None:
                return [{'root': None, 'install_id': None, 'refusal': 'registry_missing'}]
            fd = _open_file(dfd, write=False)
            if fd is None:
                return [{'root': None, 'install_id': None, 'refusal': 'registry_missing'}]
            # A torn final record is reported; only a locked writer trims it.
            data = _read_all(fd)
            state, complete = _parse(data)
        except Refusal as refusal:
            return [{'root': None, 'install_id': None, 'refusal': refusal.reason}]
    finally:
        if fd is not None:
            os.close(fd)
        if dfd is not None:
            os.close(dfd)

    rows = ([{'root': None, 'install_id': None, 'refusal': 'registry_incomplete'}]
            if complete != len(data) else [])
    for root, expected in state.items():
        route = _route(root)
        row = {'root': root, 'install_id': expected}
        state_root = route.get('state_root') if route else None
        if route is None:
            row['refusal'] = 'missing'
        elif route['install_id'] != expected:
            row['refusal'] = 'stale_route'
        elif not route.get('onboarded'):
            row['refusal'] = 'not_onboarded'
        elif not isinstance(state_root, (str, Path)) or not os.path.isabs(str(state_root)):
            row['refusal'] = 'stale_route'
        else:
            row['state_root'] = str(state_root)
        rows.append(row)
    return rows

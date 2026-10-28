"""Install-scoped structured event log: ``<install state root>/events.jsonl``.

Every hook action, recall, wiki card write, index publish, off-hook wake,
resident service transition and bundle upgrade of one memvane-db install
appends one JSON line here. It is a debugging log for the owner and the
``memvane:logs`` skill; no code path reads it back except the compile
worker's ``wiki_tail`` (recent wiki card writes for the extractor prompt).

Line schema (``v`` 1)::

    {"ts": "<ISO-8601 UTC, ms>", "v": 1, "install": <installId|null>,
     "host": "claude|codex|hermes|worker", "session": <host session id|null>,
     "event": "recall|gate|...", "outcome": "...", "reason": <code|null>,
     "ms": <elapsed ms|null>, ...event ids}

Location and safety. The state root is the directory of the verified route's
install journal (``journalPath``'s parent). A project without one logs
nothing; nothing is written under the project or a per-user directory. The
root is reached one component at a time from ``/`` with
``O_NOFOLLOW|O_DIRECTORY``: no component may be a symlink, every ancestor is
owned by the user or root and is not group/world-writable unless sticky, and
the state root itself is owned by the user with no group/other bits (0700).
The file is opened relative to that directory fd with ``O_NOFOLLOW`` and must
be a regular file owned by the user.

Cost. One line is one ``os.write`` on an ``O_APPEND`` descriptor, capped at
``MAX_LINE`` bytes, with no fsync. A process keeps its proven state-root fd
and re-checks it with one ``fstat`` instead of re-walking the path. Past
``LOG_LIMIT`` bytes the file rotates: ``.1`` moves to ``.2`` and the live file
to ``.1`` (two older generations). ``events.lock`` coordinates the two: a
writer holds it shared from opening the live file through its write, and a
rotation needs it exclusive, so a rotation never moves a file a writer is
about to append to. Every lock is non-blocking: rotation is skipped while any
writer holds the lock, a writer retries the shared lock at most
``LOCK_ATTEMPTS`` times ``LOCK_WAIT`` apart (a rotation lasts two renames) and
otherwise drops the line, and a busy refusal counter drops its increment. A
file is opened with ``O_NONBLOCK|O_NOFOLLOW`` and used only when ``fstat``
shows a regular, user-owned file. Writes are best-effort: ``emit`` never
raises ``Exception`` and never prints.

Reading. ``read`` takes at most ``READ_LIMIT`` bytes, newest first, and skips
lines whose envelope types are wrong. ``wiki_tail`` renders only ``wiki``
events with a known outcome, a relative path without control characters and a
cleaned, capped title.

Refusals. A write that fails after the state root is proven (the file is a
symlink, foreign or not regular, or the write itself fails) increments the
``refused`` counter in ``events.refused.json`` beside it; a process also
carries its unwritten count as ``dropped`` on its next written line. A state
root that fails the walk is reported by ``inspect`` (the logs skill) instead,
so an empty log is always explained.

Privacy. Prompt text and document bodies are never written by default: a
prompt is ``{"sha256", "chars"}``. ``MEMVANE_EVENT_LOG_PROMPTS=1`` adds the
first ``PROMPT_CHARS`` characters as ``prompt.text``. Keys that name secrets
(token, password, secret, credential, api key, authorization, cookie) are
dropped from every line.
"""
from __future__ import annotations

import contextvars
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time

NAME = 'events.jsonl'
REFUSED_NAME = 'events.refused.json'
LOCK_NAME = 'events.lock'
LOCK_ATTEMPTS = 5
LOCK_WAIT = 0.0005
GENERATIONS = (NAME + '.1', NAME + '.2')
LOG_LIMIT = 4 * 1024 * 1024
MAX_LINE = 4096
PROMPT_CHARS = 1000
VERSION = 1
PROMPT_ENV = 'MEMVANE_EVENT_LOG_PROMPTS'
WIKI_TAIL_CHARS = 8000
WIKI_TAIL_WINDOW = 1024 * 1024
READ_LIMIT = 8 * 1024 * 1024
_NOFOLLOW = getattr(os, 'O_NOFOLLOW', 0)
_DIRECTORY = getattr(os, 'O_DIRECTORY', 0)
_CLOEXEC = getattr(os, 'O_CLOEXEC', 0)
_NONBLOCK = getattr(os, 'O_NONBLOCK', 0)
_STR_LIMIT = 256
_LIST_LIMIT = 16
_SECRET_KEY = re.compile(r'(?i)(token|passw|secret|credential|api[_-]?key|authori[sz]|cookie)')
_SESSION = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z')
_CODE = re.compile(r'[A-Za-z0-9_.:-]{1,80}\Z')
_UNRESOLVED = object()

# Per process (and per context for an in-process host): the bound install.
_BOUND: contextvars.ContextVar = contextvars.ContextVar('memvane_event_log_bound',
                                                       default=None)
_DROPPED = 0


class Unsafe(ValueError):
    """The state root or the log file fails the ownership/no-follow checks."""


# ---------------------------------------------------------------- state root

def state_root_of(journal_path) -> Path | None:
    """The install state root for a journal path, or None."""
    if not isinstance(journal_path, (str, Path)):
        return None
    journal = str(journal_path)
    if not os.path.isabs(journal):
        return None
    return Path(journal).parent


def state_root_for_decision(decision) -> Path | None:
    """State root of a route decision (a worktree uses its main route)."""
    if not isinstance(decision, dict):
        return None
    if decision.get('state') == 'worktree' and isinstance(decision.get('mainRoute'), dict):
        decision = decision['mainRoute']
    return state_root_of(decision.get('journalPath'))


def install_of(decision):
    if not isinstance(decision, dict):
        return None
    if decision.get('state') == 'worktree' and isinstance(decision.get('mainRoute'), dict):
        decision = decision['mainRoute']
    value = decision.get('installId')
    return value if isinstance(value, str) and _SESSION.fullmatch(value) else None


def open_state_dir(path) -> int:
    """A directory fd for ``path`` reached without following any symlink."""
    uid = os.getuid()
    parts = Path(path).parts
    if (len(parts) < 2 or parts[0] != os.sep
            or any(part in ('', '.', '..') for part in parts[1:])):
        raise Unsafe('state_root_unsafe')
    fd = os.open(os.sep, os.O_RDONLY | _DIRECTORY | _CLOEXEC)
    try:
        for index, part in enumerate(parts[1:], start=1):
            child = os.open(part, os.O_RDONLY | _DIRECTORY | _NOFOLLOW | _CLOEXEC, dir_fd=fd)
            os.close(fd)
            fd = child
            info = os.fstat(fd)
            mode = stat.S_IMODE(info.st_mode)
            if not stat.S_ISDIR(info.st_mode):
                raise Unsafe('state_root_unsafe')
            if index == len(parts) - 1:
                if info.st_uid != uid or mode & 0o077:
                    raise Unsafe('state_root_unsafe')
            elif (info.st_uid not in (uid, 0)
                    or (mode & 0o022 and not mode & stat.S_ISVTX)):
                raise Unsafe('state_root_unsafe')
        return fd
    except BaseException:
        os.close(fd)
        raise


# ------------------------------------------------------------------- binding

def bind(state_root=None, *, decision=None, install=None, host=None, session=None):
    """Bind the install every later ``emit`` without ``state_root`` writes to.

    Returns a token for ``unbind``. A hook process binds once in its entry
    point after route selection; deep call sites then emit without threading
    the route through every return path.
    """
    if state_root is None and decision is not None:
        state_root = state_root_for_decision(decision)
    if install is None and decision is not None:
        install = install_of(decision)
    return _BOUND.set({'stateRoot': state_root, 'install': install,
                       'host': host, 'session': session_id(session)})


def unbind(token) -> None:
    try:
        _BOUND.reset(token)
    except (ValueError, RuntimeError):
        pass


def bound() -> dict | None:
    return _BOUND.get()


def session_id(value):
    if isinstance(value, dict):
        value = value.get('session_id') or value.get('sessionId')
    return value if isinstance(value, str) and _SESSION.fullmatch(value) else None


# ------------------------------------------------------------------ payloads

def code(value):
    """A fixed diagnostic code (never a path or a message), or None."""
    if value is None:
        return None
    text = str(value)
    return text if _CODE.fullmatch(text) else type(value).__name__ if isinstance(
        value, BaseException) else 'unlabeled'


def prompt_fields(prompt) -> dict | None:
    """``{"sha256", "chars"}`` of a prompt; the text only with the debug flag."""
    if not isinstance(prompt, str):
        return None
    out = {'sha256': hashlib.sha256(prompt.encode('utf-8', 'surrogatepass')).hexdigest(),
           'chars': len(prompt)}
    if os.environ.get(PROMPT_ENV) == '1':
        out['text'] = prompt[:PROMPT_CHARS]
    return out


def _clean(value, depth=0):
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return round(value, 3) if value == value and abs(value) != float('inf') else None
    if isinstance(value, (str, Path)):
        return str(value)[:_STR_LIMIT]
    if depth >= 3:
        return None
    if isinstance(value, dict):
        return {str(key)[:64]: _clean(item, depth + 1) for key, item in value.items()
                if not _SECRET_KEY.search(str(key))}
    if isinstance(value, (list, tuple, set, frozenset)):
        items = list(value)
        return [_clean(item, depth + 1) for item in items[:_LIST_LIMIT]]
    return code(value)


def _line(record: dict) -> bytes:
    data = json.dumps(record, ensure_ascii=False, separators=(',', ':'),
                      allow_nan=False).encode('utf-8') + b'\n'
    if len(data) <= MAX_LINE:
        return data
    # Shrink the largest optional fields first, then keep only the envelope.
    record = dict(record)
    record['truncated'] = True
    for key in ('cards', 'notes'):
        while isinstance(record.get(key), list) and record[key] and len(data) > MAX_LINE:
            record[key] = record[key][:len(record[key]) // 2]
            data = json.dumps(record, ensure_ascii=False, separators=(',', ':')).encode() + b'\n'
    if len(data) > MAX_LINE and isinstance(record.get('prompt'), dict):
        record['prompt'] = {k: v for k, v in record['prompt'].items() if k != 'text'}
        data = json.dumps(record, ensure_ascii=False, separators=(',', ':')).encode() + b'\n'
    if len(data) > MAX_LINE:
        keep = ('ts', 'v', 'install', 'host', 'session', 'event', 'outcome', 'reason',
                'ms', 'truncated')
        record = {key: record.get(key) for key in keep}
        data = json.dumps(record, ensure_ascii=False, separators=(',', ':')).encode() + b'\n'
    return data


def _now_iso():
    return (datetime.now(timezone.utc).isoformat(timespec='milliseconds')
            .replace('+00:00', 'Z'))


# ------------------------------------------------------------------- writing

def _open_private(dfd, name: str, flags: int):
    """Open ``name`` in the state root: no symlink, no FIFO wait, regular and owned.

    ``O_NONBLOCK`` keeps a planted FIFO or device from blocking the open; the
    flag is cleared again for a regular file.
    """
    fd = os.open(name, flags | _NOFOLLOW | _CLOEXEC | _NONBLOCK, 0o600, dir_fd=dfd)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise Unsafe('events_not_regular' if not stat.S_ISREG(info.st_mode)
                         else 'events_not_owned')
        if _NONBLOCK:
            os.set_blocking(fd, True)
        return fd, info
    except BaseException:
        os.close(fd)
        raise


def _try_lock(fd: int, kind: int, attempts: int = 1) -> bool:
    """A bounded, non-blocking flock: ``attempts`` tries, ``LOCK_WAIT`` apart."""
    for attempt in range(attempts):
        try:
            fcntl.flock(fd, kind | fcntl.LOCK_NB)
            return True
        except OSError:
            if attempt + 1 < attempts:
                time.sleep(LOCK_WAIT)
    return False


def _rotate(dfd, lock_fd: int, size_hint: int) -> None:
    """Rotate under the exclusive lock; any holder (writer or rotator) skips it.

    Writers hold the shared lock from opening the live file to their write,
    so no rotation can move the file a writer is about to append to.
    """
    if not _try_lock(lock_fd, fcntl.LOCK_EX):
        return
    try:
        try:
            info = os.stat(NAME, dir_fd=dfd, follow_symlinks=False)
        except FileNotFoundError:
            return
        if not stat.S_ISREG(info.st_mode) or info.st_size + size_hint <= LOG_LIMIT:
            return
        try:
            os.replace(GENERATIONS[0], GENERATIONS[1], src_dir_fd=dfd, dst_dir_fd=dfd)
        except FileNotFoundError:
            pass
        os.replace(NAME, GENERATIONS[0], src_dir_fd=dfd, dst_dir_fd=dfd)
    except OSError:
        pass
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        except OSError:
            pass


def _count_refusal(dfd, reason: str) -> None:
    """Persist one refused write beside the log; never waits, never raises.

    A busy counter (another process updating it) drops this increment.
    """
    try:
        fd, info = _open_private(dfd, REFUSED_NAME, os.O_RDWR | os.O_CREAT)
    except (OSError, ValueError):
        return
    try:
        if info.st_size > 4096 or not _try_lock(fd, fcntl.LOCK_EX):
            return
        raw = os.pread(fd, 4096, 0)
        try:
            prior = json.loads(raw.decode('utf-8')) if raw else {}
            count = prior.get('refused') if isinstance(prior, dict) else 0
        except (UnicodeError, ValueError):
            count = 0
        count = count + 1 if type(count) is int and count >= 0 else 1
        body = json.dumps({'refused': count, 'lastReason': reason,
                           'lastTs': _now_iso()}).encode()
        os.ftruncate(fd, 0)
        os.pwrite(fd, body, 0)
    except OSError:
        pass
    finally:
        os.close(fd)


# Proven state-root directory fds of this process: path -> fd. A later emit
# re-checks the fd (still a linked, user-owned 0700 directory) instead of
# re-walking every path component.
_DIRS: dict[str, int] = {}
_DIRS_MAX = 8


def _state_dir_fd(state_root) -> int:
    """A cached, re-checked fd for ``state_root``; the caller must not close it."""
    key = str(state_root)
    fd = _DIRS.get(key)
    if fd is not None:
        try:
            info = os.fstat(fd)
            if (stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
                    and not stat.S_IMODE(info.st_mode) & 0o077 and info.st_nlink > 0):
                return fd
        except OSError:
            pass
        _DIRS.pop(key, None)
        try:
            os.close(fd)
        except OSError:
            pass
    fd = open_state_dir(state_root)
    if len(_DIRS) >= _DIRS_MAX:
        old_key = next(iter(_DIRS))
        try:
            os.close(_DIRS.pop(old_key))
        except OSError:
            pass
    _DIRS[key] = fd
    return fd


def _write(state_root, data: bytes) -> str | None:
    """Append one line; returns None when written, else the refusal code.

    Order: an oversize live file is rotated first (exclusive lock, skipped
    when busy); the append then holds the shared lock from opening the live
    file through its single write. Every lock is non-blocking with at most
    ``LOCK_ATTEMPTS`` short retries, so a hook never waits on another process.
    """
    try:
        dfd = _state_dir_fd(state_root)
    except (OSError, ValueError):
        return 'state_root_unsafe'
    try:
        lock_fd, _info = _open_private(dfd, LOCK_NAME, os.O_RDWR | os.O_CREAT)
    except Unsafe as exc:
        _count_refusal(dfd, str(exc))
        return str(exc)
    except OSError:
        _count_refusal(dfd, 'events_lock_refused')
        return 'events_lock_refused'
    try:
        try:
            info = os.stat(NAME, dir_fd=dfd, follow_symlinks=False)
        except FileNotFoundError:
            info = None
        except OSError:
            _count_refusal(dfd, 'events_stat_failed')
            return 'events_stat_failed'
        if info is not None and not stat.S_ISREG(info.st_mode):
            _count_refusal(dfd, 'events_not_regular')
            return 'events_not_regular'
        if info is not None and info.st_size + len(data) > LOG_LIMIT:
            _rotate(dfd, lock_fd, len(data))
        if not _try_lock(lock_fd, fcntl.LOCK_SH, LOCK_ATTEMPTS):
            # A rotation held the lock past the bounded wait: drop, never block.
            return 'events_lock_busy'
        try:
            try:
                fd, _opened = _open_private(dfd, NAME, os.O_WRONLY | os.O_CREAT | os.O_APPEND)
            except Unsafe as exc:
                _count_refusal(dfd, str(exc))
                return str(exc)
            except OSError:
                _count_refusal(dfd, 'events_open_refused')
                return 'events_open_refused'
            try:
                if os.write(fd, data) != len(data):
                    _count_refusal(dfd, 'events_short_write')
                    return 'events_short_write'
            except OSError:
                _count_refusal(dfd, 'events_write_failed')
                return 'events_write_failed'
            finally:
                os.close(fd)
            return None
        finally:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            except OSError:
                pass
    finally:
        os.close(lock_fd)


def emit(event: str, *, outcome=None, reason=None, ms=None, state_root=_UNRESOLVED,
         decision=None, install=_UNRESOLVED, host=_UNRESOLVED, session=_UNRESOLVED,
         **fields) -> bool:
    """Append one event; returns whether the line was written. Never raises.

    ``state_root``/``install``/``host``/``session`` default to the bound
    values (``bind``); ``decision`` supplies the state root and install of a
    route decision. Without a state root nothing is written.
    """
    global _DROPPED
    try:
        context = _BOUND.get() or {}
        if state_root is _UNRESOLVED:
            state_root = (state_root_for_decision(decision) if decision is not None
                          else context.get('stateRoot'))
        if install is _UNRESOLVED:
            install = install_of(decision) if decision is not None else context.get('install')
        if host is _UNRESOLVED:
            host = context.get('host') or 'worker'
        if session is _UNRESOLVED:
            session = context.get('session')
        if state_root is None:
            return False
        record = {'ts': _now_iso(), 'v': VERSION, 'install': install,
                  'host': host if isinstance(host, str) else None,
                  'session': session_id(session), 'event': str(event)[:64],
                  'outcome': code(outcome), 'reason': code(reason),
                  'ms': (round(ms) if isinstance(ms, (int, float)) and not isinstance(ms, bool)
                         and ms == ms and ms >= 0 and ms != float('inf') else None)}
        for key, value in fields.items():
            if key in record or _SECRET_KEY.search(key) or value is None:
                continue
            record[key] = _clean(value)
        if _DROPPED:
            record['dropped'] = _DROPPED
        refusal = _write(state_root, _line(record))
        if refusal is None:
            _DROPPED = 0
            return True
        _DROPPED += 1
        return False
    except Exception:  # noqa: BLE001 - a debugging log never fails its caller
        try:
            _DROPPED += 1
        except Exception:
            pass
        return False


def dropped() -> int:
    """Lines this process could not write since its last written line."""
    return _DROPPED


def elapsed_ms(started: float) -> float:
    return (time.monotonic() - started) * 1000


# ------------------------------------------------------------------- reading

def inspect(state_root) -> dict:
    """Why the log can or cannot be written: state root, file and refusals."""
    out = {'stateRoot': str(state_root) if state_root else None, 'writable': False,
           'reason': None, 'refused': 0, 'files': []}
    if state_root is None:
        out['reason'] = 'no_selected_route'
        return out
    try:
        dfd = open_state_dir(state_root)
    except FileNotFoundError:
        out['reason'] = 'state_root_missing'
        return out
    except (OSError, ValueError):
        out['reason'] = 'state_root_unsafe'
        return out
    try:
        for name in (NAME, *GENERATIONS):
            try:
                info = os.stat(name, dir_fd=dfd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            row = {'name': name, 'bytes': info.st_size,
                   'regular': stat.S_ISREG(info.st_mode),
                   'owned': info.st_uid == os.getuid()}
            out['files'].append(row)
            if name == NAME and not (row['regular'] and row['owned']):
                out['reason'] = 'events_not_regular' if not row['regular'] else 'events_not_owned'
        try:
            fd, _info = _open_private(dfd, REFUSED_NAME, os.O_RDONLY)
        except (OSError, ValueError):
            fd = None
        if fd is not None:
            try:
                value = json.loads(os.read(fd, 4096).decode('utf-8'))
                if isinstance(value, dict) and type(value.get('refused')) is int:
                    out['refused'] = value['refused']
                    out['lastRefusal'] = {'reason': value.get('lastReason'),
                                          'ts': value.get('lastTs')}
            except (OSError, ValueError):
                pass
            finally:
                os.close(fd)
        out['writable'] = out['reason'] is None
    finally:
        os.close(dfd)
    return out


def _read_file(dfd, name: str, window: int | None = None) -> bytes:
    """The last ``window`` bytes (whole lines) of a regular, owned file, or b''."""
    try:
        fd, info = _open_private(dfd, name, os.O_RDONLY)
    except (OSError, ValueError):
        return b''
    try:
        start = 0 if window is None else max(0, info.st_size - window)
        os.lseek(fd, start, os.SEEK_SET)
        chunks, left = [], info.st_size - start
        while left > 0:
            chunk = os.read(fd, min(left, 1 << 20))
            if not chunk:
                break
            chunks.append(chunk)
            left -= len(chunk)
        data = b''.join(chunks)
        if start:
            # Drop the partial first line of a window.
            cut = data.find(b'\n')
            data = data[cut + 1:] if cut >= 0 else b''
        return data
    except OSError:
        return b''
    finally:
        os.close(fd)


_TS = re.compile(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d{1,6})?Z\Z')


def _valid(row) -> bool:
    """A v1 record whose envelope has the written types; anything else is skipped."""
    return (isinstance(row, dict) and row.get('v') == VERSION
            and isinstance(row.get('event'), str) and 0 < len(row['event']) <= 64
            and isinstance(row.get('ts'), str) and _TS.fullmatch(row['ts']) is not None
            and all(row.get(key) is None or isinstance(row.get(key), str)
                    for key in ('outcome', 'reason', 'install', 'host', 'session'))
            and (row.get('ms') is None or type(row.get('ms')) is int))


def _parse(data: bytes) -> list[dict]:
    rows = []
    for raw in data.splitlines():
        try:
            row = json.loads(raw.decode('utf-8'))
        except (UnicodeError, ValueError, RecursionError):
            continue
        if _valid(row):
            rows.append(row)
    return rows


def read(state_root, *, generations: bool = True, max_bytes=_UNRESOLVED) -> list[dict]:
    """Events oldest first (rotated generations first). Read-only.

    At most ``max_bytes`` are read, newest first: the live file's tail, then
    older generations while budget remains. Malformed lines are skipped.
    """
    try:
        dfd = open_state_dir(state_root)
    except (OSError, ValueError):
        return []
    try:
        names = [NAME] + (list(GENERATIONS) if generations else [])
        left = READ_LIMIT if max_bytes is _UNRESOLVED else max_bytes
        chunks: list[list[dict]] = []
        for name in names:
            if left is not None and left <= 0:
                break
            data = _read_file(dfd, name, left)
            if left is not None:
                left -= len(data) + 1
            chunks.append(_parse(data))
        return [row for chunk in reversed(chunks) for row in chunk]
    finally:
        os.close(dfd)


# ------------------------------------------------------------ wiki activity

def wiki(root, action: str, rel: str, title: str, *, reason=None, bound=_UNRESOLVED) -> bool:
    """One wiki card write/delete for ``root``'s install. Never fails the write."""
    try:
        state_root = wiki_state_root(root, bound)
        if state_root is None:
            return False
        install = None
        if bound is not _UNRESOLVED and bound is not None:
            install = install_of(bound[0])
        title = ' '.join(str(title).split())[:200]
        return emit('wiki', outcome=action, reason=reason, state_root=state_root,
                    install=install, path=str(rel)[:_STR_LIMIT], title=title)
    except Exception:  # noqa: BLE001
        return False


def wiki_state_root(root, bound=_UNRESOLVED) -> Path | None:
    """The install state root for ``root``'s verified route, or None.

    ``bound`` is the ``wiki_selected_laya.selected`` result the caller holds;
    omitted, it is resolved here. An unproven route yields None.
    """
    try:
        if bound is _UNRESOLVED:
            import wiki_selected_laya
            bound = wiki_selected_laya.selected(Path(root).resolve())
        if bound is None:
            return None
        return state_root_of(bound[0].get('journalPath'))
    except Exception:  # noqa: BLE001
        return None


_WIKI_OUTCOMES = frozenset(('created', 'updated', 'deleted', 'retired'))
_UNSAFE_TEXT = re.compile(r'[\x00-\x1f\x7f-\x9f\u2028\u2029\u202a-\u202e\u2066-\u2069]')
_WIKI_PATH = re.compile(r'[^\x00-\x1f\x7f-\x9f\u2028\u2029\u202a-\u202e\u2066-\u2069]{1,256}\Z')


def _wiki_line(row) -> str | None:
    """One rendered activity line, or None for a record that is not a card write.

    The tail feeds an extractor prompt, so every field is type-, charset- and
    length-checked: a forged line cannot add prompt lines or control text.
    """
    outcome, path, title = row.get('outcome'), row.get('path'), row.get('title', '')
    if (outcome not in _WIKI_OUTCOMES or not isinstance(path, str)
            or not _WIKI_PATH.fullmatch(path) or path.startswith('/') or '..' in path.split('/')
            or not isinstance(title, str)):
        return None
    title = ' '.join(_UNSAFE_TEXT.sub(' ', title).split())[:200]
    return f"- {row['ts'][:19]}Z {outcome} {path} - {title}"


def wiki_tail(root, bound=_UNRESOLVED, limit: int = WIKI_TAIL_CHARS) -> str:
    """Recent wiki card writes as ``- <ts> <action> <path> - <title>`` lines.

    Only valid ``wiki`` events are rendered: recall, upgrade and prompt fields
    never reach the extractor prompt this feeds.
    """
    state_root = wiki_state_root(root, bound)
    if state_root is None:
        return ''
    try:
        dfd = open_state_dir(state_root)
    except (OSError, ValueError):
        return ''
    try:
        lines: list[str] = []
        for name in (NAME, GENERATIONS[0]):
            rendered = [line for line in (_wiki_line(row) for row in
                                          _parse(_read_file(dfd, name, WIKI_TAIL_WINDOW))
                                          if row.get('event') == 'wiki')
                        if line is not None]
            lines = rendered + lines
            if sum(len(line) + 1 for line in lines) >= limit:
                break
    finally:
        os.close(dfd)
    text = '\n'.join(lines) + ('\n' if lines else '')
    return text[-limit:]

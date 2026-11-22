"""Edit-time recall: the failure-silent tail of the PostToolUse ``posttool`` action.

The edit proof and the route-proof recheck finish first (``host_response``).
This tail then searches the selected view with the edit's own text, injects at
most two new cards as PostToolUse ``additionalContext`` (Claude, Codex) and
hands the verified pool to the post-output ``learning-capture`` child. Any
failure, proof change or exhausted budget answers ``{}``; it never raises and
never records a sync or provider wake.

Per-session state (cards already shown, edit-recall times) lives in the
install state root, independent of learning enrollment.
"""
from __future__ import annotations

from contextlib import contextmanager, redirect_stderr
import errno
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import time
import uuid

SWITCH = 'MEMVANE_EDIT_RECALL'
# Decision 16: the tail answers within 10 seconds of hook entry (hook 18 s).
EDIT_RECALL_SECONDS = 10.0
# Kept after the selector for the recheck, rendering and the answer.
SELECTOR_RESERVE_SECONDS = 1.0
MAX_QUERY_CHARS = 8192
MIN_EDIT_CHARS = 200
MAX_NEW_CARDS = 2
CARD_TOKEN_BUDGET = 600
# Claude Code replaces an additionalContext over 10,000 characters by a file path.
MAX_CONTEXT_CHARS = 9000
THROTTLE_SECONDS = 60
THROTTLE_PER_HOUR = 12
SESSION_TTL_SECONDS = 24 * 3600
SESSION_DIR = 'edit-recall-sessions'
MAX_SESSION_CARDS = 256
MAX_SESSION_BYTES = 64 * 1024
MAX_SWEEP = 32
POOL_LIMIT = 15
# The linked worktree proof of an edit outside the main checkout: two git
# calls per checkout, bounded well inside the tail's ten seconds.
CHECKOUT_PROOF_SECONDS = 1.0
MAX_PROVED_CHECKOUTS = 2
_HOSTS = ('claude', 'codex')
_SKIP_SEGMENTS = frozenset(('.memvane', '.auto-context', '.git', 'node_modules'))
_SKIP_NAMES = re.compile(r'(?:\.lock|-lock\.json|-lock\.ya?ml|\.min\.js|\.min\.css|\.map)\Z', re.I)
_BINARY_SUFFIXES = frozenset((
    '.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.ico', '.icns', '.tif', '.tiff', '.psd',
    '.pdf', '.zip', '.gz', '.tgz', '.bz2', '.xz', '.7z', '.rar', '.tar', '.jar', '.war',
    '.woff', '.woff2', '.ttf', '.otf', '.eot', '.mp3', '.mp4', '.mov', '.avi', '.wav', '.flac',
    '.ogg', '.webm', '.exe', '.dll', '.so', '.dylib', '.bin', '.o', '.a', '.class', '.pyc',
    '.wasm', '.sqlite', '.sqlite3', '.db', '.gguf', '.safetensors', '.pt', '.onnx', '.npy'))
_OPERATIONS = {'Write': 'write', 'Edit': 'edit', 'MultiEdit': 'multi_edit',
               'NotebookEdit': 'notebook_edit', 'apply_patch': 'apply_patch'}
_PATCH_HEADER = re.compile(r'^\*\*\* (Add|Update|Delete) File: (.+)$')
_PATCH_MOVE = re.compile(r'^\*\*\* Move to: (.+)$')
_ID = re.compile(r'[A-Za-z0-9._:-]{1,160}\Z')
_NOFOLLOW = getattr(os, 'O_NOFOLLOW', 0)
_DIRECTORY = getattr(os, 'O_DIRECTORY', 0)
_CLOEXEC = getattr(os, 'O_CLOEXEC', 0)

_HOOK_STARTED = None


def mark_hook_started(started: float) -> None:
    """Hook entry time (monotonic); the tail budget counts from it."""
    global _HOOK_STARTED
    _HOOK_STARTED = started


class Skip(Exception):
    """A fixed reason this edit gets no recall; never a path or text."""


# ------------------------------------------------------------------ query

def _rel(root: Path, current: Path, raw: str) -> PurePosixPath:
    return PurePosixPath((current / raw).resolve(strict=False).relative_to(root).as_posix())


def _skipped(rel: PurePosixPath, absolute: Path, decision: dict) -> str | None:
    """Path-only skip rule of one edited file, or None."""
    parts = rel.parts
    if not parts or any(part in _SKIP_SEGMENTS for part in parts):
        return 'skip_tool_path'
    if any(part.startswith('.') for part in parts):
        return 'skip_dot_path'
    name = parts[-1]
    if _SKIP_NAMES.search(name) or PurePosixPath(name).suffix.lower() in _BINARY_SUFFIXES:
        return 'skip_file_kind'
    for item in decision.get('collections') or ():
        if item.get('role') == 'wiki':
            try:
                absolute.relative_to(Path(item['sourceRoot']))
                return 'skip_wiki_card'
            except (KeyError, TypeError, ValueError):
                pass
    skips = (decision.get('recallSettings') or {}).get('skipPaths') or []
    from memvane.settings.api import excluded_source
    if excluded_source(rel.as_posix(), skips):
        return 'skip_paths'
    return None


def _patch_sections(patch: str):
    """``(path, added lines)`` per file section of an apply_patch body."""
    sections = []
    current = None
    for line in patch.splitlines():
        header = _PATCH_HEADER.match(line)
        if header:
            current = [header.group(2), []]
            sections.append(current)
            continue
        moved = _PATCH_MOVE.match(line)
        if moved and current is not None:
            current[0] = moved.group(1)
            continue
        if current is not None and line.startswith('+'):
            current[1].append(line[1:])
    return sections


def edit_scope(decision: dict, edit_root=None, skip_paths=()) -> dict:
    """Project root, collections and skip paths that judge an edited path.

    A linked worktree edit is judged against that worktree's project root
    (``edit_root``), with the main route's collection roots rebased onto it
    and the worktree's own ``skipPaths`` added to the main route's.
    """
    if edit_root is None:
        return decision
    main, root = Path(decision['projectRoot']), edit_root
    collections = []
    for item in decision.get('collections') or ():
        try:
            rebased = str(Path(root) / Path(item['sourceRoot']).relative_to(main))
        except (KeyError, TypeError, ValueError):
            continue
        collections.append(dict(item, sourceRoot=rebased))
    settings = dict(decision.get('recallSettings') or {})
    settings['skipPaths'] = list(dict.fromkeys([*(settings.get('skipPaths') or []), *skip_paths]))
    return {'projectRoot': root, 'collections': collections, 'recallSettings': settings}


# ------------------------------------------------------------------ checkout

def _proved_skip_paths(decision: dict, expected: Path, deadline: float):
    """The worktree's own ``skipPaths`` when ``expected`` is this project's linked worktree.

    The proof is the route's own: ``select_route`` classifies the folder as a
    linked worktree whose main route is the session's. None otherwise.
    """
    from memvane.host.host_response import _RECALL_IDENTITY
    from memvane.journal.api import select_route
    try:
        route = select_route(str(expected), action='posttool', deadline=deadline)
        main = route.get('mainRoute') if isinstance(route, dict) else None
        if (route.get('state') != 'worktree' or route.get('worktreeProjectRoot') != str(expected)
                or not isinstance(main, dict)
                or any(main.get(key) != decision.get(key) for key in _RECALL_IDENTITY)):
            return None
        return list(route['worktreeSettings']['skipPaths'])
    except (OSError, RuntimeError, ValueError, TypeError, KeyError, UnicodeError):
        return None


def edit_checkout(decision: dict, current: Path, inside: list, outside: list) -> dict:
    """The checkout an edit of a main-checkout session belongs to.

    Returns ``{'target', 'root', 'paths', 'skipPaths', 'skip', 'proofMs'}``.
    ``target`` is ``main`` (paths of the main checkout), ``linked`` (paths of
    one proved linked worktree of the same repository, ``root`` its project
    root), ``linked_unproved``, ``mixed`` or ``outside``; the last three carry
    the ``skip`` reason. A path is sorted by its nearest ``.git`` entry (stat
    only); git runs once per linked worktree, never for an ordinary checkout.
    With any main path only the main paths count; otherwise one proved
    worktree's paths do.
    """
    from memvane.kernel.paths import dotgit_shape, find_dotgit
    main = Path(decision['projectRoot'])
    mains, wanted, unproved = [], {}, False
    for raw, within in [*((raw, True) for raw in inside), *((raw, False) for raw in outside)]:
        absolute = (current / raw).resolve(strict=False)
        dotgit = find_dotgit(absolute.parent)
        top = dotgit.parent if dotgit is not None else None
        if within and (top is None or top == main or top in main.parents):
            mains.append(raw)
            continue
        shape = dotgit_shape(dotgit) if dotgit is not None else None
        if shape is None or shape[0] != 'worktree':
            # Inside: a nested clone or submodule stays a main path, as before.
            if within:
                mains.append(raw)
            continue
        expected = None
        if main == shape[1] or shape[1] in main.parents:
            expected = top / main.relative_to(shape[1])
            if absolute != expected and expected not in absolute.parents:
                expected = None
        if expected is None:
            unproved = unproved or within
            continue
        wanted.setdefault(str(expected), (expected, []))[1].append(raw)
    result = {'target': 'main', 'root': None, 'paths': mains, 'skipPaths': [], 'skip': None,
              'proofMs': None}
    if mains:
        return result
    if len(wanted) > MAX_PROVED_CHECKOUTS:
        return dict(result, target='mixed', skip='skip_mixed_checkout')
    began = time.monotonic()
    deadline = min(began + CHECKOUT_PROOF_SECONDS,
                   _budget_deadline(began) - SELECTOR_RESERVE_SECONDS)
    proved = []
    for root, (expected, paths) in wanted.items():
        skips = _proved_skip_paths(decision, expected, deadline)
        if skips is None:
            unproved = True
        else:
            proved.append((root, paths, skips))
    if wanted:
        result['proofMs'] = round((time.monotonic() - began) * 1000, 3)
    if len(proved) == 1:
        root, paths, skips = proved[0]
        return dict(result, target='linked', root=root, paths=paths, skipPaths=skips)
    if proved:
        return dict(result, target='mixed', skip='skip_mixed_checkout')
    if unproved:
        late = bool(wanted) and time.monotonic() >= deadline
        return dict(result, target='linked_unproved',
                    skip='skip_checkout_proof_timeout' if late else 'skip_outside_project')
    return dict(result, target='outside', skip='skip_outside_project')


def edit_query(decision: dict, payload: dict, current: Path, inside: list):
    """``(rels, text, operation, truncated)`` of a proved edit, or raise ``Skip``.

    ``text`` is what the edit added (Write content, Edit new string, each
    MultiEdit new string in order, NotebookEdit new source, apply_patch ``+``
    lines) for files that pass the path skip rules.
    """
    tool = payload.get('tool_name')
    details = payload.get('tool_input')
    operation = _OPERATIONS.get(tool)
    if operation is None or not isinstance(details, dict):
        raise Skip('skip_tool')
    root = Path(decision['projectRoot'])
    eligible = {}
    reasons = []
    for raw in inside:
        try:
            rel = _rel(root, current, raw)
        except (OSError, RuntimeError, ValueError):
            reasons.append('skip_outside_project')
            continue
        reason = _skipped(rel, root / rel, decision)
        if reason is None:
            eligible[raw] = rel
        else:
            reasons.append(reason)
    if not eligible:
        raise Skip(reasons[0] if reasons else 'skip_outside_project')
    parts = []
    if tool == 'apply_patch':
        patch = details.get('patch', details.get('command'))
        if not isinstance(patch, str):
            raise Skip('skip_tool')
        kept = set()
        for path, added in _patch_sections(patch):
            if path in eligible and added:
                kept.add(eligible[path])
                parts.append('\n'.join(added))
        rels = sorted(kept, key=str)
    else:
        if tool == 'Write':
            values = [details.get('content')]
        elif tool == 'Edit':
            values = [details.get('new_string')]
        elif tool == 'MultiEdit':
            edits = details.get('edits')
            values = [edit.get('new_string') for edit in edits] if isinstance(edits, list) else []
        else:
            values = [details.get('new_source')]
        parts = [value for value in values if isinstance(value, str) and value]
        rels = sorted(set(eligible.values()), key=str)
    text = '\n'.join(parts)
    if not rels or len(text.strip()) < MIN_EDIT_CHARS:
        raise Skip('skip_short_edit')
    truncated = len(text) > MAX_QUERY_CHARS
    return [rel.as_posix() for rel in rels], text[:MAX_QUERY_CHARS], operation, truncated


# ------------------------------------------------------------------ session state

def _session_key(session: str) -> str:
    return hashlib.sha256(session.encode('utf-8')).hexdigest()


def _open_dir(parent_fd, name, *, create):
    if create:
        try:
            os.mkdir(name, 0o700, dir_fd=parent_fd)
        except FileExistsError:
            pass
    fd = os.open(name, os.O_RDONLY | _DIRECTORY | _NOFOLLOW | _CLOEXEC, dir_fd=parent_fd)
    info = os.fstat(fd)
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077):
        os.close(fd)
        raise OSError(errno.EPERM, 'unsafe_session_dir')
    return fd


def _open_root(state_root):
    """The install state root: a real (symlink-free) owner directory, no group/other write."""
    path = str(state_root)
    if not os.path.isabs(path) or os.path.realpath(path) != path:
        raise OSError(errno.EPERM, 'unsafe_state_root')
    fd = os.open(path, os.O_RDONLY | _DIRECTORY | _NOFOLLOW | _CLOEXEC)
    info = os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        os.close(fd)
        raise OSError(errno.EPERM, 'unsafe_state_root')
    return fd


def _state_dir(state_root, *, create):
    """Owner-only ``<install state root>/edit-recall-sessions`` dir fd."""
    root_fd = _open_root(state_root)
    try:
        return _open_dir(root_fd, SESSION_DIR, create=create)
    finally:
        os.close(root_fd)


def _empty():
    from memvane.host import session_cards
    return session_cards.loaded(None)


def _load(fd) -> dict:
    data = b''
    os.lseek(fd, 0, os.SEEK_SET)
    while len(data) <= MAX_SESSION_BYTES:
        chunk = os.read(fd, 65536)
        if not chunk:
            break
        data += chunk
    try:
        row = json.loads(data.decode('utf-8')) if data else _empty()
    except (UnicodeError, ValueError):
        return _empty()
    from memvane.host import session_cards
    row = session_cards.loaded(row)
    row['edits'] = row['edits'][-THROTTLE_PER_HOUR:]
    return row


def _save(fd, row: dict) -> None:
    from memvane.host import session_cards
    row = {**session_cards.dumped(row), 'edits': row['edits'][-THROTTLE_PER_HOUR:], 'updated': time.time()}
    data = json.dumps(row, separators=(',', ':'), ensure_ascii=True).encode('utf-8')
    # A file past the bound reads as empty and idle, and the sweep removes it with the edit
    # throttle: the oldest pairs go until it fits (long non-ASCII paths take six bytes a character).
    while len(data) > MAX_SESSION_BYTES and row['cards']:
        drop = max(1, len(row['cards']) // 4)
        for key in ('cards', 'at', 'by'):
            row[key] = row[key][drop:] if len(row[key]) >= drop else []
        data = json.dumps(row, separators=(',', ':'), ensure_ascii=True).encode('utf-8')
    os.lseek(fd, 0, os.SEEK_SET)
    os.ftruncate(fd, 0)
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


@contextmanager
def session_state(decision: dict, session: str):
    """Exclusive, non-blocking hold of one session's state file.

    Yields ``(row, save)`` or raises ``Skip('session_busy')`` when another
    hook holds it (parallel posttool calls: one recall, the others skip).
    """
    from memvane.diagnostics.api import state_root_for_decision
    state_root = state_root_for_decision(decision)
    if state_root is None:
        raise Skip('session_state_unavailable')
    try:
        dfd = _state_dir(state_root, create=True)
    except (OSError, ValueError):
        raise Skip('session_state_unavailable') from None
    try:
        try:
            fd = os.open(_session_key(session) + '.json',
                         os.O_RDWR | os.O_CREAT | _NOFOLLOW | _CLOEXEC, 0o600, dir_fd=dfd)
        except OSError:
            raise Skip('session_state_unavailable') from None
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
                raise Skip('session_state_unavailable')
            if info.st_mode & 0o077:
                os.fchmod(fd, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise Skip('session_busy') from None
            from memvane.host import session_cards
            row = _load(fd)
            # A compaction or clear seen under this lock starts the pairs over; its
            # markers go once this holder has written the record.
            markers = session_cards.apply_resets(dfd, _session_key(session) + '.json', row)
            yield row, lambda: (_save(fd, row), session_cards.clear_markers(dfd, markers))
        finally:
            os.close(fd)
        _sweep(dfd)
        session_cards.sweep_markers(dfd, time.time())
    finally:
        os.close(dfd)


def _sweep(dfd) -> None:
    """Remove at most a few session files idle for a day; best effort."""
    try:
        now = time.time()
        for name in sorted(os.listdir(dfd))[:MAX_SWEEP * 4]:
            if not re.fullmatch(r'[0-9a-f]{64}\.json', name):
                continue
            try:
                fd = os.open(name, os.O_RDWR | _NOFOLLOW | _CLOEXEC, dir_fd=dfd)
            except OSError:
                continue
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                updated = _updated(fd)
                # A future stamp (clock moved back) is not idle.
                if updated is not None and 0 <= now - updated and now - updated > SESSION_TTL_SECONDS:
                    os.unlink(name, dir_fd=dfd)
            except OSError:
                pass
            finally:
                os.close(fd)
    except OSError:
        pass


def _updated(fd):
    data = os.read(fd, MAX_SESSION_BYTES + 1)
    try:
        value = json.loads(data.decode('utf-8')).get('updated')
    except (UnicodeError, ValueError, AttributeError):
        return 0.0  # unreadable: idle
    return float(value) if type(value) in (int, float) else 0.0


def throttled(row: dict, now: float) -> bool:
    """60 s between edit recalls and 12 per hour, per session."""
    edits = [value for value in row['edits'] if now - 3600 < value <= now + 60]
    row['edits'] = edits
    if edits and now - max(edits) < THROTTLE_SECONDS:
        return True
    return len(edits) >= THROTTLE_PER_HOUR


def remember_cards(decision: dict, payload: dict, rows) -> None:
    """Record cards a prompt recall injected (``session_cards.remember``); its state, never raises."""
    from memvane.host import session_cards
    return session_cards.remember(decision, payload, rows)


# ------------------------------------------------------------------ cards

def _self_cited(row: dict, decision: dict, edited: set) -> bool:
    """The card's compiler-owned ``sourceRevisions`` names an edited file."""
    try:
        from memvane.wiki.card.api import card_revisions
        roots = {item['name']: Path(item['sourceRoot'])
                 for item in decision.get('collections') or () if isinstance(item, dict)}
        project = Path(decision['projectRoot'])
        for revision in card_revisions(row['body'].replace('\r\n', '\n')):
            path = revision.get('path')
            if not isinstance(path, str):
                continue
            base = roots.get(revision.get('collection'), project)
            if os.path.normpath(str(base / path)) in edited:
                return True
    except Exception:
        return False
    return False


def _token_estimate(text: str) -> float:
    return sum(1.0 if ord(char) > 127 else 0.25 for char in text)


def _summary(row: dict) -> str:
    from memvane.wiki.card import api as wiki_card
    normalized = row['body'].replace('\r\n', '\n').replace('\r', '\n')
    end = normalized.find('\n---\n', 4)
    if not normalized.startswith('---\n') or end < 0:
        return ''
    summary, _complete = wiki_card.extract_card_body(normalized, end + 5)
    return summary or ''


def _fit_chars(text: str, budget: float) -> int:
    used = 0.0
    for index, char in enumerate(text):
        used += 1.0 if ord(char) > 127 else 0.25
        if used > budget:
            return index
    return len(text)


def choose_cards(selected: list, decision: dict, seen: set, edited: set, limit: int):
    """``(rows, body limits, repeated, self cited)`` to inject from ``selected``."""
    rows, limits = [], {}
    repeated = cited = 0
    budget = float(CARD_TOKEN_BUDGET)
    for row in selected:
        if len(rows) >= limit:
            break
        if (row['path'], row['hash']) in seen:
            repeated += 1
            continue
        if _self_cited(row, decision, edited):
            cited += 1
            continue
        summary = _summary(row)
        cost = _token_estimate(summary)
        if rows and cost > budget:
            continue
        allowed = _fit_chars(summary, budget)
        if allowed < 1 and summary:
            continue
        budget -= min(cost, _token_estimate(summary[:allowed]))
        rows.append(row)
        limits[row['path']] = max(allowed, 1)
    return rows, limits, repeated, cited


# ------------------------------------------------------------------ tail

class _Trace:
    """A ``RecallTrace`` that keeps the edit recall's fields for its own event."""

    def __init__(self):
        self.fields_ = {}
        self.notes = []
        self.final_ = None

    def fields(self, **fields) -> None:
        self.fields_.update(fields)

    def note(self, reason: str) -> None:
        if len(self.notes) < 16:
            self.notes.append(reason)

    def final(self, reason: str) -> None:
        self.final_ = reason


def _cards(rows) -> list:
    return [{'path': row['path'], 'sha': row['hash']} for row in rows]


def _budget_deadline(now: float) -> float:
    """Hook entry + 10 s, inside the armed hook deadline.

    Without an armed hook budget (an in-process host) the tail counts from
    its own start; a hook entry time is used only together with that budget.
    """
    from memvane.kernel import hook_budget
    hook = hook_budget.deadline()
    if hook == float('inf') or _HOOK_STARTED is None:
        return now + EDIT_RECALL_SECONDS
    return min(_HOOK_STARTED + EDIT_RECALL_SECONDS, hook - SELECTOR_RESERVE_SECONDS)


def serve(host: str, decision: dict, proof, payload: dict, current: Path, inside: list, *,
          linked=None, successful=True, edit_root=None, skip_paths=()):
    """``(response, capture entry or None)`` of the posttool tail; never raises.

    ``decision`` is the selected main route. ``edit_root`` is the project
    root of the proved linked worktree the edited files lie in (path rules
    and relative paths count from it; None for the main checkout) and
    ``skip_paths`` that worktree's own skip paths. ``linked`` is the linked
    worktree route of a session opened there: search reads its own view, and
    the pool is a case of the main project only while every card equals the
    main head's.
    """
    from memvane.kernel.hook_budget import HookDeadlineExceeded
    began = time.monotonic()
    if linked is not None:
        edit_root = linked['worktreeProjectRoot']
    event = {'outcome': None, 'reason': None,
             'editTarget': 'main' if edit_root is None else 'linked'}
    try:
        with redirect_stderr(io.StringIO()):
            return _serve(host, decision, proof, payload, current, inside, linked, successful,
                          began, event, edit_root, skip_paths)
    except Skip as exc:
        _trace_skip(str(exc))
        return {}, None
    except (Exception, HookDeadlineExceeded) as exc:
        from memvane.host.host_response import _refusal_code
        event.update(outcome='refused', reason=_refusal_code(exc) if isinstance(exc, Exception)
                     else 'hook_budget_exhausted')
        _emit(host, decision, payload, event, began)
        return {}, None


def _trace_skip(reason: str) -> None:
    from memvane.host.host_response import _trace_fields
    _trace_fields(editRecall=reason)


def _serve(host, decision, proof, payload, current, inside, linked, successful, began, event,
           edit_root, skip_paths):
    if host not in _HOSTS:
        raise Skip('skip_host')
    if os.environ.get(SWITCH, '').strip().lower() == 'off':
        raise Skip('skip_switch_off')
    if not successful:
        raise Skip('skip_failed_edit')
    from memvane.diagnostics.api import session_id
    session = session_id(payload)
    if session is None:
        raise Skip('skip_no_session')
    rels, text, operation, truncated = edit_query(edit_scope(decision, edit_root, skip_paths),
                                                  payload, current, inside)
    # Card provenance names main-checkout paths; a linked edit maps onto them.
    edited = {os.path.normpath(str(Path(decision['projectRoot']) / rel)) for rel in rels}
    deadline = _budget_deadline(began)
    if time.monotonic() >= deadline - SELECTOR_RESERVE_SECONDS:
        raise Skip('skip_no_budget')
    request_id = str(uuid.uuid4())
    event['request_id'] = request_id
    with session_state(decision, session) as (row, save):
        now = time.time()
        if throttled(row, now):
            raise Skip('skip_throttled')
        row['edits'].append(now)
        save()
        query = (' '.join(rels) + '\n' + text)[:MAX_QUERY_CHARS]
        trace = _Trace()
        from memvane.recall.api import recall_scope, serve_endpoint_recall, set_edit_recall
        project = decision['projectRoot']
        with recall_scope():
            set_edit_recall(deadline=deadline, selector_deadline=deadline - SELECTOR_RESERVE_SECONDS)
            answer = serve_endpoint_recall(
                host, {'cwd': project, 'prompt': query}, cwd=project, trace=trace,
                route_decision=decision, main_proof=proof if linked is None else None,
                linked=linked)
        event['trace'] = trace
        if time.monotonic() > deadline:
            event.update(outcome='budget_exceeded', reason='edit_recall_budget')
            _emit(host, decision, payload, event, began)
            return {}, None
        reason = trace.fields_.get('selection')
        pool = list(answer.capture[1]) if answer.capture is not None else []
        if reason == 'selected':
            selection, selected, limit = 'selector', list(answer.capture[2]), MAX_NEW_CARDS
        elif reason == 'ranked':
            selection, selected, limit = 'default_rank', pool[:1], 1
        else:
            selection, selected, limit = 'none', [], 0
        from memvane.host import session_cards
        by, event['agentScoped'] = session_cards.edit_context(host, payload)
        seen = session_cards.seen(row, by, host)
        rows, limits, repeated, cited = choose_cards(selected, decision, seen, edited, limit)
        context = _render(answer, rows, limits)
        if context is None:
            rows = []
        if rows:
            event['injectedBy'] = by
            session_cards.record(row, [(item['path'], item['hash']) for item in rows], by)
            save()
    event.update(outcome='served' if rows else 'empty',
                 reason=trace.final_ or ('injected' if rows else 'no_new_cards'),
                 cards=_cards(rows), selected=_cards(selected), selection=selection,
                 pool=len(pool), repeated=repeated, selfCited=cited, operation=operation,
                 files=len(rels), queryChars=len(query), truncated=truncated)
    _emit(host, decision, payload, event, began)
    response = ({'hookSpecificOutput': {'hookEventName': 'PostToolUse',
                                        'additionalContext': context}} if rows else {})
    capture = None
    if answer.capture is not None and pool:
        capture_payload = {
            'cwd': decision['projectRoot'], 'session_id': session,
            'context_origin': 'tool_edit', 'prompt': text,
            'edit_event': {
                'path_sha256': hashlib.sha256('\n'.join(rels).encode('utf-8')).hexdigest(),
                'operation': operation, 'session_id': session,
                'tool_use_id': _tool_use_id(payload), 'truncated': truncated,
                'injected_ids': [item['path'] for item in rows]},
            '_memvane_request_id': request_id, '_memvane_edit_paths': rels,
            '_memvane_edit_target': event['editTarget']}
        if edit_root is not None:
            # The capture child proves this root again before it reads git there.
            capture_payload['_memvane_edit_root'] = str(edit_root)
        if linked is not None:
            # The pool is the worktree's own view: a case only while it equals the main head.
            capture_payload['_memvane_view'] = 'linked'
        capture = (host, decision, capture_payload, answer.capture[0], pool[:POOL_LIMIT],
                   selected)
    return response, capture


def _tool_use_id(payload: dict) -> str:
    value = payload.get('tool_use_id')
    return value if isinstance(value, str) and _ID.fullmatch(value) else ''


def _render(answer, rows, limits):
    if not rows:
        return None
    from memvane.host.host_response import render_context
    paths = {row['path'] for row in rows}
    context = render_context(
        answer.decision, rows, limit=len(rows),
        ranking='laya_selection' if answer.ranking == 'laya_selection' else 'hybrid_rrf',
        title_only_paths=answer.title_only_paths.intersection(paths),
        source_locations={path: spans for path, spans in answer.source_locations.items()
                          if path in paths},
        allow_mixed_generation=answer.allow_mixed_generation, body_limits=limits)
    context = ('[memvane] edit recall: wiki cards related to the text just written, '
               'not shown earlier in this session.\n' + context)
    return context if len(context) <= MAX_CONTEXT_CHARS else None


def _emit(host, decision, payload, event, began) -> None:
    try:
        from memvane.diagnostics import api as event_log
        trace = event.pop('trace', None)
        fields = {key: value for key, value in event.items() if key not in ('outcome', 'reason')}
        if trace is not None:
            measured = dict(trace.fields_)
            measured['selectorReason'] = measured.pop('selection', None)
            fields = {**measured, **fields}
            if trace.notes:
                fields['notes'] = trace.notes
        started = _HOOK_STARTED
        if started is not None:
            fields['hookMs'] = round((time.monotonic() - started) * 1000, 3)
        event_log.emit('recall.edit', outcome=event['outcome'], reason=event['reason'],
                       ms=event_log.elapsed_ms(began), decision=decision, host=host,
                       session=event_log.session_id(payload), **fields)
    except Exception:
        pass

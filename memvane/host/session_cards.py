"""Which cards a session already holds, per context, and when that record starts over.

The session file of ``edit_recall`` (``<install state root>/edit-recall-sessions/
<sha256(session id)>.json``) lists the injected ``[path, blob sha]`` pairs.
Beside them it keeps ``seq`` (recalls of this session that injected and
recorded cards), ``at`` (each pair's ``seq`` when injected) and ``by`` (the
context it was injected into):

    p         a prompt recall (the main context)
    e         an edit recall of the main context
    e:<hash>  an edit recall of one subagent (hash of the host's agent id)
    e?        an edit recall whose context the host did not say

A card counts as seen only in the context that holds it. An entry whose
context can lose it without a signal (every edit entry: the host may drop old
tool output; every entry of a host with no compaction signal) counts only for
``WINDOW`` recalls after its injection. A compaction or clear is a marker file
``<session file>.reset.<nonce>`` written without the lock: a reader that sees
one excludes nothing, a writer drops the pairs it found under its exclusive
lock and removes the markers it saw.

Nothing here raises into a recall: a missing, busy or damaged record excludes
nothing, which repeats a card once at worst.
"""
from __future__ import annotations

import fcntl
import hashlib
import os
import re
import stat
import time

PROMPT, EDIT, UNKNOWN = 'p', 'e', 'e?'
# Hosts whose SessionStart names a compaction or clear (``source``).
SIGNAL_HOSTS = frozenset(('claude',))
RESET_SOURCES = frozenset(('compact', 'clear'))
SESSION_SOURCES = frozenset(('startup', 'resume', 'clear', 'compact'))
# Recalls (that injected cards) an entry without a signal stays excluded for.
WINDOW = 20
MAX_CARDS = 256
MARKER_TTL_SECONDS = 24 * 3600
MAX_MARKERS = 16
WRITE_ATTEMPTS = 3
WRITE_RETRY_SECONDS = 0.05
# The wait between write tries; a name of its own so a check can count these waits alone.
_sleep = time.sleep
_MARKER = re.compile(r'[0-9a-f]{64}\.json\.reset\.[0-9a-f]{16}')
_SHA = re.compile(r'[0-9a-f]{64}')
_BY = re.compile(r'p|e|e\?|e:[0-9a-f]{8}')
_AGENT = re.compile(r'[A-Za-z0-9_.:-]{1,128}')


def _pair(card) -> bool:
    if not (isinstance(card, list) and len(card) == 2 and all(isinstance(item, str) for item in card)):
        return False
    path, sha = card
    try:
        path.encode('utf-8')
    except UnicodeError:
        return False
    # The store's relative-path rule: a pair it would refuse never reaches a search.
    return (bool(_SHA.fullmatch(sha)) and 0 < len(path) <= 1024 and not path.startswith('/')
            and not any(char in path for char in '\x00\r\n\\')
            and not any(part == '' or part.startswith('.') for part in path.split('/')))


def loaded(row) -> dict:
    """The validated record of a parsed session file (an unreadable one is empty).

    A record without ``at``/``by`` of the cards' length (an older bundle wrote
    it) reads every pair as injected now into an unknown edit context.
    """
    empty = {'v': 1, 'cards': [], 'edits': [], 'seq': 0, 'at': [], 'by': []}
    if (not isinstance(row, dict) or not isinstance(row.get('cards'), list)
            or not isinstance(row.get('edits'), list)):
        return empty
    seq = row.get('seq')
    seq = seq if type(seq) is int and 0 <= seq < 1 << 40 else 0
    at, by = row.get('at'), row.get('by')
    known = (isinstance(at, list) and isinstance(by, list) and len(at) == len(by) == len(row['cards'])
             and all(type(value) is int and 0 <= value <= seq for value in at)
             and all(isinstance(value, str) and _BY.fullmatch(value) for value in by))
    cards, ats, bys = [], [], []
    for index, card in enumerate(row['cards']):
        if _pair(card):
            cards.append(card)
            ats.append(at[index] if known else seq)
            bys.append(by[index] if known else UNKNOWN)
    edits = [value for value in row['edits'] if type(value) in (int, float)]
    return {'v': 1, 'cards': cards[-MAX_CARDS:], 'edits': edits, 'seq': seq,
            'at': ats[-MAX_CARDS:], 'by': bys[-MAX_CARDS:]}


def dumped(row: dict) -> dict:
    """The record as written: the newest ``MAX_CARDS`` pairs with their ``at`` and ``by``."""
    return {'v': 1, 'cards': row['cards'][-MAX_CARDS:], 'seq': row.get('seq', 0),
            'at': row.get('at', [])[-MAX_CARDS:], 'by': row.get('by', [])[-MAX_CARDS:]}


def edit_context(host: str, payload: dict):
    """``(by, agent scoped)`` of an edit hook: whose context its injection lands in.

    A subagent's edit hook carries the parent's session id; only an agent id
    in the payload tells them apart. A host without subagents edits in the
    main context.
    """
    agent = payload.get('agent_id') or payload.get('agentId')
    if isinstance(agent, str) and _AGENT.fullmatch(agent):
        return 'e:' + hashlib.sha256(agent.encode('utf-8')).hexdigest()[:8], True
    return (UNKNOWN if host in SIGNAL_HOSTS else EDIT), False


def seen(row: dict, context: str, host: str) -> set:
    """The ``(path, sha)`` pairs that count as already shown in ``context``."""
    if context == PROMPT:
        holds = (PROMPT, EDIT)
    elif context in (EDIT, UNKNOWN):
        holds = (PROMPT, EDIT, UNKNOWN)
    else:
        holds = (context,)
    seq, answer = row.get('seq', 0), set()
    for card, at, by in zip(row['cards'], row.get('at', ()), row.get('by', ())):
        if by not in holds:
            continue
        windowed = by != PROMPT or host not in SIGNAL_HOSTS
        if not windowed or seq - at < WINDOW:
            answer.add((card[0], card[1]))
    return answer


def record(row: dict, pairs, by: str) -> None:
    """Count one recall that injected ``pairs`` into context ``by``."""
    if not pairs:
        return
    row['seq'] = seq = row.get('seq', 0) + 1
    fresh = {(path, sha) for path, sha in pairs}
    kept = [(card, at, held) for card, at, held in zip(row['cards'], row.get('at', ()), row.get('by', ()))
            if not (held == by and (card[0], card[1]) in fresh)]
    kept += [([path, sha], seq, by) for path, sha in pairs]
    row['cards'] = [item[0] for item in kept]
    row['at'] = [item[1] for item in kept]
    row['by'] = [item[2] for item in kept]


# ------------------------------------------------------------------ reset markers

def _markers(dfd, name: str) -> list:
    prefix = name + '.reset.'
    try:
        return sorted(item for item in os.listdir(dfd)
                      if item.startswith(prefix) and _MARKER.fullmatch(item))[:MAX_MARKERS]
    except OSError:
        return []


def apply_resets(dfd, name: str, row: dict) -> list:
    """Drop the pairs of a session whose reset markers exist; the markers to remove after the save.

    Called under the session's exclusive lock. A marker written after this
    listing has another nonce and stays for the next writer.
    """
    found = _markers(dfd, name)
    if found:
        row['cards'], row['at'], row['by'] = [], [], []
    return found


def clear_markers(dfd, markers) -> None:
    for marker in markers:
        try:
            os.unlink(marker, dir_fd=dfd)
        except OSError:
            pass  # Unchecked: a marker left behind resets the session once more (a repeat, never a miss).


def mark_reset(decision: dict, session: str) -> bool:
    """Leave a reset marker for ``session`` without taking its lock; whether one was written."""
    from memvane.diagnostics.api import state_root_for_decision
    from memvane.host import edit_recall
    state_root = state_root_for_decision(decision)
    if state_root is None:
        return False
    try:
        dfd = edit_recall._state_dir(state_root, create=True)
    except (OSError, ValueError):
        return False
    try:
        name = edit_recall._session_key(session) + '.json.reset.' + os.urandom(8).hex()
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | edit_recall._NOFOLLOW | edit_recall._CLOEXEC,
                     0o600, dir_fd=dfd)
        os.close(fd)
        return True
    except OSError:
        return False
    finally:
        os.close(dfd)


def sweep_markers(dfd, now: float) -> None:
    """Remove a few reset markers older than a day (their session never wrote again); best effort."""
    try:
        names = [item for item in sorted(os.listdir(dfd)) if _MARKER.fullmatch(item)][:MAX_MARKERS * 8]
    except OSError:
        return
    for name in names:
        try:
            info = os.stat(name, dir_fd=dfd, follow_symlinks=False)
            if stat.S_ISREG(info.st_mode) and 0 <= now - info.st_mtime and now - info.st_mtime > MARKER_TTL_SECONDS:
                os.unlink(name, dir_fd=dfd)
        except OSError:
            continue


# ------------------------------------------------------------------ prompt recall

def exclusions(decision: dict, payload: dict, host: str):
    """``(pairs, state)`` a prompt recall excludes from its search; never raises.

    ``state``: ``ok``, ``none`` (no session id or no record yet), ``busy`` (an
    edit recall holds the record), ``reset_pending`` (a compaction or clear is
    not applied yet) or ``unavailable``. Every state but ``ok`` excludes nothing.
    """
    try:
        from memvane.diagnostics.api import session_id, state_root_for_decision
        from memvane.host import edit_recall
        session = session_id(payload)
        state_root = state_root_for_decision(decision)
        if session is None or state_root is None:
            return [], 'none'
        try:
            dfd = edit_recall._state_dir(state_root, create=False)
        except FileNotFoundError:
            return [], 'none'
        try:
            name = edit_recall._session_key(session) + '.json'
            if _markers(dfd, name):
                return [], 'reset_pending'
            try:
                fd = os.open(name, os.O_RDONLY | edit_recall._NOFOLLOW | edit_recall._CLOEXEC, dir_fd=dfd)
            except FileNotFoundError:
                return [], 'none'
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
                    return [], 'unavailable'
                try:
                    fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
                except OSError:
                    return [], 'busy'
                row = edit_recall._load(fd)
            finally:
                os.close(fd)
            if _markers(dfd, name):
                return [], 'reset_pending'
        finally:
            os.close(dfd)
        return sorted([path, sha] for path, sha in seen(row, PROMPT, host))[:MAX_CARDS], 'ok'
    except Exception:  # noqa: BLE001 - the record never decides whether a recall answers
        return [], 'unavailable'


def remember(decision: dict, payload: dict, rows, by: str = PROMPT) -> str:
    """Record the cards a prompt recall injected: ``ok``, ``none`` or ``write_skipped``; never raises."""
    try:
        from memvane.diagnostics.api import session_id
        from memvane.host import edit_recall
        session = session_id(payload)
        if session is None or not rows:
            return 'none'
        for attempt in range(WRITE_ATTEMPTS):
            try:
                with edit_recall.session_state(decision, session) as (row, save):
                    record(row, [(item['path'], item['hash']) for item in rows], by)
                    save()
                return 'ok'
            except edit_recall.Skip as exc:
                if str(exc) != 'session_busy':
                    return 'write_skipped'
            if attempt + 1 < WRITE_ATTEMPTS:
                _sleep(WRITE_RETRY_SECONDS)
        return 'write_skipped'
    except BaseException as exc:  # noqa: BLE001 - never alters the recall answer
        from memvane.kernel.hook_budget import HookDeadlineExceeded
        if not isinstance(exc, (Exception, HookDeadlineExceeded)):
            raise
        return 'write_skipped'

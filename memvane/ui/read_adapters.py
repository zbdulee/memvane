"""Read-only DTOs of the dashboard built from the registry, route and diagnostics APIs.

Nothing here writes, locks or starts a process. A registry row is a lookup
key only: every install or target the API serves is proved again through
``journal.api.select_route`` (install id, route state, scope id) before any
of its files is read. Overview stays free of git processes: main checkouts
are proved without the linked-worktree probe, worktree rows are listed from
the registry, and branch names come from ``<gitdir>/HEAD`` (no-follow,
4 KiB).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import re
import stat
import time

from memvane.ui.types import (BANNER_CODES, BANNER_PREFIXES, CARD_SCAN_LIMIT, HEAD_LIMIT, INSTALL_ID,
                              MAX_HOURS, MAX_RECALLS, parse_target_id, target_id, worktree_hash)

PROVED_STATES = ('candidate', 'worktree', 'preview')
REGISTRY_TROUBLE = ('absent', 'busy', 'recovery_required')
CARD_STATUSES = ('verified', 'generated')
WORKTREE_PROOF_SECONDS = 2.0
STATUS_LINE = re.compile(rb'^status:[ \t]*"?([A-Za-z_-]+)"?[ \t]*$', re.M)
SHA_HEAD = re.compile(r'[0-9a-f]{40}(?:[0-9a-f]{24})?')
BRANCH = re.compile(r'ref: refs/heads/(.+)')


class TargetError(LookupError):
    """A target or install cannot be served; ``status`` is the HTTP status, the message a code."""

    def __init__(self, code, status=404):
        super().__init__(code)
        self.status = status


# ----------------------------------------------------------------- reading


def _open_nofollow(path, flags=os.O_RDONLY):
    return os.open(path, flags | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))


def _read_small(path, limit):
    """Up to ``limit`` bytes of a regular file reached without following its last component."""
    fd = _open_nofollow(path)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError('not a regular file')
        return os.read(fd, limit + 1)
    finally:
        os.close(fd)


def git_dir(target_root):
    """The git directory of a checkout (a ``.git`` directory or a ``gitdir:`` file), or None."""
    dotgit = Path(target_root) / '.git'
    info = os.lstat(dotgit)
    if stat.S_ISDIR(info.st_mode):
        return dotgit
    if not stat.S_ISREG(info.st_mode):
        raise OSError('unsupported .git entry')
    raw = _read_small(dotgit, HEAD_LIMIT)
    if len(raw) > HEAD_LIMIT or not raw.startswith(b'gitdir:'):
        raise OSError('unsupported .git file')
    pointer = Path(os.fsdecode(raw[len(b'gitdir:'):].split(b'\n', 1)[0].strip()))
    return pointer if pointer.is_absolute() else dotgit.parent / pointer


def read_branch(target_root):
    """``<name>``, ``detached@<short7>``, ``non-git`` or ``unknown`` from ``<gitdir>/HEAD``."""
    try:
        directory = git_dir(target_root)
    except FileNotFoundError:
        return 'non-git'
    except (OSError, ValueError):
        return 'unknown'
    try:
        raw = _read_small(directory / 'HEAD', HEAD_LIMIT)
    except (OSError, ValueError):
        return 'unknown'
    if len(raw) > HEAD_LIMIT:
        return 'unknown'
    try:
        text = raw.decode('utf-8').strip()
    except UnicodeDecodeError:
        return 'unknown'
    match = BRANCH.fullmatch(text)
    if match and match.group(1) and not any(ord(ch) < 32 for ch in match.group(1)):
        return match.group(1)
    if SHA_HEAD.fullmatch(text):
        return 'detached@' + text[:7]
    return 'unknown'


def card_counts(target_root, *, limit=CARD_SCAN_LIMIT):
    """``{verified, generated, other, truncated}`` of the checkout's ``.memvane/wiki`` cards."""
    counts = {'verified': 0, 'generated': 0, 'other': 0, 'truncated': False}
    root = Path(target_root) / '.memvane' / 'wiki'
    seen = 0
    stack = [root]
    while stack:
        directory = stack.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError:
            continue
        for entry in entries:
            if entry.is_dir(follow_symlinks=False):
                if not entry.name.startswith('.'):
                    stack.append(Path(entry.path))
                continue
            if not entry.name.endswith('.md') or not entry.is_file(follow_symlinks=False):
                continue
            seen += 1
            if seen > limit:
                counts['truncated'] = True
                return counts
            try:
                head = _read_small(entry.path, 4096)
            except OSError:
                continue
            front = head.split(b'\n---', 1)[0] if head.startswith(b'---') else b''
            found = STATUS_LINE.search(front)
            status = found.group(1).decode('ascii') if found else None
            counts[status if status in CARD_STATUSES else 'other'] += 1
    return counts


def pending_cards(project_root):
    """Queued compile and verify work of the install's wiki ledger (main checkout), or None."""
    from memvane.wiki.pipeline import api as pipeline
    try:
        summary = pipeline.pipeline_summary(project_root)
    except Exception:  # noqa: BLE001 - shown as unknown, never raised
        return None
    if not isinstance(summary, dict):
        return None
    tables = summary.get('tables')
    if summary.get('ledger') != 'ok' or not isinstance(tables, dict):
        return {'ledger': summary.get('ledger'), 'pending': 0}
    pending = 0
    for name in ('compileJobs', 'verifyJobs'):
        states = (tables.get(name) or {}).get('byState') or {}
        pending += sum(count for state, count in states.items()
                       if state in ('queued', 'claimed', 'deferred') and isinstance(count, int))
    inbox = summary.get('inbox') or {}
    pending += inbox.get('pending', 0) if isinstance(inbox.get('pending'), int) else 0
    return {'ledger': 'ok', 'pending': pending}


# ------------------------------------------------------------ route proof


def _registry(home=None, *, reconcile=False, now=None):
    from memvane.registry import api as registry
    try:
        return registry.snapshot(home=home, reconcile=reconcile, now=now)
    except Exception as exc:  # noqa: BLE001 - an unreadable registry is a banner, not a crash
        return {'status': 'recovery_required', 'reason': type(exc).__name__, 'rows': [], 'scans': {}}


def _prove(target_root, install_id, *, worktree):
    """``(decision, state root)`` when the route of ``target_root`` proves ``install_id``."""
    from memvane.diagnostics import api as diagnostics
    from memvane.journal import api as journal
    try:
        if worktree:
            decision = journal.select_route(str(target_root), action='recall', linked=True,
                                            deadline=time.monotonic() + WORKTREE_PROOF_SECONDS)
        else:
            decision = journal.select_route(str(target_root), action='recall', linked=False)
    except Exception:  # noqa: BLE001 - unproved
        return None, None
    if not isinstance(decision, dict) or decision.get('state') not in PROVED_STATES:
        return None, None
    if (decision.get('state') == 'worktree') != worktree:
        return None, None
    if diagnostics.install_of(decision) != install_id:
        return None, None
    return decision, diagnostics.state_root_for_decision(decision)


def _rows(snapshot):
    rows = snapshot.get('rows') if isinstance(snapshot, dict) else None
    return [row for row in rows or () if isinstance(row, dict) and isinstance(row.get('targetRoot'), str)
            and isinstance(row.get('installId'), str) and INSTALL_ID.fullmatch(row['installId'])]


def resolve_target(value, *, home=None, snapshot=None):
    """The proved target of a target id: ``{targetId, installId, kind, targetRoot, projectRoot, stateRoot, routeState}``."""
    parsed = parse_target_id(value)
    if parsed is None:
        raise TargetError('target_id_invalid', 400)
    install, wt_hash = parsed
    snapshot = _registry(home) if snapshot is None else snapshot
    for row in _rows(snapshot):
        if row['installId'] != install or row.get('status') != 'active':
            continue
        if wt_hash is None and row.get('kind') == 'main' or (
                wt_hash is not None and row.get('kind') == 'worktree'
                and worktree_hash(row['targetRoot']) == wt_hash):
            decision, state_root = _prove(row['targetRoot'], install, worktree=wt_hash is not None)
            if decision is None or state_root is None:
                raise TargetError('target_unproved', 409)
            main_route = decision.get('mainRoute') if isinstance(decision.get('mainRoute'), dict) else {}
            scope = decision.get('scopeId') or main_route.get('scopeId')
            recorded = row.get('incarnation')
            # The registry row records the scope id it was proved with (``incarnation``).
            if isinstance(recorded, str) and isinstance(scope, str) and scope != recorded:
                raise TargetError('target_scope_mismatch', 409)
            return {'targetId': value, 'installId': install, 'kind': row['kind'],
                    'targetRoot': row['targetRoot'], 'projectRoot': row.get('projectRoot'),
                    'stateRoot': str(state_root), 'routeState': decision.get('state')}
    raise TargetError('target_unknown', 404)


# ---------------------------------------------------------------- events


def _now(now=None):
    return now if isinstance(now, datetime) else datetime.now(timezone.utc)


def _hours(value, default=24.0):
    try:
        hours = float(value) if value is not None else default
    except (TypeError, ValueError):
        raise TargetError('hours_invalid', 400) from None
    if not 0 < hours <= MAX_HOURS:
        raise TargetError('hours_invalid', 400)
    return hours


def _raw_events(state_root):
    from memvane.diagnostics import api as diagnostics
    try:
        raw = diagnostics.build_progress_events(state_root)
    except Exception:  # noqa: BLE001 - unreadable log reads as empty and is flagged
        return None
    return raw if isinstance(raw, list) else None


def _warnings(rows):
    found = {}
    for row in rows:
        codes = [row.get('reason'), *(row.get('notes') if isinstance(row.get('notes'), list) else ())]
        for code in codes:
            if isinstance(code, str) and (code in BANNER_CODES or code.startswith(BANNER_PREFIXES)):
                found[code] = found.get(code, 0) + 1
    return found


def _verdict(lines):
    states = {line.get('status') for line in lines if isinstance(line, dict)}
    if 'over' in states:
        return 'over'
    return 'ok' if 'ok' in states else 'no-data'


def install_digest(state_root, *, now=None):
    """The last 24 h of one install: recalls, hook latency, SLO verdict, errors, last publish, warnings."""
    from memvane.diagnostics import api as diagnostics
    raw = _raw_events(state_root)
    digest = {'events': 'ok' if raw is not None else 'unreadable', 'recalls24h': 0, 'p50Ms': None,
              'p95Ms': None, 'slo': 'no-data', 'errors24h': 0, 'lastPublishAt': None, 'warnings': {}}
    if raw is None:
        return digest
    now = _now(now)
    rows = diagnostics.query_events(raw, None, hours=24, raw=True, now=now)
    lines = diagnostics.slo_lines(rows, 24, state_root=state_root, now=now)
    hook = next((line for line in lines if line.get('line') == 'hook'), {})
    digest.update(recalls24h=sum(1 for row in rows if row.get('event') == 'recall'),
                  p50Ms=hook.get('p50'), p95Ms=hook.get('p95'), slo=_verdict(lines),
                  errors24h=len(diagnostics.query_events(rows, 'errors', hours=24, now=now)),
                  warnings=_warnings(rows))
    published = [row.get('ts') for row in rows
                 if row.get('event') == 'index.publish' and row.get('outcome') == 'published']
    digest['lastPublishAt'] = max((ts for ts in published if isinstance(ts, str)), default=None)
    return digest


# --------------------------------------------------------------- overview


def _banner(snapshot):
    status = snapshot.get('status')
    banner = []
    if status in REGISTRY_TROUBLE:
        banner.append({'level': 'warning', 'code': 'registry_' + status, 'reason': snapshot.get('reason')})
    elif status == 'ok':
        if snapshot.get('stale'):
            banner.append({'level': 'info', 'code': 'registry_stale'})
        if not snapshot.get('complete'):
            banner.append({'level': 'info', 'code': 'registry_incomplete'})
    return banner


def _target_row(row):
    kind = row.get('kind')
    return {'targetId': target_id(row['installId'], row['targetRoot'], kind), 'installId': row['installId'],
            'kind': kind, 'status': row.get('status'), 'reason': row.get('reason'),
            'displayPath': row['targetRoot'], 'projectRoot': row.get('projectRoot'),
            'branch': read_branch(row['targetRoot']) if row.get('status') == 'active' else 'unknown',
            'observedAt': row.get('observedAt'), 'cards': card_counts(row['targetRoot'])
            if row.get('status') == 'active' else None}


def overview(*, home=None, reconcile=False, now=None, max_installs=64, observe=None):
    """``{registry, banner, installs: [{installId, proved, digest, pending, main, worktrees}]}``.

    ``observe(install id, state root)`` is told every proved install (the server watches its log).
    """
    snapshot = _registry(home, reconcile=reconcile)
    banner = _banner(snapshot)
    installs = {}
    for row in _rows(snapshot):
        group = installs.setdefault(row['installId'], {'installId': row['installId'], 'main': None,
                                                       'worktrees': []})
        target = _target_row(row)
        if row.get('kind') == 'main':
            group['main'] = target
        else:
            group['worktrees'].append(target)
    result = []
    for index, group in enumerate(sorted(installs.values(), key=lambda g: g['installId'])):
        main = group['main']
        group['proved'] = False
        group['digest'] = None
        group['pending'] = None
        if main is not None and main['status'] == 'active' and index < max_installs:
            decision, state_root = _prove(main['displayPath'], group['installId'], worktree=False)
            if decision is not None and state_root is not None:
                group['proved'] = True
                group['routeState'] = decision.get('state')
                if observe is not None:
                    observe(group['installId'], state_root)
                group['digest'] = install_digest(state_root, now=now)
                group['pending'] = pending_cards(main['displayPath'])
                for code, count in group['digest']['warnings'].items():
                    banner.append({'level': 'warning', 'code': code, 'installId': group['installId'],
                                   'count': count})
        elif main is not None and index >= max_installs:
            group['digest'] = {'events': 'limit_exceeded'}
        group['worktrees'].sort(key=lambda target: target['displayPath'])
        result.append(group)
    registry = {key: snapshot.get(key) for key in ('status', 'reason', 'complete', 'stale')}
    registry['rows'] = len(snapshot.get('rows') or ())
    return {'registry': registry, 'banner': banner, 'installs': result,
            'generatedAt': _now(now).isoformat(timespec='seconds')}


# ---------------------------------------------------------- install views


def _install(install_id, home):
    if not isinstance(install_id, str) or not INSTALL_ID.fullmatch(install_id):
        raise TargetError('install_id_invalid', 400)
    return resolve_target(install_id, home=home)


def install_summary(install_id, *, hours=None, home=None, now=None):
    from memvane.diagnostics import api as diagnostics
    target = _install(install_id, home)
    hours = _hours(hours)
    raw = _raw_events(target['stateRoot'])
    if raw is None:
        raise TargetError('events_unreadable', 503)
    now = _now(now)
    rows = diagnostics.query_events(raw, None, hours=hours, raw=True, now=now)
    return {'installId': install_id, 'hours': hours, 'routeState': target['routeState'],
            'slo': diagnostics.slo_lines(rows, hours, state_root=target['stateRoot'], now=now),
            'summary': diagnostics.summarize_events(rows, hours, now=now),
            'pending': pending_cards(target['targetRoot'])}


def _matches(row, outcome, host, worktree):
    if outcome and row.get('outcome') != outcome:
        return False
    if host and row.get('host') != host:
        return False
    if worktree is not None and (row.get('degraded') == 'worktree') != worktree:
        return False
    return True


def install_recalls(install_id, *, hours=None, limit=100, outcome=None, host=None, target=None,
                    home=None, now=None):
    """Prompt and edit recalls, newest first; ``target`` keeps one checkout of the install."""
    from memvane.diagnostics import api as diagnostics
    resolved = _install(install_id, home)
    hours = _hours(hours)
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        raise TargetError('limit_invalid', 400) from None
    if not 0 < limit <= MAX_RECALLS:
        raise TargetError('limit_invalid', 400)
    worktree = None
    if target:
        parsed = parse_target_id(target)
        if parsed is None or parsed[0] != install_id:
            raise TargetError('target_id_invalid', 400)
        worktree = parsed[1] is not None
    raw = _raw_events(resolved['stateRoot'])
    if raw is None:
        raise TargetError('events_unreadable', 503)
    now = _now(now)
    rows = [row for row in diagnostics.query_events(raw, 'recalls', hours=hours, raw=True, now=now)
            + diagnostics.query_events(raw, None, hours=hours, event='recall.edit', raw=True, now=now)
            if _matches(row, outcome, host, worktree)]
    rows.sort(key=lambda row: str(row.get('ts') or ''), reverse=True)
    return {'installId': install_id, 'hours': hours, 'total': len(rows), 'recalls': rows[:limit]}


def install_errors(install_id, *, hours=None, details=False, home=None, now=None, limit=200):
    """Refusals and failures, newest first; ``details`` reassembles their diagnostics (explicit opt-in)."""
    from memvane.diagnostics import api as diagnostics
    resolved = _install(install_id, home)
    hours = _hours(hours)
    raw = _raw_events(resolved['stateRoot'])
    if raw is None:
        raise TargetError('events_unreadable', 503)
    now = _now(now)
    rows = diagnostics.query_events(raw, 'errors', hours=hours, raw=True, now=now)
    rows = sorted(rows, key=lambda row: str(row.get('ts') or ''), reverse=True)[:limit]
    if details:
        out = []
        for row in rows:
            ident = row.get('diagnostic_id')
            out.append(dict(row, detail=diagnostics.reassemble_diagnostic(raw, ident))
                       if isinstance(ident, str) else row)
        rows = out
    return {'installId': install_id, 'hours': hours, 'details': bool(details), 'errors': rows}


def since_iso(hours, now=None):
    return (_now(now) - timedelta(hours=hours)).isoformat(timespec='seconds')

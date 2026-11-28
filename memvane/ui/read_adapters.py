"""Read-only DTOs of the dashboard built from the registry, route and diagnostics APIs.

Nothing here writes, locks or starts a process. A registry row is a lookup
key only: every install or target the API serves is proved again through
``journal.api.select_route`` (install id, route state, scope id) before any
of its files is read. Overview stays free of git processes: main checkouts
are proved without the linked-worktree probe, worktree rows are listed from
the registry, and branch names come from ``<gitdir>/HEAD`` of the nearest
``.git`` at or above the checkout root (no-follow, 4 KiB).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
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


def _dotgit(target_root):
    """The first ``.git`` entry at or above ``target_root`` (the checkout's own rule), or None.

    Same walk as ``store.checkout.checkout_view._dotgit``: a project folder
    inside a repository reads the branch of that repository.
    """
    root = Path(target_root)
    for directory in (root, *root.parents):
        path = directory / '.git'
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            continue
        return path, info
    return None


def git_dir(target_root):
    """The git directory of the nearest ``.git`` at or above the checkout, or None when there is none."""
    found = _dotgit(target_root)
    if found is None:
        return None
    dotgit, info = found
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
    except (OSError, ValueError):
        return 'unknown'
    if directory is None:
        return 'non-git'
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
    for part in (Path(target_root) / '.memvane', root):
        try:
            if not stat.S_ISDIR(os.lstat(part).st_mode):
                return counts
        except OSError:
            return counts
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


def _proof(target_root, install_id, *, worktree):
    """``(decision, state root, None)`` when the route of ``target_root`` proves ``install_id``,
    else ``(None, None, reason code)``."""
    from memvane.diagnostics import api as diagnostics
    from memvane.journal import api as journal
    try:
        if worktree:
            decision = journal.select_route(str(target_root), action='recall', linked=True,
                                            deadline=time.monotonic() + WORKTREE_PROOF_SECONDS)
        else:
            decision = journal.select_route(str(target_root), action='recall', linked=False)
    except Exception as exc:  # noqa: BLE001 - unproved
        return None, None, 'route_error:' + type(exc).__name__
    if not isinstance(decision, dict):
        return None, None, 'route_invalid'
    state = decision.get('state')
    if state not in PROVED_STATES:
        reason = decision.get('reason')
        return None, None, 'route_' + str(state) + (':' + reason if isinstance(reason, str) and reason else '')
    if (state == 'worktree') != worktree:
        return None, None, 'route_kind_mismatch'
    if diagnostics.install_of(decision) != install_id:
        return None, None, 'install_mismatch'
    state_root = diagnostics.state_root_for_decision(decision)
    if state_root is None:
        return None, None, 'state_root_unknown'
    return decision, state_root, None


def _prove(target_root, install_id, *, worktree):
    """``(decision, state root)`` when the route of ``target_root`` proves ``install_id``."""
    decision, state_root, _ = _proof(target_root, install_id, worktree=worktree)
    return decision, state_root


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
            collections = decision.get('collections') or main_route.get('collections') or ()
            wiki_collections = tuple(
                item['name'] for item in collections
                if isinstance(item, dict) and item.get('role') == 'wiki'
                and isinstance(item.get('name'), str) and item['name']
            )
            return {'targetId': value, 'installId': install, 'kind': row['kind'],
                    'targetRoot': row['targetRoot'], 'projectRoot': row.get('projectRoot'),
                    'stateRoot': str(state_root), 'routeState': decision.get('state'),
                    'wikiCollections': wiki_collections}
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


def _error_item(install_id, row, detail=None):
    host = row.get('host')
    ident = row.get('diagnostic_id')
    item = {
        'ts': row.get('ts') or '',
        'installId': install_id,
        'targetId': None,
        'host': host if host in ('claude', 'codex', 'hermes', 'gemini') else None,
        'event': str(row.get('event') or ''),
        'reason': str(row.get('reason') or ''),
        'diagId': ident if isinstance(ident, str) else None,
    }
    if detail is not None:
        item['detail'] = detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False, indent=2)
    return item


def _recall_item(install_id, row):
    host = row.get('host')
    degraded_raw = row.get('degraded')
    degraded = ([degraded_raw] if isinstance(degraded_raw, str) and degraded_raw in ('worktree', 'reader_endpoint', 'preview')
                else [x for x in degraded_raw if x in ('worktree', 'reader_endpoint', 'preview')] if isinstance(degraded_raw, list)
                else [])
    prompt = row.get('prompt') if isinstance(row.get('prompt'), dict) else {}
    outcome = row.get('outcome')
    return {
        'ts': row.get('ts') or '',
        'event': 'recall.edit' if row.get('event') == 'recall.edit' else 'recall',
        'installId': install_id,
        'targetId': None,
        'host': host if host in ('claude', 'codex', 'hermes', 'gemini') else 'claude',
        'session': str(row.get('session') or ''),
        'outcome': outcome if outcome in ('served', 'empty', 'refused') else 'empty',
        'reason': str(row.get('reason') or ''),
        'notes': [str(note) for note in (row.get('notes') or ())],
        'semantic': row.get('semantic'),
        'vectorCoveredMembers': row.get('vectorCoveredMembers'),
        'vectorEligibleMembers': row.get('vectorEligibleMembers'),
        'degraded': degraded,
        'cards': [card for card in (row.get('cards') or ()) if isinstance(card, dict)],
        'prompt': {'sha256': str(prompt.get('sha256') or ''), 'chars': int(prompt.get('chars') or 0)},
        'timings': {key: row.get(key) for key in (
            'pinMs', 'lexicalMs', 'stillCurrentMs', 'checkoutMs',
            'queryEmbeddingMs', 'semanticMs', 'selectorResidentSeconds')},
        'totalMs': row.get('ms'),
    }


def install_digest(state_root, *, now=None):
    """The last 24 h of one install: recalls, hook latency, SLO verdict, errors, last publish, warnings."""
    from memvane.diagnostics import api as diagnostics
    raw = _raw_events(state_root)
    digest = {'events': 'ok' if raw is not None else 'unreadable', 'recalls24h': 0, 'p50Ms': None,
              'p95Ms': None, 'slo': 'no-data', 'errors24h': 0, 'lastPublishAt': None, 'warnings': {},
              'hosts': [], 'lastErrorAt': None, '_errors': []}
    if raw is None:
        return digest
    now = _now(now)
    rows = diagnostics.query_events(raw, None, hours=24, raw=True, now=now)
    lines = diagnostics.slo_lines(rows, 24, state_root=state_root, now=now)
    hook = next((line for line in lines if line.get('line') == 'hook'), {})
    errors = sorted(diagnostics.query_events(rows, 'errors', hours=24, now=now),
                    key=lambda row: str(row.get('ts') or ''), reverse=True)
    published = [row.get('ts') for row in rows
                 if row.get('event') == 'index.publish' and row.get('outcome') == 'published']
    hosts = sorted({row.get('host') for row in rows
                    if row.get('host') in ('claude', 'codex', 'hermes', 'gemini')})
    digest.update(
        recalls24h=sum(1 for row in rows if row.get('event') == 'recall'),
        p50Ms=hook.get('p50'), p95Ms=hook.get('p95'), slo=_verdict(lines),
        errors24h=len(errors), warnings=_warnings(rows), hosts=hosts,
        lastPublishAt=max((ts for ts in published if isinstance(ts, str)), default=None),
        lastErrorAt=errors[0].get('ts') if errors else None,
        _errors=errors[:50],
    )
    return digest


# --------------------------------------------------------------- overview


def _view_state(row, failure=None, *, proved_live=False):
    """``(viewState, proofReason)`` of one checkout row from its registry status, reason and proof.

    ``proved_live`` says a live route proof ran for the row; ``failure`` is its
    reason code when it failed. Without a live proof the registry's recorded
    ``proof`` (``unproved:<code>``) stands in. A row whose
    root is present but whose route no longer proves is ``proof_failed``; the
    others follow the registry: ``missing_pending`` and ``detached/missing`` are
    ``path_missing``, ``detached/released`` and ``detached/withdrawn`` are
    ``released``, ``detached/root_moved`` is ``moved``, ``conflict`` is ``conflict``.
    """
    status, reason = row.get('status'), row.get('reason')
    if status == 'active':
        if row.get('present') is False:
            return 'path_missing', None
        recorded = row.get('proof')
        if not proved_live and isinstance(recorded, str) and recorded.startswith('unproved'):
            failure = recorded.split(':', 1)[1] if ':' in recorded else recorded
        return ('proof_failed', failure) if failure is not None else ('ready', None)
    if status == 'missing_pending' or (status == 'detached' and reason == 'missing'):
        return 'path_missing', None
    if status == 'detached' and reason == 'root_moved':
        return 'moved', None
    if status == 'conflict':
        return 'conflict', None
    return 'released', None


def _node_view(row, *, digest=None, pending=0, parent_id=None, failure=None, proved_live=False):
    raw_cards = row.get('cards') or {}
    cards = {'verified': int(raw_cards.get('verified', 0)),
             'generated': int(raw_cards.get('generated', 0)),
             'pending': int(pending)}
    view_state, proof_reason = _view_state(row, failure, proved_live=proved_live)
    node = {
        'targetId': row['targetId'],
        'installId': row['installId'],
        'kind': 'worktree' if parent_id is not None else 'main',
        'name': Path(row['displayPath']).name or row['displayPath'],
        'displayRoot': row['displayPath'],
        'branch': row['branch'],
        'viewState': view_state,
        'proofReason': proof_reason,
        'hosts': digest['hosts'] if digest else [],
        'cards': cards,
        'lastSyncAt': digest['lastPublishAt'] if digest else None,
        'recalls24h': digest['recalls24h'] if digest else 0,
        'p50Ms': digest['p50Ms'] if digest else None,
        'p95Ms': digest['p95Ms'] if digest else None,
        'slo': digest['slo'] if digest else 'no-data',
    }
    if parent_id is not None:
        node['parentTargetId'] = parent_id
    return node


def _alerts(snapshot):
    status = snapshot.get('status')
    alerts = []
    if status in REGISTRY_TROUBLE:
        code = 'registry_' + status
        alerts.append({'code': code, 'severity': 'warn', 'scope': {'kind': 'global'},
                       'message': str(snapshot.get('reason') or code), 'retryInSeconds': None})
    elif status == 'ok':
        if snapshot.get('stale'):
            alerts.append({'code': 'registry_stale', 'severity': 'info', 'scope': {'kind': 'global'},
                           'message': 'registry_stale', 'retryInSeconds': None})
        if not snapshot.get('complete'):
            alerts.append({'code': 'registry_incomplete', 'severity': 'info', 'scope': {'kind': 'global'},
                           'message': 'registry_incomplete', 'retryInSeconds': None})
    return alerts


def _snapshot_meta(snapshot):
    status = snapshot.get('status')
    if status == 'ok':
        state = 'stale' if snapshot.get('stale') else 'complete'
    else:
        state = status if status in ('absent', 'busy', 'recovery_required') else 'absent'
    return {
        'state': state,
        'truncated': bool(snapshot.get('truncated', False)),
        'maxRows': int(snapshot.get('maxRows', 8192)),
        'lastReconcileAt': snapshot.get('lastReconcileAt'),
    }


def _present(target_root):
    try:
        return stat.S_ISDIR(os.lstat(target_root).st_mode)
    except OSError:
        return False


def _target_row(row):
    kind = row.get('kind')
    # Only a live row whose root is still a directory reads a branch and cards: a
    # missing root would otherwise read the branch of a repository above it.
    present = _present(row['targetRoot']) if row.get('status') == 'active' else None
    live = present is True
    return {'targetId': target_id(row['installId'], row['targetRoot'], kind), 'installId': row['installId'],
            'kind': kind, 'status': row.get('status'), 'reason': row.get('reason'), 'proof': row.get('proof'),
            'present': present,
            'displayPath': row['targetRoot'], 'projectRoot': row.get('projectRoot'),
            'branch': read_branch(row['targetRoot']) if live else 'unknown',
            'observedAt': row.get('observedAt'), 'cards': card_counts(row['targetRoot']) if live else None}


def overview(*, home=None, reconcile=False, now=None, max_installs=64, observe=None):
    """OverviewResponse DTO matching ``ui/src/api/types.ts``."""
    snapshot = _registry(home, reconcile=reconcile)
    alerts = _alerts(snapshot)
    groups = {}
    for row in _rows(snapshot):
        group = groups.setdefault(row['installId'], {'installId': row['installId'], 'main': None, 'worktrees': []})
        target = _target_row(row)
        if row.get('kind') == 'main':
            group['main'] = target
        else:
            group['worktrees'].append(target)
    projects, installs, recent_errors = [], [], []
    for index, group in enumerate(sorted(groups.values(), key=lambda g: g['installId'])):
        main = group['main']
        if main is None:
            continue
        digest, pending, failure, proved_live = None, 0, None, False
        if main['status'] == 'active' and main['present'] and index < max_installs:
            proved_live = True
            decision, state_root, failure = _proof(main['displayPath'], group['installId'], worktree=False)
            if decision is not None and state_root is not None:
                if observe is not None:
                    observe(group['installId'], state_root)
                digest = install_digest(state_root, now=now)
                pending_info = pending_cards(main['displayPath'])
                pending = pending_info['pending'] if isinstance(pending_info, dict) else 0
                for code, count in digest['warnings'].items():
                    alerts.append({'code': code, 'severity': 'warn',
                                   'scope': {'kind': 'install', 'installId': group['installId']},
                                   'message': f'{code} ({count})', 'retryInSeconds': None})
                for err in digest['_errors']:
                    recent_errors.append(_error_item(group['installId'], err))
        main_node = _node_view(main, digest=digest, pending=pending, failure=failure,
                               proved_live=proved_live)
        wt_rows = sorted(group['worktrees'], key=lambda item: item['displayPath'])
        wt_nodes = [_node_view(wt, digest=digest, parent_id=main_node['targetId']) for wt in wt_rows]
        projects.append({'installId': group['installId'], 'main': main_node, 'worktrees': wt_nodes})
        installs.append({
            'installId': group['installId'],
            'projectName': main_node['name'],
            'recalls24h': digest['recalls24h'] if digest else 0,
            'p50Ms': digest['p50Ms'] if digest else None,
            'p95Ms': digest['p95Ms'] if digest else None,
            'slo': digest['slo'] if digest else 'no-data',
            'errors24h': digest['errors24h'] if digest else 0,
            'lastErrorAt': digest['lastErrorAt'] if digest else None,
        })
    recent_errors.sort(key=lambda item: item['ts'], reverse=True)
    return {
        'generatedAt': _now(now).isoformat(timespec='seconds'),
        'snapshot': _snapshot_meta(snapshot),
        'projects': projects,
        'installs': installs,
        'alerts': alerts,
        'recentErrors': recent_errors[:50],
    }


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
    return {'installId': install_id, 'hours': hours, 'total': len(rows),
            'items': [_recall_item(install_id, row) for row in rows[:limit]]}


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
    items = []
    for row in rows:
        ident = row.get('diagnostic_id')
        detail = diagnostics.reassemble_diagnostic(raw, ident) if (details and isinstance(ident, str)) else None
        items.append(_error_item(install_id, row, detail=detail))
    return {'installId': install_id, 'hours': hours, 'details': bool(details), 'items': items}


def since_iso(hours, now=None):
    return (_now(now) - timedelta(hours=hours)).isoformat(timespec='seconds')

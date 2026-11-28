#!/usr/bin/env python3
"""View MemVane install event logs and register unified-view targets.

usage: logs.py [--cwd DIR | --state-root DIR | --all] [--hours N] [--limit N]
               [--json] [--details] [--register PROJECT ...] [--slo]
               [--learning-check]
               [summary|recalls|errors|events] [--event NAME]

The state root comes from the project's selected route (the install
journal's directory); ``--state-root`` names one directly. ``--all`` merges
every target of the owner registry (``log_targets.resolve``, which re-checks
each route) in UTC order under one ``ALL_READ_LIMIT`` byte budget split
equally between readable targets; a target that cannot be read is reported
with its reason, never shown as empty. ``--register PROJECT`` (repeatable)
explicitly adds projects to that registry (the only write this tool makes).
``--slo`` prints the service-level lines (``diagnostics.api.slo_lines``) for the window, one
block per install. ``--learning-check`` reads the project's learning state
(read-only) and prints how many rows break each invariant: counts per rule, no
row content.

Raw diagnostics. Normal text and ``--json`` never print ``diagnostic.part``
rows or stored ``prompt.text``; a ``diagnostic.header`` shows only its ID and
fixed metadata. ``--details`` is the explicit opt-in that reassembles each
shown header's text (or reports ``incomplete``). Every printed string is
terminal-escaped (ESC, CR, LF and other control/format characters become
``\\xNN``-style text), so stored content cannot fake rows or drive the
terminal. Stored bytes are never changed.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from memvane.diagnostics import api as diagnostics, event_log  # noqa: E402
# Display escaping and visibility are the diagnostics domain's; the terminal rendering stays here.
from memvane.diagnostics.event_queries import (  # noqa: E402
    DIAG_ID as _DIAG_ID, EPOCH as _EPOCH, count as _count, diagnostic_groups as _groups,
    diagnostic_ids as _diagnostic_ids, display as _display, esc as _esc, json_line as _json_line,
    short as _short, ts as _ts, visible as _visible)
from memvane.registry import log_targets  # noqa: E402

ALL_READ_LIMIT = 16 * 1024 * 1024
MAX_ALL_TARGETS = 128


class _View:
    """How rows are printed: unified prefix, and explicit --details reassembly."""

    def __init__(self, unified=False, details=False):
        self.unified = unified
        self.details = details
        self.groups = []      # per target: diagnostic ID -> rows
        self.origin = {}      # id(display row) -> target index
        self.roots = {}       # install ID (None: the single target) -> state root

    def detail(self, row, ident=None):
        ident = row.get('diagnostic_id') if ident is None else ident
        if not isinstance(ident, str) or not _DIAG_ID.fullmatch(ident):
            return {'status': 'incomplete', 'diagnostic_id': ident}
        index = self.origin.get(id(row), 0)
        group = self.groups[index].get(ident) if index < len(self.groups) else None
        # An overflowing group (None) or a missing one reassembles as ``incomplete``.
        return diagnostics.reassemble_diagnostic(group or [], ident)


def _detail_lines(result):
    ident = _esc(result.get('diagnostic_id'))
    if result.get('status') != 'complete' or not isinstance(result.get('text'), str):
        yield f"    diagnostic {ident}: {_esc(result.get('status'))}"
        return
    yield (f"    diagnostic {ident}: complete kind={_esc(result.get('kind'))}"
           f" truncated={result.get('truncated') is True}")
    lines = result['text'].split('\n')
    if lines and lines[-1] == '':
        lines.pop()
    for line in lines:
        # A fixed prefix: no stored line can look like a log row.
        yield '    | ' + _esc(line)


def _state_root(args):
    if args.state_root:
        return Path(args.state_root), None
    from memvane.journal.api import select_route
    try:
        decision = select_route(str(Path(args.cwd).resolve()), action='recall')
    except Exception as exc:  # noqa: BLE001 - reported, never raised
        return None, f'route lookup failed ({event_log.code(exc)})'
    if not isinstance(decision, dict) or decision.get('state') == 'legacy':
        return None, 'project is not onboarded (no selected MemVane route)'
    root = event_log.state_root_for_decision(decision)
    if root is None:
        return None, f"route state {decision.get('state')} has no install journal"
    return root, None


def _recall_line(row):
    line = _short(row)
    extra = []
    for key in ('degraded', 'vector_fallback', 'selection_fallback', 'retry', 'drift'):
        if row.get(key):
            extra.append(f'{key}={row[key]}')
    if isinstance(row.get('notes'), list) and row['notes']:
        extra.append('notes=' + ','.join(map(str, row['notes'])))
    extra.append(f"cards={row.get('count', 0)}")
    if row.get('session'):
        extra.append(f"session={row['session']}")
    cards = [f"  - {card.get('path')} {str(card.get('sha') or '')[:12]}"
             for card in (row.get('cards') if isinstance(row.get('cards'), list) else [])
             if isinstance(card, dict)]
    return '\n'.join([line + ' ' + ' '.join(extra), *cards])


def _print(rows, args, render=_short, view=None):
    view = view or _View()
    rows = rows[-args.limit:] if args.limit else rows
    for row in rows:
        ids = _diagnostic_ids(row) if view.details else []
        details = [view.detail(row, ident) for ident in ids]
        if args.json:
            out = dict(row, details=details) if details else row
            print(_json_line(out))
            continue
        shown = _display(row)
        prefix = ''
        if view.unified:
            prefix = f"{shown.get('root') or '-'} {shown.get('install') or '-'} " \
                     f"{shown.get('host') or '-'} "
        print(prefix + render(shown))
        for detail in details:
            for line in _detail_lines(detail):
                print(line)
    if not rows and not args.json:
        print('(no matching events)')


def _header(state_root, info):
    print(f'state root: {_esc(state_root)}')
    files = ', '.join(f"{_esc(item['name'])} {item['bytes']}B"
                      for item in info['files']) or 'none'
    print(f'log files: {files}')
    if info['reason']:
        print(f"WARNING: events cannot be written: {_esc(info['reason'])}")
    if info['reason'] in ('state_root_missing', 'state_root_unsafe'):
        # An unreadable state root is a failure, not an empty log.
        print(f"WARNING: events cannot be read: {_esc(info['reason'])}")
    if info['refused']:
        last = info.get('lastRefusal') or {}
        print(f"WARNING: {info['refused']} event write(s) refused "
              f"(last {_esc(last.get('reason'))} at {_esc(last.get('ts'))})")


def summary(rows, args, unified=False):
    for line in diagnostics.summarize_events(rows, args.hours, unified=unified):
        print(line)


def _show(rows, args, view):
    """Render one view of already-visible rows (shared by single and --all)."""
    if args.view in ('summary', 'slo') and args.hours is None:
        args.hours = 24.0
    if args.view == 'slo':
        _slo(rows, args, view)
        return
    if args.view == 'summary':
        summary(rows, args, unified=view is not None and view.unified)
        return
    render = _recall_line if args.view == 'recalls' else _short
    _print(diagnostics.query_events(rows, args.view, hours=args.hours, event=args.event),
           args, render, view)


def _slo(rows, args, view):
    """Service-level lines, one block per install (``--all``) or one block for the single target."""
    unified = view is not None and view.unified
    blocks = {}
    for row in rows:
        blocks.setdefault(row.get('install') if unified else None, []).append(row)
    if not blocks:
        blocks[None] = []
    roots = view.roots if view is not None else {}
    for install in sorted(blocks, key=str):
        block = blocks[install]
        root = next((row.get('root') for row in block if row.get('root')), None) if unified else None
        if args.json:
            for line in diagnostics.slo_lines(block, args.hours, state_root=roots.get(install)):
                print(_json_line({'slo': dict(line, **({'install': install, 'root': root} if unified else {}))}))
            continue
        if unified:
            print(f'slo install={_esc(install)} root={_esc(root or "-")} (last {args.hours:g}h)')
        else:
            print(f'slo (last {args.hours:g}h)')
        for line in diagnostics.slo_lines(block, args.hours, state_root=roots.get(install), text=True):
            print('  ' + line)


def _learning_check(args):
    """The read-only invariant check of the project's learning namespace: counts per rule only."""
    # Like the event log reader above: the skill reads the domain's module directly.
    from memvane.learning.context_learning import invariants
    result = invariants.project_counts(args.cwd)
    if args.json:
        print(_json_line({'learningCheck': result}))
        return 0
    status = result.get('status')
    if status != 'checked':
        print('learning check: ' + _esc(status) + (' (%s)' % _esc(result['error']) if result.get('error') else ''))
        return 0
    print('learning check: %d violation(s)' % result['violations'])
    for rule, count in result['rules'].items():
        print('  %s %d' % (_esc(rule), count))
    return 0


def _register(args):
    """Explicitly register the given projects; one reported row per request."""
    try:
        outcome = log_targets.register(list(args.register))
    except Exception as exc:  # noqa: BLE001 - reported, never raised
        outcome = {'results': [{'requested': None, 'root': None, 'install_id': None,
                                'status': f'register_failed:{event_log.code(exc)}'}],
                   'partial': True}
    results = outcome.get('results') if isinstance(outcome, dict) else None
    results = results if isinstance(results, list) else []
    for row in results:
        if not isinstance(row, dict):
            continue
        if args.json:
            print(_json_line({'register': row}))
        else:
            print(f"register: {_esc(row.get('requested'))} -> {_esc(row.get('status'))}"
                  f" root={_esc(row.get('root') or '-')}"
                  f" install={_esc(row.get('install_id') or '-')}")
    partial = not isinstance(outcome, dict) or outcome.get('partial') is not False
    return 1 if partial or not results else 0


def _target_status(entry, seen):
    """``(status, state_root or None)`` for one resolved registry row.

    The row is re-validated here (types, absolute normalized paths, install ID
    shape, no repeated state root) and the state root is walked no-follow by
    ``event_log.inspect`` before any byte is read.
    """
    status = {'root': None, 'install_id': None, 'status': None}
    if not isinstance(entry, dict):
        status['status'] = 'malformed_target'
        return status, None
    root, install, state_root = entry.get('root'), entry.get('install_id'), entry.get('state_root')
    status['root'] = root if isinstance(root, str) else None
    status['install_id'] = install if isinstance(install, str) else None
    if entry.get('refusal') is not None:
        status['status'] = event_log.code(entry['refusal'])
        return status, None
    state_root = str(state_root) if isinstance(state_root, (str, Path)) else None
    if (not isinstance(root, str) or not os.path.isabs(root)
            or event_log.session_id(install) is None or state_root is None
            or not os.path.isabs(state_root) or os.path.normpath(state_root) != state_root
            or '\x00' in state_root):
        status['status'] = 'malformed_target'
        return status, None
    if state_root in seen:
        status['status'] = 'duplicate'
        return status, None
    seen.add(state_root)
    try:
        info = event_log.inspect(state_root)
    except Exception:  # noqa: BLE001
        info = {'reason': 'state_root_unsafe', 'files': []}
    reason = info.get('reason')
    if reason in ('state_root_missing', 'state_root_unsafe', 'no_selected_route'):
        status['status'] = reason
        return status, None
    if reason:
        status['warning'] = event_log.code(reason)
    status['bytes'] = sum(item['bytes'] for item in info.get('files') or ()
                          if isinstance(item, dict) and _count(item.get('bytes')))
    return status, state_root


def _collect_all(details):
    """Statuses of every registered target and their merged, visible rows."""
    try:
        resolved = log_targets.resolve()
    except Exception:  # noqa: BLE001 - reported, never raised
        resolved = None
    if not isinstance(resolved, list):
        resolved = [{'root': None, 'install_id': None, 'refusal': 'registry_unreadable'}]
    statuses, ready, seen = [], [], set()
    for entry in resolved[:MAX_ALL_TARGETS]:
        status, state_root = _target_status(entry, seen)
        statuses.append(status)
        if state_root is not None:
            ready.append((status, state_root))
    if len(resolved) > MAX_ALL_TARGETS:
        statuses.append({'root': None, 'install_id': None, 'status': 'limit_exceeded'})

    # One fixed budget, split equally: no target can starve the others.
    share = ALL_READ_LIMIT // len(ready) if ready else 0
    view = _View(unified=True, details=details)
    merged = []
    for index, (status, state_root) in enumerate(ready):
        try:
            raw = event_log.read(state_root, max_bytes=share, strict=True)
            if not isinstance(raw, list):
                raise TypeError('read result')
        except Exception as exc:  # noqa: BLE001 - per-target failure, others continue
            status['status'] = 'read_failed'
            status['code'] = event_log.code(exc)
            view.groups.append({})
            continue
        view.groups.append(_groups(raw) if details else {})
        view.roots[status['install_id']] = state_root
        shown = 0
        for seq, row in enumerate(raw):
            safe = _visible(row)
            if safe is None:
                continue
            out = {'root': status['root'], 'install': status['install_id']}
            out.update((key, value) for key, value in safe.items()
                       if key not in ('root', 'install'))
            logged = safe.get('install')
            if logged is not None and logged != status['install_id']:
                out['logged_install'] = logged
            view.origin[id(out)] = index
            merged.append((_ts(out) or _EPOCH, index, seq, out))
            shown += 1
        status['events'] = shown
        if raw:
            status['status'] = 'ok'
        else:
            # Bytes on disk but no valid row: malformed, or refused at open.
            status['status'] = 'no_valid_rows' if status.get('bytes') else 'empty'
        if status.get('bytes', 0) > share:
            status['budget_truncated'] = True
    merged.sort(key=lambda item: item[:3])
    return statuses, [item[3] for item in merged], view, share


def _main_all(args):
    statuses, rows, view, share = _collect_all(args.details)
    read = sum(1 for status in statuses if status['status'] in ('ok', 'empty', 'no_valid_rows'))
    if args.json:
        for status in statuses:
            print(_json_line({'target': status}))
    else:
        print(f'targets: {len(statuses)} listed, {read} read, {share} bytes each')
        for status in statuses:
            parts = [_esc(status.get('root') or '-'),
                     f"install={_esc(status.get('install_id') or '-')}",
                     f"status={_esc(status.get('status'))}"]
            for key in ('events', 'code', 'warning'):
                if key in status:
                    parts.append(f'{key}={_esc(status[key])}')
            if status.get('budget_truncated'):
                parts.append('budget_truncated')
            print('  ' + ' '.join(parts))
    _show(rows, args, view)
    return 0 if read else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('view', nargs='?', default=None,
                        choices=('summary', 'recalls', 'errors', 'events'))
    parser.add_argument('--cwd', default=os.getcwd())
    parser.add_argument('--state-root')
    parser.add_argument('--all', action='store_true',
                        help='merge every registered target (see --register)')
    parser.add_argument('--details', action='store_true',
                        help='explicitly reveal reassembled raw diagnostic text')
    parser.add_argument('--register', action='append', metavar='PROJECT',
                        help='register a project for --all (repeatable)')
    parser.add_argument('--hours', type=float)
    parser.add_argument('--limit', type=int, default=20)
    parser.add_argument('--event')
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--slo', action='store_true',
                        help='service-level lines for the window (default 24h), per install')
    parser.add_argument('--learning-check', action='store_true',
                        help="read-only invariant check of the project's learning state: counts per rule")
    args = parser.parse_args(argv)
    if args.learning_check:
        if args.all or args.slo or args.view is not None or args.state_root:
            parser.error('--learning-check reads one project (--cwd); omit the view and the other modes')
        return _learning_check(args)
    if args.slo:
        if args.view not in (None, 'summary'):
            parser.error('--slo is its own view; omit the view name')
        args.view = 'slo'
    if args.view is None:
        args.view = 'events' if args.all else 'summary'
    if args.all and args.state_root:
        parser.error('--all reads the registered targets; omit --state-root')
    registration_code = 0
    if args.register:
        code = _register(args)
        registration_code = code
        if not args.all:
            return code
    if args.all:
        return max(registration_code, _main_all(args))
    state_root, problem = _state_root(args)
    if state_root is None:
        print(f'[memvane] {_esc(problem)}', file=sys.stderr)
        return 1
    info = event_log.inspect(state_root)
    if not args.json:
        _header(state_root, info)
    elif info.get('reason') in ('state_root_missing', 'state_root_unsafe'):
        print(f"[memvane] events cannot be read: {_esc(info['reason'])}", file=sys.stderr)
    # Newest bytes first, capped (event_log.READ_LIMIT); malformed lines are skipped.
    raw = event_log.read(state_root)
    view = _View(details=args.details)
    view.roots[None] = state_root
    view.groups.append(_groups(raw) if args.details else {})
    rows = [row for row in map(_visible, raw) if row is not None]
    _show(rows, args, view)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

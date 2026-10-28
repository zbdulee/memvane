#!/usr/bin/env python3
"""Read-only view of the current project's MemVane install event log.

usage: logs.py [--cwd DIR | --state-root DIR] [--hours N] [--limit N] [--json]
               [summary|recalls|errors|upgrades|events] [--event NAME]

The state root comes from the project's selected route (the install
journal's directory); ``--state-root`` names one directly. Nothing is
written: no directory, lock, cache or state file.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys

sys.dont_write_bytecode = True
CORE = Path(__file__).resolve().parents[3] / 'core'
sys.path.insert(0, str(CORE))

import event_log  # noqa: E402

FAILED = frozenset(('refused', 'failed', 'error', 'interrupted', 'exited'))


def _state_root(args):
    if args.state_root:
        return Path(args.state_root), None
    from laya_index_host_route import select_route
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


def _ts(row):
    """Aware UTC timestamp of a record, or None (a naive one is read as UTC)."""
    try:
        value = datetime.fromisoformat(str(row.get('ts')).replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _since(rows, hours):
    if hours is None:
        return rows
    floor = datetime.now(timezone.utc) - timedelta(hours=hours)
    return [row for row in rows if (_ts(row) or floor) >= floor]


def _short(row):
    parts = [str(row.get('ts', ''))[:19].replace('T', ' '), row.get('event'),
             row.get('outcome') or '-']
    if row.get('reason'):
        parts.append(f"reason={row['reason']}")
    if row.get('ms') is not None:
        parts.append(f"{row['ms']}ms")
    return ' '.join(str(part) for part in parts)


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


def _print(rows, args, render=_short):
    rows = rows[-args.limit:] if args.limit else rows
    for row in rows:
        print(json.dumps(row, ensure_ascii=False) if args.json else render(row))
    if not rows and not args.json:
        print('(no matching events)')


def _header(state_root, info):
    print(f'state root: {state_root}')
    files = ', '.join(f"{item['name']} {item['bytes']}B" for item in info['files']) or 'none'
    print(f'log files: {files}')
    if info['reason']:
        print(f"WARNING: events cannot be written: {info['reason']}")
    if info['refused']:
        last = info.get('lastRefusal') or {}
        print(f"WARNING: {info['refused']} event write(s) refused "
              f"(last {last.get('reason')} at {last.get('ts')})")


def summary(rows, args):
    window = _since(rows, args.hours)
    label = f'last {args.hours}h' if args.hours is not None else 'all retained'
    print(f'events ({label}): {len(window)}')
    counts = Counter((str(row.get('event')), str(row.get('outcome'))) for row in window)
    for (event, outcome), count in sorted(counts.items(), key=lambda item: (str(item[0][0]),
                                                                           str(item[0][1]))):
        print(f'  {event} {outcome}: {count}')
    recalls = [row for row in window if row.get('event') == 'recall']
    if recalls:
        empty = [row for row in recalls if row.get('outcome') != 'served']
        reasons = Counter(str(row.get('reason')) for row in empty)
        print(f"recalls: {len(recalls)} served={len(recalls) - len(empty)} "
              f"empty/refused={len(empty)}"
              + (' (' + ', '.join(f'{k} {v}' for k, v in reasons.most_common(5)) + ')'
                 if reasons else ''))
        degraded = Counter(str(row.get('degraded') or row.get('vector_fallback'))
                           for row in recalls
                           if row.get('degraded') or row.get('vector_fallback'))
        if degraded:
            print('degraded recalls: ' + ', '.join(f'{k} {v}' for k, v in degraded.items()))
    for name, test in (('last upgrade', lambda row: row.get('event') == 'bundle.upgrade'
                        and row.get('outcome') != 'started'),
                       ('last publish', lambda row: row.get('event') == 'index.publish'),
                       ('last error', lambda row: row.get('outcome') in FAILED
                        or row.get('event') == 'hook.error')):
        found = [row for row in rows if test(row)]
        if found:
            row = found[-1]
            detail = ''
            if row.get('event') == 'bundle.upgrade':
                detail = f" {row.get('from')} -> {row.get('to')}"
            elif row.get('event') == 'index.publish':
                detail = f" generation={row.get('generation')}"
            print(f'{name}: {_short(row)}{detail}')
    dropped = sum(row.get('dropped', 0) for row in window if type(row.get('dropped')) is int)
    if dropped:
        print(f'lines a process could not write before a later one: {dropped}')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('view', nargs='?', default='summary',
                        choices=('summary', 'recalls', 'errors', 'upgrades', 'events'))
    parser.add_argument('--cwd', default=os.getcwd())
    parser.add_argument('--state-root')
    parser.add_argument('--hours', type=float)
    parser.add_argument('--limit', type=int, default=20)
    parser.add_argument('--event')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    state_root, problem = _state_root(args)
    if state_root is None:
        print(f'[memvane] {problem}', file=sys.stderr)
        return 1
    info = event_log.inspect(state_root)
    if not args.json:
        _header(state_root, info)
    # Newest bytes first, capped (event_log.READ_LIMIT); malformed lines are skipped.
    rows = event_log.read(state_root)
    if args.view == 'summary':
        if args.hours is None:
            args.hours = 24.0
        summary(rows, args)
        return 0
    rows = _since(rows, args.hours)
    if args.view == 'recalls':
        _print([row for row in rows if row.get('event') == 'recall'], args, _recall_line)
    elif args.view == 'errors':
        _print([row for row in rows if row.get('outcome') in FAILED
                or row.get('event') == 'hook.error'], args)
    elif args.view == 'upgrades':
        _print([row for row in rows if row.get('event') in ('bundle.upgrade', 'journal.v4')],
               args, lambda row: _short(row) + (f" {row.get('from')} -> {row.get('to')}"
                                                if row.get('event') == 'bundle.upgrade'
                                                else ''))
    else:
        _print([row for row in rows if not args.event or row.get('event') == args.event], args)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

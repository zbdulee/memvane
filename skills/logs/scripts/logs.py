#!/usr/bin/env python3
"""View MemVane install event logs and register unified-view targets.

usage: logs.py [--cwd DIR | --state-root DIR | --all] [--hours N] [--limit N]
               [--json] [--details] [--register PROJECT ...]
               [summary|recalls|errors|events] [--event NAME]

The state root comes from the project's selected route (the install
journal's directory); ``--state-root`` names one directly. ``--all`` merges
every target of the owner registry (``log_targets.resolve``, which re-checks
each route) in UTC order under one ``ALL_READ_LIMIT`` byte budget split
equally between readable targets; a target that cannot be read is reported
with its reason, never shown as empty. ``--register PROJECT`` (repeatable)
explicitly adds projects to that registry (the only write this tool makes).

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
from collections import Counter
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import sys
import unicodedata

sys.dont_write_bytecode = True
CORE = Path(__file__).resolve().parents[3] / 'core'
sys.path.insert(0, str(CORE))
sys.path.insert(0, str(CORE.parent))

from memvane.diagnostics import event_log  # noqa: E402
from memvane.registry import log_targets  # noqa: E402

ALL_READ_LIMIT = 16 * 1024 * 1024
MAX_ALL_TARGETS = 128
PART_EVENT = 'diagnostic.part'
HEADER_EVENT = 'diagnostic.header'
_DIAG_ID = re.compile(r'[0-9a-f]{32}\Z')
_SHA = re.compile(r'[0-9a-f]{64}\Z')
_KINDS = ('exception', 'origin_stack', 'child_stderr_tail')
# Only these header fields are shown without --details.
_HEADER_KEYS = ('ts', 'v', 'install', 'host', 'session', 'event', 'outcome', 'reason',
                'ms', 'dropped')
_GROUP_CAP = 200  # rows per diagnostic ID kept for reassembly (MAX_PARTS + header)
_UNSAFE = frozenset(('Cc', 'Cf', 'Cs', 'Zl', 'Zp'))
_NAMED = {'\n': '\\n', '\r': '\\r', '\t': '\\t'}
_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


# ------------------------------------------------------------ safe display

def _escape_char(ch):
    if ch in _NAMED:
        return _NAMED[ch]
    if unicodedata.category(ch) not in _UNSAFE:
        return ch
    n = ord(ch)
    if n < 0x100:
        return f'\\x{n:02x}'
    return f'\\u{n:04x}' if n < 0x10000 else f'\\U{n:08x}'


def _esc(value):
    """Terminal-safe text: control, format and line-separator chars become escapes."""
    text = str(value)
    if text.isprintable():
        return text
    return ''.join(_escape_char(ch) for ch in text)


def _display(value, depth=0):
    """A copy of a row whose strings are all terminal-safe (for text rendering)."""
    if isinstance(value, str):
        return _esc(value)
    if depth > 6:
        return None
    if isinstance(value, dict):
        return {_esc(key): _display(item, depth + 1) for key, item in value.items()}
    if isinstance(value, list):
        return [_display(item, depth + 1) for item in value]
    return value


def _json_line(row):
    """``json.dumps`` plus \\u-escapes for DEL, C1, format and separator chars.

    The parsed value is unchanged; only the printed form is safe to show.
    """
    text = json.dumps(row, ensure_ascii=False)
    if text.isprintable():
        return text
    out = []
    for ch in text:
        if ch != ' ' and unicodedata.category(ch) in _UNSAFE:
            n = ord(ch)
            if n > 0xFFFF:
                n -= 0x10000
                out.append(f'\\u{0xD800 + (n >> 10):04x}\\u{0xDC00 + (n & 0x3FF):04x}')
            else:
                out.append(f'\\u{n:04x}')
        else:
            out.append(ch)
    return ''.join(out)


def _visible(row):
    """The row as shown without --details, or None (a raw part is never shown)."""
    if not isinstance(row, dict):
        return None
    event = row.get('event')
    if event == PART_EVENT:
        return None
    if event == HEADER_EVENT:
        out = {key: row[key] for key in _HEADER_KEYS if key in row}
        ident = row.get('diagnostic_id')
        if isinstance(ident, str) and _DIAG_ID.fullmatch(ident):
            out['diagnostic_id'] = ident
        if row.get('kind') in _KINDS:
            out['kind'] = row['kind']
        for key in ('parts', 'bytes', 'diagnostic_version'):
            if _count(row.get(key)):
                out[key] = row[key]
        if isinstance(row.get('sha256'), str) and _SHA.fullmatch(row['sha256']):
            out['sha256'] = row['sha256']
        if type(row.get('truncated')) is bool:
            out['truncated'] = row['truncated']
        source = row.get('source_event')
        if isinstance(source, str) and event_log.code(source) == source:
            out['source_event'] = source
        return out
    prompt = row.get('prompt')
    if isinstance(prompt, dict) and 'text' in prompt:
        # Older installs could store prompt text; it is never shown.
        row = dict(row)
        row['prompt'] = {key: value for key, value in prompt.items() if key != 'text'}
    return row


def _groups(rows):
    """Diagnostic rows by ID, bounded; an overflowing ID maps to None (incomplete)."""
    groups = {}
    for row in rows:
        if not isinstance(row, dict) or row.get('event') not in (PART_EVENT, HEADER_EVENT):
            continue
        ident = row.get('diagnostic_id')
        if not isinstance(ident, str) or not _DIAG_ID.fullmatch(ident):
            continue
        bucket = groups.setdefault(ident, [])
        if bucket is None:
            continue
        if len(bucket) >= _GROUP_CAP:
            groups[ident] = None
            continue
        bucket.append(row)
    return groups


class _View:
    """How rows are printed: unified prefix, and explicit --details reassembly."""

    def __init__(self, unified=False, details=False):
        self.unified = unified
        self.details = details
        self.groups = []      # per target: diagnostic ID -> rows
        self.origin = {}      # id(display row) -> target index

    def detail(self, row, ident=None):
        ident = row.get('diagnostic_id') if ident is None else ident
        incomplete = {'status': 'incomplete', 'diagnostic_id': ident}
        if not isinstance(ident, str) or not _DIAG_ID.fullmatch(ident):
            return incomplete
        index = self.origin.get(id(row), 0)
        group = self.groups[index].get(ident) if index < len(self.groups) else None
        if not group:
            return incomplete
        try:
            from memvane.diagnostics import diagnostic_trace
            result = diagnostic_trace.reassemble(group, ident)
        except Exception:  # noqa: BLE001 - reported, never raised
            return {'status': 'unavailable', 'diagnostic_id': ident}
        if not isinstance(result, dict) or result.get('status') != 'complete':
            return incomplete
        return {'status': 'complete', 'diagnostic_id': ident, 'kind': result.get('kind'),
                'truncated': result.get('truncated'), 'text': result.get('text')}


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
    if row.get('severity') == 'warning':
        parts.append('severity=warning')
    if row.get('ms') is not None:
        parts.append(f"{row['ms']}ms")
    if row.get('event') == HEADER_EVENT:
        if row.get('kind') in _KINDS:
            parts.append(f"kind={row['kind']}")
        if _count(row.get('parts')):
            parts.append(f"parts={row['parts']}")
        if row.get('truncated') is True:
            parts.append('truncated')
    ident = row.get('diagnostic_id')
    if isinstance(ident, str) and _DIAG_ID.fullmatch(ident):
        parts.append(f'diagnostic={ident}')
    ids = row.get('diagnostic_ids')
    if isinstance(ids, list):
        parts.extend(f'diagnostic={item}' for item in ids
                     if isinstance(item, str) and _DIAG_ID.fullmatch(item))
    return ' '.join(str(part) for part in parts)


def _count(value):
    return type(value) is int and 0 <= value <= 1 << 62






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
        ids = []
        if view.details:
            candidates = [row.get('diagnostic_id')]
            if isinstance(row.get('diagnostic_ids'), list):
                candidates.extend(row['diagnostic_ids'])
            ids = list(dict.fromkeys(value for value in candidates
                                     if isinstance(value, str) and _DIAG_ID.fullmatch(value)))
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


def summary(rows, args):
    window = _since(rows, args.hours)
    label = f'last {args.hours}h' if args.hours is not None else 'all retained'
    print(f'events ({label}): {len(window)}')
    counts = Counter((str(row.get('event')), str(row.get('outcome'))) for row in window)
    for (event, outcome), count in sorted(counts.items(), key=lambda item: (str(item[0][0]),
                                                                           str(item[0][1]))):
        print(f'  {_esc(event)} {_esc(outcome)}: {count}')
    recalls = [row for row in window if row.get('event') == 'recall']
    if recalls:
        empty = [row for row in recalls if row.get('outcome') != 'served']
        reasons = Counter(str(row.get('reason')) for row in empty)
        print(f"recalls: {len(recalls)} served={len(recalls) - len(empty)} "
              f"empty/refused={len(empty)}"
              + (' (' + ', '.join(f'{_esc(k)} {v}' for k, v in reasons.most_common(5)) + ')'
                 if reasons else ''))
        degraded = Counter(str(row.get('degraded') or row.get('vector_fallback'))
                           for row in recalls
                           if row.get('degraded') or row.get('vector_fallback'))
        if degraded:
            print('degraded recalls: ' + ', '.join(f'{_esc(k)} {v}'
                                                   for k, v in degraded.items()))
    for name, test in (('last publish', lambda row: row.get('event') == 'index.publish'),
                       ('last error', lambda row: row.get('severity') != 'warning' and
                        (row.get('outcome') in FAILED or row.get('event') == 'hook.error'))):
        found = [row for row in rows if test(row)]
        if found:
            row = _display(found[-1])
            detail = ''
            if row.get('event') == 'index.publish':
                detail = f" generation={row.get('generation')}"
            print(f'{name}: {_short(row)}{detail}')
    dropped = sum(row.get('dropped', 0) for row in window if type(row.get('dropped')) is int)
    if dropped:
        print(f'lines a process could not write before a later one: {dropped}')


def _show(rows, args, view):
    """Render one view of already-visible rows (shared by single and --all)."""
    if args.view == 'summary':
        if args.hours is None:
            args.hours = 24.0
        summary(rows, args)
        return
    rows = _since(rows, args.hours)
    if args.view == 'recalls':
        _print([row for row in rows if row.get('event') == 'recall'], args, _recall_line, view)
    elif args.view == 'errors':
        _print([row for row in rows if row.get('severity') != 'warning' and
                (row.get('outcome') in FAILED or row.get('event') == 'hook.error')],
               args, _short, view)
    else:
        _print([row for row in rows if not args.event or row.get('event') == args.event],
               args, _short, view)


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
    args = parser.parse_args(argv)
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
    view.groups.append(_groups(raw) if args.details else {})
    rows = [row for row in map(_visible, raw) if row is not None]
    _show(rows, args, view)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

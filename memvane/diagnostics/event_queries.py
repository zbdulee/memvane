"""Pure reads over install event rows: visibility, windows, view filters and safe display.

Nothing here opens a file. The rows come from ``event_log.read`` (or any list
of the same dicts) and are never changed in place.

Visibility: a ``diagnostic.part`` row is never shown, a ``diagnostic.header``
keeps only its fixed metadata, and a stored ``prompt.text`` (older installs)
is dropped. Display: every string a terminal prints is escaped (control,
format, surrogate and line/paragraph separator characters, Bidi controls
included, become ``\\xNN``-style text), so stored content cannot fake rows or
drive the terminal. ``reassemble`` is the only way to read a diagnostic's
text, and only when its caller asks for it.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import re
import unicodedata

from memvane.diagnostics import event_log

PART_EVENT = 'diagnostic.part'
HEADER_EVENT = 'diagnostic.header'
DIAG_ID = re.compile(r'[0-9a-f]{32}\Z')
_SHA = re.compile(r'[0-9a-f]{64}\Z')
KINDS = ('exception', 'origin_stack', 'child_stderr_tail')
# Only these header fields are shown without an explicit reassembly.
_HEADER_KEYS = ('ts', 'v', 'install', 'host', 'session', 'event', 'outcome', 'reason',
                'ms', 'dropped')
_GROUP_CAP = 200  # rows per diagnostic ID kept for reassembly (MAX_PARTS + header)
_UNSAFE = frozenset(('Cc', 'Cf', 'Cs', 'Zl', 'Zp'))
_NAMED = {'\n': '\\n', '\r': '\\r', '\t': '\\t'}
EPOCH = datetime.min.replace(tzinfo=timezone.utc)
FAILED = frozenset(('refused', 'failed', 'error', 'interrupted', 'exited'))
VIEWS = ('recalls', 'errors', 'events')


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


def esc(value):
    """Terminal-safe text: control, format and line-separator chars become escapes."""
    text = str(value)
    if text.isprintable():
        return text
    return ''.join(_escape_char(ch) for ch in text)


def display(value, depth=0):
    """A copy of a row whose strings are all terminal-safe (for text rendering)."""
    if isinstance(value, str):
        return esc(value)
    if depth > 6:
        return None
    if isinstance(value, dict):
        return {esc(key): display(item, depth + 1) for key, item in value.items()}
    if isinstance(value, list):
        return [display(item, depth + 1) for item in value]
    return value


def json_line(row):
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


# ------------------------------------------------------------ visibility

def count(value):
    return type(value) is int and 0 <= value <= 1 << 62


def visible(row):
    """The row as shown without explicit reassembly, or None (a raw part is never shown)."""
    if not isinstance(row, dict):
        return None
    event = row.get('event')
    if event == PART_EVENT:
        return None
    if event == HEADER_EVENT:
        out = {key: row[key] for key in _HEADER_KEYS if key in row}
        ident = row.get('diagnostic_id')
        if isinstance(ident, str) and DIAG_ID.fullmatch(ident):
            out['diagnostic_id'] = ident
        if row.get('kind') in KINDS:
            out['kind'] = row['kind']
        for key in ('parts', 'bytes', 'diagnostic_version'):
            if count(row.get(key)):
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


def diagnostic_ids(row):
    """The valid diagnostic IDs a row names (``diagnostic_id`` then ``diagnostic_ids``), once each."""
    candidates = [row.get('diagnostic_id')]
    if isinstance(row.get('diagnostic_ids'), list):
        candidates.extend(row['diagnostic_ids'])
    return list(dict.fromkeys(value for value in candidates
                              if isinstance(value, str) and DIAG_ID.fullmatch(value)))


def diagnostic_groups(rows):
    """Diagnostic rows by ID, bounded; an overflowing ID maps to None (incomplete)."""
    groups = {}
    for row in rows:
        if not isinstance(row, dict) or row.get('event') not in (PART_EVENT, HEADER_EVENT):
            continue
        ident = row.get('diagnostic_id')
        if not isinstance(ident, str) or not DIAG_ID.fullmatch(ident):
            continue
        bucket = groups.setdefault(ident, [])
        if bucket is None:
            continue
        if len(bucket) >= _GROUP_CAP:
            groups[ident] = None
            continue
        bucket.append(row)
    return groups


def reassemble(rows, diagnostic_id):
    """``{status, diagnostic_id[, kind, truncated, text]}`` for one diagnostic of raw ``rows``.

    ``complete`` only when the header and every part agree; ``incomplete`` for
    an invalid ID, a missing or overflowing group and any defect;
    ``unavailable`` when the reassembler itself failed.
    """
    incomplete = {'status': 'incomplete', 'diagnostic_id': diagnostic_id}
    if not isinstance(diagnostic_id, str) or not DIAG_ID.fullmatch(diagnostic_id):
        return incomplete
    group = diagnostic_groups(rows if isinstance(rows, (list, tuple)) else ()).get(diagnostic_id)
    if not group:
        return incomplete
    try:
        from memvane.diagnostics import diagnostic_trace
        result = diagnostic_trace.reassemble(group, diagnostic_id)
    except Exception:  # noqa: BLE001 - reported, never raised
        return {'status': 'unavailable', 'diagnostic_id': diagnostic_id}
    if not isinstance(result, dict) or result.get('status') != 'complete':
        return incomplete
    return {'status': 'complete', 'diagnostic_id': diagnostic_id, 'kind': result.get('kind'),
            'truncated': result.get('truncated'), 'text': result.get('text')}


# ------------------------------------------------------------ windows and views

def ts(row):
    """Aware UTC timestamp of a record, or None (a naive one is read as UTC)."""
    try:
        value = datetime.fromisoformat(str(row.get('ts')).replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def since(rows, hours, now=None):
    """Rows of the last ``hours`` (all when None); a row without a readable time is kept."""
    if hours is None:
        return rows
    floor = (now or datetime.now(timezone.utc)) - timedelta(hours=hours)
    return [row for row in rows if (ts(row) or floor) >= floor]


def is_error(row):
    return row.get('severity') != 'warning' and (row.get('outcome') in FAILED
                                                 or row.get('event') == 'hook.error')


def query(rows, view=None, *, hours=None, event=None, raw=False, now=None):
    """Rows of one view in log order.

    ``raw`` rows pass ``visible`` first. ``hours`` keeps the window; ``view``
    is ``recalls`` (prompt recalls), ``errors`` (failures that are not
    warnings, and ``hook.error``), ``events`` or None (every row, of one
    ``event`` name when given).
    """
    if view is not None and view not in VIEWS:
        raise ValueError(f'unknown view: {view}')
    if raw:
        rows = [row for row in map(visible, rows) if row is not None]
    rows = since(rows, hours, now)
    if view == 'recalls':
        return [row for row in rows if row.get('event') == 'recall']
    if view == 'errors':
        return [row for row in rows if is_error(row)]
    return [row for row in rows if not event or row.get('event') == event]


def short(row):
    """One text line of a (display-escaped) row: time, event, outcome and the fixed fields."""
    parts = [str(row.get('ts', ''))[:19].replace('T', ' '), row.get('event'),
             row.get('outcome') or '-']
    if row.get('reason'):
        parts.append(f"reason={row['reason']}")
    if row.get('severity') == 'warning':
        parts.append('severity=warning')
    if row.get('ms') is not None:
        parts.append(f"{row['ms']}ms")
    if row.get('event') == HEADER_EVENT:
        if row.get('kind') in KINDS:
            parts.append(f"kind={row['kind']}")
        if count(row.get('parts')):
            parts.append(f"parts={row['parts']}")
        if row.get('truncated') is True:
            parts.append('truncated')
    ident = row.get('diagnostic_id')
    if isinstance(ident, str) and DIAG_ID.fullmatch(ident):
        parts.append(f'diagnostic={ident}')
    ids = row.get('diagnostic_ids')
    if isinstance(ids, list):
        parts.extend(f'diagnostic={item}' for item in ids
                     if isinstance(item, str) and DIAG_ID.fullmatch(item))
    return ' '.join(str(part) for part in parts)

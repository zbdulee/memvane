"""Read the public legacy ledgers before a mutation can discard an old physical LF row."""
from __future__ import annotations

from functools import lru_cache
import json
import os
from pathlib import Path
import stat

from memvane.settings.api import compile_dir
from memvane.wiki.card import manifest_scan, types as cards

MANIFEST = 'generated-manifest.jsonl'
LIMIT = 8 * 1024 ** 2
SLAB = 65536
LINES = 4096
SEPARATORS = frozenset(('\x85', '\u2028', '\u2029'))


def _node(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
        raise OSError('unsafe_legacy_ledger')
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _unique(pairs):
    obj = dict(pairs)
    if len(obj) != len(pairs):
        raise ValueError('ambiguous_legacy_row')
    return obj


def _invalid_constant(value):
    raise ValueError('invalid_legacy_json_number')


def _rows(path, node):
    if node is None:
        return
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW), 'rb') as stream:
        info = os.fstat(stream.fileno())
        if (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns) != node:
            raise OSError('legacy_generation_changed')
        for index in range(LINES + 1):
            raw = stream.readline(SLAB)
            if not raw:
                break
            if index == LINES or not raw.endswith(b'\n'):
                raise ValueError('incomplete_or_oversized_legacy_row')
            if not raw.strip():
                continue
            row = json.loads(raw.decode('utf8'), object_pairs_hook=_unique, parse_constant=_invalid_constant)
            if not isinstance(row, dict):
                raise ValueError('unknown_legacy_row')
            yield row
    if _node(path) != node:
        raise OSError('legacy_generation_changed')


def _special(value):
    if isinstance(value, dict):
        return any(_special(k) or _special(v) for k, v in value.items())
    if isinstance(value, list):
        return any(_special(v) for v in value)
    return isinstance(value, str) and any(c in value for c in SEPARATORS)


def _sources(row):
    return {ref['path'] for field in ('sources', 'sourceRevisions') for ref in row.get(field, [])
            if isinstance(ref, dict) and isinstance(ref.get('path'), str)}


def _connected(identities, mixed):
    """Compute the transitive closure of the released compactor's symmetric matching rule."""
    groups = {}
    nonexplicit = set()
    for index, identity in enumerate(identities):
        for field in (0, 2, 3):
            key = field, identity[field]
            if key[1]:
                groups.setdefault(key, []).append(index)
                if field == 0 or identity[1] != 'explicit':
                    nonexplicit.add(key)
    pending = list(mixed)
    while pending:
        identity = identities[pending.pop()]
        for field in (0, 2, 3):
            key = field, identity[field]
            if key in nonexplicit:
                for index in groups.pop(key, ()):
                    if index not in mixed:
                        mixed.add(index)
                        pending.append(index)
    return mixed


def event_rows(path, node):
    from memvane.wiki.card import wiki_freshness as events
    if node and node[2] > LIMIT:
        raise ValueError('legacy_event_limit')
    rows = list(_rows(path, node))
    if (any(not events._valid_event(row) or not events._valid_event_time(row.get('ts')) for row in rows)
            or events._ordered_latest_strict(rows) is None):
        raise ValueError('ambiguous_legacy_events')
    return rows


def read_public_events(path):
    return event_rows(path, _node(path))


def _manifest_projection(row):
    target = row.get('targetPath')
    if (not isinstance(target, str) or not target or
            any(row.get(key) is not None and not isinstance(row[key], str)
                for key in ('sourceHash', 'canonicalKey', 'targetResolution', 'action', 'status')) or
            any(not isinstance(row.get(key, []), list) for key in ('sources', 'sourceRevisions')) or
            row.get('action') not in ('created', 'updated', 'verify-deleted')):
        raise ValueError('unknown_legacy_manifest')
    return _identity(row), tuple(sorted(_sources(row))), _special(row)


def _manifest_batch(path, node, start):
    rows = set()
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW), 'rb') as stream:
        info = os.fstat(stream.fileno())
        if (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns) != node:
            raise OSError('legacy_generation_changed')
        stream.seek(start)
        offset = start
        end = min(start + LIMIT, node[2])
        for _ in range(LINES):
            if offset == end:
                break
            raw = stream.readline(min(SLAB, end - offset))
            if not raw.endswith(b'\n'):
                if stream.tell() == end < node[2] and len(raw) < SLAB:
                    break  # Retry the whole physical row at the start of the next pass.
                raise ValueError('incomplete_or_oversized_legacy_row')
            if raw.strip():
                row = json.loads(raw.decode('utf8'), object_pairs_hook=_unique, parse_constant=_invalid_constant)
                if not isinstance(row, dict):
                    raise ValueError('unknown_legacy_row')
                rows.add(_manifest_projection(row))
            offset = stream.tell()
    if _node(path) != node:
        raise OSError('legacy_generation_changed')
    return offset, rows


@lru_cache(maxsize=4)
def _inspect(folder, manifest_node, event_node):
    identities, sources, mixed = [], [], set()
    for index, (identity, refs, special) in enumerate(manifest_scan.inspect(folder / MANIFEST, manifest_node, _manifest_batch)):
        identities.append(tuple(identity))
        sources.append(set(refs))
        if special:
            mixed.add(index)
    ledger = event_rows(folder / cards.SOURCE_REFRESH_PENDING, event_node)
    mixed_ids = {row.get('eventId', row.get('pendingEventId')) for row in ledger if _special(row)}
    unsafe_sources = {row['sourcePath'] for row in ledger if row.get('eventId') in mixed_ids}
    mixed.update(index for index, refs in enumerate(sources) if not refs.isdisjoint(unsafe_sources))
    if mixed:
        for index in _connected(identities, mixed):
            unsafe_sources.update(sources[index])
    return ({identity[0] for identity in identities}, tuple(identities[i] for i in mixed), unsafe_sources)


def _identity(row):
    return tuple(row.get(field) for field in ('targetPath', 'targetResolution', 'sourceHash', 'canonicalKey'))


def _matches(a, b):
    if a[0] and a[0] == b[0]:
        return True
    return ((a[1] != 'explicit' or b[1] != 'explicit') and
            any(a[i] and a[i] == b[i] for i in (2, 3)))


@lru_cache(maxsize=4)
def _receipts(folder, audit_node, deleted_node):
    if sum(n[2] for n in (audit_node, deleted_node) if n) > LIMIT:
        raise ValueError('legacy_evidence_limit')
    audits = []
    for row in _rows(folder / 'candidates.jsonl', audit_node):
        if row.get('action') in ('created', 'updated', 'tombstoned', 'suppressed') and row.get('targetPath'):
            audits.append(_identity(row))
    deleted = set()
    mixed = []
    for row in _rows(folder / 'tombstones.jsonl', deleted_node):
        target = row.get('targetPath')
        if not isinstance(target, str) or not target:
            raise ValueError('unknown_legacy_deletion')
        deleted.add(target)
        if _special(row):
            mixed.append(_identity(row))
    return audits, deleted, mixed


def writable(folder, missing_target=None, *, append_for=None, trimming_candidates=False):
    """Keep surviving evidence at its original name unless a reviewed target can proceed."""
    try:
        folder = Path(folder)
        if folder.resolve() != folder:
            return False
        manifest = folder / MANIFEST
        event = folder / cards.SOURCE_REFRESH_PENDING
        nodes = (_node(manifest), _node(event))
        targets, protected, unsafe_sources = _inspect(folder, *nodes)
        if protected or unsafe_sources:
            if (append_for is None or not _sources(append_for).isdisjoint(unsafe_sources) or
                    any(_matches(row, _identity(append_for)) for row in protected)):
                return False
        if missing_target is not None or trimming_candidates:
            paths = (folder / 'candidates.jsonl', folder / 'tombstones.jsonl')
            evidence_nodes = tuple(_node(path) for path in paths)
            audits, deleted, mixed = _receipts(folder, *evidence_nodes)
            query = _identity(missing_target) if missing_target is not None else None
            if query and any(_matches(row, query) for row in mixed):
                return False
            for row in audits:
                if row[0] not in targets and row[0] not in deleted and (trimming_candidates or _matches(row, query)):
                    return False
            if evidence_nodes != tuple(_node(path) for path in paths):
                return False
        return nodes == (_node(manifest), _node(event))
    except (OSError, ValueError, TypeError, RecursionError):
        return False


def project_writable(root, missing_target=None, *, append_for=None, trimming_candidates=False):
    folder = compile_dir(root)
    return folder is not None and writable(folder, missing_target, append_for=append_for, trimming_candidates=trimming_candidates)


def quarantine_preserves_special(path):
    """The generic corrupt-row quarantine must leave a mixed legacy generation at its original name."""
    try:
        folder = path.parent
        if not writable_manifest(folder):
            return True
        node = _node(path)
        if node is None:
            return False
        if node[2] > LIMIT:
            return True
        for row in _rows(path, node):
            if _special(row):
                return True
        return False
    except (ValueError, UnicodeError, RecursionError):
        # A torn ordinary ledger may still contain a later literal or escaped path.
        try:
            return _contains_separator(path)
        except OSError:
            return True
    except OSError:
        return True


def _contains_separator(path):
    needle = tuple(bytes(c, 'utf-8') for c in SEPARATORS) + (b'\\u0085', b'\\u2028', b'\\u2029')
    node = _node(path)
    if node is None or node[2] > LIMIT:
        return True
    tail = b''
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW), 'rb') as stream:
        info = os.fstat(stream.fileno())
        if (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns) != node:
            return True
        for _ in range(0, node[2], SLAB):
            block = tail + stream.read(SLAB)
            if any(n in block for n in needle):
                return True
            tail = block[-5:]
    return _node(path) != node


def writable_manifest(folder):
    node = _node(folder / MANIFEST)
    if node is None:
        return True
    try:
        _, protected, _ = _inspect(folder, node, None)
        return not protected
    except (OSError, ValueError, TypeError, RecursionError):
        return False

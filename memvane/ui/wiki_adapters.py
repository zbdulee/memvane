"""Read-only Wiki browser DTOs: card list, card detail and side-by-side source.

Every request proves its target again through ``read_adapters.resolve_target``.
Cards and sources are read from the proved checkout itself (main or linked
worktree); the compile ledger is install-wide and is read from the main
checkout only (``ledgerFrom``). Nothing here writes, locks or starts a process.

Path boundary: the proved target root is opened first (system symlinks above
it, such as macOS ``/tmp`` or ``/var``, are allowed), then each relative
component below it is opened ``O_NOFOLLOW | O_NONBLOCK`` from its parent
directory descriptor, so a symlink inside the checkout never leads out of it.
"""
from __future__ import annotations

from datetime import datetime, timezone
import errno
import math
import os
from pathlib import PurePosixPath
import re
import stat

from memvane.ui.read_adapters import TargetError, resolve_target
from memvane.ui.types import CARD_SCAN_LIMIT, parse_target_id

WIKI_PARTS = ('.memvane', 'wiki')
CARD_STATUSES = ('verified', 'generated')
LIST_FILTERS = ('all', 'verified', 'generated', 'pending', 'stale', 'hidden', 'over_budget')
TOKEN_BUDGET = 700
MAX_LIST = 1000
MAX_QUERY_CHARS = 256
MAX_REL_CHARS = 1024
CARD_LIMIT = 256 * 1024
SOURCE_LIMIT = 256 * 1024
MAX_HIGHLIGHTS = 64
OPEN_JOB_STATES = ('queued', 'claimed', 'deferred')
LEDGER_OPEN = {
    'compileJobs': OPEN_JOB_STATES,
    'verifyJobs': OPEN_JOB_STATES,
    'publishTxns': ('publishing',),
    'refreshEvents': ('pending',),
    'dedupPairs': ('queued', 'claimed'),
}
AUTO_START = re.compile(r'<!-- qmd:auto:start[^\n]*?-->[ \t]*\n?')
AUTO_END = re.compile(r'\n?<!-- qmd:auto:end -->[ \t]*\n?')
# Tokenizer-free estimate per character class (the learning budget's guidance rule).
_CLASS = re.compile(r'[0-9]|[A-Za-z]+|\n|[ \t]+|[\x00-\x7f]|.', re.S)
_LINE_BREAKS = ('\x85', '\u2028', '\u2029')
_O_NOFOLLOW = getattr(os, 'O_NOFOLLOW', 0)
_O_NONBLOCK = getattr(os, 'O_NONBLOCK', 0)
_O_DIRECTORY = getattr(os, 'O_DIRECTORY', 0)


# ----------------------------------------------------------------- paths


def _rel_parts(rel, *, suffix=None):
    """Components of a checkout-relative POSIX path, or ``TargetError('rel_invalid', 400)``."""
    if not isinstance(rel, str) or not rel or len(rel) > MAX_REL_CHARS:
        raise TargetError('rel_invalid', 400)
    if '\x00' in rel or '\\' in rel or rel.startswith('/') or any(ch in rel for ch in _LINE_BREAKS):
        raise TargetError('rel_invalid', 400)
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in rel):
        raise TargetError('rel_invalid', 400)
    parts = rel.split('/')
    if any(part in ('', '.', '..') for part in parts):
        raise TargetError('rel_invalid', 400)
    if suffix is not None and not parts[-1].endswith(suffix):
        raise TargetError('rel_invalid', 400)
    return parts


def _open_dir(root):
    """The target root directory (ancestor symlinks resolved by the kernel)."""
    return os.open(root, os.O_RDONLY | _O_DIRECTORY | _O_NONBLOCK)


def _open_below(root, parts, *, directory=False):
    """A descriptor of ``root/<parts>`` opened one no-follow component at a time."""
    fd = _open_dir(root)
    try:
        for index, part in enumerate(parts):
            last = index == len(parts) - 1
            flags = os.O_RDONLY | _O_NOFOLLOW | _O_NONBLOCK
            if not last or directory:
                flags |= _O_DIRECTORY
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _read_fd(fd, limit):
    """``(bytes up to limit, truncated, stat)`` of a regular file descriptor."""
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        raise TargetError('not_a_regular_file', 400)
    chunks, left = [], limit + 1
    while left > 0:
        chunk = os.read(fd, min(left, 1 << 16))
        if not chunk:
            break
        chunks.append(chunk)
        left -= len(chunk)
    data = b''.join(chunks)
    return data[:limit], len(data) > limit, info


def _read_below(root, parts, limit, *, missing='file_not_found'):
    try:
        fd = _open_below(root, parts)
    except FileNotFoundError:
        raise TargetError(missing, 404) from None
    except NotADirectoryError:
        raise TargetError('path_escape_refused', 403) from None
    except OSError as exc:
        # ELOOP (EMLINK on some BSDs) is a symlink met with O_NOFOLLOW.
        escaped = exc.errno in (errno.ELOOP, errno.EMLINK, errno.ENOTDIR)
        raise TargetError('path_escape_refused' if escaped else 'file_unreadable', 403) from None
    try:
        return _read_fd(fd, limit)
    finally:
        os.close(fd)


def _wiki_root_safe(root):
    """True when ``.memvane`` and ``.memvane/wiki`` are real directories (no symlink)."""
    try:
        fd = _open_below(root, WIKI_PARTS, directory=True)
    except OSError:
        return False
    os.close(fd)
    return True


def _iso(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat(timespec='seconds')


# ----------------------------------------------------------------- settings


def _skip_paths(root):
    from memvane.settings import api as settings
    try:
        found = settings.generation_skip_paths(settings.load_project_config(str(root)))
    except Exception:  # noqa: BLE001 - unreadable settings add no skip path
        return []
    return [item for item in found or () if isinstance(item, str) and item]


def _merged_skip(target, main_root):
    merged = list(_skip_paths(target['targetRoot']))
    if main_root and main_root != target['targetRoot']:
        merged.extend(item for item in _skip_paths(main_root) if item not in merged)
    return merged


def _main_root(target):
    """The proved main checkout root of ``target``'s install, or None."""
    if target['kind'] == 'main':
        return target['targetRoot']
    try:
        return resolve_target(target['installId'], home=target['_home'])['targetRoot']
    except TargetError:
        return None


def _target(target_id, home):
    target = dict(resolve_target(target_id, home=home))
    target['_home'] = home
    return target


# ----------------------------------------------------------------- card text


def estimate_tokens(text):
    """Tokenizer-free estimate: digit 1, letter run ceil(n/4), newline 1, blank 0.5, Hangul 1."""
    total = 0.0
    for match in _CLASS.finditer(text):
        group = match.group(0)
        char = group[0]
        if '0' <= char <= '9':
            total += 1
        elif char.isascii() and char.isalpha():
            total += math.ceil(len(group) / 4)
        elif char == '\n':
            total += 1
        elif char in ' \t':
            total += 0.5 * len(group)
        elif char.isascii() or '\uac00' <= char <= '\ud7a3':
            total += 1
        elif ord(char) > 0xFFFF:
            total += 4
        else:
            total += 2
    return math.ceil(total)


def _parse_card(data):
    """``{block, body, scalars, meta}`` of card bytes (frontmatter-less cards have an empty block)."""
    from memvane.wiki.card import api as card
    text = card.normalize_newlines(data.decode('utf-8', 'replace'))
    block = card.frontmatter_block(text)
    if block is None:
        return {'text': text, 'block': '', 'body': text, 'scalars': {}, 'meta': {}}
    body = text[len('---\n') + len(block) + len('\n---\n'):]
    meta, ok = card.parse_frontmatter(text)
    return {'text': text, 'block': block, 'body': body,
            'scalars': card.parse_frontmatter_scalars(block), 'meta': meta if ok else {}}


def _revisions(block):
    from memvane.wiki.card import api as card
    try:
        found = card.frontmatter_source_revisions(block) if block else []
    except Exception:  # noqa: BLE001 - an unreadable provenance list reads as none
        return []
    return [item for item in found or () if isinstance(item, dict) and isinstance(item.get('path'), str)]


def _spans(block):
    from memvane.wiki.card import api as card
    try:
        state, spans = card.read_card_spans(block) if block else ('absent', [])
    except Exception:  # noqa: BLE001 - an invalid span list shows no highlight
        return []
    if state != 'present':
        return []
    return [{'path': span['path'], 'startLine': int(span['startLine']), 'endLine': int(span['endLine'])}
            for span in spans]


def _string_list(value):
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _stale_reason(revisions, root, config):
    """``source_deleted``, ``source_changed`` or None from ``wiki.card.check_card``.

    Only a ``stale`` card has a reason; ``unknown`` freshness is not shown as
    stale. ``check_card`` stops at the first stale source, so a stale card of
    several sources is checked source by source for a deleted one.
    """
    from memvane.wiki.card import api as card
    if not revisions:
        return None
    try:
        allow = card.allow_roots_of(config)
        state = card.check_card({'sourceRevisions': revisions}, root, allow)
        if not isinstance(state, dict) or state.get('state') != 'stale':
            return None
        if state.get('reason') == 'missing':
            return 'source_deleted'
        for revision in revisions if len(revisions) > 1 else ():
            one = card.check_card({'sourceRevisions': [revision]}, root, allow)
            if isinstance(one, dict) and one.get('state') == 'stale' and one.get('reason') == 'missing':
                return 'source_deleted'
    except Exception:  # noqa: BLE001 - an unknown freshness is not shown as stale
        return None
    return 'source_changed'


def _is_denied_rel(rel, parts=None):
    from memvane.settings import api as settings
    head = parts[0] if parts else (rel.split('/', 1)[0] if isinstance(rel, str) else '')
    return (head.lower() in WIKI_PARTS[:1]
            or settings.is_denied_source_path(rel)
            or settings.is_denied_source_path(rel.lower()))


def _is_skipped_rel(rel, skip):
    from memvane.settings import api as settings
    if settings.excluded_source(rel, skip, wiki_source=True):
        return True
    lower_skip = [entry.lower() for entry in (skip or ()) if isinstance(entry, str)]
    return settings.excluded_source(rel.lower(), lower_skip, wiki_source=True)


def _is_hidden(paths, skip):
    for path in paths:
        if _is_denied_rel(path) or _is_skipped_rel(path, skip):
            return True
    return False


def split_blocks(body):
    """``[{kind: 'auto'|'manual', markdown}]`` split at the ``qmd:auto`` markers (markers dropped)."""
    blocks, position = [], 0
    while True:
        start = AUTO_START.search(body, position)
        if start is None:
            break
        end = AUTO_END.search(body, start.end())
        if end is None:
            break
        before = body[position:start.start()]
        if before.strip():
            blocks.append({'kind': 'manual', 'markdown': before.strip('\n')})
        inside = body[start.end():end.start()]
        if inside.strip():
            blocks.append({'kind': 'auto', 'markdown': inside.strip('\n')})
        position = end.end()
    rest = body[position:]
    if rest.strip():
        blocks.append({'kind': 'manual', 'markdown': rest.strip('\n')})
    return blocks


# ----------------------------------------------------------------- scanning


def _scan_cards(root, *, limit=CARD_SCAN_LIMIT):
    """``([(rel, path)], truncated)`` of the checkout's wiki cards, symlinks never followed."""
    from memvane.settings import api as settings
    if not _wiki_root_safe(root):
        return [], False
    base = os.path.join(root, *WIKI_PARTS)
    found, stack = [], [('', base)]
    while stack:
        prefix, directory = stack.pop()
        try:
            entries = sorted(os.scandir(directory), key=lambda item: item.name)
        except OSError:
            continue
        for entry in entries:
            rel = prefix + entry.name
            if entry.name.startswith('.'):
                continue
            if entry.is_dir(follow_symlinks=False):
                stack.append((rel + '/', entry.path))
                continue
            if not entry.name.endswith('.md') or not entry.is_file(follow_symlinks=False):
                continue
            if settings.is_wiki_meta_page(rel):
                continue
            if len(found) >= limit:
                return sorted(found), True
            found.append((rel, entry.path))
    return sorted(found), False


def _open_publishes(main_root):
    from memvane.wiki.pipeline import api as pipeline
    try:
        with pipeline.open_pipeline(main_root, readonly=True) as store:
            rows = store.open_publish_txns(state='publishing')
    except Exception:  # noqa: BLE001 - an absent or unreadable ledger has no open publish
        return set()
    return {PurePosixPath(str(row.get('target_path'))).as_posix() for row in rows
            if isinstance(row, dict) and row.get('target_path')}


def ledger_summary(main_root):
    """Open-work counts of the main checkout's wiki ledger; zeros when absent or unreadable."""
    from memvane.wiki.pipeline import api as pipeline
    counts = {key: 0 for key in LEDGER_OPEN}
    if not main_root:
        return counts
    try:
        summary = pipeline.pipeline_summary(main_root)
    except Exception:  # noqa: BLE001 - shown as zero, never raised
        return counts
    tables = summary.get('tables') if isinstance(summary, dict) else None
    if not isinstance(tables, dict):
        return counts
    for key, states in LEDGER_OPEN.items():
        by_state = (tables.get(key) or {}).get('byState') or {}
        counts[key] = sum(by_state.get(state, 0) for state in states
                          if isinstance(by_state.get(state, 0), int))
    return counts


def _card_meta(rel, path, root, *, config, skip, publishing):
    from memvane.ui.read_adapters import _read_small
    try:
        raw = _read_small(path, CARD_LIMIT)
        mtime = os.lstat(path).st_mtime
    except OSError:
        return None
    parsed = _parse_card(raw[:CARD_LIMIT])
    status = parsed['scalars'].get('status')
    if status not in CARD_STATUSES:
        return None
    revisions = _revisions(parsed['block'])
    paths = [item['path'] for item in revisions] + [span['path'] for span in _spans(parsed['block'])]
    tokens = estimate_tokens(parsed['body'])
    stale_reason = _stale_reason(revisions, root, config)
    title = parsed['scalars'].get('title')
    return {
        'rel': rel,
        'title': title if isinstance(title, str) and title.strip() else PurePosixPath(rel).stem,
        'status': status,
        'tokens': tokens,
        'overBudget': tokens > TOKEN_BUDGET or len(raw) > CARD_LIMIT,
        'stale': stale_reason is not None,
        'staleReason': stale_reason,
        'hidden': _is_hidden(paths, skip),
        'updatedAt': _iso(mtime),
        '_pending': status == 'generated' or rel in publishing,
        '_text': parsed['text'].lower(),
    }


def _keep(meta, status, needle):
    if status == 'verified' and meta['status'] != 'verified':
        return False
    if status == 'generated' and meta['status'] != 'generated':
        return False
    if status == 'pending' and not meta['_pending']:
        return False
    if status in ('stale', 'hidden') and not meta[status]:
        return False
    if status == 'over_budget' and not meta['overBudget']:
        return False
    return not needle or needle in meta['_text'] or needle in meta['rel'].lower()


def _list_args(status, q, limit):
    status = status or 'all'
    if status not in LIST_FILTERS:
        raise TargetError('status_invalid', 400)
    if not isinstance(q, str) or len(q) > MAX_QUERY_CHARS:
        raise TargetError('q_invalid', 400)
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        raise TargetError('limit_invalid', 400) from None
    if not 0 < limit <= MAX_LIST:
        raise TargetError('limit_invalid', 400)
    return status, q.strip().lower(), limit


# ----------------------------------------------------------------- endpoints


def wiki_list(target_id, *, status='', q='', limit=200, home=None):
    """WikiListResponse DTO matching ``ui/src/api/types.ts``."""
    from memvane.settings import api as settings
    status, needle, limit = _list_args(status, q, limit)
    target = _target(target_id, home)
    root = target['targetRoot']
    main_root = _main_root(target)
    try:
        config = settings.load_project_config(str(root))
    except Exception:  # noqa: BLE001 - freshness then reads with no extra allow roots
        config = {}
    skip = _merged_skip(target, main_root)
    publishing = _open_publishes(main_root) if target['kind'] == 'main' and main_root else set()
    entries, truncated = _scan_cards(root)
    cards = []
    for rel, path in entries:
        meta = _card_meta(rel, path, root, config=config, skip=skip, publishing=publishing)
        if meta is None or not _keep(meta, status, needle):
            continue
        if len(cards) >= limit:
            truncated = True
            break
        cards.append({key: value for key, value in meta.items() if not key.startswith('_')})
    parsed = parse_target_id(target_id)
    return {
        'targetId': target_id,
        'ledgerFrom': parsed[0],
        'cards': cards,
        'truncated': truncated,
        'ledger': ledger_summary(main_root),
    }


def _resolve_related(names, root):
    if not names:
        return []
    entries, _ = _scan_cards(root)
    rels = {rel for rel, _ in entries}
    stems = {}
    for rel in rels:
        stems.setdefault(PurePosixPath(rel).stem, []).append(rel)
    out = []
    for name in names:
        key = name[2:-2].strip() if name.startswith('[[') and name.endswith(']]') else name
        if key in rels:
            out.append(key)
        elif key + '.md' in rels:
            out.append(key + '.md')
        elif len(stems.get(key, ())) == 1:
            out.append(stems[key][0])
        else:
            out.append(key)
    return out


def wiki_card(target_id, rel, *, home=None):
    """WikiCardDetail DTO: frontmatter fields and the auto/manual blocks of one card."""
    parts = _rel_parts(rel, suffix='.md')
    target = _target(target_id, home)
    root = target['targetRoot']
    effective_rel = '/'.join(parts)
    try:
        data, truncated, _ = _read_below(root, list(WIKI_PARTS) + parts, CARD_LIMIT, missing='card_not_found')
    except TargetError as exc:
        if exc.status == 404 and len(parts) >= 2 and parts[0] in (target.get('wikiCollections') or ()):
            parts = parts[1:]
            effective_rel = '/'.join(parts)
            data, truncated, _ = _read_below(root, list(WIKI_PARTS) + parts, CARD_LIMIT, missing='card_not_found')
        else:
            raise
    if truncated:
        raise TargetError('card_too_large', 413)
    parsed = _parse_card(data)
    status = parsed['scalars'].get('status')
    if status not in CARD_STATUSES:
        raise TargetError('card_status_unsupported', 422)
    created_by = parsed['scalars'].get('createdBy')
    revisions = {item['path']: str(item.get('sha256') or '') for item in _revisions(parsed['block'])}
    return {
        'targetId': target_id,
        'rel': effective_rel,
        'frontmatter': {
            'status': status,
            'createdBy': created_by if isinstance(created_by, str) and created_by else None,
            'sourceRevisions': revisions,
            'sourceSpans': _spans(parsed['block']),
            'related': _resolve_related(_string_list(parsed['meta'].get('related')), root),
            'aliases': _string_list(parsed['meta'].get('aliases')),
        },
        'blocks': split_blocks(parsed['body']),
    }


def _highlights(value, line_count):
    """Validated ``[{startLine, endLine}]`` from a list of pairs/dicts or a ``a-b,c-d`` string."""
    if value is None or value == '':
        return []
    items = value.split(',') if isinstance(value, str) else value
    if not isinstance(items, (list, tuple)) or len(items) > MAX_HIGHLIGHTS:
        raise TargetError('highlights_invalid', 400)
    out = []
    for item in items:
        try:
            if isinstance(item, str):
                left, _, right = item.strip().partition('-')
                start, end = int(left), int(right or left)
            elif isinstance(item, dict):
                start, end = int(item['startLine']), int(item['endLine'])
            else:
                start, end = int(item[0]), int(item[1])
        except (KeyError, IndexError, TypeError, ValueError):
            raise TargetError('highlights_invalid', 400) from None
        if start < 1 or end < start:
            raise TargetError('highlights_invalid', 400)
        if start <= line_count:
            out.append({'startLine': start, 'endLine': min(end, line_count)})
    return sorted(out, key=lambda span: (span['startLine'], span['endLine']))


def wiki_source(target_id, rel, *, highlights=None, home=None):
    """WikiSourceResponse DTO: one source file of the checkout, read inside the §7.3 boundary."""
    parts = _rel_parts(rel)
    if _is_denied_rel(rel, parts):
        raise TargetError('source_denied', 403)
    target = _target(target_id, home)
    if _is_skipped_rel(rel, _merged_skip(target, _main_root(target))):
        raise TargetError('source_skipped', 403)
    data, truncated, _ = _read_below(target['targetRoot'], parts, SOURCE_LIMIT, missing='source_not_found')
    if b'\x00' in data:
        raise TargetError('source_binary_refused', 400)
    text = data.decode('utf-8', 'replace').replace('\r\n', '\n').replace('\r', '\n')
    lines = text.split('\n')
    if lines and lines[-1] == '':
        lines.pop()
    return {
        'targetId': target_id,
        'rel': rel,
        'lines': lines,
        'highlights': _highlights(highlights, len(lines)),
        'truncated': truncated,
    }

"""Service-level lines over install event rows (``logs.py --slo``).

Each line compares counts and durations of the rows in the window with a
fixed target and reports ``ok``, ``over`` or ``no-data`` (fewer rows than the
line's floor). Only event names, outcomes, reasons, hosts, session ids, card
paths with their hashes and numeric fields are read; nothing is written.
``lines`` returns plain dicts and ``render`` turns one into a text line, so
the caller owns escaping and JSON output.
"""
from __future__ import annotations

from bisect import bisect_left
from collections import Counter
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path

FLOOR = 20
SYNC_FLOOR = 5
# Prompt recall ``ms`` targets (p50, p95). The large pair, and the large
# ``lexicalMs`` target, are for an install with more than ``LARGE_CARDS`` cards.
# No event field holds the card count: ``install_size`` counts the wiki files of
# the install's route. When that cannot be read the line is judged by the
# standard target and shows the large verdict beside it.
HOOK = {'withoutSelector': (700, 1200), 'withSelector': (1500, 2500), 'withSelectorLarge': (2000, 3000)}
STAGES = (('pinMs', 'pin', 50), ('lexicalMs', 'lexical', 50),
          ('stillCurrentMs', 'stillCurrent', 50), ('checkoutMs', 'checkout', 300))
LEXICAL_LARGE = 150
LARGE_CARDS = 1000
VECTOR_CACHE = ('ok', 'building', 'absent', 'disabled', 'inconsistent')
SEMANTIC_COMPLETE, ARENA_SHARE = 0.95, 0.99
SEMANTIC_COUNTS = ('deferred_deadline', 'provider_absent', 'deferred_work_budget', 'deferred_cache_building')
SELECTOR = (1000, 1500)
SELECTOR_OVER_BUDGET = 0.02
REPEAT_SHARE = 0.05
# A host that sends no compaction signal forgets an injection after this many served recalls.
REPEAT_WINDOW = 20
SIGNAL_HOSTS = frozenset(('claude',))
SYNC_STREAK = 3
SYNC_SECONDS = 300
SYNC_OK = frozenset(('indexed', 'unchanged'))
REFUSED = frozenset(('failed', 'refused'))
CYCLE_OUTCOMES = ('trained', 'failed', 'deferred_resource', 'blocked', 'rearmed')


def _ts(row):
    try:
        value = datetime.fromisoformat(str(row.get('ts')).replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _number(value):
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def _percentile(items, fraction):
    ordered = sorted(items)
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)]


def _values(rows, key):
    return [value for value in (_number(row.get(key)) for row in rows) if value is not None]


def _text(value):
    return value if isinstance(value, str) and value else None


def _line(name, status, n=None, **values):
    return {'line': name, 'status': status, **({} if n is None else {'n': n}), **values}


def _judge(n, floor, *over):
    return 'no-data' if n < floor else 'over' if any(over) else 'ok'


def sync_runs(rows):
    """Trailing failed ``wake.sync`` passes per install: ``{install: (count, reasons, last ok ts)}``.

    A pass that neither failed nor published (``progress``) leaves the run as it is.
    """
    by_install = {}
    for row in rows:
        if row.get('event') == 'wake.sync':
            by_install.setdefault(row.get('install'), []).append(row)
    result = {}
    for install, passes in by_install.items():
        count, reasons, last_ok = 0, Counter(), None
        for row in reversed(passes):
            if row.get('outcome') in SYNC_OK:
                last_ok = row.get('ts')
                break
            if row.get('outcome') == 'failed':
                count += 1
                reasons[str(row.get('reason'))] += 1
        result[install] = (count, reasons, last_ok)
    return result


def install_size(state_root):
    """``large``, ``standard`` or None (unknown) from the number of wiki Markdown files of the install's route.

    Names only, and the walk stops at the first file past the limit.
    """
    try:
        with open(Path(state_root) / 'route.json', 'rb') as handle:
            collections = json.loads(handle.read(1 << 20)).get('collections')
        roots = [item['sourceRoot'] for item in collections
                 if isinstance(item, dict) and item.get('role') == 'wiki' and _text(item.get('sourceRoot'))]
        if not roots:
            return None
        count = 0
        for root in roots:
            if not os.path.isdir(root):
                return None
            for _folder, folders, files in os.walk(root):
                folders[:] = [name for name in folders if not name.startswith('.')]
                count += sum(1 for name in files if name.endswith('.md') and not name.startswith('.'))
                if count > LARGE_CARDS:
                    return 'large'
        return 'standard'
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def _one_query(row):
    """A recall of one query vector. A row from before the field existed has one."""
    return row.get('queryChunks') in (None, 1)


def _hook(prompt, size):
    timed = [row for row in prompt if _number(row.get('ms')) is not None]
    without = [max(0, row['ms'] - (_number(row.get('selectorMs')) or 0)) for row in timed]
    selected = [row['ms'] for row in timed if _text(row.get('selectorLane'))]
    result = []
    for name, data, key in (('hook', without, 'withoutSelector'), ('hook+selector', selected, 'withSelector')):
        if not data:
            result.append(_line(name, 'no-data', 0))
            continue
        p50, p95 = _percentile(data, .5), _percentile(data, .95)
        goal = HOOK[key]
        extra = {}
        if key == 'withSelector':
            large = HOOK['withSelectorLarge']
            if size == 'large':
                goal = large
            elif size is None:
                extra = {'largeTargetP50': large[0], 'largeTargetP95': large[1],
                         'large': _judge(len(data), FLOOR, p50 > large[0], p95 > large[1])}
        result.append(_line(name, _judge(len(data), FLOOR, p50 > goal[0], p95 > goal[1]), len(data),
                            p50=round(p50, 3), p95=round(p95, 3), targetP50=goal[0], targetP95=goal[1], **extra))
    return result


def _stage(prompt, size):
    """Stage medians of recalls with one query vector (more vectors cost more by design)."""
    single = [row for row in prompt if _one_query(row)]
    values, over, n = {}, False, None
    for key, label, goal in STAGES:
        data = _values(single, key)
        n = len(data) if n is None else min(n, len(data))
        if not data:
            continue
        if key == 'lexicalMs' and size == 'large':
            goal = LEXICAL_LARGE
        values[label] = round(_percentile(data, .5), 3)
        values[label + 'Target'] = goal
        over = over or (len(data) >= FLOOR and values[label] > goal)
        if key == 'lexicalMs' and size is None:
            values['lexicalLargeTarget'] = LEXICAL_LARGE
            values['lexicalLarge'] = _judge(len(data), FLOOR, values[label] > LEXICAL_LARGE)
    return _line('stage', _judge(n or 0, FLOOR, over), n or 0, **values)


def _semantic(prompt):
    stated = [row for row in prompt if _text(row.get('semantic'))]
    asked = [row for row in stated if row['semantic'] != 'provider_absent']
    counts = Counter(row['semantic'] for row in stated)
    values = {name: counts.get(name, 0) for name in SEMANTIC_COUNTS}
    # A recall whose arena was switched off on purpose is not an arena miss.
    sourced = [row for row in asked if _text(row.get('vectorSource')) and row.get('vectorCache') != 'disabled']
    arena = sum(row['vectorSource'] == 'arena' for row in sourced)
    cache = Counter(row['vectorCache'] for row in stated if row.get('vectorCache') in VECTOR_CACHE)
    # Rows from before the field existed do not say: unmeasured, not zero.
    values['vectorCache'] = dict(sorted(cache.items())) if cache else 'unmeasured'
    values['vectorCacheInconsistent'] = cache.get('inconsistent', 0)
    if asked:
        values['complete'] = round(counts.get('complete', 0) / len(asked), 4)
        values['completeTarget'] = SEMANTIC_COMPLETE
    if arena:
        # Before an install has an arena no recall names it: nothing to judge yet.
        values['arena'] = round(arena / len(sourced), 4)
        values['arenaTarget'] = ARENA_SHARE
    over = bool(asked) and (values['complete'] < SEMANTIC_COMPLETE
                            or values.get('arena', 1.0) < ARENA_SHARE
                            or values['deferred_cache_building'] > 0)
    return _line('semantic', _judge(len(asked), FLOOR, over), len(asked), **values)


def _selector(prompt):
    ran = [row for row in prompt if _text(row.get('selectorLane')) and _number(row.get('selectorMs')) is not None]
    if not ran:
        return _line('selector', 'no-data', 0)
    data = [row['selectorMs'] for row in ran]
    late = sum(1 for row in ran if isinstance(row.get('selection_fallback'), str)
               and ('deadline' in row['selection_fallback'] or 'budget' in row['selection_fallback']))
    p50, p95, share = _percentile(data, .5), _percentile(data, .95), late / len(ran)
    return _line('selector', _judge(len(ran), FLOOR, p50 > SELECTOR[0], p95 > SELECTOR[1],
                                    share > SELECTOR_OVER_BUDGET), len(ran),
                 p50=round(p50, 3), p95=round(p95, 3), targetP50=SELECTOR[0], targetP95=SELECTOR[1],
                 overBudget=round(share, 4), overBudgetTarget=SELECTOR_OVER_BUDGET)


def _hosts(prompt):
    result = []
    for host in sorted({row['host'] for row in prompt if _text(row.get('host'))}):
        rows = [row for row in prompt if row.get('host') == host]
        data = [max(0, row['ms'] - (_number(row.get('selectorMs')) or 0)) for row in rows
                if _number(row.get('ms')) is not None]
        asked = [row for row in rows if _text(row.get('semantic')) and row['semantic'] != 'provider_absent']
        values = {}
        if data:
            values.update(p50=round(_percentile(data, .5), 3), p95=round(_percentile(data, .95), 3))
        if asked:
            values['complete'] = round(sum(row['semantic'] == 'complete' for row in asked) / len(asked), 4)
        # Shown, never judged.
        result.append(_line('host ' + host, 'no-data' if len(data) < FLOOR else 'shown', len(data), **values))
    return result


def _cards(row):
    cards = row.get('cards')
    return [(card['path'], card['sha']) for card in (cards if isinstance(cards, list) else [])
            if isinstance(card, dict) and _text(card.get('path')) and _text(card.get('sha'))]


def _dedup(rows, floor_time):
    """Cards injected again into a session that already holds them.

    A session's history starts over at a row that carries ``sessionReset``.
    A host with no compaction signal looks back ``REPEAT_WINDOW`` served recalls.
    """
    history, injected, repeated, all_repeated, recalls, emptied = {}, 0, 0, 0, 0, 0
    for row in rows:
        session = _text(row.get('session'))
        if session and _text(row.get('sessionReset')):
            history[session] = []
        if row.get('event') != 'recall':
            continue
        current = (_ts(row) or floor_time) >= floor_time
        if current and row.get('reason') == 'no_candidates_after_exclusion':
            emptied += 1
        cards = _cards(row)
        if not session or row.get('outcome') != 'served' or not cards:
            continue
        earlier = history.setdefault(session, [])
        looked = earlier if row.get('host') in SIGNAL_HOSTS else earlier[-REPEAT_WINDOW:]
        seen = set().union(*looked) if looked else set()
        if current:
            again = sum(card in seen for card in cards)
            recalls += 1
            injected += len(cards)
            repeated += again
            all_repeated += again == len(cards)
        earlier.append(set(cards))
    if not injected:
        return _line('dedup', 'no-data', recalls, noCandidatesAfterExclusion=emptied)
    share = repeated / injected
    return _line('dedup', _judge(recalls, FLOOR, share > REPEAT_SHARE), recalls,
                 repeated=round(share, 4), repeatedTarget=REPEAT_SHARE, cards=injected,
                 allRepeated=all_repeated, noCandidatesAfterExclusion=emptied)


def _requests(window):
    return [row for row in window
            if (row.get('event') in ('update-queued', 'file-change-event') and row.get('reason') == 'sync_requested')
            or (row.get('event') == 'index' and row.get('outcome') == 'ok')]


def _sync(rows, window, now):
    """The trailing failure run, and the time from a sync request to the first pass that started after it and published."""
    count, reasons, last_ok = next(iter(sync_runs([dict(row, install=None) for row in rows]).values()),
                                   (0, Counter(), None))
    passes = []
    for row in rows:
        ended, ms = _ts(row), _number(row.get('ms'))
        if row.get('event') == 'wake.sync' and row.get('outcome') in SYNC_OK and ended and ms is not None:
            passes.append((ended - timedelta(milliseconds=ms), ended))
    passes.sort()
    starts = [item[0] for item in passes]
    delays, waiting = [], []
    for row in _requests(window):
        asked = _ts(row)
        if asked is None:
            continue
        index = bisect_left(starts, asked)
        if index < len(passes):
            delays.append((passes[index][1] - asked).total_seconds())
        else:
            waiting.append((now - asked).total_seconds())
    values = {'failedRun': count, 'failedRunTarget': SYNC_STREAK}
    if count:
        values['reasons'] = dict(reasons.most_common(3))
    if last_ok:
        values['lastOk'] = last_ok
    if delays:
        values.update(p50Seconds=round(_percentile(delays, .5), 1), p95Seconds=round(_percentile(delays, .95), 1),
                      targetSeconds=SYNC_SECONDS)
    if waiting:
        values.update(unanswered=len(waiting), oldestUnansweredSeconds=round(max(waiting), 1))
    stalled = count > SYNC_STREAK or (waiting and max(waiting) > SYNC_SECONDS)
    status = 'over' if stalled else _judge(len(delays), SYNC_FLOOR, delays and values['p95Seconds'] > SYNC_SECONDS)
    return _line('sync', status, len(delays), **values)


def _failed(window):
    bare = Counter(str(row.get('event')) for row in window
                   if row.get('outcome') in REFUSED and not _text(row.get('reason')))
    values = {'withoutReason': sum(bare.values())}
    if bare:
        values['events'] = dict(bare.most_common(5))
    return _line('failed', 'over' if bare else 'ok', **values)


def _learning(rows, window):
    cycles = [row for row in window if row.get('event') == 'learning.cycle']
    counts = Counter(str(row.get('outcome')) for row in cycles)
    values = {'stored': sum(1 for row in window if row.get('event') == 'learning.capture'
                            and row.get('outcome') == 'stored'),
              'gold': sum(1 for row in window if row.get('event') == 'learning.teacher'
                          and row.get('outcome') == 'gold'),
              **{name: counts.get(name, 0) for name in CYCLE_OUTCOMES}}
    bare = sum(1 for row in cycles if row.get('outcome') in ('deferred_resource', 'blocked')
               and not _text(row.get('reason')))
    values['withoutReason'] = bare
    # How long the cycle has been put off: the first row of the trailing deferral run.
    since = None
    for row in reversed([row for row in rows if row.get('event') == 'learning.cycle']):
        if row.get('outcome') != 'deferred_resource':
            break
        since = row.get('ts')
    if since:
        values['deferredSince'] = since
    return _line('learning', 'over' if bare else 'ok', **values)


def _identity(state_root):
    """Whether the device number sealed in the install's route is the live one (shown, never judged)."""
    try:
        with open(Path(state_root) / 'route.json', 'rb') as handle:
            node = json.loads(handle.read(65536)).get('stateNode')
        stored = node[0] if isinstance(node, list) and len(node) == 2 and type(node[0]) is int else None
        live = os.stat(state_root).st_dev
    except (OSError, ValueError, TypeError, AttributeError):
        stored = None
    if stored is None:
        return _line('identity', 'no-data')
    return _line('identity', 'shown', storedDevice=stored, liveDevice=live, moved=stored != live)


def lines(rows, hours, *, state_root=None, now=None):
    """Every line for one install's rows (oldest first)."""
    now = now or datetime.now(timezone.utc)
    floor_time = now - timedelta(hours=hours)
    window = [row for row in rows if (_ts(row) or floor_time) >= floor_time]
    prompt = [row for row in window if row.get('event') == 'recall']
    size = install_size(state_root) if state_root is not None else None
    return [_line('install', 'shown', size=size or 'unknown'),
            *_hook(prompt, size), _stage(prompt, size), _semantic(prompt), _selector(prompt), *_hosts(prompt),
            _dedup(rows, floor_time), _sync(rows, window, now), _failed(window),
            _learning(rows, window), _identity(state_root) if state_root is not None else _line('identity', 'no-data')]


def render(line, escape=str):
    parts = [f"slo {escape(line['line'])}: {line['status']}"]
    for key, value in line.items():
        if key in ('line', 'status'):
            continue
        if isinstance(value, dict):
            value = ','.join(f'{escape(name)}:{count}' for name, count in value.items())
        elif isinstance(value, float):
            value = f'{value:g}'
        else:
            value = escape(str(value))
        parts.append(f'{key}={value}')
    return ' '.join(parts)

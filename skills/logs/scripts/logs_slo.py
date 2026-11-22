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
# What the product's session record forgets: pairs beyond the newest 256, and a session idle for a day.
SESSION_PAIRS = 256
SESSION_IDLE = 24 * 3600
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
    values, over, most = {}, False, 0
    for key, label, goal in STAGES:
        # Each stage is judged on its own rows: a stage only served recalls
        # record must not hide another one that is over its target.
        data = _values(single, key)
        most = max(most, len(data))
        if not data:
            continue
        if key == 'lexicalMs' and size == 'large':
            goal = LEXICAL_LARGE
        values[label] = round(_percentile(data, .5), 3)
        values[label + 'N'] = len(data)
        values[label + 'Target'] = goal
        over = over or (len(data) >= FLOOR and values[label] > goal)
        if key == 'lexicalMs' and size is None:
            values['lexicalLargeTarget'] = LEXICAL_LARGE
            values['lexicalLarge'] = _judge(len(data), FLOOR, values[label] > LEXICAL_LARGE)
    # ``n`` is the best-measured stage: the line has data once any stage reaches the floor.
    return _line('stage', _judge(most, FLOOR, over), most, **values)


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
    # Before a bundle that reports the cache state no recall says anything about an arena:
    # nothing to judge. Once a recall reports it, no arena answer at all is a share of 0.
    measured = any(row.get('vectorCache') in VECTOR_CACHE for row in sourced)
    if sourced and (arena or measured):
        values['arena'] = round(arena / len(sourced), 4)
        values['arenaTarget'] = ARENA_SHARE
    answered = [row for row in sourced if row['vectorSource'] == 'arena']
    if answered:
        # The recalls the arena answered, on their own: their completion and their semantic stage.
        values['arenaComplete'] = round(sum(row['semantic'] == 'complete' for row in answered) / len(answered), 4)
        timed = [row['semanticMs'] for row in answered
                 if _one_query(row) and _number(row.get('semanticMs')) is not None]
        if timed:
            values.update(arenaSemanticP50=round(_percentile(timed, .5), 3),
                          arenaSemanticP95=round(_percentile(timed, .95), 3), arenaSemanticN=len(timed))
    over = bool(asked) and (values['complete'] < SEMANTIC_COMPLETE
                            or values.get('arena', 1.0) < ARENA_SHARE
                            or values['deferred_cache_building'] > 0)
    # The one cache state that is a fault: a single recall is over, however few recalls the window holds.
    status = 'over' if values['vectorCacheInconsistent'] > 0 else _judge(len(asked), FLOOR, over)
    return _line('semantic', status, len(asked), **values)


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
                 overBudget=round(share, 4), overBudgetTarget=SELECTOR_OVER_BUDGET, **_selector_detail(ran))


def _count(value):
    return value if type(value) is int and value >= 0 else None


def _selector_detail(ran):
    """What the selector's answers say beside their time; nothing here is judged.

    A row without a field did not report it (the selector did not answer, or
    an older bundle wrote the row): it is left out, never counted as zero.
    """
    values = {}
    answered = [row for row in ran if _count(row.get('rankedOverlap')) is not None]
    if answered:
        # No share: the overlap is read beside how many cards were served and how many were candidates.
        values.update(overlapRecalls=len(answered), rankedOverlap=sum(row['rankedOverlap'] for row in answered),
                      overlapServed=sum(_count(row.get('count')) or 0 for row in answered),
                      overlapCandidates=sum(_count(row.get('candidates')) or 0 for row in answered))
    checkpoints = {}
    for row in ran:
        if _text(row.get('selectorCheckpoint')):
            checkpoints[row['selectorCheckpoint']] = checkpoints.get(row['selectorCheckpoint'], 0) + 1
    if checkpoints:
        values['checkpoints'] = dict(sorted(checkpoints.items()))
    dropped = [row['selectorDroppedPairs'] for row in ran if _count(row.get('selectorDroppedPairs')) is not None]
    if dropped:
        values.update(droppedPairRecalls=sum(1 for value in dropped if value), droppedPairs=sum(dropped))
    reused = [row['selectorIdentityReused'] for row in ran
              if type(row.get('selectorIdentityReused')) in (int, bool) and row['selectorIdentityReused'] in (0, 1)]
    if reused:
        values.update(identityReused=round(sum(reused) / len(reused), 4), identityReusedN=len(reused))
    return values


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


def _holds(context):
    """The injection contexts whose cards a recall in ``context`` already has."""
    if context == 'p':
        return ('p', 'e')
    return ('p', 'e', 'e?') if context in ('e', 'e?') else (context,)


def _dedup(rows, floor_time):
    """Prompt recall cards that the session's own context already held when they were injected.

    A session's history is its served prompt and edit recalls, each with the
    context it injected into (``injectedBy``; a row without it is a prompt
    recall, or an edit recall of an unknown context). It starts over at a row
    that carries ``sessionReset``. An entry that can leave the context without
    a signal (an edit injection, any entry of a host with no compaction
    signal) counts for ``REPEAT_WINDOW`` injecting recalls only. A card that
    comes back after that, after a reset or in another context is not a repeat.
    """
    history, seen_at, injected, repeated, all_repeated, recalls, emptied = {}, {}, 0, 0, 0, 0, 0
    for row in rows:
        session = _text(row.get('session'))
        if session and _text(row.get('sessionReset')):
            history[session] = []
        prompt = row.get('event') == 'recall'
        if not prompt and row.get('event') != 'recall.edit':
            continue
        current = (_ts(row) or floor_time) >= floor_time
        if prompt and current and row.get('reason') == 'no_candidates_after_exclusion':
            emptied += 1
        cards = _cards(row)
        if not session or row.get('outcome') != 'served' or not cards:
            continue
        earlier = history.setdefault(session, [])
        moment = _ts(row)
        if earlier and moment and seen_at.get(session) and (moment - seen_at[session]).total_seconds() > SESSION_IDLE:
            # The product forgets a session's record after a day without a recall.
            del earlier[:]
        seen_at[session] = moment or seen_at.get(session)
        context = _text(row.get('injectedBy')) or ('p' if prompt else 'e?')
        if prompt and current:
            holds, signal = _holds(context), row.get('host') in SIGNAL_HOSTS
            seen, pairs = set(), 0
            for age, (held, by) in enumerate(reversed(earlier)):
                pairs += len(held)
                if pairs > SESSION_PAIRS:
                    break  # The record keeps the newest pairs only.
                if by in holds and ((by == 'p' and signal) or age < REPEAT_WINDOW):
                    seen |= held
            again = sum(card in seen for card in cards)
            recalls += 1
            injected += len(cards)
            repeated += again
            all_repeated += again == len(cards)
        earlier.append((set(cards), context))
    if not injected:
        return _line('dedup', 'no-data', recalls, noCandidatesAfterExclusion=emptied)
    share = repeated / injected
    return _line('dedup', _judge(recalls, FLOOR, share > REPEAT_SHARE), recalls,
                 repeated=round(share, 4), repeatedTarget=REPEAT_SHARE, cards=injected,
                 allRepeated=all_repeated, noCandidatesAfterExclusion=emptied)


def _requests(window):
    """Hook rows that queued a sync wake. A row of these events without the reason queued none
    (an edit outside the project, an edit that did not succeed)."""
    return [row for row in window
            if row.get('event') in ('update-queued', 'file-change-event', 'index')
            and row.get('reason') == 'sync_requested']


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
    waited, waited_reasons, last, inside = 0, Counter(), None, {id(row) for row in window}
    for row in rows:
        if row.get('event') != 'wake.sync' or row.get('outcome') not in SYNC_OK:
            continue
        linked = row.get('linked') if isinstance(row.get('linked'), dict) else {}
        # The newest published pass says whether linked views wait now; the window says how often they did.
        last = linked
        if id(row) in inside and (_number(linked.get('waiting')) or 0) > 0:
            waited += 1
            waited_reasons.update({str(key): value for key, value in (linked.get('reasons') or {}).items()
                                   if _number(value)} if isinstance(linked.get('reasons'), dict) else {})
    if waited:
        values.update(linkedWaiting=waited, linkedReasons=dict(waited_reasons.most_common(3)))
    if last:
        values.update(linkedWaitingLast=int(_number(last.get('waiting')) or 0),
                      linkedHeldLast=int(_number(last.get('held')) or 0))
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

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
import re
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
SEMANTIC_USED = ('complete', 'partial')
SEMANTIC_COUNTS = ('partial', 'deferred_deadline', 'provider_absent', 'deferred_work_budget', 'deferred_cache_building')
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
# 1.0.x-4: a sync put off by a learning gate waits at most this long at the median.
LEARNING_WAIT_SECONDS = 30


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
        # The stage's own verdict: a slow stage under the sample floor reads ``no-data`` here, not ``ok``.
        values[label + 'Status'] = _judge(len(data), FLOOR, values[label] > goal)
        over = over or (len(data) >= FLOOR and values[label] > goal)
        if key == 'lexicalMs' and size is None:
            values['lexicalLargeTarget'] = LEXICAL_LARGE
            values['lexicalLarge'] = _judge(len(data), FLOOR, values[label] > LEXICAL_LARGE)
    # ``n`` is the best-measured stage: the line has data once any stage reaches the floor.
    return _line('stage', _judge(most, FLOOR, over), most, **values)


def _arena(window):
    """What the sync owner last said about the vector arena: why it did not move, and the sets left."""
    events = [row for row in window if row.get('event') == 'vector.cache']
    if not events:
        return {}
    last, values = events[-1], {}
    if last.get('outcome') in ('skipped', 'failed') and _text(last.get('reason')):
        values['arenaSkipped'] = last['reason']
    if _number(last.get('remaining')) is not None:
        values['arenaRemaining'] = int(last['remaining'])
    return values


def full_coverage(row):
    """A sealed search, or a partial search that covered every eligible member."""
    if row.get('semantic') == 'complete':
        return True
    covered = _count(row.get('vectorCoveredMembers'))
    eligible = _count(row.get('vectorEligibleMembers'))
    return row.get('semantic') == 'partial' and eligible is not None and eligible > 0 and covered == eligible


def _semantic_shares(rows):
    return (round(sum(row['semantic'] in SEMANTIC_USED for row in rows) / len(rows), 4),
            round(sum(full_coverage(row) for row in rows) / len(rows), 4))


def _semantic(prompt, window=()):
    stated = [row for row in prompt if _text(row.get('semantic'))]
    asked = [row for row in stated if row['semantic'] != 'provider_absent']
    counts = Counter(row['semantic'] for row in stated)
    values = {name: counts.get(name, 0) for name in SEMANTIC_COUNTS}
    # Older lexical-only rows may name the selected kernel's source even though it read no vectors.
    sourced = [row for row in asked if row['semantic'] != 'lexical_only'
               and _text(row.get('vectorSource')) and row.get('vectorCache') != 'disabled']
    arena = sum(row['vectorSource'] == 'arena' for row in sourced)
    cache = Counter(row['vectorCache'] for row in stated if row.get('vectorCache') in VECTOR_CACHE)
    # Rows from before the field existed do not say: unmeasured, not zero.
    values['vectorCache'] = dict(sorted(cache.items())) if cache else 'unmeasured'
    values['vectorCacheInconsistent'] = cache.get('inconsistent', 0)
    if asked:
        values['complete'], values['fullCoverage'] = _semantic_shares(asked)
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
        values['arenaComplete'], values['arenaFullCoverage'] = _semantic_shares(answered)
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
    return _line('semantic', status, len(asked), **values, **_arena(window))


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
            values['complete'], values['fullCoverage'] = _semantic_shares(asked)
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
    delays, waiting = _publish_delays(rows, window, now)
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
    # Passes in a row that asked for the same wake again (vector fill, linked views, the vector arena).
    run = longest = 0
    for row in rows:
        if row.get('event') == 'wake.sync':
            run = run + 1 if row.get('reoffer') is True else 0
            longest = max(longest, run) if id(row) in inside else longest
    if longest:
        values.update(reofferRun=run, reofferRunMax=longest)
    stalled = count > SYNC_STREAK or (waiting and max(waiting) > SYNC_SECONDS)
    status = 'over' if stalled else _judge(len(delays), SYNC_FLOOR, delays and values['p95Seconds'] > SYNC_SECONDS)
    return _line('sync', status, len(delays), **values)


def _publish_delays(rows, window, now):
    """``(delays, waiting)`` seconds: each sync request to the end of the first pass that started after it
    and published, and the age of the requests no such pass answers yet."""
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
    return delays, waiting


def _deferral_waits(rows, window, matches, now):
    """How long sync stayed put off by the deferrals ``matches`` names (seconds).

    A run of ``wake.sync`` rows starts at the first matching row's start
    (``ts`` - ``ms``) and ends at the next published pass. Returns
    ``(unpublished rows matched in the window, waits of the runs that ended in
    the window, age of a run still open)``.
    """
    inside = {id(row) for row in window}
    matched, waits, opened = 0, [], None
    for row in rows:
        if row.get('event') != 'wake.sync':
            continue
        ended = _ts(row)
        if ended is None:
            continue
        if row.get('outcome') in SYNC_OK:
            # A published pass ends the run (its linked views may still wait; that is the linked line's).
            if opened is not None and id(row) in inside:
                waits.append((ended - opened).total_seconds())
            opened = None
        elif matches(row):
            matched += id(row) in inside
            if opened is None:
                opened = ended - timedelta(milliseconds=_number(row.get('ms')) or 0)
    return matched, waits, (None if opened is None else (now - opened).total_seconds())


_LEARNING_CODE = re.compile(r'learning_[a-z_]{1,60}')
_GATE_CODE = re.compile(r'gate_(provider|control|trainer|compute)\b')
_OWNER_BUSY = re.compile(r'(?:^|:)owner_busy(?::|$)')


def _row_text(row):
    """The fixed codes of a row (reason, detail, notes, linked reasons) as one string."""
    parts = [row.get('reason'), row.get('detail')]
    notes = row.get('notes')
    parts.extend(notes if isinstance(notes, list) else ())
    linked = row.get('linked')
    if isinstance(linked, dict) and isinstance(linked.get('reasons'), dict):
        parts.extend(linked['reasons'])
    return ' '.join(part for part in parts if isinstance(part, str))


def learning_codes(row):
    """The ``learning_*`` deferral codes a ``wake.*`` or ``recall`` row names (sorted, once each)."""
    return sorted(set(_LEARNING_CODE.findall(_row_text(row))))


def _contention(rows, window, now):
    """Deferrals the learning gates caused in sync, compile and recall, and how long sync waited.

    Judged against the 1.0.x-4 decision: no recall put off by a ``learning_*``
    reason and a sync wait median of at most ``LEARNING_WAIT_SECONDS``.
    """
    events, codes, gates = Counter(), Counter(), Counter()
    for row in window:
        event = row.get('event')
        if event not in ('wake.sync', 'wake.compile', 'recall'):
            continue
        found = learning_codes(row)
        if not found:
            continue
        name = event.removeprefix('wake.')
        if name == 'recall' and not learning_codes(dict(row, notes=[
                note for note in row.get('notes') or () if not str(note).startswith('laya_selector_')])):
            # Only the local selector met a learning gate: the recall served the hybrid rank.
            name = 'recallSelector'
        events[name] += 1
        codes.update(found)
        gates.update(_GATE_CODE.findall(_row_text(row)))
    _matched, waits, open_age = _deferral_waits(rows, window, lambda row: bool(learning_codes(row)), now)
    values = {name: events.get(name, 0) for name in ('sync', 'compile', 'recall')}
    if events.get('recallSelector'):
        values['recallSelector'] = events['recallSelector']
    if codes:
        values['reasons'] = dict(codes.most_common(5))
    if gates:
        values['gates'] = dict(gates.most_common())
    # A prompt recall whose embedding owner was put off by the resource admission: the inner
    # reason (learning or memory) stays with the resident, so it is counted beside, not judged.
    resident = sum(1 for row in window if row.get('event') == 'recall'
                   and 'laya_vector_selected_provider_resource_deferred' in _row_text(row))
    if resident:
        values['recallResourceDeferred'] = resident
    if waits:
        values.update(syncWaits=len(waits), syncWaitP50Seconds=round(_percentile(waits, .5), 1),
                      syncWaitMaxSeconds=round(max(waits), 1))
    if open_age is not None:
        values['syncWaitOpenSeconds'] = round(open_age, 1)
    values['targetSyncWaitP50Seconds'] = LEARNING_WAIT_SECONDS
    over = values['recall'] > 0 or (waits and values['syncWaitP50Seconds'] > LEARNING_WAIT_SECONDS) or (
        open_age is not None and open_age > LEARNING_WAIT_SECONDS)
    return _line('contention', 'over' if over else 'ok', sum(events.values()), **values)


def _owner(rows, window, now):
    """Sync owner occupancy of the install's selected passes (``sync.profile``), shown without a target.

    ``holdP95Ms``/``holdMaxMs`` are per pass (every owner the pass took),
    ``busy`` the acquisitions that gave up (``owner_busy``) and ``contended``
    the ones that got the owner only after it was held by another process (a
    sync of another project, an initial build or another owner's short hold).
    ``busyWait*`` is how long sync stayed put off by ``owner_busy`` until a
    pass published; ``editPublishP95Seconds`` is the sync line's request to
    publish p95.
    """
    profiles = [row for row in window if row.get('event') == 'sync.profile'
                and _number(row.get('ownerCalls')) is not None]
    values = {}
    if profiles:
        holds = _values(profiles, 'ownerHoldMs')
        waits = _values(profiles, 'ownerWaitMs')
        values.update(passes=len(profiles),
                      busy=int(sum(_values(profiles, 'ownerBusy'))),
                      contended=int(sum(_values(profiles, 'ownerContended'))))
        if holds:
            values.update(holdP50Ms=round(_percentile(holds, .5)), holdP95Ms=round(_percentile(holds, .95)),
                          holdMaxMs=round(max(_values(profiles, 'ownerHoldMaxMs') or holds)))
        if waits:
            values.update(waitP95Ms=round(_percentile(waits, .95)))
    busy_rows, busy_waits, busy_open = _deferral_waits(
        rows, window, lambda row: bool(_OWNER_BUSY.search(str(row.get('detail') or row.get('reason') or ''))), now)
    if busy_rows:
        values['busyPasses'] = busy_rows
    if busy_waits:
        values.update(busyWaitP50Seconds=round(_percentile(busy_waits, .5), 1),
                      busyWaitMaxSeconds=round(max(busy_waits), 1))
    if busy_open is not None:
        values['busyWaitOpenSeconds'] = round(busy_open, 1)
    delays, _waiting = _publish_delays(rows, window, now)
    if delays:
        values.update(editPublishP95Seconds=round(_percentile(delays, .95), 1), editPublishN=len(delays))
    return _line('owner', 'shown' if profiles or busy_rows or delays else 'no-data', **values)


def _linked(window):
    """Main publishes and linked view waits counted apart (``sync.profile`` ``main``/``linked``/``linkedViews``).

    ``mainPublished`` passes published the main view; ``mainPublishedNotOk``
    of them still did not end ``indexed``/``unchanged`` (by final reason).
    ``linkedWaitingPasses`` left linked views waiting, ``pending`` says what
    happened to them (re-offered, next request, capped). ``unpublished`` counts
    the linked views (by hashed id) that no pass of the window published,
    with the reasons of their latest state.
    """
    profiles = [row for row in window if row.get('event') == 'sync.profile' and _text(row.get('main'))]
    if not profiles:
        return _line('linked', 'no-data')
    published = [row for row in profiles if row['main'] in SYNC_OK]
    not_ok = Counter(str(row.get('reason') or row.get('passState')) for row in published
                     if row.get('passState') not in SYNC_OK)
    pending, waiting = Counter(), 0
    views, latest = set(), {}
    for row in published:
        linked = row.get('linked') if isinstance(row.get('linked'), dict) else {}
        if (_number(linked.get('waiting')) or 0) > 0 or (_number(linked.get('held')) or 0) > 0:
            waiting += 1
            if _text(linked.get('pending')):
                pending[linked['pending']] += 1
        for view in row.get('linkedViews') or ():
            if not isinstance(view, dict) or not _text(view.get('view')):
                continue
            current = view.get('state') in SYNC_OK and view.get('vector') not in ('pending', 'deferred')
            if current:
                views.add(view['view'])
                latest.pop(view['view'], None)
            elif view['view'] not in views:
                latest[view['view']] = str(view.get('reason') or view.get('vector') or view.get('state'))
    values = {'mainPublished': len(published), 'mainNotPublished': len(profiles) - len(published),
              'mainPublishedNotOk': sum(not_ok.values()), 'linkedWaitingPasses': waiting}
    if not_ok:
        values['notOkReasons'] = dict(not_ok.most_common(3))
    if pending:
        values['pending'] = dict(pending.most_common())
    if views or latest:
        values.update(views=len(views) + len(latest), unpublished=len(latest))
    if latest:
        values['unpublishedReasons'] = dict(Counter(latest.values()).most_common(3))
    return _line('linked', 'shown', len(profiles), **values)


SUMMARY_LABELS = {'owner': 'sync owner', 'linked': 'sync linked', 'contention': 'learning contention'}


def summary_lines(rows, window, now=None):
    """The occupancy lines the summary prints for one install: owner, linked views, learning contention.

    A line with nothing to say (``no-data``, or no learning deferral and no wait) is left out.
    """
    now = now or datetime.now(timezone.utc)
    found = [_owner(rows, window, now), _linked(window), _contention(rows, window, now)]
    return [line for line in found if line['status'] != 'no-data'
            and not (line['line'] == 'contention' and not line['n'] and 'syncWaits' not in line
                     and 'syncWaitOpenSeconds' not in line and 'recallResourceDeferred' not in line)]


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
            *_hook(prompt, size), _stage(prompt, size), _semantic(prompt, window), _selector(prompt), *_hosts(prompt),
            _dedup(rows, floor_time), _sync(rows, window, now), _owner(rows, window, now), _linked(window),
            _contention(rows, window, now), _failed(window),
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

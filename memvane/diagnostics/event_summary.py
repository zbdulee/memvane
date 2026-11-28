"""The summary view of install event rows as text lines (``diagnostics.api.summarize_events``).

Rows are already visible (``event_queries.visible``); every stored string in a
line is display-escaped. The lines are, in order: event counts of the window,
hook latency percentiles (with the recall semantic shares and stage medians),
recall outcomes, the trailing sync failure run and the occupancy lines per
install, the last publish and error, dropped writes, then the default-teacher
and learning lines. Nothing is read from disk and nothing is written.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import math

from memvane.diagnostics import slo
from memvane.diagnostics.event_queries import EPOCH, count, display, esc, is_error, short, since, ts

HOOKS = ('recall', 'gate', 'posttool', 'index', 'compile', 'update', 'update-queued', 'file-change-event')
_STAGE_LABELS = (('routeProofMs', 'route'), ('lexicalMs', 'lexical'),
                 ('queryEmbeddingMs', 'queryEmbedding'), ('semanticMs', 'semantic'),
                 ('selectorMs', 'selector'), ('renderMs', 'render'),
                 ('vectorReadMs', 'vectorRead'), ('vectorUnpackMs', 'vectorUnpack'),
                 ('vectorDotMs', 'vectorDot'), ('vectorLoadMs', 'vectorLoad'),
                 ('vectorScanMs', 'vectorScan'), ('vectorVerifyMs', 'vectorVerify'))
# Checkout observation stages: each is a few ms while the observation cache
# serves, and hundreds when every one runs Git.
_OBSERVED = (('pinMs', 'pin'), ('lexicalMs', 'lexical'),
             ('stillCurrentMs', 'stillCurrent'), ('checkoutMs', 'checkout'))
_CALL_OUTCOMES = ('gold', 'pending_review', 'pre_send_rejected', 'provider_refused', 'parked')
# A kept (parked) answer committed by a later pass: its call and usage are on the ``parked`` row.
_FROM_PARKED = 'parked_answer'
_USAGE = ('usageIn', 'usageOut', 'usageCached', 'usageReasoning')
_SCORE_KEYS = ('cases', 'exactRate', 'microF1', 'emptyAgree', 'nonEmptyAgree', 'defaultRank',
               'unknownSelection', 'goldOutsideK')
_PROMOTION_KEYS = ('cycle', 'trainCases', 'evalCases', 'evalEmpty', 'candidateExact', 'activeExact')
# The default-teacher cohort: train, evaluation, non-empty and empty evaluation gold.
PROMOTION_NEEDS = (('candidateTrain', 100), ('candidateEval', 50),
                   ('candidateEvalNonEmpty', 10), ('candidateEvalEmpty', 10))
_EDIT_TARGETS = ('main', 'linked', 'linked_unproved', 'mixed', 'outside')


def _values(items, field):
    return [value for row in items for value in [row.get(field)]
            if type(value) in (int, float) and math.isfinite(value) and value >= 0]


def _percentile(items, fraction):
    ordered = sorted(items)
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)]


def _tally(counter):
    return ', '.join(f'{esc(key)} {amount}' for key, amount in sorted(counter.items(), key=lambda x: str(x[0])))


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _stamp(row):
    return str(row.get('ts', ''))[:19].replace('T', ' ')


def lines(rows, hours=None, *, unified=False, now=None):
    """Summary lines of visible ``rows`` (oldest first) for the last ``hours`` (all retained when None).

    ``unified`` rows are merged from several installs: the sync run and the
    occupancy lines are then one per logged ``install``.
    """
    now = now or datetime.now(timezone.utc)
    out = []
    window = since(rows, hours, now)
    label = f'last {hours}h' if hours is not None else 'all retained'
    out.append(f'events ({label}): {len(window)}')
    counts = Counter((str(row.get('event')), str(row.get('outcome'))) for row in window)
    for (event, outcome), amount in sorted(counts.items(), key=lambda item: (str(item[0][0]),
                                                                            str(item[0][1]))):
        out.append(f'  {esc(event)} {esc(outcome)}: {amount}')
    for action in HOOKS:
        _hook_lines(out, [row for row in window if row.get('event') == action][-100:], action)
    _recall_outcomes(out, [row for row in window if row.get('event') == 'recall'])
    _sync_lines(out, rows, hours, unified, now)
    for name, test in (('last publish', lambda row: row.get('event') == 'index.publish'),
                       ('last error', is_error)):
        found = [row for row in rows if test(row)]
        if found:
            row = display(found[-1])
            detail = ''
            if row.get('event') == 'index.publish':
                detail = f" generation={row.get('generation')}"
            out.append(f'{name}: {short(row)}{detail}')
    dropped = sum(row.get('dropped', 0) for row in window if type(row.get('dropped')) is int)
    if dropped:
        out.append(f'lines a process could not write before a later one: {dropped}')
    _teacher_summary(out, window, now)
    return out


def _hook_lines(out, action_rows, action):
    elapsed = _values(action_rows, 'ms')
    if not elapsed:
        return
    extra = ''
    if action == 'recall':
        semantic = [row for row in action_rows if isinstance(row.get('semantic'), str)]
        if semantic:
            used = sum(row['semantic'] in slo.SEMANTIC_USED for row in semantic)
            full = sum(slo.full_coverage(row) for row in semantic)
            extra = (f' semantic={used}/{len(semantic)} ({100 * used / len(semantic):.1f}%)'
                     f' fullCoverage={full}/{len(semantic)}')
        extra += ' target p50=1000ms p95=1500ms large-p95=2500ms'
    out.append(f'{action} p50={_percentile(elapsed, .5):g}ms p95={_percentile(elapsed, .95):g}ms'
               f' n={len(elapsed)}{extra}')
    if action != 'recall':
        return
    for source in sorted({row['vectorSource'] for row in action_rows
                          if row.get('vectorSource') in ('arena', 'store', 'python')}):
        sourced = [row for row in action_rows if row.get('vectorSource') == source
                   and isinstance(row.get('semantic'), str)]
        if not sourced:
            continue
        used = sum(row['semantic'] in slo.SEMANTIC_USED for row in sourced)
        times = _values(sourced, 'semanticMs')
        latency = (f' p50={_percentile(times, .5):g}ms p95={_percentile(times, .95):g}ms'
                   if times else '')
        out.append(f'semantic {source}: {used}/{len(sourced)} '
                   f'({100 * used / len(sourced):.1f}%){latency}')
    # The same nearest-rank p50 as the observation line below: ``lexical`` is in both.
    for title, labels in (('recall stage p50: ', _STAGE_LABELS), ('recall observation p50: ', _OBSERVED)):
        stages = [(label, _values(action_rows, key)) for key, label in labels]
        present = [f'{label}={_percentile(data, .5):g}ms' for label, data in stages if data]
        if present:
            out.append(title + ' '.join(present))


def _recall_outcomes(out, recalls):
    if not recalls:
        return
    empty = [row for row in recalls if row.get('outcome') != 'served']
    reasons = Counter(str(row.get('reason')) for row in empty)
    out.append(f"recalls: {len(recalls)} served={len(recalls) - len(empty)} "
               f"empty/refused={len(empty)}"
               + (' (' + ', '.join(f'{esc(k)} {v}' for k, v in reasons.most_common(5)) + ')'
                  if reasons else ''))
    degraded = Counter(str(row.get('degraded') or row.get('vector_fallback'))
                       for row in recalls
                       if row.get('degraded') or row.get('vector_fallback'))
    if degraded:
        out.append('degraded recalls: ' + ', '.join(f'{esc(k)} {v}' for k, v in degraded.items()))


def _sync_lines(out, rows, hours, unified, now):
    # The trailing run over every retained row: a stalled index shows here even
    # when the window holds no pass. One line per install: merged rows of a
    # publishing install must not hide another install's run.
    runs = slo.sync_runs(rows if unified else [dict(row, install=None) for row in rows])
    for install, (streak, reasons, last_ok) in sorted(runs.items(), key=lambda item: str(item[0])):
        line = 'sync consecutive failed passes: ' + (f'install={esc(install)} ' if unified else '') + str(streak)
        if streak:
            line += ' (' + ', '.join(f'{esc(k)} {v}' for k, v in reasons.most_common(3)) + ')'
        if last_ok:
            line += f' last ok {esc(str(last_ok))}'
        out.append(line)
    # Sync owner occupancy, main publishes apart from linked view waits, and what the learning gates put
    # off: one block per install, as the failure run above.
    blocks = {}
    for row in rows:
        blocks.setdefault(row.get('install') if unified else None, []).append(row)
    for install, block in sorted(blocks.items(), key=lambda item: str(item[0])):
        for line in slo.summary_lines(block, since(block, hours, now), now=now):
            text = slo.render(line, esc)[len('slo ' + line['line'] + ': '):]
            out.append(slo.SUMMARY_LABELS[line['line']] + ': '
                       + (f'install={esc(install)} ' if unified else '') + text)


def _teacher_summary(out, window, now):
    """Default-teacher lines: calls, usage, waits, isolation, scores and promotions."""
    teacher = [row for row in window if row.get('event') == 'learning.teacher']
    calls = [row for row in teacher if row.get('outcome') in _CALL_OUTCOMES and row.get('request_id')
             and not (row.get('outcome') == 'gold' and row.get('reason') == _FROM_PARKED)]
    gold = [row for row in teacher if row.get('outcome') == 'gold' and row.get('request_id')]
    _teacher_days(out, calls, gold)
    parked = sum(1 for row in calls if row.get('outcome') == 'parked')
    if parked:
        later = sum(1 for row in gold if row.get('reason') == _FROM_PARKED)
        out.append(f'teacher parked answers: kept={parked} committed_later={later}')
    if gold:
        selected = Counter(row.get('selectedCount') for row in gold
                           if row.get('selectedCount') in (0, 1, 2, 3))
        out.append('teacher selected: ' + ' '.join(f'{n}={selected.get(n, 0)}' for n in range(4)))
        models = Counter((str(row.get('teacherHost')), str(row.get('model'))) for row in gold)
        out.append('teacher models: ' + ', '.join(f'{esc(host)} {esc(model)} {amount}'
                                                  for (host, model), amount in sorted(models.items())))
    pending = Counter(str(row.get('reason')) for row in calls if row.get('outcome') == 'pending_review')
    if pending:
        out.append(f'teacher pending_review: {_tally(pending)}')
    isolation = Counter(row.get('isolation') for row in teacher
                        if row.get('isolation') in ('refused', 'violated'))
    if isolation:
        out.append(f"teacher isolation: refused={isolation.get('refused', 0)} "
                   f"violated={isolation.get('violated', 0)}")
    _teacher_waits(out, [row for row in teacher if row.get('outcome') == 'pass'], now)
    _teacher_warnings(out, calls)
    _scores(out, window)
    promotions = [row for row in window if row.get('event') == 'learning.promotion']
    for row in promotions[-10:]:
        parts = [_stamp(row), esc(row.get('outcome') or '-')]
        parts.extend(f'{key}={esc(row[key])}' for key in _PROMOTION_KEYS if row.get(key) is not None)
        if row.get('reason'):
            parts.append(f"reason={esc(row['reason'])}")
        out.append('promotion ' + ' '.join(parts))
    _learning_inflow(out, window, promotions)


def _teacher_days(out, calls, gold):
    days = {}
    for row in gold:
        day = (ts(row) or EPOCH).date().isoformat()
        days.setdefault(day, {'calls': 0, 'gold': 0, 'failed': Counter(), 'usage': Counter()})['gold'] += 1
    for row in calls:
        day = (ts(row) or EPOCH).date().isoformat()
        entry = days.setdefault(day, {'calls': 0, 'gold': 0, 'failed': Counter(), 'usage': Counter()})
        entry['calls'] += 1
        if row.get('outcome') not in ('gold', 'parked'):
            entry['failed'][str(row.get('reason') or row.get('outcome'))] += 1
        for key in _USAGE:
            if type(row.get(key)) is int and row[key] >= 0:
                entry['usage'][key] += row[key]
    for day, entry in sorted(days.items()):
        failed = sum(entry['failed'].values())
        usage = ' '.join(f'{key}={entry["usage"][key]}' for key in _USAGE if key in entry['usage'])
        out.append(f"teacher {day}: calls={entry['calls']} gold={entry['gold']} failed={failed}"
                   + (f' ({_tally(entry["failed"])})' if failed else '') + (f' {usage}' if usage else ''))


def _teacher_warnings(out, calls):
    warned = [row for row in calls if isinstance(row.get('toolWarnings'), list) and row['toolWarnings']]
    if warned:
        tools = Counter()
        for row in warned:
            for item in row['toolWarnings']:
                if isinstance(item, dict) and type(item.get('count')) is int and item['count'] > 0:
                    tools[str(item.get('name'))] += item['count']
        out.append(f'WARNING teacher tool calls: {len(warned)} call(s); {_tally(tools)}')
    kept = [row for row in calls if isinstance(row.get('pluginWarnings'), list) and row['pluginWarnings']]
    if kept:
        plugins = Counter(str(name) for row in kept for name in row['pluginWarnings'])
        out.append(f'WARNING teacher plugins not turned off: {len(kept)} call(s); {_tally(plugins)}')


def _scores(out, window):
    scores = {}
    for row in window:
        if row.get('event') == 'learning.score' and row.get('checkpoint') is not None:
            scores.setdefault(str(row['checkpoint']), []).append(row)
    for checkpoint, rows in sorted(scores.items(), key=lambda item: str(ts(item[1][-1]) or EPOCH)):
        last = rows[-1]
        parts = [f'{key}={last[key]:g}' if isinstance(last[key], float) else f'{key}={last[key]}'
                 for key in _SCORE_KEYS if _number(last.get(key))]
        trend = [row['exactRate'] for row in rows if _number(row.get('exactRate'))]
        if len(trend) > 1:
            parts.append(f'trend exactRate {trend[0]:g}->{trend[-1]:g} (n={len(trend)})')
        out.append(f'score {esc(checkpoint)}: ' + ' '.join(parts))


def _learning_inflow(out, window, promotions):
    """Edit-recall funnel, distance to promotion, case-table room and learning deferrals."""
    posttool = [row for row in window if row.get('event') == 'posttool']
    edits = [row for row in window if row.get('event') == 'recall.edit']
    stored = [row for row in window if row.get('event') == 'learning.capture' and row.get('outcome') == 'stored']
    if posttool or edits or stored:
        skips = Counter(str(row['editRecall']) for row in posttool if isinstance(row.get('editRecall'), str))
        out.append(f'edit recall funnel: posttool={len(posttool)}'
                   + (f' ({_tally(skips)})' if skips else '')
                   + f' recall.edit={len(edits)} stored={len(stored)}')
        # Where the edited files lie: the main checkout, a linked worktree of the
        # same repository, or outside the repository (never a case).
        targets = [(name, _edit_targets(rows)) for name, rows in
                   (('posttool', posttool), ('recall.edit', edits), ('stored', stored))]
        if any(tally for _, tally in targets):
            out.append('edit recall targets: ' + '; '.join(
                f'{name} {_tally(tally)}' for name, tally in targets if tally))
    scores = [row for row in window if row.get('event') == 'learning.score' and count(row.get('gold'))]
    if scores:
        last = scores[-1]
        parts = [f"gold={last['gold']}"]
        if count(last.get('candidateTrain')):
            parts.extend(f'{key}={last[key]}/{need}' for key, need in PROMOTION_NEEDS if count(last.get(key)))
        else:
            parts.append('candidate=none')
        if promotions:
            parts.append(f"last={esc(promotions[-1].get('outcome') or '-')} {_stamp(promotions[-1])}")
        out.append('promotion progress: ' + ' '.join(parts))
        rows, limit = last.get('caseRows'), last.get('caseLimit')
        if count(rows) and count(limit) and limit and rows >= limit * .9:
            out.append(f'WARNING learning case table: rows={rows}/{limit}; capture stops at the limit')
    full = sum(1 for row in window if row.get('event') == 'learning.capture.shared'
               and row.get('outcome') == 'queue_full')
    if full:
        out.append(f'WARNING learning capture queue_full: {full}')
    cycles = [row for row in window if row.get('event') == 'learning.cycle']
    deferred = Counter(str(row.get('reason')) for row in cycles if row.get('outcome') == 'deferred_resource')
    trained = [row for row in cycles if row.get('outcome') == 'trained']
    if deferred or trained:
        last = _stamp(trained[-1]) if trained else 'none'
        out.append('learning deferred: ' + (_tally(deferred) if deferred else 'none') + f'; last trained {esc(last)}')
    rotations = [row for row in window if row.get('event') == 'learning.rotation']
    rotated = [row['count'] for row in rotations if row.get('outcome') == 'deleted' and count(row.get('count'))]
    idle = Counter(str(row.get('outcome')) for row in rotations
                   if row.get('outcome') in ('nothing_closed', 'deferred'))
    if rotated or idle:
        # nothing_closed: the table is over its limit and every row is still protected.
        out.append(f'learning rotation: deleted={sum(rotated)} passes={len(rotated)}'
                   + (f' ({_tally(idle)})' if idle else ''))
    _selector_maintenance(out, window)


def _edit_targets(rows):
    return Counter(row['editTarget'] for row in rows if row.get('editTarget') in _EDIT_TARGETS)


def _selector_maintenance(out, window):
    """Selector maintenance runs by result, with the reasons of the unfinished ones."""
    runs = [row for row in window if row.get('event') == 'learning.selector.maintenance'
            and row.get('outcome') in ('completed', 'deferred', 'failed')]
    if not runs:
        return
    parts = [f"completed={sum(1 for row in runs if row['outcome'] == 'completed')}"]
    for outcome in ('deferred', 'failed'):
        reasons = Counter(str(row.get('reason')) for row in runs if row['outcome'] == outcome)
        parts.append(f'{outcome}={sum(reasons.values())}' + (f' ({_tally(reasons)})' if reasons else ''))
    stopped = Counter(str(row['incomplete']) for row in runs if isinstance(row.get('incomplete'), str))
    if stopped:
        # The step an unfinished run (``maintenance_incomplete``) stopped at.
        parts.append(f'incomplete({_tally(stopped)})')
    busy = [row['busyRetries'] for row in runs if count(row.get('busyRetries'))]
    if busy:
        # Consecutive retries behind another install's prepare lease (not failures).
        parts.append(f'busyRetriesMax={max(busy)}')
    out.append('selector maintenance: ' + ' '.join(parts))


def _teacher_waits(out, passes, now):
    """Per host: the current wait reason, since when it holds, and the queued cases."""
    if not passes:
        return
    last = passes[-1]
    for host, wait_key, queued_key in (('claude', 'waitClaude', 'queuedClaude'),
                                       ('codex', 'waitCodex', 'queuedCodex')):
        reason = last.get(wait_key)
        if not reason:
            continue
        since_row = last
        for row in reversed(passes):
            if row.get(wait_key) != reason:
                break
            since_row = row
        start = ts(since_row)
        hours = f' {max(0.0, (now - start).total_seconds() / 3600):.1f}h' if start else ''
        queued = last.get(queued_key) if count(last.get(queued_key)) else 0
        out.append(f'teacher wait {host}: {esc(reason)} since {esc(_stamp(since_row))}'
                   f'{hours} queued={queued}')

"""Service-level lines of the logs skill (``skills/logs/scripts/logs_slo.py``, ``logs.py --slo``).

Synthetic rows only. Every line is judged from counts and recorded durations
against its fixed target; a line with fewer rows than its floor reports
``no-data``. ``now`` is passed in, so no check depends on the clock.
"""
from __future__ import annotations

import contextlib
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import runpy
import sys
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SCRIPTS = ROOT / 'skills' / 'logs' / 'scripts'
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import logs_slo  # noqa: E402
from tests.support.case import FixtureCase  # noqa: E402

NOW = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)


def at(seconds):
    """ISO time ``seconds`` before NOW."""
    return (NOW - timedelta(seconds=seconds)).isoformat().replace('+00:00', 'Z')


def recall(index, **fields):
    row = {'ts': at(3600 - index), 'event': 'recall', 'outcome': 'served', 'host': 'claude',
           'session': 's1', 'ms': 600, 'selectorMs': 0}
    row.update(fields)
    return row


def by_name(rows, hours=24, **kwargs):
    return {line['line']: line for line in logs_slo.lines(rows, hours, now=NOW, **kwargs)}


class Lines(unittest.TestCase):
    def test_a_line_below_its_floor_reports_no_data(self):
        rows = [recall(i, pinMs=900, lexicalMs=900, stillCurrentMs=900, checkoutMs=900,
                       semantic='deferred_deadline', selectorLane='prompt', selectorMs=5000, ms=9000)
                for i in range(logs_slo.FLOOR - 1)]
        lines = by_name(rows)
        for name in ('hook', 'hook+selector', 'stage', 'semantic', 'selector', 'host claude'):
            self.assertEqual(lines[name]['status'], 'no-data', name)
            self.assertEqual(lines[name]['n'], logs_slo.FLOOR - 1, name)
        lines = by_name(rows + [recall(99, pinMs=900, lexicalMs=900, stillCurrentMs=900, checkoutMs=900,
                                       semantic='deferred_deadline', selectorLane='prompt',
                                       selectorMs=5000, ms=9000)])
        for name in ('hook', 'hook+selector', 'stage', 'semantic', 'selector'):
            self.assertEqual(lines[name]['status'], 'over', name)
        self.assertEqual(lines['host claude']['status'], 'shown')

    def test_hook_is_judged_without_the_selector_time_and_with_it(self):
        rows = [recall(i, ms=2000, selectorMs=1400, selectorLane='prompt') for i in range(20)]
        lines = by_name(rows)
        self.assertEqual((lines['hook']['status'], lines['hook']['p50'], lines['hook']['p95']), ('ok', 600, 600))
        self.assertEqual((lines['hook+selector']['status'], lines['hook+selector']['large']), ('over', 'ok'))
        self.assertEqual(lines['selector']['status'], 'over')
        # Two of twenty recalls spent their time outside the selector: the p95 is one of them.
        rows = [recall(i, ms=1500, selectorMs=1400 if i < 18 else 0) for i in range(20)]
        line = by_name(rows)['hook']
        self.assertEqual((line['status'], line['p50'], line['p95']), ('over', 100, 1500))
        rows = [recall(i, ms=1300, selectorMs=900, selectorLane='prompt') for i in range(20)]
        lines = by_name(rows)
        self.assertEqual([lines[name]['status'] for name in ('hook', 'hook+selector', 'selector')], ['ok'] * 3)

    def test_stage_counts_only_recalls_of_one_query_vector(self):
        slow = dict(pinMs=900, lexicalMs=900, stillCurrentMs=900, checkoutMs=900)
        fast = dict(pinMs=5, lexicalMs=5, stillCurrentMs=5, checkoutMs=5)
        rows = [recall(i, queryChunks=4, **slow) for i in range(30)]
        stage = by_name(rows)['stage']
        self.assertEqual((stage['status'], stage['n']), ('no-data', 0))
        self.assertNotIn('pinN', stage)
        rows += [recall(30 + i, queryChunks=1, **fast) for i in range(10)]
        rows += [recall(40 + i, **fast) for i in range(10)]  # a row from before the field has one vector
        stage = by_name(rows)['stage']
        self.assertEqual((stage['status'], stage['n'], stage['pin']), ('ok', 20, 5))
        # The hook line still counts every recall.
        self.assertEqual(by_name(rows)['hook']['n'], 50)

    def test_stage_targets_are_per_stage(self):
        ok = dict(pinMs=50, lexicalMs=50, stillCurrentMs=50, checkoutMs=300)
        self.assertEqual(by_name([recall(i, **ok) for i in range(20)])['stage']['status'], 'ok')
        for key in ok:
            with self.subTest(stage=key):
                rows = [recall(i, **{**ok, key: ok[key] + 1}) for i in range(20)]
                self.assertEqual(by_name(rows)['stage']['status'], 'over')
        # A stage no row measured is not a zero, and it does not hold back the measured ones.
        rows = [recall(i, pinMs=10, lexicalMs=10) for i in range(20)]
        stage = by_name(rows)['stage']
        self.assertEqual((stage['status'], stage['n'], stage['pinN']), ('ok', 20, 20))
        self.assertNotIn('checkout', stage)
        self.assertNotIn('checkoutN', stage)

    def test_a_stage_with_too_few_rows_does_not_hide_one_that_is_over(self):
        # Only served recalls record the still-current stage: 19 of them, among 40 recalls.
        rows = [recall(i, pinMs=900, lexicalMs=900, checkoutMs=10, stillCurrentMs=5) for i in range(19)]
        rows += [recall(19 + i, outcome='empty', pinMs=900, lexicalMs=900, checkoutMs=10) for i in range(21)]
        stage = by_name(rows)['stage']
        self.assertEqual((stage['status'], stage['n']), ('over', 40))
        self.assertEqual((stage['pinN'], stage['lexicalN'], stage['checkoutN'], stage['stillCurrentN']),
                         (40, 40, 40, 19))
        # The under-measured stage is shown and never judged: over its target with 19 rows changes nothing.
        fast = [dict(row, pinMs=5, lexicalMs=5) for row in rows]
        self.assertEqual(by_name(fast)['stage']['status'], 'ok')
        slow_tail = [dict(row, pinMs=5, lexicalMs=5, **({'stillCurrentMs': 900} if 'stillCurrentMs' in row else {}))
                     for row in rows]
        self.assertEqual(by_name(slow_tail)['stage']['status'], 'ok')
        # Every stage below the floor: nothing to judge.
        self.assertEqual(by_name(rows[:19])['stage']['status'], 'no-data')

    def test_semantic_ratio_leaves_out_recalls_without_a_query_vector(self):
        rows = [recall(i, semantic='complete', vectorSource='store') for i in range(19)]
        rows += [recall(19, semantic='deferred_deadline', vectorSource='store')]
        rows += [recall(20 + i, semantic='provider_absent') for i in range(30)]
        line = by_name(rows)['semantic']
        self.assertEqual((line['status'], line['n'], line['complete'], line['provider_absent'],
                          line['deferred_deadline']), ('ok', 20, 0.95, 30, 1))
        self.assertNotIn('arena', line)
        rows[0] = recall(0, semantic='deferred_work_budget', vectorSource='store')
        line = by_name(rows)['semantic']
        self.assertEqual((line['status'], line['complete'], line['deferred_work_budget']), ('over', 0.9, 1))

    def test_semantic_judges_the_arena_share_once_an_arena_answers(self):
        rows = [recall(i, semantic='complete', vectorSource='arena') for i in range(99)]
        rows.append(recall(99, semantic='complete', vectorSource='store'))
        line = by_name(rows)['semantic']
        self.assertEqual((line['status'], line['arena']), ('ok', 0.99))
        rows.append(recall(100, semantic='complete', vectorSource='store'))
        self.assertEqual(by_name(rows)['semantic']['status'], 'over')
        rows = [recall(i, semantic='complete', vectorSource='arena') for i in range(20)]
        rows.append(recall(20, semantic='deferred_cache_building'))
        line = by_name(rows)['semantic']
        self.assertEqual((line['status'], line['deferred_cache_building']), ('over', 1))

    def test_semantic_reads_the_vector_cache_state_when_rows_carry_it(self):
        rows = [recall(i, semantic='complete', vectorSource='store') for i in range(20)]
        line = by_name(rows)['semantic']
        self.assertEqual((line['vectorCache'], line['vectorCacheInconsistent']), ('unmeasured', 0))
        # A recall whose arena was switched off is left out of the arena share.
        rows = [recall(i, semantic='complete', vectorSource='arena', vectorCache='ok') for i in range(99)]
        rows += [recall(100 + i, semantic='complete', vectorSource='store', vectorCache='disabled')
                 for i in range(5)]
        line = by_name(rows)['semantic']
        self.assertEqual((line['status'], line['arena'], line['vectorCache']),
                         ('ok', 1.0, {'disabled': 5, 'ok': 99}))
        rows += [recall(111, semantic='complete', vectorSource='store', vectorCache='building'),
                 recall(112, semantic='complete', vectorSource='store', vectorCache='not-a-state')]
        line = by_name(rows)['semantic']
        self.assertEqual((line['status'], line['vectorCacheInconsistent'], line['vectorCache']),
                         ('over', 0, {'building': 1, 'disabled': 5, 'ok': 99}))

    def test_one_inconsistent_vector_cache_is_over_whatever_the_shares_are(self):
        # Every recall completes and the arena answers all of them: only the state differs.
        rows = [recall(i, semantic='complete', vectorSource='arena', vectorCache='ok') for i in range(200)]
        line = by_name(rows)['semantic']
        self.assertEqual((line['status'], line['complete'], line['arena'], line['vectorCacheInconsistent']),
                         ('ok', 1.0, 1.0, 0))
        rows[7] = recall(7, semantic='complete', vectorSource='arena', vectorCache='inconsistent')
        line = by_name(rows)['semantic']
        self.assertEqual((line['status'], line['complete'], line['arena'], line['vectorCacheInconsistent']),
                         ('over', 1.0, 1.0, 1))
        self.assertEqual(line['deferred_cache_building'], 0)

    def test_selector_counts_answers_that_ran_out_of_budget(self):
        rows = [recall(i, selectorLane='prompt', selectorMs=900) for i in range(49)]
        rows.append(recall(49, selectorLane='prompt', selectorMs=900,
                           selection_fallback='laya_selector_resident_service_deadline_exceeded'))
        line = by_name(rows)['selector']
        self.assertEqual((line['status'], line['overBudget']), ('ok', 0.02))
        rows.append(recall(50, selectorLane='prompt', selectorMs=900, selection_fallback='laya_selector_pair_budget'))
        self.assertEqual(by_name(rows)['selector']['status'], 'over')
        # A fallback that is not about time is not an over-budget answer.
        rows = [recall(i, selectorLane='prompt', selectorMs=900,
                       selection_fallback='laya_selector_not_prepared') for i in range(20)]
        self.assertEqual(by_name(rows)['selector']['overBudget'], 0)

    def test_hosts_are_shown_apart(self):
        rows = [recall(i, host='claude', ms=500, semantic='complete') for i in range(20)]
        rows += [recall(20 + i, host='codex', ms=5000, semantic='deferred_deadline') for i in range(3)]
        lines = by_name(rows)
        self.assertEqual((lines['host claude']['status'], lines['host claude']['p50'],
                          lines['host claude']['complete']), ('shown', 500, 1.0))
        self.assertEqual((lines['host codex']['status'], lines['host codex']['n'],
                          lines['host codex']['complete']), ('no-data', 3, 0.0))

    def cards(self, *names):
        return [{'path': 'wiki/' + name + '.md', 'sha': name[0] * 64} for name in names]

    def test_dedup_counts_cards_a_session_already_holds(self):
        rows = [recall(i, cards=self.cards('a%d' % i)) for i in range(19)]
        rows.append(recall(19, cards=self.cards('a0', 'b')))
        line = by_name(rows)['dedup']
        self.assertEqual((line['status'], line['n'], line['cards'], line['repeated'], line['allRepeated']),
                         ('ok', 20, 21, round(1 / 21, 4), 0))
        rows.append(recall(20, cards=self.cards('a1')))
        line = by_name(rows)['dedup']
        self.assertEqual((line['status'], line['allRepeated']), ('over', 1))
        # Another session, and a changed card (another hash), are not repeats.
        rows = [recall(i, cards=self.cards('a')) for i in range(1)]
        rows += [recall(1 + i, session='t%d' % i, cards=self.cards('a')) for i in range(20)]
        rows.append(recall(30, cards=[{'path': 'wiki/a.md', 'sha': 'f' * 64}]))
        self.assertEqual(by_name(rows)['dedup']['repeated'], 0)

    def test_dedup_starts_over_at_a_session_reset(self):
        rows = [recall(i, cards=self.cards('a')) for i in range(10)]
        rows.append({'ts': at(3600 - 10), 'event': 'update-queued', 'outcome': 'ok', 'session': 's1',
                     'sessionReset': 'compact'})
        rows += [recall(11 + i, cards=self.cards('a')) for i in range(10)]
        line = by_name(rows)['dedup']
        # Nine repeats before the reset, nine after it; the first recall after the reset is not one.
        self.assertEqual((line['n'], line['cards'], line['repeated']), (20, 20, 0.9))
        other = [dict(row, session='s2') if 'sessionReset' in row else row for row in rows]
        self.assertEqual(by_name(other)['dedup']['repeated'], 0.95)

    def test_dedup_window_applies_to_a_host_without_a_reset_signal(self):
        def session(host):
            rows = [recall(0, host=host, cards=self.cards('a'))]
            rows += [recall(1 + i, host=host, cards=self.cards('c%d' % i)) for i in range(logs_slo.REPEAT_WINDOW)]
            rows.append(recall(40, host=host, cards=self.cards('a')))
            return rows
        self.assertEqual(by_name(session('codex'))['dedup']['repeated'], 0)
        self.assertEqual(by_name(session('claude'))['dedup']['repeated'], round(1 / 22, 4))
        inside = session('codex')
        del inside[1]
        self.assertEqual(by_name(inside)['dedup']['repeated'], round(1 / 21, 4))

    def test_dedup_counts_recalls_the_exclusion_emptied_apart(self):
        rows = [recall(i, cards=self.cards('a%d' % i)) for i in range(20)]
        rows += [recall(20 + i, outcome='empty', reason='no_candidates_after_exclusion') for i in range(3)]
        line = by_name(rows)['dedup']
        self.assertEqual((line['status'], line['n'], line['noCandidatesAfterExclusion']), ('ok', 20, 3))

    def sync_pass(self, ended, ms, outcome='indexed', **fields):
        return {'ts': at(ended), 'event': 'wake.sync', 'outcome': outcome, 'ms': ms, **fields}

    def request(self, asked, event='file-change-event', **fields):
        row = {'ts': at(asked), 'event': event, 'outcome': 'ok', 'reason': 'sync_requested'}
        row.update(fields)
        return row

    def test_sync_measures_from_a_request_to_the_first_pass_that_started_after_it(self):
        # A pass that was already running when the request came cannot have seen it.
        rows = [self.request(1000), self.sync_pass(990, 30_000), self.sync_pass(900, 10_000)]
        line = by_name(rows)['sync']
        self.assertEqual((line['status'], line['n'], line['p50Seconds']), ('no-data', 1, 100.0))
        rows = [self.request(2000 - i) for i in range(5)] + [self.sync_pass(1900, 10_000)]
        line = by_name(rows)['sync']
        self.assertEqual((line['status'], line['n'], line['p50Seconds'], line['p95Seconds']),
                         ('ok', 5, 98.0, 100.0))
        rows = [self.request(2400 - i) for i in range(5)] + [self.sync_pass(1900, 10_000)]
        self.assertEqual(by_name(rows)['sync']['status'], 'over')
        # A row of any of the three events is a request only when it queued a wake.
        for event in ('index', 'update-queued', 'file-change-event'):
            with self.subTest(event=event):
                rows = [self.request(2000 - i, event=event) for i in range(5)]
                rows += [self.request(1995 - i, event=event, reason=None) for i in range(3)]
                rows.append(self.sync_pass(1900, 10_000))
                self.assertEqual(by_name(rows)['sync']['n'], 5)

    def test_an_edit_that_queued_no_wake_is_not_an_unanswered_request(self):
        # The last activity is edits outside the project (``index ok`` with no reason), long ago.
        rows = [self.request(3000, event='index'), self.sync_pass(2900, 10_000)]
        rows += [dict(self.request(2000 - i, event='index', reason=None), external_edits={'owners': 1})
                 for i in range(4)]
        line = by_name(rows)['sync']
        self.assertEqual((line['status'], line['n']), ('no-data', 1))
        self.assertNotIn('unanswered', line)

    def test_sync_reports_requests_no_pass_has_answered(self):
        rows = [self.request(200), self.request(100)]
        line = by_name(rows)['sync']
        self.assertEqual((line['status'], line['unanswered'], line['oldestUnansweredSeconds']),
                         ('no-data', 2, 200.0))
        rows = [self.request(logs_slo.SYNC_SECONDS + 1)]
        self.assertEqual(by_name(rows)['sync']['status'], 'over')

    def test_sync_failure_run_is_over_above_the_target_and_skips_progress_rows(self):
        failed = [self.sync_pass(500 - i, 100, outcome='failed', reason='target_deferred') for i in range(3)]
        rows = [self.sync_pass(900, 100)] + failed
        line = by_name(rows)['sync']
        self.assertEqual((line['status'], line['failedRun'], line['reasons']),
                         ('no-data', 3, {'target_deferred': 3}))
        rows += [self.sync_pass(400, 100, outcome='progress', reason='target_deferred'),
                 self.sync_pass(300, 100, outcome='failed', reason='owner_busy')]
        line = by_name(rows)['sync']
        self.assertEqual((line['status'], line['failedRun'], line['lastOk']), ('over', 4, at(900)))
        # The run is counted over every retained row, also outside the window.
        self.assertEqual(by_name(rows, hours=0.01)['sync']['failedRun'], 4)

    def test_failed_counts_refusals_that_name_no_reason(self):
        rows = [{'ts': at(10), 'event': 'gate', 'outcome': 'refused', 'reason': 'selected_proof_changed'},
                {'ts': at(9), 'event': 'recall', 'outcome': 'served'}]
        self.assertEqual(by_name(rows)['failed'], {'line': 'failed', 'status': 'ok', 'withoutReason': 0})
        rows += [{'ts': at(8), 'event': 'wake.sync', 'outcome': 'failed'},
                 {'ts': at(7), 'event': 'gate', 'outcome': 'refused', 'reason': ''}]
        line = by_name(rows)['failed']
        self.assertEqual((line['status'], line['withoutReason'], line['events']),
                         ('over', 2, {'wake.sync': 1, 'gate': 1}))

    def test_learning_counts_the_funnel_and_rows_without_a_reason(self):
        def cycle(seconds, outcome, **fields):
            return {'ts': at(seconds), 'event': 'learning.cycle', 'outcome': outcome, **fields}
        rows = [{'ts': at(900), 'event': 'learning.capture', 'outcome': 'stored'},
                {'ts': at(890), 'event': 'learning.capture', 'outcome': 'no_snapshot_candidates'},
                {'ts': at(880), 'event': 'learning.teacher', 'outcome': 'gold'},
                cycle(800, 'trained'), cycle(700, 'deferred_resource', reason='learning_yield'),
                cycle(600, 'deferred_resource', reason='learning_yield'),
                cycle(500, 'blocked', reason='trainer_failed'), cycle(400, 'rearmed')]
        line = by_name(rows)['learning']
        self.assertEqual((line['status'], line['stored'], line['gold'], line['trained'],
                          line['deferred_resource'], line['blocked'], line['rearmed'], line['withoutReason']),
                         ('ok', 1, 1, 1, 2, 1, 1, 0))
        self.assertNotIn('deferredSince', line)
        rows += [cycle(300, 'deferred_resource', reason='memory'), cycle(200, 'deferred_resource')]
        line = by_name(rows)['learning']
        self.assertEqual((line['status'], line['withoutReason'], line['deferredSince']), ('over', 1, at(300)))

    def test_render_is_one_escaped_line(self):
        line = {'line': 'sync', 'status': 'over', 'n': 2, 'p95Seconds': 12.5, 'reasons': {'a\nb': 2}}
        text = logs_slo.render(line, lambda value: str(value).replace('\n', '\\n'))
        self.assertEqual(text, 'slo sync: over n=2 p95Seconds=12.5 reasons=a\\nb:2')


class Skill(FixtureCase):
    def setUp(self):
        super().setUp()
        self.logs = runpy.run_path(str(SCRIPTS / 'logs.py'), run_name='memvane_logs_skill')
        self.state = self.tmp / 'state'
        self.state.mkdir(mode=0o700)

    def write(self, rows):
        path = self.state / 'events.jsonl'
        path.write_bytes(b''.join(json.dumps(row).encode() + b'\n' for row in rows))
        os.chmod(path, 0o600)

    def run_logs(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = self.logs['main'](list(argv))
        return code, out.getvalue(), err.getvalue()

    def now_row(self, **fields):
        return {'v': 1, 'ts': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'), **fields}

    def test_slo_view_prints_every_line_and_writes_nothing(self):
        self.write([self.now_row(event='recall', outcome='served', host='claude', ms=100, selectorMs=0,
                                 pinMs=1, lexicalMs=1, stillCurrentMs=1, checkoutMs=1, semantic='complete')
                    for _ in range(20)])
        before = sorted((p.name, p.lstat().st_mtime_ns) for p in self.state.iterdir())
        code, out, err = self.run_logs('--state-root', str(self.state), '--slo')
        self.assertEqual(code, 0, err)
        lines = [line.strip() for line in out.splitlines() if line.strip().startswith('slo ')]
        self.assertEqual(lines[0], 'slo (last 24h)')
        self.assertEqual([line.split(':')[0] for line in lines[1:]],
                         ['slo install', 'slo hook', 'slo hook+selector', 'slo stage', 'slo semantic', 'slo selector',
                          'slo host claude', 'slo dedup', 'slo sync', 'slo failed', 'slo learning', 'slo identity'])
        self.assertIn('slo hook: ok n=20 p50=100 p95=100 targetP50=700 targetP95=1200', out)
        self.assertIn('slo install: shown size=unknown', out)
        self.assertIn('slo stage: ok n=20 pin=1 pinN=20 pinTarget=50 lexical=1 lexicalN=20 lexicalTarget=50 '
                      'lexicalLargeTarget=150 lexicalLarge=ok', out)
        self.assertIn('slo semantic: ok n=20', out)
        self.assertIn('slo identity: no-data', out)
        self.assertEqual(sorted((p.name, p.lstat().st_mtime_ns) for p in self.state.iterdir()), before)
        code, out, err = self.run_logs('--state-root', str(self.state), '--slo', '--json')
        self.assertEqual(code, 0, err)
        rows = [json.loads(line)['slo'] for line in out.splitlines()]
        self.assertEqual(rows[0], {'line': 'install', 'status': 'shown', 'size': 'unknown'})
        self.assertEqual(rows[1], {'line': 'hook', 'status': 'ok', 'n': 20, 'p50': 100, 'p95': 100,
                                   'targetP50': 700, 'targetP95': 1200})

    def test_large_targets_apply_to_an_install_of_more_than_a_thousand_cards(self):
        wiki = self.tmp / 'wiki'
        (wiki / 'concepts').mkdir(parents=True)
        (wiki / '.hidden').mkdir()
        (self.state / 'route.json').write_text(json.dumps({'collections': [
            {'name': 'raw', 'role': 'raw', 'sourceRoot': str(self.tmp)},
            {'name': 'wiki', 'role': 'wiki', 'sourceRoot': str(wiki)}]}), encoding='utf-8')
        for index in range(logs_slo.LARGE_CARDS):
            (wiki / 'concepts' / ('c%04d.md' % index)).touch()
        (wiki / 'concepts' / 'note.txt').touch()
        (wiki / '.hidden' / 'x.md').touch()
        self.assertEqual(logs_slo.install_size(self.state), 'standard')
        self.write([self.now_row(event='recall', outcome='served', host='claude', ms=1900, selectorMs=1300,
                                 selectorLane='prompt', pinMs=1, lexicalMs=120, stillCurrentMs=1, checkoutMs=1)
                    for _ in range(20)])
        out = self.run_logs('--state-root', str(self.state), '--slo')[1]
        self.assertIn('slo install: shown size=standard', out)
        self.assertIn('slo hook+selector: over n=20 p50=1900 p95=1900 targetP50=1500 targetP95=2500', out)
        self.assertIn('slo stage: over n=20 pin=1 pinN=20 pinTarget=50 lexical=120 lexicalN=20 lexicalTarget=50 '
                      'stillCurrent', out)
        self.assertNotIn('lexicalLarge', out)
        (wiki / 'one-more.md').touch()
        self.assertEqual(logs_slo.install_size(self.state), 'large')
        out = self.run_logs('--state-root', str(self.state), '--slo')[1]
        self.assertIn('slo install: shown size=large', out)
        self.assertIn('slo hook+selector: ok n=20 p50=1900 p95=1900 targetP50=2000 targetP95=3000', out)
        self.assertIn('slo stage: ok n=20 pin=1 pinN=20 pinTarget=50 lexical=120 lexicalN=20 lexicalTarget=150 '
                      'stillCurrent', out)
        # No readable route: the standard target judges and the large verdict stands beside it.
        (self.state / 'route.json').unlink()
        self.assertIsNone(logs_slo.install_size(self.state))
        out = self.run_logs('--state-root', str(self.state), '--slo')[1]
        self.assertIn('lexical=120 lexicalN=20 lexicalTarget=50 lexicalLargeTarget=150 lexicalLarge=ok', out)
        self.assertIn('targetP50=1500 targetP95=2500 largeTargetP50=2000 largeTargetP95=3000 large=ok', out)

    def test_identity_shows_whether_the_sealed_device_number_is_the_live_one(self):
        self.write([self.now_row(event='gate', outcome='served')])
        live = os.stat(self.state).st_dev
        for stored, moved in ((live, False), (live + 7, True)):
            with self.subTest(moved=moved):
                (self.state / 'route.json').write_text(json.dumps({'stateNode': [stored, 1]}), encoding='utf-8')
                code, out, err = self.run_logs('--state-root', str(self.state), '--slo')
                self.assertEqual(code, 0, err)
                self.assertIn(f'slo identity: shown storedDevice={stored} liveDevice={live} moved={moved}', out)
        (self.state / 'route.json').write_text('{"stateNode": "x"}', encoding='utf-8')
        self.assertIn('slo identity: no-data', self.run_logs('--state-root', str(self.state), '--slo')[1])

    def test_summary_counts_the_sync_failure_run_per_install_in_the_unified_view(self):
        def passes(install, *outcomes):
            return [{'ts': at(1000 - index), 'event': 'wake.sync', 'outcome': outcome, 'install': install,
                     'reason': 'target_deferred' if outcome == 'failed' else None}
                    for index, outcome in enumerate(outcomes)]
        stalled = passes('a' * 32, 'indexed', 'failed', 'failed', 'failed')
        publishing = [dict(row, ts=at(500 - index)) for index, row in
                      enumerate(passes('b' * 32, 'failed', 'indexed'))]
        merged = sorted(stalled + publishing, key=lambda row: row['ts'])
        def summary(**kwargs):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.logs['summary'](merged, SimpleNamespace(hours=None), **kwargs)
            return [line for line in out.getvalue().splitlines() if line.startswith('sync consecutive')]
        self.assertEqual(summary(unified=True),
                         ['sync consecutive failed passes: install=' + 'a' * 32
                          + ' 3 (target_deferred 3) last ok ' + at(1000),
                          'sync consecutive failed passes: install=' + 'b' * 32 + ' 0 last ok ' + at(499)])
        # One target's rows are one run whatever install id each row logged.
        self.assertEqual(summary(), ['sync consecutive failed passes: 0 last ok ' + at(499)])


if __name__ == '__main__':
    unittest.main(verbosity=2)

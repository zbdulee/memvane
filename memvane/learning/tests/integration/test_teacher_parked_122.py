"""1.0.22 A-1: an answer received under resource pressure is kept, not discarded.

Fake host CLIs answer on the child ``PATH`` (``fake_teacher_cli_121``); the
scope check is the fixture's, made to raise ``ResourceDeferred`` at a chosen
ledger stage. Assertions are case rows, ledger stages, launch and body
counts, breaker counters and event rows, never elapsed time.
"""
from __future__ import annotations

import json
import os
from unittest import mock

from memvane.kernel.resource_deferred import ResourceDeferred
from memvane.learning.context_learning import general_gold_ledger as ledger
from memvane.learning.context_learning import general_teacher, shared, teacher_state
from memvane.learning.context_learning.default_teacher import NOT_HOST_FAILURES
from memvane.learning.tests.fake_teacher_cli_121 import TeacherFixture
from memvane.learning.tests.integration.test_worker_teacher_121 import WorkerFixture


class ParkMixin:
    """The scope check defers while the case's ledger stage matches ``defer_at``."""

    def install_scope(self):
        self.defer_at = ()
        self.fail_at = {}
        self.launch_bodies = 0
        self.deferrals = []
        self.patches.enter_context(mock.patch.object(general_teacher, '_scope_default',
                                                     side_effect=self.scope))

    def scope(self, *args, **kwargs):
        if kwargs.get('fresh_inventory'):
            self.launch_bodies = self.bodies()  # the launch check of each call
        stage = self.stage('a1')
        if stage in self.defer_at and self.when():
            self.deferrals.append(stage)
            raise ResourceDeferred('fixture_pressure')
        if stage in self.fail_at:
            raise ValueError(self.fail_at[stage])
        return self.grant

    def when(self):
        return True

    def stage(self, request_id):
        with shared._db(self.root) as db:
            ledger.ensure(db)
        return super().stage(request_id)

    def bodies(self, host='claude'):
        return len(self.records(host, 'body'))

    def breaker(self, host='claude'):
        return self.host_state()['hosts'][host]['consecutiveFailures']

    def retries(self, rid='a1'):
        with shared._db(self.root) as db:
            return ledger.retries(db, shared._project_key(self.project), rid)

    def park(self):
        self.add('a1')
        self.defer_at = ('answer_received',)
        result = self.run_pass()
        self.defer_at = ()
        self.assertEqual(result['status'], 'deferred_resource')
        self.assertEqual(self.cases()['a1'][0], 'first_done')
        self.assertEqual(self.stage('a1'), 'answer_parked')
        return result


class Parked(ParkMixin, TeacherFixture):
    def setUp(self):
        super().setUp()
        self.install_scope()

    def test_t1_1_kept_answer_carries_usage_and_no_breaker(self):
        self.park()
        self.assertEqual(self.runs('claude'), 1)
        self.assertEqual(self.breaker(), 0)
        kept = json.loads(self.cases()['a1'][3])
        self.assertEqual(kept['choice']['selected_ids'], ['wiki/card0.md'])
        self.assertEqual(kept['teacher']['host'], 'claude')
        self.assertIn('usage', kept['choice'])
        parked = self.teacher_events('parked')
        self.assertEqual(len(parked), 1)
        self.assertEqual((parked[0]['reason'], parked[0]['usageIn'], parked[0]['usageOut']),
                         ('resource_deferred', 750, 10))
        self.assertEqual(self.teacher_events('gold'), [])

    def test_t1_2_next_pass_commits_without_a_call_even_with_no_queued_case(self):
        self.park()
        result = self.run_pass()
        self.assertEqual(self.runs('claude'), 1)
        self.assertEqual(self.cases()['a1'][0], 'gold')
        self.assertEqual(self.stage('a1'), 'committed')
        gold = json.loads(self.cases()['a1'][5])
        self.assertEqual((gold['selected_ids'], gold['review'], gold['teacher']['host']),
                         (['wiki/card0.md'], 'single_pass', 'claude'))
        events = self.teacher_events('gold')
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['reason'], 'parked_answer')
        self.assertEqual(result['outcomes'], ['gold'])
        self.run_pass()
        self.assertEqual(len(self.teacher_events('gold')), 1)
        self.assertEqual(self.runs('claude'), 1)

    def test_t1_3_kept_answer_needs_current_consent_unchanged_active_case(self):
        changes = {
            'consent': lambda: mock.patch.object(general_teacher, '_consent_file_v2',
                                                 side_effect=ValueError('teacher_consent_required')),
            'bodies': None, 'inactive': None}
        for name in ('consent', 'bodies', 'inactive', 'binding'):
            with self.subTest(change=name):
                with shared._db(self.root) as db:
                    ledger.ensure(db)
                    db.execute('DELETE FROM cases')
                    db.execute('DELETE FROM project_gold_attempts')
                self.park()
                before = self.runs('claude')
                if name == 'consent':
                    with changes['consent']():
                        self.assertEqual(self.run_pass()['status'], 'consent_required')
                    expected = ('pending_review', 'teacher_consent_required')
                elif name == 'bodies':
                    with shared._db(self.root) as db:
                        bodies = json.loads(db.execute("SELECT bodies FROM cases WHERE request_id='a1'").fetchone()[0])
                        bodies['wiki/card1.md'] += ' changed'
                        db.execute("UPDATE cases SET bodies=? WHERE request_id='a1'", (json.dumps(bodies),))
                    self.run_pass()
                    expected = ('pending_review', 'gold_capture_changed')
                elif name == 'inactive':
                    general_teacher._case_active.return_value = False
                    try:
                        self.run_pass()
                    finally:
                        general_teacher._case_active.return_value = True
                    expected = ('pending_review', 'teacher_scope_changed')
                else:
                    # A plugin update changes only the grant's binding.
                    original = self.grant
                    self.grant = dict(self.grant, binding={'codeSha256': 'f' * 64})
                    try:
                        self.run_pass()
                    finally:
                        self.grant = original
                    expected = ('gold', None)
                self.assertEqual(self.cases()['a1'][:2], expected)
                self.assertEqual(self.stage('a1'), 'committed' if expected[0] == 'gold' else 'dispatched_uncertain')
                self.assertEqual(self.runs('claude'), before)
                self.assertEqual(self.breaker(), 0)

    def test_t1_4_recovery_after_a_crash_keeps_the_kept_answer(self):
        self.park()
        with shared._db(self.root) as db:
            ledger.recover(db, shared._project_key(self.project))
        self.assertEqual(self.stage('a1'), 'answer_parked')
        self.assertEqual(self.cases()['a1'][0], 'first_done')
        self.run_pass()
        self.assertEqual((self.cases()['a1'][0], self.stage('a1'), self.runs('claude')), ('gold', 'committed', 1))

    def test_t1_5_system_pressure_after_the_body_does_not_stop_the_call(self):
        self.add('a1')
        self.scenario('claude', {'mode': 'sleep', 'seconds': 2})
        # Not ready from the moment the body is in; before 1.0.22 this stopped the call.
        report = lambda *a, **k: {'ready': not self.records('claude', 'body')}
        with mock.patch.object(general_teacher, 'resource_report', side_effect=report):
            self.run_pass()
        self.assertEqual((self.bodies(), self.cases()['a1'][0]), (1, 'gold'))
        self.assertEqual(self.breaker(), 0)

    def test_t1_6_child_rss_after_the_body_stops_and_counts_on_the_breaker(self):
        self.scenario('claude', {'mode': 'sleep', 'seconds': 3})
        usage = lambda pid: ((999999.0, 0.0) if pid != os.getpid() and self.bodies() > self.launch_bodies
                             else (10.0, 0.0))
        with mock.patch.object(general_teacher, '_process_usage', side_effect=usage):
            for index in range(4):
                self.add(f'r{index}', tag=str(index))
            result = self.run_pass()
        self.assertEqual(self.runs('claude'), 3)
        for index in range(3):
            self.assertEqual(self.cases()[f'r{index}'][:2], ('pending_review', 'gold_attempt_failed'))
        self.assertEqual(self.cases()['r3'][0], 'queued')
        row = self.host_state()['hosts']['claude']
        self.assertEqual((row['reason'], row['consecutiveFailures']), ('teacher_host_failing', 3))
        self.assertEqual(result['hosts']['claude'], 'teacher_host_failing')
        reasons = [e['reason'] for e in self.teacher_events('pending_review')]
        self.assertEqual(reasons, ['gold_memory_pressure'] * 3)

    def test_t1_8_pre_send_deferral_sends_nothing_and_spends_no_attempt(self):
        self.add('a1')
        self.defer_at = ('dispatched',)
        for _ in range(6):
            result = self.run_pass()
            self.assertEqual(result['status'], 'deferred_resource')
        self.assertEqual(self.bodies(), 0)
        self.assertEqual(self.cases()['a1'][0], 'queued')
        self.assertEqual(self.stage('a1'), 'pre_send_rejected')
        self.assertEqual((self.retries(), self.breaker()), (0, 0))
        self.assertEqual(self.teacher_events('pending_review'), [])

    def test_t1_8_launch_deferral_is_not_a_scope_change(self):
        self.add('a1')
        self.defer_at = ('reserved',)
        self.assertEqual(self.run_pass()['status'], 'deferred_resource')
        self.assertEqual(self.runs('claude'), 0)
        self.assertEqual((self.cases()['a1'][0], self.stage('a1'), self.retries()), ('queued', 'pre_send_rejected', 0))

    def test_t1_8_codex_pre_write_deferral_sends_no_byte(self):
        self.add('a1', host='codex')
        self.defer_at = ('dispatched',)
        self.assertEqual(self.run_pass()['status'], 'deferred_resource')
        self.assertEqual([r for r in self.records('codex') if 'stdin' in r], [])
        self.assertEqual((self.cases()['a1'][0], self.stage('a1'), self.retries()), ('queued', 'pre_send_rejected', 0))

    def test_t1_9_deferral_after_the_body_continues_to_gold(self):
        self.add('a1')
        self.scenario('claude', {'mode': 'sleep', 'seconds': 2})
        self.defer_at = ('dispatched',)
        self.when = lambda: bool(self.records('claude', 'body'))
        self.run_pass()
        self.assertTrue(self.deferrals)
        self.assertEqual((self.cases()['a1'][0], self.stage('a1')), ('gold', 'committed'))
        self.assertEqual(self.breaker(), 0)

    def test_t1_10_only_scope_and_case_changes_spare_the_breaker(self):
        codes = sorted(NOT_HOST_FAILURES) + ['gold_other_failure']
        self.assertEqual(set(codes[:-1]), {'teacher_scope_changed', 'gold_capture_changed',
                                           'teacher_cohort_changed', 'gold_case_changed_before_commit'})
        for index, code in enumerate(codes):
            with self.subTest(code=code):
                with shared._db(self.root) as db:
                    ledger.ensure(db)
                    db.execute('DELETE FROM cases')
                    db.execute('DELETE FROM project_gold_attempts')
                self.add('a1', tag=str(index))
                self.fail_at = {'answer_received': code}
                self.run_pass()
                self.fail_at = {}
                self.assertEqual(self.cases()['a1'][:2], ('pending_review', 'gold_attempt_failed'))
                self.assertEqual(self.breaker(), 1 if code == 'gold_other_failure' else 0)


class ParkedWorker(ParkMixin, WorkerFixture):
    """T1-1, T1-2 and T1-8 through the worker entry."""

    def setUp(self):
        super().setUp()
        self.install_scope()

    def test_worker_parks_then_commits_without_a_call(self):
        self.add('a1')
        self.defer_at = ('answer_received',)
        result = self.worker()
        self.defer_at = ()
        self.assertEqual((result['status'], result['teacher']['status']), ('deferred_resource', 'deferred_resource'))
        self.assertEqual((self.cases()['a1'][0], self.stage('a1'), self.breaker()), ('first_done', 'answer_parked', 0))
        self.assertEqual(self.runs('claude'), 1)
        result = self.worker()
        self.assertEqual(self.runs('claude'), 1)
        self.assertEqual((self.cases()['a1'][0], self.stage('a1')), ('gold', 'committed'))
        self.assertEqual(result['teacher']['outcomes'], ['gold'])
        self.assertEqual(len(self.teacher_events('gold')), 1)

    def test_worker_pre_send_deferral_keeps_the_case_queued(self):
        self.add('a1')
        self.defer_at = ('dispatched',)
        for _ in range(6):
            self.assertEqual(self.worker()['status'], 'deferred_resource')
        self.assertEqual(self.bodies(), 0)
        self.assertEqual((self.cases()['a1'][0], self.stage('a1'), self.retries(), self.breaker()),
                         ('queued', 'pre_send_rejected', 0, 0))


if __name__ == '__main__':
    import unittest
    unittest.main()

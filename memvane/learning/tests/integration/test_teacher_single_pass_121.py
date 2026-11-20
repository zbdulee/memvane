"""1.0.21 S: single-pass gold and failure handling through real fake-CLI processes."""
from __future__ import annotations

import json
from unittest import mock

from memvane.learning.context_learning import general_gold_ledger as ledger
from memvane.learning.context_learning import general_teacher, prequential, shared, teacher_state
from memvane.learning.context_learning.automatic_staging import _quality_ready
from memvane.learning.tests.fake_teacher_cli_121 import TeacherFixture


class SinglePass(TeacherFixture):
    def test_s1_one_valid_answer_is_gold_with_one_run(self):
        self.add('a1')
        result = self.run_pass()
        self.assertEqual(result['status'], 'processed')
        self.assertEqual(self.runs('claude'), 1)
        status, error, use, first, second, gold = self.cases()['a1']
        self.assertEqual((status, error, use, second), ('gold', None, 'active', None))
        gold = json.loads(gold)
        self.assertEqual(gold['review'], 'single_pass')
        self.assertEqual(gold['selected_ids'], ['wiki/card0.md'])
        self.assertEqual(gold['teacher'], {'host': 'claude', 'model': 'claude-haiku-5-5', 'tier': 1})
        self.assertEqual(self.stage('a1'), 'committed')
        self.run_pass()
        self.assertEqual(self.runs(), 1)

    def test_s2_empty_selection_is_gold_training_and_counted_empty(self):
        self.add('a1')
        self.scenario('claude', {'answer': 'empty'})
        self.run_pass()
        self.assertEqual(json.loads(self.cases()['a1'][5])['selected_ids'], [])
        with shared._db(self.root) as db:
            rows = prequential.admitted(db, self.project, self.grant, statuses=('gold',))
        self.assertEqual([row[1][1] for row in rows], ['a1'])
        self.assertEqual(rows[0][0], 'train')
        cohort = {'minQualityTrainCases': 0, 'minTrainFamilies': 0, 'minValidationCases': 1,
                  'minValidationFamilies': 1, 'minEmptyValidation': 1}
        ready, empty, _, _ = _quality_ready([], [row for _, row in rows], cohort)
        self.assertTrue(ready)
        self.assertEqual(empty, 1)

    def test_s3_spawn_failure_requeues_and_next_pass_calls(self):
        self.add('a1')
        fake = self.bin / 'claude'
        good = fake.read_text()
        fake.write_text(good.replace(good.splitlines()[0], '#!/nonexistent/interpreter'))
        result = self.run_pass()
        self.assertEqual(result['status'], 'processed')
        self.assertEqual(self.cases()['a1'][:2], ('queued', None))
        self.assertEqual(self.stage('a1'), 'pre_send_rejected')
        with shared._db(self.root) as db:
            self.assertEqual(ledger.retries(db, shared._project_key(self.project), 'a1'), 1)
        fake.write_text(good)
        self.run_pass()
        self.assertEqual(self.runs('claude'), 1)
        self.assertEqual(self.cases()['a1'][0], 'gold')
        self.assertEqual(self.stage('a1'), 'committed')

    def test_s4_guard_failure_after_reservation_is_pre_send(self):
        self.add('a1')
        original = general_teacher._scope_default.side_effect

        def scope(root, policy, *, fresh_inventory=False):
            if fresh_inventory:
                raise ValueError('fixture_scope_moved')
            return original(root, policy)
        general_teacher._scope_default.side_effect = scope
        self.run_pass()
        self.assertEqual(self.runs(), 0)
        self.assertEqual(self.cases()['a1'][0], 'queued')
        self.assertEqual(self.stage('a1'), 'pre_send_rejected')
        self.assertEqual(len(self.teacher_events('pre_send_rejected')), 1)

    def test_s5_quota_refusal_waits_without_pending_review(self):
        self.add('a1', host='codex')
        self.scenario('codex', {'mode': 'quota'})
        self.run_pass()
        self.assertEqual(self.runs('codex'), 1)
        self.assertEqual(self.cases()['a1'][0], 'queued')
        self.assertEqual(self.stage('a1'), 'provider_refused')
        row = self.host_state()['hosts']['codex']
        self.assertEqual(row['reason'], 'teacher_provider_waiting')
        self.assertIsNotNone(teacher_state.host_waiting(self.host_state(), 'codex'))
        for _ in range(10):
            result = self.run_pass()
            self.assertEqual(result['hosts'], {'codex': 'teacher_provider_waiting'})
        self.assertEqual(self.runs('codex'), 1)
        statuses = [row[0] for row in self.cases().values()]
        self.assertEqual(statuses.count('pending_review'), 0)
        with shared._db(self.root) as db:
            self.assertEqual(ledger.retries(db, shared._project_key(self.project), 'a1'), 0)
        self.expire_waits()
        self.scenario('codex', {})
        self.run_pass()
        self.assertEqual(self.runs('codex'), 2)
        self.assertEqual(self.cases()['a1'][0], 'gold')

    def test_s6_auth_and_model_refusals_wait_like_quota(self):
        for mode in ('auth', 'model'):
            with self.subTest(mode=mode):
                self.add('c-' + mode, host='codex', tag=mode)
                self.scenario('codex', {'mode': mode})
                before = self.runs('codex')
                self.run_pass()
                self.assertEqual(self.runs('codex'), before + 1)
                self.assertEqual(self.cases()['c-' + mode][0], 'queued')
                self.assertEqual(self.stage('c-' + mode), 'provider_refused')
                self.assertEqual(self.host_state()['hosts']['codex']['reason'], 'teacher_provider_waiting')
                self.expire_waits()
                self.scenario('codex', {})
                self.run_pass()
                self.assertEqual(self.cases()['c-' + mode][0], 'gold')
        self.add('cl-auth', host='claude')
        self.scenario('claude', {'mode': 'auth'})
        self.run_pass()
        self.assertEqual(self.cases()['cl-auth'][0], 'queued')
        self.assertEqual(self.stage('cl-auth'), 'provider_refused')
        self.assertEqual(self.host_state()['hosts']['claude']['reason'], 'teacher_provider_waiting')

    def test_s7_content_refusal_reviews_only_that_case(self):
        self.add('a1', host='codex', tag='x')
        self.add('a2', host='codex', tag='y')
        self.queue('codex', [{'mode': 'content'}, {}])
        self.run_pass()
        self.assertEqual(self.cases()['a1'][:2], ('pending_review', 'gold_provider_rejected_input'))
        self.assertEqual(self.stage('a1'), 'provider_refused')
        self.assertEqual(self.cases()['a2'][0], 'gold')
        self.assertIsNone(teacher_state.host_waiting(self.host_state(), 'codex'))

    def test_s8_unknown_and_invalid_failures_are_not_retried(self):
        modes = [('timeout', 'codex', {'mode': 'sleep', 'seconds': 30}, 'gold_attempt_failed'),
                 ('exit', 'codex', {'mode': 'exit'}, 'gold_attempt_failed'),
                 ('transport', 'codex', {'mode': 'transport'}, 'gold_attempt_failed'),
                 ('invalid', 'codex', {'mode': 'invalid'}, 'gold_invalid_choice'),
                 ('cl-exit', 'claude', {'mode': 'exit'}, 'gold_attempt_failed'),
                 ('cl-invalid', 'claude', {'mode': 'invalid'}, 'gold_invalid_choice')]
        for name, host, scenario, error in modes:
            with self.subTest(name=name):
                self.add(name, host=host, tag=name)
                self.scenario(host, scenario)
                before = self.runs()
                # Only the timeout case shortens the deadline below the fake's sleep.
                self.policy['safetySeconds'] = 3 if name == 'timeout' else 60
                self.run_pass()
                self.policy['safetySeconds'] = 60
                self.assertEqual(self.runs(), before + 1)
                self.assertEqual(self.cases()[name][:2], ('pending_review', error))
                self.assertEqual(self.stage(name), 'dispatched_uncertain')
                self.run_pass()
                self.assertEqual(self.runs(), before + 1)
                state = teacher_state.read_state(self.root)
                for row in state['hosts'].values():
                    row.update(consecutiveFailures=0, waitUntilUnixMs=None, reason=None)
                self.assertTrue(teacher_state.write_state(self.root, state))

    def test_s9_same_input_second_case_is_not_called(self):
        sample, bodies = self.add('a1', text='Identical author text for both captures.')
        sample = dict(sample, request_id='a2')
        self.add('a2', sample=sample, bodies=bodies)
        self.run_pass()
        self.assertEqual(self.runs(), 1)
        self.assertEqual(self.cases()['a2'][:2], ('pending_review', 'duplicate_input'))

    def test_s10_five_pre_send_failures_exhaust_without_ledger_columns(self):
        self.add('a1')
        fake = self.bin / 'claude'
        good = fake.read_text()
        fake.write_text(good.replace(good.splitlines()[0], '#!/nonexistent/interpreter'))
        for attempt in range(1, 6):
            self.run_pass()
            with shared._db(self.root) as db:
                self.assertEqual(ledger.retries(db, shared._project_key(self.project), 'a1'), attempt)
        self.assertEqual(self.cases()['a1'][:2], ('pending_review', 'gold_attempts_exhausted'))
        with shared._db(self.root) as db:
            columns = db.execute('PRAGMA table_info(project_gold_attempts)').fetchall()
        self.assertEqual(len(columns), 10)
        fake.write_text(good)
        self.run_pass()
        self.assertEqual(self.runs(), 0)

    def test_s11_breaker_rests_host_after_three_and_other_host_continues(self):
        for index in range(4):
            self.add(f'c{index}', host='codex', tag=str(index))
        self.add('cl', host='claude')
        self.scenario('codex', {'mode': 'transport'})
        result = self.run_pass()
        self.assertEqual(self.runs('codex'), 3)
        self.assertEqual(self.runs('claude'), 1)
        self.assertEqual(self.cases()['c3'][0], 'queued')
        self.assertEqual(self.cases()['cl'][0], 'gold')
        row = self.host_state()['hosts']['codex']
        self.assertEqual((row['reason'], row['consecutiveFailures']), ('teacher_host_failing', 3))
        self.assertEqual(result['hosts']['codex'], 'teacher_host_failing')
        self.run_pass()
        self.assertEqual(self.runs('codex'), 3)

    def test_s11_unwritable_state_means_no_remaining_calls(self):
        for index in range(3):
            self.add(f'a{index}', tag=str(index))
        with mock.patch.object(teacher_state, 'write_state', return_value=False):
            result = self.run_pass()
        self.assertEqual(result['status'], 'state_unavailable')
        self.assertEqual(self.runs(), 0)
        writes = iter([True, False])
        with mock.patch.object(teacher_state, 'write_state', side_effect=lambda *a: next(writes, False)):
            result = self.run_pass()
        self.assertEqual(result['status'], 'state_unavailable')
        self.assertEqual(self.runs(), 1)

    def test_s12_interrupted_first_running_goes_to_review_without_call(self):
        self.add('a1', status='first_running')
        with shared._db(self.root) as db:
            ledger.reserve(db, shared._project_key(self.project), 'a1', 'first', 'a' * 64, None,
                           rearm=True)
            ledger.transition(db, shared._project_key(self.project), 'a1', 'first', ('reserved',), 'dispatched')
        self.run_pass()
        self.assertEqual(self.cases()['a1'][:2], ('pending_review', 'interrupted_external_attempt'))
        self.assertEqual(self.stage('a1'), 'dispatched_uncertain')
        self.assertEqual(self.runs(), 0)


class Ledger(TeacherFixture):
    def test_rearm_only_for_refused_or_pre_send_and_never_for_v1(self):
        key = shared._project_key(self.project)
        with shared._db(self.root) as db:
            ledger.reserve(db, key, 'r', 'first', 'b' * 64, None, rearm=True)
            ledger.transition(db, key, 'r', 'first', ('reserved',), 'provider_refused')
            with self.assertRaisesRegex(ValueError, 'gold_attempt_already_reserved'):
                ledger.reserve(db, key, 'r', 'first', 'b' * 64, 20)
            ledger.reserve(db, key, 'r', 'first', 'b' * 64, None, rearm=True)
            self.assertEqual(ledger.stage(db, key, 'r', 'first'), 'reserved')
            ledger.transition(db, key, 'r', 'first', ('reserved',), 'dispatched_uncertain')
            with self.assertRaisesRegex(ValueError, 'gold_attempt_already_reserved'):
                ledger.reserve(db, key, 'r', 'first', 'b' * 64, None, rearm=True)
            with self.assertRaisesRegex(ValueError, 'invalid_gold_reservation'):
                ledger.reserve(db, key, 'n', 'first', 'b' * 64, None)


class Migration(TeacherFixture):
    """§14-7 user decision (b): the teacher grant time is the cutoff."""

    def test_git_failure_waits_and_unaffected_project_runs(self):
        self.add('a1')
        from memvane.learning.context_learning import edit_git_migration as migration
        with mock.patch.object(migration, 'affected', side_effect=ValueError('edit_git_check_failed')):
            self.assertEqual(self.run_pass()['status'], 'migration_pending')
        self.assertEqual(self.runs(), 0)
        with mock.patch.object(migration, 'affected', return_value=False):
            self.assertEqual(self.run_pass()['status'], 'processed')
        self.assertEqual(self.runs(), 1)

    def test_affected_project_keeps_every_pre_grant_edit_case_out(self):
        # A 1.0.19 marker no longer matters: cases before the grant leave either way.
        self.proposal['grantedAtUnixMs'] = 3_000_000
        self.add('old', captured_at=500, tag='o')
        self.add('edge', captured_at=2999.999, tag='e')
        self.add('new', captured_at=3000, tag='n')
        from memvane.learning.context_learning import edit_git_migration as migration
        with mock.patch.object(migration, 'affected', return_value=True):
            self.run_pass()
        cases = self.cases()
        self.assertEqual(cases['old'][1:3], ('pre_teacher_capture', 'historical'))
        self.assertEqual(cases['edge'][1:3], ('pre_teacher_capture', 'historical'))
        self.assertEqual(cases['new'][0], 'gold')
        self.assertEqual(self.runs(), 1)
        self.assertEqual(len(cases), 3)

    def test_unaffected_project_keeps_pre_grant_cases(self):
        self.proposal['grantedAtUnixMs'] = 3_000_000
        self.add('old', captured_at=500, tag='o')
        from memvane.learning.context_learning import edit_git_migration as migration
        with mock.patch.object(migration, 'affected', return_value=False):
            self.run_pass()
        self.assertEqual(self.cases()['old'][0], 'gold')


if __name__ == '__main__':
    import unittest
    unittest.main()

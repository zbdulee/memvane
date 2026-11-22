"""Counts a teacher pass leaves for the learning funnel (event fields and one state count).

Real passes over fake host CLIs (``TeacherFixture``). Assertions are event
fields, the consent count of ``teacher-host-state.json`` and case rows.
"""
from __future__ import annotations

from unittest import mock

from memvane.kernel.resource_deferred import ResourceDeferred
from memvane.learning.context_learning import general_teacher, shared, teacher_consent, teacher_state
from memvane.learning.tests.fake_teacher_cli_121 import TeacherFixture

INTERRUPTED = 'interrupted_external_attempt'


class TeacherCounts(TeacherFixture):
    def passes(self):
        return self.teacher_events('pass')

    def consent(self):
        return self.host_state()['consent']

    def unreadable_pass(self):
        with mock.patch.object(general_teacher, '_consent_file_v2',
                               side_effect=ResourceDeferred(teacher_consent.UNREADABLE)):
            result = self.run_pass()
        self.assertEqual((result['status'], result['reason']),
                         ('deferred_resource', teacher_consent.UNREADABLE))

    def test_a_call_cut_short_is_counted_once_as_interrupted(self):
        self.add('cut-1', status='first_running')
        self.add('cut-2', status='first_running')
        self.add('fresh')
        self.run_pass()
        rows = [e for e in self.teacher_events('pending_review') if e['reason'] == INTERRUPTED]
        self.assertEqual([row['count'] for row in rows], [2])
        cases = self.cases()
        self.assertEqual([cases[name][:2] for name in ('cut-1', 'cut-2')],
                         [('pending_review', INTERRUPTED)] * 2)
        self.assertEqual(self.runs('claude'), 1)
        self.run_pass()
        self.assertEqual(len([e for e in self.teacher_events() if e['reason'] == INTERRUPTED]), 1)

    def test_a_pass_reports_the_hosts_it_prepared_and_when_the_first_pre_send_check_ran(self):
        self.add('one')
        self.run_pass()
        row = self.passes()[-1]
        self.assertEqual((row['reason'], row['calls'], row['preparedHosts']), ('processed', 1, 1))
        self.assertIs(type(row['preCallMs']), int)
        self.assertGreaterEqual(row['preCallMs'], 0)

    def test_a_pass_deferred_before_sending_still_prepared_the_host(self):
        self.add('one')
        with mock.patch.object(general_teacher, 'resource_report', return_value={'ready': False}):
            self.run_pass()
        row = self.passes()[-1]
        self.assertEqual((row['reason'], row['calls'], row['preparedHosts']),
                         ('deferred_resource', 0, 1))
        self.assertIs(type(row['preCallMs']), int)
        self.assertEqual(self.runs('claude'), 0)
        self.assertEqual(self.cases()['one'][0], 'queued')

    def test_a_pass_whose_host_waits_prepares_no_host_and_reaches_no_pre_send_check(self):
        self.add('one')
        state = self.host_state()
        state['hosts']['claude'].update(reason='teacher_provider_waiting',
                                        waitUntilUnixMs=teacher_state.now_ms() + 3600 * 1000)
        self.assertTrue(teacher_state.write_state(self.root, state))
        self.run_pass()
        row = self.passes()[-1]
        self.assertEqual((row['calls'], row['preparedHosts'], row['preCallMs']), (0, 0, None))

    def test_unreadable_consent_passes_are_counted_and_a_readable_pass_clears_the_count(self):
        self.add('one')
        for expected in (1, 2, 3):
            self.unreadable_pass()
            self.assertEqual(self.consent()['unreadable'], expected)
        rows = [e for e in self.teacher_events('deferred')
                if e['reason'] == teacher_consent.UNREADABLE]
        self.assertEqual([row['consentUnreadable'] for row in rows], [1, 2, 3])
        self.assertEqual((self.runs(), self.cases()['one'][0]), (0, 'queued'))
        state = self.host_state()
        state['consent']['lastNoticeUnixMs'] = 5
        self.assertTrue(teacher_state.write_state(self.root, state))
        self.run_pass()
        self.assertEqual(self.consent(), {'unreadable': 0, 'lastNoticeUnixMs': None})

    def test_a_deferral_for_another_reason_is_not_counted_as_unreadable_consent(self):
        self.add('one')
        with mock.patch.object(general_teacher, '_consent_file_v2',
                               side_effect=ResourceDeferred('provider_resource_busy')):
            self.assertEqual(self.run_pass()['status'], 'deferred_resource')
        self.assertEqual(self.consent()['unreadable'], 0)
        self.assertEqual(self.teacher_events('deferred'), [])

    def test_a_readable_pass_with_no_count_does_not_write_the_state_file(self):
        with shared._db(self.root):
            pass
        self.run_pass()
        self.assertFalse((self.root / teacher_state.STATE_NAME).exists())

    def test_the_state_keeps_the_consent_count_and_drops_malformed_values(self):
        state = teacher_state.empty_state()
        state['consent'] = {'unreadable': 7, 'lastNoticeUnixMs': 9, 'other': 1}
        self.assertTrue(teacher_state.write_state(self.root, state))
        self.assertEqual(self.consent(), {'unreadable': 7, 'lastNoticeUnixMs': 9})
        for bad in ({'unreadable': -1, 'lastNoticeUnixMs': 0}, {'unreadable': True}, 'x', None):
            state['consent'] = bad
            self.assertTrue(teacher_state.write_state(self.root, state))
            self.assertEqual(self.consent(), {'unreadable': 0, 'lastNoticeUnixMs': None})

"""Going back to the previous model: the reverse of an application, and evidence prepared again.

The fixture is the candidate-apply integration fixture (real database,
journal, pointer and evidence files; a fake resident transport).
"""
from __future__ import annotations

import json
import unittest
from unittest import mock

from memvane.learning.context_learning import candidate_apply as A
from memvane.learning.context_learning import candidate_rollback as R
from memvane.learning.context_learning import inference_identity, learning_profile, prequential
from memvane.learning.tests.integration import test_context_learning_candidate_apply as apply_tests


class Case(unittest.TestCase):
    def fixture(self):
        case = apply_tests.Checks('test_success_and_repeated_wake_never_republishes_or_trains')
        case.setUp()
        self.addCleanup(case.doCleanups)
        return case

    def read(self, case, name):
        return json.loads((case.root / name).read_text())

    def reprepare(self, case):
        """What a plugin update's inference preparation does: a new receipt for the same pointer."""
        receipt = {**self.read(case, inference_identity.RECEIPT), 'createdAt': 2, 'pluginVersion': '9.9.9'}
        case.json(case.root / inference_identity.RECEIPT, receipt)
        case.json(case.root / inference_identity.BINDING,
                  {'schema': inference_identity.SCHEMA, 'receipt_sha256': learning_profile.digest(receipt)})
        return receipt


class Reprepared(Case):
    def test_an_applied_model_stays_readable_after_its_evidence_is_prepared_again(self):
        case = self.fixture()
        self.assertEqual(case.run_apply()['status'], 'applied')
        A.readable(case.root)
        self.reprepare(case)
        A.readable(case.root)

    def test_an_unfinished_application_is_still_refused(self):
        case = self.fixture()

        def stop(at):
            if at == 'pointer_committed':
                raise Stopped()
        with self.assertRaises(Stopped):
            case.run_apply(fault=stop)
        with self.assertRaisesRegex(ValueError, 'apply_activation_pending'):
            A.readable(case.root)
        # A receipt that names another pointer, or a binding of another receipt, is refused as before.
        applied = self.fixture()
        self.assertEqual(applied.run_apply()['status'], 'applied')
        receipt = self.read(applied, inference_identity.RECEIPT)
        applied.json(applied.root / inference_identity.RECEIPT, {**receipt, 'checkpoint_sha256': '0' * 64})
        with self.assertRaisesRegex(ValueError, 'apply_activation_pending'):
            A.readable(applied.root)
        applied.json(applied.root / inference_identity.RECEIPT, {**receipt, 'createdAt': 3})
        with self.assertRaisesRegex(ValueError, 'apply_activation_pending'):
            A.readable(applied.root)


class Stopped(RuntimeError):
    pass


FILES = ('active-checkpoint.json', inference_identity.RECEIPT, inference_identity.BINDING)


class Restore(Case):
    def applied(self, stop=None):
        case = self.fixture()
        self.before = {name: (case.root / name).read_bytes() for name in FILES}
        if stop is None:
            self.assertEqual(case.run_apply()['status'], 'applied')
        else:
            def fault(at):
                if at == stop:
                    raise Stopped()
            with self.assertRaises(Stopped):
                case.run_apply(fault=fault)
        return case

    def files(self, case):
        return {name: (case.root / name).read_bytes() for name in FILES}

    def restored(self, case, result):
        self.assertEqual((result['status'], result['changesApplied'], result['cycle_id']),
                         ('restored', 1, case.cycle), result)
        self.assertEqual(self.files(case), self.before)
        row = A.read_journal(case.root)
        self.assertEqual((row['phase'], row['reason']), ('rejected', R.REASON))
        A.readable(case.root)

    def test_the_plan_writes_nothing(self):
        case = self.applied()
        tree = {path: path.read_bytes() for path in case.root.rglob('*') if path.is_file()}
        plan = R.plan(case.root)
        self.assertEqual((plan['status'], plan['changesApplied'], plan['cycle_id'], plan['phase']),
                         ('rollback_planned', 0, case.cycle, 'activated'))
        self.assertEqual((plan['appliedArtifactSha256'], plan['previousArtifactSha256'], plan['previousCycle']),
                         (case.stage['artifactSha256'], case.old['artifact_sha256'], 'base'))
        self.assertEqual({path: path.read_bytes() for path in case.root.rglob('*') if path.is_file()}, tree)

    def test_the_previous_pointer_receipt_and_binding_come_back_and_the_candidate_is_not_applied_again(self):
        case = self.applied()
        self.restored(case, R.restore(case.root))
        # The same request again changes nothing; neither does the worker's next pass.
        again = R.restore(case.root)
        self.assertEqual((again['status'], again['changesApplied']), ('already_restored', 0))
        events = list(case.events)
        answer = case.run_apply()
        self.assertEqual((answer['status'], answer['reason'], case.events), ('apply_blocked', 'candidate_rejected', events))
        self.assertEqual(self.files(case), self.before)
        self.assertIn(case.cycle, prequential.rejected_cycles(case.root))
        with case.db(None) as db:
            self.assertEqual(db.execute('SELECT status FROM cycles WHERE cycle_id=?', (case.cycle,)).fetchone()[0],
                             'applied')

    def test_an_application_stuck_before_its_activation_is_restored(self):
        case = self.applied(stop='pointer_committed')
        self.assertEqual(R.plan(case.root)['phase'], 'pointer_committed')
        self.restored(case, R.restore(case.root))

    def test_evidence_prepared_again_since_the_application_is_restored_for_the_previous_pointer(self):
        case = self.applied()
        fresh = self.reprepare(case)
        result = R.restore(case.root)
        self.assertEqual(result['status'], 'restored', result)
        receipt = self.read(case, inference_identity.RECEIPT)
        self.assertEqual(self.read(case, 'active-checkpoint.json'), case.old)
        # The receipt bound now keeps what the preparation verified and names the previous pointer.
        self.assertEqual({key: value for key, value in receipt.items() if key not in ('checkpoint_sha256', 'createdAt')},
                         {key: value for key, value in fresh.items() if key not in ('checkpoint_sha256', 'createdAt')})
        self.assertEqual(receipt['checkpoint_sha256'], learning_profile.digest(case.old))
        self.assertEqual(self.read(case, inference_identity.BINDING),
                         {'schema': inference_identity.SCHEMA, 'receipt_sha256': learning_profile.digest(receipt)})
        A.readable(case.root)

    POINTS = ('journal_closed', 'receipt_restored', 'binding_restored', 'pointer_restored')

    def stopped(self, point, stop=None):
        case = self.applied(stop=stop)

        def fault(at):
            if at == point:
                raise Stopped()
        with self.assertRaises(Stopped):
            R.restore(case.root, fault=fault)
        return case

    def test_a_restore_stopped_at_any_step_is_finished_by_the_same_request(self):
        for point in self.POINTS:
            with self.subTest(point=point):
                case = self.stopped(point)
                # The decision is the first write: the journal already says so.
                row = A.read_journal(case.root)
                self.assertEqual((row['phase'], row['reason']), ('rejected', R.REASON))
                result = R.restore(case.root)
                if point == 'pointer_restored':
                    self.assertEqual((result['status'], result['changesApplied']), ('already_restored', 0))
                    self.assertEqual(self.files(case), self.before)
                else:
                    self.restored(case, result)
                self.assertEqual(R.restore(case.root)['status'], 'already_restored')

    def test_a_worker_pass_never_finishes_a_stopped_restore_the_other_way(self):
        # W13b review F-2: whatever the journal was, and wherever the restore stopped.
        for phase_stop in (None, 'pointer_committed'):
            for point in self.POINTS:
                with self.subTest(journal=phase_stop or 'activated', point=point):
                    case = self.stopped(point, stop=phase_stop)
                    events = list(case.events)
                    if point != 'pointer_restored':
                        with self.assertRaisesRegex(ValueError, 'apply_activation_pending'):
                            A.readable(case.root)  # Closed until someone finishes it.
                    answer = case.run_apply()
                    self.assertEqual((answer['status'], answer['reason'], case.events),
                                     ('apply_blocked', 'candidate_rejected', events))
                    # The worker pass's own part finishes the restore; nothing is applied.
                    resumed = R.resume(case.root)
                    self.assertEqual(None if resumed is None else resumed['status'],
                                     None if point == 'pointer_restored' else 'restored')
                    self.assertEqual(self.files(case), self.before)
                    A.readable(case.root)
                    self.assertEqual(R.restore(case.root)['status'], 'already_restored')

    def test_a_worker_pass_starts_no_restore_of_its_own(self):
        case = self.applied()
        after = self.files(case)
        self.assertIsNone(R.resume(case.root))
        self.assertEqual((self.files(case), A.read_journal(case.root)['phase']), (after, 'activated'))
        self.assertEqual(R.restore(case.root, resume_only=True)['status'], 'nothing_to_resume')
        self.assertEqual(self.files(case), after)

    def later_candidate(self, case, phase):
        """What a later candidate's intent leaves: the applied journal archived, its own journal live."""
        applied = A.read_journal(case.root)
        A._archive(case.root, applied)
        later = {**applied, 'apply_id': 'f' * 32, 'cycle_id': 'cohort-' + 'b' * 20, 'phase': phase,
                 'old_pointer': applied['new_pointer'],
                 'new_pointer': {**applied['new_pointer'], 'cycle_id': 'cohort-' + 'b' * 20,
                                 'application': {'schema': A.SCHEMA, 'id': 'f' * 32}}}
        A.put(case.root / A.JOURNAL, later)
        return applied, later

    def test_an_application_archived_behind_a_later_candidate_is_still_restored(self):
        # W13b review F-1: the pointer is the applied candidate's whatever the live journal says.
        for phase in ('intent', 'prepared', 'rejected'):
            with self.subTest(later=phase):
                case = self.applied()
                applied, later = self.later_candidate(case, phase)
                A.readable(case.root)
                plan = R.plan(case.root)
                self.assertEqual((plan['status'], plan['cycle_id'], plan['phase']),
                                 ('rollback_planned', case.cycle, 'activated'))
                result = R.restore(case.root)
                self.assertEqual((result['status'], result['cycle_id']), ('restored', case.cycle), result)
                self.assertEqual(self.files(case), self.before)
                archived = self.read(case, A.ARCHIVE + '/' + applied['apply_id'] + '.json')
                self.assertEqual((archived['phase'], archived['reason']), ('rejected', R.REASON))
                self.assertIn(case.cycle, prequential.rejected_cycles(case.root))
                live = case.root / A.JOURNAL
                if phase == 'rejected':
                    # A finished journal of the later candidate is its own record: kept.
                    self.assertEqual(A.read_journal(case.root), later)
                else:
                    # It committed nothing and names a pointer that is no longer active: gone.
                    self.assertFalse(live.exists())
                A.readable(case.root)
                self.assertEqual(R.restore(case.root)['status'], 'already_restored')

    def test_a_restore_that_stopped_before_its_history_entry_adds_it_on_the_next_request(self):
        # W13b review F-3.
        case = self.applied()
        self.assertTrue(prequential.sync_history(case.root))
        with mock.patch.object(R, '_history', side_effect=Stopped()):
            with self.assertRaises(Stopped):
                R.restore(case.root)
        self.assertEqual([entry['cycle_id'] for entry in prequential.read_history(case.root)], [case.cycle])
        self.assertEqual(R.restore(case.root)['status'], 'already_restored')
        self.assertEqual([entry['cycle_id'] for entry in prequential.read_history(case.root)],
                         [case.cycle, prequential.BASE])
        R.restore(case.root)
        self.assertEqual(len(prequential.read_history(case.root)), 2)

    def test_a_held_lock_defers_and_writes_nothing(self):
        import fcntl
        import os
        for name in ('candidate-apply.lock', 'cycles/cycle.lock'):
            with self.subTest(lock=name):
                case = self.applied()
                after = self.files(case)
                fd = os.open(case.root / name, os.O_RDWR | os.O_CREAT, 0o600)
                self.addCleanup(os.close, fd)
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                result = R.restore(case.root)
                self.assertEqual((result['status'], result['changesApplied']), ('rollback_deferred', 0))
                self.assertEqual((self.files(case), A.read_journal(case.root)['phase']), (after, 'activated'))

    def test_changed_evidence_or_a_changed_previous_artifact_is_refused_before_any_write(self):
        case = self.applied()
        after = self.files(case)
        receipt = self.read(case, inference_identity.RECEIPT)
        case.json(case.root / inference_identity.RECEIPT, {**receipt, 'checkpoint_sha256': '0' * 64})
        foreign = self.files(case)
        for call in (R.plan, R.restore):
            result = call(case.root)
            self.assertEqual((result['status'], result['reason'], result['changesApplied']),
                             ('rollback_blocked', 'rollback_evidence_changed', 0))
        self.assertEqual(self.files(case), foreign)
        case.json(case.root / inference_identity.RECEIPT, receipt)
        case.write(case.root / 'base.artifact', b'other')
        result = R.restore(case.root)
        self.assertEqual((result['status'], result['reason']), ('rollback_blocked', 'rollback_previous_artifact_changed'))
        self.assertEqual((self.files(case), A.read_journal(case.root)['phase']), (after, 'activated'))

    def test_nothing_applied_is_nothing_to_restore(self):
        case = self.fixture()
        before = self.files(case)
        self.assertEqual(R.plan(case.root), {'status': 'nothing_to_restore', 'changesApplied': 0})
        self.assertEqual(R.restore(case.root), {'status': 'nothing_to_restore', 'changesApplied': 0})
        # An application that never wrote its pointer has nothing to put back either.
        early = self.applied(stop='prepared')
        self.assertEqual(R.restore(early.root)['status'], 'nothing_to_restore')
        # A candidate the application itself rejected is not a restored one.
        refused = self.fixture()
        A.apply_candidate(refused.root, refused.policy, refused.cycle, transport=lambda *_: {'status': 'failed'})
        self.assertEqual(A.read_journal(refused.root)['phase'], 'rejected')
        self.assertEqual(R.restore(refused.root)['status'], 'nothing_to_restore')
        self.assertEqual(self.files(case), before)

    def test_the_history_names_the_previous_checkpoint_from_the_restore_on(self):
        case = self.applied()
        self.assertTrue(prequential.sync_history(case.root))
        self.assertEqual([entry['cycle_id'] for entry in prequential.read_history(case.root)], [case.cycle])
        R.restore(case.root)
        entries = prequential.read_history(case.root)
        self.assertEqual([entry['cycle_id'] for entry in entries], [case.cycle, prequential.BASE])
        self.assertGreaterEqual(entries[1]['appliedAtUnixMs'], entries[0]['appliedAtUnixMs'])
        # The next pass has nothing to add for a base pointer.
        self.assertTrue(prequential.sync_history(case.root))
        self.assertEqual(len(prequential.read_history(case.root)), 2)


class Project(Restore):
    """The command for a project: its selector namespace, the event and the upkeep request."""

    def call(self, case, **kwargs):
        from unittest import mock
        from memvane.learning import selector_maintenance
        events, requests = [], []
        with mock.patch.object(selector_maintenance, '_selector_state', return_value=case.root), \
                mock.patch.object(selector_maintenance, '_selected_route', return_value={'installId': 'i'}), \
                mock.patch.object(selector_maintenance, 'request',
                                  side_effect=lambda decision, **kw: requests.append(kw)), \
                mock.patch.object(prequential, '_emit',
                                  side_effect=lambda _project, event, **fields: events.append((event, fields))):
            result = R.restore_project(case.project, **kwargs)
        return result, events, requests

    def test_the_plan_reports_and_asks_for_nothing(self):
        case = self.applied()
        after = self.files(case)
        result, events, requests = self.call(case, plan_only=True)
        self.assertEqual((result['status'], events, requests, self.files(case)), ('rollback_planned', [], [], after))

    def test_a_restore_is_recorded_once_and_asks_the_resident_upkeep(self):
        case = self.applied()
        result, events, requests = self.call(case)
        self.restored(case, result)
        self.assertEqual(events, [('learning.promotion', {'outcome': 'rolled_back', 'cycle': case.cycle,
                                                          'previousCycle': 'base'})])
        self.assertEqual(requests, [{'force': True}])
        again, events, requests = self.call(case)
        self.assertEqual((again['status'], events, requests), ('already_restored', [], []))

    def test_a_project_without_a_selector_has_nothing_to_restore(self):
        case = self.fixture()
        (case.root / 'active-checkpoint.json').unlink()
        result, events, requests = self.call(case)
        self.assertEqual((result, events, requests), ({'status': 'nothing_to_restore', 'changesApplied': 0}, [], []))

    def test_the_learning_cli_dispatches_both_restore_commands(self):
        import contextlib
        import io
        from unittest import mock
        from memvane.learning import default_teacher_enable
        from memvane.learning.context_learning import shared
        seen = []
        with mock.patch.object(R, 'restore_project',
                               side_effect=lambda project, plan_only=False: seen.append(('model', project, plan_only))
                               or {'status': 'rollback_planned'}), \
                mock.patch.object(default_teacher_enable, 'restore_project',
                                  side_effect=lambda project, plan_only=False: seen.append(('teacher', project, plan_only))
                                  or {'status': 'restore_planned'}):
            for argv in (['restore-previous-model', '--project', '/p', '--plan'], ['restore-previous-model', '--project', '/p'],
                         ['restore-teacher-enable', '--project', '/p', '--plan']):
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(shared.main(argv), 0)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(shared.main(['teacher-off', '--project', '/p', '--plan']), 1)
        self.assertEqual(seen, [('model', '/p', True), ('model', '/p', False), ('teacher', '/p', True)])
        self.assertEqual(json.loads(out.getvalue())['reason'], 'teacher_setup_action_invalid')

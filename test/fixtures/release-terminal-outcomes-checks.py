"""Release outcome admission, using isolated fake owners and real move results.

The protocol cases use the existing synthetic bundle boundary; they do not
prove journal ownership. The projection case uses the selected-install fixture
and real upgrade.run, with only the downstream move failure injected.
"""
from contextlib import ExitStack
import importlib.util
import json
from pathlib import Path
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent


def fixture(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


RELEASE = fixture('release_fixture_for_terminal_results', 'release-coordination-checks.py')
MOVE = fixture('move_fixture_for_terminal_results', 'laya-project-dir-move-checks.py')


class TerminalOutcomes(unittest.TestCase):
    def release_case(self):
        case = RELEASE.ReleaseCoordinationChecks('runTest')
        self.addCleanup(case.doCleanups)
        case.setUp()
        return case

    def bundle(self, case, **config):
        root = case.fake_bundle(**config)
        # Existing fake_bundle supplies standard journal/build observations.
        return root

    def test_terminal_bound_inactive_and_move_outcomes_are_accepted_with_their_owner(self):
        case = self.release_case()
        for status, kind in (('bound_upgrade_complete', 'bound'),
                             ('inactive_upgrade_complete', 'inactive'),
                             ('state_move_complete', 'active'),
                             ('project_dir_move_complete', 'active')):
            with self.subTest(status=status):
                root = self.bundle(case, status=status, kind=kind)
                worker = RELEASE.PreparedWorker(root, case.projects[0],
                                                ready_seconds=5, run_seconds=5)
                try:
                    worker.ready()
                    worker.go()
                    try:
                        result = worker.finish()
                    except RELEASE.ReleaseError as exc:
                        self.fail('terminal %s was rejected: %s' % (status, exc.reason))
                    self.assertEqual(result['results'][0]['status'], status)
                    self.assertEqual(result['owners'], [{
                        'stateRoot': str(case.projects[0] / 'state'), 'pluginRoot': str(root)}])
                finally:
                    worker.close()
                case.assert_reaped(root)

    def test_a_pending_journals_previous_owner_does_not_complete_the_release(self):
        case = self.release_case()
        root = self.bundle(case, status='current', kind='active', journalState='pending')
        worker = RELEASE.PreparedWorker(root, case.projects[0], ready_seconds=5, run_seconds=5)
        try:
            worker.ready()
            worker.go()
            with self.assertRaises(RELEASE.ReleaseError):
                worker.finish()
        finally:
            worker.close()
        case.assert_reaped(root)

    def test_a_current_bundle_with_an_incomplete_followup_move_is_not_complete(self):
        case = self.release_case()
        for phase in ('deferred', 'failed', 'refused'):
            with self.subTest(phase=phase):
                root = self.bundle(case, status='current', extra={
                    'projectDirMove': {'status': phase, 'reason': 'synthetic_move_' + phase}})
                worker = RELEASE.PreparedWorker(root, case.projects[0],
                                                ready_seconds=5, run_seconds=5)
                try:
                    worker.ready()
                    worker.go()
                    with self.assertRaises(RELEASE.ReleaseError):
                        worker.finish()
                finally:
                    worker.close()
                case.assert_reaped(root)

    def test_real_upgrade_run_preserves_a_failed_followup_project_move(self):
        case = MOVE.MoveCase('runTest')
        self.addCleanup(case.doCleanups)
        case.setUp()
        with ExitStack() as patches:
            patches.enter_context(mock.patch.object(MOVE.upgrade, '_move_state',
                                                      return_value=(case.state, None)))
            patches.enter_context(mock.patch.object(MOVE.upgrade, '_move_project_dir',
                side_effect=MOVE.dir_move.ProjectDirMoveBlocked('project_dir_conflict')))
            patches.enter_context(mock.patch.object(MOVE.upgrade, '_learning_upkeep', return_value={}))
            rows = MOVE.upgrade.run(str(case.project), root=case.plugin, home=case.home)
        row = next(row for row in rows if row.get('stateRoot') == str(case.state))
        print(json.dumps({'case': 'actual-followup-projection', 'row': row}), flush=True)
        # A consumer must see the followup failure even when the bundle itself
        # is current; accepting this row must not mark the release complete.
        phase = row.get('projectDirMove')
        self.assertIsInstance(phase, dict, 'run drops the followup move outcome')
        self.assertEqual(phase.get('status'), 'refused')
        self.assertEqual(phase.get('reason'), 'project_dir_conflict')
        self.assertEqual(MOVE.upgrade.read_status(case.state)['projectDir']['status'], 'refused')

    def test_final_journal_kind_owner_record_and_observation_changes_are_rejected(self):
        case = self.release_case()
        cases = {
            'classified-kind-drift': {'observedKind': 'bound'},
            'status-kind-mismatch': {'status': 'bound_upgrade_complete'},
            'wrong-owner': {'ownerRoot': str(case.base / 'not-preferred')},
            'active-candidate': {'journalState': 'candidate_ready'},
            'bound-active': {'status': 'bound_upgrade_complete', 'kind': 'bound', 'journalState': 'active'},
            'bound-building': {'status': 'bound_upgrade_complete', 'kind': 'bound', 'buildPhase': 'building'},
            'inactive-bound': {'status': 'inactive_upgrade_complete', 'kind': 'inactive', 'buildPhase': 'bound'},
            'record-scope': {'status': 'inactive_upgrade_complete', 'kind': 'inactive',
                             'buildIdentityOverrides': {'scopeId': 'd' * 32}},
            'record-state': {'status': 'bound_upgrade_complete', 'kind': 'bound',
                             'buildIdentityOverrides': {'stateRoot': str(case.base)}},
            'journal-epoch-drift': {'epochChanges': True},
            'build-phase-drift': {'status': 'inactive_upgrade_complete', 'kind': 'inactive', 'phaseChanges': True},
            'wrong-bundle-kind': {'identityOverrides': {'bundleKind': 'engine_subbundle'}},
            'unrecognized-success': {'status': 'synthetic_new_success'},
            'unknown-followup': {'extra': {'stateMove': {'status': 'synthetic_new_success'}}},
        }
        for name, config in cases.items():
            with self.subTest(case=name):
                root = self.bundle(case, **config)
                worker = RELEASE.PreparedWorker(root, case.projects[0], ready_seconds=5, run_seconds=5)
                try:
                    worker.ready()
                    worker.go()
                    with self.assertRaises(RELEASE.ReleaseError):
                        worker.finish()
                finally:
                    worker.close()
                case.assert_reaped(root)

    def test_final_state_root_is_proved_after_the_move_not_at_its_old_location(self):
        case = self.release_case()
        final = case.projects[0] / 'state-companion'
        for stale in (False, True):
            with self.subTest(stale_phase_root=stale):
                root = self.bundle(case, status='state_move_complete', finalStateRoot=str(final),
                    extra={'stateMove': {'status': 'state_move_complete',
                        'stateRoot': str(case.projects[0] / 'state') if stale else str(final)}})
                worker = RELEASE.PreparedWorker(root, case.projects[0], ready_seconds=5, run_seconds=5)
                try:
                    worker.ready()
                    worker.go()
                    if stale:
                        with self.assertRaises(RELEASE.ReleaseError):
                            worker.finish()
                    else:
                        result = worker.finish()
                        self.assertEqual(result['owners'], [{'stateRoot': str(final), 'pluginRoot': str(root)}])
                finally:
                    worker.close()
                case.assert_reaped(root)

    def test_malformed_result_and_phase_values_refuse_with_release_error(self):
        from release_coordination import _completed_rows
        base = {'stateRoot': '/synthetic/private-state', 'status': 'current', 'kind': 'active'}
        for rows in (
                [None], [base, {}], [{**base, 'status': []}], [{**base, 'kind': {}}],
                [{**base, 'projectDirMove': {'status': []}}],
                [{'stateRoot': base['stateRoot'], 'learningState': {}}],
                [base, base], [base, {'stateRoot': base['stateRoot'], 'selectorWarm': 'selector_warm_busy'}]):
            with self.subTest(rows=rows), self.assertRaises(RELEASE.ReleaseError):
                _completed_rows(rows)


if __name__ == '__main__':
    unittest.main()

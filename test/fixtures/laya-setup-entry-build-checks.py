#!/usr/bin/env python3
"""Synthetic setup front door checks; no QMD, provider or install execution."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'core'))
import laya_initial_build
import laya_product_install as product
import setup_entry


class SetupInitialBuild(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='laya-setup-entry-')
        self.addCleanup(temp.cleanup)
        self.project = Path(temp.name) / 'project'
        self.project.mkdir()
        self.state = Path(temp.name) / 'state'
        self.state.mkdir()

    def call(self, action, *options):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = setup_entry.main([action, '--project', str(self.project), *options])
        return code, json.loads(output.getvalue())

    def test_plan_and_prepare_default_to_background_without_model_work(self):
        with mock.patch.object(product, 'plan_qmd_origin',
                               return_value={'status': 'review_required'}) as plan:
            code, row = self.call('plan-index-origin')
        self.assertEqual((code, row['status']), (0, 'review_required'))
        plan.assert_called_once_with(str(self.project), background=True)
        with mock.patch.object(product, 'prepare_owned_qmd_origin',
                               return_value={'status': 'initial_build_registered'}) as prepare:
            code, row = self.call('prepare-index-origin')
        self.assertEqual((code, row['status']), (0, 'initial_build_registered'))
        self.assertIs(prepare.call_args.kwargs['background'], True)

    def test_status_and_rescan_require_same_project_build(self):
        record = {'identity': {'projectRoot': str(self.project.resolve())}}
        with mock.patch.object(product, '_private_parent', return_value=self.state), \
             mock.patch.object(laya_initial_build, 'read', return_value=record), \
             mock.patch.object(product, 'initial_build_status',
                               return_value={'status': 'initial_build'}) as status, \
             mock.patch.object(product, 'request_initial_build_rescan',
                               return_value={'status': 'rescan_requested'}) as rescan:
            status_code, status_row = self.call('index-build-status', '--state-root', str(self.state))
            self.assertEqual(status_row['status'], 'initial_build', status_row)
            self.assertEqual(self.call('index-build-rescan', '--state-root', str(self.state))[1]
                             ['status'], 'rescan_requested')
        status.assert_called_once_with(self.state)
        rescan.assert_called_once_with(self.state)
        with mock.patch.object(product, '_private_parent', return_value=self.state), \
             mock.patch.object(laya_initial_build, 'read',
                               return_value={'identity': {'projectRoot': '/wrong'}}), \
             mock.patch.object(product, 'request_initial_build_rescan') as rescan:
            code, row = self.call('index-build-rescan', '--state-root', str(self.state))
        self.assertEqual((code, row['reason']), (1, 'initial_build_project_mismatch'))
        rescan.assert_not_called()

    def test_finalize_entry_never_activates_route(self):
        with mock.patch.object(product, '_private_parent', return_value=self.state), \
             mock.patch.object(product, 'finalize_initial_build',
                               return_value={'status': 'freshness_pending'}) as finalize, \
             mock.patch.object(product, 'activate_qmd_origin') as activate:
            code, row = self.call('finalize-index-origin', '--state-root', str(self.state))
        self.assertEqual((code, row['status']), (0, 'freshness_pending'))
        finalize.assert_called_once_with(project_root=str(self.project), state_root=self.state)
        activate.assert_not_called()

    def test_restore_entry_requires_explicit_approval_and_reports_outcome(self):
        with mock.patch.object(product, '_private_parent', return_value=self.state), \
             mock.patch.object(product, 'restore_origin_selection') as restore:
            code, row = self.call('restore-index-origin', '--state-root', str(self.state))
            self.assertEqual((code, row['status'], row['reason'], row['changesApplied']),
                             (1, 'rejected', 'index_origin_restore_approval_required', 0))
            restore.assert_not_called()
            for outcome, expected in (('qmd_restored', 0), ('qmd_selected', 0),
                                      ('recovery_required', 1), ('blocked', 1)):
                restore.reset_mock()
                restore.return_value = {'status': outcome}
                code, row = self.call('restore-index-origin', '--state-root', str(self.state),
                                      '--approve-index-origin-restore')
                self.assertEqual((code, row['status']), (expected, outcome))
                restore.assert_called_once_with(project_root=str(self.project),
                                                state_root=self.state)

    def test_retire_entry_requires_approval_and_drift_names_it(self):
        with mock.patch.object(product, '_private_parent', return_value=self.state), \
             mock.patch.object(product, 'retire_drifted_origin',
                               return_value={'status': 'origin_binding_retired'}) as retire:
            code, row = self.call('retire-index-origin', '--state-root', str(self.state))
            self.assertEqual((code, row['reason'], row['changesApplied']),
                             (1, 'index_origin_retire_approval_required', 0))
            retire.assert_not_called()
            code, row = self.call('retire-index-origin', '--state-root', str(self.state),
                                  '--approve-index-origin-retire')
            self.assertEqual((code, row['status']), (0, 'origin_binding_retired'))
            retire.assert_called_once_with(project_root=str(self.project),
                                           state_root=self.state)
        drift = product.InstallBlocked('cutover_source_drift_requires_fresh_generation')
        with mock.patch.object(product, '_private_parent', return_value=self.state), \
             mock.patch.object(product, 'finalize_initial_build', side_effect=drift):
            code, row = self.call('finalize-index-origin', '--state-root', str(self.state))
        self.assertEqual((code, row['status'], row['nextAction']),
                         (1, 'rejected', 'retire-index-origin'))

    def test_activate_and_recover_entries_use_the_syncing_product_paths(self):
        with mock.patch.object(product, '_private_parent', return_value=self.state), \
             mock.patch.object(product, '_read_private', return_value=b'{}'), \
             mock.patch.object(product, '_install_lock',
                               side_effect=lambda _state: contextlib.nullcontext()), \
             mock.patch.object(product, 'activate_qmd_origin') as raw_activate, \
             mock.patch.object(product, 'activate_origin_selection',
                               return_value={'phase': 'active',
                                             'syncWake': {'status': 'queued'}}) as activate, \
             mock.patch.object(product, 'recover_origin_selection',
                               return_value={'phase': 'prepared'}) as recover:
            code, row = self.call('activate-index-origin', '--state-root', str(self.state))
            self.assertEqual((code, row['reason']),
                             (1, 'index_origin_activation_approval_required'))
            activate.assert_not_called()
            code, row = self.call('activate-index-origin', '--state-root', str(self.state),
                                  '--approve-index-origin-activation')
            self.assertEqual((code, row['syncWake']), (0, {'status': 'queued'}))
            self.assertEqual(self.call('recover-index-origin', '--state-root',
                                       str(self.state))[1]['phase'], 'prepared')
        journal = self.state / 'install-journal.sqlite'
        activate.assert_called_once_with(self.project, journal, b'{}')
        recover.assert_called_once_with(self.project, journal, b'{}')
        raw_activate.assert_not_called()


if __name__ == '__main__':
    unittest.main()

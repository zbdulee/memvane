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


if __name__ == '__main__':
    unittest.main()

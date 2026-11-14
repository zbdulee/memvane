"""``setup_entry.py``: CLI arguments to the install use cases, approvals and exit codes.

Read-only actions run anywhere; every writing action first needs the cached
plugin (or the isolated test bypass) and its explicit approval flag. Finalize
gets the app's ``laya-sync`` dispatch check as its port and never activates.
Results map to exit codes (rejected/blocked 1, deferred 75); the registry
update after setup never changes the printed result.
"""
import contextlib
import io
import json
import os
from pathlib import Path
import unittest
from unittest import mock

from memvane.app import setup_entry
from memvane.kernel.resource_deferred import ResourceDeferred
from memvane.install.types import InstallBlocked

PROJECT = '/synthetic/project'
STATE = '/synthetic/state'
INSTALLED = str(Path(setup_entry.__file__).resolve().parents[2])


class SetupEntry(unittest.TestCase):
    def setUp(self):
        bypass = mock.patch.dict(os.environ, {'MEMVANE_TEST_ALLOW_UNINSTALLED_ROOT': '1'})
        bypass.start()
        self.addCleanup(bypass.stop)
        registry = mock.patch('memvane.registry.api.after_setup')
        self.registry = registry.start()
        self.addCleanup(registry.stop)

    def call(self, action, *options, project=PROJECT):
        out = io.StringIO()
        argv = [action] + (['--project', project] if project else []) + list(options)
        with contextlib.redirect_stdout(out):
            code = setup_entry.main(argv)
        return code, json.loads(out.getvalue())

    def install_api(self, name, **kwargs):
        return mock.patch('memvane.install.api.' + name, **kwargs)

    def test_plan_and_prepare_pass_the_reviewed_arguments(self):
        with self.install_api('plan_fresh_wiki_origin', return_value={'status': 'review_required'}) as plan:
            self.assertEqual(self.call('plan-fresh-wiki-origin'), (0, {'status': 'review_required'}))
        plan.assert_called_once_with(PROJECT)
        self.assertEqual(self.call('prepare-fresh-wiki-origin'),
                         (1, {'status': 'rejected', 'reason': 'state_root_required', 'changesApplied': 0}))
        with self.install_api('prepare_fresh_wiki_origin',
                              return_value={'status': 'initial_build_registered'}) as prepare:
            code, row = self.call('prepare-fresh-wiki-origin', '--state-root', STATE, '--install-id', '4' * 32,
                                  '--accepted-origin-sha256', 'a' * 64, '--accepted-provider-sha256', 'b' * 64,
                                  '--provider-dimension', '768')
        self.assertEqual((code, row['status']), (0, 'initial_build_registered'))
        prepare.assert_called_once_with(project_root=PROJECT, plugin_root=Path(INSTALLED), state_root=STATE,
                                        expected_proposal_sha256='a' * 64, accepted_provider_sha256='b' * 64,
                                        install_id='4' * 32, model_path=None, dimension=768)
        for retired in ('plan-index-origin', 'prepare-index-origin', 'detach-qmd-origin', 'retire-index-origin',
                        'cancel-build-wake'):
            with self.subTest(action=retired), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    self.call(retired)

    def test_status_build_and_rescan_actions_name_the_state_root(self):
        cases = (('index-origin-status', 'index_origin_status', ((STATE,), {})),
                 ('index-build-status', 'initial_build_status', ((STATE,), {'project_root': PROJECT})),
                 ('index-build-rescan', 'request_initial_build_rescan', ((STATE,), {'project_root': PROJECT})))
        for action, name, (args, kwargs) in cases:
            with self.subTest(action=action), self.install_api(name, return_value={'status': 'ok'}) as step:
                self.assertEqual(self.call(action, '--state-root', STATE), (0, {'status': 'ok'}))
            step.assert_called_once_with(*args, **kwargs)

    def test_finalize_gets_the_sync_dispatch_check_as_its_port_and_never_activates(self):
        resolved = []
        with self.install_api('finalize_initial_build', return_value={'status': 'initial_build_pending'}) as final, \
                self.install_api('activate_origin_selection') as activate, \
                self.install_api('select_origin') as select, \
                mock.patch('memvane.app.runtime_dispatch.resolve',
                           side_effect=lambda kind, project: resolved.append((kind, project))):
            self.assertEqual(self.call('finalize-index-origin', '--state-root', STATE),
                             (0, {'status': 'initial_build_pending'}))
            kwargs = final.call_args.kwargs
            self.assertEqual((kwargs['project_root'], kwargs['state_root']), (PROJECT, STATE))
            kwargs['resolve_sync_target'](PROJECT)
        self.assertEqual(resolved, [('laya-sync', PROJECT)])
        activate.assert_not_called()
        select.assert_not_called()

    def test_writing_actions_need_their_approval(self):
        cases = (('restore-index-origin', '--approve-index-origin-restore', 'restore_origin_selection',
                  'index_origin_restore_approval_required', {'project_root': PROJECT, 'state_root': STATE}),
                 ('activate-index-origin', '--approve-index-origin-activation', 'select_origin',
                  'index_origin_activation_approval_required',
                  {'project_root': PROJECT, 'state_root': STATE, 'recover': False}))
        for action, flag, name, reason, kwargs in cases:
            with self.subTest(action=action), self.install_api(name, return_value={'status': 'done'}) as step:
                self.assertEqual(self.call(action, '--state-root', STATE),
                                 (1, {'status': 'rejected', 'reason': reason, 'changesApplied': 0}))
                step.assert_not_called()
                self.assertEqual(self.call(action, '--state-root', STATE, flag), (0, {'status': 'done'}))
                step.assert_called_once_with(**kwargs)
        # Recover finishes an approved step: it takes no flag of its own.
        with self.install_api('select_origin', return_value={'status': 'selected'}) as select:
            self.assertEqual(self.call('recover-index-origin', '--state-root', STATE), (0, {'status': 'selected'}))
        select.assert_called_once_with(project_root=PROJECT, state_root=STATE, recover=True)

    def test_writing_actions_need_the_cached_plugin_read_only_ones_do_not(self):
        with mock.patch.dict(os.environ, {'MEMVANE_TEST_ALLOW_UNINSTALLED_ROOT': ''}), \
                self.install_api('plan_fresh_wiki_origin', return_value={'status': 'review_required'}), \
                self.install_api('select_origin', side_effect=AssertionError('ran')):
            self.assertEqual(self.call('plan-fresh-wiki-origin')[0], 0)
            code, row = self.call('recover-index-origin', '--state-root', STATE)
        self.assertEqual((code, row['reason']), (1, 'live_write_requires_plugin_cache'))

    def test_results_map_to_exit_codes(self):
        cases = (({'status': 'restored'}, 0), ({'status': 'released'}, 0), ({'status': 'blocked'}, 1),
                 ({'status': 'recovery_required'}, 1), ({'status': 'partial'}, 1))
        for result, code in cases:
            with self.subTest(result=result), self.install_api('restore_origin_selection', return_value=result):
                self.assertEqual(self.call('restore-index-origin', '--state-root', STATE,
                                           '--approve-index-origin-restore'), (code, result))
        for error, expected in ((InstallBlocked('initial_build_busy'),
                                 (1, {'status': 'rejected', 'reason': 'initial_build_busy', 'changesApplied': 0})),
                                (ResourceDeferred('owner_busy'),
                                 (75, {'status': 'deferred', 'reason': 'owner_busy', 'changesApplied': 0}))):
            with self.subTest(error=error), self.install_api('request_initial_build_rescan', side_effect=error):
                self.assertEqual(self.call('index-build-rescan', '--state-root', STATE), expected)

    def test_the_registry_follows_setup_and_never_changes_its_result(self):
        with self.install_api('select_origin', return_value={'status': 'selected'}):
            self.registry.side_effect = RuntimeError('registry down')
            self.assertEqual(self.call('activate-index-origin', '--state-root', STATE,
                                       '--approve-index-origin-activation'), (0, {'status': 'selected'}))
        self.registry.assert_called_once_with('activate-index-origin', PROJECT, STATE, {'status': 'selected'})

    def test_project_is_required_except_for_runtime_actions(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                self.call('plan-fresh-wiki-origin', project=None)
        with mock.patch('memvane.provider.api.select', return_value={'generation': 'g1',
                                                                    'dependencyClosure': [{'path': 'x'}]}):
            self.assertEqual(self.call('index-runtime-status', project=None),
                             (0, {'status': 'ready', 'generation': 'g1'}))

    def test_onboarding_answers_reach_only_the_notice_choice(self):
        for action, name, args in (('onboarding-skip', 'skip', (PROJECT, 's1')),
                                   ('onboarding-never', 'never', (PROJECT,)),
                                   ('onboarding-resume', 'resume', (PROJECT,)),
                                   ('onboarding-status', 'status', (PROJECT,))):
            with self.subTest(action=action), mock.patch('memvane.host.api.' + name,
                                                         return_value={'status': 'recorded'}) as choice, \
                    self.install_api('select_origin', side_effect=AssertionError('selected')):
                self.assertEqual(self.call(action, '--session', 's1'), (0, {'status': 'recorded'}))
            choice.assert_called_once_with(*args)

    def test_learning_profile_needs_an_explicit_choice_and_namespace(self):
        for options in ((), ('--learning-choice', 'local-staged'), ('--learning-namespace', '/synthetic/ns')):
            with self.subTest(options=options), \
                    mock.patch('memvane.learning.api.propose_learning_profile', side_effect=AssertionError('proposed')):
                self.assertEqual(self.call('plan-learning-profile', *options)[1]['reason'],
                                 'explicit_learning_profile_and_namespace_required')
        with mock.patch('memvane.learning.api.activate_learning_profile', side_effect=AssertionError('activated')):
            self.assertEqual(self.call('activate-learning-profile', '--learning-namespace', '/synthetic/ns')[1]['reason'],
                             'profile_acceptance_required')


if __name__ == '__main__':
    unittest.main()

"""1.0.21 default-teacher enable: one CAS transaction, its fence, recovery and renewal.

The installed-host boundary is RenewalFixture's (synthetic bundles, real
selection, authorization envelopes, setup journal, CAS and entry dispatch).
"""
from copy import deepcopy
import fcntl
import json
import os
from unittest import mock

from memvane.learning import default_enrollment, default_teacher_enable as enable
from memvane.learning import project_learning_setup as setup
from memvane.learning.context_learning import learning_profile, project_scope, shared
from memvane.learning.context_learning import teacher_consent, teacher_state
from memvane.learning.context_learning.inference_identity import file_sha
from memvane.learning.tests.renewal_fixture import RenewalFixture
from memvane.learning.tests.integration import test_default_renewal as renewal_tests

NOW = 1_800_000_000.0
GRANTED = int(NOW * 1000)
FAULTS = ('intent', project_scope.NAME, teacher_consent.NAME, 'shared', setup.NAME,
          learning_profile.NAME, learning_profile.INSTALL)


class Interrupted(Exception):
    pass


class LocalFixture(RenewalFixture):
    """An install enrolled while the teacher was switched off: default-local.

    The switch is removed after enrollment, so maintenance owes the enable.
    """
    teacher_off_switch = True

    def setUp(self):
        super().setUp()
        (self.root / teacher_state.SWITCH_NAME).unlink()
        self.assertTrue(default_enrollment.default_local_only(self.root, self.project, self.policy()))
        self.old = enable._read_all(self.root, self.project)
        self.shared_name = enable._shared(self.project)

    def policy(self):
        return self.read('shared-policy.json')

    def all(self):
        return enable._read_all(self.root, self.project)

    def reset(self):
        for name, data in self.old.items():
            path = self.root / name
            if data is None:
                if path.exists():
                    path.unlink()
            else:
                path.write_bytes(data)
        for name in (enable.INTENT, teacher_state.SWITCH_NAME, teacher_state.STATE_NAME,
                     learning_profile.RENEWAL):
            if (self.root / name).exists():
                (self.root / name).unlink()

    def run_enable(self, *, fault=lambda _: None, version='1.0.15', host=None):
        return enable.run(self.root, self.project, host=host or self.host, host_version=version,
                          install_id='fixture', fault=fault, now=lambda: NOW)

    def maintain(self):
        from memvane.learning.context_learning import selector_heal
        lease = selector_heal._prepare_lease(None)
        try:
            return enable.maintain(self.project, self.install, 'fixture', lease_fd=lease)
        finally:
            os.close(lease)

    def at(self, boundary):
        name = self.shared_name if boundary == 'shared' else boundary
        def fault(reached):
            if reached == name:
                raise Interrupted(reached)
        return fault

    def teacher_events(self):
        from memvane.diagnostics import api as diagnostics
        return [call.kwargs for call in diagnostics.emit.call_args_list
                if call.args and call.args[0] == 'learning.teacher']

    def assert_fenced(self):
        policy = self.policy()
        for consumer in (lambda: project_scope.require(self.root, policy, 'local_train'),
                         lambda: learning_profile.require(self.root, policy, 'local_train'),
                         lambda: learning_profile.selection_enabled(self.root, self.project)):
            with self.assertRaisesRegex(ValueError, 'teacher_enable_pending'):
                consumer()
        # Capture keeps storing: the shared consent is one of the two exact shapes.
        self.assertTrue(shared._consent(self.root, self.project))


class EnableTransaction(LocalFixture):
    def expected_new(self):
        new = enable.target(self.project, self.root, self.old, GRANTED)
        enable.prove_narrow(self.project, self.old, new, GRANTED)
        return new

    def test_maintenance_enables_default_teacher_without_asking(self):
        before = self.all()
        self.maintenance()
        after = self.all()
        self.assertFalse((self.root / enable.INTENT).exists())
        policy = self.policy()
        self.assertEqual(default_enrollment.profile_kind(self.root, self.project, policy), 'default-teacher')
        self.assertFalse(default_enrollment.default_local_only(self.root, self.project, policy))
        for name in enable.PINNED:
            self.assertEqual(before[name], after[name])
        # §5-5 grant/profile: only operations and runtimeApprovalSha256 differ (G-4).
        old_grant = json.loads(before[project_scope.NAME])['proposal']
        grant = json.loads(after[project_scope.NAME])['proposal']
        self.assertEqual(grant['operations'], project_scope.DEFAULT_TEACHER_OPERATIONS)
        self.assertEqual({k: v for k, v in grant.items() if k != 'operations'},
                         {k: v for k, v in old_grant.items() if k != 'operations'})
        for name in (learning_profile.NAME, learning_profile.INSTALL):
            old_row, row = json.loads(before[name]), json.loads(after[name])
            self.assertEqual({k: v for k, v in row['proposal'].items() if k != 'runtimeApprovalSha256'},
                             {k: v for k, v in old_row['proposal'].items() if k != 'runtimeApprovalSha256'})
            self.assertEqual(row['proposal']['runtimeApprovalSha256'], project_scope.proposal_sha256(grant))
        consent = json.loads(after[teacher_consent.NAME])
        self.assertIsNone(before[teacher_consent.NAME])
        self.assertEqual(consent['proposal']['grantedBy'], 'default-enrollment-upgrade')
        self.assertEqual(consent, teacher_consent.envelope(teacher_consent.proposal(
            self.project, granted_by='default-enrollment-upgrade',
            granted_at_unix_ms=consent['proposal']['grantedAtUnixMs'])))
        local = json.loads(after[self.shared_name])
        self.assertIs(local['externalTeacher'], True)
        self.assertEqual({**local, 'externalTeacher': False}, json.loads(before[self.shared_name]))
        # §5-5 journal table.
        old_journal, journal = json.loads(before[setup.NAME]), json.loads(after[setup.NAME])
        plan = journal['plan']
        self.assertEqual(plan['mode'], setup.DEFAULT_TEACHER)
        self.assertEqual(plan['teacherConsent'], consent['proposal'])
        self.assertEqual(plan['localConsent'], local)
        self.assertEqual(journal['acceptedSha256'], setup.proposal_sha256(plan))
        for name in (project_scope.NAME, teacher_consent.NAME, self.shared_name):
            self.assertEqual(journal['written'][name], file_sha(self.root / name))
        self.assertIn(teacher_consent.NAME, journal['configurationPaths'])
        self.assertEqual(journal['profileProposal'], old_journal['profileProposal'])
        for name in (learning_profile.NAME, learning_profile.INSTALL):
            self.assertEqual(journal['written'][name], old_journal['written'][name])
        # Granted operations and consent; the explicit 'gold' stays unavailable.
        self.assertEqual(project_scope.require(self.root, policy, 'gold_default'), grant)
        with self.assertRaisesRegex(ValueError, 'operation_invalid'):
            project_scope.require(self.root, policy, 'gold')
        self.assertTrue(learning_profile.require(self.root, policy, 'local_train'))
        self.assertTrue(learning_profile.selection_enabled(self.root, self.project))
        self.assertIn({'outcome': 'enabled', 'reason': None, 'grantedBy': 'default-enrollment-upgrade'},
                      [{k: e.get(k) for k in ('outcome', 'reason', 'grantedBy')} for e in self.teacher_events()])
        # Idempotent: the next pass writes nothing.
        stamps = {name: (self.root / name).stat().st_ino for name in enable.targets(self.project)}
        self.maintenance()
        self.assertEqual(after, self.all())
        self.assertEqual(stamps, {name: (self.root / name).stat().st_ino for name in enable.targets(self.project)})

    def test_every_write_boundary_resumes_or_restores_without_mixed_state(self):
        new = self.expected_new()
        for boundary in FAULTS:
            for broken in (False, True):
                with self.subTest(boundary=boundary, broken=broken):
                    self.reset()
                    with self.assertRaises(Interrupted):
                        self.run_enable(fault=self.at(boundary))
                    self.assertTrue((self.root / enable.INTENT).is_file())
                    self.assert_fenced()
                    current = self.all()
                    self.assertTrue(all(current[n] in (self.old[n], new[n]) for n in current))
                    if broken:
                        self.assertTrue(teacher_state.write_switch(self.root, False))
                        result = self.run_enable()
                        self.assertEqual(result, {'status': 'rolled_back', 'reason': 'teacher_off'})
                        self.assertEqual(self.all(), self.old)
                        self.assertTrue(default_enrollment.default_local_only(
                            self.root, self.project, self.policy()))
                    else:
                        self.assertEqual(self.run_enable()['status'], 'enabled')
                        self.assertEqual(self.all(), new)
                        self.assertTrue(default_enrollment.default_teacher(
                            self.root, self.project, self.policy()))
                    self.assertFalse((self.root / enable.INTENT).exists())

    def test_interrupted_rollback_finishes_rolling_back(self):
        with self.assertRaises(Interrupted):
            self.run_enable(fault=self.at(setup.NAME))
        self.assertTrue(teacher_state.write_switch(self.root, False))
        with self.assertRaises(Interrupted):
            enable.run(self.root, self.project, host=self.host, host_version='1.0.15', install_id='fixture',
                       fault=lambda name: (_ for _ in ()).throw(Interrupted(name))
                       if name == 'rollback:' + teacher_consent.NAME else None, now=lambda: NOW)
        self.assertEqual(json.loads((self.root / enable.INTENT).read_text())['phase'], 'rolling_back')
        (self.root / teacher_state.SWITCH_NAME).unlink()
        # A rolling-back intent finishes the rollback even with the precondition back.
        self.assertEqual(self.run_enable()['status'], 'rolled_back')
        self.assertEqual(self.all(), self.old)

    def test_lower_copy_never_touches_an_intent(self):
        with self.assertRaises(Interrupted):
            self.run_enable(fault=self.at(teacher_consent.NAME))
        before = self.all()
        self.assertEqual(self.run_enable(version='1.0.14'), {'status': 'deferred', 'reason': 'owner_newer'})
        self.assertEqual(before, self.all())
        self.assertTrue((self.root / enable.INTENT).is_file())


class EnableRefusals(LocalFixture):
    def test_not_enabled_cases_write_nothing(self):
        policy = self.policy()
        self.assertTrue(teacher_state.write_switch(self.root, False))
        self.maintenance()
        self.assertEqual(self.all(), self.old)
        self.assertEqual(self.run_enable(), {'status': 'deferred', 'reason': 'teacher_off'})
        self.reset()
        # A renewal intent defers the enable (and the reverse in RenewalInteraction).
        (self.root / learning_profile.RENEWAL).write_text('{}')
        (self.root / learning_profile.RENEWAL).chmod(0o600)
        self.assertEqual(self.run_enable(), {'status': 'deferred', 'reason': 'renewal_pending'})
        self.reset()
        # A running worker holds the project worker lock.
        fd = os.open(self.root / 'project-worker.lock', os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.run_enable(), {'status': 'deferred', 'reason': 'worker_busy'})
        finally:
            os.close(fd)
        self.assertEqual(self.all(), self.old)
        # A lower host copy than the grant's bound bundle.
        self.assertEqual(self.run_enable(version='1.0.14'), {'status': 'deferred', 'reason': 'owner_newer'})
        self.assertEqual(self.all(), self.old)
        # An explicit (non-default) setup and a synthetic one.
        for mode, expected in (('default', 'explicit'), (setup.SYNTHETIC_STAGED, None)):
            with self.subTest(mode=mode):
                self.reset()
                journal = json.loads(self.old[setup.NAME])
                journal['plan']['mode'] = mode
                journal['acceptedSha256'] = setup.proposal_sha256(journal['plan'])
                self.write(self.root / setup.NAME, journal)
                changed = self.all()
                if expected:
                    self.assertEqual(self.run_enable(), {'status': 'not_applicable', 'kind': expected})
                else:
                    with self.assertRaises(ValueError):
                        self.run_enable()
                self.maintenance()
                self.assertEqual(changed, self.all())
        self.reset()
        self.assertEqual(policy, self.policy())

    def test_running_version_below_journal_maximum_does_not_enable(self):
        with mock.patch('memvane.journal.api.inspect_journal', return_value={'maxPluginVersion': '99.0.0'}):
            self.maintenance()
        self.assertEqual(self.all(), self.old)

    def test_mixed_state_stays_fenced_until_explicit_restore(self):
        from memvane.learning import selector_maintenance
        with self.assertRaises(Interrupted):
            self.run_enable(fault=self.at(teacher_consent.NAME))
        grant = json.loads((self.root / project_scope.NAME).read_text())
        grant['proposal']['cohort']['minTrainCases'] = 1
        self.write(self.root / project_scope.NAME, grant)
        mixed = self.all()
        with self.assertRaisesRegex(ValueError, 'teacher_enable_mixed_state'):
            self.run_enable()
        self.assertEqual(mixed, self.all())
        self.assert_fenced()
        # Maintenance survives and records the refusal; the fence stays.
        self.maintenance()
        self.assertEqual(mixed, self.all())
        self.assertIn('teacher_enable_mixed_state', [e.get('reason') for e in self.teacher_events()
                                                     if e.get('outcome') == 'enable_failed'])
        self.assertTrue(selector_maintenance._teacher_enable_needed(self.decision))

        # The setup command's argument handling is covered with the setup entry itself.
        planned = enable.restore_project(self.project, plan_only=True)
        self.assertEqual(planned['status'], 'restore_planned')
        self.assertEqual(planned['changesApplied'], 0)
        states = {row['target']: row['state'] for row in planned['targets']}
        self.assertEqual(states[project_scope.NAME], 'other')
        self.assertEqual(states[teacher_consent.NAME], 'new')
        self.assertEqual(states[setup.NAME], 'old')
        self.assertEqual(mixed, self.all())
        self.assertTrue((self.root / enable.INTENT).is_file())
        restored = enable.restore_project(self.project)
        self.assertEqual((restored['status'], restored['changesApplied']), ('restored', 2))
        self.assertEqual(self.all(), self.old)
        self.assertFalse((self.root / enable.INTENT).exists())
        self.assertTrue(default_enrollment.default_local_only(self.root, self.project, self.policy()))
        self.assertTrue(project_scope.require(self.root, self.policy(), 'local_train'))
        self.assertIn('enable_restored', [e.get('outcome') for e in self.teacher_events()])
        # No off switch is left: the next maintenance enables again.
        self.assertFalse(teacher_state.switch_off(self.root))
        self.maintenance()
        self.assertTrue(default_enrollment.default_teacher(self.root, self.project, self.policy()))

    def test_same_reason_failures_stop_requests_until_version_changes(self):
        from memvane.learning import selector_maintenance
        planted = teacher_consent.envelope(teacher_consent.proposal(
            self.project, granted_by='default-enrollment-upgrade', granted_at_unix_ms=GRANTED))
        self.write(self.root / teacher_consent.NAME, planted)
        planted_state = self.all()
        with mock.patch('memvane.kernel.memvane_version.plugin_version', return_value='1.0.21'):
            for count in range(1, teacher_state.ENABLE_FAILURE_LIMIT + 1):
                self.assertTrue(selector_maintenance._teacher_enable_needed(self.decision))
                result = self.maintain()
                self.assertEqual(result, {'status': 'failed', 'reason': 'setup_default_scope_changed'})
                self.assertEqual(teacher_state.read_state(self.root)['enable']['failures'], count)
            self.assertFalse(selector_maintenance._teacher_enable_needed(self.decision))
            self.assertEqual(self.maintain(), {'status': 'stopped'})
            failed = [e for e in self.teacher_events() if e.get('outcome') == 'enable_failed']
            self.assertEqual(len(failed), teacher_state.ENABLE_FAILURE_LIMIT)
            self.assertTrue(all(e.get('counted') is True for e in failed))
        with mock.patch('memvane.kernel.memvane_version.plugin_version', return_value='1.0.22'):
            self.assertTrue(selector_maintenance._teacher_enable_needed(self.decision))
        with mock.patch('memvane.kernel.memvane_version.plugin_version', return_value='1.0.21'):
            # The recovery command resets the count too.
            self.assertEqual(enable.restore_project(self.project)['status'], 'no_enable_intent')
            self.assertEqual(teacher_state.read_state(self.root)['enable']['failures'], 0)
            self.assertTrue(selector_maintenance._teacher_enable_needed(self.decision))
        self.assertEqual(planted_state, self.all())

    def test_failing_default_scope_check_does_not_end_maintenance(self):
        planted = teacher_consent.envelope(teacher_consent.proposal(
            self.project, granted_by='default-enrollment', granted_at_unix_ms=GRANTED))
        self.write(self.root / teacher_consent.NAME, planted)
        before = self.all()
        from memvane.learning import selector_maintenance
        with mock.patch('memvane.learning.default_renewal.renew', return_value={'status': 'current'}):
            self.assertIs(selector_maintenance._run(self.project, force=True), True)
        self.assertEqual(before, self.all())
        self.assertIn('setup_default_scope_changed',
                      [e.get('reason') for e in self.teacher_events() if e.get('outcome') == 'enable_failed'])


class SetupRecoveryAfterEnable(LocalFixture):
    def files(self):
        return {**self.all(), 'settings': self.settings.read_bytes()}

    def test_recover_fails_closed_after_enable_and_runs_after_restore(self):
        self.assertEqual(self.run_enable()['status'], 'enabled')
        before = self.files()
        with self.assertRaisesRegex(ValueError, 'setup_backup_changed'):
            setup.recover(self.project, self.root)
        self.assertEqual(before, self.files())

    def test_interrupted_enable_blocks_recover_until_restore(self):
        with self.assertRaises(Interrupted):
            self.run_enable(fault=self.at(setup.NAME))
        before = self.files()
        with self.assertRaisesRegex(ValueError, 'setup_teacher_enable_pending'):
            setup.recover(self.project, self.root)
        self.assertEqual(before, self.files())
        self.assertEqual(enable.restore(self.root, self.project)['status'], 'restored')
        self.assertEqual(self.all(), self.old)
        self.assertEqual(setup.recover(self.project, self.root)['status'], 'recovered')
        self.assertFalse((self.root / project_scope.NAME).exists())


class RenewalInteraction(LocalFixture):
    def test_renewal_then_enable_then_renewal(self):
        self.select(version='1.0.16', marker='B')
        self.assertEqual(self.renew()['status'], 'renewed')
        journal = json.loads((self.root / setup.NAME).read_text())
        self.assertNotEqual(journal['written'][learning_profile.NAME], file_sha(self.root / learning_profile.NAME))
        self.assertEqual(self.run_enable(version='1.0.16')['status'], 'enabled')
        enabled = self.all()
        self.select(version='1.0.17', marker='C')
        self.assertEqual(self.renew()['status'], 'renewed')
        after = self.all()
        before_grant = json.loads(enabled[project_scope.NAME])['proposal']
        grant = json.loads(after[project_scope.NAME])['proposal']
        for key in ('operations', 'cohort', 'expiresAtUnixMs'):
            self.assertEqual(grant[key], before_grant[key])
        self.assertEqual(grant['binding']['installations'][0]['version'], '1.0.17')
        for name in (teacher_consent.NAME, self.shared_name, setup.NAME):
            self.assertEqual(enabled[name], after[name])
        self.assertTrue(default_enrollment.default_teacher(self.root, self.project, self.policy()))
        self.assertTrue(project_scope.require(self.root, self.policy(), 'gold_default'))

    def test_renewal_refuses_consent_planted_or_changed(self):
        self.write(self.root / teacher_consent.NAME, teacher_consent.envelope(teacher_consent.proposal(
            self.project, granted_by='default-enrollment', granted_at_unix_ms=GRANTED)))
        self.select(version='1.0.16', marker='B')
        before = self.all()
        with self.assertRaisesRegex(ValueError, 'scope_changed'):
            self.renew()
        self.assertEqual(before, self.all())
        self.reset()
        self.select(version='1.0.15', marker='A')
        self.assertEqual(self.run_enable()['status'], 'enabled')
        consent = json.loads((self.root / teacher_consent.NAME).read_text())
        consent['proposal']['timeoutSeconds'] = 30
        self.write(self.root / teacher_consent.NAME, teacher_consent.envelope(consent['proposal']))
        self.select(version='1.0.16', marker='B')
        before = self.all()
        with self.assertRaisesRegex(ValueError, 'scope_changed'):
            self.renew()
        self.assertEqual(before, self.all())

    def test_consent_change_during_renewal_is_refused_and_restored(self):
        from memvane.learning import default_renewal as renewal
        self.assertEqual(self.run_enable()['status'], 'enabled')
        enabled = self.all()
        self.select(version='1.0.16', marker='B')
        original = renewal._restore_bytes
        calls = [0]
        def write(path, data, **kwargs):
            original(path, data, **kwargs)
            calls[0] += 1
            if calls[0] == 1:
                local = json.loads((self.root / self.shared_name).read_text())
                self.write(self.root / self.shared_name, {**local, 'externalTeacher': False})
        with mock.patch.object(renewal, '_restore_bytes', side_effect=write):
            with self.assertRaisesRegex(ValueError, 'scope_changed'):
                self.renew()
        after = self.all()
        for name in enable.targets(self.project):
            if name != self.shared_name:
                self.assertEqual(enabled[name], after[name], name)
        self.assertFalse((self.root / learning_profile.RENEWAL).exists())

    def test_enable_intent_holds_renewal(self):
        with self.assertRaises(Interrupted):
            self.run_enable(fault=self.at('intent'))
        self.select(version='1.0.16', marker='B')
        before = self.all()
        self.assertEqual(self.renew(), {'status': 'teacher_enable_pending'})
        self.assertEqual(before, self.all())
        self.assertFalse((self.root / learning_profile.RENEWAL).exists())


class NewEnrollment(RenewalFixture):
    """The fixture enrolls with the teacher on: a new install is default-teacher."""

    def test_new_enrollment_is_default_teacher(self):
        policy = self.read('shared-policy.json')
        self.assertEqual(default_enrollment.profile_kind(self.root, self.project, policy), 'default-teacher')
        consent = teacher_consent.require_consent_v2(self.root, self.project)
        self.assertEqual(consent['grantedBy'], 'default-enrollment')
        journal = self.read(setup.NAME)
        self.assertEqual(consent['grantedAtUnixMs'], journal['plan']['notBeforeUnixMs'])
        grant = project_scope.require(self.root, policy, 'gold_default')
        self.assertEqual(grant['operations'], project_scope.DEFAULT_TEACHER_OPERATIONS)
        self.assertIs(self.read(enable._shared(self.project))['externalTeacher'], True)
        self.assertTrue(shared._consent(self.root, self.project))
        from memvane.learning import selector_maintenance
        self.assertFalse(selector_maintenance._teacher_enable_needed(self.decision))
        # Recovery accepts the default-teacher configuration set and removes the consent.
        self.assertEqual(set(journal['configurationPaths']),
                         set(setup.configuration_paths(self.project, setup.DEFAULT_TEACHER)))
        self.assertEqual(setup.recover(self.project, self.root)['status'], 'recovered')
        self.assertFalse((self.root / teacher_consent.NAME).exists())

    def test_consent_v2_file_check(self):
        good = self.read(teacher_consent.NAME)
        path = self.root / teacher_consent.NAME
        cases = (('teacher_consent_scope', lambda p: p.update(modelPolicy='gpt-6-luna')),
                 ('teacher_consent_scope', lambda p: p.update(extra=1)),
                 ('teacher_consent_scope', lambda p: p.update(passes=2)),
                 ('teacher_consent_scope', lambda p: p.update(enabled=1)),
                 ('teacher_consent_budget', lambda p: p.update(timeoutSeconds=61)),
                 ('teacher_consent_budget', lambda p: p.update(maxInputBytes=1023)))
        for reason, change in cases:
            with self.subTest(reason=reason):
                row = deepcopy(good['proposal']);change(row)
                self.write(path, teacher_consent.envelope(row))
                with self.assertRaisesRegex(ValueError, reason):
                    teacher_consent.require_consent_v2(self.root, self.project)
                with self.assertRaisesRegex(ValueError, reason):
                    project_scope.require(self.root, self.read('shared-policy.json'), 'gold_default')
        self.write(path, {**good, 'acceptedSha256': '0' * 64})
        with self.assertRaisesRegex(ValueError, 'teacher_consent_scope'):
            teacher_consent.require_consent_v2(self.root, self.project)
        path.unlink()
        with self.assertRaisesRegex(ValueError, 'teacher_consent_required'):
            teacher_consent.require_consent_v2(self.root, self.project)
        from memvane.learning.context_learning import general_teacher
        self.write(path, good)
        # The explicit (v1) check refuses the v2 shape by its key set.
        with self.assertRaisesRegex(ValueError, 'teacher_consent_scope'):
            general_teacher.require_consent(self.root, self.project)

    def test_shared_consent_accepts_exactly_two_shapes(self):
        path = self.root / enable._shared(self.project)
        good = self.read(enable._shared(self.project))
        for value, accepted in ((False, True), (True, True), (1, False), (0, False), ('true', False), (None, False)):
            with self.subTest(value=value):
                self.write(path, {**good, 'externalTeacher': value})
                self.assertIs(shared._consent(self.root, self.project), accepted)
        self.write(path, {**good, 'capture': 1})
        self.assertFalse(shared._consent(self.root, self.project))

    def test_unknown_operations_stay_refused(self):
        grant = self.read(project_scope.NAME)
        for operations in (['gold_default', 'local_train'], ['gold', 'gold_default', 'local_train', 'quality_evaluate']):
            with self.subTest(operations=operations):
                grant['proposal']['operations'] = operations
                grant['acceptedSha256'] = project_scope.proposal_sha256(grant['proposal'])
                self.write(self.root / project_scope.NAME, grant)
                with self.assertRaisesRegex(ValueError, 'authorization_invalid'):
                    project_scope._envelope(self.root)


class LocalRenewal121(renewal_tests.DefaultRenewal):
    """G-7: the existing renewal suite on a default-local install (persistent teacher-off switch)."""
    teacher_off_switch = True

    def setUp(self):
        super().setUp()
        self.assertTrue(default_enrollment.default_local_only(self.root, self.project,
                                                              self.read('shared-policy.json')))


class TeacherRenewal121(renewal_tests.DefaultRenewal):
    """The same renewal suite on a default-teacher install: renewal never widens it."""
    teacher_off_switch = False

    def setUp(self):
        super().setUp()
        self.assertTrue(default_enrollment.default_teacher(self.root, self.project,
                                                           self.read('shared-policy.json')))

    def test_real_run_project_entry_passes_renewed_grant_without_teacher(self):
        # Same entry as the local suite; the default-teacher branch drains no cases
        # (the fixture project is outside any git work tree, so no migration wait).
        import io
        from contextlib import redirect_stdout
        self.select()
        self.maintenance()
        out = io.StringIO()
        with mock.patch.object(shared, 'resource_report', return_value={'ready': True}), \
                mock.patch.object(shared, 'run_training_pending',
                                  return_value={'status': 'no_synthetic_cases'}) as cycle, \
                redirect_stdout(out):
            self.assertEqual(shared.main(['run-project', str(self.project)]), 0)
        result = json.loads(out.getvalue())
        self.assertEqual(result['teacher']['status'], 'drained')
        self.assertEqual(result['quota'], {'status': 'not_applicable_default_teacher'})
        self.assertEqual(result['cycle']['status'], 'no_synthetic_cases')
        self.assertEqual(cycle.call_count, 1)

    def test_policy_model_interpreter_manual_optout_and_expiry_are_not_renewed(self):
        super().test_policy_model_interpreter_manual_optout_and_expiry_are_not_renewed()
        # No renewal changes the teacher consent or the shared consent either.
        self.assertTrue(default_enrollment.default_teacher(self.root, self.project,
                                                           self.read('shared-policy.json')))


if __name__ == '__main__':
    import unittest
    unittest.main()

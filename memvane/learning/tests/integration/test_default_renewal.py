"""Synthetic installed-host updates through maintenance and shared entry paths."""
from contextlib import ExitStack, redirect_stdout, redirect_stderr
from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path
import runpy
import sys
import time
from unittest import mock

from tests.support.case import FixtureCase
from memvane.learning import default_enrollment, project_learning_setup as setup
from memvane.learning.context_learning import adapter, dataset, host_inventory
from memvane.learning.context_learning import learning_profile, project_scope, selector_heal, shared
from memvane.learning.context_learning.inference_identity import file_sha
from memvane.learning.context_learning.store import canonical


from memvane.learning.tests.renewal_fixture import RenewalFixture


class DefaultRenewal(RenewalFixture):
    def test_other_marketplace_changes_do_not_invalidate_grant(self):
        plugins=self.home/'.claude/plugins'
        known=json.loads((plugins/'known_marketplaces.json').read_text())
        known['unrelated']={'source':{'source':'github','repo':'other/example'}}
        self.write(plugins/'known_marketplaces.json',known)
        self.assertTrue(project_scope.require(self.root,shared._policy(self.root),'local_train'))

    def test_claude_projection_ignores_other_entries_and_timestamp_but_binds_self_source(self):
        from memvane.learning.context_learning.host_self import selection
        before=selection(self.project,host='claude')
        path=self.home/'.claude/plugins/known_marketplaces.json'
        known=json.loads(path.read_text())
        known['unrelated']={'source':{'source':'git','url':'https://other.example/repo'}}
        known['memvane-marketplace']['lastUpdated']='new timestamp'
        self.write(path,known)
        self.assertEqual(before,selection(self.project,host='claude'))
        known['memvane-marketplace']['source']['repo']='different/source'
        self.write(path,known)
        self.assertNotEqual(before,selection(self.project,host='claude'))

    def test_active_entry_update_preserves_authority_and_is_idempotent(self):
        old_grant=self.read(project_scope.NAME)['proposal']
        old_profile=self.read(learning_profile.NAME)['proposal']
        self.select()
        self.maintenance()
        grant=project_scope.require(self.root,shared._policy(self.root),'local_train')
        profile=learning_profile.require(self.root,shared._policy(self.root),'local_train')
        self.assertEqual({k:v for k,v in grant.items() if k!='binding'},
                         {k:v for k,v in old_grant.items() if k!='binding'})
        allowed={'runtimeApprovalSha256','policySha256','livePolicySha256','trainerArgv','codeSha256'}
        self.assertEqual({k:v for k,v in profile.items() if k not in allowed},
                         {k:v for k,v in old_profile.items() if k not in allowed})
        self.assertEqual(self.settings_before,(self.settings.read_bytes(),self.settings.stat().st_ino))
        self.assertEqual(self.original[setup.NAME],(self.root/setup.NAME).read_bytes())
        before={name:((self.root/name).read_bytes(),(self.root/name).stat().st_ino) for name in self.original}
        self.maintenance()
        self.assertEqual(before,{name:((self.root/name).read_bytes(),(self.root/name).stat().st_ino) for name in before})

    def test_every_publication_boundary_is_fenced_and_resumable(self):
        for boundary in range(1,6):
            with self.subTest(boundary=boundary):
                # Restore the original accepted fixture for this independent crash.
                for name,raw in self.original.items():(self.root/name).write_bytes(raw)
                self.select(version='1.0.15',marker='A')
                self.interrupted(boundary)
                for consumer in (
                    lambda:project_scope.require(self.root,self.read('shared-policy.json'),'local_train'),
                    lambda:learning_profile.require(self.root,self.read('shared-policy.json'),'local_train'),
                    lambda:learning_profile.selection_enabled(self.root,self.project),
                    lambda:selector_heal.check_scope(self.root,self.read('live-selector.json'))):
                    with self.assertRaisesRegex((ValueError,RuntimeError),'renewal_pending'):consumer()
                self.assertEqual(self.renew()['status'],'renewed')
                self.assertFalse((self.root/'project-learning-renewal.json').exists())
                self.assertTrue(learning_profile.require(self.root,shared._policy(self.root),'local_train'))

    def test_removed_old_cache_can_update(self):
        import shutil
        old=self.running
        self.select()
        shutil.rmtree(old)
        self.maintenance()
        self.assertTrue(project_scope.require(self.root,shared._policy(self.root),'local_train'))

    def test_same_version_other_host_does_not_rewrite_approval(self):
        self.select('claude','1.0.15','A')
        self.assertEqual(self.renew()['status'],'current')
        self.maintenance()
        self.assertEqual(self.original,self.snapshot_targets())

    def test_newer_companion_entry_renews_without_scope_expansion(self):
        before=self.read(project_scope.NAME)['proposal']
        self.select('claude','1.0.16','B')
        self.maintenance()
        after=project_scope.require(self.root,shared._policy(self.root),'local_train')
        self.assertEqual(after['binding']['installations'][0]['host'],'claude')
        self.assertEqual({k:v for k,v in before.items() if k!='binding'},
                         {k:v for k,v in after.items() if k!='binding'})

    def test_lower_host_cannot_overwrite_renewed_approval(self):
        self.select()
        self.renew()
        before=self.snapshot_targets()
        self.select('claude','1.0.15','A')
        with self.assertRaisesRegex(ValueError,'owner_newer'):self.renew()
        self.assertEqual(before,self.snapshot_targets())

    def test_unexplained_cas_target_remains_fenced_and_unmodified(self):
        self.interrupted(2)
        path=self.root/'live-selector.json'
        self.write(path,{**self.read('live-selector.json'),'timeoutSeconds':99})
        before=self.snapshot_targets()
        with self.assertRaisesRegex(ValueError,'mixed_state'):self.renew()
        self.assertEqual(before,self.snapshot_targets())
        self.assertTrue((self.root/'project-learning-renewal.json').is_file())

    def test_user_settings_edit_during_interruption_is_preserved(self):
        self.interrupted(2)
        value=json.loads(self.settings.read_text());value['contextLearning']['capture']=False
        self.write(self.settings,value)
        settings=(self.settings.read_bytes(),self.settings.stat().st_ino)
        before=self.snapshot_targets()
        with self.assertRaisesRegex(ValueError,'inputs_changed'):self.renew()
        self.assertEqual(before,self.snapshot_targets())
        self.assertEqual(settings,(self.settings.read_bytes(),self.settings.stat().st_ino))
        self.assertTrue((self.root/'project-learning-renewal.json').is_file())

    def test_newer_target_recovers_interrupted_update(self):
        self.interrupted(2)
        self.select(version='1.0.17',marker='C')
        self.assertEqual(self.renew()['status'],'renewed')
        self.assertEqual(self.read(project_scope.NAME)['proposal']['binding']['installations'][0]['version'],'1.0.17')

    def test_interrupted_target_cannot_change_bytes_at_same_version(self):
        self.interrupted(2)
        self.select(marker='different B bytes')
        before=self.snapshot_targets()
        with self.assertRaisesRegex(ValueError,'same_version_changed'):self.renew()
        self.assertEqual(before,self.snapshot_targets())
        self.assertTrue((self.root/'project-learning-renewal.json').is_file())

    def test_maintenance_entry_resumes_fenced_transaction(self):
        self.interrupted(2)
        self.maintenance()
        self.assertFalse((self.root/'project-learning-renewal.json').exists())
        self.assertTrue(learning_profile.require(self.root,shared._policy(self.root),'local_train'))

    def test_rollback_interruption_is_resumable(self):
        from memvane.learning import default_renewal as renewal
        self.interrupted(2)
        self.select(version='1.0.17',marker='C')
        restore=renewal._restore_bytes
        def interrupted(*args,**kwargs):
            restore(*args,**kwargs)
            raise InterruptedError('synthetic rollback interruption')
        with mock.patch.object(renewal,'_restore_bytes',side_effect=interrupted):
            with self.assertRaises(InterruptedError):self.renew()
        self.assertEqual(self.read('project-learning-renewal.json')['phase'],'rolling_back')
        self.assertEqual(self.renew()['status'],'renewed')
        self.assertTrue(learning_profile.require(self.root,shared._policy(self.root),'local_train'))

    def test_actual_cas_conflict_keeps_fence(self):
        from memvane.learning import default_renewal as renewal
        original=renewal._restore_bytes
        self.select()
        def race(path,raw,**kwargs):
            self.write(path,{'concurrent':'different value'})
            return original(path,raw,**kwargs)
        with mock.patch.object(renewal,'_restore_bytes',side_effect=race):
            with self.assertRaisesRegex(ValueError,'configuration_changed'):self.renew()
        self.assertEqual(self.read('shared-policy.json'),{'concurrent':'different value'})
        self.assertTrue((self.root/'project-learning-renewal.json').is_file())

    def test_real_run_project_entry_passes_renewed_grant_without_teacher(self):
        self.select()
        self.maintenance()
        out=io.StringIO()
        with mock.patch.object(shared,'resource_report',return_value={'ready':True}), \
                mock.patch.object(shared,'run_training_pending',return_value={'status':'no_synthetic_cases'}), \
                redirect_stdout(out):
            self.assertEqual(shared.main(['run-project',str(self.project)]),0)
        result=json.loads(out.getvalue())
        self.assertEqual(result['teacher'],{'status':'disabled_local_only'})
        self.assertEqual(result['cycle']['status'],'no_synthetic_cases')

    def test_expiry_during_recovery_only_restores_old_approval(self):
        self.interrupted(2)
        with mock.patch('time.time',return_value=time.time()+20*365*86400):
            with self.assertRaisesRegex(ValueError,'authority_changed'):self.renew()
        self.assertEqual(self.original,self.snapshot_targets())
        self.assertFalse((self.root/'project-learning-renewal.json').exists())

    def test_prepare_lease_contention_keeps_approval_unchanged(self):
        self.select()
        lease=selector_heal._prepare_lease(None)
        try:
            with self.assertRaisesRegex(selector_heal.HealDeferred,'prepare_busy'):self.renew()
        finally:os.close(lease)
        self.assertEqual(self.original,self.snapshot_targets())

    def test_policy_model_interpreter_manual_optout_and_expiry_are_not_renewed(self):
        for changed in ('policy','model','interpreter','manual','custom','teacher','optout','expired','cohort'):
            with self.subTest(changed=changed):
                for name,raw in self.original.items():(self.root/name).write_bytes(raw)
                self.settings.write_bytes(self.settings_before[0])
                self.select()
                if changed=='policy':
                    value=self.read('shared-policy.json');value['batchMax']+=1
                    self.write(self.root/'shared-policy.json',value)
                elif changed=='model':
                    self.write(self.model/adapter.MODEL_FILES[0],{'changed':True})
                elif changed=='interpreter':self.runtime.write_text('changed runtime')
                elif changed in ('manual','custom','teacher'):
                    value=self.read(setup.NAME)
                    if changed=='teacher':value['plan']['teacherConsent']={'enabled':True}
                    else:value['plan']['mode']=changed
                    value['acceptedSha256']=setup.proposal_sha256(value['plan'])
                    self.write(self.root/setup.NAME,value)
                elif changed=='optout':
                    value=json.loads(self.settings.read_text());value['contextLearning']['capture']=False
                    self.write(self.settings,value)
                elif changed=='cohort':
                    value=self.read(project_scope.NAME);value['proposal']['cohort']['minTrainCases']=1
                    value['acceptedSha256']=project_scope.proposal_sha256(value['proposal'])
                    self.write(self.root/project_scope.NAME,value)
                    accepted=self.read(learning_profile.NAME);accepted['proposal']['runtimeApprovalSha256']=value['acceptedSha256']
                    accepted['acceptedSha256']=learning_profile.digest(accepted['proposal'])
                    self.write(self.root/learning_profile.NAME,accepted)
                    self.write(self.root/learning_profile.INSTALL,{**accepted,'phase':'activated'})
                before=self.snapshot_targets()
                clock=mock.patch('time.time',return_value=time.time()+20*365*86400) if changed=='expired' else ExitStack()
                with clock:
                    with self.assertRaises((ValueError,RuntimeError)):self.renew()
                self.assertEqual(before,self.snapshot_targets())
                self.assertFalse((self.root/'project-learning-renewal.json').exists())
                # Put independent runtime/model inputs back for the next case.
                self.runtime.write_text('synthetic interpreter')
                self.write(self.model/adapter.MODEL_FILES[0],{})
                project_scope._MODEL_CACHE.clear()

    def test_own_source_and_same_version_bytes_changes_are_refused(self):
        self.codex['source']['url']='https://github.com/different/source.git'
        with self.assertRaises(ValueError):project_scope.require(self.root,self.read('shared-policy.json'),'local_train')
        self.select(version='1.0.15',marker='different bytes')
        before=self.snapshot_targets()
        with self.assertRaises(ValueError):self.renew()
        self.assertEqual(before,self.snapshot_targets())


OFFICIAL_CLAUDE={'source':'github','repo':'zbdulee/memvane'}
OFFICIAL_CODEX={'source':'git','url':'https://github.com/zbdulee/memvane.git'}


class CrossHostRenewal:
    """1.0.20: an update renewed by the other host's copy compares sources by
    official repository, since Claude and Codex record it in different shapes."""

    def registry(self):
        return self.read(project_scope.NAME)['proposal']['binding']['hostRegistry']

    def renewed_on(self, host, source):
        other={'codex':'claude','claude':'codex'}[host]
        self.assertEqual(self.registry()['host'],other)
        self.select(host,'1.0.16','B')
        self.assertEqual(self.renew()['status'],'renewed')
        grant=project_scope.require(self.root,shared._policy(self.root),'local_train')
        self.assertEqual(grant['binding']['installations'][0]['host'],host)
        self.assertEqual((grant['binding']['hostRegistry']['host'],
                          grant['binding']['hostRegistry']['marketplace']['source']),(host,source))
        self.assertFalse((self.root/'project-learning-renewal.json').exists())

    def refused(self, host, *, current=None, previous=None):
        """The cross-host source comparison itself; the separate current-source proof
        (``_official``) is bypassed so only this branch can refuse."""
        from memvane.learning import default_renewal_plan as planning
        self.select(host,'1.0.16','B')
        if current is not None:
            if host=='codex':
                self.codex['source']=current
            else:
                path=self.home/'.claude/plugins/known_marketplaces.json'
                known=json.loads(path.read_text())
                known['memvane-marketplace']['source']=current
                self.write(path,known)
        authority=planning._authority
        def recorded(*args):
            grant,accepted,journal=authority(*args)
            if previous is not None:
                grant=deepcopy(grant)
                grant['binding']['hostRegistry']['marketplace']['source']=previous
            return grant,accepted,journal
        before=self.snapshot_targets()
        with mock.patch.object(planning,'_authority',side_effect=recorded), \
                mock.patch.object(selector_heal,'_official',return_value=None), \
                self.assertRaisesRegex(ValueError,'renewal_source_changed'):
            self.renew()
        self.assertEqual(before,self.snapshot_targets())
        self.assertFalse((self.root/'project-learning-renewal.json').exists())


class CodexGrantRenewal(CrossHostRenewal, RenewalFixture):
    initial_host='codex'

    def test_codex_grant_renews_from_the_claude_copy(self):
        self.assertEqual(self.registry()['marketplace']['source'],OFFICIAL_CODEX)
        self.renewed_on('claude',OFFICIAL_CLAUDE)

    def test_unofficial_current_claude_source_is_refused(self):
        self.refused('claude',current={'source':'github','repo':'someone/else'})

    def test_unofficial_recorded_codex_source_is_refused(self):
        self.refused('claude',previous={'source':'git','url':'https://github.com/someone/else.git'})


class ClaudeGrantRenewal(CrossHostRenewal, RenewalFixture):
    initial_host='claude'

    def test_claude_grant_renews_from_the_codex_copy(self):
        self.assertEqual(self.registry()['marketplace']['source'],OFFICIAL_CLAUDE)
        self.renewed_on('codex',OFFICIAL_CODEX)

    def test_unofficial_current_codex_source_is_refused(self):
        self.refused('codex',current={'source':'git','url':'https://github.com/someone/else.git'})

    def test_unofficial_recorded_claude_source_is_refused(self):
        self.refused('codex',previous={'source':'github','repo':'someone/else'})


if __name__=='__main__':
    import unittest
    unittest.main()

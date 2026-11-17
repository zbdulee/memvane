"""Legacy migration needs accepted release evidence, not a cache-shaped path."""
from copy import deepcopy
import shutil
from unittest import mock

from memvane.learning import default_renewal_plan as planning
from memvane.learning.context_learning import learning_profile, project_scope, shared
from memvane.learning.tests.renewal_fixture import RenewalFixture


class LegacyRenewal(RenewalFixture):
    def legacy(self):
        grant=self.read(project_scope.NAME)
        grant['proposal']['binding']['hostRegistry']=project_scope._registry_binding(self.project)
        grant['acceptedSha256']=project_scope.proposal_sha256(grant['proposal'])
        self.write(self.root/project_scope.NAME,grant)
        profile=self.read(learning_profile.NAME)
        profile['proposal']['runtimeApprovalSha256']=grant['acceptedSha256']
        profile['acceptedSha256']=learning_profile.digest(profile['proposal'])
        self.write(self.root/learning_profile.NAME,profile)
        self.write(self.root/learning_profile.INSTALL,{**profile,'phase':'activated'})
        setup=self.read('project-learning-setup.json')
        setup['profileProposal']=profile['proposal']
        setup['written'][learning_profile.NAME]=planning.sha(planning.raw(profile))
        self.write(self.root/'project-learning-setup.json',setup)
        row=grant['proposal']['binding']['installations'][0]
        release={key:row[key] for key in ('version','codeSha256','executableSha256','adapterSha256')}
        release[row['host']]=row['manifestSha256']
        return release

    def test_legacy_release_evidence_migrates_with_or_without_old_cache(self):
        release=self.legacy()
        old=self.running
        self.select()
        shutil.rmtree(old)
        # Synthetic release pin exercises the algorithm. The immutable real
        # 1.0.15 pin is separately checked against the base Git tree.
        with mock.patch.object(planning,'LEGACY_RELEASE',release):
            self.assertEqual(self.renew()['status'],'renewed')
        grant=project_scope.require(self.root,shared._policy(self.root),'local_train')
        self.assertEqual(grant['binding']['hostRegistry']['schema'],'memvane-host-self-v1')

    def test_legacy_exact_cache_bytes_and_release_pin_both_apply(self):
        release=self.legacy()
        self.select()
        with mock.patch.object(planning,'LEGACY_RELEASE',release):
            self.assertEqual(self.renew()['status'],'renewed')

    def test_legacy_path_without_release_evidence_is_rejected_without_writes(self):
        release=self.legacy()
        self.select()
        release['codeSha256']='0'*64
        before=self.snapshot_targets()
        with mock.patch.object(planning,'LEGACY_RELEASE',release):
            with self.assertRaisesRegex(ValueError,'legacy_provenance_unproved'):self.renew()
        self.assertEqual(before,self.snapshot_targets())

    def test_legacy_same_version_different_bytes_is_refused(self):
        release=self.legacy()
        old=self.running
        shutil.rmtree(old)
        self.select(version='1.0.15',marker='changed')
        # A retained cache at the old path has to match its original fingerprint.
        before=self.snapshot_targets()
        with mock.patch.object(planning,'LEGACY_RELEASE',release):
            with self.assertRaisesRegex(ValueError,'previous_bundle_changed'):self.renew()
        self.assertEqual(before,self.snapshot_targets())

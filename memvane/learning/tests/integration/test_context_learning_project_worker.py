import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from memvane.learning.context_learning import project_worker


class ProjectWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory(prefix='qmd-project-worker-')
        self.root=Path(self.temporary.name).resolve()
        self.project=self.root/'project';self.project.mkdir()
        self.state=self.root/'state';self.state.mkdir(mode=0o700)
        self.cfg={'contextLearning':{'capture':True,'autoCycle':True},
                  'sharedLearning':{'enabled':True,'root':str(self.state)}}
        self.events=[]
    def tearDown(self):self.temporary.cleanup()
    def execute(self,*,gold_status='drained',ready=True):
        def gold(*args,**kwargs):
            self.events.append('gold');return {'status':gold_status}
        def train(*args,**kwargs):
            self.events.append('train');return {'status':'waiting_fresh_train','train':0,'required':5}
        with patch('memvane.learning.context_learning.shared._safe_root',return_value=self.state), \
             patch('memvane.learning.context_learning.shared._policy',return_value={'enabled':True}), \
             patch('memvane.learning.context_learning.shared.resource_report',return_value={'ready':ready}), \
             patch('memvane.learning.context_learning.project_scope.require',return_value={'projectRoot':str(self.project)}), \
             patch('memvane.learning.context_learning.general_teacher.process_pending',side_effect=gold), \
             patch('memvane.learning.context_learning.general_teacher.read_weekly_quota',return_value={'remainingPercent':80}), \
             patch('memvane.learning.context_learning.shared.run_training_pending',side_effect=train):
            return project_worker.run(self.state,self.project,self.cfg)
    def test_capture_and_auto_cycle_are_independent_stop_gates(self):
        for flag in ('capture','autoCycle'):
            cfg=copy.deepcopy(self.cfg);cfg['contextLearning'][flag]=False
            result=project_worker.run(self.state,self.project,cfg)
            self.assertEqual(result['status'],'disabled')
            self.assertEqual(self.events,[])
    def test_resource_pressure_starts_neither_teacher_nor_trainer(self):
        self.assertEqual(self.execute(ready=False)['status'],'deferred_resource')
        self.assertEqual(self.events,[])
    def test_default_local_cycle_never_calls_teacher_or_quota(self):
        with patch('memvane.learning.default_enrollment.default_local_only', return_value=True):
            answer=self.execute()
        self.assertEqual(self.events,['train'])
        self.assertEqual(answer['teacher']['status'],'disabled_local_only')
    def test_inbox_cannot_start_models_during_setup_transaction(self):
        from memvane.learning.context_learning.local_cycle import _atomic_json
        _atomic_json(self.state/'project-learning-setup.json',{'phase':'installing'})
        self.assertEqual(self.execute()['status'],'setup_in_progress')
        self.assertEqual(self.events,[])
    def test_weekly_stop_prevents_new_training_too(self):
        self.assertEqual(self.execute(gold_status='deferred_quota')['status'],'deferred_quota')
        self.assertEqual(self.events,['gold'])
    def test_existing_gold_cannot_train_when_quota_read_fails(self):
        from memvane.learning.context_learning.general_teacher import read_weekly_quota
        with patch('memvane.learning.context_learning.general_teacher.read_weekly_quota',side_effect=ValueError('weekly_quota_checkpoint')):
            # execute patches the default successful probe, so invoke the
            # worker with the remaining dependencies at their real boundary.
            from memvane.learning.context_learning import shared,project_scope,general_teacher
            with patch.object(shared,'_safe_root',return_value=self.state), \
                 patch.object(shared,'_policy',return_value={}), \
                 patch.object(shared,'resource_report',return_value={'ready':True}), \
                 patch.object(project_scope,'require',return_value={}), \
                 patch.object(general_teacher,'process_pending',return_value={'status':'drained'}), \
                 patch.object(shared,'run_training_pending',return_value={'status':'waiting_fresh_train'}) as train:
                answer=project_worker.run(self.state,self.project,self.cfg)
                self.assertEqual(answer['status'],'deferred_quota');train.assert_not_called()
    def test_small_cohort_reports_training_separately_from_publication(self):
        answer=self.execute()
        self.assertEqual(self.events,['gold','train'])
        self.assertEqual(answer['cycle']['status'],'waiting_fresh_train')
        self.assertEqual(answer['teacher']['status'],'drained')
        self.assertFalse(answer['activeCheckpointChanged'])
    def test_general_policy_resolves_other_verified_installed_host_adapter(self):
        from memvane.learning.context_learning import shared
        from memvane.learning.context_learning.local_cycle import _atomic_json
        model=self.root/'model';model.mkdir()
        _atomic_json(model/'rl_agent_config.json',{'encoder':'jhu-clsp/mmBERT-base','max_len':1024,'head_max_len':256})
        other=self.root/'other-installed-adapter.py';other.write_text('approved peer package')
        policy={'schema':'qmd-shared-learning-v1','enabled':True,'goldModel':'gpt-6-luna',
            'maxGoldCalls':20,'batchMin':5,'batchMax':128,'targetExact':.95,
            'trainerArgv':['/usr/bin/python3',str(other),str(model)],'baseArtifact':str(self.root/'base'),
            'maxRssMib':4096,'minAvailableMib':1024,'maxLoadPerCpu':.8,'safetySeconds':120,
            'experimental':True,'modelInputView':'tool-edit-8192-choice-v1'}
        _atomic_json(self.state/'shared-policy.json',policy)
        with patch('memvane.learning.context_learning.project_scope.present',return_value=True), \
             patch('memvane.learning.context_learning.runtime_authorization.active',return_value={'scope':'single-project-local-learning'}):
            resolved=shared._policy(self.state)
        self.assertEqual(resolved['trainerArgv'][1],str(Path(shared.__file__).with_name('adapter.py')))
        self.assertTrue(resolved['_runtime_authorization_v2'])
    def test_training_only_retry_rechecks_weekly_quota(self):
        from memvane.learning.context_learning import shared,project_scope,general_teacher
        with patch('memvane.settings.api.find_project_config',return_value={'config':self.cfg}), \
             patch.object(shared,'_safe_root',return_value=self.state), \
             patch.object(shared,'_policy',return_value={'_runtime_authorization_v2':True}), \
             patch.object(project_scope,'present',return_value=True), \
             patch.object(general_teacher,'read_weekly_quota',side_effect=ValueError('weekly_quota_checkpoint')):
            result=shared.run_training_pending(self.state,self.project)
        self.assertEqual(result['status'],'deferred_quota')

    def test_a_damaged_base_model_that_is_the_active_file_ends_the_pass_held_with_its_row(self):
        # 1.1.x: the grant binds the base file's bytes, so the scope check raised and the pass
        # ended with no stored result and no event.
        import hashlib
        from memvane.learning.context_learning import shared,project_scope,general_teacher
        from memvane.learning.context_learning.candidate_apply import INCUMBENT_DAMAGED
        from memvane.learning.context_learning.local_cycle import _atomic_json
        base=self.state/'base.artifact';base.write_bytes(b'base');base.chmod(0o600)
        candidate=self.state/'candidate.artifact';candidate.write_bytes(b'candidate');candidate.chmod(0o600)
        policy={'enabled':True,'baseArtifact':str(base)}
        def run(active):
            active.write_bytes(b'as installed')
            _atomic_json(self.state/'active-checkpoint.json',{'schema_version':1,'artifact_path':str(active),
                'artifact_sha256':hashlib.sha256(active.read_bytes()).hexdigest(),'cycle_id':'base'})
            active.write_bytes(b'damaged')
            emitted=[]
            with patch.object(shared,'_safe_root',return_value=self.state), \
                 patch.object(shared,'_policy',return_value=policy), \
                 patch.object(shared,'resource_report',return_value={'ready':True}), \
                 patch.object(project_scope,'require',side_effect=ValueError('project_scope_binding_changed')), \
                 patch.object(general_teacher,'process_pending',side_effect=AssertionError('teacher')), \
                 patch.object(shared,'run_training_pending',side_effect=AssertionError('cycle')), \
                 patch('memvane.journal.api.select_route',return_value={}), \
                 patch('memvane.diagnostics.api.emit',side_effect=lambda event,**f:emitted.append((event,f))):
                return project_worker.run(self.state,self.project,self.cfg),emitted
        for _ in range(2):
            answer,emitted=run(base)
            self.assertEqual(answer,{'status':'incumbent_damaged','cycle':{'status':'incumbent_damaged',
                'reason':INCUMBENT_DAMAGED},'activeCheckpointChanged':False})
            self.assertEqual(json.loads((self.state/'project-last-status.json').read_text()),answer)
        # One held row, from the pass that first ended so.
        answer,emitted=run(base)
        self.assertEqual(emitted,[])
        (self.state/'project-last-status.json').unlink()
        answer,emitted=run(base)
        self.assertEqual([(e,f['outcome'],f['reason'],f['detail']) for e,f in emitted],
                         [('learning.cycle','held','incumbent_damaged',INCUMBENT_DAMAGED)])
        # Another refusal of the grant, or a damaged file that is not the active one, still raises.
        with self.assertRaisesRegex(ValueError,'project_scope_binding_changed'):
            run(candidate)


if __name__=='__main__':unittest.main()

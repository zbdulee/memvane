import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,'core')
from context_learning import project_worker


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
        with patch('context_learning.shared._safe_root',return_value=self.state), \
             patch('context_learning.shared._policy',return_value={'enabled':True}), \
             patch('context_learning.shared.resource_report',return_value={'ready':ready}), \
             patch('context_learning.project_scope.require',return_value={'projectRoot':str(self.project)}), \
             patch('context_learning.general_teacher.process_pending',side_effect=gold), \
             patch('context_learning.general_teacher.read_weekly_quota',return_value={'remainingPercent':80}), \
             patch('context_learning.shared.run_training_pending',side_effect=train):
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
    def test_inbox_cannot_start_models_during_setup_transaction(self):
        from context_learning.local_cycle import _atomic_json
        _atomic_json(self.state/'project-learning-setup.json',{'phase':'installing'})
        self.assertEqual(self.execute()['status'],'setup_in_progress')
        self.assertEqual(self.events,[])
    def test_weekly_stop_prevents_new_training_too(self):
        self.assertEqual(self.execute(gold_status='deferred_quota')['status'],'deferred_quota')
        self.assertEqual(self.events,['gold'])
    def test_existing_gold_cannot_train_when_quota_read_fails(self):
        from context_learning.general_teacher import read_weekly_quota
        with patch('context_learning.general_teacher.read_weekly_quota',side_effect=ValueError('weekly_quota_checkpoint')):
            # execute patches the default successful probe, so invoke the
            # worker with the remaining dependencies at their real boundary.
            from context_learning import shared,project_scope,general_teacher
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
        from context_learning import shared
        from context_learning.local_cycle import _atomic_json
        model=self.root/'model';model.mkdir()
        _atomic_json(model/'rl_agent_config.json',{'encoder':'jhu-clsp/mmBERT-base','max_len':1024,'head_max_len':256})
        other=self.root/'other-installed-adapter.py';other.write_text('approved peer package')
        policy={'schema':'qmd-shared-learning-v1','enabled':True,'goldModel':'gpt-6-luna',
            'maxGoldCalls':20,'batchMin':5,'batchMax':128,'targetExact':.95,
            'trainerArgv':['/usr/bin/python3',str(other),str(model)],'baseArtifact':str(self.root/'base'),
            'maxRssMib':4096,'minAvailableMib':1024,'maxLoadPerCpu':.8,'safetySeconds':120,
            'experimental':True,'modelInputView':'tool-edit-8192-choice-v1'}
        _atomic_json(self.state/'shared-policy.json',policy)
        with patch('context_learning.project_scope.present',return_value=True), \
             patch('context_learning.runtime_authorization.active',return_value={'scope':'single-project-local-learning'}):
            resolved=shared._policy(self.state)
        self.assertEqual(resolved['trainerArgv'][1],str(Path(shared.__file__).with_name('laya_adapter.py')))
        self.assertTrue(resolved['_runtime_authorization_v2'])
    def test_training_only_retry_rechecks_weekly_quota(self):
        from context_learning import shared,project_scope,general_teacher
        with patch('config.find_project_config',return_value={'config':self.cfg}), \
             patch.object(shared,'_safe_root',return_value=self.state), \
             patch.object(shared,'_policy',return_value={'_runtime_authorization_v2':True}), \
             patch.object(project_scope,'present',return_value=True), \
             patch.object(general_teacher,'read_weekly_quota',side_effect=ValueError('weekly_quota_checkpoint')):
            result=shared.run_training_pending(self.state,self.project)
        self.assertEqual(result['status'],'deferred_quota')


if __name__=='__main__':unittest.main()

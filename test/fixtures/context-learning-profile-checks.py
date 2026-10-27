"""Private synthetic installation profile; never uses operating settings."""
from pathlib import Path
import json,tempfile,unittest,time
from unittest.mock import patch
from context_learning import runtime_authorization,laya_adapter
try:from context_learning import learning_profile as P
except ImportError:P=None

class Checks(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  self.project=Path(self.tmp.name).resolve();self.project.chmod(0o700)
  (self.project/'.auto-context').mkdir(mode=0o700)
  self.root=self.project/'private';self.root.mkdir(mode=0o700)
  self.settings=self.project/'.auto-context/settings.json'
  self.raw={'name':'synthetic','collections':['wiki'],'collectionPaths':{'wiki':'wiki'},'indexing':True}
  self.policy={'trainerArgv':['/fake/python','/fake/adapter',str(self.root/'model')], 'modelInputView':laya_adapter.EDIT_INPUT_VIEW,'_runtime_authorization_v2':True,'batchMin':5,'batchMax':120}
  self.grant={'projectRoot':str(self.project),'cohort':{'id':'fixture','minQualityTrainCases':100},'operations':['local_train','quality_evaluate']}
  for name,row in [('shared-policy.json',self.policy),('live-selector.json',{'fixture':'live'})]:self.write(self.root/name,row)
  self.write(self.settings,self.raw)
  self.expiry=int(time.time()*1000)+3600000
  self.addCleanup(patch.stopall)
  patch('context_learning.shared._policy',return_value=self.policy).start()
  self.permission=patch.object(runtime_authorization,'require_operation',return_value=self.grant).start()
  patch.object(laya_adapter,'model_identity',return_value='a'*64).start()
 def write(self,path,row):path.write_text(json.dumps(row));path.chmod(0o600)
 def plan(self,**kw):return P.proposal(self.project,self.root,choice='local-auto-apply',mode=kw.pop('mode','new'),expires_at_unix_ms=self.expiry,**kw)
 def test_recommendation_is_read_only_and_explicit_default_is_local_only(self):
  self.assertIsNotNone(P,'learning profile missing')
  before=self.settings.read_bytes();offer=P.recommendation(self.raw)
  self.assertEqual(offer['recommended'],'local-auto-apply');self.assertEqual(before,self.settings.read_bytes())
  plan=self.plan();cfg=plan['settings'];self.assertFalse(cfg['contextLearning']['capture'])
  self.assertTrue(cfg['contextLearning']['autoCycle']);self.assertTrue(plan['autoApply'])
  self.assertNotIn('gold',plan['operations']);self.assertEqual(plan['projectRoot'],str(self.project))
 def test_preserving_migration_never_reverses_false_or_grants_capture(self):
  self.assertIsNotNone(P,'learning profile missing')
  raw={**self.raw,'contextLearning':{'capture':False,'autoCycle':False,'liveSelection':False},'sharedLearning':{'enabled':False,'autoApply':False}}
  self.write(self.settings,raw)
  plan=self.plan(mode='preserve');self.assertFalse(plan['localTrain']);self.assertFalse(plan['autoApply'])
  for key in ('capture','autoCycle'):self.assertFalse(plan['settings']['contextLearning'][key])
  self.assertNotIn('liveSelection',plan['settings']['contextLearning'])
  self.assertFalse(plan['settings']['sharedLearning']['enabled'])
 def test_old_signed_binding_ignores_only_removed_selection_key(self):
  old={**self.raw,'contextLearning':{'capture':False,'autoCycle':True,'liveSelection':False},
       'sharedLearning':{'enabled':True}}
  legacy={key:old.get(key) for key in ('contextLearning','sharedLearning','indexing','collectionPaths')}
  row={'settings':old,'settingsBindingSha256':P.digest(legacy)}
  current={**old,'contextLearning':{'capture':False,'autoCycle':True}}
  self.assertTrue(P._binding_matches(row,current))
  self.assertFalse(P._binding_matches(row,{**current,'indexing':False}))
 def test_exact_acceptance_required_and_revocation_model_change_disable(self):
  self.assertIsNotNone(P,'learning profile missing')
  plan=self.plan();key=P.digest(plan)
  with self.assertRaisesRegex(ValueError,'profile_acceptance'):P.activate(self.project,self.root,accepted_sha256='0'*64,choice='local-auto-apply',mode='new',expires_at_unix_ms=self.expiry)
  self.assertFalse((self.root/P.NAME).exists())
  P.activate(self.project,self.root,accepted_sha256=key,choice='local-auto-apply',mode='new',expires_at_unix_ms=self.expiry)
  self.assertTrue(P.require(self.root,self.policy,'apply')['autoApply'])
  with patch.object(laya_adapter,'model_identity',return_value='b'*64):
   with self.assertRaises(ValueError):P.require(self.root,self.policy,'apply')
  self.permission.side_effect=ValueError('revoked')
  with self.assertRaises(ValueError):P.require(self.root,self.policy,'apply')
 def test_crash_between_settings_and_profile_recovers_same_acceptance(self):
  self.assertIsNotNone(P,'learning profile missing')
  plan=self.plan();key=P.digest(plan)
  def crash(phase):
   if phase=='settings_written':raise RuntimeError('fixture crash')
  with self.assertRaises(RuntimeError):P.activate(self.project,self.root,accepted_sha256=key,choice='local-auto-apply',mode='new',expires_at_unix_ms=self.expiry,fault=crash)
  P.activate(self.project,self.root,accepted_sha256=key,choice='local-auto-apply',mode='new',expires_at_unix_ms=self.expiry)
  self.assertTrue(P.require(self.root,self.policy,'apply')['autoApply'])
 def test_setup_entry_explicit_plan_activate_and_normalized_marker(self):
  self.assertIsNotNone(P,'learning profile missing')
  import setup_entry,config,io
  from contextlib import redirect_stdout
  args=['--project',str(self.project),'--learning-namespace',str(self.root),'--learning-choice','local-auto-apply','--learning-migration','new','--learning-expires-at-unix-ms',str(self.expiry)]
  out=io.StringIO()
  with redirect_stdout(out):status=setup_entry.main(['plan-learning-profile',*args])
  self.assertEqual(status,0);plan=json.loads(out.getvalue());self.assertEqual(plan['status'],'learning_profile_proposed')
  out=io.StringIO()
  with redirect_stdout(out):status=setup_entry.main(['activate-learning-profile',*args,'--accepted-learning-profile',plan['proposalSha256']])
  self.assertEqual(status,0)
  normalized=config.normalize_config(json.loads(self.settings.read_text()))
  self.assertEqual(normalized['sharedLearning']['profile'],P.SCHEMA)
  self.assertTrue(normalized['sharedLearning']['autoApply'])
  self.assertTrue(hasattr(P,'selection_root'),'approved selection route missing')
  self.assertEqual(P.selection_root(normalized,self.project),self.root)
 def test_off_indexing_false_and_expiry_disable_execution_and_selection(self):
  self.assertIsNotNone(P,'learning profile missing')
  raw={**self.raw,'contextLearning':{'autoCycle':True,'liveSelection':True},'sharedLearning':{'enabled':True}}
  self.write(self.settings,raw)
  off=P.proposal(self.project,self.root,choice='off',mode='preserve',expires_at_unix_ms=self.expiry)
  self.assertFalse(off['settings']['contextLearning']['autoCycle'])
  self.assertFalse(off['settings']['sharedLearning']['enabled'])
  self.write(self.settings,{**self.raw,'indexing':False})
  disabled=self.plan();self.assertFalse(disabled['localTrain']);self.assertFalse(disabled['autoApply'])
  self.write(self.settings,self.raw);plan=self.plan()
  P.activate(self.project,self.root,accepted_sha256=P.digest(plan),choice='local-auto-apply',mode='new',expires_at_unix_ms=self.expiry)
  with patch.object(P.time,'time',return_value=self.expiry/1000+1):
   with self.assertRaises(ValueError):P.selection_enabled(self.root,self.project)
 def test_missing_profile_with_install_intent_cannot_downgrade(self):
  self.assertIsNotNone(P,'learning profile missing')
  self.write(self.root/P.INSTALL,{'fixture':'interrupted'})
  self.assertTrue(P.present(self.root))
 def test_selected_profile_uses_only_local_worker_and_disables_legacy_autocycle(self):
  self.assertIsNotNone(P,'learning profile missing')
  import config,context_learning_auto as auto
  from context_learning import shared
  plan=self.plan();P.activate(self.project,self.root,accepted_sha256=P.digest(plan),choice='local-auto-apply',mode='new',expires_at_unix_ms=self.expiry)
  cfg=config.normalize_config(json.loads(self.settings.read_text()))
  old_policy={'schema':'qmd-auto-cycle-policy-v1','versionId':'legacy','revisionsJson':'/fake/revisions','trainerArgvJson':'/fake/argv','incumbentArtifact':'/fake/artifact','maxSeconds':60,'maxRssMib':512}
  self.write(self.root/auto.POLICY,old_policy)
  found={'config':cfg,'configFormat':'auto-context-dir','projectRoot':str(self.project)}
  with patch.object(config,'find_project_config',return_value=found),patch.object(auto,'_private_state',return_value=self.root):
   self.assertIsNone(auto._policy(self.project))
  with patch.object(config,'find_project_config',return_value=found),patch.object(shared,'_safe_root',return_value=self.root),patch.object(shared,'train_due',return_value={'status':'waiting_fresh_batch'}) as train,patch.object(shared,'_run_impl',side_effect=AssertionError('teacher path entered')),patch.object(shared,'sync_reviewed',side_effect=AssertionError('historical sync entered')):
   answer=shared.run(self.root,project=self.project,sync_config=cfg)
   self.assertEqual(answer['status'],'waiting_fresh_batch');train.assert_called_once()
 def test_training_disabled_profile_can_resume_existing_quality_only_job(self):
  import config
  from context_learning import shared,automatic_staging
  from context_learning.evaluation_queue import EvaluationQueue
  self.write(self.settings,{**self.raw,'contextLearning':{'autoCycle':False}})
  plan=self.plan(mode='preserve');self.assertFalse(plan['localTrain'])
  P.activate(self.project,self.root,accepted_sha256=P.digest(plan),choice='local-auto-apply',mode='preserve',expires_at_unix_ms=self.expiry)
  self.write(self.root/shared.DATABASE_NAME,{'fixture':'only existence check; evaluation queue is injected'})
  cfg=config.normalize_config(json.loads(self.settings.read_text()))
  with patch.object(config,'find_project_config',return_value={'config':cfg}),patch.object(shared,'_safe_root',return_value=self.root),patch.object(EvaluationQueue,'pending',return_value=[{'cycle_id':'existing'}]),patch.object(automatic_staging,'evaluate_pending',return_value={'status':'staged_evaluated'}) as evaluate,patch.object(shared,'train_due',side_effect=AssertionError('training forbidden')):
   self.assertEqual(shared.run_training_pending(self.root,self.project)['status'],'staged_evaluated');evaluate.assert_called_once()
 def test_incomplete_apply_resumes_before_new_training_or_evaluation(self):
  import config
  from context_learning import shared,candidate_apply
  plan=self.plan();P.activate(self.project,self.root,accepted_sha256=P.digest(plan),choice='local-auto-apply',mode='new',expires_at_unix_ms=self.expiry)
  cfg=config.normalize_config(json.loads(self.settings.read_text()))
  self.write(self.root/candidate_apply.JOURNAL,{'schema':candidate_apply.SCHEMA,'apply_id':'a'*32,'phase':'pointer_committed','cycle_id':'pending-cycle'})
  with patch.object(config,'find_project_config',return_value={'config':cfg}),patch.object(shared,'_safe_root',return_value=self.root),patch.object(shared,'train_due',side_effect=AssertionError('new training skipped pending apply')),patch.object(candidate_apply,'apply_candidate',return_value={'status':'apply_deferred'}) as recover:
   self.assertEqual(shared.run_training_pending(self.root,self.project)['status'],'apply_deferred');recover.assert_called_once_with(self.root,self.policy,'pending-cycle')
 def test_auto_apply_profile_waits_for_quality_sized_fresh_batch_within_budget(self):
  self.assertEqual(self.plan()['minFreshTrainCases'],100)
  self.policy['batchMax']=20
  with self.assertRaisesRegex(ValueError,'profile_quality_batch_budget_required'):self.plan()

if __name__=='__main__':unittest.main()

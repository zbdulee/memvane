"""Explicit second-project enrollment; all state lives in a disposable HOME."""
import hashlib,json,runpy,sys,unittest,tempfile
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,'core')
C=runpy.run_path('test/fixtures/shared-qmd-setup-checks.py')['SharedSetup']

class Enrollment(C):
 def setUp(self):
  area=tempfile.TemporaryDirectory(prefix='qmd-enrollment-fixture-',dir=Path.cwd().parent)
  self.addCleanup(area.cleanup)
  with patch('tempfile.tempdir',area.name):super().setUp()
  self.assertEqual(self.cli('prepare-store')[0],0)
  with patch('shared_qmd_setup.coordinator._daemon_identity',return_value=None):
   self.assertEqual(self.cli('activate-store',['--approve-store-activation','--allow-live-host-sessions'])[0],0)
  self.other=self.home/'second';(self.other/'.auto-context/wiki').mkdir(parents=True)
  (self.other/'docs').mkdir()
  (self.other/'.auto-context/settings.json').write_text(json.dumps({'indexing':True,'collections':['second-docs','second-wiki'],'collectionPaths':{'second-docs':'docs','second-wiki':'.auto-context/wiki'},'collectionRoles':{'second-docs':'raw','second-wiki':'wiki'}}))
 def enroll(self,approved=True):
  import contextlib,io,setup_entry
  out=io.StringIO()
  with contextlib.redirect_stdout(out):
   try:code=setup_entry.main(['enroll-store','--project',str(self.other),*(['--approve-project-enrollment','--approve-index-execution'] if approved else [])])
   except SystemExit:return 1,{'reason':'enrollment_entrypoint_missing'}
  return code,json.loads(out.getvalue())
 def test_approval_required_and_gate_stays_closed(self):
  import setup_guard
  code,r=self.enroll(False);self.assertEqual(code,1);self.assertEqual(r['reason'],'project_enrollment_review_required')
  self.assertEqual(setup_guard.status(self.other)['reason'],'project_shared_migration_not_approved')
 def test_enrollment_is_idempotent_without_changing_store_or_first_project(self):
  import qmd_shared_store as S,setup_guard,dirty_queue
  g=Path(S.select()['generation']);before={n:hashlib.sha256((g/n).read_bytes()).hexdigest() for n in ['index.sqlite','prepared.json','scope-policy.json']}
  code,r=self.enroll();self.assertEqual(code,0,r);self.assertEqual(r['status'],'enrolled')
  self.assertEqual(setup_guard.status(self.other)['status'],'ready');self.assertEqual(setup_guard.status(self.project)['status'],'ready')
  self.assertEqual({n:hashlib.sha256((g/n).read_bytes()).hexdigest() for n in before},before)
  queue=dirty_queue.queue_path().read_bytes();self.assertIn(b'second-wiki',queue)
  receipt=(g/'enrolled-projects.json').read_bytes()
  code,r=self.enroll();self.assertEqual(code,0,r);self.assertEqual(r['status'],'enrolled_unchanged')
  self.assertEqual((g/'enrolled-projects.json').read_bytes(),receipt);self.assertEqual(dirty_queue.queue_path().read_bytes(),queue)
 def test_collision_rejected_before_gate_opens(self):
  import qmd_shared_store as S,setup_guard
  (self.project/'docs').mkdir()
  S.register_config(self.project,{'indexing':True,'collections':['second-docs'],'collectionPaths':{'second-docs':'docs'},'collectionRoles':{'second-docs':'raw'}})
  code,r=self.enroll();self.assertEqual(code,1,r);self.assertEqual(r['reason'],'plugin_qmd_collection_collision')
  self.assertEqual(setup_guard.status(self.other)['reason'],'project_shared_migration_not_approved')
 def test_process_denial_prevents_approval_receipt(self):
  import qmd_shared_store as S
  with patch('qmd_write_fence.alive',side_effect=ValueError('qmd_endpoint_identity_unavailable')):
   code,r=self.enroll()
  self.assertEqual(code,1,r);self.assertIn('identity_unavailable',r['reason']);self.assertFalse((Path(S.select()['generation'])/'enrolled-projects.json').exists())
 def test_receipt_cannot_be_replayed_against_different_generation_proof(self):
  import qmd_shared_store as S
  self.assertEqual(self.enroll()[0],0)
  file=Path(S.select()['generation'])/'enrolled-projects.json';row=json.loads(file.read_text());row['scopePolicySha256']='0'*64;file.write_text(json.dumps(row))
  with self.assertRaisesRegex(ValueError,'scope_enrollment_invalid'):S.select()
 def test_reinstall_check_accepts_enrolled_common_store_without_regenerating_wiki(self):
  import setup_entry
  self.assertEqual(self.enroll()[0],0)
  row=setup_entry.check_update(self.other)
  self.assertEqual(row['status'],'up_to_date',row)
 def test_interrupted_enqueue_resumes_and_does_not_rewrite_existing_db(self):
  import dirty_queue,qmd_shared_store as S
  with patch.object(dirty_queue,'enqueue_collections',side_effect=OSError('synthetic_queue_interruption')):
   code,row=self.enroll()
  self.assertEqual(code,1);self.assertEqual(row['reason'],'synthetic_queue_interruption')
  receipt=Path(S.select()['generation'])/'enrolled-projects.json'
  self.assertFalse(json.loads(receipt.read_text())['projects'][str(self.other)]['queued'])
  code,row=self.enroll();self.assertEqual(code,0,row)
  self.assertTrue(json.loads(receipt.read_text())['projects'][str(self.other)]['queued'])
 def test_two_approved_projects_cannot_claim_same_unindexed_name(self):
  import qmd_shared_store as S
  self.assertEqual(self.enroll()[0],0)
  first=self.other;self.other=self.home/'third';(self.other/'.auto-context/wiki').mkdir(parents=True);(self.other/'docs').mkdir()
  (self.other/'.auto-context/settings.json').write_bytes((first/'.auto-context/settings.json').read_bytes())
  code,row=self.enroll();self.assertEqual(code,1,row);self.assertEqual(row['reason'],'plugin_qmd_collection_collision')
  self.assertNotIn(str(self.other),S.select()['approvedProjects'])
 def test_invalid_name_is_rejected_before_receipt_or_queue(self):
  import qmd_shared_store as S,dirty_queue
  file=self.other/'.auto-context/settings.json';cfg=json.loads(file.read_text());cfg['collections']=['bad/name'];cfg['collectionPaths']={'bad/name':'docs'};file.write_text(json.dumps(cfg))
  code,row=self.enroll();self.assertEqual(code,1,row);self.assertEqual(row['reason'],'plugin_qmd_collection_scope_invalid')
  self.assertFalse((Path(S.select()['generation'])/'enrolled-projects.json').exists());self.assertFalse(dirty_queue.queue_path().exists())
 def test_unpublished_reservation_drift_does_not_block_existing_project_and_recovers(self):
  import qmd_shared_store as S,config
  # A hard interruption after metadata reservation, before approval receipt.
  S._reserve_owners(S.root(),S._collection_owners(self.other,config.find_project_config(self.other)['config']))
  (self.other/'new-docs').mkdir()
  file=self.other/'.auto-context/settings.json';cfg=json.loads(file.read_text());cfg['collectionPaths']['second-docs']='new-docs';file.write_text(json.dumps(cfg))
  try:S.assert_all_enrolled();unchanged=True
  except ValueError:unchanged=False
  self.assertTrue(unchanged,'an unpublished reservation must not revoke the existing project')
  code,row=self.enroll();self.assertEqual(code,0,row)
  self.assertEqual(S._registry(S.root())['second-docs']['path'],str(self.other/'new-docs'))

if __name__=='__main__':
 suite=unittest.TestSuite(Enrollment(n) for n in Enrollment.__dict__ if n.startswith('test_'))
 result=unittest.TextTestRunner(verbosity=2).run(suite);raise SystemExit(not result.wasSuccessful())

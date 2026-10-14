"""Synthetic live-writer coexistence; never operates on user DBs/processes."""
import json,os,runpy,sys,threading,time,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,'core')
C=runpy.run_path('test/fixtures/shared-qmd-setup-checks.py')['SharedSetup']

class LiveCutover(C):
 def test_live_activation_does_not_require_false_restart_ack(self):
  code,result=self.cli('prepare-store');self.assertEqual(code,0,result)
  with patch('shared_qmd_setup.coordinator._daemon_identity',return_value=None):
   code,result=self.cli('activate-store',['--approve-store-activation','--allow-live-host-sessions'])
  self.assertEqual(code,0,result);self.assertEqual(result['status'],'activated')
  self.assertTrue(result['liveHostSessionsAllowed']);self.assertNotIn('oldHostSessionsRestarted',result)
 def test_only_approved_project_can_enter_new_store_while_service_sessions_continue(self):
  code,result=self.cli('prepare-store');self.assertEqual(code,0,result)
  other=self.home/'service-engineering';(other/'.auto-context').mkdir(parents=True)
  (other/'.auto-context/settings.json').write_text('{"indexing":true,"collections":[]}')
  with patch('shared_qmd_setup.coordinator._daemon_identity',return_value=None):
   code,result=self.cli('activate-store',['--approve-store-activation','--allow-live-host-sessions'])
  self.assertEqual(code,0,result)
  import setup_guard,qmd_shared_store
  self.assertEqual(setup_guard.status(self.project)['status'],'ready')
  self.assertEqual(setup_guard.status(other)['reason'],'project_shared_migration_not_approved')
  with self.assertRaisesRegex(ValueError,'project_shared_migration_not_approved'):
   qmd_shared_store.register_config(other,{'indexing':True,'collections':[]})
 def test_scope_drift_during_writer_wait_refuses_pointer_selection(self):
  code,result=self.cli('prepare-store');self.assertEqual(code,0,result)
  (self.project/'.auto-context/settings.json').write_text('{"indexing":true,"collections":[],"changed":true}')
  with patch('shared_qmd_setup.coordinator._daemon_identity',return_value=None):
   code,result=self.cli('activate-store',['--approve-store-activation','--allow-live-host-sessions'])
  self.assertEqual(code,1,result);self.assertEqual(result['reason'],'store_project_scope_changed')
  import qmd_runtime,qmd_shared_store
  self.assertFalse((qmd_runtime.managed_root()/'active.json').exists());self.assertFalse((qmd_shared_store.root()/'active.json').exists())
 def test_scope_changes_while_real_lock_waits_are_rechecked(self):
  code,result=self.cli('prepare-store');self.assertEqual(code,0,result)
  lock=self.home/'locks/qmd-update.lock.d';lock.mkdir(parents=True);(lock/'pid').write_text('12345')
  results=[]
  def activate():results.append(self.cli('activate-store',['--approve-store-activation','--allow-live-host-sessions','--writer-wait-seconds','2']))
  with patch('shared_qmd_setup.coordinator._daemon_identity',return_value=None):
   thread=threading.Thread(target=activate);thread.start();time.sleep(.1)
   (self.project/'.auto-context/settings.json').write_text('{"indexing":true,"collections":[],"changed":true}')
   (lock/'pid').unlink();lock.rmdir();thread.join(3)
  self.assertFalse(thread.is_alive());self.assertEqual(results[0][1]['reason'],'store_project_scope_changed')
 def test_published_store_stays_closed_until_journal_commits(self):
  import setup_guard,qmd_shared_store,shared_qmd_setup
  code,result=self.cli('prepare-store');self.assertEqual(code,0,result)
  real=qmd_shared_store.activate_empty;observed=[]
  def publish(*a,**k):
   row=real(*a,**k);observed.append(setup_guard.status(self.project));return row
  with patch('shared_qmd_setup.coordinator._daemon_identity',return_value=None),patch.object(qmd_shared_store,'activate_empty',side_effect=publish):
   code,result=self.cli('activate-store',['--approve-store-activation','--allow-live-host-sessions'])
  self.assertEqual(code,0,result);self.assertEqual(observed[0]['reason'],'project_shared_store_transition_pending')
  self.assertEqual(setup_guard.status(self.project)['status'],'ready')
 def test_shared_pointer_publishes_complete_bytes_and_never_overwrites(self):
  import qmd_shared_store
  code,result=self.cli('prepare-store');self.assertEqual(code,0,result)
  link=os.link;seen=[]
  def atomic(src,dst,**kwargs):
   seen.append(json.loads(Path(src).read_text()));return link(src,dst,**kwargs)
  with patch('shared_qmd_setup.coordinator._daemon_identity',return_value=None),patch.object(qmd_shared_store.os,'link',side_effect=atomic):
   code,result=self.cli('activate-store',['--approve-store-activation','--allow-live-host-sessions'])
  self.assertEqual(code,0,result);self.assertEqual(set(seen[0]),{'schema','generation'})
  pointer=qmd_shared_store.root()/'active.json';before=pointer.read_bytes()
  with self.assertRaises(FileExistsError):qmd_shared_store.activate_empty(result['generation'])
  self.assertEqual(pointer.read_bytes(),before)
 def test_v2_queue_does_not_alias_or_consume_late_legacy_writes(self):
  import dirty_queue
  old=self.home/'.config/qmd/dirty-queue';old.parent.mkdir(parents=True);old.write_text('legacy before\n')
  new=dirty_queue.queue_path();self.assertNotEqual(new,old)
  dirty_queue.enqueue_collections({'synthetic':str(self.project)},project_root=str(self.project))
  before=new.read_bytes();old.write_text(old.read_text()+'legacy late\n')
  self.assertEqual(new.read_bytes(),before);self.assertIn('legacy late',old.read_text())
  with patch.dict(os.environ,{'QMD_DIRTY_QUEUE':str(old)}):
   with self.assertRaisesRegex(ValueError,'legacy_dirty_queue_alias_forbidden'):dirty_queue.queue_path()

class WriterFence(unittest.TestCase):
 def test_lost_reservation_fails_and_never_removes_replacement_holder(self):
  import qmd_write_fence as F,tempfile
  with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,{'QMD_LOCK_BASE':tmp}),patch.object(F,'alive',return_value=True):
   with F.hold(1) as verify:
    pid=Path(tmp)/'qmd-update.lock.d/pid';pid.write_text('12345')
    with self.assertRaisesRegex(ValueError,'qmd_writer_fence_lost'):verify()
   self.assertEqual(pid.read_text(),'12345')
 def test_common_writer_and_embed_locks_wait_without_signalling_holder(self):
  import qmd_write_fence as F,tempfile
  with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,{'QMD_LOCK_BASE':tmp}),patch.object(F,'alive',return_value=True):
   lock=Path(tmp)/'qmd-update.lock.d';lock.mkdir();(lock/'pid').write_text('12345')
   entered=[]
   def waiter():
    with F.hold(1):
     entered.append(True);self.assertTrue((Path(tmp)/'qmd-embed.lock.d/pid').exists())
   thread=threading.Thread(target=waiter);thread.start();time.sleep(.1);self.assertEqual(entered,[])
   (lock/'pid').unlink();lock.rmdir();thread.join(2);self.assertEqual(entered,[True])
   self.assertFalse(lock.exists());self.assertFalse((Path(tmp)/'qmd-embed.lock.d').exists())
 def test_busy_or_denied_holder_is_retained(self):
  import qmd_write_fence as F,tempfile
  with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,{'QMD_LOCK_BASE':tmp}),patch.object(F,'alive',return_value=True):
   lock=Path(tmp)/'qmd-embed.lock.d';lock.mkdir();(lock/'pid').write_text('12345')
   with self.assertRaisesRegex(ValueError,'qmd_writer_fence_busy'):
    with F.hold(.1):pass
   self.assertEqual((lock/'pid').read_text(),'12345');self.assertFalse((Path(tmp)/'qmd-update.lock.d').exists())
   with patch.object(F,'alive',side_effect=ValueError('qmd_endpoint_identity_unavailable')):
    with self.assertRaisesRegex(ValueError,'identity_unavailable'):
     with F.hold(.1):pass
   self.assertTrue(lock.exists())

if __name__=='__main__':
 # Only these new cases, not every inherited installer test again.
 names=[n for n in LiveCutover.__dict__ if n.startswith('test_')]
 suite=unittest.TestSuite([*(LiveCutover(n) for n in names),unittest.defaultTestLoader.loadTestsFromTestCase(WriterFence)])
 result=unittest.TextTestRunner(verbosity=2).run(suite);raise SystemExit(not result.wasSuccessful())

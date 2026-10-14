"""Real flock contention in disposable installer state; no user processes."""
import fcntl, json, os, runpy, sys, time, unittest, tempfile
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, 'core')
Install = runpy.run_path('test/fixtures/shared-install-checks.py')['Install']

class Contention(Install):
 def short_flags(self):
  return self.flags + ['--deadline-seconds', '.15', '--writer-wait-seconds', '.1']
 def test_live_writer_defers_and_resumes_without_removing_holder(self):
  self.assertEqual(self.entry()[0], 0)
  import qmd_write_fence
  lock=qmd_write_fence.paths()[0];lock.mkdir();(lock/'pid').write_text('12345')
  try:
   started=time.monotonic();code,row=self.entry(flags=self.short_flags())
   self.assertEqual(code,75,row);self.assertEqual(row['reason'],'qmd_writer_fence_busy')
   self.assertLess(time.monotonic()-started,2)
   self.assertEqual((lock/'pid').read_text(),'12345');self.preserved()
  finally:(lock/'pid').unlink();lock.rmdir()
  self.assertEqual(self.entry()[0],0)
 def hold_file(self,path):
  path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
  fd=os.open(path,os.O_RDWR|os.O_CREAT,0o600);fcntl.flock(fd,fcntl.LOCK_EX)
  return fd
 def test_project_lock_timeout_does_not_write_another_install_journal(self):
  import shared_install
  lock=self.project/'.auto-context/.install-update.lock';fd=self.hold_file(lock)
  try:
   started=time.monotonic();code,row=self.entry(flags=self.short_flags())
   self.assertEqual(code,75,row);self.assertEqual(row['reason'],'install_lock_busy')
   self.assertLess(time.monotonic()-started,2)
   self.assertFalse((lock.parent/shared_install.JOURNAL).exists())
   self.assertFalse((lock.parent/'settings.json').exists());self.preserved()
  finally:os.close(fd)
  self.assertEqual(self.entry()[0],0)
 def test_nested_bootstrap_registry_and_queue_locks_defer_then_resume(self):
  import dirty_queue,qmd_shared_store as S,shared_install
  self.assertEqual(self.entry()[0],0)
  queue=dirty_queue.queue_path()
  paths=[S.root()/'bootstrap.lock',S.root()/'collections.lock',Path(str(queue)+'.lock'),queue]
  # Queue enqueue is otherwise unnecessary after a completed install. A new
  # synthetic source ensures the real append/ACK path participates on retry.
  for number,path in enumerate(paths):
   with self.subTest(lock=path.name):
    (self.project/'docs/source.md').write_text('Synthetic change '+str(number))
    before=path.read_bytes();fd=self.hold_file(path)
    try:
     started=time.monotonic();code,row=self.entry(flags=self.short_flags())
     self.assertEqual(code,75,row);self.assertLess(time.monotonic()-started,2)
     self.assertEqual(path.read_bytes(),before)
     journal=json.loads((self.project/'.auto-context'/shared_install.JOURNAL).read_text())
     self.assertEqual(journal['phase'],'deferred')
    finally:os.close(fd)
    self.assertEqual(self.entry()[0],0)
  # Only the deliberately edited synthetic source differs from its baseline.
  for p,body in self.original_bytes.items():self.assertEqual(p.read_bytes(),body,str(p))

 def test_partial_activation_cleanup_busy_requires_recovery_and_retains_pointer(self):
  import install_deadline as D,qmd_runtime as R,qmd_shared_store as S,shared_qmd_setup as B
  (self.project/'.auto-context/settings.json').write_bytes(self.legacy.read_bytes())
  self.assertEqual(self.entry('prepare-store')[0],0)
  with patch.object(S,'activate_empty',side_effect=TimeoutError('synthetic publication interrupted')),patch.object(R,'deselect_if_matches',side_effect=D.InstallBusy('install_lock_busy')):
   code,row=self.entry('activate-store')
  self.assertEqual(code,1,row)
  self.assertEqual(row['reason'],'store_activation_cleanup_busy_requires_recovery')
  self.assertEqual(B.status()['status'],'recovery_required')
  self.assertTrue((R.managed_root()/'active.json').is_file())
  self.assertFalse((S.root()/'active.json').exists());self.preserved()

class Budget(unittest.TestCase):
 def test_real_flock_wait_is_bounded_and_context_resets(self):
  import install_deadline as D
  with tempfile.TemporaryDirectory() as tmp:
   path=Path(tmp)/'held.lock'
   owner=os.open(path,os.O_RDWR|os.O_CREAT,0o600)
   fcntl.flock(owner,fcntl.LOCK_EX)
   fd=os.open(path,os.O_RDWR)
   try:
    started=time.monotonic()
    with D.operation(.08),self.assertRaisesRegex(D.InstallBusy,'install_lock_busy'):
     D.flock(fd,fcntl.LOCK_EX)
    self.assertLess(time.monotonic()-started,.5)
    self.assertIsNone(D._deadline.get())
   finally:os.close(fd);os.close(owner)
 def test_nested_budget_cannot_extend_parent_and_denial_is_not_busy(self):
  import install_deadline as D
  with D.operation(.08) as parent:
   with D.operation(30) as child:self.assertEqual(child,parent)
   with patch.object(D.fcntl,'flock',side_effect=PermissionError('inspection denied')):
    with self.assertRaises(PermissionError):D.flock(99,fcntl.LOCK_EX)
  for value in (0,-1,float('nan'),float('inf'),3601):
   with self.assertRaises(ValueError):
    with D.operation(value):pass
 def test_runtime_activation_lock_keeps_both_pointers_unselected(self):
  # No model invocation or real global runtime. Test the native flock edge
  # with the same disposable generation proof used by the installer fixture.
  import install_deadline as D,qmd_runtime as R
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);generation=root/'generations/g-synthetic';generation.mkdir(parents=True)
   lock=root/'activation.lock';fd=os.open(lock,os.O_RDWR|os.O_CREAT,0o600)
   fcntl.flock(fd,fcntl.LOCK_EX)
   try:
    with patch.object(R,'_prepared',return_value={}),D.operation(.08):
     with self.assertRaises(D.InstallBusy):R.activate(root,generation,require_unselected=True)
    self.assertFalse((root/'active.json').exists())
   finally:os.close(fd)
 def test_endpoint_lock_timeout_does_not_start_a_daemon(self):
  import install_deadline as D,qmd_endpoint as E
  with tempfile.TemporaryDirectory() as tmp:
   base=Path(tmp);lock=base/'endpoint-start.lock'
   fd=os.open(lock,os.O_RDWR|os.O_CREAT,0o600);fcntl.flock(fd,fcntl.LOCK_EX)
   try:
    with patch.object(E,'root',return_value=base),patch.object(E,'inspection_available'),patch.object(E,'ensure_locked') as start,D.operation(.08):
     with self.assertRaises(D.InstallBusy):E.ensure()
     start.assert_not_called()
    self.assertFalse((base/'endpoint.json').exists())
   finally:os.close(fd)

if __name__=='__main__':
 names=[n for n in Contention.__dict__ if n.startswith('test_')]
 suite=unittest.TestSuite([*(Contention(n) for n in names),unittest.defaultTestLoader.loadTestsFromTestCase(Budget)])
 result=unittest.TextTestRunner(verbosity=2).run(suite)
 raise SystemExit(not result.wasSuccessful())

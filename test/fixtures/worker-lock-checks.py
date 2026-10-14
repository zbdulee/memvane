import json,os,sys,tempfile,unittest,subprocess
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,'core')
try:import worker_lock as L
except ImportError:raise SystemExit('worker_lock_helper_missing')

class Locks(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.lock=self.root/'writer';self.lock.mkdir(mode=0o700);(self.lock/'pid').write_text('12345')
 def tearDown(self):self.temp.cleanup()
 def test_denial_never_reclaims(self):
  def denied(_):raise ValueError('qmd_endpoint_identity_unavailable')
  before=(self.lock/'pid').read_bytes();r=L.reclaim(self.lock,alive=denied)
  self.assertEqual(r['status'],'busy');self.assertEqual(r['reason'],'inspection_denied');self.assertEqual((self.lock/'pid').read_bytes(),before)
 def test_actual_alive_eperm_path_never_reclaims(self):
  with patch('qmd_endpoint.os.kill',side_effect=PermissionError(1,'Operation not permitted')):
   r=L.reclaim(self.lock)
  self.assertEqual(r['reason'],'inspection_denied');self.assertTrue((self.lock/'pid').exists())
 def test_install_maintenance_unknown_and_denied_are_not_dead(self):
  import install_maintenance as M
  self.assertEqual(M.locks_status([('writer',self.lock)],alive=lambda _:None)['busy'],'writer')
  with patch('qmd_endpoint.os.kill',side_effect=PermissionError(1,'Operation not permitted')):
   with self.assertRaisesRegex(ValueError,'identity_unavailable'):M.locks_status([('writer',self.lock)],alive=L.process_alive)
 def test_live_holder_preserved(self):self.assertEqual(L.reclaim(self.lock,alive=lambda _:True)['status'],'busy');self.assertTrue(self.lock.exists())
 def test_proved_dead_owned_marker_only(self):self.assertEqual(L.reclaim(self.lock,alive=lambda _:False)['status'],'reclaimed');self.assertFalse(self.lock.exists())
 def test_construction_window_preserved(self):
  (self.lock/'pid').unlink();self.assertEqual(L.reclaim(self.lock,alive=lambda _:False)['status'],'busy');self.assertTrue(self.lock.exists())
 def test_replaced_directory_is_never_removed(self):
  def replaced(_):self.lock.rename(self.root/'old');self.lock.mkdir();(self.lock/'pid').write_text('12345');return False
  self.assertEqual(L.reclaim(self.lock,alive=replaced)['status'],'busy');self.assertEqual((self.lock/'pid').read_text(),'12345')
 def test_extra_files_and_symlinks_refused(self):
  (self.lock/'extra').write_text('preserve')
  with self.assertRaises(ValueError):L.reclaim(self.lock,alive=lambda _:False)
  (self.lock/'extra').unlink();(self.lock/'pid').unlink();(self.lock/'pid').symlink_to(self.root/'other')
  with self.assertRaises((ValueError,OSError)):L.reclaim(self.lock,alive=lambda _:False)
 def test_unknown_result_is_not_proven_dead(self):
  self.assertEqual(L.reclaim(self.lock,alive=lambda _:None)['status'],'busy');self.assertTrue(self.lock.exists())
 def test_delayed_parent_publication_cannot_overwrite_replacement_owner(self):
  dirfd=os.open(self.lock,os.O_RDONLY);pidfd=os.open(self.lock/'pid',os.O_WRONLY)
  try:
   child=subprocess.run([sys.executable,'-c',"import sys;sys.path.insert(0,'core');import worker_lock as L;raise SystemExit(L.main(['release',sys.argv[1],sys.argv[2],sys.argv[3]]))",str(self.lock),str(pidfd),str(dirfd)],pass_fds=(pidfd,dirfd),capture_output=True,text=True,timeout=5)
   self.assertEqual(child.returncode,0,child.stderr);self.lock.mkdir();(self.lock/'pid').write_text(str(os.getpid()))
   os.write(pidfd,b'999999999')  # late parent writes the old, unlinked inode
   self.assertEqual((self.lock/'pid').read_text(),str(os.getpid()))
   self.assertFalse(L.release(self.lock,pidfd,dirfd));self.assertTrue(self.lock.exists())
  finally:os.close(pidfd);os.close(dirfd)
 def test_readonly_parent_release_preserves_owner_marker(self):
  self.root.chmod(0o500)
  try:
   with self.assertRaises(PermissionError):L.release_pid(self.lock,12345)
   self.assertEqual((self.lock/'pid').read_text(),'12345')
  finally:self.root.chmod(0o700)
 def test_release_and_dead_reclaim_serialize_before_path_rename(self):
  dirfd=os.open(self.lock,os.O_RDONLY);pidfd=os.open(self.lock/'pid',os.O_RDONLY);rename=Path.rename;competing=[]
  def before_rename(source,target):
   # A reclaimer arriving exactly after identity validation cannot retire the
   # original and let a replacement occupy its pathname under this release.
   competing.append(L.reclaim(source,alive=lambda _:False))
   return rename(source,target)
  try:
   with patch.object(Path,'rename',autospec=True,side_effect=before_rename):self.assertTrue(L.release(self.lock,pidfd,dirfd))
   self.assertEqual(competing,[{'status':'busy','reason':'reclaim_lease_busy'}]);self.assertFalse(self.lock.exists())
   self.lock.mkdir();(self.lock/'pid').write_text(str(os.getpid()));self.assertFalse(L.release(self.lock,pidfd,dirfd));self.assertTrue(self.lock.exists())
  finally:os.close(pidfd);os.close(dirfd)
 def test_shell_worker_preserves_denied_marker_and_queue(self):
  (self.lock/'pid').write_text(str(os.getpid()));queue=self.root/'queue';queue.write_text('fixture\t'+str(self.root)+'\n');queue.chmod(0o600)
  shim=self.root/'python3';called=self.root/'helper-called'
  shim.write_text('#!/bin/sh\ncase "$1" in */worker_lock.py) echo called > "'+str(called)+'"; exit 75;; esac\nexec '+sys.executable+' "$@"\n');shim.chmod(0o700)
  qmd=self.root/'qmd';qmd.write_text('#!/bin/sh\nexit 0\n');qmd.chmod(0o700)
  env=dict(os.environ,PATH=str(self.root)+os.pathsep+os.environ['PATH'],QMD_SETUP_GUARD_FIXTURE='1',QMD_RECALL_LOG='',QMD_FAKE_QMD=str(qmd),QMD_BIN=str(qmd),QMD_DIRTY_QUEUE=str(queue),QMD_LOCK_BASE=str(self.root/'locks'),QMD_INDEX_WORKER_LOCKDIR=str(self.lock),QMD_CACHE_DIR=str(self.root/'cache'),QMD_INDEX_WORKER_LOG=str(self.root/'log'))
  p=subprocess.run(['bash','backend/index_worker.sh'],env=env,capture_output=True,text=True,timeout=10)
  self.assertEqual(p.returncode,75,p.stderr);self.assertTrue(called.exists());self.assertEqual((self.lock/'pid').read_text(),str(os.getpid()));self.assertTrue(queue.read_bytes())
if __name__=='__main__':unittest.main(verbosity=2)

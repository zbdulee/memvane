"""Actual subprocess abrupt exits, synthetic store only; no OS/user signals."""
import json,os,runpy,sqlite3,subprocess,sys,unittest,time,hashlib
from pathlib import Path
sys.path.insert(0,'core')
import shared_qmd_setup as B,shared_install as I,qmd_shared_store as S,worker_lock as L,qmd_write_fence as F
C=runpy.run_path('test/fixtures/shared-install-checks.py')['Install']
class Abrupt(C):
 def setUp(self):
  super().setUp()
  self.qmd.write_text(self.qmd.read_text().replace('a=sys.argv[1:]',"a=sys.argv[1:]\nwith (Path(os.environ['HOME'])/'native-commands.jsonl').open('a') as log:log.write(json.dumps({'args':a,'cwd':os.getcwd()})+'\\n')"))
 def fault(self,mode):
  request=self.home/'fault.json';request.write_text(json.dumps({'mode':mode,'project':str(self.project),'flags':self.flags}))
  child=subprocess.Popen([sys.executable,'test/fixtures/install-fault-child.py',str(request)],env=dict(os.environ),stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
  out,err=child.communicate(timeout=25);self.assertEqual(child.returncode,77,out+err)
  return child.pid
 def test_abrupt_embed_checkpoint_reuses_generation(self):
  self.fault('embed_checkpoint');row=json.loads((S.root()/B.JOURNAL).read_text());generation=row['building']['generation']
  self.assertIn('embed',row['building']['done']);code,row=self.entry();self.assertEqual(code,0,row);self.assertEqual(S.select()['generation'],generation);self.preserved()
  commands=[json.loads(v) for v in (self.home/'native-commands.jsonl').read_text().splitlines()]
  self.assertEqual(sum(c['cwd']==generation and c['args'][:1]==['embed'] for c in commands),1,'completed bootstrap embed must not be relaunched')
 def test_abrupt_settings_publication_resumes_without_replacing_original(self):
  self.fault('settings_copy');settings=self.project/'.auto-context/settings.json';inode=settings.stat().st_ino
  code,row=self.entry();self.assertEqual(code,0,row);self.assertEqual(settings.stat().st_ino,inode);self.preserved()
 def test_abrupt_remove_success_before_checkpoint_resumes(self):
  self.fault('remove_before_checkpoint');journal=json.loads((S.root()/B.JOURNAL).read_text());generation=journal['building']['generation']
  self.assertEqual(journal['building']['inFlight'],'remove');code,row=self.entry();self.assertEqual(code,0,row);self.assertEqual(S.select()['generation'],generation);self.preserved()
 def test_abrupt_repair_commit_resumes_and_preserves_healthy_vectors(self):
  self.assertEqual(self.entry()[0],0);selected=S.select();index=selected['INDEX_PATH']
  with sqlite3.connect(index) as db:
   h=db.execute('SELECT hash FROM documents WHERE collection="fixture-docs"').fetchone()[0]
   healthy=db.execute('SELECT * FROM content_vectors WHERE hash!=?',(h,)).fetchall();physical=db.execute('SELECT * FROM vectors_vec WHERE hash_seq!=?',(h+'_0',)).fetchall()
   db.execute('DELETE FROM vectors_vec WHERE hash_seq=?',(h+'_0',))
  dead_pid=self.fault('repair_commit');row=json.loads((self.project/'.auto-context'/I.JOURNAL).read_text());self.assertEqual(row['vectorRepair']['phase'],'metadata_invalidated')
  # wait() above proves this *synthetic* controller exited. Reclaim only its
  # disposable markers through the production helper; no forced deletion.
  for lock in F.paths():
   self.assertEqual((lock/'pid').read_text().strip(),str(dead_pid))
   self.assertEqual(L.reclaim(lock,alive=lambda pid:False if pid==dead_pid else True)['status'],'reclaimed')
  code,row=self.entry();self.assertEqual(code,0,row);self.assertTrue(row['readiness']['physicalVectorAudit']);self.assertEqual(S.select()['generation'],selected['generation'])
  with sqlite3.connect(index) as db:
   self.assertEqual(db.execute('SELECT * FROM content_vectors WHERE hash!=?',(h,)).fetchall(),healthy)
   self.assertEqual(db.execute('SELECT * FROM vectors_vec WHERE hash_seq!=?',(h+'_0',)).fetchall(),physical)
  self.preserved()
 def test_live_native_after_controller_exit_defers_without_second_writer(self):
  # This runner is a local synthetic Python child, never a model executable.
  body=self.qmd.read_text();body=body.replace('a=sys.argv[1:]','''a=sys.argv[1:]
if a and a[0]=='embed':
 import time
 home=Path(os.environ['HOME']);(home/'embed-alive').write_text(str(os.getpid()))
 end=time.monotonic()+20
 while not (home/'release-embed').exists() and time.monotonic()<end:time.sleep(.02)
''');self.qmd.write_text(body)
  self.fault('live_embed');base=S.root();before=(base/B.JOURNAL).read_bytes();row=json.loads(before);g=Path(row['building']['generation']);sha=hashlib.sha256((g/'index.sqlite').read_bytes()).hexdigest()
  try:
   code,row=self.entry();self.assertEqual(code,75,row);self.assertIn('bootstrap_native_writer_busy',row['reason'])
   self.assertEqual((base/B.JOURNAL).read_bytes(),before);self.assertEqual(hashlib.sha256((g/'index.sqlite').read_bytes()).hexdigest(),sha)
  finally:(self.home/'release-embed').touch()
  until=time.monotonic()+10
  while True:
   try:
    with B._native_lease(base):break
   except BlockingIOError:
    if time.monotonic()>=until:self.fail('synthetic writer did not release lease')
    time.sleep(.02)
  code,row=self.entry();self.assertEqual(code,0,row);self.assertEqual(S.select()['generation'],str(g));self.preserved()
 def test_spawning_package_launcher_is_bypassed_and_writer_keeps_lease(self):
  # Unlike the exec shim, this package launcher would drop lease FDs when
  # spawning its CLI. Direct CPU entry must reach the CLI instead.
  cli=self.qmd.parent.parent/'dist/cli/qmd.js';body=self.qmd.read_text()
  body=body.replace('a=sys.argv[1:]','''a=sys.argv[1:]
if a and a[0]=='embed':
 import time
 home=Path(os.environ['HOME']);(home/'embed-alive').write_text(str(os.getpid()))
 end=time.monotonic()+20
 while not (home/'release-embed').exists() and time.monotonic()<end:time.sleep(.02)
''');cli.write_text(body)
  self.qmd.write_text('import os,sys,subprocess\nfrom pathlib import Path\na=sys.argv[1:]\nif a[:1]==["--version"]:print("qmd 2.5.3");sys.exit(0)\nif a[:1]==["--help"]:print("qmd vsearch qmd search qmd get qmd collection add qmd update qmd embed");sys.exit(0)\n(Path(os.environ["HOME"])/"launcher-writer").touch()\nraise SystemExit(subprocess.call(['+repr(sys.executable)+','+repr(str(cli))+',*a],close_fds=True))\n')
  self.node.write_text('#!/bin/sh\nif [ "$1" = "--version" ]; then echo v24.14.0; else exec '+sys.executable+' "$@"; fi\n')
  self.fault('live_embed');base=S.root();before=(base/B.JOURNAL).read_bytes();g=json.loads(before)['building']['generation']
  try:
   self.assertFalse((self.home/'launcher-writer').exists());code,row=self.entry();self.assertEqual(code,75,row);self.assertEqual((base/B.JOURNAL).read_bytes(),before)
  finally:(self.home/'release-embed').touch()
  until=time.monotonic()+10
  while True:
   try:
    with B._native_lease(base):break
   except BlockingIOError:
    if time.monotonic()>=until:self.fail('synthetic direct CLI lease did not release')
    time.sleep(.02)
  code,row=self.entry();self.assertEqual(code,0,row);self.assertEqual(S.select()['generation'],g);self.assertFalse((self.home/'launcher-writer').exists());self.preserved()
if __name__=='__main__':
 suite=unittest.TestSuite(Abrupt(n) for n in Abrupt.__dict__ if n.startswith('test_'));r=unittest.TextTestRunner(verbosity=2).run(suite);raise SystemExit(not r.wasSuccessful())

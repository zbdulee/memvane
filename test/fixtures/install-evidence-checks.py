"""Pure install evidence decisions and their shared_install lifecycle wiring.

No install, QMD, worker, DB or model runs. The lifecycle cases patch only the
journal save and queue append to observe intent → enqueue → ACK ordering.
"""
import ast,copy,json,sys,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0,'core')
import install_evidence as V

OWNERS={'b-docs':{'path':'/p/b','indexed':True},'a-docs':{'path':'/p/a','indexed':True},'wiki-src':{'path':'/p/s','indexed':False}}
MODEL={'path':'/m/embed.gguf','sha256':'0'*64}

class Pure(unittest.TestCase):
 def test_module_has_no_io_imports(self):
  tree=ast.parse(Path('core/install_evidence.py').read_text())
  self.assertEqual([n for n in ast.walk(tree) if isinstance(n,(ast.Import,ast.ImportFrom))],[])

 def test_indexed_scope_keeps_request_order(self):
  self.assertEqual(V.indexed_names(OWNERS),['b-docs','a-docs'])
  self.assertEqual(V.indexed_paths(OWNERS),{'b-docs':'/p/b','a-docs':'/p/a'})
  self.assertEqual(V.indexed_names({}),[])

 def test_file_identity_includes_mtime_and_ctime(self):
  info=SimpleNamespace(st_dev=1,st_ino=2,st_size=3,st_mtime_ns=4,st_ctime_ns=5)
  self.assertEqual(V.file_identity(info),(1,2,3,4,5))
  later=SimpleNamespace(st_dev=1,st_ino=2,st_size=3,st_mtime_ns=4,st_ctime_ns=6)
  self.assertNotEqual(V.file_identity(info),V.file_identity(later))

 def test_source_queue_names(self):
  snap={'a-docs':{'x.md':{'sha256':'1'}},'b-docs':{}}
  self.assertEqual(V.source_queue_names(snap,copy.deepcopy(snap),{},OWNERS),[])
  changed=dict(snap,**{'b-docs':{'y.md':{'sha256':'2'}}})
  self.assertEqual(V.source_queue_names(snap,changed,{},OWNERS),['b-docs'])
  # First pass: no previous ACK, every current collection is published, sorted.
  self.assertEqual(V.source_queue_names({},snap,{},OWNERS),['a-docs','b-docs'])
  # Unacknowledged intent is republished even when the snapshot now matches.
  self.assertEqual(V.source_queue_names(snap,snap,{'collections':['b-docs']},OWNERS),['b-docs'])

 def test_source_queue_names_rejects_out_of_scope_intent(self):
  snap={'a-docs':{}}
  for intent in ({'collections':['gone']},{'collections':['b-docs']}):
   with self.assertRaisesRegex(ValueError,'^invalid_install_source_queue_intent$'):V.source_queue_names(snap,snap,intent,OWNERS)
  with self.assertRaisesRegex(ValueError,'^invalid_install_source_queue_intent$'):
   V.source_queue_names({},{'wiki-src':{}},{},OWNERS)

 def test_vector_scope(self):
  proof={'modelFingerprints':[{'model':MODEL['path'],'fingerprint':'fp'}]}
  self.assertEqual(V.vector_scope(proof,OWNERS,MODEL),(['b-docs','a-docs'],MODEL['path'],'fp'))
  for bad in ([],[{'model':'/other','fingerprint':'fp'}],[{'model':MODEL['path'],'fingerprint':'a'},{'model':MODEL['path'],'fingerprint':'b'}]):
   with self.assertRaisesRegex(ValueError,'^vector_model_identity_unproved$'):V.vector_scope({'modelFingerprints':bad},OWNERS,MODEL)

 def test_scoped_search_count(self):
  self.assertEqual(V.scoped_search_count('a-docs',[{'file':'qmd://a-docs/x.md'},{'file':'qmd://a-docs/d/y.md'}]),2)
  for hits in ([],None,[{'file':'qmd://a-docs/x.md'},{'file':'qmd://a-docs-extra/x.md'}],[{'file':'qmd://a-docs'}],[{}]):
   with self.assertRaisesRegex(ValueError,'^install_scoped_search_not_ready:a-docs$'):V.scoped_search_count('a-docs',hits)

class Lifecycle(unittest.TestCase):
 def setUp(self):
  import shared_install
  self.si=shared_install;self.events=[]
  save=patch.object(shared_install,'_save',side_effect=lambda auto,row:self.events.append(('save',copy.deepcopy(row))))
  enqueue=patch.object(shared_install.dirty_queue,'enqueue_collections',side_effect=lambda paths,project_root:self.events.append(('enqueue',dict(paths),project_root)))
  for p in (save,enqueue):p.start();self.addCleanup(p.stop)

 def row(self,**extra):return {'request':{'collections':OWNERS},**extra}

 def test_shared_install_delegates_to_evidence_boundary(self):
  self.assertIs(self.si.V,V)

 def test_unchanged_sources_publish_and_ack_nothing(self):
  snap={'a-docs':{}};row=self.row(sourceSnapshot=snap)
  self.si._queue_sources(Path('/root'),row,Path('/auto'),copy.deepcopy(snap))
  self.assertEqual(self.events,[]);self.assertNotIn('queueCommitted',row)

 def test_intent_is_persisted_before_enqueue_then_acknowledged(self):
  current={'b-docs':{'y.md':{'sha256':'2'}},'a-docs':{}}
  row=self.row(sourceSnapshot={'a-docs':{},'b-docs':{}})
  self.si._queue_sources(Path('/root'),row,Path('/auto'),current)
  kinds=[e[0] for e in self.events];self.assertEqual(kinds,['save','enqueue','save'])
  self.assertEqual(self.events[0][1]['sourceQueueIntent'],{'snapshot':current,'collections':['b-docs']})
  self.assertEqual(self.events[0][1]['sourceSnapshot'],{'a-docs':{},'b-docs':{}})
  self.assertEqual(self.events[1][1:],({'b-docs':'/p/b'},'/root'))
  ack=self.events[2][1]
  self.assertEqual(ack['sourceSnapshot'],current);self.assertIs(ack['queueCommitted'],True);self.assertNotIn('sourceQueueIntent',ack)

 def test_resumed_intent_is_republished(self):
  snap={'a-docs':{},'b-docs':{}}
  row=self.row(sourceSnapshot=snap,sourceQueueIntent={'snapshot':snap,'collections':['a-docs','b-docs']})
  self.si._queue_sources(Path('/root'),row,Path('/auto'),copy.deepcopy(snap))
  self.assertEqual(self.events[1][1],{'a-docs':'/p/a','b-docs':'/p/b'})
  self.assertNotIn('sourceQueueIntent',row)

 def test_invalid_intent_raises_before_any_write(self):
  row=self.row(sourceSnapshot={'a-docs':{}},sourceQueueIntent={'collections':['gone']})
  before=copy.deepcopy(row)
  with self.assertRaisesRegex(ValueError,'^invalid_install_source_queue_intent$'):
   self.si._queue_sources(Path('/root'),row,Path('/auto'),{'a-docs':{}})
  self.assertEqual(self.events,[]);self.assertEqual(row,before)

 def test_vector_scope_reads_bootstrap_proof_then_decides(self):
  class Proof:
   def __init__(self,body):self.body=body
   def read_text(self):return json.dumps({'proof':self.body})
  request={'collections':OWNERS,'model':MODEL}
  good={'modelFingerprints':[{'model':MODEL['path'],'fingerprint':'fp'}]}
  with patch.object(self.si.S,'root',return_value=Path('/synthetic-store')),patch.object(self.si.S,'_private_file',return_value=Proof(good)):
   self.assertEqual(self.si._vector_scope(None,request),(['b-docs','a-docs'],MODEL['path'],'fp'))
  with patch.object(self.si.S,'root',return_value=Path('/synthetic-store')),patch.object(self.si.S,'_private_file',return_value=Proof({'modelFingerprints':[]})):
   with self.assertRaisesRegex(ValueError,'^vector_model_identity_unproved$'):self.si._vector_scope(None,request)

if __name__=='__main__':
 unittest.main(verbosity=2)

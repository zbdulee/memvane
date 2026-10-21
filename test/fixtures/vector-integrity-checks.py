import json,platform,sqlite3,struct,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import Mock
sys.path.insert(0,'core')
try:import vector_integrity as V
except ImportError:raise SystemExit('physical_vector_audit_missing')

class Vectors(unittest.TestCase):
 def setUp(self):
  self.db=sqlite3.connect(':memory:')
  self.db.executescript('CREATE TABLE documents(collection TEXT,hash TEXT,active INTEGER);CREATE TABLE content(hash TEXT,doc TEXT);CREATE TABLE content_vectors(hash TEXT,seq INTEGER,model TEXT,embed_fingerprint TEXT,total_chunks INTEGER);CREATE TABLE vectors_vec(hash_seq TEXT PRIMARY KEY,embedding BLOB);')
  self.add('good','own',2);self.add('foreign','other',1)
 def tearDown(self):self.db.close()
 def add(self,h,c,n):
  self.db.execute('INSERT INTO documents VALUES (?,?,1)',(c,h));self.db.execute('INSERT INTO content VALUES (?,?)',(h,'synthetic body'))
  for s in range(n):
   self.db.execute('INSERT INTO content_vectors VALUES (?,?,"model","fp",?)',(h,s,n));self.db.execute('INSERT INTO vectors_vec VALUES (?,?)',(h+'_'+str(s),struct.pack('<3f',1,2,3)))
  self.db.commit()
 def audit(self):return V.audit(self.db,['own'],'model','fp',3)
 def test_complete_rows_are_reusable(self):self.assertEqual(self.audit()['defects'],[])
 def test_active_document_without_content_cannot_be_complete(self):
  self.db.execute('DELETE FROM content WHERE hash="good"');self.assertEqual(self.audit()['defects'][0]['reason'],'content_missing_or_invalid')
 def test_metadata_only_complete_is_not_physical_complete(self):
  self.db.execute('DELETE FROM vectors_vec WHERE hash_seq="good_1"');self.assertEqual(self.audit()['defects'][0]['reason'],'physical_missing_or_invalid')
 def test_sequence_gap_is_incomplete_even_when_count_matches(self):
  self.db.execute('UPDATE content_vectors SET seq=2 WHERE hash="good" AND seq=0');self.assertTrue(self.audit()['defects'])
 def test_wrong_fingerprint_and_missing_metadata_are_not_complete(self):
  self.db.execute('UPDATE content_vectors SET embed_fingerprint="old" WHERE hash="good"');self.assertTrue(self.audit()['defects'])
 def test_bad_physical_dimensions_and_nonfinite_values_are_not_complete(self):
  for payload in (b'bad',struct.pack('<3f',float('nan'),1,2)):
   self.db.execute('UPDATE vectors_vec SET embedding=? WHERE hash_seq="good_1"',(payload,));self.assertTrue(self.audit()['defects'])
 def test_scoped_invalidation_preserves_vectors_and_healthy_foreign_metadata(self):
  self.db.execute('DELETE FROM vectors_vec WHERE hash_seq="good_1"');self.db.commit();before=self.db.execute('SELECT * FROM vectors_vec ORDER BY hash_seq').fetchall()
  self.db.execute('BEGIN IMMEDIATE');r=V.invalidate(self.db,['own'],'model','fp',3);self.db.commit()
  self.assertEqual(r['invalidatedHashes'],['good']);self.assertEqual(self.db.execute('SELECT * FROM vectors_vec ORDER BY hash_seq').fetchall(),before)
  self.assertEqual(self.db.execute('SELECT count(*) FROM content_vectors WHERE hash="foreign"').fetchone()[0],1)
 def test_shared_foreign_hash_refuses_before_any_invalidation(self):
  self.db.execute('INSERT INTO documents VALUES ("other","good",1)');self.db.execute('DELETE FROM vectors_vec WHERE hash_seq="good_1"');self.db.commit()
  before=self.db.execute('SELECT * FROM content_vectors').fetchall();self.db.execute('BEGIN IMMEDIATE')
  with self.assertRaisesRegex(ValueError,'cross_project'):V.invalidate(self.db,['own'],'model','fp',3)
  self.db.rollback();self.assertEqual(self.db.execute('SELECT * FROM content_vectors').fetchall(),before)
 def test_transaction_rollback_restores_invalidation(self):
  self.db.execute('DELETE FROM vectors_vec WHERE hash_seq="good_1"');self.db.commit();self.db.execute('BEGIN IMMEDIATE');V.invalidate(self.db,['own'],'model','fp',3);self.db.rollback()
  self.assertEqual(self.db.execute('SELECT count(*) FROM content_vectors WHERE hash="good"').fetchone()[0],2)

class ExtensionLocation(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory(prefix='vector-extension-');self.addCleanup(self.temp.cleanup)
  base=Path(self.temp.name)/'node_modules';self.package=base/'@tobilu/qmd'
  (self.package/'bin').mkdir(parents=True)
  (self.package/'package.json').write_text(json.dumps({'version':'2.5.3'}))
  self.runtime={'proof':{'qmd':str(self.package/'bin/qmd')}}
  system={'Darwin':('darwin','dylib'),'Linux':('linux','so')}[platform.system()]
  arch={'arm64':'arm64','aarch64':'arm64','x86_64':'x64'}[platform.machine()]
  extension='sqlite-vec-'+system[0]+'-'+arch+'/vec0.'+system[1]
  self.nested=self.package/'node_modules'/extension
  self.hoisted=base/extension
  self.db=Mock()
  self.db.execute.return_value.fetchone.return_value=('CREATE VIRTUAL TABLE vectors_vec USING vec0(embedding float[3])',)
 def create(self,path):
  path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b'fixture');path.chmod(0o600)
 def test_package_local_extension_is_loaded(self):
  self.create(self.nested)
  V.load_vectors(self.db,self.runtime)
  self.db.load_extension.assert_called_once_with(str(self.nested))
 def test_hoisted_extension_is_loaded_when_local_is_absent(self):
  self.create(self.hoisted)
  V.load_vectors(self.db,self.runtime)
  self.db.load_extension.assert_called_once_with(str(self.hoisted))
 def test_unsafe_local_extension_does_not_fall_back(self):
  self.create(self.hoisted)
  self.nested.parent.mkdir(parents=True,exist_ok=True)
  self.nested.symlink_to(self.hoisted)
  with self.assertRaisesRegex(ValueError,'unsafe_vector_extension'):
   V.load_vectors(self.db,self.runtime)
  self.db.load_extension.assert_not_called()
 def test_missing_python_sqlite_extension_api_fails_clearly(self):
  self.create(self.nested)
  self.db.enable_load_extension=None
  with self.assertRaisesRegex(ValueError,'python_sqlite_extension_loading_unavailable'):
   V.load_vectors(self.db,self.runtime)
  self.db.load_extension.assert_not_called()
if __name__=='__main__':unittest.main(verbosity=2)

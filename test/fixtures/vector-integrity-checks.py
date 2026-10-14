import sqlite3,struct,sys,unittest
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
if __name__=='__main__':unittest.main(verbosity=2)

import hashlib,json,runpy,sqlite3,sys,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,'core')
import qmd_shared_store as S
import shared_install as I
C=runpy.run_path('test/fixtures/shared-install-checks.py')['Install']
class Resume(C):
 def test_physical_hole_repaired_without_reembedding_healthy_hash(self):
  code,row=self.entry();self.assertEqual(code,0,row);index=S.select()['INDEX_PATH']
  with sqlite3.connect(index) as db:
   h=db.execute('SELECT hash FROM documents WHERE collection="fixture-docs"').fetchone()[0]
   healthy=db.execute('SELECT * FROM content_vectors WHERE hash!=?',(h,)).fetchall()
   physical=db.execute('SELECT * FROM vectors_vec WHERE hash_seq!=?',(h+'_0',)).fetchall()
   db.execute('DELETE FROM vectors_vec WHERE hash_seq=?',(h+'_0',))
  code,row=self.entry();self.assertEqual(code,0,row);self.assertTrue(row['readiness']['physicalVectorAudit']);self.assertGreater(row['workerAttempts'],0)
  with sqlite3.connect(index) as db:
   self.assertEqual(db.execute('SELECT * FROM content_vectors WHERE hash!=?',(h,)).fetchall(),healthy)
   self.assertEqual(db.execute('SELECT * FROM vectors_vec WHERE hash_seq!=?',(h+'_0',)).fetchall(),physical)
   self.assertIsNotNone(db.execute('SELECT 1 FROM vectors_vec WHERE hash_seq=?',(h+'_0',)).fetchone())
  self.preserved()
 def test_complete_physical_vectors_reinstall_skips_worker(self):
  self.assertEqual(self.entry()[0],0);code,row=self.entry();self.assertEqual(code,0,row);self.assertEqual(row['workerAttempts'],0);self.assertTrue(row['readiness']['physicalVectorAudit'])
 def test_second_normal_repair_success_is_audited_before_failure(self):
  self.assertEqual(self.entry()[0],0);index=S.select()['INDEX_PATH']
  with sqlite3.connect(index) as db:
   h=db.execute('SELECT hash FROM documents WHERE collection="fixture-docs"').fetchone()[0];db.execute('DELETE FROM vectors_vec WHERE hash_seq=?',(h+'_0',))
  drain=I._drain;partial=[False]
  def first_partial(*a,**kw):
   result=drain(*a,**kw)
   if result['attempts'] and not partial[0]:
    partial[0]=True
    with sqlite3.connect(index) as db:db.execute('DELETE FROM vectors_vec WHERE hash_seq=?',(h+'_0',))
   return result
  with patch.object(I,'_drain',side_effect=first_partial):code,row=self.entry()
  self.assertEqual(code,0,row);self.assertTrue(row['readiness']['physicalVectorAudit']);self.assertGreaterEqual(row['workerAttempts'],2);self.preserved()
if __name__=='__main__':
 suite=unittest.TestSuite(Resume(n) for n in Resume.__dict__ if n.startswith('test_'));r=unittest.TextTestRunner(verbosity=2).run(suite);raise SystemExit(not r.wasSuccessful())

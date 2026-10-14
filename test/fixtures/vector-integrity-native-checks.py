"""Explicit existing sqlite-vec extension; tiny vectors, no model/native build."""
import hashlib,json,sqlite3,struct,sys
from pathlib import Path
sys.path.insert(0,'core')
import vector_integrity as V
extension=Path(sys.argv[1]);db=sqlite3.connect(':memory:')
db.enable_load_extension(True);db.load_extension(str(extension));db.enable_load_extension(False)
db.executescript('CREATE TABLE documents(collection TEXT,hash TEXT,active INTEGER);CREATE TABLE content(hash TEXT,doc TEXT);CREATE TABLE content_vectors(hash TEXT,seq INTEGER,model TEXT,embed_fingerprint TEXT,total_chunks INTEGER);CREATE VIRTUAL TABLE vectors_vec USING vec0(hash_seq TEXT PRIMARY KEY,embedding float[3] distance_metric=cosine);')
for h,c in [('owned','own'),('foreign','other')]:
 db.execute('INSERT INTO documents VALUES (?,?,1)',(c,h));db.execute('INSERT INTO content VALUES (?,?)',(h,'synthetic'))
 db.execute('INSERT INTO content_vectors VALUES (?,0,"model","fp",1)',(h,));db.execute('INSERT INTO vectors_vec VALUES (?,?)',(h+'_0',struct.pack('<3f',1,2,3)))
db.commit();assert not V.audit(db,['own'],'model','fp',3)['defects']
db.execute('DELETE FROM vectors_vec WHERE hash_seq="owned_0"');db.commit();assert V.audit(db,['own'],'model','fp',3)['defects']
foreign=db.execute('SELECT embedding FROM vectors_vec WHERE hash_seq="foreign_0"').fetchone()[0]
db.execute('BEGIN IMMEDIATE');assert V.invalidate(db,['own'],'model','fp',3)['invalidatedHashes']==['owned'];db.commit()
assert db.execute('SELECT embedding FROM vectors_vec WHERE hash_seq="foreign_0"').fetchone()[0]==foreign
assert db.execute('SELECT count(*) FROM content_vectors WHERE hash="foreign"').fetchone()[0]==1
print(json.dumps({'sqliteVec':db.execute('SELECT vec_version()').fetchone()[0],'extensionSha256':hashlib.sha256(extension.read_bytes()).hexdigest(),'completeDetected':True,'physicalMissingDetected':True,'targetMetadataInvalidated':True,'foreignPhysicalAndMetadataPreserved':True,'modelsCalled':0}))

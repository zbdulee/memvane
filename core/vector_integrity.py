"""Scoped metadata + physical vector completeness, never model execution.

Invalidation touches only defective target hashes' metadata, in a caller-held
transaction. Ordinary QMD embed then recomputes incomplete documents. Physical
rows and healthy/foreign metadata are retained; shared foreign hashes refuse.
"""
import array,math,os,platform,stat,json,re
from pathlib import Path

def load_vectors(db,runtime):
 sql=db.execute('SELECT sql FROM sqlite_master WHERE name="vectors_vec"').fetchone()
 if not sql:raise ValueError('physical_vector_table_missing')
 if not re.search(r'\bUSING\s+vec0\b',sql[0],re.I):return  # Relational stand-in in synthetic tests.
 package=Path(runtime['proof']['qmd']).parent.parent
 if json.loads((package/'package.json').read_text()).get('version')!='2.5.3':raise ValueError('unproved_vector_runtime')
 # Node resolves a package-local dependency before a hoisted one. The
 # supported QMD 2.5.3 install keeps sqlite-vec under its own node_modules;
 # a hoisted package is also valid when that local dependency is absent.
 modules=(package.parent.parent if package.parent.name=='@tobilu' else package.parent)
 system={'Darwin':('darwin','dylib'),'Linux':('linux','so')}.get(platform.system())
 arch={'arm64':'arm64','aarch64':'arm64','x86_64':'x64'}.get(platform.machine())
 if not system or not arch:raise ValueError('physical_vector_extension_unavailable')
 extension='sqlite-vec-'+system[0]+'-'+arch
 filename='vec0.'+system[1]
 candidates=(package/'node_modules'/extension/filename,
             modules/extension/filename)
 path=next((item for item in candidates if item.exists() or item.is_symlink()),None)
 if path is None:raise ValueError('physical_vector_extension_unavailable')
 info=path.lstat()
 if not stat.S_ISREG(info.st_mode) or path.is_symlink() or info.st_uid not in (0,os.getuid()) or info.st_mode&0o022:raise ValueError('unsafe_vector_extension')
 if not callable(getattr(db,'enable_load_extension',None)) or not callable(getattr(db,'load_extension',None)):
  raise ValueError('python_sqlite_extension_loading_unavailable')
 db.enable_load_extension(True)
 try:db.load_extension(str(path))
 finally:db.enable_load_extension(False)

def audit(db,collections,model,fingerprint,dimension):
 names=list(collections)
 if not names or type(dimension) is not int or dimension<1:raise ValueError('invalid_vector_audit_scope')
 placeholders=','.join('?' for _ in names)
 rows=db.execute('SELECT DISTINCT d.hash,c.doc FROM documents d LEFT JOIN content c ON c.hash=d.hash WHERE d.active=1 AND d.collection IN ('+placeholders+')',names).fetchall()
 defects=[];eligible=0
 for h,body in rows:
  if not isinstance(body,str):
   eligible+=1;defects.append({'hash':h,'reason':'content_missing_or_invalid'});continue
  if not body.strip().strip('\ufeff').strip():continue
  eligible+=1
  meta=db.execute('SELECT seq,total_chunks FROM content_vectors WHERE hash=? AND model=? AND embed_fingerprint=? ORDER BY seq',(h,model,fingerprint)).fetchall()
  total=meta[0][1] if meta else None
  valid=bool(meta) and type(total) is int and total>0 and len(meta)==total and all(type(s) is int and s==i and n==total for i,(s,n) in enumerate(meta))
  reason=None
  if not valid:reason='metadata_incomplete_or_drifted'
  else:
   for seq,_ in meta:
    physical=db.execute('SELECT embedding FROM vectors_vec WHERE hash_seq=?',(str(h)+'_'+str(seq),)).fetchall()
    if len(physical)!=1 or not isinstance(physical[0][0],bytes) or len(physical[0][0])!=dimension*4:
     reason='physical_missing_or_invalid';break
    values=array.array('f');values.frombytes(physical[0][0])
    if not all(math.isfinite(v) for v in values):reason='physical_missing_or_invalid';break
  if reason:defects.append({'hash':h,'reason':reason})
 return {'eligibleHashes':eligible,'completeHashes':eligible-len(defects),'defects':defects,'physicalVectorAudit':True}

def invalidate(db,collections,model,fingerprint,dimension):
 if not db.in_transaction:raise ValueError('vector_repair_transaction_required')
 report=audit(db,collections,model,fingerprint,dimension);bad=[r['hash'] for r in report['defects']]
 names=list(collections);ph=','.join('?' for _ in names)
 for h in bad:
  if db.execute('SELECT 1 FROM documents WHERE hash=? AND active=1 AND collection NOT IN ('+ph+') LIMIT 1',[h,*names]).fetchone():
   raise ValueError('cross_project_vector_repair_requires_review')
 for h in bad:db.execute('DELETE FROM content_vectors WHERE hash=? AND model=?',(h,model))
 return {**report,'invalidatedHashes':bad}

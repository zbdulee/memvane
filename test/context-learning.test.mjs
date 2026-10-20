import {test} from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
import {mkdtempSync, rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';

test('shared enqueue failure retains safe diagnostics and the private capture', () => {
 const base = mkdtempSync(join(tmpdir(), 'qmd-shared-failure-'));
 try {
 const out = execFileSync('python3', ['-c', `
import json,sys,sqlite3
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,'core')
from context_learning.seam import observe
from context_learning.contracts import digest
from context_learning.local_cycle import ResourceDeferred
base=Path(sys.argv[1]).resolve();project=base/'project';private=base/'private'
(project/'docs').mkdir(parents=True);private.mkdir(mode=0o700)
(project/'docs/guide.md').write_text('synthetic evidence')
config={'collectionPaths':{'sample':'docs'},'contextLearning':{'capture':True,'stateRoot':str(private)},'sharedLearning':{'enabled':True}}
state=private/digest(str(project))
class Unprintable(ValueError):
 def __str__(self):raise RuntimeError('diagnostic formatting failed')
for error,reason in [(ResourceDeferred('preparation_budget'),'preparation_budget'),(ValueError('synthetic body /private/path TOKEN=secret'),'enqueue_unavailable'),(Unprintable('private message'),'enqueue_unavailable'),(ValueError('project_scope_installation_changed'),'project_scope_installation_changed')]:
 with patch('context_learning.corpus.snapshot',return_value=None),patch('context_learning.shared.enqueue',side_effect=error),patch('context_learning.shared.launch') as launch:
  assert observe({'prompt':'synthetic question'},config,project,[{'file':'sample/guide.md'}])=='stored'
  launch.assert_not_called()
 status=json.loads((state/'shared-last-status.json').read_text())
 assert status['status']=='unavailable'
 assert status.get('reason')==reason,status
 assert status['stage']=='enqueue' and status['errorType']==type(error).__name__
 assert 'TOKEN' not in json.dumps(status) and '/private/path' not in json.dumps(status)
for error,reason in [(ResourceDeferred('learning_compute_busy'),'learning_compute_busy'),(ValueError('private message'),'launch_unavailable')]:
 with patch('context_learning.corpus.snapshot',return_value=None),patch('context_learning.shared.enqueue',return_value='queued'),patch('context_learning.shared.launch',side_effect=error) as launch:
  assert observe({'prompt':'synthetic question'},config,project,[{'file':'sample/guide.md'}])=='stored'
  launch.assert_called_once()
 status=json.loads((state/'shared-last-status.json').read_text())
 assert status['status']=='unavailable' and status['stage']=='launch'
 assert status['reason']==reason and status['errorType']==type(error).__name__
assert not (state/'shared-learning.sqlite3').exists()
with sqlite3.connect(state/'learning.sqlite3') as db:assert db.execute('SELECT COUNT(*) FROM samples').fetchone()[0]==6
print('ok')
`, base], {encoding:'utf8',timeout:10000,env:{...process.env,QMD_RECALL_LOG:'',PYTHONDONTWRITEBYTECODE:'1'}});
 assert.equal(out.trim(),'ok');
 } finally {rmSync(base,{recursive:true,force:true});}
});

test('worker diagnostics keep the original failure when a synthetic database is corrupt', () => {
 const base = mkdtempSync(join(tmpdir(), 'qmd-worker-diagnostic-'));
 try {
 const out = execFileSync('python3', ['-c', `
import ast,json,sqlite3,sys
from pathlib import Path
from types import SimpleNamespace
base=Path(sys.argv[1]).resolve()
(base/'shared-learning.sqlite3').write_bytes(b'corrupt synthetic database')
(base/'shared-last-status.json').write_text(json.dumps({'reason':'preparation_budget'}))
source=ast.parse(Path('test/fixtures/context-learning-project-hook-e2e.py').read_text())
functions={n.name:n for n in ast.walk(source) if isinstance(n,ast.FunctionDef) and n.name in ('rows','wait_requested')}
clock=iter([0,0,16]);namespace={'sqlite3':sqlite3,'time':SimpleNamespace(monotonic=lambda:next(clock),sleep=lambda _:None),
 'shared':SimpleNamespace(FULL_WORKER_REQUEST='full-worker-request.json'),'request_nonces':set()}
exec(compile(ast.fix_missing_locations(ast.Module(body=[functions['rows'],functions['wait_requested']],type_ignores=[])),'fixture-diagnostic','exec'),namespace)
before=(base/'shared-learning.sqlite3').read_bytes()
try:namespace['wait_requested'](base,base)
except AssertionError as error:
 message=str(error)
 assert 'full-worker request not persisted' in message and 'preparation_budget' in message,message
 assert 'captures=0' in message and 'cases=unavailable' in message,message
else:raise AssertionError('missing failure')
assert (base/'shared-learning.sqlite3').read_bytes()==before
assert not (base/'learning.sqlite3').exists()
print('ok')
`, base], {encoding:'utf8',timeout:5000,env:{...process.env,QMD_RECALL_LOG:'',PYTHONDONTWRITEBYTECODE:'1'}});
 assert.equal(out.trim(),'ok');
 } finally {rmSync(base,{recursive:true,force:true});}
});

test('offline contracts and storage are isolated, explicit and fail open', () => {
 const dir = mkdtempSync(join(tmpdir(), 'qmd-learning-'));
 try {
 const result = execFileSync('python3', ['-c', `
import sys, sqlite3
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, 'core')
from context_learning.contracts import request, validate_label, digest
from context_learning.store import capture
root=Path(sys.argv[1])
c=[dict(candidate_id='c1',revision_sha256=digest('source'),excerpt='evidence'*100,eligible=True)]
for host in ('claude','codex','hermes'):
 s=request({'prompt':'question'*400},c,host=host,request_id=host)
 assert s['prompt']['truncated'] and s['candidates'][0]['excerpt']['truncated']
 assert len(s['candidates'][0]['excerpt']['text'])==600
 before=list(root.iterdir())
 assert capture(s)=='disabled' and list(root.iterdir())==before
 assert capture(s,enabled='true',state_dir=root)=='disabled'
 assert capture(s,enabled=True,state_dir=root)=='stored'
 assert capture(s,enabled=True,state_dir=root)=='stored'
 label=dict(schema_version=1,request_id=host,candidate_id='c1',revision_sha256=digest('source'),abstain=False,relevance='necessary',evidence='evidence')
 assert validate_label(label,s)['relevance']=='necessary'
 for change in ({'schema_version':2},{'candidate_id':'unknown'},{'revision_sha256':digest('changed')},{'evidence':'absent'}, {'abstain':True}):
  try: validate_label(dict(label,**change),s)
  except ValueError: pass
  else: raise AssertionError(change)
 assert validate_label(dict(label,abstain=True,relevance=None),s)['abstain']
for p in ({}, {'prompt':None}, {'prompt':''}):
 try: request(p,c,host='claude',request_id='x')
 except ValueError: pass
 else: raise AssertionError(p)
with sqlite3.connect(root/'learning.sqlite3') as db:
 assert db.execute('select count(*) from samples').fetchone()[0]==3
 db.execute('BEGIN EXCLUSIVE')
 assert capture(s,enabled=True,state_dir=root)=='storage_unavailable'
with patch('context_learning.store.sqlite3.connect',side_effect=sqlite3.OperationalError('disk full')):
 assert capture(s,enabled=True,state_dir=root)=='storage_unavailable'
assert capture(s,enabled=True)=='missing_state_dir'
assert capture(dict(s,schema_version=99),enabled=True,state_dir=root)=='invalid_schema'
assert capture(dict(s,prompt=dict(s['prompt'],sha256='bad')),enabled=True,state_dir=root)=='storage_unavailable'
for candidates in (c*9, c+c, [dict(c[0],eligible=False)], [dict(c[0],revision_sha256='bad')]):
 try: request({'prompt':'question'},candidates,host='codex',request_id='x')
 except ValueError: pass
 else: raise AssertionError('invalid candidates accepted')
(root/'link').symlink_to(root, target_is_directory=True)
assert capture(s,enabled=True,state_dir=root/'link')=='invalid_state_dir'
print('ok')
`, dir], {encoding:'utf8',env:{...process.env,QMD_RECALL_LOG:'',PYTHONDONTWRITEBYTECODE:'1'}});
 assert.equal(result.trim(),'ok');
 } finally {rmSync(dir,{recursive:true,force:true});}
});

test('Claude and Codex payload capture preserves exact output and isolates projects', () => {
 const parent = mkdtempSync(join(tmpdir(), 'qmd-host-learning-'));
 try {
 const output = execFileSync('python3', ['-c', `
import json, os, subprocess, sys, sqlite3
from pathlib import Path
base=Path(sys.argv[1]).resolve()
state_root=base/'private-state'; state_root.mkdir(mode=0o700)
for host in ('claude','codex'):
 root=base/host; (root/'.auto-context').mkdir(parents=True); (root/'docs').mkdir()
 (root/'docs'/'guide.md').write_text('safe synthetic evidence')
 config={'collections':['sample'],'collectionPaths':{'sample':'.'}}
 settings=root/'.auto-context'/'settings.json'
 def run():
  env=dict(os.environ,QMD_ENGINE=host,QMD_RECALL_LOG='',QMD_QUERY_FIXTURE='test/fixtures/daemon-response.json',INDEX_PATH=str(base/'unused-index.sqlite'),PYTHONDONTWRITEBYTECODE='1')
  return subprocess.check_output(['python3','core/recall.py'],input=json.dumps({'prompt':'검색 결과 정렬은 어떻게 동작해?', 'cwd':str(root),'hook_event_name':'UserPromptSubmit','session_id':'synthetic'}).encode(),env=env)
 settings.write_text(json.dumps(config)); before=run()
 import hashlib
 state=state_root/hashlib.sha256(str(root).encode()).hexdigest()
 assert before and not state.exists()
 settings.write_text(json.dumps(dict(config,contextLearning={'capture':True,'stateRoot':str(state_root)})))
 assert run()==before
 with sqlite3.connect(state/'learning.sqlite3') as db:
  body=json.loads(db.execute('select body from samples').fetchone()[0])
  assert body['host']==host and len(body['candidates'])==1
  assert body['candidates'][0]['revision_sha256']
  assert body['candidates'][0]['excerpt']['text']=='safe synthetic evidence'
  assert not (root/'.auto-context'/'context-learning').exists()
 settings.write_text(json.dumps(dict(config,contextLearning={'capture':'true','stateRoot':str(state_root)})))
 assert run()==before
 with sqlite3.connect(state/'learning.sqlite3') as db: assert db.execute('select count(*) from samples').fetchone()[0]==1
 settings.write_text(json.dumps(dict(config,contextLearning={'capture':True,'stateRoot':str(state_root)})))
 with sqlite3.connect(state/'learning.sqlite3') as db:
  db.execute('BEGIN EXCLUSIVE')
  assert run()==before
  db.rollback()
 dbpath=state/'learning.sqlite3'; saved=state/'saved.sqlite3'
 dbpath.rename(saved); dbpath.symlink_to(saved)
 assert run()==before
 dbpath.unlink(); saved.rename(dbpath)
 (root/'docs'/'guide.md').unlink()
 assert run()==before
 with sqlite3.connect(dbpath) as db:
  partial=json.loads(db.execute('select body from samples order by rowid desc').fetchone()[0])
  assert partial['sampling']['snapshot_failures'] and not partial['sampling']['complete_within_returned_bound']
 (root/'docs'/'guide.md').write_text('safe synthetic evidence')
 # State roots in the indexed project are forbidden, even with exact opt-in.
 settings.write_text(json.dumps(dict(config,contextLearning={'capture':True,'stateRoot':str(root)})))
 assert run()==before and not (root/hashlib.sha256(str(root).encode()).hexdigest()).exists()
print('ok')
`, parent], {encoding:'utf8',env:{...process.env,QMD_RECALL_LOG:'',PYTHONDONTWRITEBYTECODE:'1'}});
 assert.equal(output.trim(),'ok');
 } finally {rmSync(parent,{recursive:true,force:true});}
});


test('source snapshot rejects non-regular files without blocking', () => {
 const dir = mkdtempSync(join(tmpdir(), 'qmd-source-kind-'));
 try {
 const out = execFileSync('python3', ['-c', `
import sys,os
from pathlib import Path
sys.path.insert(0,'core')
from context_learning.seam import _snapshot
root=Path(sys.argv[1]); fifo=root/'fifo';os.mkfifo(fifo)
assert _snapshot(fifo) is None
regular=root/'regular';regular.write_text('snapshot')
assert _snapshot(regular)[1]==b'snapshot'
regular.write_bytes(b'x'*65537)
assert _snapshot(regular) is None
print('ok')
`, dir], {encoding:'utf8',timeout:5000,env:{...process.env,PYTHONDONTWRITEBYTECODE:'1'}});
 assert.equal(out.trim(),'ok');
 } finally {rmSync(dir,{recursive:true,force:true});}
});

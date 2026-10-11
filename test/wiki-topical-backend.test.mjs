import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';

function python(script) {
  const result = spawnSync('python3', ['-c', script], {
    cwd: process.cwd(), encoding: 'utf8', env: { ...process.env, QMD_RECALL_LOG: '', PYTHONDONTWRITEBYTECODE: '1' },
  });
  assert.equal(result.status, 0, result.stderr || result.stdout);
  return JSON.parse(result.stdout);
}

test('topical bridge binds separate mock/backend pass records to card and source bytes', () => {
  const result = python(`
import hashlib,json,sys,tempfile
from pathlib import Path
sys.path.insert(0,'core')
import wiki_topical as t, wiki_topical_backend as b
with tempfile.TemporaryDirectory(prefix='qmd-topical-backend-') as temporary:
 root=Path(temporary).resolve()
 (root/t.MARKER).write_text('synthetic only\\n')
 (root/'sources').mkdir()
 body='The cobalt gate opens at sunrise.\\n'
 (root/'sources/story.md').write_text(body)
 rev=t.source_snapshot(root,'sources/story.md',{})[0]
 quote='The cobalt gate opens at sunrise.'
 span={'sourcePath':'sources/story.md','sourceRevisionSha256':rev['sha256'],'startLine':1,'endLine':1,
       'quoteAnchor':quote,'quoteSha256':t.digest(quote.encode())}
 card={'cardId':'cobalt-gate','title':'Cobalt gate','category':'world-rule','details':'synthetic',
       'claims':[{'claimId':'opening','statement':quote,'state':'actual','timeScope':'chapter-1',
                  'condition':'sunrise','evidence':[span]}]}
 made=b.stage_generation_response(root,{'schema':t.SCHEMA,'cards':[card]})
 generation=made['generationId']
 page=root/'topical-generations'/generation/'cards/cobalt-gate.md'
 original=page.read_bytes()
 request=b.verification_payload(root,generation,'cobalt-gate')
 response={'verdict':'pass','checks':[{'claimId':'opening','sourcePath':'sources/story.md',
           'quoteSha256':span['quoteSha256'],'quoteAnchor':quote,'supported':True}], 'reasons':[]}
 mock=b.validate_verification_response(root,request,response,mode='mock')
 mock_file=b.publish_pass_attestation(root,mock)
 cfg={'extractor':{'builtins':['codex']},'verify':{'builtins':['codex'],'crossEngine':'off'}}
 calls=[]
 def_runner=lambda argv,payload,timeout,cwd:(calls.append((argv,payload['task'])) or ({**response,'_qmd':{}},None,0))
 backend=b.run_verification_backend(root,generation,'cobalt-gate',cfg,'codex',
                                   allow_backend_execution=True,runner=def_runner)
 backend_file=root/'topical-attestations/backend'/generation/'cobalt-gate.json'
 assert backend_file.exists() and mock_file.exists()
 assert page.read_bytes()==original and b'\\nstatus: generated\\n' in original
 assert mock['status']=='mock_pass' and backend['status']=='backend_pass'
 assert backend['cardMarkdownSha256']==hashlib.sha256(original).hexdigest()
 assert backend['sourceRevisionsSha256']==t.digest(t.encoded([rev]))
 assert calls and calls[0][1]==b.VERIFY_TASK
 print(json.dumps({'mock':mock['status'],'backend':backend['status'],'separate':mock_file!=backend_file,
                   'cardUnchanged':page.read_bytes()==original,'backendCalls':len(calls)}))
`);
  assert.deepEqual(result, { mock: 'mock_pass', backend: 'backend_pass', separate: true,
    cardUnchanged: true, backendCalls: 1 });
});

test('invalid claim support and changed source preserve prior pass and card; disabled transport calls nothing', () => {
  const result = python(`
import json,sys,tempfile
from pathlib import Path
sys.path.insert(0,'core')
import wiki_topical as t, wiki_topical_backend as b
with tempfile.TemporaryDirectory(prefix='qmd-topical-backend-') as temporary:
 root=Path(temporary).resolve()
 (root/t.MARKER).write_text('synthetic only\\n')
 (root/'sources').mkdir()
 quote='The violet bell rings at noon.'
 (root/'sources/story.md').write_text(quote+'\\n')
 rev=t.source_snapshot(root,'sources/story.md',{})[0]
 span={'sourcePath':'sources/story.md','sourceRevisionSha256':rev['sha256'],'startLine':1,'endLine':1,
       'quoteAnchor':quote,'quoteSha256':t.digest(quote.encode())}
 card={'cardId':'violet-bell','title':'Violet bell','category':'world-rule','details':'synthetic',
       'claims':[{'claimId':'ring','statement':quote,'state':'actual','timeScope':'chapter-1',
                  'condition':'noon','evidence':[span]}]}
 made=b.stage_generation_response(root,{'schema':t.SCHEMA,'cards':[card]})
 gid=made['generationId']; page=root/'topical-generations'/gid/'cards/violet-bell.md'; original=page.read_bytes()
 request=b.verification_payload(root,gid,'violet-bell')
 base={'claimId':'ring','sourcePath':'sources/story.md','quoteSha256':span['quoteSha256'],
       'quoteAnchor':quote,'supported':True}
 good={'verdict':'pass','checks':[base],'reasons':[]}
 record=b.validate_verification_response(root,request,good,mode='backend',engine='codex')
 pass_file=b.publish_pass_attestation(root,record); pass_bytes=pass_file.read_bytes()
 cfg={'extractor':{'builtins':['codex']},'verify':{'builtins':['codex'],'crossEngine':'off'}}
 called=[]
 def runner(argv,payload,timeout,cwd):
  called.append(True)
  return ({'verdict':'fail','checks':[{**base,'supported':False}],'reasons':['contradiction']},None,0)
 failed=b.run_verification_backend(root,gid,'violet-bell',cfg,'codex',
                                   allow_backend_execution=True,runner=runner)
 assert failed['status']=='backend_fail' and pass_file.read_bytes()==pass_bytes
 try:
  b.run_verification_backend(root,gid,'violet-bell',cfg,'codex',runner=runner)
 except t.TopicalError as exc: assert exc.code=='backend_execution_not_enabled'
 else: raise AssertionError('backend was not locked')
 assert len(called)==1
 bad={**good,'checks':[{**base,'quoteAnchor':'invented quote'}]}
 try: b.validate_verification_response(root,request,bad,mode='backend',engine='codex')
 except t.TopicalError as exc: assert exc.code=='verification_quote_mismatch'
 else: raise AssertionError('invented quote accepted')
 (root/'sources/story.md').write_text('A changed line.\\n')
 try: b.validate_verification_response(root,request,good,mode='backend',engine='codex')
 except t.TopicalError: pass
 else: raise AssertionError('stale source accepted')
 assert page.read_bytes()==original and pass_file.read_bytes()==pass_bytes
 print(json.dumps({'failure':failed['status'],'priorPassUnchanged':True,'cardUnchanged':True,'calls':len(called)}))
`);
  assert.deepEqual(result, { failure: 'backend_fail', priorPassUnchanged: true, cardUnchanged: true, calls: 1 });
});

test('generation backend accepts only explicit local stub response and preserves a prior generation on failure', () => {
  const result = python(`
import json,sys,tempfile
from pathlib import Path
sys.path.insert(0,'core')
import wiki_topical as t, wiki_topical_backend as b
with tempfile.TemporaryDirectory(prefix='qmd-topical-backend-') as temporary:
 root=Path(temporary).resolve()
 (root/t.MARKER).write_text('synthetic only\\n'); (root/'sources').mkdir()
 quote='The indigo bridge is closed at night.'
 (root/'sources/story.md').write_text(quote+'\\n')
 rev=t.source_snapshot(root,'sources/story.md',{})[0]
 card={'cardId':'indigo-bridge','title':'Indigo bridge','category':'world-rule','details':'synthetic',
       'claims':[{'claimId':'closed','statement':quote,'state':'rule','timeScope':'chapter-1',
                  'condition':'night','evidence':[{'sourcePath':'sources/story.md','sourceRevisionSha256':rev['sha256'],
                  'startLine':1,'endLine':1,'quoteAnchor':quote,'quoteSha256':t.digest(quote.encode())}]}]}
 response={'schema':t.SCHEMA,'cards':[card]}
 cfg={'extractor':{'builtins':['codex']}}
 calls=[]
 try: b.stage_generation_response(root,{'schema':t.SCHEMA,'cards':[]},requested_sources=['sources/story.md'])
 except t.TopicalError as exc: assert exc.code=='empty_generation_response'
 else: raise AssertionError('empty response silently succeeded')
 empty=b.stage_generation_response(root,{'schema':t.SCHEMA,'cards':[]},requested_sources=['sources/story.md'],allow_empty=True)
 assert empty['status']=='no_candidates' and not (root/'topical-generations').exists()
 badhash=json.loads(json.dumps(response))
 badhash['cards'][0]['claims'][0]['evidence'][0]['quoteSha256']='0'*64
 try:b.stage_generation_response(root,badhash,requested_sources=['sources/story.md'])
 except t.TopicalError as exc:assert exc.code=='model_supplied_hash_mismatch'
 else:raise AssertionError('wrong model hash accepted')
 def runner(argv,payload,timeout,cwd):
  calls.append(payload['task'])
  return response,None,0
 try: b.run_generation_backend(root,['sources/story.md'],cfg,'codex',runner=runner)
 except t.TopicalError as exc: assert exc.code=='backend_execution_not_enabled'
 else: raise AssertionError('backend was not locked')
 assert not calls
 made=b.run_generation_backend(root,['sources/story.md'],cfg,'codex',allow_backend_execution=True,runner=runner)
 manifest=root/'topical-generations'/made['generationId']/'manifest.json'; prior=manifest.read_bytes()
 wrong={**response,'cards':[{**card,'claims':[{**card['claims'][0],
          'evidence':[{**card['claims'][0]['evidence'][0],'sourcePath':'sources/other.md'}]}]}]}
 try: b.stage_generation_response(root,wrong,requested_sources=['sources/story.md'])
 except t.TopicalError as exc: assert exc.code=='unrequested_source_reference'
 else: raise AssertionError('unrequested source accepted')
 try: b.stage_generation_response(root,{'schema':t.SCHEMA,'cards':[{**card,'claims':[]}]},
                                  requested_sources=['sources/story.md'])
 except t.TopicalError: pass
 else: raise AssertionError('invalid generation accepted')
 assert manifest.read_bytes()==prior
 assert len([p for p in (root/'topical-generations').iterdir() if p.is_dir()])==1
 print(json.dumps({'generated':made['status'],'calls':len(calls),'priorUnchanged':True}))
`);
  assert.deepEqual(result, { generated: 'generated_unverified', calls: 1, priorUnchanged: true });
});

test('empty backend response is an explicit failure with structured evidence preserved', () => {
  const result = python(`
import json,sys,tempfile
from pathlib import Path
sys.path.insert(0,'core')
import wiki_topical as t, wiki_topical_backend as b
with tempfile.TemporaryDirectory(prefix='qmd-topical-empty-') as temporary:
 root=Path(temporary).resolve();(root/t.MARKER).write_text('synthetic only\\n');(root/'sources').mkdir()
 (root/'sources/story.md').write_text('A durable fact exists.\\n')
 cfg={'extractor':{'builtins':['codex']}}
 calls=[]
 def runner(argv,payload,timeout,cwd):
  calls.append(True)
  return {'schema':t.SCHEMA,'cards':[]},None,0
 try:b.run_generation_backend(root,['sources/story.md'],cfg,'codex',allow_backend_execution=True,runner=runner)
 except t.TopicalError as exc:assert exc.code=='empty_generation_response'
 else:raise AssertionError('empty response was business success')
 try:b.run_generation_backend(root,['sources/story.md'],cfg,'codex',allow_backend_execution=True,runner=runner)
 except t.TopicalError as exc:assert exc.code=='generation_already_attempted'
 else:raise AssertionError('repeat backend invocation was allowed')
 assert len(calls)==1
 audit=list((root/'topical-backend-audit').glob('*.generation.json'))
 assert len(audit)==1 and json.loads(audit[0].read_text())['adapterResponse']['cards']==[]
 assert not (root/'topical-generations').exists()
 print(json.dumps({'error':'empty_generation_response','auditCount':len(audit),'generationCount':0}))
`);
  assert.deepEqual(result, { error: 'empty_generation_response', auditCount: 1, generationCount: 0 });
});

test('generation transport failure needs a new attempt ID and bounded budget', () => {
  const result = python(`
import json,sys,tempfile
from pathlib import Path
sys.path.insert(0,'core')
import wiki_topical as t, wiki_topical_backend as b
with tempfile.TemporaryDirectory(prefix='qmd-topical-retry-') as temporary:
 root=Path(temporary).resolve();(root/t.MARKER).write_text('synthetic only\\n');(root/'sources').mkdir()
 (root/'sources/story.md').write_text('A durable synthetic fact.\\n')
 cfg={'extractor':{'builtins':['codex']}};calls=[]
 def timeout(argv,payload,seconds,cwd):
  calls.append('timeout');return None,'timeout',124
 def empty(argv,payload,seconds,cwd):
  calls.append('empty');return {'schema':t.SCHEMA,'cards':[]},None,0
 def invoke(runner,**options):
  return b.run_generation_backend(root,['sources/story.md'],cfg,'codex',
      allow_backend_execution=True,runner=runner,**options)
 try:invoke(timeout)
 except t.TopicalError as exc:assert exc.code=='timeout'
 else:raise AssertionError('timeout accepted')
 try:invoke(empty)
 except t.TopicalError as exc:assert exc.code=='generation_already_attempted'
 else:raise AssertionError('same ID repeated call')
 assert calls==['timeout']
 try:invoke(empty,attempt_id='retry-1',max_attempts=2,max_cost_cents=10)
 except t.TopicalError as exc:assert exc.code=='backend_attempt_budget_exhausted'
 else:raise AssertionError('cost budget ignored')
 try:invoke(empty,attempt_id='retry-1',max_attempts=2,max_cost_cents=20)
 except t.TopicalError as exc:assert exc.code=='empty_generation_response'
 else:raise AssertionError('empty generation accepted')
 try:invoke(empty,attempt_id='retry-2',max_attempts=2,max_cost_cents=30)
 except t.TopicalError as exc:assert exc.code=='backend_attempt_budget_exhausted'
 else:raise AssertionError('unexpected third call')
 # Two calls are recorded; a third would require a separate explicit budget.
 assert calls==['timeout','empty']
 records=[json.loads(p.read_text()) for p in (root/'topical-backend-audit').glob('*.attempt.json')]
 states=sorted(x['state'] for x in records)
 for record in records:
  metrics=record['runtimeMetrics']
  assert metrics['wallMs']>=0 and metrics['childCpuMs']>=0
  assert metrics['childRssHighWaterMiB']>=metrics['childRssHighWaterBeforeMiB']
  assert metrics['rssMeaning']=='reaped_children_lifetime_high_water_not_per_call_peak'
 print(json.dumps({'calls':len(calls),'states':states}))
`);
  assert.deepEqual(result, { calls: 2, states: ['completed', 'failed'] });
});

test('single verification reuses completed response and bundle retries an uncertain call explicitly', () => {
  const result = python(`
import json,sys,tempfile
from pathlib import Path
sys.path.insert(0,'core')
import wiki_topical as t, wiki_topical_backend as b
with tempfile.TemporaryDirectory(prefix='qmd-topical-verify-retry-') as temporary:
 root=Path(temporary).resolve();(root/t.MARKER).write_text('synthetic only\\n');(root/'sources').mkdir()
 quote='The synthetic bell is silent.'
 (root/'sources/story.md').write_text(quote+'\\n')
 rev=t.source_snapshot(root,'sources/story.md',{})[0]
 span={'sourcePath':'sources/story.md','sourceRevisionSha256':rev['sha256'],
       'startLine':1,'endLine':1,'quoteAnchor':quote,'quoteSha256':t.digest(quote.encode())}
 card={'cardId':'silent-bell','title':'Silent bell','category':'world-rule','details':'',
       'claims':[{'claimId':'silent','statement':quote,'state':'actual','timeScope':'chapter-1',
                  'condition':'','evidence':[span]}]}
 gid=b.stage_generation_response(root,{'schema':t.SCHEMA,'cards':[card]})['generationId']
 check={'claimId':'silent','sourcePath':'sources/story.md','quoteSha256':span['quoteSha256'],
        'quoteAnchor':quote,'supported':True}
 response={'verdict':'pass','checks':[check],'reasons':[]}
 cfg={'extractor':{'builtins':['codex']},'verify':{'builtins':['codex'],'crossEngine':'off'}}
 calls=[]
 def single(argv,payload,seconds,cwd):
  calls.append('single');return response,None,0
 first=b.run_verification_backend(root,gid,'silent-bell',cfg,'codex',allow_backend_execution=True,runner=single)
 second=b.run_verification_backend(root,gid,'silent-bell',cfg,'codex',allow_backend_execution=True,runner=single)
 assert first==second and calls==['single']
 try:b._attempt(root,'verification',b.verification_payload(root,gid,'silent-bell'),
                ['other-backend'],120,single,'initial',1,10,10)
 except t.TopicalError as exc:assert exc.code=='backend_attempt_transport_changed'
 else:raise AssertionError('different backend reused an old response')
 def uncertain(argv,payload,seconds,cwd):
  calls.append('uncertain');raise RuntimeError('lost result')
 try:b.run_verification_bundle_backend(root,gid,['silent-bell'],cfg,'codex',
                                       allow_backend_execution=True,runner=uncertain)
 except RuntimeError:pass
 else:raise AssertionError('uncertain call accepted')
 try:b.run_verification_bundle_backend(root,gid,['silent-bell'],cfg,'codex',
                                       allow_backend_execution=True,runner=uncertain)
 except t.TopicalError as exc:assert exc.code=='bundle_verification_already_attempted'
 else:raise AssertionError('uncertain call repeated')
 assert calls==['single','uncertain']
 def retry(argv,payload,seconds,cwd):
  calls.append('retry');return {'cards':[{'cardId':'silent-bell',**response}]},None,0
 bundle=b.run_verification_bundle_backend(root,gid,['silent-bell'],cfg,'codex',
          allow_backend_execution=True,runner=retry,attempt_id='retry-1',max_attempts=2,max_cost_cents=20)
 again=b.run_verification_bundle_backend(root,gid,['silent-bell'],cfg,'codex',
          allow_backend_execution=True,runner=retry,attempt_id='retry-1',max_attempts=2,max_cost_cents=20)
 assert bundle==again and bundle[0]['status']=='backend_pass' and calls==['single','uncertain','retry']
 print(json.dumps({'calls':calls,'singleReused':first==second,'bundleReused':bundle==again}))
`);
  assert.deepEqual(result, { calls: ['single', 'uncertain', 'retry'], singleReused: true, bundleReused: true });
});

test('existing Codex adapter routes topical tasks and extracts JSON through synthetic stdout only', () => {
  const result = python(`
import io,json,sys
from contextlib import redirect_stdout
from unittest.mock import patch
sys.path.insert(0,'core/extractors')
import lib
generation={'task':'generate_topical_candidates_sandbox_only','outputSchema':'qmd-topical-sandbox-v1',
            'sources':[{'path':'sources/story.md','sourceRevisionSha256':'a'*64,'numberedContent':'1: A safe line.'}],
            'rules':[],'cardShape':{},'leadBudgetChars':600}
verification={'task':'verify_topical_claims_sandbox_only','card':{'claims':[]},'sources':generation['sources']}
outputs=[json.dumps({'schema':'qmd-topical-sandbox-v1','cards':[]}),
         json.dumps({'verdict':'inconclusive','checks':[],'reasons':['uncertain']})]
prompts=[]
def fake_run(cmd,timeout):
 prompts.append(cmd[-1]); return outputs.pop(0),0,''
captured=[]
with patch.object(lib,'resolve_bin',return_value='synthetic-cli'),patch.object(lib,'run_isolated_detailed',side_effect=fake_run):
 for payload in (generation,verification):
  with patch.object(lib,'read_payload',return_value=payload),redirect_stdout(io.StringIO()) as out:
   assert lib.run_adapter('codex','QMD_EXTRACTOR_CODEX_BIN',lambda binary,prompt,effort:[binary,prompt],engine='codex')==0
   captured.append(json.loads(out.getvalue()))
assert len(prompts)==2 and 'SOURCE SNAPSHOTS:' in prompts[0] and 'Adversarially check' in prompts[1]
print(json.dumps({'generationSchema':captured[0]['schema'],'verificationVerdict':captured[1]['verdict'],
                  'prompts':len(prompts)}))
`);
  assert.deepEqual(result, { generationSchema: 'qmd-topical-sandbox-v1',
    verificationVerdict: 'inconclusive', prompts: 2 });
});

test('topical generation prompt states optional details and byte exact multiline evidence', () => {
  const result = python(`
import json,sys,tempfile
from pathlib import Path
sys.path.insert(0,'core');sys.path.insert(0,'core/extractors')
import wiki_topical_experiment as e,lib
with tempfile.TemporaryDirectory(prefix='qmd-topical-contract-') as temporary:
 root=Path(temporary).resolve();(root/'.qmd-topical-sandbox').write_text('synthetic only\\n')
 (root/'sources').mkdir();(root/'sources/story.md').write_text('First line.\\nSecond line.\\n')
 prompt=lib.build_topical_generation_prompt(e.generation_contract(root,['sources/story.md']))
 assert 'details is optional; omission means the empty string' in prompt
 assert 'quoteAnchor may contain source line breaks' in prompt
 assert '1: First line.' in prompt and '2: Second line.' in prompt
 print(json.dumps('ok'))
`);
  assert.equal(result, 'ok');
});

test('existing worker subprocess transport accepts synthetic topical JSON without any host CLI', () => {
  const result = python(`
import json,sys,tempfile
from pathlib import Path
sys.path.insert(0,'core')
import wiki_topical as t, wiki_topical_backend as b
with tempfile.TemporaryDirectory(prefix='qmd-topical-transport-') as temporary:
 root=Path(temporary).resolve()
 (root/t.MARKER).write_text('synthetic only\\n'); (root/'sources').mkdir()
 quote='The bronze tower stands beside the lake.'
 (root/'sources/story.md').write_text(quote+'\\n')
 rev=t.source_snapshot(root,'sources/story.md',{})[0]
 span={'sourcePath':'sources/story.md','sourceRevisionSha256':rev['sha256'],'startLine':1,'endLine':1,
       'quoteAnchor':quote,'quoteSha256':t.digest(quote.encode())}
 card={'cardId':'bronze-tower','title':'Bronze tower','category':'entity','details':'synthetic',
       'claims':[{'claimId':'location','statement':quote,'state':'observation','timeScope':'chapter-1',
                  'condition':'','evidence':[span]}]}
 model_card=json.loads(json.dumps(card))
 model_span=model_card['claims'][0]['evidence'][0]
 del model_span['sourceRevisionSha256'];del model_span['quoteSha256']
 generation={'schema':t.SCHEMA,'cards':[model_card]}
 verdict={'verdict':'pass','checks':[{'claimId':'location','sourcePath':'sources/story.md',
          'quoteSha256':span['quoteSha256'],'quoteAnchor':quote,'supported':True}],'reasons':[]}
 stub=root/'stub.py'
 stub.write_text('import json,sys\\n'
                 'payload=json.load(sys.stdin)\\n'
                 'data='+repr({'generate_topical_candidates_sandbox_only':generation,
                               'verify_topical_claims_sandbox_only':verdict})+'\\n'
                 'print(json.dumps(data[payload["task"]]))\\n')
 cfg={'extractor':{'backends':{'codex':[sys.executable,str(stub)]}},
      'verify':{'crossEngine':'off'}}
 made=b.run_generation_backend(root,['sources/story.md'],cfg,'codex',allow_backend_execution=True)
 staged=json.loads((root/'topical-generations'/made['generationId']/'cards/bronze-tower.evidence.json').read_text())
 assert staged['claims'][0]['evidence'][0]['quoteSha256']==span['quoteSha256']
 assert staged['claims'][0]['evidence'][0]['sourceRevisionSha256']==rev['sha256']
 checked=b.run_verification_backend(root,made['generationId'],'bronze-tower',cfg,'codex',
                                   allow_backend_execution=True)
 assert checked['status']=='backend_pass'
 assert (root/'topical-attestations/backend'/made['generationId']/'bronze-tower.json').is_file()
 print(json.dumps({'generation':made['status'],'verification':checked['status']}))
`);
  assert.deepEqual(result, { generation: 'generated_unverified', verification: 'backend_pass' });
});

test('one synthetic verifier response adjudicates every card in a source bundle', () => {
  const result = python(`
import json,sys,tempfile
from pathlib import Path
sys.path.insert(0,'core')
import wiki_topical as t, wiki_topical_backend as b
with tempfile.TemporaryDirectory(prefix='qmd-topical-bundle-') as temporary:
 root=Path(temporary).resolve(); (root/t.MARKER).write_text('synthetic only\\n');(root/'sources').mkdir()
 lines=['The blue key opens the gate.','The red bell stays silent.']
 (root/'sources/story.md').write_text('\\n'.join(lines)+'\\n')
 rev=t.source_snapshot(root,'sources/story.md',{})[0]
 cards=[];checks=[]
 for n,(identifier,line) in enumerate(zip(('blue-key','red-bell'),lines),1):
  span={'sourcePath':'sources/story.md','sourceRevisionSha256':rev['sha256'],'startLine':n,'endLine':n,
        'quoteAnchor':line,'quoteSha256':t.digest(line.encode())}
  cards.append({'cardId':identifier,'title':identifier,'category':'entity','details':'synthetic',
                'claims':[{'claimId':identifier+'-claim','statement':line,'state':'actual',
                           'timeScope':'chapter-1','condition':'','evidence':[span]}]})
  checks.append({'cardId':identifier,'verdict':'pass' if n==1 else 'inconclusive',
                 'checks':[{'claimId':identifier+'-claim','sourcePath':'sources/story.md',
                            'quoteSha256':span['quoteSha256'],'quoteAnchor':line,
                            'supported':True if n==1 else None}], 'reasons':[]})
 made=b.stage_generation_response(root,{'schema':t.SCHEMA,'cards':cards})
 preview=b.verification_bundle_payload(root,made['generationId'],['blue-key','red-bell'])
 assert preview['task']==b.BUNDLE_VERIFY_TASK and len(preview['cards'])==2 and len(preview['sources'])==1
 cfg={'extractor':{'builtins':['codex']},'verify':{'builtins':['codex'],'crossEngine':'off'}}
 calls=[]
 def runner(argv,payload,timeout,cwd):
  calls.append(payload)
  return {'cards':checks},None,0
 records=b.run_verification_bundle_backend(root,made['generationId'],['blue-key','red-bell'],cfg,'codex',
                                           allow_backend_execution=True,runner=runner)
 assert len(calls)==1 and calls[0]==preview
 directory=root/'topical-attestations/backend'/made['generationId']
 assert (directory/'blue-key.json').is_file() and not (directory/'red-bell.json').exists()
 print(json.dumps({'calls':len(calls),'statuses':[r['status'] for r in records],
                   'publishedPasses':len(list(directory.glob('*.json')))}))
`);
  assert.deepEqual(result, { calls: 1, statuses: ['backend_pass', 'backend_inconclusive'], publishedPasses: 1 });
});

test('replays exact empty pilot response as explicit business failure and retains raw adapter stdout', () => {
  const result = python(`
import hashlib,io,json,os,sys,tempfile
from pathlib import Path
from contextlib import redirect_stdout
from unittest.mock import patch
sys.path[:0]=['core','core/extractors']
import lib,wiki_topical as t,wiki_topical_backend as b
raw=Path('test/fixtures/topical-pilot-empty-response.json').read_text()
assert hashlib.sha256(raw.encode()).hexdigest()=='aea9c8493e2089adb3826499a6f51790a64b47468b8f4e26ae20ae0e0055d63c'
assert lib.extract_topical_bundle(raw)=={'schema':t.SCHEMA,'cards':[]}
assert lib.extract_topical_bundle('{"response":{"schema":"qmd-topical-sandbox-v1","cards":[]}}')=={}
with tempfile.TemporaryDirectory(prefix='qmd-topical-replay-') as temporary:
 root=Path(temporary).resolve();(root/t.MARKER).write_text('synthetic only\\n')
 original=Path.cwd();os.chdir(root)
 try:
  payload={'task':'generate_topical_candidates_sandbox_only','sources':[],'rules':[]}
  with patch.object(lib,'read_payload',return_value=payload),patch.object(lib,'resolve_bin',return_value='local-stub'),\\
       patch.object(lib,'run_isolated_detailed',return_value=(raw,0,'')),redirect_stdout(io.StringIO()) as out:
   assert lib.run_adapter('codex','QMD_EXTRACTOR_CODEX_BIN',lambda binary,prompt,effort:[binary,prompt],engine='codex')==0
   structured=json.loads(out.getvalue())
  assert structured['cards']==[]
  try:b.stage_generation_response(root,structured)
  except t.TopicalError as exc:assert exc.code=='empty_generation_response'
  else:raise AssertionError('empty pilot output silently accepted')
  files=list((root/'topical-backend-audit').glob('*.raw-stdout.txt'))
  assert len(files)==1 and files[0].read_text()==raw
  assert len(list((root/'topical-backend-audit').glob('*.raw-stderr.txt')))==1
  assert len(list((root/'topical-backend-audit').glob('*.request-prompt.txt')))==1
  metadata=json.loads(next((root/'topical-backend-audit').glob('*.raw-metadata.json')).read_text())
  assert metadata['rawStdoutSha256']==hashlib.sha256(raw.encode()).hexdigest()
  assert not metadata['stdoutRedacted'] and not metadata['stderrRedacted'] and not metadata['promptRedacted']
 finally:os.chdir(original)
print(json.dumps({'parsedCards':0,'businessError':'empty_generation_response','rawCaptured':True}))
`);
  assert.deepEqual(result, { parsedCards: 0, businessError: 'empty_generation_response', rawCaptured: true });
});

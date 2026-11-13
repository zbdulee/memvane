import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';

function run(py) {
  return JSON.parse(execFileSync('python3', ['-c', py], {
    cwd: process.cwd(), encoding: 'utf8', env: { ...process.env, PYTHONPATH: '.', QMD_SANDBOX: '' },
  }));
}

test('generation route propagates generation effort and keeps extractor audit metadata on the candidate', () => {
  const out = run(`
import json, shutil, sys, tempfile
from pathlib import Path
import memvane.settings.config as config; import memvane.wiki.compile.wiki_compile_worker as w
root = Path(tempfile.mkdtemp(prefix='effort-generation-')).resolve()
try:
    (root / 'docs').mkdir()
    (root / '.auto-context' / 'wiki').mkdir(parents=True)
    (root / 'docs' / 'source.md').write_text('# source\\n', encoding='utf-8')
    cfg = config.normalize_config({'collections':['docs','wiki'], 'collectionPaths':{'docs':'docs','wiki':'.auto-context/wiki'}, 'collectionRoles':{'docs':'raw','wiki':'wiki'}, 'wikiPath':'.auto-context/wiki', 'compile': {'mode':'auto-wiki', 'extractor': {'backends': {'claude': ['python3','stub']}}, 'reasoningEffort': {'generation':'low','verify':'medium','semanticDedup':'medium','engines': {'claude': {'generation':'high'}}}}})
    seen = {}
    def fake_run(argv, payload, timeout, project):
        seen['payload'] = payload
        return {'candidates':[{'title':'T','summary':'S','suggestedType':'concept','confidence':'high'}], '_qmd': {'reasoningEffort': {'requested':'high','applied':'high','status':'applied','reason':'capability_flag'}}}, None, 0
    def fake_compile(project, candidate):
        seen['candidate'] = candidate
        return {'action':'candidate'}
    old_run, old_compile = w.run_extractor, w.compile_candidate
    w.run_extractor, w.compile_candidate = fake_run, fake_compile
    try:
        w.process_job(root, cfg, cfg['compile'], {'engine':'claude','trigger':'manual','source':{'path':'docs/source.md','collection':'docs'}})
    finally:
        w.run_extractor, w.compile_candidate = old_run, old_compile
    print(json.dumps({'requested': seen['payload']['_qmd']['reasoningEffort']['requested'], 'candidateQmd': seen['candidate']['_qmd']}))
finally:
    shutil.rmtree(root, ignore_errors=True)
`);
  assert.equal(out.requested, 'high');
  assert.equal(out.candidateQmd.reasoningEffort.status, 'unsupported');
});

test('verify route propagates verify effort and persists only bounded audit metadata', () => {
  const out = run(`
import json, os, shutil, tempfile
from pathlib import Path
import memvane.settings.config as config; import memvane.wiki.compile.wiki_verify_worker as v
root = Path(tempfile.mkdtemp(prefix='effort-verify-')).resolve()
try:
    os.environ['QMD_DIRTY_QUEUE'] = str(root / 'dirty-queue')
    (root / '.auto-context' / 'wiki' / 'concepts').mkdir(parents=True)
    (root / 'docs').mkdir()
    (root / 'docs' / 'source.md').write_text('# source\\nDurable claim.\\n', encoding='utf-8')
    card = root / '.auto-context' / 'wiki' / 'concepts' / 'test.md'
    card.write_text('---\\ntitle: Test\\nstatus: generated\\ncreatedBy: qmd-auto-context\\nreviewed: false\\n---\\n<!-- qmd:auto:start id="main" sourceHash="h" -->\\n## Summary\\nClaim.\\n<!-- qmd:auto:end -->\\n', encoding='utf-8')
    cfg = config.normalize_config({'collections':['docs','wiki'], 'collectionPaths':{'docs':'docs','wiki':'.auto-context/wiki'}, 'collectionRoles':{'docs':'raw','wiki':'wiki'}, 'wikiPath':'.auto-context/wiki', 'compile': {'mode':'auto-wiki', 'extractor': {'backends': {'claude': ['python3','stub']}}, 'reasoningEffort': {'generation':'low','verify':'medium','semanticDedup':'medium','engines': {'claude': {'verify':'high'}}}, 'verify': {'enabled':True, 'timeout':30}}})
    seen = {}
    def fake_run(argv, payload, timeout, project):
        seen['payload'] = payload
        return {'verdict':'pass','claims':[{'claim': 'c', 'supported': True, 'quote': 'q', 'sourcePath': 'docs/source.md'}],'reasons':[], '_qmd': {'reasoningEffort': {'requested':'high','applied':'high','status':'applied','reason':'capability_flag'}}}, None, 0
    old = v.wcw.run_extractor
    v.wcw.run_extractor = fake_run
    try:
        v.process_verify_job(root, cfg, cfg['compile'], cfg['compile']['verify'], {'targetPath':'.auto-context/wiki/concepts/test.md','sources':[{'kind':'file','path':'docs/source.md','collection':'docs'}],'sourceHash':'h','engine':'claude'}, root / 'verify-log.jsonl')
    finally:
        v.wcw.run_extractor = old
    row = json.loads((root / 'verify-log.jsonl').read_text(encoding='utf-8').strip())
    print(json.dumps({'requested': seen['payload']['_qmd']['reasoningEffort']['requested'], 'audit': row['_qmd'], 'hasBody': 'Claim.' in json.dumps(row)}))
finally:
    shutil.rmtree(root, ignore_errors=True)
`);
  assert.equal(out.requested, 'high');
  assert.equal(out.audit.reasoningEffort.status, 'unsupported');
  assert.equal(out.hasBody, false);
});

test('semantic dedup route propagates semanticDedup effort and exposes audit metadata in diagnostics', () => {
  const out = run(`
import json, shutil, tempfile
from pathlib import Path
import memvane.settings.config as config; import memvane.wiki.dedup.wiki_dedup_judge as j
root = Path(tempfile.mkdtemp(prefix='effort-dedup-')).resolve()
try:
    cfg = config.normalize_config({'compile': {'mode':'auto-wiki', 'extractor': {'backends': {'claude': ['python3','stub']}}, 'reasoningEffort': {'generation':'low','verify':'medium','semanticDedup':'low','engines': {'claude': {'semanticDedup':'xhigh'}}}, 'semanticDedup': {'judge': {'enabled':True}}}})['compile']
    seen = {}
    def fake_run(argv, payload, timeout, project):
        seen['payload'] = payload
        return {'verdict':'distinct','reason':'different','_qmd': {'reasoningEffort': {'requested':'xhigh','applied':'xhigh','status':'applied','reason':'capability_flag'}}}, None, 0
    import memvane.wiki.compile.wiki_compile_worker as w
    old = w.run_extractor
    w.run_extractor = fake_run
    try:
        verdict, info = j.judge_pair(root, cfg, {'path':'a','content':'A'}, {'path':'b','content':'B'}, engine='claude')
    finally:
        w.run_extractor = old
    print(json.dumps({'verdict': verdict, 'requested': seen['payload']['_qmd']['reasoningEffort']['requested'], 'audit': info['_qmd'], 'diagnostic': j.diagnostics(info)}))
finally:
    shutil.rmtree(root, ignore_errors=True)
`);
  assert.equal(out.verdict, 'distinct');
  assert.equal(out.requested, 'xhigh');
  assert.equal(out.audit.reasoningEffort.status, 'unsupported');
  assert.equal(out.diagnostic._qmd.reasoningEffort.status, 'unsupported');
});

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';

function run(py) {
  return JSON.parse(execFileSync('python3', ['-c', py], {
    cwd: process.cwd(), encoding: 'utf8', env: { ...process.env, PYTHONPATH: '.', QMD_SANDBOX: '' },
  }));
}

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

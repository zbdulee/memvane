import test from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';

test('pending cleanup preserves claimed work and retry identity', () => {
  const result=spawnSync('python3',['-B','test/fixtures/pending-cleanup-recovery-checks.py'],
    {encoding:'utf8',timeout:60000,env:{...process.env,PYTHONDONTWRITEBYTECODE:'1'}});
  if(result.stdout)process.stdout.write(result.stdout);
  assert.ifError(result.error);assert.equal(result.status,0,result.stdout+result.stderr);
  assert.match(result.stderr,/Ran 12 tests/);assert.match(result.stderr,/\nOK\b/);
});

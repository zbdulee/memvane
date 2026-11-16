import {test} from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {fixtureEnv,fixtureHome,removeTemp} from '../support/temp.mjs';
test('legacy reattestation through run-hook and sync owner', {timeout:300000}, () => {
  const fixture = fixtureHome('mvr');
  try {
    const r=spawnSync('python3',['-B','memvane/provider/tests/integration/reattest_hook_e2e.py'],
      {cwd:process.cwd(),encoding:'utf8',timeout:290000,env:fixtureEnv(fixture)});
    process.stdout.write(r.stdout||''); process.stderr.write(r.stderr||'');
    assert.equal(r.signal,null); assert.equal(r.status,0);
  } finally { removeTemp(fixture.home); }
});

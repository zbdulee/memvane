import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {existsSync} from 'node:fs';
test('shared QMD endpoint uses actual localhost bind, ownership and bounded collision recovery',()=>{
 assert.ok(existsSync('core/qmd_endpoint.py'),'missing shared endpoint coordinator');
 const p=spawnSync('python3',['test/fixtures/qmd-endpoint-checks.py'],{
  encoding:'utf8',timeout:45000,env:{...process.env,QMD_RECALL_LOG:''}});
 assert.equal(p.status,0,p.stdout+p.stderr);
 const cases=Number(p.stderr.match(/Ran (\d+) tests/)?.[1]);
 assert.ok(cases>=34,'expected endpoint identity, port policy and IPv4/IPv6 race cases');
 console.info(`endpoint synthetic cases: ${cases}`);
});

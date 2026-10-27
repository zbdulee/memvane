import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
test('required Laya readiness is distinct from usable QMD and learning opt-out',()=>{
 const p=spawnSync('python3',['test/fixtures/setup-component-readiness-checks.py'],{encoding:'utf8',timeout:15000,env:{...process.env,QMD_RECALL_LOG:'',PYTHONDONTWRITEBYTECODE:'1'}});
 assert.equal(p.status,0,p.stdout+p.stderr);
 assert.match(p.stderr,/Ran 20 tests/);
});

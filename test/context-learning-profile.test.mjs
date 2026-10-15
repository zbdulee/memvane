import {test} from 'node:test';
import {execFileSync} from 'node:child_process';
test('explicit local profiles preserve existing false settings and exact scope',()=>{
 execFileSync('python3',['test/fixtures/context-learning-profile-checks.py'],{encoding:'utf8',timeout:30000,
  env:{...process.env,PYTHONPATH:'core',PYTHONDONTWRITEBYTECODE:'1',QMD_RECALL_LOG:''}});
});

import {test} from 'node:test';
import {execFileSync} from 'node:child_process';
test('scoped publication preserves incumbent and recovers every CAS journal boundary',()=>{
 execFileSync('python3',['test/fixtures/context-learning-candidate-apply-checks.py'],{encoding:'utf8',timeout:30000,
  env:{...process.env,PYTHONPATH:'core',PYTHONDONTWRITEBYTECODE:'1',QMD_RECALL_LOG:''}});
});

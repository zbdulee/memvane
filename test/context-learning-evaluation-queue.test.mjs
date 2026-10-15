import {test} from 'node:test';
import {execFileSync} from 'node:child_process';
test('evaluation-only work survives deferral and keeps a private child lease',()=>{
 execFileSync('python3',['test/fixtures/context-learning-evaluation-queue-checks.py'],{encoding:'utf8',timeout:30000,
  env:{...process.env,PYTHONPATH:'core',PYTHONDONTWRITEBYTECODE:'1',QMD_RECALL_LOG:''}});
});

import {test} from 'node:test';
import {execFileSync} from 'node:child_process';
test('durable learning consumes semantic evidence once with fenced recovery',()=>{
  execFileSync('python3',['test/fixtures/context-learning-learning-queue-checks.py'],{
    encoding:'utf8',timeout:30000,env:{...process.env,PYTHONPATH:'core',PYTHONDONTWRITEBYTECODE:'1',QMD_RECALL_LOG:''}});
});

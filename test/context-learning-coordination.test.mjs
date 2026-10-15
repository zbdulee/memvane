import {test} from 'node:test';
import {execFileSync} from 'node:child_process';
test('inference priority and resumable learning use private kernel leases',()=>{
 execFileSync('python3',['test/fixtures/context-learning-coordination-checks.py'],{
  encoding:'utf8',timeout:30000,env:{...process.env,PYTHONPATH:'core',PYTHONDONTWRITEBYTECODE:'1',QMD_RECALL_LOG:''}});
});

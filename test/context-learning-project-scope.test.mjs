import {test} from 'node:test';
import {execFileSync} from 'node:child_process';
test('general project scope binds installation and disjoint cohorts without widening legacy grants',()=>{
 execFileSync('python3',['test/fixtures/context-learning-project-scope-checks.py'],{encoding:'utf8',timeout:45000,
  env:{...process.env,PYTHONPATH:'core',PYTHONDONTWRITEBYTECODE:'1',QMD_RECALL_LOG:''}});
});

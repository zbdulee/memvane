import {test} from 'node:test';
import {execFileSync} from 'node:child_process';
test('project learning recovery holds the shared settings writer lock across CAS and restore',()=>{
 execFileSync('python3',['test/fixtures/project-learning-recovery-lock-checks.py'],{encoding:'utf8',timeout:15000,
  env:{...process.env,PYTHONPATH:'core',PYTHONDONTWRITEBYTECODE:'1',QMD_RECALL_LOG:''}});
});

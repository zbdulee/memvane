import {test} from 'node:test';
import {execFileSync} from 'node:child_process';
test('project learning setup reviews, atomically enables and safely recovers owner configuration',()=>{
 execFileSync('python3',['test/fixtures/project-learning-setup-checks.py'],{encoding:'utf8',timeout:60000,
  env:{...process.env,PYTHONPATH:'core',PYTHONDONTWRITEBYTECODE:'1',QMD_RECALL_LOG:''}});
});

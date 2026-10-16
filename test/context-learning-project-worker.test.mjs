import {test} from 'node:test';
import {execFileSync} from 'node:child_process';
test('general project worker preserves flags, bounded gold and inference-first local training',()=>{
  execFileSync('python3',['-B','test/fixtures/context-learning-project-worker-checks.py'],{
    cwd:process.cwd(),encoding:'utf8',env:{...process.env,QMD_RECALL_LOG:'',PYTHONDONTWRITEBYTECODE:'1'}});
});

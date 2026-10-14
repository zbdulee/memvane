import {test} from 'node:test';
import {execFileSync} from 'node:child_process';
test('explicit calibration retains relative reserve, floors and native pressure gates',()=>{
 execFileSync('python3',['test/fixtures/context-learning-calibration-checks.py'],{
  encoding:'utf8',timeout:10000,env:{...process.env,PYTHONDONTWRITEBYTECODE:'1',QMD_RECALL_LOG:''}});
});

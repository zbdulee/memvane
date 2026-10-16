import {test} from 'node:test';
import {execFileSync} from 'node:child_process';

test('runtime resource coordination retains durable work and foreground priority',()=>{
  execFileSync('python3',['-B','-m','unittest','discover','-s','test','-p','runtime_*test.py'],{
    encoding:'utf8',timeout:15000,
    env:{...process.env,PYTHONDONTWRITEBYTECODE:'1',QMD_RECALL_LOG:''},
  });
});

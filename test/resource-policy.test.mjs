import {test} from 'node:test';
import {execFileSync} from 'node:child_process';

for (const [folder, pattern] of [['tests/guard','development_guard_test.py'],['test','platform_resource_policy_test.py'],['test','calibration_policy_test.py']]) {
  test(`resource policy: ${pattern}`,()=>{
    execFileSync('python3',['-B','-m','unittest','discover','-s',folder,'-p',pattern],{
      encoding:'utf8',timeout:15000,
      env:{...process.env,PYTHONDONTWRITEBYTECODE:'1',QMD_RECALL_LOG:''},
    });
  });
}

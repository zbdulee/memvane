import {test} from 'node:test';
import {execFileSync} from 'node:child_process';
test('adaptive memory parsing, compatibility, microbatch admission and durable defer', () => {
 execFileSync('python3',['test/fixtures/context-learning-adaptive-memory-checks.py'],{
  encoding:'utf8',timeout:10000,
  env:{...process.env,PYTHONDONTWRITEBYTECODE:'1',QMD_RECALL_LOG:''}});
});

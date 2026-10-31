import {test} from 'node:test';
import {execFileSync} from 'node:child_process';

test('recall latency: selector warm-up, cold-service trigger, exact vector math', () => {
  execFileSync('python3', ['test/fixtures/context-learning-selector-warm-checks.py'], {
    encoding: 'utf8', timeout: 60000,
    env: {...process.env, QMD_RECALL_LOG: '', PYTHONDONTWRITEBYTECODE: '1'},
  });
});

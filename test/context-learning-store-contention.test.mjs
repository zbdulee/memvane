import test from 'node:test';
import {execFileSync} from 'node:child_process';

test('learning store waits for short competing transactions without stalling the prompt hook', () => {
  execFileSync('python3', ['test/fixtures/context-learning-store-contention-checks.py'], {
    env: {...process.env, PYTHONPATH: 'core', QMD_RECALL_LOG: '', PYTHONDONTWRITEBYTECODE: '1'},
    encoding: 'utf8',
  });
});

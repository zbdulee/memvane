import {test} from 'node:test';
import {execFileSync} from 'node:child_process';

test('resource recovery latch releases a busy but admissible machine after a longer window', () => {
  execFileSync('python3', ['test/fixtures/context-learning-recovery-latch-checks.py'], {
    encoding: 'utf8', timeout: 60000,
    env: {...process.env, QMD_RECALL_LOG: '', PYTHONDONTWRITEBYTECODE: '1'},
  });
});

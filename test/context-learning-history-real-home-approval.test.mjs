import test from 'node:test';
import {execFileSync} from 'node:child_process';

test('real-account history activation needs an approval bound to one exact switch', () => {
  execFileSync('python3', ['test/fixtures/context-learning-history-real-home-approval-checks.py'], {
    env: {...process.env, PYTHONPATH: 'core', QMD_RECALL_LOG: '',
          PYTHONDONTWRITEBYTECODE: '1'},
    encoding: 'utf8',
  });
});

import test from 'node:test';
import {execFileSync} from 'node:child_process';

test('gold receipt stages distinguish pre-send, unknown, launched, and committed', () => {
  execFileSync('python3', ['test/fixtures/context-learning-gold-receipt-checks.py'], {
    env: {...process.env, PYTHONPATH: 'core', QMD_RECALL_LOG: '', PYTHONDONTWRITEBYTECODE: '1'},
    encoding: 'utf8',
  });
});

import test from 'node:test';
import {execFileSync} from 'node:child_process';

test('learning-ledger readers follow the active history selection', () => {
  execFileSync('python3', ['test/fixtures/context-learning-history-readers-checks.py'], {
    env: {...process.env, PYTHONPATH: 'core', QMD_RECALL_LOG: '', PYTHONDONTWRITEBYTECODE: '1'},
    encoding: 'utf8',
  });
});

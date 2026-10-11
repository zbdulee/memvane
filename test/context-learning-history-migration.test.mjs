import test from 'node:test';
import {execFileSync} from 'node:child_process';

test('plugin history preparation preserves receipts and detects legacy drift', () => {
  execFileSync('python3', ['test/fixtures/context-learning-history-migration-checks.py'], {
    env: {...process.env, PYTHONPATH: 'core', QMD_RECALL_LOG: ''},
    encoding: 'utf8',
  });
});

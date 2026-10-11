import test from 'node:test';
import {execFileSync} from 'node:child_process';

test('coordinated history activation refuses stale hosts and preserves rollback safety', () => {
  execFileSync('python3', ['test/fixtures/context-learning-history-activation-checks.py'], {
    env: {...process.env, PYTHONPATH: 'core', QMD_RECALL_LOG: '', PYTHONDONTWRITEBYTECODE: '1'},
    encoding: 'utf8',
  });
});

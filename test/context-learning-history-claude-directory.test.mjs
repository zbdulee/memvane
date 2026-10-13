import test from 'node:test';
import {execFileSync} from 'node:child_process';

test('cutover verifies and acknowledges the folder Claude runs for a directory marketplace', () => {
  execFileSync('python3', ['test/fixtures/context-learning-history-claude-directory-checks.py'], {
    env: {...process.env, PYTHONPATH: 'core', QMD_RECALL_LOG: '', PYTHONDONTWRITEBYTECODE: '1'},
    encoding: 'utf8',
  });
});

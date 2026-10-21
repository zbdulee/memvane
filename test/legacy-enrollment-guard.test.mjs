import {test} from 'node:test';
import {execFileSync} from 'node:child_process';

test('selected legacy store refuses new unproved owner before QMD writes', () => {
  execFileSync('python3', ['test/fixtures/legacy-enrollment-guard-checks.py'], {
    encoding: 'utf8', timeout: 180000,
    env: {...process.env, QMD_RECALL_LOG: '', PYTHONDONTWRITEBYTECODE: '1'},
  });
});

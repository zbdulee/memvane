import {test} from 'node:test';
import {execFileSync} from 'node:child_process';

test('explicit new-machine setup proves a private store before separate activation', () => {
  execFileSync('python3', ['test/fixtures/shared-qmd-setup-checks.py'], {
    encoding: 'utf8', timeout: 180000,
    env: {...process.env, QMD_RECALL_LOG: '', PYTHONDONTWRITEBYTECODE: '1'},
  });
});

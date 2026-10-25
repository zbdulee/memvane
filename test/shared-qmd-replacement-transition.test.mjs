import {test} from 'node:test';
import {execFileSync} from 'node:child_process';

test('inactive QMD replacement evidence keeps the selected store unchanged', () => {
  execFileSync('python3', ['test/fixtures/shared-qmd-replacement-transition-checks.py'], {
    encoding: 'utf8', timeout: 180000,
    env: {...process.env, QMD_RECALL_LOG: '', PYTHONDONTWRITEBYTECODE: '1'},
  });
});

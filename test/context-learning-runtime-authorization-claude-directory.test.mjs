import {test} from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';

test('execution grant accepts the folder Claude runs for a directory marketplace', () => {
  const raw = execFileSync('python3',
    ['test/fixtures/context-learning-runtime-authorization-claude-directory-checks.py'], {
      env: {...process.env, PYTHONPATH: 'core', QMD_RECALL_LOG: '',
        PYTHONDONTWRITEBYTECODE: '1'},
      encoding: 'utf8', timeout: 20000,
    });
  assert.equal(JSON.parse(raw).claude_executed_folder, true);
});

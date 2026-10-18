import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';

test('private QMD candidate config is proved before synthetic publication and ACK', () => {
  const p = spawnSync('python3', ['test/fixtures/shadow-qmd-adapter-checks.py'], {
    encoding: 'utf8', timeout: 30000,
    env: {...process.env, PYTHONDONTWRITEBYTECODE: '1', QMD_RECALL_LOG: ''}
  });
  assert.equal(p.status, 0, p.stdout + p.stderr);
  assert.match(p.stderr, /Ran 7 tests/);
});

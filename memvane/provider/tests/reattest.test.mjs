import {test} from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';

test('reattest focused', () => {
  const r = spawnSync('python3', ['-B', '-m', 'unittest', 'memvane.provider.tests.unit.test_reattest', '-v'], {encoding:'utf8', env:{...process.env, PYTHONDONTWRITEBYTECODE:'1'}});
  process.stderr.write(r.stderr || '');
  assert.equal(r.status, 0);
});

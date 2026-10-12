import test from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';

test('direct topical publish does not drop the ninth candidate', () => {
  const raw = execFileSync('python3',
    ['test/fixtures/topical-nine-candidate-publish.py'],
    {encoding: 'utf8', env: {...process.env, QMD_RECALL_LOG: ''}});
  assert.deepEqual(JSON.parse(raw), {allNineCompared: true});
});

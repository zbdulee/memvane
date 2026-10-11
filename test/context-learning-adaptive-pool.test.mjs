import {test} from 'node:test';
import {strict as assert} from 'node:assert';
import {execFileSync} from 'node:child_process';

test('shared experimental candidate bound adapts to resources and accepts over fifteen', () => {
  const result = JSON.parse(execFileSync('python3',
    ['test/fixtures/context-learning-adaptive-pool-checks.py'], {
      encoding: 'utf8', env: {...process.env, QMD_RECALL_LOG: '', PYTHONDONTWRITEBYTECODE: '1'},
      timeout: 10000,
    }));
  assert.equal(result.adaptive, true);
  assert.equal(result.moreThanFifteen, true);
});

import {test} from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';

test('hook-observed source cards require two consumed tickets and exact index ACKs', () => {
  const raw = execFileSync('python3',
    ['test/fixtures/context-learning-source-card-provenance-checks.py'], {
      env: {...process.env, PYTHONPATH: 'core', QMD_RECALL_LOG: '',
        PYTHONDONTWRITEBYTECODE: '1'},
      encoding: 'utf8', timeout: 10000,
    });
  const result = JSON.parse(raw);
  assert.equal(result.source_ticket, true);
  assert.equal(result.card_ticket, true);
  assert.equal(result.registration, 'verified');
  assert.equal(result.rejections, 14);
});

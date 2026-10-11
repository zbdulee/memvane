import {test} from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';

test('mixed raw/wiki PostToolUse retries card registration after index ACK', () => {
  const raw = execFileSync('python3',
    ['test/fixtures/context-learning-posttool-mixed-checks.py'], {
      env: {...process.env, PYTHONPATH: 'core', QMD_RECALL_LOG: '',
        PYTHONDONTWRITEBYTECODE: '1'},
      encoding: 'utf8', timeout: 10000,
    });
  assert.deepEqual(JSON.parse(raw), {
    mixed_card_retry: true, raw_recall_preserved: true,
  });
});

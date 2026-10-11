import test from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';

test('nested synthetic Codex edit queues the owner collection and SessionStart reaches lexical readiness', () => {
  const result = JSON.parse(execFileSync('python3',
    ['test/fixtures/nested-index-sessionstart-checks.py'], {
      encoding: 'utf8', timeout: 15000,
      env: {...process.env, PYTHONDONTWRITEBYTECODE: '1', QMD_RECALL_LOG: ''},
    }));
  assert.equal(result.nestedEditQueued, true);
  assert.equal(result.canonicalCollectionPath, true);
  assert.equal(result.lexicalReady, true);
});

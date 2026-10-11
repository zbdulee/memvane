import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';

test('legacy queue bytes survive private quarantine and a crash after done claim', () => {
  execFileSync('python3', ['test/fixtures/dirty-queue-legacy-quarantine-checks.py'], {
    encoding: 'utf8', env: { ...process.env, PYTHONPATH: 'core', QMD_RECALL_LOG: '' },
  });
});

test('legacy daemon handoff remains inside the update writer lock', () => {
  const script = readFileSync('backend/index_worker.sh', 'utf8');
  const lock = script.indexOf('if ! mkdir "$WRITER_LOCK"');
  const handoff = script.indexOf('needs-reload "$QUEUE"');
  const repair = script.indexOf('quarantine-ownerless "$QUEUE"');
  assert.ok(lock > 0 && lock < handoff && handoff < repair);
  assert.match(script.slice(lock, handoff), /writer lock busy — claim retained/);
});

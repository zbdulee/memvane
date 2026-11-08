import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';

test('compatible legacy vectors are copied and compared without changing the reader', () => {
  const result = spawnSync('python3', ['-B', 'test/fixtures/laya-shadow-vectors-checks.py'], {
    encoding: 'utf8', timeout: 300000,
    env: {...process.env, QMD_SANDBOX: '1', QMD_RECALL_LOG: '', PYTHONDONTWRITEBYTECODE: '1'},
  });
  assert.equal(result.status, 0, result.stdout + result.stderr);
  const ratio = result.stdout.match(/compat_ratio (\d+)\/(\d+) ([0-9.]+)/);
  assert.ok(ratio, 'compat ratio measurement missing');
  console.log(`compat_ratio ${ratio[1]}/${ratio[2]} ${ratio[3]}`);
  const calls = result.stdout.match(/^vector_page_calls .*$/m);
  assert.ok(calls, 'vector page call counts missing');
  console.log(calls[0]);
  assert.match(result.stderr, /Ran 12 tests\b/);
  assert.match(result.stderr, /\nOK\b/);
});

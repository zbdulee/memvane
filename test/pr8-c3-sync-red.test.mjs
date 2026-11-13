import {test} from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {mkdtempSync, realpathSync, rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
for (const name of ['GenerationSync', 'RowSync']) {
  test(`${name} consumes compiled intent on unchanged pass`, () => {
    const home = realpathSync(mkdtempSync(join(tmpdir(), 'pr8-c3-sync-')));
    try {
      const r = spawnSync('python3', ['-B', 'test/fixtures/pr8-c3-sync-red.py', `${name}.test_unchanged_pass_consumes_intent_once`], {
        encoding: 'utf8', timeout: 180000, env: {...process.env, HOME: home, TMPDIR: home,
          PYTHONDONTWRITEBYTECODE: '1', QMD_SETUP_GUARD_FIXTURE: '1', QMD_SANDBOX: '1', QMD_RECALL_LOG: '',
          GIT_CONFIG_GLOBAL: '/dev/null', GIT_CONFIG_SYSTEM: '/dev/null', GIT_TERMINAL_PROMPT: '0'},
      });
      if (r.stdout) process.stdout.write(r.stdout);
      if (r.stderr) process.stdout.write(r.stderr);
      assert.ifError(r.error); assert.equal(r.status, 0);
    } finally { rmSync(home, {recursive: true, force: true}); }
  });
}

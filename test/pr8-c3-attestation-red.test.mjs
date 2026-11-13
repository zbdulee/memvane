import {test} from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {mkdtempSync, realpathSync, rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
for (const name of ['test_format3_compiled_attestation', 'test_format4_compiled_attestation']) {
  test(name, () => {
    const home = realpathSync(mkdtempSync(join(tmpdir(), 'pr8-c3-')));
    try {
      const result = spawnSync('python3', ['-B', 'test/fixtures/pr8-c3-attestation-red.py', `CompiledAttestationChecks.${name}`], {
        encoding: 'utf8', timeout: 120000, env: {...process.env, HOME: home, TMPDIR: home,
          PYTHONDONTWRITEBYTECODE: '1', QMD_SETUP_GUARD_FIXTURE: '1', QMD_SANDBOX: '1', QMD_RECALL_LOG: ''},
      });
      if (result.stdout) process.stdout.write(result.stdout);
      if (result.stderr) process.stdout.write(result.stderr);
      assert.ifError(result.error); assert.equal(result.status, 0);
    } finally { rmSync(home, {recursive: true, force: true}); }
  });
}

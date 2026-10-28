import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {existsSync, mkdirSync, mkdtempSync, writeFileSync} from 'node:fs';
import {join} from 'node:path';
import {removeTemp} from './helpers/temp.mjs';

// A project whose Laya selection cannot be proved classifies as `blocked`:
// a non-legacy route. Manual skills must neither start nor query the QMD
// daemon for it, and must not fall back to QMD.
function blockedProject() {
  const dir = mkdtempSync('/tmp/laya-no-qmd-');
  mkdirSync(join(dir, '.auto-context'), {recursive: true});
  writeFileSync(join(dir, '.auto-context', 'settings.json'),
                JSON.stringify({indexing: true, collections: ['sample']}));
  writeFileSync(join(dir, '.auto-context', 'laya-index-selection.json'), '{');
  const managerLog = join(dir, 'manager.log');
  const manager = join(dir, 'manager.sh');
  writeFileSync(manager, `#!/usr/bin/env bash\necho "$@" >> "${managerLog}"\n`, {mode: 0o755});
  return {dir, manager, managerLog};
}

for (const [skill, args] of [['query', ['검색 결과는?']], ['update', []]]) {
  test(`${skill} skill on a non-legacy Laya route never touches the QMD daemon`, () => {
    const {dir, manager, managerLog} = blockedProject();
    try {
      const result = spawnSync('bash', [`skills/${skill}/scripts/${skill}.sh`, dir, ...args], {
        encoding: 'utf8', timeout: 30000,
        env: {...process.env, QMD_RECALL_LOG: '', QMD_BACKEND_MANAGER: manager,
              QMD_QUERY_FIXTURE: 'test/fixtures/daemon-response.json'},
      });
      assert.equal(result.status, 3, result.stdout + result.stderr);
      assert.match(result.stderr, /request refused/);
      assert.equal(result.stdout, '');
      assert.equal(existsSync(managerLog), false, 'backend manager was invoked');
    } finally {
      removeTemp(dir);
    }
  });
}

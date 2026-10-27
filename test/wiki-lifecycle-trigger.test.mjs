import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {mkdtempSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {removeTemp} from './helpers/temp.mjs';

// 선택된 Laya route의 wiki 수명 주기 trigger(SessionStart·편집·성공한 Bash → laya-compile pass).
// 모든 프로젝트·HOME·state·wake queue는 임시 디렉터리다. 각 python 검사는 자기 HOME을 다시 잡는다.
test('wiki lifecycle trigger: SessionStart·편집이 compile pass에서 수명 주기를 돌린다', () => {
  const home = mkdtempSync(join(tmpdir(), 'lifecycle-trigger-home-'));
  try {
    const result = spawnSync('python3', ['-B', 'test/fixtures/wiki-lifecycle-trigger-checks.py'], {
      encoding: 'utf8', timeout: 240000,
      env: {...process.env, HOME: home, QMD_RECALL_LOG: '', QMD_SANDBOX: '',
        QMD_SYNC_STATE_DIR: join(home, 'state'),
        QMD_WIKI_LIFECYCLE_LOG: join(home, 'lifecycle.log'),
        PYTHONDONTWRITEBYTECODE: '1'},
    });
    assert.equal(result.status, 0, result.stdout + result.stderr);
    assert.match(result.stderr, /Ran 10 tests/);
  } finally {
    removeTemp(home);
  }
});

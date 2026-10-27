import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {mkdtempSync, realpathSync} from 'node:fs';
import {join} from 'node:path';
import {tmpdir} from 'node:os';
import {removeTemp} from './helpers/temp.mjs';

// 브랜치 전환·워크트리에서 원문이 안 보이는 것을 실제 삭제로 오판하지 않는다.
// 실제 임시 git 저장소로 lifecycle 보류·같은 계보 삭제·rebase 중 건너뜀과
// 연결 워크트리의 main checkout 읽기 전용 recall을 검증한다.
test('git branch/worktree safety: wiki 카드 보류와 워크트리 읽기 전용 recall', () => {
  const home = realpathSync(mkdtempSync(join(tmpdir(), 'git-branch-safety-home-')));
  try {
    const result = spawnSync('python3', ['-B', 'test/fixtures/git-branch-safety-checks.py'], {
      encoding: 'utf8', timeout: 180000,
      env: {...process.env, HOME: home, QMD_RECALL_LOG: '', QMD_SKIP_BACKGROUND_WORKER: '1',
        PYTHONDONTWRITEBYTECODE: '1'},
    });
    assert.equal(result.status, 0, result.stdout + result.stderr);
    assert.match(result.stderr, /Ran 12 tests/);
    assert.match(result.stderr, /\nOK\b/);
  } finally { removeTemp(home); }
});

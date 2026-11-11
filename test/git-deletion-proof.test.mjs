import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {mkdtempSync, realpathSync} from 'node:fs';
import {join} from 'node:path';
import {tmpdir} from 'node:os';
import {removeTemp} from './helpers/temp.mjs';

// schema3 원문 삭제 증명(설계 §7.1): 같은 worktree의 관측 지문, 알려진 조상 commit,
// 그 commit에 있던 경로의 committed 삭제만 retire한다. 나머지 행렬은 hold이고
// Git 호출 수를 개수로 단정한다. 실제 임시 git 저장소만 쓴다.
test('git deletion proof: same-worktree ancestor committed deletion only', () => {
  const home = realpathSync(mkdtempSync(join(tmpdir(), 'git-deletion-proof-home-')));
  try {
    const result = spawnSync('python3', ['-B', 'test/fixtures/git-deletion-proof-checks.py'], {
      encoding: 'utf8', timeout: 180000,
      env: {...process.env, HOME: home, PYTHONDONTWRITEBYTECODE: '1'},
    });
    assert.equal(result.signal, null, result.stdout + result.stderr);
    assert.equal(result.status, 0, result.stdout + result.stderr);
    assert.match(result.stderr, /Ran 29 tests/);
    assert.match(result.stderr, /\nOK\b/);
  } finally { removeTemp(home); }
});

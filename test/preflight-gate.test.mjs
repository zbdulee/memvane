import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdirSync, writeFileSync, existsSync, utimesSync } from 'node:fs';
import { join } from 'node:path';
import { createHash } from 'node:crypto';
import { isolatedHomeProject, removeTemp } from './helpers/temp.mjs';

// gate 는 `$HOME/.config/qmd/{optout,skip}` 의 마커를 읽고, 이 파일은 update.sh --optout/--skip
// 으로 그 마커를 실제로 쓴다. HOME 을 실제 값으로 두면 사용자 홈에 optout 마커가 영구
// 누적됐다(실측: 전체 테스트 실행마다 +1, 누적 489개). 그래서 모든 케이스가 가짜 HOME 하위
// 프로젝트에서 돌고 자식 프로세스 env 에 그 HOME 을 넘긴다 — 격리를 지우지 말 것.
// (부수 효과로 skip 마커도 가짜 HOME 안에 떨어져 removeTemp(home) 한 번에 함께 사라진다.)
function makeTmpDir() {
  return isolatedHomeProject('gate');
}

function gate(payload, env) {
  return execFileSync('python3', ['core/preflight_gate.py'], {
    input: JSON.stringify(payload), encoding: 'utf8',
    env,
    cwd: process.cwd(),
  });
}

test('pending(config 없음) + Edit → deny', () => {
  const { home, dir, env } = makeTmpDir();
  try {
    const out = gate({ tool_name: 'Edit', tool_input: { file_path: join(dir, 'a.md') }, cwd: dir, session_id: 's1' }, env());
    const j = JSON.parse(out);
    assert.equal(j.hookSpecificOutput.permissionDecision, 'deny');
    const reason = j.hookSpecificOutput.permissionDecisionReason;
    assert.match(reason, /사용자에게 자연어로/);
    assert.match(reason, /추천 범위 확인/);
    assert.match(reason, /끄기/);
    assert.match(reason, /2시간 미루기/);
    assert.match(reason, /setup 스킬/);
    assert.doesNotMatch(reason, /update\.sh|--(?:recommend|optin|optout|skip)\b|\.auto-context\/settings\.json/);
  } finally { removeTemp(home); }
});

test('동의(indexing:true+collections) → allow(무출력)', () => {
  const { home, dir, env } = makeTmpDir();
  writeFileSync(join(dir, '.auto-context.json'), JSON.stringify({ indexing: true, collections: ['x'] }));
  try {
    const out = gate({ tool_name: 'Edit', tool_input: { file_path: join(dir, 'a.md') }, cwd: dir, session_id: 's1' }, env());
    assert.equal(out.trim(), '');
  } finally { removeTemp(home); }
});

test('거절(indexing:false) → allow', () => {
  const { home, dir, env } = makeTmpDir();
  writeFileSync(join(dir, '.auto-context.json'), JSON.stringify({ indexing: false }));
  try {
    assert.equal(gate({ tool_name: 'Edit', tool_input: { file_path: join(dir,'a.md') }, cwd: dir, session_id: 's1' }, env()).trim(), '');
  } finally { removeTemp(home); }
});

test('로컬 optout marker → allow', () => {
  const { home, dir, env } = makeTmpDir();
  try {
    execFileSync('bash', [join(process.cwd(), 'core', 'update.sh'), '--optout', dir], { encoding: 'utf8', env: env() });
    assert.equal(gate({ tool_name: 'Edit', tool_input: { file_path: join(dir,'a.md') }, cwd: dir, session_id: 's1' }, env()).trim(), '');
  } finally { removeTemp(home); }
});

test('sandbox → allow', () => {
  const { home, dir, env } = makeTmpDir();
  try {
    assert.equal(gate({ tool_name: 'Edit', tool_input: { file_path: join(dir,'a.md') }, cwd: dir, session_id: 's1' }, env({ QMD_SANDBOX: '1' })).trim(), '');
  } finally { removeTemp(home); }
});

test('잘못된 tool_name(Read) → allow', () => {
  const { home, dir, env } = makeTmpDir();
  try {
    assert.equal(gate({ tool_name: 'Read', tool_input: { file_path: join(dir,'a.md') }, cwd: dir, session_id: 's1' }, env()).trim(), '');
  } finally { removeTemp(home); }
});

test('pending에서 Bash는 기존 설계대로 허용하고 Write 계열은 차단', () => {
  const { home, dir, env } = makeTmpDir();
  try {
    assert.equal(gate({ tool_name: 'Bash', tool_input: { command: 'true' }, cwd: dir }, env()).trim(), '');
    for (const tool_name of ['Write', 'MultiEdit', 'NotebookEdit']) {
      const out = gate({ tool_name, tool_input: { file_path: join(dir, 'a.md') }, cwd: dir }, env());
      assert.equal(JSON.parse(out).hookSpecificOutput.permissionDecision, 'deny', tool_name);
    }
  } finally { removeTemp(home); }
});

test('Codex apply_patch(patch, file_path 없음) + pending → deny (경로 무관)', () => {
  const { home, dir, env } = makeTmpDir();
  try {
    const out = gate({ tool_name: 'apply_patch', tool_input: { patch: '*** Begin Patch\n*** End Patch' }, cwd: dir, session_id: 's1' }, env());
    assert.equal(JSON.parse(out).hookSpecificOutput.permissionDecision, 'deny');
  } finally { removeTemp(home); }
});

test('CODEX_SANDBOX(workspace-write) 단독 + pending apply_patch → 여전히 deny', () => {
  // 일반 Codex workspace-write 실행도 CODEX_SANDBOX 를 켠다. 그것만으로 gate 가 꺼지면
  // pending 프로젝트 편집 차단이 Codex 에서 통째로 사라진다. 중첩 격리 표식은 QMD_SANDBOX 다.
  const { home, dir, env } = makeTmpDir();
  try {
    const out = gate(
      { tool_name: 'apply_patch', tool_input: { patch: '*** Begin Patch\n*** End Patch' }, cwd: dir, session_id: 's1' },
      env({ CODEX_SANDBOX: 'seatbelt', QMD_SANDBOX: '', GEMINI_SANDBOX: '', CLAUDE_HEADLESS: '' }),
    );
    assert.equal(JSON.parse(out).hookSpecificOutput.permissionDecision, 'deny');
  } finally { removeTemp(home); }
});

// --- Task 7: --skip 마커 테스트 ---

// skip 마커 파일 경로를 JS에서 계산 (core/preflight_gate.py와 동일 알고리즘: sha256(realpath(cwd))).
// HOME 은 격리된 가짜 HOME 이므로 `homedir()`가 아니라 그 값을 받아 쓴다.
function skipMarkerPath(home, cwd) {
  const realcwd = execFileSync('python3', ['-c', `import os; print(os.path.realpath(${JSON.stringify(cwd)}))`], { encoding: 'utf8' }).trim();
  const hash = createHash('sha256').update(realcwd).digest('hex');
  return join(home, '.config', 'qmd', 'skip', hash);
}

test('--skip <dir> 실행 후 gate(cwd=dir) → allow(무출력)', () => {
  const { home, dir, env } = makeTmpDir();
  try {
    // Step 1: --skip 실행
    const skipOut = execFileSync('bash', [join(process.cwd(), 'core', 'update.sh'), '--skip', dir], {
      encoding: 'utf8',
      env: env(),
    });
    // 안내 메시지가 있어야 함
    assert.ok(skipOut.length > 0, `--skip should print a message, got: ${JSON.stringify(skipOut)}`);

    // Step 2: gate 호출 → allow (무출력)
    const out = gate({ tool_name: 'Edit', tool_input: { file_path: join(dir, 'a.md') }, cwd: dir, session_id: 's1' }, env());
    assert.equal(out.trim(), '', `skip 마커 후 gate가 allow(무출력)이어야 함, got: ${out}`);
  } finally {
    removeTemp(home);   // 마커도 가짜 HOME 안에 있어 함께 사라진다
  }
});

test('skip 안 한 다른 pending dir → deny', () => {
  // 두 프로젝트가 같은 가짜 HOME 을 공유해야 skip 마커 스코프 비교가 성립한다.
  const { home, dir: dir1, env } = makeTmpDir();
  const dir2 = join(home, 'project2');
  try {
    mkdirSync(dir2, { recursive: true });
    // dir1만 skip
    execFileSync('bash', [join(process.cwd(), 'core', 'update.sh'), '--skip', dir1], { encoding: 'utf8', env: env() });

    // dir2는 skip 안 했으므로 deny
    const out = gate({ tool_name: 'Edit', tool_input: { file_path: join(dir2, 'a.md') }, cwd: dir2, session_id: 's1' }, env());
    const j = JSON.parse(out);
    assert.equal(j.hookSpecificOutput.permissionDecision, 'deny', `skip 안 한 dir2는 deny여야 함`);
  } finally {
    removeTemp(home);
  }
});

test('TTL 만료된 마커 → deny + 마커 unlink', () => {
  const { home, dir, env } = makeTmpDir();
  const markerPath = skipMarkerPath(home, dir);
  try {
    // --skip으로 마커 생성
    execFileSync('bash', [join(process.cwd(), 'core', 'update.sh'), '--skip', dir], { encoding: 'utf8', env: env() });

    // 마커가 생성됐는지 확인
    assert.ok(existsSync(markerPath), `마커 파일이 생성돼야 함: ${markerPath}`);

    // mtime을 3시간 전으로 조작 (TTL 2시간 초과)
    const pastTime = new Date(Date.now() - 3 * 60 * 60 * 1000);
    utimesSync(markerPath, pastTime, pastTime);

    // gate 호출 → TTL 만료로 deny
    const out = gate({ tool_name: 'Edit', tool_input: { file_path: join(dir, 'a.md') }, cwd: dir, session_id: 's1' }, env());
    const j = JSON.parse(out);
    assert.equal(j.hookSpecificOutput.permissionDecision, 'deny', `TTL 만료 마커는 deny여야 함`);

    // lazy expire: 마커가 unlink 됐어야 함
    assert.ok(!existsSync(markerPath), `TTL 만료 마커는 unlink 돼야 함: ${markerPath}`);
  } finally {
    removeTemp(home);
  }
});

test('config/resolve_paths 조회 중 예상 못한 예외가 나도 gate는 fail-open(exit 0, 무출력)한다', () => {
  // gate는 soft protection이라, 샌드박스/권한 등 환경 차이로 config 조회 자체가
  // 죽더라도(예: PermissionError) hook 프로세스가 non-zero exit로 죽어 편집을
  // 막는 사고를 방지해야 한다. load_project_config를 강제로 raise시켜 검증한다.
  const script = `
import sys, json
sys.path.insert(0, 'core')
import config as qmd_config
import preflight_gate as pg

def boom(cwd):
    raise PermissionError("simulated sandboxed fs error")
qmd_config.load_project_config = boom

import io
sys.stdin = io.StringIO(json.dumps({"tool_name": "Edit", "tool_input": {"file_path": "/tmp/a.md"}, "cwd": "/tmp/does-not-matter"}))
rc = pg.main()
assert rc == 0, rc
print("OK")
`;
  const out = execFileSync('python3', ['-c', script], { encoding: 'utf8', cwd: process.cwd() }).trim();
  assert.equal(out, 'OK');
});

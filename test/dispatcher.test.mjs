import { test, after } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { readFileSync, existsSync, mkdtempSync, mkdirSync, writeFileSync, openSync, closeSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { removeTemp } from './helpers/temp.mjs';

// Legacy .agents fixtures deliberately exercise dispatch, not upgrade setup.
// Keep their logs and HOME-dependent gate fixture inside disposable test roots.
const fixtureRoot = mkdtempSync(join(tmpdir(), 'qmd-dispatch-fixture-'));
after(() => removeTemp(fixtureRoot));
const fixtureEnv = {
  QMD_SETUP_GUARD_FIXTURE: '1',
  QMD_CACHE_DIR: join(fixtureRoot, 'cache'),
  QMD_RECALL_LOG: '',
  // posttool launches manager ensure in the background. Keep that lifecycle
  // inside this fixture instead of starting the user's actual QMD endpoint.
  QMD_BACKEND_MANAGER: '/bin/true',
};

// hooks/run-hook <action> <engine> 를 fixture 모드로 실행하고 stdout 반환
function dispatch(args, payload, env = {}) {
  return execFileSync('bash', ['hooks/run-hook', ...args], {
    input: JSON.stringify(payload),
    encoding: 'utf8',
    env: { ...process.env, ...fixtureEnv, QMD_QUERY_FIXTURE: 'test/fixtures/daemon-response.json', ...env },
  }).trim();
}

// Sandbox/headless guards exit before reading stdin. Feed the same payload
// from a file so a fast exit cannot race spawnSync's write and cause EPIPE.
function dispatchGuard(args, payload, env = {}) {
  const dir = mkdtempSync(join(tmpdir(), 'qmd-guard-input-'));
  const payloadPath = join(dir, 'payload.json');
  writeFileSync(payloadPath, JSON.stringify(payload));
  const inputFd = openSync(payloadPath, 'r');
  try {
    return execFileSync('bash', ['hooks/run-hook', ...args], {
      stdio: [inputFd, 'pipe', 'pipe'],
      encoding: 'utf8',
      env: { ...process.env, ...fixtureEnv, QMD_QUERY_FIXTURE: 'test/fixtures/daemon-response.json', ...env },
    }).trim();
  } finally {
    closeSync(inputFd);
    removeTemp(dir);
  }
}

function selectionEvents(logPath) {
  if (!existsSync(logPath)) return [];
  return readFileSync(logPath, 'utf8').trim().split('\n').filter(Boolean)
    .map((l) => JSON.parse(l)).filter((e) => e.event === 'qmd_recall_selection');
}

const PROMPT = '검색 결과 정렬은 어떻게 동작해?';

test('recall claude → additionalContext 생성', () => {
  const dir = mkdtempSync(join(tmpdir(), 'qmd-disp-'));
  mkdirSync(join(dir, '.agents'), { recursive: true });
  writeFileSync(join(dir, '.agents', 'qmd-recall.json'), JSON.stringify({ collections: ['sample'] }));
  try {
    const out = dispatch(['recall', 'claude'], { prompt: PROMPT, cwd: dir });
    assert.match(JSON.parse(out).hookSpecificOutput.additionalContext, /\[sample\]/);
  } finally { removeTemp(dir); }
});

test('engine 라벨이 selection 로그에 기록 (claude/codex/gemini)', () => {
  for (const engine of ['claude', 'codex', 'gemini']) {
    const dir = mkdtempSync(join(tmpdir(), `qmd-disp-${engine}-`));
    mkdirSync(join(dir, '.agents'), { recursive: true });
    writeFileSync(join(dir, '.agents', 'qmd-recall.json'), JSON.stringify({ collections: ['sample'] }));
    const logPath = join(dir, 'r.log');
    try {
      dispatch(['recall', engine], { prompt: PROMPT, cwd: dir }, { QMD_RECALL_LOG: logPath });
      const ev = selectionEvents(logPath);
      assert.ok(ev.length > 0, 'no selection events');
      assert.equal(ev[0].engine, engine, `engine=${engine}`);
    } finally { removeTemp(dir); }
  }
});

// BUG-D regression: QMD_RECALL_LOG override가 없으면 hook 기본 로그가 /tmp가 아니라
// 유저 격리 캐시 경로($HOME/.cache/qmd, QMD_CACHE_DIR override)여야 한다.
test('BUG-D: QMD_RECALL_LOG 미지정 시 기본 로그는 /tmp가 아닌 캐시 경로', () => {
  const dir = mkdtempSync(join(tmpdir(), 'qmd-disp-log-'));
  mkdirSync(join(dir, '.agents'), { recursive: true });
  writeFileSync(join(dir, '.agents', 'qmd-recall.json'), JSON.stringify({ collections: ['sample'] }));
  const cacheDir = join(dir, 'cache');
  const env = { ...process.env, ...fixtureEnv, QMD_CACHE_DIR: cacheDir };
  delete env.QMD_RECALL_LOG;
  try {
    execFileSync('bash', ['hooks/run-hook', 'recall', 'claude'], {
      input: JSON.stringify({ prompt: PROMPT, cwd: dir }),
      encoding: 'utf8',
      env: { ...env, QMD_QUERY_FIXTURE: 'test/fixtures/daemon-response.json' },
    });
    // recall이 selection을 기본 로그(캐시 경로)에 기록했어야 한다.
    assert.equal(existsSync(join(cacheDir, 'qmd-claude-hook.log')), true,
      '기본 recall 로그가 캐시 경로에 생성돼야 함(/tmp 아님)');
  } finally { removeTemp(dir); }
});

// BUG-D regression: QMD_RECALL_LOG override는 반드시 존중돼야 한다(기존 동작 보존).
test('BUG-D: QMD_RECALL_LOG override가 존중된다', () => {
  const dir = mkdtempSync(join(tmpdir(), 'qmd-disp-logov-'));
  mkdirSync(join(dir, '.agents'), { recursive: true });
  writeFileSync(join(dir, '.agents', 'qmd-recall.json'), JSON.stringify({ collections: ['sample'] }));
  const logPath = join(dir, 'custom.log');
  try {
    dispatch(['recall', 'claude'], { prompt: PROMPT, cwd: dir }, { QMD_CACHE_DIR: join(dir, 'cache'), QMD_RECALL_LOG: logPath });
    assert.equal(existsSync(logPath), true, 'override 경로에 로그가 기록돼야 함');
  } finally { removeTemp(dir); }
});

test('CLAUDE_HEADLESS=1 → 무출력', () => {
  const out = dispatchGuard(['recall', 'claude'], { prompt: PROMPT, cwd: '/tmp' }, { CLAUDE_HEADLESS: '1' });
  assert.equal(out, '');
});

test('--sandbox 인자 → 무출력', () => {
  const out = dispatchGuard(['recall', 'claude', '--sandbox'], { prompt: PROMPT, cwd: '/tmp' });
  assert.equal(out, '');
});

test('CODEX_SANDBOX / GEMINI_SANDBOX → 무출력', () => {
  assert.equal(dispatchGuard(['recall', 'codex'], { prompt: PROMPT, cwd: '/tmp' }, { CODEX_SANDBOX: '1' }), '');
  assert.equal(dispatchGuard(['recall', 'gemini'], { prompt: PROMPT, cwd: '/tmp' }, { GEMINI_SANDBOX: '1' }), '');
});

test('QMD_SANDBOX=1 → 무출력 (cross-engine 공통 가드)', () => {
  assert.equal(dispatchGuard(['recall', 'claude'], { prompt: PROMPT, cwd: '/tmp' }, { QMD_SANDBOX: '1' }), '');
});

test('posttool action → 비-스토리 입력에서 graceful 종료', () => {
  // collectionPaths 없는 config + 비-스토리 경로 → posttool이 빈 출력으로 graceful 종료
  const dir = mkdtempSync(join(tmpdir(), 'qmd-posttool-'));
  mkdirSync(join(dir, '.agents'), { recursive: true });
  writeFileSync(join(dir, '.agents', 'qmd-recall.json'), JSON.stringify({ collections: ['sample'] }));
  try {
    assert.doesNotThrow(() =>
      dispatch(['posttool', 'claude'], {
        hook_event_name: 'PostToolUse',
        cwd: dir,
        tool_input: { file_path: '/tmp/unrelated.txt', content: 'some unrelated content for testing' },
      })
    );
  } finally { removeTemp(dir); }
});

test('알 수 없는 action → 비정상 종료', () => {
  assert.throws(() => dispatch(['bogus', 'claude'], { prompt: PROMPT, cwd: '/tmp' }));
});

test('run-hook index → index_enqueue.py 위임 (sandbox 무출력)', () => {
  const out = dispatchGuard(['index', 'claude', '--sandbox'], {});
  assert.equal(out, '');
});

test('run-hook compile → wiki_compile_enqueue.py 위임 후 worker kick, stdout silent', () => {
  const dir = mkdtempSync(join(tmpdir(), 'qmd-compile-dispatch-'));
  const enqueueLog = join(dir, 'enqueue.json');
  const managerLog = join(dir, 'manager.log');
  const enqueue = join(dir, 'enqueue.py');
  const manager = join(dir, 'manager.sh');
  writeFileSync(enqueue, `#!/usr/bin/env python3
import sys
open(${JSON.stringify(enqueueLog)}, 'w').write(sys.stdin.read())
`);
  writeFileSync(manager, `#!/usr/bin/env bash
printf '%s\n' "$*" >> ${JSON.stringify(managerLog)}
`);
  try {
    const payload = { hook_event_name: 'PostToolUse', cwd: dir, tool_input: { file_path: join(dir, 'docs', 'x.md') } };
    const out = execFileSync('bash', ['hooks/run-hook', 'compile', 'claude'], {
      input: JSON.stringify(payload),
      encoding: 'utf8',
      env: { ...process.env, QMD_CORE_COMPILE_ENQUEUE_SCRIPT: enqueue, QMD_BACKEND_MANAGER: manager },
    });
    assert.equal(out, '');
    assert.deepEqual(JSON.parse(readFileSync(enqueueLog, 'utf8')), payload);
    assert.equal(readFileSync(managerLog, 'utf8').trim(), `kick-wiki-compile ${dir}`);
  } finally { removeTemp(dir); }
});


test('run-hook compile forwards QMD_ENGINE to wiki compile enqueue', () => {
  const dir = mkdtempSync(join(tmpdir(), 'qmd-compile-engine-'));
  const engineLog = join(dir, 'engine.txt');
  const enqueue = join(dir, 'enqueue.py');
  const manager = join(dir, 'manager.sh');
  writeFileSync(enqueue, `#!/usr/bin/env python3
import os, sys
sys.stdin.read()
open(${JSON.stringify(engineLog)}, 'w').write(os.environ.get('QMD_ENGINE', ''))
`);
  writeFileSync(manager, '#!/usr/bin/env bash\nexit 0\n');
  try {
    execFileSync('bash', ['hooks/run-hook', 'compile', 'codex'], {
      input: JSON.stringify({ hook_event_name: 'PostToolUse', cwd: dir, tool_input: { file_path: join(dir, 'docs', 'x.md') } }),
      encoding: 'utf8',
      env: { ...process.env, QMD_CORE_COMPILE_ENQUEUE_SCRIPT: enqueue, QMD_BACKEND_MANAGER: manager },
    });
    assert.equal(readFileSync(engineLog, 'utf8'), 'codex');
  } finally { removeTemp(dir); }
});

test('run-hook compile --sandbox → 무출력', () => {
  const out = dispatchGuard(['compile', 'claude', '--sandbox'], {});
  assert.equal(out, '');
});

test('run-hook gate claude → preflight_gate.py 위임 (tool 비-gated 시 무출력)', () => {
  // tool_name이 GATED_TOOLS에 없으면 preflight_gate.py가 무출력으로 allow
  const payload = { hook_event_name: 'PreToolUse', tool_name: 'bash', tool_input: {} };
  // A neutral cwd: a checkout of this repository can itself be a selected Laya project.
  const out = execFileSync('bash', [join(process.cwd(), 'hooks/run-hook'), 'gate', 'claude'], {
    input: JSON.stringify(payload), encoding: 'utf8', cwd: tmpdir(),
  }).trim();
  // bash는 gated tool이 아니므로 무출력
  assert.equal(out, '');
});

test('run-hook gate claude → pending 프로젝트에서 gated tool 차단', () => {
  // pending 프로젝트(collections=[] and in safe path)에서 Edit 도구 차단
  const home = mkdtempSync(join(process.cwd(), 'qmd-test-gate-home-'));
  const dir = mkdtempSync(join(home, 'project-'));
  mkdirSync(join(dir, '.agents'), { recursive: true });
  // 빈 collections = pending (resolve_paths가 'risky' 아닌 'pending' reason 반환)
  writeFileSync(join(dir, '.agents', 'qmd-recall.json'), JSON.stringify({ collections: [] }));
  try {
    const payload = { hook_event_name: 'PreToolUse', tool_name: 'Edit', tool_input: {}, cwd: dir };
    const out = execFileSync('bash', ['hooks/run-hook', 'gate', 'claude'], {
      input: JSON.stringify(payload), encoding: 'utf8',
      env: { ...process.env, ...fixtureEnv, HOME: home },
    }).trim();
    assert.ok(out, 'pending 프로젝트는 gate 응답을 출력해야 함');
    const resp = JSON.parse(out);
    assert.equal(resp.hookSpecificOutput.permissionDecision, 'deny');
  } finally { removeTemp(home); }
});

test('CODEX_SANDBOX 는 codex recall 은 끄지만 pending gate 는 끄지 않는다', () => {
  // 일반 Codex workspace-write 실행이 CODEX_SANDBOX 를 켜므로 gate 만은 그것으로 건너뛰면
  // 안 된다. 같은 pending fixture 에서 recall(무출력)과 gate(deny)를 함께 고정한다.
  const home = mkdtempSync(join(process.cwd(), 'qmd-test-gate-home-'));
  const dir = mkdtempSync(join(home, 'project-'));
  mkdirSync(join(dir, '.agents'), { recursive: true });
  writeFileSync(join(dir, '.agents', 'qmd-recall.json'), JSON.stringify({ collections: [] }));
  const env = { HOME: home, CODEX_SANDBOX: 'seatbelt', QMD_SANDBOX: '', GEMINI_SANDBOX: '', CLAUDE_HEADLESS: '' };
  try {
    assert.equal(dispatchGuard(['recall', 'codex'], { prompt: PROMPT, cwd: dir }, env), '');
    const payload = { hook_event_name: 'PreToolUse', tool_name: 'apply_patch', tool_input: { patch: '*** Begin Patch\n*** End Patch' }, cwd: dir };
    const out = execFileSync('bash', ['hooks/run-hook', 'gate', 'codex'], {
      input: JSON.stringify(payload), encoding: 'utf8',
      env: { ...process.env, ...fixtureEnv, ...env },
    }).trim();
    assert.ok(out, 'CODEX_SANDBOX 단독으로는 pending gate 응답이 사라지면 안 됨');
    assert.equal(JSON.parse(out).hookSpecificOutput.permissionDecision, 'deny');
  } finally { removeTemp(home); }
});

test('run-hook gate --sandbox → 무출력', () => {
  const out = dispatchGuard(['gate', 'claude', '--sandbox'], {});
  assert.equal(out, '');
});

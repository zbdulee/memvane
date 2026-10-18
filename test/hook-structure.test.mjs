// 배포할 hooks/*.json 이 각 플랫폼 표준 구조인지 검증.
// Claude/Codex 이벤트 entry 는 hooks 배열 + {type:'command', command}.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

function assertStandardHooks(file) {
  const data = JSON.parse(readFileSync(file, 'utf8'));
  const hooks = data.hooks || {};
  for (const [event, entries] of Object.entries(hooks)) {
    assert.ok(Array.isArray(entries), `${file} ${event}: entries 배열 아님`);
    for (const entry of entries) {
      assert.ok(Array.isArray(entry.hooks), `${file} ${event}: entry에 hooks 배열 없음(비표준 평탄화 구조)`);
      for (const h of entry.hooks) {
        assert.equal(h.type, 'command', `${file} ${event}: hook.type 누락`);
        assert.ok(typeof h.command === 'string' && h.command.length > 0, `${file} ${event}: hook.command 누락`);
      }
    }
  }
}

test('claude hooks.json 표준 구조', () => assertStandardHooks('hooks/hooks.json'));
test('Claude Bash changes use a dedicated deferred detector, never blanket index or compile', () => {
  const hooks = JSON.parse(readFileSync('hooks/hooks.json', 'utf8')).hooks.PostToolUse;
  const bash = hooks.find(entry => entry.matcher === 'Bash');
  assert.ok(bash, 'successful Bash changes need a PostToolUse detector');
  assert.equal(bash.hooks.length, 1);
  assert.ok(bash.hooks[0].command.includes('file-change-event claude'));
  assert.equal(bash.hooks[0].timeout, 5);
  assert.ok(!bash.hooks[0].command.includes(' index '));
  assert.ok(!bash.hooks[0].command.includes(' compile '));
});
test('codex hooks-codex.json 표준 구조', () => assertStandardHooks('hooks/hooks-codex.json'));

test('prompt hooks stay synchronous with bounded host timeouts', () => {
  for (const file of ['hooks/hooks.json', 'hooks/hooks-codex.json']) {
    const hooks = JSON.parse(readFileSync(file, 'utf8')).hooks;
    assert.equal(hooks.UserPromptSubmit[0].hooks[0].timeout, 22);
    assert.notEqual(hooks.UserPromptSubmit[0].hooks[0].async, true);
    assert.equal(hooks.PostToolUse[0].hooks[0].timeout, 22);
    assert.equal(hooks.SessionStart[0].hooks[0].timeout, 5);
    assert.ok(hooks.SessionStart[0].hooks[0].command.includes('update-queued'));
    assert.equal(hooks.Stop[0].hooks[0].timeout, 3);
  }
});

// 공식 스펙 이벤트명 (developers.openai.com/codex/hooks, code.claude.com/docs/hooks)
function events(file) {
  return Object.keys(JSON.parse(readFileSync(file, 'utf8')).hooks || {});
}

test('claude 이벤트명: SessionStart/UserPromptSubmit/PreToolUse/PostToolUse/Stop', () => {
  assert.deepEqual(events('hooks/hooks.json').sort(), ['PostToolUse', 'PreToolUse', 'SessionStart', 'Stop', 'UserPromptSubmit']);
});

test('codex 이벤트명: PascalCase (공식 — snake_case 아님)', () => {
  const e = events('hooks/hooks-codex.json');
  assert.deepEqual(e.sort(), ['PostToolUse', 'PreToolUse', 'SessionStart', 'Stop', 'UserPromptSubmit']);
  assert.ok(!e.includes('session_start'), 'snake_case는 Codex가 인식 못함');
});

test('claude hooks.json PreToolUse gate 등록 확인', () => {
  const data = JSON.parse(readFileSync('hooks/hooks.json', 'utf8'));
  const pre = data.hooks['PreToolUse'];
  assert.ok(Array.isArray(pre) && pre.length > 0, 'PreToolUse 항목 없음');
  const entry = pre[0];
  assert.equal(entry.matcher, 'Edit|Write|MultiEdit|NotebookEdit', 'matcher 불일치');
  assert.ok(entry.hooks.some(h => h.command && h.command.includes('gate claude')), 'gate claude command 없음');
});

test('codex hooks-codex.json PreToolUse gate 등록 확인', () => {
  const data = JSON.parse(readFileSync('hooks/hooks-codex.json', 'utf8'));
  const pre = data.hooks['PreToolUse'];
  assert.ok(Array.isArray(pre) && pre.length > 0, 'PreToolUse 항목 없음');
  const entry = pre[0];
  assert.equal(entry.matcher, 'apply_patch|Edit|Write', 'matcher 불일치');
  assert.ok(entry.hooks.some(h => h.command && h.command.includes('gate codex')), 'gate codex command 없음');
});

test('claude PreToolUse matcher = PostToolUse matcher (우회 방지)', () => {
  const data = JSON.parse(readFileSync('hooks/hooks.json', 'utf8'));
  const preMatcher = data.hooks['PreToolUse'][0].matcher;
  const postMatcher = data.hooks['PostToolUse'][0].matcher;
  assert.equal(preMatcher, postMatcher, 'PreToolUse/PostToolUse matcher 불일치 — 우회 가능');
});

test('codex PreToolUse matcher = PostToolUse matcher (우회 방지)', () => {
  const data = JSON.parse(readFileSync('hooks/hooks-codex.json', 'utf8'));
  const preMatcher = data.hooks['PreToolUse'][0].matcher;
  const postMatcher = data.hooks['PostToolUse'][0].matcher;
  assert.equal(preMatcher, postMatcher, 'PreToolUse/PostToolUse matcher 불일치 — 우회 가능');
});

test('claude PostToolUse includes posttool, index, compile in order', () => {
  const hooks = JSON.parse(readFileSync('hooks/hooks.json', 'utf8')).hooks['PostToolUse'][0].hooks;
  assert.ok(hooks[0].command.includes('posttool claude'));
  assert.ok(hooks[1].command.includes('index claude'));
  assert.ok(hooks[2].command.includes('compile claude'));
});

test('codex PostToolUse includes posttool, index, compile without runtime order dependence', () => {
  const hooks = JSON.parse(readFileSync('hooks/hooks-codex.json', 'utf8')).hooks['PostToolUse'][0].hooks;
  const commands = hooks.map(h => h.command || '');
  assert.ok(commands.some(command => command.includes('posttool codex')));
  assert.ok(commands.some(command => command.includes('index codex')));
  assert.ok(commands.some(command => command.includes('compile codex')));
});

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const read = (p) => JSON.parse(readFileSync(p, 'utf8'));

test('Claude 매니페스트 필수 필드', () => {
  const m = read('.claude-plugin/plugin.json');
  assert.equal(m.name, 'memvane');
  assert.ok(m.description && m.version);
});

test('Claude hooks.json — 3 이벤트 + run-hook 호출', () => {
  const h = read('hooks/hooks.json').hooks;
  assert.ok(h.SessionStart && h.UserPromptSubmit && h.PostToolUse);
  const cmd = h.UserPromptSubmit[0].hooks[0].command;
  assert.match(cmd, /run-hook" recall claude/);
  assert.match(cmd, /\$\{CLAUDE_PLUGIN_ROOT\}/);
  assert.match(h.PostToolUse[0].matcher, /Edit\|Write\|MultiEdit\|NotebookEdit/);
});

test('Codex 매니페스트 — hooks 경로 명시 + interface', () => {
  const m = read('.codex-plugin/plugin.json');
  assert.equal(m.name, 'memvane');
  assert.equal(m.hooks, './hooks/hooks-codex.json');
  assert.ok(m.interface && m.interface.displayName);
});

test('Codex hooks-codex.json — CLAUDE_PLUGIN_ROOT 사용 (모든 codex 플러그인 관례)', () => {
  const h = read('hooks/hooks-codex.json').hooks;
  const cmd = h.UserPromptSubmit[0].hooks[0].command;
  assert.match(cmd, /run-hook" recall codex/);
  // Codex의 신뢰성 있는 plugin-root 변수는 CLAUDE_PLUGIN_ROOT (로컬 캐시의 OpenAI codex·
  // zax·superpowers 등 모든 codex 플러그인이 codex hooks에서 이 변수를 사용). PLUGIN_ROOT는
  // 간헐적으로만 주입돼 단독 사용 시 exit 127을 유발했다.
  assert.match(cmd, /\$\{CLAUDE_PLUGIN_ROOT\}/, 'CLAUDE_PLUGIN_ROOT 참조 누락');
  // novel한 파라미터 확장(:-)이나 제어 연산자는 쓰지 않는다(템플릿 치환 메커니즘 안전).
  assert.ok(!/:-/.test(cmd), 'novel 파라미터 확장(:-) 금지');
  const m = h.PostToolUse[0].matcher;
  assert.match(m, /apply_patch/, 'codex 편집 tool 이름 포함');
  assert.ok(!/MultiEdit|NotebookEdit/.test(m), 'Claude 전용 이름 제거');
});

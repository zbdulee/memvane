import { test } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, readFileSync } from 'node:fs';

test('claude marketplace.json: source ./ + qmd-auto-context plugin', () => {
  const m = JSON.parse(readFileSync('.claude-plugin/marketplace.json', 'utf8'));
  assert.ok(Array.isArray(m.plugins), 'plugins 배열 존재');
  const p = m.plugins.find(x => x.name === 'qmd-auto-context');
  assert.ok(p, 'qmd-auto-context plugin 항목');
  assert.equal(p.source, './', 'source는 루트(./)');
});

test('codex marketplace.json: qmd-auto-context plugin 항목', () => {
  const m = JSON.parse(readFileSync('.agents/plugins/marketplace.json', 'utf8'));
  assert.ok(Array.isArray(m.plugins), 'plugins 배열 존재');
  const p = m.plugins.find(x => x.name === 'qmd-auto-context');
  assert.ok(p, 'qmd-auto-context plugin 항목');
  assert.equal(p.version, '0.36.0', 'codex marketplace version');
});

test('runtime authorization pins the installed plugin version', () => {
  const version = JSON.parse(readFileSync('package.json', 'utf8')).version;
  const source = readFileSync('core/context_learning/runtime_authorization.py', 'utf8');
  const pinned = source.match(/^REVIEWED_PLUGIN_VERSION = '([^']+)'$/m);
  assert.ok(pinned, 'runtime authorization version pin');
  assert.equal(pinned[1], version);
  for (const file of ['plugin.json', '.claude-plugin/plugin.json', '.codex-plugin/plugin.json']) {
    assert.equal(JSON.parse(readFileSync(file, 'utf8')).version, version, file);
  }
  for (const file of ['.claude-plugin/marketplace.json', '.agents/plugins/marketplace.json']) {
    const marketplace = JSON.parse(readFileSync(file, 'utf8'));
    const plugin = marketplace.plugins.find(item => item.name === 'qmd-auto-context');
    assert.equal(plugin?.version, version, file);
  }
  const yamlVersion = readFileSync('plugin.yaml', 'utf8').match(/^version: "([^"]+)"$/m);
  assert.equal(yamlVersion?.[1], version, 'plugin.yaml');
});

// codex manifest는 단순 source:"./"가 아니라 git 배포용 source 객체 + policy + category를
// 요구한다(brief 초안). 존재만 검증하면 §배포에서 스키마 누락이 silent하게 통과하므로,
// 초안 형태를 명시적으로 고정한다(§배포 실측에서 확정 시 함께 갱신).
test('codex marketplace.json: git source 객체 + policy + category 스키마', () => {
  const m = JSON.parse(readFileSync('.agents/plugins/marketplace.json', 'utf8'));
  const p = m.plugins.find(x => x.name === 'qmd-auto-context');
  assert.ok(p, 'qmd-auto-context plugin 항목');
  assert.equal(p.source.source, 'url', 'source.source는 url');
  assert.equal(p.source.url, 'https://github.com/zbdulee/qmd-auto-context', 'source.url은 repo URL');
  assert.equal(p.policy.installation, 'AVAILABLE', 'policy.installation은 AVAILABLE');
  assert.equal(p.policy.authentication, 'ON_INSTALL', 'policy.authentication은 ON_INSTALL');
  assert.equal(p.category, 'Productivity', 'category는 Productivity');
});

// AGY는 marketplace/root hooks manifest가 아니라 프로젝트 로컬 installer가 제품 경로다.
test('agy 루트 plugin.json: name qmd-auto-context', () => {
  const p = JSON.parse(readFileSync('plugin.json', 'utf8'));
  assert.equal(p.name, 'qmd-auto-context');
  assert.equal(p.version, '0.36.0');
  assert.doesNotMatch(p.description, /Task \d|install\.sh|uninstall\.sh/);
});

test('agy 루트 hooks.json은 제공하지 않고 local installer는 stale qmd hook만 정리한다', () => {
  assert.equal(existsSync('hooks.json'), false);
  const installer = readFileSync('core/agy_local_install.py', 'utf8');
  assert.match(installer, /PostToolUse payload/);
  assert.match(installer, /MARKER = "run-hook"/);
  assert.doesNotMatch(installer, /run-hook" posttool gemini/);
  assert.doesNotMatch(installer, /run-hook" index gemini/);
});

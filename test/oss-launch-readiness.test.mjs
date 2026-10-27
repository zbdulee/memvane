import { test } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, readFileSync } from 'node:fs';

const read = (path) => readFileSync(path, 'utf8');

test('README states the supported hosts and an English quickstart', () => {
  const readme = read('README.md');
  assert.match(readme, /Claude Code/);
  assert.match(readme, /Codex/);
  assert.match(readme, /Hermes Agent/);
  assert.match(readme, /## Quickstart \(English\)/);
});

test('README describes the gate on selected and not-onboarded projects and the data-handling link', () => {
  const readme = read('README.md');
  assert.match(readme, /선택된 프로젝트에서.*gate가/s);
  assert.match(readme, /온보딩되지 않은\s*프로젝트에서는 gate가 아무 작업도 하지 않습니다/);
  assert.doesNotMatch(readme, /2시간/);
  assert.match(readme, /docs\/privacy\.md/);
});

test('privacy guide explains optional wiki compile host-CLI content handling', () => {
  const privacy = read('docs/privacy.md');
  assert.match(privacy, /optional wiki compile/i);
  assert.match(privacy, /configured host CLI/i);
  assert.match(privacy, /source content/i);
  assert.match(privacy, /disable/i);
});

test('public contribution, security, release, and community documents exist', () => {
  for (const path of [
    'CONTRIBUTING.md',
    'SECURITY.md',
    'CODE_OF_CONDUCT.md',
    'docs/release.md',
    '.github/ISSUE_TEMPLATE/bug_report.yml',
    '.github/pull_request_template.md',
  ]) {
    assert.ok(existsSync(path), `${path} must exist`);
  }
});

test('CI installs the declared Node versions and runs the deterministic suite', () => {
  const workflow = read('.github/workflows/test.yml');
  assert.match(workflow, /actions\/setup-node/);
  assert.match(workflow, /node-version/);
  assert.match(workflow, /npm test/);
  assert.match(workflow, /python3/);
});

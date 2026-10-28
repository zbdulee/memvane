import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { chmodSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, statSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { removeTemp } from './helpers/temp.mjs';

// 소스 존재 판정 SSOT(`wiki_source_missing` → `recall.resolve_existing_source`)와 카드·wiki
// 산출물의 원자적 쓰기 계약. 원문 소실 카드의 처리는 test/wiki-lifecycle.test.mjs 가 본다.

function setupProject({ compile = {}, indexing = true, base } = {}) {
  const dir = base || mkdtempSync(join(tmpdir(), 'qmd-srcmiss-'));
  mkdirSync(join(dir, '.auto-context', 'wiki', 'entities'), { recursive: true });
  mkdirSync(join(dir, 'docs'), { recursive: true });
  writeFileSync(join(dir, '.auto-context', 'settings.json'), JSON.stringify({
    indexing,
    collections: ['proj-docs', 'proj-wiki'],
    collectionPaths: { 'proj-docs': 'docs', 'proj-wiki': '.auto-context/wiki' },
    collectionRoles: { 'proj-docs': 'raw', 'proj-wiki': 'wiki' },
    wikiPath: '.auto-context/wiki',
    compile: { enabled: true, mode: 'auto-wiki', ...compile },
  }));
  return dir;
}

function writeCard(dir, rel, { status = 'generated', sources = [], title = 'Card', createdBy = 'qmd-auto-context' } = {}) {
  const full = join(dir, '.auto-context', 'wiki', rel);
  mkdirSync(join(full, '..'), { recursive: true });
  const lines = ['---', `title: "${title}"`, `status: ${status}`, `createdBy: ${createdBy}`];
  if (sources.length) {
    lines.push('sources:');
    for (const src of sources) lines.push(`  - ${src}`);
  }
  lines.push('---', '', '<!-- qmd:auto:start id="main" sourceHash="abc" -->', '## Summary', 'body', '<!-- qmd:auto:end -->', '');
  writeFileSync(full, lines.join('\n'));
  return full;
}

function fileSource(path) {
  return `{kind: "file", path: "${path}"}`;
}

// python 스크립트를 파일로 실행한다. `bash -c "python3 -c <JSON.stringify(...)>"` 는
// 개행이 `\n` 리터럴로 들어가 python SyntaxError 가 되고, 그 실패가 try/catch 에 삼켜져
// 테스트가 아무것도 검증하지 않는 거짓 통과가 된다.
function runPy(dir, lines, { ulimit } = {}) {
  const script = join(dir, `run-${Math.random().toString(36).slice(2)}.py`);
  writeFileSync(script, ['import sys', 'sys.path.insert(0, "core")', ...lines].join('\n') + '\n');
  const cmd = ulimit ? `ulimit -f ${ulimit}; exec python3 ${JSON.stringify(script)}`
    : `exec python3 ${JSON.stringify(script)}`;
  return execFileSync('bash', ['-c', cmd], { cwd: process.cwd(), encoding: 'utf8' });
}


// ─────────────────────────────────────────────────────────────────────────────
// 확인 라운드 반영 — 자동 경로 원자성 / 권한 / 판정 통일 / observe 死코드
// ─────────────────────────────────────────────────────────────────────────────

test('자동 경로(patch_frontmatter_fields)도 쓰기 실패에서 원본을 지킨다', () => {
  const dir = mkdtempSync(join(tmpdir(), 'qmd-atomic-auto-'));
  try {
    const card = join(dir, 'card.md');
    const body = ['---', 'title: "C"', 'status: generated', '---', '', 'body line', ''].join('\n')
      + 'x'.repeat(40 * 1024) + '\n';
    writeFileSync(card, body);
    const out = runPy(dir, [
      'import wiki_compile as wc',
      'from pathlib import Path',
      `ok = wc.patch_frontmatter_fields(Path(${JSON.stringify(card)}), {"status": "verified"})`,
      'print("ok" if ok else "failed")',
    ], { ulimit: 4 });
    assert.equal(out.trim(), 'failed', '쓰기 상한 아래에서는 실패를 반환해야 한다');
    const after = readFileSync(card, 'utf8');
    assert.equal(after, body, '실패해도 원본이 바이트 그대로 남아야 한다');
    assert.match(after, /^status: generated$/m, '패치가 반영되지 않은 상태여야 한다');
    assert.deepEqual(readdirSync(dir).filter((n) => n.includes('.tmp-')), []);
  } finally { removeTemp(dir); }
});

test('verify pass 스탬프(자동, 사람 개입 없음)가 쓰기 실패로 카드를 절단하지 않는다', () => {
  const dir = mkdtempSync(join(tmpdir(), 'qmd-atomic-verify-'));
  try {
    // wiki_verify_worker가 status: verified를 패치하는 그 경로를 직접 호출한다.
    const card = join(dir, 'v.md');
    const body = ['---', 'title: "V"', 'status: generated', 'createdBy: qmd-auto-context',
      '---', '', 'claim body', ''].join('\n') + 'y'.repeat(40 * 1024) + '\n';
    writeFileSync(card, body);
    const out = runPy(dir, [
      'import wiki_compile as wc',
      'from pathlib import Path',
      `ok = wc.patch_frontmatter_fields(Path(${JSON.stringify(card)}), {"status": "verified", "verifiedBy": "claude", "verifiedAt": "2026-07-30T00:00:00Z"})`,
      'print("ok" if ok else "failed")',
    ], { ulimit: 4 });
    assert.equal(out.trim(), 'failed');
    // 이 경로는 verify worker가 사람 개입 없이 도는 곳이다 — 절단되면 다음 회차가
    // 절단본을 읽어 changed_during_verify로 skip하고 영구 손상이 조용히 남는다.
    assert.equal(readFileSync(card, 'utf8'), body, '카드가 바이트 그대로여야 한다');
    // 상한을 풀면 정상 스탬프된다(원자적 경로가 기능을 깨지 않았다).
    assert.equal(runPy(dir, [
      'import wiki_compile as wc',
      'from pathlib import Path',
      `wc.patch_frontmatter_fields(Path(${JSON.stringify(card)}), {"status": "verified"})`,
      'print("done")',
    ]).trim(), 'done');
    assert.match(readFileSync(card, 'utf8'), /^status: verified$/m);
  } finally { removeTemp(dir); }
});

test('원자적 쓰기가 원본 권한을 보존한다 (0600 → 0600)', () => {
  const dir = setupProject();
  try {
    const card = writeCard(dir, 'entities/secret.md', { status: 'verified', sources: [fileSource('docs/old.md')] });
    chmodSync(card, 0o600);
    // 자동 경로(frontmatter 패치). os.replace가 임시파일 권한을 남기면 회귀다.
    runPy(dir, [
      'import wiki_compile as wc',
      'from pathlib import Path',
      `wc.patch_frontmatter_fields(Path(${JSON.stringify(card)}), {"status": "contested"})`,
    ]);
    assert.equal(statSync(card).mode & 0o777, 0o600);
  } finally { removeTemp(dir); }
});


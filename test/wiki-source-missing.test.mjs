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


test('source_missing 판정은 recall의 존재 판정을 재사용한다(두 벌 금지)', () => {
  const out = execFileSync('python3', ['-c', [
    'import sys; sys.path.insert(0, "core")',
    'import inspect, recall, wiki_source_missing as wsm',
    // 존재 판정 SSOT: 두 분류 함수가 모두 recall의 함수를 호출한다.
    'src = inspect.getsource(wsm.classify_entries) + inspect.getsource(wsm.classify_records)',
    'assert "classify_source_entry" in src and "resolve_existing_source" in src, src',
    'assert "classify_source_entry(entry, project_root, allow_roots)" in inspect.getsource(recall.resolve_source_path)',
    'print("ok")',
  ].join('\n')], { cwd: process.cwd(), encoding: 'utf8' });
  assert.equal(out.trim(), 'ok');
});

test('injectSourcePathsPerCard:0 에서도 진단 로그가 켜지면 카운터가 산다 / 꺼지면 비용 0', () => {
  // (a) 로그 off + 상한 0 → allowRoots resolve도, 분류도 하지 않는다(추가 비용 0 계약).
  const off = execFileSync('python3', ['-c', [
    'import sys, json; sys.path.insert(0, "core")',
    'import recall',
    'cfg = {"injectSourcePathsPerCard": 0, "allowRoots": ["/tmp"]}',
    'print(json.dumps([recall.source_inject_opts(cfg), recall.source_inject_opts(cfg, observe=True)], default=str))',
  ].join('\n')], { cwd: process.cwd(), encoding: 'utf8' });
  const [noObserve, observe] = JSON.parse(off);
  assert.deepEqual(noObserve, [0, [], false]);
  assert.equal(observe[0], 0);
  assert.equal(observe[2], true, '로그가 켜지면 관측 전용 모드');
  assert.ok(observe[1].length > 0, '관측 모드에서는 allowRoots를 해석한다');
});

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

test('전부 소실 판정이 두 경로에서 같은 구현을 쓴다 (예산 절단은 입력 차이로만 남는다)', () => {
  const out = execFileSync('python3', ['-c', [
    'import sys, json; sys.path.insert(0, "core")',
    'import inspect, recall, wiki_source_missing as wsm',
    // 구현은 한 벌이다.
    'assert "sources_all_missing(" in inspect.getsource(wsm.all_sources_missing)',
    'assert "sources_all_missing(" in inspect.getsource(recall.card_sources_all_missing)',
    // 같은 (missing, present, undecidable) 입력이면 답이 같다.
    'cases = [(1,0,0),(0,0,0),(2,1,0),(3,0,1),(1,0,2)]',
    'rule = [recall.sources_all_missing(*c) for c in cases]',
    'wsmr = [wsm.all_sources_missing({"missing": ["x"]*m, "present": ["y"]*p, "unknown": {"z": u} if u else {}}) for m,p,u in cases]',
    'assert rule == wsmr, (rule, wsmr)',
    // recall 쪽 환산: duplicate는 판정불가가 아니고(살아 있는 경로의 중복), over_scan_budget은 판정불가다.
    'r = {"_wiki_source_reasons": {"missing": 7}, "_wiki_source_present": 0}',
    'assert recall.card_sources_all_missing(r) is True',
    'r2 = {"_wiki_source_reasons": {"missing": 6, "over_scan_budget": 1}, "_wiki_source_present": 0}',
    'assert recall.card_sources_all_missing(r2) is False',
    'print(json.dumps({"rule": rule}))',
  ].join('\n')], { cwd: process.cwd(), encoding: 'utf8' });
  assert.deepEqual(JSON.parse(out).rule, [true, false, false, false, false]);
});

test('observe 모드에서 duplicate 사유가 실제로 발화한다 (死코드 없음)', () => {
  const out = execFileSync('python3', ['-c', [
    'import sys, json, tempfile, os; sys.path.insert(0, "core")',
    'import recall',
    'from pathlib import Path',
    'root = Path(tempfile.mkdtemp()).resolve()',
    '(root / "docs").mkdir()',
    '(root / "docs" / "a.md").write_text("x")',
    'block = \'sources:\\n  - {kind: "file", path: "docs/a.md"}\\n  - {kind: "file", path: "docs/a.md"}\'',
    'inject = recall.collect_source_paths(block, root, str(root), 3, [], False)',
    'observe = recall.collect_source_paths(block, root, str(root), 0, [], True)',
    'print(json.dumps({"inject": [inject[0], inject[2], inject[3]], "observe": [observe[0], observe[2], observe[3]]}))',
  ].join('\n')], { cwd: process.cwd(), encoding: 'utf8' });
  const { inject, observe } = JSON.parse(out);
  assert.deepEqual(inject[1], { duplicate: 1 });
  assert.deepEqual(observe[1], { duplicate: 1 }, 'observe 모드에서도 중복이 집계돼야 한다');
  assert.deepEqual(observe[0], [], '관측 모드는 주입하지 않는다');
  assert.equal(inject[2], 2);
  assert.equal(observe[2], 2, 'present 집계는 두 모드가 같다');
  // over_cap은 주입 상한 전용이므로 observe 모드에서 발화하지 않는다.
  assert.ok(!('over_cap' in observe[1]));
});

test('index.md 쓰기 실패에서 누적 인덱스가 온전히 남는다 / 정상 append는 불변 / log는 append 그대로', () => {
  const dir = mkdtempSync(join(tmpdir(), 'qmd-index-'));
  try {
    const wiki = join(dir, 'wiki');
    mkdirSync(join(wiki, 'concepts'), { recursive: true });
    writeFileSync(join(wiki, 'concepts', 'new.md'), 'card\n');
    // 누적 인덱스(라이브는 106KB / 850줄)
    const existing = '# Auto-context Wiki Index\n\n'
      + Array.from({ length: 900 }, (_, i) => `- concepts/old-${i}.md - 카드 ${i}`).join('\n') + '\n';
    writeFileSync(join(wiki, 'index.md'), existing);
    writeFileSync(join(wiki, 'log.md'), '# Auto-context Wiki Log\n\n- old log line\n');

    const call = (limit) => runPy(dir, [
      'import wiki_compile as wc',
      'from pathlib import Path',
      `wiki = Path(${JSON.stringify(wiki)})`,
      'ok = wc.update_index(wiki, wiki / "concepts" / "new.md", "새 카드")',
      'wc.append_log(wiki, "created", wiki / "concepts" / "new.md", "새 카드")',
      'print("ok" if ok else "failed")',
    ], limit ? { ulimit: limit } : {}).trim();

    // (a) 쓰기 상한 아래: 실패를 반환하고 기존 인덱스는 그대로다.
    assert.equal(call(4), 'failed');
    assert.equal(readFileSync(join(wiki, 'index.md'), 'utf8'), existing, '누적 인덱스가 절단되면 안 된다');
    assert.deepEqual(readdirSync(wiki).filter((n) => n.includes('.tmp-')), []);
    // append 모드인 log는 상한 아래에서도 truncate되지 않는다.
    assert.match(readFileSync(join(wiki, 'log.md'), 'utf8'), /^# Auto-context Wiki Log/);
    assert.match(readFileSync(join(wiki, 'log.md'), 'utf8'), /- old log line/);

    // (b) 정상 경로: 한 줄 append + 중복 호출은 늘리지 않는다(기존 동작 불변).
    assert.equal(call(0), 'ok');
    const after = readFileSync(join(wiki, 'index.md'), 'utf8');
    assert.equal(after.split('\n').filter((l) => l.includes('concepts/new.md')).length, 1);
    assert.ok(after.startsWith('# Auto-context Wiki Index'));
    assert.equal(after.split('\n').filter((l) => l.startsWith('- concepts/old-')).length, 900);
    assert.equal(call(0), 'ok');
    assert.equal(readFileSync(join(wiki, 'index.md'), 'utf8'), after, '중복 줄을 추가하지 않는다');
    // log는 호출마다 한 줄씩 늘어난다(append 계약).
    assert.equal(readFileSync(join(wiki, 'log.md'), 'utf8')
      .split('\n').filter((l) => l.includes('created')).length, 3);
  } finally { removeTemp(dir); }
});

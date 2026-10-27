import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync, spawnSync } from 'node:child_process';
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { homedir, tmpdir } from 'node:os';
import { join } from 'node:path';
import { removeTemp } from './helpers/temp.mjs';

// SessionStart wiki 수명 주기(`core/wiki_lifecycle.py`). 조치(삭제·재생성 enqueue·가드·
// 멱등성)는 python fixture가 selected route를 붙여 검증하고, 여기서는 그 결과와 update.sh
// worker가 쓰는 ineligible 집계 계약을 본다. 모든 프로젝트는 임시 디렉터리다.

test('wiki lifecycle: generated 카드의 원문 소실·변경·지문 없음·유지·가드·멱등성', () => {
  const result = spawnSync('python3', ['-B', 'test/fixtures/wiki-lifecycle-checks.py'],
    { encoding: 'utf8', env: { ...process.env, QMD_RECALL_LOG: '' } });
  assert.equal(result.status, 0, result.stderr);
  assert.match(result.stderr, /Ran 16 tests/);
});

function setupProject({ compile = {}, indexing = true, base } = {}) {
  const dir = base || mkdtempSync(join(tmpdir(), 'qmd-lifecycle-'));
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

function writeCard(dir, rel, {
  status = 'generated', title = 'Card', createdBy = 'qmd-auto-context',
  sourceRevisions = status === 'verified',
} = {}) {
  const full = join(dir, '.auto-context', 'wiki', rel);
  mkdirSync(join(full, '..'), { recursive: true });
  const lines = ['---', `title: "${title}"`, `status: ${status}`, `createdBy: ${createdBy}`];
  if (sourceRevisions) {
    lines.push('sourceRevisions:');
    lines.push('  - {kind: "file", path: "docs/compiler-source.md", collection: "proj-docs", sha256: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", size: 1, mtimeNs: 1}');
  }
  lines.push('---', '', '<!-- qmd:auto:start id="main" sourceHash="abc" -->', '## Summary', 'body', '<!-- qmd:auto:end -->', '');
  writeFileSync(full, lines.join('\n'));
  return full;
}

function runLifecycle(dir, env = {}) {
  const out = execFileSync('python3', ['core/wiki_lifecycle.py', '--cwd', dir, '--json'], {
    cwd: process.cwd(),
    encoding: 'utf8',
    env: {
      ...process.env,
      QMD_RECALL_LOG: '',
      QMD_SYNC_STATE_DIR: join(dir, '.lifecycle-state'),
      QMD_WIKI_LIFECYCLE_LOG: join(dir, 'lifecycle.log'),
      ...env,
    },
  });
  return out.trim() ? JSON.parse(out) : {};
}

test('wiki lifecycle: sandbox와 opt-out 프로젝트에서는 무동작', () => {
  const dir = setupProject({ indexing: false });
  try {
    writeCard(dir, 'entities/legacy.md');
    assert.deepEqual(runLifecycle(dir).ineligibleMeasured, false);
    assert.equal(runLifecycle(dir, { QMD_SANDBOX: '1' }).cards, undefined);
    assert.equal(existsSync(join(dir, '.auto-context', 'wiki', 'entities', 'legacy.md')), true);
  } finally { removeTemp(dir); }
});


// `ineligible`은 수명 주기 조치와 **다른 지표**다: "provenance가 없어 recall이 구조적으로
// 주입할 수 없는 카드 수"이고, 종점은 update.sh의 SessionStart notice다. 이 값이 필요한
// 이유는 source freshness 도입으로 기존 카드가 **한 번에** 전부 recall에서 빠지기 때문이다
// (라이브 실측 service-engineering 931/931, ai-proxy 26/26). 절벽 자체는 의도된 정책이지만
// 조용하면 "어느 날 wiki recall이 그냥 비었다"로 보여 버그로 신고된다.
test('wiki lifecycle: ineligible은 provenance 없는 카드를 recall과 같은 판정으로 센다', () => {
  const dir = setupProject();
  try {
    // 신뢰 조건 3개(verified · qmd 소유 · sourceRevisions)를 하나씩 깨뜨린다.
    writeCard(dir, 'entities/ok.md', { status: 'verified' });                       // 적격
    writeCard(dir, 'entities/no-rev.md', { status: 'verified', sourceRevisions: false }); // 지문 없음
    writeCard(dir, 'entities/draft.md', { status: 'generated' });                   // 미검수
    writeCard(dir, 'entities/foreign.md', { status: 'verified', createdBy: 'human' }); // 외부 작성
    const r = runLifecycle(dir);
    assert.equal(r.cards, 4);
    assert.equal(r.ineligible, 3, 'ok.md만 적격이어야 한다');
  } finally { removeTemp(dir); }
});

// **`sourceScan.enabled`는 수명 주기 조치만 끈다.** `ineligible`은 별개 기능(provenance
// 결측 총량)이고 별개 알림을 가지므로 그 스위치의 대상이 아니다. 커플링돼 있던 동안
// `enabled:false`는 부적격 카드 N장을 **0장으로 보고**했고, update.sh는 0을 받으면 상태
// 파일을 지워 `notice_clear wiki-ineligible`을 발동한다 — 그 알림 문구가 "남은 수는 이
// 알림이 사라지면 0입니다"라고 스스로 명시하므로 결과는 **거짓 복구 보고**다.
test('wiki lifecycle: enabled:false는 조치만 끄고 ineligible 집계는 유지한다', () => {
  const dir = setupProject({ compile: { sourceScan: { enabled: false } } });
  try {
    writeCard(dir, 'entities/ok.md', { status: 'verified' });
    writeCard(dir, 'entities/draft.md', { status: 'generated' });
    writeCard(dir, 'entities/no-rev.md', { status: 'verified', sourceRevisions: false });
    const r = runLifecycle(dir, { QMD_SOURCE_SCAN_MAX: '' });
    assert.equal(r.cards, 3, '카드 목록은 여전히 만든다(집계 입력)');
    assert.equal(r.ineligible, 2, 'ineligible은 스위치와 무관하게 센다');
    assert.equal(r.ineligibleMeasured, true, '측정했다는 사실이 결과에 남아야 한다');
    assert.equal(r.examined, 0, '수명 주기 판정은 하지 않는다');
  } finally { removeTemp(dir); }
});

// **"0"과 "안 셌다"는 다르다.** 미측정 run이 0으로 보고되면 update.sh가 상태 파일을 지워
// 거짓 복구 보고가 된다. 측정 불가 경로(unsafe wikiPath)는 measured:false여야 하고,
// 카드가 하나도 없는 경로는 **측정된 0**이어야 한다(wiki를 지우면 알림도 사라져야 한다).
test('wiki lifecycle: ineligibleMeasured가 "0"과 "안 셌다"를 구분한다', () => {
  const unsafe = setupProject();
  try {
    // wikiPath가 root 밖 → 판정 불가. 카드가 없다는 뜻이 아니다.
    const settings = join(unsafe, '.auto-context', 'settings.json');
    const cfg = JSON.parse(readFileSync(settings, 'utf8'));
    cfg.wikiPath = '../outside';
    writeFileSync(settings, JSON.stringify(cfg));
    const r = runLifecycle(unsafe, { QMD_SOURCE_SCAN_MAX: '' });
    assert.equal(r.ineligible, 0);
    assert.equal(r.ineligibleMeasured, false, '판정 불가는 미측정이다');
  } finally { removeTemp(unsafe); }

  const noWiki = setupProject();
  try {
    rmSync(join(noWiki, '.auto-context', 'wiki'), { recursive: true, force: true });
    const r = runLifecycle(noWiki, { QMD_SOURCE_SCAN_MAX: '' });
    assert.equal(r.ineligible, 0);
    assert.equal(r.ineligibleMeasured, true, '카드가 없으면 측정된 0이다');
  } finally { removeTemp(noWiki); }

  const normal = setupProject();
  try {
    writeCard(normal, 'entities/draft.md', { status: 'generated' });
    const r = runLifecycle(normal, { QMD_SOURCE_SCAN_MAX: '' });
    assert.equal(r.ineligible, 1);
    assert.equal(r.ineligibleMeasured, true);
  } finally { removeTemp(normal); }
});

// update.sh worker → SessionStart notice의 **상태 파일**까지가 이 신호의 종점이다.
// 스캐너 단위 테스트만으로는 "worker가 0을 받으면 파일을 지운다"는 마지막 한 칸을 못 잡고,
// 그 칸이 거짓 복구 보고(`notice_clear wiki-ineligible`)의 실제 발생 지점이다.
test('update.sh worker: 미측정 run은 ineligible 상태 파일을 지우지 않는다', () => {
  const base = join(homedir(), '.cache');
  mkdirSync(base, { recursive: true });
  const work = mkdtempSync(join(base, 'qmd-ineligible-'));
  const fakehome = join(work, 'fakehome');
  const bin = join(work, 'bin');
  mkdirSync(fakehome, { recursive: true });
  mkdirSync(bin, { recursive: true });
  writeFileSync(join(bin, 'qmd'), '#!/usr/bin/env sh\nexit 0\n', { mode: 0o755 });
  const statePath = () => {
    const hash = execFileSync('python3', ['-c',
      'import hashlib,sys; print(hashlib.sha256(sys.argv[1].encode()).hexdigest()[:16])', work,
    ], { encoding: 'utf8' }).trim();
    return join(fakehome, `notice-wiki-ineligible-state-${hash}`);
  };
  const runWorker = () => execFileSync('bash', [join(process.cwd(), 'core', 'update.sh'), '--worker', work], {
    encoding: 'utf8',
    env: {
      ...process.env,
      PATH: `${bin}:${process.env.PATH}`,
      HOME: fakehome,
      QMD_CACHE_DIR: fakehome,
      QMD_LOCK_BASE: join(work, 'locks'),
      QMD_BACKEND_MANAGER: '/bin/true',
      QMD_SKIP_BACKGROUND_EMBED: '1',
      QMD_WIKI_LIFECYCLE_LOG: join(work, 'lifecycle.log'),
    },
  });
  const setScan = (sourceScan, extra = {}) => {
    const path = join(work, '.auto-context', 'settings.json');
    const cfg = JSON.parse(readFileSync(path, 'utf8'));
    cfg.compile.sourceScan = sourceScan;
    Object.assign(cfg, extra);
    writeFileSync(path, JSON.stringify(cfg));
  };

  try {
    setupProject({ base: work });
    writeCard(work, 'entities/draft.md', { status: 'generated' });  // provenance 없음
    runWorker();
    assert.equal(readFileSync(statePath(), 'utf8'), '1', '측정된 1장이 상태 파일에 남는다');

    // 수명 주기 조치만 껐다 — ineligible은 계속 측정되므로 수가 유지된다.
    setScan({ enabled: false });
    runWorker();
    assert.equal(readFileSync(statePath(), 'utf8'), '1', 'enabled:false가 수를 0으로 만들지 않는다');

    // 측정 불가(unsafe wikiPath) — 0을 보고하지만 상태 파일을 **지우지 않는다**.
    setScan({ enabled: true }, { wikiPath: '../outside' });
    runWorker();
    assert.equal(existsSync(statePath()), true, '미측정 run이 상태를 지우면 거짓 복구 보고가 된다');
    assert.equal(readFileSync(statePath(), 'utf8'), '1');

    // 실제로 복구되면(카드가 적격) 측정된 0이므로 파일이 사라진다 — 알림도 사라져야 한다.
    setScan({ enabled: true }, { wikiPath: '.auto-context/wiki' });
    writeCard(work, 'entities/draft.md', { status: 'verified' });
    runWorker();
    assert.equal(existsSync(statePath()), false, '측정된 0은 상태 파일을 지운다(notice_clear)');
  } finally { removeTemp(work); }
});

// 수명 주기 판정은 순환 커서로 창을 나눠 여러 회차에 덮지만, `ineligible`은 사용자에게 보여 줄
// **총량**이라 같은 창에 갇히면 틀린 수를 말하게 된다("3장 남았습니다"라고 했는데 실제로는
// 300장인 상태). 전량 집계라는 성질을 여기서 못박는다 — frontmatter만 읽으므로
// 라이브 931장 실측 113ms이고 이 경로는 blocking hook이 아니라 update worker다.
test('wiki lifecycle: ineligible은 순환 커서 창이 아니라 전량을 센다', () => {
  const dir = setupProject();
  try {
    for (let i = 0; i < 5; i += 1) {
      writeCard(dir, `entities/c${i}.md`, { status: 'generated' });
    }
    const r = runLifecycle(dir, { QMD_SOURCE_SCAN_MAX: '1' });
    assert.equal(r.ineligible, 5, 'ineligible은 창과 무관하게 전량이다');
  } finally { removeTemp(dir); }
});

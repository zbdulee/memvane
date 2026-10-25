// 순위 폴백(rank fallback) 계약 테스트.
//
// rerank=false 경로에서 데몬 score는 1/rank라 minScore는 유사도 임계가 아니라 순위
// 컷이다(rank1=1.0, rank2=0.5). 컷을 통과한 소수의 결과가 hard filter
// (skipPaths·recallVerifiedOnly·excludeStatusesFromRecall)에서 떨어지면 recall이 통째로
// 0건이 됐다 — docs/plans/2026-07-29-wiki-only-architecture-review.md 5.0의 라이브 증거.
// 여기서는 hierarchical/wikiOnly 전략에서 "wiki rescue → raw backfill" 순서와
// raw 누출 금지를 검증한다.
//
// 로컬 데몬에 의존하지 않도록 test/helpers/shadow_probe.py 의 stub 데몬을 쓴다
// (node의 execFileSync는 자기 event loop를 막아 테스트 프로세스 안의 http 서버로는
// 훅에 응답할 수 없다).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, statSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { removeTemp } from './helpers/temp.mjs';

const PROMPT = 'recall minScore 필터가 RRF rank 로 동작하는 이유를 설명해줘';

function probe(options = {}) {
  const out = execFileSync('python3', [
    'test/helpers/shadow_probe.py',
    JSON.stringify({ prompt: PROMPT, log: true, ...options }),
  ], { encoding: 'utf8', maxBuffer: 8 * 1024 * 1024 });
  return JSON.parse(out);
}

function selection(result) {
  const lines = result.log.filter((e) => e.event === 'qmd_recall_selection');
  assert.equal(lines.length, 1, 'selection 라인 정확히 1줄');
  return lines[0];
}

// raw 컬렉션으로 나간 질의만 센다. 본 recall 뒤에는 lex 게이트 프로브(무관 주입 차단용
// lex 단독 질의, 주입할 본문이 있을 때 recall 실행당 1회)가 붙으므로 총 건수로는
// "backfill이 돌았는가"를 판정할 수 없다.
function rawQueries(r) {
  return r.queries.filter((q) => (q.collections || []).includes('proj'));
}

test('hierarchical: wiki rescue가 raw backfill보다 먼저 시도된다 (raw 질의 없음)', () => {
  const r = probe({
    scenario: 'filtered-out',
    settings: { recallStrategy: 'hierarchical', minScore: 0.8 },
  });
  assert.match(r.stdout, /card\.md/, '컷 밖 검수 wiki 카드가 구제되어야 함');
  // queries[1]은 lex 게이트 프로브(주입할 본문이 있을 때 recall 실행당 1회). raw 질의가
  // 아니라는 것을 컬렉션으로 못박는다 — 건수만 세면 프로브와 backfill이 구분되지 않는다.
  assert.equal(rawQueries(r).length, 0, 'wiki rescue가 성공하면 raw는 질의조차 하지 않는다');
  const s = selection(r);
  assert.equal(s.reason, 'selected');
  assert.equal(s.selected_count, 1, '정확히 1건');
  assert.equal(s.selected.length, 1);
  assert.equal(s.rank_fallback_used, true);
  assert.equal(s.fallback_phase, 'wiki');
  assert.equal(s.rescued_original_rank, 2);
});

test('hierarchical: eligible wiki가 전혀 없을 때만 raw backfill로 넘어간다', () => {
  const r = probe({
    scenario: 'all-unverified',
    settings: { recallStrategy: 'hierarchical', minScore: 0.8 },
  });
  assert.equal(rawQueries(r).length, 1, 'wiki에 살릴 것이 없으면 raw를 질의한다');
  assert.deepEqual(r.queries[0].collections, ['proj-wiki']);
  assert.deepEqual(r.queries[1].collections, ['proj']);
  assert.match(r.stdout, /raw\.md/, 'raw 원문으로 degrade');
  const s = selection(r);
  // raw 후보는 score 1(=rank1)이라 컷을 통과한다 → backfill 자체는 폴백이 아니다.
  assert.equal(s.rank_fallback_used, false);
});

test('hierarchical: raw phase도 컷 통과 후보가 전멸하면 1건 rescue', () => {
  // raw 1위(score 1)는 컷을 통과하지만 skipPaths로 제거 → 컷 밖 2위(0.5)를 구제한다.
  const r = probe({
    scenario: 'raw-ranked',
    settings: {
      recallStrategy: 'hierarchical',
      minScore: 0.8,
      rawFallbackMinScore: 0.8,
      skipPaths: ['raw.md'],
      topN: 3,
    },
  });
  assert.equal(r.queries.length, 2, 'wiki 0건 → raw backfill');
  assert.match(r.stdout, /other\.md/);
  assert.doesNotMatch(r.stdout, /raw\.md/, 'skip 대상은 구제 대상이 아니다');
  const s = selection(r);
  assert.equal(s.reason, 'selected');
  assert.equal(s.selected_count, 1, 'raw phase도 정확히 1건만 살린다');
  assert.equal(s.selected.length, 1);
  assert.equal(s.rank_fallback_used, true);
  assert.equal(s.fallback_phase, 'raw');
  assert.equal(s.rescued_original_rank, 2);
  assert.equal(s.min_score, 0.8, 'active min_score는 rawFallbackMinScore 그대로');
  assert.ok(s.dropped_min_score > 0, 'raw phase 순위 컷 탈락 수도 그대로 기록');
});

test('rawFallbackMinScore: 1.01 은 raw fallback을 완전히 차단한다 (docs/settings.md 계약)', () => {
  // raw 후보 최고 score가 1.0 → 임계 이상 후보 0건 → 순위 폴백도 살리지 않는다.
  const r = probe({
    scenario: 'all-unverified',
    settings: { recallStrategy: 'hierarchical', minScore: 0.8, rawFallbackMinScore: 1.01 },
  });
  assert.equal(r.queries.length, 2, 'backfill 질의 자체는 일어난다');
  assert.equal(r.stdout.trim(), '', 'raw fallback 차단 설정이 실제로 차단해야 함');
  const s = selection(r);
  assert.equal(s.reason, 'no_results_after_filter');
  assert.equal(s.rank_fallback_used, false);
});

test('minScore로 wiki 후보를 전부 컷하면 wiki rescue 없이 raw backfill로 간다', () => {
  // 컷 이상 wiki 후보가 0건이면(= 명시적 차단) wiki 구제는 일어나지 않아야 한다.
  const r = probe({
    scenario: 'filtered-out',       // wiki 1위 score 1.0(미검수), 2·3위 verified
    // raw 쪽은 통과 가능하게 둬서 "wiki 구제가 안 일어난다"만 분리 검증한다.
    settings: { recallStrategy: 'hierarchical', minScore: 1.01, rawFallbackMinScore: 0.8 },
  });
  assert.equal(r.queries.length, 2, 'wiki 구제 없이 raw backfill로 내려가야 함');
  assert.doesNotMatch(r.stdout, /card\.md/, '컷으로 차단된 wiki 카드가 새면 안 됨');
  assert.match(r.stdout, /raw\.md/);
  assert.equal(selection(r).rank_fallback_used, false);
});

test('EP promotion과 rescue가 겹쳐도 rescued_original_rank는 promotion 전 데몬 순위', () => {
  // 데몬 1위(미검수)와 3위 EP 카드(미검수, promotion으로 score 1.0)가 모두 drop되고
  // 데몬 2위 검수 카드가 구제된다. 재정렬 후 위치는 3이지만 로그는 2여야 하며,
  // shadow의 selected[].original_rank와 같은 값이어야 한다(한 줄 내 정합성).
  const r = probe({
    prompt: 'EP 99 에서 무슨 일이 있었는지 정리해줘',
    shadow: true,
    scenario: 'ep-rescue',
    settings: { minScore: 0.8, lexicalPatterns: ['ep'] },
  });
  assert.match(r.stdout, /card\.md/, '검수 카드가 구제되어야 함');
  assert.doesNotMatch(r.stdout, /ep-99\.md/, '미검수 EP 카드는 promotion돼도 surface 금지');
  const s = selection(r);
  assert.equal(s.rank_fallback_used, true);
  assert.equal(s.rescued_original_rank, 2, 'promotion 전 데몬 순위(재정렬 후 위치 3이 아님)');
  const shadow = r.log.find((e) => e.event === 'qmd_recall_shadow');
  assert.equal(shadow.rescued_original_rank, 2);
  assert.equal(
    shadow.selected[0].original_rank, s.rescued_original_rank,
    'selection과 shadow의 rank가 같은 map을 써야 한다',
  );
});

test('wikiOnly: rescue가 raw를 surface하지 않는다', () => {
  const r = probe({ scenario: 'filtered-out', settings: { minScore: 0.8 } });
  assert.equal(rawQueries(r).length, 0, 'wikiOnly는 raw를 질의하지 않는다');
  assert.match(r.stdout, /card\.md/);
  assert.doesNotMatch(r.stdout, /raw\.md|other\.md|third\.md/, 'raw 누출 금지');
  assert.doesNotMatch(r.stdout, /\[raw\]/);
  assert.equal(selection(r).fallback_phase, 'wiki');
});

test('wikiOnly: 후보 전체가 미검수면 rescue 없이 0건 (raw로 새지 않는다)', () => {
  const r = probe({ scenario: 'all-unverified', settings: { minScore: 0.8 } });
  assert.equal(r.stdout.trim(), '', '미검수는 rescue 대상이 아니다');
  assert.equal(rawQueries(r).length, 0, 'raw 질의 없음');
  const s = selection(r);
  assert.equal(s.reason, 'no_results_after_filter');
  assert.equal(s.dropped_unverified, 1, '컷 통과한 1위가 미검수로 제거된 수는 그대로');
  assert.equal(s.dropped_min_score, 2, '컷 탈락 수도 그대로');
  assert.equal(s.rank_fallback_used, false);
});

test('cutoff 안에 eligible이 있으면 rescue가 일어나지 않는다 (회귀 방지)', () => {
  const r = probe({}); // 기본 시나리오: wiki 1위가 verified
  assert.match(r.stdout, /card\.md/);
  const s = selection(r);
  assert.equal(s.reason, 'selected');
  assert.equal(s.rank_fallback_used, false, '정상 경로는 폴백을 쓰지 않는다');
  assert.equal('rescued_original_rank' in s, false);
  assert.equal('fallback_phase' in s, false);
});

// wiki + raw 가 섞인 결과를 직접 주입해 wikiOnly 방어 코드를 자극한다. 라이브에서는
// wiki 컬렉션만 질의하므로 이 방어는 fixture / 설정 변경 / 컬렉션 role 오설정 같은
// 경로에서만 발동한다 — 혼합 주입 없이는 코드가 실행되지 않는다.
function mixedFixtureProject(dir, settings = {}) {
  mkdirSync(join(dir, '.auto-context', 'wiki', 'concepts'), { recursive: true });
  mkdirSync(join(dir, 'docs'), { recursive: true });
  writeFileSync(join(dir, '.auto-context', 'settings.json'), JSON.stringify({
    indexing: true,
    collections: ['proj-wiki', 'proj'],
    collectionPaths: { 'proj-wiki': '.auto-context/wiki', proj: 'docs' },
    collectionRoles: { 'proj-wiki': 'wiki', proj: 'raw' },
    recallStrategy: 'wikiOnly',
    topN: 3,
    ...settings,
  }));
  const sourcePath = join(dir, 'docs', 'raw.md');
  writeFileSync(sourcePath, '# Raw doc\n');
  const bytes = readFileSync(sourcePath);
  const stat = statSync(sourcePath, { bigint: true });
  const hash = createHash('sha256').update(bytes).digest('hex');
  const revision = `{kind: "file", path: "docs/raw.md", collection: "proj", sha256: "${hash}", size: ${stat.size}, mtimeNs: ${stat.mtimeNs}}`;
  writeFileSync(join(dir, '.auto-context', 'wiki', 'concepts', 'card.md'),
    `---\nstatus: verified\ncreatedBy: qmd-auto-context\nsourceRevisions:\n  - ${revision}\n---\n# Card\n`);
  writeFileSync(join(dir, '.auto-context', 'wiki', 'concepts', 'gen.md'),
    '---\nstatus: generated\n---\n# Generated\n');
}

function runFixture(dir, results, env = {}) {
  const fixture = join(dir, 'mixed-fixture.json');
  writeFileSync(fixture, JSON.stringify({ results }));
  const logPath = join(dir, 'recall.log');
  const stdout = execFileSync('python3', ['core/recall.py'], {
    input: JSON.stringify({ prompt: '위키 카드 설계 결정 내용을 알려줘', cwd: dir }),
    encoding: 'utf8',
    env: { ...process.env, QMD_QUERY_FIXTURE: fixture, QMD_RECALL_LOG: logPath, ...env },
  }).trim();
  const log = readFileSync(logPath, 'utf8').trim().split('\n').filter(Boolean).map((l) => JSON.parse(l));
  return { stdout, selection: log.find((e) => e.event === 'qmd_recall_selection') };
}

test('wikiOnly + 혼합 fixture: raw가 1위여도 절대 surface하지 않고 wiki를 구제한다', () => {
  const dir = mkdtempSync(join(tmpdir(), 'qmd-mixed-'));
  mixedFixtureProject(dir, { minScore: 0.8 });
  try {
    const r = runFixture(dir, [
      { file: 'proj/raw.md', title: 'Raw doc', score: 1 },        // 컷 통과하지만 raw
      { file: 'proj-wiki/concepts/card.md', title: 'Card', score: 0.5 }, // 컷 밖 검수 wiki
    ]);
    assert.match(r.stdout, /card\.md/, '컷 밖 wiki 검수 카드를 구제');
    assert.doesNotMatch(r.stdout, /raw\.md/, 'raw 누출 금지');
    assert.doesNotMatch(r.stdout, /\[raw\]/);
    assert.equal(r.selection.rank_fallback_used, true);
    assert.equal(r.selection.fallback_phase, 'wiki');
  } finally { removeTemp(dir); }
});

test('wikiOnly + 혼합 fixture: wiki가 전부 미검수면 raw를 구제하지 않고 0건', () => {
  const dir = mkdtempSync(join(tmpdir(), 'qmd-mixed-unv-'));
  mixedFixtureProject(dir, { minScore: 0.8 });
  try {
    const r = runFixture(dir, [
      { file: 'proj/raw.md', title: 'Raw doc', score: 1 },
      { file: 'proj-wiki/concepts/gen.md', title: 'Generated', score: 0.5 },
    ]);
    assert.equal(r.stdout, '', 'raw로 degrade하면 wikiOnly 계약 위반');
    assert.equal(r.selection.reason, 'no_results_after_filter');
    assert.equal(r.selection.rank_fallback_used, false);
  } finally { removeTemp(dir); }
});

test('excludeStatusesFromRecall 카드는 rescue 대상이 아니다', () => {
  // 컷 안의 1위는 미검수, 컷 밖 2·3위는 verified지만 status를 exclude 목록에 넣으면
  // eligible이 사라져 wiki rescue가 실패하고 raw backfill로 내려가야 한다.
  const r = probe({
    scenario: 'filtered-out',
    settings: {
      recallStrategy: 'hierarchical',
      minScore: 0.8,
      compile: { excludeStatusesFromRecall: ['verified'] },
    },
  });
  assert.equal(r.queries.length, 2, 'exclude로 wiki eligible이 없으면 raw backfill');
  assert.match(r.stdout, /raw\.md/);
  assert.doesNotMatch(r.stdout, /card\.md/, 'exclude 대상은 어떤 경로로도 surface 금지');
});

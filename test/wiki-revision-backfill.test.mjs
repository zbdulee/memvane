// 로드맵 3.8 — `sourceRevisions` 백필의 자격 판정과 쓰기 계약.
//
// 이 스크립트의 쓰기는 카드에 **recall 신뢰**를 부여한다. 그래서 이 파일이 막는 회귀는
// 두 종류다: (a) 근거가 없는 카드에 신선 배지를 다는 것, (b) 쓰기 실패를 성공으로
// 보고해 "백필됐다"는 기록만 남기는 것. 픽스처는 실제 git 저장소를 만들어 실제 커밋을
// 찍는다 — 판정 재료가 git 이력이므로 mock 으로는 그 판정을 검증할 수 없다.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync, spawnSync } from 'node:child_process';
import { chmodSync, mkdtempSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { removeTemp } from './helpers/temp.mjs';

const SCRIPT = 'scripts/wiki-revision-backfill.py';
const CARD_REL = '.auto-context/wiki/concepts/a.md';

function git(dir, args, env = {}) {
  return execFileSync('git', ['-C', dir, ...args], {
    encoding: 'utf8',
    env: {
      ...process.env,
      GIT_CONFIG_GLOBAL: '/dev/null',
      GIT_CONFIG_SYSTEM: '/dev/null',
      GIT_AUTHOR_NAME: 'fixture', GIT_AUTHOR_EMAIL: 'fixture@example.com',
      GIT_COMMITTER_NAME: 'fixture', GIT_COMMITTER_EMAIL: 'fixture@example.com',
      ...env,
    },
  }).trim();
}

function card({ sources = ['  - {kind: file, path: "docs/a.md", collection: "proj-docs"}'],
                status = 'verified', createdBy = 'qmd-auto-context',
                revisions = null } = {}) {
  return [
    '---',
    'title: "Card A"',
    'type: concept',
    `status: ${status}`,
    'created: 2026-01-01',
    'updated: 2026-01-01',
    `createdBy: ${createdBy}`,
    'confidence: medium',
    'sources:',
    ...sources,
    ...(revisions === null ? [] : ['sourceRevisions:', ...revisions]),
    'triggers: []',
    '---',
    '',
    '<!-- qmd:auto:start id="main" sourceHash="deadbeef" -->',
    '## Summary',
    'Durable fact: the port is 8080.',
    '<!-- qmd:auto:end -->',
    '',
  ].join('\n');
}

// 원문 커밋 → 그 뒤에 컴파일된 카드. 즉 기본 픽스처는 "건전 백필 가능"이다.
// 개별 테스트는 이 기본형에서 **한 가지만** 어긋나게 해서 그 한 조건을 검증한다.
function setupProject({
  commitDate = '2026-01-01T00:00:00+00:00',
  compileTs = '2026-01-02T00:00:00Z',
  manifestSources = [{ kind: 'file', path: 'docs/a.md', collection: 'proj-docs' }],
  action = 'created',
  omitTs = false,
  cardOptions = {},
  trackSource = true,
  extraCommitAfter = null,
  backdatedCommit = null,
  mergeFeatAt = null,
  mergeAt = null,
} = {}) {
  const dir = mkdtempSync(join(tmpdir(), 'qmd-revbf-'));
  mkdirSync(join(dir, '.auto-context', 'compile'), { recursive: true });
  mkdirSync(join(dir, '.auto-context', 'wiki', 'concepts'), { recursive: true });
  mkdirSync(join(dir, 'docs'), { recursive: true });
  writeFileSync(join(dir, 'docs', 'a.md'), '# a\n\nThe port is 8080.\n');
  writeFileSync(join(dir, 'docs', 'b.md'), '# b\n\nA second source.\n');
  writeFileSync(join(dir, '.auto-context', 'settings.json'), JSON.stringify({
    indexing: true,
    collections: ['proj-docs', 'proj-wiki'],
    collectionPaths: { 'proj-docs': 'docs', 'proj-wiki': '.auto-context/wiki' },
    collectionRoles: { 'proj-docs': 'raw', 'proj-wiki': 'wiki' },
    wikiPath: '.auto-context/wiki',
  }));
  writeFileSync(join(dir, CARD_REL), card(cardOptions));

  git(dir, ['init', '-q', '-b', 'main']);
  if (trackSource) {
    git(dir, ['add', 'docs/a.md', 'docs/b.md']);
    git(dir, ['commit', '-q', '-m', 'sources'],
        { GIT_AUTHOR_DATE: commitDate, GIT_COMMITTER_DATE: commitDate });
  } else {
    // 원문이 버전 관리 밖이면 판정 재료가 없다. 다른 파일만 커밋해 HEAD 는 존재하게 한다.
    writeFileSync(join(dir, 'README.md'), '# fixture\n');
    git(dir, ['add', 'README.md']);
    git(dir, ['commit', '-q', '-m', 'init'],
        { GIT_AUTHOR_DATE: commitDate, GIT_COMMITTER_DATE: commitDate });
  }
  if (mergeFeatAt) {
    // `--no-ff` merge 로 새 내용이 트리에 들어오지만 **브랜치 커밋 날짜는 컴파일보다
    // 앞선다**. pathspec 이력 단순화(`log -1 -- path`)가 그 브랜치 커밋을 보고하므로
    // 시각 비교만으로는 "원문 그대로"로 읽힌다.
    git(dir, ['checkout', '-q', '-b', 'feat']);
    writeFileSync(join(dir, 'docs', 'a.md'), '# a\n\nThe port is 9999.\n');
    git(dir, ['add', 'docs/a.md']);
    git(dir, ['commit', '-q', '-m', 'feat change'],
        { GIT_AUTHOR_DATE: mergeFeatAt, GIT_COMMITTER_DATE: mergeFeatAt });
    git(dir, ['checkout', '-q', 'main']);
    git(dir, ['merge', '-q', '--no-ff', '-m', 'merge feat', 'feat'],
        { GIT_AUTHOR_DATE: mergeAt, GIT_COMMITTER_DATE: mergeAt });
  }
  if (extraCommitAfter) {
    writeFileSync(join(dir, 'docs', 'a.md'), '# a\n\nThe port is 9999.\n');
    git(dir, ['add', 'docs/a.md']);
    git(dir, ['commit', '-q', '-m', 'change source'],
        { GIT_AUTHOR_DATE: extraCommitAfter, GIT_COMMITTER_DATE: extraCommitAfter });
  }
  if (backdatedCommit) {
    // 두 날짜를 **엇갈리게** 준 실제 커밋. author 는 컴파일 이후인데 committer 만
    // 과거로 되돌려져 있다(import·`filter-branch`·`GIT_COMMITTER_DATE` 명시가 내는 모양).
    writeFileSync(join(dir, 'docs', 'a.md'), '# a\n\nThe port is 9999.\n');
    git(dir, ['add', 'docs/a.md']);
    git(dir, ['commit', '-q', '-m', 'backdated committer'],
        { GIT_AUTHOR_DATE: backdatedCommit.authorDate,
          GIT_COMMITTER_DATE: backdatedCommit.committerDate });
  }

  const record = {
    action, targetPath: CARD_REL, sources: manifestSources,
    ...(omitTs ? {} : { ts: compileTs }),
  };
  writeFileSync(join(dir, '.auto-context', 'compile', 'generated-manifest.jsonl'),
    `${JSON.stringify(record)}\n`);
  return dir;
}

// 상위 git 저장소 **안**에 프로젝트 루트를 둔 픽스처. `git -C <project> rev-parse
// HEAD:<rel>` 의 `<rel>` 은 cwd 가 아니라 저장소 top-level 기준이므로, 접두 없이
// 조회하면 같은 이름의 **상위 파일**이 나온다. 두 파일 내용을 다르게 두어 어느 쪽을
// 봤는지가 분류로 드러나게 한다.
function setupNestedProject({
  commitDate = '2026-01-01T00:00:00+00:00',
  compileTs = '2026-01-02T00:00:00Z',
  changedAfter = '2026-01-03T00:00:00+00:00',
} = {}) {
  const outer = mkdtempSync(join(tmpdir(), 'qmd-revbf-outer-'));
  const dir = join(outer, 'project');
  mkdirSync(join(outer, 'docs'), { recursive: true });
  mkdirSync(join(dir, '.auto-context', 'compile'), { recursive: true });
  mkdirSync(join(dir, '.auto-context', 'wiki', 'concepts'), { recursive: true });
  mkdirSync(join(dir, 'docs'), { recursive: true });
  // 상위의 동명 파일은 **끝까지 바뀌지 않는다** — 그래서 접두를 빼먹은 조회는 항상
  // "원문 그대로"로 읽힌다. 내용은 하위 프로젝트의 *현재* 본문과 같게 두어, 워킹 트리
  // 대조까지 통과시켜 잘못된 판정이 끝까지 도달하게 한다.
  writeFileSync(join(outer, 'docs', 'a.md'), '# a\n\nThe port is 9999.\n');
  writeFileSync(join(dir, 'docs', 'a.md'), '# a\n\nThe port is 8080.\n');
  writeFileSync(join(dir, '.auto-context', 'settings.json'), JSON.stringify({
    indexing: true,
    collections: ['proj-docs', 'proj-wiki'],
    collectionPaths: { 'proj-docs': 'docs', 'proj-wiki': '.auto-context/wiki' },
    collectionRoles: { 'proj-docs': 'raw', 'proj-wiki': 'wiki' },
    wikiPath: '.auto-context/wiki',
  }));
  writeFileSync(join(dir, CARD_REL), card());

  git(outer, ['init', '-q', '-b', 'main']);
  git(outer, ['add', 'docs/a.md', 'project/docs/a.md']);
  git(outer, ['commit', '-q', '-m', 'sources'],
      { GIT_AUTHOR_DATE: commitDate, GIT_COMMITTER_DATE: commitDate });
  // 컴파일 **이후** 하위 프로젝트의 원문만 바뀐다 → 올바른 분류는 source_newer 다.
  writeFileSync(join(dir, 'docs', 'a.md'), '# a\n\nThe port is 9999.\n');
  git(outer, ['add', 'project/docs/a.md']);
  git(outer, ['commit', '-q', '-m', 'change nested source'],
      { GIT_AUTHOR_DATE: changedAfter, GIT_COMMITTER_DATE: changedAfter });

  writeFileSync(join(dir, '.auto-context', 'compile', 'generated-manifest.jsonl'),
    `${JSON.stringify({
      action: 'created', targetPath: CARD_REL, ts: compileTs,
      sources: [{ kind: 'file', path: 'docs/a.md', collection: 'proj-docs' }],
    })}\n`);
  return { outer, dir };
}

// 쓰기 실패는 이제 exit 1 이므로 status 를 삼키지 않고 함께 돌려준다.
function runFull(dir, extraArgs = []) {
  const res = spawnSync('python3', [SCRIPT, dir, '--json', ...extraArgs], {
    cwd: process.cwd(), encoding: 'utf8',
  });
  assert.equal(res.stderr, '', `stderr 가 비어야 한다: ${res.stderr}`);
  const report = JSON.parse(res.stdout);
  return { status: res.status, report, result: report.projects[dir] };
}

function run(dir, extraArgs = []) {
  const { status, result } = runFull(dir, extraArgs);
  assert.equal(status, 0, '쓰기 실패가 없으면 exit 0 이다');
  return result;
}

function python(script, ...args) {
  return execFileSync('python3', ['-c', script, ...args], {
    cwd: process.cwd(), encoding: 'utf8',
  }).trim();
}

// 백필된 카드를 recall 이 실제로 신뢰하는가 + freshness 가 fresh 로 보는가.
// 스크립트가 "적용 1"을 보고하는 것만으로는 목적 달성을 증명하지 못한다 —
// 3.8 의 목적은 그 카드가 recall 에 나오는 것이다.
function trustState(dir) {
  return JSON.parse(python([
    'import json, sys',
    'from pathlib import Path',
    'sys.path.insert(0, "core")',
    'import recall as R, wiki_compile as wc, wiki_freshness as WF, wiki_source_missing as wsm',
    'root = Path(sys.argv[1]).resolve()',
    'text = (root / sys.argv[2]).read_text(encoding="utf-8")',
    'block = wc.FRONTMATTER_RE.match(text).group(1)',
    'meta = R.parse_frontmatter_scalars(block)',
    'revisions = R.frontmatter_source_revisions(block)',
    'meta = {"status": meta.get("status"), "createdBy": meta.get("createdBy"),',
    '        "sourceRevisions": revisions}',
    'config = json.loads((root / ".auto-context/settings.json").read_text())',
    'fresh = WF.check_card({"sourceRevisions": revisions}, root, wsm.allow_roots_of(config))',
    'print(json.dumps({"trusted": R.is_auto_trusted_card(meta),',
    '                  "revisions": revisions, "freshness": fresh}))',
  ].join('\n'), dir, CARD_REL));
}

test('건전 케이스(원문 커밋 < 컴파일 시각, HEAD 일치)는 백필되고 그 카드가 recall 신뢰를 통과한다', () => {
  // 3.8 의 목적 자체. 쓰기가 났다는 것과 recall 이 그 카드를 신뢰하는 것은 다른 사실이라
  // 둘을 함께 단정한다(예전에 status 만 찍고 증명 필드가 빠진 카드 27장이 라이브에 남았다).
  const dir = setupProject();
  try {
    const before = run(dir);
    assert.equal(before.counts.backfillable, 1, 'dry-run 분류가 건전 케이스를 잡아야 한다');
    assert.equal(trustState(dir).trusted, false, '백필 전에는 신뢰되지 않는다');

    const after = run(dir, ['--apply']);
    assert.equal(after.applied, 1);
    assert.equal(after.writeFailed, 0);
    assert.equal(after.sourceMismatch, 0);

    const state = trustState(dir);
    assert.equal(state.trusted, true, 'is_auto_trusted_card 를 통과해야 한다');
    assert.equal(state.revisions.length, 1);
    assert.equal(state.revisions[0].path, 'docs/a.md');
    assert.equal(state.revisions[0].collection, 'proj-docs',
      'collection 은 ts·path 를 준 같은 매니페스트 레코드에서 온다');
    assert.equal(state.freshness.state, 'fresh', '찍은 해시는 현재 원문과 일치해야 한다');

    const text = readFileSync(join(dir, CARD_REL), 'utf8');
    assert.equal((text.match(/^sourceRevisions:/gm) || []).length, 1,
      'top-level sourceRevisions 헤더는 정확히 하나여야 한다(중복이면 파서가 거부한다)');
    assert.match(text, /^status: verified$/m, 'status 는 건드리지 않는다');
  } finally {
    removeTemp(dir);
  }
});

test('--dry-run(기본)은 카드를 한 바이트도 바꾸지 않는다', () => {
  // 분류만 보려는 실행이 신뢰를 부여해 버리면, 라이브에서 판정을 확인할 방법이 없어진다.
  const dir = setupProject();
  try {
    const original = readFileSync(join(dir, CARD_REL), 'utf8');
    const implicit = run(dir);
    assert.equal(readFileSync(join(dir, CARD_REL), 'utf8'), original);
    assert.equal(implicit.applied, 0, 'dry-run 은 적용 0 이다');
    run(dir, ['--dry-run']);
    assert.equal(readFileSync(join(dir, CARD_REL), 'utf8'), original);
  } finally {
    removeTemp(dir);
  }
});

test('원문이 컴파일보다 새로우면 source_newer 로 분류하고 건드리지 않는다', () => {
  // 이 카드는 재컴파일(3.2) 대상이다. 여기서 찍으면 낡은 본문에 신선 배지를 단다.
  const dir = setupProject({ extraCommitAfter: '2026-01-03T00:00:00+00:00' });
  try {
    const result = run(dir, ['--apply']);
    assert.equal(result.counts.source_newer, 1);
    assert.equal(result.reasons['source_newer:content_changed_since_compile'], 1);
    assert.equal(result.counts.backfillable, 0);
    assert.equal(result.applied, 0);
    assert.equal(trustState(dir).trusted, false);
  } finally {
    removeTemp(dir);
  }
});

test('merge 로 들어온 변경을 놓치지 않는다 — 판정은 시각이 아니라 blob 이다', () => {
  // `git log -1 --format=%cI -- <path>` 는 pathspec 이력 단순화로 **feat 브랜치 커밋**을
  // 보고한다. base(1/1)=X → feat(1/15)=Y → `--no-ff` merge(8/1) 이고 카드가 5/1 에 X 로
  // 컴파일됐으면, plain 은 1/15 를 보고해 "컴파일보다 앞선다" = 건전으로 읽고 **X 카드에
  // sha256(Y) 를 찍는다**(라이브 102 target 중 2건이 이 경로였고 blob 이 우연히 같아
  // 무해했을 뿐이다). `--first-parent` 를 더하는 것으로 고치지 않는다 — 시각 추론은
  // 어느 트리를 볼지 고르는 데만 쓰고 "바뀌지 않았다"는 내용으로 증명한다.
  const dir = setupProject({
    commitDate: '2026-01-01T00:00:00+00:00',
    mergeFeatAt: '2026-01-15T00:00:00+00:00',
    mergeAt: '2026-08-01T00:00:00+00:00',
    compileTs: '2026-05-01T00:00:00Z',
  });
  try {
    const result = run(dir, ['--apply']);
    assert.equal(result.counts.source_newer, 1,
      '컴파일 시점 mainline blob 과 HEAD blob 이 다르다');
    assert.equal(result.reasons['source_newer:content_changed_since_compile'], 1);
    assert.equal(result.counts.backfillable, 0);
    assert.equal(result.applied, 0);
    assert.equal(trustState(dir).trusted, false);
  } finally {
    removeTemp(dir);
  }
});

test('컴파일 시각이 로컬 오프셋이면 UTC 로 정규화해 base 커밋을 고른다', () => {
  // 매니페스트 `ts` 에 오프셋이 붙을 수 있고(라이브 13행이 `+00:00`), 그 값이 그대로
  // `--before` 로 가면 어느 트리를 base 로 볼지가 달라진다. compileTs 09:00+09:00
  // = 01-02 00:00Z 이므로 01-01 23:30Z 커밋은 base 이고 01-02 00:30Z 커밋은 그 뒤다.
  // 후자가 내용을 바꿨으므로 base blob ≠ HEAD blob = source_newer 여야 한다.
  const dir = setupProject({
    commitDate: '2026-01-01T23:30:00+00:00',
    compileTs: '2026-01-02T09:00:00+09:00',
    extraCommitAfter: '2026-01-02T00:30:00+00:00',
  });
  try {
    const result = run(dir, ['--apply']);
    assert.equal(result.counts.source_newer, 1);
    assert.equal(result.applied, 0);
  } finally {
    removeTemp(dir);
  }
});

test('committer 만 과거로 되돌려진 커밋은 앵커 자격이 없다 — author date 도 본다', () => {
  // `rev-list --before` 는 **committer date** 만 본다. author 는 컴파일 이후인데
  // committer 가 과거로 되돌려진 커밋(import·`filter-branch`·`GIT_COMMITTER_DATE`
  // 명시)이 있으면 그것이 앵커로 뽑히고, 그 blob 은 HEAD blob 과 같으니 대조를
  // **통과**한다 — 즉 X 로 컴파일된 카드에 Y 의 sha256 을 찍는다. 앵커 자격을
  // `max(author, committer) <= compiledAt` 로 두면 그 커밋이 탈락하고 base(X)가
  // 앵커가 되어 blob 이 달라 거부된다. 이 픽스처는 그 한 가지만 어긋나게 한다.
  const dir = setupProject({
    commitDate: '2026-01-01T00:00:00+00:00',
    compileTs: '2026-08-02T00:00:00Z',
    backdatedCommit: {
      authorDate: '2026-08-03T00:00:00+00:00',
      committerDate: '2026-01-01T12:00:00+00:00',
    },
  });
  try {
    const result = run(dir, ['--apply']);
    assert.equal(result.counts.source_newer, 1, '앵커는 base(X) 여야 한다');
    assert.equal(result.counts.backfillable, 0);
    assert.equal(result.applied, 0, '컴파일 시점 내용이 아닌 blob 을 찍어서는 안 된다');
  } finally {
    removeTemp(dir);
  }
});

test('프로젝트 루트가 git 저장소 하위면 상위의 동명 파일을 보지 않는다', () => {
  // `git -C <project> rev-parse HEAD:docs/a.md` 의 경로는 **저장소 top-level 기준**
  // 이라 접두(`project/`)를 빼면 상위 저장소의 `docs/a.md` blob 이 나온다. 픽스처는
  // 상위 파일을 하위의 *현재* 본문과 같게 두어 워킹 트리 대조까지 통과시키므로,
  // 접두가 없으면 "컴파일 이후 안 바뀜"으로 읽혀 **stale 카드에 신뢰가 부여된다**.
  // 올바른 판정은 하위 파일이 컴파일 이후 바뀐 사실을 본 source_newer 다.
  const { outer, dir } = setupNestedProject();
  try {
    const result = run(dir, ['--apply']);
    assert.equal(result.counts.source_newer, 1,
      '하위 프로젝트의 원문이 컴파일 이후 바뀌었다');
    assert.equal(result.counts.backfillable, 0);
    assert.equal(result.applied, 0);
  } finally {
    removeTemp(outer);
  }
});

test('컴파일 시각 이전 mainline 커밋이 없으면 undecidable', () => {
  // 대조할 트리가 없다. 시각만으로 "안 바뀌었다"를 추론하지 않는다.
  const dir = setupProject({
    commitDate: '2026-01-03T00:00:00+00:00',
    compileTs: '2026-01-02T00:00:00Z',
  });
  try {
    const result = run(dir, ['--apply']);
    assert.equal(result.counts.undecidable, 1);
    assert.equal(result.reasons['undecidable:no_base_commit'], 1);
    assert.equal(result.applied, 0);
  } finally {
    removeTemp(dir);
  }
});

test('소스가 둘 이상이면 undecidable — 단일 소스만 판정한다', () => {
  // 다중 소스 카드는 "어느 원문이 카드의 어느 문장을 뒷받침하는가"를 git 만으로 가릴 수
  // 없다. 하나라도 판정 못 하면 전체 provenance 가 부분적으로만 참이 된다.
  const dir = setupProject({
    manifestSources: [
      { kind: 'file', path: 'docs/a.md', collection: 'proj-docs' },
      { kind: 'file', path: 'docs/b.md', collection: 'proj-docs' },
    ],
  });
  try {
    const result = run(dir, ['--apply']);
    assert.equal(result.counts.undecidable, 1);
    assert.equal(result.reasons['undecidable:multi_source'], 1);
    assert.equal(result.applied, 0);
  } finally {
    removeTemp(dir);
  }
});

test('워킹 트리 내용이 HEAD 와 다르면 dirty 로 보류한다', () => {
  // 커밋되지 않은 편집은 "컴파일 이후 바뀌지 않았다"의 반례일 수 있다.
  const dir = setupProject();
  try {
    writeFileSync(join(dir, 'docs', 'a.md'), '# a\n\nThe port is 9999.\n');
    const result = run(dir, ['--apply']);
    assert.equal(result.counts.dirty, 1);
    assert.equal(result.reasons['dirty:working_tree_differs_from_head'], 1);
    assert.equal(result.applied, 0);
  } finally {
    removeTemp(dir);
  }
});

test('매니페스트에 컴파일 시각이 없으면 undecidable — 카드 mtime 으로 대체하지 않는다', () => {
  // 카드 mtime 은 verify 스탬프·dedup 병합으로도 갱신되므로 컴파일 시각의 프록시가
  // 아니다. 그 프록시를 쓰면 라이브에서 415장을 후보로 잡지만 git 검증은 40장이다.
  const dir = setupProject({ omitTs: true });
  try {
    const result = run(dir, ['--apply']);
    assert.equal(result.counts.undecidable, 1);
    assert.equal(result.reasons['undecidable:no_manifest_ts'], 1);
    assert.equal(result.applied, 0);
  } finally {
    removeTemp(dir);
  }
});

test('원문이 사라졌으면 source_missing — 삭제도 백필도 하지 않는다', () => {
  // 원문 소실 카드는 SessionStart 수명 주기 몫이다. 존재 판정은 recall 의 SSOT 를 쓴다.
  const dir = setupProject({
    manifestSources: [{ kind: 'file', path: 'docs/gone.md', collection: 'proj-docs' }],
    cardOptions: { sources: ['  - {kind: file, path: "docs/gone.md", collection: "proj-docs"}'] },
  });
  try {
    const result = run(dir, ['--apply']);
    assert.equal(result.counts.source_missing, 1);
    assert.equal(result.reasons['source_missing:missing'], 1);
    assert.equal(result.applied, 0);
  } finally {
    removeTemp(dir);
  }
});

test('git 이력이 없는 원문은 undecidable — 판정 재료가 없으면 찍지 않는다', () => {
  // 버전 관리 밖의 원문에는 "컴파일 이후 바뀌지 않았다"를 뒷받침할 증거가 하나도 없다.
  const dir = setupProject({ trackSource: false });
  try {
    const result = run(dir, ['--apply']);
    assert.equal(result.counts.undecidable, 1);
    assert.equal(result.reasons['undecidable:no_git_history'], 1);
    assert.equal(result.applied, 0);
  } finally {
    removeTemp(dir);
  }
});

test('이미 sourceRevisions 헤더가 있으면(파싱 불가여도) 두 번째 헤더를 붙이지 않는다', () => {
  // `frontmatter_source_revisions` 는 깨진 블록도 "증거 없음"(`[]`)으로 낸다. 그 값으로
  // 대상을 고르면 헤더가 이미 있는 카드에 두 번째 헤더가 붙고, 같은 파서가 중복 선언을
  // 거부하므로 카드만 망가지고 신뢰는 얻지 못한다.
  const dir = setupProject({
    cardOptions: { revisions: ['  - {kind: file, path: "docs/a.md"'] },
  });
  try {
    const result = run(dir, ['--apply']);
    assert.equal(result.counts.undecidable, 1);
    assert.equal(result.reasons['undecidable:invalid_existing_revisions'], 1);
    assert.equal(result.applied, 0);
    const text = readFileSync(join(dir, CARD_REL), 'utf8');
    assert.equal((text.match(/^sourceRevisions:/gm) || []).length, 1);
  } finally {
    removeTemp(dir);
  }
});

test('쓰기가 실패하면 적용 성공으로 보고하지 않고 exit 1 + 실패 카드 경로를 낸다', () => {
  // CLAUDE.md "쓰기 반환값" 기준 1(작업 유실·상태 오염). 삼키면 카드는 그대로인데
  // 보고는 "백필됨"이라 다음 사람이 recall 침묵을 설명할 수 없다.
  // **exit 0 이면 보고가 정직해도 호출자·CI 는 성공으로 읽는다.** 그리고 개수만으로는
  // 어느 카드가 안 찍혔는지 알 수 없으므로 경로 목록이 함께 나와야 한다.
  const dir = setupProject();
  const cardDir = join(dir, '.auto-context', 'wiki', 'concepts');
  try {
    chmodSync(cardDir, 0o500);  // write_text_atomic 의 임시파일 생성이 실패한다
    const { status, report, result } = runFull(dir, ['--apply']);
    assert.equal(status, 1, '쓰기 실패가 있으면 non-zero 로 종료한다');
    assert.equal(report.writeFailedTotal, 1);
    assert.equal(result.counts.backfillable, 1, '분류는 그대로 건전 백필 가능이다');
    assert.equal(result.applied, 0, '실패를 적용으로 세지 않는다');
    assert.equal(result.writeFailed, 1, '실패가 표면화되어야 한다');
    assert.deepEqual(result.writeFailedCards, [CARD_REL], '어느 카드인지 나와야 한다');
    chmodSync(cardDir, 0o700);
    assert.equal(trustState(dir).trusted, false, '카드는 그대로 미신뢰 상태다');
  } finally {
    chmodSync(cardDir, 0o700);
    removeTemp(dir);
  }
});

test('사람용 출력에도 실패 카드 경로가 나온다', () => {
  // `--json` 만 신원을 알면 사람이 그 run 을 진단할 수 없다.
  const dir = setupProject();
  const cardDir = join(dir, '.auto-context', 'wiki', 'concepts');
  try {
    chmodSync(cardDir, 0o500);
    const res = spawnSync('python3', [SCRIPT, dir, '--apply'],
      { cwd: process.cwd(), encoding: 'utf8' });
    assert.equal(res.status, 1);
    assert.match(res.stdout, /\[쓰기 실패\]/);
    assert.ok(res.stdout.includes(CARD_REL), `카드 경로가 있어야 한다:\n${res.stdout}`);
  } finally {
    chmodSync(cardDir, 0o700);
    removeTemp(dir);
  }
});

test('카드가 선언한 소스를 읽을 수 없으면 fail-closed — 가드가 우회되지 않는다', () => {
  // `if (declared && ...)` 형태였을 때 미지원 표기(block mapping)면 집합이 비어 가드가
  // 통째로 우회됐다: 카드 `sources` 는 docs/a.md, 매니페스트는 docs/b.md 인데
  // `applied=1 sourceMismatch=0` 이었다. 결과는 **자기모순 provenance 로 신뢰를 얻은
  // 카드**다(freshness 는 b 만 보고, 주입 `↳` 는 검증되지 않은 a 를 모델에 준다).
  // "키 없음"과 "키 있음 + 값 미지"를 구분하고 후자는 fail-closed — `COLLECTION_ROLE_INVALID`
  // 와 같은 규칙이다. 사유도 `sourceMismatch`("다르다")와 분리한다("읽을 수 없다").
  const dir = setupProject({
    manifestSources: [{ kind: 'file', path: 'docs/b.md', collection: 'proj-docs' }],
    cardOptions: { sources: ['  - kind: file', '    path: "docs/a.md"'] },
  });
  try {
    const dry = runFull(dir);
    assert.equal(dry.result.sourcesUndeterminable, 1, 'dry-run 도 센다');
    assert.equal(dry.result.sourceMismatch, 0, '"다르다"와 뭉치지 않는다');
    assert.deepEqual(dry.result.sourcesUndeterminableCards, [CARD_REL]);
    assert.equal(dry.result.targets.length, 0);

    const { status, result } = runFull(dir, ['--apply']);
    assert.equal(status, 0, '보류는 쓰기 실패가 아니다');
    assert.equal(result.applied, 0, '찍지 않는다');
    assert.equal(trustState(dir).trusted, false);
  } finally {
    removeTemp(dir);
  }
});

test('백필은 원 frontmatter 의 모든 줄을 순서대로 보존한다', () => {
  // 삽입이 다른 줄을 재작성·재배열하면 title·status·sources 가 조용히 바뀔 수 있다.
  // 판정은 "삽입한 블록을 빼면 원본과 축자 동일"이다(경험적 확인이 아니라 단정).
  // **범위 단서 — 줄바꿈은 예외다.** `wiki_compile` 의 `read_text`/`write_text_atomic`
  // 이 universal newline 으로 CRLF 를 LF 로 정규화하므로 CRLF 카드는 바이트가 바뀐다.
  // 이 계약은 "줄의 내용과 순서"에 대한 것이고 줄바꿈 표기까지 포함하지 않는다
  // (기존 파이프라인 성질이고 라이브 해당 0장이라 고치지 않기로 한 것).
  const dir = setupProject();
  try {
    const before = readFileSync(join(dir, CARD_REL), 'utf8');
    const beforeFm = before.split('---\n')[1].split('\n---\n')[0].split('\n');
    run(dir, ['--apply']);
    const after = readFileSync(join(dir, CARD_REL), 'utf8');
    const afterFm = after.split('---\n')[1].split('\n---\n')[0].split('\n');
    const inserted = afterFm.filter((line, i) => {
      const start = afterFm.indexOf('sourceRevisions:');
      return i === start || (i > start && line.startsWith('  - {kind: "file"'));
    });
    assert.equal(inserted[0], 'sourceRevisions:');
    assert.equal(inserted.length, 2, '헤더 1줄 + 항목 1줄');
    const withoutInserted = afterFm.filter((line) => !inserted.includes(line));
    assert.deepEqual(withoutInserted, beforeFm, '나머지 줄은 순서까지 그대로여야 한다');
    // 그리고 블록은 `sources` 섹션 **직후**여야 한다(writer 형태와 같은 자리).
    assert.equal(afterFm[afterFm.indexOf('sourceRevisions:') - 1],
      '  - {kind: file, path: "docs/a.md", collection: "proj-docs"}');
    // 본문(auto 블록 포함)은 손대지 않는다.
    assert.equal(after.split('\n---\n').slice(1).join('\n---\n'),
      before.split('\n---\n').slice(1).join('\n---\n'));
  } finally {
    removeTemp(dir);
  }
});

test('미검수·외부 카드는 대상이 아니다', () => {
  // 이 스크립트는 provenance 만 채운다 — status 를 올리는 경로가 아니다.
  const generated = setupProject({ cardOptions: { status: 'generated' } });
  const foreign = setupProject({ cardOptions: { createdBy: 'someone-else' } });
  try {
    for (const dir of [generated, foreign]) {
      const result = run(dir, ['--apply']);
      assert.deepEqual(result.counts,
        { backfillable: 0, source_newer: 0, source_missing: 0, undecidable: 0, dirty: 0 });
      assert.equal(result.applied, 0);
    }
  } finally {
    removeTemp(generated);
    removeTemp(foreign);
  }
});

test('카드가 적은 소스와 매니페스트 경로가 어긋나면 찍지 않는다', () => {
  // `sources` 는 파일 A 를 가리키는데 `sourceRevisions` 는 파일 B 의 해시인 카드는
  // 그 자체로 provenance 가 틀린 상태다. 라이브 dry-run 실측으로 4프로젝트에 3건 있고
  // (service-engineering 2 / ai-proxy 1), 어긋난 카드에 신뢰를 주는 것이 이 스크립트가
  // 만들 수 있는 최악의 결과라 분리해 센다.
  // **dry-run 도 같은 수를 센다** — apply 전용 판정이면 dry-run 이 "찍힐 수"를 예고하지
  // 못하고(분류 N 인데 적용 N-k), 라이브에서 그 수를 확인할 유일한 모드가 dry-run 이라
  // 0건이라는 실측 자체가 불가능해진다.
  const dir = setupProject({
    cardOptions: { sources: ['  - {kind: file, path: "docs/b.md", collection: "proj-docs"}'] },
  });
  try {
    const dry = run(dir);
    assert.equal(dry.sourceMismatch, 1, 'dry-run 도 불일치를 센다');
    assert.equal(dry.targets.length, 0, '찍을 대상 목록에서 빠진다');

    const result = run(dir, ['--apply']);
    assert.equal(result.counts.backfillable, 1, '분류 자체는 매니페스트 기준이다');
    assert.equal(result.applied, 0);
    assert.equal(result.sourceMismatch, 1);
    assert.equal(trustState(dir).trusted, false);
  } finally {
    removeTemp(dir);
  }
});

test('백필된 카드는 wiki_compile 이 쓴 카드와 같은 형태다', () => {
  // 직렬화를 새로 구현하면 읽는 쪽과 갈린다(라이브 731장 중 8장 title 오염이 그 사례).
  // 백필 결과가 writer 형태와 같은지를 `markdown_page` 의 출력과 대조한다.
  const dir = setupProject();
  try {
    run(dir, ['--apply']);
    const text = readFileSync(join(dir, CARD_REL), 'utf8');
    const line = text.split('\n').find((l) => l.trim().startsWith('- {kind: "file"'));
    assert.ok(line, `flow mapping 항목이 있어야 한다:\n${text}`);
    const emitted = python([
      'import sys',
      'sys.path.insert(0, "core")',
      'import yaml_scalars as ys',
      'rev = {"kind": "file", "path": "docs/a.md", "collection": "proj-docs",',
      '       "sha256": "0" * 64, "size": 1, "mtimeNs": 2}',
      'print("  - " + ys.dump_source_revisions([rev])[0])',
    ].join('\n'));
    // 키 순서·인용 규칙이 writer 와 같아야 한다(값은 픽스처별로 다르므로 키 목록만 본다).
    const keysOf = (s) => (s.match(/(\w+):/g) || []).join(',');
    assert.equal(keysOf(line), keysOf(emitted));
  } finally {
    removeTemp(dir);
  }
});

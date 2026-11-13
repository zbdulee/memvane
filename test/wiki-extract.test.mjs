import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { existsSync, linkSync, mkdtempSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { homedir } from 'node:os';
import { removeTemp } from './helpers/temp.mjs';
import { stampVerified, wikiTrustState } from './helpers/wiki_trust.mjs';

function repoTemp(prefix) {
  const base = join(homedir(), '.cache');
  mkdirSync(base, { recursive: true });
  return mkdtempSync(join(base, `qmd-test-${prefix}-`));
}

function writeSettings(work, compile = {}) {
  mkdirSync(join(work, '.auto-context'), { recursive: true });
  writeFileSync(join(work, '.auto-context', 'settings.json'), JSON.stringify({
    indexing: true,
    collections: ['proj-wiki'],
    collectionPaths: { 'proj-wiki': '.auto-context/wiki' },
    collectionRoles: { 'proj-wiki': 'wiki' },
    wikiPath: '.auto-context/wiki',
    compile: {
      mode: 'auto-wiki',
      defaultStatus: 'generated',
      ...compile,
    },
  }));
}

function runExtract(work, payload, env = {}) {
  // QMD_DIRTY_QUEUE를 임시 경로로 격리하지 않으면 dirty queue 유출 시 사용자 실제 qmd 인덱스에 고아 컬렉션이 등록되며(복구 불가 고아 등록), 자동으로 정리할 수 있는 경로가 없다.
  return execFileSync('python3', ['entry/run.py', 'wiki-extract', '--cwd', work], {
    encoding: 'utf8',
    input: JSON.stringify(payload),
    env: { QMD_DIRTY_QUEUE: join(work, 'dirty-queue'), ...process.env, ...env },
  });
}

test('wiki_extract: compact durable summary becomes wiki_compile candidate and page', () => {
  const work = repoTemp('wiki-extract-compact');
  try {
    writeSettings(work);
    const out = runExtract(work, {
      trigger: 'manual',
      sourceRef: 'session:local',
      durable: {
        title: 'Config Layout Decision',
        summary: 'Canonical config lives in .auto-context/settings.json; legacy root config is migration-only.',
        type: 'decision',
        confidence: 'high',
      },
    });

    assert.match(out, /created/);
    const page = readFileSync(join(work, '.auto-context', 'wiki', 'decisions', 'config-layout-decision.md'), 'utf8');
    assert.match(page, /Canonical config lives/);
    assert.match(page, /kind: "session"/);
    assert.match(page, /ref: "session:local"/);
  } finally {
    removeTemp(work);
  }
});

test('wiki_extract: preserves canonicalKey and aliases from compact candidates', () => {
  const work = repoTemp('wiki-extract-identity');
  try {
    writeSettings(work);
    runExtract(work, {
      trigger: 'manual',
      sourceRef: 'session:identity',
      durable: {
        title: 'Signal Perception Rule',
        canonicalKey: 'signal-perception-rule',
        aliases: ['Signal rule'],
        summary: 'Identity fields from compact input should reach the compile writer.',
        type: 'concept',
        confidence: 'high',
      },
    });

    const page = readFileSync(join(work, '.auto-context', 'wiki', 'concepts', 'signal-perception-rule.md'), 'utf8');
    assert.match(page, /canonicalKey: "signal-perception-rule"/);
    assert.match(page, /aliases:\n  - "Signal rule"/);
  } finally {
    removeTemp(work);
  }
});

test('wiki_extract: raw transcript-shaped input is rejected before compile writer persists it', () => {
  const work = repoTemp('wiki-extract-transcript');
  try {
    writeSettings(work);
    const out = runExtract(work, {
      trigger: 'post_session_summary',
      sourceRef: 'session:local',
      durable: {
        title: 'Transcript Dump',
        canonicalKey: 'transcript-dump',
        aliases: ['Transcript dump alias'],
        summary: 'User: save this entire chat\nAssistant: ok I will',
        type: 'session',
        confidence: 'high',
      },
    });

    assert.match(out, /rejected/);
    assert.equal(existsSync(join(work, '.auto-context', 'wiki', 'sessions', 'transcript-dump.md')), false);
    const candidate = readFileSync(join(work, '.auto-context', 'compile', 'candidates.jsonl'), 'utf8');
    assert.match(candidate, /transcript_like/);
    assert.doesNotMatch(candidate, /User: save this entire chat/);
    assert.match(candidate, /transcript-dump/);
    assert.match(candidate, /Transcript dump alias/);
  } finally {
    removeTemp(work);
  }
});

test('wiki_extract: no durable summary is a no-op with empty stdout', () => {
  const work = repoTemp('wiki-extract-noop');
  try {
    writeSettings(work);
    const out = runExtract(work, { trigger: 'manual', notes: 'brainstorm only' });
    assert.equal(out.trim(), '');
    assert.equal(existsSync(join(work, '.auto-context', 'compile', 'candidates.jsonl')), false);
  } finally {
    removeTemp(work);
  }
});

// ─────────────────────────────────────────────────────────────────────────────
// 수동 경로의 provenance 소유권. 자동 훅 경로는 `wiki_compile_worker` 가 자기가 읽은
// 파일의 스냅샷을 candidate 에 대입해 provenance 를 소유하지만, 수동 경로에는 큐도
// worker 도 없어 예전에는 `sourceRevisions` 가 아예 비었다 — 그 카드는
// `recall.is_auto_trusted_card`(status + createdBy + **비어 있지 않은**
// sourceRevisions)를 영구히 통과하지 못해 검수를 받아도 recall 에 나오지 않는다.
// caller 가 정하는 것은 "어느 파일인가"뿐이고 스냅샷은 컴파일러가 직접 읽어 만든다.
function seedSource(work, rel, body) {
  const abs = join(work, rel);
  mkdirSync(dirname(abs), { recursive: true });
  writeFileSync(abs, body);
  return createHash('sha256').update(readFileSync(abs)).digest('hex');
}

test('wiki_extract: caller 가 준 sourceRevisions 는 버려지고 컴파일러가 읽은 파일 스냅샷이 실린다', () => {
  // decoy + malformed 를 동시에 보낸다. 여기서 caller 값이 채택되면 recall 신뢰의
  // 근거를 caller 가 스스로 발급하는 것이 되고, 그것이 이 신뢰 경계의 존재 이유다.
  const work = repoTemp('wiki-extract-provenance');
  try {
    writeSettings(work);
    const sha = seedSource(work, 'docs/api.md', '# API\n\n포트는 8080이다.\n');
    runExtract(work, {
      trigger: 'manual',
      candidates: [{
        title: 'API Port',
        summary: 'The API listens on port 8080 per docs/api.md.',
        suggestedType: 'concept',
        sources: [{ kind: 'file', path: 'docs/api.md' }, { kind: 'session', ref: 'chat' }],
        sourceRevisions: [
          { kind: 'file', path: 'decoy.md', collection: '', sha256: '0'.repeat(64), size: 1, mtimeNs: 1 },
          { kind: 'file', path: 'docs/api.md' },
        ],
      }],
    });
    const cardRel = join('.auto-context', 'wiki', 'concepts', 'api-port.md');
    const text = readFileSync(join(work, cardRel), 'utf8');
    assert.match(text, new RegExp(`sha256: "${sha}"`), '컴파일러가 읽은 파일 전체 해시');
    assert.doesNotMatch(text, /decoy\.md/);
    assert.doesNotMatch(text, new RegExp('sha256: "0{64}"'));

    // 규칙의 마지막 링크: 카드에 실린 provenance 와 **검수가 읽는 근거**가 같은 집합이어야
    // 한다. 갈리면 "검증은 A 를 보는데 provenance 는 B 를 가리킨다"가 된다.
    const job = JSON.parse(readFileSync(
      join(work, '.auto-context', 'compile', 'verify-queue.jsonl'), 'utf8').trim().split('\n')[0]);
    assert.equal(job.authoritativeSources.length, 1);
    assert.equal(job.authoritativeSources[0].path, 'docs/api.md');
    assert.equal(job.sourceRevisions[0].sha256, sha);

    const state = wikiTrustState(work, cardRel);
    assert.equal(state.revisions.length, 1, '비파일 소스(session)는 스냅샷되지 않는다');
    assert.equal(state.revisions[0].path, 'docs/api.md');
    assert.equal(state.trusted, false, 'generated 카드는 아직 신뢰되지 않는다');

    stampVerified(work, cardRel);
    const verified = wikiTrustState(work, cardRel);
    assert.equal(verified.trusted, true, '검수를 받으면 recall 신뢰를 통과해야 한다');
    assert.equal(verified.freshness.state, 'fresh');
  } finally {
    removeTemp(work);
  }
});

test('wiki_extract: 파일 소스가 없으면 provenance 키를 만들지 않는다(기존 잡 형태 불변)', () => {
  // 기본 수동 입력은 `{kind: session, ref: ...}` 하나다. 스냅샷할 파일이 없는데 키를
  // 만들면 빈 목록이 근거처럼 보이고, `dump_source_revisions` 도 그것을 버린다.
  const work = repoTemp('wiki-extract-no-file-source');
  try {
    writeSettings(work);
    runExtract(work, {
      trigger: 'manual',
      sourceRef: 'session:local',
      durable: { title: 'Session Only', summary: 'No file source backs this durable note.', type: 'concept' },
    });
    const text = readFileSync(join(work, '.auto-context', 'wiki', 'concepts', 'session-only.md'), 'utf8');
    assert.doesNotMatch(text, /^sourceRevisions:$/m);
    assert.match(text, /kind: "session"/);
  } finally {
    removeTemp(work);
  }
});

test('wiki_extract: 파일 소스 하나라도 스냅샷 못 하면 provenance 를 전혀 내지 않는다', () => {
  // 전부 아니면 전무. 부분 목록은 완결된 근거처럼 보이는데 검수는 그 목록을 읽어야
  // 하고, 없는 파일·root 밖 경로는 `wiki_verify_worker.load_sources` 가 어차피 거부한다.
  const work = repoTemp('wiki-extract-partial-provenance');
  try {
    writeSettings(work);
    seedSource(work, 'docs/api.md', '# API\n\n포트는 8080이다.\n');
    for (const [slug, sources] of [
      ['missing-sibling', [{ kind: 'file', path: 'docs/api.md' }, { kind: 'file', path: 'docs/gone.md' }]],
      // 존재하지만 root 밖인 절대 경로 — `missing` 이 아니라 `outside_root` 로 걸려야 한다.
      ['escaping-source', [{ kind: 'file', path: '/etc/hosts' }]],
    ]) {
      runExtract(work, {
        trigger: 'manual',
        candidates: [{
          title: slug, summary: `Provenance must stay absent for ${slug} input.`,
          suggestedType: 'concept', sources,
        }],
      });
      const text = readFileSync(join(work, '.auto-context', 'wiki', 'concepts', `${slug}.md`), 'utf8');
      assert.doesNotMatch(text, /^sourceRevisions:$/m, `${slug}: 부분 provenance 금지`);
    }
  } finally {
    removeTemp(work);
  }
});

test('wiki_extract: 검수가 읽는 파일 소스 폭은 wiki_verify_worker.MAX_SOURCES 와 같다', () => {
  // `authoritativeSources` 는 verifier 에서 `MAX_SOURCES` 로 잘리지 않는다(반드시 읽어야
  // 하는 근거이기 때문이다) — 즉 caller 목록 길이가 곧 유료 검수의 읽기 폭이다. 두 상수가
  // 갈리면 수동 카드 하나가 그 폭을 넘겨 읽게 되므로 코드에서 유도해 단정한다.
  const values = JSON.parse(execFileSync('python3', ['-c', [
    'import json, sys',
    'sys.path.insert(0, ".")',
    'import memvane.wiki.compile.wiki_extract as we; import memvane.wiki.compile.wiki_verify_worker as vw',
    'print(json.dumps([we.MAX_PROVENANCE_FILE_SOURCES, vw.MAX_SOURCES]))',
  ].join('\n')], { cwd: process.cwd(), encoding: 'utf8' }));
  assert.equal(values[0], values[1]);
});

// 같은 물리 파일의 **별칭**은 별개 provenance 가 아니다. 그리고 상한이 중복 제거보다 먼저
// 걸리면 별칭 4개짜리 목록이 provenance 를 통째로 없애고(= 그 카드는
// `recall.is_auto_trusted_card` 를 영구히 통과하지 못해 recall 에 절대 나오지 않는다) 별칭
// 2개는 같은 파일을 두 번 세어 유료 검수의 읽기 폭을 헛되게 소진한다.
test('wiki_extract: 같은 파일의 별칭은 접히고 상한은 서로 다른 파일 수에 걸린다', () => {
  const work = repoTemp('wiki-extract-alias-provenance');
  try {
    writeSettings(work);
    const sha = seedSource(work, 'docs/api.md', '# API\n\n포트는 8080이다.\n');
    // 별칭은 **하드링크**로 만든다. 대소문자 무시·NFC↔NFD 별칭은 마운트마다 성립 여부가
    // 다르고, 심볼릭 링크는 `resolve()` 가 정규화해 중복 제거에 도달조차 하지 않는다.
    // 하드링크는 어떤 문자열 정규화로도 같아지지 않으므로 `(st_dev, st_ino)` 동치
    // (= `os.path.samefile`) 판정만이 접을 수 있다.
    const aliases = ['docs/a1.md', 'docs/a2.md', 'docs/a3.md'];
    for (const alias of aliases) linkSync(join(work, 'docs/api.md'), join(work, alias));

    runExtract(work, {
      trigger: 'manual',
      candidates: [{
        title: 'Alias Card',
        summary: 'The API listens on port 8080 per docs/api.md.',
        suggestedType: 'concept',
        sources: ['docs/api.md', ...aliases].map((path) => ({ kind: 'file', path })),
      }],
    });
    const cardRel = join('.auto-context', 'wiki', 'concepts', 'alias-card.md');
    const state = wikiTrustState(work, cardRel);
    assert.equal(state.revisions.length, 1, '별칭 4개는 서로 다른 파일 1개다(상한에 걸리지 않는다)');
    assert.equal(state.revisions[0].path, 'docs/api.md', '겹치면 먼저 나온 rel 이 남는다(first-wins)');
    assert.equal(state.revisions[0].sha256, sha);
    // caller 가 준 `sources:` 는 그대로 남는다(그것은 caller 의 의견이다) — 접히는 것은
    // **컴파일러가 소유한** provenance 쪽이다. 그래서 별칭 부재는 그 블록에서만 단정한다.
    const revisionLines = readFileSync(join(work, cardRel), 'utf8')
      .split('\n')
      .filter((line) => line.startsWith('  - {') && line.includes('sha256:'));
    assert.equal(revisionLines.length, 1, 'sourceRevisions 항목은 하나뿐이다');
    for (const alias of aliases) {
      assert.ok(!revisionLines[0].includes(alias), `${alias}: 같은 파일을 두 번 세지 않는다`);
    }
    const job = JSON.parse(readFileSync(
      join(work, '.auto-context', 'compile', 'verify-queue.jsonl'), 'utf8').trim().split('\n').at(-1));
    assert.deepEqual(job.authoritativeSources.map((entry) => entry.path), ['docs/api.md'],
      '검수가 읽는 근거도 같은 집합이어야 한다');

    // 상한 자체는 그대로다 — 서로 다른 파일이 상한을 넘으면 여전히 전무(검수 읽기 폭).
    for (const rel of ['docs/d1.md', 'docs/d2.md', 'docs/d3.md', 'docs/d4.md']) {
      seedSource(work, rel, `# ${rel}\n\nDistinct body for ${rel}.\n`);
    }
    // 조사할 항목 수도 상한의 2배로 유계다 — 별칭이든 아니든 창을 넘기면 전무다.
    for (const [slug, sources] of [
      ['four-distinct', ['docs/d1.md', 'docs/d2.md', 'docs/d3.md', 'docs/d4.md']],
      ['over-scan-budget', ['docs/api.md', ...aliases, 'docs/d1.md', 'docs/d2.md', 'docs/d3.md']],
    ]) {
      runExtract(work, {
        trigger: 'manual',
        candidates: [{
          title: slug, summary: `Provenance must stay absent for ${slug} input.`,
          suggestedType: 'concept', sources: sources.map((path) => ({ kind: 'file', path })),
        }],
      });
      const text = readFileSync(join(work, '.auto-context', 'wiki', 'concepts', `${slug}.md`), 'utf8');
      assert.doesNotMatch(text, /^sourceRevisions:$/m, `${slug}: 상한·조사 예산은 유지된다`);
    }
  } finally {
    removeTemp(work);
  }
});

test('wiki_extract: provenance 조사 예산은 파일 소스 상한의 2배로 유계다', () => {
  // 중복 제거는 항목을 다 열어 본 뒤에야 확정되므로 상한을 항목 수에 걸 수 없다. 그렇다고
  // 무한히 조사하면 caller 목록 길이가 곧 syscall 수가 되므로 예산을 코드에서 유도해 단정한다.
  const values = JSON.parse(execFileSync('python3', ['-c', [
    'import json, sys',
    'sys.path.insert(0, ".")',
    'import memvane.wiki.compile.wiki_extract as we',
    'print(json.dumps([we.MAX_PROVENANCE_FILE_SOURCES, we.MAX_PROVENANCE_SOURCE_SCAN]))',
  ].join('\n')], { cwd: process.cwd(), encoding: 'utf8' }));
  assert.equal(values[1], values[0] * 2);
});

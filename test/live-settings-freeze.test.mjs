// 라이브 9개 프로젝트의 `settings.json` 사본을 `normalize_config()`에 통과시킨
// **effective config**를 동결한다.
//
// `config-emission-freeze.test.mjs`와 짝이지만 덮는 모집단이 다르다 — 저쪽은 writer가
// **새로** 만드는 프로젝트고, 이쪽은 손으로 편집된 값을 가진 **기존** 파일이다
// (`verify.maxPerRun` 15/3/60 · `semanticDedup.maxPairsPerScan` 100 · 커스텀 `skipPaths` ·
// `recallStrategy: wikiOnly` · legacy novel 컬렉션). 스키마 정리 리팩터
// (H단계)는 이 파일들을
// 진리표대로 마이그레이션하는데, 마이그레이션 후 effective가 여기와 달라지면 그것이 곧
// **조용한 동작 변경**이다(§7.2가 경고하는 "새 키로 이관하지 않으면 기본값으로 떨어진다").
//
// 스냅샷 갱신은 의도적으로 수동이다 — `QMD_FREEZE_UPDATE=1 node --test <이 파일>`.
// 리팩터 중에 무심코 재생성하면 동결의 의미가 사라지므로, 갱신 시에는 diff를 반드시 읽을 것.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { existsSync, mkdirSync, readFileSync, readdirSync, writeFileSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const REPO_ROOT = resolve(fileURLToPath(import.meta.url), '..', '..');
const CORE = join(REPO_ROOT, 'core');
const LIVE_DIR = join(REPO_ROOT, 'test', 'fixtures', 'live-settings');
const FIXTURE = join(REPO_ROOT, 'test', 'fixtures', 'live-settings-effective-freeze.json');
const UPDATE = process.env.QMD_FREEZE_UPDATE === '1';

// `normalize_config`는 dict→dict 순수 함수라 데몬·HOME·시계에 의존하지 않는다.
// 그래서 이 파일은 프로젝트 디렉터리를 만들지 않고 파일 내용만 통과시킨다.
const NORMALIZE_ALL_PY = `import json, os, sys
sys.path.insert(0, sys.argv[1])
import config as qmd_config
out = {}
for name in sorted(os.listdir(sys.argv[2])):
    if not name.endswith(".json"):
        continue
    with open(os.path.join(sys.argv[2], name), encoding="utf-8") as fh:
        out[name] = qmd_config.normalize_config(json.load(fh))
print(json.dumps(out, ensure_ascii=False, sort_keys=True))
`;

function normalizeAll() {
  const out = execFileSync('python3', ['-c', NORMALIZE_ALL_PY, CORE, LIVE_DIR], {
    cwd: REPO_ROOT,
    encoding: 'utf8',
  });
  return JSON.parse(out);
}

const fixtureNames = readdirSync(LIVE_DIR).filter((n) => n.endsWith('.json')).sort();
const captured = normalizeAll();

if (UPDATE) {
  mkdirSync(join(REPO_ROOT, 'test', 'fixtures'), { recursive: true });
  writeFileSync(FIXTURE, `${JSON.stringify(captured, null, 2)}\n`);
}

assert.equal(
  existsSync(FIXTURE),
  true,
  `스냅샷이 없다. 최초 1회: QMD_FREEZE_UPDATE=1 node --test test/live-settings-freeze.test.mjs`,
);
const snapshot = JSON.parse(readFileSync(FIXTURE, 'utf8'));

test('라이브 fixture가 조용히 빠지거나 늘지 않는다', () => {
  assert.deepEqual(Object.keys(snapshot).sort(), fixtureNames);
  assert.ok(fixtureNames.length >= 9, `fixture가 ${fixtureNames.length}개뿐이다`);
});

// fixture가 머신 의존이 되면 이 스냅샷은 다른 머신에서 즉시 깨진다. 실측 9개에는
// 절대 경로가 하나도 없었고(`collectionPaths`는 전부 프로젝트 상대), 그 성질을 못박는다.
// 새 fixture를 추가할 때 홈 경로가 섞여 들어오는 것을 여기서 잡는다.
test('fixture에 머신 의존 절대 경로가 없다', () => {
  for (const name of fixtureNames) {
    const text = readFileSync(join(LIVE_DIR, name), 'utf8');
    const raw = JSON.parse(text);
    const abs = [];
    const walk = (node, path) => {
      if (typeof node === 'string') {
        if (node.startsWith('/') || node.startsWith('~') || node.includes('/Users/')) abs.push(`${path}=${node}`);
      } else if (Array.isArray(node)) {
        node.forEach((v, i) => walk(v, `${path}[${i}]`));
      } else if (node && typeof node === 'object') {
        for (const [k, v] of Object.entries(node)) walk(v, path ? `${path}.${k}` : k);
      }
    };
    walk(raw, '');
    assert.deepEqual(abs, [], `${name}: 절대 경로가 섞여 있다`);
  }
});

for (const name of fixtureNames) {
  test(`effective config 동결: ${name}`, () => {
    assert.deepEqual(captured[name], snapshot[name]);
  });
}

// 아래 두 테스트는 blob 동결이 놓치는 것을 겨냥한다. deep-equal 스냅샷은 "무엇이 바뀌었나"는
// 잡지만 "이 값이 왜 중요한가"를 남기지 않아, 마이그레이션에서 통째로 재생성되면 조용히
// 승인된다. 계획 §7.2가 이름을 대고 경고한 값들은 별도로 못박는다.
// 하위호환은 의도적으로 폐기됐다(계획 §6 H단계는 파일을 손으로 옮긴다). 그래서 이
// fixture들은 **아직 마이그레이션되지 않은** 파일이고, 옛 위치에 적힌 값은 이제 읽히지
// 않는다. 그 사실을 숨기지 않고 두 방향으로 단정한다:
//   (a) 옛 위치의 값은 실제로 유실된다 — 마이그레이션 부채의 정확한 목록.
//   (b) §2/§7.2가 지정한 **새 위치**에 옮겨 적으면 클램프까지 포함해 원래 effective가
//       복원된다 — H단계가 무엇을 써야 하는지의 명세.
// (b)가 없으면 이 파일은 "값이 사라졌다"만 말하고 복구 방법을 남기지 않는다.
test('§7.2 마이그레이션 부채: 옛 위치의 손편집 budget 값은 더 이상 읽히지 않는다', () => {
  const b = (n) => captured[n].compile.budget;
  const sd = (n) => captured[n].compile.semanticDedup;
  // verify.maxPerRun 15 / 3 / 60(→50 클램프)이 전부 기본값 3으로 떨어진다.
  assert.equal(b('service-engineering.json').verifyPerRun, 3);
  assert.equal(b('ai-proxy.json').verifyPerRun, 3);
  assert.equal(b('novel_귀신은_약효가_돌_때_보인다.json').verifyPerRun, 3);
  // 제거된 키는 정규화 출력에 남지 않는다(남으면 소비자가 다시 읽기 시작한다).
  for (const name of fixtureNames) {
    assert.equal('maxPerRun' in captured[name].compile.verify, false, `${name}: verify.maxPerRun`);
    assert.equal('maxPerRun' in captured[name].compile.batch, false, `${name}: batch.maxPerRun`);
    assert.equal('maxCardsPerSource' in captured[name].compile, false, `${name}: maxCardsPerSource`);
    for (const k of ['threshold', 'topK', 'similarPageMaxChars', 'autoMergeThreshold']) {
      assert.equal(k in sd(name), false, `${name}: semanticDedup.${k}`);
    }
  }
  // 반대로 `semanticDedup.maxPairsPerScan`은 **살아 있는 키**다(무료 score gate). 유일한
  // 손편집 값인 novel/귀신의 100이 클램프 없이 그대로 남는 것이 이 리팩터의 계약이다.
  assert.equal(sd('novel_귀신은_약효가_돌_때_보인다.json').maxPairsPerScan, 100);
});

test('§7.2 마이그레이션 명세: 새 위치로 옮기면 클램프까지 포함해 복원된다', () => {
  const py = `import json, sys
sys.path.insert(0, sys.argv[1])
import config as qmd_config
raw = {"compile": {"mode": "auto-wiki", "budget": json.loads(sys.argv[2])}}
print(json.dumps(qmd_config.normalize_config(raw)["compile"]["budget"]))`;
  const budgetOf = (v) => JSON.parse(execFileSync(
    'python3', ['-c', py, CORE, JSON.stringify(v)], { cwd: REPO_ROOT, encoding: 'utf8' },
  ));
  // service-engineering: 15는 MAX_VERIFY_PER_RUN(50) 아래라 그대로.
  assert.equal(budgetOf({ verifyPerRun: 15 }).verifyPerRun, 15);
  assert.equal(budgetOf({ verifyPerRun: 3 }).verifyPerRun, 3);
  // novel/귀신: 원본 60은 클램프돼 50이 된다 — "60 보존"이 아니라 "effective 50 보존"이
  // 목표다. 클램프가 budget으로 함께 옮겨오지 않았으면 여기서 60이 나와 깨진다.
  assert.equal(budgetOf({ verifyPerRun: 60 }).verifyPerRun, 50);
  // 나머지 넷도 옛 위치의 값이 그대로 옮겨진다(전 프로젝트 공통 기본값이라 이관 부채는 없다).
  assert.deepEqual(budgetOf({}), {
    extractorPerRun: 10, cardsPerSource: 10, verifyPerRun: 3,
    dedupPairsPerScan: 8, dedupPairsPerCompile: 1,
  });
});

test('compile을 켠 3개 프로젝트의 게이트 입력이 보존된다', () => {
  // 마이그레이션 진리표(§2)의 입력. 셋 다 구 `auto-wiki + autoWrite:true`였으므로 새
  // `mode`도 `auto-wiki`이고, 나머지 6개는 전부 `off`다. 이 파일들은 아직 구 형식이지만
  // `mode` 값 자체는 진리표가 지정한 목표와 같아 결과가 보존된다(그 셋의 `enabled:true`가
  // 무시되는 것과 무관하게).
  const on = ['ai-proxy.json', 'novel_귀신은_약효가_돌_때_보인다.json', 'service-engineering.json'];
  for (const name of fixtureNames) {
    const c = captured[name].compile;
    assert.equal(c.mode, on.includes(name) ? 'auto-wiki' : 'off', `${name}: mode`);
    // 죽은 스위치는 정규화 출력에서 사라졌다 — 남아 있으면 게이트가 다시 읽을 수 있다.
    assert.equal('enabled' in c, false, `${name}: compile.enabled`);
    assert.equal('autoWrite' in c, false, `${name}: compile.autoWrite`);
  }
});

// compile을 켠 3개는 큐에 든 잡을 실제로 처리한다 — `dispatch` 게이트가 사라진 뒤에도
// 그 셋의 엔진이 **여전히 해석되는지**가 이 리팩터의 원자성 근거였다(해석 실패는
// `missing_extractor`이고 그것은 requeue가 아니라 **잡 폐기**다).
test('compile을 켠 3개 프로젝트는 dispatch 키 없이도 엔진이 해석된다', () => {
  const seng = captured['service-engineering.json'].compile.extractor;
  assert.deepEqual(Object.keys(seng.backends).sort(), ['claude', 'codex', 'hermes']);
  for (const name of ['ai-proxy.json', 'novel_귀신은_약효가_돌_때_보인다.json']) {
    assert.deepEqual(
      captured[name].compile.extractor.builtins,
      ['claude', 'codex', 'hermes'],
      `${name}: builtins`,
    );
  }
});

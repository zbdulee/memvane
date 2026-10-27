// 제거된 설정 키 알림(계획 §5 / 단계 G)의 두 반쪽을 검증한다.
//
// 1. **왕복 무잔소리**(계획 §6.2 T2) — writer 4벌(=5경로)이 방금 쓴 settings.json을
//    `config.deprecated_keys()`에 통과시키면 **0건**이어야 한다. 하나라도 나오면
//    도구가 자기가 쓴 설정에 대해 영구히 잔소리하는 루프가 된다(자기 잔소리 루프).
//    이 가드가 없으면 delta-only 생성기에 제거된 키가 한 줄 되살아나도 아무도 모른다.
// 2. **감지가 실제로 발화한다** — 라이브 구형 config(실물 fixture)에서 기대한 키가
//    보고되고, 옮겨진 키는 **행선지를 함께** 말한다. 1번만 있으면 "아무것도 감지하지
//    않는 구현"이 통과한다.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { existsSync, mkdirSync, readdirSync, writeFileSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { isolatedHomeProject, removeTemp, tempCacheDir } from './helpers/temp.mjs';

const REPO_ROOT = resolve(fileURLToPath(import.meta.url), '..', '..');
const CORE = join(REPO_ROOT, 'core');
const LIVE_DIR = join(REPO_ROOT, 'test', 'fixtures', 'live-settings');

// update.sh --worker의 detached embed fork가 호출자 반환 뒤에도 workdir에 써서
// removeTemp와 경합한다(ENOTEMPTY). 이 파일 전역에서 끈다.
process.env.QMD_SKIP_BACKGROUND_EMBED = '1';
// worker fork 자체도 막는다. 그 자식은 스크립트를 처음부터 재실행하며 선두의
// `mkdir -p "$_QMD_CACHE_DIR"` 로 이 파일이 이미 지운 임시 디렉터리를 되살린다
// (`removeTemp` 는 성공하는데 그 **뒤에** 되살아나 정리 쪽에서는 막을 수 없다).
// embed 가드는 fork **안**의 단계만 끄므로 이 잔해를 막지 못한다.
process.env.QMD_SKIP_BACKGROUND_WORKER = '1';

const CACHE = tempCacheDir('deprecated-keys');
const PROJECT_NAME = 'depproj';

function childEnv(home) {
  return {
    ...process.env,
    HOME: home,
    QMD_CACHE_DIR: CACHE,
    CLAUDE_PLUGIN_ROOT: REPO_ROOT,
    QMD_SKIP_BACKGROUND_EMBED: '1',
    QMD_FAKE_PLATFORMS: 'none',
    QMD_BACKEND_MANAGER: '/bin/true',
    QMD_SANDBOX: '',
  };
}

const DEPRECATED_PY = `import json, sys
sys.path.insert(0, sys.argv[1])
import config as qmd_config
with open(sys.argv[2], encoding="utf-8") as fh:
    raw = json.load(fh)
print(json.dumps({
    "records": qmd_config.deprecated_keys(raw),
    "notice": qmd_config.deprecated_key_notice(raw),
}, ensure_ascii=False))
`;

/** settings.json 경로 → {records, notice} */
function detect(settingsPath) {
  const out = execFileSync('python3', ['-c', DEPRECATED_PY, CORE, settingsPath], {
    cwd: REPO_ROOT,
    encoding: 'utf8',
  });
  return JSON.parse(out);
}

function captureWriter(prefix, run) {
  const { home, dir } = isolatedHomeProject(prefix, PROJECT_NAME);
  try {
    run(dir, childEnv(home));
    const settings = join(dir, '.auto-context', 'settings.json');
    assert.equal(existsSync(settings), true, `writer가 settings.json을 쓰지 않았다: ${settings}`);
    return detect(settings);
  } finally {
    removeTemp(home);
  }
}

function sh(args, dir, env) {
  execFileSync('bash', [join(CORE, 'update.sh'), ...args, dir], {
    cwd: REPO_ROOT,
    encoding: 'utf8',
    env,
  });
}

// --- 1. 왕복 무잔소리 (writer 4벌 = 5경로) ---------------------------------
// 모집단은 config-emission-freeze.test.mjs와 같다(그쪽은 effective 동결, 이쪽은
// deprecated 0건). writer가 늘면 두 파일 모두에 추가해야 한다.

const WRITERS = {
  'wiki_compile_defaults.compile_block': () => captureWriter('dep-block', (dir, env) => {
    mkdirSync(join(dir, '.auto-context'), { recursive: true });
    const py = `import json, sys
sys.path.insert(0, sys.argv[1])
import wiki_compile_defaults as d
with open(sys.argv[2], "w", encoding="utf-8") as fh:
    json.dump({"compile": d.compile_block(sys.argv[1])}, fh, ensure_ascii=False)
`;
    execFileSync('python3', ['-c', py, CORE, join(dir, '.auto-context', 'settings.json')], {
      cwd: REPO_ROOT, encoding: 'utf8', env,
    });
  }),

  'update.sh --init-wiki': () => captureWriter('dep-initwiki', (dir, env) => {
    sh(['--init-wiki'], dir, env);
  }),

  'update.sh --init-wiki --preset novel': () => captureWriter('dep-novel', (dir, env) => {
    sh(['--init-wiki', '--preset', 'novel'], dir, env);
  }),

  'update.sh --enable-compile': () => captureWriter('dep-enable', (dir, env) => {
    mkdirSync(join(dir, '.auto-context'), { recursive: true });
    mkdirSync(join(dir, 'docs'), { recursive: true });
    writeFileSync(join(dir, '.auto-context', 'settings.json'), `${JSON.stringify({
      indexing: true,
      collections: [`${PROJECT_NAME}-docs`],
      collectionPaths: { [`${PROJECT_NAME}-docs`]: 'docs' },
    }, null, 2)}\n`);
    sh(['--enable-compile'], dir, env);
  }),

  'update.sh --optin --recommended': () => captureWriter('dep-optin', (dir, env) => {
    mkdirSync(join(dir, 'docs', 'current'), { recursive: true });
    mkdirSync(join(dir, 'docs', 'plans'), { recursive: true });
    sh(['--optin', '--recommended'], dir, env);
  }),
};

for (const [name, capture] of Object.entries(WRITERS)) {
  test(`왕복 무잔소리: ${name}`, () => {
    const { records, notice } = capture();
    assert.deepEqual(
      records, [],
      `${name}이(가) 스키마에서 제거된 키를 쓴다 — 도구가 자기가 쓴 설정에 잔소리한다: ${notice}`,
    );
    assert.equal(notice, '');
  });
}

// --- 2. 감지가 실제로 발화한다 ---------------------------------------------

test('구형 라이브 config에서 제거된 키를 보고한다', () => {
  const { records, notice } = detect(join(LIVE_DIR, 'service-engineering.json'));
  const keys = records.map((r) => r.key);
  // 이 fixture가 실제로 담고 있는 구형 키(전수).
  assert.deepEqual(keys.slice().sort(), [
    'compile.autoWrite',
    'compile.candidatePath',
    'compile.enabled',
    'compile.extractor.default',
    'compile.extractor.dispatch',
    'compile.manifestPath',
    'compile.requireReviewForCanon',
    'compile.sourceQueuePath',
    'compile.tombstonePath',
    'compile.verify.maxPerRun',
  ]);
  // 옮겨진 키는 **행선지를 말해야** 한다 — "무시됩니다"만 알리면 사용자가 값을
  // 어디에 다시 적어야 하는지 모른다(라이브 값 15가 그대로 유실된다).
  const relocated = records.find((r) => r.key === 'compile.verify.maxPerRun');
  assert.equal(relocated.kind, 'relocated');
  assert.equal(relocated.replacement, 'compile.budget.verifyPerRun');
  assert.match(notice, /compile\.verify\.maxPerRun → compile\.budget\.verifyPerRun/);
  // mode로 흡수된 스위치는 "제거"가 아니라 "통합"으로 말한다.
  assert.equal(records.find((r) => r.key === 'compile.enabled').kind, 'folded');
  assert.equal(records.find((r) => r.key === 'compile.autoWrite').replacement, 'compile.mode');
  assert.match(notice, /compile\.mode로 통합/);
  assert.match(notice, /제거됨/);
});

test('라이브 fixture 9개 중 구형 키를 가진 것이 다수다 (감지 모집단 확인)', () => {
  const names = readdirSync(LIVE_DIR).filter((n) => n.endsWith('.json')).sort();
  assert.ok(names.length >= 9);
  const dirty = names.filter((n) => detect(join(LIVE_DIR, n)).records.length > 0);
  // 제거된 키는 전부 `compile.*`이라 compile을 쓰는 프로젝트에만 나타난다(실측 3개 —
  // ai-proxy · service-engineering · novel/귀신. 나머지 6개는 recall 전용이라 애초에
  // compile 블록이 없다). H단계(로컬 마이그레이션) 후 fixture를 갱신하면 이 수가 0이
  // 될 수 있는데, 그러면 감지 테스트의 모집단이 사라진 것이므로 위 케이스를 합성
  // config로 옮겨야 한다 — 그 순간을 여기서 잡는다.
  assert.ok(dirty.length >= 3, `구형 fixture가 ${dirty.length}개뿐이다`);
});

test('정리된 config는 아무것도 보고하지 않는다', () => {
  const { home, dir } = isolatedHomeProject('dep-clean', PROJECT_NAME);
  try {
    mkdirSync(join(dir, '.auto-context'), { recursive: true });
    const settings = join(dir, '.auto-context', 'settings.json');
    writeFileSync(settings, `${JSON.stringify({
      indexing: true,
      collections: [`${PROJECT_NAME}-wiki`],
      collectionPaths: { [`${PROJECT_NAME}-wiki`]: '.auto-context/wiki' },
      compile: {
        mode: 'auto-wiki',
        budget: { verifyPerRun: 15, extractorPerRun: 10 },
        extractor: { builtins: ['claude'] },
      },
    }, null, 2)}\n`);
    const { records, notice } = detect(settings);
    assert.deepEqual(records, []);
    assert.equal(notice, '');
  } finally {
    removeTemp(home);
  }
});

// --- 3. SessionStart 알림 종점 ---------------------------------------------
// 감지가 실제 stdout 1줄로 나오는지, notice_once가 2회차를 억제하는지, 정리된
// config가 무출력인지를 update.sh 경로로 확인한다. 감지 함수 단위 테스트만으로는
// "종점이 연결되지 않았다"(update.sh에서 아예 안 부른다)를 잡지 못한다.

function runSessionStart(dir, env) {
  return execFileSync('bash', [join(CORE, 'update.sh')], {
    cwd: REPO_ROOT,
    encoding: 'utf8',
    env,
    input: JSON.stringify({ hook_event_name: 'SessionStart', cwd: dir }),
  });
}

function sessionStartProject(prefix, compileBlock) {
  const { home, dir } = isolatedHomeProject(prefix, PROJECT_NAME);
  mkdirSync(join(dir, '.auto-context'), { recursive: true });
  mkdirSync(join(dir, 'docs'), { recursive: true });
  writeFileSync(join(dir, '.auto-context', 'settings.json'), `${JSON.stringify({
    indexing: true,
    collections: [`${PROJECT_NAME}-docs`],
    collectionPaths: { [`${PROJECT_NAME}-docs`]: 'docs' },
    compile: compileBlock,
  }, null, 2)}\n`);
  // notice marker는 프로젝트 해시 단위라 케이스마다 캐시를 갈라야 서로 억제하지 않는다.
  const env = { ...childEnv(home), QMD_CACHE_DIR: tempCacheDir(prefix) };
  return { home, dir, env };
}

test('SessionStart가 구형 config에 알림 1줄을 내고 2회차는 억제한다', () => {
  const { home, dir, env } = sessionStartProject('dep-notice', {
    enabled: true,
    mode: 'auto-wiki',
    verify: { maxPerRun: 15 },
    candidatePath: '.auto-context/compile/candidates.jsonl',
  });
  try {
    const first = runSessionStart(dir, env);
    const lines = first.split('\n').filter((l) => l.includes('더 이상 읽지 않는 설정 키'));
    assert.equal(lines.length, 1, `알림이 1줄이 아니다:\n${first}`);
    assert.match(lines[0], /^\[qmd\] /);
    assert.match(lines[0], /compile\.verify\.maxPerRun → compile\.budget\.verifyPerRun/);
    assert.match(lines[0], /compile\.enabled/);
    assert.match(lines[0], /compile\.candidatePath/);
    // 2회차: marker TTL(4h) 안이므로 침묵.
    const second = runSessionStart(dir, env);
    assert.equal(second.includes('더 이상 읽지 않는 설정 키'), false, `2회차가 억제되지 않았다:\n${second}`);
  } finally {
    removeTemp(home);
  }
});

test('SessionStart가 정리된 config에는 아무 알림도 내지 않는다', () => {
  const { home, dir, env } = sessionStartProject('dep-notice-clean', {
    mode: 'auto-wiki',
    budget: { verifyPerRun: 15 },
  });
  try {
    const out = runSessionStart(dir, env);
    assert.equal(out.includes('더 이상 읽지 않는 설정 키'), false, out);
  } finally {
    removeTemp(home);
  }
});

// --- 4. 죽은 설정 키 3개의 처리 방식 (계획 2026-08-19 §3.4) -----------------
// 세 키가 서로 **다른** 처리를 받으므로 그 차이를 한 자리에서 못박는다. 하나라도
// 뒤집히면(예: sourceScan을 다시 removed 목록에 넣거나 sourceMissingPath를 살리면)
// 여기서 잡힌다.

function detectRaw(raw) {
  const path = join(CACHE, `raw-${Math.random().toString(36).slice(2)}.json`);
  mkdirSync(CACHE, { recursive: true });
  writeFileSync(path, JSON.stringify(raw));
  return detect(path);
}

test('§3.4 sourceScan은 살아난 키라 알림 대상이 아니다', () => {
  // 값이 실제로 읽히므로(config.compile_config) 제거·이전 목록 어디에도 없다.
  const { records, notice } = detectRaw({
    compile: { mode: 'auto-wiki', sourceScan: { enabled: false, maxCardsPerScan: 50 } },
  });
  assert.deepEqual(records, []);
  assert.equal(notice, '');
});

test('§3.4 sourceMissingPath는 알림만 나간다 (경로는 compile_paths 상수)', () => {
  // 이 경로 키를 읽는 소비자가 없으므로 살릴 대상이 아니라 알릴 대상이다.
  const { records, notice } = detectRaw({
    compile: { mode: 'auto-wiki', sourceMissingPath: '.auto-context/compile/mine.jsonl' },
  });
  assert.deepEqual(records, [
    { key: 'compile.sourceMissingPath', kind: 'removed', replacement: null },
  ]);
  assert.match(notice, /제거됨\(대체 없음, 지우세요\): compile\.sourceMissingPath/);

  // 키를 적지 않은 프로젝트에는 아무 알림도 나가지 않는다(present일 때만).
  const clean = detectRaw({ compile: { mode: 'auto-wiki' } });
  assert.deepEqual(clean.records, []);
  assert.equal(clean.notice, '');
});

test('§3.4 maxCardsPerSource는 값이 이전되어도 알림은 유지된다', () => {
  // 이전은 **잠정 반영**이고 스키마가 아니다 — 사용자는 새 키로 옮겨 적어야 하므로
  // 행선지 알림이 계속 나가야 한다(docs/settings.md가 이 예외를 함께 적는다).
  const { records, notice } = detectRaw({ compile: { mode: 'auto-wiki', maxCardsPerSource: 4 } });
  assert.deepEqual(records, [{
    key: 'compile.maxCardsPerSource',
    kind: 'relocated',
    replacement: 'compile.budget.cardsPerSource',
  }]);
  assert.match(notice, /compile\.maxCardsPerSource → compile\.budget\.cardsPerSource/);
});

// 값을 못 읽어 기본값으로 폴백한 사실의 **종점**. 판정 함수 단위 테스트만으로는
// "update.sh에서 아무도 안 부른다"를 잡지 못한다(위 deprecated 알림과 같은 이유).
test('SessionStart가 읽을 수 없는 compile 스위치 값을 알리고 고치면 재무장한다', () => {
  const { home, dir, env } = sessionStartProject('dep-flag', {
    mode: 'auto-wiki',
    sourceScan: { enabled: 'false' },
  });
  const settings = join(dir, '.auto-context', 'settings.json');
  const write = (sourceScan) => writeFileSync(settings, `${JSON.stringify({
    indexing: true,
    collections: [`${PROJECT_NAME}-docs`],
    collectionPaths: { [`${PROJECT_NAME}-docs`]: 'docs' },
    compile: { mode: 'auto-wiki', sourceScan },
  }, null, 2)}\n`);
  try {
    const first = runSessionStart(dir, env);
    const lines = first.split('\n').filter((l) => l.includes('compile 설정 값을 읽을 수 없어'));
    assert.equal(lines.length, 1, `알림이 1줄이 아니다:\n${first}`);
    assert.match(lines[0], /compile\.sourceScan\.enabled/);
    // fail-open 방향(스캔 유지)이 안내에 있어야 한다 — 무엇이 지금 일어나는지 모르면
    // 사용자는 자기 opt-out이 무력화된 것을 알 수 없다.
    assert.match(lines[0], /스캔이 계속 돕니다/);

    // TTL 억제.
    assert.doesNotMatch(runSessionStart(dir, env), /compile 설정 값을 읽을 수 없어/);
    // 값을 고치면 조용해지고(notice_clear) 다시 틀리면 또 알린다.
    write({ enabled: false });
    assert.doesNotMatch(runSessionStart(dir, env), /compile 설정 값을 읽을 수 없어/);
    write({ enabled: 'nope' });
    assert.match(runSessionStart(dir, env), /compile 설정 값을 읽을 수 없어/);
  } finally {
    removeTemp(home);
  }
});

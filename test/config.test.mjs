import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { existsSync, mkdtempSync, mkdirSync, readFileSync, writeFileSync, realpathSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname } from 'node:path';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { removeTemp } from './helpers/temp.mjs';

// compile.semanticDedup.judge — LLM dedup judge defaults (core/config.py DEFAULTS)
const JUDGE_DEFAULTS = {
  enabled: true,
  // 판정 엔진을 신규 후보를 만든 엔진과 분리한다(verify.crossEngine과 같은 값 집합).
  crossEngine: 'prefer',
  timeout: 120,
  cooldownSeconds: 600,
  maxCharsPerPage: 6000,
};
// 유료 호출 수 상한은 전부 compile.budget 한 블록이다(core/config.py DEFAULTS).
const BUDGET_DEFAULTS = {
  extractorPerRun: 10,
  cardsPerSource: 10,
  verifyPerRun: 3,
  dedupPairsPerScan: 8,
  dedupPairsPerCompile: 1,
};

function loadConfig(json, cwd = '/tmp/x') {
  const out = execFileSync('python3', ['core/config.py', '--cwd', cwd], { input: json });
  return JSON.parse(out);
}

function findProjectConfig(cwd, env = {}) {
  const code = `
import json
import config
result = config.find_project_config(${JSON.stringify(cwd)})
print(json.dumps(result, ensure_ascii=False))
`;
  const out = execFileSync('python3', ['-c', code], {
    encoding: 'utf8',
    env: { ...process.env, PYTHONPATH: 'core', ...env },
  });
  return JSON.parse(out);
}

function loadProjectConfig(cwd) {
  const code = `
import json
import config
print(json.dumps(config.load_project_config(${JSON.stringify(cwd)}), ensure_ascii=False))
`;
  const out = execFileSync('python3', ['-c', code], {
    encoding: 'utf8',
    env: { ...process.env, PYTHONPATH: join(cwd, 'core') },
  });
  return JSON.parse(out);
}

test('dogfood project pins the wiki extractor reasoning-effort policy', () => {
  const projectRoot = realpathSync(join(dirname(fileURLToPath(import.meta.url)), '..'));
  const cfg = loadProjectConfig(projectRoot);
  const policy = {
    generation: 'low',
    verify: 'medium',
    semanticDedup: 'medium',
    engines: {},
  };
  const settings = JSON.parse(readFileSync(join(projectRoot, '.auto-context', 'settings.json'), 'utf8'));
  // The dogfood settings are minimal; the policy is the derived default.
  assert.deepEqual(Object.keys(settings).sort(), ['name', 'skipPaths']);
  assert.equal(cfg.settingsModel, 'minimal');
  assert.deepEqual(cfg.compile.reasoningEffort, policy);
});

function migrateLegacyConfig(cwd, env = {}) {
  const code = `
import json
import config
result = config.migrate_legacy_config(${JSON.stringify(cwd)})
print(json.dumps(result, ensure_ascii=False))
`;
  const out = execFileSync('python3', ['-c', code], {
    encoding: 'utf8',
    env: { ...process.env, PYTHONPATH: 'core', ...env },
  });
  return JSON.parse(out);
}

test('기존 novel 스키마 무수정 동작 (신규 필드 부재 → 기본값)', () => {
  const cfg = loadConfig(JSON.stringify({
    name: 'My Story', collections: ['story-manuscript'], minScore: 0.8,
  }));
  assert.equal(cfg.name, 'My Story');
  assert.deepEqual(cfg.collections, ['story-manuscript']);
  assert.equal(cfg.minScore, 0.8);
  assert.equal(cfg.topN, 3);                       // 기본값
  assert.deepEqual(cfg.lexicalPatterns, ['ep']);   // legacy novel collection names auto-enable EP exact search
  assert.deepEqual(cfg.events, ['sessionStart', 'userPromptSubmit', 'postToolUse']);
  assert.equal(cfg.sessionStartSync, true);
});

test('SessionStart file sync defaults on without changing explicit off or compile consent', () => {
  const enabled = loadConfig(JSON.stringify({compile: {mode: 'off'}}));
  const disabled = loadConfig(JSON.stringify({sessionStartSync: false, compile: {mode: 'off'}}));
  assert.equal(enabled.sessionStartSync, true);
  assert.equal(disabled.sessionStartSync, false);
  assert.equal(enabled.compile.mode, 'off');
  assert.deepEqual(enabled.compile.triggers, []);
  for (const value of ['false', 'true', 0, 1, null, [], {}]) {
    assert.equal(loadConfig(JSON.stringify({sessionStartSync: value})).sessionStartSync, false,
      JSON.stringify(value));
  }
});

test('신규 필드 파싱', () => {
  const cfg = loadConfig(JSON.stringify({
    name: 'x', collections: ['c'], minScore: 0.5,
    lexicalPatterns: ['ep'], skipPaths: ['.auto-context-ignore'], topN: 5, queryTimeout: 8,
  }));
  assert.deepEqual(cfg.lexicalPatterns, ['ep']);
  assert.deepEqual(cfg.skipPaths, ['.auto-context-ignore']);
  assert.equal(cfg.topN, 5);
  assert.equal(cfg.queryTimeout, 8);
});

test('recallStrategy 유효값은 통과하고 잘못된/미지정 값은 기본(hierarchical)으로 coerce된다', () => {
  assert.equal(loadConfig(JSON.stringify({ recallStrategy: 'wikiOnly' })).recallStrategy, 'wikiOnly');
  assert.equal(loadConfig(JSON.stringify({ recallStrategy: 'hierarchical' })).recallStrategy, 'hierarchical');
  assert.equal(loadConfig(JSON.stringify({ recallStrategy: 'flat' })).recallStrategy, 'flat');
  // 알 수 없는 값 → DEFAULT_CONFIG(hierarchical). wiki role이 없으면 hierarchical은 flat과 동일 동작.
  assert.equal(loadConfig(JSON.stringify({ recallStrategy: 'bogus' })).recallStrategy, 'hierarchical');
  // 필드 자체 미지정도 동일하게 기본값.
  assert.equal(loadConfig(JSON.stringify({ collections: ['x'] })).recallStrategy, 'hierarchical');
});

test('wiki recall 신규 필드는 additive로 normalize 된다', () => {
  const cfg = loadConfig(JSON.stringify({
    collections: ['proj-docs', 'proj-wiki'],
    collectionRoles: { 'proj-docs': 'raw', 'proj-wiki': 'wiki', 'proj-bad': 'unknown' },
    recallStrategy: 'hierarchical',
    wikiPath: '.auto-context/wiki',
    compile: {
      mode: 'auto-wiki',
      defaultStatus: 'generated',
      // 제거된 키를 그대로 남겨 둔다 — 정규화가 **무시**하는지(effective에 안 나타나는지)를
      // 같은 자리에서 단정하기 위해서다. 예전에는 이 값들이 effective에 실렸다.
      enabled: true,
      autoWrite: true,
      excludeStatusesFromRecall: ['discarded', 'contested', 'bogus'],
      lowPriorityStatuses: ['generated', 'tentative', 'canon'],
      triggers: ['manual', 'post_session_summary', 'post_tool_source', 'bad'],
      maxAutoPageLines: '80',
      maxSourceChars: '12000',
      extractor: { backends: { claude: ['python3', 'scripts/extract.py'] }, timeout: '30' },
    },
  }));
  assert.deepEqual(cfg.collectionRoles, { 'proj-docs': 'raw', 'proj-wiki': 'wiki' });
  assert.equal(cfg.recallStrategy, 'hierarchical');
  assert.equal(cfg.wikiPath, '.auto-context/wiki');
  // deepEqual이라 이 표가 곧 "스키마 전수"다 — 제거된 9개 경로 키와
  // enabled/autoWrite/requireReviewForCanon/canonSignals/maxCardsPerSource가
  // effective에 **없다**는 것을 여기서 단정한다(입력에는 그대로 들어 있었다).
  assert.deepEqual(cfg.compile, {
    mode: 'auto-wiki',
    defaultStatus: 'generated',
    excludeStatusesFromRecall: ['discarded', 'contested'],
    lowPriorityStatuses: ['generated', 'tentative'],
    recallVerifiedOnly: true,
    triggers: ['manual', 'post_session_summary', 'post_tool_source'],
    maxAutoPageLines: 80,
    maxSourceChars: 12000,
    // 소스 소실 스캐너 설정. 화이트리스트 밖이던 동안 `enabled:false`를 적어도 스캔이
    // 돌았다(값이 정규화 출력에 없으면 소비자가 기본값을 본다).
    sourceScan: { enabled: true, maxCardsPerScan: 300 },
    reasoningEffort: {
      generation: 'low',
      verify: 'medium',
      semanticDedup: 'medium',
      engines: {},
    },
    extractor: {
      backends: { claude: ['python3', 'scripts/extract.py'] },
      builtins: [],
      timeout: 30,
      cooldownSeconds: 600,
    },
    budget: BUDGET_DEFAULTS,
    batch: { idleSeconds: 90, maxItems: 5 },
    semanticDedup: { enabled: true, maxPairsPerScan: 10, candidateMinScore: 0.3, judge: JUDGE_DEFAULTS },
    verify: {
      enabled: true,
      timeout: 120,
      onFail: 'delete',
      onInconclusive: 'delete',
      crossEngine: 'prefer',
      builtins: [],
      cooldownSeconds: 600,
    },
  });
});

test('compile.reasoningEffort resolves phase defaults and symbolic engine overrides, invalid values fall back safely', () => {
  const cfg = loadConfig(JSON.stringify({
    compile: {
      reasoningEffort: {
        generation: 'bogus',
        verify: 'high',
        semanticDedup: 'xhigh',
        engines: {
          claude: { verify: 'low', generation: 'invalid' },
          codex: { generation: 'high' },
          futureEngine: { semanticDedup: 'medium' },
          ignored: 'not-an-object',
        },
        model: { name: 'must-not-be-supported' },
      },
    },
  }));
  assert.deepEqual(cfg.compile.reasoningEffort, {
    generation: 'low',
    verify: 'high',
    semanticDedup: 'xhigh',
    engines: {
      claude: { verify: 'low' },
      codex: { generation: 'high' },
      futureEngine: { semanticDedup: 'medium' },
    },
  });
  const py = `import json,sys; sys.path.insert(0,'core'); import config
cfg = config.normalize_config(json.loads(${JSON.stringify(JSON.stringify({ compile: { reasoningEffort: { generation: 'low', verify: 'medium', semanticDedup: 'medium', engines: { claude: { verify: 'high' }, codex: { generation: 'high' } } } } }))}))
print(json.dumps({k: config.resolve_reasoning_effort(cfg['compile'], *pair) for k, pair in {'claudeVerify': ('claude','verify'), 'claudeGeneration': ('claude','generation'), 'codexGeneration': ('codex','generation'), 'hermesDedup': ('hermes','semanticDedup'), 'unknownPhase': ('claude','unknown')}.items()}))`;
  const resolved = JSON.parse(execFileSync('python3', ['-c', py], { encoding: 'utf8' }));
  assert.deepEqual(resolved, {
    claudeVerify: 'high',
    claudeGeneration: 'low',
    codexGeneration: 'high',
    hermesDedup: 'medium',
    unknownPhase: 'high',
  });
});

test('compile recallVerifiedOnly: 기본 true, bool 커스텀 존중, 불량 값은 기본값 폴백', () => {
  // 미설정 → 기본 true
  assert.equal(loadConfig(JSON.stringify({ compile: { enabled: true } })).compile.recallVerifiedOnly, true);
  // 명시 false 존중
  assert.equal(loadConfig(JSON.stringify({ compile: { recallVerifiedOnly: false } })).compile.recallVerifiedOnly, false);
  // 불량 값(문자열)은 기본값 true로 폴백
  assert.equal(loadConfig(JSON.stringify({ compile: { recallVerifiedOnly: 'yes' } })).compile.recallVerifiedOnly, true);
});

test('compile verify config: 커스텀 값 정규화 + 불량 값은 기본값 폴백', () => {
  const cfg = loadConfig(JSON.stringify({
    compile: {
      enabled: true,
      mode: 'auto-wiki',
      verify: { enabled: false, timeout: 60, onFail: 'contested', onInconclusive: 'none' },
      budget: { verifyPerRun: 5 },
    },
  }));
  assert.equal(cfg.compile.verify.enabled, false);
  assert.equal(cfg.compile.verify.timeout, 60);
  assert.equal(cfg.compile.verify.onFail, 'contested');
  // 'none' = 0.x 하위호환("generated 유지") — 파괴적 기본값을 끄는 유일한 경로
  assert.equal(cfg.compile.verify.onInconclusive, 'none');
  assert.equal(cfg.compile.budget.verifyPerRun, 5, 'verify 예산은 compile.budget로 이관됐다');

  const bad = loadConfig(JSON.stringify({
    compile: {
      enabled: true,
      verify: { enabled: 'yes', timeout: -1, onFail: 'explode', onInconclusive: 'maybe', queuePath: 123 },
    },
  }));
  assert.equal(bad.compile.verify.enabled, true);
  assert.equal(bad.compile.verify.timeout, 120);
  assert.equal(bad.compile.verify.onFail, 'delete');
  assert.equal(bad.compile.verify.onInconclusive, 'delete', 'onFail과 같은 값 집합 — 불량 값은 기본값 폴백');
  assert.equal(bad.compile.verify.queuePath, undefined, 'verify 경로 4개는 상수화돼 스키마에 없다');
});

// `excludeStatusesFromRecall`은 여전히 WIKI_STATUSES 전체를 받는다(필터 대상이라 넓어도
// 안전하다). 반면 `defaultStatus`는 **신규 카드에 찍히는 값**이라 집합이 좁다 —
// `verified`를 허용하면 기계 검수를 거치지 않은 카드가 `recallVerifiedOnly` 기본값 아래에서
// 캐논 근거가 된다. 예전 테스트는 `verified`가 **통과하는 것**을 단정했다(구멍의 동결).
test('defaultStatus는 generated/tentative만 받는다 (verified는 검수 우회라 거부)', () => {
  const cfg = loadConfig(JSON.stringify({
    compile: {
      defaultStatus: 'verified',
      excludeStatusesFromRecall: ['discarded', 'verified'],
    },
  }));
  assert.equal(cfg.compile.defaultStatus, 'generated', 'verified는 집합 밖 → 기본값');
  assert.deepEqual(cfg.compile.excludeStatusesFromRecall, ['discarded', 'verified'],
    'excludeStatuses는 필터라 WIKI_STATUSES 전체를 그대로 받는다');
  assert.equal(loadConfig(JSON.stringify({ compile: { defaultStatus: 'tentative' } })).compile.defaultStatus, 'tentative');
});

test('compile extractor config drops shell strings and invalid timeout', () => {
  const cfg = loadConfig(JSON.stringify({
    compile: {
      mode: 'guarded',
      maxSourceChars: 'NaN',
      extractor: { command: 'python3 script.py', argv: 'python3 script.py', timeout: 'Infinity' },
    },
  }));
  assert.equal(cfg.compile.sourceQueuePath, undefined, '경로 키는 상수화돼 스키마에 없다');
  assert.equal(cfg.compile.maxSourceChars, 12000);
  // `argv`/`command`는 둘 다 스키마 밖이라 흔적이 남지 않는다(예전에는 argv가 [] 로 남았다).
  assert.deepEqual(cfg.compile.extractor, { backends: {}, builtins: [], timeout: 120, cooldownSeconds: 600 });
});

// **회귀 가드 (T5)**: `dispatch` 키가 **없어도** backends/builtins가 해석된다.
// 게이트가 있던 동안 이 config는 엔진을 하나도 해석하지 못했고, 그 실패는 큐 잡을
// requeue가 아니라 **폐기**했다(wiki_compile_worker.process_job → missing_extractor).
test('compile extractor config preserves valid built-ins and drops invalid values (dispatch 키 없이)', () => {
  const cfg = loadConfig(JSON.stringify({
    compile: {
      mode: 'auto-wiki',
      extractor: {
        backends: { codex: ['python3', 'custom.py'], bogus: 'python3 bad.py' },
        builtins: ['codex', 'bogus', 42, 'hermes'],
        timeout: 120,
      },
    },
  }));
  assert.deepEqual(cfg.compile.extractor, {
    timeout: 120,
    cooldownSeconds: 600,
    backends: { codex: ['python3', 'custom.py'] },
    builtins: ['codex', 'hermes'],
  });
  // 예전 스키마의 dispatch/default/argv를 그대로 적어도 무시된다(흡수되거나 남지 않는다).
  const legacy = loadConfig(JSON.stringify({
    compile: { extractor: { dispatch: 'by-engine', default: ['python3', 'f.py'], argv: ['python3', 'a.py'], builtins: ['claude'] } },
  }));
  assert.deepEqual(legacy.compile.extractor, {
    timeout: 120, cooldownSeconds: 600, backends: {}, builtins: ['claude'],
  });
});

test('빈/깨진 JSON → 전부 기본값', () => {
  const cfg = loadConfig('not json at all');
  assert.deepEqual(cfg.collections, []);
  assert.equal(cfg.topN, 3);
  assert.equal(cfg.queryTimeout, 5);
  assert.equal(cfg.minScore, 0.0);
  assert.deepEqual(cfg.collectionPaths, {});
});

test('indexing 필드 passthrough (true/false/없음)', () => {
  const norm = (input) => JSON.parse(execFileSync('python3', ['core/config.py', '--cwd', '/tmp'], { input: JSON.stringify(input) }).toString());
  assert.equal(norm({ indexing: true }).indexing, true);
  assert.equal(norm({ indexing: false }).indexing, false);
  assert.equal(norm({}).indexing, null);
  assert.equal(norm({ indexing: 'yes' }).indexing, null);
});

test('indexing 문자열 "true"/"false" 강제 (그 외는 null)', () => {
  const norm = (input) => JSON.parse(execFileSync('python3', ['core/config.py', '--cwd', '/tmp'], { input: JSON.stringify(input) }).toString());
  assert.equal(norm({ indexing: 'false' }).indexing, false);   // opt-out 의도 보존
  assert.equal(norm({ indexing: 'TRUE' }).indexing, true);
  assert.equal(norm({ indexing: 'garbage' }).indexing, null);
});

test('config 숫자 타입은 보수적으로 coercion 하고 실패 시 기본값', () => {
  const cfg = loadConfig(JSON.stringify({
    minScore: '0.75',
    rawFallbackMinScore: '0.7',
    topN: '2',
    queryTimeout: '4.5',
  }));
  assert.equal(cfg.minScore, 0.75);
  assert.equal(cfg.rawFallbackMinScore, 0.7);
  assert.equal(cfg.topN, 2);
  assert.equal(cfg.queryTimeout, 4.5);

  const fallback = loadConfig(JSON.stringify({
    minScore: 'NaN',
    rawFallbackMinScore: 'NaN',
    topN: 'NaN',
    queryTimeout: 'Infinity',
  }));
  assert.equal(fallback.minScore, 0.0);
  assert.equal(fallback.rawFallbackMinScore, 0.0);
  assert.equal(fallback.topN, 3);
  assert.equal(fallback.queryTimeout, 5);
});

test('rawFallbackMinScore 누락 시 정규화된 minScore를 따른다', () => {
  const cfg = loadConfig(JSON.stringify({
    minScore: '0.7',
  }));
  assert.equal(cfg.minScore, 0.7);
  assert.equal(cfg.rawFallbackMinScore, 0.7);
});

test('find_project_config: cwd .auto-context.json root/path 반환', () => {
  const home = mkdtempSync(join(tmpdir(), 'qmd-cfg-home-'));
  const dir = join(home, 'proj');
  mkdirSync(dir, { recursive: true });
  writeFileSync(join(dir, '.auto-context.json'), JSON.stringify({
    indexing: true,
    collections: ['story'],
  }));
  try {
    const result = findProjectConfig(dir, { HOME: home });
    assert.equal(result.projectRoot, realpathSync(dir));
    assert.equal(result.configPath, join(realpathSync(dir), '.auto-context.json'));
    assert.equal(result.configFormat, 'auto-context-json');
    assert.deepEqual(result.config.collections, ['story']);
  } finally {
    removeTemp(home);
  }
});

test('find_project_config: .auto-context/settings.json preferred', () => {
  const home = mkdtempSync(join(tmpdir(), 'qmd-cfg-home-'));
  const dir = join(home, 'proj');
  mkdirSync(join(dir, '.auto-context'), { recursive: true });
  writeFileSync(join(dir, '.auto-context', 'settings.json'), JSON.stringify({
    indexing: true,
    collections: ['settings'],
  }));
  try {
    const result = findProjectConfig(dir, { HOME: home });
    assert.equal(result.projectRoot, realpathSync(dir));
    assert.equal(result.configPath, join(realpathSync(dir), '.auto-context', 'settings.json'));
    assert.equal(result.configFormat, 'auto-context-dir');
    assert.deepEqual(result.config.collections, ['settings']);
  } finally {
    removeTemp(home);
  }
});

test('find_project_config: settings.json beats legacy .auto-context.json when both exist', () => {
  const home = mkdtempSync(join(tmpdir(), 'qmd-cfg-home-'));
  const dir = join(home, 'proj');
  mkdirSync(join(dir, '.auto-context'), { recursive: true });
  writeFileSync(join(dir, '.auto-context.json'), JSON.stringify({
    indexing: true,
    collections: ['legacy-root'],
  }));
  writeFileSync(join(dir, '.auto-context', 'settings.json'), JSON.stringify({
    indexing: true,
    collections: ['settings'],
  }));
  try {
    const result = findProjectConfig(dir, { HOME: home });
    assert.equal(result.configPath, join(realpathSync(dir), '.auto-context', 'settings.json'));
    assert.equal(result.configFormat, 'auto-context-dir');
    assert.deepEqual(result.config.collections, ['settings']);
  } finally {
    removeTemp(home);
  }
});

test('find_project_config: parent .auto-context.json found from child cwd', () => {
  const home = mkdtempSync(join(tmpdir(), 'qmd-cfg-home-'));
  const dir = join(home, 'proj');
  const child = join(dir, 'docs', 'nested');
  mkdirSync(child, { recursive: true });
  writeFileSync(join(dir, '.auto-context.json'), JSON.stringify({
    indexing: true,
    collections: ['parent'],
  }));
  try {
    const result = findProjectConfig(child, { HOME: home });
    assert.equal(result.projectRoot, realpathSync(dir));
    assert.equal(result.configPath, join(realpathSync(dir), '.auto-context.json'));
    assert.equal(result.configFormat, 'auto-context-json');
    assert.deepEqual(result.config.collections, ['parent']);
  } finally {
    removeTemp(home);
  }
});

test('find_project_config: legacy .agents/qmd-recall.json still works', () => {
  const home = mkdtempSync(join(tmpdir(), 'qmd-cfg-home-'));
  const dir = join(home, 'proj');
  mkdirSync(join(dir, '.agents'), { recursive: true });
  writeFileSync(join(dir, '.agents', 'qmd-recall.json'), JSON.stringify({
    collections: ['legacy'],
  }));
  try {
    const result = findProjectConfig(dir, { HOME: home });
    assert.equal(result.projectRoot, realpathSync(dir));
    assert.equal(result.configPath, join(realpathSync(dir), '.agents', 'qmd-recall.json'));
    assert.equal(result.configFormat, 'agents-legacy');
    assert.deepEqual(result.config.collections, ['legacy']);
  } finally {
    removeTemp(home);
  }
});

test('find_project_config: no config returns null path and cwd root', () => {
  const home = mkdtempSync(join(tmpdir(), 'qmd-cfg-home-'));
  const dir = join(home, 'proj');
  mkdirSync(dir, { recursive: true });
  try {
    const result = findProjectConfig(dir, { HOME: home });
    assert.equal(result.projectRoot, realpathSync(dir));
    assert.equal(result.configPath, null);
    assert.equal(result.configFormat, 'none');
    assert.deepEqual(result.config.collections, []);
  } finally {
    removeTemp(home);
  }
});

test('find_project_config: 내부 경로 탐색에서 예상 못한 예외가 나도 fail-open(빈 config)한다', () => {
  // recall/posttool/index_enqueue/wiki_compile_enqueue/preflight_gate 등 여러 hook
  // entrypoint가 find_project_config를 개별 try/except 없이 직접 호출한다. 샌드박스/
  // 권한 등 환경 차이로 내부 탐색(_find_project_config_unsafe)이 죽어도, 공개 함수는
  // 절대 raise하지 않고 "설정 없음"으로 fail-open해야 호출자가 non-zero exit로 죽지
  // 않는다.
  const home = mkdtempSync(join(tmpdir(), 'qmd-cfg-home-'));
  const dir = join(home, 'proj');
  mkdirSync(dir, { recursive: true });
  try {
    const code = `
import json
import config

def boom(cwd):
    raise PermissionError("simulated sandboxed fs error")
config._find_project_config_unsafe = boom

result = config.find_project_config(${JSON.stringify(dir)})
print(json.dumps(result, ensure_ascii=False))
`;
    const out = execFileSync('python3', ['-c', code], {
      encoding: 'utf8',
      env: { ...process.env, PYTHONPATH: 'core', HOME: home },
    });
    const result = JSON.parse(out);
    assert.equal(result.configPath, null);
    assert.equal(result.configFormat, 'none');
    assert.deepEqual(result.config.collections, []);
  } finally {
    removeTemp(home);
  }
});

test('migrate_legacy_config moves .auto-context.json to .auto-context/settings.json and deletes old file', () => {
  const home = mkdtempSync(join(tmpdir(), 'qmd-cfg-home-'));
  const dir = join(home, 'proj');
  mkdirSync(dir, { recursive: true });
  writeFileSync(join(dir, '.auto-context.json'), JSON.stringify({
    indexing: true,
    collections: ['legacy'],
    minScore: 0.7,
  }));
  try {
    const result = migrateLegacyConfig(dir, { HOME: home });
    assert.equal(result.migrated, true);
    assert.equal(result.from, join(realpathSync(dir), '.auto-context.json'));
    assert.equal(result.to, join(realpathSync(dir), '.auto-context', 'settings.json'));
    assert.equal(existsSync(join(dir, '.auto-context.json')), false);
    const cfg = JSON.parse(readFileSync(join(dir, '.auto-context', 'settings.json'), 'utf8'));
    assert.deepEqual(cfg.collections, ['legacy']);
    assert.equal(cfg.minScore, 0.7);
  } finally {
    removeTemp(home);
  }
});

test('migrate_legacy_config is no-op when settings.json already exists', () => {
  const home = mkdtempSync(join(tmpdir(), 'qmd-cfg-home-'));
  const dir = join(home, 'proj');
  mkdirSync(join(dir, '.auto-context'), { recursive: true });
  writeFileSync(join(dir, '.auto-context.json'), JSON.stringify({ collections: ['legacy-root'] }));
  writeFileSync(join(dir, '.auto-context', 'settings.json'), JSON.stringify({ collections: ['settings'] }));
  try {
    const result = migrateLegacyConfig(dir, { HOME: home });
    assert.equal(result.migrated, false);
    assert.equal(result.reason, 'settings_exists');
    assert.equal(existsSync(join(dir, '.auto-context.json')), true);
    assert.deepEqual(JSON.parse(readFileSync(join(dir, '.auto-context', 'settings.json'), 'utf8')).collections, ['settings']);
  } finally {
    removeTemp(home);
  }
});

test('migrate_legacy_config leaves legacy file on invalid JSON', () => {
  const home = mkdtempSync(join(tmpdir(), 'qmd-cfg-home-'));
  const dir = join(home, 'proj');
  mkdirSync(dir, { recursive: true });
  writeFileSync(join(dir, '.auto-context.json'), '{not json');
  try {
    const result = migrateLegacyConfig(dir, { HOME: home });
    assert.equal(result.migrated, false);
    assert.equal(result.reason, 'invalid_json');
    assert.equal(existsSync(join(dir, '.auto-context.json')), true);
    assert.equal(existsSync(join(dir, '.auto-context', 'settings.json')), false);
  } finally {
    removeTemp(home);
  }
});

test('migrate_legacy_config does not migrate .agents/qmd-recall.json', () => {
  const home = mkdtempSync(join(tmpdir(), 'qmd-cfg-home-'));
  const dir = join(home, 'proj');
  mkdirSync(join(dir, '.agents'), { recursive: true });
  writeFileSync(join(dir, '.agents', 'qmd-recall.json'), JSON.stringify({ collections: ['agents'] }));
  try {
    const result = migrateLegacyConfig(dir, { HOME: home });
    assert.equal(result.migrated, false);
    assert.equal(result.reason, 'agents_legacy_not_migrated');
    assert.equal(existsSync(join(dir, '.auto-context', 'settings.json')), false);
    assert.equal(existsSync(join(dir, '.agents', 'qmd-recall.json')), true);
  } finally {
    removeTemp(home);
  }
});

test('compile extractor cooldownSeconds is preserved and defaults to 600', () => {
  const withCooldown = loadConfig(JSON.stringify({
    compile: {
      enabled: true,
      mode: 'guarded',
      extractor: { backends: { claude: ['python3', 'extract.py'] }, timeout: 30, cooldownSeconds: 300 },
    },
  }));
  assert.equal(withCooldown.compile.extractor.cooldownSeconds, 300);

  const withDefault = loadConfig(JSON.stringify({
    compile: {
      enabled: true,
      mode: 'guarded',
      extractor: { backends: { claude: ['python3', 'extract.py'] }, timeout: 30 },
    },
  }));
  assert.equal(withDefault.compile.extractor.cooldownSeconds, 600);
});

// `batch`에는 **시작 조건만** 남는다. `maxPerRun`(run당 extractor 상한)은 compile.budget로
// 옮겨졌다 — 같은 서브트리에 "이만큼 모이면 시작"과 "이보다 많이 돌리지 마라"가 동거하던
// 것이 혼동원이었다. 클램프(MAX_COMPILE_PER_RUN)는 함께 이동했고 아래에서 단정한다.
test('compile.batch는 시작 조건만 정규화한다 (maxPerRun은 budget으로 이동)', () => {
  const withBatch = loadConfig(JSON.stringify({
    compile: {
      mode: 'guarded',
      batch: { idleSeconds: 10, maxItems: 2, maxPerRun: '4' },
    },
  }));
  assert.deepEqual(withBatch.compile.batch, { idleSeconds: 10, maxItems: 2 });
  assert.equal(withBatch.compile.budget.extractorPerRun, 10, 'batch.maxPerRun은 더 이상 읽히지 않는다');

  const withDefaults = loadConfig(JSON.stringify({ compile: { mode: 'guarded' } }));
  assert.deepEqual(withDefaults.compile.batch, { idleSeconds: 90, maxItems: 5 });
});

test('compile.budget: 값 정규화 + 유료 호출 클램프가 함께 이동했다', () => {
  const custom = loadConfig(JSON.stringify({
    compile: { budget: { extractorPerRun: '4', cardsPerSource: 7, verifyPerRun: 15, dedupPairsPerScan: 3, dedupPairsPerCompile: 2 } },
  }));
  assert.deepEqual(custom.compile.budget, {
    extractorPerRun: 4, cardsPerSource: 7, verifyPerRun: 15, dedupPairsPerScan: 3, dedupPairsPerCompile: 2,
  });

  // MAX_COMPILE_PER_RUN / MAX_CARDS_PER_SOURCE / MAX_VERIFY_PER_RUN = 50 (core/config.py)
  const clamped = loadConfig(JSON.stringify({
    compile: { budget: { extractorPerRun: 99999999, cardsPerSource: '100', verifyPerRun: 60 } },
  }));
  assert.equal(clamped.compile.budget.extractorPerRun, 50);
  assert.equal(clamped.compile.budget.cardsPerSource, 50);
  assert.equal(clamped.compile.budget.verifyPerRun, 50);

  const bad = loadConfig(JSON.stringify({ compile: { budget: { extractorPerRun: 'nope', verifyPerRun: -1 } } }));
  assert.deepEqual(bad.compile.budget, BUDGET_DEFAULTS);
  assert.deepEqual(loadConfig(JSON.stringify({ compile: {} })).compile.budget, BUDGET_DEFAULTS);
});

// --- 죽은 설정 키 3개 (계획 2026-08-19 §3.4) --------------------------------
// `sourceScan`은 `wiki_lifecycle.run`이 `enabled`·`maxCardsPerScan`을 읽는다. 값 규칙은
// 여기가 SSOT다.
test('compile.sourceScan: enabled는 bool만, maxCardsPerScan은 클램프된다', () => {
  const SCAN_DEFAULTS = { enabled: true, maxCardsPerScan: 300 };
  const scanOf = (v) => loadConfig(JSON.stringify({ compile: { sourceScan: v } })).compile.sourceScan;
  const maxScan = JSON.parse(execFileSync('python3', ['-c', [
    'import json, sys',
    "sys.path.insert(0, 'core')",
    'import config as c',
    'print(json.dumps(c.MAX_SOURCE_SCAN_CARDS))',
  ].join('\n')], { encoding: 'utf8' }).trim());

  // 사용자가 기록해 둔 opt-out이 실제로 반영된다 — 이 한 줄이 §3.4의 본체다.
  assert.deepEqual(scanOf({ enabled: false }), { enabled: false, maxCardsPerScan: 300 });
  assert.deepEqual(scanOf({ maxCardsPerScan: '50' }), { enabled: true, maxCardsPerScan: 50 });
  assert.deepEqual(loadConfig(JSON.stringify({ compile: {} })).compile.sourceScan, SCAN_DEFAULTS);

  // bool 강제: `"false"`가 truthy로 읽혀 "껐는데 돈다"가 재발하지 않게 한다.
  assert.deepEqual(scanOf({ enabled: 'false' }), SCAN_DEFAULTS);
  assert.deepEqual(scanOf({ enabled: 0 }), SCAN_DEFAULTS);
  assert.deepEqual(scanOf('nope'), SCAN_DEFAULTS);

  // 클램프 경계. 상한은 리터럴이 아니라 코드 상수에서 읽는다(갈리면 이 단정이 무의미).
  assert.equal(scanOf({ maxCardsPerScan: 99999999 }).maxCardsPerScan, maxScan);
  assert.equal(scanOf({ maxCardsPerScan: maxScan }).maxCardsPerScan, maxScan);
  assert.equal(scanOf({ maxCardsPerScan: maxScan + 1 }).maxCardsPerScan, maxScan);
  // 0·음수·비수치는 기본값(0은 "끄기"가 아니다 — 끄는 것은 `enabled`다).
  for (const bad of [0, -1, 'nope', null]) {
    assert.equal(scanOf({ maxCardsPerScan: bad }).maxCardsPerScan, 300, `maxCardsPerScan: ${bad}`);
  }
});

// 공용 `coerce_int`의 계약: 양의 **정수**만 받는다. bool 과 비정수 실수는 거부한다.
// 교차 엔진 리뷰가 잡은 자리다 — 예전에는 `int(True) == 1`, `int(1.9) == 1`로 둘 다 조용히
// 통과해 `maxCardsPerScan: 1.9`를 적은 사용자가 창 **1장**을 받았다(300장을 기대한 값이
// 가장 좁은 값으로 뒤집히는 방향이라 조용한 축소다). 규칙은 `config.coerce_int` 한 곳이고
// `budget.*` 전 키가 같이 적용받는다 — 여기서 함께 단정해 그 사실을 고정한다.
test('coerce_int 계약: bool·비정수 실수는 거부하고 기본값으로 보낸다 (budget.* 공용)', () => {
  const scanOf = (v) => loadConfig(JSON.stringify({ compile: { sourceScan: v } })).compile.sourceScan;
  for (const bad of [true, false, 1.9, 2.5]) {
    assert.equal(scanOf({ maxCardsPerScan: bad }).maxCardsPerScan, 300, `maxCardsPerScan: ${bad}`);
  }
  // 정수값 실수와 숫자 문자열은 계속 받는다(JSON number 는 실수로 오고, 문자열화도 흔하다).
  assert.equal(scanOf({ maxCardsPerScan: 50.0 }).maxCardsPerScan, 50);
  assert.equal(scanOf({ maxCardsPerScan: '50' }).maxCardsPerScan, 50);
  // 같은 coercion 을 쓰는 예산 키도 같은 규칙이다(이 동작이 이 키만의 것이 아니라는 증거).
  const budgetOf = (v) => loadConfig(JSON.stringify({ compile: { budget: v } })).compile.budget;
  assert.equal(budgetOf({ extractorPerRun: true }).extractorPerRun, 10);
  assert.equal(budgetOf({ extractorPerRun: 2.5 }).extractorPerRun, 10);
  assert.equal(budgetOf({ extractorPerRun: 4 }).extractorPerRun, 4);
});
// `enabled` 비-bool은 기본값(true = 스캔 유지)으로 폴백하는데, 그것만으로는 사용자가 적어
// 둔 opt-out이 **무력화된 채 조용하다**(결과가 "껐는데 돈다"다 — 이 작업이 고치려던 실패의
// 한 단계 이동). `COLLECTION_ROLE_INVALID`와 같은 규칙으로 표면화하고, 그 판정이 여기다.
test('invalid_compile_flags: 값 미지 스위치를 표면화한다 (키 없음과 구분)', () => {
  const flagsOf = (compile) => JSON.parse(execFileSync('python3', ['-c', [
    'import json, sys',
    "sys.path.insert(0, 'core')",
    'import config as c',
    'print(json.dumps(c.invalid_compile_flags(json.loads(sys.argv[1]))))',
  ].join('\n'), JSON.stringify(compile)], { encoding: 'utf8' }).trim());

  // 값 미지 — 켜려는 의도(`1`)든 끄려는 의도(`"false"`)든 우리는 못 읽었다는 사실만 안다.
  assert.deepEqual(flagsOf({ sourceScan: { enabled: 'false' } }), ['compile.sourceScan.enabled']);
  assert.deepEqual(flagsOf({ sourceScan: { enabled: 1 } }), ['compile.sourceScan.enabled']);
  assert.deepEqual(flagsOf({ sourceScan: { enabled: null } }), ['compile.sourceScan.enabled']);
  // 블록 자체를 못 읽으면 그 안의 모든 값이 기본값이 된다.
  assert.deepEqual(flagsOf({ sourceScan: 'off' }), ['compile.sourceScan']);
  // 키 없음·정상 bool은 보고하지 않는다(의도 없음 / 의도대로 동작).
  assert.deepEqual(flagsOf({}), []);
  assert.deepEqual(flagsOf({ sourceScan: {} }), []);
  assert.deepEqual(flagsOf({ sourceScan: { enabled: false } }), []);
  assert.deepEqual(flagsOf({ sourceScan: { enabled: true } }), []);
  assert.deepEqual(flagsOf('nope'), []);
  // maxCardsPerScan은 이 알림의 대상이 아니다 — 창 폭이 기본값이 될 뿐 순환 커서가
  // 커버리지를 유지하므로 `enabled`(opt-out 무력화)와 방향이 다르다.
  assert.deepEqual(flagsOf({ sourceScan: { maxCardsPerScan: 0 } }), []);
  assert.deepEqual(flagsOf({ sourceScan: { maxCardsPerScan: 'many' } }), []);
});

// `compile.maxCardsPerSource`는 `DEPRECATED_RELOCATED_KEYS`의 이름값대로 **값이 이전**된다.
// 같은 목록의 나머지 4키는 이전하지 않는다 — 근거는 core/config.py의 주석 3항목이고
// (frozen contract / 비용 증가 방향 / 라이브 발생 0건) 여기서 그 비대칭을 못박는다.
// 이 단정이 없으면 다음 "전부 고쳐라" 스윕이 프리즈 테스트를 깨면서 되돌린다.
test('compile.maxCardsPerSource만 budget.cardsPerSource로 값이 이전된다', () => {
  const budgetOf = (compile) => loadConfig(JSON.stringify({ compile })).compile.budget;

  // 새 키가 없으면 옛 값이 반영된다(관측된 동기 사례: 4 < 기본 10 = 비용 감소 방향).
  assert.equal(budgetOf({ maxCardsPerSource: 4 }).cardsPerSource, 4);
  assert.equal(budgetOf({ maxCardsPerSource: '4' }).cardsPerSource, 4);
  // 새 키가 있으면 새 키가 이긴다(값이 옛 키보다 작아도).
  assert.equal(budgetOf({ maxCardsPerSource: 40, budget: { cardsPerSource: 7 } }).cardsPerSource, 7);
  // 이전은 위치 이동이므로 새 키에 적은 것과 같은 클램프·같은 폴백을 통과한다.
  assert.equal(budgetOf({ maxCardsPerSource: 99999999 }).cardsPerSource, 50);
  assert.equal(budgetOf({ maxCardsPerSource: 'nope' }).cardsPerSource, 10);
  assert.equal(budgetOf({ maxCardsPerSource: 0 }).cardsPerSource, 10);
  // 옛 키는 정규화 출력에 남지 않는다(남으면 소비자가 다시 읽기 시작한다).
  assert.equal('maxCardsPerSource' in loadConfig(JSON.stringify({
    compile: { maxCardsPerSource: 4 },
  })).compile, false);

  // 나머지 4개 relocated 키는 여전히 값이 유실된다(알림만) — 의도된 비대칭이다.
  const stale = loadConfig(JSON.stringify({
    compile: {
      batch: { maxPerRun: 30 },
      verify: { maxPerRun: 15 },
      semanticDedup: { judge: { maxPairsPerScan: 40, maxPairsPerCompile: 5 } },
    },
  })).compile.budget;
  assert.deepEqual(stale, BUDGET_DEFAULTS, '4키는 이전하지 않는다(비용 증가 방향 + 동결된 계약)');
});

// score 레버 4개(threshold·topK·similarPageMaxChars·autoMergeThreshold)는 상수가 됐다 —
// daemon score가 순위 기반이라 어떤 값도 "같은 사실"을 표현하지 못했고, judge-less 폴백
// 동작은 예전 기본값 그대로 동결됐다(config.DEDUP_* 상수 + judge-less 동작 테스트).
test('compile.semanticDedup는 enabled/maxPairsPerScan/candidateMinScore만 정규화한다', () => {
  const withSemantic = loadConfig(JSON.stringify({
    compile: { semanticDedup: { enabled: false, threshold: '0.5', topK: 7 } },
  }));
  assert.deepEqual(withSemantic.compile.semanticDedup, { enabled: false, maxPairsPerScan: 10, candidateMinScore: 0.3, judge: JUDGE_DEFAULTS });

  const withDefaults = loadConfig(JSON.stringify({ compile: {} }));
  assert.deepEqual(withDefaults.compile.semanticDedup, { enabled: true, maxPairsPerScan: 10, candidateMinScore: 0.3, judge: JUDGE_DEFAULTS });

  const withBadValues = loadConfig(JSON.stringify({
    compile: { semanticDedup: { enabled: 'nope', threshold: 'nan', topK: -1 } },
  }));
  assert.deepEqual(withBadValues.compile.semanticDedup, { enabled: true, maxPairsPerScan: 10, candidateMinScore: 0.3, judge: JUDGE_DEFAULTS });

  // judge.crossEngine은 verify.crossEngine과 **같은 닫힌 집합**이다(CROSS_ENGINE_MODES).
  const withCross = loadConfig(JSON.stringify({ compile: { semanticDedup: { judge: { crossEngine: 'require' } } } }));
  assert.equal(withCross.compile.semanticDedup.judge.crossEngine, 'require');
  const withBadCross = loadConfig(JSON.stringify({ compile: { semanticDedup: { judge: { crossEngine: 'sometimes' } } } }));
  assert.equal(withBadCross.compile.semanticDedup.judge.crossEngine, 'prefer', '집합 밖 값은 기본값으로');
});

// 상수화된 4개 레버의 **값 자체**는 예전 기본값과 같아야 한다 — judge 없는 머신의 레거시
// score 게이트 동작이 이 리팩터로 바뀌면 안 된다. 설정으로 덮을 수 없다는 것도 함께 본다.
test('dedup score 레버 4개는 상수이고 값은 예전 기본값과 동일하다', () => {
  const consts = JSON.parse(execFileSync('python3', ['-c', [
    'import json, sys',
    "sys.path.insert(0, 'core')",
    'import config as c',
    'print(json.dumps({',
    '  "threshold": c.DEDUP_SCORE_THRESHOLD,',
    '  "autoMerge": c.DEDUP_AUTO_MERGE_THRESHOLD,',
    '  "topK": c.DEDUP_TOP_K,',
    '  "similarPageMaxChars": c.DEDUP_SIMILAR_PAGE_MAX_CHARS,',
    '}))',
  ].join('\n')], { encoding: 'utf8' }));
  assert.deepEqual(consts, { threshold: 0.82, autoMerge: 0.9, topK: 3, similarPageMaxChars: 12000 });

  const overridden = loadConfig(JSON.stringify({
    compile: { semanticDedup: { threshold: 0.1, topK: 99, similarPageMaxChars: 1, autoMergeThreshold: 0.1 } },
  }));
  for (const key of ['threshold', 'topK', 'similarPageMaxChars', 'autoMergeThreshold']) {
    assert.equal(overridden.compile.semanticDedup[key], undefined, `${key}는 설정으로 남지 않는다`);
  }
});

test('compile.semanticDedup.maxPairsPerScan normalizes with a 10 default', () => {
  const withValue = loadConfig(JSON.stringify({
    compile: { semanticDedup: { maxPairsPerScan: 3 } },
  }));
  assert.equal(withValue.compile.semanticDedup.maxPairsPerScan, 3);

  const withDefaults = loadConfig(JSON.stringify({ compile: {} }));
  assert.equal(withDefaults.compile.semanticDedup.maxPairsPerScan, 10);

  const withBadValue = loadConfig(JSON.stringify({
    compile: { semanticDedup: { maxPairsPerScan: -1 } },
  }));
  assert.equal(withBadValue.compile.semanticDedup.maxPairsPerScan, 10);
});

test('WIKI_STATUSES / lowPriorityStatuses accept superseded', () => {
  const withSuperseded = loadConfig(JSON.stringify({
    compile: { lowPriorityStatuses: ['generated', 'tentative', 'superseded', 'bogus'] },
  }));
  assert.deepEqual(withSuperseded.compile.lowPriorityStatuses, ['generated', 'tentative', 'superseded']);

  // `superseded`는 lowPriorityStatuses에서는 유효하지만 defaultStatus 집합 밖이다
  // (신규 카드를 superseded로 만드는 것은 의미가 없고, 좁힌 집합의 부수 효과다).
  const defaultStatusRejected = loadConfig(JSON.stringify({ compile: { defaultStatus: 'superseded' } }));
  assert.equal(defaultStatusRejected.compile.defaultStatus, 'generated');
});

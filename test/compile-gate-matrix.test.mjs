// compile 활성 게이트 6곳의 동작을 입력 조합별로 못박는다.
//
// 스키마 정리 리팩터가
// `compile.enabled` + `compile.autoWrite`를 `compile.mode` 한 값으로 접었다. 리팩터 **전**
// 에는 게이트 6곳의 판정이 균일하지 않았다:
//
//   - `wiki_compile_enqueue.py` · `wiki_compile_worker.py` · `wiki_compile.py` ·
//     `wiki_verify_worker.py` → `enabled` AND `mode != "off"`
//   - `wiki_dedup_scan.py:310` · `update.sh:1121`(bash) → **`enabled`만** 보고 `mode`는 무시
//
// 그 비대칭 때문에 `enabled:true + mode:"off"`에서 뒤 둘은 **돌고** 앞 넷은 멈췄다. 이제
// 판정은 `config.compile_active`(= `mode != "off"`) 하나이므로 그 칸이 전부 skip으로 바뀐다
// — 계획 §2 ※가 명시한 **유일한** 비보존 지점이다.
//
// 이 파일의 계약: 아래 각 행은 `legacy`(리팩터 전 raw 입력)와 `compile`(§2 진리표로
// 마이그레이션한 새 입력)을 함께 들고, 기대 결과는 리팩터 전 값을 그대로 유지한다.
// **`legacyOnly` 표시가 붙은 칸 말고 다른 칸의 답이 바뀌면 회귀다.**
//
// 판정을 JS로 재구현하지 않는다. 각 게이트는 실제 모듈/스크립트를 임시 프로젝트에 대해
// 실행하고 "일이 통과했는가"를 관측 가능한 부작용으로 읽는다(큐 줄 · stdout JSON ·
// 로그의 SKIP 줄 · SessionStart notice).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { chmodSync, existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { isolatedHomeProject, removeTemp } from './helpers/temp.mjs';

const REPO_ROOT = resolve(fileURLToPath(import.meta.url), '..', '..');
const CORE = join(REPO_ROOT, 'core');

// update.sh --worker의 detached embed fork가 호출자 반환 뒤에도 workdir에 써서
// removeTemp와 경합한다(ENOTEMPTY). 이 파일 전역에서 끈다.
process.env.QMD_SKIP_BACKGROUND_EMBED = '1';

// --- 입력 조합 ---------------------------------------------------------------
//
// 행 하나 = 리팩터 전 raw 조합 하나. `legacy`는 그 조합을 그대로 적어 두고(기대 결과가
// 어느 세계의 관측인지 추적 가능하게), `compile`은 §2 진리표가 지정한 마이그레이션
// 결과다. 게이트에 실제로 먹이는 것은 `compile`이다.
//
// 진리표(§2): enabled false/부재 → off · enabled true + mode off/무효/부재 → off ·
// enabled true + candidates → candidates · true+guarded/auto-wiki + autoWrite true →
// 그대로 · autoWrite false → candidates.
const SCENARIOS = [
  { id: 'compile-absent', legacy: undefined, compile: undefined },
  { id: 'enabled-false', legacy: { enabled: false, mode: 'auto-wiki', autoWrite: true }, compile: { mode: 'off' } },
  { id: 'enabled-absent', legacy: { mode: 'auto-wiki', autoWrite: true }, compile: { mode: 'off' } },
  // ↓ 세 행은 리팩터 전 정규화가 전부 `[enabled:true, mode:"off"]` 한 상태로 접었다
  //   (`compile_config`가 무효/부재 mode를 off로 강제했다). 즉 §2 ※의 "유일한 비보존
  //   칸"은 raw 세 줄로 나타나는 **하나의** 정규화 상태다.
  { id: 'enabled-true+mode-off', legacy: { enabled: true, mode: 'off', autoWrite: true }, compile: { mode: 'off' }, legacyOnly: true },
  { id: 'enabled-true+mode-invalid', legacy: { enabled: true, mode: 'bogus', autoWrite: true }, compile: { mode: 'off' }, legacyOnly: true },
  { id: 'enabled-true+mode-absent', legacy: { enabled: true, autoWrite: true }, compile: { mode: 'off' }, legacyOnly: true },
  { id: 'candidates+autoWrite-true', legacy: { enabled: true, mode: 'candidates', autoWrite: true }, compile: { mode: 'candidates' } },
  { id: 'candidates+autoWrite-false', legacy: { enabled: true, mode: 'candidates', autoWrite: false }, compile: { mode: 'candidates' } },
  { id: 'guarded+autoWrite-true', legacy: { enabled: true, mode: 'guarded', autoWrite: true }, compile: { mode: 'guarded' } },
  { id: 'guarded+autoWrite-false', legacy: { enabled: true, mode: 'guarded', autoWrite: false }, compile: { mode: 'candidates' } },
  { id: 'auto-wiki+autoWrite-true', legacy: { enabled: true, mode: 'auto-wiki', autoWrite: true }, compile: { mode: 'auto-wiki' } },
  { id: 'auto-wiki+autoWrite-false', legacy: { enabled: true, mode: 'auto-wiki', autoWrite: false }, compile: { mode: 'candidates' } },
  // 하위호환은 폐기됐지만 **끄는 방향은 예외다**. 실패 방향이 비대칭이기 때문이다:
  // `enabled:true`를 무시하면 카드가 안 생길 뿐이지만(복구 가능), `enabled:false`를 무시하면
  // 사용자가 기록해 둔 opt-out을 어기고 유료 host CLI가 돈다(계정 청구, 복구 불가).
  // 0.26.0 문서가 `enabled:false`를 off 스위치로 안내했으므로 그 형태로 꺼 둔 프로젝트가
  // 업그레이드만으로 재무장해서는 안 된다. 그래서 off 방향만 존중하고 on 방향은 무시한다.
  { id: 'legacy off switch: enabled-false suppresses', legacy: null, compile: { enabled: false, autoWrite: false, mode: 'auto-wiki' } },
  { id: 'dead-keys: enabled-true must not activate', legacy: null, compile: { enabled: true, autoWrite: true, mode: 'off' } },
];

// 게이트를 통과했을 때 실제 동작(카드 쓰기·verify 큐잉)까지는 가되, 유료 host CLI가
// 절대 스폰되지 않도록 extractor를 비워 둔다. batch는 "시작 조건 미충족"으로 두어
// worker가 extractor 없이 JSON만 내고 큐를 되돌리게 한다.
function settingsFor(compile) {
  return {
    indexing: true,
    collections: ['gateproj-docs'],
    collectionPaths: { 'gateproj-docs': 'docs' },
    wikiPath: '.auto-context/wiki',
    ...(compile === undefined ? {} : {
      compile: {
        triggers: ['post_tool_source'],
        batch: { idleSeconds: 99999, maxItems: 99 },
        ...compile,
      },
    }),
  };
}

/** 게이트 하나를 위한 독립 프로젝트. 드라이버끼리 부작용이 섞이지 않게 매번 새로 만든다. */
function makeProject(prefix, compile) {
  const { home, dir } = isolatedHomeProject(prefix, 'gateproj');
  mkdirSync(join(dir, '.auto-context', 'compile'), { recursive: true });
  mkdirSync(join(dir, '.auto-context', 'wiki'), { recursive: true });
  mkdirSync(join(dir, 'docs'), { recursive: true });
  writeFileSync(join(dir, 'docs', 'note.md'), '# note\n\n한 문장 사실.\n');
  writeFileSync(
    join(dir, '.auto-context', 'settings.json'),
    `${JSON.stringify(settingsFor(compile), null, 2)}\n`,
  );
  return { home, dir };
}

function childEnv(home, extra = {}) {
  return {
    ...process.env,
    HOME: home,
    QMD_CACHE_DIR: join(home, 'cache'),
    QMD_SKIP_BACKGROUND_EMBED: '1',
    QMD_FAKE_PLATFORMS: 'none',
    QMD_BACKEND_MANAGER: '/bin/true',
    // 데몬에 절대 닿지 않게 한다 — 이 머신에 :8483이 떠 있으면 결과가 흔들린다.
    QMD_DAEMON_URL: 'http://127.0.0.1:1',
    QMD_SANDBOX: '',
    ...extra,
  };
}

function py(script, args, env, input) {
  return execFileSync('python3', [join(CORE, script), ...args], {
    cwd: REPO_ROOT,
    encoding: 'utf8',
    env,
    ...(input === undefined ? {} : { input }),
  });
}

// --- 게이트 드라이버 ---------------------------------------------------------
//
// 각 드라이버는 'pass' | 'skip'(또는 wiki_compile처럼 더 구체적인 결과 문자열)을 돌려준다.

const GATES = {
  // 1. core/wiki_compile_enqueue.py:106 (compile_gate)
  //    관측: 편집 훅 payload를 먹였을 때 source-queue.jsonl에 줄이 생기는가.
  'enqueue': (compile) => {
    const { home, dir } = makeProject('gate-enq', compile);
    try {
      py('wiki_compile_enqueue.py', [], childEnv(home), JSON.stringify({
        hook_event_name: 'PostToolUse',
        cwd: dir,
        tool_input: { file_path: join(dir, 'docs', 'note.md') },
      }));
      const queue = join(dir, '.auto-context', 'compile', 'source-queue.jsonl');
      if (!existsSync(queue)) return 'skip';
      return readFileSync(queue, 'utf8').trim() ? 'pass' : 'skip';
    } finally {
      removeTemp(home);
    }
  },

  // 2. core/wiki_compile_worker.py:953
  //    관측: --json이 무언가를 출력하는가. 게이트에서 막히면 print 전에 return 0이다.
  //    batch 시작 조건을 일부러 미충족으로 둬서 extractor 스폰 없이 JSON만 나온다.
  'worker': (compile) => {
    const { home, dir } = makeProject('gate-wrk', compile);
    try {
      writeFileSync(
        join(dir, '.auto-context', 'compile', 'source-queue.jsonl'),
        `${JSON.stringify({
          ts: '2026-08-03T00:00:00Z',
          trigger: 'post_tool_source',
          engine: 'unknown',
          cwd: dir,
          source: { kind: 'file', path: 'docs/note.md', collection: 'gateproj-docs' },
        })}\n`,
      );
      const out = py('wiki_compile_worker.py', ['--cwd', dir, '--json'], childEnv(home)).trim();
      return out ? 'pass' : 'skip';
    } finally {
      removeTemp(home);
    }
  },

  // 3. core/wiki_compile.py:1270 (+ autoWrite/guarded 분기 :1365,:1371)
  //    관측: stdout의 action. 게이트에서 막히면 무출력, 통과하면 'created' 또는
  //    'candidate'다. `targetPath`를 명시해 target_reason='explicit'으로 만들고
  //    extractor를 비워 judge를 unavailable로 두면 데몬 질의가 한 번도 일어나지 않는다.
  'wiki_compile': (compile, { confidence = 'high' } = {}) => {
    const { home, dir } = makeProject('gate-cmp', compile);
    try {
      const out = py('wiki_compile.py', ['--cwd', dir], childEnv(home), JSON.stringify({
        title: 'Gate probe',
        summary: '한 문장 사실.',
        suggestedType: 'concept',
        targetPath: 'concepts/gate-probe.md',
        confidence,
        sources: [{ kind: 'file', path: 'docs/note.md' }],
      })).trim();
      if (!out) return 'skip';
      return JSON.parse(out).action;
    } finally {
      removeTemp(home);
    }
  },

  // 4. core/wiki_dedup_scan.py:310  ← 리팩터 전에는 `enabled`만 봤다(비대칭의 한쪽)
  //    관측: dedup.log의 SKIP 줄. 게이트에서 막히면 'compile.mode is off',
  //    통과하면 그 다음 게이트('no wiki collection configured')까지 간다.
  //    wiki collection을 설정하지 않았으므로 데몬에는 닿지 않는다.
  'dedup_scan': (compile) => {
    const { home, dir } = makeProject('gate-ded', compile);
    const log = join(home, 'dedup.log');
    try {
      py('wiki_dedup_scan.py', ['--cwd', dir], childEnv(home, { QMD_DEDUP_LOG: log }));
      const text = existsSync(log) ? readFileSync(log, 'utf8') : '';
      if (text.includes('SKIP: compile.mode is off')) return 'skip';
      assert.match(text, /SKIP: no wiki collection configured/, 'dedup scan이 예상 밖 지점에서 멈췄다');
      return 'pass';
    } finally {
      removeTemp(home);
    }
  },

  // 5. core/wiki_verify_worker.py:959
  //    관측: --json이 무언가를 출력하는가(게이트 통과 시 run() 결과가 반드시 찍힌다).
  'verify_worker': (compile) => {
    const { home, dir } = makeProject('gate-ver', compile);
    try {
      const out = py('wiki_verify_worker.py', ['--cwd', dir, '--json'], childEnv(home)).trim();
      return out ? 'pass' : 'skip';
    } finally {
      removeTemp(home);
    }
  },

  // 6. core/update.sh:1121 (bash)  ← 리팩터 전에는 `enabled`만 봤다(비대칭의 다른 쪽)
  //    관측: SessionStart의 verify-ledger notice. compile 디렉터리를 읽기 전용으로 만들면
  //    `preflight_block_reason`이 'audit_ledger_unwritable'을 돌려주고 update.sh가 1줄
  //    알린다. 게이트에서 막히면 python 스니펫이 빈 문자열을 찍어 notice가 없다.
  //    (`test/wiki-compile-notice.test.mjs`에 이 케이스가 없어 침묵해도 스위트가 통과했다.)
  'update_notice': (compile) => {
    const { home, dir } = makeProject('gate-upd', compile);
    const bin = join(home, 'bin');
    const compileDir = join(dir, '.auto-context', 'compile');
    mkdirSync(bin, { recursive: true });
    // 실제 qmd/데몬에 닿지 않게 stub. (notice 테스트와 같은 관용구)
    writeFileSync(join(bin, 'qmd'), '#!/usr/bin/env sh\nexit 0\n', { mode: 0o755 });
    writeFileSync(join(bin, 'curl'), '#!/usr/bin/env sh\nexit 1\n', { mode: 0o755 });
    writeFileSync(join(bin, 'nohup'), '#!/usr/bin/env sh\nexit 0\n', { mode: 0o755 });
    chmodSync(compileDir, 0o555);
    try {
      const out = execFileSync('bash', [join(CORE, 'update.sh')], {
        cwd: REPO_ROOT,
        input: JSON.stringify({ cwd: dir }),
        encoding: 'utf8',
        env: childEnv(home, {
          PATH: `${bin}:${process.env.PATH}`,
          QMD_NOTICE_STATE_DIR: join(home, 'notice-state'),
          QMD_DIRTY_QUEUE: join(home, 'dirty-queue'),
          QMD_LOCK_BASE: join(home, 'locks'),
        }),
      });
      return out.includes('verify-deleted.jsonl') ? 'pass' : 'skip';
    } finally {
      chmodSync(compileDir, 0o755);
      removeTemp(home);
    }
  },
};

// --- 기대 매트릭스 -----------------------------------------------------------
//
// 열: enqueue · worker · wiki_compile · dedup_scan · verify_worker · update_notice
// `wiki_compile`만 'created'/'candidate'로 갈린다(autoWrite·guarded가 붙는 유일한 게이트).
const EXPECTED = {
  'compile-absent':            ['skip', 'skip', 'skip',      'skip', 'skip', 'skip'],
  'enabled-false':             ['skip', 'skip', 'skip',      'skip', 'skip', 'skip'],
  'enabled-absent':            ['skip', 'skip', 'skip',      'skip', 'skip', 'skip'],
  // ↓ 계획 §2 ※ — 유일한 비보존 칸. 리팩터 전에는 dedup_scan·update_notice만 돌았다
  //   (`['skip','skip','skip','pass','skip','pass']`). 판정이 하나가 되면서 전부 멈춘다.
  //   raw 세 줄이지만 리팩터 전 정규화 상태로는 하나(`enabled:true + mode:"off"`)다.
  'enabled-true+mode-off':     ['skip', 'skip', 'skip',      'skip', 'skip', 'skip'],
  'enabled-true+mode-invalid': ['skip', 'skip', 'skip',      'skip', 'skip', 'skip'],
  'enabled-true+mode-absent':  ['skip', 'skip', 'skip',      'skip', 'skip', 'skip'],
  'candidates+autoWrite-true':  ['pass', 'pass', 'candidate', 'pass', 'pass', 'pass'],
  'candidates+autoWrite-false': ['pass', 'pass', 'candidate', 'pass', 'pass', 'pass'],
  'guarded+autoWrite-true':     ['pass', 'pass', 'created',   'pass', 'pass', 'pass'],
  'guarded+autoWrite-false':    ['pass', 'pass', 'candidate', 'pass', 'pass', 'pass'],
  'auto-wiki+autoWrite-true':   ['pass', 'pass', 'created',   'pass', 'pass', 'pass'],
  'auto-wiki+autoWrite-false':  ['pass', 'pass', 'candidate', 'pass', 'pass', 'pass'],
  // 레거시 off 스위치는 존중하고(전 게이트 정지), 켜는 방향은 무시한다.
  'legacy off switch: enabled-false suppresses': ['skip', 'skip', 'skip',    'skip', 'skip', 'skip'],
  'dead-keys: enabled-true must not activate':   ['skip', 'skip', 'skip',    'skip', 'skip', 'skip'],
};

const GATE_NAMES = ['enqueue', 'worker', 'wiki_compile', 'dedup_scan', 'verify_worker', 'update_notice'];

test('기대 매트릭스가 시나리오·게이트를 빠짐없이 덮는다', () => {
  assert.deepEqual(Object.keys(EXPECTED).sort(), SCENARIOS.map((s) => s.id).sort());
  // 복사본을 정렬한다 — GATE_NAMES 자체를 정렬하면 열 순서가 바뀌어 아래 매트릭스가
  // 통째로 어긋난다(실제로 그렇게 깨졌다).
  assert.deepEqual([...GATE_NAMES].sort(), Object.keys(GATES).sort());
  for (const [id, row] of Object.entries(EXPECTED)) {
    assert.equal(row.length, GATE_NAMES.length, `${id}: 행 길이`);
  }
});

for (const scenario of SCENARIOS) {
  test(`게이트 매트릭스: ${scenario.id}`, () => {
    const observed = GATE_NAMES.map((name) => GATES[name](scenario.compile));
    assert.deepEqual(
      observed,
      EXPECTED[scenario.id],
      `${scenario.id}\n  gates:    ${GATE_NAMES.join(' ')}\n  expected: ${EXPECTED[scenario.id].join(' ')}\n  observed: ${observed.join(' ')}`,
    );
  });
}

// `guarded`는 dead가 아니다(계획 "검토에서 정정된 사실"). `mode:"guarded"`에서
// `confidence != "high"`인 후보는 auto-write되지 않고 candidate로 강등된다
// (`wiki_compile.py:1371`). 매트릭스 본체는 confidence='high'로 돌리므로 낮은 쪽을 따로 본다.
test('guarded는 confidence가 high가 아니면 candidate로 강등한다', () => {
  const compile = { mode: 'guarded' };
  assert.equal(GATES.wiki_compile(compile, { confidence: 'high' }), 'created');
  assert.equal(GATES.wiki_compile(compile, { confidence: 'medium' }), 'candidate');
  // auto-wiki는 confidence를 보지 않는다 — 두 모드의 차이가 바로 이것이다.
  assert.equal(GATES.wiki_compile({ mode: 'auto-wiki' }, { confidence: 'medium' }), 'created');
});

// 게이트가 보는 값은 raw가 아니라 `config.compile_config()`가 정규화한 값이다.
// 리팩터 전에는 이 정규화가 조합을 접었고(`enabled:false` → mode 강제 off, 무효 mode → off)
// raw 36조합이 도달하는 상태가 5개(× autoWrite 2 = 10줄)였다. 이제 `enabled`·`autoWrite`는
// 스키마에 없으므로 **mode 하나가 유일한 결정자**이고 도달 상태는 4개다. 두 사실을 함께
// 단정한다: (1) 제거된 키는 정규화 출력에 존재하지 않고 결과에 영향도 주지 않는다,
// (2) 마이그레이션 진리표(§2)가 지정한 목표 mode가 실제로 그 mode로 정규화된다.
test('정규화는 raw 36조합을 4개 상태로 접는다 (레거시 off 스위치는 존중)', () => {
  const script = `import json, sys
sys.path.insert(0, sys.argv[1])
import config as qmd_config
out = []
for enabled in (True, False, None):
    for mode in ("off", "candidates", "guarded", "auto-wiki", "bogus", None):
        for auto in (True, False):
            raw = {}
            if enabled is not None:
                raw["enabled"] = enabled
            if mode is not None:
                raw["mode"] = mode
            raw["autoWrite"] = auto
            n = qmd_config.compile_config(raw)
            out.append({
                "in": [enabled, mode, auto],
                "mode": n["mode"],
                "deadKeys": sorted(k for k in ("enabled", "autoWrite") if k in n),
            })
print(json.dumps(out))`;
  const rows = JSON.parse(execFileSync('python3', ['-c', script, CORE], {
    cwd: REPO_ROOT, encoding: 'utf8',
  }));
  assert.equal(rows.length, 36);
  assert.deepEqual(
    [...new Set(rows.map((r) => r.mode))].sort(),
    ['auto-wiki', 'candidates', 'guarded', 'off'],
  );
  for (const row of rows) {
    const [enabled, mode, autoWrite] = row.in;
    // mode가 활성 판정의 주 스위치이되, 레거시 **off 방향** 두 개는 존중한다: `enabled:false`는
    // 문서화됐던 off 스위치이고 `autoWrite:false`는 "카드를 자동으로 쓰지 말라"는 선택이라,
    // 무시하면 사용자가 기록해 둔 opt-out을 어기고 유료 호출이 난다. 켜는 방향(enabled:true)은
    // 여전히 무시한다 — 그쪽으로 틀리면 손해가 "카드가 안 생김"뿐이다.
    let expectedMode = ['candidates', 'guarded', 'auto-wiki'].includes(mode) ? mode : 'off';
    if (enabled === false) expectedMode = 'off';
    else if (autoWrite === false && ['auto-wiki', 'guarded'].includes(expectedMode)) expectedMode = 'candidates';
    assert.equal(row.mode, expectedMode, `in=${JSON.stringify(row.in)}`);
    // 제거된 키는 정규화 출력에 되살아나지 않는다(되살아나면 소비자가 다시 읽기 시작한다).
    assert.deepEqual(row.deadKeys, [], `in=${JSON.stringify(row.in)} dead keys leaked`);
  }
});

// §2 마이그레이션 진리표 자체를 못박는다. 위 테스트는 "새 스키마가 mode만 본다"이고
// 이것은 "구 파일을 어떤 mode로 옮겨야 동작이 보존되는가"다 — 후자는 H단계(로컬
// settings.json 마이그레이션)가 따라야 할 명세이므로 코드와 함께 고정한다.
test('§2 진리표: 구 (enabled, mode, autoWrite) → 새 mode', () => {
  const target = (enabled, mode, autoWrite) => {
    if (enabled !== true) return 'off';
    if (!['candidates', 'guarded', 'auto-wiki'].includes(mode)) return 'off';
    if (mode === 'candidates') return 'candidates';
    return autoWrite === true ? mode : 'candidates';
  };
  // 표에 적힌 8행을 그대로 확인한다(대표값 하나씩).
  assert.equal(target(false, 'auto-wiki', true), 'off');
  assert.equal(target(undefined, 'auto-wiki', true), 'off');
  assert.equal(target(true, 'off', true), 'off');
  assert.equal(target(true, 'bogus', true), 'off');
  assert.equal(target(true, undefined, true), 'off');
  assert.equal(target(true, 'candidates', false), 'candidates');
  assert.equal(target(true, 'auto-wiki', false), 'candidates');
  assert.equal(target(true, 'auto-wiki', true), 'auto-wiki');
  assert.equal(target(true, 'guarded', false), 'candidates');
  assert.equal(target(true, 'guarded', true), 'guarded');
  // 그리고 SCENARIOS의 legacy → compile 매핑이 표와 어긋나지 않는지 대조한다.
  for (const s of SCENARIOS) {
    if (!s.legacy) continue;
    assert.equal(
      s.compile.mode,
      target(s.legacy.enabled, s.legacy.mode, s.legacy.autoWrite),
      `${s.id}: 마이그레이션 입력이 §2 진리표와 다르다`,
    );
  }
});

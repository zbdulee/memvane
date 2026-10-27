// 모든 config writer가 만들어 내는 **effective config**(= 쓰인 파일을 다시 읽어
// `core/config.py`의 `normalize_config()`에 통과시킨 결과)를 스냅샷으로 동결한다.
//
// 왜 emitted가 아니라 effective인가: writer를 "delta-only"(DEFAULT_CONFIG 기본값과 같은
// 키는 쓰지 않음)로 바꾸는 리팩터가 예정돼 있고, 그 리팩터는 **emitted 모양을 의도적으로
// 줄인다**. 따라서 emitted를 단정하는 기존 테스트(`wiki-compile-defaults` ·
// `enable-compile` · `recommend-config`)는 "의도된 축소"와 "우발적 동작 변경"을
// 구분하지 못한다. writer가 쓰던 값이 기본값과 **다른데** 그 키를 지우면 effective가
// 조용히 바뀌는데, 이 파일이 그 클래스를 잡는 유일한 장치다.
//
// 스냅샷 갱신은 의도적으로 수동이다 — `QMD_FREEZE_UPDATE=1 node --test <이 파일>`.
// 리팩터 중에 무심코 재생성하면 동결의 의미가 사라지므로, 갱신 시에는 diff를 반드시 읽을 것.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { isolatedHomeProject, removeTemp, tempCacheDir } from './helpers/temp.mjs';

const REPO_ROOT = resolve(fileURLToPath(import.meta.url), '..', '..');
const CORE = join(REPO_ROOT, 'core');
const FIXTURE = join(REPO_ROOT, 'test', 'fixtures', 'config-effective-freeze.json');
const UPDATE = process.env.QMD_FREEZE_UPDATE === '1';

// update.sh --worker의 detached embed fork가 호출자 반환 뒤에도 workdir에 써서
// removeTemp와 경합한다(ENOTEMPTY). 이 파일 전역에서 끈다.
process.env.QMD_SKIP_BACKGROUND_EMBED = '1';

// notice/TTL marker가 실제 HOME(~/.cache/qmd)에 쌓이지 않도록 파일당 하나.
const CACHE = tempCacheDir('emission-freeze');

// 프로젝트 디렉터리 이름은 컬렉션 이름(`<slug>-wiki` 등)에 그대로 들어간다.
// 스냅샷이 결정적이려면 mkdtemp 랜덤 접미사가 아니라 고정 이름이어야 한다.
const PROJECT_NAME = 'freezeproj';

function childEnv(home) {
  return {
    ...process.env,
    HOME: home,
    QMD_CACHE_DIR: CACHE,
    CLAUDE_PLUGIN_ROOT: REPO_ROOT,
    QMD_SKIP_BACKGROUND_EMBED: '1',
    // 실제 데몬/플랫폼 감지를 타지 않게 한다(이 writer 경로들은 backend를 부르지 않지만,
    // 경로가 바뀌어도 사용자 환경을 건드리지 않도록 고정한다).
    QMD_FAKE_PLATFORMS: 'none',
    QMD_BACKEND_MANAGER: '/bin/true',
    QMD_SANDBOX: '',
  };
}

const NORMALIZE_PY = `import json, sys
sys.path.insert(0, sys.argv[1])
import config as qmd_config
with open(sys.argv[2], encoding="utf-8") as fh:
    raw = json.load(fh)
print(json.dumps(qmd_config.normalize_config(raw), ensure_ascii=False, sort_keys=True))
`;

/** 쓰인 settings.json → normalize_config() → effective config */
function effective(settingsPath) {
  const out = execFileSync('python3', ['-c', NORMALIZE_PY, CORE, settingsPath], {
    cwd: REPO_ROOT,
    encoding: 'utf8',
  });
  return JSON.parse(out);
}

/** writer를 격리 HOME 하위 고정 이름 프로젝트에 대해 돌리고 effective config를 돌려준다. */
function captureWriter(prefix, run) {
  const { home, dir, } = isolatedHomeProject(prefix, PROJECT_NAME);
  try {
    run(dir, childEnv(home));
    const settings = join(dir, '.auto-context', 'settings.json');
    assert.equal(existsSync(settings), true, `writer가 settings.json을 쓰지 않았다: ${settings}`);
    return effective(settings);
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

// --- writer 4종(= 5경로) --------------------------------------------------

const WRITERS = {
  // 1. core/wiki_compile_defaults.py :: compile_block()
  //    파일 writer가 아니라 블록 생성기이므로, 이 블록만 담은 config를 통과시킨다.
  'wiki_compile_defaults.compile_block': () => captureWriter('freeze-block', (dir, env) => {
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

  // 2a. core/update.sh --init-wiki (기본 preset)
  'update.sh --init-wiki': () => captureWriter('freeze-initwiki', (dir, env) => {
    sh(['--init-wiki'], dir, env);
  }),

  // 2b. core/update.sh --init-wiki --preset novel (compile 블록을 따로 쓰는 분기)
  'update.sh --init-wiki --preset novel': () => captureWriter('freeze-novel', (dir, env) => {
    sh(['--init-wiki', '--preset', 'novel'], dir, env);
  }),

  // 3. core/update.sh --enable-compile (opt-in된 프로젝트가 전제)
  'update.sh --enable-compile': () => captureWriter('freeze-enable', (dir, env) => {
    mkdirSync(join(dir, '.auto-context'), { recursive: true });
    mkdirSync(join(dir, 'docs'), { recursive: true });
    writeFileSync(join(dir, '.auto-context', 'settings.json'), `${JSON.stringify({
      indexing: true,
      collections: [`${PROJECT_NAME}-docs`],
      collectionPaths: { [`${PROJECT_NAME}-docs`]: 'docs' },
    }, null, 2)}\n`);
    sh(['--enable-compile'], dir, env);
  }),

  // 4. core/recommend_config.py 경유 (--optin --recommended가 그 출력을 그대로 쓴다)
  'update.sh --optin --recommended': () => captureWriter('freeze-optin', (dir, env) => {
    mkdirSync(join(dir, 'docs', 'current'), { recursive: true });
    mkdirSync(join(dir, 'docs', 'plans'), { recursive: true });
    sh(['--optin', '--recommended'], dir, env);
  }),
};

// --- 스냅샷 -----------------------------------------------------------------

const captured = Object.fromEntries(
  Object.entries(WRITERS).map(([name, capture]) => [name, capture()]),
);

if (UPDATE) {
  mkdirSync(join(REPO_ROOT, 'test', 'fixtures'), { recursive: true });
  writeFileSync(FIXTURE, `${JSON.stringify(captured, null, 2)}\n`);
}

assert.equal(
  existsSync(FIXTURE),
  true,
  `스냅샷이 없다. 최초 1회: QMD_FREEZE_UPDATE=1 node --test ${'test/config-emission-freeze.test.mjs'}`,
);
const snapshot = JSON.parse(readFileSync(FIXTURE, 'utf8'));

test('스냅샷이 모든 writer를 덮는다 (writer가 조용히 빠지지 않게)', () => {
  assert.deepEqual(Object.keys(snapshot).sort(), Object.keys(WRITERS).sort());
  for (const [name, cfg] of Object.entries(snapshot)) {
    assert.equal(typeof cfg, 'object', `${name}: 스냅샷이 객체가 아니다`);
    assert.ok(Object.keys(cfg).length > 10, `${name}: 스냅샷이 placeholder처럼 비어 있다`);
    assert.equal(typeof cfg.compile, 'object', `${name}: compile 하위트리 결측`);
  }
});

for (const name of Object.keys(WRITERS)) {
  test(`effective config 동결: ${name}`, () => {
    assert.deepEqual(captured[name], snapshot[name]);
  });
}

// 예전에는 writer(120)와 `DEFAULT_CONFIG`(30)가 갈려 있었고, 이 자리는 그 불일치를 못박아
// delta-only 리팩터가 "기본값과 같으니 생략"으로 오판하는 것을 막는 가드였다.
// 그 불일치는 기본값을 120으로 올려 해소했다(§6.1) — 이제는 **모든 경로가 120으로 수렴**하는
// 것이 불변식이다. 기본값이 다시 갈리면(생성기만 올리거나 기본값만 내리면) 여기서 먼저 깨진다.
// compile을 켜면서 `extractor`를 쓰지 않는 writer(`--init-wiki` novel preset)가 조용히
// 짧은 timeout을 물려받던 것이 이 클래스의 실제 피해였다.
test('compile.extractor.timeout은 기본값·생성기 모두 120이다', () => {
  const py = `import json, sys
sys.path.insert(0, sys.argv[1])
import config as qmd_config
print(json.dumps(qmd_config.DEFAULT_CONFIG["compile"]["extractor"]["timeout"]))`;
  const dflt = JSON.parse(execFileSync('python3', ['-c', py, CORE], { cwd: REPO_ROOT, encoding: 'utf8' }));
  assert.equal(dflt, 120);
  // 스냅샷이 아니라 이번 실행에서 실제로 관측한 값을 본다 — 스냅샷을 보면
  // "writer가 이 키를 떨어뜨렸다"를 잡지 못하고 동결 테스트에만 의존하게 된다.
  for (const name of Object.keys(WRITERS)) {
    assert.equal(captured[name].compile.extractor.timeout, 120, `${name}: extractor.timeout`);
  }
});

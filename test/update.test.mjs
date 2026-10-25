import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync, spawnSync } from 'node:child_process';
import { existsSync, mkdirSync, readdirSync, rmSync, symlinkSync, utimesSync, writeFileSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { removeTemp, repoTemp, tempCacheDir } from './helpers/temp.mjs';
import { waitUntil, waitUntilAsync } from './helpers/timing.mjs';

function prepareTestStore(home) {
  mkdirSync(home, { recursive: true });
  execFileSync('python3', ['test/fixtures/prepare-shared-qmd-store.py', home], {
    env: { ...process.env, HOME: home, PYTHONDONTWRITEBYTECODE: '1' },
  });
  execFileSync('python3', ['test/fixtures/prepare-synthetic-qmd-runtime.py', home], {
    env: { ...process.env, HOME: home, PYTHONDONTWRITEBYTECODE: '1' },
  });
}

function seedMissingCollection(home, work, name = 'gone', relative = 'missing') {
  execFileSync('python3', ['test/fixtures/seed-missing-qmd-collection.py',
    home, work, name, relative], {
    env: { ...process.env, HOME: home, PYTHONDONTWRITEBYTECODE: '1' },
  });
}

function readIfExists(path) {
  return existsSync(path) ? readFileSync(path, 'utf8') : '';
}

// 진단 로그는 단정이 실패할 때만, 그리고 있을 때만 읽는다(성공 경로에서 ENOENT 금지).
function withLog(logPath, check) {
  try {
    check();
  } catch (error) {
    error.message += `\n--- ${logPath} ---\n${readIfExists(logPath) || '(absent)'}`;
    throw error;
  }
}

// 제품 게이트 함수를 격리 HOME 에서 직접 불러 차단 이유를 얻는다(CLI 는 exit 1 만 준다).
// npm test 가 물려주는 QMD_SETUP_GUARD_FIXTURE 는 비워 실제 게이트를 검증한다.
function gateResult(home, code, args = []) {
  const py = `import json, sys
sys.path.insert(0, "core")
${code}`;
  return JSON.parse(execFileSync('python3', ['-c', py, ...args], {
    encoding: 'utf8',
    env: { ...process.env, HOME: home, PYTHONDONTWRITEBYTECODE: '1', QMD_SETUP_GUARD_FIXTURE: '' },
  }));
}

function sharedRegistry(home) {
  const raw = readIfExists(join(home, 'Library/Application Support/qmd-auto-context/shared-qmd/collections.json'));
  return raw ? JSON.parse(raw) : {};
}

// 생성기는 delta-only다(기본값과 같은 키는 쓰지 않는다). 따라서 "writer가 설정을 켰는가"는
// emit된 키가 아니라 **effective config**(= normalize_config 통과 결과)로 봐야 한다.
// emit을 단정하면 "의도된 축소"와 "우발적 동작 변경"이 구분되지 않는다
// (test/config-emission-freeze.test.mjs 헤더 주석 참고).
function effectiveConfig(settingsPath) {
  const py = `import json, sys
sys.path.insert(0, "core")
import config as qmd_config
with open(sys.argv[1], encoding="utf-8") as fh:
    print(json.dumps(qmd_config.normalize_config(json.load(fh)), ensure_ascii=False))`;
  return JSON.parse(execFileSync('python3', ['-c', py, settingsPath], { encoding: 'utf8' }));
}

function resolvePaths(cwd, configJson) {
  // update.sh --resolve-only: qmd 미실행, 컬렉션→경로 매핑 결과만 stdout JSON.
  // 상태(pending/optout/동의)는 stdin config의 indexing/collections로만 판정(파일/전역 안 읽음).
  const out = execFileSync('bash', ['core/update.sh', '--resolve-only', '--cwd', cwd], { input: configJson });
  return JSON.parse(out.toString());
}

// core/update.sh --worker forks a DETACHED background subshell (embed + dedup scan) that
// keeps writing into the temp workdir after execFileSync returns, racing rmSync (ENOTEMPTY,
// measured ~1 run in 4). Every test here uses a stub qmd, so that background work is
// meaningless; the one test that verifies the fork itself opts out below.
process.env.QMD_SKIP_BACKGROUND_EMBED = '1';
// 그 fork 는 스크립트를 처음부터 재실행하므로 선두의 `mkdir -p "$_QMD_CACHE_DIR"` 가 테스트가
// 이미 지운 임시 디렉터리를 되살린다(`removeTemp` 는 성공하는데 그 **뒤에** 되살아나므로 정리
// 쪽에서는 막을 수 없다). embed 가드는 fork **안**의 단계만 끄므로 이 잔해를 막지 못한다 —
// fork 자체를 막는 스위치가 따로 필요하다. 아래 fork 검증 테스트만 이것도 함께 해제한다.
process.env.QMD_SKIP_BACKGROUND_WORKER = '1';

// 정리 재시도(removeTemp)와 임시 디렉터리 base(repoTemp)는 test/helpers/temp.mjs 가 SSOT다
// — 예전엔 이 파일에만 있어서 나머지 테스트 40여 개가 맨 rmSync 로 같은 경합에 노출됐다.

// update.sh 를 호출하는 모든 자리에 QMD_CACHE_DIR 을 준다 — 기본값 `$HOME/.cache/qmd` 에
// notice_once TTL marker/update-status/hook.log 가 떨어져 사용자 홈에 계속 쌓였다(실측 이
// 파일 1회 실행당 12개, 누적 5,100개). 근거·왜 workdir 밖인지는 tempCacheDir docstring 참고.
const CACHE_DIR = tempCacheDir('update');

test('collectionPaths 매핑 해석 (story 패턴)', () => {
  const r = resolvePaths('/Users/example/work/novel/my-novel', JSON.stringify({
    collections: ['story-manuscript', 'story-plot'],
    collectionPaths: { '*-manuscript': '04_Manuscript', '*-plot': '03_Plot' },
  }));
  assert.ok(r.entries.some(e => e.name === 'story-manuscript' && e.path.endsWith('04_Manuscript')));
});

test('설정 없으면 인덱싱하지 않고 pending', () => {
  // 빈 config(파일 없음) → pending. resolve_paths는 stdin config만 보므로 전역 파일 불필요.
  const r = resolvePaths('/Users/example/work/sample', '');
  assert.equal(r.refused, true);
  assert.equal(r.reason, 'pending');
  assert.deepEqual(r.entries, []);
});

test('risky 시스템 경로 거부', () => {
  const r = resolvePaths('/Library/OSAnalytics', '');
  assert.equal(r.refused, true);
});

test('collectionPaths 절대경로와 traversal 은 cwd 밖이면 skip', () => {
  const cwd = repoTemp('qmd-safe-root');
  const outside = repoTemp('qmd-outside');
  try {
    const r = resolvePaths(cwd, JSON.stringify({
      collections: ['ok', 'escape', 'absolute'],
      collectionPaths: {
        ok: '.',
        escape: '../outside',
        absolute: outside,
      },
    }));
    assert.deepEqual(r.entries.map(e => e.name), ['ok']);
  } finally {
    removeTemp(cwd);
    removeTemp(outside);
  }
});

test('collectionPaths 명시 allowRoots 하위 절대경로는 허용', () => {
  const cwd = repoTemp('qmd-safe-root');
  const allowed = repoTemp('qmd-allowed');
  try {
    const r = resolvePaths(cwd, JSON.stringify({
      collections: ['allowed'],
      collectionPaths: { allowed },
      allowRoots: [allowed],
    }));
    assert.deepEqual(r.entries, [{ name: 'allowed', path: allowed }]);
  } finally {
    removeTemp(cwd);
    removeTemp(allowed);
  }
});

test('update core: sessionStart disabled이면 qmd 실행 없이 skip', () => {
  const work = repoTemp('qmd-update-events');
  const bin = join(work, 'bin');
  const qmdLog = join(work, 'qmd.log');
  try {
    mkdirSync(join(work, '.agents'), { recursive: true });
    mkdirSync(bin, { recursive: true });
    writeFileSync(join(work, '.agents', 'qmd-recall.json'), JSON.stringify({ collections: ['x'], events: ['userPromptSubmit'] }));
    writeFileSync(join(bin, 'qmd'), `#!/usr/bin/env sh\necho "$@" >> "${qmdLog}"\nexit 0\n`, { mode: 0o755 });

    execFileSync('bash', [join(process.cwd(), 'core', 'update.sh'), '--worker', work], {
      env: { ...process.env, PATH: `${bin}:${process.env.PATH}`, QMD_CACHE_DIR: CACHE_DIR },
    });

    assert.throws(() => readFileSync(qmdLog, 'utf8'), 'qmd should not be invoked when sessionStart is disabled');
  } finally {
    removeTemp(work);
  }
});

test('update core: sessionStart disabled from .auto-context/settings.json skips qmd', () => {
  const work = repoTemp('qmd-update-settings-events');
  const bin = join(work, 'bin');
  const qmdLog = join(work, 'qmd.log');
  try {
    mkdirSync(join(work, '.auto-context'), { recursive: true });
    mkdirSync(bin, { recursive: true });
    writeFileSync(join(work, '.auto-context', 'settings.json'), JSON.stringify({
      collections: ['x'],
      collectionPaths: { x: 'missing' },
      events: ['userPromptSubmit'],
    }));
    writeFileSync(join(bin, 'qmd'), `#!/usr/bin/env sh\necho "$@" >> "${qmdLog}"\nexit 0\n`, { mode: 0o755 });

    execFileSync('bash', [join(process.cwd(), 'core', 'update.sh'), '--worker', work], {
      env: { ...process.env, PATH: `${bin}:${process.env.PATH}`, QMD_CACHE_DIR: CACHE_DIR },
    });

    assert.throws(() => readFileSync(qmdLog, 'utf8'), 'qmd should not be invoked when sessionStart is disabled');
    const cfg = JSON.parse(readFileSync(join(work, '.auto-context', 'settings.json'), 'utf8'));
    assert.deepEqual(cfg.collections, ['x'], 'disabled sessionStart must not prune settings');
  } finally {
    removeTemp(work);
  }
});

test('update core: missing settings collection root is pruned before qmd update', () => {
  const work = repoTemp('qmd-update-prune-missing');
  const bin = join(work, 'bin');
  const fakeHome = join(work, 'fakehome');
  prepareTestStore(fakeHome);
  const qmdLog = join(work, 'qmd.log');
  const lockBase = join(work, 'locks');
  try {
    mkdirSync(join(work, '.auto-context'), { recursive: true });
    mkdirSync(join(work, 'docs'), { recursive: true });
    mkdirSync(bin, { recursive: true });
    mkdirSync(fakeHome, { recursive: true });
    writeFileSync(join(work, '.auto-context', 'settings.json'), JSON.stringify({
      indexing: true,
      collections: ['docs', 'gone'],
      collectionPaths: { docs: 'docs', gone: 'missing' },
      collectionRoles: { docs: 'raw', gone: 'wiki' },
    }));
    seedMissingCollection(fakeHome, work);
    writeFileSync(join(bin, 'qmd'), [
      '#!/usr/bin/env sh',
      `log="${qmdLog}"`,
      'echo "$@" >> "$log"',
      'exit 0',
    ].join('\n'), { mode: 0o755 });

    execFileSync('bash', [join(process.cwd(), 'core', 'update.sh'), '--worker', work], {
      encoding: 'utf8',
      env: {
        ...process.env,
        PATH: `${bin}:${process.env.PATH}`,
        HOME: fakeHome,
        QMD_CACHE_DIR: CACHE_DIR,
        QMD_LOCK_BASE: lockBase,
      },
    });

    const cfg = JSON.parse(readFileSync(join(work, '.auto-context', 'settings.json'), 'utf8'));
    withLog(join(CACHE_DIR, 'hook.log'), () => assert.deepEqual(cfg.collections, ['docs']));
    assert.deepEqual(cfg.collectionPaths, { docs: 'docs' });
    assert.deepEqual(cfg.collectionRoles, { docs: 'raw' });
    const log = readFileSync(qmdLog, 'utf8');
    assert.match(log, /collection remove gone/);
    assert.doesNotMatch(log, /collection add .*missing --name gone/);
    assert.match(log, /^update$/m);
  } finally {
    removeTemp(work);
  }
});

test('update core: failed qmd collection remove keeps settings collection for retry', () => {
  const work = repoTemp('qmd-update-prune-remove-fail');
  const bin = join(work, 'bin');
  const fakeHome = join(work, 'fakehome');
  prepareTestStore(fakeHome);
  const qmdLog = join(work, 'qmd.log');
  const lockBase = join(work, 'locks');
  try {
    mkdirSync(join(work, '.auto-context'), { recursive: true });
    mkdirSync(join(work, 'docs'), { recursive: true });
    mkdirSync(bin, { recursive: true });
    mkdirSync(fakeHome, { recursive: true });
    writeFileSync(join(work, '.auto-context', 'settings.json'), JSON.stringify({
      indexing: true,
      collections: ['docs', 'gone'],
      collectionPaths: { docs: 'docs', gone: 'missing' },
      collectionRoles: { docs: 'raw', gone: 'wiki' },
    }));
    seedMissingCollection(fakeHome, work);
    writeFileSync(join(bin, 'qmd'), [
      '#!/usr/bin/env sh',
      `log="${qmdLog}"`,
      'echo "$@" >> "$log"',
      'case "$1 $2" in',
      '  "collection remove") exit 1 ;;',
      '  *) exit 0 ;;',
      'esac',
    ].join('\n'), { mode: 0o755 });

    execFileSync('bash', [join(process.cwd(), 'core', 'update.sh'), '--worker', work], {
      encoding: 'utf8',
      env: {
        ...process.env,
        PATH: `${bin}:${process.env.PATH}`,
        HOME: fakeHome,
        QMD_CACHE_DIR: CACHE_DIR,
        QMD_LOCK_BASE: lockBase,
      },
    });

    const cfg = JSON.parse(readFileSync(join(work, '.auto-context', 'settings.json'), 'utf8'));
    assert.deepEqual(cfg.collections, ['docs', 'gone']);
    assert.deepEqual(cfg.collectionPaths, { docs: 'docs', gone: 'missing' });
    assert.deepEqual(cfg.collectionRoles, { docs: 'raw', gone: 'wiki' });
    assert.match(readFileSync(qmdLog, 'utf8'), /collection remove gone/);
  } finally {
    removeTemp(work);
  }
});

test('update core: pruning the last settings collection writes indexing false', () => {
  const work = repoTemp('qmd-update-prune-last');
  const bin = join(work, 'bin');
  const fakeHome = join(work, 'fakehome');
  prepareTestStore(fakeHome);
  const qmdLog = join(work, 'qmd.log');
  const lockBase = join(work, 'locks');
  try {
    mkdirSync(join(work, '.auto-context'), { recursive: true });
    mkdirSync(bin, { recursive: true });
    mkdirSync(fakeHome, { recursive: true });
    writeFileSync(join(work, '.auto-context', 'settings.json'), JSON.stringify({
      indexing: true,
      collections: ['gone'],
      collectionPaths: { gone: 'missing' },
      collectionRoles: { gone: 'wiki' },
    }));
    seedMissingCollection(fakeHome, work);
    writeFileSync(join(bin, 'qmd'), [
      '#!/usr/bin/env sh',
      `log="${qmdLog}"`,
      'echo "$@" >> "$log"',
      'exit 0',
    ].join('\n'), { mode: 0o755 });

    execFileSync('bash', [join(process.cwd(), 'core', 'update.sh'), '--worker', work], {
      encoding: 'utf8',
      env: {
        ...process.env,
        PATH: `${bin}:${process.env.PATH}`,
        HOME: fakeHome,
        QMD_CACHE_DIR: CACHE_DIR,
        QMD_LOCK_BASE: lockBase,
      },
    });

    const cfg = JSON.parse(readFileSync(join(work, '.auto-context', 'settings.json'), 'utf8'));
    assert.equal(cfg.indexing, false);
    assert.deepEqual(cfg.collections, []);
    assert.deepEqual(cfg.collectionPaths, {});
    assert.deepEqual(cfg.collectionRoles, {});
    assert.match(readFileSync(qmdLog, 'utf8'), /collection remove gone/);
  } finally {
    removeTemp(work);
  }
});

test('update core: worker migration keeps missing legacy collection without an approved removal', () => {
  const work = repoTemp('qmd-update-prune-legacy-migrated');
  const bin = join(work, 'bin');
  const fakeHome = join(work, 'fakehome');
  prepareTestStore(fakeHome);
  const qmdLog = join(work, 'qmd.log');
  const hookLog = join(work, 'hook.log');
  const lockBase = join(work, 'locks');
  try {
    mkdirSync(bin, { recursive: true });
    mkdirSync(fakeHome, { recursive: true });
    writeFileSync(join(work, '.auto-context.json'), JSON.stringify({
      indexing: true,
      collections: ['gone'],
      collectionPaths: { gone: 'missing' },
    }));
    const legacyBytes = readFileSync(join(work, '.auto-context.json'));
    writeFileSync(join(bin, 'qmd'), [
      '#!/usr/bin/env sh',
      `log="${qmdLog}"`,
      'echo "$@" >> "$log"',
      'exit 0',
    ].join('\n'), { mode: 0o755 });

    execFileSync('bash', [join(process.cwd(), 'core', 'update.sh'), '--worker', work], {
      encoding: 'utf8',
      env: {
        ...process.env,
        PATH: `${bin}:${process.env.PATH}`,
        HOME: fakeHome,
        QMD_CACHE_DIR: CACHE_DIR,
        QMD_LOCK_BASE: lockBase,
        QMD_HOOK_LOG: hookLog,
        QMD_SETUP_GUARD_FIXTURE: '',
      },
    });

    // The selected-route ownership gate aborts before migration, so the legacy
    // config stays byte-identical and no canonical settings are written.
    assert.deepEqual(readFileSync(join(work, '.auto-context.json')), legacyBytes);
    assert.equal(existsSync(join(work, '.auto-context', 'settings.json')), false);
    withLog(hookLog, () => {
      const hook = readFileSync(hookLog, 'utf8');
      assert.doesNotMatch(hook, /PRUNE MISSING COLLECTION/);
      assert.match(hook, /ABORT: selected QMD route unavailable or project ownership not approved/);
    });
    const register = spawnSync('python3', ['core/qmd_route.py', 'register-config', work], {
      encoding: 'utf8',
      env: { ...process.env, HOME: fakeHome, PYTHONDONTWRITEBYTECODE: '1', QMD_SETUP_GUARD_FIXTURE: '' },
    });
    assert.equal(register.status, 1, 'unregistered missing root must be rejected by register-config');
    assert.equal('gone' in sharedRegistry(fakeHome), false, 'rejected name must not be reserved');
    assert.equal(existsSync(qmdLog), false, 'unregistered missing collection blocks QMD writes');
  } finally {
    removeTemp(work);
  }
});

test('update core: missing root prune refuses symlinked .auto-context directory', () => {
  const work = repoTemp('qmd-update-prune-symlink');
  const outside = repoTemp('qmd-update-prune-symlink-outside');
  const bin = join(work, 'bin');
  const fakeHome = join(work, 'fakehome');
  prepareTestStore(fakeHome);
  const qmdLog = join(work, 'qmd.log');
  const hookLog = join(work, 'hook.log');
  const lockBase = join(work, 'locks');
  try {
    mkdirSync(bin, { recursive: true });
    mkdirSync(fakeHome, { recursive: true });
    symlinkSync(outside, join(work, '.auto-context'), 'dir');
    writeFileSync(join(outside, 'settings.json'), JSON.stringify({
      indexing: true,
      collections: ['gone'],
      collectionPaths: { gone: 'missing' },
    }));
    writeFileSync(join(bin, 'qmd'), [
      '#!/usr/bin/env sh',
      `log="${qmdLog}"`,
      'echo "$@" >> "$log"',
      'exit 0',
    ].join('\n'), { mode: 0o755 });

    execFileSync('bash', [join(process.cwd(), 'core', 'update.sh'), '--worker', work], {
      encoding: 'utf8',
      env: {
        ...process.env,
        PATH: `${bin}:${process.env.PATH}`,
        HOME: fakeHome,
        QMD_CACHE_DIR: CACHE_DIR,
        QMD_LOCK_BASE: lockBase,
        QMD_HOOK_LOG: hookLog,
        QMD_SETUP_GUARD_FIXTURE: '',
      },
    });

    const cfg = JSON.parse(readFileSync(join(outside, 'settings.json'), 'utf8'));
    assert.deepEqual(cfg.collections, ['gone']);
    // The settings-dir gate classifies the symlink as unsafe setup state.
    assert.deepEqual(gateResult(fakeHome, `import config
print(json.dumps(list(config._safe_project_settings_dir(sys.argv[1]))))`, [work]),
    [null, 'unsafe_settings_dir']);
    // Prune never touches the symlinked settings, and the ownership gate still blocks the missing root.
    withLog(hookLog, () => {
      const hook = readFileSync(hookLog, 'utf8');
      assert.doesNotMatch(hook, /PRUNE MISSING COLLECTION/);
      assert.match(hook, /ABORT: selected QMD route unavailable or project ownership not approved/);
    });
    assert.equal(existsSync(qmdLog), false, 'symlinked settings block QMD writes');
  } finally {
    removeTemp(work);
    removeTemp(outside);
  }
});

test('update core: settings write failure after remove aborts stale update', () => {
  const work = repoTemp('qmd-update-prune-write-fail');
  const bin = join(work, 'bin');
  const fakeHome = join(work, 'fakehome');
  prepareTestStore(fakeHome);
  const qmdLog = join(work, 'qmd.log');
  const lockBase = join(work, 'locks');
  try {
    mkdirSync(join(work, '.auto-context'), { recursive: true });
    mkdirSync(join(work, 'docs'), { recursive: true });
    mkdirSync(bin, { recursive: true });
    mkdirSync(fakeHome, { recursive: true });
    writeFileSync(join(work, '.auto-context', 'settings.json'), JSON.stringify({
      indexing: true,
      collections: ['docs', 'gone'],
      collectionPaths: { docs: 'docs', gone: 'missing' },
    }));
    seedMissingCollection(fakeHome, work);
    writeFileSync(join(bin, 'qmd'), [
      '#!/usr/bin/env sh',
      `log="${qmdLog}"`,
      'echo "$@" >> "$log"',
      'case "$1 $2" in',
      `  "collection remove") chmod 500 "${join(work, '.auto-context')}"; exit 0 ;;`,
      '  *) exit 0 ;;',
      'esac',
    ].join('\n'), { mode: 0o755 });

    execFileSync('bash', [join(process.cwd(), 'core', 'update.sh'), '--worker', work], {
      encoding: 'utf8',
      env: {
        ...process.env,
        PATH: `${bin}:${process.env.PATH}`,
        HOME: fakeHome,
        QMD_CACHE_DIR: CACHE_DIR,
        QMD_LOCK_BASE: lockBase,
      },
    });

    const log = readFileSync(qmdLog, 'utf8');
    assert.match(log, /collection remove gone/);
    assert.doesNotMatch(log, /collection add .*missing --name gone/);
    assert.doesNotMatch(log, /^update$/m);
  } finally {
    try {
      execFileSync('chmod', ['700', join(work, '.auto-context')]);
    } catch {
      // ignore cleanup permission repair failures
    }
    removeTemp(work);
  }
});

test('update core: --migrate-config migrates legacy config and prints result', () => {
  const work = repoTemp('qmd-migrate-config');
  try {
    writeFileSync(join(work, '.auto-context.json'), JSON.stringify({ indexing: true, collections: ['x'] }));
    const out = execFileSync('bash', [join(process.cwd(), 'core', 'update.sh'), '--migrate-config', work], { encoding: 'utf8' });
    assert.ok(out.includes('Migrated'), `expected migrated message, got: ${out}`);
    assert.equal(existsSync(join(work, '.auto-context.json')), false);
    assert.deepEqual(JSON.parse(readFileSync(join(work, '.auto-context', 'settings.json'), 'utf8')).collections, ['x']);
  } finally {
    removeTemp(work);
  }
});

test('update core: --migrate-config no-op when settings exists', () => {
  const work = repoTemp('qmd-migrate-noop');
  try {
    mkdirSync(join(work, '.auto-context'), { recursive: true });
    writeFileSync(join(work, '.auto-context.json'), JSON.stringify({ collections: ['old'] }));
    writeFileSync(join(work, '.auto-context', 'settings.json'), JSON.stringify({ collections: ['new'] }));
    const out = execFileSync('bash', [join(process.cwd(), 'core', 'update.sh'), '--migrate-config', work], { encoding: 'utf8' });
    assert.ok(out.includes('settings_exists'), `expected settings_exists message, got: ${out}`);
    assert.equal(existsSync(join(work, '.auto-context.json')), true);
    assert.deepEqual(JSON.parse(readFileSync(join(work, '.auto-context', 'settings.json'), 'utf8')).collections, ['new']);
  } finally {
    removeTemp(work);
  }
});

test('update core: --migrate-config refuses symlinked .auto-context directory', () => {
  const work = repoTemp('qmd-migrate-symlink');
  const outside = repoTemp('qmd-migrate-outside');
  try {
    writeFileSync(join(work, '.auto-context.json'), JSON.stringify({ indexing: true, collections: ['x'] }));
    symlinkSync(outside, join(work, '.auto-context'), 'dir');

    const out = execFileSync('bash', [join(process.cwd(), 'core', 'update.sh'), '--migrate-config', work], { encoding: 'utf8' });

    assert.match(out, /unsafe_settings_dir/);
    assert.equal(existsSync(join(work, '.auto-context.json')), true);
    assert.equal(existsSync(join(outside, 'settings.json')), false);
  } finally {
    removeTemp(work);
    removeTemp(outside);
  }
});

test('update core: --init-wiki creates scaffold and enables wiki recall without dropping existing collections', () => {
  const work = repoTemp('qmd-init-wiki');
  try {
    mkdirSync(join(work, '.auto-context'), { recursive: true });
    writeFileSync(join(work, '.auto-context', 'settings.json'), JSON.stringify({
      indexing: true,
      collections: ['docs'],
    }));
    const out = execFileSync('bash', [join(process.cwd(), 'core', 'update.sh'), '--init-wiki', work], { encoding: 'utf8' });
    assert.match(out, /wiki scaffold/);
    assert.equal(existsSync(join(work, '.auto-context', 'wiki', 'SCHEMA.md')), true);
    assert.equal(existsSync(join(work, '.auto-context', 'wiki', 'decisions')), true);
    const cfg = JSON.parse(readFileSync(join(work, '.auto-context', 'settings.json'), 'utf8'));
    const wikiCollection = cfg.collections.find(c => c !== 'docs');
    assert.match(wikiCollection, /-wiki$/);
    assert.deepEqual(cfg.collections, ['docs', wikiCollection]);
    assert.deepEqual(cfg.collectionPaths, { [wikiCollection]: '.auto-context/wiki' });
    assert.equal(cfg.collectionRoles.docs, 'raw');
    assert.equal(cfg.collectionRoles[wikiCollection], 'wiki');
    // hierarchical은 기본값과 같아 emit되지 않는다(delta-only). 켜졌는지는 effective로 본다.
    assert.equal(cfg.recallStrategy, undefined);
    assert.equal(
      effectiveConfig(join(work, '.auto-context', 'settings.json')).recallStrategy,
      'hierarchical',
    );

    writeFileSync(join(work, '.auto-context', 'wiki', 'index.md'), '# custom\n');
    execFileSync('bash', [join(process.cwd(), 'core', 'update.sh'), '--init-wiki', work], { encoding: 'utf8' });
    assert.equal(readFileSync(join(work, '.auto-context', 'wiki', 'index.md'), 'utf8'), '# custom\n');
  } finally {
    removeTemp(work);
  }
});

// scaffold 디렉터리 목록(`update.sh`의 base_dirs)과 프롬프트가 모델에게 제시하는 타입
// 집합(`extractors/lib.ALLOWED_TYPES`)은 **같은 사실의 두 표현**이다 — 전자는 "여기에 카드가
// 쌓인다"는 약속이고 후자는 "쌓일 수 있는 카드"의 전부다. 갈리면 두 방향 다 나쁘다:
//   - scaffold가 넓으면 영영 빈 디렉터리가 남아 "왜 안 쌓이지"라는 오진을 부른다
//     (라이브 ai-proxy에 sessions/queries가 0건으로 남아 있던 상태 — 이 테스트가 막는 것).
//   - scaffold가 좁으면 새 타입을 프롬프트에 추가했을 때 그 자리가 눈에 띄지 않는다.
// 기대값을 리터럴로 적지 않고 **코드에서 유도**하는 이유가 이것이다. 리터럴이면 프롬프트에
// 타입을 추가할 때 이 테스트도 같이 고쳐야 해서 드리프트를 잡아 주지 못한다.
// 판정은 소스 텍스트가 아니라 **실제로 만들어진 디렉터리**로 한다(CLAUDE.md: 정규식 단정은
// 동작이 no-op이어도 통과한다 — backend_manager reload가 그렇게 새어 나갔다).
test('update core: --init-wiki scaffold는 프롬프트가 제시하는 타입의 디렉터리만 만든다', () => {
  const py = `import json, sys
sys.path.insert(0, "core")
import wiki_compile
from extractors import lib
print(json.dumps(sorted(wiki_compile.TYPE_DIRS[t] for t in lib.ALLOWED_TYPES)))`;
  const expected = JSON.parse(execFileSync('python3', ['-c', py], { encoding: 'utf8' }));

  const work = repoTemp('qmd-init-wiki-dirs');
  try {
    execFileSync('bash', [join(process.cwd(), 'core', 'update.sh'), '--init-wiki', work], { encoding: 'utf8' });
    const wiki = join(work, '.auto-context', 'wiki');
    const dirs = readdirSync(wiki, { withFileTypes: true })
      .filter(e => e.isDirectory()).map(e => e.name).sort();
    assert.deepEqual(dirs, expected,
      'scaffold 디렉터리는 extractors/lib.ALLOWED_TYPES를 TYPE_DIRS로 매핑한 집합과 정확히 같아야 한다');

    // 위 단정의 이면: 지운 것은 **미리 만드는 것**이지 타입 지원이 아니다. session 카드는
    // 수동 wiki-compile 경로로 여전히 쓸 수 있어야 하고 그때 디렉터리가 생겨야 한다
    // (wiki_compile의 `target.parent.mkdir(parents=True)`). 이 절반이 없으면 다음 사람이
    // "session은 이제 안 되는구나"로 읽고 TYPE_DIRS에서 지운다.
    assert.equal(dirs.includes('sessions'), false, 'sessions는 자동 compile이 채울 수 없어 미리 만들지 않는다');
    assert.equal(existsSync(join(wiki, 'index.md')), true);
    for (const name of expected) {
      assert.equal(existsSync(join(wiki, name)), true, `${name} 디렉터리가 있어야 한다`);
    }
  } finally {
    removeTemp(work);
  }
});

test('update core: --init-wiki without settings creates an opt-in wiki-only config', () => {
  const work = repoTemp('qmd-init-wiki-empty');
  try {
    execFileSync('bash', [join(process.cwd(), 'core', 'update.sh'), '--init-wiki', work], { encoding: 'utf8' });
    const cfg = JSON.parse(readFileSync(join(work, '.auto-context', 'settings.json'), 'utf8'));
    assert.equal(cfg.indexing, true);
    assert.equal(cfg.collections.length, 1);
    assert.match(cfg.collections[0], /-wiki$/);
    assert.deepEqual(cfg.collectionPaths, { [cfg.collections[0]]: '.auto-context/wiki' });
    assert.deepEqual(cfg.collectionRoles, { [cfg.collections[0]]: 'wiki' });
    // delta-only: 기본값과 같은 recallStrategy/wikiPath는 쓰지 않고 기본값에 맡긴다.
    assert.equal(cfg.recallStrategy, undefined);
    assert.equal(cfg.wikiPath, undefined);
    const eff = effectiveConfig(join(work, '.auto-context', 'settings.json'));
    assert.equal(eff.recallStrategy, 'hierarchical');
    assert.equal(eff.wikiPath, '.auto-context/wiki');
  } finally {
    removeTemp(work);
  }
});

// recallStrategy는 예전에 **대입**이라 기존 값을 강제로 덮었다. delta-only로 바꾸면서
// 그냥 안 쓰기만 하면 기존 "flat"이 살아남아 wiki 우선 recall이 켜지지 않는다 — 지워야
// "키 없음 → 기본값 hierarchical"로 예전과 같은 결과가 된다.
test('update core: --init-wiki는 기존 recallStrategy: flat을 hierarchical로 되돌린다', () => {
  const work = repoTemp('qmd-init-wiki-flat');
  try {
    mkdirSync(join(work, '.auto-context'), { recursive: true });
    writeFileSync(join(work, '.auto-context', 'settings.json'), JSON.stringify({
      indexing: true, collections: ['docs'], recallStrategy: 'flat',
    }));
    execFileSync('bash', [join(process.cwd(), 'core', 'update.sh'), '--init-wiki', work], { encoding: 'utf8' });
    const settings = join(work, '.auto-context', 'settings.json');
    assert.equal(JSON.parse(readFileSync(settings, 'utf8')).recallStrategy, undefined);
    assert.equal(effectiveConfig(settings).recallStrategy, 'hierarchical');
  } finally {
    removeTemp(work);
  }
});

// 반대로 wikiPath는 setdefault였으므로 사용자 커스텀 값을 파괴하면 안 된다.
test('update core: --init-wiki는 사용자 지정 wikiPath를 보존한다', () => {
  const work = repoTemp('qmd-init-wiki-custom-path');
  try {
    mkdirSync(join(work, '.auto-context'), { recursive: true });
    writeFileSync(join(work, '.auto-context', 'settings.json'), JSON.stringify({
      indexing: true, collections: ['docs'], wikiPath: 'notes/wiki',
    }));
    execFileSync('bash', [join(process.cwd(), 'core', 'update.sh'), '--init-wiki', work], { encoding: 'utf8' });
    const settings = join(work, '.auto-context', 'settings.json');
    assert.equal(JSON.parse(readFileSync(settings, 'utf8')).wikiPath, 'notes/wiki');
    assert.equal(effectiveConfig(settings).wikiPath, 'notes/wiki');
  } finally {
    removeTemp(work);
  }
});

test('update core: --init-wiki --preset novel creates novel dirs and compile defaults', () => {
  const work = repoTemp('qmd-init-wiki-novel');
  try {
    const out = execFileSync('bash', [join(process.cwd(), 'core', 'update.sh'), '--init-wiki', '--preset', 'novel', work], { encoding: 'utf8' });
    assert.match(out, /wiki scaffold/);
    for (const dir of ['characters', 'world', 'timeline', 'plot', 'style', 'discarded', 'sessions', 'decisions']) {
      assert.equal(existsSync(join(work, '.auto-context', 'wiki', dir)), true, `${dir} should exist`);
    }
    const cfg = JSON.parse(readFileSync(join(work, '.auto-context', 'settings.json'), 'utf8'));
    // 활성화 스위치는 mode 하나다(enabled/autoWrite는 스키마에서 제거됐다) — auto-wiki는
    // 예전 {enabled:true, mode:'auto-wiki', autoWrite:true}와 같은 상태다.
    assert.equal(cfg.compile.mode, 'auto-wiki');
    // post_tool_source가 없으면 자동 수집 트리거가 하나도 없어 세션 노트를 채워도
    // 카드가 생기지 않는다(post_session_summary는 수동 경로 라벨일 뿐이다).
    assert.ok(cfg.compile.triggers.includes('post_tool_source'));
    // delta-only: 기본값과 같은 값은 emit하지 않는다. 실제 적용값은 effective로 확인한다.
    assert.equal(cfg.compile.defaultStatus, undefined);
    const eff = effectiveConfig(join(work, '.auto-context', 'settings.json'));
    assert.equal(eff.compile.mode, 'auto-wiki');
    assert.equal(eff.compile.defaultStatus, 'generated');
    // 이 preset은 compile을 켜면서 extractor를 쓰지 않는다 — timeout 기본값이 30이던
    // 동안 이 경로로 온보딩한 프로젝트는 adapter 호출이 매번 timeout됐다(§6.1).
    assert.equal(eff.compile.extractor.timeout, 120);
  } finally {
    removeTemp(work);
  }
});

test('update core: --init-wiki preserves invalid existing settings.json', () => {
  const work = repoTemp('qmd-init-wiki-invalid');
  try {
    mkdirSync(join(work, '.auto-context'), { recursive: true });
    writeFileSync(join(work, '.auto-context', 'settings.json'), '{not json');

    assert.throws(() => execFileSync('bash', [join(process.cwd(), 'core', 'update.sh'), '--init-wiki', work], { encoding: 'utf8' }));

    assert.equal(readFileSync(join(work, '.auto-context', 'settings.json'), 'utf8'), '{not json');
    assert.equal(existsSync(join(work, '.auto-context', 'wiki')), false);
  } finally {
    removeTemp(work);
  }
});

test('update core: --init-wiki refuses symlinked .auto-context directory', () => {
  const work = repoTemp('qmd-init-wiki-symlink');
  const outside = repoTemp('qmd-init-wiki-outside');
  try {
    symlinkSync(outside, join(work, '.auto-context'), 'dir');

    assert.throws(() => execFileSync('bash', [join(process.cwd(), 'core', 'update.sh'), '--init-wiki', work], { encoding: 'utf8' }));

    assert.equal(existsSync(join(outside, 'settings.json')), false);
    assert.equal(existsSync(join(outside, 'wiki')), false);
  } finally {
    removeTemp(work);
    removeTemp(outside);
  }
});

test('update core: --init-wiki refuses symlinked wiki directory', () => {
  const work = repoTemp('qmd-init-wiki-dir-symlink');
  const outside = repoTemp('qmd-init-wiki-dir-outside');
  try {
    mkdirSync(join(work, '.auto-context'), { recursive: true });
    symlinkSync(outside, join(work, '.auto-context', 'wiki'), 'dir');

    assert.throws(() => execFileSync('bash', [join(process.cwd(), 'core', 'update.sh'), '--init-wiki', work], { encoding: 'utf8' }));

    assert.equal(existsSync(join(outside, 'SCHEMA.md')), false);
    assert.equal(existsSync(join(outside, 'decisions')), false);
    assert.equal(existsSync(join(work, '.auto-context', 'settings.json')), false);
  } finally {
    removeTemp(work);
    removeTemp(outside);
  }
});

test('update core: worker migrates .auto-context.json before loading config', () => {
  const work = repoTemp('qmd-worker-migrate');
  const bin = join(work, 'bin');
  const fakeHome = join(work, 'fakehome');
  prepareTestStore(fakeHome);
  const qmdLog = join(work, 'qmd.log');
  try {
    mkdirSync(bin, { recursive: true });
    mkdirSync(fakeHome, { recursive: true });
    writeFileSync(join(work, '.auto-context.json'), JSON.stringify({
      indexing: true,
      collections: ['x'],
    }));
    writeFileSync(join(bin, 'qmd'), [
      '#!/usr/bin/env sh',
      `echo "$@" >> "${qmdLog}"`,
      'exit 0',
    ].join('\n'), { mode: 0o755 });

    execFileSync('bash', [join(process.cwd(), 'core', 'update.sh'), '--worker', work], {
      encoding: 'utf8',
      env: { ...process.env, PATH: `${bin}:${process.env.PATH}`, HOME: fakeHome, QMD_CACHE_DIR: CACHE_DIR, QMD_LOCK_BASE: join(work, 'locks') },
    });

    assert.equal(existsSync(join(work, '.auto-context.json')), false);
    assert.deepEqual(JSON.parse(readFileSync(join(work, '.auto-context', 'settings.json'), 'utf8')).collections, ['x']);
  } finally {
    removeTemp(work);
  }
});

test('update core: QMD_SANDBOX=true → 무출력 exit 0', () => {
  const out = execFileSync('bash', ['core/update.sh'], {
    env: { ...process.env, QMD_SANDBOX: 'true' },
  });
  assert.equal(out.toString().trim(), '');
});

test('update core: --sandbox 인자 → 무출력 exit 0', () => {
  const out = execFileSync('bash', ['core/update.sh', '--sandbox']);
  assert.equal(out.toString().trim(), '');
});

// BUG-2 regression: collection add가 "already exists" + exit 1 반환해도 update/embed는 실행돼야 함
test('pending: SessionStart는 내부 명령 대신 자연어 선택과 setup 스킬을 안내', () => {
  // pending 폴더(config 없음)를 stdin으로 전달해 main() 경로의 pending 분기를 실행.
  // qmd, curl 등 외부 명령이 없어도 pending 분기는 메시지만 출력하고 종료하므로 PATH stub 불필요.
  const work = repoTemp('qmd-pending-msg');
  try {
    // pending 폴더: .auto-context.json 없음. qmd stub도 최소한만 — pending 분기에서 qmd 호출 안 함.
    const bin = join(work, 'bin');
    mkdirSync(bin, { recursive: true });
    // curl stub (healthcheck 억제)
    writeFileSync(join(bin, 'curl'), '#!/usr/bin/env sh\nexit 1\n', { mode: 0o755 });
    // qmd stub (혹시 qmd collection list 같은 게 호출되더라도 exit 0)
    writeFileSync(join(bin, 'qmd'), '#!/usr/bin/env sh\nexit 0\n', { mode: 0o755 });

    const out = execFileSync('bash', [join(process.cwd(), 'core', 'update.sh')], {
      encoding: 'utf8',
      input: JSON.stringify({ cwd: work }),
      env: { ...process.env, PATH: `${bin}:${process.env.PATH}`, QMD_CACHE_DIR: CACHE_DIR },
    });

    for (const choice of ['자연어', '추천 범위 확인', '켜기', '끄기', '2시간 미루기', 'setup 스킬']) {
      assert.ok(out.includes(choice), `${choice} 없음: ${out}`);
    }
    assert.doesNotMatch(out, /update\.sh|--(?:recommend|optin|optout|skip)\b|\.auto-context\/settings\.json/);
  } finally {
    removeTemp(work);
  }
});

test('update core: collection add already-exists exit 1도 update 실행 (BUG-2)', () => {
  const work = repoTemp('qmd-update-already-exists');
  const bin = join(work, 'bin');
  const fakeHome = join(work, 'fakehome');
  prepareTestStore(fakeHome);
  const qmdLog = join(work, 'qmd.log');
  const lockBase = join(work, 'locks');
  try {
    mkdirSync(join(work, '.agents'), { recursive: true });
    mkdirSync(bin, { recursive: true });
    mkdirSync(fakeHome, { recursive: true });
    // indexing: true + collections: ['x'] → resolve-only가 entry를 반환하도록
    writeFileSync(join(work, '.agents', 'qmd-recall.json'), JSON.stringify({
      indexing: true,
      collections: ['x'],
    }));
    // stub qmd: collection list/show → exit 0 (빈 출력); collection add → "already exists" + exit 1;
    // update/embed → exit 0, 로그 기록
    writeFileSync(join(bin, 'qmd'), [
      '#!/usr/bin/env sh',
      `log="${qmdLog}"`,
      'echo "$@" >> "$log"',
      'case "$1 $2" in',
      '  "collection list") exit 0 ;;',
      '  "collection show") exit 0 ;;',
      '  "collection add") echo "Collection \'x\' already exists" >&2; exit 1 ;;',
      '  *) exit 0 ;;',
      'esac',
    ].join('\n'), { mode: 0o755 });

    execFileSync('bash', [join(process.cwd(), 'core', 'update.sh'), '--worker', work], {
      encoding: 'utf8',
      env: {
        ...process.env,
        PATH: `${bin}:${process.env.PATH}`,
        HOME: fakeHome,           // normalize_qmd_path가 ~/.bun/bin 등을 PATH에 추가 못 하도록
        QMD_CACHE_DIR: CACHE_DIR,
        QMD_LOCK_BASE: lockBase,
      },
    });

    const log = readFileSync(qmdLog, 'utf8');
    assert.ok(log.includes('update'), `qmd update가 호출돼야 하는데 qmd.log 내용: ${log}`);
  } finally {
    removeTemp(work);
  }
});

test('update core: unmanaged QMD_BIN override cannot replace the selected wrapper', () => {
  const work = repoTemp('qmd-update-qmd-bin');
  const bin = join(work, 'bin');
  const fakeHome = join(work, 'fakehome');
  prepareTestStore(fakeHome);
  const qmdLog = join(work, 'qmd.log');
  const hookLog = join(work, 'hook.log');
  const lockBase = join(work, 'locks');
  const qmdBin = join(bin, 'qmd-custom');
  try {
    mkdirSync(join(work, '.agents'), { recursive: true });
    mkdirSync(bin, { recursive: true });
    mkdirSync(fakeHome, { recursive: true });
    writeFileSync(join(work, '.agents', 'qmd-recall.json'), JSON.stringify({
      indexing: true,
      collections: ['x'],
    }));
    writeFileSync(qmdBin, [
      '#!/usr/bin/env sh',
      `log="${qmdLog}"`,
      'echo "$@" >> "$log"',
      'case "$1 $2" in',
      '  "collection list") exit 0 ;;',
      '  "collection show") exit 0 ;;',
      '  "collection add") exit 0 ;;',
      '  *) exit 0 ;;',
      'esac',
    ].join('\n'), { mode: 0o755 });

    execFileSync('bash', [join(process.cwd(), 'core', 'update.sh'), '--worker', work], {
      encoding: 'utf8',
      env: {
        ...process.env,
        PATH: `/usr/bin:/bin`,
        HOME: fakeHome,
        QMD_BIN: qmdBin,
        QMD_CACHE_DIR: CACHE_DIR,
        QMD_LOCK_BASE: lockBase,
        QMD_HOOK_LOG: hookLog,
        QMD_SETUP_GUARD_FIXTURE: '',
      },
    });

    // Strict managed selection: the same HOME resolves its wrapper, the override is refused.
    const resolveBin = `import qmd_route
try:
    info = qmd_route.binary_info(json.loads(sys.argv[1]))
    print(json.dumps({"bin": info["QMD_BIN"], "managed": info["managed"]}))
except ValueError as error:
    print(json.dumps({"error": str(error)}))`;
    const managed = gateResult(fakeHome, resolveBin, [JSON.stringify({ HOME: fakeHome })]);
    assert.equal(managed.managed, true);
    assert.notEqual(managed.bin, qmdBin);
    assert.deepEqual(gateResult(fakeHome, resolveBin, [JSON.stringify({ HOME: fakeHome, QMD_BIN: qmdBin })]),
      { error: 'managed_qmd_not_selected' });
    // resolve_qmd_bin fails before run_update logs START.
    assert.doesNotMatch(readIfExists(hookLog), /START:/);
    assert.equal(existsSync(qmdLog), false, 'unmanaged binary must not be invoked');
  } finally {
    removeTemp(work);
  }
});

test('update core: dedup hint absent when dedup-needed.jsonl is empty/missing (regression guard)', () => {
  const work = repoTemp('qmd-dedup-hint-empty');
  const bin = join(work, 'bin');
  try {
    mkdirSync(join(work, '.auto-context'), { recursive: true });
    mkdirSync(bin, { recursive: true });
    writeFileSync(join(work, '.auto-context', 'settings.json'), JSON.stringify({
      indexing: true, collections: ['x'],
    }));
    writeFileSync(join(bin, 'curl'), '#!/usr/bin/env sh\nexit 1\n', { mode: 0o755 });
    writeFileSync(join(bin, 'qmd'), '#!/usr/bin/env sh\nexit 0\n', { mode: 0o755 });

    const out = execFileSync('bash', [join(process.cwd(), 'core', 'update.sh')], {
      encoding: 'utf8',
      input: JSON.stringify({ cwd: work }),
      env: { ...process.env, PATH: `${bin}:${process.env.PATH}`, QMD_CACHE_DIR: CACHE_DIR },
    });
    assert.doesNotMatch(out, /wiki-dedup-resolver/);
  } finally {
    removeTemp(work);
  }
});

test('update core: dedup hint fires with the exact workflow block when the queue is non-empty (including a stale entry from a past run)', () => {
  const work = repoTemp('qmd-dedup-hint-nonempty');
  const bin = join(work, 'bin');
  try {
    mkdirSync(join(work, '.auto-context', 'compile'), { recursive: true });
    mkdirSync(bin, { recursive: true });
    writeFileSync(join(work, '.auto-context', 'settings.json'), JSON.stringify({
      indexing: true, collections: ['x'],
    }));
    writeFileSync(
      join(work, '.auto-context', 'compile', 'dedup-needed.jsonl'),
      JSON.stringify({ pageA: 'entities/a.md', pageB: 'entities/b.md', score: 0.95 }) + '\n',
    );
    writeFileSync(join(bin, 'curl'), '#!/usr/bin/env sh\nexit 1\n', { mode: 0o755 });
    writeFileSync(join(bin, 'qmd'), '#!/usr/bin/env sh\nexit 0\n', { mode: 0o755 });

    const out = execFileSync('bash', [join(process.cwd(), 'core', 'update.sh')], {
      encoding: 'utf8',
      input: JSON.stringify({ cwd: work }),
      env: { ...process.env, PATH: `${bin}:${process.env.PATH}`, QMD_CACHE_DIR: CACHE_DIR },
    });
    assert.match(out, /wiki-dedup-resolver/);
    const agentBody = readFileSync('agents/wiki-dedup-resolver.md', 'utf8');
    const startMarker = '<!-- WORKFLOW:START -->';
    const endMarker = '<!-- WORKFLOW:END -->';
    const block = agentBody.slice(agentBody.indexOf(startMarker) + startMarker.length, agentBody.indexOf(endMarker)).trim();
    assert.ok(out.includes(block), 'hint stdout must contain the exact workflow block, byte-for-byte');
  } finally {
    removeTemp(work);
  }
});

test('update core: dedup hint does not shell out to qmd or curl (file test + text extraction only)', () => {
  const work = repoTemp('qmd-dedup-hint-no-daemon-call');
  const bin = join(work, 'bin');
  const qmdLog = join(work, 'qmd.log');
  try {
    mkdirSync(join(work, '.auto-context', 'compile'), { recursive: true });
    mkdirSync(bin, { recursive: true });
    writeFileSync(join(work, '.auto-context', 'settings.json'), JSON.stringify({
      indexing: true, collections: ['x'],
    }));
    writeFileSync(
      join(work, '.auto-context', 'compile', 'dedup-needed.jsonl'),
      JSON.stringify({ pageA: 'entities/a.md', pageB: 'entities/b.md', score: 0.95 }) + '\n',
    );
    // curl always fails (healthcheck suppressed); qmd logs any call it receives.
    writeFileSync(join(bin, 'curl'), '#!/usr/bin/env sh\nexit 1\n', { mode: 0o755 });
    writeFileSync(join(bin, 'qmd'), `#!/usr/bin/env sh\necho "$@" >> "${qmdLog}"\nexit 0\n`, { mode: 0o755 });

    execFileSync('bash', [join(process.cwd(), 'core', 'update.sh')], {
      encoding: 'utf8',
      input: JSON.stringify({ cwd: work }),
      env: { ...process.env, PATH: `${bin}:${process.env.PATH}`, QMD_CACHE_DIR: CACHE_DIR },
    });
    // main() legitimately calls qmd for other reasons (preflight, resolve-only) before
    // forking the worker, so we only assert the hint step itself adds no NEW qmd calls
    // beyond what the pre-existing pending/notice logic already makes. The dedup hint
    // logic must never invoke qmd/curl at all -- verified structurally in the next step.
    assert.equal(existsSync(qmdLog), false, 'this pending-style project makes no qmd calls before the dedup hint runs, so any call here would have come from the hint logic');
  } finally {
    removeTemp(work);
  }
});

test('update core: dedup queue surfaces a user-facing notice (count + skill trigger), TTL-suppressed on re-run while the model hint still fires every time', () => {
  const work = repoTemp('qmd-dedup-notice');
  const bin = join(work, 'bin');
  const fakeHome = join(work, 'home');
  prepareTestStore(fakeHome);
  try {
    mkdirSync(join(work, '.auto-context', 'compile'), { recursive: true });
    mkdirSync(bin, { recursive: true });
    mkdirSync(fakeHome, { recursive: true });
    writeFileSync(join(work, '.auto-context', 'settings.json'), JSON.stringify({
      indexing: true, collections: ['x'],
    }));
    writeFileSync(
      join(work, '.auto-context', 'compile', 'dedup-needed.jsonl'),
      JSON.stringify({ pageA: 'entities/a.md', pageB: 'entities/b.md', score: 0.95 }) + '\n' +
      JSON.stringify({ pageA: 'entities/c.md', pageB: 'entities/d.md', score: 0.91 }) + '\n',
    );
    writeFileSync(join(bin, 'curl'), '#!/usr/bin/env sh\nexit 1\n', { mode: 0o755 });
    writeFileSync(join(bin, 'qmd'), '#!/usr/bin/env sh\nexit 0\n', { mode: 0o755 });

    const env = { ...process.env, PATH: `${bin}:${process.env.PATH}`, HOME: fakeHome, QMD_CACHE_DIR: CACHE_DIR };
    const out = execFileSync('bash', [join(process.cwd(), 'core', 'update.sh')], {
      encoding: 'utf8', input: JSON.stringify({ cwd: work }), env,
    });
    // (a) user-facing notice: exact pending count + skill trigger
    assert.match(out, /wiki 중복 후보 2건 대기/);
    assert.match(out, /\/wiki-dedup/);
    // (b) model-facing spawn hint fires the same run
    assert.match(out, /wiki-dedup-resolver/);

    // Second run within TTL: notice suppressed, but the model hint still fires.
    const out2 = execFileSync('bash', [join(process.cwd(), 'core', 'update.sh')], {
      encoding: 'utf8', input: JSON.stringify({ cwd: work }), env,
    });
    assert.doesNotMatch(out2, /wiki 중복 후보/);
    assert.match(out2, /wiki-dedup-resolver/);
  } finally {
    removeTemp(work);
  }
});

test('update core: notice_once는 미래 mtime marker에 영구 침묵하지 않는다 (모든 종점의 유일한 채널)', () => {
  // notice_once가 조용해지면 orphan 회수 실패·unregister 실패·source_missing·invalid role
  // 등 **모든 가드의 종점**이 사용자에게 닿지 못한다. `now - mtime`을 그대로 쓰면 미래
  // mtime marker(시계 되돌림·백업 복원)에서 나이가 음수 → `-lt $ttl`이 영구 참 →
  // 그 키의 알림이 영구 억제된다(그리고 억제 분기는 marker를 다시 쓰지 않아 스스로
  // 풀리지도 않는다).
  const work = repoTemp('qmd-notice-future-mtime');
  const cacheDir = tempCacheDir('notice-future-mtime');
  const bin = join(work, 'bin');
  const fakeHome = join(work, 'home');
  prepareTestStore(fakeHome);
  try {
    mkdirSync(join(work, '.auto-context', 'compile'), { recursive: true });
    mkdirSync(bin, { recursive: true });
    mkdirSync(fakeHome, { recursive: true });
    writeFileSync(join(work, '.auto-context', 'settings.json'), JSON.stringify({
      indexing: true, collections: ['x'],
    }));
    writeFileSync(
      join(work, '.auto-context', 'compile', 'dedup-needed.jsonl'),
      JSON.stringify({ pageA: 'entities/a.md', pageB: 'entities/b.md', score: 0.95 }) + '\n',
    );
    writeFileSync(join(bin, 'curl'), '#!/usr/bin/env sh\nexit 1\n', { mode: 0o755 });
    writeFileSync(join(bin, 'qmd'), '#!/usr/bin/env sh\nexit 0\n', { mode: 0o755 });

    const env = { ...process.env, PATH: `${bin}:${process.env.PATH}`, HOME: fakeHome, QMD_CACHE_DIR: cacheDir };
    const run = () => execFileSync('bash', [join(process.cwd(), 'core', 'update.sh')], {
      encoding: 'utf8', input: JSON.stringify({ cwd: work }), env,
    });
    assert.match(run(), /wiki 중복 후보 1건 대기/);
    assert.doesNotMatch(run(), /wiki 중복 후보/, 'TTL 억제 자체는 유지되어야 한다');

    // marker의 mtime을 미래로 밀면 (가드가 없을 때) 그 키는 영구 침묵한다.
    const marker = readdirSync(cacheDir).find((name) => name.startsWith('notice-wiki-dedup-'));
    assert.ok(marker, 'notice marker가 생성되지 않았다');
    const future = Math.floor(Date.now() / 1000) + 365 * 24 * 3600;
    utimesSync(join(cacheDir, marker), future, future);
    assert.match(run(), /wiki 중복 후보 1건 대기/, '미래 mtime marker가 알림을 영구 억제했다');
  } finally {
    removeTemp(work);
  }
});

test('wiki-dedup skill exists and does NOT re-copy the resolver workflow (SSOT stays in the agent file)', () => {
  const skill = readFileSync('skills/wiki-dedup/SKILL.md', 'utf8');
  // Must reference the resolver agent (the workflow SSOT)...
  assert.match(skill, /wiki-dedup-resolver/);
  // ...but must never re-copy the workflow block itself.
  assert.doesNotMatch(skill, /<!-- WORKFLOW:START -->/);
  // frontmatter name must be the skill's own name
  assert.match(skill, /^name:\s*wiki-dedup\s*$/m);
});

test('update core: merge-needed remains a passive diagnostic with no review notice or resolver spawn', () => {
  const script = readFileSync(join(process.cwd(), 'core', 'update.sh'), 'utf8');
  assert.doesNotMatch(script, /wiki-review|wiki-review-resolver|review_agent_file/);
  assert.doesNotMatch(script, /merge_queue=.*merge-needed/,
    'SessionStart must not scan the collision ledger or turn it into a human queue');
});

test('update core: dedup scanner is wired inside the embed subshell, after embed and the conditional reload', () => {
  const script = readFileSync(join(process.cwd(), 'core', 'update.sh'), 'utf8');
  const embedCallIdx = script.indexOf('"$QMD_BIN_RESOLVED" embed');
  const reloadBlockEndIdx = script.indexOf("fi\n", script.indexOf('EMBED reload skipped'));
  const scannerCallIdx = script.indexOf('wiki_dedup_scan.py');
  const nohupBlockEndIdx = script.indexOf("' >/dev/null 2>&1 &");
  assert.ok(embedCallIdx !== -1, 'embed call not found');
  assert.ok(scannerCallIdx !== -1, 'wiki_dedup_scan.py call not found in update.sh');
  assert.ok(scannerCallIdx > embedCallIdx, 'scanner must be wired after the embed call');
  assert.ok(scannerCallIdx > reloadBlockEndIdx, 'scanner must be wired after the conditional reload block');
  assert.ok(scannerCallIdx < nohupBlockEndIdx, 'scanner must still be inside the nested nohup subshell, not after it');
});

test('update core: dedup scanner actually runs inside the embed subshell at runtime', () => {
  const work = repoTemp('qmd-dedup-scanner-runtime');
  const bin = join(work, 'bin');
  const fakeHome = join(work, 'fakehome');
  prepareTestStore(fakeHome);
  const dedupLog = join(work, 'dedup.log');
  try {
    mkdirSync(join(work, '.auto-context'), { recursive: true });
    mkdirSync(bin, { recursive: true });
    mkdirSync(fakeHome, { recursive: true });
    writeFileSync(join(work, '.auto-context', 'settings.json'), JSON.stringify({
      indexing: true, collections: ['x'],
    }));
    writeFileSync(join(bin, 'qmd'), [
      '#!/usr/bin/env sh',
      'case "$1" in',
      '  update) exit 0 ;;',
      '  embed) echo "embedded 0 chunks"; exit 0 ;;',
      '  *) exit 0 ;;',
      'esac',
    ].join('\n'), { mode: 0o755 });

    execFileSync('bash', [join(process.cwd(), 'core', 'update.sh'), '--worker', work], {
      encoding: 'utf8',
      env: {
        ...process.env,
        PATH: `${bin}:${process.env.PATH}`,
        HOME: fakeHome,
        QMD_CACHE_DIR: CACHE_DIR,
        QMD_LOCK_BASE: join(work, 'locks'),
        QMD_DEDUP_LOG: dedupLog,
        QMD_DEDUP_COOLDOWN_DIR: join(work, 'dedup-cooldown'),
        QMD_SYNC_STATE_DIR: join(work, 'sync-state'),
        // this test IS the fork's regression guard, so it must actually fork
        QMD_SKIP_BACKGROUND_EMBED: '',
        QMD_SKIP_BACKGROUND_WORKER: '',
      },
    });

    // The embed step (and the scanner after it) run in a detached background
    // subshell; poll for the scanner's own log line to appear. 상한·간격은
    // test/helpers/timing.mjs 가 SSOT다(3s 고정 + 폴당 프로세스 스폰이던 자리).
    const seen = waitUntil(() => existsSync(dedupLog));
    assert.equal(seen, true, 'wiki_dedup_scan.py did not log within the wait budget; embed subshell wiring likely broken');
  } finally {
    removeTemp(work);
  }
});

test('synthetic SessionStart kick releases its embed lock without deleting the successor lock', async () => {
  const work = repoTemp('qmd-synthetic-kick-lock');
  const bin = join(work, 'bin');
  const fakeHome = join(work, 'fakehome');
  prepareTestStore(fakeHome);
  const lock = join(work, 'embed.lock');
  const managerLog = join(work, 'manager.log');
  const hookLog = join(work, 'hook.log');
  try {
    mkdirSync(join(work, '.auto-context'), {recursive: true});
    mkdirSync(join(work, 'docs'), {recursive: true});
    mkdirSync(bin, {recursive: true});
    mkdirSync(fakeHome, {recursive: true});
    writeFileSync(join(work, '.auto-context/settings.json'), JSON.stringify({
      indexing: true, collections: ['x'], collectionPaths: {x: 'docs'},
    }));
    const python = execFileSync('which', ['python3'], {encoding: 'utf8'}).trim();
    writeFileSync(join(bin, 'python3'), [
      '#!/bin/sh',
      'case "$1" in',
      '  */context_learning_synthetic_reconcile.py) echo \'{"wikiCollectionsQueued":1}\'; exit 0 ;;',
      'esac',
      `exec "${python}" "$@"`,
    ].join('\n'), {mode: 0o755});
    writeFileSync(join(bin, 'qmd'), [
      '#!/bin/sh',
      'case "$1" in embed) echo "0 chunks" ;; esac',
      'exit 0',
    ].join('\n'), {mode: 0o755});
    const manager = join(work, 'manager.sh');
    writeFileSync(manager, [
      '#!/bin/sh',
      '[ "$1" = kick-index ] || exit 0',
      `[ ! -d "${lock}" ] || { echo busy > "${managerLog}"; exit 1; }`,
      `mkdir "${lock}" || exit 1`,
      `echo $$ > "${lock}/pid"`,
      `echo successor > "${managerLog}"`,
    ].join('\n'), {mode: 0o755});
    execFileSync('bash', [join(process.cwd(), 'core/update.sh'), '--worker', work], {
      encoding: 'utf8', env: {
        ...process.env, PATH: `${bin}:${process.env.PATH}`, HOME: fakeHome,
        QMD_CACHE_DIR: join(work, 'cache'), QMD_LOCK_BASE: join(work, 'locks'),
        QMD_HOOK_LOG: hookLog, QMD_EMBED_LOCKDIR: lock,
        QMD_BACKEND_MANAGER: manager, QMD_SETUP_GUARD_FIXTURE: '1',
        QMD_SKIP_BACKGROUND_EMBED: '', QMD_SKIP_BACKGROUND_WORKER: '',
        QMD_RECALL_LOG: '',
      },
    });
    assert.equal(await waitUntilAsync(() => existsSync(managerLog)), true);
    assert.equal(readFileSync(managerLog, 'utf8').trim(), 'successor');
    const match = readFileSync(hookLog, 'utf8').match(/EMBED: started in background \(pid=(\d+)\)/);
    assert.ok(match, 'detached child pid must be recorded');
    const finished = await waitUntilAsync(() => {
      try { process.kill(Number(match[1]), 0); return false; } catch { return true; }
    });
    assert.equal(finished, true, 'detached child did not exit');
    assert.equal(existsSync(join(lock, 'pid')), true,
      'old child EXIT trap removed its successor lock');
  } finally {
    rmSync(lock, {recursive: true, force: true});
    removeTemp(work);
  }
});

test('update.sh main: 빈/비JSON stdin에도 SessionStart hook은 exit 0 (set -e 아래 substitution 실패 방어)', () => {
  // 히스토리 버그: main()의 stdin 파싱 substitution이 set -e 아래 python3 crash로
  // 실패하면 SessionStart hook 전체가 exit 1 → 호스트 "SessionStart hook (failed)".
  // main()은 set +e로 동기 경로 전체를 fail-open해야 한다. pending temp dir을 cwd로
  // 줘 fork 없이 pending 안내 후 exit 0 하는 경로를 검증한다.
  const home = repoTemp('update-emptystdin-home');
  const proj = join(home, 'proj');
  mkdirSync(proj, { recursive: true });
  const env = {
    ...process.env,
    HOME: home,
    QMD_CACHE_DIR: CACHE_DIR,
    QMD_DIRTY_QUEUE: join(home, 'dirty-queue'),
    QMD_LOCK_BASE: join(home, 'locks'),
  };
  try {
    for (const input of ['', 'not json {{{', '{}']) {
      const res = spawnSync('bash', [join(process.cwd(), 'core', 'update.sh')], {
        input, cwd: proj, env, encoding: 'utf8',
      });
      assert.equal(res.status, 0, `input=${JSON.stringify(input)} → exit ${res.status} (stderr: ${res.stderr})`);
    }
  } finally {
    removeTemp(home);
  }
});

// ---------------------------------------------------------------------------
// run_update() 3단계 "Add collections" 회귀 (조용한 실패 = 성공 위장)
//
// 히스토리 버그: entries 추출 python 한 줄이 f-string 안 `\"` 이스케이프 때문에
// SyntaxError 였고 `2>/dev/null` 이 그 stderr 를 버려서, while 루프가 조용히 0회
// 돌았다. collections_ok 초기값 1 → `qmd update` 만 실행 → `END rc=0`.
// 즉 SessionStart 는 `qmd collection add` 를 한 번도 하지 않았는데 로그는 성공으로
// 보였다(index_worker.sh 가 대신 등록해 증상이 가려졌다).
//
// --worker 는 collection add / qmd update 를 동기로 수행하고 embed 만 nohup 백그라운드로
// 넘기므로, 아래 테스트는 폴링 없이 종료 직후 로그를 읽어도 결정적이다.
// ---------------------------------------------------------------------------

function writeQmdStub(path, body) {
  writeFileSync(path, ['#!/usr/bin/env sh', ...body].join('\n'), { mode: 0o755 });
}

function runWorker(work, extraEnv = {}) {
  const bin = join(work, 'bin');
  const fakeHome = join(work, 'fakehome');
  prepareTestStore(fakeHome);
  return execFileSync('bash', [join(process.cwd(), 'core', 'update.sh'), '--worker', work], {
    encoding: 'utf8',
    env: {
      ...process.env,
      PATH: `${bin}:${process.env.PATH}`,
      HOME: fakeHome,
      QMD_CACHE_DIR: CACHE_DIR,
      QMD_LOCK_BASE: join(work, 'locks'),
      QMD_HOOK_LOG: join(work, 'hook.log'),
      ...extraEnv,
    },
  });
}

test('update core: resolved entries마다 ADD COLLECTION 로그 + qmd collection add 실제 호출 (조용한 0회 루프 회귀)', () => {
  const work = repoTemp('qmd-update-addcol-logs');
  const bin = join(work, 'bin');
  const qmdLog = join(work, 'qmd.log');
  const hookLog = join(work, 'hook.log');
  try {
    mkdirSync(join(work, '.auto-context'), { recursive: true });
    mkdirSync(join(work, 'docs'), { recursive: true });
    mkdirSync(join(work, 'notes'), { recursive: true });
    mkdirSync(bin, { recursive: true });
    mkdirSync(join(work, 'fakehome'), { recursive: true });
    writeFileSync(join(work, '.auto-context', 'settings.json'), JSON.stringify({
      indexing: true,
      collections: ['docs', 'notes'],
      collectionPaths: { docs: 'docs', notes: 'notes' },
    }));
    writeQmdStub(join(bin, 'qmd'), [`echo "$@" >> "${qmdLog}"`, 'exit 0']);

    runWorker(work);

    const hook = readFileSync(hookLog, 'utf8');
    const qmd = readFileSync(qmdLog, 'utf8');

    // (1) 핵심 회귀 방지: 로그 부재가 곧 버그의 증상이었다.
    for (const name of ['docs', 'notes']) {
      assert.ok(
        hook.includes(`ADD COLLECTION: name=${name} path=${join(work, name)}`),
        `ADD COLLECTION 로그가 ${name} 에 없다 — entries 추출이 조용히 0건이 된 회귀:\n${hook}`,
      );
      // (2) 로그만이 아니라 qmd 가 실제 호출됐는지 인자까지 캡처해 확인.
      assert.ok(
        qmd.includes(`collection add ${join(work, name)} --name ${name}`),
        `qmd collection add 가 ${name} 에 대해 호출되지 않았다:\n${qmd}`,
      );
    }
    // entries 가 있었으므로 빈-entries 경고는 없어야 한다.
    assert.doesNotMatch(hook, /WARN: no collection entries resolved/);
    assert.match(hook, /END rc=0/);
  } finally {
    removeTemp(work);
  }
});

test('update core: entries가 비면 WARN 로그로 표면화 (조용한 실패 금지)', () => {
  const work = repoTemp('qmd-update-addcol-warn');
  const bin = join(work, 'bin');
  const qmdLog = join(work, 'qmd.log');
  const hookLog = join(work, 'hook.log');
  try {
    mkdirSync(join(work, '.auto-context'), { recursive: true });
    mkdirSync(bin, { recursive: true });
    mkdirSync(join(work, 'fakehome'), { recursive: true });
    // cwd 밖 경로는 resolve_paths 가 unsafe 로 skip 하고(entries=[]) prune 도 건드리지
    // 않으므로(unsafe 는 missing 판정에서 continue), refused 아닌 채 entries 만 빈
    // 상태를 만들 수 있다.
    writeFileSync(join(work, '.auto-context', 'settings.json'), JSON.stringify({
      indexing: true,
      collections: ['escape'],
      collectionPaths: { escape: '../outside-of-project' },
    }));
    writeQmdStub(join(bin, 'qmd'), [`echo "$@" >> "${qmdLog}"`, 'exit 0']);

    runWorker(work);

    const hook = readFileSync(hookLog, 'utf8');
    assert.match(hook, /WARN: no collection entries resolved/, `빈 entries가 경고 없이 지나갔다:\n${hook}`);
    assert.doesNotMatch(hook, /ADD COLLECTION/);
    assert.doesNotMatch(readFileSync(qmdLog, 'utf8'), /collection add/);
  } finally {
    removeTemp(work);
  }
});

test('update core: collection add 실패는 qmd update를 건너뛰고 END rc=1로 남는다 (rc=0 위장 금지)', () => {
  const work = repoTemp('qmd-update-addcol-fail');
  const bin = join(work, 'bin');
  const qmdLog = join(work, 'qmd.log');
  const hookLog = join(work, 'hook.log');
  try {
    mkdirSync(join(work, '.auto-context'), { recursive: true });
    mkdirSync(join(work, 'docs'), { recursive: true });
    mkdirSync(bin, { recursive: true });
    mkdirSync(join(work, 'fakehome'), { recursive: true });
    writeFileSync(join(work, '.auto-context', 'settings.json'), JSON.stringify({
      indexing: true,
      collections: ['docs'],
      collectionPaths: { docs: 'docs' },
    }));
    // "already exists" 를 포함하지 않는 진짜 실패여야 retry() 가 성공으로 삼지 않는다(BUG-2 대비).
    writeQmdStub(join(bin, 'qmd'), [
      `echo "$@" >> "${qmdLog}"`,
      'case "$1 $2" in',
      '  "collection add") echo "permission denied" >&2; exit 1 ;;',
      '  *) exit 0 ;;',
      'esac',
    ]);

    runWorker(work);

    const hook = readFileSync(hookLog, 'utf8');
    const qmd = readFileSync(qmdLog, 'utf8');
    assert.match(hook, /ADD COLLECTION: name=docs/);
    assert.doesNotMatch(qmd, /^update$/m, `collections_ok=0 인데 qmd update가 실행됐다:\n${qmd}`);
    assert.match(hook, /END rc=1/, `collection add 실패가 rc=1로 기록되지 않았다:\n${hook}`);
    assert.doesNotMatch(hook, /END rc=0/, 'collection add 실패가 END rc=0으로 위장됐다');
  } finally {
    removeTemp(work);
  }
});

test('update core: entries 추출 python 한 줄이 SyntaxError 없이 TSV를 내고, stderr를 /dev/null로 버리지 않는다', () => {
  // 원인 자체를 직접 단정한다: update.sh 안의 구현 문자열을 그대로 꺼내 실행하므로
  // f-string(또는 다른 quoting 사고)이 다시 들어오면 여기서 즉시 깨진다.
  const script = readFileSync(join(process.cwd(), 'core', 'update.sh'), 'utf8');
  // 키 이름은 role 도입(7단계)에서 entries → indexEntries 로 바뀌었다. 가드가 지키는
  // 성질(구문 오류 없이 TSV를 내고 stderr를 버리지 않는다)은 그대로이므로 키만 따라간다.
  const line = script.split('\n').find(l => l.includes('get("indexEntries", [])') && l.includes('python3 -c'));
  assert.ok(line, 'collection add용 entries 추출 python3 -c 한 줄을 update.sh에서 찾지 못했다');

  // 버그의 은폐 절반: stderr 를 버리면 SyntaxError 가 다시 조용해진다.
  assert.doesNotMatch(line, /2>\/dev\/null/, `entries 추출 stderr를 /dev/null로 버리면 안 된다: ${line}`);

  const open = line.indexOf("python3 -c '") + "python3 -c '".length;
  const code = line.slice(open, line.indexOf("'", open));
  assert.ok(code.includes('json.load'), `추출한 python 코드가 이상하다: ${code}`);

  // 실행: SyntaxError면 exit 1 → execFileSync throw.
  // 동시에 role 계약도 못박는다 — collection add 대상은 indexEntries 뿐이고, role
  // `source`만 담긴 entries/sourceEntries 는 여기로 새면 안 된다.
  const out = execFileSync('python3', ['-c', code], {
    encoding: 'utf8',
    input: JSON.stringify({
      entries: [{ name: 'a', path: 'docs' }, { name: 'b', path: 'notes' }, { name: 'src', path: 'archive' }],
      indexEntries: [{ name: 'a', path: 'docs' }, { name: 'b', path: 'notes' }],
      sourceEntries: [{ name: 'src', path: 'archive' }],
    }),
  });
  assert.equal(out, 'a\tdocs\nb\tnotes\n');

  // indexEntries 키가 없어도 조용히 빈 출력(기본값 경로).
  assert.equal(execFileSync('python3', ['-c', code], { encoding: 'utf8', input: '{}' }), '');
});

test('update core: discard ledger cursor resets when ledger shrinks (partial clear)', () => {
  const home = repoTemp('qmd-update-cursor-');
  const proj = join(home, 'proj');
  const compileDir = join(proj, '.auto-context', 'compile');
  mkdirSync(compileDir, { recursive: true });

  writeFileSync(join(proj, '.auto-context', 'settings.json'), JSON.stringify({
    indexing: true,
    collections: ['proj'],
    collectionPaths: { proj: 'docs' },
  }));

  const ledgerPath = join(compileDir, 'discard-ledger.jsonl');
  const cursorPath = join(compileDir, '.discard-ledger.cursor');

  // stub qmd 를 PATH 에 넣는다(이 파일의 다른 테스트 전부와 같은 패턴). 없으면 실제 qmd
  // 바이너리와 :8483 데몬에 의존해, 전체 스위트에서 데몬이 바빠지면 update가 알림 블록
  // **전에** 조기 반환해 커서가 전진하지 않는다 — 격리 실행만 통과하는 flaky가 된다(실측).
  const bin = join(home, 'bin');
  mkdirSync(bin, { recursive: true });
  writeFileSync(join(bin, 'qmd'), '#!/usr/bin/env sh\nexit 0\n', { mode: 0o755 });

  const env = {
    ...process.env,
    PATH: `${bin}:${process.env.PATH}`,
    HOME: home,
    QMD_CACHE_DIR: join(home, 'cache'),
    QMD_DIRTY_QUEUE: join(home, 'dirty-queue'),
    QMD_LOCK_BASE: join(home, 'locks'),
  };

  try {
    // 1. Write 2 lines to ledger and run update.sh
    writeFileSync(ledgerPath, '{"job":1}\n{"job":2}\n');
    spawnSync('bash', [join(process.cwd(), 'core', 'update.sh')], { cwd: proj, env });
    assert.equal(readFileSync(cursorPath, 'utf8').trim(), '2');

    // 2. Shrink ledger to 1 line (partial clear)
    writeFileSync(ledgerPath, '{"job":2}\n');
    spawnSync('bash', [join(process.cwd(), 'core', 'update.sh')], { cwd: proj, env });
    assert.equal(readFileSync(cursorPath, 'utf8').trim(), '1', 'cursor must reset to 1 when ledger shrinks');

    // 3. 축소는 알림을 **재무장**해야 한다 — 그 관측 가능한 형태가 marker 해제다.
    //    "재무장 후 실제 재알림"까지 한 테스트에서 세 번 연속 update.sh를 돌려 단정하지
    //    않는다: 실측 1/6만 통과하고(커서가 1에 머문다) 단계 사이에 지연을 주면 이번엔
    //    2단계 리셋이 깨진다(`222`). 이 하네스는 실제 :8483 데몬 응답 시간에 노출돼 있어
    //    3회 연속 호출의 관측 순서가 결정적이지 않다. marker 해제까지가 이 블록의 계약이고,
    //    "marker 없으면 발화"는 notice_once 자신의 동작으로 이 파일이 따로 검증한다.
    const marker = readdirSync(join(home, 'cache')).filter((n) => n.startsWith('notice-discard-ledger-'));
    assert.deepEqual(marker, [], 'shrink must clear the notice marker so the next growth notifies again');
  } finally {
    removeTemp(home);
  }
});

// ─────────────────────────────────────────────────────────────────────────────
// 손상 원장 격리의 종점: worker가 격리 → 상태 파일 → 다음 SessionStart가 notice_once.
// worker는 detached fork라 stdout이 사용자에게 닿지 않는다. 조용하면 유료 반복 호출과
// 빈 wiki recall이 원인 불명으로 남으므로, 이 알림 자체가 그 기능의 절반이다.
// ─────────────────────────────────────────────────────────────────────────────
test('SessionStart: 손상된 원문 갱신 원장을 격리하고 그 사실을 1회 알린다', () => {
  const work = repoTemp('qmd-pending-quarantine');
  const bin = join(work, 'bin');
  const fakeHome = join(work, 'home');
  prepareTestStore(fakeHome);
  try {
    mkdirSync(bin, { recursive: true });
    mkdirSync(fakeHome, { recursive: true });
    mkdirSync(join(work, '.auto-context', 'compile'), { recursive: true });
    mkdirSync(join(work, '.auto-context', 'wiki'), { recursive: true });
    mkdirSync(join(work, 'docs'), { recursive: true });
    writeFileSync(join(work, '.auto-context', 'settings.json'), JSON.stringify({
      indexing: true,
      collections: ['proj-docs', 'proj-wiki'],
      collectionPaths: { 'proj-docs': 'docs', 'proj-wiki': '.auto-context/wiki' },
      collectionRoles: { 'proj-docs': 'raw', 'proj-wiki': 'wiki' },
      wikiPath: '.auto-context/wiki',
    }));
    // short-write가 남기는 형태 그대로: 정상 1줄 + 개행 없는 잘린 줄.
    const ledger = join(work, '.auto-context', 'compile', 'source-refresh-pending.jsonl');
    writeFileSync(ledger, JSON.stringify({
      eventId: 'e'.repeat(32), ts: '2026-08-01T00:00:00Z', sourcePath: 'docs/a.md',
      state: 'pending_refresh', engine: 'claude',
    }) + '\n{"engine": "claude", "eventId": "aaa", "sourceP');
    writeFileSync(join(bin, 'curl'), '#!/usr/bin/env sh\nexit 1\n', { mode: 0o755 });
    writeFileSync(join(bin, 'qmd'), '#!/usr/bin/env sh\nexit 0\n', { mode: 0o755 });
    // 이 테스트는 **worker 가 실제로 돌아야** 한다 — 손상 원장 격리는 worker 단계에서
    // 일어난다. 파일 전역 QMD_SKIP_BACKGROUND_WORKER 를 여기서만 해제한다(위 fork
    // 검증 테스트가 embed 가드를 같은 방식으로 해제하는 것과 같은 이유).
    // **QMD_LOCK_BASE 를 반드시 격리한다.** 기본값은 `$TMPDIR/qmd-auto-context-locks-<user>`
    // 라는 **전역** 경로이고, worker 는 그 아래 writer 락을 잡지 못하면 `acquire_lock` 이
    // 즉시 실패해 "SKIP: qmd update already running" 으로 **exit 0** 한다 — 기다리지 않는다.
    // 즉 락을 다투면 격리가 아예 일어나지 않아 아래 waitUntil 이 예산을 태우고 터진다.
    // 다투는 상대는 같은 스위트의 다른 worker 와 **실제 세션의 SessionStart 훅**이므로
    // 부하가 걸릴 때만 실패했다(단독 실행은 늘 통과 → flake 로 보였다).
    // 전역 락을 살아 있는 pid 로 점유하면 이 테스트는 100% 재현 실패한다.
    // 대기 예산을 늘리는 것으로는 절대 고쳐지지 않는다(worker 가 포기하고 나간다).
    const env = { ...process.env, PATH: `${bin}:${process.env.PATH}`, HOME: fakeHome,
                  QMD_CACHE_DIR: CACHE_DIR, QMD_LOCK_BASE: join(work, 'locks'),
                  QMD_SKIP_BACKGROUND_WORKER: '' };
    // 절대 경로로 부른다 — 상대 경로면 update.sh 안의 `dirname "$0"` 파생 경로가
    // workdir 기준으로 풀려 config import가 죽고 sessionStart가 통째로 skip된다.
    const run = () => execFileSync('bash', [join(process.cwd(), 'core', 'update.sh')], {
      encoding: 'utf8', input: JSON.stringify({ cwd: work }), env,
    });
    const quarantined = () => readdirSync(join(work, '.auto-context', 'compile'))
      .filter((name) => name.startsWith('source-refresh-pending.corrupt-'));

    // 1회차: 동기 경로는 아직 알릴 것이 없고, detached worker가 격리한다.
    run();
    // worker는 비동기라 결과를 기다린다. 폴링 상한·간격은 test/helpers/timing.mjs 가
    // SSOT다 — 예전에는 `execFileSync('sleep', ['0.05']) × 100`(= 폴당 프로세스 스폰)
    // 이라 부하가 걸리면 폴링 자체가 대기 예산을 먹었고 이 단정이 0 !== 1 로 터졌다.
    waitUntil(() => quarantined().length > 0);
    assert.equal(quarantined().length, 1, 'worker가 손상 원장을 격리한다');
    assert.equal(existsSync(ledger), false, '원장은 부재로 남는다(다음 편집이 새로 만든다)');

    // 2회차: 동기 경로가 그 사실을 알린다.
    assert.match(run(), /격리했습니다 → source-refresh-pending\.corrupt-/, '격리를 표면화한다');

    // 3회차: 같은 사건을 반복하지 않는다(상태 파일을 소비했다).
    assert.doesNotMatch(run(), /격리했습니다/, '사건 1회당 알림 1회');
  } finally { removeTemp(work); }
});

// ── 알림 문구의 지시형 계약 ────────────────────────────────────────────────────
// 이 채널의 수신자는 사람과 모델 둘 다다(stdout이 모델 컨텍스트로 들어간다). 실측:
// 지시 형태를 취한 dedup 힌트는 모델이 사용자 요청 없이 자율 처리했고, 숫자만 보고한
// 알림은 전부 무시됐다 — 이 알림이 반복 표시되는 동안 `/wiki-source-repair`는 한 세션도
// 실행되지 않았다. 아래 세 테스트가 그 계약(지시형 / 자율 적용 금지 / 상태형 예외)을 고정한다.

test('update core: source-missing 알림은 지시형이고, 자율 적용은 금지하며, TTL 억제를 받는다', () => {
  const work = repoTemp('qmd-source-missing-notice');
  const bin = join(work, 'bin');
  const fakeHome = join(work, 'home');
  prepareTestStore(fakeHome);
  const cacheDir = tempCacheDir('source-missing-notice');
  try {
    mkdirSync(join(work, '.auto-context', 'compile'), { recursive: true });
    mkdirSync(bin, { recursive: true });
    mkdirSync(fakeHome, { recursive: true });
    writeFileSync(join(work, '.auto-context', 'settings.json'), JSON.stringify({
      indexing: true, collections: ['x'],
    }));
    // 대기 = 카드별 최신 행이 `detected`. 카드 파일은 없어도 되고(verified 집계만 0),
    // 판정은 Python(wiki_source_missing.pending_summary)이 SSOT다.
    writeFileSync(
      join(work, '.auto-context', 'compile', 'source-missing.jsonl'),
      JSON.stringify({
        targetPath: 'concepts/a.md', action: 'detected', status: 'generated',
        missingSources: ['docs/gone-07-20.md'], origin: 'scan', ts: '2026-08-21T00:00:00Z',
      }) + '\n',
    );
    writeFileSync(join(bin, 'curl'), '#!/usr/bin/env sh\nexit 1\n', { mode: 0o755 });
    writeFileSync(join(bin, 'qmd'), '#!/usr/bin/env sh\nexit 0\n', { mode: 0o755 });

    const env = { ...process.env, PATH: `${bin}:${process.env.PATH}`, HOME: fakeHome, QMD_CACHE_DIR: cacheDir };
    const run = () => execFileSync('bash', [join(process.cwd(), 'core', 'update.sh')], {
      encoding: 'utf8', input: JSON.stringify({ cwd: work }), env,
    });

    const out = run();
    // (a) 사람용: 정확한 대기 건수
    assert.match(out, /원문 소실 1건 대기/);
    // (b) 모델용: 수신자 전환 + 실행할 skill + 자율 처리 근거
    assert.match(out, /에이전트:/, '모델용 지시가 없다 (상태 보고만 하는 알림은 무시된다)');
    assert.match(out, /wiki-source-repair/, '실행할 skill 이름이 지시에 없다');
    assert.match(out, /사용자 요청과 독립적인 백그라운드 유지보수/);
    // dedup 힌트는 "지금 즉시"지만 이 알림은 아니다 — dedup은 subagent가 자율 처리해
    // 메인 턴을 쓰지 않고, 이 작업은 사용자 확인이 필수라 정의상 메인 턴에서 일어난다.
    // "지금 즉시"로 두면 세션 첫 턴이 사용자 질문 대신 유지보수로 소모된다.
    assert.match(out, /사용자의 현재 요청을 먼저 처리하고/,
      '실행 순서가 없어 세션 첫 턴을 가로챈다');
    // (c) 경계: 후보 선택은 사람 판단이다. 잘못 매칭하면 카드가 무관한 원문을 가리킨 채
    //     verify에서 삭제되고, 임의 dismiss는 같은 소실 집합의 다음 진짜 소실을 가린다.
    assert.match(out, /repoint·dismiss를 임의로 적용하지 말 것/,
      '자율 적용 금지가 지시에 없다');
    // (c') 배치 상한. 라이브 실측 278건 = 111KB — 상한 없는 "제시하라"는 그 한 번으로
    //      세션 예산을 태우고, 그러면 지시형으로 바꾼 목적이 그 자리에서 무너진다.
    assert.match(out, /최대 10건씩/, '배치 상한이 지시에 없다 (전량 제시는 예산을 태운다)');

    // (d) 사용자 확인형이라 조건이 저절로 꺼지지 않는다 → 지시까지 notice_once 안에 두고
    //     TTL 억제를 받는다(dedup의 모델 힌트는 반대로 매 run 무조건 나간다 — 큐가 비면
    //     조건이 스스로 꺼지므로 잔소리가 끝난다).
    const out2 = run();
    assert.doesNotMatch(out2, /원문 소실 1건 대기/, 'TTL 억제가 동작하지 않았다');
    assert.doesNotMatch(out2, /wiki-source-repair/,
      '지시가 notice_once 밖에 있어 매 세션 반복된다');
  } finally {
    removeTemp(work);
  }
});

test('update core: 알림은 절차 본문을 복제하지 않는다 (SSOT는 skill/agent 파일)', () => {
  // 절차를 알림 문구에 복제하면 (1) 매 세션 stdout에 전문이 실려 고정비가 되고
  // (2) skill 파일과 갈리는 순간 알림이 틀린 절차를 지시한다. dedup은 agent 파일에서
  // WORKFLOW 블록을 awk로 추출해 이 규칙을 지킨다 — 알림 문구는 이름만 가리킨다.
  const sh = readFileSync('core/update.sh', 'utf8');
  const skill = readFileSync('skills/wiki-source-repair/SKILL.md', 'utf8');
  assert.match(sh, /wiki-source-repair/, '지시가 skill을 가리키지 않는다');
  // 래퍼 호출 형태(절차의 본체)는 skill에만 있어야 한다.
  assert.match(skill, /wiki-source-repair\.sh/);
  assert.doesNotMatch(sh, /wiki-source-repair\.sh/,
    'update.sh가 repair 래퍼 호출 절차를 복제했다');
});

test('update core: wiki-ineligible은 상태형으로 남는다 (지시형 스윕 금지)', () => {
  // 조치할 **무료·안전** 경로가 없는 알림은 지시형으로 바꾸지 않는다. 일괄 재검증은
  // host CLI 호출이고 사용자 계정에 과금되므로, 어떤 지시 문구도 과금 유발로 읽힌다.
  // 판정은 실제 emit 문구가 아니라 소스의 그 한 줄이다 — 이 테스트가 막는 것은 라이브
  // 동작이 아니라 "전부 지시형으로" 스윕이고, 그 스윕은 소스에서 일어난다.
  const line = readFileSync('core/update.sh', 'utf8')
    .split('\n')
    .find((l) => l.includes('notice_once wiki-ineligible'));
  assert.ok(line, 'wiki-ineligible 알림이 사라졌다');
  assert.doesNotMatch(line, /에이전트:/,
    'wiki-ineligible을 지시형으로 바꿨다 — 일괄 재검증은 유료 호출이다');
  assert.match(line, /일괄 재검증은 유료 호출이라 하지 않습니다/,
    '유료라서 하지 않는다는 근거가 문구에서 빠졌다');
});

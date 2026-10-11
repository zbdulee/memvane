// 데몬 pid 추적(입양)·reload 직렬화 동작 테스트.
//
// 기존 reload 테스트는 전부 스크립트 **소스 텍스트 정규식**(test/wal-checkpoint-fix.test.mjs)
// 이거나 `QMD_BACKEND_MANAGER` stub이라, reload가 완전 no-op이어도 통과했다. 실측 사고가
// 정확히 그 형태였다: PID_FILE이 한 번 지워지면 살아 있는 데몬을 다시 추적할 코드가 없어
// reload가 로그 한 줄 없이 아무것도 하지 않았고(reload 전후 pid 동일), start_daemon은 이미
// 점유된 포트에 재스폰해 EADDRINUSE를 반복했다. 그래서 여기서는 **진짜 프로세스를 띄우고**
// 죽었는지/입양됐는지를 관측한다.
//
// 격리 규칙: 포트는 반드시 동적으로 확보한 free port를 쓰고 기본값 8483(사용자의 실제 데몬)은
// 어떤 경로로도 대상이 되면 안 된다. STATE_DIR/PID/LOG도 전부 임시 디렉터리로 오버라이드한다.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync, spawn, spawnSync } from 'node:child_process';
import { createServer } from 'node:net';
import { existsSync, mkdirSync, mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { removeTemp } from './helpers/temp.mjs';
import { waitUntilAsync } from './helpers/timing.mjs';

const MANAGER = resolve('core/backend_manager.sh');
const NODE = process.execPath;

function whichDir(bin) {
  try {
    return dirname(execFileSync('/usr/bin/which', [bin], { encoding: 'utf8' }).trim());
  } catch {
    return '';
  }
}
// health()/version_ok()가 python3를, discover가 lsof/pgrep을 쓴다. 제한 PATH에 필요한
// 것만 넣되 실제 위치를 찾아 붙인다(python3가 없으면 health가 항상 실패해 오진단이 된다).
const BASE_PATH = ['/usr/bin', '/bin', '/usr/sbin', '/sbin', whichDir('python3'), dirname(NODE)]
  .filter(Boolean)
  .join(':');

// **동기 sleep(Atomics.wait)을 쓰지 말 것.** 이벤트 루프를 막으면 node가 자식 프로세스를
// 수확(SIGCHLD)하지 못해 죽은 가짜 데몬이 좀비로 남고 `kill(pid, 0)`이 계속 성공한다
// (= "죽었는가" 단정이 영원히 거짓). 이 파일의 대기는 전부 비동기다.
const delay = (ms) => new Promise((r) => { setTimeout(r, ms); });

/** 실제로 비어 있는 포트를 확보한다. 8483(사용자 데몬)은 구조적으로 나올 수 없다. */
function freePort() {
  return new Promise((res, rej) => {
    const srv = createServer();
    srv.on('error', rej);
    srv.listen(0, '127.0.0.1', () => {
      const { port } = srv.address();
      srv.close(() => res(port));
    });
  });
}

function makeFakeQmd(home, version = '2.5.3') {
  const bin = join(home, '.bun', 'bin');
  mkdirSync(bin, { recursive: true });
  writeFileSync(join(bin, 'qmd'), `#!/usr/bin/env sh\necho qmd ${version}\n`, { mode: 0o755 });
}

/**
 * 가짜 데몬 스크립트. 경로에 `qmd`가 들어가고 argv가 `mcp --http --port <p>`라
 * `pid_is_daemon`의 cmdline 검증과 pgrep 폴백의 qmd 확인을 모두 통과한다.
 * `listen: false`면 포트를 잡지 않아 lsof에는 보이지 않는다(pgrep 경로 전용).
 */
function writeFakeDaemon(dir, { listen = true } = {}) {
  const file = join(dir, listen ? 'qmd_fake_daemon.cjs' : 'qmd_fake_idle.cjs');
  const body = listen
    ? `const http = require('node:http');
const port = Number(process.argv[process.argv.indexOf('--port') + 1]);
http.createServer((req, res) => { res.writeHead(200); res.end('ok'); }).listen(port, '127.0.0.1');
`
    : `setTimeout(() => {}, 120000);\n`;
  writeFileSync(file, body);
  return file;
}

const strays = new Set();
function spawnFakeDaemon(file, port) {
  const child = spawn(NODE, [file, 'mcp', '--http', '--port', String(port)], { stdio: 'ignore' });
  child.unref();
  strays.add(child.pid);
  return child;
}
function alive(pid) {
  if (!pid) return false;
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
}
/** 우리가 spawn한 자식은 종료 이벤트로 판정한다(좀비를 "살아 있음"으로 읽지 않기 위해). */
function childAlive(child) {
  return child.exitCode === null && child.signalCode === null;
}
function killQuiet(pid) {
  if (!pid) return;
  try { process.kill(pid, 'SIGKILL'); } catch { /* already gone */ }
  strays.delete(pid);
}
process.on('exit', () => { for (const pid of strays) { try { process.kill(pid, 'SIGKILL'); } catch { /* noop */ } } });

function healthy(port) {
  return spawnSync('/usr/bin/curl', ['-sf', '-m', '1', `http://localhost:${port}/health`]).status === 0;
}
// 상한·폴 간격은 test/helpers/timing.mjs 가 SSOT다(각자 100×50ms = 5s 고정이던 자리).
// `healthy()`는 폴마다 curl 을 스폰하므로 부하가 걸리면 폴링 자체가 대기 예산을 먹는다 —
// 스폰을 없앨 수는 없지만(그게 health 검사다) 상한은 넉넉해야 한다.
// 여기서 기다리는 것은 "일어나야 하는 일"이고 상한 초과는 호출부의 기능 단정이 잡는다.
async function waitHealthy(port) {
  return waitUntilAsync(() => healthy(port));
}
async function waitChildDead(child) {
  return waitUntilAsync(() => !childAlive(child));
}

function managerEnv(home, port, extra = {}) {
  assert.notEqual(String(port), '8483', 'test must never target the real daemon port');
  return {
    ...process.env,
    HOME: home,
    PATH: BASE_PATH,
    QMD_BACKEND_STATE_DIR: home,
    QMD_DAEMON_PORT: String(port),
    // These legacy adoption cases exercise the explicit fixture-only path.
    QMD_SETUP_GUARD_FIXTURE: '1',
    QMD_DAEMON_PID: join(home, 'daemon.pid'),
    QMD_BACKEND_LOG: join(home, 'manager.log'),
    QMD_DAEMON_LOG: join(home, 'daemon-out.log'),
    ...extra,
  };
}

function run(args, env) {
  return spawnSync('/bin/bash', [MANAGER, ...args], { encoding: 'utf8', env });
}

function managerLog(home) {
  try { return readFileSync(join(home, 'manager.log'), 'utf8'); } catch { return ''; }
}

test('default managed PID proof is isolated by port', async () => {
  const home = mkdtempSync(join(tmpdir(), 'qmd-port-pid-'));
  const firstPort = await freePort();
  let secondPort = await freePort();
  while (secondPort === firstPort) secondPort = await freePort();
  let first = null;
  let second = null;
  try {
    makeFakeQmd(home);
    const fake = writeFakeDaemon(home);
    first = spawnFakeDaemon(fake, firstPort);
    second = spawnFakeDaemon(fake, secondPort);
    assert.ok(await waitHealthy(firstPort));
    assert.ok(await waitHealthy(secondPort));
    writeFileSync(join(home, 'daemon.pid'), `${first.pid}\n`);
    const envFor = (port) => {
      const env = managerEnv(home, port, { QMD_SETUP_GUARD_FIXTURE: '1' });
      delete env.QMD_DAEMON_PID;
      return env;
    };
    assert.equal(run(['start'], envFor(firstPort)).status, 0);
    assert.equal(run(['start'], envFor(secondPort)).status, 0);
    assert.equal(readFileSync(join(home, `daemon-${firstPort}.pid`), 'utf8').trim(), String(first.pid));
    assert.equal(readFileSync(join(home, `daemon-${secondPort}.pid`), 'utf8').trim(), String(second.pid));
    assert.equal(readFileSync(join(home, 'daemon.pid'), 'utf8').trim(), String(first.pid),
      'other ports must never overwrite the legacy proof');
    assert.match(managerLog(home), /daemon PID proof migrated pid=/,
      'upgrade must preserve a verified legacy listener');
    assert.ok(childAlive(first) && childAlive(second), 'neither listener may be replaced');
  } finally {
    killQuiet(first && first.pid);
    killQuiet(second && second.pid);
    removeTemp(home);
  }
});

test('reload kills and restarts a live daemon even when the pid file is missing (adoption)', async () => {
  const home = mkdtempSync(join(tmpdir(), 'qmd-reload-adopt-'));
  const port = await freePort();
  let oldChild = null;
  let newPid = 0;
  try {
    makeFakeQmd(home);
    const fake = writeFakeDaemon(home);
    const starts = join(home, 'starts.log');
    const daemonScript = join(home, 'daemon.sh');
    writeFileSync(daemonScript,
      `#!/usr/bin/env bash\necho start >> "${starts}"\nexec "${NODE}" "${fake}" mcp --http --port "$QMD_DAEMON_PORT"\n`,
      { mode: 0o755 });

    oldChild = spawnFakeDaemon(fake, port);
    assert.ok(await waitHealthy(port), 'fake daemon should come up');
    const pidFile = join(home, 'daemon.pid');
    assert.equal(existsSync(pidFile), false, 'precondition: tracking is lost');

    const res = run(['reload'], managerEnv(home, port, {
      QMD_DAEMON_SCRIPT: daemonScript,
      QMD_DAEMON_READY_ATTEMPTS: '60',
      QMD_DAEMON_SHUTDOWN_ATTEMPTS: '40',
    }));
    assert.equal(res.status, 0);

    assert.ok(await waitChildDead(oldChild), 'reload must SIGTERM the daemon it discovered on the port');
    assert.equal(readFileSync(starts, 'utf8').trim().split('\n').filter(Boolean).length, 1,
      'reload must restart the daemon exactly once');
    assert.equal(existsSync(pidFile), true, 'restarted daemon must be tracked again');
    newPid = Number(readFileSync(pidFile, 'utf8').trim());
    assert.ok(newPid > 0 && newPid !== oldChild.pid && alive(newPid), 'pid file must name the new live daemon');
    assert.match(managerLog(home), /daemon adopted pid=/, 'adoption must be observable in the manager log');
  } finally {
    killQuiet(oldChild && oldChild.pid);
    killQuiet(newPid);
    removeTemp(home);
  }
});

test('a healthy daemon is adopted back into the pid file without respawning', async () => {
  const home = mkdtempSync(join(tmpdir(), 'qmd-adopt-'));
  const port = await freePort();
  let pid = 0;
  try {
    makeFakeQmd(home);
    const fake = writeFakeDaemon(home);
    const starts = join(home, 'starts.log');
    const daemonScript = join(home, 'daemon.sh');
    writeFileSync(daemonScript, `#!/usr/bin/env bash\necho start >> "${starts}"\nsleep 5\n`, { mode: 0o755 });

    pid = spawnFakeDaemon(fake, port).pid;
    assert.ok(await waitHealthy(port), 'fake daemon should come up');
    const pidFile = join(home, 'daemon.pid');

    const res = run(['start'], managerEnv(home, port, { QMD_DAEMON_SCRIPT: daemonScript }));
    assert.equal(res.status, 0);
    assert.equal(existsSync(starts), false, 'must not spawn a second daemon onto a live port (EADDRINUSE storm)');
    assert.equal(readFileSync(pidFile, 'utf8').trim(), String(pid), 'pid file must be restored by adoption');
  } finally {
    killQuiet(pid);
    removeTemp(home);
  }
});

test('concurrent reloads are serialized: they do not kill each other and restart once', async () => {
  const home = mkdtempSync(join(tmpdir(), 'qmd-reload-race-'));
  const port = await freePort();
  let oldChild = null;
  let newPid = 0;
  try {
    makeFakeQmd(home);
    const fake = writeFakeDaemon(home);
    const starts = join(home, 'starts.log');
    const daemonScript = join(home, 'daemon.sh');
    // sleep 1: 승자가 wait_health로 락을 붙잡고 있는 창을 넓혀 경합을 결정적으로 만든다.
    writeFileSync(daemonScript,
      `#!/usr/bin/env bash\necho start >> "${starts}"\nsleep 1\nexec "${NODE}" "${fake}" mcp --http --port "$QMD_DAEMON_PORT"\n`,
      { mode: 0o755 });

    oldChild = spawnFakeDaemon(fake, port);
    assert.ok(await waitHealthy(port), 'fake daemon should come up');

    const res = spawnSync('/bin/bash', ['-c', `"$M" reload & "$M" reload & wait`], {
      encoding: 'utf8',
      env: managerEnv(home, port, {
        M: MANAGER,
        QMD_DAEMON_SCRIPT: daemonScript,
        QMD_DAEMON_READY_ATTEMPTS: '60',
        QMD_DAEMON_SHUTDOWN_ATTEMPTS: '40',
      }),
    });
    assert.equal(res.status, 0);

    const startCount = existsSync(starts)
      ? readFileSync(starts, 'utf8').trim().split('\n').filter(Boolean).length
      : 0;
    assert.equal(startCount, 1, 'the loser must skip instead of killing the winner\'s fresh daemon');
    assert.match(managerLog(home), /reload skipped: lock busy/, 'the loser must say why it did nothing');
    newPid = Number(readFileSync(join(home, 'daemon.pid'), 'utf8').trim());
    assert.ok(alive(newPid), 'the surviving daemon must still be tracked and alive');
    assert.ok(await waitChildDead(oldChild), 'the winner must have replaced the original daemon');
    assert.ok(healthy(port), 'the port must still be served after two concurrent reloads');
  } finally {
    killQuiet(oldChild && oldChild.pid);
    killQuiet(newPid);
    removeTemp(home);
  }
});

test('a failing SIGTERM aborts immediately instead of blocking on the 30s shutdown wait', async () => {
  const home = mkdtempSync(join(tmpdir(), 'qmd-term-fail-'));
  const port = await freePort();
  let pid = 0;
  try {
    makeFakeQmd(home);
    const fake = writeFakeDaemon(home);
    const child = spawnFakeDaemon(fake, port);
    pid = child.pid;
    assert.ok(await waitHealthy(port), 'fake daemon should come up');
    writeFileSync(join(home, 'daemon.pid'), `${pid}\n`);

    // EPERM(다른 사용자 소유 프로세스)은 테스트에서 만들 수 없으므로 `kill -TERM`만 실패하는
    // shim을 깔고 매니저를 source한다. `bash -c '<script>' <path>`가 $0를 매니저 경로로
    // 고정하므로 매니저 안의 ROOT 계산이 그대로 성립한다.
    const script = `
kill() { case "$1" in -TERM) return 1 ;; *) builtin kill "$@" ;; esac; }
set -- health
. "$0"
reload
`;
    const res = spawnSync('/bin/bash', ['-c', script, MANAGER], {
      encoding: 'utf8',
      // 기본 60×0.5s = 30초. 버그가 남아 있으면 여기서 그대로 걸린다.
      env: managerEnv(home, port, { QMD_DAEMON_SHUTDOWN_ATTEMPTS: '60' }),
      // 이 timeout 이 "30초 대기에 걸렸는가"의 판정자다. 정상 경로는 실측 ~230ms 라
      // 25s 는 100배 여유이고, 버그 경로는 확정 30초라 반드시 걸린다.
      timeout: 25000,
    });

    // 대기에 걸리면 하네스가 자식을 죽여 signal 이 남는다. 이것이 벽시계 측정을
    // 대체하는 상태 단정이고, 예전 `elapsed < 10000` 보다 오히려 강하다 —
    // 그 판이 남긴 구멍을 같이 막는다: 대기에 걸려 timeout 으로 죽으면
    // `res.status` 는 null 이 되고 아래 `notEqual(res.status, 0)` 이 공허하게
    // 통과했다. 타이밍 정책은 test/helpers/timing.mjs 참고.
    assert.equal(res.signal, null, 'reload must not be killed by the harness timeout (30s wait regression)');
    // 반환 계약: "데몬을 재시작했는가". 신호가 가지 않았으므로 재시작이 없었고,
    // logrotate.sh:25가 이 non-zero를 보고 회전을 원복해야 한다(항상 0이면 그 원복이
    // 죽은 코드가 되어 $LOG 없이 남고 이후 회전이 전부 조기 종료된다).
    assert.notEqual(res.status, 0, 'reload must report that it did not restart the daemon');
    assert.match(managerLog(home), /daemon SIGTERM failed pid=/, 'the failure must be logged, not swallowed');
    assert.ok(childAlive(child), 'the daemon that could not be signalled must be left alone');
  } finally {
    killQuiet(pid);
    removeTemp(home);
  }
});

test('reload only clears the pid file when it still names the pid it killed', async () => {
  const home = mkdtempSync(join(tmpdir(), 'qmd-pidfile-keep-'));
  const port = await freePort();
  let pid = 0;
  try {
    makeFakeQmd(home);
    const fake = writeFakeDaemon(home);
    const child = spawnFakeDaemon(fake, port);
    pid = child.pid;
    assert.ok(await waitHealthy(port), 'fake daemon should come up');
    const pidFile = join(home, 'daemon.pid');
    writeFileSync(pidFile, `${pid}\n`);

    // 종료 대기 도중 다른 경로(동시 start/reload)가 새 pid를 써 넣은 상황을 재현한다.
    // 무조건 `rm -f "$PID_FILE"`이면 그 정상 pid까지 지워져 추적을 다시 잃는다.
    const script = `
set -- health
. "$0"
wait_pid_exit() { echo 999999 > "$PID_FILE"; return 0; }
start_daemon() { return 0; }
wait_health() { return 0; }
reload
`;
    const res = spawnSync('/bin/bash', ['-c', script, MANAGER], {
      encoding: 'utf8',
      env: managerEnv(home, port),
      timeout: 20000,
    });
    assert.equal(res.status, 0);
    assert.equal(existsSync(pidFile), true, 'a pid written by someone else must not be deleted');
    assert.equal(readFileSync(pidFile, 'utf8').trim(), '999999');
  } finally {
    killQuiet(pid);
    removeTemp(home);
  }
});

test('port matching is exact: PORT=5123 does not claim a --port 51234 process', async () => {
  const home = mkdtempSync(join(tmpdir(), 'qmd-port-prefix-'));
  let pid = 0;
  try {
    makeFakeQmd(home);
    const idle = writeFakeDaemon(home, { listen: false });
    const child = spawnFakeDaemon(idle, 51234);
    pid = child.pid;
    await delay(200);
    assert.ok(childAlive(child), 'idle stand-in should be running');
    const pidFile = join(home, 'daemon.pid');
    writeFileSync(pidFile, `${pid}\n`);

    const noop = join(home, 'daemon.sh');
    writeFileSync(noop, '#!/usr/bin/env bash\nexit 0\n', { mode: 0o755 });
    const res = run(['reload'], managerEnv(home, 5123, {
      QMD_DAEMON_PID: pidFile,
      QMD_DAEMON_SCRIPT: noop,
      QMD_DAEMON_READY_ATTEMPTS: '1',
    }));
    // 스텁 daemon.sh가 아무것도 띄우지 않으므로 wait_health가 실패한다 = "재시작하지 못했다".
    // 새 반환 계약상 non-zero가 맞다(핵심 단정은 아래 두 줄 — 남의 포트 프로세스 불가침).
    assert.notEqual(res.status, 0, 'no healthy daemon after reload must be reported');

    await delay(200);
    assert.ok(childAlive(child), 'prefix match must not send SIGTERM to a different port\'s process');
    assert.match(managerLog(home), /ignore stale\/non-qmd daemon pid=/, 'the pid must be rejected as not ours');
  } finally {
    killQuiet(pid);
    removeTemp(home);
  }
});

test('rotate hands QMD_BACKEND_MANAGER to logrotate so its reload branch is reachable', async () => {
  const home = mkdtempSync(join(tmpdir(), 'qmd-rotate-env-'));
  const port = await freePort();
  try {
    const seen = join(home, 'seen.txt');
    const stub = join(home, 'logrotate.sh');
    writeFileSync(stub,
      `#!/usr/bin/env bash\nprintf 'manager=%s pid=%s log=%s\\n' "\${QMD_BACKEND_MANAGER:-}" "\${QMD_DAEMON_PID:-}" "\${QMD_DAEMON_LOG:-}" > "${seen}"\n`,
      { mode: 0o755 });
    const env = managerEnv(home, port, { QMD_LOGROTATE_SCRIPT: stub });
    delete env.QMD_BACKEND_MANAGER; // 상속이 아니라 rotate가 직접 넘기는지를 본다
    const res = run(['rotate'], env);
    assert.equal(res.status, 0);
    const out = readFileSync(seen, 'utf8');
    assert.match(out, /manager=\S*core\/backend_manager\.sh/,
      'logrotate.sh의 첫 branch는 QMD_BACKEND_MANAGER 없이는 구조적으로 도달 불가');
    assert.match(out, /pid=\S+ log=\S+/, 'pid-file/log overrides must still be passed');
  } finally {
    removeTemp(home);
  }
});

// 샌드박스 자식(core/extractors/lib.py가 wiki 카드용 host CLI를 띄울 때 QMD_SANDBOX=1을
// 넣는다 — 유일한 setter)이 매니저에 도달해도 데몬을 건드리면 안 된다. 지금은 호출부
// 4갈래가 각자 막고 있지만 가드가 흩어져 있으면 다음 경로가 빠뜨린다. reload가 더 이상
// no-op이 아니므로(이 파일의 다른 테스트) 잘못 도달하면 실제로 데몬이 죽는다.
for (const flag of ['QMD_SANDBOX', 'GEMINI_SANDBOX']) {
  test(`${flag} stops the manager before it can touch the daemon`, async () => {
    const home = mkdtempSync(join(tmpdir(), 'qmd-sandbox-'));
    const port = await freePort();
    let pid = 0;
    try {
      makeFakeQmd(home);
      const fake = writeFakeDaemon(home);
      const child = spawnFakeDaemon(fake, port);
      pid = child.pid;
      assert.ok(await waitHealthy(port), 'fake daemon should come up');
      writeFileSync(join(home, 'daemon.pid'), `${pid}\n`);

      const res = run(['reload'], managerEnv(home, port, { [flag]: '1' }));
      assert.equal(res.status, 0, 'the guard must exit quietly, not error');
      assert.equal(res.stdout, '', 'sandbox must produce no output');

      await delay(300);
      assert.ok(childAlive(child), `${flag} must not let reload kill the daemon`);
      assert.equal(managerLog(home), '', 'a guarded run must not even log');
    } finally {
      killQuiet(pid);
      removeTemp(home);
    }
  });
}

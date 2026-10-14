import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync, spawnSync } from 'node:child_process';
import { mkdtempSync, writeFileSync, readFileSync, existsSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

test('backend 스크립트에 사용자 하드코딩 없음', () => {
  for (const f of ['daemon.sh', 'keepalive.sh', 'logrotate.sh', 'index_worker.sh']) {
    const sh = readFileSync(`backend/${f}`, 'utf8');
    assert.ok(!/\/Users\/[a-z]/.test(sh), `${f}: /Users/<user> 홈 경로 하드코딩 잔존`);
  }
});

test('keepalive 기본값은 전역 vec warm ping을 보내지 않는 health-only 모드다', () => {
  const sh = readFileSync('backend/keepalive.sh', 'utf8');
  assert.match(sh, /QMD_KEEPALIVE_VEC_WARM:-0/, 'vec warm ping은 기본 off 여야 함');
  assert.match(sh, /\[ "\$\{QMD_KEEPALIVE_VEC_WARM:-0\}" = "1" \] \|\| exit 0/, 'opt-in 전에는 /query 전에 종료해야 함');
  assert.match(sh, /\/query/, '명시 opt-in 용 warm ping 경로는 유지');
});

test('backend manager health timeout invalid 값은 2초 fallback', () => {
  const d = mkdtempSync(join(tmpdir(), 'qmd-manager-health-'));
  const bin = join(d, 'bin');
  const log = join(d, 'curl.log');
  try {
    execFileSync('mkdir', ['-p', bin]);
    writeFileSync(join(bin, 'curl'), `#!/usr/bin/env sh\necho "$@" >> "${log}"\nexit 1\n`, { mode: 0o755 });
    execFileSync('bash', ['core/backend_manager.sh', 'health'], {
      encoding: 'utf8',
      env: {
        ...process.env,
        PATH: `${bin}:${process.env.PATH}`,
        QMD_HEALTH_TIMEOUT: 'invalid',
        QMD_DAEMON_PORT: '59999',
        QMD_BACKEND_STATE_DIR: join(d, 'state'),
        QMD_BACKEND_LOG: join(d, 'manager.log'),
        QMD_DAEMON_LOG: join(d, 'daemon.log'),
      },
    });
    assert.match(readFileSync(log, 'utf8'), / -m 2 /);
  } finally {
    execFileSync('rm', ['-rf', d]);
  }
});

// BUG-4: index_worker 큐 락은 flock(1) 명령에 의존하면 안 된다(macOS 부재).
// core/dirty_queue.py와 동일한 python fcntl.flock으로 직렬화해야 한다.
test('BUG-4: index_worker 큐 스냅샷이 flock(1) 명령에 의존하지 않고 python fcntl을 쓴다', () => {
  const sh = readFileSync('backend/index_worker.sh', 'utf8');
  // flock(1) 셸 명령 호출 형태(`flock 9`, `flock -u 9`)가 없어야 한다. python fcntl.flock은 허용.
  assert.ok(!/(^|[;&|\s])flock\s+(-[a-z]\s+)?[0-9]/m.test(sh), 'index_worker.sh가 flock(1) 명령을 사용 — macOS에서 무동작');
  assert.match(sh, /dirty_queue_claim\.py/, 'index_worker.sh는 공통 fcntl 기반 claim/ack 경로를 사용해야 함');
  assert.match(readFileSync('core/dirty_queue_claim.py', 'utf8'), /fcntl\.flock/, 'claim/ack는 고정 큐 락을 잡아야 함');
});

// BUG-5(b)/락 통일: update.sh와 index_worker.sh의 공유 락 기본값(WRITER/EMBED)이 동일해야
// 두 프로세스가 직렬화된다. 한쪽 기본값만 바뀌면 직렬화가 깨진다.
test('락 통일: update.sh와 index_worker.sh의 WRITER/EMBED 락 기본 경로가 동일하다', () => {
  // 두 스크립트의 LOCK_BASE 계산식이 동일해야 하므로 같은 env에서 실제 경로를 추출해 비교한다.
  const env = { ...process.env, QMD_SANDBOX: '', HOME: process.env.HOME };
  const probe = (file, vars) =>
    execFileSync('bash', ['-c',
      // 스크립트의 변수 정의부만 source 하지 않고, 동일 계산식을 재현해 비교하기보다
      // 실제 스크립트를 dry 실행해 echo 하기 위해 변수 정의 라인을 평가한다.
      `set -u
       _QMD_UID="$(/usr/bin/id -un 2>/dev/null || id -u 2>/dev/null || echo qmd)"
       _QMD_LOCK_BASE="\${QMD_LOCK_BASE:-\${TMPDIR:-/tmp}/qmd-auto-context-locks-\${_QMD_UID}}"
       echo "$_QMD_LOCK_BASE/qmd-update.lock.d|$_QMD_LOCK_BASE/qmd-embed.lock.d"`],
      { encoding: 'utf8', env }).trim();
  const expected = probe();
  const upd = readFileSync('core/update.sh', 'utf8');
  const wk = readFileSync('backend/index_worker.sh', 'utf8');
  // 두 파일 모두 동일한 _QMD_LOCK_BASE 산식과 동일한 락 파일명을 사용해야 한다.
  for (const [name, src] of [['update.sh', upd], ['index_worker.sh', wk]]) {
    assert.match(src, /QMD_LOCK_BASE:-\$\{TMPDIR:-\/tmp\}\/qmd-auto-context-locks-\$\{_QMD_UID\}/, `${name}: LOCK_BASE 산식 불일치`);
    assert.match(src, /QMD_WRITER_LOCKDIR:-\$_QMD_LOCK_BASE\/qmd-update\.lock\.d/, `${name}: WRITER 락 기본값 불일치`);
    assert.match(src, /QMD_EMBED_LOCKDIR:-\$_QMD_LOCK_BASE\/qmd-embed\.lock\.d/, `${name}: EMBED 락 기본값 불일치`);
  }
  assert.ok(expected.includes('qmd-update.lock.d'));
});

// BUG-5(b): update.sh가 predictable /tmp 공유 경로(로그/status)를 기본값으로 쓰지 않는다.
test('BUG-5: update.sh 로그/status 기본값이 /tmp 고정 경로가 아니다', () => {
  const upd = readFileSync('core/update.sh', 'utf8');
  assert.ok(!/LOG="\/tmp\/qmd-hook\.log"/.test(upd), 'update.sh LOG가 /tmp 고정');
  assert.ok(!/STATUS="\/tmp\/qmd-update-status\.txt"/.test(upd), 'update.sh STATUS가 /tmp 고정');
  assert.match(upd, /QMD_HOOK_LOG:-\$_QMD_CACHE_DIR\/hook\.log/);
  assert.match(upd, /QMD_UPDATE_STATUS/);
  assert.match(upd, /update-status-\{digest\}\.txt/);
});

// BUG-6: stale cleanup must use the proved-owner helper, never recursive deletion.
test('BUG-6: update.sh stale embed lock 정리가 rm -rf 대신 rmdir를 쓴다', () => {
  const upd = readFileSync('core/update.sh', 'utf8');
  // EMBED_LOCK stale 분기에서 rm -rf "$EMBED_LOCK"가 없어야 한다.
  assert.ok(!/rm -rf "\$EMBED_LOCK"/.test(upd), 'update.sh가 EMBED_LOCK을 rm -rf로 재귀 삭제');
  assert.match(upd, /python3 "\$_QMD_CORE_DIR\/worker_lock\.py" reclaim "\$EMBED_LOCK"/, 'EMBED_LOCK cleanup must delegate owner/inode checks');
});

// BUG-6 동작: 예상 밖 내용이 든 lock dir은 rmdir 실패로 보호되어 삭제되지 않는다.
test('BUG-6: unsafe lock contents retain both directory and PID marker', () => {
  const d = mkdtempSync(join(tmpdir(), 'qmd-rmdir-'));
  try {
    const lock = join(d, 'el.d');
    execFileSync('mkdir', [lock]);
    // dead pid (절대 살아있지 않은 큰 pid) → stale 판정
    writeFileSync(join(lock, 'pid'), '2147483646');
    // 예상 밖 내용 추가
    writeFileSync(join(lock, 'unexpected'), 'do-not-delete');
    const result=spawnSync('python3',['core/worker_lock.py','reclaim',lock],{encoding:'utf8',env:{...process.env,QMD_RECALL_LOG:''}});
    assert.equal(result.status,1,result.stderr);
    assert.deepEqual(JSON.parse(result.stdout),{status:'unsafe',reason:'unsafe_worker_lock_contents'});
    assert.equal(readFileSync(join(lock,'pid'),'utf8'),'2147483646','unknown contents must retain the PID marker too');
    // 예상 밖 파일이 있으므로 rmdir 실패 → dir 보존
    assert.equal(existsSync(lock), true, 'rmdir 보호로 lock dir이 보존돼야 함');
    assert.equal(existsSync(join(lock, 'unexpected')), true, '예상 밖 파일은 삭제되지 않아야 함');
  } finally {
    execFileSync('rm', ['-rf', d]);
  }
});

// BUG-5(a): logrotate가 cross-platform 파일 크기 측정 후 회전한다.
// macOS에선 stat -f%z, Linux에선 stat -c%s/wc -c 폴백으로 동일 동작.
test('BUG-5: logrotate가 MAX 초과 로그를 회전한다(cross-platform 크기 측정)', () => {
  const d = mkdtempSync(join(tmpdir(), 'qmd-rotate-'));
  try {
    const log = join(d, 'mcp.daemon.log');
    const rlog = join(d, 'manager.log');
    // 11MB > 10MB MAX_BYTES
    execFileSync('bash', ['-c', `head -c $((11*1024*1024)) /dev/zero > "${log}"`]);
    const manager = join(d, 'manager.sh');
    writeFileSync(manager, `#!/bin/bash\necho "$@" >> "${rlog}"\n`, { mode: 0o755 });
    execFileSync('bash', ['backend/logrotate.sh'], { encoding: 'utf8', env: {
      ...process.env, QMD_DAEMON_LOG: log, QMD_BACKEND_MANAGER: manager,
    }});
    assert.equal(existsSync(`${log}.1`), true, '로그가 .1로 회전돼야 함');
    assert.match(readFileSync(rlog, 'utf8'), /^reload$/m, 'manager reload 호출돼야 함');
  } finally {
    execFileSync('rm', ['-rf', d]);
  }
});

test('BUG-5: logrotate가 MAX 미만 로그는 회전하지 않는다', () => {
  const d = mkdtempSync(join(tmpdir(), 'qmd-norotate-'));
  try {
    const log = join(d, 'mcp.daemon.log');
    writeFileSync(log, 'small\n');
    execFileSync('bash', ['backend/logrotate.sh'], { encoding: 'utf8', env: {
      ...process.env, QMD_DAEMON_LOG: log,
    }});
    assert.equal(existsSync(`${log}.1`), false, '작은 로그는 회전하면 안 됨');
  } finally {
    execFileSync('rm', ['-rf', d]);
  }
});

test('plugin-managed backend manager exists and has no user hardcoding', () => {
  const sh = readFileSync('core/backend_manager.sh', 'utf8');
  assert.ok(!/\/Users\/[a-z]/.test(sh), 'backend_manager.sh: /Users/<user> 홈 경로 하드코딩 잔존');
  assert.match(sh, /check-qmd/);
  assert.match(sh, /kick-index/);
  assert.match(sh, /cleanup-legacy/);
});

// recall 진단 로그(QMD_RECALL_LOG)는 프로젝트 무관 전역 파일 하나라 방치하면 계속 자란다
// (실측 14.6MB / 26,493줄, 로테이션 없음). 데몬 로그와 달리 훅이 append 마다 open/close 해
// 붙잡힌 fd 가 없으므로 mv 만으로 끝이고 데몬을 reload 하면 안 된다.
test('logrotate가 MAX 초과 recall 진단 로그를 회전한다(데몬 reload 없이)', () => {
  const d = mkdtempSync(join(tmpdir(), 'qmd-rotate-recall-'));
  try {
    const daemonLog = join(d, 'mcp.daemon.log');
    const recallLog = join(d, 'recall-diag.jsonl');
    const rlog = join(d, 'manager.log');
    writeFileSync(daemonLog, 'small\n');            // 데몬 로그는 회전 대상 아님
    execFileSync('bash', ['-c', `head -c $((11*1024*1024)) /dev/zero > "${recallLog}"`]);
    const manager = join(d, 'manager.sh');
    writeFileSync(manager, `#!/bin/bash\necho "$@" >> "${rlog}"\n`, { mode: 0o755 });
    execFileSync('bash', ['backend/logrotate.sh'], { encoding: 'utf8', env: {
      ...process.env, QMD_DAEMON_LOG: daemonLog, QMD_RECALL_LOG: recallLog,
      QMD_BACKEND_MANAGER: manager,
    }});
    assert.equal(existsSync(`${recallLog}.1`), true, 'recall 로그가 .1로 회전돼야 함');
    assert.equal(existsSync(recallLog), false, '회전 후 원 경로는 비어 있다(다음 훅이 새로 만든다)');
    // 데몬 로그는 작으니 건드리지 않고, 진단 로그 회전만으로 데몬을 흔들어선 안 된다.
    assert.equal(existsSync(`${daemonLog}.1`), false, '작은 데몬 로그는 회전 대상 아님');
    assert.equal(existsSync(rlog), false, '진단 로그 회전은 manager reload 를 부르지 않는다');
  } finally {
    execFileSync('rm', ['-rf', d]);
  }
});

test('logrotate가 MAX 미만 recall 진단 로그는 회전하지 않는다', () => {
  const d = mkdtempSync(join(tmpdir(), 'qmd-norotate-recall-'));
  try {
    const recallLog = join(d, 'recall-diag.jsonl');
    writeFileSync(recallLog, '{"event":"qmd_recall_selection"}\n');
    execFileSync('bash', ['backend/logrotate.sh'], { encoding: 'utf8', env: {
      ...process.env, QMD_DAEMON_LOG: join(d, 'absent.log'), QMD_RECALL_LOG: recallLog,
    }});
    assert.equal(existsSync(`${recallLog}.1`), false, '미달 로그는 그대로 둔다');
    assert.match(readFileSync(recallLog, 'utf8'), /qmd_recall_selection/, '내용 보존');
  } finally {
    execFileSync('rm', ['-rf', d]);
  }
});

// 회귀: 데몬 로그가 없으면 예전 스크립트는 첫 줄에서 exit 0 했다. 두 대상의 판정이
// 딸려 있으면 데몬 로그 부재만으로 진단 로그가 영원히 회전되지 않는다.
test('데몬 로그가 아예 없어도 recall 진단 로그는 회전된다(판정 독립)', () => {
  const d = mkdtempSync(join(tmpdir(), 'qmd-rotate-indep-'));
  try {
    const recallLog = join(d, 'recall-diag.jsonl');
    execFileSync('bash', ['-c', `head -c $((11*1024*1024)) /dev/zero > "${recallLog}"`]);
    execFileSync('bash', ['backend/logrotate.sh'], { encoding: 'utf8', env: {
      ...process.env, QMD_DAEMON_LOG: join(d, 'no-such-daemon.log'), QMD_RECALL_LOG: recallLog,
    }});
    assert.equal(existsSync(`${recallLog}.1`), true, '데몬 로그 부재와 무관하게 회전');
  } finally {
    execFileSync('rm', ['-rf', d]);
  }
});

// QMD_RECALL_LOG 가 비어 있으면(수동 호출) 회전 대상이 없다 — set -u 아래에서 죽지 않아야 한다.
test('QMD_RECALL_LOG 미설정이면 logrotate는 조용히 통과한다', () => {
  const d = mkdtempSync(join(tmpdir(), 'qmd-rotate-norecall-'));
  try {
    const env = { ...process.env, QMD_DAEMON_LOG: join(d, 'absent.log') };
    delete env.QMD_RECALL_LOG;
    const res = execFileSync('bash', ['backend/logrotate.sh'], { encoding: 'utf8', env });
    assert.equal(res.trim(), '', '무출력');
  } finally {
    execFileSync('rm', ['-rf', d]);
  }
});

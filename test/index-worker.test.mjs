import { test } from "node:test";
import assert from "node:assert";
import { execFileSync, spawn, spawnSync } from "node:child_process";
import { mkdtempSync, writeFileSync, readFileSync, existsSync, chmodSync, mkdirSync, readdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

function makeStubQmd(dir, logFile) {
  const stub = join(dir, "qmd");
  writeFileSync(stub, `#!/bin/bash
echo "$@" >> "${logFile}"
case "$1" in
  embed) echo "Embedded 3 chunks from 1 documents in 1s" ;;
  update) echo "All collections updated." ;;
esac
`);
  chmodSync(stub, 0o755);
  return stub;
}

function makeStubManager(dir, logFile) {
  const manager = join(dir, "manager.sh");
  writeFileSync(manager, `#!/bin/bash\necho "$@" >> "${logFile}"\n`);
  chmodSync(manager, 0o755);
  return manager;
}

test("큐 drain → collection add/update/embed 호출 + 큐 비움", () => {
  const d = mkdtempSync(join(tmpdir(), "wk-"));
  const log = join(d, "calls.log");
  const stub = makeStubQmd(d, log);
  const proj = join(d, "proj"); mkdirSync(join(proj, "04_M"), { recursive: true });
  const q = join(d, "queue");
  writeFileSync(q, `x-manuscript\t${join(proj, "04_M")}\n`);
  execFileSync("bash", ["backend/index_worker.sh"], { encoding: "utf8", env: {
    ...process.env, QMD_DIRTY_QUEUE: q, QMD_FAKE_QMD: stub,
    QMD_INDEX_WORKER_LOCKDIR: join(d, "wlock.d"),
    QMD_WRITER_LOCKDIR: join(d, "ulock.d"), QMD_EMBED_LOCKDIR: join(d, "elock.d"),
    QMD_NO_RELOAD: "1",
  }});
  const calls = readFileSync(log, "utf8");
  assert.match(calls, /collection add .*04_M --name x-manuscript/);
  assert.match(calls, /update/);
  assert.match(calls, /embed/);
  assert.equal(readFileSync(q, "utf8").trim(), ""); // 큐 비움
});

test("중복 큐 항목 dedupe", () => {
  const d = mkdtempSync(join(tmpdir(), "wk-"));
  const log = join(d, "calls.log");
  const stub = makeStubQmd(d, log);
  const proj = join(d, "proj"); mkdirSync(join(proj, "04_M"), { recursive: true });
  const q = join(d, "queue");
  writeFileSync(q, `x\t${join(proj, "04_M")}\nx\t${join(proj, "04_M")}\n`);
  execFileSync("bash", ["backend/index_worker.sh"], { encoding: "utf8", env: {
    ...process.env, QMD_DIRTY_QUEUE: q, QMD_FAKE_QMD: stub,
    QMD_INDEX_WORKER_LOCKDIR: join(d, "wlock.d"),
    QMD_WRITER_LOCKDIR: join(d, "ulock.d"), QMD_EMBED_LOCKDIR: join(d, "elock.d"),
    QMD_NO_RELOAD: "1",
  }});
  const addCount = (readFileSync(log, "utf8").match(/collection add/g) || []).length;
  assert.equal(addCount, 1);
});

test("존재하지 않는 경로 skip", () => {
  const d = mkdtempSync(join(tmpdir(), "wk-"));
  const log = join(d, "calls.log");
  const stub = makeStubQmd(d, log);
  const q = join(d, "queue");
  writeFileSync(q, `x\t${join(d, "does-not-exist")}\n`);
  execFileSync("bash", ["backend/index_worker.sh"], { encoding: "utf8", env: {
    ...process.env, QMD_DIRTY_QUEUE: q, QMD_FAKE_QMD: stub,
    QMD_INDEX_WORKER_LOCKDIR: join(d, "wlock.d"),
    QMD_WRITER_LOCKDIR: join(d, "ulock.d"), QMD_EMBED_LOCKDIR: join(d, "elock.d"),
    QMD_NO_RELOAD: "1",
  }});
  const logContent = existsSync(log) ? readFileSync(log, "utf8") : "";
  assert.doesNotMatch(logContent, /collection add/);
});

test("single-flight: live lock defers with75 and preserves queue and owner", () => {
  const d = mkdtempSync(join(tmpdir(), "wk-"));
  const log = join(d, "calls.log");
  const stub = makeStubQmd(d, log);
  const proj = join(d, "proj"); mkdirSync(join(proj, "04_M"), { recursive: true });
  const q = join(d, "queue");
  writeFileSync(q, `x\t${join(proj, "04_M")}\n`);
  const wlock = join(d, "wlock.d"); mkdirSync(wlock); // 미리 잡아둠
  writeFileSync(join(wlock, 'pid'), String(process.pid));
  const result = spawnSync("bash", ["backend/index_worker.sh"], { encoding: "utf8", env: {
    ...process.env, QMD_DIRTY_QUEUE: q, QMD_FAKE_QMD: stub,
    QMD_CACHE_DIR: join(d,'cache'),QMD_LOCK_BASE: join(d,'locks'),QMD_INDEX_WORKER_LOG: join(d,'worker.log'),
    QMD_INDEX_WORKER_LOCKDIR: wlock, QMD_WRITER_LOCKDIR: join(d, "ulock.d"), QMD_NO_RELOAD: "1",
  }});
  assert.equal(result.status,75,result.stderr);
  assert.equal(readFileSync(join(wlock,'pid'),'utf8'),String(process.pid));
  assert.equal(existsSync(log), false);      // qmd 미호출
  assert.match(readFileSync(q, "utf8"), /04_M/); // 큐 보존
});

// (A) reload 테스트
test("새 임베딩>0 → backend manager reload 호출", () => {
  const d = mkdtempSync(join(tmpdir(), "wk-"));
  const log = join(d, "calls.log");
  const rlog = join(d, "reload.log");
  const stub = makeStubQmd(d, log); // embed가 "Embedded 3 chunks" 출력
  const manager = makeStubManager(d, rlog);
  const proj = join(d, "proj"); mkdirSync(join(proj, "04_M"), { recursive: true });
  const q = join(d, "queue"); writeFileSync(q, `x\t${join(proj, "04_M")}\n`);
  execFileSync("bash", ["backend/index_worker.sh"], { encoding: "utf8", env: {
    ...process.env, QMD_DIRTY_QUEUE: q, QMD_FAKE_QMD: stub,
    QMD_BACKEND_MANAGER: manager,
    QMD_INDEX_WORKER_LOCKDIR: join(d, "wl.d"), QMD_WRITER_LOCKDIR: join(d, "ul.d"),
    QMD_EMBED_LOCKDIR: join(d, "el.d"),
    QMD_HEALTH_SKIP: "1",
  }});
  assert.match(readFileSync(rlog, "utf8"), /^reload$/m);
});

test("새 임베딩 0 → reload 스킵", () => {
  const d = mkdtempSync(join(tmpdir(), "wk-"));
  const log = join(d, "calls.log");
  const rlog = join(d, "reload.log");
  // embed가 0 chunks
  const stub = join(d, "qmd");
  writeFileSync(stub, `#!/bin/bash\necho "$@" >> "${log}"\n[ "$1" = embed ] && echo "Embedded 0 chunks from 0 documents in 0s"\n[ "$1" = update ] && echo "All collections updated."\nexit 0\n`);
  chmodSync(stub, 0o755);
  const manager = makeStubManager(d, rlog);
  const proj = join(d, "proj"); mkdirSync(join(proj, "04_M"), { recursive: true });
  const q = join(d, "queue"); writeFileSync(q, `x\t${join(proj, "04_M")}\n`);
  execFileSync("bash", ["backend/index_worker.sh"], { encoding: "utf8", env: {
    ...process.env, QMD_DIRTY_QUEUE: q, QMD_FAKE_QMD: stub,
    QMD_BACKEND_MANAGER: manager,
    QMD_INDEX_WORKER_LOCKDIR: join(d, "wl.d"), QMD_WRITER_LOCKDIR: join(d, "ul.d"),
    QMD_EMBED_LOCKDIR: join(d, "el.d"),
    QMD_HEALTH_SKIP: "1",
  }});
  assert.equal(existsSync(rlog), false);
});

// (pid) lock 획득 후 pid 파일 생성, 종료 후 lock dir 정리 검증
test("정상 종료 시 lock dir과 pid 파일이 모두 제거된다", () => {
  const d = mkdtempSync(join(tmpdir(), "wk-"));
  const log = join(d, "calls.log");
  const stub = makeStubQmd(d, log);
  const proj = join(d, "proj"); mkdirSync(join(proj, "04_M"), { recursive: true });
  const q = join(d, "queue");
  writeFileSync(q, `x\t${join(proj, "04_M")}\n`);
  const wl = join(d, "wlock.d");
  const ul = join(d, "ulock.d");
  const el = join(d, "elock.d");
  execFileSync("bash", ["backend/index_worker.sh"], { encoding: "utf8", env: {
    ...process.env, QMD_DIRTY_QUEUE: q, QMD_FAKE_QMD: stub,
    QMD_INDEX_WORKER_LOCKDIR: wl, QMD_WRITER_LOCKDIR: ul, QMD_EMBED_LOCKDIR: el,
    QMD_NO_RELOAD: "1",
  }});
  // 정상 완료 후 세 lock dir 모두 제거돼야 한다
  assert.equal(existsSync(wl), false, "WORKER_LOCK dir should be removed");
  assert.equal(existsSync(ul), false, "WRITER_LOCK dir should be removed");
  assert.equal(existsSync(el), false, "EMBED_LOCK dir should be removed");
});

// (B) embed lock 테스트
test("EMBED_LOCK 잡혀 있으면 embed 스킵 + 큐 복원", () => {
  const d = mkdtempSync(join(tmpdir(), "wk-"));
  const log = join(d, "calls.log");
  const stub = makeStubQmd(d, log);
  const proj = join(d, "proj"); mkdirSync(join(proj, "04_M"), { recursive: true });
  const q = join(d, "queue"); writeFileSync(q, `x\t${join(proj, "04_M")}\n`);
  const elock = join(d, "el.d"); mkdirSync(elock);
  // pid 파일에 살아있는 pid(현재 Node 프로세스) 기록 → worker가 stale 오판하지 않도록
  writeFileSync(join(elock, "pid"), String(process.pid));
  assert.throws(() => execFileSync("bash", ["backend/index_worker.sh"], { encoding: "utf8", env: {
    ...process.env, QMD_DIRTY_QUEUE: q, QMD_FAKE_QMD: stub,
    QMD_INDEX_WORKER_LOCKDIR: join(d, "wl.d"), QMD_WRITER_LOCKDIR: join(d, "ul.d"),
    QMD_EMBED_LOCKDIR: elock, QMD_NO_RELOAD: "1",
  }}), {status: 75});
  const calls = readFileSync(log, "utf8");
  assert.doesNotMatch(calls, /embed/); // embed 미호출
  assert.match(readFileSync(q, "utf8"), /04_M/); // 큐 복원
});

test("embed 실패 뒤 큐가 남아 다음 worker 실행에서 재시도된다", () => {
  const d = mkdtempSync(join(tmpdir(), "wk-embed-retry-"));
  const calls = join(d, "calls.log");
  const failOnce = join(d, "fail-once");
  const stub = join(d, "qmd");
  writeFileSync(stub, `#!/bin/bash
echo "$@" >> "${calls}"
if [ "$1" = embed ]; then
  if [ ! -e "${failOnce}" ]; then
    touch "${failOnce}"
    echo "embedding context unavailable" >&2
    exit 1
  fi
  echo "Embedded 1 chunks from 1 documents in 1s"
fi
`);
  chmodSync(stub, 0o755);
  const collection = join(d, "docs"); mkdirSync(collection);
  const queue = join(d, "queue");
  const entry = `synthetic-docs\t${collection}\n`;
  writeFileSync(queue, entry);
  const env = {
    ...process.env, QMD_DIRTY_QUEUE: queue, QMD_FAKE_QMD: stub,
    QMD_INDEX_WORKER_LOG: join(d, "worker.log"),
    QMD_INDEX_WORKER_LOCKDIR: join(d, "wl.d"),
    QMD_WRITER_LOCKDIR: join(d, "ul.d"), QMD_EMBED_LOCKDIR: join(d, "el.d"),
    QMD_NO_RELOAD: "1",
  };
  assert.throws(() => execFileSync("bash", ["backend/index_worker.sh"], { encoding: "utf8", env }), {status: 1});
  assert.equal(readFileSync(queue, "utf8"), entry);
  assert.match(readFileSync(env.QMD_INDEX_WORKER_LOG, "utf8"), /embed failed — claim retained/);
  execFileSync("bash", ["backend/index_worker.sh"], { encoding: "utf8", env });
  assert.equal(readFileSync(queue, "utf8"), "");
  assert.equal((readFileSync(calls, "utf8").match(/^embed$/gm) || []).length, 2);
  assert.equal(existsSync(env.QMD_EMBED_LOCKDIR), false);
});

// (C) delete-triggered reload — 새 임베딩 0이지만 update에서 N removed → reload 필요
test("삭제-only update (0 새 임베딩, 1 removed) → backend manager reload 호출", () => {
  const d = mkdtempSync(join(tmpdir(), "wk-"));
  const log = join(d, "calls.log");
  const rlog = join(d, "reload.log");
  // embed: 0 chunks (no new embeddings), update: reports 1 removed
  const stub = join(d, "qmd");
  writeFileSync(stub, `#!/bin/bash
echo "$@" >> "${log}"
case "$1" in
  embed) echo "Embedded 0 chunks from 0 documents in 0s" ;;
  update) echo "Indexed: 0 new, 0 updated, 0 unchanged, 1 removed" ;;
esac
`);
  chmodSync(stub, 0o755);
  const manager = makeStubManager(d, rlog);
  const proj = join(d, "proj"); mkdirSync(join(proj, "04_M"), { recursive: true });
  const q = join(d, "queue"); writeFileSync(q, `x\t${join(proj, "04_M")}\n`);
  execFileSync("bash", ["backend/index_worker.sh"], { encoding: "utf8", env: {
    ...process.env, QMD_DIRTY_QUEUE: q, QMD_FAKE_QMD: stub,
    QMD_BACKEND_MANAGER: manager,
    QMD_INDEX_WORKER_LOCKDIR: join(d, "wl.d"), QMD_WRITER_LOCKDIR: join(d, "ul.d"),
    QMD_EMBED_LOCKDIR: join(d, "el.d"),
    QMD_HEALTH_SKIP: "1",
  }});
  // reload가 발생했어야 한다
  assert.match(readFileSync(rlog, "utf8"), /^reload$/m);
});

// BUG-4 regression: macOS엔 flock(1)이 없다. 큐 스냅샷/truncate가 python fcntl.flock으로
// 동작해 dirty_queue.py(enqueue)와 실제로 직렬화되는지(락 무동작이 아닌지) 검증.
test("BUG-4: 큐 스냅샷이 flock(1) 없이 python fcntl로 정확히 drain한다", () => {
  const d = mkdtempSync(join(tmpdir(), "wk-"));
  const log = join(d, "calls.log");
  const stub = makeStubQmd(d, log);
  const proj = join(d, "proj"); mkdirSync(join(proj, "04_M"), { recursive: true });
  const q = join(d, "queue");
  // 여러 줄 + 공백 줄 포함 → snapshot 후 큐는 완전히 비워져야 한다.
  writeFileSync(q, `a\t${join(proj, "04_M")}\nb\t${join(proj, "04_M")}\n`);
  execFileSync("bash", ["backend/index_worker.sh"], { encoding: "utf8", env: {
    ...process.env, QMD_DIRTY_QUEUE: q, QMD_FAKE_QMD: stub,
    QMD_INDEX_WORKER_LOCKDIR: join(d, "wlock.d"),
    QMD_WRITER_LOCKDIR: join(d, "ulock.d"), QMD_EMBED_LOCKDIR: join(d, "elock.d"),
    QMD_NO_RELOAD: "1",
  }});
  assert.equal(readFileSync(q, "utf8").trim(), "", "큐가 fcntl 락 하에 정확히 비워져야 함");
  const calls = readFileSync(log, "utf8");
  assert.match(calls, /collection add .* --name a/);
  assert.match(calls, /collection add .* --name b/);
});

// BUG-4 락 상호배제: index_worker의 fcntl LOCK_EX가 유지되는 동안 dirty_queue.py enqueue가
// 블록되는지(같은 락 메커니즘) 직접 검증. flock(1) 무동작이었다면 락이 안 걸려 동시 진행됨.
test("BUG-4: fcntl LOCK_EX가 동일 큐에 대한 enqueue를 실제로 직렬화한다", () => {
  const d = mkdtempSync(join(tmpdir(), "wk-lock-"));
  const q = join(d, "queue");
  writeFileSync(q, "seed\t/x\n");
  // 한 프로세스가 LOCK_EX를 0.4s 잡고, 그 사이 다른 프로세스가 append+LOCK_EX 시도.
  // 락이 동작하면 두 번째 write는 첫 번째 unlock 이후에만 일어나 순서가 보장된다.
  const script = `
import fcntl, os, sys, time, threading
q = ${JSON.stringify(q)}
order = []
def holder():
    with open(q, "r+", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        order.append("hold-start")
        time.sleep(0.4)
        order.append("hold-end")
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
def writer():
    time.sleep(0.1)
    with open(q, "a", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        order.append("write")
        f.write("late\\tx\\n")
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
t1 = threading.Thread(target=holder); t2 = threading.Thread(target=writer)
t1.start(); t2.start(); t1.join(); t2.join()
print(",".join(order))
`;
  try {
    const out = execFileSync("python3", ["-c", script], { encoding: "utf8" }).trim();
    // write는 반드시 hold-end 이후 (락이 동작했다는 증거)
    assert.equal(out, "hold-start,hold-end,write", `락 직렬화 실패: ${out}`);
  } finally {
    execFileSync("rm", ["-rf", d]);
  }
});

// BUG-C regression: writer lock busy 시 requeue가 셸 리디렉션이 아니라 dirty_queue.py/snapshot과
// 동일한 fcntl.flock(LOCK_EX)로 큐에 append 하는지 검증. 외부에서 큐의 fcntl 락을 잡고 있으면
// requeue는 그 락이 풀릴 때까지 블록돼야 한다(락 미사용이면 즉시 append하고 끝난다).
test("BUG-C: writer lock busy에서도 durable claim과 원본 큐가 남는다", () => {
  const d = mkdtempSync(join(tmpdir(), "wk-req-"));
  const stub = makeStubQmd(d, join(d, "calls.log"));
  const proj = join(d, "proj"); mkdirSync(join(proj, "04_M"), { recursive: true });
  const q = join(d, "queue");
  writeFileSync(q, `x\t${join(proj, "04_M")}\n`);
  const wlock = join(d, "ulock.d"); mkdirSync(wlock); // writer lock 선점 → pending 경로 강제
  writeFileSync(join(wlock, 'pid'), String(process.pid));

  // 외부 holder: 큐 파일의 fcntl LOCK_EX를 ~0.6s 잡고 있다가 푼다.
  const holderScript = `
import fcntl, time
with open(${JSON.stringify(q)}, "r+", encoding="utf-8") as f:
    fcntl.flock(f.fileno(), fcntl.LOCK_EX)
    time.sleep(0.6)
    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
`;
  const holder = spawn("python3", ["-c", holderScript]);
  // holder가 락을 잡을 시간을 준다.
  execFileSync("python3", ["-c", "import time; time.sleep(0.2)"]);

  const t0 = Date.now();
  assert.throws(() => execFileSync("bash", ["backend/index_worker.sh"], { encoding: "utf8", env: {
    ...process.env, QMD_DIRTY_QUEUE: q, QMD_FAKE_QMD: stub,
    QMD_INDEX_WORKER_LOCKDIR: join(d, "wlock.d"),
    QMD_WRITER_LOCKDIR: wlock, QMD_EMBED_LOCKDIR: join(d, "elock.d"),
    QMD_NO_RELOAD: "1",
  }}), {status: 75});
  const elapsed = Date.now() - t0;
  holder.kill();

  // 원본 큐는 건드리지 않으며 claim 생성은 고정 sidecar 락으로 직렬화된다.
  assert.match(readFileSync(q, "utf8"), /04_M/); // 큐 복원(무유실)
  assert.equal(JSON.parse(readFileSync(q + '.claim.json', 'utf8')).phase, 'processing');
});

// BUG-D regression: index-worker 동작 로그는 QMD_RECALL_LOG를 상속하지 않고 전용
// QMD_INDEX_WORKER_LOG / $HOME(QMD_CACHE_DIR) 캐시 경로를 쓴다. recall 로그와 분리.
test("BUG-D: index-worker는 QMD_RECALL_LOG가 아닌 전용 로그 경로를 쓴다", () => {
  const d = mkdtempSync(join(tmpdir(), "wk-log-"));
  const stub = makeStubQmd(d, join(d, "calls.log"));
  const proj = join(d, "proj"); mkdirSync(join(proj, "04_M"), { recursive: true });
  const q = join(d, "queue"); writeFileSync(q, `x\t${join(proj, "04_M")}\n`);
  const recallLog = join(d, "recall.log");      // run-hook이 export하는 recall 로그(주입 시뮬)
  const workerLog = join(d, "worker.log");       // 전용 worker 로그
  const cacheDir = join(d, "cache"); mkdirSync(cacheDir);
  execFileSync("bash", ["backend/index_worker.sh"], { encoding: "utf8", env: {
    ...process.env, QMD_DIRTY_QUEUE: q, QMD_FAKE_QMD: stub,
    QMD_INDEX_WORKER_LOCKDIR: join(d, "wl.d"), QMD_WRITER_LOCKDIR: join(d, "ul.d"),
    QMD_EMBED_LOCKDIR: join(d, "el.d"), QMD_NO_RELOAD: "1",
    QMD_RECALL_LOG: recallLog, QMD_INDEX_WORKER_LOG: workerLog, QMD_CACHE_DIR: cacheDir,
  }});
  // worker 동작 로그(qmd 출력 등)는 전용 로그로 가고 recall 로그는 건드리지 않는다.
  assert.equal(existsSync(workerLog), true, "전용 worker 로그가 생성돼야 함");
  assert.ok(readFileSync(workerLog, "utf8").length > 0, "worker 로그에 내용이 있어야 함");
  assert.equal(existsSync(recallLog), false, "recall 로그(QMD_RECALL_LOG)는 worker가 건드리면 안 됨");
});

// BUG-D regression: QMD_INDEX_WORKER_LOG / QMD_RECALL_LOG 둘 다 없으면 기본 로그가
// /tmp가 아니라 $HOME/.cache/qmd(QMD_CACHE_DIR override) 하위여야 한다.
test("BUG-D: 기본 worker 로그는 /tmp가 아닌 캐시 경로", () => {
  const d = mkdtempSync(join(tmpdir(), "wk-defaultlog-"));
  const stub = makeStubQmd(d, join(d, "calls.log"));
  const proj = join(d, "proj"); mkdirSync(join(proj, "04_M"), { recursive: true });
  const q = join(d, "queue"); writeFileSync(q, `x\t${join(proj, "04_M")}\n`);
  const cacheDir = join(d, "cache");
  const env = {
    ...process.env, QMD_DIRTY_QUEUE: q, QMD_FAKE_QMD: stub,
    QMD_INDEX_WORKER_LOCKDIR: join(d, "wl.d"), QMD_WRITER_LOCKDIR: join(d, "ul.d"),
    QMD_EMBED_LOCKDIR: join(d, "el.d"), QMD_NO_RELOAD: "1",
    QMD_CACHE_DIR: cacheDir,
  };
  delete env.QMD_RECALL_LOG;
  delete env.QMD_INDEX_WORKER_LOG;
  execFileSync("bash", ["backend/index_worker.sh"], { encoding: "utf8", env });
  // 기본 로그는 QMD_CACHE_DIR(=$HOME/.cache/qmd 대용) 하위 index-worker.log 여야 한다.
  assert.equal(existsSync(join(cacheDir, "index-worker.log")), true,
    "기본 worker 로그가 캐시 경로에 생성돼야 함(/tmp 아님)");
});

// BUG-1 regression: collection add가 "already exists"로 exit 1 반환해도 update/embed가 호출돼야 한다.
test("collection add already-exists(exit 1) → update/embed 여전히 호출됨", () => {
  const d = mkdtempSync(join(tmpdir(), "wk-"));
  const log = join(d, "calls.log");
  // stub: collection add → stderr "already exists" + exit 1, update/embed → 성공
  const stub = join(d, "qmd");
  writeFileSync(stub, `#!/bin/bash
echo "$@" >> "${log}"
case "$1" in
  collection) echo "Collection 'x' already exists. Use a different name" >&2; exit 1 ;;
  update) echo "All collections updated." ;;
  embed) echo "Embedded 1 chunks from 1 documents in 1s" ;;
esac
`);
  chmodSync(stub, 0o755);
  const proj = join(d, "proj"); mkdirSync(join(proj, "04_M"), { recursive: true });
  const q = join(d, "queue");
  writeFileSync(q, `x\t${join(proj, "04_M")}\n`);
  execFileSync("bash", ["backend/index_worker.sh"], { encoding: "utf8", env: {
    ...process.env, QMD_DIRTY_QUEUE: q, QMD_FAKE_QMD: stub,
    QMD_INDEX_WORKER_LOCKDIR: join(d, "wlock.d"),
    QMD_WRITER_LOCKDIR: join(d, "ulock.d"), QMD_EMBED_LOCKDIR: join(d, "elock.d"),
    QMD_NO_RELOAD: "1",
  }});
  const calls = readFileSync(log, "utf8");
  assert.match(calls, /collection add/);   // collection add 호출됨
  assert.match(calls, /^update/m);          // update 호출됨 (BUG-1: 기존엔 스킵됨)
  assert.match(calls, /^embed/m);           // embed 호출됨  (BUG-1: 기존엔 스킵됨)
  assert.equal(readFileSync(q, "utf8").trim(), ""); // 큐 비워짐
});

test("selected Laya 프로젝트의 legacy dirty 항목은 claim을 붙잡지 않고 버려진다", () => {
  const d = mkdtempSync(join(tmpdir(), "wk-"));
  const log = join(d, "calls.log");
  const stub = makeStubQmd(d, log);
  const selected = join(d, "selected"); mkdirSync(join(selected, "wiki"), { recursive: true });
  const legacy = join(d, "legacy"); mkdirSync(join(legacy, "docs"), { recursive: true });
  const route = join(d, "route.py");
  writeFileSync(route, `import sys\nprint('candidate' if sys.argv[3]==${JSON.stringify(selected)} else 'legacy')\n`);
  const q = join(d, "queue");
  const env = {
    ...process.env, QMD_SETUP_GUARD_FIXTURE: "1", QMD_DIRTY_QUEUE: q, QMD_FAKE_QMD: stub,
    QMD_INDEX_WORKER_LOCKDIR: join(d, "wlock.d"), QMD_INDEX_WORKER_LOG: join(d, "worker.log"),
    QMD_WRITER_LOCKDIR: join(d, "ulock.d"), QMD_EMBED_LOCKDIR: join(d, "elock.d"),
    QMD_NO_RELOAD: "1", QMD_CORE_LAYA_ROUTE_SCRIPT: route,
  };
  // Only the selected project's entries: ACKed without any QMD write.
  writeFileSync(q, `sel\t${join(selected, "wiki")}\t${selected}\nsel-raw\t${selected}\t${selected}\n`);
  execFileSync("bash", ["backend/index_worker.sh"], { encoding: "utf8", env });
  assert.equal(readFileSync(q, "utf8").trim(), "");
  assert.equal(existsSync(log), false);
  assert.match(readFileSync(join(d, "worker.log"), "utf8"),
    /selected Laya route owns sel — legacy entry dropped/);
  // Mixed with a legacy project: only the legacy collection is written.
  writeFileSync(q, `sel\t${join(selected, "wiki")}\t${selected}\nold\t${join(legacy, "docs")}\n`);
  execFileSync("bash", ["backend/index_worker.sh"], { encoding: "utf8", env });
  const calls = readFileSync(log, "utf8");
  assert.match(calls, /collection add .*legacy\/docs --name old/);
  assert.doesNotMatch(calls, /--name sel/);
  assert.equal(readFileSync(q, "utf8").trim(), "");
});

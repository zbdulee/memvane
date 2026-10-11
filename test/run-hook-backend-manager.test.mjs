import { test } from "node:test";
import assert from "node:assert/strict";
import { execFileSync, spawnSync } from "node:child_process";
import { existsSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { removeTemp } from './helpers/temp.mjs';
import { waitUntil } from './helpers/timing.mjs';

function makeExecutable(path, body) {
  writeFileSync(path, body, { mode: 0o755 });
  return path;
}

function runHook(args, input, env = {}) {
  return execFileSync("/bin/bash", ["hooks/run-hook", ...args], {
    input,
    encoding: "utf8",
    env: { ...process.env, ...env },
  });
}

test("sandbox exits before backend manager", () => {
  const d = mkdtempSync(join(tmpdir(), "qmd-runhook-sandbox-"));
  try {
    const marker = join(d, "manager.log");
    const manager = makeExecutable(join(d, "manager.sh"), `#!/usr/bin/env bash\necho "$@" >> "${marker}"\n`);
    // Sandbox exits before reading stdin; writing to its closed pipe can
    // raise EPIPE on Node 22 under CI load.
    const out = runHook(["update", "claude", "--sandbox"], undefined, { QMD_BACKEND_MANAGER: manager });
    assert.equal(out, "");
    assert.equal(existsSync(marker), false);
  } finally {
    removeTemp(d);
  }
});

// 상한·폴 간격은 test/helpers/timing.mjs 가 SSOT다 — 예전에는 4s 고정 + 폴당
// `/bin/sleep` 프로세스 스폰이라 부하가 곧 폴링 비용이었다(근거는 그 파일의 규칙 (1)).
function waitForFile(path, predicate) {
  waitUntil(() => existsSync(path) && predicate(readFileSync(path, "utf8")));
  // 상한 초과도 그대로 돌려준다 — 판정은 호출부의 기능 단정이 한다.
  return existsSync(path) ? readFileSync(path, "utf8") : "";
}

test("update action runs core, then ensures/warms/rotates backend in background (no --wait)", () => {
  const d = mkdtempSync(join(tmpdir(), "qmd-runhook-update-"));
  try {
    const managerLog = join(d, "manager.log");
    const coreLog = join(d, "core.log");
    const manager = makeExecutable(join(d, "manager.sh"), `#!/usr/bin/env bash\necho "$@" >> "${managerLog}"\n`);
    const updateCore = makeExecutable(join(d, "update.sh"), `#!/usr/bin/env bash\necho update >> "${coreLog}"\n`);
    const out = runHook(["update", "codex"], "{}", {
      QMD_BACKEND_MANAGER: manager,
      QMD_CORE_UPDATE_SCRIPT: updateCore,
    });
    assert.equal(out, "");
    // update core runs synchronously (foreground exec)
    assert.equal(readFileSync(coreLog, "utf8"), "update\n");
    // backend lifecycle runs in background, ordered, and WITHOUT the blocking --wait
    const managerContent = waitForFile(managerLog, (c) => c.split("\n").filter(Boolean).length >= 3);
    assert.equal(managerContent, "ensure\nwarm\nrotate\n");
  } finally {
    removeTemp(d);
  }
});

test("update action does not block on a slow backend ensure", () => {
  const d = mkdtempSync(join(tmpdir(), "qmd-runhook-update-noblock-"));
  try {
    const managerStarted = join(d, "manager.started");
    const managerFinished = join(d, "manager.finished");
    const release = join(d, "release");
    const coreLog = join(d, "core.log");
    // ensure blocks until the test releases it (simulates an unresponsive daemon →
    // wait_health). No `sleep <N>` constant: the gate is a file this test controls, so
    // "did the hook wait for ensure?" is decided by state, not by a wall clock.
    // The 20s cap only keeps a stray shell from lingering if the assertions blow up.
    const manager = makeExecutable(
      join(d, "manager.sh"),
      `#!/usr/bin/env bash
if [ "$1" = "ensure" ]; then
  echo started > "${managerStarted}"
  for _ in $(seq 1 400); do [ -f "${release}" ] && break; sleep 0.05; done
  echo finished > "${managerFinished}"
fi
`,
    );
    const updateCore = makeExecutable(join(d, "update.sh"), `#!/usr/bin/env bash\necho update >> "${coreLog}"\n`);
    const out = runHook(["update", "codex"], "{}", {
      QMD_BACKEND_MANAGER: manager,
      QMD_CORE_UPDATE_SCRIPT: updateCore,
    });
    assert.equal(out, "");
    assert.equal(readFileSync(coreLog, "utf8"), "update\n");
    // **이것이 "블로킹하지 않는다"를 잡는 단정이다.** ensure 는 아직 gate 에 걸려 있어
    // 완료 마커를 쓸 수 없으므로, 훅이 ensure 를 기다렸다면 그 마커가 반드시 존재한다.
    // 예전에는 `elapsed < 2000` + `sleep 3` 조합이었고 두 세계의 간격이 3초뿐이라
    // 부하가 걸린 머신에서 좁혀질 수 있었다 — 이제 간격이 시간이 아니라 상태다.
    // 타이밍 정책은 test/helpers/timing.mjs 참고.
    assert.equal(existsSync(managerFinished), false, "update hook blocked on backend ensure");
    // backend ensure was still kicked (in background)
    assert.notEqual(waitForFile(managerStarted, () => true), "");
    // release the gate so the background shell exits promptly
    writeFileSync(release, "");
    assert.notEqual(waitForFile(managerFinished, () => true), "", "backgrounded ensure must still complete");
  } finally {
    removeTemp(d);
  }
});

test("posttool action starts backend without waiting before posttool core", () => {
  const d = mkdtempSync(join(tmpdir(), "qmd-runhook-posttool-"));
  try {
    const managerLog = join(d, "manager.log");
    const coreLog = join(d, "core.log");
    const manager = makeExecutable(join(d, "manager.sh"), `#!/usr/bin/env bash\necho "$@" >> "${managerLog}"\n`);
    const posttoolCore = makeExecutable(join(d, "posttool.py"), `#!/usr/bin/env python3\nopen("${coreLog}", "a").write("posttool\\n")\n`);
    const out = runHook(["posttool", "gemini"], "{}", {
      QMD_BACKEND_MANAGER: manager,
      QMD_CORE_POSTTOOL_SCRIPT: posttoolCore,
    });
    assert.equal(out, "");
    assert.notEqual(waitForFile(managerLog, () => true), "");
    assert.equal(readFileSync(managerLog, "utf8"), "ensure\n");
    assert.equal(readFileSync(coreLog, "utf8"), "posttool\n");
  } finally {
    removeTemp(d);
  }
});

test("index action enqueues through core then kicks async worker", () => {
  const d = mkdtempSync(join(tmpdir(), "qmd-runhook-index-"));
  try {
    const managerLog = join(d, "manager.log");
    const stdinLog = join(d, "stdin.json");
    const manager = makeExecutable(join(d, "manager.sh"), `#!/usr/bin/env bash\necho "$@" >> "${managerLog}"\n`);
    const indexCore = makeExecutable(join(d, "index_enqueue.py"), `#!/usr/bin/env python3\nimport sys\nopen("${stdinLog}", "w").write(sys.stdin.read())\n`);
    const payload = '{"hook_event_name":"PostToolUse","cwd":"/tmp"}';
    const out = runHook(["index", "codex"], payload, {
      QMD_BACKEND_MANAGER: manager,
      QMD_CORE_INDEX_SCRIPT: indexCore,
    });
    assert.equal(out, "");
    assert.equal(readFileSync(stdinLog, "utf8"), payload);
    assert.equal(readFileSync(managerLog, "utf8"), "kick-index\n");
  } finally {
    removeTemp(d);
  }
});

test("index action stays silent if index_enqueue.py invocation fails, and still kicks the worker", () => {
  // index는 compile과 달리 payload를 한 번만 읽으므로 tmp 버퍼링 없이 stdin을
  // 직접 파이프한다 -- python3 호출 자체가 실패해도(스크립트 부재 등) `|| true`로
  // 삼켜져 stdout/stderr silent + exit 0을 유지하고, kick-index는 그와 무관하게
  // 실행돼야 한다(원래 tmp 버퍼링 경로와 동일한 무음 계약, mktemp 의존은 제거됨).
  const d = mkdtempSync(join(tmpdir(), "qmd-runhook-index-fail-"));
  try {
    const managerLog = join(d, "manager.log");
    const manager = makeExecutable(join(d, "manager.sh"), `#!/usr/bin/env bash\necho "$@" >> "${managerLog}"\n`);

    const result = spawnSync("/bin/bash", ["hooks/run-hook", "index", "codex"], {
      input: "{}",
      encoding: "utf8",
      env: {
        ...process.env,
        QMD_BACKEND_MANAGER: manager,
        QMD_CORE_INDEX_SCRIPT: join(d, "nonexistent-index-enqueue.py"),
      },
    });

    assert.equal(result.status, 0);
    assert.equal(result.stdout, "");
    assert.equal(result.stderr, "");
    assert.equal(readFileSync(managerLog, "utf8"), "kick-index\n");
  } finally {
    removeTemp(d);
  }
});

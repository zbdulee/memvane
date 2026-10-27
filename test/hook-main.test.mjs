import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { removeTemp } from './helpers/temp.mjs';

// hook_main.run is the single fail-open boundary for every Python hook
// entrypoint: hooks MUST exit 0 (a deny is JSON-on-stdout, never a non-zero
// exit), so any uncaught exception in main()'s call tree must be coerced to 0
// instead of surfacing as a host-visible "hook (failed): exited with code 1".

// 세션 env 의 QMD_RECALL_LOG 를 그대로 물려받으면 이 파일의 예외 테스트가 사용자의
// **라이브** 진단 로그에 traceback 을 쓴다(실측: 이 파일 1회 실행에 57줄, 누적 610건이
// ~/.cache/qmd/recall-diag.jsonl 에 남아 있었고 전부 첫 프레임이 hook_main.py 였다).
// 테스트가 제품 진단 데이터를 오염시키면 안 된다 — backend-manager 테스트가 managerEnv
// 로 포트 8483 을 assert 로 거부하는 것과 같은 격리 규칙이다. 빈 문자열이면
// `os.environ.get` 이 falsy 를 돌려주므로 미설정과 같고, 아래 `...env` 로 명시 override
// 는 그대로 먹는다(순서가 중요하다).
function cleanEnv(env = {}) {
  return { ...process.env, QMD_RECALL_LOG: '', PYTHONPATH: 'core', ...env };
}

function runPy(code, env = {}) {
  return execFileSync('python3', ['-c', code], {
    encoding: 'utf8',
    env: cleanEnv(env),
  }).trim();
}

test('hook_main.run: main()이 예외를 던지면 0을 반환한다', () => {
  const out = runPy(`
import hook_main
def boom():
    raise RuntimeError("kaboom")
print("rc=%d" % hook_main.run(boom))
`);
  assert.equal(out, 'rc=0');
});

test('hook_main.run: 예외를 QMD_RECALL_LOG에 파일로만 기록하고 stdout은 오염하지 않는다', () => {
  const dir = mkdtempSync(join(tmpdir(), 'qmd-hookmain-'));
  const log = join(dir, 'recall.log');
  try {
    const out = runPy(`
import hook_main
def boom():
    raise ValueError("distinct-marker-42")
rc = hook_main.run(boom)
print("rc=%d" % rc)
`, { QMD_RECALL_LOG: log });
    assert.equal(out, 'rc=0');
    const logged = readFileSync(log, 'utf8');
    assert.match(logged, /qmd_hook_uncaught_exception/);
    assert.match(logged, /distinct-marker-42/);
    // 로그 내용이 stdout(모델 컨텍스트)엔 절대 안 나가야 한다.
    assert.doesNotMatch(out, /distinct-marker-42/);
  } finally {
    removeTemp(dir);
  }
});

test('hook_main.run: 정상(0) 반환은 0', () => {
  const out = runPy(`
import hook_main
print("rc=%d" % hook_main.run(lambda: 0))
`);
  assert.equal(out, 'rc=0');
});

test('hook_main.run: non-zero/음수 int 반환도 0으로 강등한다 (항상 exit 0 불변식)', () => {
  const out = runPy(`
import hook_main
vals = [hook_main.run(lambda: 1), hook_main.run(lambda: -1), hook_main.run(lambda: 7)]
print("rcs=%s" % vals)
`);
  assert.equal(out, 'rcs=[0, 0, 0]');
});

test('hook_main.run: main()이 SystemExit(non-zero)를 던져도 0으로 삼킨다', () => {
  const out = runPy(`
import sys, hook_main
def boom():
    sys.exit(7)
print("rc=%d" % hook_main.run(boom))
`);
  assert.equal(out, 'rc=0');
});

test('hook_main.run: 비-int 반환은 0으로 강등한다', () => {
  const out = runPy(`
import hook_main
print("rc=%d" % hook_main.run(lambda: None))
`);
  assert.equal(out, 'rc=0');
});

test('hook_main.run: QMD_RECALL_LOG 미설정 시에도 예외를 삼키고 0을 반환한다', () => {
  const out = runPy(`
import hook_main, os
os.environ.pop("QMD_RECALL_LOG", None)
def boom():
    raise OSError("no log configured")
print("rc=%d" % hook_main.run(boom))
`);
  assert.equal(out, 'rc=0');
});

// End-to-end: each real hook entrypoint stays exit 0 even when its shared
// config lookup is forced to raise (simulating a sandboxed-fs denial).
for (const script of ['wiki_compile_enqueue.py']) {
  test(`${script}: config 조회가 예외를 던져도 프로세스는 exit 0 (무출력)`, () => {
    const mod = script.replace('.py', '');
    // Monkeypatch config to raise, then drive the real module's main() through
    // its own hook_main wrapper the same way `python3 core/<script>` would.
    const code = `
import sys, io, json
sys.path.insert(0, 'core')
import config as qmd_config
def boom(*a, **k):
    raise PermissionError("simulated sandboxed fs error")
qmd_config.load_project_config = boom
qmd_config.find_project_config = boom
import ${mod} as mod
import hook_main
sys.stdin = io.StringIO(json.dumps({
    "prompt": "x"*50,
    "hook_event_name": "PostToolUse",
    "tool_name": "Edit",
    "tool_input": {"file_path": "/tmp/a.md"},
    "cwd": "/tmp/does-not-matter",
}))
rc = hook_main.run(mod.main)
sys.stderr.write("RC=%d\\n" % rc)
assert rc == 0, rc
`;
    const res = execFileSync('python3', ['-c', code], {
      encoding: 'utf8',
      env: cleanEnv(),
    });
    // stdout must be silent (no partial hook JSON) on the fail-open path.
    assert.equal(res.trim(), '');
  });
}

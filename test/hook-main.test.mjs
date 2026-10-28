import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, readFileSync, realpathSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { removeTemp } from './helpers/temp.mjs';

// hook_main.run is the single fail-open boundary for every Python hook
// entrypoint: hooks MUST exit 0 (a deny is JSON-on-stdout, never a non-zero
// exit), so any uncaught exception in main()'s call tree must be coerced to 0
// instead of surfacing as a host-visible "hook (failed): exited with code 1".

// The exception lands in the install event log bound by the entry point
// (core/event_log.py); nothing reads a QMD_RECALL_LOG path any more.
function cleanEnv(env = {}) {
  return { ...process.env, PYTHONPATH: 'core', ...env };
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

test('hook_main.run: 예외를 바인딩된 설치 이벤트 로그에 hook.error 로만 기록하고 stdout은 오염하지 않는다', () => {
  const dir = realpathSync(mkdtempSync(join(tmpdir(), 'qmd-hookmain-')));
  const log = join(dir, 'recall.log');
  try {
    const out = runPy(`
import hook_main, event_log
event_log.bind(${JSON.stringify(dir)}, install="i1", host="claude", session="s-1")
def boom():
    raise ValueError("distinct-marker-42")
rc = hook_main.run(boom)
print("rc=%d" % rc)
`, { QMD_RECALL_LOG: log });
    assert.equal(out, 'rc=0');
    const lines = readFileSync(join(dir, 'events.jsonl'), 'utf8').trim().split('\n').map(JSON.parse);
    assert.equal(lines.length, 1);
    assert.equal(lines[0].event, 'hook.error');
    assert.equal(lines[0].reason, 'ValueError');
    assert.equal(lines[0].install, 'i1');
    assert.equal(lines[0].session, 's-1');
    assert.equal(lines[0].where, '<string>:5');
    // The message never reaches the log or stdout (model context).
    assert.doesNotMatch(JSON.stringify(lines), /distinct-marker-42/);
    assert.doesNotMatch(out, /distinct-marker-42/);
    // QMD_RECALL_LOG is retired: the path it names is never written.
    assert.throws(() => readFileSync(log, 'utf8'), /ENOENT/);
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

test('hook_main.run: 바인딩된 설치가 없어도 예외를 삼키고 0을 반환한다', () => {
  const out = runPy(`
import hook_main
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

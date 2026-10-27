import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { existsSync, mkdtempSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { removeTemp } from './helpers/temp.mjs';

function runPython(source, env = {}) {
  return execFileSync('python3', ['-c', source], {
    encoding: 'utf8',
    env: { ...process.env, PYTHONPATH: process.cwd(), ...env },
  }).trim();
}

function makeExecutable(path, body) {
  writeFileSync(path, body, { mode: 0o755 });
  return path;
}

function makeFakeRoot() {
  const root = mkdtempSync(join(tmpdir(), 'qmd-hermes-root-'));
  const core = join(root, 'core');
  mkdirSync(core, { recursive: true });
  const managerLog = join(root, 'manager.log');
  makeExecutable(join(core, 'backend_manager.sh'), `#!/usr/bin/env bash\necho "$@" >> "${managerLog}"\n`);
  return { root, core, managerLog };
}

test('Hermes plugin manifest declares qmd hook adapter only, leaving Claude/Codex manifests separate', () => {
  assert.equal(existsSync('plugin.yaml'), true, 'Hermes plugin.yaml should exist at repo root');
  const manifest = readFileSync('plugin.yaml', 'utf8');
  assert.match(manifest, /name:\s*qmd-auto-context/);
  for (const hook of ['pre_llm_call', 'on_session_start', 'pre_tool_call', 'post_tool_call']) {
    assert.match(manifest, new RegExp(hook), `missing ${hook}`);
  }
  assert.equal(existsSync('hooks/hooks.json'), true, 'Claude hook manifest remains in hooks/');
  assert.equal(existsSync('hooks/hooks-codex.json'), true, 'Codex hook manifest remains in hooks/');
});

test('Hermes root plugin shim registers lifecycle hooks through hermes_adapter.plugin', () => {
  assert.equal(existsSync('__init__.py'), true, 'Hermes directory plugin entrypoint should exist');
  const rootInit = readFileSync('__init__.py', 'utf8');
  assert.match(rootInit, /hermes_adapter\.plugin/);
  const plugin = readFileSync('hermes_adapter/plugin.py', 'utf8');
  for (const hook of ['pre_llm_call', 'on_session_start', 'pre_tool_call', 'post_tool_call']) {
    assert.match(plugin, new RegExp(`register_hook\\(["']${hook}["']`), `missing register_hook(${hook})`);
  }
});

test('Hermes recall bridge delegates to core/recall.py and returns pre_llm_call context', () => {
  const { root, core, managerLog } = makeFakeRoot();
  const stdinLog = join(root, 'recall.stdin.json');
  makeExecutable(join(core, 'recall.py'), `#!/usr/bin/env python3\nimport json, os, sys\nopen("${stdinLog}", "w").write(sys.stdin.read())\nprint(json.dumps({"hookSpecificOutput":{"additionalContext":"관련 문서:\\n- [sample] docs/a.md"}}))\n`);
  try {
    const out = runPython(`
import json
from hermes_adapter.core_bridge import recall_context
print(json.dumps(recall_context(user_message='검색 결과 정렬?', cwd='/tmp/project')))
`, { QMD_HERMES_PLUGIN_ROOT_FOR_TEST: root, QMD_QUERY_FIXTURE: 'fixture.json' });
    assert.deepEqual(JSON.parse(out), { context: '관련 문서:\n- [sample] docs/a.md' });
    const payload = JSON.parse(readFileSync(stdinLog, 'utf8'));
    assert.equal(payload.hook_event_name, 'UserPromptSubmit');
    assert.equal(payload.prompt, '검색 결과 정렬?');
    assert.equal(payload.cwd, '/tmp/project');
    assert.equal(existsSync(managerLog), false, 'fixture mode should skip backend ensure');
  } finally {
    removeTemp(root);
  }
});

test('Hermes recall bounds the core hook budget below its own kill timeout', () => {
  const { root, core } = makeFakeRoot();
  const envLog = join(root, 'recall.env.json');
  makeExecutable(join(core, 'recall.py'), `#!/usr/bin/env python3\nimport json, os, sys\nsys.stdin.read()\nopen("${envLog}", "w").write(json.dumps(os.environ.get("QMD_HOOK_TOTAL_SECONDS")))\nprint(json.dumps({"hookSpecificOutput":{"additionalContext":"ok"}}))\n`);
  const recall = `
import json
from hermes_adapter.core_bridge import recall_context
print(json.dumps(recall_context(user_message='질문', cwd='/tmp/project')))
`;
  try {
    const env = { QMD_HERMES_PLUGIN_ROOT_FOR_TEST: root, QMD_QUERY_FIXTURE: 'fixture.json' };
    assert.deepEqual(JSON.parse(runPython(recall, { ...env, QMD_HOOK_TOTAL_SECONDS: '' })), { context: 'ok' });
    assert.equal(JSON.parse(readFileSync(envLog, 'utf8')), '6', 'default 18s budget must not outlive the 8s Hermes kill');
    runPython(recall, { ...env, QMD_HOOK_TOTAL_SECONDS: '3' });
    assert.equal(JSON.parse(readFileSync(envLog, 'utf8')), '3', 'a smaller user budget is kept');
    runPython(recall, { ...env, QMD_HOOK_TOTAL_SECONDS: '20' });
    assert.equal(JSON.parse(readFileSync(envLog, 'utf8')), '6', 'a larger user budget is capped');
  } finally {
    removeTemp(root);
  }
});

test('Hermes recall keeps flushed context when the core outlives the kill timeout', () => {
  const { root, core } = makeFakeRoot();
  // Prints and flushes like core/recall.py, then keeps working past the kill.
  makeExecutable(join(core, 'recall.py'), `#!/usr/bin/env python3\nimport json, sys, time\nsys.stdin.read()\nprint(json.dumps({"hookSpecificOutput":{"additionalContext":"flushed"}}), flush=True)\ntime.sleep(30)\n`);
  try {
    const out = runPython(`
import json
import hermes_adapter.core_bridge as bridge
bridge.RECALL_TIMEOUT = 1.0
print(json.dumps(bridge.recall_context(user_message='질문', cwd='/tmp/project')))
`, { QMD_HERMES_PLUGIN_ROOT_FOR_TEST: root, QMD_QUERY_FIXTURE: 'fixture.json' });
    assert.deepEqual(JSON.parse(out), { context: 'flushed' });
  } finally {
    removeTemp(root);
  }
});

test('recall flushes its injected context before post-output learning work', () => {
  const recall = readFileSync('core/recall.py', 'utf8');
  const printAt = recall.indexOf('print(json.dumps(output, ensure_ascii=False), flush=True)');
  assert.ok(printAt > 0, 'final hook output must be flushed');
  assert.ok(printAt < recall.indexOf('# Optional learning observes eligible returned candidates'),
    'flush must precede capture/expansion work that can outlive a host timeout');
});

test('Hermes session update bridge mirrors run-hook backend lifecycle order', () => {
  const { root, core, managerLog } = makeFakeRoot();
  const stdinLog = join(root, 'update.stdin.json');
  makeExecutable(join(core, 'update.sh'), `#!/usr/bin/env bash\ncat > "${stdinLog}"\n`);
  try {
    runPython(`
from hermes_adapter.core_bridge import session_update
session_update(cwd='/tmp/project')
`, { QMD_HERMES_PLUGIN_ROOT_FOR_TEST: root });
    assert.equal(readFileSync(managerLog, 'utf8'), 'ensure --wait\nwarm\nrotate\n');
    const payload = JSON.parse(readFileSync(stdinLog, 'utf8'));
    assert.equal(payload.hook_event_name, 'SessionStart');
    assert.equal(payload.cwd, '/tmp/project');
  } finally {
    removeTemp(root);
  }
});

test('Hermes pre_tool_call gate maps write_file to core Write deny block', () => {
  const { root, core } = makeFakeRoot();
  const stdinLog = join(root, 'gate.stdin.json');
  makeExecutable(join(core, 'preflight_gate.py'), `#!/usr/bin/env python3\nimport json, sys\nopen("${stdinLog}", "w").write(sys.stdin.read())\nprint(json.dumps({"hookSpecificOutput":{"permissionDecision":"deny","permissionDecisionReason":"pending project"}}))\n`);
  try {
    const out = runPython(`
import json
from hermes_adapter.core_bridge import pre_edit_gate
print(json.dumps(pre_edit_gate(tool_name='write_file', args={'path':'docs/a.md','content':'x'}, cwd='/tmp/project')))
`, { QMD_HERMES_PLUGIN_ROOT_FOR_TEST: root });
    assert.deepEqual(JSON.parse(out), { action: 'block', message: 'pending project' });
    const payload = JSON.parse(readFileSync(stdinLog, 'utf8'));
    assert.equal(payload.hook_event_name, 'PreToolUse');
    assert.equal(payload.tool_name, 'Write');
    assert.equal(payload.tool_input.file_path, 'docs/a.md');
    assert.equal(payload.cwd, '/tmp/project');
  } finally {
    removeTemp(root);
  }
});

test('Hermes post_tool_call index maps patch mode=patch and kicks workers after enqueue', () => {
  const { root, core, managerLog } = makeFakeRoot();
  const stdinLog = join(root, 'index.stdin.json');
  const compileLog = join(root, 'compile.stdin.json');
  makeExecutable(join(core, 'index_enqueue.py'), `#!/usr/bin/env python3\nimport sys\nopen("${stdinLog}", "w").write(sys.stdin.read())\n`);
  makeExecutable(join(core, 'wiki_compile_enqueue.py'), `#!/usr/bin/env python3\nimport sys\nopen("${compileLog}", "w").write(sys.stdin.read())\n`);
  makeExecutable(join(core, 'posttool.py'), '#!/usr/bin/env python3\nimport sys\nsys.stdin.read()\n');
  try {
    runPython(`
from hermes_adapter.core_bridge import post_edit_sync
post_edit_sync(tool_name='patch', args={'mode':'patch','patch':'*** Begin Patch'}, result='{}', status='ok', cwd='/tmp/project')
`, { QMD_HERMES_PLUGIN_ROOT_FOR_TEST: root });
    assert.equal(readFileSync(managerLog, 'utf8'), 'ensure --wait\nkick-index\nkick-wiki-compile /tmp/project\n');
    const payload = JSON.parse(readFileSync(stdinLog, 'utf8'));
    assert.equal(payload.hook_event_name, 'PostToolUse');
    assert.equal(payload.tool_name, 'apply_patch');
    assert.equal(payload.tool_input.patch, '*** Begin Patch');
    assert.equal(payload.cwd, '/tmp/project');
    assert.deepEqual(JSON.parse(readFileSync(compileLog, 'utf8')), payload);
  } finally {
    removeTemp(root);
  }
});

test('Hermes post_tool_call skips compile enqueue on failed tool result', () => {
  const { root, core, managerLog } = makeFakeRoot();
  const compileLog = join(root, 'compile.stdin.json');
  makeExecutable(join(core, 'wiki_compile_enqueue.py'), `#!/usr/bin/env python3\nimport sys\nopen("${compileLog}", "w").write(sys.stdin.read())\n`);
  try {
    runPython(`
from hermes_adapter.core_bridge import post_edit_sync
post_edit_sync(tool_name='write_file', args={'path':'docs/a.md','content':'x'}, result='{"error":"boom"}', status='error', cwd='/tmp/project')
`, { QMD_HERMES_PLUGIN_ROOT_FOR_TEST: root });
    assert.equal(existsSync(managerLog), false);
    assert.equal(existsSync(compileLog), false);
  } finally {
    removeTemp(root);
  }
});


test('Hermes post/session hooks sandbox-headless no-op before manager or core side effects', () => {
  for (const envName of ['QMD_SANDBOX', 'HERMES_HEADLESS']) {
    const { root, core, managerLog } = makeFakeRoot();
    const posttoolLog = join(root, 'posttool.called');
    const indexLog = join(root, 'index.called');
    const compileLog = join(root, 'compile.called');
    const updateLog = join(root, 'update.called');
    makeExecutable(join(core, 'posttool.py'), `#!/usr/bin/env python3\nopen(${JSON.stringify(posttoolLog)}, 'w').write('1')\n`);
    makeExecutable(join(core, 'index_enqueue.py'), `#!/usr/bin/env python3\nopen(${JSON.stringify(indexLog)}, 'w').write('1')\n`);
    makeExecutable(join(core, 'wiki_compile_enqueue.py'), `#!/usr/bin/env python3\nopen(${JSON.stringify(compileLog)}, 'w').write('1')\n`);
    makeExecutable(join(core, 'update.sh'), `#!/usr/bin/env bash\necho called > ${JSON.stringify(updateLog)}\n`);
    try {
      runPython(`
from hermes_adapter.core_bridge import post_edit_sync, session_update
post_edit_sync(tool_name='write_file', args={'path':'docs/a.md','content':'x'}, result='{}', status='ok', cwd='/tmp/project')
session_update(cwd='/tmp/project')
`, { QMD_HERMES_PLUGIN_ROOT_FOR_TEST: root, [envName]: '1' });
      assert.equal(existsSync(managerLog), false, `${envName}: manager should not run`);
      assert.equal(existsSync(posttoolLog), false, `${envName}: posttool should not run`);
      assert.equal(existsSync(indexLog), false, `${envName}: index enqueue should not run`);
      assert.equal(existsSync(compileLog), false, `${envName}: compile enqueue should not run`);
      assert.equal(existsSync(updateLog), false, `${envName}: update should not run`);
    } finally {
      removeTemp(root);
    }
  }
});

test('Hermes bridge does not reimplement qmd daemon/query logic', () => {
  const bridge = readFileSync('hermes_adapter/core_bridge.py', 'utf8');
  assert.doesNotMatch(bridge, /\/query/);
  assert.doesNotMatch(bridge, /urlopen|requests\./);
  for (const script of ['core/recall.py', 'core/update.sh', 'core/index_enqueue.py', 'core/wiki_compile_enqueue.py', 'core/preflight_gate.py']) {
    assert.match(bridge, new RegExp(script.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')));
  }
});

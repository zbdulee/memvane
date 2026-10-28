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

test('Hermes plugin manifest declares qmd hook adapter only, leaving Claude/Codex manifests separate', () => {
  assert.equal(existsSync('plugin.yaml'), true, 'Hermes plugin.yaml should exist at repo root');
  const manifest = readFileSync('plugin.yaml', 'utf8');
  assert.match(manifest, /name:\s*memvane/);
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

test('Hermes hooks do nothing for a project without a selected Laya route', () => {
  const home = mkdtempSync(join(tmpdir(), 'qmd-hermes-home-'));
  const project = join(home, 'project');
  mkdirSync(join(project, '.auto-context'), { recursive: true });
  writeFileSync(join(project, '.auto-context', 'settings.json'), JSON.stringify({ name: 'demo', skipPaths: [] }));
  try {
    const out = runPython(`
import json, os
from hermes_adapter import core_bridge as bridge
cwd = ${JSON.stringify(project)}
results = [
    bridge.recall_context(user_message='프로젝트 설정은 어디에 있어?', cwd=cwd),
    bridge.session_update(cwd=cwd),
    bridge.pre_edit_gate(tool_name='write_file', args={'path': 'docs/a.md', 'content': 'x'}, cwd=cwd),
    bridge.post_edit_sync(tool_name='write_file', args={'path': 'docs/a.md', 'content': 'x'},
                          result='{}', status='ok', cwd=cwd),
]
print(json.dumps({'results': results, 'files': sorted(os.listdir(os.path.join(cwd, '.auto-context')))}))
`, { HOME: home, QMD_SANDBOX: '', HERMES_SANDBOX: '', HERMES_HEADLESS: '', QMD_SETUP_GUARD_FIXTURE: '' });
    assert.deepEqual(JSON.parse(out), { results: [null, null, null, null], files: ['settings.json'] });
  } finally {
    removeTemp(home);
  }
});

test('Hermes post/session hooks sandbox-headless no-op before any route lookup', () => {
  for (const envName of ['QMD_SANDBOX', 'HERMES_HEADLESS']) {
    const out = runPython(`
import json
from hermes_adapter import core_bridge as bridge
calls = []
bridge._laya_route_state = lambda *a: calls.append(a) or 'legacy'
bridge.post_edit_sync(tool_name='write_file', args={'path':'docs/a.md','content':'x'}, result='{}', status='ok', cwd='/tmp/project')
bridge.session_update(cwd='/tmp/project')
print(json.dumps(calls))
`, { [envName]: '1' });
    assert.equal(out, '[]', envName);
  }
});

test('Hermes bridge serves only the selected Laya route', () => {
  const bridge = readFileSync('hermes_adapter/core_bridge.py', 'utf8');
  assert.doesNotMatch(bridge, /\/query/);
  assert.doesNotMatch(bridge, /urlopen|requests\./);
  for (const script of ['recall.py', 'update.sh', 'index_enqueue.py', 'preflight_gate.py', 'backend_manager', 'setup_guard']) {
    assert.doesNotMatch(bridge, new RegExp(script.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')));
  }
  assert.match(bridge, /laya_host_response import serve_selected/);
});

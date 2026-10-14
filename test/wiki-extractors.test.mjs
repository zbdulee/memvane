import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { writeFileSync, mkdtempSync, existsSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { removeTemp } from './helpers/temp.mjs';

function runLib(pyBody, input) {
  return execFileSync('python3', ['-c', pyBody], { cwd: process.cwd(), input, encoding: 'utf8' });
}

test('extractor CLI resolution preserves PATH choice before fnm/bun fallback', () => {
  const out = JSON.parse(runLib(`import sys,os,json,tempfile
from pathlib import Path
sys.path.insert(0,'core/extractors');import lib
with tempfile.TemporaryDirectory() as tmp:
 home=Path(tmp);os.environ['HOME']=tmp
 preferred=home/'host-bin';preferred.mkdir()
 fallback=home/'.local/share/fnm/node-versions/v99.0.0/installation/bin';fallback.mkdir(parents=True)
 for base in (preferred,fallback):
  p=base/'codex';p.write_text('#!/bin/sh\\nexit 0\\n');p.chmod(0o700)
 os.environ['PATH']=str(preferred)
 selected=lib.resolve_bin('codex','QMD_EXTRACTOR_TEST_CODEX_BIN')
 os.environ['PATH']=str(home/'missing-bin')
 absent=lib.resolve_bin('codex','QMD_EXTRACTOR_TEST_CODEX_BIN')
 os.environ['QMD_EXTRACTOR_TEST_CODEX_BIN']='/explicit/reviewed/codex'
 explicit=lib.resolve_bin('codex','QMD_EXTRACTOR_TEST_CODEX_BIN')
 print(json.dumps({'pathFirst':selected==str(preferred/'codex'),'fallbackWhenAbsent':absent==str(fallback/'codex'),'explicitUnchanged':explicit=='/explicit/reviewed/codex'}))`, ''));
  assert.deepEqual(out, { pathFirst: true, fallbackWhenAbsent: true, explicitUnchanged: true });
});

test('adapter reports a bounded CLI signal failure without exposing prompt body', () => {
  const d = mkdtempSync(join(tmpdir(), 'extractor-signal-'));
  try {
    const fake = join(d, 'signal-codex');
    writeFileSync(fake, '#!/bin/sh\nkill -KILL $$\n', { mode: 0o700 });
    let failure;
    try {
      execFileSync('python3', ['core/extractors/codex_adapter.py'], {
        input: JSON.stringify({ task: 'verify', card: { content: 'PRIVATE_SYNTHETIC_BODY' }, sources: [] }),
        encoding: 'utf8', env: { ...process.env, QMD_EXTRACTOR_CODEX_BIN: fake }, stdio: 'pipe',
      });
    } catch (err) { failure = err; }
    assert.ok(failure);
    assert.match(failure.stderr, /extractor_cli_failed: engine=codex returncode=-9/);
    assert.doesNotMatch(failure.stderr, /PRIVATE_SYNTHETIC_BODY/);
  } finally { removeTemp(d); }
});

test('extract_candidates pulls JSON object from fenced/prose output', () => {
  const py = `import sys; sys.path.insert(0,'core/extractors'); import lib
text = 'Here you go:\\n\\u0060\\u0060\\u0060json\\n{"candidates":[{"title":"T"}]}\\n\\u0060\\u0060\\u0060\\nDone.'
import json; print(json.dumps(lib.extract_candidates(text)))`;
  const out = JSON.parse(runLib(py, ''));
  assert.equal(out.candidates[0].title, 'T');
});

test('extract_candidates returns empty dict when no JSON present', () => {
  const py = `import sys; sys.path.insert(0,'core/extractors'); import lib
import json; print(json.dumps(lib.extract_candidates('no json here')))`;
  assert.deepEqual(JSON.parse(runLib(py, '')), {});
});

test('extract_candidates handles unbalanced braces inside string values', () => {
  // summary contains a lone "{" — a naive brace counter would never close the object
  const py = `import sys,json; sys.path.insert(0,'core/extractors'); import lib
text = json.dumps({"candidates":[{"title":"T","summary":"code: function foo() {"}]})
out = lib.extract_candidates('here:\\n' + text + '\\nthanks')
print(json.dumps(out))`;
  const out = JSON.parse(runLib(py, ''));
  assert.equal(out.candidates[0].title, 'T');
  assert.match(out.candidates[0].summary, /function foo\(\) \{/);
});

test('build_prompt embeds source content and a candidates-only instruction', () => {
  const py = `import sys,json; sys.path.insert(0,'core/extractors'); import lib
p=lib.build_prompt({'source':{'path':'docs/x.md','content':'UNIQ_SRC_BODY'},'wiki':{'schema':'S','index':'I','logTail':''}})
print(json.dumps({'has_body':'UNIQ_SRC_BODY' in p,'has_candidates':'candidates' in p,'has_identity':'canonicalKey' in p and 'aliases' in p and 'targetPath' in p,'no_tools':'tool' in p.lower()}))`;
  const out = JSON.parse(runLib(py, ''));
  assert.equal(out.has_body, true);
  assert.equal(out.has_candidates, true);
  assert.equal(out.has_identity, true);
  assert.equal(out.no_tools, true);
});

test('build_prompt renders similarPages section and omits EXISTING WIKI INDEX when present', () => {
  const script = `
import sys
sys.path.insert(0, 'core/extractors')
import lib
payload = {
    'source': {'path': 'docs/a.md', 'content': 'new source text'},
    'wiki': {
        'schema': 'SCHEMA',
        'index': '- some/old/index/line.md - Old Title',
        'similarPages': [
            {'path': '.auto-context/wiki/entities/known.md', 'score': 0.91, 'content': '## Summary\\nThe known fact.'},
        ],
    },
}
print(lib.build_prompt(payload))
`;
  const out = execFileSync('python3', ['-c', script], { encoding: 'utf8' });
  assert.match(out, /TOP MATCHING EXISTING WIKI PAGES/);
  assert.match(out, /\.auto-context\/wiki\/entities\/known\.md/);
  assert.match(out, /The known fact\./);
  assert.doesNotMatch(out, /EXISTING WIKI INDEX/);
  assert.doesNotMatch(out, /some\/old\/index\/line\.md/);
});

test('build_prompt falls back to EXISTING WIKI INDEX exactly as before when similarPages is absent', () => {
  const scriptWithout = `
import sys
sys.path.insert(0, 'core/extractors')
import lib
payload = {
    'source': {'path': 'docs/a.md', 'content': 'new source text'},
    'wiki': {'schema': 'SCHEMA', 'index': '- some/old/index/line.md - Old Title'},
}
print(lib.build_prompt(payload))
`;
  const withoutSimilarPages = execFileSync('python3', ['-c', scriptWithout], { encoding: 'utf8' });
  assert.match(withoutSimilarPages, /EXISTING WIKI INDEX \(avoid duplicates\):/);
  assert.match(withoutSimilarPages, /some\/old\/index\/line\.md/);
  assert.doesNotMatch(withoutSimilarPages, /TOP MATCHING EXISTING WIKI PAGES/);

  const scriptEmpty = `
import sys
sys.path.insert(0, 'core/extractors')
import lib
payload = {
    'source': {'path': 'docs/a.md', 'content': 'new source text'},
    'wiki': {'schema': 'SCHEMA', 'index': '- some/old/index/line.md - Old Title', 'similarPages': []},
}
print(lib.build_prompt(payload))
`;
  const withEmptySimilarPages = execFileSync('python3', ['-c', scriptEmpty], { encoding: 'utf8' });
  assert.equal(withEmptySimilarPages, withoutSimilarPages);
});

test('build_verify_prompt embeds card/sources and adversarial verdict instruction', () => {
  const py = `import sys,json; sys.path.insert(0,'core/extractors'); import lib
p=lib.build_verify_prompt({'task':'verify','card':{'path':'.auto-context/wiki/concepts/x.md','content':'CARD_BODY'},'sources':[{'path':'docs/src.md','content':'SRC_BODY','truncated':True}]})
print(json.dumps({'has_card':'CARD_BODY' in p,'has_src':'SRC_BODY' in p,'has_trunc':'truncated: true' in p,'has_verdict':'"verdict"' in p,'has_refute':'REFUTE' in p,'no_tools':'Do NOT use any tools' in p}))`;
  const out = JSON.parse(runLib(py, ''));
  assert.equal(out.has_card, true);
  assert.equal(out.has_src, true);
  assert.equal(out.has_trunc, true);
  assert.equal(out.has_verdict, true);
  assert.equal(out.has_refute, true);
  assert.equal(out.no_tools, true);
});

test('extract_verdict pulls last valid verdict object, ignores invalid verdict values', () => {
  const py = `import sys,json; sys.path.insert(0,'core/extractors'); import lib
text = 'thinking {"verdict":"maybe"} then\\n{"verdict":"fail","claims":[{"claim":"c","supported":False if 0 else False}],"reasons":["contradicts"]}'
text = 'thinking {"verdict":"maybe"} then\\n' + json.dumps({"verdict":"fail","claims":[{"claim":"c","supported":False}],"reasons":["contradicts"]})
print(json.dumps(lib.extract_verdict(text)))`;
  const out = JSON.parse(runLib(py, ''));
  assert.equal(out.verdict, 'fail');
  assert.equal(out.reasons[0], 'contradicts');
});

test('claude adapter: task=verify payload → verify prompt 사용 + verdict JSON emit', () => {
  const d = mkdtempSync(join(tmpdir(), 'claude-verify-'));
  const promptLog = join(d, 'prompt.txt');
  const fakeCli = join(d, 'fake-claude');
  writeFileSync(fakeCli, `#!/usr/bin/env bash\nprintf '%s' "$@" > "${promptLog}"\necho '{"verdict":"pass","claims":[{"claim":"c1","supported":true,"quote":"q","sourcePath":"docs/s.md"}],"reasons":[]}'\n`, { mode: 0o755 });
  const payload = JSON.stringify({
    task: 'verify',
    card: { path: '.auto-context/wiki/concepts/x.md', content: 'CARD_BODY' },
    sources: [{ path: 'docs/s.md', content: 'SRC_BODY', truncated: false }],
  });
  const out = execFileSync('python3', ['core/extractors/claude_adapter.py'], {
    cwd: process.cwd(), input: payload, encoding: 'utf8',
    env: { ...process.env, QMD_EXTRACTOR_CLAUDE_BIN: fakeCli },
  });
  const parsed = JSON.parse(out);
  assert.equal(parsed.verdict, 'pass');
  assert.equal(parsed.claims[0].supported, true);
  const prompt = readFileSync(promptLog, 'utf8');
  assert.match(prompt, /REFUTE/);
  assert.match(prompt, /CARD_BODY/);
  assert.doesNotMatch(prompt, /wiki candidates/, 'extraction 프롬프트가 아님');
  removeTemp(d);
});

test('run_isolated injects QMD_SANDBOX=1 into the child env', () => {
  const py = `import sys; sys.path.insert(0,'core/extractors'); import lib
out, code = lib.run_isolated(['bash','-lc','printf %s "$QMD_SANDBOX"'], 10)
print(out)`;
  assert.equal(runLib(py, '').trim(), '1');
});

test('claude adapter calls its CLI in a temp cwd and emits candidates', () => {
  const d = mkdtempSync(join(tmpdir(), 'claude-ad-'));
  const cwdLog = join(d, 'cwd.txt');
  const argsLog = join(d, 'args.txt');
  const sandboxLog = join(d, 'sandbox.txt');
  const fakeCli = join(d, 'fake-claude');
  // fake CLI: record the cwd it ran in, its args, and the QMD_SANDBOX env it inherited
  writeFileSync(fakeCli, `#!/usr/bin/env bash\npwd > "${cwdLog}"\necho "$@" > "${argsLog}"\nprintf '%s' "$QMD_SANDBOX" > "${sandboxLog}"\necho 'sure:'\necho '{"candidates":[{"title":"C","summary":"Durable claude.","suggestedType":"concept","confidence":"high"}]}'\n`, { mode: 0o755 });
  const payload = JSON.stringify({ source: { path: 'docs/x.md', content: 'body' }, wiki: {} });
  const out = execFileSync('python3', ['core/extractors/claude_adapter.py'], {
    cwd: process.cwd(), input: payload, encoding: 'utf8',
    env: { ...process.env, QMD_EXTRACTOR_CLAUDE_BIN: fakeCli },
  });
  const parsed = JSON.parse(out);
  assert.equal(parsed.candidates[0].title, 'C');
  // ran in a temp dir, NOT the project cwd
  assert.notEqual(readFileSync(cwdLog, 'utf8').trim(), process.cwd());
  // session persistence disabled (no CLI-side session record)
  assert.match(readFileSync(argsLog, 'utf8'), /--no-session-persistence/);
  // compile extractor should not load custom skills/plugins/hooks/MCP/rules.
  assert.match(readFileSync(argsLog, 'utf8'), /--safe-mode/);
  // nested qmd hooks neutered: child inherits QMD_SANDBOX=1
  assert.equal(readFileSync(sandboxLog, 'utf8'), '1');
  removeTemp(d);
});

test('claude adapter exits 127 when its CLI is absent', () => {
  let code = 0;
  try {
    execFileSync('python3', ['core/extractors/claude_adapter.py'], {
      cwd: process.cwd(), input: '{"source":{"path":"x","content":"y"},"wiki":{}}', encoding: 'utf8',
      env: { ...process.env, QMD_EXTRACTOR_CLAUDE_BIN: '/nonexistent/claude-xyz', PATH: '/usr/bin:/bin' },
    });
  } catch (e) { code = e.status; }
  assert.equal(code, 127);
});

test('codex adapter passes read-only sandbox and emits candidates', () => {
  const d = mkdtempSync(join(tmpdir(), 'codex-ad-'));
  const argsLog = join(d, 'args.txt');
  const fakeCli = join(d, 'fake-codex');
  writeFileSync(fakeCli, `#!/usr/bin/env bash\necho "$@" > "${argsLog}"\necho '{"candidates":[{"title":"CX","summary":"Durable codex.","suggestedType":"decision","confidence":"medium"}]}'\n`, { mode: 0o755 });
  const out = execFileSync('python3', ['core/extractors/codex_adapter.py'], {
    cwd: process.cwd(), input: '{"source":{"path":"docs/x.md","content":"b"},"wiki":{}}', encoding: 'utf8',
    env: { ...process.env, QMD_EXTRACTOR_CODEX_BIN: fakeCli },
  });
  assert.equal(JSON.parse(out).candidates[0].title, 'CX');
  const args = readFileSync(argsLog, 'utf8');
  assert.match(args, /exec/);
  assert.match(args, /-s read-only/);
  assert.match(args, /--ephemeral/);
  assert.match(args, /--ignore-user-config/);
  assert.match(args, /--ignore-rules/);
  removeTemp(d);
});

test('hermes adapter passes safe-mode/no-tools and emits candidates', () => {
  const d = mkdtempSync(join(tmpdir(), 'hermes-ad-'));
  const argsLog = join(d, 'args.txt');
  const fakeCli = join(d, 'fake-hermes');
  writeFileSync(fakeCli, `#!/usr/bin/env bash\necho "$@" > "${argsLog}"\necho '{"candidates":[{"title":"HM","summary":"Durable hermes.","suggestedType":"entity","confidence":"low"}]}'\n`, { mode: 0o755 });
  const out = execFileSync('python3', ['core/extractors/hermes_adapter.py'], {
    cwd: process.cwd(), input: '{"source":{"path":"docs/x.md","content":"b"},"wiki":{}}', encoding: 'utf8',
    env: { ...process.env, QMD_EXTRACTOR_HERMES_BIN: fakeCli },
  });
  assert.equal(JSON.parse(out).candidates[0].title, 'HM');
  const args = readFileSync(argsLog, 'utf8');
  assert.match(args, /-z/);
  assert.match(args, /--safe-mode/);
  removeTemp(d);
});

test('built-in adapters apply requested effort and emit bounded _qmd audit metadata', () => {
  const d = mkdtempSync(join(tmpdir(), 'effort-argv-'));
  const claudeArgs = join(d, 'claude.args');
  const codexArgs = join(d, 'codex.args');
  const hermesArgs = join(d, 'hermes.args');
  const make = (name, argsLog, output) => {
    const file = join(d, name);
    writeFileSync(file, `#!/usr/bin/env bash\nprintf '%s\\n' "$@" > ${JSON.stringify(argsLog)}\nprintf '%s' ${JSON.stringify(output)}\n`, { mode: 0o755 });
    return file;
  };
  const claude = make('claude', claudeArgs, '{"candidates":[]}');
  const codex = make('codex', codexArgs, '{"candidates":[]}');
  const hermes = make('hermes', hermesArgs, '{"candidates":[]}');
  const base = { source: { path: 'docs/x.md', content: 'body' }, wiki: {} };
  try {
    const claudeOut = JSON.parse(execFileSync('python3', ['core/extractors/claude_adapter.py'], {
      cwd: process.cwd(), input: JSON.stringify({ ...base, _qmd: { reasoningEffort: { requested: 'high' } } }), encoding: 'utf8',
      env: { ...process.env, QMD_EXTRACTOR_CLAUDE_BIN: claude },
    }));
    const codexOut = JSON.parse(execFileSync('python3', ['core/extractors/codex_adapter.py'], {
      cwd: process.cwd(), input: JSON.stringify({ ...base, _qmd: { reasoningEffort: { requested: 'xhigh' } } }), encoding: 'utf8',
      env: { ...process.env, QMD_EXTRACTOR_CODEX_BIN: codex },
    }));
    const hermesOut = JSON.parse(execFileSync('python3', ['core/extractors/hermes_adapter.py'], {
      cwd: process.cwd(), input: JSON.stringify({ ...base, _qmd: { reasoningEffort: { requested: 'medium' } } }), encoding: 'utf8',
      env: { ...process.env, QMD_EXTRACTOR_HERMES_BIN: hermes },
    }));
    assert.deepEqual(claudeOut._qmd.reasoningEffort, {
      requested: 'high', applied: 'high', status: 'applied', reason: 'capability_flag',
    });
    assert.deepEqual(codexOut._qmd.reasoningEffort, {
      requested: 'xhigh', applied: 'xhigh', status: 'applied', reason: 'capability_flag',
    });
    assert.deepEqual(hermesOut._qmd.reasoningEffort, {
      requested: 'medium', applied: null, status: 'unsupported', reason: 'no_hermetic_invocation_mechanism',
    });
    assert.match(readFileSync(claudeArgs, 'utf8'), /--effort\nhigh/);
    assert.match(readFileSync(codexArgs, 'utf8'), /-c\nmodel_reasoning_effort=xhigh/);
    assert.doesNotMatch(readFileSync(hermesArgs, 'utf8'), /effort|model_reasoning_effort/);
    assert.equal(JSON.stringify(claudeOut).includes('body'), false, 'audit metadata must not contain source content');
  } finally { removeTemp(d); }
});

test('effort-option rejection retries exactly once without effort and records retried_without_effort', () => {
  const d = mkdtempSync(join(tmpdir(), 'effort-retry-'));
  const count = join(d, 'calls');
  const fake = join(d, 'claude');
  writeFileSync(fake, `#!/usr/bin/env bash\ncount=$(cat ${JSON.stringify(count)} 2>/dev/null || echo 0)\ncount=$((count + 1))\nprintf '%s' "$count" > ${JSON.stringify(count)}\nif printf '%s' "$*" | grep -q -- '--effort'; then echo "unknown option '--effort'" >&2; exit 2; fi\nprintf '%s' '{"candidates":[]}'\n`, { mode: 0o755 });
  try {
    const out = JSON.parse(execFileSync('python3', ['core/extractors/claude_adapter.py'], {
      cwd: process.cwd(), input: JSON.stringify({ source: { path: 'x', content: 'y' }, wiki: {}, _qmd: { reasoningEffort: { requested: 'high' } } }), encoding: 'utf8',
      env: { ...process.env, QMD_EXTRACTOR_CLAUDE_BIN: fake },
    }));
    assert.equal(readFileSync(count, 'utf8'), '2');
    assert.deepEqual(out._qmd.reasoningEffort, {
      requested: 'high', applied: null, status: 'retried_without_effort', reason: 'effort_option_rejected',
    });
  } finally { removeTemp(d); }
});

test('generic failure and a different engine rejection are never retried', () => {
  const d = mkdtempSync(join(tmpdir(), 'effort-no-retry-'));
  const count = join(d, 'calls');
  const make = (name, stderr) => {
    const file = join(d, name);
    writeFileSync(file, `#!/usr/bin/env bash\nn=$(cat ${JSON.stringify(count)} 2>/dev/null || echo 0)\nn=$((n + 1))\nprintf '%s' "$n" > ${JSON.stringify(count)}\necho ${JSON.stringify(stderr)} >&2\nexit 2\n`, { mode: 0o755 });
    return file;
  };
  const run = (adapter, envName, fake) => {
    try {
      execFileSync('python3', [adapter], {
        cwd: process.cwd(), input: JSON.stringify({ source: { path: 'x', content: 'y' }, wiki: {}, _qmd: { reasoningEffort: { requested: 'high' } } }), encoding: 'utf8',
        env: { ...process.env, [envName]: fake },
      });
      assert.fail('expected adapter failure');
    } catch (error) { assert.equal(error.status, 2); }
  };
  try {
    run('core/extractors/claude_adapter.py', 'QMD_EXTRACTOR_CLAUDE_BIN', make('generic', 'network failure'));
    assert.equal(readFileSync(count, 'utf8'), '1');
    writeFileSync(count, '0');
    run('core/extractors/codex_adapter.py', 'QMD_EXTRACTOR_CODEX_BIN', make('wrong-engine', "unknown option '--effort'"));
    assert.equal(readFileSync(count, 'utf8'), '1');
  } finally { removeTemp(d); }
});

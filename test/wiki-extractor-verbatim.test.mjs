// P3 — extractor output contract: identifiers must survive into the card body.
// Cards are lex-searched, so a token present in the body is a match opportunity;
// unlike the query side (where extra terms AND-narrow), verbatim is a pure win.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { existsSync, mkdtempSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { removeTemp } from './helpers/temp.mjs';

function buildPrompt(payload) {
  return execFileSync('python3', ['-c', `
import sys, json
sys.path.insert(0, '.')
import memvane.wiki.engines.extractors.lib as lib
print(lib.build_prompt(json.loads(sys.stdin.read())))
`], { cwd: process.cwd(), input: JSON.stringify(payload), encoding: 'utf8' });
}

function pyCompile(body) {
  return execFileSync('python3', ['-c', `
import sys
sys.path.insert(0, '.')
import memvane.wiki.compile.wiki_compile as wc
${body}
`], { cwd: process.cwd(), encoding: 'utf8' });
}

const basePayload = { source: { path: 'docs/x.md', content: 'BODY' }, wiki: { schema: 'S' } };

test('prompt template instructs verbatim preservation of every identifier class', () => {
  const prompt = buildPrompt(basePayload);
  assert.match(prompt, /VERBATIM RULES/);
  assert.match(prompt, /backtick code spans/i);
  assert.match(prompt, /file and directory paths/i);
  // paths must stay whole: query-side sends basename stems, and FTS5 splits on `/`,
  // so a full path in the card matches the stem anyway. Shortening only loses tokens.
  assert.match(prompt, /do NOT shorten it to a basename/);
  assert.match(prompt, /signatures/i);
  assert.match(prompt, /configuration keys, field names, env vars, CLI flags/i);
  assert.match(prompt, /numeric thresholds, limits, versions, percentages/i);
  assert.match(prompt, /error strings, status\/reason codes, log lines, exit codes/i);
  assert.match(prompt, /bracketed or prefixed tags and IDs/i);
  assert.match(prompt, /Do NOT invent identifiers/);
});

test('prompt no longer rewards brevity but still forbids transcripts', () => {
  const prompt = buildPrompt(basePayload);
  assert.doesNotMatch(prompt, /compact, durable wiki candidates/);
  assert.doesNotMatch(prompt, /short durable conclusion/);
  assert.match(prompt, /There is no reward for brevity/);
  assert.match(prompt, /never drop an identifier to save space/);
  // transcript_like lint still exists, so the anti-transcript contract must hold
  assert.match(prompt, /NOT a transcript, NOT step-by-step dialog, NOT a scene-by-scene retelling/);
});

test('prompt keeps narrative identifiers in the same rule (novel corpus non-regression)', () => {
  // novel sources already preserve well; the verbatim list is conditional ("whenever
  // the source contains"), so a narrative source triggers only the name/quote clause.
  const prompt = buildPrompt(basePayload);
  assert.match(prompt, /names of people, places, entities and short quoted lines/i);
  assert.match(prompt, /reproduce the wording exactly/);
  assert.match(prompt, /EP13/); // episode-style tag named as an identifier class
});

test('secrets still outrank verbatim rules in the prompt', () => {
  const prompt = buildPrompt(basePayload);
  assert.match(prompt, /Never include secrets, API keys, tokens, or credentials — this OUTRANKS the verbatim rules/);
});

test('prompt states the lint line budget from payload.maxLines, defaulting to 120', () => {
  assert.match(buildPrompt({ ...basePayload, maxLines: 40 }), /up to 40 lines/);
  assert.match(buildPrompt(basePayload), /up to 120 lines/);
  assert.match(buildPrompt({ ...basePayload, maxLines: 'nope' }), /up to 120 lines/);
  assert.match(buildPrompt({ ...basePayload, maxLines: 0 }), /up to 120 lines/);
});

test('prompt marks a truncated source and stays unmarked otherwise', () => {
  const truncated = buildPrompt({ source: { path: 'docs/x.md', content: 'BODY', truncated: true }, wiki: {} });
  assert.match(truncated, /SOURCE FILE: docs\/x\.md \(truncated: true/);
  assert.match(truncated, /never assert that something is absent from the document/);
  const whole = buildPrompt({ source: { path: 'docs/x.md', content: 'BODY', truncated: false }, wiki: {} });
  assert.match(whole, /SOURCE FILE: docs\/x\.md\n/);
  assert.doesNotMatch(whole, /truncated: true/);
});

test('worker forwards source truncation and maxAutoPageLines into the extractor payload', () => {
  const dir = mkdtempSync(join(tmpdir(), 'qwiki-verbatim-'));
  const dump = join(dir, 'payload.json');
  const extractor = join(dir, 'extract.py');
  try {
    mkdirSync(join(dir, '.auto-context', 'compile'), { recursive: true });
    mkdirSync(join(dir, '.auto-context', 'wiki'), { recursive: true });
    mkdirSync(join(dir, 'docs'), { recursive: true });
    // longer than maxSourceChars below → must be reported as truncated
    writeFileSync(join(dir, 'docs', 'source.md'), `# Source\n${'x'.repeat(500)}\n`);
    writeFileSync(extractor, `#!/usr/bin/env python3
import json, sys
payload = json.loads(sys.stdin.read())
open(${JSON.stringify(dump)}, 'w').write(json.dumps({
  'truncated': payload['source'].get('truncated'),
  'maxLines': payload.get('maxLines'),
  'contentLen': len(payload['source']['content']),
}))
print(json.dumps({'candidates': []}))
`);
    writeFileSync(join(dir, '.auto-context', 'settings.json'), JSON.stringify({
      indexing: true,
      collections: ['proj-docs', 'proj-wiki'],
      collectionPaths: { 'proj-docs': 'docs', 'proj-wiki': '.auto-context/wiki' },
      collectionRoles: { 'proj-docs': 'raw', 'proj-wiki': 'wiki' },
      wikiPath: '.auto-context/wiki',
      compile: {
        mode: 'guarded', defaultStatus: 'generated',
        triggers: ['post_tool_source'],
        maxSourceChars: 100,
        maxAutoPageLines: 60,
        extractor: { backends: { claude: ['python3', extractor] }, timeout: 30 },
      },
    }));
    writeFileSync(join(dir, '.auto-context', 'compile', 'source-queue.jsonl'), JSON.stringify({
      ts: '2026-07-29T00:00:00Z', trigger: 'post_tool_source', engine: 'claude', cwd: dir,
      source: { kind: 'file', path: 'docs/source.md', collection: 'proj-docs' },
    }) + '\n');

    execFileSync('python3', ['entry/run.py', 'wiki-compile-worker', '--cwd', dir], {
      cwd: process.cwd(), encoding: 'utf8',
      env: { ...process.env, QMD_DIRTY_QUEUE: join(dir, 'queue') },
    });
    const seen = JSON.parse(readFileSync(dump, 'utf8'));
    assert.equal(seen.truncated, true);
    assert.equal(seen.contentLen, 100);
    assert.equal(seen.maxLines, 60);
  } finally {
    removeTemp(dir);
  }
});

// --- SECRET_PATTERNS: narrow false positives without weakening the last line of defense

test('real credentials are still detected and redacted', () => {
  const out = JSON.parse(pyCompile(`
import json
cases = {
  'sk_literal': 'leaked sk-1234567890abcdef1234567890abcdef here',
  'opaque_token': 'token: ghp_A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6',
  'api_key_eq': 'API_KEY=AKIAIOSFODNN7EXAMPLE',
  'secret_colon': 'secret: hunter2correcthorsebattery',
  'quoted_token': 'token: "eyJhbGciOiJIUzI1NiJ9.payload.sig"',
  'apikey_dash': 'api-key: 8f14e45fceea167a5a36dedd4bea2543',
}
print(json.dumps({k: [wc.has_secret_like(v), wc.redact(v)[0]] for k, v in cases.items()}))
`));
  for (const [name, [detected, redacted]] of Object.entries(out)) {
    assert.equal(detected, true, `${name} must still be detected`);
    assert.match(redacted, /\[REDACTED\]/, `${name} must be redacted`);
  }
  assert.doesNotMatch(out.sk_literal[1], /1234567890abcdef/);
  assert.doesNotMatch(out.opaque_token[1], /ghp_A1b2C3d4/);
});

test('technical cards that merely name a secret-ish config key are no longer flagged', () => {
  const out = JSON.parse(pyCompile(`
import json
cases = [
  'compile.extractor 설정은 \`token: <REDACTED>\` 형태로 값을 가린다',
  'Set \`API_KEY=$OPENAI_API_KEY\` before running the worker',
  '\`secret: null\` 이면 인증을 건너뛴다',
  'payload schema: {token: string}',
  'token = {{value}} 를 템플릿에서 치환한다',
  'api_key: ***',
  'secret: ...',
  'GITHUB_TOKEN=\${GITHUB_TOKEN} 을 env 로 전달한다',
]
print(json.dumps([[wc.has_secret_like(c), wc.redact(c)] for c in cases]))
`));
  out.forEach(([detected, [text, redactions]], i) => {
    assert.equal(detected, false, `case ${i} must not be flagged as secret_like`);
    assert.deepEqual(redactions, [], `case ${i} must record no redaction`);
    assert.doesNotMatch(text, /\[REDACTED\]/, `case ${i} must survive verbatim`);
  });
});

test('wiki_compile writes an identifier-dense technical card but still rejects a real credential', () => {
  const work = mkdtempSync(join(tmpdir(), 'qwiki-secret-e2e-'));
  const settings = {
    indexing: true,
    collections: ['proj-wiki'],
    collectionPaths: { 'proj-wiki': '.auto-context/wiki' },
    collectionRoles: { 'proj-wiki': 'wiki' },
    wikiPath: '.auto-context/wiki',
    compile: {
      mode: 'auto-wiki', defaultStatus: 'generated',
    },
  };
  const run = (payload) => execFileSync('python3', ['entry/run.py', 'wiki-compile', '--cwd', work], {
    cwd: process.cwd(), encoding: 'utf8', input: JSON.stringify(payload),
    env: { ...process.env, QMD_DIRTY_QUEUE: join(work, 'queue') },
  });
  try {
    mkdirSync(join(work, '.auto-context'), { recursive: true });
    writeFileSync(join(work, '.auto-context', 'settings.json'), JSON.stringify(settings));

    run({
      trigger: 'manual',
      title: 'Extractor Auth Config',
      summary: '`compile.extractor.timeout` 기본 30초, `maxSourceChars` 12000자.\n'
        + '`token: <REDACTED>` 로 표기하며 실제 값은 `$QMD_TOKEN` env 로만 넘긴다.\n'
        + 'exit 127 이면 host CLI 부재다.',
      suggestedType: 'concept',
      confidence: 'high',
      targetPath: '.auto-context/wiki/concepts/extractor-auth-config.md',
    });
    const page = join(work, '.auto-context', 'wiki', 'concepts', 'extractor-auth-config.md');
    assert.equal(existsSync(page), true, 'identifier-dense card must be written');
    const text = readFileSync(page, 'utf8');
    assert.doesNotMatch(text, /\[REDACTED\]\n|redactions:\n  - secret_like/);
    for (const token of ['compile.extractor.timeout', 'maxSourceChars', '12000', '$QMD_TOKEN', 'exit 127']) {
      assert.match(text, new RegExp(token.replace(/[.$]/g, '\\$&')), `${token} must survive into the card`);
    }

    run({
      trigger: 'manual',
      title: 'Leaky Card',
      summary: 'token: ghp_A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6 을 쓴다',
      suggestedType: 'concept',
      confidence: 'high',
      targetPath: '.auto-context/wiki/concepts/leaky-card.md',
    });
    assert.equal(existsSync(join(work, '.auto-context', 'wiki', 'concepts', 'leaky-card.md')), false);
    const candidates = readFileSync(join(work, '.auto-context', 'compile', 'candidates.jsonl'), 'utf8');
    assert.match(candidates, /secret_like/);
    assert.doesNotMatch(candidates, /ghp_A1b2C3d4/);
  } finally {
    removeTemp(work);
  }
});

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { execFileSync, spawn, spawnSync } from 'node:child_process';
import { createServer } from 'node:http';
import { chmodSync, existsSync, mkdtempSync, mkdirSync, readFileSync, statSync, utimesSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { removeTemp } from './helpers/temp.mjs';
import { stampVerified, wikiTrustState } from './helpers/wiki_trust.mjs';

function setupProject(extraCompile = {}) {
  const dir = mkdtempSync(join(tmpdir(), 'qwiki-worker-'));
  mkdirSync(join(dir, '.auto-context', 'compile'), { recursive: true });
  mkdirSync(join(dir, '.auto-context', 'wiki'), { recursive: true });
  mkdirSync(join(dir, 'docs'), { recursive: true });
  writeFileSync(join(dir, 'docs', 'source.md'), '# Source\n\nDurable decision: generated wiki pages cite source markdown.\n');
  writeFileSync(join(dir, '.auto-context', 'settings.json'), JSON.stringify({
    indexing: true,
    collections: ['proj-docs', 'proj-wiki'],
    collectionPaths: {
      'proj-docs': 'docs',
      'proj-wiki': '.auto-context/wiki',
    },
    collectionRoles: {
      'proj-docs': 'raw',
      'proj-wiki': 'wiki',
    },
    wikiPath: '.auto-context/wiki',
    compile: {
      mode: 'guarded',
      defaultStatus: 'generated',
      triggers: ['post_tool_source', 'manual'],
      maxSourceChars: 12000,
      extractor: { backends: {}, timeout: 30 },
      ...extraCompile,
    },
  }));
  writeFileSync(join(dir, '.auto-context', 'compile', 'source-queue.jsonl'), JSON.stringify({
    ts: '2026-06-26T00:00:00Z',
    trigger: 'post_tool_source',
    engine: 'claude',
    cwd: dir,
    source: { kind: 'file', path: 'docs/source.md', collection: 'proj-docs' },
  }) + '\n');
  return dir;
}

function runWorker(project, env = {}, extraArgs = []) {
  const dirtyQueue = env.QMD_DIRTY_QUEUE || join(project, '.auto-context', 'compile', 'dirty-queue');
  return execFileSync('python3', ['core/wiki_compile_worker.py', '--cwd', project, ...extraArgs], {
    cwd: process.cwd(),
    encoding: 'utf8',
    env: { ...process.env, QMD_DIRTY_QUEUE: dirtyQueue, ...env },
  });
}

function jsonl(path) {
  if (!existsSync(path)) return [];
  return readFileSync(path, 'utf8').trim().split('\n').filter(Boolean).map((line) => JSON.parse(line));
}

function seedPending(project, engine = 'claude') {
  return JSON.parse(execFileSync('python3', ['-c', [
    'import json, sys',
    'from pathlib import Path',
    'sys.path.insert(0, "core")',
    'import wiki_freshness as F',
    'print(json.dumps(F.record_pending_refresh(Path(sys.argv[1]), "docs/source.md", sys.argv[2])))',
  ].join('\n'), project, engine], { cwd: process.cwd(), encoding: 'utf8' }));
}

function attachPendingToQueuedJob(project, pending) {
  const queue = join(project, '.auto-context', 'compile', 'source-queue.jsonl');
  const job = jsonl(queue)[0];
  job.pendingRefresh = { eventId: pending.eventId, ts: pending.ts, engine: pending.engine };
  writeFileSync(queue, JSON.stringify(job) + '\n');
}

test('worker recovers a pending-only crash window by reenqueuing the source job', () => {
  const project = setupProject({ batch: { idleSeconds: 3600, maxItems: 5 } });
  try {
    writeFileSync(join(project, '.auto-context', 'compile', 'source-queue.jsonl'), '');
    const pending = seedPending(project, 'codex');
    runWorker(project, {}, ['--json']);
    const jobs = jsonl(join(project, '.auto-context', 'compile', 'source-queue.jsonl'));
    assert.equal(jobs.length, 1);
    assert.equal(jobs[0].source.path, 'docs/source.md');
    assert.deepEqual(jobs[0].pendingRefresh, {
      eventId: pending.eventId, ts: pending.ts, engine: 'codex',
    });
  } finally { removeTemp(project); }
});

test('worker recovery coalesces canonical cwd aliases in the queue-append crash window', () => {
  const project = setupProject({ batch: { idleSeconds: 3600, maxItems: 5 } });
  try {
    const older = seedPending(project, 'claude');
    attachPendingToQueuedJob(project, older);
    const latest = seedPending(project, 'codex');
    runWorker(project, {}, ['--json']);
    const jobs = jsonl(join(project, '.auto-context', 'compile', 'source-queue.jsonl'));
    assert.equal(jobs.length, 1, 'recovery and the already-appended job coalesce by source');
    assert.deepEqual(jobs[0].pendingRefresh, {
      eventId: latest.eventId, ts: latest.ts, engine: latest.engine,
    });
  } finally { removeTemp(project); }
});

test('worker runs the engine backend argv, writes generated wiki page, and stays silent', () => {
  const extractor = join(mkdtempSync(join(tmpdir(), 'extractor-')), 'extract.py');
  writeFileSync(extractor, `#!/usr/bin/env python3
import json, sys
payload = json.loads(sys.stdin.read())
assert 'content' in payload['source']
print(json.dumps({'candidates': [{
  'title': 'Source Compile Decision',
  'summary': 'Generated wiki pages cite source markdown instead of copying raw source.',
  'suggestedType': 'decision',
  'confidence': 'high',
  'targetPath': '.auto-context/wiki/decisions/source-compile-decision.md'
}]}))
`);
  const project = setupProject({ extractor: { backends: { claude: ['python3', extractor] }, timeout: 30 } });
  const dirtyQueue = join(mkdtempSync(join(tmpdir(), 'dirty-')), 'queue');
  try {
    const pending = seedPending(project, 'claude');
    attachPendingToQueuedJob(project, pending);
    const out = runWorker(project, { QMD_DIRTY_QUEUE: dirtyQueue }, ['--flush-all']);
    assert.equal(out, '');
    const page = join(project, '.auto-context', 'wiki', 'decisions', 'source-compile-decision.md');
    assert.equal(existsSync(page), true);
    const text = readFileSync(page, 'utf8');
    assert.match(text, /Source Compile Decision/);
    assert.match(text, /path: "docs\/source.md"/);
    assert.match(text, /^sourceRevisions:$/m);
    const sourceBytes = readFileSync(join(project, 'docs', 'source.md'));
    assert.match(text, new RegExp(`sha256: "${createHash('sha256').update(sourceBytes).digest('hex')}"`),
      'trusted revision hashes the complete source file');
    const verifyJob = jsonl(join(project, '.auto-context', 'compile', 'verify-queue.jsonl'))[0];
    assert.equal(verifyJob.sourceRevisions[0].sha256,
      createHash('sha256').update(sourceBytes).digest('hex'));
    assert.equal(readFileSync(join(project, '.auto-context', 'compile', 'source-queue.jsonl'), 'utf8'), '');
    assert.match(readFileSync(dirtyQueue, 'utf8'), /^proj-wiki\t/);
    const refreshRows = jsonl(join(project, '.auto-context', 'compile', 'source-refresh-pending.jsonl'));
    assert.deepEqual(refreshRows.map((row) => row.state), ['pending_refresh', 'resolved']);
    assert.equal(refreshRows[1].pendingEventId, pending.eventId);
    assert.deepEqual(Object.keys(refreshRows[1]).sort(), ['pendingEventId', 'state', 'ts']);
    assert.doesNotMatch(JSON.stringify(refreshRows), /sha256|sourceRevision|mtimeNs|"size"|content|secret/i,
      'the refresh ledger persists event metadata only');
  } finally {
    removeTemp(project);
  }
});

test('process_job binds the latest unresolved event before capture and rejects a replayed queue token', () => {
  const project = setupProject({ verify: { enabled: false } });
  try {
    const older = seedPending(project, 'claude');
    attachPendingToQueuedJob(project, older);
    const latest = seedPending(project, 'codex');
    const out = JSON.parse(execFileSync('python3', ['-c', `
import json, pathlib, sys
sys.path.insert(0, 'core')
import config, wiki_compile_worker as w
root = pathlib.Path(${JSON.stringify(project)}).resolve()
found = config.find_project_config(str(root))
job = json.loads((root / '.auto-context/compile/source-queue.jsonl').read_text().splitlines()[0])
w.resolve_extractor_argv = lambda *_: ['fake-extractor']
w.run_extractor = lambda *_: ({'candidates': [{
  'title': 'Latest Event Binding',
  'summary': 'The worker binds the latest unresolved event before source capture.',
  'suggestedType': 'decision',
  'confidence': 'high',
  'targetPath': '.auto-context/wiki/decisions/latest-event-binding.md',
}]}, '', 0)
w.gather_similar_pages = lambda *_: None
seen = {}
def compile_candidate(_root, candidate):
    seen['eventId'] = candidate.get('sourceRefreshEventId')
    return {
      'action': 'created',
      'targetPath': candidate['targetPath'],
      'sourceRevisions': candidate['sourceRevisions'],
      'sourceRefreshEventId': candidate.get('sourceRefreshEventId'),
    }
w.compile_candidate = compile_candidate
result = w.process_job(root, found['config'], found['config']['compile'], job)
print(json.dumps({'result': result, 'seen': seen, 'rows': w.wiki_freshness.read_pending_refreshes(root)}))
`], { cwd: process.cwd(), encoding: 'utf8' }));
    assert.equal(out.seen.eventId, latest.eventId, 'stale queued token is replaced before capture');
    assert.equal(out.rows.at(-1).pendingEventId, latest.eventId);
    assert.equal(out.rows.filter((row) => row.state === 'pending_refresh').length, 2);
    assert.equal(out.result[1], false);
  } finally { removeTemp(project); }
});

test('worker preserves the source job when resolved-event append fails after card write', () => {
  const extractor = join(mkdtempSync(join(tmpdir(), 'extractor-resolve-failure-')), 'extract.py');
  writeFileSync(extractor, `#!/usr/bin/env python3
import json
print(json.dumps({'candidates': [{
  'title': 'Resolve Failure Retry',
  'summary': 'A durable pending event remains unresolved until its successful captured revision is recorded.',
  'suggestedType': 'decision',
  'confidence': 'high',
  'targetPath': '.auto-context/wiki/decisions/resolve-failure-retry.md'
}]}))
`);
  const project = setupProject({ extractor: { backends: { claude: ['python3', extractor] }, timeout: 30 } });
  const ledger = join(project, '.auto-context', 'compile', 'source-refresh-pending.jsonl');
  try {
    const pending = seedPending(project, 'claude');
    attachPendingToQueuedJob(project, pending);
    chmodSync(ledger, 0o444);
    runWorker(project, {}, ['--flush-all']);
    assert.equal(existsSync(join(project, '.auto-context', 'wiki', 'decisions', 'resolve-failure-retry.md')), true,
      'the card write can succeed before the separate resolved append');
    assert.equal(jsonl(ledger).at(-1).state, 'pending_refresh');
    assert.equal(jsonl(join(project, '.auto-context', 'compile', 'source-queue.jsonl')).length, 1,
      'failed resolution is retried instead of inferred from the card write');
  } finally {
    chmodSync(ledger, 0o644);
    removeTemp(project);
  }
});

test('worker preserves the source job and writes no card when source changes during extraction', () => {
  const extractor = join(mkdtempSync(join(tmpdir(), 'extractor-source-race-')), 'extract.py');
  writeFileSync(extractor, `#!/usr/bin/env python3
import json, pathlib, sys
payload = json.loads(sys.stdin.read())
pathlib.Path(payload['cwd'], payload['source']['path']).write_text('# Changed during extraction\\n', encoding='utf-8')
print(json.dumps({'candidates': [{
  'title': 'Stale Extraction Must Not Land',
  'summary': 'This candidate was produced from a source revision that changed during extraction.',
  'suggestedType': 'decision',
  'confidence': 'high',
  'targetPath': '.auto-context/wiki/decisions/stale-extraction.md'
}]}))
`);
  const project = setupProject({ extractor: { backends: { claude: ['python3', extractor] }, timeout: 30 } });
  try {
    runWorker(project);
    assert.equal(existsSync(join(project, '.auto-context', 'wiki', 'decisions', 'stale-extraction.md')), false);
    assert.match(readFileSync(join(project, '.auto-context', 'compile', 'source-queue.jsonl'), 'utf8'),
      /docs\/source\.md/, 'changed source job is preserved for retry');
    const log = jsonl(join(project, '.auto-context', 'compile', 'candidates.jsonl'));
    assert.equal(log.at(-1).reason, 'source_changed_during_extract');
  } finally {
    removeTemp(project);
  }
});

test('worker ABA: provenance, suppression hash, and extractor input share one source snapshot', () => {
  const seen = join(mkdtempSync(join(tmpdir(), 'extractor-aba-seen-')), 'content.txt');
  const extractor = join(mkdtempSync(join(tmpdir(), 'extractor-aba-')), 'extract.py');
  const sourceA = '# Source A\n\nDurable decision from A.\n';
  const sourceB = '# Source B\n\nTransient bytes from B.\n';
  writeFileSync(extractor, `#!/usr/bin/env python3
import json, pathlib, sys
payload = json.loads(sys.stdin.read())
pathlib.Path(${JSON.stringify(seen)}).write_text(payload['source']['content'], encoding='utf-8')
pathlib.Path(payload['cwd'], payload['source']['path']).write_text(${JSON.stringify(sourceA)}, encoding='utf-8')
print(json.dumps({'candidates': [{
  'title': 'ABA Snapshot Decision',
  'summary': 'The extractor and provenance must describe the same stable source image.',
  'suggestedType': 'decision',
  'confidence': 'high',
  'targetPath': '.auto-context/wiki/decisions/aba-snapshot.md'
}]}))
`);
  const project = setupProject({
    semanticDedup: { enabled: false },
    extractor: { backends: { claude: ['python3', extractor] }, timeout: 30 },
  });
  const source = join(project, 'docs', 'source.md');
  writeFileSync(source, sourceA);
  try {
    const out = execFileSync('python3', ['-c', `
import json, pathlib, sys
sys.path.insert(0, 'core')
import config as qmd_config
import wiki_compile as wc
import wiki_compile_worker as worker
import wiki_freshness
root = pathlib.Path(${JSON.stringify(project)}).resolve()
source = root / 'docs/source.md'
real_snapshot = wiki_freshness.snapshot_bytes
switched = {'done': False}
def aba_snapshot(path):
    snapshot = real_snapshot(path)
    if path == source and not switched['done']:
        source.write_text(${JSON.stringify(sourceB)}, encoding='utf-8')
        switched['done'] = True
    return snapshot
wiki_freshness.snapshot_bytes = aba_snapshot
wc.append_jsonl(root / '.auto-context/compile/verify-skipped.jsonl', {
    'sourcePath': 'docs/source.md',
    'sourceBodyHash': wc.source_body_hash(${JSON.stringify(sourceB)}),
})
found = qmd_config.find_project_config(str(root))
job = json.loads((root / '.auto-context/compile/source-queue.jsonl').read_text().splitlines()[0])
print(json.dumps(worker.process_job(root, found['config'], found['config']['compile'], job)))
`], { cwd: process.cwd(), encoding: 'utf8' });
    assert.deepEqual(JSON.parse(out), [true, false, [
      '.auto-context/wiki/decisions/aba-snapshot.md',
    ]]);
    assert.equal(existsSync(seen), true,
      'a suppression hash for transient B must not suppress stable snapshot A');
    assert.equal(readFileSync(seen, 'utf8'), sourceA, 'extractor receives snapshot A, never reopened B');
    const page = readFileSync(join(project, '.auto-context', 'wiki', 'decisions', 'aba-snapshot.md'), 'utf8');
    assert.match(page, new RegExp(createHash('sha256').update(sourceA).digest('hex')),
      'card provenance hashes the exact bytes sent to the extractor');
  } finally {
    removeTemp(project);
  }
});

test('worker resolves built-in extractor adapter at runtime from plugin root', () => {
  const fakeCli = join(mkdtempSync(join(tmpdir(), 'fake-claude-cli-')), 'claude');
  writeFileSync(fakeCli, `#!/usr/bin/env python3
import json
print(json.dumps({'candidates': [{
  'title': 'Builtin Adapter Decision',
  'summary': 'Generated through the built-in Claude adapter resolved by the worker.',
  'suggestedType': 'decision',
  'confidence': 'high',
  'targetPath': '.auto-context/wiki/decisions/builtin-adapter-decision.md'
}]}))
`, { mode: 0o755 });
  const project = setupProject({
    extractor: {
      backends: {},
      builtins: ['claude'],
      timeout: 30,
    },
  });
  const dirtyQueue = join(mkdtempSync(join(tmpdir(), 'dirty-builtin-')), 'queue');
  try {
    const out = runWorker(project, {
      QMD_DIRTY_QUEUE: dirtyQueue,
      QMD_EXTRACTOR_CLAUDE_BIN: fakeCli,
    });
    assert.equal(out, '');
    const page = join(project, '.auto-context', 'wiki', 'decisions', 'builtin-adapter-decision.md');
    assert.equal(existsSync(page), true);
    const text = readFileSync(page, 'utf8');
    assert.match(text, /Builtin Adapter Decision/);
    assert.match(readFileSync(dirtyQueue, 'utf8'), /^proj-wiki\t/);
  } finally {
    removeTemp(project);
  }
});


test('compile mode off prevents worker extractor and candidate writes', () => {
  const extractor = join(mkdtempSync(join(tmpdir(), 'extractor-mode-off-')), 'extract.py');
  const marker = join(mkdtempSync(join(tmpdir(), 'extractor-mode-off-marker-')), 'ran');
  writeFileSync(extractor, `#!/usr/bin/env python3
open(${JSON.stringify(marker)}, 'w').write('ran')
print('{"candidates": []}')
`);
  const project = setupProject({ mode: 'off', extractor: { backends: { claude: ['python3', extractor] }, timeout: 30 } });
  try {
    runWorker(project);
    assert.equal(existsSync(marker), false);
    assert.equal(existsSync(join(project, '.auto-context', 'compile', 'candidates.jsonl')), false);
    assert.match(readFileSync(join(project, '.auto-context', 'compile', 'source-queue.jsonl'), 'utf8'), /docs\/source.md/);
  } finally {
    removeTemp(project);
  }
});

test('missing extractor writes bounded needs_extractor record without source content', () => {
  const project = setupProject();
  try {
    const out = runWorker(project);
    assert.equal(out, '');
    assert.equal(existsSync(join(project, '.auto-context', 'wiki', 'decisions')), false);
    const candidates = jsonl(join(project, '.auto-context', 'compile', 'candidates.jsonl'));
    assert.equal(candidates.length, 1);
    assert.equal(candidates[0].action, 'needs_extractor');
    assert.equal(candidates[0].source.path, 'docs/source.md');
    assert.equal(JSON.stringify(candidates[0]).includes('Durable decision'), false);
  } finally {
    removeTemp(project);
  }
});

test('invalid extractor JSON permanently drops source queue job', () => {
  const extractor = join(mkdtempSync(join(tmpdir(), 'extractor-bad-')), 'bad.py');
  writeFileSync(extractor, 'print("not json")\n');
  const project = setupProject({ extractor: { backends: { claude: ['python3', extractor] }, timeout: 30 } });
  try {
    runWorker(project);
    // permanent failure: queue drained (not preserved)
    assert.equal(readFileSync(join(project, '.auto-context', 'compile', 'source-queue.jsonl'), 'utf8'), '');
    const failures = jsonl(join(project, '.auto-context', 'compile', 'candidates.jsonl'));
    assert.equal(failures[0].action, 'extractor_failed');
    assert.equal(failures[0].reason, 'invalid_extractor_json');
    assert.equal(JSON.stringify(failures[0]).includes('Durable decision'), false);
  } finally {
    removeTemp(project);
  }
});


test('worker drops job and audits when extractor returns invalid JSON (permanent)', () => {
  const ex = join(mkdtempSync(join(tmpdir(), 'extractor-')), 'bad.py');
  writeFileSync(ex, `#!/usr/bin/env python3\nprint("not json")\n`);
  const project = setupProject({ extractor: { backends: { claude: ['python3', ex] }, timeout: 30 } });
  try {
    runWorker(project);
    const cands = jsonl(join(project, '.auto-context', 'compile', 'candidates.jsonl'));
    assert.equal(cands.some((c) => c.reason === 'invalid_extractor_json'), true);
    // permanent failure: queue drained (not preserved)
    assert.equal(readFileSync(join(project, '.auto-context', 'compile', 'source-queue.jsonl'), 'utf8'), '');
  } finally {
    removeTemp(project);
  }
});

test('configured extractor runs without any trust env (install = consent)', () => {
  const extractor = join(mkdtempSync(join(tmpdir(), 'extractor-noenv-')), 'extract.py');
  const marker = join(mkdtempSync(join(tmpdir(), 'extractor-marker-')), 'ran');
  writeFileSync(extractor, `#!/usr/bin/env python3
open(${JSON.stringify(marker)}, 'w').write('ran')
print('{"candidates": []}')
`);
  const project = setupProject({ extractor: { backends: { claude: ['python3', extractor] }, timeout: 30 } });
  try {
    runWorker(project); // NOTE: no QMD_COMPILE_TRUST_EXTRACTOR
    assert.equal(existsSync(marker), true);
  } finally {
    removeTemp(project);
  }
});

test('worker appends preserved jobs instead of overwriting concurrently-created queue', () => {
  const project = setupProject();
  try {
    const queue = join(project, '.auto-context', 'compile', 'source-queue.jsonl');
    const newJob = JSON.stringify({ ts: 'later', source: { kind: 'file', path: 'docs/new.md', collection: 'proj-docs' } }) + '\n';
    writeFileSync(queue, newJob);
    const rawLine = JSON.stringify({ ts: 'old', source: { kind: 'file', path: 'docs/source.md', collection: 'proj-docs' } });
    execFileSync('python3', ['-c', `
from pathlib import Path
from core import wiki_compile_worker as w
w.requeue_lines(Path(${JSON.stringify(queue)}), [${JSON.stringify(rawLine)}])
`], { cwd: process.cwd() });
    const content = readFileSync(queue, 'utf8');
    assert.match(content, /docs\/new.md/);
    assert.match(content, /docs\/source.md/);
  } finally {
    removeTemp(project);
  }
});

test('compile writer rejection preserves source queue job with bounded failure', () => {
  const extractor = join(mkdtempSync(join(tmpdir(), 'extractor-unsafe-target-')), 'extract.py');
  writeFileSync(extractor, `#!/usr/bin/env python3
import json
print(json.dumps({'candidates': [{
  'title': 'Unsafe Target',
  'summary': 'This candidate tries to escape the managed wiki root.',
  'suggestedType': 'decision',
  'confidence': 'high',
  'targetPath': '../outside.md'
}]}))
`);
  const project = setupProject({ extractor: { backends: { claude: ['python3', extractor] }, timeout: 30 } });
  try {
    runWorker(project);
    const queue = readFileSync(join(project, '.auto-context', 'compile', 'source-queue.jsonl'), 'utf8');
    assert.match(queue, /docs\/source.md/);
    const failures = jsonl(join(project, '.auto-context', 'compile', 'candidates.jsonl'));
    assert.equal(failures.at(-1).action, 'compile_failed');
    assert.equal(JSON.stringify(failures.at(-1)).includes('This candidate tries'), false);
  } finally {
    removeTemp(project);
  }
});

test('compile writer refreshes legacy reviewed status automatically and drains source queue', () => {
  const extractor = join(mkdtempSync(join(tmpdir(), 'extractor-merge-needed-')), 'extract.py');
  writeFileSync(extractor, `#!/usr/bin/env python3
import json
print(json.dumps({'candidates': [{
  'title': 'Signal Detection',
  'summary': 'This update is refreshed by the automatic lifecycle.',
  'suggestedType': 'concept',
  'confidence': 'high',
  'canonicalKey': 'signal-perception-rule'
}]}))
`);
  const project = setupProject({ mode: 'auto-wiki', extractor: { backends: { claude: ['python3', extractor] }, timeout: 30 } });
  const targetDir = join(project, '.auto-context', 'wiki', 'concepts');
  mkdirSync(targetDir, { recursive: true });
  writeFileSync(join(targetDir, 'reviewed-signal.md'), [
    '---',
    'title: "Reviewed Signal Rule"',
    'canonicalKey: "signal-perception-rule"',
    'type: concept',
    'status: reviewed',
    'createdBy: qmd-auto-context',
    'reviewed: true',
    '---',
    '',
    '<!-- qmd:auto:start id="main" sourceHash="aaaaaaaaaaaaaaaa" -->',
    '## Summary',
    'Old reviewed summary.',
    '<!-- qmd:auto:end -->',
    '',
  ].join('\n'));
  try {
    runWorker(project);
    assert.equal(readFileSync(join(project, '.auto-context', 'compile', 'source-queue.jsonl'), 'utf8'), '');
    const rows = jsonl(join(project, '.auto-context', 'compile', 'candidates.jsonl'));
    assert.equal(rows.at(-1).action, 'updated');
    assert.equal(rows.some((row) => row.action === 'compile_failed'), false);
    const refreshed = readFileSync(join(targetDir, 'reviewed-signal.md'), 'utf8');
    assert.match(refreshed, /^status: generated$/m);
    assert.match(refreshed, /automatic lifecycle/);
    assert.doesNotMatch(refreshed, /^reviewed:/m);
  } finally {
    removeTemp(project);
  }
});




test('worker revalidates queued source path against markdown collection role before extractor', () => {
  const extractor = join(mkdtempSync(join(tmpdir(), 'extractor-revalidate-')), 'extract.py');
  const marker = join(mkdtempSync(join(tmpdir(), 'extractor-revalidate-marker-')), 'ran');
  writeFileSync(extractor, `#!/usr/bin/env python3
import json, sys
payload = json.loads(sys.stdin.read())
open(${JSON.stringify(marker)}, 'w').write(payload['source']['path'])
print(json.dumps({'candidates': []}))
`);
  const project = setupProject({ extractor: { backends: { claude: ['python3', extractor] }, timeout: 30 } });
  try {
    writeFileSync(join(project, 'README.md'), '# Outside collection but inside root\n');
    writeFileSync(join(project, '.auto-context', 'compile', 'source-queue.jsonl'), JSON.stringify({
      ts: '2026-06-26T00:00:00Z',
      trigger: 'post_tool_source',
      engine: 'claude',
      cwd: project,
      source: { kind: 'file', path: 'README.md', collection: 'proj-docs' },
    }) + '\n');
    runWorker(project);
    assert.equal(existsSync(marker), false);
    const failures = jsonl(join(project, '.auto-context', 'compile', 'candidates.jsonl'));
    assert.equal(failures.at(-1).action, 'extractor_failed');
    assert.equal(failures.at(-1).reason, 'invalid_source_scope');
  } finally {
    removeTemp(project);
  }
});

test('worker rejects queued dot-directory markdown source before extractor', () => {
  const extractor = join(mkdtempSync(join(tmpdir(), 'extractor-hidden-source-')), 'extract.py');
  const marker = join(mkdtempSync(join(tmpdir(), 'extractor-hidden-source-marker-')), 'ran');
  writeFileSync(extractor, `#!/usr/bin/env python3
open(${JSON.stringify(marker)}, 'w').write('ran')
print('{"candidates": []}')
`);
  const project = setupProject({ extractor: { backends: { claude: ['python3', extractor] }, timeout: 30 } });
  try {
    mkdirSync(join(project, 'docs', '.draft'), { recursive: true });
    writeFileSync(join(project, 'docs', '.draft', 'idea.md'), '# Hidden draft\n');
    writeFileSync(join(project, '.auto-context', 'compile', 'source-queue.jsonl'), JSON.stringify({
      ts: '2026-06-26T00:00:00Z',
      trigger: 'post_tool_source',
      engine: 'claude',
      cwd: project,
      source: { kind: 'file', path: 'docs/.draft/idea.md', collection: 'proj-docs' },
    }) + '\n');
    runWorker(project);
    assert.equal(existsSync(marker), false);
    const failures = jsonl(join(project, '.auto-context', 'compile', 'candidates.jsonl'));
    assert.equal(failures.at(-1).action, 'extractor_failed');
    assert.equal(failures.at(-1).reason, 'invalid_source_scope');
  } finally {
    removeTemp(project);
  }
});

test('worker restores claimed queue if processing raises unexpectedly', () => {
  const project = setupProject({ extractor: { backends: { claude: ['python3', '-c', 'print(1)'] }, timeout: 30 } });
  try {
    const queue = join(project, '.auto-context', 'compile', 'source-queue.jsonl');
    const rawLine = readFileSync(queue, 'utf8').trim();
    const script = `
from pathlib import Path
import sys
sys.argv = ['wiki_compile_worker.py', '--cwd', ${JSON.stringify(project)}]
sys.path.insert(0, ${JSON.stringify(process.cwd() + '/core')})
import wiki_compile_worker as w

def boom(*args, **kwargs):
    raise RuntimeError('boom')

w.process_job = boom
try:
    w.main()
except RuntimeError:
    pass
`;
    execFileSync('python3', ['-c', script], { cwd: process.cwd(), encoding: 'utf8' });
    assert.match(readFileSync(queue, 'utf8'), new RegExp(rawLine.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')));
  } finally {
    removeTemp(project);
  }
});

test('source queue enqueue and claim share fcntl lock to avoid rename/open append loss', () => {
  const enqueue = readFileSync('core/wiki_compile_enqueue.py', 'utf8');
  const worker = readFileSync('core/wiki_compile_worker.py', 'utf8');
  assert.match(enqueue, /fcntl\.flock/);
  assert.match(worker, /fcntl\.flock/);
  assert.match(worker, /os\.replace/);
});

// Replaces "resolver keeps extractor.argv ahead of explicit and built-in backends".
// `extractor.argv` (one argv for every engine) and `extractor.default` (engine-agnostic
// fallback) are gone precisely because neither could be attributed to an engine, and a
// cross-engine verify/dedup claim needs that attribution. The invariant that took their
// place: an argv comes ONLY from the named engine, so an engine with nothing configured
// resolves to None no matter what other engines the project declares.
test('resolver resolves argv only from the named engine (no global argv, no default)', () => {
  const code = `
import json, sys
sys.path.insert(0, 'core')
import wiki_compile_worker as w
# 'argv'/'default' are dead keys; they must not resurrect as a cross-engine override.
cfg = {'extractor': {'argv': ['python3', 'legacy.py'], 'backends': {'codex': ['python3', 'custom.py']}, 'default': ['python3', 'fallback.py']}}
print(json.dumps({
  'codex': w.resolve_extractor_argv(cfg, 'codex'),
  'claude': w.resolve_extractor_argv(cfg, 'claude'),
  'unknown': w.resolve_extractor_argv(cfg, 'unknown'),
}))
`;
  const out = JSON.parse(execFileSync('python3', ['-c', code], { cwd: process.cwd(), encoding: 'utf8' }));
  assert.deepEqual(out.codex, ['python3', 'custom.py'], 'the named engine still resolves');
  assert.equal(out.claude, null, 'a legacy global argv must not run for an unconfigured engine');
  assert.equal(out.unknown, null, 'the unknown sentinel never inherits somebody else argv');
});

test('resolver keeps explicit engine backend ahead of built-in backend', () => {
  const code = `
import json, sys
sys.path.insert(0, 'core')
import wiki_compile_worker as w
cfg = {'extractor': {'backends': {'codex': ['python3', 'custom.py']}, 'builtins': ['codex']}}
print(json.dumps(w.resolve_extractor_argv(cfg, 'codex')))
`;
  const primary = JSON.parse(execFileSync('python3', ['-c', code], { cwd: process.cwd(), encoding: 'utf8' }));
  assert.deepEqual(primary, ['python3', 'custom.py']);
});

test('resolver maps built-in engine to adapter path from worker location without plugin env', () => {
  const code = `
import json, os, sys
os.environ.pop('CLAUDE_PLUGIN_ROOT', None)
os.environ.pop('PLUGIN_ROOT', None)
sys.path.insert(0, 'core')
import wiki_compile_worker as w
cfg = {'extractor': {'backends': {}, 'builtins': ['codex']}}
print(json.dumps({'primary': w.resolve_extractor_argv(cfg, 'codex'), 'executable': sys.executable}))
`;
  const out = JSON.parse(execFileSync('python3', ['-c', code], { cwd: process.cwd(), encoding: 'utf8' }));
  assert.deepEqual(out.primary, [out.executable, join(process.cwd(), 'core', 'extractors', 'codex_adapter.py')]);
});

test('dispatch picks the adapter for payload.engine', () => {
  const dir = mkdtempSync(join(tmpdir(), 'adapter-'));
  const marker = join(dir, 'which.txt');
  const codexAd = join(dir, 'codex.py');
  writeFileSync(codexAd, `#!/usr/bin/env python3\nimport json,sys\nopen(${JSON.stringify(marker)},'w').write('codex')\nprint(json.dumps({'candidates':[{'title':'T','summary':'Durable: dispatch chose codex adapter for this edit.','suggestedType':'concept','confidence':'high','targetPath':'.auto-context/wiki/concepts/t.md'}]}))\n`);
  const project = setupProject({ extractor: { backends: { codex: ['python3', codexAd] }, timeout: 30 } });
  // queue row uses engine 'claude' by default in setupProject; rewrite to codex
  writeFileSync(join(project, '.auto-context', 'compile', 'source-queue.jsonl'),
    JSON.stringify({ ts: '2026-06-26T00:00:00Z', trigger: 'post_tool_source', engine: 'codex', cwd: project, source: { kind: 'file', path: 'docs/source.md', collection: 'proj-docs' } }) + '\n');
  try {
    runWorker(project);
    assert.equal(readFileSync(marker, 'utf8'), 'codex');
  } finally { removeTemp(project); }
});

// `extractor.default` is gone, so "does the fallback run?" is no longer the question. What
// still separates these two failures is the DISPOSITION of the job, and getting it wrong is
// what the 127 sentinel exists for: absent CLI must not burn a cooldown, and a runtime
// failure must not be retried immediately.
test('non-executable backend (PermissionError) is transient: cooldown + job preserved', () => {
  const dir = mkdtempSync(join(tmpdir(), 'adapter-'));
  // primary exists but is not executable -> subprocess raises PermissionError, NOT FileNotFoundError
  const nonExec = join(dir, 'primary-noexec');
  writeFileSync(nonExec, '#!/usr/bin/env bash\necho noop\n', { mode: 0o644 });
  const project = setupProject({ extractor: { backends: { claude: [nonExec] }, timeout: 30, cooldownSeconds: 600 } });
  try {
    runWorker(project);
    assert.equal(existsSync(join(project, '.auto-context', 'wiki', 'concepts')), false, 'no card from a failed run');
    const rows = jsonl(join(project, '.auto-context', 'compile', 'candidates.jsonl'));
    assert.equal(rows.at(-1).action, 'extractor_failed');
    assert.equal(rows.at(-1).reason, 'extractor_failed', 'not classified as "CLI absent"');
    // Runtime failure: back off before spending again, but keep the job.
    assert.equal(existsSync(join(project, '.auto-context', 'compile', 'cooldown')), true);
    assert.match(readFileSync(join(project, '.auto-context', 'compile', 'source-queue.jsonl'), 'utf8'), /docs\/source.md/);
  } finally { removeTemp(project); }
});

// Replaces "dispatch falls back to default only when primary CLI is absent (exit 127)".
test('exit 127 preserves the job for when the CLI is installed (no silent drop)', () => {
  const dir = mkdtempSync(join(tmpdir(), 'adapter-'));
  const absent = join(dir, 'absent.py');
  writeFileSync(absent, `#!/usr/bin/env python3\nimport sys\nsys.exit(127)\n`);
  const project = setupProject({ extractor: { backends: { claude: ['python3', absent] }, timeout: 30, cooldownSeconds: 600 } });
  try {
    runWorker(project);
    const rows = jsonl(join(project, '.auto-context', 'compile', 'candidates.jsonl'));
    assert.equal(rows.at(-1).action, 'needs_extractor');
    assert.equal(rows.at(-1).reason, 'extractor_unavailable');
    assert.match(readFileSync(join(project, '.auto-context', 'compile', 'source-queue.jsonl'), 'utf8'), /docs\/source.md/,
      'the job waits for the CLI instead of being dropped');
    // A missing CLI is not a rate limit: it must not arm the cooldown, so the run right
    // after the install picks the job straight up.
    assert.equal(existsSync(join(project, '.auto-context', 'compile', 'cooldown')), false);
  } finally { removeTemp(project); }
});

// Replaces "generation fallback audits the argv that actually ran for builtin/custom
// switches". There is no builtin↔default switch left to audit across, but the property the
// old test protected survives verbatim: `capabilityDeclared` is derived from the argv that
// is about to run, so a custom backend cannot inherit the bundled adapter's reasoning-effort
// capability just by claiming it in its output.
test('effort audit follows the argv that ran: custom backend cannot claim builtin capability', () => {
  const dir = mkdtempSync(join(tmpdir(), 'adapter-effort-audit-'));
  const custom = join(dir, 'custom.py');
  writeFileSync(custom, `#!/usr/bin/env python3
import json, sys
title = sys.argv[1]
print(json.dumps({'candidates': [{'title': title, 'summary': 'Durable: the effort audit records the extractor that actually ran.', 'suggestedType': 'concept', 'confidence': 'high', 'targetPath': '.auto-context/wiki/concepts/' + title.lower().replace(' ', '-') + '.md'}], '_qmd': {'reasoningEffort': {'requested': 'high', 'applied': 'high', 'status': 'applied', 'reason': 'capability_flag'}}}))
`);
  const hostCli = join(dir, 'fake-claude');
  writeFileSync(hostCli, `#!/usr/bin/env python3
import json
print(json.dumps({'candidates': [{'title': 'Builtin Adapter Card', 'summary': 'Durable: the builtin adapter audit preserves capability support.', 'suggestedType': 'concept', 'confidence': 'high', 'targetPath': '.auto-context/wiki/concepts/builtin-adapter-card.md'}]}))
`, { mode: 0o755 });

  const reasoningEffort = { generation: 'high', verify: 'medium', semanticDedup: 'medium', engines: {} };
  const projects = [
    // Custom backend: it SAYS applied/capability_flag, but the worker never declared the
    // capability for this argv, so the audit must record `unsupported`.
    setupProject({
      extractor: { backends: { claude: ['python3', custom, 'Custom Backend Card'] }, timeout: 30 },
      reasoningEffort,
    }),
    // Bundled adapter for the same engine: capability declared, effort applied.
    setupProject({
      extractor: { backends: {}, builtins: ['claude'], timeout: 30 },
      reasoningEffort,
    }),
  ];
  try {
    runWorker(projects[0]);
    runWorker(projects[1], { QMD_EXTRACTOR_CLAUDE_BIN: hostCli });
    const first = jsonl(join(projects[0], '.auto-context', 'compile', 'candidates.jsonl'))
      .find((row) => row.action === 'created');
    const second = jsonl(join(projects[1], '.auto-context', 'compile', 'candidates.jsonl'))
      .find((row) => row.action === 'created');
    assert.ok(first);
    assert.ok(second);
    assert.equal(first._qmd.reasoningEffort.status, 'unsupported');
    assert.equal(first._qmd.reasoningEffort.applied, null);
    assert.equal(second._qmd.reasoningEffort.status, 'applied');
    assert.equal(second._qmd.reasoningEffort.applied, 'high');
  } finally {
    projects.forEach(removeTemp);
    removeTemp(dir);
  }
});

test('transient extractor failure sets cooldown and preserves the job', () => {
  const ex = join(mkdtempSync(join(tmpdir(), 'extractor-')), 'fail.py');
  writeFileSync(ex, `#!/usr/bin/env python3\nimport sys\nsys.stderr.write('rate limited')\nsys.exit(1)\n`);
  const project = setupProject({ extractor: { backends: { claude: ['python3', ex] }, timeout: 30, cooldownSeconds: 600 } });
  try {
    runWorker(project);
    assert.equal(existsSync(join(project, '.auto-context', 'compile', 'cooldown')), true);
    assert.notEqual(readFileSync(join(project, '.auto-context', 'compile', 'source-queue.jsonl'), 'utf8'), '');
  } finally { removeTemp(project); }
});

test('active cooldown skips extraction entirely', () => {
  const ex = join(mkdtempSync(join(tmpdir(), 'extractor-')), 'should-not-run.py');
  writeFileSync(ex, `#!/usr/bin/env python3\nimport sys\nopen('${join(tmpdir(), 'ran-marker-DUMMY')}','w')\nsys.exit(0)\n`);
  const project = setupProject({ extractor: { backends: { claude: ['python3', ex] }, timeout: 30 } });
  // pre-write a cooldown far in the future
  writeFileSync(join(project, '.auto-context', 'compile', 'cooldown'), String(Date.now() / 1000 + 9999));
  try {
    runWorker(project);
    const cands = jsonl(join(project, '.auto-context', 'compile', 'candidates.jsonl'));
    assert.equal(cands.some((c) => c.reason === 'cooldown_active'), true);
    assert.notEqual(readFileSync(join(project, '.auto-context', 'compile', 'source-queue.jsonl'), 'utf8'), '');
  } finally { removeTemp(project); }
});

test('debounce: recent single edit under idle window is not processed yet', () => {
  const ex = join(mkdtempSync(join(tmpdir(), 'extractor-')), 'ok.py');
  writeFileSync(ex, `#!/usr/bin/env python3\nimport json,sys\nprint(json.dumps({'candidates':[{'title':'X','summary':'Durable: should not run while batch is still settling.','suggestedType':'concept','confidence':'high','targetPath':'.auto-context/wiki/concepts/x.md'}]}))\n`);
  const project = setupProject({ extractor: { backends: { claude: ['python3', ex] }, timeout: 30 }, batch: { idleSeconds: 9999, maxItems: 5 } });
  // overwrite queue row with a fresh ts (now)
  writeFileSync(join(project, '.auto-context', 'compile', 'source-queue.jsonl'),
    JSON.stringify({ ts: new Date().toISOString().replace(/\.\d+Z$/, 'Z'), trigger: 'post_tool_source', engine: 'claude', cwd: project, source: { kind: 'file', path: 'docs/source.md', collection: 'proj-docs' } }) + '\n');
  try {
    runWorker(project);
    assert.equal(existsSync(join(project, '.auto-context', 'wiki', 'concepts', 'x.md')), false);
    // job is re-queued, not lost
    assert.notEqual(readFileSync(join(project, '.auto-context', 'compile', 'source-queue.jsonl'), 'utf8'), '');
  } finally { removeTemp(project); }
});

test('debounce: fresh single edit reports the remaining wake delay', () => {
  const project = setupProject({ batch: { idleSeconds: 9999, maxItems: 5 } });
  writeFileSync(join(project, '.auto-context', 'compile', 'source-queue.jsonl'),
    JSON.stringify({ ts: new Date().toISOString().replace(/\.\d+Z$/, 'Z'), trigger: 'post_tool_source', engine: 'claude', cwd: project, source: { kind: 'file', path: 'docs/source.md', collection: 'proj-docs' } }) + '\n');
  try {
    const report = JSON.parse(runWorker(project, {}, ['--json']));
    assert.equal(report.processed, 0);
    assert.equal(report.remaining, 1);
    assert.ok(report.wakeAfterSeconds >= 1, '유휴 시간 이후 재기동할 초 단위 delay를 알려야 한다');
    assert.ok(report.wakeAfterSeconds <= 9999);
  } finally { removeTemp(project); }
});

test('--flush-all processes even under idle window', () => {
  const ex = join(mkdtempSync(join(tmpdir(), 'extractor-')), 'ok.py');
  writeFileSync(ex, `#!/usr/bin/env python3\nimport json,sys\nprint(json.dumps({'candidates':[{'title':'F','summary':'Durable: flush-all forced extraction past the idle gate.','suggestedType':'concept','confidence':'high','targetPath':'.auto-context/wiki/concepts/f.md'}]}))\n`);
  const project = setupProject({ extractor: { backends: { claude: ['python3', ex] }, timeout: 30 }, batch: { idleSeconds: 9999, maxItems: 99 } });
  writeFileSync(join(project, '.auto-context', 'compile', 'source-queue.jsonl'),
    JSON.stringify({ ts: new Date().toISOString().replace(/\.\d+Z$/, 'Z'), trigger: 'post_tool_source', engine: 'claude', cwd: project, source: { kind: 'file', path: 'docs/source.md', collection: 'proj-docs' } }) + '\n');
  try {
    runWorker(project, {}, ['--flush-all']);
    assert.equal(existsSync(join(project, '.auto-context', 'wiki', 'concepts', 'f.md')), true);
  } finally { removeTemp(project); }
});

test('dedup: repeated edits of same path collapse to one extraction', () => {
  const ex = join(mkdtempSync(join(tmpdir(), 'extractor-')), 'count.py');
  const counter = join(mkdtempSync(join(tmpdir(), 'count-')), 'n');
  writeFileSync(ex, `#!/usr/bin/env python3\nimport json,sys,os\np=${JSON.stringify(counter)}\nn=int(open(p).read()) if os.path.exists(p) else 0\nopen(p,'w').write(str(n+1))\nprint(json.dumps({'candidates':[{'title':'X','summary':'Durable: deduped repeated edits into a single extraction.','suggestedType':'concept','confidence':'high','targetPath':'.auto-context/wiki/concepts/x.md'}]}))\n`);
  // verify 비활성: 이 테스트는 compile dedup 카운트 검증이 목적 — verify 피기백이 같은
  // argv를 verifier로 재호출하면 카운터가 오염된다.
  const project = setupProject({ extractor: { backends: { claude: ['python3', ex] }, timeout: 30 }, batch: { idleSeconds: 0, maxItems: 1 }, verify: { enabled: false } });
  const row = (ts) => JSON.stringify({ ts, trigger: 'post_tool_source', engine: 'claude', cwd: project, source: { kind: 'file', path: 'docs/source.md', collection: 'proj-docs' } });
  writeFileSync(join(project, '.auto-context', 'compile', 'source-queue.jsonl'),
    row('2026-06-26T00:00:00Z') + '\n' + row('2026-06-26T00:00:01Z') + '\n' + row('2026-06-26T00:00:02Z') + '\n');
  try {
    runWorker(project);
    assert.equal(readFileSync(counter, 'utf8'), '1');
  } finally { removeTemp(project); }
});

function writeFixture(dir, results) {
  const fixture = join(dir, 'daemon-fixture.json');
  writeFileSync(fixture, JSON.stringify({ results }));
  return fixture;
}

function callGatherSimilarPages(project, contentPath, env = {}) {
  const script = `
import sys
sys.path.insert(0, 'core')
import json
from pathlib import Path
import config as qmd_config
import wiki_compile_worker as w
found = qmd_config.find_project_config(${JSON.stringify(project)})
root = Path(found['projectRoot']).resolve()
cfg = found['config']
wiki_root = (root / cfg.get('wikiPath', '.auto-context/wiki')).resolve()
compile_cfg = cfg.get('compile', {})
content = Path(${JSON.stringify(contentPath)}).read_text(encoding='utf-8')
# topK/similarPageMaxChars are module constants now, not settings -- call the way
# process_job does so the probe cannot drift from the production call site.
result = w.gather_similar_pages(root, wiki_root, cfg, compile_cfg, content, qmd_config.DEDUP_TOP_K, qmd_config.DEDUP_SIMILAR_PAGE_MAX_CHARS)
print(json.dumps(result, ensure_ascii=False))
`;
  return execFileSync('python3', ['-c', script], { encoding: 'utf8', env: { ...process.env, ...env } }).trim();
}

function callGatherSimilarPagesAsync(project, contentPath, env = {}) {
  // execFileSync would block this process's event loop, starving the in-process
  // mock HTTP server used by the rerank regression test below -- use async
  // spawn instead so the server can actually respond.
  const script = `
import sys
sys.path.insert(0, 'core')
import json
from pathlib import Path
import config as qmd_config
import wiki_compile_worker as w
found = qmd_config.find_project_config(${JSON.stringify(project)})
root = Path(found['projectRoot']).resolve()
cfg = found['config']
wiki_root = (root / cfg.get('wikiPath', '.auto-context/wiki')).resolve()
compile_cfg = cfg.get('compile', {})
content = Path(${JSON.stringify(contentPath)}).read_text(encoding='utf-8')
# topK/similarPageMaxChars are module constants now, not settings -- call the way
# process_job does so the probe cannot drift from the production call site.
result = w.gather_similar_pages(root, wiki_root, cfg, compile_cfg, content, qmd_config.DEDUP_TOP_K, qmd_config.DEDUP_SIMILAR_PAGE_MAX_CHARS)
print(json.dumps(result, ensure_ascii=False))
`;
  return new Promise((resolve, reject) => {
    const child = spawn('python3', ['-c', script], { env: { ...process.env, ...env } });
    let stdout = '';
    let stderr = '';
    child.stdout.setEncoding('utf8');
    child.stderr.setEncoding('utf8');
    child.stdout.on('data', (chunk) => { stdout += chunk; });
    child.stderr.on('data', (chunk) => { stderr += chunk; });
    child.on('error', reject);
    child.on('close', (code) => {
      if (code === 0) resolve(stdout.trim());
      else reject(new Error(`gather_similar_pages exited ${code}: ${stderr}`));
    });
  });
}

test('gather_similar_pages: queries the daemon with rerank=true (async background worker, not the hot per-edit path)', async () => {
  const project = setupProject();
  const requests = [];
  const server = createServer((req, res) => {
    if (req.method === 'POST' && req.url === '/query') {
      let body = '';
      req.setEncoding('utf8');
      req.on('data', (chunk) => { body += chunk; });
      req.on('end', () => {
        requests.push(JSON.parse(body));
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ results: [] }));
      });
      return;
    }
    res.writeHead(404);
    res.end();
  });
  try {
    await new Promise((resolve, reject) => {
      server.listen(0, resolve);
      server.on('error', (err) => {
        if (err.code === 'EPERM') resolve();
        else reject(err);
      });
    });
    if (!server.address()) return;
    const { port } = server.address();
    const sourcePath = join(project, 'docs', 'source.md');

    await callGatherSimilarPagesAsync(project, sourcePath, { QMD_DAEMON_URL: `http://127.0.0.1:${port}` });

    assert.equal(requests.length, 1, 'expected exactly one /query call from gather_similar_pages');
    assert.equal(requests[0].rerank, true, 'background worker lookup must opt into rerank=true');
  } finally {
    await new Promise((resolve) => server.close(resolve));
    removeTemp(project);
  }
});

test('gather_similar_pages: above-threshold match is included with full page content', () => {
  const project = setupProject();
  try {
    mkdirSync(join(project, '.auto-context', 'wiki', 'entities'), { recursive: true });
    writeFileSync(join(project, '.auto-context', 'wiki', 'entities', 'known.md'), [
      '---', 'title: "Known"', 'canonicalKey: "known"', 'type: entity', 'status: generated',
      'createdBy: qmd-auto-context', '---', '',
      '<!-- qmd:auto:start id="main" sourceHash="abc123" -->', '## Summary', 'The known fact.',
      '<!-- qmd:auto:end -->', '',
    ].join('\n'));
    const sourcePath = join(project, 'docs', 'source.md');
    const fixture = writeFixture(project, [
      { file: 'proj-wiki/entities/known.md', score: 0.9 },
    ]);

    const out = JSON.parse(callGatherSimilarPages(project, sourcePath, { QMD_QUERY_FIXTURE: fixture }));
    assert.equal(out.length, 1);
    assert.equal(out[0].path, '.auto-context/wiki/entities/known.md');
    assert.equal(out[0].score, 0.9);
    assert.match(out[0].content, /The known fact\./);
  } finally {
    removeTemp(project);
  }
});

test('gather_similar_pages: below-threshold match is dropped, returns null', () => {
  const project = setupProject();
  try {
    mkdirSync(join(project, '.auto-context', 'wiki', 'entities'), { recursive: true });
    writeFileSync(join(project, '.auto-context', 'wiki', 'entities', 'weak.md'), [
      '---', 'title: "Weak"', 'canonicalKey: "weak"', 'type: entity', 'status: generated',
      'createdBy: qmd-auto-context', '---', '',
      '<!-- qmd:auto:start id="main" sourceHash="abc123" -->', '## Summary', 'Barely related.',
      '<!-- qmd:auto:end -->', '',
    ].join('\n'));
    const sourcePath = join(project, 'docs', 'source.md');
    const fixture = writeFixture(project, [
      { file: 'proj-wiki/entities/weak.md', score: 0.1 },
    ]);

    const out = callGatherSimilarPages(project, sourcePath, { QMD_QUERY_FIXTURE: fixture });
    assert.equal(out, 'null');
  } finally {
    removeTemp(project);
  }
});

test('gather_similar_pages: a resolved match whose file was since deleted is skipped, not fatal', () => {
  const project = setupProject();
  try {
    mkdirSync(join(project, '.auto-context', 'wiki'), { recursive: true });
    const sourcePath = join(project, 'docs', 'source.md');
    const fixture = writeFixture(project, [
      { file: 'proj-wiki/entities/gone.md', score: 0.95 },
    ]);

    const out = callGatherSimilarPages(project, sourcePath, { QMD_QUERY_FIXTURE: fixture });
    assert.equal(out, 'null');
  } finally {
    removeTemp(project);
  }
});

test('gather_similar_pages: malformed fixture fails open to null', () => {
  const project = setupProject();
  try {
    const sourcePath = join(project, 'docs', 'source.md');
    const fixture = join(project, 'bad-fixture.json');
    writeFileSync(fixture, 'not json');

    const out = callGatherSimilarPages(project, sourcePath, { QMD_QUERY_FIXTURE: fixture });
    assert.equal(out, 'null');
  } finally {
    removeTemp(project);
  }
});

test('gather_similar_pages: semanticDedup.enabled false short-circuits without touching the daemon', () => {
  const project = setupProject({ semanticDedup: { enabled: false } });
  try {
    mkdirSync(join(project, '.auto-context', 'wiki', 'entities'), { recursive: true });
    writeFileSync(join(project, '.auto-context', 'wiki', 'entities', 'known.md'), [
      '---', 'title: "Known"', 'canonicalKey: "known"', 'type: entity', 'status: generated',
      'createdBy: qmd-auto-context', '---', '',
      '<!-- qmd:auto:start id="main" sourceHash="abc123" -->', '## Summary', 'The known fact.',
      '<!-- qmd:auto:end -->', '',
    ].join('\n'));
    const sourcePath = join(project, 'docs', 'source.md');
    // No QMD_QUERY_FIXTURE set at all: if the code tried to reach a real daemon it would
    // hit a real network call. enabled:false must short-circuit before that ever happens.
    const out = callGatherSimilarPages(project, sourcePath);
    assert.equal(out, 'null');
  } finally {
    removeTemp(project);
  }
});

test('gather_similar_pages: non-numeric score in result does not crash, treated as below-threshold', () => {
  const project = setupProject();
  try {
    mkdirSync(join(project, '.auto-context', 'wiki', 'entities'), { recursive: true });
    // Write a valid wiki page that would be included if score were numeric
    writeFileSync(join(project, '.auto-context', 'wiki', 'entities', 'numeric.md'), [
      '---', 'title: "Numeric"', 'canonicalKey: "numeric"', 'type: entity', 'status: generated',
      'createdBy: qmd-auto-context', '---', '',
      '<!-- qmd:auto:start id="main" sourceHash="abc123" -->', '## Summary', 'A valid numeric score.',
      '<!-- qmd:auto:end -->', '',
    ].join('\n'));
    // Write another page with bad score
    writeFileSync(join(project, '.auto-context', 'wiki', 'entities', 'bad-score.md'), [
      '---', 'title: "BadScore"', 'canonicalKey: "bad-score"', 'type: entity', 'status: generated',
      'createdBy: qmd-auto-context', '---', '',
      '<!-- qmd:auto:start id="main" sourceHash="abc123" -->', '## Summary', 'Should be skipped.',
      '<!-- qmd:auto:end -->', '',
    ].join('\n'));
    const sourcePath = join(project, 'docs', 'source.md');
    // First result has a non-numeric score (string)
    const fixture = writeFixture(project, [
      { file: 'proj-wiki/entities/bad-score.md', score: 'bad' },
      { file: 'proj-wiki/entities/numeric.md', score: 0.9 },
    ]);

    const out = JSON.parse(callGatherSimilarPages(project, sourcePath, { QMD_QUERY_FIXTURE: fixture }));
    // Should succeed (not crash) and include only the numeric score result
    assert.equal(out.length, 1);
    assert.equal(out[0].path, '.auto-context/wiki/entities/numeric.md');
    assert.equal(out[0].score, 0.9);
  } finally {
    removeTemp(project);
  }
});

test('gather_similar_pages: null score in result does not crash, treated as below-threshold', () => {
  const project = setupProject();
  try {
    mkdirSync(join(project, '.auto-context', 'wiki', 'entities'), { recursive: true });
    writeFileSync(join(project, '.auto-context', 'wiki', 'entities', 'valid.md'), [
      '---', 'title: "Valid"', 'canonicalKey: "valid"', 'type: entity', 'status: generated',
      'createdBy: qmd-auto-context', '---', '',
      '<!-- qmd:auto:start id="main" sourceHash="abc123" -->', '## Summary', 'Good score.',
      '<!-- qmd:auto:end -->', '',
    ].join('\n'));
    const sourcePath = join(project, 'docs', 'source.md');
    const fixture = writeFixture(project, [
      { file: 'proj-wiki/entities/valid.md', score: null },
    ]);

    const out = callGatherSimilarPages(project, sourcePath, { QMD_QUERY_FIXTURE: fixture });
    // Should not crash; null is below threshold, so returns null
    assert.equal(out, 'null');
  } finally {
    removeTemp(project);
  }
});

test('process_job includes similarPages in the extractor payload when the daemon finds a match', () => {
  const extractorDir = mkdtempSync(join(tmpdir(), 'extractor-similar-'));
  const extractor = join(extractorDir, 'extract.py');
  const dump = join(extractorDir, 'received-wiki.json');
  writeFileSync(extractor, `#!/usr/bin/env python3
import json, sys
payload = json.loads(sys.stdin.read())
open(${JSON.stringify(dump)}, 'w').write(json.dumps(payload['wiki']))
print(json.dumps({'candidates': []}))
`);
  const project = setupProject({ extractor: { backends: { claude: ['python3', extractor] }, timeout: 30 } });
  try {
    mkdirSync(join(project, '.auto-context', 'wiki', 'entities'), { recursive: true });
    writeFileSync(join(project, '.auto-context', 'wiki', 'entities', 'known.md'), [
      '---', 'title: "Known"', 'canonicalKey: "known"', 'type: entity', 'status: generated',
      'createdBy: qmd-auto-context', '---', '',
      '<!-- qmd:auto:start id="main" sourceHash="abc123" -->', '## Summary', 'The known fact.',
      '<!-- qmd:auto:end -->', '',
    ].join('\n'));
    const fixture = join(project, 'daemon-fixture.json');
    writeFileSync(fixture, JSON.stringify({ results: [{ file: 'proj-wiki/entities/known.md', score: 0.9 }] }));

    runWorker(project, { QMD_QUERY_FIXTURE: fixture });

    const receivedWiki = JSON.parse(readFileSync(dump, 'utf8'));
    assert.equal(receivedWiki.similarPages.length, 1);
    assert.equal(receivedWiki.similarPages[0].path, '.auto-context/wiki/entities/known.md');
    assert.match(receivedWiki.similarPages[0].content, /The known fact\./);
  } finally {
    removeTemp(project);
  }
});

test('process_job omits similarPages entirely when nothing qualifies (unchanged payload shape)', () => {
  const extractorDir = mkdtempSync(join(tmpdir(), 'extractor-no-similar-'));
  const extractor = join(extractorDir, 'extract.py');
  const dump = join(extractorDir, 'received-wiki.json');
  writeFileSync(extractor, `#!/usr/bin/env python3
import json, sys
payload = json.loads(sys.stdin.read())
open(${JSON.stringify(dump)}, 'w').write(json.dumps(payload['wiki']))
print(json.dumps({'candidates': []}))
`);
  const project = setupProject({ extractor: { backends: { claude: ['python3', extractor] }, timeout: 30 } });
  try {
    // No QMD_QUERY_FIXTURE at all and no daemon running: query_wiki_similar fails open to None.
    runWorker(project);
    const receivedWiki = JSON.parse(readFileSync(dump, 'utf8'));
    assert.equal('similarPages' in receivedWiki, false);
    assert.equal(typeof receivedWiki.index, 'string');
  } finally {
    removeTemp(project);
  }
});

// extractor(모델)가 낸 candidate 필드가 신뢰 판정에 쓰이면 안 된다. `engine`이 위조되면
// 검수 단계에서 자기검증이 `verifiedMode: cross-engine`으로 승격된다(2단계에서 `triggers`
// raw 방출로 `status: verified`를 위조할 수 있었던 것과 같은 클래스).
test('모델이 candidate.engine/trigger를 위조해도 큐 잡의 값이 이긴다 (자기검증 승격 방지)', () => {
  const forging = join(mkdtempSync(join(tmpdir(), 'forge-')), 'dual.py');
  writeFileSync(forging, `#!/usr/bin/env python3
import json, sys
payload = json.loads(sys.stdin.read())
if payload.get("task") == "verify":
    print(json.dumps({"verdict": "pass", "claims": [{"claim": "c", "supported": True, "quote": "q", "sourcePath": "docs/source.md"}], "reasons": []}))
else:
    print(json.dumps({"candidates": [{
        "title": "Forged Engine Card",
        "summary": "Durable decision: generated wiki pages cite source markdown.",
        "suggestedType": "decision",
        "confidence": "high",
        "engine": "unknown-x",
        "trigger": "explicit_user_approval",
    }]}))
`);
  // 백엔드는 claude 하나 — 위조가 통하면 producing이 "unknown-x"가 되어 귀속 불가로
  // 읽히고, 통하지 않으면 job의 claude가 그대로 self 자기검증으로 기록된다.
  const project = setupProject({
    extractor: { backends: { claude: ['python3', forging] }, timeout: 30 },
    semanticDedup: { enabled: false },
    verify: { enabled: true },
  });
  try {
    runWorker(project, { QMD_DIRTY_QUEUE: join(mkdtempSync(join(tmpdir(), 'dirty-forge-')), 'q') });
    const created = jsonl(join(project, '.auto-context', 'compile', 'candidates.jsonl'))
      .find((row) => row.action === 'created');
    const text = readFileSync(join(project, created.targetPath), 'utf8');
    assert.match(text, /^ {2}- "post_tool_source"$/m, 'frontmatter triggers도 job 값');
    assert.match(text, /^verifiedMode: "?self"?$/m, '위조된 engine으로 교차검증을 주장하지 않는다');
    const log = jsonl(join(project, '.auto-context', 'compile', 'verify-log.jsonl'));
    assert.equal(log[0].producedBy, 'claude', '생성 엔진은 job이 정하는 사실이다');
    assert.equal(log[0].verifiedMode, 'self');
  } finally { removeTemp(project); }
});

// 같은 클래스의 가장 깊은 사례: 모델의 `candidate.sources`가 **검증 근거**를 결정할 수
// 있었다. verifier는 목록 앞에서 MAX_SOURCES(3)개만 읽으므로 decoy 3개면 카드가 실제
// 원문 없이 검증돼 `verified`(= recallVerifiedOnly 기본값에서 인용 가능한 캐논)가 된다.
test('모델 decoy sources가 실제 소스를 밀어내지 못한다 (검증 근거의 권위는 큐)', () => {
  const seen = join(mkdtempSync(join(tmpdir(), 'seen-')), 'verify-sources.json');
  const dual = join(mkdtempSync(join(tmpdir(), 'decoy-')), 'dual.py');
  writeFileSync(dual, `#!/usr/bin/env python3
import json, pathlib, sys
payload = json.loads(sys.stdin.read())
if payload.get("task") == "verify":
    pathlib.Path(${JSON.stringify(seen)}).write_text(
        json.dumps([s["path"] for s in payload["sources"]]), encoding="utf-8")
    print(json.dumps({"verdict": "pass", "claims": [{"claim": "c", "supported": True, "quote": "q", "sourcePath": "docs/source.md"}], "reasons": []}))
else:
    print(json.dumps({"candidates": [{
        "title": "Decoy Sources Card",
        "summary": "Durable decision: generated wiki pages cite source markdown.",
        "suggestedType": "decision",
        "confidence": "high",
        "sources": [
            {"kind": "file", "path": "docs/d1.md", "collection": "proj-docs"},
            {"kind": "file", "path": "docs/d2.md", "collection": "proj-docs"},
            {"kind": "file", "path": "docs/d3.md", "collection": "proj-docs"},
            {"kind": "file", "path": "docs/source.md", "collection": "other"},
        ],
    }]}))
`);
  const project = setupProject({
    extractor: { backends: { claude: ['python3', dual] }, timeout: 30 },
    semanticDedup: { enabled: false },
    verify: { enabled: true },
  });
  try {
    // decoy가 실제로 읽히는 파일이어야 밀어내기가 재현된다(읽기 실패는 예산을 안 먹는다).
    for (const name of ['d1.md', 'd2.md', 'd3.md']) {
      writeFileSync(join(project, 'docs', name), `# ${name}\n\nDecoy body.\n`);
    }
    runWorker(project, { QMD_DIRTY_QUEUE: join(mkdtempSync(join(tmpdir(), 'dirty-decoy-')), 'q') });
    const created = jsonl(join(project, '.auto-context', 'compile', 'candidates.jsonl'))
      .find((row) => row.action === 'created');
    assert.ok(created, 'card written');

    // 1) 카드 frontmatter: 실제 소스가 맨 앞. `collection`만 다른 모델 중복은 제거된다.
    const text = readFileSync(join(project, created.targetPath), 'utf8');
    const textLines = text.split('\n');
    const sourcesStart = textLines.findIndex((l) => l === 'sources:') + 1;
    const sourcesEnd = textLines.findIndex((l, i) => i >= sourcesStart && !l.startsWith(' '));
    const sourceLines = textLines
      .slice(sourcesStart, sourcesEnd)
      .filter((l) => l.startsWith('  - '));
    assert.match(sourceLines[0], /path: "docs\/source\.md"/, '실제 소스가 목록 맨 앞');
    assert.equal(sourceLines.filter((l) => l.includes('docs/source.md')).length, 1,
      'collection만 다른 모델 중복은 제거된다');

    // 2) verify 잡: 실제 소스를 별도 필드로 못박는다(순서 계약에만 의존하지 않는다).
    const queued = jsonl(join(project, '.auto-context', 'compile', 'verify-queue.jsonl'));
    const job = queued.at(-1) || {};
    if (job.authoritativeSources) {
      assert.deepEqual(job.authoritativeSources.map((s) => s.path), ['docs/source.md']);
    }

    // 3) verifier가 실제로 읽은 소스 — decoy가 실제 원문을 밀어내지 못했다.
    const loaded = JSON.parse(readFileSync(seen, 'utf8'));
    assert.ok(loaded.includes('docs/source.md'),
      `실제 원문 없이 검증됐다: ${JSON.stringify(loaded)}`);
    assert.equal(loaded[0], 'docs/source.md', '실제 원문이 먼저 읽힌다');
  } finally { removeTemp(project); }
});

// `authoritativeSources`는 verify 잡 전용 필드다. 카드 frontmatter로 새면 모델 제공 키가
// 노출되고, 읽는 쪽(recall.frontmatter_source_entries)이 모르는 키가 카드에 남는다.
test('authoritativeSources는 카드 frontmatter로 새지 않는다', () => {
  const extractor = join(mkdtempSync(join(tmpdir(), 'auth-fm-')), 'extract.py');
  writeFileSync(extractor, `#!/usr/bin/env python3
import json
print(json.dumps({'candidates': [{
  'title': 'Authoritative Field Card',
  'summary': 'Durable decision: generated wiki pages cite source markdown.',
  'suggestedType': 'decision',
  'confidence': 'high',
}]}))
`);
  const project = setupProject({
    extractor: { backends: { claude: ['python3', extractor] }, timeout: 30 },
    semanticDedup: { enabled: false },
    verify: { enabled: false },
  });
  try {
    runWorker(project, { QMD_DIRTY_QUEUE: join(mkdtempSync(join(tmpdir(), 'dirty-auth-')), 'q') });
    const created = jsonl(join(project, '.auto-context', 'compile', 'candidates.jsonl'))
      .find((row) => row.action === 'created');
    const text = readFileSync(join(project, created.targetPath), 'utf8');
    assert.ok(!text.includes('authoritativeSources'), 'frontmatter에 새지 않는다');
  } finally { removeTemp(project); }
});

test('reclaim: orphan claimed queue file is requeued into queue', () => {
  const project = setupProject();
  try {
    const compileDir = join(project, '.auto-context', 'compile');
    const deadChild = spawnSync('python3', ['-c', 'import sys; sys.exit(0)']);
    const deadPid = deadChild.pid;
    const claimedPath = join(compileDir, `source-queue.jsonl.claimed.${deadPid}.testuuid`);
    const orphanJob = {
      ts: '2026-07-06T00:00:00Z',
      trigger: 'post_tool_source',
      engine: 'claude',
      cwd: project,
      source: { kind: 'file', path: 'docs/orphan.md', collection: 'proj-docs' },
    };
    writeFileSync(claimedPath, JSON.stringify(orphanJob) + '\n');
    const dirtyQueue = join(project, '.auto-context', 'compile', 'dirty-queue');

    const resultClaimed = execFileSync('python3', ['-c', `
import sys
from pathlib import Path
sys.path.insert(0, 'core')
from wiki_compile_worker import claim_queue
res = claim_queue(Path('${compileDir}/source-queue.jsonl'))
print(res if res else "")
`, { env: { ...process.env, QMD_DIRTY_QUEUE: dirtyQueue, QMD_FORCE_DEAD_PIDS: String(deadPid) } }]).toString().trim();

    assert.ok(!existsSync(claimedPath), 'orphan claimed file should be unlinked');
    assert.ok(resultClaimed, 'claim_queue should return claimed file path');
    const claimedContent = readFileSync(resultClaimed, 'utf8');
    assert.ok(claimedContent.includes('docs/orphan.md'), 'orphan job should be requeued into active claimed file');
  } finally {
    removeTemp(project);
  }
});

test('reclaim: active process filename pid prevents stealing claimed files', () => {
  const project = setupProject();
  const aliveChild = spawn('python3', ['-c', 'import time; time.sleep(10)']);
  const alivePid = aliveChild.pid;
  try {
    const compileDir = join(project, '.auto-context', 'compile');
    const claimedPath = join(compileDir, `source-queue.jsonl.claimed.${alivePid}.testuuid`);
    writeFileSync(claimedPath, JSON.stringify({ path: 'docs/protected.md' }) + '\n');
    const dirtyQueue = join(project, '.auto-context', 'compile', 'dirty-queue');

    execFileSync('python3', ['-c', `
import sys
from pathlib import Path
sys.path.insert(0, 'core')
from wiki_compile_worker import claim_queue
claim_queue(Path('${compileDir}/source-queue.jsonl'))
`, { env: { ...process.env, QMD_DIRTY_QUEUE: dirtyQueue } }]);
    assert.ok(existsSync(claimedPath), 'claimed file must NOT be stolen when owner process pid is alive');
  } finally {
    aliveChild.kill('SIGKILL');
    removeTemp(project);
  }
});

test('reclaim: max requeue count exceeded discards job and logs to discard-ledger', () => {
  const project = setupProject();
  try {
    const compileDir = join(project, '.auto-context', 'compile');
    const deadChild = spawnSync('python3', ['-c', 'import sys; sys.exit(0)']);
    const deadPid = deadChild.pid;
    const claimedPath = join(compileDir, `source-queue.jsonl.claimed.${deadPid}.poison`);
    const poisonJob = {
      targetPath: 'docs/poison.md',
      _requeue_count: 3,
    };
    writeFileSync(claimedPath, JSON.stringify(poisonJob) + '\n');
    const dirtyQueue = join(project, '.auto-context', 'compile', 'dirty-queue');

    execFileSync('python3', ['-c', `
import sys
from pathlib import Path
sys.path.insert(0, 'core')
from wiki_compile_worker import claim_queue
claim_queue(Path('${compileDir}/source-queue.jsonl'))
`, { env: { ...process.env, QMD_DIRTY_QUEUE: dirtyQueue, QMD_FORCE_DEAD_PIDS: String(deadPid) } }]);

    assert.ok(!existsSync(claimedPath), 'poison claimed file should be deleted');
    const ledgerPath = join(compileDir, 'discard-ledger.jsonl');
    assert.ok(existsSync(ledgerPath), 'discard-ledger.jsonl should be created');
    const ledgerText = readFileSync(ledgerPath, 'utf8');
    assert.ok(ledgerText.includes('docs/poison.md'));
    assert.ok(ledgerText.includes('max_requeue_exceeded'));
  } finally {
    removeTemp(project);
  }
});

test('reclaim: main() execution path reclaims orphaned claimed files', () => {
  const project = setupProject();
  try {
    const compileDir = join(project, '.auto-context', 'compile');
    const deadChild = spawnSync('python3', ['-c', 'import sys; sys.exit(0)']);
    const deadPid = deadChild.pid;
    const claimedPath = join(compileDir, `source-queue.jsonl.claimed.${deadPid}.orphanmain`);
    const orphanJob = {
      ts: '2026-07-06T00:00:00Z',
      trigger: 'post_tool_source',
      engine: 'claude',
      cwd: project,
      source: { kind: 'file', path: 'docs/main_orphan.md', collection: 'proj-docs' },
    };
    writeFileSync(claimedPath, JSON.stringify(orphanJob) + '\n');
    runWorker(project, { QMD_FORCE_DEAD_PIDS: String(deadPid) });
    assert.ok(!existsSync(claimedPath), 'orphan claimed file should be unlinked via main()');
  } finally {
    removeTemp(project);
  }
});

test('reclaim: claiming old backlog queue does not steal alive owner claimed batch', () => {
  const project = setupProject();
  const aliveChild = spawn('python3', ['-c', 'import time; time.sleep(10)']);
  const alivePid = aliveChild.pid;
  try {
    const compileDir = join(project, '.auto-context', 'compile');
    const queuePath = join(compileDir, 'source-queue.jsonl');
    writeFileSync(queuePath, JSON.stringify({ path: 'docs/backlog.md' }) + '\n');
    const oldTime = Math.floor(Date.now() / 1000) - 7200;
    utimesSync(queuePath, oldTime, oldTime);

    const claimedPath = join(compileDir, `source-queue.jsonl.claimed.${alivePid}.workerbatch`);
    writeFileSync(claimedPath, JSON.stringify({ path: 'docs/working.md' }) + '\n');
    utimesSync(claimedPath, oldTime, oldTime);

    runWorker(project);

    assert.ok(existsSync(claimedPath), 'alive owner batch must NOT be stolen despite old mtime');
  } finally {
    aliveChild.kill('SIGKILL');
    removeTemp(project);
  }
});

test('reclaim: corrupted _requeue_count does not wedge queue and converges', () => {
  const project = setupProject();
  try {
    const compileDir = join(project, '.auto-context', 'compile');
    const deadChild = spawnSync('python3', ['-c', 'import sys; sys.exit(0)']);
    const deadPid = deadChild.pid;
    const claimedPath = join(compileDir, `source-queue.jsonl.claimed.${deadPid}.corrupt`);
    const corruptJob = {
      targetPath: 'docs/corrupt.md',
      _requeue_count: '3x',
    };
    writeFileSync(claimedPath, JSON.stringify(corruptJob) + '\n');

    const dirtyQueue = join(project, '.auto-context', 'compile', 'dirty-queue');
    const res = execFileSync('python3', ['-c', `
import sys
from pathlib import Path
sys.path.insert(0, 'core')
from wiki_compile_worker import claim_queue
res = claim_queue(Path('${compileDir}/source-queue.jsonl'))
print(res if res else "")
`, { env: { ...process.env, QMD_DIRTY_QUEUE: dirtyQueue, QMD_FORCE_DEAD_PIDS: String(deadPid) } }]).toString().trim();

    assert.ok(!existsSync(claimedPath), 'corrupt claimed file should be processed without raise');
    const content = res && existsSync(res) ? readFileSync(res, 'utf8') : (existsSync(join(compileDir, 'source-queue.jsonl')) ? readFileSync(join(compileDir, 'source-queue.jsonl'), 'utf8') : '');
    assert.ok(content.includes('docs/corrupt.md'));
    assert.ok(content.includes('"_requeue_count": 1'));
  } finally {
    removeTemp(project);
  }
});

test('worker preserves claimed file on requeue failure for reaper recovery', () => {
  const project = setupProject();
  try {
    const compileDir = join(project, '.auto-context', 'compile');
    const queuePath = join(compileDir, 'source-queue.jsonl');
    writeFileSync(queuePath, JSON.stringify({ path: 'docs/test.md' }) + '\n');

    const script = `
from pathlib import Path
import sys
sys.path.insert(0, 'core')
import wiki_compile_worker as w
queue = Path('${compileDir}/source-queue.jsonl')
claimed = w.claim_queue(queue)
def bad_requeue(*args, **kwargs):
    raise OSError(28, "No space left on device")
w.requeue_lines = bad_requeue
try:
    rows = w.read_queue(claimed)
    w.requeue_lines(queue, [r for r, _ in rows])
except OSError:
    pass
print(claimed if claimed.exists() else "")
`;
    const dirtyQueue = join(project, '.auto-context', 'compile', 'dirty-queue');
    const res = execFileSync('python3', ['-c', script], { env: { ...process.env, QMD_DIRTY_QUEUE: dirtyQueue } }).toString().trim();
    assert.ok(res.length > 0, 'claimed file must remain on disk when requeue fails so reaper can recover it');
  } finally {
    removeTemp(project);
  }
});

test('reclaim: orphan claimed file with future mtime is reclaimed', () => {
  const project = setupProject();
  try {
    const compileDir = join(project, '.auto-context', 'compile');
    const deadChild = spawnSync('python3', ['-c', 'import sys; sys.exit(0)']);
    const deadPid = deadChild.pid;
    const claimedPath = join(compileDir, `source-queue.jsonl.claimed.${deadPid}.future`);
    const orphanJob = {
      path: 'docs/future_orphan.md',
    };
    writeFileSync(claimedPath, JSON.stringify(orphanJob) + '\n');
    const futureTime = Math.floor(Date.now() / 1000) + 86400;
    utimesSync(claimedPath, futureTime, futureTime);

    const dirtyQueue = join(project, '.auto-context', 'compile', 'dirty-queue');
    const res = execFileSync('python3', ['-c', `
import sys
from pathlib import Path
sys.path.insert(0, 'core')
from wiki_compile_worker import claim_queue
res = claim_queue(Path('${compileDir}/source-queue.jsonl'))
print(res if res else "")
`, { env: { ...process.env, QMD_DIRTY_QUEUE: dirtyQueue, QMD_FORCE_DEAD_PIDS: String(deadPid) } }]).toString().trim();

    assert.ok(!existsSync(claimedPath), 'future mtime orphan claimed file should be unlinked');
    const content = res && existsSync(res) ? readFileSync(res, 'utf8') : (existsSync(join(compileDir, 'source-queue.jsonl')) ? readFileSync(join(compileDir, 'source-queue.jsonl'), 'utf8') : '');
    assert.ok(content.includes('docs/future_orphan.md'));
  } finally {
    removeTemp(project);
  }
});

test('reclaim: 24h mtime backstop reclaims pid-alive claimed files if stale >= 24h', () => {
  const project = setupProject();
  const aliveChild = spawn('python3', ['-c', 'import time; time.sleep(10)']);
  const alivePid = aliveChild.pid;
  try {
    const compileDir = join(project, '.auto-context', 'compile');
    const claimedPath = join(compileDir, `source-queue.jsonl.claimed.${alivePid}.backstop24h`);
    writeFileSync(claimedPath, JSON.stringify({ path: 'docs/stale24h.md' }) + '\n');
    // Set mtime to 25 hours ago (> 24h backstop threshold)
    const oldTime = Math.floor(Date.now() / 1000) - 90000;
    utimesSync(claimedPath, oldTime, oldTime);

    const dirtyQueue = join(project, '.auto-context', 'compile', 'dirty-queue');
    const res = execFileSync('python3', ['-c', `
import sys
from pathlib import Path
sys.path.insert(0, 'core')
from wiki_compile_worker import claim_queue
res = claim_queue(Path('${compileDir}/source-queue.jsonl'))
print(res if res else "")
`, { env: { ...process.env, QMD_DIRTY_QUEUE: dirtyQueue } }]).toString().trim();

    assert.ok(!existsSync(claimedPath), 'pid-alive claimed file stale >= 24h MUST be reclaimed by backstop');
  } finally {
    aliveChild.kill('SIGKILL');
    removeTemp(project);
  }
});

test('reclaim: pid-alive claimed file within 24h is NOT stolen', () => {
  const project = setupProject();
  const aliveChild = spawn('python3', ['-c', 'import time; time.sleep(10)']);
  const alivePid = aliveChild.pid;
  try {
    const compileDir = join(project, '.auto-context', 'compile');
    const claimedPath = join(compileDir, `source-queue.jsonl.claimed.${alivePid}.recent_alive`);
    writeFileSync(claimedPath, JSON.stringify({ path: 'docs/recent_alive.md' }) + '\n');
    // Set mtime to 23 hours ago (< 24h backstop threshold)
    const recentTime = Math.floor(Date.now() / 1000) - 82800;
    utimesSync(claimedPath, recentTime, recentTime);

    const dirtyQueue = join(project, '.auto-context', 'compile', 'dirty-queue');
    execFileSync('python3', ['-c', `
import sys
from pathlib import Path
sys.path.insert(0, 'core')
from wiki_compile_worker import claim_queue
claim_queue(Path('${compileDir}/source-queue.jsonl'))
`, { env: { ...process.env, QMD_DIRTY_QUEUE: dirtyQueue } }]);

    assert.ok(existsSync(claimedPath), 'pid-alive claimed file within 24h MUST NOT be stolen');
  } finally {
    aliveChild.kill('SIGKILL');
    removeTemp(project);
  }
});

test('reclaim: I/O failure on orphan child does not crash claim_queue', () => {
  const project = setupProject();
  try {
    const compileDir = join(project, '.auto-context', 'compile');
    const queuePath = join(compileDir, 'source-queue.jsonl');
    writeFileSync(queuePath, JSON.stringify({ path: 'docs/test_io.md' }) + '\n');
    const claimedPath = join(compileDir, `source-queue.jsonl.claimed.9999999.testio`);
    writeFileSync(claimedPath, JSON.stringify({ path: 'docs/orphan_io.md' }) + '\n');

    const script = `
import sys
from pathlib import Path
sys.path.insert(0, 'core')
import wiki_compile_worker as w

orig_unlink = Path.unlink
def mock_unlink(self, *args, **kwargs):
    if "testio" in self.name:
        raise OSError(13, "Permission denied")
    return orig_unlink(self, *args, **kwargs)

Path.unlink = mock_unlink

queue = Path('${compileDir}/source-queue.jsonl')
res = w.claim_queue(queue)
print(res if res else "")
`;
    const dirtyQueue = join(project, '.auto-context', 'compile', 'dirty-queue');
    const res = execFileSync('python3', ['-c', script], { env: { ...process.env, QMD_DIRTY_QUEUE: dirtyQueue, QMD_FORCE_DEAD_PIDS: "9999999" } }).toString().trim();
    assert.ok(res.length > 0, 'claim_queue must succeed and return claimed path even if orphan unlink raises OSError');
  } finally {
    removeTemp(project);
  }
});

test('reclaim: discard ledger extracts targetPath for all 4 queues including dedup-needed pageA/pageB', () => {
  const project = setupProject();
  try {
    const compileDir = join(project, '.auto-context', 'compile');
    const dirtyQueue = join(project, '.auto-context', 'compile', 'dirty-queue');

    const queues = [
      { name: 'source-queue.jsonl', payload: { source: { path: 'docs/source_q.md' }, _requeue_count: 3 } },
      { name: 'verify-queue.jsonl', payload: { card: { targetPath: 'docs/verify_q.md' }, _requeue_count: 3 } },
      { name: 'merge-needed.jsonl', payload: { candidate: { targetPath: 'docs/merge_q.md' }, _requeue_count: 3 } },
      { name: 'dedup-needed.jsonl', payload: { pageA: 'docs/dedup_a.md', pageB: 'docs/dedup_b.md', _requeue_count: 3 } },
    ];

    for (const q of queues) {
      const claimedPath = join(compileDir, `${q.name}.claimed.9999999.discardtest`);
      writeFileSync(claimedPath, JSON.stringify(q.payload) + '\n');
      execFileSync('python3', ['-c', `
import sys
from pathlib import Path
sys.path.insert(0, 'core')
from wiki_compile_worker import claim_queue
claim_queue(Path('${compileDir}/${q.name}'))
`, { env: { ...process.env, QMD_DIRTY_QUEUE: dirtyQueue, QMD_FORCE_DEAD_PIDS: '9999999' } }]);
    }

    const ledgerPath = join(compileDir, 'discard-ledger.jsonl');
    assert.ok(existsSync(ledgerPath), 'discard-ledger.jsonl should be created');
    const ledgerLines = readFileSync(ledgerPath, 'utf8').trim().split('\n').map(l => JSON.parse(l));

    assert.equal(ledgerLines.length, 4);
    assert.equal(ledgerLines[0].targetPath, 'docs/source_q.md');
    assert.equal(ledgerLines[1].targetPath, 'docs/verify_q.md');
    assert.equal(ledgerLines[2].targetPath, 'docs/merge_q.md');
    assert.equal(ledgerLines[3].targetPath, 'docs/dedup_a.md <-> docs/dedup_b.md');
  } finally {
    removeTemp(project);
  }
});

// ─────────────────────────────────────────────────────────────────────────────
// provenance 소유권: `sourceRevisions` 는 **워커가 읽은 파일**에서 나오고 모델 값은
// 채택되지 않는다. 이 필드가 비면 그 카드는 `recall.is_auto_trusted_card`(status +
// createdBy + **비어 있지 않은** sourceRevisions)를 영구히 통과하지 못해 recall 에
// 절대 주입되지 않는다 — 즉 모델이 값을 안 주거나 형식을 틀리는 것만으로 카드가
// 조용히 무용해진다. 라이브 census 로 그 손실을 셌다(service-engineering 2026-08
// 카드 중 128장이 provenance 없음).
const PROVENANCE_EXTRACTOR = `#!/usr/bin/env python3
import json, sys
payload = json.loads(sys.stdin.read())
assert 'content' in payload['source']
decoy = {'kind': 'file', 'path': 'decoy.md', 'collection': 'proj-docs',
         'sha256': '0' * 64, 'size': 1, 'mtimeNs': 1}
print(json.dumps({'candidates': [
  {
    'title': 'Silent Model',
    'summary': 'The extractor returned no provenance at all.',
    'suggestedType': 'concept', 'confidence': 'high',
    'targetPath': '.auto-context/wiki/concepts/silent-model.md',
  },
  {
    'title': 'Malformed Model',
    'summary': 'The extractor returned a provenance record missing required keys.',
    'suggestedType': 'concept', 'confidence': 'high',
    'targetPath': '.auto-context/wiki/concepts/malformed-model.md',
    'sourceRevisions': [{'kind': 'file', 'path': 'docs/source.md'}],
  },
  {
    'title': 'Decoy Model',
    'summary': 'The extractor pointed provenance at a file it never read.',
    'suggestedType': 'concept', 'confidence': 'high',
    'targetPath': '.auto-context/wiki/concepts/decoy-model.md',
    'sourceRevisions': [decoy],
    'authoritativeSources': [{'kind': 'file', 'path': 'decoy.md', 'collection': 'proj-docs'}],
    'sources': [{'kind': 'url', 'ref': 'https://example.invalid/decoy'}],
  },
]}))
`;

function provenanceProject() {
  const extractor = join(mkdtempSync(join(tmpdir(), 'extractor-provenance-')), 'extract.py');
  writeFileSync(extractor, PROVENANCE_EXTRACTOR);
  return setupProject({
    extractor: { backends: { claude: ['python3', extractor] }, timeout: 30 },
    verify: { enabled: false },
  });
}

test('worker 가 sourceRevisions 를 소유한다: 모델이 안 주든 malformed 든 decoy 든 실제 읽은 소스가 실린다', () => {
  // 세 후보를 한 run 에 태우는 이유: 세 실패 형태가 **같은 대입 한 줄**로 막히는지를
  // 보는 것이고, 하나만 통과해도 나머지가 새면 계약이 아니다. decoy 후보는
  // authoritativeSources·sources 까지 위조해 "검증 근거와 provenance 가 함께 모델
  // 손에 넘어가는" 최악 형태를 만든다.
  const project = provenanceProject();
  try {
    runWorker(project, {}, ['--flush-all']);
    const real = createHash('sha256')
      .update(readFileSync(join(project, 'docs', 'source.md'))).digest('hex');
    for (const slug of ['silent-model', 'malformed-model', 'decoy-model']) {
      const rel = join('.auto-context', 'wiki', 'concepts', `${slug}.md`);
      const text = readFileSync(join(project, rel), 'utf8');
      assert.match(text, /^sourceRevisions:$/m, `${slug}: provenance 블록이 있어야 한다`);
      assert.match(text, new RegExp(`sha256: "${real}"`), `${slug}: 실제 원문 해시여야 한다`);
      assert.match(text, /path: "docs\/source\.md"/, `${slug}: 워커가 읽은 경로여야 한다`);
      assert.doesNotMatch(text, /decoy\.md/, `${slug}: 모델이 준 경로가 새면 안 된다`);
      assert.doesNotMatch(text, new RegExp('sha256: "0{64}"'), `${slug}: 모델 해시가 새면 안 된다`);
    }
  } finally { removeTemp(project); }
});

test('워커가 찍은 provenance 카드는 검수 후 recall 신뢰를 통과하고 freshness 가 fresh 다', () => {
  // 목적은 "블록을 썼다"가 아니라 "그 카드가 recall 에 나온다"다. status·createdBy·
  // sourceRevisions 세 조건이 실제 predicate 를 함께 통과하는지, 그리고 원문을 고치면
  // 그 신뢰가 stale 로 무너지는지(= 해시가 파일 전체에 결속돼 있다)까지 본다.
  const project = provenanceProject();
  const cardRel = join('.auto-context', 'wiki', 'concepts', 'silent-model.md');
  try {
    runWorker(project, {}, ['--flush-all']);
    assert.equal(wikiTrustState(project, cardRel).trusted, false, 'generated 는 아직 신뢰되지 않는다');

    stampVerified(project, cardRel);
    const state = wikiTrustState(project, cardRel);
    assert.equal(state.trusted, true, 'is_auto_trusted_card 를 통과해야 한다');
    assert.equal(state.revisions.length, 1);
    assert.equal(state.revisions[0].path, 'docs/source.md');
    assert.equal(state.freshness.state, 'fresh');

    writeFileSync(join(project, 'docs', 'source.md'), '# Source\n\n다른 결론으로 바뀌었다.\n');
    assert.equal(wikiTrustState(project, cardRel).freshness.state, 'stale',
      '원문이 바뀌면 파일 전체 해시가 어긋나 stale 이어야 한다');
  } finally { removeTemp(project); }
});

test('절단된 소스에서도 sourceRevisions 의 sha256 은 파일 전체 기준이다', () => {
  // extractor 에는 `maxSourceChars` 로 자른 본문을 준다. provenance 는 그 층이 아니라
  // 파일 전체 스냅샷이어야 하고(`recall.card_freshness` 도 전체 파일로 대조한다),
  // 절단 사실을 해시에 섞으면 편집 없이도 영구 stale 이 된다.
  const project = provenanceProject();
  const source = join(project, 'docs', 'source.md');
  writeFileSync(source, `# Source\n\n${'가'.repeat(20000)}\n`);
  try {
    runWorker(project, {}, ['--flush-all']);
    const text = readFileSync(join(project, '.auto-context', 'wiki', 'concepts', 'silent-model.md'), 'utf8');
    const whole = createHash('sha256').update(readFileSync(source)).digest('hex');
    assert.match(text, new RegExp(`sha256: "${whole}"`));
    const size = Number(/size: (\d+)/.exec(text)[1]);
    assert.equal(size, statSync(source).size, 'size 도 절단본이 아니라 파일 전체다');
    assert.ok(size > 12000, '이 소스는 실제로 maxSourceChars 를 넘겨야 한다');
    const card = wikiTrustState(project, join('.auto-context', 'wiki', 'concepts', 'silent-model.md'));
    assert.equal(card.freshness.state, 'fresh', '절단 소스라도 편집 없이 fresh 여야 한다');
  } finally { removeTemp(project); }
});

test('갱신(updated) 경로의 provenance 는 새 소스 기준으로 갱신된다', () => {
  // 카드가 이미 있으면 `rewrite_generated_card` 가 옛 sourceRevisions 를 버리고 새
  // 블록을 넣는다. 여기가 새면 원문을 고쳐도 카드가 옛 해시를 들고 있어 영구 stale 이
  // 되고, `recallVerifiedOnly` 아래에서 그 카드는 다시 나오지 않는다.
  const project = provenanceProject();
  const source = join(project, 'docs', 'source.md');
  const cardRel = join('.auto-context', 'wiki', 'concepts', 'silent-model.md');
  try {
    runWorker(project, {}, ['--flush-all']);
    const first = wikiTrustState(project, cardRel).revisions[0].sha256;

    writeFileSync(source, '# Source\n\n갱신된 결론: 카드는 새 원문을 가리켜야 한다.\n');
    writeFileSync(join(project, '.auto-context', 'compile', 'source-queue.jsonl'),
      JSON.stringify({
        ts: '2026-06-27T00:00:00Z', trigger: 'post_tool_source', engine: 'claude', cwd: project,
        source: { kind: 'file', path: 'docs/source.md', collection: 'proj-docs' },
      }) + '\n');
    runWorker(project, {}, ['--flush-all']);

    const after = wikiTrustState(project, cardRel);
    assert.equal(after.revisions.length, 1, 'provenance 블록이 중복되지 않는다');
    assert.notEqual(after.revisions[0].sha256, first, '옛 해시가 남아 있으면 안 된다');
    assert.equal(after.revisions[0].sha256,
      createHash('sha256').update(readFileSync(source)).digest('hex'));
    assert.equal(after.freshness.state, 'fresh');
  } finally { removeTemp(project); }
});

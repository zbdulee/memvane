// Per-run throughput guards for the compile worker.
//
// cap: the worker had NO per-run cap — batch.maxItems is a start condition (it stays in
//      compile.batch), so a queue of N drove N consecutive host CLI spawns in one worker
//      process. Every paid-call cap now lives in compile.budget.*.
// verify budget: verify was fixed at 3 per run, so editing ~10 documents left most of the
//      resulting cards `generated` for several runs, and `compile.recallVerifiedOnly: true`
//      (default) hides a `generated` card from recall. Accurate cards arrived late.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { existsSync, mkdtempSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { removeTemp } from './helpers/temp.mjs';

// One mock host CLI adapter serving both tasks: the verifier reuses the extractor pool,
// dispatching on payload.task (see extractors/lib.py).
function mockAdapter(dir, logPath, cardsPerSource = 1) {
  const script = join(dir, 'adapter.py');
  writeFileSync(script, `#!/usr/bin/env python3
import json, pathlib, sys
payload = json.loads(sys.stdin.read())
log = pathlib.Path(${JSON.stringify(logPath)})
if payload.get('task') == 'verify':
    with log.open('a') as handle:
        handle.write('verify\\t' + payload['card']['path'] + '\\n')
    print(json.dumps({'verdict': 'pass', 'claims': [{'claim': 'c', 'supported': True, 'quote': 'q', 'sourcePath': 'docs/source.md'}], 'reasons': []}))
    raise SystemExit(0)
rel = payload['source']['path']
stem = pathlib.Path(rel).stem
with log.open('a') as handle:
    handle.write('extract\\t' + rel + '\\n')
# The number of candidates is MODEL output. The worker must not let it decide how many
# paid calls this run makes, so the mock is free to return as many as it likes.
cards = []
for index in range(${cardsPerSource}):
    suffix = '' if index == 0 else '-%d' % index
    cards.append({
        'title': 'Card ' + stem + suffix,
        'summary': 'Durable decision: the card for ' + rel + suffix + ' cites its source markdown.',
        'suggestedType': 'decision',
        'confidence': 'high',
        'targetPath': '.auto-context/wiki/decisions/card-' + stem + suffix + '.md',
    })
print(json.dumps({'candidates': cards}))
`);
  return script;
}

function setupProject({ sources, cardsPerSource = 1, compile: compileOverrides = {} }) {
  const dir = mkdtempSync(join(tmpdir(), 'qwiki-throughput-'));
  const logPath = join(dir, 'adapter-calls.log');
  mkdirSync(join(dir, '.auto-context', 'compile'), { recursive: true });
  mkdirSync(join(dir, '.auto-context', 'wiki'), { recursive: true });
  mkdirSync(join(dir, 'docs'), { recursive: true });
  const adapter = mockAdapter(dir, logPath, cardsPerSource);
  writeFileSync(join(dir, '.auto-context', 'settings.json'), JSON.stringify({
    indexing: true,
    collections: ['proj-docs', 'proj-wiki'],
    collectionPaths: { 'proj-docs': 'docs', 'proj-wiki': '.auto-context/wiki' },
    collectionRoles: { 'proj-docs': 'raw', 'proj-wiki': 'wiki' },
    wikiPath: '.auto-context/wiki',
    compile: {
      mode: 'auto-wiki',
      defaultStatus: 'generated',
      triggers: ['post_tool_source', 'manual'],
      maxSourceChars: 12000,
      extractor: { backends: { claude: ['python3', adapter] }, timeout: 60 },
      // keep the run deterministic and daemon-free
      semanticDedup: { enabled: false },
      verify: { enabled: true, timeout: 60 },
      ...compileOverrides,
    },
  }));
  const queue = [];
  for (const rel of sources) {
    writeFileSync(join(dir, rel), `# ${rel}\n\nDurable decision: ${rel} records a real decision.\n`);
    queue.push(JSON.stringify({
      ts: '2026-07-30T00:00:00Z',
      trigger: 'post_tool_source',
      engine: 'claude',
      cwd: dir,
      source: { kind: 'file', path: rel, collection: 'proj-docs' },
    }));
  }
  writeFileSync(join(dir, '.auto-context', 'compile', 'source-queue.jsonl'), queue.join('\n') + '\n');
  return { dir, logPath };
}

function runWorker(project, env = {}) {
  const dirtyQueue = env.QMD_DIRTY_QUEUE || join(project, '.auto-context', 'compile', 'dirty-queue');
  const out = execFileSync('python3', ['entry/run.py', 'wiki-compile-worker', '--cwd', project, '--json', '--flush-all'], {
    cwd: process.cwd(), encoding: 'utf8', env: { ...process.env, QMD_DIRTY_QUEUE: dirtyQueue, ...env },
  });
  return JSON.parse(out.trim());
}

function calls(logPath, kind) {
  if (!existsSync(logPath)) return [];
  return readFileSync(logPath, 'utf8').trim().split('\n').filter(Boolean)
    .map((line) => line.split('\t'))
    .filter(([task]) => task === kind)
    .map(([, target]) => target);
}

function queueLines(project) {
  const path = join(project, '.auto-context', 'compile', 'source-queue.jsonl');
  if (!existsSync(path)) return [];
  return readFileSync(path, 'utf8').trim().split('\n').filter(Boolean);
}

test('compile worker per-run cap bounds host CLI spawns and defers the rest without loss', () => {
  const sources = ['docs/a.md', 'docs/b.md', 'docs/c.md', 'docs/d.md', 'docs/e.md', 'docs/f.md', 'docs/g.md'];
  const { dir, logPath } = setupProject({
    sources,
    compile: { batch: { idleSeconds: 0, maxItems: 1 }, budget: { extractorPerRun: 3 } },
  });
  const dirtyQueue = join(mkdtempSync(join(tmpdir(), 'dirty-cap-')), 'queue');
  try {
    const first = runWorker(dir, { QMD_DIRTY_QUEUE: dirtyQueue });
    assert.equal(first.processed, 3, 'exactly budget.extractorPerRun jobs run');
    assert.equal(first.deferred, 4);
    assert.equal(first.remaining, 4);
    assert.equal(calls(logPath, 'extract').length, 3, 'cap bounds extractor spawns, not just bookkeeping');
    assert.equal(queueLines(dir).length, 4, 'overflow is requeued, never dropped');

    // Draining across runs must cover every source exactly once.
    const second = runWorker(dir, { QMD_DIRTY_QUEUE: dirtyQueue });
    const third = runWorker(dir, { QMD_DIRTY_QUEUE: dirtyQueue });
    assert.equal(second.processed, 3);
    assert.equal(third.processed, 1);
    assert.equal(queueLines(dir).length, 0);
    assert.deepEqual(calls(logPath, 'extract').sort(), [...sources].sort());
  } finally {
    removeTemp(dir);
  }
});

test('verify budget rises to the cards the same run produced (no generated backlog)', () => {
  const sources = ['docs/a.md', 'docs/b.md', 'docs/c.md', 'docs/d.md', 'docs/e.md'];
  const { dir, logPath } = setupProject({
    sources,
    compile: {
      batch: { idleSeconds: 0, maxItems: 1 },
      // standing backlog budget stays at the shipped default
      budget: { extractorPerRun: 5, verifyPerRun: 3 },
      verify: { enabled: true, timeout: 60 },
    },
  });
  const dirtyQueue = join(mkdtempSync(join(tmpdir(), 'dirty-vb-')), 'queue');
  try {
    const out = runWorker(dir, { QMD_DIRTY_QUEUE: dirtyQueue });
    assert.equal(out.processed, 5);
    assert.equal(out.verifyQueued, 5);
    // Before this change the piggybacked pass verified 3 and left 2 cards `generated`,
    // i.e. invisible to recall under recallVerifiedOnly until some later run picked them up.
    assert.equal(calls(logPath, 'verify').length, 5);
    assert.equal(readFileSync(join(dir, '.auto-context', 'compile', 'verify-queue.jsonl'), 'utf8'), '');
    for (const rel of sources) {
      const card = join(dir, '.auto-context', 'wiki', 'decisions', `card-${rel.split('/')[1].replace('.md', '')}.md`);
      assert.match(readFileSync(card, 'utf8'), /^status: verified$/m, rel);
    }
  } finally {
    removeTemp(dir);
  }
});

test('verify keeps its standing per-run cap when nothing was produced this run', () => {
  // The production floor must be a floor, not a replacement: a standalone drain of an
  // existing backlog still honours budget.verifyPerRun.
  const dir = mkdtempSync(join(tmpdir(), 'qwiki-verify-cap-'));
  const logPath = join(dir, 'adapter-calls.log');
  mkdirSync(join(dir, '.auto-context', 'compile'), { recursive: true });
  mkdirSync(join(dir, '.auto-context', 'wiki', 'decisions'), { recursive: true });
  mkdirSync(join(dir, 'docs'), { recursive: true });
  const adapter = mockAdapter(dir, logPath);
  writeFileSync(join(dir, '.auto-context', 'settings.json'), JSON.stringify({
    indexing: true,
    collections: ['proj-docs', 'proj-wiki'],
    collectionPaths: { 'proj-docs': 'docs', 'proj-wiki': '.auto-context/wiki' },
    collectionRoles: { 'proj-docs': 'raw', 'proj-wiki': 'wiki' },
    wikiPath: '.auto-context/wiki',
    compile: {
      mode: 'auto-wiki', defaultStatus: 'generated',
      triggers: ['post_tool_source'], maxSourceChars: 12000,
      extractor: { backends: { claude: ['python3', adapter] }, timeout: 60 },
      budget: { verifyPerRun: 3 },
      verify: { enabled: true, timeout: 60 },
    },
  }));
  const jobs = [];
  for (const name of ['a', 'b', 'c', 'd', 'e']) {
    writeFileSync(join(dir, 'docs', `${name}.md`), `# ${name}\n\nDurable claim: card ${name} cites markdown.\n`);
    const rel = `.auto-context/wiki/decisions/card-${name}.md`;
    writeFileSync(join(dir, rel), [
      '---', `title: "Card ${name}"`, 'status: generated', 'createdBy: qmd-auto-context',
      'reviewed: false', '---', '',
      '<!-- qmd:auto:start id="main" sourceHash="d1" -->', '## Summary',
      `Durable claim: card ${name} cites markdown.`, '<!-- qmd:auto:end -->', '',
    ].join('\n'));
    jobs.push(JSON.stringify({
      ts: '2026-07-30T00:00:00Z', targetPath: rel,
      sources: [{ kind: 'file', path: `docs/${name}.md`, collection: 'proj-docs' }],
      sourceHash: 'd1', engine: 'claude', trigger: 'post_tool_source',
    }));
  }
  writeFileSync(join(dir, '.auto-context', 'compile', 'verify-queue.jsonl'), jobs.join('\n') + '\n');
  try {
    const out = JSON.parse(execFileSync('python3', ['core/wiki_verify_worker.py', '--cwd', dir, '--json'], {
      cwd: process.cwd(), encoding: 'utf8',
      env: { ...process.env, QMD_DIRTY_QUEUE: join(dir, 'dirty-queue') },
    }).trim());
    assert.equal(out.processed, 3);
    assert.equal(out.remaining, 2);
    assert.equal(calls(logPath, 'verify').length, 3);
  } finally {
    removeTemp(dir);
  }
});

function jsonl(path) {
  if (!existsSync(path)) return [];
  return readFileSync(path, 'utf8').trim().split('\n').filter(Boolean).map((line) => JSON.parse(line));
}

test('model output cannot decide how many paid calls a run makes', () => {
  // The reported defect: `produced` was the sum of len(candidates), and candidates is
  // model output with no slice anywhere. Measured before the fix: queue 5, model returns
  // 40 cards per source -> 5 extract + 200 verify = 205 paid calls in one run.
  const sources = ['docs/a.md', 'docs/b.md', 'docs/c.md', 'docs/d.md', 'docs/e.md'];
  const { dir, logPath } = setupProject({
    sources,
    cardsPerSource: 40,
    compile: {
      batch: { idleSeconds: 0, maxItems: 1 },
      budget: { extractorPerRun: 10, verifyPerRun: 3 },
      verify: { enabled: true, timeout: 60 },
    },
  });
  const dirtyQueue = join(mkdtempSync(join(tmpdir(), 'dirty-hardcap-')), 'queue');
  try {
    const out = runWorker(dir, { QMD_DIRTY_QUEUE: dirtyQueue });
    assert.equal(out.processed, 5);
    assert.equal(calls(logPath, 'extract').length, 5);

    // budget.cardsPerSource (default 10) bounds cards per source, so 40 -> 10.
    assert.equal(out.verifyQueued, 50, '5 sources x budget.cardsPerSource 10');
    // VERIFY_PRODUCED_HARD_CAP bounds the model-derived verify budget at 30, and the
    // starvation reserve cannot add anything because every queued job is from this run.
    const verifyCalls = calls(logPath, 'verify').length;
    assert.equal(verifyCalls, 30, `verify calls must be capped, got ${verifyCalls}`);
    // Total paid calls this run: 5 extract + 30 verify = 35, not 205.
    assert.ok(verifyCalls + 5 <= 35);
    // Nothing is lost: the uncapped 20 cards stay queued for the next run.
    assert.equal(jsonl(join(dir, '.auto-context', 'compile', 'verify-queue.jsonl')).length, 20);
  } finally {
    removeTemp(dir);
  }
});

test('cards beyond budget.cardsPerSource are recorded, not silently dropped', () => {
  const { dir } = setupProject({
    sources: ['docs/a.md'],
    cardsPerSource: 14,
    compile: {
      batch: { idleSeconds: 0, maxItems: 1 },
      budget: { extractorPerRun: 10, cardsPerSource: 4 },
      verify: { enabled: false },
    },
  });
  try {
    runWorker(dir);
    const rows = jsonl(join(dir, '.auto-context', 'compile', 'candidates.jsonl'));
    const capped = rows.find((r) => r.reason === 'cards_per_source_cap');
    assert.ok(capped, 'the cap must leave a record');
    assert.equal(capped.action, 'skipped');
    assert.equal(capped.cardsReturned, 14);
    assert.equal(capped.cardsCompiled, 4);
    assert.equal(capped.skippedTitles.length, 10);
    assert.equal(capped.source.path, 'docs/a.md');
  } finally {
    removeTemp(dir);
  }
});

test('budget.extractorPerRun is clamped, so a huge value cannot drive a huge run', () => {
  const sources = Array.from({ length: 4 }, (_, i) => `docs/s${i}.md`);
  const { dir, logPath } = setupProject({
    sources,
    compile: {
      // config normalizes this to MAX_COMPILE_PER_RUN (50); the worker clamps again.
      batch: { idleSeconds: 0, maxItems: 1 },
      budget: { extractorPerRun: 99999999 },
      verify: { enabled: false },
    },
  });
  try {
    const out = runWorker(dir);
    assert.equal(out.processed, 4, 'a 4-item queue still drains fully');
    assert.equal(calls(logPath, 'extract').length, 4);
    const py = `import json,sys; sys.path.insert(0,'.'); import memvane.settings.config as config
print(json.dumps(config.compile_config({'budget': {'extractorPerRun': 99999999}})['budget']['extractorPerRun']))`;
    const clamped = JSON.parse(execFileSync('python3', ['-c', py], {
      cwd: process.cwd(), encoding: 'utf8', env: { ...process.env, QMD_DIRTY_QUEUE: join(dir, 'dirty-queue') },
    }).trim());
    assert.equal(clamped, 50);
  } finally {
    removeTemp(dir);
  }
});

function seedBacklogCard(dir, name) {
  const rel = `.auto-context/wiki/decisions/backlog-${name}.md`;
  mkdirSync(join(dir, '.auto-context', 'wiki', 'decisions'), { recursive: true });
  writeFileSync(join(dir, 'docs', `backlog-${name}.md`), `# ${name}\n\nDurable claim: backlog ${name} cites markdown.\n`);
  writeFileSync(join(dir, rel), [
    '---', `title: "Backlog ${name}"`, 'status: generated', 'createdBy: qmd-auto-context',
    'reviewed: false', '---', '',
    '<!-- qmd:auto:start id="main" sourceHash="d1" -->', '## Summary',
    `Durable claim: backlog ${name} cites markdown.`, '<!-- qmd:auto:end -->', '',
  ].join('\n'));
  return JSON.stringify({
    ts: '2026-07-01T00:00:00Z', targetPath: rel,
    sources: [{ kind: 'file', path: `docs/backlog-${name}.md`, collection: 'proj-docs' }],
    sourceHash: 'd1', engine: 'claude', trigger: 'post_tool_source',
  });
}

test("this run's cards are verified before older backlog, and backlog still advances", () => {
  // Sizing the budget was not enough: the queue is FIFO, so an enlarged budget was spent
  // on the OLDEST jobs and the run's own cards stayed `generated` — invisible to recall
  // under the default recallVerifiedOnly, which is the outcome the enlargement targeted.
  const sources = ['docs/new1.md', 'docs/new2.md'];
  const { dir, logPath } = setupProject({
    sources,
    compile: {
      batch: { idleSeconds: 0, maxItems: 1 },
      budget: { extractorPerRun: 10, verifyPerRun: 1 },
      verify: { enabled: true, timeout: 60 },
    },
  });
  const dirtyQueue = join(mkdtempSync(join(tmpdir(), 'dirty-prio-')), 'queue');
  try {
    const backlog = ['a', 'b', 'c', 'd'].map((name) => seedBacklogCard(dir, name));
    writeFileSync(join(dir, '.auto-context', 'compile', 'verify-queue.jsonl'), backlog.join('\n') + '\n');

    const out = runWorker(dir, { QMD_DIRTY_QUEUE: dirtyQueue });
    assert.equal(out.verifyQueued, 2);

    const verified = calls(logPath, 'verify');
    // Both of this run's cards are verified in this run.
    for (const rel of ['new1', 'new2']) {
      const card = join(dir, '.auto-context', 'wiki', 'decisions', `card-${rel}.md`);
      assert.match(readFileSync(card, 'utf8'), /^status: verified$/m, rel);
      assert.ok(verified.some((p) => p.endsWith(`card-${rel}.md`)), rel);
    }
    // Starvation reserve: budget was fully consumed by fresh cards, yet exactly one
    // backlog job still ran, so a sustained bulk period cannot freeze the backlog.
    const backlogVerified = verified.filter((p) => p.includes('backlog-'));
    assert.equal(backlogVerified.length, 1, 'exactly one reserved backlog slot');
    assert.equal(verified.length, 3);
    // The other three backlog jobs are still queued (nothing lost).
    assert.equal(jsonl(join(dir, '.auto-context', 'compile', 'verify-queue.jsonl')).length, 3);
  } finally {
    removeTemp(dir);
  }
});

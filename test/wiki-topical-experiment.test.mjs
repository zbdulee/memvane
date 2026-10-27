import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { spawnSync, execFileSync } from 'node:child_process';
import { existsSync, mkdtempSync, mkdirSync, readFileSync, readdirSync, rmSync, statSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const sha = (value) => createHash('sha256').update(value).digest('hex');
const py = (script, ...args) => execFileSync('python3', ['-c', script, ...args], {
  cwd: process.cwd(), encoding: 'utf8', env: { ...process.env, QMD_RECALL_LOG: '', QMD_SANDBOX: '1' },
});

function sandbox() {
  const root = mkdtempSync(join(tmpdir(), 'qmd-v2-hook-test-'));
  mkdirSync(join(root, 'sources'));
  mkdirSync(join(root, 'legacy-cards'));
  writeFileSync(join(root, '.qmd-topical-sandbox'), 'synthetic only\n');
  return root;
}

function source(root, name, body) {
  const rel = `sources/${name}.md`;
  writeFileSync(join(root, rel), body);
  return { rel, body, revision: sha(body) };
}

function evidence(src, quote, line) {
  return { sourcePath: src.rel, sourceRevisionSha256: src.revision,
    startLine: line, endLine: line, quoteAnchor: quote, quoteSha256: sha(quote) };
}

function claim(id, statement, state, timeScope, condition, spans) {
  return { claimId: id, statement, state, timeScope, condition, evidence: spans };
}

function card(id, title, claims, category = 'world-rule') {
  return { cardId: id, title, category, details: 'Synthetic detail outside the hook lead.', claims };
}

function cli(root, action, input, extra = []) {
  const processResult = spawnSync('python3', ['core/wiki_topical_experiment.py', action,
    '--sandbox-root', root, ...extra], { input: JSON.stringify(input), encoding: 'utf8',
    env: { ...process.env, QMD_RECALL_LOG: '', QMD_SANDBOX: '1' } });
  assert.equal(processResult.stderr, '', processResult.stderr);
  return { code: processResult.status, result: JSON.parse(processResult.stdout) };
}

function compile(root, cards, budget = 600) {
  const processResult = spawnSync('python3', ['core/wiki_topical.py', '--sandbox-root', root,
    '--lead-budget', String(budget)], { input: JSON.stringify({ schema: 'qmd-topical-sandbox-v1', cards }),
    encoding: 'utf8', env: { ...process.env, QMD_RECALL_LOG: '', QMD_SANDBOX: '1' } });
  assert.equal(processResult.stderr, '', processResult.stderr);
  return { code: processResult.status, result: JSON.parse(processResult.stdout) };
}

function staged(root, generationId, cardId) {
  return JSON.parse(readFileSync(join(root, 'topical-generations', generationId, 'cards', `${cardId}.evidence.json`), 'utf8'));
}

function semantic(cardData, verdict = 'pass', override = {}) {
  const checks = cardData.claims.flatMap((c) => c.evidence.map((e) => ({
    claimId: c.claimId, sourcePath: e.sourcePath, quoteSha256: e.quoteSha256,
    supported: override[`${c.claimId}:${e.sourcePath}`] ?? (verdict === 'pass' ? true : verdict === 'fail' ? false : null),
  })));
  return { verdict, checks };
}

function hit(generationId, cardId, cardData, rank, verdict = 'pass', override = {}) {
  return { kind: 'topical', generationId, cardId, rank, score: 1 / rank,
    mockSemantic: semantic(cardData, verdict, override) };
}

function invoke(root, hits, budgetChars = 2400, topN = 3) {
  return cli(root, 'invoke', { schema: 'qmd-topical-sandbox-hook-v1', hits, budgetChars, topN });
}

test('v2 synthetic end-to-end: prompt contract, multi-topic cards, mock pass, full lead and line/version hook', () => {
  const root = sandbox();
  try {
    const src = source(root, 'chapter', 'The keeper has a silver map.\nThe gate stays closed at night.\n');
    const contract = cli(root, 'contract', { sources: [src.rel], leadBudgetChars: 600 });
    assert.equal(contract.code, 0);
    assert.equal(contract.result.compilerOwnedCardSchemaVersion, 2);
    assert.match(contract.result.rules.join(' '), /several independently useful topical cards/);
    assert.match(contract.result.sources[0].numberedContent, /2: The gate stays closed at night/);
    const cards = [
      card('keeper-map', 'Keeper map', [claim('map-owner', 'The keeper has a silver map.',
        'actual', 'chapter-1', '', [evidence(src, 'The keeper has a silver map.', 1)])], 'entity'),
      card('gate-night', 'Gate night', [claim('gate-shut', 'The gate stays closed at night.',
        'rule', 'chapter-1', 'at night', [evidence(src, 'The gate stays closed at night.', 2)])]),
    ];
    const compiled = compile(root, cards);
    assert.equal(compiled.code, 0);
    const id = compiled.result.generationId;
    assert.match(readFileSync(join(root, 'topical-generations', id, 'cards', 'keeper-map.md'), 'utf8'), /^schemaVersion: 2$/m);
    const gate = staged(root, id, 'gate-night');
    const keeper = staged(root, id, 'keeper-map');
    const result = invoke(root, [hit(id, 'gate-night', gate, 1), hit(id, 'keeper-map', keeper, 2)]);
    assert.equal(result.code, 0);
    assert.deepEqual(result.result.selected.map((x) => x.cardId), ['gate-night', 'keeper-map']);
    assert.equal(result.result.ingestion.eligibleBeforeTopK, 2);
    assert.ok(result.result.usedChars <= result.result.budgetChars);
    assert.ok(result.result.context.includes(gate.lead));
    assert.ok(result.result.context.includes(keeper.lead));
    assert.match(result.result.context, /sources\/chapter\.md#L2-L2 @sha256:[0-9a-f]{64}/);
    assert.doesNotMatch(result.result.context, /Synthetic detail outside/);
    assert.doesNotMatch(result.result.context, /schemaVersion: 2/);
    assert.equal(readFileSync(join(root, 'topical-generations', id, 'manifest.json'), 'utf8').includes('generated_unverified'), true);
  } finally { rmSync(root, { recursive: true, force: true }); }
});

test('QMD projection admits only mock-passing v2 cards and binds retrieval to original hashes', () => {
  const root = sandbox();
  try {
    const src = source(root, 'projection', 'The copper lantern shines at dusk.\nThe glass lantern stays dark.\n');
    const created = compile(root, [
      card('copper-lantern', 'Copper lantern', [claim('copper-light', 'The copper lantern shines at dusk.',
        'actual', 'chapter-1', 'dusk', [evidence(src, 'The copper lantern shines at dusk.', 1)])]),
      card('glass-lantern', 'Glass lantern', [claim('glass-dark', 'The glass lantern stays dark.',
        'actual', 'chapter-1', '', [evidence(src, 'The glass lantern stays dark.', 2)])]),
    ]);
    const id = created.result.generationId;
    writeFileSync(join(root, 'legacy-cards', 'old.md'), '---\nschemaVersion: 1\n---\nlegacy lantern\n');
    const rows = [
      hit(id, 'copper-lantern', staged(root, id, 'copper-lantern'), 1),
      hit(id, 'glass-lantern', staged(root, id, 'glass-lantern'), 2, 'fail'),
      { kind: 'legacy', path: 'legacy-cards/old.md' },
    ];
    const script = [
      'import json,sys', 'from pathlib import Path', 'sys.path.insert(0,"core")',
      'import wiki_topical_experiment as e', 'root=Path(sys.argv[1])', 'hits=json.loads(sys.argv[2])',
      'manifest=e.build_qmd_projection(root,hits,root/"qmd-v2-projection")',
      'assert len(manifest["cards"])==1 and len(manifest["excludedBeforeTopK"])==2',
      'uri="qmd://"+e.INDEX_COLLECTION+"/"+manifest["cards"][0]["projectionFile"]',
      'actual=[{"file":uri,"score":0.9}]',
      'prepared=e.qmd_hits_to_prepared(root,manifest,actual)',
      'assert len(prepared["eligible"])==1',
      'good=e.render_hook(root,prepared)',
      'projection=root/"qmd-v2-projection"/manifest["cards"][0]["projectionFile"]',
      'projection.write_text(projection.read_text()+"tamper\\n")',
      'bad=e.qmd_hits_to_prepared(root,manifest,actual)',
      'print(json.dumps({"manifest":manifest,"good":good,"bad":bad}))',
    ].join('\n');
    const result = JSON.parse(py(script, root, JSON.stringify(rows)));
    assert.equal(result.good.selected[0].cardId, 'copper-lantern');
    assert.match(result.good.context, /sources\/projection\.md#L1-L1 @sha256:/);
    assert.equal(result.bad.eligible.length, 0);
    assert.equal(result.bad.excluded[0].reason, 'projection_content_hash_mismatch');
    assert.equal(readdirSync(join(root, 'qmd-v2-projection')).filter((x) => x.endsWith('.md')).length, 1);
  } finally { rmSync(root, { recursive: true, force: true }); }
});

test('mock pass, inconclusive, fail, and incomplete support remain distinct; no verified stamp', () => {
  const root = sandbox();
  try {
    const src = source(root, 'states', 'First fact is present.\nSecond fact is present.\nThird fact is present.\n');
    const cards = ['first', 'second', 'third'].map((name, index) => card(`${name}-fact`, `${name} fact`, [
      claim(`${name}-claim`, `${name} fact is present.`, 'actual', 'chapter-2', '',
        [evidence(src, `${name[0].toUpperCase()}${name.slice(1)} fact is present.`, index + 1)]),
    ]));
    const created = compile(root, cards);
    assert.equal(created.code, 0);
    const id = created.result.generationId;
    const hits = ['first', 'second', 'third'].map((name, index) =>
      hit(id, `${name}-fact`, staged(root, id, `${name}-fact`), index + 1,
        ['pass', 'inconclusive', 'fail'][index]));
    const result = invoke(root, hits);
    assert.deepEqual(result.result.selected.map((c) => c.cardId), ['first-fact']);
    assert.deepEqual(result.result.excluded.map((x) => x.reason).sort(), ['mock_semantic_fail', 'mock_semantic_inconclusive']);
    assert.equal(readFileSync(join(root, 'topical-generations', id, 'cards', 'first-fact.md'), 'utf8').includes('status: generated'), true);
    const missing = structuredClone(hits[0]);
    missing.mockSemantic.checks = [];
    assert.equal(invoke(root, [missing]).result.excluded[0].reason, 'mock_check_incomplete');
  } finally { rmSync(root, { recursive: true, force: true }); }
});

test('whole-card aggregate budget excludes overflow without slicing the lead', () => {
  const root = sandbox();
  try {
    const src = source(root, 'budget', 'First complete rule stays intact.\nSecond complete rule stays intact.\n');
    const cards = ['first', 'second'].map((name, index) => card(`${name}-rule`, `${name} rule`, [
      claim(`${name}-rule`, `${name} complete rule stays intact.`, 'rule', 'chapter-3', '',
        [evidence(src, `${name[0].toUpperCase()}${name.slice(1)} complete rule stays intact.`, index + 1)]),
    ]));
    const created = compile(root, cards);
    const id = created.result.generationId;
    const hits = ['first', 'second'].map((name, index) =>
      hit(id, `${name}-rule`, staged(root, id, `${name}-rule`), index + 1));
    const full = invoke(root, hits, 2400);
    assert.equal(full.result.selected.length, 2);
    const firstOnlyBudget = full.result.usedChars - 5;
    const limited = invoke(root, hits, firstOnlyBudget);
    assert.equal(limited.result.selected.length, 1);
    assert.equal(limited.result.excluded.find((x) => x.reason === 'whole_card_over_budget').hit, 1);
    assert.match(limited.result.context, /first complete rule stays intact/);
    assert.doesNotMatch(limited.result.context, /second complete rule stays intact/);
    assert.doesNotMatch(limited.result.context, /이하 생략|\.\.\./);
    assert.ok(limited.result.usedChars <= firstOnlyBudget);
  } finally { rmSync(root, { recursive: true, force: true }); }
});

test('same canonical fact does not crowd topN; different plan/actual states remain separate', () => {
  const root = sandbox();
  try {
    const src = source(root, 'dup', 'The bell is planned for dusk.\nThe bell rings at dawn.\nThe river stays blue.\n');
    const plan = card('bell', 'Bell', [claim('planned-bell', 'The bell is planned for dusk.', 'plan', 'chapter-4', '',
      [evidence(src, 'The bell is planned for dusk.', 1)])]);
    const actual = card('bell', 'Bell', [claim('actual-bell', 'The bell rings at dawn.', 'actual', 'chapter-4', '',
      [evidence(src, 'The bell rings at dawn.', 2)])]);
    const river = card('river', 'River', [claim('river-color', 'The river stays blue.', 'observation', 'chapter-4', '',
      [evidence(src, 'The river stays blue.', 3)])]);
    const a = compile(root, [plan], 600).result.generationId;
    const a2 = compile(root, [plan], 601).result.generationId;
    const b = compile(root, [actual]).result.generationId;
    const c = compile(root, [river]).result.generationId;
    const hits = [hit(a, 'bell', staged(root, a, 'bell'), 1), hit(a2, 'bell', staged(root, a2, 'bell'), 2),
      hit(c, 'river', staged(root, c, 'river'), 3), hit(b, 'bell', staged(root, b, 'bell'), 4)];
    const two = invoke(root, hits, 3000, 2);
    assert.deepEqual(two.result.selected.map((x) => x.cardId), ['bell', 'river']);
    assert.ok(two.result.excluded.some((x) => x.reason === 'duplicate_canonical_fact'));
    const three = invoke(root, hits, 3000, 3);
    assert.deepEqual(three.result.selected.map((x) => x.cardId), ['bell', 'river', 'bell']);
    assert.match(three.result.context, /\[plan\|chapter-4\]/);
    assert.match(three.result.context, /\[actual\|chapter-4\]/);
  } finally { rmSync(root, { recursive: true, force: true }); }
});

test('source drift after ingestion is rechecked before injection', () => {
  const root = sandbox();
  try {
    const src = source(root, 'fresh', 'The steady clock ticks once.\n');
    const created = compile(root, [card('clock', 'Clock', [claim('clock-tick', 'The steady clock ticks once.',
      'actual', 'chapter-5', '', [evidence(src, 'The steady clock ticks once.', 1)])])]);
    const id = created.result.generationId;
    const row = hit(id, 'clock', staged(root, id, 'clock'), 1);
    const script = [
      'import json,sys', 'from pathlib import Path', 'sys.path.insert(0,"core")',
      'import wiki_topical_experiment as e', 'root=Path(sys.argv[1])', 'hit=json.loads(sys.argv[2])',
      'prepared=e.prepare_v2_index(root,[hit])', 'assert len(prepared["eligible"])==1',
      '(root/"sources/fresh.md").write_text("New first line.\\nThe steady clock ticks once.\\n")',
      'out=e.render_hook(root,prepared)', 'print(json.dumps(out))',
    ].join('\n');
    const out = JSON.parse(py(script, root, JSON.stringify(row)));
    assert.equal(out.selected.length, 0);
    assert.equal(out.excluded[0].reason, 'source_revision_mismatch');
    assert.doesNotMatch(out.context, /steady clock/);
  } finally { rmSync(root, { recursive: true, force: true }); }
});

test('invalid quote with a rehashed sidecar and tampered schema metadata are excluded', () => {
  const root = sandbox();
  try {
    const src = source(root, 'quote', 'The exact blue signal is visible.\n');
    const created = compile(root, [card('signal', 'Signal', [claim('signal-blue', 'The blue signal is visible.',
      'observation', 'chapter-6', '', [evidence(src, 'The exact blue signal is visible.', 1)])])]);
    const id = created.result.generationId;
    const data = staged(root, id, 'signal');
    const goodHit = hit(id, 'signal', data, 1);
    const gen = join(root, 'topical-generations', id);
    const sidePath = join(gen, 'cards', 'signal.evidence.json');
    const manifestPath = join(gen, 'manifest.json');
    const bad = structuredClone(data);
    bad.claims[0].evidence[0].quoteAnchor = 'Not present in the source.';
    bad.claims[0].evidence[0].quoteSha256 = sha(bad.claims[0].evidence[0].quoteAnchor);
    writeFileSync(sidePath, JSON.stringify(bad) + '\n');
    const manifest = JSON.parse(readFileSync(manifestPath, 'utf8'));
    manifest.cards[0].evidenceSha256 = sha(readFileSync(sidePath));
    writeFileSync(manifestPath, JSON.stringify(manifest) + '\n');
    assert.equal(invoke(root, [goodHit]).result.excluded[0].reason, 'quote_outside_span');
    writeFileSync(sidePath, JSON.stringify(data) + '\n');
    manifest.cards[0].evidenceSha256 = sha(readFileSync(sidePath));
    const mdPath = join(gen, 'cards', 'signal.md');
    const changedMd = readFileSync(mdPath, 'utf8').replace('schemaVersion: 2', 'schemaVersion: 9');
    writeFileSync(mdPath, changedMd);
    manifest.cards[0].markdownSha256 = sha(readFileSync(mdPath));
    writeFileSync(manifestPath, JSON.stringify(manifest) + '\n');
    assert.equal(invoke(root, [goodHit]).result.excluded[0].reason, 'unsupported_schema_version');
  } finally { rmSync(root, { recursive: true, force: true }); }
});

test('multi-source agreement passes; conflicting or incomplete mock evidence does not', () => {
  const root = sandbox();
  try {
    const a = source(root, 'a', 'The blue gate opens at sunrise.\n');
    const b = source(root, 'b', 'At sunrise the blue gate opens.\n');
    const first = card('blue-gate', 'Blue gate', [claim('opening', 'The blue gate opens at sunrise.', 'rule',
      'chapter-7', 'sunrise', [evidence(a, 'The blue gate opens at sunrise.', 1)])]);
    const second = card('blue-gate', 'Blue gate', [claim('opening', 'The blue gate opens at sunrise.', 'rule',
      'chapter-7', 'sunrise', [evidence(b, 'At sunrise the blue gate opens.', 1)])]);
    const created = compile(root, [first, second]);
    const id = created.result.generationId;
    const data = staged(root, id, 'blue-gate');
    assert.equal(data.sourceRevisions.length, 2);
    assert.equal(invoke(root, [hit(id, 'blue-gate', data, 1)]).result.selected.length, 1);
    const disagree = hit(id, 'blue-gate', data, 1, 'fail', { 'opening:sources/a.md': true,
      'opening:sources/b.md': false });
    assert.equal(invoke(root, [disagree]).result.excluded[0].reason, 'mock_semantic_fail');
    disagree.mockSemantic.verdict = 'pass';
    assert.equal(invoke(root, [disagree]).result.excluded[0].reason, 'mock_verdict_inconsistent');
  } finally { rmSync(root, { recursive: true, force: true }); }
});

test('failed staged bundle leaves prior generation untouched and no partial new generation', () => {
  const root = sandbox();
  try {
    const src = source(root, 'atomic', 'The complete fact is preserved.\n');
    const good = card('complete', 'Complete', [claim('complete-fact', 'The complete fact is preserved.',
      'actual', 'chapter-9', '', [evidence(src, 'The complete fact is preserved.', 1)])]);
    const created = compile(root, [good]);
    assert.equal(created.code, 0);
    const prior = readFileSync(join(created.result.path, 'manifest.json'));
    const bad = card('invalid', 'Invalid', [claim('bad-fact', 'A bad unsupported fact.', 'actual', 'chapter-9', '',
      [evidence(src, 'The complete fact is preserved.', 99)])]);
    const rejected = compile(root, [good, bad]);
    assert.equal(rejected.code, 1);
    assert.equal(rejected.result.reason, 'invalid_line_span');
    assert.deepEqual(readFileSync(join(created.result.path, 'manifest.json')), prior);
    assert.equal(readdirSync(join(root, 'topical-generations')).filter((name) => /^[0-9a-f]{24}$/.test(name)).length, 1);
    assert.equal(readdirSync(join(root, 'topical-generations')).filter((name) => name.startsWith('.stage-')).length, 0);
  } finally { rmSync(root, { recursive: true, force: true }); }
});

test('migration dry-run classifies fresh compact legacy as review candidate and long legacy as split needed', () => {
  const root = sandbox();
  try {
    const src = source(root, 'legacy-source', 'The old source contains a compact verified fact.\n');
    const stat = statSync(join(root, src.rel), { bigint: true });
    const revision = `{kind: "file", path: "${src.rel}", collection: "sandbox-source", sha256: "${src.revision}", size: ${stat.size}, mtimeNs: ${stat.mtimeNs}}`;
    const make = (name, body) => ['---', `title: "${name}"`, 'schemaVersion: 1', 'status: verified',
      'createdBy: qmd-auto-context', 'sourceRevisions:', `  - ${revision}`, '---', '## Summary', body, ''].join('\n');
    const short = make('short', 'One compact fact.');
    const long = make('long', 'A'.repeat(650));
    writeFileSync(join(root, 'legacy-cards', 'short.md'), short);
    writeFileSync(join(root, 'legacy-cards', 'long.md'), long);
    const dry = cli(root, 'migration-dry-run', { paths: ['legacy-cards/short.md', 'legacy-cards/long.md'] });
    assert.equal(dry.code, 0);
    assert.deepEqual(dry.result.cards.map((c) => c.migrationClass), [
      'candidate_for_reviewed_conversion', 'semantic_split_or_regeneration_needed',
    ]);
    assert.equal(dry.result.changesApplied, 0);
    assert.equal(dry.result.counts.candidate_for_reviewed_conversion, 1);
    assert.equal(readFileSync(join(root, 'legacy-cards', 'short.md'), 'utf8'), short);
    writeFileSync(join(root, src.rel), 'Changed source.\n');
    assert.equal(cli(root, 'migration-dry-run', { paths: ['legacy-cards/short.md'] })
      .result.cards[0].migrationClass, 'source_unconfirmed');
  } finally { rmSync(root, { recursive: true, force: true }); }
});

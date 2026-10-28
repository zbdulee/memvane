// The project wiki holds only cards. The plugin neither creates nor reads
// wiki-root SCHEMA.md / index.md / log.md: card rules ship in the bundle
// (core/wiki_card_rules), the card catalog is memvane-db, and the activity log
// lives outside the project (core/wiki_activity_log.py).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, symlinkSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { tmpdir } from 'node:os';
import { removeTemp } from './helpers/temp.mjs';
import { activityLogPath, readActivityLog } from './helpers/wiki_activity.mjs';

const ROOT_FILES = ['SCHEMA.md', 'index.md', 'log.md'];
const SENTINEL = 'PROJECT_ROOT_FILE_SENTINEL_7f3a';
const bundled = (name) => readFileSync(join('core', 'wiki_card_rules', `${name}.md`), 'utf8');

function py(lines, env = {}) {
  return execFileSync('python3', ['-c', lines.join('\n')], {
    cwd: process.cwd(), encoding: 'utf8', env: { ...process.env, ...env },
  });
}

test('wiki_init creates the wiki and type directories but no root files (default and novel)', () => {
  for (const [preset, dirs] of [
    ['default', ['comparisons', 'concepts', 'decisions', 'entities']],
    ['novel', ['characters', 'decisions', 'discarded', 'plot', 'sessions', 'style', 'timeline', 'world']],
  ]) {
    const project = mkdtempSync(join(tmpdir(), `wiki-init-${preset}-`));
    try {
      writeFileSync(join(project, '.keep'), '');
      execFileSync('python3', ['core/wiki_init.py', project, preset], { encoding: 'utf8' });
      const wiki = join(project, '.auto-context', 'wiki');
      for (const name of ROOT_FILES) {
        assert.equal(existsSync(join(wiki, name)), false, `${preset}: ${name} must not be created`);
      }
      assert.deepEqual(readdirSync(wiki).sort(), dirs, preset);
    } finally { removeTemp(project); }
  }
});

// layout: 'default' (default wiki dirs), 'manuscript' (owner's novel layout:
// numbered root dirs incl. 04_Manuscript/, default wiki dirs), 'wikiNovel'
// (novel-preset wiki dir only, no manuscript root dir).
function setupProject({ layout = 'default' } = {}) {
  const dir = mkdtempSync(join(tmpdir(), 'wiki-cards-only-'));
  const wiki = join(dir, '.auto-context', 'wiki');
  mkdirSync(join(dir, '.auto-context', 'compile'), { recursive: true });
  mkdirSync(wiki, { recursive: true });
  for (const name of ['concepts', 'decisions', 'entities', 'comparisons']) mkdirSync(join(wiki, name));
  if (layout === 'manuscript') {
    for (const name of ['01_Settings', '02_Characters', '03_Plot', '04_Manuscript']) mkdirSync(join(dir, name));
    writeFileSync(join(dir, 'build_nova_index.py'), '# synthetic\n');
  }
  if (layout === 'wikiNovel') mkdirSync(join(wiki, 'characters'));
  mkdirSync(join(dir, 'docs'), { recursive: true });
  writeFileSync(join(dir, 'docs', 'source.md'), '# Source\n\nDurable decision: generated wiki pages cite source markdown.\n');
  writeFileSync(join(dir, '.auto-context', 'settings.json'), JSON.stringify({
    indexing: true,
    collections: ['proj-docs', 'proj-wiki'],
    collectionPaths: { 'proj-docs': 'docs', 'proj-wiki': '.auto-context/wiki' },
    collectionRoles: { 'proj-docs': 'raw', 'proj-wiki': 'wiki' },
    wikiPath: '.auto-context/wiki',
    compile: {
      mode: 'guarded', defaultStatus: 'generated', triggers: ['post_tool_source', 'manual'],
      maxSourceChars: 12000, extractor: { backends: {}, timeout: 30 },
      verify: { enabled: false },
    },
  }));
  return dir;
}

function captureExtractor(capture) {
  const extractor = join(mkdtempSync(join(tmpdir(), 'extractor-')), 'extract.py');
  writeFileSync(extractor, `#!/usr/bin/env python3
import json, sys
payload = json.loads(sys.stdin.read())
sys.path.insert(0, ${JSON.stringify(join(process.cwd(), 'core', 'extractors'))})
import lib
assert payload.get('task') != 'verify', 'verify is disabled in this fixture'
open(${JSON.stringify(capture)}, 'w', encoding='utf-8').write(json.dumps({
  'payload': payload, 'prompt': lib.build_prompt(payload)}))
print(json.dumps({'candidates': [{
  'title': 'Source Compile Decision',
  'summary': 'Generated wiki pages cite source markdown instead of copying raw source.',
  'suggestedType': 'decision', 'confidence': 'high',
  'targetPath': '.auto-context/wiki/decisions/source-compile-decision.md'
}]}))
`);
  return extractor;
}

function runCompile(project, extractor) {
  const settingsPath = join(project, '.auto-context', 'settings.json');
  const settings = JSON.parse(readFileSync(settingsPath, 'utf8'));
  settings.compile.extractor = { backends: { claude: ['python3', extractor] }, timeout: 30 };
  writeFileSync(settingsPath, JSON.stringify(settings));
  writeFileSync(join(project, '.auto-context', 'compile', 'source-queue.jsonl'), JSON.stringify({
    ts: '2026-10-28T00:00:00Z', trigger: 'post_tool_source', engine: 'claude', cwd: project,
    source: { kind: 'file', path: 'docs/source.md', collection: 'proj-docs' },
  }) + '\n');
  const pending = JSON.parse(py([
    'import json, sys', 'from pathlib import Path', 'sys.path.insert(0, "core")',
    'import wiki_freshness as F',
    `print(json.dumps(F.record_pending_refresh(Path(${JSON.stringify(project)}), "docs/source.md", "claude")))`,
  ]));
  const queue = join(project, '.auto-context', 'compile', 'source-queue.jsonl');
  const job = JSON.parse(readFileSync(queue, 'utf8').trim());
  job.pendingRefresh = { eventId: pending.eventId, ts: pending.ts, engine: pending.engine };
  writeFileSync(queue, JSON.stringify(job) + '\n');
  execFileSync('python3', ['core/wiki_compile_worker.py', '--cwd', project, '--flush-all'], {
    cwd: process.cwd(), encoding: 'utf8',
    env: { ...process.env, QMD_DIRTY_QUEUE: join(project, '.auto-context', 'compile', 'dirty-queue') },
  });
}

for (const [layout, preset] of [['default', 'default'], ['manuscript', 'novel'], ['wikiNovel', 'novel']]) {
  test(`compile prompt carries the bundled ${preset} card rules for a ${layout} layout and never reads project root files`, () => {
    const project = setupProject({ layout });
    const capture = join(mkdtempSync(join(tmpdir(), 'capture-')), 'payload.json');
    try {
      const wiki = join(project, '.auto-context', 'wiki');
      // An existing install keeps its old root files; a customised SCHEMA.md is one.
      const leftovers = {};
      for (const name of ROOT_FILES) {
        leftovers[name] = `# Leftover ${name}\n\n${SENTINEL} ${name}\n`;
        writeFileSync(join(wiki, name), leftovers[name]);
      }
      const logPath = activityLogPath(project);
      mkdirSync(dirname(logPath), { recursive: true, mode: 0o700 });
      writeFileSync(logPath, '- 2026-10-27T00:00:00Z created concepts/earlier.md - Earlier Card\n');

      runCompile(project, captureExtractor(capture));

      const { payload, prompt } = JSON.parse(readFileSync(capture, 'utf8'));
      assert.equal(payload.wiki.schema, bundled(preset));
      assert.notEqual(bundled('default'), bundled('novel'));
      assert.ok(prompt.includes(`CARD RULES (bundled with the plugin):\n${bundled(preset).slice(0, 200)}`));
      if (preset === 'novel') assert.match(prompt, /settings, plot, Nova, and manuscript/);
      else assert.doesNotMatch(prompt, /Nova/);
      assert.doesNotMatch(JSON.stringify(payload), new RegExp(SENTINEL), 'no project root file reaches the payload');
      assert.equal('index' in payload.wiki, false);
      assert.match(payload.wiki.logTail, /created concepts\/earlier\.md - Earlier Card/);

      assert.ok(existsSync(join(wiki, 'decisions', 'source-compile-decision.md')), 'the card is written');
      for (const name of ROOT_FILES) {
        assert.equal(readFileSync(join(wiki, name), 'utf8'), leftovers[name], `${name} is left untouched`);
      }
      assert.match(readActivityLog(project), /created decisions\/source-compile-decision\.md - Source Compile Decision/);
    } finally { removeTemp(project); removeTemp(dirname(capture)); }
  });
}

test('a fresh wiki gets no root files from compile; the activity line goes to the state root', () => {
  const project = setupProject();
  const capture = join(mkdtempSync(join(tmpdir(), 'capture-')), 'payload.json');
  try {
    runCompile(project, captureExtractor(capture));
    const wiki = join(project, '.auto-context', 'wiki');
    assert.ok(existsSync(join(wiki, 'decisions', 'source-compile-decision.md')), 'the card is written');
    for (const name of ROOT_FILES) assert.equal(existsSync(join(wiki, name)), false, name);
    assert.ok(!activityLogPath(project).startsWith(project), 'log lives outside the project');
    assert.match(readActivityLog(project), /created decisions\/source-compile-decision\.md/);
  } finally { removeTemp(project); removeTemp(dirname(capture)); }
});

test('dedup merge logs the deletion to the state root and leaves a leftover index.md as is', () => {
  const project = setupProject();
  try {
    const wiki = join(project, '.auto-context', 'wiki');
    mkdirSync(join(wiki, 'entities'), { recursive: true });
    writeFileSync(join(wiki, 'entities', 'loser.md'), '---\ntitle: Loser Card\n---\n\nloser\n');
    writeFileSync(join(wiki, 'entities', 'winner.md'), '---\ntitle: Winner Card\n---\n\nwinner\n');
    const leftover = '# Auto-context Wiki Index\n\n- entities/loser.md - Loser Card\n';
    writeFileSync(join(wiki, 'index.md'), leftover);
    writeFileSync(join(project, '.auto-context', 'compile', 'dedup-needed.jsonl'),
      JSON.stringify({ pageA: 'entities/loser.md', pageB: 'entities/winner.md', score: 0.95 }) + '\n');
    const out = JSON.parse(execFileSync('python3', [
      'core/wiki_dedup_resolve.py', '--cwd', project,
      '--index', '0', '--action', 'merge', '--delete', 'entities/loser.md',
    ], { encoding: 'utf8' }));
    assert.equal(out.action, 'deleted');
    assert.equal(existsSync(join(wiki, 'entities', 'loser.md')), false);
    assert.equal(readFileSync(join(wiki, 'index.md'), 'utf8'), leftover);
    assert.equal(existsSync(join(wiki, 'log.md')), false);
    assert.match(readActivityLog(project), /deleted entities\/loser\.md - Loser Card/);
  } finally { removeTemp(project); }
});

test('wiki_activity_log: install state root, owner-only guard, size cap and tail', () => {
  const state = mkdtempSync(join(tmpdir(), 'wiki-activity-state-'));
  const project = mkdtempSync(join(tmpdir(), 'wiki-activity-project-'));
  try {
    const out = JSON.parse(py([
      'import json, os, sys', 'from pathlib import Path', 'sys.path.insert(0, "core")',
      'import wiki_activity_log as L',
      `state = Path(${JSON.stringify(state)}).resolve(); project = Path(${JSON.stringify(project)})`,
      'os.chmod(state, 0o700)',
      'bound = ({"journalPath": str(state / "install-journal.sqlite")}, {})',
      'result = {"path": str(L.log_path(project, bound))}',
      'result["wrote"] = L.append(project, "created", "concepts/a.md", "A\\nB", bound=bound)',
      'result["line"] = (state / L.LOG_NAME).read_text()',
      'os.chmod(state, 0o755)',
      'result["unsafePath"] = L.log_path(project, bound) is None',
      'result["unsafeWrote"] = L.append(project, "created", "concepts/b.md", "B", bound=bound)',
      'os.chmod(state, 0o700)',
      'L.LOG_LIMIT = 300',
      'for i in range(40): L.append(project, "updated", f"concepts/c{i}.md", "C", bound=bound)',
      'live = (state / L.LOG_NAME).stat().st_size',
      'result["capped"] = live <= L.LOG_LIMIT + 200 and (state / (L.LOG_NAME + ".1")).exists()',
      'result["tail"] = L.tail(project, bound, limit=60)',
      'result["noJournal"] = L.log_path(project, ({}, {})) is None',
      'print(json.dumps(result))',
    ]));
    assert.equal(out.path.endsWith('/wiki-activity.log'), true);
    assert.equal(out.wrote, true);
    assert.match(out.line, /^- \d{4}-\d\d-\d\dT[\d:]+Z created concepts\/a\.md - A B\n$/);
    assert.equal(out.unsafePath, true);
    assert.equal(out.unsafeWrote, false);
    assert.equal(out.capped, true);
    assert.equal(out.tail.length <= 60, true);
    assert.match(out.tail, /concepts\/c39\.md - C\n$/);
    assert.equal(out.noJournal, true);
    assert.equal(existsSync(join(project, '.auto-context')), false, 'nothing is written into the project');
  } finally { removeTemp(state); removeTemp(project); }
});

test('card_rules_preset: a numbered *_Manuscript root dir means novel; other near-misses do not', () => {
  const base = mkdtempSync(join(tmpdir(), 'card-rules-preset-'));
  try {
    const cases = {
      owner04: { dirs: ['01_Settings', '02_Characters', '03_Plot', '04_Manuscript'] },
      owner03: { dirs: ['01_Settings', '02_Characters', '03_Manuscript'] },
      plain: { dirs: ['docs', 'src'] },
      noDigits: { dirs: ['Manuscript', '_Manuscript'] },
      lowercase: { dirs: ['04_manuscript'] },
      suffixed: { dirs: ['04_Manuscript_old', 'x04_Manuscript'] },
      fileNotDir: { files: ['04_Manuscript'] },
      symlinkDir: { dirs: ['real'], links: [['real', '04_Manuscript']] },
      nestedOnly: { dirs: ['docs', 'docs/04_Manuscript'] },
    };
    for (const [name, spec] of Object.entries(cases)) {
      const root = join(base, name);
      mkdirSync(join(root, '.auto-context', 'wiki'), { recursive: true });
      for (const dir of spec.dirs || []) mkdirSync(join(root, dir), { recursive: true });
      for (const file of spec.files || []) writeFileSync(join(root, file), '');
      for (const [target, link] of spec.links || []) symlinkSync(join(root, target), join(root, link));
    }
    const out = JSON.parse(py([
      'import json, os, sys', 'from pathlib import Path', 'sys.path.insert(0, "core")',
      'import wiki_compile_worker as w',
      `base = Path(${JSON.stringify(base)})`,
      'before = {p: sorted(os.listdir(base / p)) for p in os.listdir(base)}',
      'result = {p: w.card_rules_preset(base / p, base / p / ".auto-context" / "wiki", {}) for p in sorted(os.listdir(base))}',
      'result["_unchanged"] = before == {p: sorted(os.listdir(base / p)) for p in os.listdir(base)}',
      'print(json.dumps(result))',
    ]));
    assert.deepEqual(out, {
      owner04: 'novel', owner03: 'novel', plain: 'default', noDigits: 'default',
      lowercase: 'default', suffixed: 'default', fileNotDir: 'default',
      symlinkDir: 'default', nestedOnly: 'default', _unchanged: true,
    });
  } finally { removeTemp(base); }
});

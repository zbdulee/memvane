// The project wiki holds only cards. The plugin neither creates nor reads
// wiki-root SCHEMA.md / index.md / log.md: card rules ship in the bundle
// (core/wiki_card_rules), the card catalog is memvane-db, and the activity log
// is the install event log's `wiki` events (core/event_log.py).
//
// Every child process here runs with a temporary HOME: nothing may reach the
// owner's real per-user state.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import {
  chmodSync, existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, realpathSync,
  symlinkSync, writeFileSync,
} from 'node:fs';
import { dirname, join } from 'node:path';
import { tmpdir } from 'node:os';
import { removeTemp } from './helpers/temp.mjs';

const ROOT_FILES = ['SCHEMA.md', 'index.md', 'log.md'];
const SENTINEL = 'PROJECT_ROOT_FILE_SENTINEL_7f3a';
const bundled = (name) => readFileSync(join('core', 'wiki_card_rules', `${name}.md`), 'utf8');

// Real paths: the activity log refuses any symlinked ancestor (macOS /var).
function realTemp(prefix) {
  return realpathSync(mkdtempSync(join(tmpdir(), prefix)));
}

function withHome(fn) {
  const home = realTemp('wiki-cards-home-');
  try {
    return fn(home);
  } finally {
    // Nothing per-user is written for an unselected (fixture) project.
    const state = join(home, '.local', 'state');
    assert.equal(existsSync(state) ? readdirSync(state).length : 0, 0,
      'no per-user state is written');
    removeTemp(home);
  }
}

function env(home, extra = {}) {
  return { ...process.env, HOME: home, ...extra };
}

function py(home, lines) {
  return execFileSync('python3', ['-c', lines.join('\n')], {
    cwd: process.cwd(), encoding: 'utf8', env: env(home),
  });
}

test('wiki_init creates the wiki and type directories but no root files (default and novel)', () => {
  withHome((home) => {
    for (const [preset, dirs] of [
      ['default', ['comparisons', 'concepts', 'decisions', 'entities']],
      ['novel', ['characters', 'decisions', 'discarded', 'plot', 'sessions', 'style', 'timeline', 'world']],
    ]) {
      const project = realTemp(`wiki-init-${preset}-`);
      try {
        writeFileSync(join(project, '.keep'), '');
        execFileSync('python3', ['core/wiki_init.py', project, preset], { encoding: 'utf8', env: env(home) });
        const wiki = join(project, '.auto-context', 'wiki');
        for (const name of ROOT_FILES) {
          assert.equal(existsSync(join(wiki, name)), false, `${preset}: ${name} must not be created`);
        }
        assert.deepEqual(readdirSync(wiki).sort(), dirs, preset);
      } finally { removeTemp(project); }
    }
  });
});

// layout: 'default' (default wiki dirs), 'manuscript' (owner's novel layout:
// numbered root dirs incl. 04_Manuscript/, default wiki dirs), 'wikiNovel'
// (novel-preset wiki dir only, no manuscript root dir).
function setupProject({ layout = 'default' } = {}) {
  const dir = realTemp('wiki-cards-only-');
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
  const extractor = join(realTemp('extractor-'), 'extract.py');
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

function runCompile(home, project, extractor) {
  const settingsPath = join(project, '.auto-context', 'settings.json');
  const settings = JSON.parse(readFileSync(settingsPath, 'utf8'));
  settings.compile.extractor = { backends: { claude: ['python3', extractor] }, timeout: 30 };
  writeFileSync(settingsPath, JSON.stringify(settings));
  writeFileSync(join(project, '.auto-context', 'compile', 'source-queue.jsonl'), JSON.stringify({
    ts: '2026-10-28T00:00:00Z', trigger: 'post_tool_source', engine: 'claude', cwd: project,
    source: { kind: 'file', path: 'docs/source.md', collection: 'proj-docs' },
  }) + '\n');
  const pending = JSON.parse(py(home, [
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
    env: env(home, { QMD_DIRTY_QUEUE: join(project, '.auto-context', 'compile', 'dirty-queue') }),
  });
}

for (const [layout, preset] of [['default', 'default'], ['manuscript', 'novel'], ['wikiNovel', 'novel']]) {
  test(`compile prompt carries the bundled ${preset} card rules for a ${layout} layout and never reads project root files`, () => {
    withHome((home) => {
      const project = setupProject({ layout });
      const capture = join(realTemp('capture-'), 'payload.json');
      try {
        const wiki = join(project, '.auto-context', 'wiki');
        // An existing install keeps its old root files; a customised SCHEMA.md is one.
        const leftovers = {};
        for (const name of ROOT_FILES) {
          leftovers[name] = `# Leftover ${name}\n\n${SENTINEL} ${name}\n`;
          writeFileSync(join(wiki, name), leftovers[name]);
        }

        runCompile(home, project, captureExtractor(capture));

        const { payload, prompt } = JSON.parse(readFileSync(capture, 'utf8'));
        assert.equal(payload.wiki.schema, bundled(preset));
        assert.notEqual(bundled('default'), bundled('novel'));
        assert.ok(prompt.includes(`CARD RULES (bundled with the plugin):\n${bundled(preset).slice(0, 200)}`));
        if (preset === 'novel') assert.match(prompt, /settings, plot, Nova, and manuscript/);
        else assert.doesNotMatch(prompt, /Nova/);
        assert.doesNotMatch(JSON.stringify(payload), new RegExp(SENTINEL), 'no project root file reaches the payload');
        assert.equal('index' in payload.wiki, false);
        assert.equal(payload.wiki.logTail, '', 'an unselected project has no activity log');

        assert.ok(existsSync(join(wiki, 'decisions', 'source-compile-decision.md')), 'the card is written');
        for (const name of ROOT_FILES) {
          assert.equal(readFileSync(join(wiki, name), 'utf8'), leftovers[name], `${name} is left untouched`);
        }
      } finally { removeTemp(project); removeTemp(dirname(capture)); }
    });
  });
}

test('a fresh wiki gets no root files from compile', () => {
  withHome((home) => {
    const project = setupProject();
    const capture = join(realTemp('capture-'), 'payload.json');
    try {
      runCompile(home, project, captureExtractor(capture));
      const wiki = join(project, '.auto-context', 'wiki');
      assert.ok(existsSync(join(wiki, 'decisions', 'source-compile-decision.md')), 'the card is written');
      for (const name of ROOT_FILES) assert.equal(existsSync(join(wiki, name)), false, name);
    } finally { removeTemp(project); removeTemp(dirname(capture)); }
  });
});

test('wiki_compile never targets a leftover root meta page, by identity or by explicit targetPath', () => {
  withHome((home) => {
    const project = setupProject();
    try {
      const wiki = join(project, '.auto-context', 'wiki');
      // A leftover page whose frontmatter claims the candidate's identity.
      const leftover = '---\ntitle: Config Layout Decision\ncanonicalKey: config-layout\n'
        + 'aliases: [config layout]\nstatus: generated\ncreatedBy: memvane\n---\n'
        + `${SENTINEL}\n`;
      for (const name of ROOT_FILES) writeFileSync(join(wiki, name), leftover);
      const run = (candidate) => JSON.parse(execFileSync('python3', ['core/wiki_compile.py', '--cwd', project], {
        encoding: 'utf8', input: JSON.stringify({
          trigger: 'manual', title: 'Config Layout Decision', canonicalKey: 'config-layout',
          summary: 'Canonical config lives in .auto-context/settings.json.',
          suggestedType: 'decision', confidence: 'high',
          sources: [{ kind: 'file', path: 'docs/source.md' }], ...candidate,
        }),
        env: env(home, { QMD_DIRTY_QUEUE: join(project, 'dirty-queue') }),
      }).trim().split('\n').pop());
      const byIdentity = run({});
      assert.equal(byIdentity.action, 'created', JSON.stringify(byIdentity));
      assert.match(byIdentity.targetPath, /decisions\/config-layout-decision\.md$/);
      for (const name of ['index.md', 'log.md']) {
        const explicit = run({ title: `Explicit ${name}`, canonicalKey: `explicit-${name}`,
          targetPath: `.auto-context/wiki/${name}` });
        assert.ok(!String(explicit.targetPath || '').endsWith(`wiki/${name}`), JSON.stringify(explicit));
      }
      for (const name of ROOT_FILES) {
        assert.equal(readFileSync(join(wiki, name), 'utf8'), leftover, `${name} is never written`);
      }
    } finally { removeTemp(project); }
  });
});

test('dedup merge with a selected install logs to its state root and leaves a leftover index.md as is', () => {
  withHome((home) => {
    const project = setupProject();
    const state = realTemp('wiki-activity-state-');
    try {
      chmodSync(state, 0o700);
      const wiki = join(project, '.auto-context', 'wiki');
      writeFileSync(join(wiki, 'entities', 'loser.md'), '---\ntitle: Loser Card\n---\n\nloser\n');
      writeFileSync(join(wiki, 'entities', 'winner.md'), '---\ntitle: Winner Card\n---\n\nwinner\n');
      const leftover = '# Auto-context Wiki Index\n\n- entities/loser.md - Loser Card\n';
      writeFileSync(join(wiki, 'index.md'), leftover);
      const out = JSON.parse(py(home, [
        'import json, sys', 'from pathlib import Path', 'from unittest.mock import patch',
        'sys.path.insert(0, "core")',
        'import wiki_dedup_resolve as R, wiki_selected_laya as S',
        `root = Path(${JSON.stringify(project)}); wiki = root / ".auto-context" / "wiki"`,
        `bound = ({"journalPath": ${JSON.stringify(join(state, 'install-journal.sqlite'))}}, {})`,
        'compile_dir = root / ".auto-context" / "compile"',
        'entry = {"pageA": "entities/loser.md", "pageB": "entities/winner.md", "score": 0.95}',
        'with patch.object(S, "recheck"), patch.object(S, "request_sync", return_value=True):',
        '    result = R.resolve_entry(root, wiki, compile_dir, entry, "merge", "entities/loser.md", laya_bound=bound)',
        'print(json.dumps(result))',
      ]));
      assert.equal(out.action, 'deleted');
      assert.equal(existsSync(join(wiki, 'entities', 'loser.md')), false);
      assert.equal(readFileSync(join(wiki, 'index.md'), 'utf8'), leftover);
      assert.equal(existsSync(join(wiki, 'log.md')), false);
      assert.equal(existsSync(join(state, 'wiki-activity.log')), false);
      const events = readFileSync(join(state, 'events.jsonl'), 'utf8').trim().split('\n').map(JSON.parse);
      assert.equal(events.length, 1);
      assert.equal(events[0].v, 1);
      assert.equal(events[0].event, 'wiki');
      assert.equal(events[0].outcome, 'deleted');
      assert.equal(events[0].reason, 'dedup');
      assert.equal(events[0].path, 'entities/loser.md');
      assert.equal(events[0].title, 'Loser Card');
    } finally { removeTemp(project); removeTemp(state); }
  });
});

test('wiki activity: event log in the state root only, no-follow walk, owner-only guards, rotation and tail', () => {
  withHome((home) => {
    const base = realTemp('wiki-activity-');
    const project = realTemp('wiki-activity-project-');
    try {
      const out = JSON.parse(py(home, [
        'import json, os, sys', 'from pathlib import Path', 'sys.path.insert(0, "core")',
        'import event_log as E, wiki_activity_log as L, wiki_compile_worker as W',
        `base = Path(${JSON.stringify(base)}); project = Path(${JSON.stringify(project)})`,
        'def bound(d): return ({"journalPath": str(d / "install-journal.sqlite"), "installId": "i1"}, {})',
        'state = base / "state"; state.mkdir(mode=0o700); os.chmod(state, 0o700)',
        'r = {}',
        'r["root"] = str(E.wiki_state_root(project, bound(state)))',
        'r["wrote"] = L.append(project, "created", "concepts/a.md", "A\\nB", bound=bound(state), reason="compile")',
        'r["line"] = json.loads((state / E.NAME).read_text())',
        'r["legacy"] = (state / "wiki-activity.log").exists()',
        // Unselected project: no state root, nothing written anywhere.
        'r["noBound"] = [E.wiki_state_root(project, None), L.append(project, "created", "x.md", "X", bound=None), L.tail(project, None)]',
        'r["noJournal"] = L.append(project, "created", "x.md", "X", bound=({}, {}))',
        // A symlinked ancestor is refused and nothing lands behind it.
        'real = base / "real"; (real / "inner").mkdir(parents=True, mode=0o700); os.chmod(real / "inner", 0o700)',
        'os.symlink(real, base / "link")',
        'r["symlinkAncestor"] = [L.append(project, "created", "s.md", "S", bound=bound(base / "link" / "inner")), (real / "inner" / E.NAME).exists()]',
        // A group/world-writable non-sticky ancestor is refused.
        'loose = base / "loose"; (loose / "inner").mkdir(parents=True); os.chmod(loose, 0o777); os.chmod(loose / "inner", 0o700)',
        'r["looseAncestor"] = [L.append(project, "created", "l.md", "L", bound=bound(loose / "inner")), (loose / "inner" / E.NAME).exists()]',
        // A state root with group/other bits is refused.
        'wide = base / "wide"; wide.mkdir(); os.chmod(wide, 0o755)',
        'r["wideLeaf"] = [L.append(project, "created", "w.md", "W", bound=bound(wide)), (wide / E.NAME).exists()]',
        // A symlinked log file is refused; its target is untouched.
        'other = base / "other.txt"; other.write_text("keep")',
        'lnk = base / "lnkstate"; lnk.mkdir(mode=0o700); os.chmod(lnk, 0o700); os.symlink(other, lnk / E.NAME)',
        'r["symlinkFile"] = [L.append(project, "created", "f.md", "F", bound=bound(lnk)), other.read_text()]',
        // Rotation and tail: non-wiki events never reach the tail.
        'E.LOG_LIMIT = 600',
        'for i in range(40): L.append(project, "updated", f"concepts/c{i}.md", "C", bound=bound(state)); E.emit("recall", state_root=state, outcome="served", prompt=E.prompt_fields("SECRET PROMPT"))',
        'r["capped"] = (state / E.NAME).stat().st_size <= E.LOG_LIMIT and (state / (E.NAME + ".1")).exists()',
        'r["tail"] = L.tail(project, bound(state), limit=60)',
        // The compile worker reads its logTail from the state root, never from the project.
        'wiki = project / ".auto-context" / "wiki"; wiki.mkdir(parents=True)',
        `(wiki / "log.md").write_text("# Auto-context Wiki Log\\n\\n${SENTINEL}\\n")`,
        'o = W.orientation(project, wiki, {}, bound(state))',
        `r["orientation"] = [o["logTail"].endswith("concepts/c39.md - C\\n"), "${SENTINEL}" in json.dumps(o), "recall" in o["logTail"], "sha256" in o["logTail"]]`,
        'print(json.dumps(r))',
      ]));
      assert.equal(out.root, join(base, 'state'));
      assert.equal(out.wrote, true);
      assert.equal(out.legacy, false);
      assert.equal(out.line.event, 'wiki');
      assert.equal(out.line.outcome, 'created');
      assert.equal(out.line.reason, 'compile');
      assert.equal(out.line.path, 'concepts/a.md');
      assert.equal(out.line.title, 'A B');
      assert.equal(out.line.install, 'i1');
      assert.match(out.line.ts, /^\d{4}-\d\d-\d\dT[\d:.]+Z$/);
      assert.deepEqual(out.noBound, [null, false, '']);
      assert.equal(out.noJournal, false);
      assert.deepEqual(out.symlinkAncestor, [false, false]);
      assert.deepEqual(out.looseAncestor, [false, false]);
      assert.deepEqual(out.wideLeaf, [false, false]);
      assert.deepEqual(out.symlinkFile, [false, 'keep']);
      assert.equal(out.capped, true);
      assert.equal(out.tail.length <= 60, true);
      assert.match(out.tail, /concepts\/c39\.md - C\n$/);
      assert.deepEqual(out.orientation, [true, false, false, false]);
    } finally { removeTemp(base); removeTemp(project); }
  });
});

test('card_rules_preset: a numbered *_Manuscript root dir means novel; other near-misses do not', () => {
  withHome((home) => {
    const base = realTemp('card-rules-preset-');
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
      const out = JSON.parse(py(home, [
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
});

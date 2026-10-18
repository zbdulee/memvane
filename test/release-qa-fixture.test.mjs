import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import {
  copyFileSync, existsSync, lstatSync, mkdirSync, mkdtempSync, readFileSync,
  readdirSync, realpathSync, rmSync, symlinkSync, writeFileSync,
} from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

const REPO = fileURLToPath(new URL('..', import.meta.url));
const HELPER = join(REPO, 'test/fixtures/release-qa-fixture.py');
const PARENT_PREFIX = '/tmp/qmd-rqa-test-';

// Each test gets an owner-private (mkdtemp 0700) parent under /tmp, a synthetic
// HOME inside it, and a minimal env: no GIT_*, QMD_* or plugin variables.
function withSandbox(fn) {
  const parent = mkdtempSync(PARENT_PREFIX);
  try {
    const home = join(parent, 'home');
    mkdirSync(home);
    const env = {
      PATH: process.env.PATH, HOME: home, XDG_CONFIG_HOME: join(home, '.config'),
      LC_ALL: 'C', PYTHONDONTWRITEBYTECODE: '1',
    };
    const run = (...args) => spawnSync('python3', ['-B', HELPER, ...args],
      { cwd: REPO, env, encoding: 'utf8', timeout: 60000 });
    const ok = (...args) => {
      const p = run(...args);
      assert.equal(p.status, 0, `${args.join(' ')}: ${p.stdout}${p.stderr}`);
      return JSON.parse(p.stdout);
    };
    const refused = (reason, ...args) => {
      const p = run(...args);
      assert.equal(p.status, 1, `${args.join(' ')} should be refused: ${p.stdout}${p.stderr}`);
      assert.equal(JSON.parse(p.stderr).reason, reason, p.stderr);
    };
    fn({ parent, home, ok, refused });
  } finally {
    if (parent.startsWith(PARENT_PREFIX)) rmSync(parent, { recursive: true, force: true });
  }
}

const read = (...parts) => readFileSync(join(...parts), 'utf8');

test('release QA fixture refuses occupied, repository-control, host-state and configured targets', () => {
  withSandbox(({ parent, home, refused }) => {
    const occupied = join(parent, 'occupied');
    mkdirSync(occupied);
    writeFileSync(join(occupied, 'keep.txt'), 'user data\n');
    refused('target_not_empty_directory', 'create', occupied);
    assert.deepEqual(readdirSync(occupied), ['keep.txt']);
    assert.equal(read(occupied, 'keep.txt'), 'user data\n');

    mkdirSync(join(parent, 'repo', '.git'), { recursive: true });
    const inGitDir = join(parent, 'repo', '.git', 'fixture');
    refused('inside_repository_or_host_state', 'create', inGitDir);

    mkdirSync(join(home, 'Library'));
    const inHostState = join(home, 'Library', 'fixture');
    refused('inside_host_or_runtime_state', 'create', inHostState);

    // Upward config discovery would make the fixture look already opted in.
    const configured = join(parent, 'configured');
    mkdirSync(configured);
    writeFileSync(join(configured, '.auto-context.json'), '{}\n');
    const underConfig = join(configured, 'fixture');
    refused('ancestor_has_auto_context_config', 'create', underConfig);

    refused('target_must_be_absolute_new_directory', 'create', 'relative-fixture');

    for (const target of [inGitDir, inHostState, underConfig, join(REPO, 'relative-fixture')]) {
      assert.equal(existsSync(target), false, `refused create left ${target}`);
    }
  });
});

test('release QA fixture simulates pull, branch, worktree and dirty work without global Git changes, then cleans only itself', () => {
  withSandbox(({ parent, home, ok, refused }) => {
    const bystander = join(parent, 'bystander');
    mkdirSync(bystander);
    writeFileSync(join(bystander, 'keep.txt'), 'not a fixture\n');

    const fixture = join(parent, 'fixture');
    const created = ok('create', fixture);
    const root = join(realpathSync(parent), 'fixture');
    assert.equal(created.root, root);
    const { project, worktree } = created.paths;
    const tokens = created.tokens;
    assert.match(read(project, 'docs/qa/alpha.md'), new RegExp(tokens.ALPHA_MAIN));
    assert.match(read(project, 'docs/qa/beta.md'), new RegExp(tokens.BETA_V1));

    // Never overwrites an existing fixture.
    refused('target_not_empty_directory', 'create', fixture);

    // Upstream change is invisible until the simulated user pull.
    ok('stage', fixture, 'upstream-change');
    assert.equal(existsSync(join(project, 'docs/qa/delta.md')), false);
    ok('stage', fixture, 'pull');
    assert.match(read(project, 'docs/qa/delta.md'), new RegExp(tokens.DELTA_ADDED));
    assert.match(read(project, 'docs/qa/beta.md'), new RegExp(tokens.BETA_V2));
    assert.doesNotMatch(read(project, 'docs/qa/beta.md'), new RegExp(tokens.BETA_V1));
    assert.equal(existsSync(join(project, 'docs/qa/gamma.md')), false);

    ok('stage', fixture, 'switch-feature');
    assert.match(read(project, 'docs/qa/alpha.md'), new RegExp(tokens.ALPHA_FEATURE));
    assert.match(read(project, 'docs/qa/feature-only.md'), new RegExp(tokens.FEATURE_ONLY));
    ok('stage', fixture, 'switch-main');
    assert.match(read(project, 'docs/qa/alpha.md'), new RegExp(tokens.ALPHA_MAIN));
    assert.equal(existsSync(join(project, 'docs/qa/feature-only.md')), false);

    ok('stage', fixture, 'worktree-add');
    assert.equal(worktree, join(root, 'wt-worktree'));
    assert.match(read(worktree, 'docs/qa/worktree-only.md'), new RegExp(tokens.WORKTREE_ONLY));
    assert.match(read(worktree, 'docs/qa/alpha.md'), new RegExp(tokens.ALPHA_WORKTREE));
    assert.doesNotMatch(read(worktree, 'docs/qa/alpha.md'), new RegExp(tokens.ALPHA_MAIN));
    assert.equal(existsSync(join(project, 'docs/qa/worktree-only.md')), false);

    ok('stage', fixture, 'dirty');
    const alpha = read(project, 'docs/qa/alpha.md');
    assert.match(alpha, new RegExp(tokens.ALPHA_MAIN));
    assert.match(alpha, new RegExp(tokens.DIRTY_TRACKED));
    assert.match(read(project, 'docs/qa/untracked-note.md'), new RegExp(tokens.UNTRACKED_NEW));

    const shown = ok('show', fixture);
    assert.equal(shown.fixtureId, created.fixtureId);
    assert.deepEqual(shown.events.map((e) => e.action),
      ['create', 'upstream-change', 'pull', 'switch-feature', 'switch-main', 'worktree-add', 'dirty']);
    const [onCreate, onUpstream, onPull, onFeature, onMain] = shown.events;
    assert.equal(onUpstream.projectHead, onCreate.projectHead, 'upstream change touched project');
    assert.notEqual(onPull.projectHead, onCreate.projectHead);
    assert.equal(onFeature.projectBranch, 'qa/feature');
    assert.equal(onMain.projectBranch, 'main');

    // Git isolation: fixture-local config and empty hooks only; nothing global.
    assert.ok(read(project, '.git/config').includes(`hooksPath = ${root}/.qa-no-hooks`),
      'project does not pin the fixture-local empty hooks directory');
    assert.deepEqual(readdirSync(join(root, '.qa-no-hooks')), []);
    assert.equal(read(root, '.qa-gitconfig'), '');
    const projectHooks = join(project, '.git', 'hooks');
    assert.ok(!existsSync(projectHooks) || readdirSync(projectHooks).length === 0,
      'sample hooks were installed');
    assert.deepEqual(readdirSync(home), [], 'helper wrote to HOME (global Git config or host state)');

    // Cleanup refuses anything that is not this marker-owned fixture.
    refused('unsafe_fixture_marker', 'cleanup', bystander);
    const link = join(parent, 'link');
    symlinkSync(fixture, link);
    refused('fixture_path_invalid', 'cleanup', link);
    const decoy = join(parent, 'decoy');
    for (const dir of ['seed', 'project', 'upstream.git']) mkdirSync(join(decoy, dir), { recursive: true });
    for (const name of ['MANIFEST.json', '.qa-fixture-owner']) copyFileSync(join(root, name), join(decoy, name));
    refused('fixture_marker_mismatch', 'cleanup', decoy);
    assert.ok(existsSync(join(root, 'MANIFEST.json')));

    ok('stage', fixture, 'deny-read');
    const removed = ok('cleanup', fixture);
    assert.deepEqual(removed, { ok: true, removed: root, fixtureId: created.fixtureId });
    assert.equal(existsSync(fixture), false);
    assert.equal(read(bystander, 'keep.txt'), 'not a fixture\n');
    assert.ok(existsSync(join(decoy, 'MANIFEST.json')));
    assert.ok(lstatSync(link).isSymbolicLink());
    assert.deepEqual(readdirSync(home), []);
  });
});

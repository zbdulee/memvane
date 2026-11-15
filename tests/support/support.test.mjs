// The shared fixtures in tests/support (test_support.py), in a fixture HOME.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { leftoverProcesses } from './runner.mjs';
import { fixtureEnv, fixtureHome, removeTemp } from './temp.mjs';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');

test('tests/support fixtures', () => {
  const fixture = fixtureHome('mvs');
  try {
    const r = spawnSync('python3', ['-B', '-m', 'unittest', 'tests.support.test_support', '-v'],
      { cwd: ROOT, encoding: 'utf8', timeout: 600000, env: fixtureEnv(fixture) });
    if (r.stderr) process.stderr.write(r.stderr.slice(-12000));
    assert.ifError(r.error);
    assert.equal(r.signal, null);
    assert.equal(r.status, 0);
    assert.match(r.stderr, /\nOK\b/);
    assert.deepEqual(leftoverProcesses(fixture.home), []);
  } finally {
    removeTemp(fixture.home);
  }
});

test('wiki trust probe reads a card the project builder wrote', async () => {
  const { wikiTrustState } = await import('./wiki_trust.mjs');
  const fixture = fixtureHome('mvw');
  try {
    const env = fixtureEnv(fixture);
    const made = spawnSync('python3', ['-B', '-c', [
      'import sys',
      'from tests.support.project import make_project',
      'p = make_project(sys.argv[1], name="trust")',
      'n = p.write_note("notes/a.md", "The amber lantern glows.\\n")',
      'p.write_card("concepts/a.md", title="Amber", body="The amber lantern glows.\\n", sources=[n])',
      'print(p.root)',
    ].join('\n'), fixture.home], { cwd: ROOT, encoding: 'utf8', env });
    assert.equal(made.status, 0, made.stderr);
    const project = made.stdout.trim();
    const previous = process.env.HOME;
    process.env.HOME = fixture.home;
    try {
      const state = wikiTrustState(project, '.memvane/wiki/concepts/a.md');
      assert.equal(state.status, 'verified');
      assert.equal(state.trusted, true);
      assert.equal(state.revisions.length, 1);
    } finally {
      process.env.HOME = previous;
    }
  } finally {
    removeTemp(fixture.home);
  }
});

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { chmodSync, existsSync, mkdirSync, mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { homedir } from 'node:os';
import { removeTemp } from './helpers/temp.mjs';

function repoTemp(prefix) {
  const base = join(homedir(), '.cache');
  mkdirSync(base, { recursive: true });
  return mkdtempSync(join(base, `qmd-test-${prefix}-`));
}

function writeSettings(work) {
  mkdirSync(join(work, '.auto-context'), { recursive: true });
  writeFileSync(join(work, '.auto-context', 'settings.json'), JSON.stringify({
    indexing: true,
    collections: ['proj-wiki'],
    collectionPaths: { 'proj-wiki': '.auto-context/wiki' },
    collectionRoles: { 'proj-wiki': 'wiki' },
    wikiPath: '.auto-context/wiki',
    compile: { mode: 'auto-wiki' },
  }));
}

function writeWikiPage(work, rel, content) {
  const full = join(work, '.auto-context', 'wiki', rel);
  mkdirSync(join(full, '..'), { recursive: true });
  writeFileSync(full, content);
}

function writeDedupNeeded(work, entries) {
  mkdirSync(join(work, '.auto-context', 'compile'), { recursive: true });
  writeFileSync(
    join(work, '.auto-context', 'compile', 'dedup-needed.jsonl'),
    entries.map((e) => JSON.stringify(e)).join('\n') + '\n',
  );
}

function readDedupNeeded(work) {
  const path = join(work, '.auto-context', 'compile', 'dedup-needed.jsonl');
  if (!existsSync(path)) return [];
  return readFileSync(path, 'utf8').trim().split('\n').filter(Boolean).map((l) => JSON.parse(l));
}

function readDedupDeleted(work) {
  const path = join(work, '.auto-context', 'compile', 'dedup-deleted.jsonl');
  if (!existsSync(path)) return [];
  return readFileSync(path, 'utf8').trim().split('\n').filter(Boolean).map((l) => JSON.parse(l));
}

function readDedupSkipped(work) {
  const path = join(work, '.auto-context', 'compile', 'dedup-skipped.jsonl');
  if (!existsSync(path)) return [];
  return readFileSync(path, 'utf8').trim().split('\n').filter(Boolean).map((l) => JSON.parse(l));
}

// Independent recomputation of the shared hash, through the scanner module --
// asserting the CLI's recorded hash equals this cross-checks that both sides
// of the suppression contract hash identically.
function bodyHashOf(absPath) {
  return execFileSync('python3', ['-c', [
    'import sys',
    "sys.path.insert(0, 'core')",
    'from pathlib import Path',
    'from wiki_dedup_scan import body_hash',
    "print(body_hash(Path(sys.argv[1]).read_text(encoding='utf-8')), end='')",
  ].join('\n'), absPath], { encoding: 'utf8' });
}

function runResolve(work, index, action, extra = [], env = {}) {
  return execFileSync('python3', [
    'core/wiki_dedup_resolve.py', '--cwd', work, '--index', String(index), '--action', action, ...extra,
  ], { encoding: 'utf8', env: { ...process.env, ...env } });
}

// The CLI contract is exit 0 on success, exit 1 on rejection (see interface
// spec). execFileSync throws on a non-zero exit, but the JSON body is still
// on the thrown error's stdout -- use this for rejection-path assertions.
function runResolveAllowReject(work, index, action, extra = [], env = {}) {
  try {
    return runResolve(work, index, action, extra, env);
  } catch (e) {
    return e.stdout;
  }
}

test('wiki_dedup_resolve: merge deletes the named loser, logs full content first', () => {
  const work = repoTemp('dedup-resolve-merge');
  const dirtyQueue = join(work, 'dirty-queue');
  try {
    writeSettings(work);
    writeWikiPage(work, 'entities/loser.md', '---\ntitle: Loser\n---\n\nLoser content.\n');
    writeWikiPage(work, 'entities/winner.md', '---\ntitle: Winner\n---\n\nWinner content.\n');
    writeDedupNeeded(work, [{ pageA: 'entities/loser.md', pageB: 'entities/winner.md', score: 0.95 }]);

    const out = JSON.parse(runResolve(work, 0, 'merge', ['--delete', 'entities/loser.md'], { QMD_DIRTY_QUEUE: dirtyQueue }));
    assert.equal(out.action, 'deleted');
    assert.equal(out.deletedPath, 'entities/loser.md');
    assert.equal(existsSync(join(work, '.auto-context', 'wiki', 'entities', 'loser.md')), false);
    assert.equal(existsSync(join(work, '.auto-context', 'wiki', 'entities', 'winner.md')), true);

    const deleted = readDedupDeleted(work);
    assert.equal(deleted.length, 1);
    assert.equal(deleted[0].deletedPath, 'entities/loser.md');
    assert.match(deleted[0].content, /Loser content\./);
    assert.equal(deleted[0].pairedWith, 'entities/winner.md');
    assert.equal(deleted[0].score, 0.95);
    assert.ok(deleted[0].resolvedAt);

    // A project without a selected Laya route has no index to wake.
    assert.equal(existsSync(dirtyQueue), false);
    assert.deepEqual(readDedupNeeded(work), []);
  } finally {
    removeTemp(work);
  }
});

test('wiki_dedup_resolve: skip removes the entry, no filesystem change, nothing logged as deleted', () => {
  const work = repoTemp('dedup-resolve-skip');
  try {
    writeSettings(work);
    writeWikiPage(work, 'entities/a.md', '---\ntitle: A\n---\n\nA content.\n');
    writeWikiPage(work, 'entities/b.md', '---\ntitle: B\n---\n\nB content.\n');
    writeDedupNeeded(work, [{ pageA: 'entities/a.md', pageB: 'entities/b.md', score: 0.91 }]);

    const out = JSON.parse(runResolve(work, 0, 'skip'));
    assert.equal(out.action, 'skipped');
    assert.equal(existsSync(join(work, '.auto-context', 'wiki', 'entities', 'a.md')), true);
    assert.equal(existsSync(join(work, '.auto-context', 'wiki', 'entities', 'b.md')), true);
    assert.deepEqual(readDedupDeleted(work), []);
    assert.deepEqual(readDedupNeeded(work), []);
  } finally {
    removeTemp(work);
  }
});

test('wiki_dedup_resolve: --delete not matching the entry pageA/pageB is rejected, queue restored', () => {
  const work = repoTemp('dedup-resolve-delete-mismatch');
  try {
    writeSettings(work);
    writeWikiPage(work, 'entities/a.md', '---\ntitle: A\n---\n\nA content.\n');
    writeWikiPage(work, 'entities/b.md', '---\ntitle: B\n---\n\nB content.\n');
    writeDedupNeeded(work, [{ pageA: 'entities/a.md', pageB: 'entities/b.md', score: 0.95 }]);

    const out = JSON.parse(runResolveAllowReject(work, 0, 'merge', ['--delete', 'entities/not-in-entry.md']));
    assert.equal(out.action, 'rejected');
    assert.equal(out.reason, 'delete_not_in_entry');
    assert.equal(existsSync(join(work, '.auto-context', 'wiki', 'entities', 'a.md')), true);
    assert.equal(existsSync(join(work, '.auto-context', 'wiki', 'entities', 'b.md')), true);
    assert.equal(readDedupNeeded(work).length, 1, 'rejected resolution must restore the queue entry');
  } finally {
    removeTemp(work);
  }
});

test('wiki_dedup_resolve: --delete escaping wiki_root is rejected', () => {
  const work = repoTemp('dedup-resolve-escape');
  try {
    writeSettings(work);
    writeWikiPage(work, 'entities/a.md', '---\ntitle: A\n---\n\nA content.\n');
    writeDedupNeeded(work, [{ pageA: 'entities/a.md', pageB: '../../etc/passwd', score: 0.95 }]);

    const out = JSON.parse(runResolveAllowReject(work, 0, 'merge', ['--delete', '../../etc/passwd']));
    assert.equal(out.action, 'rejected');
    assert.equal(out.reason, 'unsafe_delete_path');
  } finally {
    removeTemp(work);
  }
});

test('wiki_dedup_resolve: --delete target already missing degrades to skip, not an error', () => {
  const work = repoTemp('dedup-resolve-stale');
  try {
    writeSettings(work);
    writeWikiPage(work, 'entities/winner.md', '---\ntitle: Winner\n---\n\nWinner content.\n');
    writeDedupNeeded(work, [{ pageA: 'entities/already-gone.md', pageB: 'entities/winner.md', score: 0.95 }]);

    const out = JSON.parse(runResolve(work, 0, 'merge', ['--delete', 'entities/already-gone.md']));
    assert.equal(out.action, 'skipped');
    assert.equal(out.reason, 'stale_target');
    assert.deepEqual(readDedupNeeded(work), []);
    assert.deepEqual(readDedupSkipped(work), [], 'the merge stale_target degrade must record nothing');
  } finally {
    removeTemp(work);
  }
});

test('wiki_dedup_resolve: unresolved entries before/after the resolved index are preserved in order', () => {
  const work = repoTemp('dedup-resolve-order');
  try {
    writeSettings(work);
    writeWikiPage(work, 'entities/p1.md', 'p1');
    writeWikiPage(work, 'entities/p2.md', 'p2');
    writeWikiPage(work, 'entities/p3.md', 'p3');
    writeWikiPage(work, 'entities/p4.md', 'p4');
    writeDedupNeeded(work, [
      { pageA: 'entities/p1.md', pageB: 'entities/p2.md', score: 0.9 },
      { pageA: 'entities/p3.md', pageB: 'entities/p4.md', score: 0.91 },
    ]);

    runResolve(work, 0, 'skip');
    const remaining = readDedupNeeded(work);
    assert.equal(remaining.length, 1);
    assert.equal(remaining[0].pageA, 'entities/p3.md');
  } finally {
    removeTemp(work);
  }
});

test('wiki_dedup_resolve: a crash mid-resolve leaves the queue exactly as it was', () => {
  const work = repoTemp('dedup-resolve-crash');
  try {
    writeSettings(work);
    // No wiki page written for pageA -> unlink() inside resolve_entry raises after
    // is_file() somehow lies (simulate via a directory in place of the file, which
    // is_file() reports False for -> falls to stale_target, not a crash). To force
    // a genuine exception, make the wiki root read-only after queuing so unlink()
    // raises PermissionError while the file itself is present.
    writeWikiPage(work, 'entities/loser.md', 'loser');
    writeWikiPage(work, 'entities/winner.md', 'winner');
    writeDedupNeeded(work, [{ pageA: 'entities/loser.md', pageB: 'entities/winner.md', score: 0.95 }]);
    const entitiesDir = join(work, '.auto-context', 'wiki', 'entities');
    const before = readDedupNeeded(work);

    let threw = false;
    const originalMode = 0o755;
    try {
      chmodSync(entitiesDir, 0o555);
      execFileSync('python3', ['core/wiki_dedup_resolve.py', '--cwd', work, '--index', '0', '--action', 'merge', '--delete', 'entities/loser.md'], { encoding: 'utf8' });
    } catch {
      threw = true;
    } finally {
      chmodSync(entitiesDir, originalMode);
    }
    assert.equal(threw, true, 'unlink on a read-only directory must raise');
    assert.deepEqual(readDedupNeeded(work), before);
  } finally {
    removeTemp(work);
  }
});

test('wiki_dedup_resolve: skip on an intact pair records one sorted, hashed suppression record', () => {
  const work = repoTemp('dedup-resolve-skip-record');
  try {
    writeSettings(work);
    writeWikiPage(work, 'entities/a.md', '---\ntitle: A\n---\n\nA content.\n');
    writeWikiPage(work, 'entities/b.md', '---\ntitle: B\n---\n\nB content.\n');
    // Deliberately reversed order in the queue entry: the record must come out sorted,
    // proving the pair key is order-independent.
    writeDedupNeeded(work, [{ pageA: 'entities/b.md', pageB: 'entities/a.md', score: 0.91 }]);

    const out = JSON.parse(runResolve(work, 0, 'skip'));
    assert.equal(out.action, 'skipped');
    assert.equal(out.recorded, true);

    const skipped = readDedupSkipped(work);
    assert.equal(skipped.length, 1);
    assert.equal(skipped[0].pageA, 'entities/a.md', 'pair key must be sorted');
    assert.equal(skipped[0].pageB, 'entities/b.md', 'pair key must be sorted');
    assert.match(skipped[0].pageAHash, /^[0-9a-f]{64}$/);
    assert.match(skipped[0].pageBHash, /^[0-9a-f]{64}$/);
    assert.equal(skipped[0].pageAHash, bodyHashOf(join(work, '.auto-context', 'wiki', 'entities', 'a.md')));
    assert.equal(skipped[0].pageBHash, bodyHashOf(join(work, '.auto-context', 'wiki', 'entities', 'b.md')));
    assert.ok(skipped[0].skippedAt);
    assert.deepEqual(readDedupNeeded(work), []);
  } finally {
    removeTemp(work);
  }
});

test('wiki_dedup_resolve: stale skip (either page missing) records nothing', () => {
  const work = repoTemp('dedup-resolve-skip-stale');
  try {
    writeSettings(work);
    writeWikiPage(work, 'entities/b.md', '---\ntitle: B\n---\n\nB content.\n');
    writeDedupNeeded(work, [{ pageA: 'entities/gone.md', pageB: 'entities/b.md', score: 0.91 }]);

    const out = JSON.parse(runResolve(work, 0, 'skip'));
    assert.equal(out.action, 'skipped');
    assert.equal(out.recorded, false);
    assert.deepEqual(readDedupSkipped(work), [], 'a stale skip is not a content judgment; never record it');
    assert.deepEqual(readDedupNeeded(work), []);
  } finally {
    removeTemp(work);
  }
});

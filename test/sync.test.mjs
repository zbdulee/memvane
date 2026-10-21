import { test } from "node:test";
import assert from "node:assert/strict";
import { execFileSync, spawnSync } from "node:child_process";
import { removeTemp } from './helpers/temp.mjs';
import {
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  utimesSync,
  writeFileSync,
} from "node:fs";
import { homedir, tmpdir } from "node:os";
import { delimiter, join } from "node:path";

// runtime_work's wake queue follows the OS account home, not HOME.
const WAKE_STUB = join(process.cwd(), "test", "helpers", "runtime-wake-stub");

function makeEnv() {
  const cache = join(homedir(), ".cache");
  mkdirSync(cache, { recursive: true });
  const base = mkdtempSync(join(cache, "qmd-sync-env-"));
  // Exercise the ordinary ready route with an isolated prepared shared store.
  execFileSync("python3", ["test/fixtures/prepare-shared-qmd-store.py", base], {
    env: { ...process.env, HOME: base, PYTHONDONTWRITEBYTECODE: "1" },
  });
  return {
    base,
    state: join(base, "state"),
    queue: join(base, "queue"),
    env: {
      ...process.env,
      HOME: base,
      PYTHONDONTWRITEBYTECODE: "1",
      PYTHONPATH: [WAKE_STUB, join(process.cwd(), "core")].join(delimiter),
      QMD_TEST_RUNTIME_WAKE_LOG: join(base, "wakes.jsonl"),
      QMD_SYNC_STATE_DIR: join(base, "state"),
      QMD_DIRTY_QUEUE: join(base, "queue"),
      QMD_SYNC_LOCKDIR: join(base, "lock.d"),
      QMD_SETUP_GUARD_FIXTURE: "",
    },
  };
}

function makeProject(envInfo) {
  return mkdtempSync(join(envInfo.base, "project-"));
}

function writeConfig(dir, collections, collectionPaths = {}) {
  mkdirSync(join(dir, ".auto-context"), { recursive: true });
  writeFileSync(join(dir, ".auto-context", "settings.json"), JSON.stringify({
    indexing: true,
    collections,
    collectionPaths,
  }));
}

// The prepared store is a legacy (unscoped) selection, so the setup guard admits
// only an owner whose registry rows already equal its current config. Seed those
// rows inside the isolated HOME with the same derivation check_project uses; a
// config the gate would reject (e.g. a missing root) makes this throw.
const SEED_OWNER = `
import sys
sys.path.insert(0, 'core')
import config as qmd_config
import qmd_shared_store as store
home, project = sys.argv[1], sys.argv[2]
owner = qmd_config.find_project_config(project)['projectRoot']
wanted = store._collection_owners(owner, qmd_config.find_project_config(owner)['config'])
if not wanted:
    raise SystemExit('no owner rows to seed')
store._reserve_owners(store._unlinked_root(home), wanted)
`;

function seedOwner(envInfo, dir) {
  execFileSync("python3", ["-c", SEED_OWNER, envInfo.base, dir], {
    encoding: "utf8",
    env: envInfo.env,
  });
}

function runSync(cwd, envInfo, args = []) {
  const out = execFileSync("python3", ["core/sync.py", "--cwd", cwd, "--json", ...args], {
    encoding: "utf8",
    env: envInfo.env,
  });
  return out.trim() ? JSON.parse(out) : null;
}

function queueLines(envInfo) {
  if (!existsSync(envInfo.queue)) return [];
  return readFileSync(envInfo.queue, "utf8").trim().split("\n").filter(Boolean);
}

test("no config exits with no_collections and no queue", () => {
  const envInfo = makeEnv();
  const dir = makeProject(envInfo);
  try {
    const result = runSync(dir, envInfo);
    assert.equal(result.ok, true);
    assert.equal(result.reason, "no_collections");
    assert.deepEqual(result.collectionsQueued, []);
    assert.equal(existsSync(envInfo.queue), false);
  } finally {
    removeTemp(envInfo.base);
    removeTemp(dir);
  }
});

test("first run creates snapshot and queues collection once; second run is unchanged", () => {
  const envInfo = makeEnv();
  const dir = makeProject(envInfo);
  mkdirSync(join(dir, "docs"), { recursive: true });
  writeConfig(dir, ["sync-smoke"], { "sync-smoke": "docs" });
  writeFileSync(join(dir, "docs", "a.md"), "one\n");
  seedOwner(envInfo, dir);
  try {
    const first = runSync(dir, envInfo);
    assert.equal(first.reason, "synced");
    assert.equal(first.created, 1);
    assert.equal(first.updated, 0);
    assert.equal(first.deleted, 0);
    assert.deepEqual(first.collectionsQueued, ["sync-smoke"]);
    assert.deepEqual(queueLines(envInfo), [`sync-smoke\t${join(dir, "docs")}\t${dir}`]);
    // The index wake is requested, but only into the test stub's log.
    assert.deepEqual(readFileSync(join(envInfo.base, "wakes.jsonl"), "utf8").trim().split("\n")
      .map((line) => JSON.parse(line)), [{ kind: "index", target: "", start: false }]);

    const second = runSync(dir, envInfo);
    assert.equal(second.reason, "unchanged");
    assert.equal(second.created, 0);
    assert.deepEqual(second.collectionsQueued, []);
    assert.deepEqual(queueLines(envInfo), [`sync-smoke\t${join(dir, "docs")}\t${dir}`]);
  } finally {
    removeTemp(envInfo.base);
    removeTemp(dir);
  }
});

test("baseline-only records state without queue, then update and delete enqueue collection", () => {
  const envInfo = makeEnv();
  const dir = makeProject(envInfo);
  mkdirSync(join(dir, "docs"), { recursive: true });
  writeConfig(dir, ["story"], { story: "docs" });
  const file = join(dir, "docs", "a.md");
  writeFileSync(file, "one\n");
  seedOwner(envInfo, dir);
  try {
    const baseline = runSync(dir, envInfo, ["--baseline-only"]);
    assert.equal(baseline.reason, "baseline");
    assert.equal(existsSync(envInfo.queue), false);

    writeFileSync(file, "one plus more\n");
    const updated = runSync(dir, envInfo);
    assert.equal(updated.updated, 1);
    assert.deepEqual(updated.collectionsQueued, ["story"]);

    rmSync(file);
    const deleted = runSync(dir, envInfo);
    assert.equal(deleted.deleted, 1);
    assert.deepEqual(deleted.collectionsQueued, ["story"]);
    assert.deepEqual(queueLines(envInfo), [
      `story\t${join(dir, "docs")}\t${dir}`,
      `story\t${join(dir, "docs")}\t${dir}`,
    ]);
  } finally {
    removeTemp(envInfo.base);
    removeTemp(dir);
  }
});

test("multiple changed files in same collection enqueue one line", () => {
  const envInfo = makeEnv();
  const dir = makeProject(envInfo);
  mkdirSync(join(dir, "docs"), { recursive: true });
  writeConfig(dir, ["story"], { story: "docs" });
  seedOwner(envInfo, dir);
  try {
    writeFileSync(join(dir, "docs", "a.md"), "a\n");
    writeFileSync(join(dir, "docs", "b.md"), "b\n");
    const result = runSync(dir, envInfo);
    assert.equal(result.created, 2);
    assert.deepEqual(result.collectionsQueued, ["story"]);
    assert.deepEqual(queueLines(envInfo), [`story\t${join(dir, "docs")}\t${dir}`]);
  } finally {
    removeTemp(envInfo.base);
    removeTemp(dir);
  }
});

test("two changed collections enqueue two sorted lines", () => {
  const envInfo = makeEnv();
  const dir = makeProject(envInfo);
  mkdirSync(join(dir, "a"), { recursive: true });
  mkdirSync(join(dir, "z"), { recursive: true });
  writeConfig(dir, ["zeta", "alpha"], { alpha: "a", zeta: "z" });
  writeFileSync(join(dir, "a", "one.md"), "a\n");
  writeFileSync(join(dir, "z", "one.md"), "z\n");
  seedOwner(envInfo, dir);
  try {
    const result = runSync(dir, envInfo);
    assert.equal(result.created, 2);
    assert.deepEqual(result.collectionsQueued, ["alpha", "zeta"]);
    assert.deepEqual(queueLines(envInfo), [
      `alpha\t${join(dir, "a")}\t${dir}`,
      `zeta\t${join(dir, "z")}\t${dir}`,
    ]);
  } finally {
    removeTemp(envInfo.base);
    removeTemp(dir);
  }
});

test("dry-run reports changes without queue or snapshot", () => {
  const envInfo = makeEnv();
  const dir = makeProject(envInfo);
  mkdirSync(join(dir, "docs"), { recursive: true });
  writeConfig(dir, ["story"], { story: "docs" });
  writeFileSync(join(dir, "docs", "a.md"), "a\n");
  seedOwner(envInfo, dir);
  try {
    const result = runSync(dir, envInfo, ["--dry-run"]);
    assert.equal(result.reason, "dry_run");
    assert.equal(result.created, 1);
    assert.equal(existsSync(envInfo.queue), false);
    assert.equal(existsSync(result.statePath), false);
  } finally {
    removeTemp(envInfo.base);
    removeTemp(dir);
  }
});

test("missing collection root fails closed at the owner gate and is not queued", () => {
  const envInfo = makeEnv();
  const dir = makeProject(envInfo);
  writeConfig(dir, ["missing"], { missing: "docs" });
  try {
    // No owner rows can exist for this config: the gate derives them from the
    // resolved root, so a missing root is never an approved existing owner.
    const res = spawnSync("python3", ["core/sync.py", "--cwd", dir, "--json"], {
      encoding: "utf8",
      env: envInfo.env,
    });
    assert.equal(res.status, 1);
    assert.deepEqual(JSON.parse(res.stdout.trim()), { ok: false, reason: "setup_required" });
    assert.equal(existsSync(envInfo.queue), false);
    assert.equal(existsSync(envInfo.state), false);

    // The sync-unit missing-root report, isolated from the CLI's setup gate.
    const unit = execFileSync("python3", ["-c", `
import json, sys
from pathlib import Path
sys.path.insert(0, 'core')
import sync
root = sys.argv[1]
snapshot, totals, changed, files, warnings = sync.build_snapshot(
    root, None, [('missing', Path(root) / 'docs')], None)
print(json.dumps({'totals': totals, 'changed': changed, 'warnings': warnings}))
`, dir], { encoding: "utf8", env: envInfo.env });
    const parsed = JSON.parse(unit.trim());
    assert.deepEqual(parsed.warnings, [{ collection: "missing", reason: "missing_root" }]);
    assert.deepEqual(parsed.changed, {});
    assert.equal(parsed.totals.created, 0);
    assert.equal(parsed.totals.updated, 0);
    assert.equal(parsed.totals.deleted, 0);
    assert.equal(existsSync(envInfo.queue), false);
  } finally {
    removeTemp(envInfo.base);
    removeTemp(dir);
  }
});

test("QMD_SANDBOX exits with no output and no side effects", () => {
  const envInfo = makeEnv();
  const dir = makeProject(envInfo);
  mkdirSync(join(dir, "docs"), { recursive: true });
  writeConfig(dir, ["story"], { story: "docs" });
  writeFileSync(join(dir, "docs", "a.md"), "a\n");
  try {
    const out = execFileSync("python3", ["core/sync.py", "--cwd", dir, "--json"], {
      encoding: "utf8",
      env: { ...envInfo.env, QMD_SANDBOX: "1" },
    });
    assert.equal(out, "");
    assert.equal(existsSync(envInfo.queue), false);
    assert.equal(existsSync(envInfo.state), false);
  } finally {
    removeTemp(envInfo.base);
    removeTemp(dir);
  }
});

test("active lock reports sync_busy without removing lock", () => {
  const envInfo = makeEnv();
  const dir = makeProject(envInfo);
  mkdirSync(join(dir, "docs"), { recursive: true });
  writeConfig(dir, ["story"], { story: "docs" });
  writeFileSync(join(dir, "docs", "a.md"), "a\n");
  seedOwner(envInfo, dir);
  mkdirSync(join(envInfo.base, "lock.d"));
  writeFileSync(join(envInfo.base, "lock.d", "pid"), String(process.pid));
  try {
    const result = runSync(dir, envInfo);
    assert.equal(result.reason, "sync_busy");
    assert.equal(result.lockPath, join(envInfo.base, "lock.d"));
    assert.equal(existsSync(join(envInfo.base, "lock.d")), true);
    assert.equal(existsSync(envInfo.queue), false);
  } finally {
    removeTemp(envInfo.base);
    removeTemp(dir);
  }
});

test("stale lock from dead pid is removed and sync proceeds", () => {
  const envInfo = makeEnv();
  const dir = makeProject(envInfo);
  mkdirSync(join(dir, "docs"), { recursive: true });
  writeConfig(dir, ["story"], { story: "docs" });
  writeFileSync(join(dir, "docs", "a.md"), "a\n");
  seedOwner(envInfo, dir);
  mkdirSync(join(envInfo.base, "lock.d"));
  writeFileSync(join(envInfo.base, "lock.d", "pid"), "99999999");
  try {
    const result = runSync(dir, envInfo);
    assert.equal(result.reason, "synced");
    assert.deepEqual(result.collectionsQueued, ["story"]);
    assert.deepEqual(queueLines(envInfo), [`story\t${join(dir, "docs")}\t${dir}`]);
    assert.equal(existsSync(join(envInfo.base, "lock.d")), false);
  } finally {
    removeTemp(envInfo.base);
    removeTemp(dir);
  }
});

test("stale lock with unexpected content is not recursively deleted", () => {
  const envInfo = makeEnv();
  const dir = makeProject(envInfo);
  mkdirSync(join(dir, "docs"), { recursive: true });
  writeConfig(dir, ["story"], { story: "docs" });
  writeFileSync(join(dir, "docs", "a.md"), "a\n");
  seedOwner(envInfo, dir);
  const lockDir = join(envInfo.base, "lock.d");
  mkdirSync(lockDir);
  // stale pid (dead) so release path triggers, plus an unexpected precious file.
  writeFileSync(join(lockDir, "pid"), "99999999");
  writeFileSync(join(lockDir, "precious.txt"), "do not delete\n");
  try {
    const result = runSync(dir, envInfo);
    // Lock dir cannot be rmdir'd because it still holds precious.txt → sync stays busy.
    assert.equal(result.reason, "sync_busy");
    // The unexpected content survives — no recursive delete.
    assert.equal(existsSync(join(lockDir, "precious.txt")), true);
    assert.equal(readFileSync(join(lockDir, "precious.txt"), "utf8"), "do not delete\n");
  } finally {
    removeTemp(envInfo.base);
    removeTemp(dir);
  }
});

test("pid-less fresh lock stays busy, but old pid-less lock is recovered", () => {
  const envInfo = makeEnv();
  envInfo.env.QMD_SYNC_LOCK_STALE_SECONDS = "1";
  const dir = makeProject(envInfo);
  mkdirSync(join(dir, "docs"), { recursive: true });
  writeConfig(dir, ["story"], { story: "docs" });
  writeFileSync(join(dir, "docs", "a.md"), "a\n");
  seedOwner(envInfo, dir);
  const lockDir = join(envInfo.base, "lock.d");
  mkdirSync(lockDir);
  try {
    const busy = runSync(dir, envInfo);
    assert.equal(busy.reason, "sync_busy");
    assert.equal(existsSync(lockDir), true);

    const old = new Date(Date.now() - 2000);
    utimesSync(lockDir, old, old);
    const recovered = runSync(dir, envInfo);
    assert.equal(recovered.reason, "synced");
    assert.deepEqual(recovered.collectionsQueued, ["story"]);
    assert.equal(existsSync(lockDir), false);
  } finally {
    removeTemp(envInfo.base);
    removeTemp(dir);
  }
});

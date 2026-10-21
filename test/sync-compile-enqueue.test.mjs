import { test } from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { homedir, tmpdir } from "node:os";
import { delimiter, join } from "node:path";
import { removeTemp } from './helpers/temp.mjs';

const BUILTINS = ["claude", "codex", "hermes"];
// runtime_work's wake queue follows the OS account home, not HOME.
const WAKE_STUB = join(process.cwd(), "test", "helpers", "runtime-wake-stub");

function makeEnv(extra = {}) {
  const cache = join(homedir(), ".cache");
  mkdirSync(cache, { recursive: true });
  const base = mkdtempSync(join(cache, "qmd-sync-compile-env-"));
  // Exercise the ordinary ready route with an isolated prepared shared store.
  execFileSync("python3", ["test/fixtures/prepare-shared-qmd-store.py", base], {
    env: { ...process.env, HOME: base, PYTHONDONTWRITEBYTECODE: "1" },
  });
  return {
    base,
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
      // engine resolution must be deterministic regardless of which host CLI is on PATH.
      QMD_ENGINE: "codex",
      ...extra,
    },
  };
}

function makeProject(envInfo) {
  // config discovery walks up to the HOME boundary, so projects must live under HOME.
  return mkdtempSync(join(envInfo.base, "project-"));
}

function writeSettings(dir, overrides = {}) {
  const settings = {
    indexing: true,
    collections: ["docs-src"],
    collectionPaths: { "docs-src": "docs" },
    collectionRoles: { "docs-src": "raw" },
    ...overrides,
    compile: {
      mode: "auto-wiki",
      triggers: ["post_tool_source", "post_sync_source", "manual"],
      extractor: {
        backends: {},
        builtins: BUILTINS,
        timeout: 120,
      },
      ...(overrides.compile || {}),
    },
  };
  mkdirSync(join(dir, ".auto-context"), { recursive: true });
  writeFileSync(join(dir, ".auto-context", "settings.json"), JSON.stringify(settings));
}

// The prepared store is a legacy (unscoped) selection, so the setup guard admits
// only an owner whose registry rows already equal its current config. Seed those
// rows inside the isolated HOME with the same derivation check_project uses; call
// it after the final writeSettings so the rows match what sync will check.
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

function compileQueuePath(dir) {
  return join(dir, ".auto-context", "compile", "source-queue.jsonl");
}

function compileRecords(dir) {
  const path = compileQueuePath(dir);
  if (!existsSync(path)) return [];
  return readFileSync(path, "utf8").trim().split("\n").filter(Boolean).map((line) => JSON.parse(line));
}

function queuedSourcePaths(dir) {
  return compileRecords(dir).map((record) => record.source.path).sort();
}

function dirtyQueueLines(envInfo) {
  if (!existsSync(envInfo.queue)) return [];
  return readFileSync(envInfo.queue, "utf8").trim().split("\n").filter(Boolean);
}

function cleanup(envInfo, dir) {
  removeTemp(envInfo.base);
  removeTemp(dir);
}

test("changed markdown is enqueued to the compile source queue with the sync trigger", () => {
  const envInfo = makeEnv();
  const dir = makeProject(envInfo);
  mkdirSync(join(dir, "docs", "sub"), { recursive: true });
  writeSettings(dir);
  seedOwner(envInfo, dir);
  writeFileSync(join(dir, "docs", "a.md"), "a\n");
  writeFileSync(join(dir, "docs", "sub", "b.md"), "b\n");
  try {
    const result = runSync(dir, envInfo);
    assert.equal(result.reason, "synced");
    assert.equal(result.compileQueued, 2);
    assert.equal(result.compileDeferred, 0);
    assert.equal(result.compileReason, "queued");

    const records = compileRecords(dir);
    assert.deepEqual(records.map((r) => r.source.path).sort(), ["docs/a.md", "docs/sub/b.md"]);
    for (const record of records) {
      assert.equal(record.trigger, "post_sync_source");
      assert.equal(record.engine, "codex");
      assert.equal(record.source.kind, "file");
      assert.equal(record.source.collection, "docs-src");
      assert.equal(record.cwd, dir);
      assert.match(record.ts, /Z$/);
    }

    // A second sync sees no change and must not re-enqueue.
    const second = runSync(dir, envInfo);
    assert.equal(second.reason, "unchanged");
    assert.equal(second.compileQueued, 0);
    assert.equal(compileRecords(dir).length, 2);
  } finally {
    cleanup(envInfo, dir);
  }
});

test("updates are enqueued but deletions are not", () => {
  const envInfo = makeEnv();
  const dir = makeProject(envInfo);
  mkdirSync(join(dir, "docs"), { recursive: true });
  writeSettings(dir);
  seedOwner(envInfo, dir);
  const keep = join(dir, "docs", "keep.md");
  const gone = join(dir, "docs", "gone.md");
  writeFileSync(keep, "keep\n");
  writeFileSync(gone, "gone\n");
  try {
    runSync(dir, envInfo, ["--baseline-only"]);
    assert.equal(existsSync(compileQueuePath(dir)), false);

    writeFileSync(keep, "keep plus more\n");
    rmSync(gone);
    const result = runSync(dir, envInfo);
    assert.equal(result.updated, 1);
    assert.equal(result.deleted, 1);
    assert.equal(result.compileQueued, 1);
    assert.deepEqual(queuedSourcePaths(dir), ["docs/keep.md"]);
  } finally {
    cleanup(envInfo, dir);
  }
});

test("wiki role collection changes are never enqueued for compile", () => {
  const envInfo = makeEnv();
  const dir = makeProject(envInfo);
  mkdirSync(join(dir, ".auto-context", "wiki"), { recursive: true });
  writeSettings(dir, {
    collections: ["proj-wiki"],
    collectionPaths: { "proj-wiki": ".auto-context/wiki" },
    collectionRoles: { "proj-wiki": "wiki" },
  });
  seedOwner(envInfo, dir);
  writeFileSync(join(dir, ".auto-context", "wiki", "card.md"), "# card\n");
  try {
    const result = runSync(dir, envInfo);
    // dirty-queue (indexing) still gets the collection; compile must not.
    assert.deepEqual(result.collectionsQueued, ["proj-wiki"]);
    assert.equal(result.compileQueued, 0);
    assert.equal(result.compileReason, "no_sources");
    assert.equal(existsSync(compileQueuePath(dir)), false);
  } finally {
    cleanup(envInfo, dir);
  }
});

test("non-markdown files are not enqueued for compile", () => {
  const envInfo = makeEnv();
  const dir = makeProject(envInfo);
  mkdirSync(join(dir, "docs"), { recursive: true });
  writeSettings(dir);
  seedOwner(envInfo, dir);
  writeFileSync(join(dir, "docs", "notes.txt"), "text\n");
  writeFileSync(join(dir, "docs", "data.json"), "{}\n");
  writeFileSync(join(dir, "docs", "README.MD"), "upper\n");
  try {
    const result = runSync(dir, envInfo);
    assert.equal(result.created, 3);
    // Only the (case-insensitive) .md file qualifies.
    assert.equal(result.compileQueued, 1);
    assert.deepEqual(queuedSourcePaths(dir), ["docs/README.MD"]);
  } finally {
    cleanup(envInfo, dir);
  }
});

test("dot-prefix policy: registered dot root is exempt, dot segments below it are not", () => {
  const envInfo = makeEnv();
  const dir = makeProject(envInfo);
  mkdirSync(join(dir, ".nova", "sessions"), { recursive: true });
  mkdirSync(join(dir, ".nova", ".draft"), { recursive: true });
  writeSettings(dir, {
    collections: ["nova"],
    collectionPaths: { nova: ".nova" },
    collectionRoles: { nova: "raw" },
  });
  seedOwner(envInfo, dir);
  writeFileSync(join(dir, ".nova", "sessions", "s1.md"), "s\n");
  writeFileSync(join(dir, ".nova", ".draft", "d.md"), "d\n");
  writeFileSync(join(dir, ".nova", ".hidden.md"), "h\n");
  try {
    const result = runSync(dir, envInfo);
    assert.equal(result.created, 3);
    // Root's own dot segment is exempt; new dot segments below it stay excluded.
    assert.deepEqual(queuedSourcePaths(dir), [".nova/sessions/s1.md"]);
    assert.equal(result.compileQueued, 1);
  } finally {
    cleanup(envInfo, dir);
  }
});

test("DENIED_SOURCE_SEGMENTS are blocked even when registered as a raw collection root", () => {
  const envInfo = makeEnv();
  const dir = makeProject(envInfo);
  mkdirSync(join(dir, ".auto-context", "wiki"), { recursive: true });
  writeSettings(dir, {
    collections: ["meta"],
    collectionPaths: { meta: ".auto-context" },
    collectionRoles: { meta: "raw" },
  });
  seedOwner(envInfo, dir);
  writeFileSync(join(dir, ".auto-context", "wiki", "card.md"), "# card\n");
  try {
    const result = runSync(dir, envInfo);
    assert.ok(result.created >= 1);
    assert.equal(result.compileQueued, 0);
    assert.equal(existsSync(compileQueuePath(dir)), false);
  } finally {
    cleanup(envInfo, dir);
  }
});

test('collectionPaths "." keeps the dot check on the whole relative path', () => {
  const envInfo = makeEnv();
  const dir = makeProject(envInfo);
  mkdirSync(join(dir, "docs"), { recursive: true });
  mkdirSync(join(dir, ".claude"), { recursive: true });
  writeSettings(dir, {
    collections: ["root"],
    collectionPaths: { root: "." },
    collectionRoles: { root: "raw" },
  });
  seedOwner(envInfo, dir);
  writeFileSync(join(dir, "docs", "ok.md"), "ok\n");
  writeFileSync(join(dir, ".claude", "agent.md"), "no\n");
  try {
    const result = runSync(dir, envInfo);
    assert.deepEqual(queuedSourcePaths(dir), ["docs/ok.md"]);
    assert.equal(result.compileQueued, 1);
  } finally {
    cleanup(envInfo, dir);
  }
});

test("compile.enabled false and absent trigger both skip compile enqueue", () => {
  for (const [label, compile] of [
    ["disabled", { enabled: false, mode: "off" }],
    ["mode off", { enabled: true, mode: "off" }],
    ["manual only", { triggers: ["manual"] }],
    ["empty triggers", { triggers: [] }],
  ]) {
    const envInfo = makeEnv();
    const dir = makeProject(envInfo);
    mkdirSync(join(dir, "docs"), { recursive: true });
    writeSettings(dir, { compile });
    seedOwner(envInfo, dir);
    writeFileSync(join(dir, "docs", "a.md"), "a\n");
    try {
      const result = runSync(dir, envInfo);
      assert.equal(result.compileQueued, 0, label);
      assert.equal(result.compileReason, "compile_disabled", label);
      assert.equal(existsSync(compileQueuePath(dir)), false, label);
      // Indexing is unaffected by compile gating.
      assert.deepEqual(result.collectionsQueued, ["docs-src"], label);
    } finally {
      cleanup(envInfo, dir);
    }
  }
});

test("post_tool_source alone still grants sync-path compile enqueue (backward compat)", () => {
  const envInfo = makeEnv();
  const dir = makeProject(envInfo);
  mkdirSync(join(dir, "docs"), { recursive: true });
  writeSettings(dir, { compile: { triggers: ["post_tool_source", "manual"] } });
  seedOwner(envInfo, dir);
  writeFileSync(join(dir, "docs", "a.md"), "a\n");
  try {
    const result = runSync(dir, envInfo);
    assert.equal(result.compileQueued, 1);
    assert.equal(compileRecords(dir)[0].trigger, "post_sync_source");
  } finally {
    cleanup(envInfo, dir);
  }
});

test("dry-run and baseline-only never write the compile queue", () => {
  for (const flag of ["--dry-run", "--baseline-only"]) {
    const envInfo = makeEnv();
    const dir = makeProject(envInfo);
    mkdirSync(join(dir, "docs"), { recursive: true });
    writeSettings(dir);
    seedOwner(envInfo, dir);
    writeFileSync(join(dir, "docs", "a.md"), "a\n");
    try {
      const result = runSync(dir, envInfo, [flag]);
      assert.equal(result.compileQueued, 0, flag);
      assert.equal(result.compileReason, "not_attempted", flag);
      assert.equal(existsSync(compileQueuePath(dir)), false, flag);
    } finally {
      cleanup(envInfo, dir);
    }
  }
});

test("per-sync cap defers overflow without advancing its snapshot entries", () => {
  const envInfo = makeEnv({ QMD_SYNC_COMPILE_MAX: "2" });
  const dir = makeProject(envInfo);
  mkdirSync(join(dir, "docs"), { recursive: true });
  writeSettings(dir);
  seedOwner(envInfo, dir);
  for (const name of ["a.md", "b.md", "c.md", "d.md", "e.md"]) {
    writeFileSync(join(dir, "docs", name), `${name}\n`);
  }
  try {
    const first = runSync(dir, envInfo);
    assert.equal(first.compileQueued, 2);
    assert.equal(first.compileDeferred, 3);
    assert.equal(first.compileReason, "capped");
    assert.deepEqual(queuedSourcePaths(dir), ["docs/a.md", "docs/b.md"]);

    // Deferred files were left out of the snapshot, so the next sync picks them up.
    const second = runSync(dir, envInfo);
    assert.equal(second.compileQueued, 2);
    assert.equal(second.compileDeferred, 1);
    assert.deepEqual(queuedSourcePaths(dir), [
      "docs/a.md", "docs/b.md", "docs/c.md", "docs/d.md",
    ]);

    const third = runSync(dir, envInfo);
    assert.equal(third.compileQueued, 1);
    assert.equal(third.compileDeferred, 0);
    assert.equal(third.compileReason, "queued");

    const fourth = runSync(dir, envInfo);
    assert.equal(fourth.reason, "unchanged");
    assert.equal(fourth.compileQueued, 0);
    assert.equal(compileRecords(dir).length, 5);
  } finally {
    cleanup(envInfo, dir);
  }
});

test("dirty-queue enqueue stays collection-level with compile enabled", () => {
  const envInfo = makeEnv();
  const dir = makeProject(envInfo);
  mkdirSync(join(dir, "docs"), { recursive: true });
  mkdirSync(join(dir, "notes"), { recursive: true });
  writeSettings(dir, {
    collections: ["docs-src", "notes-src"],
    collectionPaths: { "docs-src": "docs", "notes-src": "notes" },
    collectionRoles: { "docs-src": "raw", "notes-src": "session" },
  });
  seedOwner(envInfo, dir);
  writeFileSync(join(dir, "docs", "a.md"), "a\n");
  writeFileSync(join(dir, "docs", "b.md"), "b\n");
  writeFileSync(join(dir, "notes", "c.md"), "c\n");
  try {
    const result = runSync(dir, envInfo);
    assert.equal(result.created, 3);
    assert.deepEqual(result.collectionsQueued, ["docs-src", "notes-src"]);
    // One dirty-queue line per changed collection, not per file.
    assert.deepEqual(dirtyQueueLines(envInfo), [
      `docs-src\t${join(dir, "docs")}\t${dir}`,
      `notes-src\t${join(dir, "notes")}\t${dir}`,
    ]);
    // One index wake per enqueue, recorded only in the test stub's private log.
    assert.deepEqual(readFileSync(join(envInfo.base, "wakes.jsonl"), "utf8").trim().split("\n")
      .map((line) => JSON.parse(line)), [{ kind: "index", target: "", start: false }]);
    // Compile is per file, and the session role qualifies alongside raw.
    assert.equal(result.compileQueued, 3);
    assert.deepEqual(queuedSourcePaths(dir), ["docs/a.md", "docs/b.md", "notes/c.md"]);
  } finally {
    cleanup(envInfo, dir);
  }
});

test("--json keeps every pre-existing field alongside the new compile fields", () => {
  const envInfo = makeEnv();
  const dir = makeProject(envInfo);
  mkdirSync(join(dir, "docs"), { recursive: true });
  writeSettings(dir);
  seedOwner(envInfo, dir);
  writeFileSync(join(dir, "docs", "a.md"), "a\n");
  try {
    const result = runSync(dir, envInfo);
    for (const key of [
      "ok", "reason", "projectRoot", "created", "updated", "deleted",
      "collectionsQueued", "statePath", "warnings",
    ]) {
      assert.ok(key in result, `missing ${key}`);
    }
    assert.equal(result.ok, true);
    assert.deepEqual(result.warnings, []);
    for (const key of ["compileQueued", "compileDeferred", "compileReason"]) {
      assert.ok(key in result, `missing ${key}`);
    }
  } finally {
    cleanup(envInfo, dir);
  }
});

test("engine falls back to a configured builtin when QMD_ENGINE is unset", () => {
  const envInfo = makeEnv();
  delete envInfo.env.QMD_ENGINE;
  const dir = makeProject(envInfo);
  mkdirSync(join(dir, "docs"), { recursive: true });
  writeSettings(dir);
  seedOwner(envInfo, dir);
  writeFileSync(join(dir, "docs", "a.md"), "a\n");
  try {
    const result = runSync(dir, envInfo);
    assert.equal(result.compileQueued, 1);
    // The worker maps engine -> adapter, so the label must be resolvable.
    assert.ok(BUILTINS.includes(compileRecords(dir)[0].engine));
  } finally {
    cleanup(envInfo, dir);
  }
});

test("engine resolves from extractor.backends when builtins is empty", () => {
  // A live project configures explicit backends only (no builtins). That returned
  // "unknown", which the worker cannot map to an adapter -> every manually enqueued
  // job died with missing_extractor. Only the hook path (which sets QMD_ENGINE)
  // happened to work.
  const envInfo = makeEnv();
  delete envInfo.env.QMD_ENGINE;
  const dir = makeProject(envInfo);
  mkdirSync(join(dir, "docs"), { recursive: true });
  writeSettings(dir, {
    compile: {
      extractor: {
        backends: { claude: ["scripts/extract.sh", "claude"], codex: ["scripts/extract.sh", "codex"] },
        builtins: [],
        default: [],
        timeout: 120,
      },
    },
  });
  seedOwner(envInfo, dir);
  writeFileSync(join(dir, "docs", "a.md"), "a\n");
  try {
    const result = runSync(dir, envInfo);
    assert.equal(result.compileQueued, 1);
    assert.ok(["claude", "codex"].includes(compileRecords(dir)[0].engine));
  } finally {
    cleanup(envInfo, dir);
  }
});

test("sync never runs the compile worker or an extractor", () => {
  const envInfo = makeEnv();
  const dir = makeProject(envInfo);
  mkdirSync(join(dir, "docs"), { recursive: true });
  writeSettings(dir);
  seedOwner(envInfo, dir);
  writeFileSync(join(dir, "docs", "a.md"), "a\n");
  try {
    runSync(dir, envInfo);
    // Only the source queue (+ its lock) is written; no candidates/manifest/wiki output.
    const compileDir = join(dir, ".auto-context", "compile");
    assert.equal(existsSync(join(compileDir, "candidates.jsonl")), false);
    assert.equal(existsSync(join(compileDir, "generated-manifest.jsonl")), false);
    assert.equal(existsSync(join(compileDir, "extractor.log")), false);
    assert.equal(existsSync(join(dir, ".auto-context", "wiki")), false);
  } finally {
    cleanup(envInfo, dir);
  }
});

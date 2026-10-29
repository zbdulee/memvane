import { test } from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdirSync, mkdtempSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { removeTemp } from './helpers/temp.mjs';

function frontmatter(text) {
  const match = /^---\n([\s\S]*?)\n---/.exec(text);
  assert.ok(match, "missing YAML frontmatter");
  return Object.fromEntries(match[1].split("\n").map((line) => {
    const idx = line.indexOf(":");
    return [line.slice(0, idx).trim(), line.slice(idx + 1).trim()];
  }));
}

test("update skill metadata and wrapper contract", () => {
  const skill = readFileSync("skills/update/SKILL.md", "utf8");
  const meta = frontmatter(skill);
  assert.equal(meta.name, "update");
  assert.match(meta.description, /sync/i);
  assert.match(meta.description, /memvane-db/);
  assert.match(skill, /setup skill/);

  const wrapper = readFileSync("skills/update/scripts/update.sh", "utf8");
  assert.match(wrapper, /SKILL_DIR=.*dirname/);
  assert.match(wrapper, /laya_index_host_route\.py" classify update/);
  assert.match(wrapper, /run-hook" update "\$HOST_ENGINE"/);
  assert.match(wrapper, /current host is required/);
  assert.doesNotMatch(wrapper, /backend_manager|setup_guard|recall\.py|core\/update\.sh/);
  assert.ok((statSync("skills/update/scripts/update.sh").mode & 0o111) !== 0, "wrapper must be executable");
});

test("update wrapper names onboarding for a project without a selected route", () => {
  const home = mkdtempSync(join(tmpdir(), "qmd-test-update-skill-"));
  const dir = join(home, "project");
  mkdirSync(join(dir, ".auto-context"), { recursive: true });
  writeFileSync(join(dir, ".auto-context", "settings.json"), JSON.stringify({ name: "demo", skipPaths: [] }));
  try {
    const result = spawnSync("bash", ["skills/update/scripts/update.sh", dir], {
      encoding: "utf8", env: { ...process.env, HOME: home, QMD_SANDBOX: "" },
    });
    assert.equal(result.status, 1, result.stdout + result.stderr);
    assert.equal(result.stdout, "");
    assert.match(result.stderr, /setup skill/);
  } finally {
    removeTemp(home);
  }
});

test("update wrapper is a no-op in a sandbox", () => {
  const dir = mkdtempSync(join(tmpdir(), "qmd-test-update-skill-sandbox-"));
  try {
    const result = spawnSync("bash", ["skills/update/scripts/update.sh", dir], {
      encoding: "utf8", env: { ...process.env, QMD_SANDBOX: "1" },
    });
    assert.equal(result.status, 0, result.stdout + result.stderr);
    assert.equal(result.stdout, "");
  } finally {
    removeTemp(dir);
  }
});

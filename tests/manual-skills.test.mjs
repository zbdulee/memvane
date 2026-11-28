import { test } from "node:test";
import assert from "node:assert/strict";
import { existsSync, readdirSync, readFileSync } from "node:fs";

test("manual skills expose setup and existing skills; review and hint are not skills", () => {
  const skillDirs = readdirSync("skills", { withFileTypes: true })
    .filter((entry) => entry.isDirectory())
    .map((entry) => entry.name)
    .filter((name) => name !== "qmd")
    .sort();

  assert.deepEqual(skillDirs, ["logs", "query", "setup", "ui", "update", "wiki-compile", "wiki-dedup"]);
  assert.equal(existsSync("skills/sync"), false);
  assert.equal(existsSync("skills/wiki-source-repair"), false);
  assert.equal(existsSync("skills/hint"), false);
});

test("plugin descriptions list manual skills without hint", () => {
  const files = [
    "plugin.json",
    ".claude-plugin/plugin.json",
    ".codex-plugin/plugin.json",
    ".claude-plugin/marketplace.json",
    ".agents/plugins/marketplace.json",
  ];

  for (const file of files) {
    const text = readFileSync(file, "utf8");
    assert.match(text, /setup\/query\/update\/wiki-compile\/wiki-dedup\/logs\/ui manual skills/);
    assert.doesNotMatch(text, /wiki-source-repair/);
    assert.doesNotMatch(text, /wiki-review/);
  }
});

test("setup skill exposes the Laya onboarding entry", () => {
  const skill = readFileSync("skills/setup/SKILL.md", "utf8");
  assert.match(skill, /^name: setup$/m);
  assert.match(skill, /entry\/setup\.py/);
  assert.match(skill, /plan-fresh-wiki-origin.*review-index-provider.*prepare-fresh-wiki-origin.*finalize-index-origin.*activate-index-origin/s);
  assert.doesNotMatch(skill, /install_update|plan-install|enroll-store/);
});

test("ui skill forwards to entry/run.py ui through a Python wrapper", () => {
  const skill = readFileSync("skills/ui/SKILL.md", "utf8");
  assert.match(skill, /^name: ui$/m);
  assert.match(skill, /skills\/ui\/scripts\/ui\.py" open --cwd "\$PWD"/);
  assert.match(skill, /manualUrl/);
  assert.equal(existsSync("skills/ui/scripts/ui.sh"), false);
  const wrapper = readFileSync("skills/ui/scripts/ui.py", "utf8");
  assert.match(wrapper, /parents\[3\]/);
  assert.match(wrapper, /'entry' \/ 'run\.py'\), 'ui'/);
  assert.match(wrapper, /'-B'/);
  assert.doesNotMatch(wrapper, /shell=True/);
  const entry = readFileSync("entry/run.py", "utf8");
  assert.match(entry, /'ui': \('memvane\.ui\.cli:main', \(\), True\)/);
});

test("wiki-compile skill metadata and wrapper contract", () => {
  assert.equal(existsSync("skills/wiki-compile/SKILL.md"), true);
  assert.equal(existsSync("skills/wiki-compile/scripts/wiki-compile.sh"), true);
  const skill = readFileSync("skills/wiki-compile/SKILL.md", "utf8");
  assert.match(skill, /memvane\/wiki\/compile\/wiki_extract\.py/);
  assert.match(skill, /compact/);
  assert.match(skill, /never paste a raw transcript/i);
  const wrapper = readFileSync("skills/wiki-compile/scripts/wiki-compile.sh", "utf8");
  assert.match(wrapper, /entry\/run\.py" wiki-extract/);
  assert.match(wrapper, /entry\/run\.py" classify compile/);
  assert.doesNotMatch(wrapper, /backend_manager|setup_guard/);
});

// Removed review entry points must not return to the active surface.
const REMOVED_REVIEW_SURFACES = [
  "skills/wiki-review/SKILL.md",
  "skills/wiki-review/scripts/wiki-review.sh",
  "agents/wiki-review-resolver.md",
  "memvane/wiki/review",
];
const REVIEW_SURFACE_POINTER =
  /skills\/wiki-review|wiki-review\.sh|wiki\/review\/|wiki_review\.py|wiki-review-resolver|\/wiki-review\b/i;

test("active operator docs expose automatic trust and no removed review lifecycle", () => {
  for (const surface of REMOVED_REVIEW_SURFACES) {
    assert.equal(existsSync(surface), false, `removed review surface is back: ${surface}`);
  }

  const files = ["CLAUDE.md", "AGENTS.md", "docs/settings.md", "docs/architecture.md",
    ...readdirSync("docs/dev/contexts").filter((name) => name.endsWith(".md"))
      .map((name) => `docs/dev/contexts/${name}`)];
  for (const file of files) {
    assert.doesNotMatch(readFileSync(file, "utf8"), REVIEW_SURFACE_POINTER, file);
  }

  const combined = files.map((file) => readFileSync(file, "utf8")).join("\n");
  assert.match(combined, /createdBy.*memvane/is);
  assert.match(combined, /sourceRevisions/);
  assert.match(combined, /merge-needed.*passive|passive.*merge-needed/is);


});

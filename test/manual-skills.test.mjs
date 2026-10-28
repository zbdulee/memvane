import { test } from "node:test";
import assert from "node:assert/strict";
import { existsSync, readdirSync, readFileSync } from "node:fs";

test("manual skills expose setup and existing skills; review and hint are not skills", () => {
  const skillDirs = readdirSync("skills", { withFileTypes: true })
    .filter((entry) => entry.isDirectory())
    .map((entry) => entry.name)
    .filter((name) => name !== "qmd")
    .sort();

  assert.deepEqual(skillDirs, ["logs", "query", "setup", "update", "wiki-compile", "wiki-dedup"]);
  assert.equal(existsSync("skills/sync"), false);
  assert.equal(existsSync("skills/wiki-source-repair"), false);
  assert.equal(existsSync("skills/hint"), false);
  assert.equal(existsSync("test/hint-skill.test.mjs"), false);
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
    assert.match(text, /setup\/query\/update\/wiki-compile\/wiki-dedup\/logs manual skills/);
    assert.doesNotMatch(text, /wiki-source-repair/);
    assert.doesNotMatch(text, /wiki-review/);
  }
});

test("setup skill exposes the Laya onboarding entry", () => {
  const skill = readFileSync("skills/setup/SKILL.md", "utf8");
  assert.match(skill, /^name: setup$/m);
  assert.match(skill, /core\/setup_entry\.py/);
  assert.match(skill, /plan-fresh-wiki-origin.*review-index-provider.*prepare-fresh-wiki-origin.*finalize-index-origin.*activate-index-origin/s);
  assert.doesNotMatch(skill, /install_update|plan-install|enroll-store/);
});

test("wiki-compile skill metadata and wrapper contract", () => {
  assert.equal(existsSync("skills/wiki-compile/SKILL.md"), true);
  assert.equal(existsSync("skills/wiki-compile/scripts/wiki-compile.sh"), true);
  const skill = readFileSync("skills/wiki-compile/SKILL.md", "utf8");
  assert.match(skill, /core\/wiki_extract\.py/);
  assert.match(skill, /compact/);
  assert.match(skill, /never paste a raw transcript/i);
  const wrapper = readFileSync("skills/wiki-compile/scripts/wiki-compile.sh", "utf8");
  assert.match(wrapper, /core\/wiki_extract\.py/);
  assert.match(wrapper, /laya_index_host_route\.py" classify compile/);
  assert.doesNotMatch(wrapper, /backend_manager|setup_guard/);
});

// 사람 검수 lifecycle 제거의 회귀 가드.
//
// **단어를 금지하지 않는다.** 처음엔 활성 문서에서 `/wiki-review|wiki_review|\breviewed\b/i`의
// 부재를 단정했는데, 그 규칙은 만족 불가능이다: `docs/settings.md`는 마이그레이션이 무엇을
// 하는지("`reviewed`·`canon`·`manual` 상태를 `generated`으로 정규화하고 `reviewed:`를 제거")를
// 운영자에게 설명해야 하고, **제거된 것의 이름을 부르지 않고 제거를 설명할 방법이 없다**.
// 금지어 가드는 그래서 문서가 옳을수록 빨개졌고, 통과시키려면 사용자가 알아야 할 사실을
// 지우는 수밖에 없다. 가드가 문서를 이기면 안 된다.
//
// 실제로 막아야 하는 회귀는 세 가지이고 전부 단어가 아니라 **표면**이다:
//   (1) 제거된 파일이 되살아나는 것        → existsSync (동작 단정)
//   (2) 문서가 없는 skill/script로 안내하는 것 → 경로·스크립트·모듈 이름만 금지
//   (3) 새 카드에 `reviewed:`가 다시 나가는 것 → 여기가 아니라
//       test/wiki-compile.test.mjs가 생성된 frontmatter에 `^reviewed:` 부재를 단정한다
//       (문서 grep보다 강하다 — 산출물 자체를 본다).
// 산문에서 `wiki-review`를 백틱으로 인용하는 것은 (2)가 아니다. 사용자가 실행할 수 있는
// 형태(`skills/wiki-review/...`, `wiki-review.sh`, `/wiki-review`)만 포인터로 본다.
const REMOVED_REVIEW_SURFACES = [
  "skills/wiki-review/SKILL.md",
  "skills/wiki-review/scripts/wiki-review.sh",
  "agents/wiki-review-resolver.md",
  "core/wiki_review.py",
];
const REVIEW_SURFACE_POINTER =
  /skills\/wiki-review|wiki-review\.sh|core\/wiki_review|wiki_review\.py|wiki-review-resolver|\/wiki-review\b/i;

test("active operator docs expose automatic trust and no removed review lifecycle", () => {
  for (const surface of REMOVED_REVIEW_SURFACES) {
    assert.equal(existsSync(surface), false, `removed review surface is back: ${surface}`);
  }

  const files = ["CLAUDE.md", "docs/settings.md"];
  for (const file of files) {
    assert.doesNotMatch(readFileSync(file, "utf8"), REVIEW_SURFACE_POINTER, file);
  }

  const combined = files.map((file) => readFileSync(file, "utf8")).join("\n");
  assert.match(combined, /createdBy.*memvane/is);
  assert.match(combined, /sourceRevisions/);
  assert.match(combined, /merge-needed.*passive|passive.*merge-needed/is);

  // 금지어 가드를 뒤집는다: 운영자 문서는 legacy `verified`가 왜 사라지는지를 **설명해야**
  // 한다. 이 마이그레이션은 라이브 프로젝트에서 수백 장을 한 번에 recall에서 빼고
  // (실측 service-engineering 744장 / ai-proxy 26장이 `sourceRevisions` 0), 자동 재검증이
  // 없어 각 원문이 편집될 때까지 돌아오지 않는다. 설명 없는 그 절벽은 버그로 신고된다.
  //
  // 두 단정은 **AND이고 각각 문구 하나만** 받는다. 처음엔 소비자 친화적으로 보이는
  // `/A|B|C/` 나열을 썼는데, 그러면 A를 지워도 문서 다른 곳의 B가 통과시켜 teeth 검증에서
  // 실제로 안 물었다(설명을 지웠는데 초록). 여기서 문구가 바뀌어 빨개지는 것은 옳은 방향의
  // 실패다 — "설명을 지웠나?"를 묻게 하지, 문서를 지우게 만들지 않는다.
  const settings = readFileSync("docs/settings.md", "utf8");
  assert.match(
    settings, /provenance 없는 legacy/,
    "docs/settings.md must name what the legacy migration acts on",
  );
  assert.match(
    settings, /이 카드는 recall에 나오지 않습니다/,
    "docs/settings.md must state that migrated legacy cards stop appearing in recall",
  );
});

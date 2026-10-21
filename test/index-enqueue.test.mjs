import { test, after } from "node:test";
import assert from "node:assert";
import { execFileSync } from "node:child_process";
import { mkdtempSync, writeFileSync, mkdirSync, readFileSync, existsSync, realpathSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { removeTemp } from './helpers/temp.mjs';

const isolatedHome = mkdtempSync(join(process.cwd(), 'qmd-index-enqueue-home-'));
const disposable = [isolatedHome];
function testTemp(prefix) {
  const path = mkdtempSync(join(tmpdir(), prefix));
  disposable.push(path);
  return path;
}
execFileSync('python3', ['test/fixtures/prepare-shared-qmd-store.py', isolatedHome],
  {encoding: 'utf8', env: {...process.env, PYTHONDONTWRITEBYTECODE: '1'}});
after(() => { for (const path of disposable.reverse()) removeTemp(path); });

function enqueue(cwd, payload, queuePath) {
  execFileSync("python3", ["core/index_enqueue.py"], {
    input: JSON.stringify(payload), encoding: "utf8",
    // These cases exercise hook path parsing. The synthetic selected store
    // has no approved owner registry, so production routing must reject it.
    env: { ...process.env, HOME: isolatedHome, QMD_SETUP_GUARD_FIXTURE: '1',
      QMD_DIRTY_QUEUE: queuePath, PYTHONDONTWRITEBYTECODE: '1' },
  });
}

function setupProj(collections, indexing = true) {
  const dir = testTemp("qproj-");
  mkdirSync(join(dir, "04_Manuscript"), { recursive: true });
  writeFileSync(join(dir, ".auto-context.json"), JSON.stringify({
    collections, indexing, collectionPaths: { [collections[0]]: "04_Manuscript" },
  }));
  return dir;
}

test("연결된 폴더 story-path 편집 → 큐에 적재", () => {
  const proj = setupProj(["x-manuscript"]);
  const q = join(testTemp("q-"), "queue");
  enqueue(proj, { hook_event_name: "PostToolUse", cwd: proj,
    tool_input: { file_path: join(proj, "04_Manuscript", "ep1.md") } }, q);
  const lines = readFileSync(q, "utf8").trim().split("\n");
  assert.equal(lines.length, 1);
  assert.deepEqual(lines[0].split('\t'), ['x-manuscript',
    realpathSync(join(proj, '04_Manuscript'))]);
});

test("agy TargetFile payload → 큐에 적재", () => {
  const proj = setupProj(["x-manuscript"]);
  const q = join(testTemp("q-"), "queue");
  enqueue(proj, {
    hook_event_name: "PostToolUse",
    tool_name: "write_to_file",
    cwd: proj,
    tool_input: { TargetFile: join(proj, "04_Manuscript", "ep1.md") },
  }, q);
  const lines = readFileSync(q, "utf8").trim().split("\n");
  assert.equal(lines.length, 1);
  assert.deepEqual(lines[0].split('\t'), ['x-manuscript',
    realpathSync(join(proj, '04_Manuscript'))]);
});

test("agy ReplacementChunks payload → 큐에 적재", () => {
  const proj = setupProj(["x-manuscript"]);
  const q = join(testTemp("q-"), "queue");
  enqueue(proj, {
    hook_event_name: "PostToolUse",
    tool_name: "multi_replace_file_content",
    cwd: proj,
    tool_input: {
      ReplacementChunks: [
        { TargetFile: join(proj, "04_Manuscript", "ep1.md"), ReplacementContent: "충분히 긴 수정 텍스트" },
      ],
    },
  }, q);
  const lines = readFileSync(q, "utf8").trim().split("\n");
  assert.equal(lines.length, 1);
  assert.deepEqual(lines[0].split('\t'), ['x-manuscript',
    realpathSync(join(proj, '04_Manuscript'))]);
});

test("collections 빈(pending) → 큐 미생성", () => {
  const proj = setupProj([], false); // indexing:false → collections=[]
  const q = join(testTemp("q-"), "queue");
  enqueue(proj, { hook_event_name: "PostToolUse", cwd: proj,
    tool_input: { file_path: join(proj, "04_Manuscript", "ep1.md") } }, q);
  assert.equal(existsSync(q), false);
});

test("컬렉션 밖 편집 → 큐 미생성", () => {
  const proj = setupProj(["x-manuscript"]);
  const q = join(testTemp("q-"), "queue");
  enqueue(proj, { hook_event_name: "PostToolUse", cwd: proj,
    tool_input: { file_path: join(proj, "README.md") } }, q);
  assert.equal(existsSync(q), false);
});

test("cwd 밖 절대경로 collectionPath 편집 → 큐 미생성 (boundary)", () => {
  const dir = testTemp("qproj-");
  const outside = testTemp("qoutside-");
  writeFileSync(join(dir, ".auto-context.json"), JSON.stringify({
    collections: ["leak"], indexing: true,
    collectionPaths: { "leak": outside },
  }));
  writeFileSync(join(outside, "secret.md"), "x\n");
  const q = join(testTemp("q-"), "queue");
  enqueue(dir, { hook_event_name: "PostToolUse", cwd: dir,
    tool_input: { file_path: join(outside, "secret.md") } }, q);
  assert.equal(existsSync(q), false);
});

test("allowRoots 안의 절대경로 collectionPath 편집 → 큐에 적재", () => {
  const dir = testTemp("qproj-");
  const outside = testTemp("qoutside-");
  writeFileSync(join(dir, ".auto-context.json"), JSON.stringify({
    collections: ["shared"], indexing: true,
    collectionPaths: { "shared": outside },
    allowRoots: [outside],
  }));
  writeFileSync(join(outside, "doc.md"), "x\n");
  const q = join(testTemp("q-"), "queue");
  enqueue(dir, { hook_event_name: "PostToolUse", cwd: dir,
    tool_input: { file_path: join(outside, "doc.md") } }, q);
  const lines = readFileSync(q, "utf8").trim().split("\n");
  assert.equal(lines.length, 1);
  assert.match(lines[0], /^shared\t/);
});

test("sandbox → 무동작", () => {
  const proj = setupProj(["x-manuscript"]);
  const q = join(testTemp("q-"), "queue");
  execFileSync("python3", ["core/index_enqueue.py"], {
    input: JSON.stringify({ hook_event_name: "PostToolUse", cwd: proj,
      tool_input: { file_path: join(proj, "04_Manuscript", "ep1.md") } }),
    encoding: "utf8", env: { ...process.env, QMD_DIRTY_QUEUE: q, QMD_SANDBOX: "1" },
  });
  assert.equal(existsSync(q), false);
});

test("postToolUse 이벤트 비활성화 → 큐 미생성", () => {
  const dir = testTemp("qproj-");
  mkdirSync(join(dir, "04_Manuscript"), { recursive: true });
  writeFileSync(join(dir, ".auto-context.json"), JSON.stringify({
    collections: ["x-manuscript"], indexing: true,
    collectionPaths: { "x-manuscript": "04_Manuscript" },
    events: ["sessionStart", "userPromptSubmit"],
  }));
  const q = join(testTemp("q-"), "queue");
  enqueue(dir, { hook_event_name: "PostToolUse", cwd: dir,
    tool_input: { file_path: join(dir, "04_Manuscript", "ep1.md") } }, q);
  assert.equal(existsSync(q), false);
});

test("shared dirty_queue writer uses sorted two-column lines", () => {
  const q = join(testTemp("q-"), "queue");
  execFileSync("python3", ["-c", `
import sys
sys.path.insert(0, "core")
import dirty_queue
dirty_queue.enqueue_collections({"z": "/tmp/z", "a": "/tmp/a"})
`], {
    encoding: "utf8",
    env: { ...process.env, QMD_DIRTY_QUEUE: q },
  });
  assert.equal(readFileSync(q, "utf8"), "a\t/tmp/a\nz\t/tmp/z\n");
});

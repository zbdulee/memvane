// **T5 회귀 가드** (계획 §6.2 #5 · §6 "원자 컷오버").
//
// `compile.extractor.dispatch: "by-engine"`은 제거됐다. 그 키가 있던 동안
// `resolve_extractor_argv`는 **키가 없으면 backends/builtins를 통째로 무시하고 None**을
// 돌려줬고, `process_job`은 그 None을
//
//     append_jsonl(cpath, bounded_failure("needs_extractor", job, "missing_extractor"))
//     return True, False, []
//                ^^^^ "잡 소비됨"
//
// 으로 처리한다 — **requeue가 아니라 폐기**다. 즉 리졸버가 이해하지 못하는 config는
// 큐에 든 잡을 조용히 파괴하고, 사용자는 카드가 안 생기는 것 말고는 아무 신호도 못 받는다.
// A(스키마 축소)와 D(extractor 일원화)를 한 커밋으로 묶어야 했던 이유가 이것이다.
//
// 그래서 이 파일은 세 가지를 함께 못박는다:
//   1. `dispatch` 키가 **없는** config에서 builtins가 번들 adapter로 해석된다.
//   2. `dispatch` 키가 **적혀 있어도**(마이그레이션 안 된 파일) 결과가 같다 — 죽은 키다.
//   3. 잡 폐기(`missing_extractor`)는 여전히 **정말로 설정이 없을 때만** 일어난다.
//      3을 빼면 1·2가 "그냥 항상 통과"로 약해져 가드가 되지 않는다.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { existsSync, mkdtempSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { removeTemp } from './helpers/temp.mjs';

const REPO_ROOT = resolve(fileURLToPath(import.meta.url), '..', '..');
const CORE = REPO_ROOT;

process.env.QMD_SKIP_BACKGROUND_EMBED = '1';

/** `wiki_compile_worker.resolve_extractor_argv`를 그대로 호출한다(JS 재구현 금지). */
function resolveArgv(extractor, engine) {
  const py = `import json, sys
sys.path.insert(0, sys.argv[1])
import memvane.wiki.compile.wiki_compile_worker as wcw
print(json.dumps(wcw.resolve_extractor_argv({"extractor": json.loads(sys.argv[2])}, sys.argv[3])))`;
  return JSON.parse(execFileSync('python3', ['-c', py, CORE, JSON.stringify(extractor), engine], {
    cwd: REPO_ROOT, encoding: 'utf8',
  }));
}

test('dispatch 키 없이 builtins가 번들 adapter로 해석된다 (missing_extractor 아님)', () => {
  const argv = resolveArgv({ builtins: ['claude'], backends: {}, timeout: 30 }, 'claude');
  assert.notEqual(argv, null, 'builtins-only config에서 argv가 해석되지 않으면 잡이 폐기된다');
  assert.equal(argv.length, 2);
  assert.equal(argv[1], join(CORE, 'extractors', 'claude_adapter.py'));
  // backends의 명시 argv는 builtin adapter보다 우선한다(escape hatch가 살아 있어야 한다).
  assert.deepEqual(
    resolveArgv({ builtins: ['claude'], backends: { claude: ['python3', 'custom.py'] } }, 'claude'),
    ['python3', 'custom.py'],
  );
});

test('마이그레이션 안 된 파일의 dispatch/default/argv는 해석 결과를 바꾸지 않는다', () => {
  const withDeadKeys = resolveArgv({
    dispatch: 'by-engine', default: ['python3', 'fallback.py'], argv: ['python3', 'legacy.py'],
    builtins: ['codex'], backends: {},
  }, 'codex');
  const withoutDeadKeys = resolveArgv({ builtins: ['codex'], backends: {} }, 'codex');
  assert.deepEqual(withDeadKeys, withoutDeadKeys);
  assert.equal(withDeadKeys[1], join(CORE, 'extractors', 'codex_adapter.py'));
  // 특히 `argv`(0.x 단일 argv)는 더 이상 backends/builtins를 가로채지 않는다.
  assert.notEqual(withDeadKeys[1], 'legacy.py');
});

test('정말로 설정이 없을 때만 None — 가드가 항상-통과로 약해지지 않는다', () => {
  assert.equal(resolveArgv({ builtins: [], backends: {} }, 'claude'), null);
  assert.equal(resolveArgv({ builtins: ['codex'], backends: {} }, 'claude'), null,
    '풀에 없는 엔진은 해석되지 않는다');
  assert.equal(resolveArgv({ argv: ['python3', 'legacy.py'] }, 'claude'), null,
    '제거된 argv 하나만 적힌 config는 해석되지 않는다(죽은 키다)');
});

// --- 잡 파괴의 실물 확인 -----------------------------------------------------
//
// 위 셋은 리졸버 단위다. 이 아래는 "해석 실패 = 잡 폐기"라는 대가가 실제로 존재함을
// 확인한다 — 그 대가가 없으면 위 가드가 지키는 것도 없다.

function setupProject(extractor) {
  const dir = mkdtempSync(join(tmpdir(), 'qmd-dispatch-guard-'));
  mkdirSync(join(dir, '.auto-context', 'compile'), { recursive: true });
  mkdirSync(join(dir, '.auto-context', 'wiki'), { recursive: true });
  mkdirSync(join(dir, 'docs'), { recursive: true });
  writeFileSync(join(dir, 'docs', 'source.md'),
    '# Source\n\nDurable decision: generated wiki pages cite source markdown.\n');
  writeFileSync(join(dir, '.auto-context', 'settings.json'), JSON.stringify({
    indexing: true,
    collections: ['proj-docs', 'proj-wiki'],
    collectionPaths: { 'proj-docs': 'docs', 'proj-wiki': '.auto-context/wiki' },
    collectionRoles: { 'proj-docs': 'raw', 'proj-wiki': 'wiki' },
    wikiPath: '.auto-context/wiki',
    compile: {
      mode: 'auto-wiki',
      triggers: ['post_tool_source', 'manual'],
      // verify는 유료 호출이라 끈다 — 이 파일이 재는 것은 extractor 해석뿐이다.
      verify: { enabled: false },
      semanticDedup: { enabled: false },
      extractor,
    },
  }));
  writeFileSync(join(dir, '.auto-context', 'compile', 'source-queue.jsonl'), `${JSON.stringify({
    ts: '2026-08-03T00:00:00Z',
    trigger: 'post_tool_source',
    engine: 'claude',
    cwd: dir,
    source: { kind: 'file', path: 'docs/source.md', collection: 'proj-docs' },
  })}\n`);
  return dir;
}

function runWorker(project, env = {}) {
  return execFileSync('python3', [join(CORE, 'entry/run.py'), 'wiki-compile-worker', '--cwd', project], {
    cwd: REPO_ROOT,
    encoding: 'utf8',
    env: {
      ...process.env,
      QMD_SKIP_BACKGROUND_EMBED: '1',
      QMD_DIRTY_QUEUE: join(project, '.auto-context', 'compile', 'dirty-queue'),
      QMD_DAEMON_URL: 'http://127.0.0.1:1',
      ...env,
    },
  });
}

function jsonl(path) {
  if (!existsSync(path)) return [];
  return readFileSync(path, 'utf8').trim().split('\n').filter(Boolean).map((l) => JSON.parse(l));
}

/** 번들 claude adapter가 부를 CLI를 stub으로 바꾼다 — 실제 유료 호출을 절대 하지 않는다. */
function stubClaudeCli() {
  const bin = join(mkdtempSync(join(tmpdir(), 'qmd-stub-cli-')), 'claude');
  writeFileSync(bin, `#!/usr/bin/env python3
import json
print(json.dumps({'candidates': [{
  'title': 'Dispatch Guard Decision',
  'summary': 'The builtins-only config resolved to the bundled adapter and produced a card.',
  'suggestedType': 'decision',
  'confidence': 'high',
  'targetPath': '.auto-context/wiki/decisions/dispatch-guard-decision.md'
}]}))
`, { mode: 0o755 });
  return bin;
}

test('builtins-only config는 잡을 실제로 처리한다 (dispatch 게이트가 없다)', () => {
  const project = setupProject({ builtins: ['claude'], backends: {}, timeout: 60 });
  try {
    runWorker(project, { QMD_EXTRACTOR_CLAUDE_BIN: stubClaudeCli() });
    const cpath = join(project, '.auto-context', 'compile', 'candidates.jsonl');
    const reasons = jsonl(cpath).map((r) => r.reason).filter(Boolean);
    assert.equal(reasons.includes('missing_extractor'), false,
      `builtins-only가 missing_extractor로 폐기됐다: ${JSON.stringify(reasons)}`);
    assert.equal(
      existsSync(join(project, '.auto-context', 'wiki', 'decisions', 'dispatch-guard-decision.md')),
      true,
      '카드가 쓰이지 않았다 — 해석은 됐어도 파이프라인이 끝까지 가지 않았다',
    );
  } finally {
    removeTemp(project);
  }
});

test('extractor 미설정은 missing_extractor로 잡을 폐기한다 (requeue 아님 — 가드의 대가)', () => {
  const project = setupProject({ builtins: [], backends: {}, timeout: 60 });
  try {
    runWorker(project);
    const reasons = jsonl(join(project, '.auto-context', 'compile', 'candidates.jsonl'))
      .map((r) => r.reason);
    assert.equal(reasons.includes('missing_extractor'), true);
    // 이것이 "조용한 파괴"의 실체다: 큐가 비었으므로 다음 kick이 다시 집지 않는다.
    assert.equal(
      readFileSync(join(project, '.auto-context', 'compile', 'source-queue.jsonl'), 'utf8').trim(),
      '',
      'missing_extractor는 잡을 requeue하지 않는다 — 그래서 리졸버가 이해 못 하는 config가 위험하다',
    );
  } finally {
    removeTemp(project);
  }
});

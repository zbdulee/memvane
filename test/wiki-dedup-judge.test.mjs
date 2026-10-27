import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { homedir, tmpdir } from 'node:os';
import { removeTemp } from './helpers/temp.mjs';

// LLM dedup judge (core/wiki_dedup_judge.py) wired into both dedup paths.
//
// No test here may invoke a real host CLI: every judge is a bash/python stub
// handed to compile.extractor.backends, exactly like test/wiki-verify-worker.test.mjs
// stubs the verifier.

function repoTemp(prefix) {
  const base = join(homedir(), '.cache');
  mkdirSync(base, { recursive: true });
  return mkdtempSync(join(base, `qmd-test-${prefix}-`));
}

// 임시 디렉터리 정리 재시도(update.test.mjs의 removeTemp와 같은 이유·같은 형태):
// 병렬 실행에서 ENOTEMPTY/EBUSY가 flaky 실패를 만든다.

/** Per-engine judge stub. `label` lands in the shared call log so a test can prove
 *  WHICH adapter argv ran, not merely which engine label the plan passed down. */
function engineJudgeStub(label, verdict, tracker, { exitCode = 0, sleep = 0 } = {}) {
  const script = join(mkdtempSync(join(tmpdir(), `dedup-${label}-`)), `${label}.py`);
  writeFileSync(script, `#!/usr/bin/env python3
import json, sys, time
payload = json.loads(sys.stdin.read())
assert payload.get('task') == 'dedup', 'dedup payload expected, got %r' % payload.get('task')
with open(${JSON.stringify(tracker)}, 'a', encoding='utf-8') as fh:
    fh.write(json.dumps({'who': ${JSON.stringify(label)}, 'engine': payload.get('engine', '')}) + '\\n')
time.sleep(${sleep})
if ${exitCode} != 0:
    sys.exit(${exitCode})
print(json.dumps({'verdict': ${JSON.stringify(verdict)}, 'reason': 'judged by ${label}'}))
`);
  return ['python3', script];
}

function trackerFile(label) {
  return join(mkdtempSync(join(tmpdir(), `dedup-tracker-${label}-`)), 'calls');
}

/** Engine labels of every adapter that actually executed, in order. */
function judgedByEngines(tracker) {
  return jsonl(tracker).map((row) => row.who);
}

/** A fake host CLI adapter: reads the dedup payload, asserts its shape, emits a verdict. */
function judgeStub(verdict, { reason = 'stub reason', callLog = null, exitCode = 0, sleep = 0 } = {}) {
  const dir = mkdtempSync(join(tmpdir(), 'dedup-judge-'));
  const script = join(dir, 'judge.py');
  writeFileSync(script, `#!/usr/bin/env python3
import json, sys, time
payload = json.loads(sys.stdin.read())
assert payload.get('task') == 'dedup', 'dedup payload expected, got %r' % payload.get('task')
assert payload['pageA']['content'], 'pageA content required'
assert payload['pageB']['content'], 'pageB content required'
${callLog ? `open(${JSON.stringify(callLog)}, 'a', encoding='utf-8').write(json.dumps({'a': payload['pageA']['path'], 'b': payload['pageB']['path']}) + '\\n')` : 'pass'}
time.sleep(${sleep})
if ${exitCode} != 0:
    sys.exit(${exitCode})
print(json.dumps({
    'verdict': ${JSON.stringify(verdict)},
    'reason': ${JSON.stringify(reason)},
    'sharedFacts': ['shared'],
    'uniqueToA': ['only in A'],
    'uniqueToB': ['only in B'],
}))
`);
  return ['python3', script];
}

// 레거시(무료) score 게이트의 임계는 더 이상 설정이 아니라 core/config.py의 상수다.
// 리터럴을 테스트에 복제하면 상수를 옮기는 순간 이 파일이 조용히 다른 세계를 검증하므로
// 실제 값을 읽어 픽스처 score가 그 위/아래임을 명시적으로 단정한다.
const [DEDUP_SCORE_THRESHOLD, DEDUP_AUTO_MERGE_THRESHOLD] = execFileSync('python3', ['-c',
  "import sys; sys.path.insert(0, 'core'); import config;"
  + " print(config.DEDUP_SCORE_THRESHOLD); print(config.DEDUP_AUTO_MERGE_THRESHOLD)",
], { cwd: process.cwd(), encoding: 'utf8' }).trim().split('\n').map(Number);

function callCount(logPath) {
  if (!existsSync(logPath)) return 0;
  return readFileSync(logPath, 'utf8').trim().split('\n').filter(Boolean).length;
}

function jsonl(path) {
  if (!existsSync(path)) return [];
  return readFileSync(path, 'utf8').trim().split('\n').filter(Boolean).map((l) => JSON.parse(l));
}

// ---------------------------------------------------------------- adapter layer

function runLib(py) {
  return execFileSync('python3', ['-c', py], { cwd: process.cwd(), encoding: 'utf8' });
}

test('build_dedup_prompt embeds both card bodies and demands a reason, not a bare yes/no', () => {
  const py = `import sys,json; sys.path.insert(0,'core/extractors'); import lib
p=lib.build_dedup_prompt({'task':'dedup','pageA':{'path':'entities/a.md','content':'BODY_A'},'pageB':{'path':'entities/b.md','content':'BODY_B'}})
print(json.dumps({
 'has_a':'BODY_A' in p,'has_b':'BODY_B' in p,
 'has_verdict':'"duplicate"|"distinct"|"unclear"' in p,
 'has_reason':'"reason"' in p,
 'has_unique':'uniqueToA' in p,
 'topical_guard':'Being topically related is NOT being duplicate' in p,
 'no_tools':'Do NOT use any tools' in p}))`;
  const out = JSON.parse(runLib(py));
  for (const [k, v] of Object.entries(out)) assert.equal(v, true, `${k} missing from dedup prompt`);
});

test('extract_dedup_verdict takes the last valid verdict and rejects verify-only values', () => {
  const py = `import sys,json; sys.path.insert(0,'core/extractors'); import lib
text = 'noise {"verdict":"pass"} more ' + json.dumps({'verdict':'distinct','reason':'different events'})
print(json.dumps({'picked': lib.extract_dedup_verdict(text), 'verify_scanner': lib.extract_verdict('{"verdict":"duplicate"}')}))`;
  const out = JSON.parse(runLib(py));
  assert.equal(out.picked.verdict, 'distinct');
  assert.equal(out.picked.reason, 'different events');
  // The two verdict vocabularies must stay disjoint: a dedup verdict is not a verify verdict.
  assert.deepEqual(out.verify_scanner, {});
});

test('claude adapter: task=dedup uses the dedup prompt and emits a dedup verdict', () => {
  const d = mkdtempSync(join(tmpdir(), 'claude-dedup-'));
  const promptLog = join(d, 'prompt.txt');
  const fakeCli = join(d, 'fake-claude');
  writeFileSync(fakeCli, `#!/usr/bin/env bash\nprintf '%s' "$@" > "${promptLog}"\necho '{"verdict":"duplicate","reason":"same rule","sharedFacts":["r"],"uniqueToA":[],"uniqueToB":["extra"]}'\n`, { mode: 0o755 });
  const out = execFileSync('python3', ['core/extractors/claude_adapter.py'], {
    cwd: process.cwd(),
    input: JSON.stringify({
      task: 'dedup',
      pageA: { path: 'entities/a.md', content: 'BODY_A' },
      pageB: { path: 'entities/b.md', content: 'BODY_B' },
    }),
    encoding: 'utf8',
    env: { ...process.env, QMD_EXTRACTOR_CLAUDE_BIN: fakeCli },
  });
  const parsed = JSON.parse(out);
  assert.equal(parsed.verdict, 'duplicate');
  assert.equal(parsed.reason, 'same rule');
  assert.deepEqual(parsed.uniqueToB, ['extra']);
  const prompt = readFileSync(promptLog, 'utf8');
  assert.match(prompt, /BODY_A/);
  assert.match(prompt, /BODY_B/);
  assert.doesNotMatch(prompt, /REFUTE/, 'verify 프롬프트가 아님');
  assert.doesNotMatch(prompt, /wiki candidates/, 'extraction 프롬프트가 아님');
  removeTemp(d);
});

test('judge_engine_pool: backends-only config resolves without a producer (retroactive scan has none)', () => {
  // Regression: the scan calls is_available()/judge_pair() with no engine, so a
  // config using extractor.backends WITHOUT builtins resolved to "" -> backends[""]
  // missed -> judge permanently "unavailable" -> every scan silently degraded to the
  // legacy score gate. Both dogfood projects use exactly that shape.
  const py = `import json,os,sys; sys.path.insert(0,'core'); import wiki_dedup_judge as j
cfg_backends = {'extractor': {'backends': {'codex': ['/x/codex'], 'claude': ['/x/claude']}}}
cfg_builtins = {'extractor': {'builtins': ['codex'], 'backends': {'claude': ['/x/claude']}}}
out = {}
out['pool_no_producer'] = j.judge_engine_pool(cfg_backends)
out['pool_builtins_first'] = j.judge_engine_pool(cfg_builtins)
os.environ['QMD_ENGINE'] = 'codex'
out['env_configured'] = j.judge_engine_pool(cfg_backends)
os.environ['QMD_ENGINE'] = 'gemini'
out['env_unconfigured'] = j.judge_engine_pool(cfg_backends)
del os.environ['QMD_ENGINE']
out['available_no_producer'] = j.is_available(cfg_backends)
# 생산자를 아는 경우(write-time): 그 엔진이 **마지막**으로 밀린다.
attempts, mode, reason = j.plan_judge_attempts(cfg_backends, 'claude')
out['order_with_producer'] = [a['engine'] for a in attempts]
out['modes'] = [a['mode'] for a in attempts]
out['mode'] = mode
# 생산자를 모르는 경우(scan): 예전 순서 그대로이고 어떤 시도도 교차를 주장하지 않는다.
scan_attempts, _, _ = j.plan_judge_attempts(cfg_backends)
out['order_scan'] = [a['engine'] for a in scan_attempts]
out['modes_scan'] = [a['mode'] for a in scan_attempts]
print(json.dumps(out))`;
  const out = JSON.parse(runLib(py));
  // Deterministic pick (sorted) so two hosts scanning the same project agree.
  assert.deepEqual(out.pool_no_producer, ['claude', 'codex']);
  assert.deepEqual(out.pool_builtins_first, ['codex', 'claude'], 'builtins가 backends 키보다 우선');
  assert.equal(out.env_configured[0], 'codex', '설정된 host engine이면 QMD_ENGINE 우선');
  assert.equal(out.env_unconfigured[0], 'claude', '미설정 engine 라벨로는 죽지 않음');
  assert.equal(out.available_no_producer, true, '생산자 없이도 judge 가용');
  assert.deepEqual(out.order_with_producer, ['codex', 'claude'], '생산 엔진(claude)이 마지막');
  assert.deepEqual(out.modes, ['cross-engine', 'self']);
  assert.equal(out.mode, 'prefer');
  assert.deepEqual(out.order_scan, ['claude', 'codex'], 'scan 순서는 예전 그대로');
  assert.deepEqual(out.modes_scan, ['unknown', 'unknown'], '생산자 불명이면 교차를 주장하지 않는다');
});

// ------------------------------------------------- write-time gate (wiki_compile)

function writeCompileSettings(work, compile = {}) {
  mkdirSync(join(work, '.auto-context'), { recursive: true });
  writeFileSync(join(work, '.auto-context', 'settings.json'), JSON.stringify({
    indexing: true,
    collections: ['proj-wiki'],
    collectionPaths: { 'proj-wiki': '.auto-context/wiki' },
    collectionRoles: { 'proj-wiki': 'wiki' },
    wikiPath: '.auto-context/wiki',
    compile: {
      mode: 'auto-wiki',
      defaultStatus: 'generated',
      verify: { enabled: false },
      ...compile,
    },
  }));
}

function writeExistingCard(work, rel, body = 'The stalker asked for CCTV footage at the front desk.') {
  const full = join(work, '.auto-context', 'wiki', rel);
  mkdirSync(join(full, '..'), { recursive: true });
  writeFileSync(full, [
    '---',
    'title: "Existing"',
    'type: entity',
    'status: generated',
    'createdBy: qmd-auto-context',
    'reviewed: false',
    '---',
    '',
    '> Auto-generated by qmd-auto-context from conversation/work context. Review, edit, or delete if wrong.',
    '',
    '<!-- qmd:auto:start id="main" sourceHash="abc123" -->',
    '## Summary',
    body,
    '<!-- qmd:auto:end -->',
    '',
  ].join('\n'));
  return full;
}

function runCompile(work, payload, env = {}) {
  return JSON.parse(execFileSync('python3', ['core/wiki_compile.py', '--cwd', work], {
    encoding: 'utf8',
    input: JSON.stringify(payload),
    // QMD_DIRTY_QUEUE를 임시 경로로 격리하지 않으면 dirty queue 유출 시 사용자 실제 qmd 인덱스에 고아 컬렉션이 등록되며(복구 불가 고아 등록), 자동으로 정리할 수 있는 경로가 없다.
    env: { ...process.env, QMD_DIRTY_QUEUE: join(work, 'dirty-queue'), ...env },
  }));
}

function fixtureFor(work, hits) {
  const fixture = join(work, 'fixture.json');
  writeFileSync(fixture, JSON.stringify({ results: hits }));
  return fixture;
}

// This is the regression test for the leak that produced the surviving
// near-duplicates: the write-time gate used to run only for `slug`-resolved
// targets, so an extractor-supplied explicit targetPath skipped dedup entirely.

test('write-time gate: with no extractor configured nothing is judged and the daemon is not even queried', () => {
  const work = repoTemp('judge-unavailable-legacy');
  try {
    writeCompileSettings(work); // no extractor at all
    writeExistingCard(work, 'entities/existing.md');
    // An explicit targetPath took the legacy no-gate path before this change, and
    // must keep taking it when no judge exists — including making no /query call.
    const out = runCompile(work, {
      title: 'Explicit target, no judge available',
      summary: 'Machines without a host CLI must behave exactly as before.',
      suggestedType: 'entity',
      confidence: 'high',
      targetPath: '.auto-context/wiki/entities/no-judge.md',
    }, { QMD_DAEMON_URL: 'http://127.0.0.1:9' });

    assert.equal(out.action, 'created');
    assert.equal(existsSync(join(work, '.auto-context/wiki/entities/no-judge.md')), true);
  } finally {
    removeTemp(work);
  }
});

// ------------------------------------- 교차 엔진 (write-time gate 한정)
//
// 4단계가 verify 에서 실측한 노출: verified 688장 중 655장(95%)이 자기검증이었다. dedup judge 도
// 같은 구조였다 — 카드를 만든 엔진을 hint 로 받아 **그 엔진으로** 판정했다.
//
// verify 의 처방(`plan_verify_attempts`)을 그대로 쓸 수는 없다: dedup 은 카드 **두 장**을
// 비교하고 기존 카드의 생산자는 기록되지 않으므로(라이브 실측 0/12쌍) 무엇이 자기검증인지
// 확정되지 않는다. 그래서 생산자가 사실로 확정된 write-time gate 만 확장하고, 공용화한 것은
// **순서 규칙**(`wiki_compile_worker.plan_engine_order`) 하나다.

function crossEngineSettings(work, backends, judge = {}, extra = {}) {
  writeCompileSettings(work, {
    extractor: { backends, timeout: 30 },
    semanticDedup: { judge, ...extra },
  });
}

function runCrossCompile(work, fixture, title, engine = 'claude') {
  return runCompile(work, {
    title,
    summary: 'A near-duplicate of the existing card, restated in other words.',
    suggestedType: 'entity',
    confidence: 'high',
    // 생성 엔진은 **큐 잡이 정하는 사실**이다(worker 가 candidate.engine 을 대입한다).
    engine,
  }, { QMD_QUERY_FIXTURE: fixture });
}

const mergeNeeded = (work) => jsonl(join(work, '.auto-context/compile/merge-needed.jsonl'));
const globalCooldown = (work) => join(work, '.auto-context/compile/dedup-judge-cooldown');
const engineCooldown = (work) => join(work, '.auto-context/compile/dedup-judge-engine-cooldown.json');

// degrade 의 **종점**: 선호 후보가 유료로 실패하면(비127) 같은 run 에서 다음 엔진을 부르지
// 않는다(이중 과금). 그 후보만 엔진 단위로 식혀 **다음 run 이 생산 엔진으로 degrade** 하게
// 한다 — 전역 식힘만 있으면 만료마다 같은 후보를 다시 불러 판정이 영구 정지한다(verify 가
// 0.x 전역 식힘에서 겪은 것과 같은 클래스).

// --------------------------------------------- retroactive scan (wiki_dedup_scan)

function writeScanSettings(work, semanticDedup = {}, compile = {}) {
  mkdirSync(join(work, '.auto-context'), { recursive: true });
  writeFileSync(join(work, '.auto-context', 'settings.json'), JSON.stringify({
    indexing: true,
    collections: ['proj-wiki'],
    collectionPaths: { 'proj-wiki': '.auto-context/wiki' },
    collectionRoles: { 'proj-wiki': 'wiki' },
    wikiPath: '.auto-context/wiki',
    compile: {
      mode: 'auto-wiki',
      semanticDedup,
      ...compile,
    },
  }));
}

function writePage(work, rel, body) {
  const full = join(work, '.auto-context', 'wiki', rel);
  mkdirSync(join(full, '..'), { recursive: true });
  writeFileSync(full, [
    '---',
    'title: "Test Page"',
    'type: entity',
    'status: generated',
    'createdBy: qmd-auto-context',
    '---',
    '',
    '> Auto-generated by qmd-auto-context from conversation/work context. Review, edit, or delete if wrong.',
    '',
    '<!-- qmd:auto:start id="main" sourceHash="abc123" -->',
    '## Summary',
    body,
    '<!-- qmd:auto:end -->',
    '',
  ].join('\n'));
  return full;
}

function runScan(work, env = {}) {
  const dirs = {
    cooldownDir: join(work, 'dedup-cooldown'),
    stateDir: join(work, 'sync-state'),
    logFile: join(work, 'dedup.log'),
  };
  execFileSync('python3', ['core/wiki_dedup_scan.py', '--cwd', work], {
    encoding: 'utf8',
    env: {
      ...process.env,
      QMD_DEDUP_COOLDOWN_DIR: dirs.cooldownDir,
      QMD_SYNC_STATE_DIR: dirs.stateDir,
      QMD_DEDUP_LOG: dirs.logFile,
      ...env,
    },
  });
  return dirs;
}

function clearCooldown(dirs) {
  removeTemp(dirs.cooldownDir);
}

function snapshotOf(stateDir) {
  if (!existsSync(stateDir)) return null;
  const file = readdirSync(stateDir).find((f) => f.endsWith('-wiki-dedup.json'));
  return file ? JSON.parse(readFileSync(join(stateDir, file), 'utf8')) : null;
}

const scanQueue = (work) => jsonl(join(work, '.auto-context/compile/dedup-needed.jsonl'));
const scanSkipped = (work) => jsonl(join(work, '.auto-context/compile/dedup-skipped.jsonl'));

// The measured failure: a real duplicate can only ever be retrieved at rank>=2,
// where the score is capped around 0.625, so autoMergeThreshold's 0.9 default can
// never fire. The judge has to be what decides.

// **retroactive scan 은 교차 엔진으로 확장하지 않는다.** 기존 카드 두 장의 생산자는 어디에도
// 기록되지 않아(라이브 실측: judge 가 돈 12쌍 전부 `generated-manifest.jsonl` 에 생산 엔진 없음)
// "무엇이 자기검증인지"가 정의되지 않고, 두 생산자가 다르면 중립 엔진이라는 것 자체가
// 성립하지 않는다. 그래서 이 경로는 예전 선택(host engine → 풀 첫 후보)을 그대로 쓴다.

// verify 원장과 같은 클래스: `distinct` 판정(유료)을 기록할 곳이 없으면 다음 scan 이 같은 쌍을
// 다시 판정하고 매 scan 이 과금이다. 종점은 유료 호출을 아예 하지 않는 것이므로 판정 전에
// 억제 원장 쓰기 가능성을 확인한다(`wiki_compile.ledger_writable` — verify 와 같은 함수).

// ========================================================== 적대적 리뷰 major 3건 (2026-07-31)

// MAJOR 1 — `crossEngine:"require"` 가 retroactive scan 의 판정을 통째로 죽였다.
//
// `require` 는 "생성 엔진이 **아닌** 엔진이 판정한다"는 약속인데 scan 경로에는 생성 엔진이라는
// 사실이 존재하지 않는다(카드 두 장, 둘째 생산자 미기록 — 실측 12쌍 중 0쌍). 즉 **절대 만족될 수
// 없는 조건에 fail-closed** 가 걸려 있었다: producing="" → attributable=False →
// `cross_engine_unavailable` → `is_available` False → scan 의 `judging=False` → retrieval floor 가
// `autoMergeThreshold`(0.9)로 올라가 사실상 아무것도 큐되지 않는다. 이 커밋 자신의 docstring 과
// docs/settings.md 는 "scan 은 확장하지 않고 예전 선택을 그대로 쓴다"고 서술해 그 상실을 부정했다.

test('MAJOR 1 — 귀속 가능성은 경로의 성질이다: scan 은 waive, write-time 은 그대로 fail-closed', () => {
  const py = `import json,sys; sys.path.insert(0,'core'); import wiki_dedup_judge as j
cfg = {'extractor': {'backends': {'claude': ['/x/claude'], 'codex': ['/x/codex']}},
       'semanticDedup': {'judge': {'crossEngine': 'require'}}}
out = {}
# retroactive scan: 생산자라는 사실이 존재하지 않는다 → require 미적용, prefer 와 같은 순서.
a, mode, reason = j.plan_judge_attempts(cfg, '', attribution=j.ATTRIBUTION_NONE)
out['scan'] = [[x['engine'] for x in a], mode, reason]
out['scan_modes'] = [x['mode'] for x in a]
out['scan_available'] = j.is_available(cfg, attribution=j.ATTRIBUTION_NONE)
out['scan_waived'] = j.require_waived(cfg, j.ATTRIBUTION_NONE)
# write-time 인데 이번 카드의 생산자를 못 읽음 → 사용자가 고칠 수 있는 상태라 fail-closed 유지.
b, bmode, breason = j.plan_judge_attempts(cfg, '', attribution=j.ATTRIBUTION_JOB)
out['job_unknown_producer'] = [[x['engine'] for x in b], bmode, breason]
out['job_waived'] = j.require_waived(cfg, j.ATTRIBUTION_JOB)
# write-time + 귀속 가능 → 교차가 실제로 강제된다(생산 엔진 제외).
c, _, _ = j.plan_judge_attempts(cfg, 'claude', attribution=j.ATTRIBUTION_JOB)
out['job_attributed'] = [[x['engine'] for x in c], [x['mode'] for x in c]]
# 기본값은 write-time 이다 — 새 호출자가 실수로 waive 하지 않는다.
d, _, dreason = j.plan_judge_attempts(cfg, '')
out['default_reason'] = dreason
print(json.dumps(out))`;
  const out = JSON.parse(runLib(py));
  assert.deepEqual(out.scan, [['claude', 'codex'], 'require', ''], 'scan 은 후보를 잃지 않는다');
  assert.deepEqual(out.scan_modes, ['unknown', 'unknown']);
  assert.equal(out.scan_available, true, 'is_available 도 같은 판정을 써야 한다');
  assert.equal(out.scan_waived, true);
  assert.deepEqual(out.job_unknown_producer, [[], 'require', 'cross_engine_unavailable']);
  assert.equal(out.job_waived, false);
  assert.deepEqual(out.job_attributed, [['codex'], ['cross-engine']]);
  assert.equal(out.default_reason, 'cross_engine_unavailable', '기본 attribution 은 JOB');
});

// MAJOR 2 — 판정 불가 출력이 억제 기록 없이 매 scan 재과금됐다.
//
// judge 는 `invalid_extractor_json`/`invalid_verdict` 를 `unclear`+OK 로 돌리는데 scan 은
// `distinct` 만 `dedup-skipped.jsonl` 에 기록했다 → 마커가 없어 같은 쌍이 매 scan 재판정 = 재과금.
// verify 는 같은 클래스를 `UNUSABLE_OUTPUT_REASONS`+`defer_or_drop` 로 이미 막았다(남은 후보가
// 있으면 degrade, 소진되면 종점). 대칭으로 만든다.

// MAJOR 3 — dedup 의 transient·식힘이 어디에도 기록되지 않았다(종점 부재 + 거짓 문서).
//
// `_back_off` 의 `engineCooldown`/`cooldownWriteFailed` 를 읽는 소비자가 저장소 전체에 없었고
// (`wiki_dedup_scan`·`wiki_compile` 은 verdict 있는 행만 기록) 요약 줄은 `judged=1 queued=0` 뿐이라
// "판정 실패"와 "unclear 판정"이 구분 불가였다. verify 는 `wiki_verify_worker.defer_or_drop` 에서
// 실제로 로그에 쓴다 — 같은 처방.

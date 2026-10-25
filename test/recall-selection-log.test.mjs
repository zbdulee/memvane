import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { readFileSync, existsSync, mkdtempSync, mkdirSync, writeFileSync, realpathSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { removeTemp } from './helpers/temp.mjs';

// recall.py 를 fixture 모드로 실행하고 stdout(훅 출력)을 그대로 돌려준다.
function run(payload, env = {}) {
  return execFileSync('python3', ['core/recall.py'], {
    input: JSON.stringify(payload),
    encoding: 'utf8',
    env: { ...process.env, QMD_QUERY_FIXTURE: 'test/fixtures/daemon-response.json', ...env },
  }).trim();
}

// QMD_RECALL_LOG 파일에서 selection 사유 이벤트만 파싱.
function selectionEvents(logPath) {
  if (!existsSync(logPath)) return [];
  return readFileSync(logPath, 'utf8')
    .trim()
    .split('\n')
    .filter(Boolean)
    .map((line) => JSON.parse(line))
    .filter((e) => e.event === 'qmd_recall_selection');
}

// 로그의 **모든** 이벤트(두 writer + shadow 진단). 귀속 필드는 event 종류와 무관하게
// 붙어야 하므로 selection 만 걸러 보면 다른 writer 의 누락을 못 잡는다.
function allEvents(logPath) {
  if (!existsSync(logPath)) return [];
  return readFileSync(logPath, 'utf8')
    .trim()
    .split('\n')
    .filter((line) => line.startsWith('{'))
    .map((line) => JSON.parse(line));
}

function withProject(config, fn) {
  const dir = mkdtempSync(join(tmpdir(), 'qmd-sel-log-'));
  mkdirSync(join(dir, '.agents'), { recursive: true });
  writeFileSync(join(dir, '.agents', 'qmd-recall.json'), JSON.stringify(config));
  const logPath = join(dir, 'recall.log');
  try {
    return fn(dir, logPath);
  } finally {
    removeTemp(dir);
  }
}

const PROMPT = '검색 결과 정렬은 어떻게 동작해?';

test('결과 선택 시 reason="selected" 기록', () => {
  withProject({ collections: ['sample'] }, (dir, logPath) => {
    const out = run({ prompt: PROMPT, cwd: dir }, { QMD_RECALL_LOG: logPath });
    assert.ok(out, 'stdout 훅 출력이 있어야 함');
    const ev = selectionEvents(logPath);
    assert.equal(ev.length, 1, 'selection 이벤트 한 줄');
    assert.equal(ev[0].reason, 'selected');
    assert.ok(ev[0].selected_count > 0, 'selected > 0');
    assert.equal(ev[0].selected.length, ev[0].selected_count);
    assert.ok(ev[0].candidates > 0, 'candidates > 0');
  });
});

test('minScore가 모든 후보를 컷하면 결과를 완전히 차단한다 (순위 폴백도 안 살림)', () => {
  // 임계 이상 후보가 애초에 0건이면 사용자가 명시적으로 차단한 것이다 → 0건이 정답.
  // 순위 폴백은 "컷을 통과한 후보가 hard filter로 전멸한" 경우만 구제한다.
  withProject({ collections: ['sample'], minScore: 999 }, (dir, logPath) => {
    const out = run({ prompt: PROMPT, cwd: dir }, { QMD_RECALL_LOG: logPath });
    assert.equal(out, '', 'minScore 전탈락이면 빈 출력');
    const ev = selectionEvents(logPath);
    assert.equal(ev.length, 1);
    assert.equal(ev[0].reason, 'no_results_after_filter');
    assert.ok(ev[0].dropped_min_score > 0, 'minScore 탈락 수가 잡혀야 함');
    assert.equal(ev[0].selected_count, 0);
    assert.deepEqual(ev[0].selected, []);
    assert.equal(ev[0].rank_fallback_used, false, '컷 자체를 무효화하면 안 된다');
  });
});

test('컷 통과 후보가 hard filter로 전멸하면 컷 밖 eligible 1건을 rescue', () => {
  // (b) 케이스: 1위는 컷을 통과했지만 skipPaths로 제거 → 2위를 구제한다.
  // dropped_* 는 최초 cutoff/drop 수를 그대로 유지한다(집계 호환).
  const fixtureBody = JSON.stringify({ results: [
    { file: 'sample/docs/skipme.md', title: 'Skip me', score: 1 },
    { file: 'sample/docs/keep.md', title: 'Keep me', score: 0.5 },
  ] });
  withProject({ collections: ['sample'], minScore: 0.8, skipPaths: ['skipme'] }, (dir, logPath) => {
    const fixture = join(dir, 'fixture.json');
    writeFileSync(fixture, fixtureBody);
    const out = run({ prompt: PROMPT, cwd: dir }, { QMD_RECALL_LOG: logPath, QMD_QUERY_FIXTURE: fixture });
    assert.match(out, /keep\.md/, '컷 밖 eligible 1건이 구제되어야 함');
    assert.doesNotMatch(out, /skipme/, 'skip 대상은 어떤 경로로도 surface 금지');
    const ev = selectionEvents(logPath);
    assert.equal(ev[0].reason, 'selected');
    assert.equal(ev[0].selected_count, 1, '정확히 1건만 rescue');
    assert.equal(ev[0].selected.length, 1);
    assert.equal(ev[0].dropped_skip, 1);
    assert.equal(ev[0].dropped_min_score, 1, '최초 순위 컷 탈락 수는 그대로 기록');
    assert.equal(ev[0].rank_fallback_used, true);
    assert.equal(ev[0].rescued_original_rank, 2);
    assert.equal(ev[0].fallback_phase, 'primary', 'flat 전략의 primary phase rescue');
  });
});

test('skipPaths로 전부 탈락하면 rescue 없이 0건 (skip은 절대 살리지 않는다)', () => {
  withProject({ collections: ['sample'], skipPaths: ['docs/guide.md'] }, (dir, logPath) => {
    const out = run({ prompt: PROMPT, cwd: dir }, { QMD_RECALL_LOG: logPath });
    assert.equal(out, '', 'skip 대상은 rescue 대상이 아니므로 빈 출력');
    const ev = selectionEvents(logPath);
    assert.equal(ev[0].reason, 'no_results_after_filter');
    assert.equal(ev[0].dropped_skip, 1);
    assert.equal(ev[0].rank_fallback_used, false);
    assert.equal('rescued_original_rank' in ev[0], false, 'rescue 없으면 필드도 없다');
  });
});

test('cutoff 안에 eligible이 있으면 rescue가 일어나지 않는다 (회귀 방지)', () => {
  withProject({ collections: ['sample'], minScore: 0.5 }, (dir, logPath) => {
    const out = run({ prompt: PROMPT, cwd: dir }, { QMD_RECALL_LOG: logPath });
    assert.ok(out);
    const ev = selectionEvents(logPath);
    assert.equal(ev[0].reason, 'selected');
    assert.equal(ev[0].rank_fallback_used, false, '정상 경로는 폴백을 쓰지 않아야 함');
  });
});

test('selection 로그를 켜도 stdout은 순수 훅 JSON만 (로그는 파일로만)', () => {
  withProject({ collections: ['sample'] }, (dir, logPath) => {
    const out = run({ prompt: PROMPT, cwd: dir }, { QMD_RECALL_LOG: logPath });
    const parsed = JSON.parse(out); // 한 줄 JSON 파싱 성공 = stdout 오염 없음
    assert.ok(parsed.hookSpecificOutput.additionalContext);
    assert.ok(!out.includes('qmd_recall_selection'), '로그가 stdout(모델 컨텍스트)에 새어나오면 안 됨');
    assert.ok(existsSync(logPath), '로그는 파일에 기록되어야 함');
  });
});

test('이벤트 비활성 시 reason="event_disabled" 기록', () => {
  withProject({ collections: ['sample'], events: ['sessionStart', 'postToolUse'] }, (dir, logPath) => {
    const out = run(
      { hook_event_name: 'UserPromptSubmit', prompt: PROMPT, cwd: dir },
      { QMD_RECALL_LOG: logPath },
    );
    assert.equal(out, '', '비활성 이벤트면 빈 출력');
    const ev = selectionEvents(logPath);
    assert.equal(ev.length, 1);
    assert.equal(ev[0].reason, 'event_disabled');
  });
});

test('wikiOnly + wiki role 없음 → fixture 경로에서도 reason="no_wiki_collections" + 빈 출력', () => {
  // fixture 경로에서도 live와 동일하게 조기 종료해야 한다(수정 전엔 no_results_after_filter로 오탐).
  withProject(
    { collections: ['sample'], collectionRoles: { sample: 'raw' }, recallStrategy: 'wikiOnly' },
    (dir, logPath) => {
      const out = run({ prompt: PROMPT, cwd: dir }, { QMD_RECALL_LOG: logPath });
      assert.equal(out, '', 'wiki role 없으면 wikiOnly는 무출력(raw 누출 금지)');
      const ev = selectionEvents(logPath);
      assert.equal(ev.length, 1);
      assert.equal(ev[0].reason, 'no_wiki_collections');
    },
  );
});

test('QMD_RECALL_LOG 미설정이면 부작용 없이 정상 동작 (no-op)', () => {
  withProject({ collections: ['sample'] }, (dir) => {
    const out = run({ prompt: PROMPT, cwd: dir }); // QMD_RECALL_LOG 없음
    const parsed = JSON.parse(out);
    assert.ok(parsed.hookSpecificOutput.additionalContext);
  });
});

// **조기 return 도 사유를 남긴다.** 줄이 하나도 없으면 "정상 skip" 과 "훅이 도달하지 못함"
// (디스패처 오류·plugin root 미주입 exit 127)을 구분할 수 없고, E2E 진단이 이 로그에만
// 의존한다. sandbox 는 "즉시 무출력 종료" 가 계약이라 유일한 예외다.
test('짧은 프롬프트 조기 return도 reason="prompt_too_short"를 남긴다', () => {
  withProject({ collections: ['sample'] }, (dir, logPath) => {
    const out = run({ prompt: '짧다', cwd: dir }, { QMD_RECALL_LOG: logPath });
    assert.equal(out, '', '주입은 없다(기존 동작)');
    const events = selectionEvents(logPath);
    assert.equal(events.length, 1, '조용히 나가지 않는다');
    assert.equal(events[0].reason, 'prompt_too_short');
    assert.equal(events[0].prompt_chars, 2, '판정 근거(길이)도 남는다');
  });
});

test('빈 stdin / JSON 아닌 payload도 사유를 남긴다', () => {
  withProject({ collections: ['sample'] }, (dir, logPath) => {
    for (const input of ['', 'not-json{']) {
      execFileSync('python3', ['core/recall.py'], {
        input, encoding: 'utf8',
        env: { ...process.env, QMD_RECALL_LOG: logPath },
      });
    }
    const reasons = selectionEvents(logPath).map((e) => e.reason);
    assert.deepEqual(reasons, ['empty_stdin', 'invalid_payload_json']);
    const bad = selectionEvents(logPath)[1];
    assert.equal(bad.stdin_chars, 'not-json{'.length, '길이만 남긴다(본문 유출 금지)');
    assert.ok(!JSON.stringify(bad).includes('not-json'), 'payload 본문이 로그로 새지 않는다');
  });
});

test('sandbox는 로그 파일조차 만들지 않는다 (즉시 무출력 종료 계약)', () => {
  withProject({ collections: ['sample'] }, (dir, logPath) => {
    const out = execFileSync('python3', ['core/recall.py'], {
      input: JSON.stringify({ prompt: PROMPT, cwd: dir }),
      encoding: 'utf8',
      env: { ...process.env, QMD_RECALL_LOG: logPath, QMD_SANDBOX: '1' },
    });
    assert.equal(out.trim(), '');
    assert.equal(existsSync(logPath), false, 'sandbox는 부작용이 0이어야 한다');
  });
});

// ── 프로젝트 귀속 ───────────────────────────────────────────────────────────
// QMD_RECALL_LOG 는 프로젝트별 파일이 아니라 **전역 파일 하나**다(라이브 설정이
// ~/.claude/settings.json 의 env 로 한 경로를 준다). 줄에 귀속이 없으면 여러 프로젝트의
// 진단이 한 파일에 섞여 "이 프로젝트의 recall 이 나아졌는가"를 물을 수 없다 — 3.8 백필
// 검증에서 실제로 막혀 census 델타로 우회했다.
test('모든 로그 줄에 project_root 귀속이 붙는다 (writer 두 종류 공통)', () => {
  withProject({ collections: ['sample'] }, (dir, logPath) => {
    run({ prompt: PROMPT, cwd: dir }, { QMD_RECALL_LOG: logPath });
    // tmpdir 은 macOS 에서 /var → /private/var 심볼릭 링크라 resolve 된 값과 비교한다.
    const root = realpathSync(dir);
    const events = allEvents(logPath);
    const kinds = new Set(events.map((e) => e.event));
    assert.ok(kinds.has('qmd_recall_selection'), `selection 줄이 있어야 한다: ${[...kinds]}`);
    // log_score_observation 은 log_recall_event 와 **다른 writer** 라 한쪽만 고치면
    // 조용히 갈린다. 라이브 로그의 9,051줄이 이 writer 소산이다.
    assert.ok(kinds.has('qmd_score_observation'), `score observation 줄이 있어야 한다: ${[...kinds]}`);
    for (const ev of events) {
      assert.equal(ev.project_root, root, `${ev.event} 줄의 project_root`);
    }
  });
});

test('cwd 를 알기 전 줄은 project_root 가 null 이고 키 자체는 존재한다', () => {
  // 키 부재(귀속 도입 전 포맷)와 "아직 프로젝트를 모른다"는 다른 사실이다. 뭉치면
  // 옛 줄과 새 줄이 구분되지 않는다 — `lex_hits` 의 "안 물었다 ≠ 물었는데 0" 과 같은 규약.
  withProject({ collections: ['sample'] }, (dir, logPath) => {
    execFileSync('python3', ['core/recall.py'], {
      input: '', encoding: 'utf8',
      env: { ...process.env, QMD_RECALL_LOG: logPath },
    });
    const ev = selectionEvents(logPath);
    assert.equal(ev.length, 1);
    assert.equal(ev[0].reason, 'empty_stdin');
    assert.ok('project_root' in ev[0], '키는 언제나 있어야 한다');
    assert.equal(ev[0].project_root, null, 'cwd 를 모르는 줄은 null');
  });
});

test('하위 디렉터리에서 시작한 세션도 같은 project_root 로 묶인다', () => {
  // cwd 로 묶으면 한 프로젝트가 세션 시작 위치마다 여러 갈래로 쪼개져 집계가 무의미해진다.
  // HOME 을 가짜로 두는 이유: `config._project_search_dirs` 는 **HOME 하위에서만** 부모를
  // 탐색한다(HOME 밖이면 cwd 하나만 본다). tmpdir 은 HOME 밖이라 그대로 두면 이 테스트가
  // 검증하려는 상향 탐색 자체가 일어나지 않는다 — 라이브 프로젝트는 전부 HOME 하위다.
  const home = mkdtempSync(join(tmpdir(), 'qmd-sel-home-'));
  const dir = join(home, 'proj');
  const sub = join(dir, 'packages', 'inner');
  mkdirSync(join(dir, '.agents'), { recursive: true });
  mkdirSync(sub, { recursive: true });
  writeFileSync(join(dir, '.agents', 'qmd-recall.json'), JSON.stringify({ collections: ['sample'] }));
  const logPath = join(home, 'recall.log');
  try {
    run({ prompt: PROMPT, cwd: dir }, { QMD_RECALL_LOG: logPath, HOME: home });
    run({ prompt: PROMPT, cwd: sub }, { QMD_RECALL_LOG: logPath, HOME: home });
    const events = selectionEvents(logPath);
    assert.equal(events.length, 2, `두 세션 모두 줄을 남긴다: ${events.map((e) => e.reason)}`);
    const roots = new Set(events.map((e) => e.project_root));
    assert.deepEqual([...roots], [realpathSync(dir)], '두 세션이 한 프로젝트로 묶인다');
  } finally {
    removeTemp(home);
  }
});

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';

function kw(prompt, patterns = []) {
  const out = execFileSync('python3', ['core/keywords.py', '--patterns', patterns.join(',')], { input: prompt, encoding: 'utf8' });
  return JSON.parse(out); // { keywords: [...], identifiers: [...], lexicalTerms: [...] }
}

// qmd 2.5.3 `dist/store.js` 의 규칙을 그대로 옮긴 것 (기대값 하드코딩).
//   sanitizeFTS5Term:  term.replace(/[^\p{L}\p{N}'_]/gu, '').toLowerCase()  → `"x"*` prefix
//   isHyphenatedToken: 하이픈 토큰은 조각 phrase 로 (buildFTS5Query 가 먼저 분기)
// 즉 `.` `/` 는 **삭제**되므로 term 에 남아 있으면 색인에 없는 토큰이 만들어진다.
const sanitizeFTS5Term = t => t.replace(/[^\p{L}\p{N}'_]/gu, '').toLowerCase();
const isHyphenatedToken = t => /^[\p{L}\p{N}][\p{L}\p{N}'-]*-[\p{L}\p{N}][\p{L}\p{N}'-]*$/u.test(t);

// 모든 식별자 term 은 qmd 가 색인 토큰에 매칭시킬 수 있는 plain term 이어야 한다.
// quoted phrase 는 exact adjacency + AND 결합 때문에 lex 전체를 0건으로 만들 수 있어
// 절대 만들지 않는다(실측: wiki 카드 본문에는 원본 파일 경로가 보존되지 않는다).
function assertIndexableLexTerm(term) {
  assert.ok(!term.includes('"'), `quoted phrase 를 만들면 안 됨: ${term}`);
  assert.ok(!/[./]/.test(term), `sanitize 로 붙어버리는 구분자가 남음: ${term}`);
  if (isHyphenatedToken(term)) return;   // qmd 가 하이픈을 알아서 조각낸다
  assert.equal(sanitizeFTS5Term(term), term.toLowerCase(), `plain term 이 sanitize 로 뭉개짐: ${term}`);
}

test('stopwords 제거 + 한국어 어간', () => {
  const r = kw('검색 결과 기반 정렬은 어떻게 동작하나요');
  assert.ok(r.keywords.includes('검색') || r.keywords.includes('정렬'));
  assert.ok(!r.keywords.includes('어떻게'));   // stopword
});

test('ep 패턴 off면 EP 용어 없음', () => {
  const r = kw('EP12 복선', []);
  assert.ok(!r.lexicalTerms.some(t => /EP0?12/.test(t)));
});

test('ep 패턴 on이면 EP 정규화 용어 생성', () => {
  const r = kw('EP12 복선', ['ep']);
  assert.ok(r.lexicalTerms.includes('EP012'));
  assert.ok(r.lexicalTerms.includes('EP12'));
});

test('keywords 상한 5', () => {
  const r = kw('하나 둘 셋 넷 다섯 여섯 일곱 여덟 아홉 열');
  assert.ok(r.keywords.length <= 5);
});

test('host instruction identifiers cannot contaminate the selected question', () => {
  const prompt = '도구를 쓰지 말고 `MCP/Tool.py` 컨텍스트만 써.\r\nrelease QA alpha 문서의 codename은?';
  const r = kw(prompt);
  assert.equal(r.keywordSpan, 'question');
  assert.ok(!r.identifiers.some(t => /MCP|Tool/i.test(t)), JSON.stringify(r));
  assert.doesNotMatch(r.lexQueries[0], /MCP|Tool/i);
});

test('multi-line question selection leaves ambiguous and ordinary requests unchanged', () => {
  const ambiguous = '도구를 쓰지 말고 컨텍스트만 써.\n첫째 질문은?\n둘째 질문은?';
  const ordinary = '원문에서 중요한 맥락을 설명해.\nrelease QA alpha 문서의 codename은?';
  for (const prompt of [ambiguous, ordinary]) {
    assert.equal(kw(prompt).keywordSpan, 'full');
    assert.deepEqual(kw(prompt).keywords, kw(prompt.replaceAll('\n', ' ')).keywords);
  }
  const codeQuestion = 'Use only tool context.\nhttps://example.test/?a=1\nrelease QA alpha codename?';
  assert.equal(kw(codeQuestion).keywordSpan, 'question');
  assert.equal(kw('Use only tool context.\nhttps://example.test/?a=1').keywordSpan, 'full');
  const productName = 'auto-context tool 설정 문제\nrelease QA alpha codename?';
  assert.equal(kw(productName).keywordSpan, 'full');
  const bothQuestions = '도구 없이 컨텍스트 주입되는 이유가 뭐야?\nrecall hook 로그 어디서 봐?';
  assert.equal(kw(bothQuestions).keywordSpan, 'full');
  const ep = 'EP012 도구를 쓰지 말고 컨텍스트만 써.\nrelease QA alpha 문서의 codename은?';
  assert.equal(kw(ep, ['ep']).keywordSpan, 'question');
  assert.ok(kw(ep, ['ep']).epTerms.length > 0);
  assert.deepEqual(kw(ep, ['ep']).epTerms, kw(ep.replaceAll('\n', ' '), ['ep']).epTerms);
});

// --- 식별자(정확 토큰) 추출: lex 검색이 정확 토큰에 매칭되게 하는 입력 측 경로 ---

test('백틱 코드스팬 내용이 식별자로 추출된다', () => {
  const r = kw('`recallVerifiedOnly` 기본값이 뭐야');
  assert.ok(r.identifiers.includes('recallVerifiedOnly'));
  // 식별자는 일반 키워드보다 앞에 온다
  assert.equal(r.lexicalTerms[0], 'recallVerifiedOnly');
});

test('백틱 안 여러 토큰은 각각 추출된다', () => {
  const r = kw('`npm test` 돌리면 실패하는 이유');
  assert.ok(r.identifiers.includes('npm'));
  assert.ok(r.identifiers.includes('test'));
});

test('경로형 토큰은 basename stem plain term으로 승격된다', () => {
  const r = kw('docs/settings.md 파일 구조 설명해줘');
  // 원문 그대로면 sanitize 후 "docssettingsmd"* 가 되어 색인에 없는 토큰이 된다.
  assert.ok(!r.identifiers.includes('docs/settings.md'));
  assert.ok(r.identifiers.includes('settings'));
  assert.ok(r.lexicalTerms.includes('settings'));
  // 회귀 방지: 어떤 term 도 붙어버린 토큰으로 sanitize 되지 않아야 한다.
  assert.ok(!r.lexicalTerms.some(t => sanitizeFTS5Term(t) === 'docssettingsmd'));
  r.identifiers.forEach(assertIndexableLexTerm);
});

test('경로·dotted 토큰은 quoted phrase 를 만들지 않는다 (lex 0건 회귀 방지)', () => {
  // `"settings md"` phrase 는 실측에서 0건이었고 AND 결합이라 lex 전체를 죽였다.
  const prompts = [
    'docs/settings.md 파일 구조 설명해줘',
    'core/wiki_verify_worker.py 동작 확인해줘',
    '`verify-queue.jsonl` 에 enqueue 되는 조건이 뭐야',
    '.auto-context/settings.json 의 recallStrategy 기본값',
    'config.load_project_config 가 어디서 불리나',
  ];
  for (const p of prompts) {
    const r = kw(p);
    assert.ok(!r.lexicalTerms.some(t => t.includes('"')), `${p} → ${JSON.stringify(r.lexicalTerms)}`);
    r.identifiers.forEach(assertIndexableLexTerm);
  }
});

test('경로 stem 승격: 확장자를 떼고 가장 구체적인 조각만 남는다', () => {
  assert.ok(kw('core/wiki_verify_worker.py 동작 확인해줘').identifiers.includes('wiki_verify_worker'));
  assert.ok(kw('config.load_project_config 가 어디서 불리나').identifiers.includes('load_project_config'));
  const r = kw('docs/settings.md 파일 구조 설명해줘');
  assert.ok(!r.identifiers.includes('"settings md"'));
  // 일반 키워드 경로도 같은 토큰(settings)을 내지만 dedup 으로 한 번만 실린다.
  assert.equal(r.lexicalTerms.filter(t => t === 'settings').length, 1);
});

test('생성된 모든 식별자 term은 qmd lex 문법에서 색인 가능한 형태다', () => {
  const prompts = [
    'docs/settings.md 랑 core/wiki_verify_worker.py 를 비교해줘',
    '`verify-queue.jsonl` 에 enqueue 되는 조건이 뭐야',
    'wiki_compile_worker 가 sqlite-wal 을 키우는 이유',
    'CLAUDE_PLUGIN_ROOT 비면 HTTPServer 어댑터가 죽어',
    '.auto-context/settings.json 의 recallStrategy 기본값',
  ];
  for (const p of prompts) {
    const r = kw(p);
    r.identifiers.forEach(assertIndexableLexTerm);
  }
});

test('호출형·snake·kebab·CamelCase 식별자 추출', () => {
  const r = kw('classifyOrigin() 이 wiki_compile_worker 랑 sqlite-wal 을 CamelHumpName 으로 다루나');
  assert.ok(r.identifiers.includes('classifyOrigin'));         // 호출형은 이름만
  assert.ok(r.identifiers.includes('wiki_compile_worker'));    // `_` 는 FTS5 토큰 문자라 원형 유지
  assert.ok(r.identifiers.includes('CamelHumpName'));
  assert.ok(r.identifiers.includes('sqlite-wal'));             // 하이픈은 qmd 가 처리한다
});

test('SCREAMING_SNAKE·대괄호 태그 식별자 추출', () => {
  const r = kw('CLAUDE_PLUGIN_ROOT 비면 [DEV20100] 처럼 exit 127 나고 minScore 0.8 이 안 맞아');
  assert.ok(r.identifiers.includes('CLAUDE_PLUGIN_ROOT'));
  assert.ok(r.identifiers.includes('DEV20100'));
});

test('맨 숫자는 식별자로 승격하지 않는다 (AND 결합에서 lex 를 통째로 죽인다)', () => {
  const r = kw('CLAUDE_PLUGIN_ROOT 비면 exit 127 나고 minScore 0.8 이 안 맞아');
  assert.ok(!r.identifiers.includes('127'));
  assert.ok(!r.identifiers.includes('0.8'));
  assert.ok(!r.identifiers.some(t => /^"?[\d\s.]+"?$/.test(t)));
});

test('숫자는 백틱으로 감쌌을 때만 식별자가 된다', () => {
  const r = kw('exit code 는 `127` 이고 임계치는 `12000` 이야');
  assert.ok(r.identifiers.includes('127'));
  assert.ok(r.identifiers.includes('12000'));
  // 소수점 숫자는 백틱이어도 제외 — sanitize 후 색인에 없는 `08` 토큰이 된다.
  assert.ok(!kw('임계치는 `0.8` 이야 확인해줘').identifiers.some(t => /\d/.test(t)));
});

test('날짜·IP 는 식별자로 새지 않는다', () => {
  const dated = kw('2026년 7월 29일 배포된 docs/settings.md 확인');
  assert.ok(!dated.identifiers.some(t => /\d/.test(t)));
  assert.ok(dated.identifiers.includes('settings'));   // 경로 stem 은 그대로 살아남는다

  const ip = kw('192.168.0.1 에서 timeout 이 나는데 core/recall.py 확인해줘');
  assert.ok(!ip.identifiers.some(t => /\d/.test(t)));
  assert.ok(ip.identifiers.includes('recall'));
});

test('acronym CamelCase 는 원형 그대로 보존된다', () => {
  const r = kw('HTTPServer 랑 XMLParser 어댑터 구조 설명해줘');
  assert.ok(r.identifiers.includes('HTTPServer'));   // PServer 로 잘리면 매칭 불가
  assert.ok(r.identifiers.includes('XMLParser'));    // LParser 로 잘리면 매칭 불가
});

test('부분 문자열이 겹치는 별개 이름이 함께 살아남는다', () => {
  const r = kw('`notebook` 이랑 book() 이 서로 충돌해');
  assert.ok(r.identifiers.includes('notebook'));
  assert.ok(r.identifiers.includes('book'));   // substring dedup 이면 탈락한다
});

test('6번째 이후에 등장하는 식별자도 lex 용어에 포함된다', () => {
  const prompt = '하나 둘 셋 넷 다섯 여섯 일곱 여덟 문서에서 wiki_verify_worker.py 동작 확인';
  const r = kw(prompt);
  assert.equal(r.keywords.length, 5);                              // 일반 단어 cap 유지
  assert.ok(!r.keywords.includes('wiki_verify_worker'));           // 일반 경로로는 못 들어옴
  assert.ok(r.lexicalTerms.includes('wiki_verify_worker'));
});

test('식별자에는 한국어 어간 절단을 적용하지 않는다', () => {
  const r = kw('`settings` 값은 어디서 읽어');
  assert.ok(r.identifiers.includes('settings'));  // "setting"으로 잘리면 매칭이 깨진다
  assert.ok(r.lexicalTerms.includes('settings'));
});

test('식별자 예산은 4개 상한', () => {
  // qmd buildFTS5Query 는 positive term 을 AND 로 결합한다. term 이 늘어날수록
  // 조건이 좁아져 lex 전체가 0건이 될 확률이 오르므로 예산을 작게 유지한다.
  const r = kw('`a_one` `b_two` `c_three` `d_four` `e_five` `f_six` `g_seven` `h_eight`');
  assert.equal(r.identifiers.length, 4);
});

test('한글 전용 프롬프트는 식별자 없이 기존 동작 유지', () => {
  const r = kw('검색 결과 기반 정렬은 어떻게 동작하나요');
  assert.deepEqual(r.identifiers, []);
  assert.deepEqual(r.lexicalTerms, r.keywords);
  assert.ok(!r.keywords.includes('어떻게'));
});

test('ep 패턴 off면 식별자 경로로도 EP 용어가 새지 않는다', () => {
  const prompts = [
    '`EP12` 복선 정리해줘', 'EP-12 복선 정리해줘', 'ep_12 복선 정리해줘',
    // 확장자 변형: 경로 stem 승격 후에도 EP형이 남는다 → 선두 조각으로 잡아야 한다
    'docs/EP12.md 확인해줘 복선 정리', 'docs/EP-12.md 확인해줘 복선 정리', 'docs/ep_12.md 확인해줘 복선 정리',
  ];
  for (const p of prompts) {
    const r = kw(p, []);
    assert.ok(!r.lexicalTerms.some(t => /EP[\s_-]?0?12/i.test(t)), `누출: ${p} → ${JSON.stringify(r.lexicalTerms)}`);
  }
});

test('백틱 식별자는 일반 후보 상한(400) 뒤에 있어도 살아남는다', () => {
  // 코드스팬은 "가장 강한 신호" 계약이므로 bounded work 상한에 밀려선 안 된다.
  const filler = Array.from({ length: 405 }, () => 'alphaword').join(' ');
  const r = kw(`${filler} \`criticalIdentifier\` 확인해줘`);
  assert.ok(r.identifiers.includes('criticalIdentifier'), JSON.stringify(r.identifiers));
  assert.ok(r.lexicalTerms.includes('criticalIdentifier'));
});

test('식별자 추출은 어떤 입력에도 죽지 않는다', () => {
  for (const p of ['``` ``` `` ` ...', '((((((((((()))))))', '[[[[]]]] ///// ____ ....', '`' + 'a'.repeat(500) + '`']) {
    const r = kw(p);
    assert.ok(Array.isArray(r.identifiers));
  }
});

test('adversarial 입력에도 추출이 50ms 이내 (UserPromptSubmit blocking hook)', () => {
  // 인터프리터 기동 시간이 섞이지 않게 in-process 로 측정한다.
  const script = `
import sys, time, json
sys.path.insert(0, "core")
import keywords as K
out = {}
for label, text in [("a", "a"*10000), ("paren", "("*5000), ("dots", "a."*5000), ("log", ("2026-07-29 12:00:00 WARN core/recall.py:123 query_failed retry=3\\n"*140))]:
    start = time.perf_counter()
    K.build_lexical_terms(text, [])
    out[label] = (time.perf_counter() - start) * 1000
print(json.dumps(out))
`;
  const timings = JSON.parse(execFileSync('python3', ['-c', script], { encoding: 'utf8' }));
  for (const [label, ms] of Object.entries(timings)) {
    assert.ok(ms < 50, `${label}: ${ms.toFixed(1)}ms (50ms 예산 초과)`);
  }
});

// --- 훅 경로(recall.py)가 CLI와 같은 EP 정책을 쓰는지: 실제 lex 쿼리로 검증 ---

// --- EP 변형은 독립 lex search 여야 한다 (AND 결합으로 lex 가 죽는 버그) ---

test('순수 숫자 단독 변형(`012`)은 lex 엔트리로 나가지 않는다 (RRF 노이즈)', () => {
  // AND 결합과는 별개 이유다: `012`* 는 EP 표기가 아니라 "012 로 시작하는 아무 토큰"에
  // 걸려(특히 카드 frontmatter 의 sources 경로) 재현율 없이 노이즈만 늘린다.
  // novel 실측: 012 제외 3건(전부 관련) vs 포함 8건(무관 5건 추가).
  for (const [prompt, patterns] of [
    ['EP12 에서 서미래가 먹은 약과 부작용', ['ep']],
    ['EP 12 무슨 일이 있었는지', ['ep']],
    ['12화 줄거리 정리', ['ep']],
    ['EP1 EP2 EP3 정리', ['ep']],
  ]) {
    const r = kw(prompt, patterns);
    for (const q of r.lexQueries.slice(1)) {
      assert.ok(/^EP\d+$/i.test(q), `EP 접두 없는 변형이 실렸다: ${q}`);
    }
    // 합성 변형 목록 자체에도 남지 않아야 한다 — extract_ep_terms 단계에서 제거했으므로
    // 로그의 term 수(lex_terms)와 실제 payload 가 어긋나지 않는다.
    assert.ok(!r.epTerms.some(t => /^\d+$/.test(t)),
      `순수 숫자 변형 누출: ${JSON.stringify(r.epTerms)}`);
    // 원문에 직접 쓴 순수 숫자도 일반 AND 문자열에 남지 않는다(정책 일관성).
    assert.ok(!/(^|\s)\d+(\s|$)/.test(r.lexQueries[0]),
      `EP 언급의 순수 숫자가 일반 문자열에 남았다: ${r.lexQueries[0]}`);
  }
  // EP 접두 표기는 그대로 유지된다.
  assert.deepEqual(kw('EP12 복선', ['ep']).lexQueries.slice(1), ['EP012', 'EP12']);
});

test('단독 조사 토큰은 stopword — 의미어가 term 예산(5)에서 밀려나지 않는다', () => {
  // "EP12 에서 …" 처럼 띄어 쓰면 `에서` 가 토큰 하나로 남아 5개 cap 을 먹고
  // AND 조건만 좁힌다(라이브에서 `부작용` 이 탈락했다).
  const r = kw('EP12 에서 서미래가 먹은 약과 부작용');
  assert.ok(!r.keywords.includes('에서'));
  assert.ok(r.keywords.includes('부작용'), `조사가 의미어를 밀어냈다: ${JSON.stringify(r.keywords)}`);
  // 활용형(`먹은`)은 건드리지 않는다 — 어간 판정 없이 버리면 의미어를 잃는다.
  assert.ok(r.keywords.includes('먹은'));
  // `약과` 처럼 조사를 포함한 듯한 의미어는 그대로 살아야 한다.
  assert.ok(r.keywords.includes('약과'));
});

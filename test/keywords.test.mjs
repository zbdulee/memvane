import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';

function kw(prompt, patterns = []) {
  const out = execFileSync('python3', ['core/keywords.py', '--patterns', patterns.join(',')], { input: prompt, encoding: 'utf8' });
  return JSON.parse(out); // { keywords: [...], identifiers: [...], lexicalTerms: [...] }
}

// recall.py(훅 경로)가 실제로 데몬에 보내는 lex 쿼리를 회수한다. CLI와 정책을 공유하는지
// (build_lexical_terms 단일화) 확인하려면 문자열 추출만으로는 부족하다.
function recallSearches(prompt, settings) {
  const out = execFileSync('python3', ['test/helpers/capture_query.py', prompt, JSON.stringify(settings)], { encoding: 'utf8' });
  const { queries } = JSON.parse(out);
  assert.equal(queries.length, 1, 'stub 데몬이 정확히 한 번 질의를 받아야 한다');
  return queries[0].searches;
}

// EP 변형은 독립 lex 엔트리라 lex 문자열이 여러 개일 수 있다.
function recallLexQueries(prompt, settings) {
  return recallSearches(prompt, settings).filter(s => s.type === 'lex').map(s => s.query);
}

// 일반 키워드(식별자 + 키워드)가 실리는 lex 문자열 = searches 의 첫 lex 엔트리.
function recallLexQuery(prompt, settings) {
  return recallLexQueries(prompt, settings)[0];
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

test('multi-line host instruction does not replace the actual question in lex search', () => {
  const prompt = [
    '도구를 쓰지 말고 이번 프롬프트에 자동 주입된 프로젝트 컨텍스트만 근거로 답해줘.',
    'release QA alpha 문서의 codename은?',
    '주입된 컨텍스트에 없으면 "없음"이라고만 답해.',
  ].join('\n');
  const built = kw(prompt);
  const lex = recallLexQuery(prompt, { collections: ['qa'] });
  assert.equal(built.keywordSpan, 'question');
  assert.match(lex, /release|alpha|codename/i);
  assert.doesNotMatch(lex, /도구|쓰지|말고|컨텍스트|없으면/);
  assert.ok(built.keywords.some(t => /release|alpha|codename/i.test(t)));
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

test('recall 경로: ep-off면 cap 뒤에 등장한 EP 도 lex 쿼리에 없다', () => {
  const prompt = '하나 둘 셋 넷 다섯 여섯 일곱 여덟 아홉 열 EP12 복선 정리';
  const lex = recallLexQuery(prompt, { collections: ['sample'] });
  assert.ok(!/EP\s?0?12/i.test(lex), `누출: ${lex}`);
});

test('recall 경로: lexicalPatterns:["ep"] 게이팅은 그대로 동작한다', () => {
  const prompt = '하나 둘 셋 넷 다섯 여섯 일곱 여덟 아홉 열 EP12 복선 정리';
  const lex = recallLexQueries(prompt, { collections: ['sample'], lexicalPatterns: ['ep'] });
  assert.ok(lex.includes('EP012'), `ep 게이팅이 켜지면 EP 변형이 실려야 함: ${JSON.stringify(lex)}`);
});

// --- EP 변형은 독립 lex search 여야 한다 (AND 결합으로 lex 가 죽는 버그) ---

test('recall 경로: ep-on 이면 EP 변형이 각각 독립 lex 엔트리로 들어간다', () => {
  // 한 문자열로 합치면 `"ep012"* AND "ep12"*` 가 되어 한 문서가 두 표기를 모두
  // 가져야 하고, 현실적으로 불가능하므로 EP 쿼리의 lex 는 항상 0건이 된다.
  const searches = recallSearches('EP12 에서 서미래가 먹은 약과 부작용', {
    collections: ['sample'], lexicalPatterns: ['ep'],
  });
  const lex = searches.filter(s => s.type === 'lex').map(s => s.query);

  assert.deepEqual(lex.slice(1), ['EP012', 'EP12'], 'EP 변형이 각각 별도 엔트리');
  for (const q of lex.slice(1)) {
    assert.ok(!/\s/.test(q), `EP 엔트리에 다른 term 이 합쳐졌다 (AND 결합): ${q}`);
  }
  // 일반 키워드는 여전히 하나의 lex 문자열이다 — AND 로 좁히는 것은 의도된 동작.
  // term 수만 GENERAL_LEX_TERM_CAP(3)으로 잘린다: `부작용`이 컷 뒤로 밀려 AND 조건이
  // 하나 느슨해진다(4-term AND 는 서사 코퍼스에서 0건이 되기 쉽다). EP 변형 엔트리는
  // 이 상한의 대상이 아니므로 위 slice(1) 단정이 그대로 유지된다.
  assert.equal(lex[0], '서미래 먹은 약과');
  assert.ok(!/EP/i.test(lex[0]), `EP 표기가 일반 문자열에 남으면 AND 가 다시 좁아진다: ${lex[0]}`);
  // vec 는 프롬프트 원문 그대로 1건 유지.
  assert.deepEqual(searches.filter(s => s.type === 'vec').map(s => s.query),
    ['EP12 에서 서미래가 먹은 약과 부작용']);
});

test('recall 경로: ep-off 면 payload 가 기존과 동일하다 (lex 1 + vec 1)', () => {
  const searches = recallSearches('EP12 에서 서미래가 먹은 약과 부작용', { collections: ['sample'] });
  assert.deepEqual(searches, [
    // lex term 3개 상한(GENERAL_LEX_TERM_CAP) — 엔트리 **수**는 예전 그대로 1개다.
    { type: 'lex', query: '서미래 먹은 약과' },
    { type: 'vec', query: 'EP12 에서 서미래가 먹은 약과 부작용' },
  ]);
});

test('recall 경로: ep-off 일반 프롬프트도 lex 엔트리는 정확히 1개', () => {
  const searches = recallSearches('docs/settings.md 구조를 설명해줘', { collections: ['sample'] });
  assert.equal(searches.filter(s => s.type === 'lex').length, 1);
  assert.equal(searches.filter(s => s.type === 'vec').length, 1);
});

test('EP 독립 search 는 예산(6) 상한을 넘지 않고 넘친 변형은 일반 문자열로 되돌지 않는다', () => {
  // 되돌리면 AND 조건이 다시 좁아져 고치려던 버그가 재발한다.
  const lex = recallLexQueries('EP1 EP2 EP3 EP4 정리', {
    collections: ['sample'], lexicalPatterns: ['ep'],
  });
  assert.equal(lex.length - 1, 6, `EP 독립 search 6개 상한 (실제 ${lex.length - 1})`);
  assert.equal(lex[0], '정리');
  assert.ok(!/EP/i.test(lex[0]), `예산 초과 EP 변형이 일반 문자열로 새면 안 됨: ${lex[0]}`);
  // 일반 lex 1 + EP 6 + vec 1 = 8 → qmd MCP `searches` 스키마 상한(10) 안.
  assert.ok(lex.length + 1 <= 10, 'searches 총 개수가 qmd 스키마 상한을 넘으면 안 됨');
});

test('EP 언급 원문 조각(`EP 12` → EP, 12)도 일반 lex 문자열에서 빠진다', () => {
  // 띄어 쓴 형태는 keywords 가 `EP`·`12` 를 별개 토큰으로 낸다. 독립 search 가 이미
  // 덮으므로 일반 문자열에 남기면 AND 조건만 좁아진다.
  const lex = recallLexQueries('EP 12 에서 무슨 일이 있었는지 정리해줘', {
    collections: ['sample'], lexicalPatterns: ['ep'],
  });
  assert.deepEqual(lex.slice(1), ['EP012', 'EP12']);
  assert.equal(lex[0], '무슨 일이 있었는지');

  // 같은 span 에서 나온 조각만 제외한다 — 무관한 숫자 토큰은 살아 있어야 한다.
  const r = kw('12개 항목 EP 12 확인', ['ep']);
  assert.equal(r.lexQueries[0], '12개 항목 확인');
});

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

test('recall 경로: 경로형 식별자가 plain term 으로 선두에 실린다 (phrase 없음)', () => {
  const lex = recallLexQuery('docs/settings.md 구조를 설명해줘', { collections: ['sample'] });
  assert.ok(!lex.includes('"'), `phrase 가 섞이면 AND 결합에서 lex 가 0건이 된다: ${lex}`);
  assert.ok(lex.startsWith('settings'), lex);   // 식별자가 일반 키워드보다 앞
  assert.match(lex, /\bdocs\b/);                // 디렉터리는 일반 키워드가 커버
});

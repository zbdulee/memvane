// `lexQueries` 동결 테스트 — 일반 lex 문자열에 들어갈 AND term 선택 규칙.
//
// 배경(라이브 service-engineering 실측): qmd는 한 lex 문자열 안의 positive term을
// **AND**로 결합한다. `KO_STOPWORDS`는 조사는 잡지만 활용형 어미(`뭐였지`·`발생해`)는
// 못 잡으므로, 질문형 프롬프트는 lex가 **항상 0건**이었다 — vec 운에 기대고 있었고
// lex 게이트가 그것을 드러냈다(관련 질의인데 본문이 stripped).
//
// 1차 대응은 **위치 컷**(GENERAL_LEX_TERM_CAP=3)이었다. 어미는 문장 끝에 오므로
// 앞에서 3개를 자르면 구조적으로 밀려난다 — 그러나 같은 이유로 **문장 앞의 군더더기가
// 실제 내용어를 밀어낸다**("이 부분 관련해서 sendbird 장애 원인" → `부분 관련해서
// sendbird` → 0건). 그래서 선택 기준이 위치에서 **코퍼스 존재(DF>0)**로 바뀌었고,
// 위치 컷은 그 뒤의 backstop 겸 조회 실패 시 폴백으로 남는다.
//
// 이 파일은 두 축을 못박는다:
//   (A) 데몬 없는 경로(`build_lexical_terms`) = 위치 컷 — 기존 동결값 그대로.
//   (B) 데몬 있는 경로(`select_general_terms`) = DF 필터 + 상한 + lone-survivor 폴백.
// EP 변형(`lexQueries[1:]`)은 어느 축의 대상도 아니다.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';

function py(snippet, payload) {
  const out = execFileSync('python3', ['-c', [
    'import json, sys',
    'sys.path.insert(0, ".")',
    'import memvane.recall.keywords as keywords',
    'payload = json.loads(sys.stdin.read())',
    snippet,
  ].join('\n')], { input: JSON.stringify(payload), encoding: 'utf8' });
  return JSON.parse(out);
}

function build(prompt, patterns = []) {
  return py(
    'print(json.dumps(keywords.build_lexical_terms(payload["prompt"], payload["patterns"]), ensure_ascii=False))',
    { prompt, patterns },
  );
}

// 데몬이 있는 경로 재현: build_lexical_terms 가 낸 generalTerms 를 DF 집합으로 좁힌다.
// recall.narrow_general_lex 가 부르는 것과 **같은 함수**다(선택 규칙 SSOT).
function narrow(prompt, presentTerms) {
  return py([
    'b = keywords.build_lexical_terms(payload["prompt"], [])',
    'probed = b["generalTerms"][:6]  # recall.DF_PROBE_MAX_TERMS',
    'terms, mode = keywords.select_general_terms_explained(probed, set(payload["present"]))',
    'print(json.dumps({"query": " ".join(terms), "mode": mode, "probed": probed}, ensure_ascii=False))',
  ].join('\n'), { prompt, present: presentTerms.map((t) => t.toLowerCase()) });
}

// ── 라이브 실측 (service-engineering wiki 871장, 2026-08-05) ───────────────────
//
// `present`는 term 당 lex 단독 질의(limit 1)로 잰 DF>0 집합이다. `hits`는 그 질의
// 문자열이 limit 8 에서 실제로 낸 히트 수 — 게이트가 보는 값이다.
//
// **브리핑 표와 두 행이 다르다**(재측정으로 확인, 원인 기록):
//   - `관련해서`는 DF 0 이 아니다(871장 중 1장). DF 필터로는 빠지지 않아 1행이
//     회복되지 않았다 → 이미 `관해서`·`대해서`가 있던 연결어미 범주에 이 토큰을
//     더해 해결했다(KO_STOPWORDS). 그래서 1행은 **위치 컷 값 자체가** 바뀐다.
//   - `오늘`도 DF 0 이 아니다(1장). DF 필터가 이 토큰 하나만 남겨 무관 프롬프트에
//     히트 1건을 만들었다 → lone-survivor 폴백이 그것을 막는다.
const CASES = [
  // ── 구어체 군더더기가 앞에 붙은 관련 질의 (이번에 고쳐지는 2건) ──
  {
    prompt: '이 부분 관련해서 sendbird 장애 원인 알려줘',
    // `관련해서`가 stopword가 되어 위치 컷 값 자체가 달라졌다(0건 → 3건).
    positional: '부분 sendbird 장애', positionalHits: 3,
    present: ['부분', 'sendbird', '장애', '원인'],
    narrowed: '부분 sendbird 장애', mode: 'present', hits: 3,
    was: '부분 관련해서 sendbird', wasHits: 0,
  },
  {
    prompt: '아까 말한 그거 있잖아 VN 콜백 이벤트',
    positional: '아까 말한 있잖아', positionalHits: 0,
    // 아까/말한/있잖아 = DF 0. `이벤트`는 키워드 5개 상한 밖이라 애초에 없다.
    present: ['VN', '콜백'],
    narrowed: 'VN 콜백', mode: 'present', hits: 8,
  },
  // ── 직접 질의: 어느 축에서도 값이 바뀌면 안 된다 ──
  {
    prompt: 'sendbird 장애 원인이 뭐였지?',
    positional: 'sendbird 장애 원인', positionalHits: 8,
    present: ['sendbird', '장애', '원인'],  // 뭐였지 = DF 0
    narrowed: 'sendbird 장애 원인', mode: 'present', hits: 8,
  },
  {
    prompt: 'VN 콜백 이벤트 언제 발생해?',
    positional: 'VN 콜백 이벤트', positionalHits: 7,
    present: ['VN', '콜백', '이벤트', '발생해'],
    narrowed: 'VN 콜백 이벤트', mode: 'present', hits: 7,
  },
  {
    prompt: '중복 판정은 어떻게 하나요?',
    positional: '중복 판정', positionalHits: 8,
    present: ['중복', '판정'],
    narrowed: '중복 판정', mode: 'present', hits: 8,
  },
  // ── 무관 질의: DF 필터가 게이트를 열면 안 된다 ──
  {
    prompt: 'python list comprehension 문법 알려줘',
    positional: 'python list comprehension', positionalHits: 0,
    present: ['python', 'list', 'comprehension'],  // 문법 = DF 0
    narrowed: 'python list comprehension', mode: 'present', hits: 0,
  },
  {
    prompt: '리액트 useEffect 의존성 배열 규칙 설명',
    // 식별자(useEffect)가 일반 키워드보다 앞에 온다 — IDENTIFIER_BUDGET 경로.
    positional: 'useEffect 리액트 의존성', positionalHits: 0,
    present: ['의존성', '배열', '규칙'],  // useEffect·리액트 = DF 0
    narrowed: '의존성 배열 규칙', mode: 'present', hits: 0,
  },
  {
    prompt: '오늘 점심 뭐 먹을까 고민이네',
    positional: '오늘 점심 먹을까', positionalHits: 0,
    // 점심·먹을까·고민이네 = DF 0 → `오늘` 하나만 남는다. 그 1-term 질의는 1건을
    // 내므로(871장 중 1장) 채택하면 **무관 프롬프트에 게이트가 열린다**.
    present: ['오늘'],
    narrowed: '오늘 점심 먹을까', mode: 'lone_survivor', hits: 0,
  },
  {
    prompt: '이 함수 이름을 뭐로 지을까 고민',
    positional: '함수 이름 뭐로', positionalHits: 0,
    present: ['함수', '이름', '고민'],  // 뭐로·지을까 = DF 0
    narrowed: '함수 이름 고민', mode: 'present', hits: 0,
  },
  // ── 3토큰 이하 + 전부 존재: 어느 규칙도 아무 일을 하지 않는다 ──
  {
    prompt: '설정 파일 위치 알려줘',
    positional: '설정 파일 위치', positionalHits: null,
    present: ['설정', '파일', '위치'],
    narrowed: '설정 파일 위치', mode: 'present', hits: null,
  },
];

test('(A) 데몬 없는 경로: lexQueries[0] 은 위치 컷 (동결)', () => {
  for (const c of CASES) {
    const r = build(c.prompt);
    assert.equal(r.lexQueries[0], c.positional,
      `${c.prompt}${c.was ? `\n  (stopword 추가 전: ${JSON.stringify(c.was)})` : ''}`);
    assert.equal(r.lexQueries.length, 1, 'ep 미활성이면 lex 엔트리는 1개');
  }
});

test('(B) 데몬 있는 경로: DF>0 term 으로 좁히고 상한을 적용한다 (동결)', () => {
  for (const c of CASES) {
    const r = narrow(c.prompt, c.present);
    assert.equal(r.query, c.narrowed, `${c.prompt}\n  (위치 컷: ${JSON.stringify(c.positional)})`);
    assert.equal(r.mode, c.mode, c.prompt);
  }
});

test('DF 필터는 문장 앞 군더더기에 밀려난 내용어를 되살린다', () => {
  // 이 파일의 존재 이유. 위치 컷은 등장 순서를 보므로 구어체 도입부가 그대로 AND 에
  // 실렸다 — 같은 질문을 구어체로 물었다는 이유만으로 카드 본문을 잃었다(838자 → 134자).
  const r = narrow('아까 말한 그거 있잖아 VN 콜백 이벤트', ['VN', '콜백']);
  assert.equal(r.query, 'VN 콜백');
  assert.deepEqual(r.probed, ['아까', '말한', '있잖아', 'VN', '콜백'],
    '조회 대상은 상한 전 일반 term 목록 그대로');
});

test('DF 필터가 연접을 1개로 줄이면 채택하지 않는다 (무관 프롬프트 게이트 유지)', () => {
  // `오늘`은 코퍼스 871장 중 1장에 있다. 채택하면 "오늘 점심 뭐 먹을까"가 lex 1건을
  // 내고 게이트가 열려 무관 카드 본문이 다시 주입된다 — 86c8d81 이 고친 회귀다.
  const r = narrow('오늘 점심 뭐 먹을까 고민이네', ['오늘']);
  assert.equal(r.mode, 'lone_survivor');
  assert.equal(r.query, '오늘 점심 먹을까', '원래(불만족) 질의를 그대로 유지 = 오늘의 동작');
  // 반대로 2개가 남으면 채택한다 — 경계값이 규칙의 전부다.
  assert.equal(narrow('오늘 점심 뭐 먹을까 고민이네', ['오늘', '점심']).mode, 'present');
});

test('DF 를 모르면(조회 실패·CLI) 위치 컷으로 폴백한다', () => {
  const r = py([
    'terms = ["sendbird", "장애", "원인", "뭐였지"]',
    'q, mode = keywords.select_general_terms_explained(terms, None)',
    'print(json.dumps({"query": " ".join(q), "mode": mode}, ensure_ascii=False))',
  ].join('\n'), {});
  assert.equal(r.query, 'sendbird 장애 원인');
  assert.equal(r.mode, 'positional');
});

test('상한은 DF 필터 뒤에도 살아 있다 (긴 AND 방지 + 폴백 안전망)', () => {
  // DF>0 토큰이 많아도 AND 를 4개 이상으로 늘리지 않는다. 개별 토큰이 존재해도 한
  // 문서가 전부 갖고 있을 확률은 term 수에 따라 급감한다.
  const r = py([
    'terms = ["a", "b", "c", "d", "e"]',
    'q, mode = keywords.select_general_terms_explained(terms, {"a","b","c","d","e"})',
    'print(json.dumps({"n": len(q), "mode": mode, "cap": keywords.GENERAL_LEX_TERM_CAP}))',
  ].join('\n'), {});
  assert.equal(r.cap, 3, 'GENERAL_LEX_TERM_CAP 불변');
  assert.equal(r.n, 3);
  assert.equal(r.mode, 'present');
});

test('상한은 term 수만 자르고 AND 결합·순서·stemming 을 바꾸지 않는다', () => {
  // AND 를 OR 로 바꾸는 것이 아니다 — CLAUDE.md "일반 키워드의 AND 결합은 유지한다".
  const r = build('sendbird 장애 원인이 뭐였지?');
  assert.deepEqual(r.lexQueries[0].split(' '), ['sendbird', '장애', '원인'], '앞에서부터 3개, 순서 보존');
  // lexicalTerms 계약은 그대로다(상한·DF 필터는 lex **문자열**에만 걸린다).
  assert.deepEqual(r.lexicalTerms, ['sendbird', '장애', '원인', '뭐였지']);
  assert.deepEqual(r.keywords, ['sendbird', '장애', '원인', '뭐였지']);
  // generalTerms 는 상한 **전** 목록이다(recall 의 DF 조회 입력).
  assert.deepEqual(r.generalTerms, ['sendbird', '장애', '원인', '뭐였지']);
});

test('EP 경로는 어느 선택 규칙의 대상도 아니다 (변형 emit·예산 무변화)', () => {
  const r = build('EP12 와 EP7 에서 주인공 감정선 변화 정리해줘', ['ep']);
  // 일반 문자열에는 EP 변형·조각이 빠지고 나머지가 상한까지만 들어간다.
  assert.ok(!/EP/i.test(r.lexQueries[0]), `일반 lex 에 EP 가 남으면 AND 가 좁아진다: ${r.lexQueries[0]}`);
  assert.ok(r.lexQueries[0].split(' ').filter(Boolean).length <= 3);
  assert.ok(!r.generalTerms.some((t) => /^EP/i.test(t)), 'generalTerms 에도 EP 변형이 없다');
  // EP 변형은 독립 엔트리로 그대로 나간다(개수·순서 불변).
  assert.deepEqual(r.lexQueries.slice(1), ['EP012', 'EP12', 'EP007', 'EP7']);
  assert.deepEqual(r.epTerms, ['EP012', 'EP12', 'EP007', 'EP7']);
});

test('EP 예산은 그대로 EP_SEARCH_BUDGET 로 유계다 (payload 상한 10 유지)', () => {
  const r = build('EP1 EP2 EP3 EP4 EP5 화 정리', ['ep']);
  const out = execFileSync('python3', ['-c', [
    'import sys; sys.path.insert(0, ".")',
    'import memvane.recall.keywords as keywords; print(keywords.EP_SEARCH_BUDGET)',
  ].join('\n')], { encoding: 'utf8' }).trim();
  assert.equal(Number(out), 6, 'EP_SEARCH_BUDGET 상수 불변');
  assert.ok(r.lexQueries.length - 1 <= 6, 'EP 변형 엔트리 수는 예산 안');
  // 일반 lex 1 + EP 변형 ≤6 + vec 1 = 최대 8 → qmd searches 스키마 .max(10) 안.
  assert.ok(r.lexQueries.length + 1 <= 10, 'searches payload 총 엔트리 ≤ 10');
});

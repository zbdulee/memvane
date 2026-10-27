// 유료 host CLI 호출 상한의 **문서 셈이 코드와 일치하는가**.
//
// 문서의 상한(`docs/settings.md` Batch 절)은 사용자가 설정을 올릴지 판단하는 근거이고, 이
// 저장소에서 과금 관련 셈이 틀린 것은 관측 가능한 결함이다. 실제로 verify 경로의 backlog
// 기아 방지 예약(`+1`)이 셈에서 빠져 문서의 값이 **1 작았다**(140 vs 실제 141, clamp 2581 vs
// 2582). 그 예약은 의도된 동작이므로 코드가 아니라 문서를 고치는 것이 정답이고, 다음에 또
// 갈리지 않도록 여기서 코드 상수로부터 다시 계산해 대조한다.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

const DOC = readFileSync('docs/settings.md', 'utf8');

function pyJson(expr) {
  const out = execFileSync('python3', ['-c', `import sys, json
sys.path.insert(0, ${JSON.stringify(resolve('core'))})
import config as c
print(json.dumps(${expr}))
`], { cwd: process.cwd(), encoding: 'utf8' });
  return JSON.parse(out.trim());
}

// 한 run의 유료 호출 총량. 세 항이 각각 유계라는 문서의 주장을 그대로 계산한다.
// verify 항의 `+1`은 `wiki_verify_worker.run`의 backlog 예약(leftover == 0 && backlog → 1).
function worstCase({ batchMaxPerRun, cardsPerSource, verifyMaxPerRun, producedCap }) {
  const cards = batchMaxPerRun * cardsPerSource;
  const verifyBudget = Math.max(verifyMaxPerRun, Math.min(cards, producedCap));
  return batchMaxPerRun + cards + verifyBudget + 1;
}

test('문서의 기본값 최악 유료 호출 수가 코드 상수와 일치한다', () => {
  // 유료 호출 상한은 전부 `compile.budget.*`가 SSOT다(예전 batch.maxPerRun /
  // maxCardsPerSource / verify.maxPerRun). 클램프 상수 이름은 그대로다.
  const k = pyJson(`{
    "batchMaxPerRun": c.DEFAULT_CONFIG["compile"]["budget"]["extractorPerRun"],
    "cardsPerSource": c.DEFAULT_CONFIG["compile"]["budget"]["cardsPerSource"],
    "verifyMaxPerRun": c.DEFAULT_CONFIG["compile"]["budget"]["verifyPerRun"],
    "producedCap": c.VERIFY_PRODUCED_HARD_CAP,
    "maxCards": c.MAX_CARDS_PER_SOURCE,
    "maxBatch": c.MAX_COMPILE_PER_RUN,
  }`);
  const dflt = worstCase(k);
  assert.equal(dflt, 141, '기본값(10 × 10)의 최악 = 10 + 100 + 31');
  assert.ok(DOC.includes(`= ${dflt}회`), `문서에 기본값 최악 ${dflt}회가 없다`);

  const clamped = worstCase({ ...k, batchMaxPerRun: k.maxBatch, cardsPerSource: k.maxCards });
  // 예전 문서의 두 값은 서로 다른 모델로 계산돼 있었다: `140`은 예약 `+1` 을 빼고,
  // `2,581`은 (다른 항에서 어긋난 채) 우연히 예약을 포함한 값과 같았다. 지금은 세 값이
  // 모두 같은 식에서 나온다.
  assert.equal(clamped, 2581, 'clamp 상한(50 × 50)의 최악 = 50 + 2500 + 31');
  assert.ok(DOC.includes('50 + 2,500 + 31 = 2,581회'), `문서에 clamp 최악 ${clamped}회가 없다`);

  const measured = worstCase({ ...k, cardsPerSource: k.maxCards });
  assert.equal(measured, 541, '실측 조합(10 × 50) = 10 + 500 + 31');
  assert.ok(DOC.includes('10 + 500 + 31 = 541회'), `문서에 실측 조합 ${measured}회가 없다`);
});

// 라이브 두 프로젝트의 최악값도 같은 식에서 나오는지 — 문서가 인용하는 숫자다.
test('문서가 인용한 라이브 최악값이 같은 식에서 나온다', () => {
  const k = pyJson('{"producedCap": c.VERIFY_PRODUCED_HARD_CAP, "maxVerify": c.MAX_VERIFY_PER_RUN}');
  // service-engineering: batch 10 × cards 10, verify.maxPerRun 15
  assert.equal(worstCase({
    batchMaxPerRun: 10, cardsPerSource: 10, verifyMaxPerRun: 15, producedCap: k.producedCap,
  }), 141);
  // 귀신은 약효가 돌 때 보인다: verify.maxPerRun 60 → MAX_VERIFY_PER_RUN(50)으로 클램프
  assert.equal(worstCase({
    batchMaxPerRun: 10, cardsPerSource: 10,
    verifyMaxPerRun: Math.min(60, k.maxVerify), producedCap: k.producedCap,
  }), 161);
  assert.ok(DOC.includes('= ` **141**'), '문서에 141 셈이 있다');
  assert.ok(DOC.includes('= ` **161**'), '문서에 161 셈이 있다');
});

test('문서가 verify 항의 +1 예약을 셈에 명시한다', () => {
  assert.match(DOC, /`\+1`은 backlog 기아 방지 예약/,
    '예약이 셈에 포함된다는 사실이 문서에 없으면 다음 리뷰가 같은 1 차이를 다시 만든다');
  assert.match(DOC, /예산 \+ 1|`예산 \+ 1`/, 'verify 상한이 예산+1임을 명시');
});

test('문서가 blocking 훅 예산을 코드 기본값(5초)으로 기술한다', () => {
  const timeout = pyJson('c.DEFAULT_CONFIG["queryTimeout"]');
  assert.equal(timeout, 5, '코드 기본값이 5초');
  assert.match(DOC, /기본값은 `5`초입니다/, '문서가 5초를 명시');
  // "3초 예산"은 이 저장소 어디에도 근거가 없는 수치였다 — 문서에 기준으로 남으면 안 된다.
  assert.ok(!/queryTimeout 3초|3초 예산/.test(DOC.replace(/\*\*"queryTimeout 3초 예산"[^*]*\*\*/g, '')),
    '근거 없는 "3초 예산"이 기준으로 기술돼 있다');
});

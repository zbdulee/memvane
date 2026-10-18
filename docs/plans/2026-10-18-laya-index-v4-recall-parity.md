# Laya Index v4 recall parity checkpoint

**2026-10-18 모델 없는 청킹 후보:** 새 `laya_qmd_chunking.mjs`는 고정 fork의 regex 경로를 이식했고 worker/provider는 명시적 `document_chunks` recipe를 추가했다. tokenizer는 실행 시 승인된 모델에서 주입되어야 하므로 합성 tokenizer 결과만 검증했다. AST 경로와 원문이 달라지는 detokenize fallback은 거부한다. 동일 본문 hash라도 파일명에 따라 QMD title이 달라질 수 있어 현재 공유 벡터 키와 충돌하며, off-hook batch 준비가 모델 호출 전에 차단한다. 제품 활성화 전에 schema/recipe 결정과 실제 동일 tokenizer·embedding 출력 비교가 필요하다. 가드 집중 검사 `d2fedc8d5b1448f18808e15713dd2007`은 통과했고, 아래 1,601 PASS 전체 회귀는 이 추가 전 기록이다.

Status: synthetic implementation under review; no product selection. One bounded
embedding functional smoke ran on synthetic text; retrieval parity and product
worker performance remain unverified.

## Current QMD behavior to preserve or measure

The selected QMD hook in `core/recall.py` sends independent lexical searches and
one vector search to `/query`. `keywords.build_lexical_terms` builds a bounded
general AND query from identifiers and keywords; when `lexicalPatterns` enables
`ep`, EP012 and EP12 variants are separate lexical searches. QMD combines those
lists using reciprocal rank fusion. The hook explicitly sets `rerank:false`, so
QMD's model reranker is not part of this default recall path. Before injection,
the hook also uses collection roles and strategy, corpus document-frequency
probes, EP exact promotion, `minScore`, `topN`, skip paths, wiki/raw backfill,
lexical hit gating, and source verification. Those policies are not a single
score threshold that can be copied to cosine distance.

## Implemented v4 candidate boundary

`core/laya_host_response.py` has a pure same-pin hybrid candidate function.
It requires a caller-proved provider whose profile and artifact identity match
the route and the vector seal. Under one A-int pin it obtains source-verified
FTS lists for the general query and independent EP variants, plus a source-
verified exact cosine list. It fuses paths deterministically with RRF
`2/(60+rank)` for the general lexical list and `1/(60+rank)` for each EP and
vector list, then sorts ties by UTF-8 path bytes. The number of candidates per
list is bounded at 20; the final context is bounded at five results. Missing or
mismatched provider, stale source, generation/profile mismatch, query failure,
or candidate inconsistency fails the entire request. It never falls back to
lexical-only selected recall or QMD.

This is a **candidate policy**, not QMD equivalence. A-int FTS tokenization,
QMD's internal RRF details, document-frequency narrowing, EP filename
promotion, minScore/topN interpretation, wiki/raw hierarchy, lexical hit
gating, and source annotation still differ or lack a reviewed mapping. Exact
identifier and EP variant synthetic tests exercise the local fusion policy;
they do not establish retrieval quality on a representative corpus.

The product selected hook remains blocked. The installer production provider
registry is empty. Exact model/runtime bytes were hashed and one bounded
functional smoke returned finite 768-dimensional vectors, but full chunking
parity, peak memory, product worker latency, and installed provider approval
remain unverified.
The development-only approval path is excluded from hook dispatch.

## Release evidence gates

1. Review and pin the provider executable, model bytes, runtime dependency
   closure, recipe, dimension, and document/query embedding identity in the
   installer proof. Measure cold and warm worker latency and the bounded hook
   client deadline separately; a hook must not cold-load the model. Do not infer approval from route JSON, environment, PATH, or a
   development-only manifest.
2. Compare QMD and Laya on the same disposable source corpus with exact
   identifiers, EP variants, Korean/English morphology, lexical-only matches,
   semantic-only matches, exclusions, source edits, and generation swaps.
   Record hit sets and rank changes rather than claiming parity from an RRF
   formula alone.
3. Decide and test the supported settings matrix. Either implement or reject
   QMD's wiki/raw strategies, DF probes, EP promotion, minScore/topN, and
   lexical gate before activating a route that relies on them.
4. Run the focused host/vector/sync tests and the model-excluded full regression
   under the repository guard after all v4 edits. Keep actual model tests behind
   an explicit opt-in gate and report them separately.

No result in this document is a power-loss, marketplace fresh-host, operating
DB, or retrieval-quality validation.
## 2026-10-18 제품 provider 메타데이터와 청킹 검증 보강

선택된 플러그인 QMD 2.5.3 설정의 embed ID는 `hf:ggml-org/embeddinggemma-300M-GGUF/embeddinggemma-300M-Q8_0.gguf`이며, 전용 cache에 같은 모델 이름의 328,576,992바이트 파일이 있다. 설치된 `node-llama-cpp`는 3.18.1이고 타입 선언에는 `build:"never"`와 `skipDownload`가 있다. 이것은 파일·API 선언의 확인이지 실제 provider 승인이나 추론/지연 증거가 아니다. 일반 QMD와 플러그인 전용 cache를 혼동하지 않는다.

고정 fork의 `chunkDocumentByTokensWithLlm`은 단순히 900개 토큰씩 잘라 135개를 겹치는 함수가 아니다. 먼저 문자 공간에서 break point와 코드 fence를 고려해 후보를 만들고, 모델 tokenizer로 900토큰을 넘는 후보를 재귀 분할한다. 병적인 마지막 fallback은 token slice를 detokenize할 수 있다. 단순 `[0,900), [765,1665)` 합성 시험만으로 QMD 청킹 동등성을 주장하지 않는다. 실제 모델을 쓰는 별도 게이트에서 동일한 source·tokenizer·chunk strategy와 위치/본문을 대조하고, 잘림·서로게이트 경계의 허용 정책도 결정해야 한다. 현재 full-document bridge의 recipe는 별개이며 제품 route에 승인하지 않는다.

hook의 22초 host timeout은 기존 v2의 전체 18초 기본·20초 최대 deadline과 종료 여유 2초를 위한 값이다. QMD 질의 기본 `queryTimeout=5`와 다른 층이며 새 provider의 cold load를 기다리는 예산이 아니다. 선택된 Laya route에서는 벡터 준비 중이거나 지문이 어긋나면 stale/blocked 상태를 내고, 모델 작업은 소유된 비동기 worker로 옮기는 설계가 필요하다. lexical-only 조용한 결과는 현재 제품 계약을 충족하지 않는다.

승인된 합성 문서·질의 각 1회에 한정한 embedding 기능 smoke는 guard `af90592d823d482ba8318de9016a010b`에서 통과했다. 설치된 QMD와 같은 Metal native backend 선택에 `gpuLayers:0`을 적용했고, 두 벡터는 유한한 768차원이었다. 이 실행은 요청 한 번의 cold load·문서·질의 시간을 기록했지만 5초 표본 사이의 실제 peak RSS를 포착하지 못했다. 실제 QMD tokenizer/chunk 출력과의 대조, 대표 corpus의 검색 품질, 설치 후보의 지속 worker 자원·지연 검증은 여전히 남는다. 추가 모델 호출로 RSS 빈칸을 임의로 메우지 않는다.

QMD 원본 제목은 문서 embedding과 검색 inventory에 영향을 준다. 이관
digest에 제목을 포함해 title-only 변경을 stale로 만들었고, 서로 다른
제목이 동일 본문 hash를 공유하는 벡터 후보는 사전 거부한다. 모델 없는
QMD/전환 집중 가드 `6dc4cd9fcbe9440cb18b0513ce37dc37`은 Node
10/10 통과했다. 최종 모델 제외 전체 회귀는 1,617건 중 1,608 PASS·
9 SKIP·0 FAIL(`608c3a4808a94c1294cb407de09e5c5d`,
`passed/settled:true`)였다. 이 수치는 실제 모델 parity·설치 후보 L1
검증을 대신하지 않는다.

# MemVane 로드맵


열린 항목의 단일 목록이다. 끝난 항목은 지우고 「출시됨」 표에 한 줄만 남긴다.

## 읽는 법

- **우선순위.** 대형 프로젝트의 recall 정확성 → 업데이트 신뢰성 → 학습 → 정리 순이다.
- **릴리스 번호.** `1.0.x` 패치는 [1.x 불변 목록](dev/contexts/release.md)과 사용자 승인 흐름을 바꾸지 않는 변경이다. 기본값과 동작의 변경을 포함한다. `1.1`은 설계 문서와 독립 리뷰가 먼저 필요한 변경을 담는다. 둘 다 불변 목록을 바꾸지 않는다.
- **불변.** `없음`은 영속 기록·lock·wake kind·settings 키를 건드리지 않는다. `확인`은 설계 단계에서 저촉 여부를 먼저 판정한다. `저촉`은 2.0 항목이다.
- **임베딩 규칙.** 정체성(모델 GGUF·tokenizer·chunker·차원·pooling·정규화·최대 토큰 길이)이 바뀌면 재빌드한다. 실행 방식만 바뀌면 sync owner가 `provider-reattest.json`으로 재봉인한다.
- **버전 혼재.** 한 기계에서 Claude·Codex·Hermes가 서로 다른 1.x 버전을 로드하는 상태가 정상이다. writer·worker 동작을 바꾸는 항목은 낮은 버전 호스트가 같은 store와 기록에 쓰는 경우를 설계에 넣는다.
- **크기.** S = 1~2일, M = 3~5일, L = 1주 이상이거나 설계 문서와 독립 리뷰가 먼저 필요하다.
- **합격.** 항목마다 live에서 재는 기준을 적는다. 출처는 설치 이벤트 로그(`memvane:logs`), store 읽기 전용 질의, 프로세스 목록이다. 기준 프로젝트는 카나리(qmd-auto-context)와 service-engineering(카드 3,751, vector 18,412)이다.
- **hook 지연 목표.** recall p50 1.0초·p95 1.5초, 대형 프로젝트(카드 1,000장 초과)의 recall p95 2.5초다. semantic 단계를 포함한 값이다.
- **검증 방식.** 변경 단위 focused 시험, PR CI, 출시 뒤 live 합격 기준 확인이다. 긴 전체 검사는 릴리스 게이트가 아니다.
- **모양 제약.** 새 lock·journal record·wake kind·hook action·settings 키·compile 큐 dedup 키는 사용자 결정 사항이다.

## 출시됨

| 버전 | 내용 |
|---|---|
| 1.0.1 | 학습 runtime 활성화 명령, resident 재시도 예산, 원문 불일치 카드의 trust 해제, view 행 GC, Linux CI, hook import 지연 |
| 1.0.2 | 첫 lexical 세대에서 초기 빌드 ready, `progress` outcome, build-status 진행 표시, 호스트 업데이트 백그라운드 인계 |
| 1.0.3 | 임베딩 worker Metal GPU, 재빌드 없는 재봉인, 경량 학습 runtime 활성화, 옛 번들 선점 |
| 1.0.4 | 모든 provider 경로의 재봉인 인정, provider 부재 시 lexical recall |
| 1.0.5 | provider API 인자 불일치 수정 |
| 1.0.6 | vector fill 처리량(cursor 유지, ingest pass 예산, capture·plan 재사용, fill batch), 단계별 시간 이벤트 |
| 1.0.7 | 학습 기본 켜짐(`local-auto-apply`, 기존 settings 키로 끈다) |

## 한눈에

| 릴리스 | 항목 | 크기 | 불변 |
|---|---|---|---|
| 1.0.8 | 1 vector 세대 단일 게시 | M | 없음 |
| 1.0.8 | 2 hook 지연 목표와 recall 단계 계측 | S | 없음 |
| 1.0.8 | 3 대형 프로젝트 semantic 예산 1차 | M | 없음 |
| 1.0.8 | 4 재봉인 기록의 버전 혼재 안전 | M | 확인 |
| 1.0.8 | 5 임베딩 정체성 manifest 보강 | S | 확인 |
| 1.0.8 | 6 업데이트 인계 마무리 | M | 없음 |
| 1.0.8 | 7 릴리스 게이트: provider smoke와 live 확인 목록 | S | 없음 |
| 1.0.8 | 8 pending 증명과 selector 유지보수 분리 | S | 없음 |
| 1.0.x | 1 연결 worktree view 대기의 실패 기록 | S | 없음 |
| 1.0.x | 2 resident 선행 요청 | M | 없음 |
| 1.0.x | 3 ingest delta화와 원문 지문 누적 정지 | M | 확인 |
| 1.0.x | 4 학습과 색인의 경합 계측 | S | 없음 |
| 1.0.x | 5 sync owner 점유 계측 | S | 없음 |
| 1.0.x | 6 재봉인·온보딩 안내 정리 | S | 없음 |
| 1.0.x | 7 GPU 폴백 후속 | S | 없음 |
| 1.0.x | 8 운영 가시성 | M | 없음 |
| 1.0.x | 9 시험 기반 | M | 없음 |
| 1.0.x | 10 문서와 릴리스 절차 정합 | S | 없음 |
| 1.0.x | 11 방어 심화, 세션당 고정비 | S | 없음 |
| 1.0.x | 12 검증 job 없는 교체 pending 정리 | S | 확인 |
| 1.0.x | 13 setup gitignore 안내 보강 | S | 없음 |
| 1.0.x | 14 머신 간 중복 카드 정리 | M | 확인 |
| 1.1 | 1 대형 프로젝트 semantic 검색 구조 | L | 확인 |
| 1.1 | 2 worker 안 batch 임베딩 | M | 확인 |
| 1.1 | 3 admission 분리와 busy 사유 | M | 없음 |
| 1.1 | 4 공유 content GC | L | 없음 |
| 1.1 | 5 진단과 deferral 정확성 | M | 없음 |
| 1.1 | 6 recall·wiki 품질 | M | 확인 |
| 1.1 | 7 설치 수명 주기 마무리 | M | 확인 |
| 1.1 | 8 시간 예산 | L | 확인 |
| 롤아웃 | G7 나머지 8개 프로젝트 | — | — |
| 롤아웃 | G8 mini | — | — |

```
1.0.7 관찰 24시간 ── G7 ── 1.0.x-4 ── 1.1-3
1.0.8  2 ── 3 ── 1.1-1
       4 · 5 ── 1.1-2 (worker 바이트를 바꾸는 모든 릴리스)
       6 ── G8
       7 (이후 모든 릴리스의 게이트)
1.0.x  3 ── 1.1-4
       5 ── 1.1-8
```

## 1.0.8

### 1.0.8-1 vector 세대 단일 게시

- **현재 동작.** 카드가 바뀐 pass는 lexical 세대를 먼저 게시하고 fill 뒤 vector 세대를 따로 게시한다. 그 사이 20~25초 동안 head가 lexical이라 recall이 `selected_view_vectors_pending`으로 semantic 없이 답한다. vector pass가 미뤄지면 다음 pass까지 남는다.
- **목표.** vector 세대가 봉인된 view에서는 카드 변경이 head를 lexical로 내리지 않는다.
- **범위.** head가 설치 spec으로 봉인돼 있으면 바뀐 카드의 embedding set을 게시 전에 채우고 vector 세대 하나만 게시한다. fill이 예산·provider 사유로 끝나지 못할 때만 lexical 우선 게시로 내려가고, 그 분기를 시험으로 고정한다. 초기 빌드의 lexical-ready는 그대로다.
- **의존.** 없음. **크기.** M. **불변.** 없음(DDL·journal record를 유지한다).
- **합격(live).** service-engineering에서 카드가 바뀐 pass 10회 연속으로 `s_generations`에 lexical 세대가 새로 쌓이지 않는다(provider 정상일 때). 카드 변경 뒤 60초 안에 들어온 recall의 `selected_view_vectors_pending` note가 24시간 동안 0건이다. 새 카드가 lexical로 보이기까지의 지연이 카드당 1초 이하다.

### 1.0.8-2 hook 지연 목표와 recall 단계 계측

- **목표.** recall 한 건의 시간이 단계별로 이벤트에 남고, 목표 대비 현재 값이 조회 한 번으로 나온다.
- **범위.** recall 이벤트에 단계별 소요(route 증명, lexical, query 임베딩, semantic, selector, 렌더)를 ms로 싣는다. `memvane:logs` 요약이 hook action별 p50·p95와 semantic 사용 비율을 「읽는 법」의 목표와 나란히 낸다.
- **의존.** 없음. 1.0.8-3·1.1-1의 측정 기반이다.
- **크기.** S. **불변.** 없음(이벤트 필드 추가는 원문·경로를 싣지 않는다).
- **합격(live).** 카나리와 service-engineering의 최근 recall 100건에 대해 logs 요약 한 번으로 p50·p95·semantic 비율·단계별 중앙값이 나온다.

### 1.0.8-3 대형 프로젝트 semantic 예산 1차

- **현재 동작.** service-engineering의 recall은 semantic 단계가 작업 예산을 넘겨 `deferred_work_budget`으로 끝난다. vector 색인은 완성돼 있지만 recall은 lexical 결과만 쓴다. recall은 2.6~3.1초다.
- **목표.** vector 18,000개 규모에서 recall의 semantic 단계가 hook 예산 안에 끝난다.
- **범위.** 1.0.8-2의 계측으로 exact 검색의 실제 비용(행 읽기, 언패킹, 내적)을 나눈다. 영속 형식을 바꾸지 않는 범위에서 줄인다: 작업 예산 단위의 재조정, vector 읽기·내적의 묶음 처리, lexical 후보에 먼저 semantic 점수를 주는 순서. 이 범위로 목표에 닿지 않으면 남은 차이를 1.1-1의 입력으로 적는다.
- **의존.** 1.0.8-2. **크기.** M. **불변.** 없음.
- **합격(live).** service-engineering의 recall 100건 중 `deferred_work_budget`이 5건 이하다. recall p95가 1.0.8-2의 대형 프로젝트 목표 이하다. 카나리의 recall p50이 늘지 않는다.

### 1.0.8-4 재봉인 기록의 버전 혼재 안전

- **현재 동작.** `provider-reattest.json`은 `attested → current` 한 쌍만 담는다. worker 바이트가 다시 바뀌는 릴리스에서 두 호스트가 다른 버전이면 서로 기록을 덮어쓰고, 진 쪽 호스트는 "다시 온보딩" 안내를 내며 recall 외 hook을 막는다. 기록을 쓸 수 없는 상태가 이어지면 recall·편집 hook마다 wake offer가 나가 worker가 매번 뜬다.
- **목표.** 서로 다른 1.x 버전의 두 호스트가 같은 설치를 번갈아 써도 양쪽 재봉인이 함께 유효하다.
- **범위.** 재봉인 기록을 current 쌍별로 둔다(상한 있음). 1.0.3~1.0.7 호스트가 읽는 v1 파일은 그 모양 그대로 유지한다. 재봉인이 연속으로 실패하면 offer를 멈추는 표시를 둔다. `provider-reattest.json`(`memvane-provider-reattest-v1`)의 모양을 불변 목록에 올리고, 릴리스마다 직전 worker 쌍을 legacy 표에 넣는 절차를 릴리스 문서에 적는다. `writer_floor` 인상 경로의 현재 상태를 조사해 인상 수단의 제안서를 낸다(구현은 결정 뒤다).
- **의존.** 없음. 1.1-2와 worker 바이트를 바꾸는 모든 릴리스의 선행이다.
- **크기.** M. **불변.** 확인(v1 파일의 모양은 불변이다. 쌍별 기록은 새 영속 기록이라 모양을 사용자가 승인한다).
- **열린 결정.** `writer_floor` 인상의 형태(setup 명시 명령 / 릴리스가 선언한 최소 버전으로 owner가 자동 인상). 제안서를 보고 정한다.
- **합격(live).** worker 바이트가 다른 두 버전을 Claude와 Codex에 나눠 설치한 상태에서 양쪽 SessionStart를 번갈아 5회씩 실행했을 때 `provider.reattest rejected` 0건, "다시 온보딩" 안내 0건, 두 호스트 모두 recall이 semantic을 쓴다. 기록 쓰기를 막은 상태에서 hook 20회 동안 새로 뜬 worker가 2개 이하다.

### 1.0.8-5 임베딩 정체성 manifest 보강

- **현재 동작.** 정체성 manifest에 query·document 프롬프트 템플릿과 context 여유가 없다. 템플릿만 바꾼 릴리스는 실행 방식 변경으로 통과해 저장 vector와 query vector가 어긋난다. `tokenizer`·`chunkRules`·`metric`은 양쪽이 같은 상수라 비교가 되지 않는다.
- **목표.** vector의 의미를 바꾸는 worker 안의 값이 전부 정체성에 들어 있고 실제 값으로 비교된다.
- **범위.** 정체성에 `queryTemplate`·`documentTemplate`·`contextMargin`을 넣는다. manifest의 값과 worker 소스의 문자열이 같다는 것을 시험으로 고정한다. legacy 쌍 표에 같은 값을 넣어 기존 설치가 재빌드 없이 넘어간다. 불변 목록의 재빌드 규칙 문장에 프롬프트 템플릿과 context 여유를 더한다.
- **의존.** 1.0.8-4와 같은 릴리스다. **크기.** S. **불변.** 확인(attestation 키 집합은 그대로다. 재빌드 규칙 문장이 바뀐다).
- **합격(live).** 업데이트 뒤 카나리와 service-engineering에서 재빌드 0회, `reattest_legacy_unknown` 0건, recall의 semantic 비율이 업데이트 전과 같다. 템플릿을 바꾼 합성 번들은 재봉인이 거절된다(focused 시험).

### 1.0.8-6 업데이트 인계 마무리

- **현재 동작.** 1.0.3 이후 버전 사이의 업데이트는 수동 개입 없이 넘어간다. 남은 조각: timer를 내린 호출에서 hook이 최대 1초 기다린다. 대기 행이 있으면 hook이 매번 `ps -ax`와 `lsof`를 실행한다. `preferred()`의 예외 분기가 False라 순간 읽기 실패에 resident가 drain한다. `settings.json`·`config.toml`이 심볼릭 링크나 group-writable이면 그 호스트가 인벤토리에서 빠진다. 다운그레이드 뒤 캐시에 남은 더 높은 버전 디렉터리를 고른다. Hermes 사본은 enabled 검사 없이 후보가 된다.
- **목표.** 호스트 업데이트 뒤 2분 안에 모든 백그라운드 프로세스가 새 버전으로 돌고, 그 과정이 hook 지연을 늘리지 않는다.
- **범위.** 위 조각의 수정. 옛 프로세스 조사는 자기 번들이 최신일 때만 하고 결과를 수십 초 캐시한다. 인벤토리를 읽지 못한 호스트는 이벤트로 남긴다. setup 상태 조회가 실행 중인 MemVane 프로세스의 버전별 개수를 보여 준다. 두 번들 버전을 세워 timer·monitor·resident·worker 인계를 끝까지 보는 합성 시험.
- **의존.** 없음. G8의 선행이다.
- **크기.** M. **불변.** 없음(service 파일 이름·lock·protocol `1`을 유지한다).
- **합격(live).** 연속 두 번의 업데이트에서 새 버전 SessionStart 뒤 2분 안에 옛 버전 경로의 프로세스가 0개이고 수동 SIGTERM이 0회다. 그 구간의 hook p95가 평소보다 0.3초 넘게 늘지 않는다. 한 호스트만 올린 상태로 1시간 동안 worker·timer의 기동과 종료가 번갈아 반복되지 않는다(`runtime.bundle` 이벤트 기준 10건 이하).

### 1.0.8-7 릴리스 게이트: provider smoke와 live 확인 목록

- **목표.** 실제 worker를 띄워 보면 드러나는 결함이 출시 전에 잡히고, 출시 뒤 확인이 정해진 목록으로 10분 안에 끝난다.
- **범위.** 실제 embed runtime으로 resident worker를 띄워 문서 요청 하나와 query 요청 하나를 보내는 smoke(`--kind model-test`). 이 문서의 「합격(live)」을 모은 출시 뒤 확인 목록과 그 값을 한 번에 뽑는 읽기 전용 조회. `docs/release.md`에 릴리스 절차로 적는다: focused 시험, PR CI, smoke, legacy 쌍 표 갱신, live 확인.
- **의존.** 없음. **크기.** S. **불변.** 없음.
- **합격(live).** 다음 릴리스의 PR에 smoke 결과가 붙고, 출시 뒤 확인 기록이 카나리와 service-engineering에 대해 남는다.

### 1.0.8-8 pending 증명과 selector 유지보수 분리

- **현재 동작.** selector 유지보수가 pending 활성화 증명을 먼저 시도하고, 메모리 압박이면 `ResourceDeferred`로 유지보수 전체가 끝난다. 손상된 `pending-activation.json`은 지우는 주체가 없어 유지보수가 매번 실패한다.
- **목표.** pending 증명이 미뤄지거나 깨져도 기존 active runtime의 heal·warm·plugin 버전 게시는 돈다.
- **범위.** 유지보수 경로에서 pending 증명의 deferral을 삼키고 기존 active로 계속한다. 형식이 틀린 pending 기록은 실패 기록으로 옮기고 지운다. 첫 활성화 실패의 안내 문구를 "이전 선택 유지"와 구분한다.
- **의존.** 없음. 학습이 기본으로 켜져 있어 모든 설치가 이 경로를 탄다.
- **크기.** S. **불변.** 없음.
- **합격(live).** pending 기록이 있는 상태에서 메모리 압박 deferral이 난 유지보수 run의 이벤트에 heal 또는 warm 단계가 1건 이상 남는다. 7일 동안 유지보수의 `invalid_pending_activation` 0건.

## 1.0.x 패치

번호는 권장 순서다.

### 1.0.x-1 연결 worktree view 대기의 실패 기록

- **현재 동작.** main view가 게시된 pass도 연결 worktree view가 cold면 `schema3_linked_pending`으로 wake가 `failed target_deferred`가 되고 재시도 횟수가 쌓인다. service-engineering은 연결 view 2개가 stage만 있고 게시된 head가 없어 매 pass 이렇게 끝난다.
- **목표.** main 게시는 성공으로 기록되고, 연결 view의 대기는 사유와 함께 따로 기록된다.
- **범위.** wake 결과에서 main 게시와 연결 view 대기를 분리한다. `pending`에 사유를 채운다. `memvane:logs` 요약이 둘을 따로 센다. 연결 view가 끝내 게시되지 않는 원인을 service-engineering에서 진단한다.
- **의존.** 없음. **크기.** S. **불변.** 없음.
- **합격(live).** service-engineering의 24시간 `wake.sync`에서 main이 게시된 pass의 `failed`가 0건이다. 연결 view 2개가 게시되거나, 게시되지 않는 사유가 이벤트에 남는다.

### 1.0.x-2 resident 선행 요청

- **현재 동작.** fill은 문서 하나를 임베딩하고 store commit·검증이 끝난 뒤 다음 요청을 보낸다. 임베딩 중에는 Python이, commit 중에는 worker가 논다.
- **목표.** fill window에서 worker가 쉬지 않는다.
- **범위.** 다음 target의 요청을 이전 target의 commit·검증과 겹쳐 보낸다. protocol `1`과 요청 모양을 유지한다. content claim은 임베딩 동안 보유하고, deadline 뒤에 끝난 결과는 게시하지 않으며, provider 호출 경계의 초기 빌드 양보 검사를 선행 요청에도 적용한다.
- **의존.** 없음. **크기.** M. **불변.** 없음.
- **합격(live).** service-engineering의 fill pass에서 `sync.profile`의 provider 시간이 pass 벽시계의 70% 이상이다(현재 약 45%). 새 카드 1장당 벽시계가 0.3초 이하다.

### 1.0.x-3 ingest delta화와 원문 지문 누적 정지

- **현재 동작.** 카드 한 장이 바뀐 pass도 카드 전부를 읽고 해시하며 FTS 행을 pass당 4회 다시 만든다. 고정비가 16~20초다. `source_fingerprints`는 pass마다 source 수만큼 행이 늘어 service-engineering에서 source 1,991개에 225,612행이다.
- **목표.** ingest pass의 비용이 바뀐 카드 수에 비례하고, 원문 지문 행 수가 관측 내용이 바뀔 때만 는다.
- **범위.** FTS 행과 wiki 파일 대조를 delta로 만든다. 같은 `(worktree, branch, path, sha, tracked_state)` 관측은 행을 다시 쓰지 않는다. 이미 쌓인 행의 회수는 1.1-4가 맡는다.
- **의존.** 없음. 1.1-4의 선행이다.
- **크기.** M. **불변.** 확인(DDL은 유지한다. fingerprint id와 manifest `sources`의 의미가 바뀌므로 세대 seal·stage digest 입력과 낮은 버전 호스트의 검증을 먼저 판정한다).
- **합격(live).** service-engineering에서 카드 1장 변경 pass의 `sync.profile` scan+plan+publish 합이 5초 이하다. 편집 없는 24시간 동안 `source_fingerprints` 행 수 증가가 0이다.

### 1.0.x-4 학습과 색인의 경합 계측

- **목표.** 학습 기본 켜짐 뒤 학습이 sync·초기 빌드·recall을 얼마나 미루는지 수치로 안다.
- **범위.** G7 전 기준선과 G7 뒤 7일의 `learning_*` 사유 deferral 건수·대기 시간을 logs 요약으로 낸다. busy 사유에 막은 게이트(provider·control·trainer)를 싣는다.
- **의존.** G7. 1.1-3의 착수 판단 입력이다.
- **크기.** S. **불변.** 없음.
- **합격(live).** 7일 요약에서 `learning_*` 사유로 미뤄진 recall이 0건이고 미뤄진 sync의 대기 중앙값이 30초 이하면 1.1-3을 뒤로 미룬다. 넘으면 1.1의 첫 항목으로 올린다.

### 1.0.x-5 sync owner 점유 계측

- **현재 동작.** ingest pass도 최대 240초의 vector window를 쓰므로 사용자당 sync owner를 그만큼 쥔다. 선택된 프로젝트가 10개가 되면 다른 프로젝트의 sync가 그 뒤에 선다.
- **목표.** 프로젝트 사이의 sync 대기가 수치로 보인다.
- **범위.** sync owner 보유 시간과 `owner_busy` 대기 시간을 이벤트에 싣고 logs 요약이 프로젝트별로 낸다. resident의 5초 주기 소유 재증명과의 경합 빈도를 함께 센다.
- **의존.** G7. 1.1-8의 입력이다.
- **크기.** S. **불변.** 없음.
- **합격(live).** G7 뒤 7일 요약에서 편집부터 그 프로젝트의 게시까지 p95가 나온다. p95가 5분을 넘으면 1.1-8의 window 분할을 패치로 당긴다.

### 1.0.x-6 재봉인·온보딩 안내 정리

- **범위.** prepare 뒤 activate 전인 설치도 재봉인 대상에 넣거나, 진행 중 온보딩은 다시 시작한다는 안내를 낸다. 수동 `update` skill이 재봉인 가능한 설치에 "다시 온보딩"을 내지 않는다. `provider.reattest rejected` 이벤트를 설치당 한 번만 남긴다. 재봉인 검사 실패를 resident가 `attestation_invalid`로 분류한다. 유효한 기록이 있으면 sync pass가 owner 획득 없이 재봉인 준비를 건너뛴다. setup skill의 버전 전환 서술을 현재형으로 고친다.
- **의존.** 1.0.8-4. **크기.** S. **불변.** 없음.
- **합격(live).** 업데이트 한 번에 설치당 `provider.reattest` 이벤트가 2건 이하다. 재봉인 가능한 설치에서 `update` skill이 queued를 보고한다.

### 1.0.x-7 GPU 폴백 후속

- **범위.** CPU로 내려간 worker가 resident 수명 동안 CPU에 남지 않게 GPU 복귀 시도를 둔다(상한 있음). CPU 폴백의 thread 수를 성능 코어 수 이하로 둔다. 입력 하나의 결정적 실패와 GPU 실패를 구분한다.
- **의존.** 1.0.8-4. **크기.** S. **불변.** 확인(worker 바이트 변경은 재봉인 대상이다).
- **합격(live).** 7일 동안 resident worker의 실행 상태가 GPU인 시간 비율이 99% 이상이다. CPU 폴백 중 recall p95가 목표의 1.5배 이하다.

### 1.0.x-8 운영 가시성

- **목표.** 멈춘 이유가 사용자에게 보인다.
- **범위.** 디스크가 가득 찼을 때 wake worker 재기동 루프를 막고 `candidate_storage_full`을 안내한다. 수명 주기 대량 삭제 가드가 보류한 run을 SessionStart 한 줄로 알린다. compile이 돌지 못하는 사유(`needs_extractor`)를 안내한다. 처음 보는 브랜치의 cold view 동안의 안내. 백그라운드가 번들을 찾지 못한 상태(`unavailable`)를 이벤트로 남긴다. 활성화 뒤 첫 backfill의 호스트 CLI 호출 규모를 setup 승인 단계에 적는다. embed runtime을 다시 준비했을 때 기존 설치가 재봉인되는지 재빌드되는지를 setup이 미리 알려 준다.
- **의존.** 없음. **크기.** M. **불변.** 없음.
- **합격(live).** 각 상태를 fixture로 만든 SessionStart에서 안내가 정확히 한 줄 나온다. G7의 8개 프로젝트 온보딩에서 사용자가 로그를 열어야 원인을 알 수 있던 정지가 0건이다.

### 1.0.x-9 시험 기반

- **목표.** 로컬 전체 검사가 20~30분이고, 지원 하한(Python 3.9)과 최신에서 같은 검사가 CI로 돈다.
- **범위.** 일반 `guard --kind test`의 파일 병렬도를 3~4로 올린다(자원 제한이 있는 작업과 단독 실행 규칙은 유지한다). Node 22·24 × Python 3.9·3.13 조합은 GitHub Actions에서 병렬로 돌고 로컬은 Python 3.9와 Node 한 버전으로 돈다. 시험 공백: 쓰기 때 현재 원문 판정의 경계 사례, 온보딩 중간 기록이 재부팅으로 `st_dev`만 바뀐 뒤 재개되는지, Hermes 선택 route 분류 뒤 import 실패, vector page 예산 경계.
- **의존.** 없음. **크기.** M. **불변.** 없음.
- **열린 결정.** 파일 병렬도 3과 4 중 자원 관측에 맞는 값. PR 기본 조합.
- **합격.** 로컬 전체 검사 벽시계 30분 이하. main CI 전체 조합이 연속 5회 통과한다.

### 1.0.x-10 문서와 릴리스 절차 정합

- **범위.** `docs/release.md`·`docs/release-agent-qa.md`를 현재 검증 방식에 맞춰 줄인다. 불변 목록에 `provider-reattest.json`과 재봉인 규칙을 반영한다. `recipe_fingerprint`의 loader 문자열이 값이 아닌 라벨이라는 점을 적는다. 사용자 문서의 state 경로, 없는 진단 이름, minimal settings의 허용 키 5개, `CONTRIBUTING.md`의 폴더 이름. `_render_context`의 비위키 행 분기 확인. 저장소 자체 위키에서 없는 동작을 설명하는 카드의 퇴역.
- **의존.** 1.0.8-4·7. **크기.** S. **불변.** 없음.
- **열린 결정.** 설치본 agent QA 계약과 유예된 assertion 5개를 live 확인 목록으로 대체하는지.

### 1.0.x-11 방어 심화, 세션당 고정비

- **범위.** fixture provider seam 게이트의 temp-root 조건. reader 경로 `journal_directory(create=True)`의 경로 모양 검사. journal `_open`의 `mode=ro` 예외 전제를 단정으로 고정. fill call 범위에서 store header 검증 결과를 `schema_version`·`data_version`이 같을 때 재사용한다(카드당 약 10 ms).
- **의존.** 없음. **크기.** S. **불변.** 없음.
- **합격.** focused 시험. 고정비는 `sync.profile`의 storage 시간이 카드당 10 ms 이상 줄 때만 유지한다.

### 1.0.x-12 검증 job 없는 교체 pending 정리

- **현재 동작.** verified 카드의 교체 후보 `.<card>.pending-<hash>`가 대응하는 verify-queue job 없이 남을 수 있다.
- **운영 근거.** 0.x에서 생성된 job 없는 후보가 service-engineering 191개, agentflow 120개, rccar 37개 관측됐다. 이 348개는 운영자가 archive로 옮겼다.
- **범위.** 수명 주기 또는 verify worker가 job 없는 pending의 나이와 소유·경로를 확인해 정해진 보존 기간이 지난 파일을 정리하고 결과를 이벤트로 남긴다. 활성 job과 진행 중인 교체 후보는 보존하고, 삭제 직전에 job과 파일 정체성을 다시 확인한다.
- **의존.** 없음. **크기.** S. **불변.** 확인(기존 queue·lock·파일 모양을 유지한다).
- **열린 결정.** 보존 기간과 정리 주체.
- **합격(live).** 보존 기간이 지난 job 없는 pending은 다음 정리 후 0개이고 정리 건수가 이벤트와 일치한다. 활성 job의 pending과 verified 카드는 보존된다.

### 1.0.x-13 setup gitignore 안내 보강

- **운영 근거.** 프로젝트 포인터 외에도 compile 작업 파일과 wiki의 숨김 임시 파일이 Git 미추적 항목으로 나타날 수 있다.
- **범위.** setup의 `.gitignore` 안내에 `.memvane/compile/`과 `.memvane/wiki/**/.*`를 더한다. 기존 안내와 추적 파일은 유지한다.
- **의존.** 없음. **크기.** S. **불변.** 없음.
- **합격.** setup 안내에 두 줄이 각각 한 번 나오고, 적용 뒤 compile과 숨김 임시 파일은 무시되며 일반 wiki 카드와 이미 추적된 파일은 유지된다.

### 1.0.x-14 머신 간 중복 카드 정리

- **운영 근거.** 같은 원문에서 머신마다 이름이 다른 wiki 카드가 생겨 중복이 누적될 수 있다.
- **범위.** 1.0의 사후 dedup 스캔이 실제로 실행되는지 확인한다. 같은 원문 카드의 결정적 경로와 기존 중복 처리 기준을 정하고, 중복이 누적되면 SessionStart에 안내한다.
- **의존.** dedup 스캔 동작 확인. **크기.** M. **불변.** 확인(기존 카드·queue 계약 유지 여부를 판정한다).
- **합격.** 두 머신에서 같은 원문에 대한 카드 경로가 같고, 이름이 다른 기존 중복을 스캔이 발견한다. 누적 조건에서 SessionStart 안내가 나오고 정상 카드가 보존된다.

## 롤아웃

### G7 나머지 8개 프로젝트

- **조건.** 1.0.7을 카나리와 service-engineering에서 24시간 관찰한 값이 다음을 채운다: 기본 학습 프로필이 프로젝트당 1개이고 SessionStart를 반복해도 프로필·grant 수가 늘지 않는다. selector warm 진입 이벤트가 프로젝트당 1건 이상이다. `hook.error` 0건이다. recall p50이 학습을 켜기 전보다 0.2초 넘게 늘지 않는다. `learning_*` 사유의 sync·recall deferral 건수를 기준선으로 기록한다.
- **절차.** S7 runbook 4절. 프로젝트마다 prepare·activate는 사용자 승인이다.
- **합격(live).** 8개 모두 첫 lexical 세대에서 ready가 되고 vector 세대가 게시된다. 프로젝트별 초기 빌드 시간과 fill 처리량을 기록한다. 온보딩 중 수동 프로세스 개입 0회.

### G8 mini

- **조건.** 1.0.8이 이 Mac에서 1.0.8-6의 합격 기준을 한 번 채운다. mini의 `claude`·`codex` 설치 경로(Homebrew 포함)와 `settings.json`·`config.toml`의 링크 여부를 먼저 확인한다.
- **절차.** S7 runbook 5절(M1~M8). store는 복사하지 않는다.
- **합격(live).** 새 기계에서 embed runtime 준비부터 첫 프로젝트 recall의 semantic 사용까지 수동 프로세스 개입 0회. mini에서 한 번의 업데이트가 1.0.8-6 기준을 채운다.

## 1.1

### 1.1-1 대형 프로젝트 semantic 검색 구조

- **목표.** vector 100,000개 규모에서도 recall의 semantic 단계가 hook 예산 안에 끝난다.
- **범위.** 설계 문서와 독립 리뷰가 먼저다. 1.0.8-3의 계측을 근거로 방향을 고른다: lexical 후보 한정 rerank, resident에 검색 위임, 버릴 수 있는 파생 검색 캐시. 봉인된 세대의 exact 결과와의 일치 기준(상위 k 겹침)을 정한다.
- **의존.** 1.0.8-2·3. **크기.** L.
- **불변.** 확인(store DDL에 색인을 더하면 저촉이다. resident 요청 종류를 더할 때 낮은 버전 resident의 동작을 판정한다. 파생 캐시는 세대 digest에 묶이고 없으면 exact로 돌아가야 한다).
- **합격(live).** service-engineering의 recall에서 semantic 사용 비율 99% 이상, p95가 목표 이하, exact 대비 상위 10개 겹침 9개 이상(표본 100건).

### 1.1-2 worker 안 batch 임베딩

- **목표.** 여러 chunk·문서를 한 번의 worker 호출로 임베딩한다.
- **범위.** `laya_local_embed_worker.mjs`의 batch 처리. 실행 방식 변경이므로 재봉인으로 넘어간다. 정체성(템플릿·최대 토큰·절단 없음)은 그대로다.
- **의존.** 1.0.8-4·5, 1.0.x-2. **크기.** M. **불변.** 확인(worker 파일 이름과 요청 envelope을 유지한다).
- **합격(live).** 업데이트 뒤 재빌드 0회. 초기 빌드의 fill 처리량이 1.0.x-2 대비 1.5배 이상이다.

### 1.1-3 admission 분리와 busy 사유

- **현재 동작.** 학습과 색인이 같은 wake 큐와 dispatcher lease를 쓴다. 학습 추론 슬롯이 차 있으면 sync·초기 빌드·recall admission이 `learning_*` 사유로 미뤄진다.
- **목표.** 기존 큐·wake kind·lock을 유지한 채 학습이 recall·sync·초기 빌드를 막지 않는다.
- **범위.** admission을 색인용과 학습용으로 나눈다. sync·초기 빌드·recall 경로에서 학습 수요 확인을 뺀다. 학습이 상위 수요에 한 step(5초 이하) 안에 양보한다. 학습 예약 메모리를 회수 가능으로 센다.
- **의존.** 1.0.x-4. **크기.** M. **불변.** 없음(큐·lease 분리는 2.0이다).
- **합격(live).** 7일 동안 `learning_*` 사유로 미뤄진 recall·sync 0건. 학습 cycle 완료 건수가 분리 전의 80% 이상이다.

### 1.1-4 공유 content GC

- **현재 동작.** 여러 view가 공유하는 blob 본문·admission·attestation·원문 지문·chunk/embedding set은 지우지 않는다.
- **목표.** 어떤 view의 head·직전 세대에서도 닿지 않는 공유 내용을 회수한다.
- **범위.** 선택 sync의 owner·writer scope 안에서 게시·fill이 정상 반환된 뒤 같은 프로세스에서만 실행한다. journal이 선택한 store만 대상으로 한다. 유계 `BEGIN IMMEDIATE`마다 참조를 다시 확인하고 vector → embedding set → chunk → chunk set → attestation → admission → blob 순으로 지운다. head·직전·stage·delta·진행 중 fill 대상·compile intent·trust chain에 닿는 blob을 보호한다. 참조 없는 원문 지문과 소비가 끝난 compile intent 파일을 회수한다.
- **의존.** 1.0.x-3, 1.0.8-4의 `writer_floor` 판정. **크기.** L(설계 문서와 독립 리뷰 선행). **불변.** 없음(DDL과 compile intent 키 집합을 유지한다).
- **합격(live).** service-engineering의 `source_fingerprints`가 225,612행에서 살아 있는 관측 수의 2배 이하로 준다. store 파일 크기가 30일 동안 카드 수 변화 비율 안에서 움직인다. GC가 돈 pass의 recall p95가 목표 이하다.

### 1.1-5 진단과 deferral 정확성

- **범위.** recall 거절 이벤트의 내부 고정 코드 필드. lexical 0건 recall의 reason은 `no_candidates`다. off-hook 자식 진입점 전체가 `ResourceDeferred`를 deferral로 끝내는지 전수 점검. `normal_writer_deadline`의 sync 분류. compile 중첩 worker의 실패 범주와 backlog 크기 필드.
- **의존.** 1.0.x-1. **크기.** M. **불변.** 없음.
- **합격(live).** 7일 이벤트에서 사유 없는 `failed`·`refused`가 0건이다.

### 1.1-6 recall·wiki 품질

- **범위.** verify defer 루프의 종점(`source_changed_before_verify`, `content_hash_mismatch`, `missing`의 분리). vector 대상에서 빠지는 카드(service-engineering 3,751장 중 568장)의 사유 분류와 대책. 긴 프롬프트의 `laya_selector_invalid_pool`. PostToolUse recall 주입과 subagent·하네스 생성 프롬프트의 주입 기준.
- **의존.** 1.1-5. **크기.** M. **불변.** 확인(`.memvane/compile/*.jsonl` 행에 재시도 상태를 싣는 방식은 추가 키로 되는지 판정한다).
- **열린 결정.** 주입 기준 2건. 판단 전에 이벤트 로그로 건수와 분량을 잰다.
- **합격(live).** verify 큐에서 14일 넘게 남은 행 0건. vector 대상에서 빠진 카드 비율이 5% 이하이거나 사유별 건수가 이벤트에 남는다.

### 1.1-7 설치 수명 주기 마무리

- **범위.** 해제된 설치 디렉터리의 미리보기와 승인 삭제. uninstall이 지우는 범위의 정의. 플러그인을 제거·비활성한 뒤 남는 pending wake와 resident·monitor의 종료. 레지스트리의 이전 tombstone, 사라진 worktree 행, 조회 경로 Git 호출의 예산. 학습 runtime 세대 GC가 pending 세대를 지우지 않는지 확인.
- **의존.** 없음. **크기.** M. **불변.** 확인(레지스트리 DDL을 유지한다).
- **합격(live).** 플러그인을 비활성한 뒤 10분 안에 MemVane 프로세스가 0개다. 해제된 설치 디렉터리가 승인 삭제 뒤 0개다.

### 1.1-8 시간 예산

- **목표.** 사용자당 lock을 한 번에 쥐는 시간에 상한이 있고, resident 교체 중 vector 폴백 구간이 5초 이하다.
- **범위.** vector window를 여러 프로젝트가 나눠 쓰도록 쪼갠다. embed runtime 준비의 `npm ci`·모델 다운로드를 사용자당 lock 밖으로 옮긴다. resident 교체를 make-before-break로 바꾸고 메모리 admission이 막으면 break-before-make로 돌아간다.
- **의존.** 1.0.x-5, 1.1-3. **크기.** L. **불변.** 확인(resident 교체 절차가 `successor.lock`·protocol `1`의 의미를 바꾸면 저촉이다).
- **합격(live).** 10개 프로젝트 환경에서 편집부터 게시까지 p95 2분 이하. 업데이트 중 `laya_service_preparing` 폴백 recall이 5초 구간 밖에서 0건이다.

## 1.1 이후

| 항목 | 크기 | 불변 | 다시 꺼낼 조건 |
|---|---|---|---|
| 세션 위치와 무관한 recall(미온보딩 루트, 프로젝트 간 편집) | L | 확인 | 1.1-7 뒤. 설계 문서 선행 |
| 연결 worktree의 카드 수명 주기 | L | 확인 | 위 항목 뒤 |
| 대시보드 편집·알림 | M | 확인 | 1.1-7 뒤 |
| 도메인 `domain/` 층 실체화 | L | 없음 | 패치 릴리스 간격이 1주 이상으로 안정된 뒤. 도메인 단위로 나눈다 |
| 원문 소실 보류(`held_missing`)와 삭제 receipt | L | 확인 | 삭제 근거를 store에서 찾아야 하는 사례가 생길 때 |
| 학습 중지·재개의 end-to-end 증거, 학습 자식 부하 측정 | M | 없음 | 1.1-3과 함께 |

## 2.0 후보

1.x 불변 목록을 바꾸는 항목이다. 지금은 열지 않는다.

| 항목 | 저촉 지점 |
|---|---|
| 학습 큐와 dispatcher lease 분리 | wake 큐 DB의 위치·DDL, lock 이름. 1.1-3 뒤에도 경합이 실측될 때 |
| lock 범위 축소(`provider.lock`을 설치 단위, sync owner를 프로젝트 단위) | lock 이름과 위치. 1.1-8로 부족할 때 |
| 같은 store 안의 임베딩 정체성 교체(모델·chunker 변경 뒤 store 유지) | route·build·journal의 profile 결속. 지금은 재빌드다 |
| store 안의 영속 근사 검색 색인 | store DDL. 1.1-1의 파생 캐시로 부족할 때 |
| resident protocol 번호 변경이 필요한 검색·batch 위임 | protocol `1`, service 파일 |
| `provider-reattest.json` v1 모양의 변경·폐기 | 1.0.3 이후 호스트가 읽는 기록 |
| 원문 지문 id·세대 seal digest 입력의 변경으로 낮은 버전 호스트가 세대를 검증하지 못하는 경우 | store 행 의미. 1.0.x-3의 판정 결과에 따른다 |
| 학습 끄기를 한 키로 묶는 설정, hook 지연 목표 설정 등 새 settings 키 | settings 허용 키 집합 |
| 영속 이름 정리(`laya_*.mjs`, wake kind `laya-sync`·`laya-compile`, `laya-install.lock`, `schema3` 식별자) | 파일·kind·lock 이름 |

## 하지 않는 것

| 항목 | 이유 |
|---|---|
| frontmatter 없는 카드 writer | frontmatter 원문 지문을 유지한다 |
| recall 쪽 trust 규칙 변경 | 현재성은 쓰는 쪽이 맡는다 |
| 새 lock으로 공유 content GC 보호 | 그 lock을 모르는 버전에는 효력이 없다 |
| store의 WAL 전환, 단일 FTS + 세대 필터 | rollback journal과 view별 head FTS를 유지한다 |
| retired-wiki 보관본 자동 삭제 | 자동 삭제가 없다 |
| 기존 카드 일괄 재작성, 지문 없는 카드의 소급 백필 | 명시 요청이 있을 때만 한다 |
| 다른 메이저의 state를 읽거나 옮기는 코드 | 메이저가 다르면 다시 온보딩한다 |
| 긴 전체 검사를 릴리스 게이트로 쓰는 것 | focused 시험, PR CI, live 확인으로 검증한다 |

## 관찰

| 항목 | 다시 꺼낼 조건 |
|---|---|
| CPU 병목의 Rust 부분 도입 | 1.0.x-2·3과 1.1-1 뒤에도 Python 연산이 단계 시간의 절반을 넘을 때 |
| 중첩 프로젝트 온보딩, 다른 기계로 store 복사 | 요청이 있을 때 |
| Hermes의 SessionStart 안내 통로, 편집 뒤 컨텍스트 주입 | Hermes가 해당 hook 반환값을 쓰게 될 때 |
| `npx skills add` 설치 진입점 | installer 수명 주기 설계가 선행한다 |
| 카드 유효 구간, 카드 간 모순 lint, stale 전파, 주입 효과 평가 지표 | 사용자가 다시 다루기로 정할 때 |
| embed runtime 세대 GC 자동화 | `gc-index-runtime`으로 부족할 때 |

## 열린 결정

1. 설치본 agent QA 계약(`docs/release-agent-qa.md`)과 유예된 assertion 5개(`RQA-GIT-01`, `RQA-GIT-03`, `RQA-WIKI-01`의 유료 중복 호출 부분, `RQA-WIKI-03`, `RQA-RES-02`)를 live 확인 목록으로 대체하는지, 실행하는지(1.0.x-10).
2. `writer_floor` 인상의 형태(1.0.8-4의 제안서 뒤).
3. 재봉인 쌍별 기록의 모양(1.0.8-4).
4. 대형 프로젝트 semantic의 방향(1.1-1). 1.0.8-3 계측 뒤에 정한다.
5. PostToolUse 주입과 subagent·하네스 프롬프트 주입 기준(1.1-6).
6. 프로젝트 settings의 `diagnostics`를 켜 둘지.
7. 시험 파일 병렬도 3과 4, PR 기본 조합(1.0.x-9).

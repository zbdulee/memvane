# 원래 기능의 남은 연결 간극 정리 계획 (2026-10-18)

계획 문서다. 코드·테스트·설치·운영 데이터는 바꾸지 않았고, 아래 "증거"는 이 worktree
(PR #13, head `077f8186`)의 소스를 읽은 결과다. 실행 검증이 아니다.

## 1. 두 가지 "완료"를 구분한다

- **아키텍처 Stage 1–6 완료**: [기능 경계 계획](2026-10-17-architecture-boundaries.md)의
  경계 이동(런타임 handler·lease, 위키 revision 키, recall 학습 읽기 포트, 설치 증거 판정,
  A-int multi-root source와 v2 product sync worker 연결)이 소스에 들어가 집중 가드 회귀와
  PR #13 CI(Node 22/24/26, head `077f8186`)를 통과했다는 뜻이다.
- **원래 기능의 end-to-end 완료**: 사용자가 pull·branch·worktree·편집·삭제를 한 뒤
  선택된 프로젝트의 검색이 결국 새 내용을 돌려주고, 위키가 중복 비용 없이 최신 출처로
  따라오며, 자료가 커져도 조용히 낡은 결과를 내지 않는 것. 이것은 **아직 완료가 아니다.**
  아래 네 간극이 남아 있고, L1 설치본 QA도 실행하지 않았다.

Stage 1–6 통과를 위 원래 기능의 완료로 보고하지 않는다.

## 2. 이미 정해진 것 (이 계획에서 다시 논의하지 않음)

- 저장 구조: A-int **단일 SQLite**. generation은 immutable이며 매번 full membership을 갖고,
  content(hash)·vector(hash, title, profile)는 generation 사이에 공유한다. head 이동과
  receipt는 같은 트랜잭션이고 head CAS, receipt 기반 조건부 ACK를 유지한다.
- 일관성: **비동기 최종 일관성**이 의도된 동작이다. hook은 durable wake만 기록하고 모델·
  색인 작업은 소유 worker가 한다. 같은 세션 안의 동기 SLA(예: "편집 후 N초 안에 검색")를
  새로 만들지 않는다.
- 최신성 실패는 명시적 갱신 필요 오류로 드러낸다([계약 게이트](2026-10-17-laya-index-a-product-contract-gates.md)).
  선택 route 실패 시 QMD fallback은 없다.
- 등록: 중앙 등록은 이미 승인된 indexing ON 범위를 찾는 **읽기 모델**이다
  ([중앙 등록 결정](2026-10-17-central-registration-options.md)). 외부 경로·새 worktree의
  자동 등록·자동 승인은 없다. 변경 소유권과 현재 세션 검색 scope는 별개다
  ([같은 세션 계획](2026-10-17-same-session-worktree.md)).
- 이 계획은 새 기능이나 지원 범위(host, collection 수, compile 모드)를 넓히지 않는다.
- 소유권: `core/laya_product_install.py`와 claim queue(`laya_index_claim_queue` 등) 변경은
  **상위 릴리즈 작업자 소유**다. 이 계획의 단계가 그 파일을 필요로 하면 그 작업자와 맞춘
  인터페이스로만 요청한다.

## 3. 간극 1 — stale 차단 → durable sync wake → 전체 generation 게시 → 새 결과

**현재 증거**
- v2 검색 `_v2_phase`(`core/laya_host_response.py:414`)는 pin한 seal manifest와 live
  `scan_collections`가 다르면 `index_stale`을 던진다. `main`(`:822`)은 모든 예외를
  `selected_proof_or_query_failed`로 바꿔 exit 3으로 끝낸다. **recall 경로는 sync wake를
  요청하지 않는다.**
- wake는 `update`/`update-queued`/`index`/`file-change-event` 행동에서만
  `runtime_work.request('laya-sync', projectRoot)`로 기록된다(`:697-745`). worker는
  `laya_product_sync_worker.run` → `sync_full`로 full-membership generation을 게시한다.
- route는 payload `cwd`의 조상 탐색으로 고른다(`core/laya_index_host_route.py:226`).
  하위 디렉터리 cwd는 같은 projectRoot로 묶인다. 새 worktree는 route record가 없거나
  (legacy로 분류) 복사된 record의 `projectRoot` 불일치(blocked)가 된다. 어느 쪽인지는
  `.auto-context` 추적 여부에 따라 다르며 회귀로 고정되지 않았다.
- 에이전트 밖의 `git pull`/checkout은 hook 이벤트가 없다. Codex에는 Bash 변경 관찰이 없다.
  즉 SessionStart 전까지 이를 깨우는 경로는 stale을 본 recall뿐인데, 그 경로가 끊겨 있다.

**빠진 연결**: stale 판정 → durable wake. 그리고 stale과 "용량 초과"(간극 4)의 구분.

**최소 단계**
1. `laya_host_response`에서 `IndexContractError('index_stale')`이고 원인이
   manifest/seal 불일치일 때만, route proof를 다시 확인한 뒤 `runtime_work.request('laya-sync', projectRoot)`
   를 호출한다. 결과는 기다리지 않는다. 낡은 행은 여전히 내보내지 않는다.
2. stderr 진단 코드를 `index_stale_sync_queued` / `index_stale_sync_unavailable`로 나눈다.
   exit 코드와 "QMD fallback 거부" 의미는 유지한다.
3. 용량 초과(`too many files`/`oversize`)는 이 wake를 보내지 않는다(간극 4의 상태로 보낸다).

**합성 수용 기준** (임시 A-int DB/queue, 두 disjoint root, 가짜 provider·service; 기존
`laya-product-sync-worker-checks.py` 형태)
- G1 게시 → raw 파일 수정 → recall: G1의 수정 전 본문을 내지 않고, WakeQueue에
  `laya-sync`/projectRoot 행이 1개 생긴다. recall을 5번 반복해도 행은 1개다.
- 그 wake를 명시적으로 drain → G2 게시(full membership, receipt, 조건부 ACK) → recall이
  새 본문을 돌려준다. hook 안에서 provider 호출·모델 시작은 0회다.
- 합성 git fixture에서 pull(fast-forward)·branch 전환의 **생성/수정/삭제**를 raw와 wiki
  양쪽에 적용: 위 흐름 뒤 삭제 경로는 G2 membership과 결과에 없다.
- 하위 디렉터리 cwd에서도 같은 projectRoot·같은 wake 키를 쓴다.
- `git worktree add`로 만든 다른 경로 cwd는 원 프로젝트의 generation을 쓰지 않으며
  (legacy 또는 blocked 중 실제 분류를 고정), 원 프로젝트로 wake를 보내지 않는다.
- wake 기록 실패 시 결과 0건과 `index_stale_sync_unavailable`.

**결정 상태**: 모두 기존 합의 안의 구현이다. 새 정책 결정은 없다.

## 4. 간극 2 — cwd와 독립된 등록 indexing root 소유권

**현재 증거**
- `core/change_owner.py`(B1/B2a)는 legacy 공유 QMD store의 `approvedProjects`·`collections.json`
  으로 present 파일과, 이전 관측 ledger로 missing 경로를 소유자에게 보낸다. 선택된 Laya
  route는 이 registry에 연결돼 있지 않다(연결 코드 없음).
- 선택 route의 `index` 행동은 `_verified_edit`(`laya_host_response.py:607`)에서 cwd와 편집
  경로가 둘 다 **cwd에서 고른** projectRoot 안에 있어야 하고, 아니면 `edit_outside_project`
  로 실패한다. 다른 cwd에서 선택 프로젝트 파일을 편집하면 그 소유자는 깨어나지 않는다.
- 선택 경로 코드에 기본 브랜치 이름 하드코딩은 보이지 않는다. worktree identity는
  route record의 정규 `projectRoot`·`scopeId`·`dbIncarnation`이다.

**빠진 연결**: 선택 route 프로젝트를 소유자 읽기 모델에 포함하고, cwd 밖 편집·삭제·이동을
그 소유자의 `laya-sync` wake로 보내는 것.

**최소 단계**
1. 설치기가 이미 쓰는 선택 route record(설치 journal이 승인한 projectRoot·collection roots)
   를 `change_owner`가 읽을 수 있는 읽기 전용 목록으로 노출한다. 쓰기·등록은 설치 소유자
   (상위 작업자)만 한다.
2. 선택 route 소유 경로는 per-file enqueue가 아니라 **full-scan wake**이므로, 등록 root
   아래의 정규화·symlink 없는 lexical 경로이면 present/missing/rename 양쪽 모두 소유자
   wake 근거로 충분하다. 실제 삭제 여부는 worker의 full scan이 판정한다(스캔 실패 ≠ 삭제).
3. `index`/`file-change-event`에서 편집 경로가 현재 route 밖이면 실패 대신 위 소유자 판정을
   하고, 소유자가 정확히 1개일 때만 그 projectRoot로 `laya-sync`를 요청한다. 현재 세션의
   검색 scope는 바꾸지 않는다.

**합성 수용 기준**
- 선택 프로젝트 A, 세션 cwd는 B: A 파일 절대 경로 편집/삭제/이동(양쪽) → A에만 wake,
  B의 recall scope·DB는 불변. drain 뒤 A recall이 변경을 반영한다.
- 미등록 경로·OFF·중첩 모호·symlink·root 밖 → wake 0, registry·설정 bytes 불변,
  새 등록 0.
- A의 worktree A′(다른 경로): 명시 승인 전에는 wake·등록 0. 합성 설치 fixture로 승인한 뒤
  A′는 A와 다른 `scopeId`/DB binding을 쓴다. 브랜치 이름을 `trunk`와 detached HEAD로 바꿔도
  결과가 같다(기본 브랜치 비의존).

**결정 상태**
- 기존 합의 안의 코드 작업: 명시 승인, 자동 등록 없음, worktree별 별도 identity, 모호하면
  보류. 승인된 root로의 소유자 routing(위 단계 1–3)은 이 합의 안의 구현이다. 위 단계 2의
  "등록 root 아래 lexical 경로면 wake 충분"도 같다 — B2a 관측 ledger를 선택 route에는
  요구하지 않는다. 내용 신뢰가 아니라 재스캔 요청이므로 기존 원칙 안이다.
- **설치 소유자와의 인터페이스 조율**(사용자 정책 결정이 아님): 선택 route 소유자 목록의
  단일 출처와 읽기 형식, 소유자 wake API. `core/laya_product_install.py`는 별도 작업자가
  수정 중이므로 그 작업자와 맞춘다. 권장: 설치 journal을 출처로 하는 읽기 전용 목록.

## 5. 간극 3 — 위키 cross-trigger, claim 이후 revision/비용 dedup, provenance 최신성

**현재 증거**
- wiki → 색인: 카드 쓰기·검증 뒤 `wiki_selected_laya.request_sync`가 `laya-sync`를 요청한다
  (`wiki_compile.py:1727`, `wiki_verify_worker.py:421`).
- raw → 위키: 편집 `compile` 행동은 `enqueue_payload`로 source queue에 넣는다. SessionStart의
  선택 경로는 queue가 비어 있지 않거나 pending refresh가 있을 때만 `laya-compile`을 깨운다.
  선택 route의 `index`/`file-change-event`(Bash·pull 등)는 `laya-sync`만 요청한다.
  **pull/branch로 바뀐 raw가 선택 route에서 위키 source queue에 들어가는 경로는 확인하지
  못했다**(검증 간극).
- dedup: Stage 3는 **live 큐에 남은** 같은 `(collection, path, contentRevision, trigger)`만
  합친다. claim·처리된 행, trigger가 다른 행, revision 없는 PostToolUse·수동 sync 행은
  합치지 않는다. worker의 extractor 전 비용 가드는 `verify_suppression_hash`(검증 불가로
  삭제된 같은 본문)뿐이다(`wiki_compile_worker.py:808`). 이미 같은 sha로 검증된 카드가
  있어도 다시 유료 추출할 수 있다.
- 최신성: 선택 v2는 카드의 `sourceRevisions` 각 항목의 sha256·size·**mtimeNs**가 현재 파일과
  같고 pending refresh가 없을 때만 카드를 낸다(`_verified_wiki_row`). 같은 bytes로 돌아온
  checkout도 mtime이 바뀌면 카드가 빠지고 raw로 내려간다.
- 출처 줄 링크: production compile(`wiki_compile.py`)은 파일 단위 `sourceRevisions`만 기록한다.
  `startLine/endLine/quoteAnchor` 줄 단위 증거는 topical 경로(`wiki_topical*`, fake pipeline,
  experiment)에만 있고, 선택 route는 topical marker를 `selected_topical_unsupported`로 거절한다.
  `recall.line_start_offset`은 raw 발췌 위치 계산이지 카드 출처가 아니다.
  이전 리뷰의 "선택 production 위키에 줄 span이 없다"는 **리뷰 지적이며 모든 경로(extractor
  반환 필드, verify worker, 기존 카드)에 대해 확인하지 않았다. 확정 결함이 아니라
  검증·계약 간극**으로 다룬다. topical 합성 fixture의 줄 링크를 production 기능으로 표기하지 않는다.

**최소 단계**
1. 검증: 선택 route에서 Bash/pull/SessionStart 변경이 raw source를 위키 queue에 넣는지
   합성으로 고정한다. 없으면 SessionStart 선택 경로에 기존 `enqueue_changed_sources`(동의
   gate 그대로)를 연결한다. 새 trigger를 만들지 않는다.
2. claim 이후 dedup: worker가 extractor 실행 **전에**, 같은 `(collection, path, sha256)`을
   `sourceRevisions`로 가진 검증된 카드가 있고 pending refresh가 없으면
   `already_compiled_revision`으로 건너뛴다. 근거는 이미 durable한 카드 frontmatter이며 새
   ledger를 만들지 않는다.
3. 줄 링크: 위 inventory를 문서화하고, production 계약에 줄 단위 링크가 있는지 확정 전까지
   문서·UI에서 topical 합성 링크를 production 출처처럼 설명하지 않는다.

**합성 수용 기준** (가짜 extractor 호출 수 계측, 실제 유료 호출 없음)
- 같은 revision을 SessionStart·file-change·PostToolUse·수동 sync로 여러 회차(claim·처리 뒤
  포함) 넣어도 extractor 1회. bytes 변경 시 1회 추가.
- 카드 게시 → `laya-sync` wake → 새 generation에 카드 포함 → 현재 출처일 때만 recall이 카드를 낸다.
- raw 수정·삭제·rename 뒤: 이전 카드는 제공되지 않고 pending refresh 또는 raw fallback, 삭제
  자체로 유료 호출 없음.
- 선택 production 카드 출력에 줄 span이 있는지/없는지를 실제 경로로 단정하는 검사.

**결정 상태**
- 기존 합의 안의 코드 작업: 동의 gate·상한 유지, 비용 호출 전 멱등, 스캔 실패 ≠ 삭제.
  따라서 같은 content revision은 trigger가 달라도 유료 추출 1회이고, 동의 판정은 trigger별로
  유지한다(단계 2). 기존 계약이 줄 단위 출처를 요구하는 경로라면 production에 그 span을
  기록하는 것도 이 범위의 구현이다.
- **기존 계약의 확인**(새 정책 결정이 아님): (a) 카드 최신성 계약이 content(sha256·size)
  기준인지, mtimeNs까지 요구하는지 — 내용 기준이면 mtime은 진단으로 낮추는 수정이다.
  (b) production 위키 계약에 줄 단위 provenance가 이미 있는지 — 있으면 위 구현, 없으면
  topical 링크를 production으로 표기하지 않는 현재 원칙을 유지한다.

## 6. 간극 4 — 200 파일/5 MB를 넘는 선택 v2 raw+wiki 성장

**현재 증거**
- `scan_collections`는 모든 collection에 **하나의** 예산(`max_files=200`, `max_bytes=5_000_000`)
  을 쓰고(`laya_index_source_port.py:302-326`), 넘으면 `index_stale`("too many files"/
  "oversize")을 던진다. 스캔은 파일 전체를 메모리에 둔다.
- sync worker의 `prepare_candidate_batch_off_hook` 스캔이 같은 예외로 끝나 handler는 ACK하지
  않는다. recall의 `_v2_phase` 스캔도 매번 stale로 실패한다. 위키 카드가 쌓이면 raw가
  그대로여도 이 한도를 넘는다.
- 그 밖의 상한: `wiki_matches` 200/5 MB, vector `MAX_CANDIDATES` 20,000, `sync_full` claim
  backlog 64, 설치 초기 `MAX_INITIAL_FILES=200`(상위 작업자 소유), 추천 200/5 MB.

**빠진 연결**: 한도 초과를 stale과 다른 명시 상태로 다루고, 큰 source도 완전한 manifest의
generation으로 게시하는 경로.

**권장 최소 단계** (상한 값을 올리기만 하는 해법은 채택하지 않는다)
1. 상태 분리: 스캔 예산 초과를 `index_over_cap`으로 구분하고 worker 결과에 `over_cap`/
   `backlog`(진행 수 포함)를 기록한다. 같은 원인의 wake 재시도는 backoff로 묶어 루프를 막는다.
2. bounded 증분·배치 게시: 정렬된 경로 범위 단위로 스캔·hash·임베딩(캐시된 chunk set 재사용)
   을 wake마다 정해진 양만 진행하고 같은 후보 generation에 durable하게 쌓는다. **전체
   manifest가 모이고 게시 직전 재대조가 통과할 때만** head CAS로 한 번 게시한다. 중간 상태는
   게시하지 않는다(full membership 유지).
3. 검색: backlog/over_cap 동안 이전 generation을 "현재"로 내지 않는다. 기본은 명시 오류
   (`index_backlog`/`index_over_cap`). 제한 제공을 허용한다면 반환 행 각각의 파일 bytes를
   현재 파일과 대조한 행만, "갱신 중" 표시와 함께 낸다(아래 미결정).
4. query-time 최신성 증명은 hook에서 전체 재스캔이 불가능한 크기에서 bounded해야 한다.

**합성 수용 기준**
- 250 파일·6 MB(raw+wiki) fixture: 첫 wake들은 `backlog`(진행 수 증가, 재시작 뒤 이어감)이고
  head는 바뀌지 않는다. 마지막 wake가 250개 full membership의 generation 하나를 게시한다.
  중간에 파일이 바뀌면 그 회차는 게시하지 않고 재계획한다.
- backlog 동안 recall은 이전 generation 행을 현재로 표시하지 않는다(명시 reason).
- 확정 hard limit 초과: `over_cap` 기록, wake 루프 없음, DB·queue 무변경, 진단 1회.
- 스캔·배치 메모리는 회차 예산 안이다(전체 bytes 동시 보유 없음).

**결정 상태**
- 기존 합의 안의 코드 작업: generation마다 full membership, 낡은·부분 결과를 현재로 내지 않음,
  상한 조정만으로 해결하지 않음. 따라서 over-cap/backlog의 fail-safe 상태(단계 1)와 그 동안의
  기본 검색 동작 — 명시 `index_over_cap`/`index_backlog`, 거짓으로 최신인 결과 없음(단계 3) —
  은 바로 구현한다.
- **측정으로 정할 engineering 한도**: wake당 배치 크기·시간·메모리 예산, hook의 bounded 최신성
  증명 방식(전체 재스캔 대체). 합성·실측 결과로 정하고 문서에 근거를 남긴다.
- **설치 소유자와의 인터페이스 조율**: 설치 초기 `MAX_INITIAL_FILES`와 추천 200/5 MB를 선택
  route 한도와 맞출지(설치 쪽 변경은 상위 작업자 소유).
- **실제 미결 제품 정책**(합의된 범위를 넘을 때만): 지원 corpus 상한(hard limit)을 합의 범위
  밖으로 넓힐지, backlog 중 행별 검증된 부분 결과를 허용할지. 부분 결과는 명시 상태 표시가
  필수이며 정책 선택 전에는 기본값(명시 오류)을 유지한다. 상한 값을 올리는 것만으로 지원
  corpus를 넓히지 않는다.

## 7. 순서와 상태

권장 순서: 간극 1(stale ≠ over_cap 구분 포함) → 간극 4 → 간극 3 → 간극 2(설치 쪽 목록 합의
필요). 각 단계는 한 경계만 바꾸고 개발 가드 집중 회귀 뒤 이 문서와 경계 계획 TODO를 갱신한다.

CI·QA: PR #13 head `077f8186`의 CI(Node 22/24/26)는 통과했다. L1 설치본 QA와 실제
Claude/Codex 새 세션, 운영 runtime·모델 검증은 실행하지 않았다. 이 계획의 어떤 수용 기준도
그 QA를 대신하지 않는다.

## 8. 요약: 결정의 종류

대부분은 기존 합의 안의 구현·조율·확인이다. 사용자 정책 결정은 마지막 행에 한정한다.

| 구분 | 항목 |
|---|---|
| 기존 합의 안의 코드 작업 | 간극 1 비동기 stale→durable sync wake와 진단 분리. 간극 2 cwd 밖 편집·삭제·이동의 승인 root 소유자 routing(명시 승인, 자동 등록 없음). 간극 3 선택 경로 raw→위키 연결 검증, 같은 content revision의 cross-trigger 유료 추출 1회(trigger별 동의 유지), 기존 계약이 요구하는 곳의 production 출처 span. 간극 4 over_cap/backlog fail-safe 상태·루프 방지·bounded 배치 게시 |
| 설치 소유자와 인터페이스 조율 | 선택 route 소유자 목록(catalog)·proof의 출처와 소유자 wake API, 설치 초기·추천 상한 정합. `core/laya_product_install.py`는 별도 작업자가 수정 중 |
| 기존 계약의 확인 | 카드 최신성의 mtimeNs vs content hash·size, production 줄 단위 provenance 계약 유무 |
| 측정으로 정할 engineering 한도 | wake당 배치 크기·시간·메모리 예산, hook의 bounded 최신성 증명 방식 |
| 실제 미결 제품 정책 (합의 범위를 넘을 때만) | 지원 corpus 상한을 넓힐지, over-cap/backlog 중 검색이 명시 상태와 함께 부분 결과를 낼지. 권장 안전 기본값은 명시 `index_over_cap`/`index_backlog`이며 거짓으로 최신인 결과를 내지 않는다. 상한 값 인상만으로 지원 corpus를 넓히지 않는다 |

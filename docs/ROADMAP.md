# MemVane 로드맵

1.0.0 다음에 할 일을 릴리스 단위로 적는다. 열린 항목의 단일 목록이다. 끝난 항목은 지운다.

## 읽는 법

- **릴리스 번호.** `1.0.x`는 결함·안내·문서·시험과 저장 공간 유지 관리를 다루는 패치다. `1.1`·`1.2`·`1.3`은 기능을 더하는 마이너다. 둘 다 [1.x 불변 목록](dev/contexts/release.md)을 바꾸지 않는다.
- **불변 목록 판정.** 항목마다 「불변」 칸에 적는다. `없음`은 영속 기록·lock·wake kind·settings 키를 건드리지 않는다는 뜻이다. `확인`은 설계 단계에서 저촉 여부를 먼저 판정한다는 뜻이다. `저촉`은 2.0 항목이다.
- **버전 혼재.** 한 기계에서 Claude·Codex·Hermes 호스트가 서로 다른 1.x 버전을 로드하는 상태가 정상이다. writer 동작을 바꾸는 항목은 낮은 버전 호스트가 같은 store에 쓰는 경우를 설계에 넣는다.
- **크기.** S = 1~2일, M = 3~5일, L = 1주 이상이거나 설계 문서와 독립 리뷰가 먼저 필요하다.
- **모양 제약.** 새 lock·journal record·wake kind·hook action·settings 키·compile 큐 dedup 키는 사용자 결정 사항이다.

## 한눈에

| 릴리스 | 항목 | 크기 | 불변 |
|---|---|---|---|
| 1.0.1 | 1 PR19 resident 안전망과 vector page 예산 경계 | M | 없음 |
| 1.0.1 | 2 교체본이 검수에서 떨어진 카드의 주입(R6) | M | 확인 |
| 1.0.1 | 3 view 행 GC 활성화 | M | 없음 |
| 1.0.x | 3 운영 가시성 | M | 없음 |
| 1.0.x | 4 방어 심화 | S | 없음 |
| 1.0.x | 5 문서와 QA 계약 정합 | S | 없음 |
| 1.0.x | 6 Python CI 매트릭스 | S | 없음 |
| 1.0.x | 7 시험 공백 | M | 없음 |
| 1.0.x | 8 시험 속도 | M | 없음 |
| 1.1 | 1 `writerFloor` 인상 수단 | M | 확인 |
| 1.1 | 3 공유 content GC | L | 없음 |
| 1.1 | 4 진단과 deferral 정확성 | M | 없음 |
| 1.1 | 5 recall·wiki 품질 | M | 확인 |
| 1.1 | 6 설치 수명 주기 마무리 | M | 확인 |
| 1.1 | 7 도메인 `domain/` 층 실체화 | L | 없음 |
| 1.1 | 8 원문 소실 보류와 삭제 receipt | L | 확인 |
| 1.1 | 9 학습 runtime 증거와 릴리스 QA 실행 | M | 없음 |
| 1.2 | 1 admission 분리와 busy 사유 | M | 없음 |
| 1.2 | 2 시간 예산 | L | 확인 |
| 1.2 | 3 세션 위치와 무관한 recall | L | 확인 |
| 1.3 | 1 연결 worktree의 카드 수명 주기 | L | 확인 |
| 1.3 | 2 대시보드 편집·알림 | M | 확인 |
| 2.0 | 1 학습 큐와 dispatcher lease 분리 | L | 저촉 |
| 2.0 | 2 lock 범위 축소 | L | 저촉 |
| 2.0 | 3 같은 store 안의 provider 교체 | L | 저촉 여부 판정 |
| 2.0 | 4 영속 이름 정리 | M | 저촉 |
| 2.0 | 5 settings 키 추가가 필요한 기능 | — | 저촉 |

```
1.0.1  1 ─────────────────────────────┐ (같은 파일)
       2 ── 1.1-5                     └─ 1.2-2
       3 ── 1.1-3
1.0.x  6 · 7 · 8 (다른 항목의 회귀 검사 기반)
1.1    1 ── (chunker·writer 동작을 바꾸는 모든 릴리스)
       4 ── 1.2-1 ── 1.2-2 ── 2.0-1 · 2.0-2
       6 ── 1.3-1 · 1.3-2
       7 (다른 항목과 병행, 도메인 단위)
1.2    3 ── 1.3-1
```

## 1.0.1

### 1.0.1-1 PR19 resident 안전망과 vector page 예산 경계

- **목표.** resident service와 vector fill이 예산 경계에서 멈추거나 실패 예산을 잃지 않는다.
- **범위.** vector page 예산: member 안 Python 작업의 협력적 deadline, 전제 확인 뒤의 시간 검사, 마무리 예약 fsync 상한, `SQLITE_INTERRUPT` 뒤 `ROLLBACK TO` 계약. resident: warm attach 경로의 반복 defer cooldown, 재시작 뒤 실패 예산 보존, 연속 실패 카운터의 키 격리. 항목마다 합성 RED와 GREEN을 붙인다.
- **의존.** 없음. 1.2-2와 같은 파일(`memvane/resident/`)을 고친다.
- **크기.** M. **불변.** 없음(resident 프로토콜 `1`과 서비스 파일 이름을 유지한다).
- **열린 결정.** 착수 전에 대상 경계를 현재 코드와 대조해 남은 것만 범위로 확정한다.

### 1.0.1-2 R6: 교체본이 검수에서 떨어진 카드의 주입

- **현재 동작.** verified 카드의 원문이 바뀌고 재추출본이 `fail`·`inconclusive`를 받거나 extractor가 없으면, 디스크의 옛 카드는 바이트가 그대로라 trust를 유지하고 계속 주입된다. 재생성 재시도 간격은 7일(`REFRESH_RETRY_SECONDS`)이고 사유는 `verify-log.jsonl`에만 남는다.
- **목표.** 원문과 어긋난 것이 확인된 카드는 검수 통과 카드로 주입되지 않거나, 어긋남이 사용자에게 보인다.
- **범위.** 쓰는 쪽(sync ingest의 현재 원문 판정, 수명 주기 재시도)에서 다룬다. recall의 trust 규칙은 바꾸지 않는다. 유료 재호출 루프를 만들지 않는다.
- **의존.** 1.1-5(verify defer 종점)와 같은 worker를 고친다.
- **크기.** M. **불변.** 확인(카드 frontmatter 필드, `.memvane/compile/*.jsonl` 행 모양을 유지하는 방식만 1.x).
- **열린 결정.** 처리 방식: (a) 원문 해시 불일치가 확인된 카드의 색인 자격을 내린다, (b) 재시도 간격을 줄이고 SessionStart 한 줄로 알린다, (c) 알리기만 한다.

### 1.0.1-3 view 행 GC 활성화

- **현재 동작.** `ViewStore.collect_view_rows`는 정의와 시험만 있고 제품 호출부가 없다. 게시가 쌓일수록 view 행·delta·stage가 남는다.
- **목표.** view마다 현재 head와 직전 세대만 남는다.
- **범위.** sync owner·writer scope 안에서 게시가 정상 반환된 뒤 유계 batch로 호출한다. 실패·defer pass에서는 돌지 않는다. GC가 도는 pass의 recall 지연을 잰다.
- **의존.** 낮은 버전 호스트가 같은 store에 쓰는 혼재 상황에서 안전성을 설계·검증한다. 필요한 버전 게이트는 착수 전에 판정한다.
- **크기.** M. **불변.** 없음(DDL을 유지한다). **열린 결정.** 없음.

## 1.0.x 패치

### 1.0.x-3 운영 가시성

- **목표.** 멈춘 이유가 사용자에게 보인다.
- **범위.**
  - 디스크가 가득 찼을 때 wake worker가 곧바로 다시 뜨는 루프를 막고 `candidate_storage_full`을 안내한다.
  - 수명 주기 대량 삭제 가드가 보류한 run을 SessionStart 한 줄로 알린다(지금은 `wiki-lifecycle.log`뿐이다).
  - compile이 돌지 못하는 사유(`needs_extractor`, verify stamp 뒤 sync 요청 누락)를 안내한다.
  - 처음 보는 브랜치의 cold view로 recall이 비는 동안의 안내.
  - `provision-index-runtime` 재실행이 기존 설치를 `provider_identity_changed`로 만든다는 사전 경고.
  - 활성화 뒤 첫 backfill의 호스트 CLI 호출 규모를 setup 승인 단계에 적는다.
- **의존.** 없음. **크기.** M. **불변.** 없음. **열린 결정.** 없음.

### 1.0.x-4 방어 심화

- **범위.** fixture provider seam 게이트에 temp-root 조건을 더한다. reader 경로의 `journal_directory(create=True)`에 실행 위치 경로 모양 검사를 적용한다. journal `_open`의 `mode=ro` 예외가 전제하는 "같은 프로세스 동시 사용 없음"을 단정으로 고정한다. runtime digest 캐시의 한계(저장 매체 손상, 같은 UID의 변조)와 Apple libsqlite3의 hot journal 동작을 문서에 적는다.
- **의존.** 없음. **크기.** S. **불변.** 없음. **열린 결정.** 없음.

### 1.0.x-5 문서와 QA 계약 정합

- **범위.**
  - 사용자 문서의 state 경로에 `v1` 반영, 없는 진단 이름 제거, minimal settings 설명을 허용 키 5개에 맞춤, setup skill restore 문단의 `--state-root` 안내, `CONTRIBUTING.md`의 폴더 이름.
  - `docs/release.md`와 `docs/release-agent-qa.md`를 1.x 릴리스 절차에 맞춘다. 사용자 유예를 기록하는 상태값(`DEFERRED BY USER`)을 정의한다.
  - `memvane/journal/initial_build.py`의 `retired` docstring을 두 abandon 경로에 맞춘다.
  - `memvane/host/host_response.py` `_render_context`의 비위키 행 분기: 도달 가능성을 확인하고, 도달하면 `raw_row_in_selected_index` 거절로 바꾼다. header 문구를 "wiki cards"로 고친다.
  - 저장소 자체 위키(`.memvane/wiki/`)에서 없는 동작을 설명하는 카드를 수명 주기로 퇴역시킨다.
- **의존.** 없음. **크기.** S. **불변.** 없음.
- **열린 결정.** 패치 릴리스마다 설치본 agent QA를 돌리는지, 카나리로 대신하는지.

### 1.0.x-6 Python CI 매트릭스

- **목표.** 지원 하한(3.9)과 최신 Python에서 같은 검사가 돈다.
- **범위.** 로컬 전체 검사는 Python 3.9와 Node 한 버전으로 돈다. CI는 PR에서 기본 조합을, 주간 예약 실행에서 Python × Node 전체 조합을 병렬로 돈다. `.github/workflows/test.yml`에 Python 버전을 명시한다.
- **의존.** 없음. **크기.** S. **불변.** 없음.
- **열린 결정.** PR 기본 조합(3.9 한 줄을 PR에 넣을지), 주간 실행 실패의 알림 경로.

### 1.0.x-7 시험 공백

- **범위.**
  - 쓰기 때 현재 원문 판정: 크기만 다른 revision, 관측 오류, raw root 없음, receipt digest 직접 대조, 관측이 바뀐 뒤 재ingest, main과 연결 worktree의 서로 다른 원문.
  - 구조 가드: 함수 인자로 넘어간 handle의 내부 접근, handle 목록에 없는 객체의 공개 멤버.
  - 온보딩 중간 기록(plan·prepare 산출물)이 재부팅으로 `st_dev`만 바뀐 뒤 재개되는지.
  - Hermes 선택 route 분류 후 import 실패 시험: 부하가 높은 환경에서 분류 subprocess의 시간 초과·실행 오류와 hook 반환을 구분해 관측하고, 선택 이후 실패가 거절로 남는지 검증한다.
  - main CI 전체 조합의 연속 통과 기록.
- **의존.** 없음. **크기.** M. **불변.** 없음. **열린 결정.** 없음.

### 1.0.x-8 시험 속도

- **목표.** 로컬 전체 검사 시간을 20~30분으로 줄인다.
- **범위.** 자원 제한이 없는 일반 `guard --kind test`의 파일 병렬도를 3~4로 조정한다. 이 변경에 한해 보호 파일 `scripts/development_guard.py` 수정이 승인됐다. 실제 모델·백업 등 자원 제한이 있는 작업과 단독 실행 규칙은 유지한다. Node 22·24 × Python 3.9·3.13 전체 조합은 GitHub Actions에서 병렬로 실행하고, 로컬 전체 검사는 Python 3.9와 Node 한 버전으로 실행한다.
- **의존.** 1.0.x-6(CI 매트릭스). **크기.** M. **불변.** 없음. **열린 결정.** 파일 병렬도 3과 4 중 자원 관측에 맞는 값.

## 1.1

### 1.1-1 `writerFloor` 인상 수단

- **현재 동작.** 설치 기록의 `writer_floor`는 읽기 분기만 있다. 실행 버전이 floor보다 낮은 호스트는 recall만 하고 wake를 처리하지 않는다. floor를 올리는 명령이 없다.
- **목표.** writer 동작을 바꾸는 1.x 릴리스가 낮은 버전 호스트의 쓰기를 멈추게 할 수 있다.
- **범위.** floor를 올리는 진입점 하나, 올린 뒤 낮은 버전 호스트의 안내, 두 호스트가 번갈아 자기 recipe로 세대를 게시하는 경우(`laya_qmd_chunking.mjs`·`CHUNK_RULES` 변경)의 합성 시험.
- **의존.** 없음. 1.1-3과 chunker를 바꾸는 모든 릴리스의 선행이다.
- **크기.** M. **불변.** 확인(floor는 올리기만 하고 비교 규칙은 유지한다).
- **열린 결정.** 진입점 형태: setup의 명시 명령인지, 릴리스가 선언한 최소 버전으로 owner가 자동으로 올리는지.

### 1.1-3 공유 content GC

- **현재 동작.** 여러 view가 공유하는 blob 본문·admission·attestation·source fingerprint·chunk/embedding set·recipe/profile은 지우지 않는다. 교체된 카드 버전마다 약 11~14 KB(추정)가 남는다.
- **목표.** 어떤 view의 head·직전 세대에서도 닿지 않는 공유 내용을 회수한다.
- **범위.** 활성화 규칙:
  - R1. 선택 sync의 issued owner·writer scope 안에서 모든 게시·fill이 정상 반환된 뒤 같은 프로세스·스레드에서만 실행한다. 이후 메모리의 blob id·fill target을 재사용하지 않는다.
  - R2. journal이 선택한 store만 대상으로 하고 inode·incarnation·writer proof를 대조한다. 선택 해제 store와 연결 worktree route는 제외한다.
  - R3. 유계 `BEGIN IMMEDIATE`마다 모든 참조를 다시 확인하고 vector → embedding set → chunk → chunk set → attestation source → attestation → admission → blob 순으로 지운다. FK 위반·busy는 전체 rollback 뒤 defer한다.
  - R4. 모든 view의 head·직전, stage, delta, 진행·완료 fill 대상, store 밖 기록(compile intent), trust chain, `coverage=complete`의 암묵 참조에 닿는 blob을 보호한다. 살아 있는 blob의 모든 recipe/profile set을 보존한다.
  - R5. orphan prepared/stage는 head·epoch를 transaction에서 재확인한 뒤 유계 batch로 정리하고 crash 뒤 재개를 검증한다.
  - 합성 RED: chunk set commit과 embedding callback 사이의 GC, lexical head의 fill set과 `coverage=complete` 암묵 참조, 진행 중 prepare·fill 핸들.
  - 참조 없는 원문 지문과 소비가 끝난 compile intent 파일(`.consumed`·`.rejected`·`.superseded`)의 회수.
- **의존.** 1.0.1-3, 1.1-1.
- **크기.** L(설계 문서와 독립 리뷰 선행). **불변.** 없음(DDL과 compile intent 키 집합을 유지한다).
- **열린 결정.** 미게시·실패 캐시의 유예 기간. provider 교체 뒤 옛 recipe/profile set의 회수 규칙(2.0-3과 함께).

### 1.1-4 진단과 deferral 정확성

- **목표.** 거절·대기·실패가 이벤트에서 원인별로 구분된다.
- **범위.**
  - recall 거절 이벤트에 내부 고정 코드(`[a-z0-9_]{1,80}`)를 별도 필드로 남긴다. refusal code는 그대로 둔다.
  - main view가 게시된 pass는 연결 worktree view가 `pending`이어도 main 게시를 이벤트로 남기고, `pending`에 사유를 채운다(`schema3_ingest_backlog` 등).
  - lexical 0건 recall의 reason은 `no_candidates`다. vector 대기 note를 reason으로 올리지 않는다.
  - off-hook 자식 진입점 전체가 `ResourceDeferred`를 실패가 아닌 deferral로 끝내는지 전수 점검한다.
  - `mark_new_write`의 `normal_writer_deadline`이 sync에서 deferred로 분류되는지 확인한다. 실패 원장이 wake id마다 최신 한 건만 남기는 것도 함께 본다.
  - compile 중첩 worker의 실패 범주, `deferred:pending:unlabeled`의 attempt 계산, backlog 크기 필드.
  - `memvane:logs` 요약이 main 게시와 linked 대기를 따로 센다.
- **의존.** 없음. 1.2-1의 계측 기반이다.
- **크기.** M. **불변.** 없음(이벤트 로그 필드 추가는 원문·경로를 싣지 않는다).
- **열린 결정.** 내부 코드를 hook stderr에도 내는지.

### 1.1-5 recall·wiki 품질

- **범위.**
  - verify defer 루프의 종점: `source_changed_before_verify` 재시도 상태, `content_hash_mismatch`의 재컴파일 전환, `missing`의 전멸·일부·그 외 분리. 중복 enqueue를 만들지 않는다. `source_unreadable`의 원인을 먼저 진단한다.
  - vector에서 빠지는 카드: provider가 token window로 다시 쪼개는 카드가 하나라도 있으면 그 view가 lexical 세대로 남는다(`chunk_text_unreconstructable`). 재온보딩 뒤 실제 coverage를 재고 대책을 정한다.
  - 대형 설치의 provider 준비 중 recall, 긴 프롬프트의 `laya_selector_invalid_pool`.
  - 결정 2건: PostToolUse recall 주입(유지 / 링크·제목만 / 폐지), subagent 세션과 하네스 생성 프롬프트(`<task-notification>` 등)의 주입 기준.
- **의존.** 1.0.1-2, 1.1-4.
- **크기.** M. **불변.** 확인(`.memvane/compile/*.jsonl` 행에 재시도 상태를 싣는 방식은 행 모양 변경이다. 추가 키로 되는지 판정한다).
- **열린 결정.** 위 결정 2건. 판단 전에 이벤트 로그로 건수와 분량을 잰다.

### 1.1-6 설치 수명 주기 마무리

- **현재 동작.** `restore-index-origin`은 첫 쓰기 뒤에도 프로젝트 선택을 해제하고 옛 설치 디렉터리(`<state base v1>/installs/<id>`)를 남긴다. 그 디렉터리를 회수하는 명령은 없다. 레지스트리 reconcile은 SessionStart·setup·조회 때만 돈다.
- **목표.** 해제된 설치의 흔적을 명시 요청으로 회수할 수 있고, 레지스트리가 디스크 상태를 따라간다.
- **범위.** 해제된 설치 디렉터리의 미리보기와 승인 삭제(setup skill). uninstall이 지우는 범위(state, wiki 카드, 학습 상태)의 정의. 레지스트리: 새 설치 등록 뒤 남는 이전 tombstone, 미완료 backfill의 완료 기록, 조회 경로 Git 호출의 예산, 행 수 상한 전의 전체 읽기, 사라진 worktree 행. resident service 시작·주기 reconcile과 일시 소실 유예.
- **의존.** 없음. 1.3-1·1.3-2의 선행이다.
- **크기.** M. **불변.** 확인(레지스트리 DDL을 유지한다. resident에 reconcile 호출을 더하는 것은 프로토콜 변경이 아니다).
- **열린 결정.** unregister한 worktree view의 회수 시점. 같은 경로 재온보딩과 옛 설치 재사용 여부.

### 1.1-7 도메인 `domain/` 층 실체화

- **목표.** 대표 변경 과제를 "kernel + 그 도메인 폴더 + 다른 도메인 `api.py`"만 읽고 끝낼 때 읽는 줄 수가 약 1,400~1,600이다.
- **범위.** 동작 불변 리팩터를 도메인 단위로 나눠 한다.
  - 규칙 파일 추출: sync 게시 규칙, verify verdict 처리, recall outcome·reason(host → recall). worker 단일 파일(`wiki_verify_worker.py`, `product_sync_worker.py`)의 판정과 I/O 분리.
  - 반환 모양과 사유 코드를 `api.py` docstring 또는 `types.py` 상수로 올린다(`BlobStoreError` 사유, extractor 사유, selector 사유, `read_view_baseline` 반환).
  - `settings/types.py`의 wiki 정책 상수를 각 wiki 도메인으로 옮긴다.
  - 문자열로 다른 도메인 내부를 아는 곳 제거, `decision`·`proof` dict 타입화, kernel 어휘 정리, 도메인 README 보강.
  - store 헤더 읽기를 경로를 받는 store api 하나로, journal writer token과 reader 값 객체를 `types.py`와 가드 handle 목록에 올린다.
  - journal `build_*`와 `install.api.build_*`의 중복 계약을 하나로.
- **의존.** 없음. 함수 단위 `ast.dump` 대조와 도메인 판정 뒤집기 확인을 묶음마다 한다.
- **크기.** L(여러 릴리스에 걸친다). **불변.** 없음. **열린 결정.** 없음.

### 1.1-8 원문 소실 보류와 삭제 receipt

- **목표.** 원문이 사라진 카드의 보류 상태와 삭제 근거가 store에 남는다.
- **범위.** member presence `held_missing`의 writer, 수명 주기가 읽는 store API, delete intent와 `delete_receipts` writer, 관련 trust 판정. 형식 4 DDL에 `delete_receipts` 테이블과 `held_missing` 값이 이미 있다.
- **의존.** 1.1-1.
- **크기.** L. **불변.** 확인(행을 더 쓰기만 하면 1.x다. DDL이나 compile intent 필수 키가 바뀌면 2.0이다).
- **열린 결정.** 낮은 버전 호스트가 `held_missing` 행을 만났을 때의 읽기 동작.

### 1.1-9 학습 runtime 증거와 릴리스 QA 실행

- **범위.** 설치 후보에서 학습 중지·재개(stop pipe, `training_cursor.py`의 head slot·그룹 계획·RNG receipt)의 end-to-end 증거. 학습 자식과 색인 작업이 겹칠 때의 부하 측정. 첫 한정 학습 시험의 고정 항목(adapter·체크포인트·모델 해시, 합성 입력, 동의·grant, 소유 경로, device 프로필)과 `--kind model-test` 가드 아래 한 그룹 trial. 유예된 assertion 5개(`RQA-GIT-01`, `RQA-GIT-03`, `RQA-WIKI-01`의 유료 중복 호출 부분, `RQA-WIKI-03`, `RQA-RES-02`)의 실행.
- **의존.** 1.0.x-5(QA 계약 정합).
- **크기.** M. **불변.** 없음.
- **열린 결정.** 가드의 5초 샘플링이 peak를 놓칠 수 있다. 자식 내 계측을 먼저 넣을지.

## 1.2

### 1.2-1 admission 분리와 busy 사유

- **현재 동작.** 학습과 색인이 같은 wake 큐와 dispatcher lease를 쓴다. 학습 추론 슬롯이 차 있으면 sync·초기 빌드·recall 질의 admission이 `learning_*` 사유로 미뤄진다. provider·control·trainer 어느 게이트가 막아도 `learning_compute_busy`로 보고된다.
- **목표.** 기존 큐·wake kind·lock을 유지한 채, 학습이 recall·sync·초기 빌드를 막지 않고 busy 사유가 점유자를 밝힌다.
- **범위.** admission을 색인용과 학습용으로 나눈다. sync·초기 빌드·recall 경로에서 학습 수요 확인을 뺀다. busy·deferred 코드에 lane·점유자 종류·작업 id·점유 시간을 싣는다. 학습이 상위 수요에 한 step(5초 이하) 안에 양보한다. 학습 예약 메모리를 회수 가능으로 센다.
- **의존.** 1.1-4.
- **크기.** M. **불변.** 없음(큐·lease 분리는 2.0-1).
- **열린 결정.** 없음.

### 1.2-2 시간 예산

- **목표.** per-user lock을 10초 넘게 쥐는 단계가 없고, resident 교체 중 vector 폴백 구간이 5초 이하다.
- **범위.**
  - embed runtime 준비의 `npm ci`·모델 다운로드를 per-user lock 밖으로 옮긴다.
  - 선택 sync에 wall-clock 예산을 둔다. provider 호출 한 번(최대 270초) 동안의 sync owner·provider lane 점유를 줄인다.
  - resident의 5초 주기 소유 재증명과 compile·sync의 `owner_busy` 경합을 점유 시간·빈도로 계측한다.
  - resident 교체를 make-before-break로 바꾼다. 메모리 admission이 막으면 break-before-make로 돌아간다.
  - 30초 넘는 단계는 진행 이벤트를 남긴다.
- **의존.** 1.2-1, 1.0.1-1.
- **크기.** L. **불변.** 확인(resident 교체 절차가 `successor.lock`·프로토콜 `1`의 의미를 바꾸면 2.0이다).
- **열린 결정.** 계측 결과에 따라 2.0-2(lock 범위 축소)의 필요 여부를 정한다.

### 1.2-3 세션 위치와 무관한 recall

- **현재 동작.** route 선택은 hook payload의 `cwd`에서 위로만 올라가며 `.memvane/settings.json`을 찾는다. 선택된 A 세션에서 선택된 B 원문을 고치면 B 카드는 만들어지지만 그 세션의 recall은 A 색인만 쓴다. 미온보딩 루트(여러 프로젝트를 하위 폴더로 둔 monorepo 루트)에서 연 세션은 하위 선택 프로젝트를 고쳐도 카드도 recall도 없다. 프로젝트 밖 편집 대기 큐(64경로)를 넘는 알림은 full-scan wake만 offer된다.
- **목표.** hook이 도는 세션이면 세션을 어디서 열었든 작업 대상 선택 프로젝트의 compile이 돌고 그 카드가 주입된다.
- **범위.** 설계 문서와 독립 리뷰가 먼저다. 방향 후보: 편집 라우팅 대상 확대, 세션 작업 집합(session id별 TTL 기록), 하위 프로젝트 탐색, 다중 프로젝트 병합. 먼저 정할 계약: 프로젝트별 route 증명과 실패 격리, hook 예산, 프로젝트 간 주입의 기본 허용, 작업 집합의 저장 위치·수명, 선택 기준, 연결 worktree·preview·blocked 대상, 미온보딩 루트의 SessionStart 안내, 대기 큐 초과분의 보존.
- **검증 기준.** 미온보딩 monorepo 루트 세션에서 하위 선택 프로젝트 원문을 고치면 그 카드가 생기고 다음 프롬프트에 주입된다. A 세션에서 B 원문을 고치면 B 카드가 생기고 B 관련 프롬프트에 주입된다. 다중 질의가 hook 예산 안에 끝나고 한 프로젝트 실패가 다른 프로젝트 주입을 막지 않는다.
- **의존.** 레지스트리(1.1-6), 1.1-4, 1.1-5의 subagent 주입 결정.
- **크기.** L. **불변.** 확인(프로젝트별 opt-out을 settings 키로 두면 2.0-5다. 새 hook action·wake kind·lock이 필요하면 2.0이다).
- **열린 결정.** B 카드를 A 세션에 주입하는 것의 기본값. opt-out을 settings 밖(설치 기록, 사용자별 설정)에 둘 수 있는지.

## 1.3

### 1.3-1 연결 worktree의 카드 수명 주기

- **현재 동작.** 연결 worktree에서는 recall만 동작한다. compile·수명 주기·dedup scan은 main checkout에서만 돈다. worktree에서 고친 원문은 브랜치가 main에 merge되고 main checkout에서 수명 주기가 돌아야 카드에 반영된다.
- **목표.** worktree에서 작업하는 동안 그 브랜치 기준으로 compile과 수명 주기가 돈다.
- **범위.** 설계 문서와 독립 리뷰가 먼저다. 먼저 정할 계약: worktree writer의 route·binding 증명, 카드 위치와 정본, compile·verify 큐와 scan 상태의 위치, main writer와의 상호 배제, 브랜치 전환·rebase·merge·worktree 삭제 때의 대량 삭제 가드와 `git_branch_safety` 판정, 유료 호출 상한(worktree 수만큼 늘지 않는다), 사라진 worktree의 큐·상태·카드 회수. 상한을 넘은 pass의 원문 관측 보강과 manifest tracked 판정 통일.
- **의존.** 1.1-6, 1.2-3.
- **크기.** L. **불변.** 확인(writer 상호 배제가 새 lock을 요구하면 2.0이다).
- **열린 결정.** 카드 정본의 위치. member trust를 compile attestation 기준으로 고르는 규칙을 함께 넣을지.

### 1.3-2 대시보드 편집·알림

- **목표.** 로컬 대시보드에서 `skipPaths`를 고치고 건강 신호 알림과 학습 점수를 본다.
- **범위.** settings를 바꾸는 플러그인 진입점(권한·원자적 쓰기·route 증명 불변을 플러그인이 보장한다), 대시보드의 편집 화면, 알림, 학습 점수 실데이터, WAL DB가 필요한 지표(claim 큐 깊이, vector fill 진행), 카드 예산 초과·수명 주기 실행·온보딩 거절 기록, 플러그인 skill(`memvane:dashboard`).
- **의존.** 1.1-6(레지스트리가 목록 원천이다), 학습 프로필.
- **크기.** M. **불변.** 확인(편집 대상은 허용 키 5개 안이다).
- **열린 결정.** 대시보드가 settings 파일을 직접 고치는지, 플러그인 명령을 부르는지.

## 2.0 후보

1.x 불변 목록을 바꾸는 항목이다. 2.0은 state base가 달라지고 다시 온보딩하거나 그 메이저용 변환 명령이 필요하다.

### 2.0-1 학습 큐와 dispatcher lease 분리

학습 wake(`learning`·`auto`)를 별도 큐와 별도 dispatcher lease로 옮기고 control lock을 lane별로 나눈다. wake 큐 DB의 위치·DDL과 lock 이름이 바뀐다. 1.2-1 적용 뒤에도 학습이 색인 lane을 막는 것이 실측될 때 착수한다.

### 2.0-2 lock 범위 축소

per-user `provider.lock`을 설치 단위로, sync owner를 프로젝트 단위로 나눈다. 프로젝트 간 배제는 메모리·CPU admission으로 다룬다. 1.2-2의 계측으로 필요 여부를 정한다.

### 2.0-3 같은 store 안의 provider 교체

현재는 provider·모델·runtime이 바뀌면 fail-closed로 거절하고 다시 온보딩을 안내한다. 같은 store 안의 교체는 route·build·ledger·journal의 profile 결속을 journal 단일 권위로 바꾸는 일이다. journal 의미 변경인지 먼저 판정하고, 아니면 1.x로 내린다.

### 2.0-4 영속 이름 정리

1.x에 고정된 이름: `laya_local_embed_worker.mjs`·`laya_qmd_chunking.mjs`, wake kind `laya-sync`·`laya-compile`, lock `laya-install.lock`, store의 `verification_kind` 식별자(`memvane:v6-reader`, `legacy-lines-v1`), schema id의 `schema3`·`selected-main-schema3-v1`. 다음 메이저에서 한 번에 바꾼다.

### 2.0-5 settings 키 추가가 필요한 기능

허용 키 집합(`name`·`skipPaths`·`diagnostics`·`contextLearning`·`sharedLearning`)과 두 학습 블록의 필드는 1.x에서 닫혀 있다. 새 키가 필요한 기능은 여기에 모은다.

## 하지 않는 것

| 항목 | 이유 |
|---|---|
| frontmatter 없는 카드 writer | frontmatter 원문 지문을 유지한다. 읽기 규칙은 DB 우선, 없으면 frontmatter, 불일치면 DB + 이벤트다 |
| recall 쪽 trust 규칙 변경(같은 원문 경로를 다른 sha로 인용한 카드의 거절) | 현재성은 쓰는 쪽이 맡는다 |
| 새 lock으로 공유 content GC 보호 | 그 lock을 모르는 버전에는 효력이 없다 |
| store의 WAL 전환, 단일 FTS + 세대 필터 | rollback journal과 view별 head FTS를 유지한다 |
| retired-wiki 보관본 자동 삭제 | 자동 삭제가 없다 |
| 기존 카드 일괄 재작성, 지문 없는 카드의 소급 백필 | 명시 요청이 있을 때만 한다 |
| 다른 메이저의 state를 읽거나 옮기는 코드 | 메이저가 다르면 다시 온보딩한다 |

## 관찰

| 항목 | 다시 꺼낼 조건 |
|---|---|
| 중첩 프로젝트(선택된 프로젝트 안의 프로젝트) 온보딩 | 요청이 있을 때. 1.2-3의 하위 프로젝트 탐색과 함께 본다 |
| 다른 기계로 store 복사 | 요청이 있을 때. 지금은 기계마다 온보딩한다 |
| Hermes의 SessionStart 안내 통로, 편집 뒤 컨텍스트 주입 | Hermes가 해당 hook 반환값을 쓰게 될 때 |
| `npx skills add` 설치 진입점 | installer 수명 주기 설계가 선행한다 |
| 카드 유효 구간, 카드 간 모순 lint, 카드 관계, stale 전파, 주입 효과 평가 지표 | 사용자가 다시 다루기로 정할 때 |
| embed runtime 세대 GC 자동화 | `gc-index-runtime`(승인 시만 삭제)으로 부족할 때 |
| CPU 병목의 Rust 부분 도입 | 측정으로 병목이 확인될 때. 전체 재작성은 하지 않는다 |

## 열린 결정

1. 1.0.1-2의 처리 방식 (a)·(b)·(c).
2. 1.1-1 `writerFloor` 인상의 진입점 형태.
3. 1.1-5의 PostToolUse 주입과 subagent·하네스 프롬프트 주입 기준.
4. 2.0-3이 journal 의미 변경인지.
5. 1.2-3의 프로젝트 간 주입 기본값과 opt-out 위치.
6. 패치 릴리스의 QA 방식(설치본 agent QA / 카나리).
7. 프로젝트 settings의 `diagnostics`를 재온보딩 뒤에도 켜 둘지.

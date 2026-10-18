# 기능 경계와 점진적 분리 계획 (2026-10-17)

이 문서는 현재 코드의 의존성과 상태 소유권을 기록하고, 이후 변경의 경계를 정한다.
목표 구조는 설계안이다. 아래에서 **이번 변경**으로 표시한 부분 외에는 구현 완료를
뜻하지 않는다. 저장소 전체를 한 번에 재작성하거나 모든 함수에 포트·이벤트 버스를
씌우지 않는다.

**제품 방향 후속 결정:** 이 프로젝트의 QMD backend를 내부 작업명
[Laya Index](2026-10-17-embedded-sqlite-search-engine.md)의 SQLite 검색·색인
엔진으로 교체할 계획이다. 별도로 설치된 global QMD는 제거하지 않는다.
플러그인 소유 QMD runtime은 이관·검증·rollback backup 보존 후 ownership
manifest로 범위를 증명해 정리한다. 소유권이 불확실하면 삭제하지 않는다.
Laya 학습 모델과도 구분한다. 아래 QMD adapter·공유 DB 게시 항목은 현재
코드와 당시의 격리 원형을 설명한다. worktree 멤버십·공유 콘텐츠·세대 게시
책임과 [데이터 이전](2026-10-17-embedded-engine-data-migration.md)은 새 계획을
따른다. 운영 경로 변경은 아직 없다.

## 현재 구조 지도

| 기능 | 현재 진입점·구현 | 현재 주요 상태 |
|---|---|---|
| 검색·컨텍스트 주입 | `core/recall.py`, `qmd_project_query.py`, `qmd_route.py`, `wiki_freshness.py` | 선택된 QMD index와 프로젝트 route, recall 진단 로그 |
| 학습 데이터·평가·학습 | `core/context_learning/shared.py`, `corpus.py`, `local_cycle.py`, `live_select.py` | 사유화된 `shared-learning.sqlite3`, 후보·평가 기록, checkpoint 선택 pointer |
| 위키 생성·출처 검증 | `core/wiki_compile_enqueue.py`, `wiki_compile_worker.py`, `wiki_compile.py`, `wiki_freshness.py` | 프로젝트 `.auto-context/compile`의 source/verify/dedup queue·ledger와 `.auto-context/wiki` 카드 |
| 프로젝트·파일 동기화 | `core/sync.py`, `file_change_queue.py`, `index_enqueue.py` | collection snapshot, `.auto-context/file-changes`, index dirty queue |
| 설치·업데이트·마이그레이션 | `core/setup_entry.py`, `shared_qmd_setup.py`, `shared_install.py`, `qmd_shared_store.py`, `setup_guard.py` | 설치 journal, 생성 세대, store·runtime active pointer, scope 승인 |
| 런타임 운영 | `core/runtime_work.py`, `qmd_endpoint.py`, `context_learning/resource_coordination.py`, `backend/index_worker.sh` | `runtime-wakes.sqlite3`, writer/compute lease, endpoint·모델 프로세스 수명 |

### 상태 소유권 (코드 근거)

| 기능 | 쓰는 상태(쓰는 코드) | 다른 기능이 읽거나 쓰는 지점 |
|---|---|---|
| 파일 동기화 | 수동 sync snapshot `QMD_SYNC_STATE_DIR`(기본 `~/.config/qmd/sync-state/<sha>.json`), SessionStart snapshot `<shared store>/session-sync/`, lock `/tmp/qmd-sync.lock.d`·`<shared>/.session-sync.lock.d`(`sync.py`). Bash 경로 `.auto-context/file-changes/{pending,snapshot,status}.json`(`file_change_queue.py`) | SessionStart 경로가 `qmd_shared_store.select/root`로 설치 소유 store 위치를 읽는다. Bash 첫 baseline은 `index_snapshot.active_document_hashes`로 QMD 활성 경로·해시를 받는다 |
| 색인·검색 | dirty queue `~/.config/qmd/dirty-queue`(`dirty_queue.py`), QMD index(`backend/index_worker.sh`), owner 관측 ledger `<shared>/owner-observations.json`(`change_owner.py`) | `dirty_queue`가 `runtime_work`·`qmd_route`를 지연 import한다. 위키(`wiki_compile`, `wiki_verify_worker`, `wiki_dedup_resolve`)와 설치(`shared_install`)가 `dirty_queue`에 직접 쓴다. `index_enqueue → change_owner → qmd_shared_store` registry를 읽는다 |
| 위키 | `.auto-context/compile` source/verify/dedup queue(`wiki_compile_enqueue._safe_queue_path`), `.auto-context/wiki` 카드(`wiki_compile`) | `sync`·`file_change_queue`가 `enqueue_changed_sources`를 호출한다. `wiki_freshness`·`wiki_source_scan`·`wiki_extract` 등이 `recall`을 import(일부 지연)한다. `wiki_compile_worker`가 `context_learning.local_cycle.ResourceDeferred`를 import한다 |
| 학습 | `context_learning/*` 사유 DB·평가·checkpoint pointer | `recall.py`가 `context_learning.{seam,corpus,live_select,shared,adaptive_pool,…}`를 함수 안에서 지연 import한다. `posttool.py`, `edit_context.py`, `topical_hook_queue.py`도 직접 호출한다 |
| 설치 | `qmd_shared_store` root의 `active.json`, `generations/*/prepared.json`, `collections.json` registry, `bootstrap-journal.json` | `qmd_route`가 모듈 상단에서 `qmd_shared_store`를 import하고 `recall`, `file_change_queue`, `context_learning.corpus`, `update.sh`가 `qmd_route`를 거친다. `setup_guard`가 `shared_install`·`qmd_route`·`context_learning.synthetic_scope`를 지연 import한다 |
| 런타임 | `runtime-wakes.sqlite3`, writer/compute lease(`runtime_work.py`, `context_learning/resource_coordination.py`) | `runtime_work`가 모듈 상단에서 `context_learning.{resource_coordination,local_cycle,resident}`를 import한다. `qmd_endpoint`, `runtime_admission`, `runtime_monitor`, `orphan_reclaim`도 학습의 `ResourceDeferred`를 import한다 |

순서 결합의 근거는 `core/update.sh`의 worker 구간이다. embed 성공 뒤
`update_hook_queue.py readiness`, `sync.py --session-start`, `context_learning_synthetic_reconcile.py`,
`context_learning_auto.py launch-current`, `wiki_dedup_scan.py`, `orphan_reclaim.py`가
차례로 호출된다. `update_hook_queue`는 `file_change_queue`를 import한다. 이 지도는 import와
쓰기 지점을 읽어 만든 것이며 런타임 호출 그래프 전체를 실행으로 검증하지 않았다.

`core/config.py`는 정규화 정책과 파일 탐색·마이그레이션을 함께 담고 있다.
설정은 프로젝트/사용자가 소유하며, 설치 마이그레이션 외의 런타임 소비자가
임의로 덮어쓰지 않아야 한다. 영속화 헬퍼와 관측성은 여러 기능이 필요로 하지만,
하나의 큰 공통 서비스가 기능별 결정을 대신 소유해서는 안 된다.

## 지금 드러난 결합과 위험

- `sync.py`는 snapshot 비교 뒤 index dirty queue와 위키 source queue를 별도로
  호출한다. 이번 변경으로 위키 엔진·후보·상한·큐 쓰기는 위키 소유 모듈의 공개
  `enqueue_changed_sources`로 옮겼다. enqueue 이후 snapshot 기록 사이에 실패하면
  중복 재시도가 가능한 결합은 여전히 남아 있다.
- `file_change_queue.py`는 `sync.py`를 재사용하고, 첫 snapshot의 QMD 활성
  경로·해시는 `index_snapshot.py`에서 받는다. 이 읽기 경계가 생겨도 검색
  저장소 형식이 바뀔 때 adapter 계약을 검증해야 한다.
- `update.sh`의 한 worker가 QMD update/embed, SessionStart snapshot, 합성
  학습 reconcile, auto cycle, 위키 후속 처리를 순서대로 호출한다. 한 단계의
  실패·재시도·완료 표시가 다른 단계의 완료로 오해될 수 있다.
- `recall.py`는 검색 중 `context_learning`의 후보·선택·관측 경로를 호출한다.
  `runtime_work.py`는 공용 wake queue를 운영하면서 학습의 resource
  coordinator와 handler를 직접 가져온다. 현재 코드의 사실이며 곧바로 순환
  import가 존재한다는 뜻은 아니지만, 경계 변경 시 순환 의존 위험이 있다.
- `shared_install.py`는 설정·scope·vector 무결성·endpoint·scoped search를
  모두 확인한다. 설치 판정에 필요한 증거는 유지하되, 검색 구현 세부가 설치
  유스케이스의 입력 포트보다 넓게 노출되지 않도록 해야 한다.

## 목표 의존성 및 상태 소유권

호스트 hook·CLI는 진입 어댑터다. 유스케이스가 설정된 정책과 상태 전이를
결정하고, QMD·Git·LLM·OS·SQLite·파일시스템은 바깥 어댑터로 둔다.
도메인 값과 유스케이스는 구체적인 실행 파일·환경 변수·경로 형식에 직접
의존하지 않는다. 실제로 바꿀 가능성이 있는 외부 효과에만 작은 포트를 둔다.

| 소유 유스케이스 | 최소 입력/포트와 결과 | 소유하지 않는 것 |
|---|---|---|
| 파일 동기화 | 허용 scope의 파일 열거, 이전 snapshot 읽기/원자 저장, 생성·수정·삭제 change set 반환. Git CLI 호출 없이 현재 worktree 파일을 기준으로 한다. | 위키 카드 쓰기, 검색 결과 게시, 학습 실행 |
| 색인·검색 | change set에서 index 작업을 받아 ACK하고, 한 요청에 게시된 generation 하나를 읽는다. QMD query는 adapter다. | Git pull, 위키 소스 동의, checkpoint 승격 |
| 위키 | 명시된 trigger·원문 revision·출처 증거를 받아 source queue에 넣고 카드 검증·게시를 관리한다. | 파일 snapshot 소유, 자동 학습 동의 |
| 학습 | 승인된 관측과 고정 corpus/label을 받아 사유 DB·평가·checkpoint를 관리한다. | 파일 변경 즉시 실행, 검색 generation 변경 |
| 설치 | 검토된 요청과 증명으로 runtime/store generation을 준비·선택하고 마이그레이션 journal을 관리한다. | 검색 점수 정책, 위키 내용, 학습 결과 |
| 런타임 | 고정 kind의 wake, 자원 lease, endpoint·모델 수명과 재시작을 관리한다. 각 handler는 실행 직전 자기 기능의 승인·scope를 재확인한다. | 기능별 콘텐츠·동의 결정 |

파일 동기화가 색인과 위키에 전달할 값은 최소한 `project/worktree`,
`collection`, `relativePath`, `changeKind`, `contentRevision`, `scopeRevision`
및 scan 결과 상태다. 이 계약과 실제 스키마는 아직 구현하지 않았다. 현재
`file_change_plan.compare_collection`의 순수 비교와 collection 단위 dirty queue 계약은
점진적 분리의 출발점으로 재사용한다. 공용 설정은 정규화 값과 파일 읽기를
분리하고, 관측 기록은 각 기능의 소유 상태를 대체하지 않는 보조 증거로 둔다.

## 실패·재시도·취소의 경계

- 스캔 I/O 오류는 삭제가 아니다. 이번 변경의 `sync.scan_files`는 읽지 못한
  하위 경로를 따로 보고하고, 수동 sync·SessionStart의 `build_snapshot`은 그 경로의
  이전 상태를 보존한다. 루트 오류에서는 snapshot을 진전시키지 않는다.
  sync snapshot 자체가 손상됐거나 읽히지 않으면 원본을 보존하고 index·위키
  enqueue 없이 `state_unreadable`(종료 코드 3)로 끝난다. Bash
  `file_change_queue` handoff는 부분 오류도 회차 전체를 `deferred`로 남기고
  pending을 유지한다. 계속 읽을 수 없는 경로가 있으면 그 handoff는 진행하지 않는다.
  enqueue 뒤 snapshot 저장이 실패하면 중복 enqueue가 가능하다. 특히 위키
  source queue 이후 유료 추출의 완전한 멱등성은 아직 증명되지 않았다.
- 현재 SessionStart는 QMD update/embed 성공 뒤 snapshot을 처리한다. 기존
  readiness의 `completed`는 embed 완료로 먼저 기록되며 뒤따르는 sync dirty
  queue ACK나 현재 worktree 검색 최신성을 증명하지 않는다. 검색 직전 상태 확인,
  worktree별 게시 generation, 한 검색의 단일 generation 고정은 후속 구현이다.
- 위키 source queue는 `compile.mode`와 해당 trigger의 동의, 원문 revision,
  건수 상한을 자체적으로 확인해야 한다. snapshot 완료를 위키 게시나 유료
  extractor 완료로 간주하지 않는다. 삭제·rename의 기존 카드 처리도 별도
  provenance 계약이 필요하다.
- 학습 capture·평가·trainer·checkpoint 승격은 자체 승인과 멱등성 기록을
  유지한다. 기존 `autoCycle` 승인 프로젝트는 `update.sh`에서 QMD embed 성공
  뒤 launch-current를 시도한다. 이번 파일 감지 설정이 그 승인을 새로 켜지는 않는다.
- 준비 전 취소, 큐 등록 후 재시도, 게시 후 rollback은 각 소유자가 정의한다.
  하나의 전역 취소 이벤트나 무근거 자동 rollback을 새로 만들지 않는다.
  외부 호출이 있었으나 ACK가 불확실한 상태는 성공으로 표기하지 않는다.

## 이번 변경의 최소 분리와 단계적 이전

**이번 변경:** `sessionStartSync`를 기본 ON인 파일 감지 설정으로 두고 명시적
`false`를 존중한다. bool이 아닌 값은 진단과 함께 OFF로 처리한다. OFF이면
SessionStart 위키 enqueue도 실행되지 않는다. 기존 `session_start_source`는
위키 컴파일 enqueue 동의로
남긴다. 따라서 compile이 OFF인 프로젝트의 변경 감지는 실행되지만 위키
생성·`capture`·`autoCycle`은 새로 활성화되지 않는다. 합성 Git pull·branch
전환의 생성/수정/삭제와 I/O 오류 회귀를 추가했다. 위키 후보·engine·상한·큐
쓰기는 `wiki_compile_enqueue.enqueue_changed_sources`가 소유한다. 파일 sync는
변경 집합만 전달하고 위키가 반환한 deferred 항목으로 자기 snapshot만 조정한다.
`qmd_shared_store.select`는 기존 pointer를 읽어 검증하며 생성·선택하지 않는다.
SessionStart의 상태 경로는 호출자 환경 변수에 쓰지 않고 로컬 값으로 전달한다.
index enqueue와 위키 enqueue의 순서 결합은 남아 있으며 전체 경계 분리는 아니다.

1. 현재 snapshot·dirty queue·compile queue 순서와 crash 재시도 결과를
   검증한다. 수동 sync, SessionStart, PostToolUse, `file_change_queue`는
   각자 원문을 위키 큐에 넣으므로 같은 revision의 중복 등록이 가능하다.
   수동 sync와 SessionStart 조합은 합성 회귀로 확인했다. **후속 목표:** 위키
   소유 큐의 원문 revision 멱등성으로 이를 막는다. 문서의 목표 상태를 현재
   제품 보증으로 바꾸지 않는다.
2. `sync.py`의 비교·변경 계획을 외부 효과 없는 함수로 추출한다. 파일 열거와
   snapshot 저장, index enqueue, wiki enqueue를 좁은 adapter로 빼되 기존
   상태 형식과 ACK를 보존한다. 새 범용 이벤트 버스는 만들지 않는다.
   첫 인터페이스는 아래 `file_change_plan`으로 들어갔다. QMD index 읽기는
   `index_snapshot`의 좁은 읽기 경계로 분리했다.
3. 검색 계층에 worktree별 게시 generation과 query-time freshness 판정을
   도입한다. raw 즉시 fallback, 콘텐츠·파서·청킹·모델 버전별 캐시 재사용,
   rename 재사용은 각각 증명·성능 검증을 거쳐 추가한다.
4. 위키의 전체 원문 의존 집합을 버전화하고 추가·삭제·rename 때 공유 카드
   재사용 판정을 검증한다. worktree view 삭제와 공유 캐시 GC를 분리한다.
5. 학습 소비자는 게시된 corpus 증거만 받게 하고 검색 hot path의 학습 구현
   import를 얇은 선택 포트 뒤로 이동한다. 런타임 wake handler와 자원
   coordinator도 기능별 handler 계약으로 좁힌다.
6. 설치·복구는 선택된 store의 누락 proof를 직접 생성하지 않고, 비활성
   generation에서 검증 후 별도 승인으로 전환하는 경로를 설계·시험한다.

### 2026-10-17 작은 경계 추출

`plan_collection`은 이전 파일 상태, 이번 스캔, 읽기 실패 경로만 받아
보존할 이전 항목과 생성·수정·삭제 계획을 부작용 없이 반환한다. SessionStart/수동
`build_snapshot`과 Bash 변경 worker가 같은 비교 정책을 사용한다.

**이번 변경(2단계의 첫 인터페이스):** `core/file_change_plan.py`를 새로 두었다.
import가 없는 모듈이며 두 부분으로 나뉜다.

- 순수 계획: `compare_collection`, `plan_collection`, `defer_snapshot_entries`를
  `sync.py`에서 옮겼고 `sync`는 같은 함수 객체를 다시 노출한다. 새
  `plan_change_set(entries, is_indexed=None)`는
  `(name, root, previous_files, scanned_files, unknown)` 목록을 받아
  `{collections, totals, changedFiles, indexTargets}`를 반환한다. 입력 순서를
  보존해 위키 상한이 같은 순서로 적용된다. `source` role은 `changedFiles`에만 들어간다.
- 효과 순서: `apply_change_set(index_targets, changed_files, snapshot, previous, *,
  enqueue_index, enqueue_wiki, persist, ack=None)`가 index enqueue → 위키 enqueue →
  위키가 미룬 항목의 snapshot 되돌림 → persist → ACK 순서를 한 곳에서 소유한다.
  각 효과는 호출자가 넘기는 callable adapter다. persist 전 실패는 snapshot과 ACK를
  진전시키지 않는다.

`sync.build_snapshot`은 스캔(I/O·missing root 경고)만 하고 계획은
`plan_change_set`에 맡긴다. `sync.run`과 `file_change_queue.worker`는 기존
`dirty_queue.enqueue_project_collections`, `wiki_compile_enqueue.enqueue_changed_sources`,
`write_state_atomic`/`_write`, pending.json ACK를 adapter로 넘긴다. snapshot·status·queue
형식, 첫 SessionStart의 index 생략(`enqueue_index=None`), Bash worker의 scope 재확인과
status·kick 위치는 바꾸지 않았다. 합성 검사는 `test/file-change-plan.test.mjs`
(`test/fixtures/file-change-plan-checks.py`)에 있다. 개발 가드로 신규 계획 시험과
기존 file-change, SessionStart, sync/compile 회귀를 순차 실행해 47/47 통과했고
가드는 `settled:true`였다(receipt `0ae034bac0b44932b2c6e16f7dd823ed`).

**추가된 검색/색인 읽기 경계:** `index_snapshot.active_document_hashes`가 선택
route와 QMD `documents` 스키마를 소유한다. Bash worker는 collection별 상대
경로·해시만 받아 이전 snapshot 메타데이터와 비교한다. 읽기 실패는 그대로
worker의 pending/deferred 경로로 가고 삭제로 바꾸지 않는다. ownership 판단은
`change_owner`에, 결과 신선도는 `qmd_scope`와 `published_view`에 남아 있다.
이 adapter 추출 뒤 파일 변경 계획과 Bash 변경 worker 합성 회귀는 17/17
통과했고 가드는 `settled:true`였다(receipt `3429943bdb0f45b2b42682189dc7afae`).
이 추출은 여섯 기능 전체의
포트 전환이나 위키 queue 멱등성 완료를 의미하지 않으며, enqueue 뒤 persist 실패 시
중복 enqueue가 가능한 점도 그대로다.

### 여섯 영역의 다음 코드 경계와 순서

아래 순서는 현재 import·상태 쓰기 지점을 기준으로 한 작은 변경 단위다. 각 단계는
기존 파일 형식, 실패 코드, 동의 판단과 ACK 순서를 유지한 채 독립 회귀를 먼저
통과해야 한다. 한꺼번에 저장소 구조나 hook 계약을 바꾸지 않는다.

| 영역 | 다음 추출 단위와 호출 계약 | 먼저 고정할 회귀·남은 결정 |
|---|---|---|
| 파일 동기화 | `file_change_plan`이 만든 변경 집합에 원문 revision·scope revision을 덧붙이는 **값 변환**을 둔다. `sync.run`과 `file_change_queue.worker`의 기존 snapshot 저장·pending ACK는 그대로 호출자가 소유한다. | 스캔 실패는 삭제가 아니고, enqueue 뒤 persist 실패는 재시도된다. 위키 큐 멱등성이 생기기 전 중복 enqueue 가능성을 없앴다고 선언하지 않는다. |
| 색인·검색 | `index_snapshot`의 읽기와 `shadow_index.backup_inactive`의 후보 사본을 좁은 adapter로 유지한다. `published_view`는 요청 identity와 CAS 판단을 소유한다. 다음 운영 단계에서 `backend/index_worker.sh`와 `update.sh`의 모든 writer가 후보 세대만 쓰도록 연결한다. | `shadow_index.publish_candidate`는 별도 private pointer에서만 동작한다. 실제 공유 `active.json`, endpoint, queue claim, 검색 요청 pin의 연결·복구는 [게시 옵션](2026-10-17-qmd-published-view-options.md)의 구조 선택 뒤 검증한다. |
| 위키 | `wiki_compile_enqueue.enqueue_changed_sources` 입구에서 `(owner, collection, relative path, content revision, trigger)` 멱등 키를 계산하고 source queue/ledger의 기존 상태 전이 안에서 중복만 합친다. 카드 작성과 verify/dedup queue는 현재 소유 모듈에 둔다. | 동일 revision의 수동 sync·SessionStart·PostToolUse 중복, enqueue 뒤 snapshot 실패, 삭제·rename 재시도와 기존 카드 출처 보존을 먼저 합성 검증한다. 비용이 드는 extractor 호출은 이 단계에서 실행하지 않는다. |
| 학습 | `recall.py`의 지연 import 중 읽기 전용 후보 선택(`context_learning.live_select`·`corpus`)을 하나의 얇은 선택 함수 뒤로 묶는다. capture·`shared.enqueue`·`shared.run_training_pending`은 별도 승인과 사유 DB의 현 계약을 유지한다. | 미동의/후보 없음/오래된 corpus에서 기존 raw 검색 결과와 진단이 같은지 비교한다. 학습 실행이나 checkpoint 승격은 경계 추출의 시험 대상이 아니다. |
| 설치 | `shared_install._source_snapshot`·`_vector_report`의 증거 수집 결과를 좁은 값으로 만들어 `_install`의 journal 판정에 전달한다. `qmd_shared_store.stage_empty/activate_empty`와 `setup_guard`의 상태 코드는 유지한다. | 승인 scope 변경, worker deadline, 중단 후 재개, 기존 store 무변경을 `shared-install` 사례별 회귀로 고정한다. 기존 최초 활성화와 반복 검색 세대 게시는 다른 계약으로 둔다. |
| 런타임 | `runtime_work.dispatch`의 고정 kind handler를 각 소유 기능의 작은 함수로 나누되 `WakeQueue` SQLite schema·revision ACK와 `ResourceDeferred` 반환 의미를 유지한다. 공통 runtime은 lease·재시도만 판정한다. | index/update/cleanup/learning/auto 각 handler의 실행 직전 승인 재확인과 busy·불확실 ACK를 합성 검증한다. 자식 소유 PID와 endpoint 확인 없이 종료 범위를 넓히지 않는다. |

순서는 파일 변경 값 계약 → 위키 멱등 경계 → 검색 후보/게시 계약 → 학습 읽기
포트 → 설치 증거 포트 → 런타임 handler다. 검색의 실제 게시 연결은 공유 DB
writer·daemon 전환과 함께 별도 릴리즈 단계로 잡는다. 아래의 작은 순수 판단
추출은 들어갔지만, 위 표의 revision 멱등성·운영 게시·기능별 handler 분리는
여전히 작업 계획이다.

### 학습·위키·설치·런타임의 좁은 판단 경계 (2026-10-17)

Claude가 각 영역의 소스 파일을 별도 소유해 작성한 뒤 통합 회귀로 확인했다.
새 모듈은 상태 전이나 외부 효과를 소유하지 않는다.

| 영역 | 들어간 코드 경계 | 호출자가 계속 소유하는 것 |
|---|---|---|
| 학습 | `context_learning/selection_port.py`가 recall의 indexed revision, wiki 후보 거절, 후보 row의 경로·snapshot·hash 검증 및 선택 ID 매핑을 계산한다. | `recall.py`의 상태 경로, `corpus_snapshot`, `choose`, 진단과 fallback. `_private_state(create=True)`는 여전히 query 중 디렉터리를 만들 수 있다. capture·trainer는 이동하지 않았다. |
| 위키 | `wiki_source_intent.py`가 trigger label/허용 집합, 변경 경로 순서, cap 분할과 회차 내 중복 키를 결정한다. | `wiki_compile_enqueue.py`의 동의 gate, 파일 적격성, 환경 상한, source queue append. 입력에 content revision이 없어 여러 회차의 중복 제거는 구현하지 않았다. |
| 설치 | `install_evidence.py`가 색인 대상, 파일 identity, source queue 이름, 단일 모델 fingerprint, scoped search hit의 증거 판정을 수행한다. | `shared_install.py`의 proof 읽기, journal intent→enqueue→ACK, 승인·deadline·벡터 복구·endpoint·검색 실행. |
| 런타임 | `runtime_wake_policy.py`가 wake 검증·키·용량·우선순위·재시도·ACK 결정을 수행한다. | `runtime_work.py`의 SQLite claim/ACK, lease, 자원 admission과 실제 handler. 기존 `runtime_work.MAX_WAKES` override는 값을 정책 함수에 전달해 유지한다. |

새 경계의 네 합성 fixture와 영향 영역 집중 검사에서 처음 발견한 실패는
새 학습 fixture의 tuple-key dict 생성 오류와 런타임 `MAX_WAKES` override
호환 한 건이었다. fixture 및 호출 계약을 수정한 뒤 런타임 집중 검사 2/2,
전체 개발 가드 `npm test` **1,584건 중 1,582 PASS·0 FAIL·2 SKIP**,
`passed/settled:true`로 끝났다(run `42d0c8947e4f48f5b7b187cd61d3ec1f`).
PyYAML은 테스트 전용 venv의 site-packages만 시스템 Python의 `PYTHONPATH`에
제공했다. 운영 학습·모델·QMD 세션과 실제 위키 데이터는 검증 대상이 아니다.

### 남은 결합의 실행 우선순위

| 우선 | 남은 결합과 완료 기준 | 현재 상태 |
|---|---|---|
| 1. 색인·검색 게시 | 승인된 store/worktree revision의 비활성 후보만 QMD writer가 쓰고, 후보 daemon 준비·요청 세대 pin·late worker 거절·queue ACK 복구가 한 게시 계약으로 연결돼야 한다. | C2b에서 private 후보 설정·SQLite 게시·ACK callback·이전 게시 DB source 연속성을 검증했다. C2c에서는 게시 전 receipt를 받는 source callback, 불확실 ACK의 순수 판정, recall의 선택 route/URL pin을 추가했다. `backend/index_worker.sh`·`update.sh`와 공유 pointer/endpoint는 그대로여서 운영 경계는 미완료. [C2 옵션](2026-10-17-qmd-published-view-options.md)의 A/B 저장 구조 선택·전환 승인 필요. 이후 선택 경로의 저장 구조는 A-int 단일 SQLite로 확정됐고 Stage 6은 QMD 후보 writer 대신 A-int producer 위에서 진행한다(6a 소스 완료·집중 가드 회귀 6/6 통과, 6b 소스 완료·집중 가드 회귀 Node 6/6 통과, 둘 다 PR #13 head `077f8186` CI(Node 22/24/26) 통과, L1 설치본·운영 QA 미실행; 아래 Stage 6 절). |
| 2. 파일 동기화 → 위키 비용 경계 | change set이 content/scope revision을 위키에 넘기고, 같은 원문 revision의 수동 sync·SessionStart·PostToolUse enqueue가 하나의 유료 작업으로 합쳐져야 한다. | `file_change_plan`과 `wiki_source_intent`의 순수 순서·상한 분리는 완료. Stage 3(집중 가드 회귀 통과)에서 hash 스캔 경로만 content revision을 넘기고, live 큐에 남은 같은 `(collection, path, revision, trigger)` 행만 건너뛴다. 처리된 행과의 cross-run 멱등성, PostToolUse·수동 sync 병합, scope revision은 미완료. |
| 3. 런타임 → 학습 실행 | wake kind별 handler가 실행 직전 승인·scope를 재확인하고 공통 queue는 lease·재시도·revision ACK만 소유해야 한다. | `runtime_wake_policy` 결정 추출과 recall의 읽기 전용 `selection_port`는 완료. kind별 handler는 Stage 2에서 `runtime_handlers`로 분리했다(집중 가드 회귀 통과). Stage 2b(소스 변경 완료, 집중 가드 회귀 통과 Node 35/35·중첩 Python 묶음)에서 `runtime_work` 모듈 상단의 학습 패키지 import를 중립 `runtime_lease`로 바꿨다. 실제 `Coordinator`·`default_root`·`sample`은 여전히 호출 시 학습 패키지를 적재하고, `recall`의 학습 상태·실행 import 결합도 남아 있다. |
| 4. 설치와 검색 준비 | 설치 journal은 증명된 후보·scope 결과만 받아야 하며 반복 검색 게시와 최초 빈 store 활성화는 별도 계약이어야 한다. | `install_evidence` 판정 추출은 완료(Stage 5에서 남은 source identity·벡터 결함 판정까지 이동, 집중 가드 회귀 31/31 통과). 증거 수집 IO·벡터 복구·endpoint 준비는 `shared_install`에 남는다. 반복 검색 게시 경로는 설치에 없으며 연결은 남아 있다. |

1순위의 소스 단계로 `qmd_candidate_config`, `shadow_publish_flow`,
`shadow_qmd_adapter`를 추가했다. 새·기존 C1/C2a 집중 회귀 5/5 통과,
가드 `settled:true`(run `45a44ae39cbd4091ba33c347c0936b7c`).
실제 QMD·모델·운영 DB·pointer는 실행하거나 변경하지 않았다. 이 완료
기준은 격리 adapter에만 해당하며 운영 RQA-RACE-01은 계속 BLOCKED다.

C2c에서 `request_view_pin`을 `recall.py`의 실제 선택 경로에 연결했고,
`shadow_ack_policy`를 private flow의 선택적 게시 전 receipt callback과
격리 fixture로 연결했다. recall/게시/ACK 집중 회귀는 각각 57/57 및
4/4 통과했다(가드 run `a2d120f02e294d1f98354d923c647d97`,
`da996f209c3f4be2bd4753fb87fc7b3a`; 둘 다 `settled:true`).
운영 queue claim의 조건부 ACK와 요청 전체에 immutable DB를 제공하는
daemon cutover는 아직 구현하지 않았다.

### 2026-10-18 단계별 TODO·상태

위 기록을 대체하지 않고 현재 코드 기준으로 요약한다. "분리됨"은 해당 모듈이
존재하고 호출자가 쓰고 있다는 뜻이며 운영 경계 완료를 뜻하지 않는다.

CI 기준(2026-10-18 정정): Stage 1–6을 포함한 정확한 head `077f8186`에서 PR #13 CI
(Node 22/24/26)가 통과했다. 아래 Stage 2b·6a·6b 상태의 CI 표기는 이 결과를 반영한다.
단계별 "집중 가드" 수치는 개발 가드의 집중 회귀 결과다. CI 통과는 합성 회귀의 통과이며
L1 설치본 QA, 운영 runtime·endpoint·설치 QA, 원래 기능의 end-to-end 동작은 여전히
실행·검증하지 않았다.

Stage 1–6 완료는 경계 분리의 완료이며 원래 기능의 end-to-end 완료가 아니다. stale→sync
wake, cwd와 독립된 소유권, 위키 cross-trigger·claim 뒤 비용 dedup·출처 최신성, 200 파일/5 MB
초과 성장의 네 간극은 [원래 기능 간극 정리 계획](2026-10-18-original-function-gap-closure.md)이
다룬다.

| 영역 | 이미 분리된 모듈 | 남은 작업 |
|---|---|---|
| 검색·컨텍스트 | `index_snapshot`, `published_view`, `shadow_index`, `request_view_pin`(recall 연결), `context_learning/selection_port` | Stage 6b v2 product sync worker 연결의 L1 설치본·운영 QA(집중 가드 회귀와 PR #13 head `077f8186` CI는 통과), recall의 학습 지연 import 축소. 비선택 QMD 프로젝트의 `backend/index_worker.sh`·`update.sh`·공유 pointer/endpoint는 그대로다 |
| 학습 데이터·평가·학습 | `selection_port`(읽기 전용 판단), `resource_deferred`(Stage 1, 비학습 소비자의 공용 예외) | `runtime_work`의 학습 coordinator·handler 직접 import, capture·trainer 경계 |
| 위키·출처 | `wiki_source_intent`(`revision_key`, Stage 3), `wiki_compile_enqueue.enqueue_changed_sources` | 이미 claim·처리된 행과의 멱등성, revision 없는 PostToolUse·수동 sync 병합, 삭제·rename 카드 provenance |
| 파일·프로젝트 동기화 | `file_change_plan`(순수 계획·효과 순서, hash 스캔의 `revisions`) | scope revision 부여. enqueue 뒤 persist 실패의 재시도는 행이 live 큐에 남아 있을 때만 중복 enqueue를 피한다 |
| 설치·마이그레이션 | `install_evidence`(Stage 5에서 source identity·벡터 결함 판정 추가, 집중 가드 회귀 31/31 통과) | 반복 검색 게시 경로는 없으며 Stage 6 게시 연결과 함께 다룬다. 증거 수집 IO·벡터 복구·endpoint 준비 순서는 의도적으로 `shared_install`에 남긴다 |
| 런타임 큐·자원·모델 실행 | `runtime_wake_policy`, `resource_deferred`(Stage 1), `runtime_handlers`(Stage 2, 집중 가드 회귀 통과), `runtime_lease`(Stage 2b, 소스 완료·집중 가드 회귀 통과) | 실제 `Coordinator`·`default_root`·`decision`·`TrainingGuard`·`sample`·CPU budget·`service_root`는 학습 패키지에 남아 호출 시 적재된다 |

**Stage 1 (상태: 소스 변경 완료, 집중 가드 회귀 통과, 구 head `4279feb`의 PR #13 CI 통과):** `ResourceDeferred`만
`core/context_learning/local_cycle.py`에서 새 `core/resource_deferred.py`로 옮긴다.
생성자 `(reason, report=None)`, `ValueError` 하위 클래스, `report` 기본값 `{}` 의미는
그대로 두고 `local_cycle`은 같은 클래스 객체를 다시 노출한다. 비학습 소비자
(`runtime_work`, `runtime_admission`, `runtime_monitor`, `runtime_cpu`, `qmd_endpoint`,
`orphan_reclaim`, `wiki_compile_worker`, `extractors/lib`, `setup_entry`,
`laya_product_install`, `laya_product_sync_worker`)만 새 모듈에서 import하고, 지연
import 위치는 유지한다. `context_learning/*` 내부 import와 `_atomic_json` 등 다른
`local_cycle` import는 바꾸지 않는다. 종료 코드·예외 의미는 바뀌지 않는다.

구현 기록: `local_cycle`은 `from resource_deferred import ResourceDeferred`로 같은
클래스를 노출하므로 기존 `local_cycle.ResourceDeferred` 사용과 `isinstance`/`except`
판정은 동일하다. 클래스 `__module__`만 `resource_deferred`로 바뀌며 core에서 이를
읽는 코드는 없다. 모듈 상단 import 5곳(`runtime_work`, `runtime_admission`,
`runtime_monitor`, `wiki_compile_worker`, `laya_product_sync_worker`)과
함수 안 지연 import(`runtime_cpu`, `qmd_endpoint` 3곳, `orphan_reclaim` 2곳,
`extractors/lib`, `setup_entry`, `laya_product_install`)를 위치 그대로 바꿨다.
`runtime_work`·`runtime_admission`의 `_atomic_json` import와 학습 내부 10개 모듈의
`ResourceDeferred` 사용은 그대로다. `wiki_compile_worker`는 이제 import 시
`context_learning`을 적재하지 않는다. 테스트 파일은 여전히 `local_cycle`에서
import하며 재노출로 호환된다.

검증 기록: Claude가 개발 가드로 집중 회귀 두 묶음을 실행했다. A 묶음(runtime-resources,
runtime-wake-policy, qmd-endpoint, wiki-compile-worker, laya-product-sync-worker,
orphan-reclaim, context-learning 7개 파일)은 Node 93/93과 Python 117건, B 묶음
(resource-policy, shared-install, wiki-extractors, extractor-dispatch-guard,
laya-product-bundle 5개 파일)은 Node 56/56과 Python 그룹 19·25·20을 통과했다. 두 가드
checkpoint 모두 completed/settled/passed, 종료 코드 0이었다. 이후 Stage 1 커밋 `4279feb`를
head로 한 PR #13의 CI(Node 22/24/26)가 모두 통과했다.

**Stage 2 (상태: 소스 변경 완료, 집중 가드 회귀 통과 Node 35/35·Python 묶음 통과):** `runtime_work.dispatch`의 kind별
본문을 새 `core/runtime_handlers.py`의 함수(`index`, `update`, `laya_sync`,
`laya_compile`, `learning`, `auto`, `cleanup`)로 옮기고 고정 `HANDLERS` 표로 고른다.
본문은 그대로 옮겼다. 같은 `core/` 디렉터리라 `index_worker.sh`·`orphan_reclaim.py`
경로가 같고, 함수 안 지연 import, 실행 직전 `setup_guard` 재확인(learning은
`worker_lease` 안에서 한 번 더), 자식 env(`QMD_RUNTIME_DISPATCH=1`, cleanup의 route
키), 반환 boolean과 `invalid_runtime_handler` 예외도 같다. `runtime_work`는 같은
`dispatch` 객체를 다시 노출하므로 `drain(execute=dispatch)` 기본값과
`runtime_work.dispatch` 외부 호출자는 그대로다. learning/auto만 compute admission을
기다리는 kind 판정은 순수 함수 `runtime_wake_policy.needs_compute_admission`으로
옮겼고, 표본 수집·`Coordinator.check`는 `drain`에 남았다. 쓰지 않던 `decision`
import만 지웠다. `runtime_handlers` import는 `context_learning`을 적재하지 않으며
새 `test/runtime_handlers_test.py`가 별도 인터프리터에서 이를 고정한다.

이 단계는 원래 표의 "모듈 상단 `context_learning` import를 handler 안쪽으로 좁힌다"를
완료하지 않는다. `Coordinator`·`default_root`·`_lock`은 `foreground`·`guarded_run`·
`drain`·`wake`가, `resident._private_state`는 `WakeQueue`가, `_atomic_json`은 queue
capacity 기록이 쓰며, 테스트가 `runtime_work.Coordinator`·`default_root`를 patch한다.
따라서 `runtime_work` import 시점의 학습 패키지 적재 해소는 **Stage 2b**로 미룬다.
Stage 2b는 이 공용 queue/lease 도구를 비학습 모듈로 옮기면서 기존 patch seam을
호환해야 한다. 집중 회귀(runtime-resources, runtime-wake-policy, resource-policy,
context-learning, qmd-endpoint, orphan-reclaim, laya-product-sync-worker)는 개발
가드로 통과했다(Node 35/35, 해당 Python 묶음 통과). Stage 2b(중립 lease 도구 이동)는
아래에 따로 기록한다.

**Stage 2b (상태: 소스 변경 완료, 집중 가드 회귀 통과 Node 35/35·중첩 Python 묶음, PR #13 head `077f8186` CI 통과):** stdlib만 쓰는 새
`core/runtime_lease.py`에 네 primitive를 동작 그대로 옮겼다. `private_state`(절대·비symlink·
디렉터리·소유자·`0o077` 검사, `resident_state_unsafe`), `private_lock`(`O_CREAT|O_RDWR|O_NOFOLLOW`,
`0o600`, 정규 파일·소유자·모드 검사, `resident_lock_unsafe`), `coordination_lock`(위 lock에
`st_nlink==1` 추가, `unsafe_coordination_lock`), `atomic_json`(`store.canonical`과 같은
`ensure_ascii=False`·`sort_keys`·`(',',':')` UTF-8 bytes + `\n`, 16 MiB `MAX_FILE` 초과 시
`output_budget_exceeded`, 같은 디렉터리 `.write-` 임시 파일 → `fchmod 0o600` → `fsync` →
`os.replace`, 실패 시 임시 파일 삭제)이다.

- `resident._private_state`/`_private_lock`, `resource_coordination._lock`,
  `local_cycle._atomic_json`/`MAX_FILE`은 같은 함수 객체(상수는 같은 값)를 다시 노출한다.
  학습 내부 호출자 import는 바꾸지 않았다.
- `runtime_work`는 모듈 load 시 위 중립 primitive만 import한다. `Coordinator`·`default_root`는
  호출 시점에 원래 `context_learning.resource_coordination`의 실제 클래스·함수로 위임하는
  module-level shim이라 `patch.object(runtime_work,'Coordinator'|'default_root', ...)`
  seam과 `resource_coordination.default_root` patch가 그대로 적용된다. `sample`·`wake` seam도
  그대로다.
- 옮기지 않은 것: 실제 `Coordinator`, `default_root`, `decision`, `TrainingGuard`, `sample`,
  CPU budget(`runtime_cpu`), `service_root`. 측정 가능한 경계는 "새 인터프리터에서
  `import runtime_work` 후 `WakeQueue(<명시 임시 root>).put/pending`이 `context_learning`
  모듈을 하나도 적재하지 않는다"로 한정된다. 기본 root(`WakeQueue()`), `foreground`,
  `guarded_run`, `drain`, `wake`, `request`는 호출 시 여전히 학습 패키지를 적재한다.
- 한계: shim 이후 `runtime_work.Coordinator`는 클래스가 아니라 함수이므로 이를
  `isinstance`·상속 대상으로 쓰는 외부 호출자는 깨진다(현재 core·test에는 없음). 이제
  `resident._private_lock`·`local_cycle.canonical`·`local_cycle.MAX_FILE`을 patch해도 옮긴
  primitive의 동작은 바뀌지 않는다(현재 그런 patch는 없음).
- 새 합성 검사 `test/runtime_lease_test.py`(`runtime-resources.test.mjs`의
  `runtime_*test.py` discover 대상): 새 인터프리터 import 경계, alias 동일성, shim의 실제
  coordinator·root 위임, unsafe state/lock 거절(공유 모드·symlink·hard link), 정확한 bytes·
  `0o600`·oversize 무변경. 이 검사를 포함한 Stage 2 집중 묶음은 개발 가드로 통과했다
  (Node 35/35, 각 Node 파일이 실행하는 중첩 Python 묶음 통과). 이후 PR #13 head `077f8186`
  CI(Node 22/24/26)도 통과했다.
  실제 모델·runtime·설치 검증은 이 단계의 범위가 아니며 수행하지 않았다.

**Stage 3 (상태: 소스 변경 완료, 최초 집중 가드 112/112, 리뷰 후속 수정 뒤 집중 가드 37/37 통과):** 원래 표의 "cross-run 중복을 합친다"보다
좁다. 합치는 대상은 **아직 live source queue에 남아 있는 행**뿐이다.

- `file_change_plan.plan_change_set`은 hash 스캔(SessionStart sync, file-change worker)에서
  created/updated 파일의 `sha256`이 있을 때만 그 컬렉션의 메모리 `changedFiles`에
  `revisions`(`{rel: sha256}`)를 붙인다. snapshot은 `collections`에서 저장되므로
  snapshot·status·pending.json 형식은 그대로이고, hash가 없는 수동 sync의 change set과
  큐 bytes는 이전과 같다. scope revision은 넣지 않았다.
- `wiki_compile_enqueue.compile_candidates`는 revision이 있는 행에만 `contentRevision`을
  기록한다. `wiki_source_intent.revision_key`는 `(collection, path, contentRevision,
  trigger)`다. 큐 파일이 이미 프로젝트 소유이므로 owner 경로·scope hash는 키에 없다.
- `enqueue_changed_sources`는 키가 있는 후보가 있을 때만 기존 queue lock 안에서 live 큐를
  읽어 같은 키 행을 제외한 뒤 상한 분할·append를 한다. 제외된 후보는 queued·deferred
  어느 쪽에도 세지 않지만 **상한 칸은 쓴다**(같은 wake가 그 행을 compile한다). 새 후보는
  `cap - 제외 수`까지만 append하고 나머지는 deferred로 남겨 snapshot rollback으로 다음
  회차가 다시 집는다. 그래서 hash 소스 60개·상한 50에서 첫 회차가 50개를 넣은 뒤
  snapshot 저장이 실패해도, 재시도는 50개를 건너뛰고 나머지 10개를 미루며 한 wake의
  live 행은 50개를 넘지 않는다. 전부 제외되면 공개 reason은 기존 `no_sources`다. 동의
  gate와 `apply_change_set`의 효과·ACK 순서는 바뀌지 않았다.
- lock 안의 live 큐 읽기는 `QUEUE_SCAN_MAX_BYTES`(8 MiB)+1 바이트까지만 한다. 큐가 그보다
  크면(worker가 오래 멈춘 경우) 키를 찾지 않고 같은 lock 안에서 기존처럼 상한 분할·append만
  한다. 이때는 중복 유료 작업이 가능하지만 기존 행을 고치거나 잃지 않고, 무제한 읽기도 없다.
- 제외된 후보는 아직 live 큐에 남은 작업이므로, 하나라도 제외되면 결과 dict에 내부 신호
  `wakePending: True`가 붙는다(제외가 없으면 키 자체가 없다). queued·deferred·상한에는
  들어가지 않고 sync 출력의 `compileQueued`·status.json·pending.json에도 기록되지 않는다.
  `file_change_queue.worker`는 `queued` 또는 `wakePending`이면 기존대로 즉시
  `kick-wiki-compile`을 하고, selected Laya route의 `request_compile`도 같은 조건으로
  요청된다. 따라서 enqueue 뒤 snapshot 저장이 실패한 재시도는 행을 다시 넣지 않으면서
  (`compileQueued`=0) 그 회차에 compile worker를 깨운다.
- `sync.py`(수동·SessionStart)는 결과에 `wakePending`이 있을 때만 JSON에 내부 필드
  `compileWakePending: true`를 덧붙인다. 없으면 키 자체가 없어 평소 출력은 그대로이고,
  `compileQueued`는 실제 append 수 그대로다. `update.sh`의 SessionStart 단계는
  `compileQueued > 0` 또는 `compileWakePending`이면 기존 `kick-wiki-compile --flush`를
  한 번 한다. 동의가 없거나 오류·baseline·unchanged인 sync는 이 필드를 내지 않으므로
  kick도 추가되지 않는다.

남은 한계: worker가 이미 claim(`*.claimed.*`)했거나 처리해 큐에서 빠진 행은 보지 않으므로
처리 완료 뒤의 중복 유료 작업은 막지 못한다. trigger가 키에 있으므로 같은 revision이라도
SessionStart·file-change 이벤트(`post_sync_source`)·수동 sync는 서로 합쳐지지 않고,
PostToolUse·수동 sync·pending 복구 행은 revision이 없어 그대로 중복될 수 있다(worker의
`(path, collection)` 병합은 기존대로). 수동 `sync` CLI는 `compileWakePending`을 출력할
뿐 스스로 kick하지 않는다(원래 수동 sync는 compile worker를 돌리지 않는다). 삭제·rename 카드
출처는 이 단계 범위가 아니다. 합성 회귀(`file-change-plan-checks.py`,
`wiki-source-intent-checks.py`, `file-change-queue.py`의 `compile-cap` 재시도 kick 확인,
`sessionstart-sync-checks.py`의 enqueue 뒤 snapshot 저장 실패 → SessionStart 재시도
(`compileQueued`=0, `compileWakePending`, 큐 bytes 불변)와 `sessionstart-sync.test.mjs`가
그 실제 재시도 출력을 `update.sh --worker`의 sync 결과로 재생해 `kick-wiki-compile --flush`
1회, 신호 없는 queued=0의 무kick, queued>0의 기존 kick을 확인하는 사례)를 포함한 집중
회귀는 상한 수정 **전** 개발 가드로 통과했다(Node 112/112, 가드 체크포인트 통과).
`update.sh` 사례는 sync.py 호출을 stub으로 바꿔 재생하므로 같은 프로세스 안의 실제 저장
실패를 거치지는 않는다.

Stage 3 리뷰 후속(M1·L1): Stage 2–4 읽기 전용 집중 리뷰가 제외된 live 행을 상한에서 빼지
않아 한 wake의 live 행이 상한을 넘을 수 있는 회귀(M1)를 찾았다. 제외된 행이 상한 칸을 쓰도록
바꾸고 live 큐 읽기에 8 MiB 상한을 두었다. `wiki-source-intent-checks.py`에 60개·상한 50 저장
실패 재시도(live 50행 유지, 꼬리 10개 deferred·snapshot 미진전, drain 뒤 다음 회차가 꼬리만
queue)와 8 MiB 초과 큐의 plain append 사례를 추가했고, 기존 "제외 행은 상한 칸을 쓰지
않는다" 사례는 새 계약으로 고쳤다. 수정 뒤 집중 가드 회귀 37/37이 통과했다.

**Stage 4 (상태: 소스 변경 완료, 집중 가드 회귀 통과 42/42):** recall의 live selection 블록 하나만
새 조정자 `context_learning/selection_read.select(...)`로 옮겼다. `selection_port`는 순수
함수로 남고(docstring만 갱신), 상태 해석·공유/profile 동의 확인·corpus snapshot·작업량
추정·`live_select.choose` 호출은 `selection_read`가 기존 순서 그대로 한다.

- recall에 남은 것: `adaptive_pool.plan`(질의 전 pool 계획), `qmd_laya_selection`을 포함한
  모든 recall 로그, `request_view_pin` 확인, capture·observe·write 경로, 공개 출력 순서.
  recall은 여전히 함수 안에서 `context_learning`을 가져온다(`selection_read`, plan,
  capture 등). 학습 패키지 import가 recall에서 사라진 것은 아니다.
- 보존한 계약: 검사·예외 순서, non-profile(trial grant)과 profile(`readable`) 동의 분리,
  `no_candidates`·`non_wiki_pool`·`private_state_unavailable`·`shared_state_unavailable`·
  plan reason, 오래된 corpus·오류의 `candidate_unavailable` 대응과 두 통과 reason
  (`input_window_unavailable`·`responsiveness_deferred`), 실패 전까지 얻은
  work_estimate·corpus·model_rows·resources 유지, 기본 top-3과 trial fallback top_n,
  `HookDeadlineExceeded` 전파, corpus에 원래 `qmd_paths` 전달, 잡는 예외 tuple
  `(OSError, ValueError, UnicodeError, KeyError, TypeError)`. 학습 이름은 호출마다 함수
  안에서 import하므로 테스트의 모듈 attribute patch(`live_select.choose`,
  `shared._policy` 등)가 그대로 적용된다. deadline은 기존처럼 사용 시점마다
  `hook_budget.deadline`을 호출한다.
- 쿼리 중 `_private_state(create=True)` 디렉터리 생성은 그대로이며 별도 변경으로 남긴다.
- `learning-selection-port-checks.py`에 recall 구조 단정 갱신과, 이전 inline 블록 사본과
  `select`를 같은 patch 아래 돌려 결과·예외·학습 호출 순서를 비교하는 동등성 사례를
  추가했다. 집중 회귀는 개발 가드로 42/42 통과했다.

**Stage 5 (상태: 소스 변경 완료, 집중 가드 회귀 통과 31/31):** 대부분의 분리(색인 대상, 파일
identity, source queue 이름, 단일 모델 fingerprint, scoped search hit)는 이미
`install_evidence`에 있었다. 이번에는 `shared_install`에 남아 있던 순수 판정만 옮겼다.

- 옮긴 것: source 파일의 해시 전 stat과 이후 stat의 identity 비교
  (`same_source_identity`, ctime만 달라도 불일치), snapshot 항목 dict
  `{'sha256','identity'}`(`source_entry`), 벡터 감사 결함 여부(`vectors_complete`),
  호출자별 reason의 `ValueError`(`require_complete_vectors`: `physical_vectors_still_incomplete`·
  `physical_vectors_incomplete`·`physical_vectors_changed_during_readiness`), 복구 journal의
  `vectorRepair` intent dict(`vector_repair_intent`). `install_evidence`는 계속 import가 없다.
- `shared_install`에 남은 것: 파일·DB·프로세스 IO, deadline, journal 저장, 설정·store 준비,
  `UnstableSourceSnapshot` 발생 위치. 첫 번째(fd `fstat`)와 두 번째(path `stat`) 확인은 기존
  순서와 단락 평가를 그대로 유지해 첫 확인이 실패하면 path `stat`을 호출하지 않는다.
  bootstrap proof는 `_vector_scope` 호출마다 다시 읽고, `_save`·`dirty_queue`·`S.root`·
  `S._private_file`·`_readiness` 등 patch seam과 활성화 순서는 바꾸지 않았다.
- 바꾸지 않은 것: journal 형식은 새로 만들지 않았고(dict 내용·키 순서 동일), 반복 게시 경로는
  추가하지 않았다. `shared_install`은 `qmd_shared_store.stage_empty/activate_empty`를 호출하지
  않으므로 그 계약은 이 단계의 보존·검증 대상이 아니다(최초 store 준비·활성화는 기존대로
  `shared_qmd_setup.prepare/activate`).
- `install-evidence-checks.py`에 ctime만 다른 identity 불일치, snapshot 항목의 정확한 형태,
  결함 판정·repair intent dict, reason별 `ValueError`, 그리고 합성 임시 collection에서
  `_source_snapshot`의 항목 형태와 두 번째 `fstat`의 ctime만 바뀐 경우의
  `source_changed_while_hashing`을 추가했다(13→18건). 집중 회귀는 개발 가드로 31/31
  통과했다. Stage 5/ACK 읽기 전용 집중 리뷰는 Critical/High/Medium 문제를 찾지 않았다.

**Stage 6 (상태: 6a A-int multi-root source 경계 소스 완료·집중 가드 회귀 통과, 6b v2 product worker 연결 소스 완료·집중 가드 회귀 통과, PR #13 head `077f8186` CI 통과, L1 설치본·운영 QA 미실행):**
이전의 "A/B source·pointer, `scopeRevision`/`incarnation`, durable receipt, live read·embed
모순 미결정" BLOCKED 기록은 QMD shadow/A-copy 원형 기준이었다. 상위 결정으로 저장 구조는
A-int **단일 SQLite**(`LayaIndexA`)로 확정됐다: immutable generation, generation마다 full
membership, content(hash)·vector(hash, title, profile)는 generation 사이에 공유한다. 기존
A-int에는 이미 DB `incarnation`, head 이동과 receipt의 같은 트랜잭션, head CAS, 별도
`ClaimQueue`(index incarnation binding)의 receipt 기반 조건부 ACK가 있다. 따라서 위 미결
항목은 A-int 기준으로 해소됐고, QMD `qmd_candidate_config`·`shadow_publish_flow`·
`shadow_qmd_adapter`·`shadow_ack_policy`·`dirty_queue_claim` 조건부 ACK는 **A-int에 연결하지
않는다**(격리 원형으로 남는다). 비선택(legacy QMD) 프로젝트의 `backend/index_worker.sh`·
`update.sh`·공유 `active.json`·endpoint·운영 DB는 이 단계에서 바꾸지 않았다.

A-int producer map(현재 코드):

| 경로 | producer/consumer | 단일·v2 | 게시 계약 |
|---|---|---|---|
| 최초 origin(설치자 전용) | `laya_product_install` → `laya_qmd_migration`(QMD 읽기 전용 → `index.stage`) → `laya_qmd_cutover`(`publish_from_source` / multi는 `publish_collections_from_source`, base `None`, `require_vector=True`) → journal·route 기록 | 둘 다 | 게시 직전 `scan`/`scan_collections` manifest 재대조, vector seal 검증, incarnation·queue binding 확인 |
| 반복 selected sync | hook(`laya_host_response`의 index·SessionStart·file-change-event) → `runtime_work.request('laya-sync')` → `runtime_handlers.laya_sync` → `laya_product_sync_worker.run` → `LayaIndexRuntime` + `prepare_candidate_batch_off_hook` + `laya_index_sync.sync_full` → `laya_index_flow.process_one` | 둘 다(6b): v1은 `decision['sourceRoot']` 그대로, v2는 provider 시작 전 검증한 `decision['collectionRoots']` | queue 옆 owner lock → `before_publish` journal CAS fence → lease → stage → 새 full scan 재대조 → head CAS publish(+receipt 같은 트랜잭션) → 정확한 receipt 재확인 뒤 조건부 ACK. 64개 초과 backlog는 `claim_backlog` deferred |
| 검색(소비) | `laya_host_response`: v1 `query_selected_hybrid`(`search_verified`·vector), v2 `query_selected_v2`(`scan_collections` + 한 pin의 collection prefix 검색) | 둘 다 | pin한 generation의 seal manifest와 live full scan 일치, 행별 hash 검증 |

**Stage 6a (상태: 소스 변경 완료, 집중 가드 회귀 Node 6/6 통과(run `e86eb2dce21a4a7491dbe17ae4fbc9fa`), PR #13 head `077f8186` CI 통과):** v1 동작을 바꾸지 않는
multi-root source 경계만 추가했다.

- `LayaIndexRuntime(source_root=...)`는 기존 `str`(검사 그대로) 또는 collection 이름 →
  절대·resolve된 디렉터리 `Mapping`을 받는다. Mapping은 1–16개, 이름은 source port의
  `_COLLECTION` 규칙, root는 `str`·절대·디렉터리·`resolve(strict=True)` 문자열과 정확히 같음,
  서로 같거나 조상/자손이면 거절한다(문자열 접두가 아니라 `Path.parents` 비교라 `/`도
  모든 root와 겹친다). 하나라도 어기면 index·queue DB를 열기 **전에**
  `invalid_runtime_binding`으로 거절한다. 통과하면 정렬된 `MappingProxyType` 사본을 보관해
  호출자 dict 변경이 binding을 바꾸지 못한다.
- `laya_index_flow.process_one`/`_run_leased`와 `laya_index_sync`(`sync_full`,
  `prepare_candidate_batch_off_hook`)는 root가 Mapping일 때만 기존
  `scan_collections`·`publish_collections_from_source`로 보낸다. 문자열 root는 전과 같은
  `source_port.scan`·`publish_from_source`(sync는 모듈의 `scan` 이름) 호출이며 manifest,
  호출 순서(fence→process→enqueue→process), ACK·replan·deferred·fence 의미는 그대로다.
  `runtime.query`·`enqueue_claim`도 같은 방식으로 `search_collections_verified`·
  `scan_collections`를 고른다. vector 검색은 이미 Mapping을 처리한다.
- `laya_product_sync_worker`는 6a에서 바꾸지 않았다(당시 v2는 provider attestation·embed
  service 시작 **뒤**의 `KeyError`로 실패했다. 6b에서 해소).
- 합성 검사(임시 DB/queue만): `laya-index-sync-checks.py`에 두 disjoint root의 indexed→
  unchanged(fence 1회, membership·head manifest), 한 root 수정 뒤 두 root 전체 membership의
  새 generation(이전 generation 불변, content 공유), 남은 collection claim의 collection
  publish 경유 replay·ACK, namespaced path의 vector batch·검색, v1 문자열 root가 collection
  helper를 한 번도 부르지 않음을 추가했다. `laya-index-runtime-checks.py`에 중첩·역순 중첩·
  같은 root·`/`·상대·끝 `/`·`..`·symlink·없음·파일·숨김/`/` 이름·비문자열·17개 거절과 DB·queue
  bytes·디렉터리 항목·lock 무변경, 동결 사본과 collection manifest enqueue·stale 거절을
  추가했다. 위 집중 가드 회귀로 통과했다.

**Stage 6b (상태: 소스·합성 검사 작성 완료, 집중 가드 회귀 Node 6/6 통과(run `9a37e5287b5e4e3a9bc0dfca85d54465`), PR #13 head `077f8186` CI 통과):**
`laya_product_sync_worker`만 바꿨다.

- `run`은 `_selected` 직후, QA barrier·provider 생성·attestation·resource admission·service
  시작 **전에** `_route_source(decision)`로 source binding을 고른다. v2 키(`collections`/
  `collectionRoots`)가 없으면 v1이며 `decision['sourceRoot']` 객체를 그대로 넘긴다(없으면
  `selected_route_source_missing`). v2 키와 `sourceRoot`가 함께 있으면
  `selected_route_source_ambiguous`.
- v2는 `collectionRoots`가 Mapping이고 `laya_index_runtime._collection_roots`(runtime의 같은
  검사 함수 재사용, 별도 구현 없음)를 통과해야 하며, 아니면 `selected_collection_roots_invalid`.
  `collections`의 `{name: sourceRoot}`가 그 binding과 정확히 같지 않으면
  `selected_collection_roots_mismatch`. 통과한 동결 사본을 `LayaIndexRuntime`에 넘기고 runtime은
  DB를 열기 전에 다시 검사한다.
- 이후 파이프라인(`prepare_candidate_batch_off_hook`→`sync_full`, `before_publish`의 journal
  CAS fence, attestation·proof 재확인, priority yield, QA barrier, `finally` service stop,
  receipt 뒤 조건부 ACK)은 그대로다.
- 합성 검사(`laya-product-sync-worker-checks.py`, 11→16건; 임시 A-int DB/queue/두 disjoint
  root, 가짜 route proof·journal CAS·provider·service): 빈 DB에서 두 collection origin
  indexed(fence 1회, full membership `alpha/one.md`·`beta/two.md`, claim acked, service stop)
  → unchanged(fence·추론 없음); 한 root 수정 뒤 두 root 전체 membership의 새 generation(이전
  generation 불변, content 공유, 수정 문서만 재추론, 남은 root의 (hash, `qmd_markdown_title`, profile)
  vector identity 유지 — title은 heading 또는 basename이며 namespaced path는 membership·문서
  경로로 title 도출에 전달되지만 collection 접두는 title을 바꾸지 않는다); v1이 `sourceRoot` 객체를 그대로 받고 collection helper를 부르지
  않음; 잘못된 v2 8종이 명시 reason으로 provider·attestation·admission·service 시작 전에
  거절되고 DB/queue bytes·디렉터리 항목이 변하지 않음.
- cutover origin 위의 첫 sync(1건): 합성 QMD 형식 DB(`laya-qmd-migration-checks.py`의
  `make_source`, 실제 QMD 아님)를 `backup_qmd_collections`로 백업하고, 설치기 staging 함수
  `prepare_qmd_candidate`(A-int `stage` → `register_profile`·`insert_chunk`·`seal_chunk_set`·
  `seal_generation`, worker provider와 같은 model/fingerprint/dimension의 합성 vector)로 두
  collection candidate를 만든 뒤, `bind_qmd_origin`과 같은 `verify_generation_seal` 확인과
  `publish_collections_from_source(..., base None, claim 'qmd-migration', claim_epoch 0,
  require_vector=True)` 호출로 origin을 게시한다. 그 DB/새 queue를 가리키는 selected decision으로
  실제 `run`→`prepare_candidate_batch_off_hook`→`sync_full`(가짜 proof·CAS·provider·service)을
  돌리면 `unchanged`(같은 origin generation, claim 없음)이고, journal CAS 0회, provider 추론 0회,
  full membership `alpha/one.md`·`beta/two.md` 유지, `gen_vector_rows`의 (namespaced path, hash,
  heading/basename 기반 `qmd_markdown_title`, profile) 집합·`vector_chunk_sets` 2개·head가 전후 동일, batch의
  (hash, title)이 sealed 집합과 같고 queue에 active claim이 없다.
- 한계: 위 검사는 `bind_qmd_origin` 자체를 호출하지 않는다. 그 함수의 journal `candidate_ready`·
  attestation(`preflight_product_candidate`)·route bytes·settings preflight·QMD digest 재확인·
  ledger/marker 기록은 이 검사 범위 밖이고(`laya-qmd-cutover-checks.py`가 따로 다룸),
  `activate_qmd_origin`의 route 게시·journal CAS 활성화도 거치지 않는다. 실제 설치기·QMD·모델·
  운영 route·설치/릴리즈 QA는 별도 단계다. 검증 기록: 개발 가드로
  `npm run test:focused -- test/laya-product-sync-worker.test.mjs test/laya-index-sync.test.mjs
  test/laya-index-runtime.test.mjs test/laya-multi-source.test.mjs test/laya-index-vector.test.mjs
  test/laya-host-response.test.mjs`를 실행해 Node 6/6, `laya-product-sync-worker` fixture 16건이
  통과했다(공유 가드 run `9a37e5287b5e4e3a9bc0dfca85d54465`, settled/completed true, passed,
  종료 코드 0). 이후 이 코드를 포함한 PR #13 head `077f8186` CI(Node 22/24/26)가 통과했다.
  실제 설치기·운영 route·L1 설치본 QA는 여전히 실행하지 않았다.

이전 QMD 원형 기준으로 추가한 조건부 ACK seam 기록(A-int에는 연결하지 않음):

- `dirty_queue_claim.claim_identity(queue)`: `queue_lock` 안에서 claim을 읽기만 하고
  `shadow_ack_policy`가 받는 `{id, digest}`(`id`=`qmd-dirty-claim-v1:<queue inode>:<size>`,
  `digest`=claim의 검증된 `sha256`)를 돌려준다. claim이 없으면 `None`이고, claim도 lock도
  없으면 lock sidecar를 만들지 않는다. identity는 processing→done과 `reloadRequired` 재기록에
  변하지 않는다.
- `ack(queue, expect=None)`: `expect`가 없으면 기존 동작·CLI(`ack <queue>`)와 같다.
  `expect`가 있으면 모양을 lock 전에 검사하고, 같은 lock 보유 안에서 claim을 한 번 읽어
  기존 `dirty_queue_claim_missing`→`dirty_queue_reload_required` 순서 다음에 identity를
  비교한다. 불일치(`dirty_queue_claim_identity_mismatch`)는 phase 기록·파일 변경 전에 거절한다.
- 크기 상한: 조건부 경로만 claim 파일을 `MAX_IDENTITY_CLAIM_BYTES`(8 MiB)까지 읽고, 넘으면
  `dirty_queue_claim_too_large`로 무변경 거절한다(부분 파싱 없음). 기존 ACK의 읽기는 그대로다.
  일치한 뒤의 queue 접두 비교·교체 읽기는 기존 ACK와 같아 상한이 없다.
- 남은 한계: claim 파일에 nonce가 없어(추가하면 기존 claim bytes가 바뀐다) inode가 재사용되고
  같은 bytes가 다시 claim되면 이전 identity와 구분되지 않는다. durable receipt 결정과 함께 다룬다.
- `shadow_publish_flow`의 ACK callback 주입은 그대로다. 새 합성 검사
  `dirty-queue-claim-identity-checks.py`를 포함한 이 선행 seam의 집중 가드 회귀는 22/22
  통과했다. QMD 후보 writer 연결은 A-int 결정으로 진행하지 않는다.
- 8 MiB 제한: 실제 claim이 8 MiB를 넘으면 조건부 ACK는 항상 `dirty_queue_claim_too_large`로
  거절한다. 이 claim은 이후 재시도해도 같은 이유로 거절되므로, 미래 소비자(게시 flow의 ACK
  callback)는 이 결과를 **DEFER**(ACK 보류·불확실 상태 유지, 다음 결정까지 대기)로 다뤄야 하며
  즉시 재시도 루프에 넣으면 안 된다. `expect` 없는 기존 ACK 경로는 이 제한의 영향을 받지 않는다.

#### 이후 단계 (Stage 2b–6)

전체 아키텍처 분리 작업은 승인돼 있으므로 단계마다 따로 승인을 받지 않는다. 다만 각
단계는 하나의 경계만 옮기고, 아래 집중 회귀를 개발 가드로 통과한 뒤 이 절의 상태를
갱신한다. 운영 pointer·endpoint 전환, 실제 설치 QA, 유료 extractor·학습 실행은 어느
단계의 완료 조건에도 포함하지 않으며 해당 작업은 별도 릴리즈 단계로 남긴다. 순서는
Stage 1에서 바로 이어지는 런타임 결합을 먼저 풀고, 운영 구조 선택이 필요한 검색 게시를
마지막에 둔다.

| 단계 | 대상 모듈 | 보존할 계약 | 집중 회귀 |
|---|---|---|---|
| Stage 2 런타임 handler (소스 완료, 집중 가드 회귀 통과 Node 35/35·Python 묶음) | `runtime_work.dispatch`의 index/update/laya-sync/laya-compile/learning/auto/cleanup 본문을 `runtime_handlers`의 고정 `HANDLERS` 표로 분리하고 `runtime_work`는 같은 `dispatch`를 재노출한다. compute admission 대상 kind 판정은 `runtime_wake_policy`로 옮긴다. 모듈 상단 `context_learning` import는 queue·lease·drain·wake가 쓰므로 유지한다. | `WakeQueue` SQLite schema, revision ACK, lease·재시도, `ResourceDeferred` 반환 의미, `MAX_WAKES` override, 실행 직전 승인·scope 재확인 위치, `runtime_work.dispatch`·patch seam | runtime-resources(새 `runtime_handlers_test.py` 포함), runtime-wake-policy, resource-policy, context-learning, qmd-endpoint, orphan-reclaim, laya-product-sync-worker |
| Stage 2b `runtime_work` import 분리 (소스 완료, 집중 가드 회귀 통과 Node 35/35·중첩 Python 묶음) | `_lock`·`resident._private_state`/`_private_lock`·`_atomic_json`만 stdlib `runtime_lease`로 옮기고 학습 모듈은 같은 객체를 재노출한다. `Coordinator`·`default_root`는 `runtime_work`의 호출 시점 shim으로 남아 실제 구현은 이동하지 않는다. 경계는 명시 root의 `WakeQueue.put/pending`까지이다. | Stage 2 계약 전부, `runtime_work.Coordinator`·`default_root`·`sample`·`wake` patch seam, lock 파일 경로·권한 | Stage 2 묶음과 같음 |
| Stage 3 파일 변경 → 위키 revision (소스 완료, 집중 가드 회귀 통과: 원래 Node 112/112, 상한 수정 뒤 37/37) | hash 스캔의 `changedFiles`에만 content revision(`revisions`)을 덧붙이고, `wiki_source_intent.revision_key`의 `(collection, relative path, content revision, trigger)` 키로 **아직 live 큐에 남은** 같은 행만 queue lock 안에서 건너뛴다. scope revision·owner 경로는 키에 없다. claim·처리된 행과의 중복, PostToolUse·수동 sync(revision 없음)의 중복은 합치지 않는다. 건너뛴 행이 있으면 내부 `wakePending` 신호로 file-change worker의 즉시 compile kick을 유지하고, sync JSON의 내부 `compileWakePending`으로 SessionStart `update.sh` kick도 유지한다. | snapshot·status·pending.json 형식, 수동 sync 큐 bytes, `apply_change_set`의 효과·ACK 순서, 스캔 실패 ≠ 삭제, `state_unreadable`(3), 동의 gate·상한(리뷰 수정 뒤: 건너뛴 live 행도 상한 칸을 쓰며, live 큐 스캔은 8 MiB까지), 공개 reason `no_sources`·`compileQueued`, trigger 구분, file-change worker의 즉시 kick | `test/file-change-plan.test.mjs`·`test/wiki-source-intent.test.mjs`·`test/file-change-queue.test.mjs`(`compile-cap`), 기존 SessionStart·sync/compile 회귀, wiki-compile-worker. 같은 키 skip, 다른/없는 revision, 상한·deferred, enqueue 뒤 snapshot 실패 재시도(`wakePending`과 실제 `kick-wiki-compile`), 수동 sync bytes 불변, claim된 행 미병합을 합성 사례로 고정한다. extractor는 호출하지 않는다. |
| Stage 4 recall 학습 읽기 포트 (소스 완료, 집중 가드 회귀 통과 42/42) | `recall.py`의 live selection 블록(`live_select`·`corpus`·`seam`·`shared`·`learning_profile`·`adaptive_pool.actual_work`·`compact_input`·`candidate_apply` 읽기)을 새 `selection_read.select` 하나로 옮기고, 그 조정자가 순수 `selection_port`를 호출한다. `adaptive_pool.plan`·로그·`request_view_pin`·capture는 recall에 남는다. `posttool`·`edit_context`·`topical_hook_queue`의 capture 호출은 옮기지 않는다. | raw 검색 결과·진단·fallback, `request_view_pin`의 route/URL pin, 미동의·후보 없음·오래된 corpus 동작. 쿼리 중 `_private_state(create=True)` 디렉터리 생성은 별도 변경으로 다룬다. | recall 집중 회귀(C2c의 57건 묶음), context-learning, selection_port fixture |
| Stage 5 설치 증거 판정 (소스 완료, 집중 가드 회귀 통과 31/31) | `shared_install._source_snapshot`·`_ensure_vectors`·`_readiness`에 남은 순수 판정(source identity 비교·snapshot 항목, 벡터 결함·reason별 `ValueError`·repair intent dict)만 `install_evidence`로 옮긴다. 수집 IO·journal 저장·`UnstableSourceSnapshot` 발생은 `shared_install`에 남는다. 새 journal 형식이나 반복 게시 경로는 만들지 않는다. | journal intent→enqueue→ACK 순서와 기존 dict 형태, fd/path 두 stat 확인의 순서·단락, 오류 reason, bootstrap proof 반복 읽기, patch seam, 활성화 순서(`shared_qmd_setup.prepare/activate`), `setup_guard` 상태 코드, 승인 scope·deadline·중단 후 재개, 기존 store 무변경. `qmd_shared_store.stage_empty`는 `shared_install`이 호출하지 않으므로 대상이 아니다. | `shared-install`, `shared-install-resilience`(vector-resume·abrupt-resume fixture), `install-evidence`. 실제 설치·업데이트 QA는 이 단계에서 수행하지 않는다. |
| Stage 6 A-int 검색 게시 writer (6a multi-root source 경계: 소스 완료·집중 가드 회귀 6/6 통과. 6b v2 product worker 연결: 소스 완료·집중 가드 회귀 6/6 통과. 둘 다 PR #13 head `077f8186` CI 통과, 설치 QA 미실행. QMD shadow/A-copy 원형은 연결하지 않음) | 6a: `LayaIndexRuntime`이 문자열 또는 검증된 disjoint collection root Mapping을 받고, `laya_index_flow`·`laya_index_sync`가 Mapping일 때만 `scan_collections`·`publish_collections_from_source`로 보낸다. 6b: `laya_product_sync_worker`가 v2 `collectionRoots`로 같은 `sync_full`을 호출한다. 저장 구조는 A-int 단일 SQLite([Laya Index 계획](2026-10-17-embedded-sqlite-search-engine.md))다. | v1 문자열 root의 manifest·호출 순서·owner lock·`before_publish` fence·lease·head CAS·같은 트랜잭션 receipt·조건부 ACK, generation마다 full membership, 변경 전 fail-closed 검증, 잘못된 v2 source는 provider 시작 전 명시 reason·무쓰기 | `laya-index-sync-checks.py`·`laya-index-runtime-checks.py`(새 multi-root 사례 포함), `laya-multi-source-checks.py`, `laya-index-a`·`laya-index-vector`, `laya-product-sync-worker`, `laya-host-response`. 운영 cutover·설치 QA는 범위 밖이다. |

## Claude 설계 검토 후속 기록

초기 읽기 전용 검토는 첫 실행의 명시적 위키 동의, enqueue와 snapshot
사이의 중복 위험, 기존 autoCycle 경로와 목표 설계의 구분을 지적했다.
그 뒤 사용자 승인에 따른 Sonnet 범위별 설계 검토 네 건이 완료됐다.
Laya Index의 A/H 저장 비교, scoped 검색 비용, 별도 DB의 receipt/CAS/ACK,
pin·GC 및 이관 레드팀 항목의 채택·보류 근거는
[엔진 계획](2026-10-17-embedded-sqlite-search-engine.md)과
[이관 계획](2026-10-17-embedded-engine-data-migration.md)에 기록한다.
이 절의 QMD C2 A/B는 당시 격리 원형이며 새 엔진의 물리 구조 결정이 아니다.

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
| 1. 색인·검색 게시 | 승인된 store/worktree revision의 비활성 후보만 QMD writer가 쓰고, 후보 daemon 준비·요청 세대 pin·late worker 거절·queue ACK 복구가 한 게시 계약으로 연결돼야 한다. | C2b에서 private 후보 설정·SQLite 게시·ACK callback·이전 게시 DB source 연속성을 검증했다. C2c에서는 게시 전 receipt를 받는 source callback, 불확실 ACK의 순수 판정, recall의 선택 route/URL pin을 추가했다. `backend/index_worker.sh`·`update.sh`와 공유 pointer/endpoint는 그대로여서 운영 경계는 미완료. [C2 옵션](2026-10-17-qmd-published-view-options.md)의 A/B 저장 구조 선택·전환 승인 필요. |
| 2. 파일 동기화 → 위키 비용 경계 | change set이 content/scope revision을 위키에 넘기고, 같은 원문 revision의 수동 sync·SessionStart·PostToolUse enqueue가 하나의 유료 작업으로 합쳐져야 한다. | `file_change_plan`과 `wiki_source_intent`의 순수 순서·상한 분리는 완료. 현재 `changedFiles`는 경로만 가져 cross-run 멱등성과 ACK 복구는 미완료. |
| 3. 런타임 → 학습 실행 | wake kind별 handler가 실행 직전 승인·scope를 재확인하고 공통 queue는 lease·재시도·revision ACK만 소유해야 한다. | `runtime_wake_policy` 결정 추출과 recall의 읽기 전용 `selection_port`는 완료. `runtime_work.dispatch`와 `recall`의 학습 상태·실행 import 결합은 남아 있다. |
| 4. 설치와 검색 준비 | 설치 journal은 증명된 후보·scope 결과만 받아야 하며 반복 검색 게시와 최초 빈 store 활성화는 별도 계약이어야 한다. | `install_evidence` 판정 추출은 완료. `shared_install`의 증거 수집·벡터 복구·endpoint 준비 순서와 반복 게시 연결은 남아 있다. |

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

## Claude 설계 검토 후속 기록

초기 읽기 전용 검토는 첫 실행의 명시적 위키 동의, enqueue와 snapshot
사이의 중복 위험, 기존 autoCycle 경로와 목표 설계의 구분을 지적했다.
그 뒤 사용자 승인에 따른 Sonnet 범위별 설계 검토 네 건이 완료됐다.
Laya Index의 A/H 저장 비교, scoped 검색 비용, 별도 DB의 receipt/CAS/ACK,
pin·GC 및 이관 레드팀 항목의 채택·보류 근거는
[엔진 계획](2026-10-17-embedded-sqlite-search-engine.md)과
[이관 계획](2026-10-17-embedded-engine-data-migration.md)에 기록한다.
이 절의 QMD C2 A/B는 당시 격리 원형이며 새 엔진의 물리 구조 결정이 아니다.

# 기능 경계와 점진적 분리 계획 (2026-10-17)

이 문서는 현재 코드의 의존성과 상태 소유권을 기록하고, 이후 변경의 경계를 정한다.
목표 구조는 설계안이다. 아래에서 **이번 변경**으로 표시한 부분 외에는 구현 완료를
뜻하지 않는다. 저장소 전체를 한 번에 재작성하거나 모든 함수에 포트·이벤트 버스를
씌우지 않는다.

## 현재 구조 지도

| 기능 | 현재 진입점·구현 | 현재 주요 상태 |
|---|---|---|
| 검색·컨텍스트 주입 | `core/recall.py`, `qmd_project_query.py`, `qmd_route.py`, `wiki_freshness.py` | 선택된 QMD index와 프로젝트 route, recall 진단 로그 |
| 학습 데이터·평가·학습 | `core/context_learning/shared.py`, `corpus.py`, `local_cycle.py`, `live_select.py` | 사유화된 `shared-learning.sqlite3`, 후보·평가 기록, checkpoint 선택 pointer |
| 위키 생성·출처 검증 | `core/wiki_compile_enqueue.py`, `wiki_compile_worker.py`, `wiki_compile.py`, `wiki_freshness.py` | 프로젝트 `.auto-context/compile`의 source/verify/dedup queue·ledger와 `.auto-context/wiki` 카드 |
| 프로젝트·파일 동기화 | `core/sync.py`, `file_change_queue.py`, `index_enqueue.py` | collection snapshot, `.auto-context/file-changes`, index dirty queue |
| 설치·업데이트·마이그레이션 | `core/setup_entry.py`, `shared_qmd_setup.py`, `shared_install.py`, `qmd_shared_store.py`, `setup_guard.py` | 설치 journal, 생성 세대, store·runtime active pointer, scope 승인 |
| 런타임 운영 | `core/runtime_work.py`, `qmd_endpoint.py`, `context_learning/resource_coordination.py`, `backend/index_worker.sh` | `runtime-wakes.sqlite3`, writer/compute lease, endpoint·모델 프로세스 수명 |

`core/config.py`는 정규화 정책과 파일 탐색·마이그레이션을 함께 담고 있다.
설정은 프로젝트/사용자가 소유하며, 설치 마이그레이션 외의 런타임 소비자가
임의로 덮어쓰지 않아야 한다. 영속화 헬퍼와 관측성은 여러 기능이 필요로 하지만,
하나의 큰 공통 서비스가 기능별 결정을 대신 소유해서는 안 된다.

## 지금 드러난 결합과 위험

- `sync.py`는 snapshot 비교 뒤 index dirty queue와 위키 source queue를 별도로
  호출한다. 이번 변경으로 위키 엔진·후보·상한·큐 쓰기는 위키 소유 모듈의 공개
  `enqueue_changed_sources`로 옮겼다. enqueue 이후 snapshot 기록 사이에 실패하면
  중복 재시도가 가능한 결합은 여전히 남아 있다.
- `file_change_queue.py`는 `sync.py`를 재사용하면서 QMD index SQLite까지
  읽어 첫 snapshot을 만든다. 검출 정책을 바꿀 때 검색 저장소 형식도 영향을 준다.
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
`sync.compare_collection`의 순수 비교와 collection 단위 dirty queue 계약은
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

## Claude 재개 시 설계 리뷰 요청

Claude의 읽기 전용 설계 검토를 재개했다. 검토는 첫 실행의 명시적 위키 동의,
enqueue와 snapshot 사이의 중복 위험, 기존 autoCycle 경로와 목표 설계의
구분을 지적했다. 코드·회귀 검토는 별도 기록으로 이어진다.

# 런타임

wake 큐, admission, lease와 자원 정책을 다룬다. wake kind는 `laya-sync`·`laya-compile`·`learning`·`auto`다. 알 수 없는 wake kind는 보존하고, 출시 전 빌드가 남긴 `index`·`update`·`cleanup` 행만 큐를 읽을 때 `retired_kind` 사유(`runtime-wake-drops.json`)로 지운다.

구현: `host_bundle.py`, `qa_model_budget.py`, `resource_controls.py`, `resource_platform.py`, `runtime_admission.py`, `runtime_cpu.py`, `runtime_handlers.py`, `runtime_lease.py`, `runtime_monitor.py`, `runtime_wake_policy.py`, `runtime_work.py`, `wake_cancel.py`.

selector 유지보수의 재시도 예약은 기존 `learning` kind와 canonical project
target을 사용한다. `laya-sync` 행의 due는 학습 backoff로 미루지 않는다.

`laya-compile`의 `deferred:pending:compile_progress`는 backlog(source 큐 행과
시도할 refresh 이벤트)가 pass 앞보다 줄어든 pass다. 남은 작업은 30초 뒤
재시도하고 기존 실패 횟수를 늘리지 않는다. 선택 실행은 pass 뒤의 backlog를
결과에 싣고, 큐는 wake별 최저치(`floor`)와 "최저치를 갱신하지 못한 연속
pass 수"를 큐 옆 `runtime-wake-progress.json`에 둔다. 최저치를 갱신한 pass는
그 수를 0으로 되돌리므로 계속 줄어드는 backlog는 크기와 무관하게 무집계로
이어진다. 줄었다가 되돌아오는 backlog가 연속 120회
(`COMPILE_PROGRESS_STREAK_CAP`)를 넘으면 그 뒤는 집계되는 재시도이고 기존
retry cap을 따른다. backlog를 싣지 않는 worker의 답은 매 pass를 센다. 다른
결과가 나오면 기록을 지운다. 무진척 pending과 실제 실패는 기존 retry cap을
따른다. wake kind·저장 형식은 그대로이며, 이 detail을 모르는 구 worker는
기존 실패 집계 규칙으로 처리한다.

wake 이벤트의 `outcome`은 dispatcher(`memvane/app/runtime_dispatch.py`)가 정한다.
다음 pass로 일을 넘긴 정상 handoff는 `progress`다: `laya-sync`의
`schema3_progress_ingest`·`schema3_progress_vector`·`schema3_vector_budget`·
`schema3_vector_content_busy`와 `laya-compile`의 `compile_progress`. 그 밖의
미완료는 `failed`다. main view를 게시한 `laya-sync` pass는 연결 view가
기다려도 `indexed`·`unchanged`로 ACK되고 대기는 `linked` 필드에 남는다.

백그라운드 프로세스는 호스트 설치 기록과 경로·manifest를 확인한 최신 1.x 번들에서 실행한다. 자기 번들이 사라지거나 바뀌면 timer와 worker는 기존 wake를 보존하고 후속 프로세스에 넘긴다. 설치 기록을 확인할 수 없으면 기존 큐를 남긴 채 재시도하며, 백그라운드 번들 목록 확인 실패는 1시간이 아니라 짧은 재시도 간격(60초→300초→3600초 백오프, 설치 기록 변경 시 초기화) 뒤 다시 확인한다. 프로젝트의 journal·provider 증명은 후속 worker가 다시 수행한다.

`wake_cancel.py`는 한 (kind, target)의 wake 행과 overflow 여부, 큐 DB에 남아 있을 수 있는 `wake_cancellations` 감사 행을 읽기 전용으로 보여 준다(install의 `build-wake-status`). wake 행을 지우는 경로는 없다.

다른 도메인이 쓰는 자원 핸들(`Coordinator`, `WakeQueue`, `Supervisor`, `TrainingGuard`)의 공개 멤버는 `types.py`의 Protocol에 적힌 것뿐이다.

`writerFloor` 미달(`writer_floor`)이나 다른 메이저(`state_format_mismatch`)로 처리할 수 없는 wake 행은 due·시도 횟수·reason을 그대로 둔 채 `pending`으로 돌려놓는다. 그 사실은 큐 옆 `runtime-wake-deferred.json`에 행 revision·플러그인 버전·재확인 시각(300초 뒤)으로 남고, 같거나 낮은 버전의 호스트는 그 revision을 재확인 시각까지 due로 보지 않는다(worker·timer 기동 없음). 더 높은 버전의 호스트는 행의 원래 due에 처리한다. 그 기록을 쓸 수 없거나 이 호스트의 플러그인 버전을 읽을 수 없으면 행의 due를 재확인 시각으로 미뤄 worker를 연달아 다시 띄우지 않는다. 이때는 더 높은 버전의 호스트도 그 시각까지 기다린다. 호스트 설치 경로 밖의 사본은 live wake 큐를 열지 않는다.

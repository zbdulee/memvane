# 런타임

wake 큐, admission, lease와 자원 정책을 다룬다. 알 수 없는 wake kind는 보존한다.

구현: `qa_model_budget.py`, `resource_controls.py`, `resource_platform.py`, `runtime_admission.py`, `runtime_cpu.py`, `runtime_handlers.py`, `runtime_lease.py`, `runtime_monitor.py`, `runtime_wake_policy.py`, `runtime_work.py`, `wake_cancel.py`.

`wake_cancel.py`는 소유가 증명된 pending wake 한 행을 검증된 백업 뒤 한 트랜잭션으로 CAS 삭제하고 큐 DB의 `wake_cancellations`에 감사 행을 남긴다. 소유 증명은 호출자(install)가 하고, 트랜잭션 안의 재증명은 호출자가 넘긴 콜백으로 한다.

다른 도메인이 쓰는 자원 핸들(`Coordinator`, `WakeQueue`, `Supervisor`, `TrainingGuard`)의 공개 멤버는 `types.py`의 Protocol에 적힌 것뿐이다.

`writerFloor` 미달(`writer_floor`)이나 다른 메이저(`state_format_mismatch`)로 처리할 수 없는 wake 행은 due·시도 횟수·reason을 그대로 둔 채 `pending`으로 돌려놓는다. 그 사실은 큐 옆 `runtime-wake-deferred.json`에 행 revision·플러그인 버전·재확인 시각(300초 뒤)으로 남고, 같거나 낮은 버전의 호스트는 그 revision을 재확인 시각까지 due로 보지 않는다(worker·timer 기동 없음). 더 높은 버전의 호스트는 행의 원래 due에 처리한다. 그 기록을 쓸 수 없거나 이 호스트의 플러그인 버전을 읽을 수 없으면 행의 due를 재확인 시각으로 미뤄 worker를 연달아 다시 띄우지 않는다. 이때는 더 높은 버전의 호스트도 그 시각까지 기다린다. 호스트 설치 경로 밖의 사본은 live wake 큐를 열지 않는다.

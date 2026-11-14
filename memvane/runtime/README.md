# 런타임

wake 큐, admission, lease와 자원 정책을 다룬다. 알 수 없는 wake kind는 보존한다.

구현: `qa_model_budget.py`, `resource_controls.py`, `resource_platform.py`, `runtime_admission.py`, `runtime_cpu.py`, `runtime_handlers.py`, `runtime_lease.py`, `runtime_monitor.py`, `runtime_wake_policy.py`, `runtime_work.py`.

`writerFloor` 미달(`writer_floor`)이나 다른 메이저(`state_format_mismatch`)로 처리할 수 없는 wake 행은 due·시도 횟수·reason을 그대로 둔 채 `pending`으로 돌려놓는다. 그 사실은 큐 옆 `runtime-wake-deferred.json`에 행 revision·플러그인 버전·재확인 시각(300초 뒤)으로 남고, 같거나 낮은 버전의 호스트는 그 revision을 재확인 시각까지 due로 보지 않는다(worker·timer 기동 없음). 더 높은 버전의 호스트는 행의 원래 due에 처리한다. 호스트 설치 경로 밖의 사본은 live wake 큐를 열지 않는다.

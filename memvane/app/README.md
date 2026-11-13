# 앱 진입

setup, wake dispatch와 process 진입을 연결한다. 자식 프로세스는 entry/run.py를 사용한다.

구현: `runtime_dispatch.py`, `setup_entry.py`, `wake_entry.py`.

## 온보딩 단계의 재실행

`setup_workflow.py`(plan·prepare·rescan)와 `setup_activation.py`(finalize·activate·restore)의 각 단계는 임의 지점에서 멈춘 뒤 같은 명령을 다시 실행하면 같은 결과로 수렴한다.

- `prepare`는 raw 원문을 sync와 같은 상한(4,096 파일·64 MB)으로 먼저 스캔하고, 넘으면 빌드 전에 `raw_scan_limit_exceeded`로 거절한다.
- 영속 기록(`route.json`의 `stateNode`·`storeNode`, reader 기록의 `*_node`)은 재실행에서 inode로 대조하고 기록된 `st_dev` 바이트를 유지한다. plan의 `scopeId` seed는 프로젝트 `st_dev`를 그대로 쓴다.
- `finalize`는 게시 head 뒤에 위키 카드나 raw 원문이 바뀌었으면 빌드를 `building`으로 되돌리고 wake를 요청한 뒤 `initial_build_pending`을 낸다. 빌드가 다시 `ready`가 되면 다음 `finalize`가 활성화 전 reader 기록을 새 head로 다시 봉인한다. `activate`가 `initial_build_out_of_date`·`initial_build_head_changed`로 거절하면 `finalize`부터 다시 실행한다.
- `activate`는 포인터를 쓰기 전이면 이전 활성화가 남긴 비활성 discovery snapshot을 새로 증명한 snapshot으로 바꾼다.
- `build-rescan`(`index-build-rescan`)은 journal이 `prepared`이고 프로젝트 포인터가 없을 때만 동작한다. 선택 뒤에는 `initial_build_already_selected`로 거절하고 갱신은 sync owner가 맡는다.
- `restore`는 활성화 전 설치를 버린다(abandon): journal을 `restored`로 바꾸고 build 포인터와 `build.json`을 지워 새 install id의 `prepare`가 시작되게 한다. 활성화 뒤에는 첫 선택 쓰기 전까지 포인터를 거둔다. 첫 쓰기 뒤에는 attestation의 provider·모델·런타임이 이 호스트와 다를 때만 선택을 해제하고(`released`), 그 밖에는 `rollback_forward_only`다. 어느 경우에도 state root·store·journal·ledger는 남고 정리는 사용자 runbook·gc가 맡는다. `build.json`은 마지막에 지우므로, 그 뒤의 재실행은 `restored` journal을 읽고 `changesApplied: 0`을 낸다.

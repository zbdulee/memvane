# 앱 진입

setup, wake dispatch와 process 진입을 연결한다. 자식 프로세스는 entry/run.py를 사용한다.

구현: `runtime_dispatch.py`, `setup_entry.py`, `setup_workflow.py`, `setup_activation.py`, `wake_entry.py`.

- `setup_entry.py`와 `setup_workflow.py`는 인자를 해석하고 승인 플래그를 확인한 뒤 `install.api`의 온보딩 동작을 부른다.
- `setup_activation.py`는 `install.api.finalize_initial_build`에 app의 `laya-sync` dispatch 확인(`runtime_dispatch.resolve`)을 `resolve_sync_target` port로 넣는다.
- 온보딩 단계의 재실행 규칙은 `memvane/install/README.md`의 "온보딩 단계의 재실행"을 따른다.

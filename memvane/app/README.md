# 앱 진입

setup, wake dispatch와 process 진입을 연결한다. 자식 프로세스는 entry/run.py를 사용한다.

capture supervisor는 45초 제한 뒤 TERM을 보내고 최대 5초 뒤에도 살아
있으면 kill한 후 회수한다. worker의 정상 처리 상태는 worker 이벤트를
사용하며 supervisor가 `lost`로 중복 기록하지 않는다.

구현: `runtime_dispatch.py`, `setup_entry.py`, `setup_activation.py`, `wake_entry.py`.

- `setup_entry.py`는 인자를 해석하고 승인 플래그를 확인한 뒤 `install.api`의 온보딩 동작을 부른다.
- `setup_activation.py`는 `install.api.finalize_initial_build`에 app의 `laya-sync` dispatch 확인(`runtime_dispatch.resolve`)을 `resolve_sync_target` port로 넣는다.
- 온보딩 단계의 재실행 규칙은 `memvane/install/README.md`의 "온보딩 단계의 재실행"을 따른다.

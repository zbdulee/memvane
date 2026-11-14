# 앱 진입

setup, wake dispatch와 process 진입을 연결한다. 자식 프로세스는 entry/run.py를 사용한다.

구현: `runtime_dispatch.py`, `setup_entry.py`, `setup_workflow.py`, `setup_activation.py`, `wake_entry.py`.

- `setup_entry.py`와 `setup_workflow.py`는 인자를 해석해 `install.api`의 온보딩 동작과 `setup_activation.py`를 부른다.
- `setup_activation.py`는 finalize·activate·recover를 조립한다. 이 단계는 install 기록과 sync의 원문 관측(`sync.api.schema3_sources`·`schema3_wiki_cards`)을 함께 쓰고, sync가 install 위 층이라 install 안에 둘 수 없다. 단계별 판정 helper는 `install.api`에서 받는다.
- 온보딩 단계의 재실행 규칙은 `memvane/install/README.md`의 "온보딩 단계의 재실행"을 따른다.

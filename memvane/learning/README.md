# 학습

학습 선택, 추론 서비스와 selector 유지보수를 담당한다. 초기 활성화 후 detached 작업이 heal 다음 warm을 수행한다.

구현: `context_learning_auto.py`, `context_learning_cli.py`, `prepare_laya_inference.py`, `project_learning_setup.py`, `selector_maintenance.py`.

저장된 recall capture의 후보 출처는 별도 `learning.capture.sources` 진단
이벤트에 기록한다. `request_id`로 기존 표본과 연결하며 후보 id의 SHA-256과
`store`·`python`·`lexical` 값만 담는다. 표본 스키마·학습 입력은 그대로다.
capture가 꺼져 있으면 출처 이벤트도 쓰지 않는다.

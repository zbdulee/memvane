# 학습

학습 선택, 추론 서비스와 selector 유지보수를 담당한다. 초기 활성화 후 detached 작업이 heal 다음 warm을 수행한다.

base checkpoint 포인터는 `schema_version: 1`을 기록한다. 해당 키만 없는
기본 enrollment 포인터는 보정하고 receipt 재준비 후 warm을 수행한다.
다른 값이 달라진 포인터는 거절한다. selector 재시도는 최소 10분 간격이며
지속 실패 시 최대 1시간까지 지수 backoff를 적용한다. 자원 부족과
시작 경로·권한·IO 실패는 별도 고정 사유 코드로 기록한다.

기본 namespace의 adapter 경로만 달라진 정책은 heal의 내용·승인 범위
검증으로 재결속한다. 준비할 selector가 없으면 warm 없이 버전을 게시한다.
후보 apply의 resident 시작 실패는 재시도를 예약하며, 실행 중인 resident의
정체성·권한 거절은 후보 적용을 차단한다.

구현: `context_learning_auto.py`, `context_learning_cli.py`, `prepare_laya_inference.py`, `project_learning_setup.py`, `selector_maintenance.py`.

저장된 recall capture의 후보 출처는 별도 `learning.capture.sources` 진단
이벤트에 기록한다. `request_id`로 기존 표본과 연결하며 후보 id의 SHA-256과
`store`·`python`·`lexical` 값만 담는다. 표본 스키마·학습 입력은 그대로다.
capture가 꺼져 있으면 출처 이벤트도 쓰지 않는다.

# 구조 경계 검사

`context-map.json`은 `memvane/`, `entry/`, `hermes_adapter/`의 Python 모듈을 도메인·계층별로 분류한다. `pinned_paths`는 host 진입점, manifest, provider와 worker 등 필수 경로를 기록한다.

`test/helpers/structure_guard.py`는 정적 import 그래프, 함수 크기와 고정 경로를 조사한다. 도메인 API 경계와 기준선을 함께 관리한다.

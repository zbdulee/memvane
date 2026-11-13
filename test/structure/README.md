# 구조 경계 검사

`context-map.json`은 `core/`와 `hermes_adapter/`의 Python 모듈 및 루트 plugin 진입점을 context·계층·고정 경로 여부로 분류한다. 새 모듈은 표에 추가한다. `pinned_paths`는 번들 필수 경로, 엔진 subbundle, 실행 위치와 번들 간 진입점 및 보호 파일을 포함한다.

| context | 범위 |
|---|---|
| `config`, `diagnostics`, `shared` | 설정·경로, 진단, 공통 안전 헬퍼 |
| `store`, `journal`, `install`, `v6` | view/blob store, journal 선택, 설치 증명, v6 색인 |
| `runtime`, `upgrade`, `migration` | wake·서비스, 번들·상태 이동, shadow 전환 |
| `sync`, `host`, `wiki`, `learning`, `setup` | sync 게시, host route, wiki, 학습, setup 진입점 |

`contexts`의 숫자와 계층 순서 `domain → adapter → application → entry`는 낮은 방향으로의 import를 허용한다. 기존 역방향 import는 `baseline.json`에 호출 위치 종류(top/lazy)와 각 모듈 간 간선별 횟수로 기록한다. 기존 위반의 재사용은 허용하고 새로운 간선과 기존 간선 횟수 증가는 거절한다. 기존 모듈 분류·context 순위·진입점을 바꾸거나 상한 강제일을 늦추려면 기준선을 명시적으로 갱신하고 근거를 리뷰한다.

순수 `domain` 모듈은 I/O 모듈 import, 동적 import, 파일 열기 및 파일 접근 메서드를 사용하지 않는다. `pathlib`의 `PurePath` 계열은 허용한다. 분석은 정적 AST 검사이며, 넘겨받은 객체나 숨겨진 호출을 통한 I/O를 증명하지 않는다.

hook 진입점의 최상위 import 폐쇄는 내부 모듈과 직접 탐지한 외부 모듈 이름을 기준선과 비교한다. 함수 내부 지연 import는 이 폐쇄에 포함하지 않는다. `importlib`·문자열 동적 import, 조건의 실행 가능성, 외부 라이브러리의 재귀 의존성은 일반 import 그래프가 해석하지 않는다. 고정 경로 검사는 존재 여부를 확인하며 파일 내용 증명이 아니다.

파일 800줄·함수 150줄 상한은 2026-11-26까지 보고 전용이고 2026-11-27부터 강제한다. 기존 초과 항목은 기준선보다 증가하면 같은 방식으로 보고하거나 거절한다. 파일·함수 줄 수는 Python 소스의 물리 줄 수다. 범위는 context 표에 포함된 Python 파일이며 다른 형식은 포함하지 않는다.

검사는 전역 가드를 이용한다.

```bash
npm run test:focused -- test/structure-guard.test.mjs
```

기준선은 `test/helpers/structure_guard.py`의 `snapshot` 결과다. 기준선을 갱신할 때는 각 위반·분류·폐쇄·크기 차이와 사유를 검토하고 focused 검사를 실행한다. 이 자료는 실행 코드나 제품 상태를 import하거나 열지 않는다.

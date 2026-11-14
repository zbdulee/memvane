# 구조 경계 검사

`context-map.json`은 `memvane/`, `entry/`, `hermes_adapter/`의 Python 모듈을 도메인·계층별로 분류한다. `pinned_paths`는 host 진입점, manifest, provider와 worker 등 필수 경로를 기록한다.

`test/helpers/structure_guard.py`는 정적 import 그래프, 함수 크기와 고정 경로를 조사한다. 도메인 API 경계와 기준선을 함께 관리한다.

`v1_guard.py`는 G1~G8을 `issues`에 보고한다. 엄격 규칙은 `strict_issues`에 따로 보고하고, `--strict`를 줄 때만 종료 코드에 반영한다.

- G1(엄격): 다른 도메인 `api.py`·`types.py`에서 `_` 이름을 import하거나, 그 api에서 얻은 모듈·객체·호출 결과의 `_` 속성에 접근하거나, `__getattr__` 프록시 객체와 api에 정의되지 않은 이름(모듈 `__getattr__` 전달)을 쓰는 곳.
- G5(엄격): `api.py`의 모듈 이름(`_` 포함, dunder와 `__future__` 제외)과 클래스 멤버를 합친 속성이 30개를 넘는 경우, 모듈 `__getattr__`, `import_module`·`__import__`, `__getattr__`를 정의한 클래스, 서명이 `*args`·`**kwargs`뿐인 함수와 lambda.
- G9: `CREATE TABLE`이 있는 도메인을 그 테이블의 소유자로 보고, 다른 도메인 문자열 리터럴의 `FROM`·`INTO`·`UPDATE`·`DELETE FROM`·`JOIN` 뒤에 그 테이블 이름이 오는 곳.

`--rules G1,G9`는 보고할 규칙을, `--domain journal`은 경계를 넘어 닿는 api·테이블 소유 도메인을, `--source sync`는 위반이 있는 파일의 도메인을 고른다. `strict_by_domain`은 닿는 도메인별 건수다. 탐지기 자체 검사는 `tests/structure/guard-rules.test.mjs`(합성 코드, `tests/structure/guard_rules.py`)가 맡는다.

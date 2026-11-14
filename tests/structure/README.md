# 구조 경계 검사

`context-map.json`은 `memvane/`, `entry/`, `hermes_adapter/`의 Python 모듈을 도메인·계층별로 분류한다. `pinned_paths`는 host 진입점, manifest, provider와 worker 등 필수 경로를 기록한다.

`structure_guard.py`는 정적 import 그래프, 함수 크기와 고정 경로를 조사한다. 도메인 API 경계와 기준선을 함께 관리한다.

`v1_guard.py`는 G1~G8을 `issues`에 보고한다. 엄격 규칙은 `strict_issues`에 따로 보고하고, `--strict`를 줄 때만 종료 코드에 반영한다.

- G1(엄격): 다른 도메인 `api.py`·`types.py`에서 `_` 이름을 import하거나, 그 api에서 얻은 모듈·객체·호출 결과의 `_` 속성에 접근하거나, `__getattr__` 프록시 객체와 api에 정의되지 않은 이름(모듈 `__getattr__` 전달)을 쓰는 곳. 연결·lock을 쥔 핸들을 돌려주는 api 함수(`_HANDLE_SOURCES`: `store.api.open_store`, `journal.api.pin_schema3`의 첫 값, journal writer scope, runtime·provider 핸들)의 결과는 소유 도메인 `types.py`의 `typing.Protocol`(`store.types.ViewStoreHandle`, `journal.types.WriterHandle` 등)에 선언된 공개 멤버만 쓸 수 있고, 그 밖의 멤버는 `undeclared_handle_member`다. Protocol 메서드의 서명은 구현 클래스(`_HANDLE_CLASSES`) 메서드와 같아야 한다(`handle_signature`). 값 객체를 돌려주는 api 함수(`_VALUE_SOURCES`: `journal.api.resolve_endpoint`·`reader_selection`, `recall.api.query_endpoint`)의 결과는 소유 도메인 `types.py`의 dataclass·`__slots__` 클래스에 선언된 필드와 메서드만 읽을 수 있고, 그 밖은 `undeclared_value_member`다. 추적 범위는 한 scope 안의 대입·호출·첨자·`with`·`for`, 선언된 핸들·값 타입으로 주석을 단 매개변수, 같은 모듈 최상위 함수에 위치 인자(`*args` 앞)나 키워드 인자로 넘긴 매개변수다. 메서드·중첩 함수·다른 모듈로 넘기거나 반환한 값은 따라가지 않는다.
- G5(엄격): `api.py`의 모듈 이름(`_` 포함, dunder와 `__future__` 제외)과 클래스 멤버를 합친 속성이 30개를 넘는 경우, 모듈 `__getattr__`, `import_module`·`__import__`, `__getattr__`를 정의한 클래스, 서명이 `*args`·`**kwargs`뿐인 함수와 lambda.
- G9: `CREATE TABLE`이 있는 도메인을 그 테이블의 소유자로 보고, 다른 도메인 문자열 리터럴의 `FROM`·`INTO`·`UPDATE`·`DELETE FROM`·`JOIN` 뒤에 그 테이블 이름이 오는 곳. 층 방향 때문에 소유 도메인 api를 쓸 수 없는 읽기는 `_G9_READ_EXCEPTIONS`(파일·테이블, 근거 주석)에 둔다. 쓰기 키워드가 없는 문자열의 `FROM`·`JOIN`만 면제되고, 맞는 문자열이 없는 예외는 `unused_read_exception`이다.

- G8: 시험 파일의 `patch` 대상이 실제 모듈·속성인지 본다. `test/`, `tests/`, `memvane/**/tests/`의 Python 파일을 읽는다. `test/` 밖에서는 `mock.patch('…')`와 문자열 전체가 `memvane.` 점 경로인 값(patch 대상 표)도 대상으로 읽는다.
- 시험 G1(`--tests`): 시험이 이름을 부르는 범위. `memvane/<A>/tests/**`는 `memvane.<A>.*`, `memvane.kernel.*`, `tests.support.*`, 다른 도메인의 `api`·`types`만, `tests/<X>/**`는 `api`·`types`, kernel, `tests.support.*`와 자기 폴더만 부른다. import 문, 리터럴 `import_module`·`__import__` 인자, `patch`·`patch.object` 대상, 문자열과 `.mjs` 안의 Python import·patch 문장을 모두 센다. 다른 도메인 api의 `_` 이름은 `private_api`, 없는 `memvane.` 모듈은 `missing_module`이다. 예외는 `_TEST_G1_EXCEPTIONS`(시험 파일, 점 이름) 표에만 두고, 파일이 있는데 쓰지 않는 예외는 `unused_exception`이다. 가드 자기시험의 합성 소스가 있는 `tests/structure/`는 시험 G1을 보지 않는다. 출력 `tests`에는 파일별·폴더별 시험 G1·G8·미해석 patch 대상 수가 있다. 표준 라이브러리 대상(`time.time` 등)은 `external_patch_targets`로 따로 센다.

`--rules G1,G9`는 보고할 규칙을, `--domain journal`은 경계를 넘어 닿는 api·테이블 소유 도메인을, `--source sync`는 위반이 있는 파일의 도메인을 고른다. `--source`는 G8과 시험 결과도 그 소유자(도메인, `tests/<X>`, `test`)의 시험 파일로 좁힌다. `strict_by_domain`은 닿는 도메인별 건수다. 탐지기 자체 검사는 `tests/structure/guard-rules.test.mjs`(합성 코드, `tests/structure/guard_rules.py`)가 맡는다.

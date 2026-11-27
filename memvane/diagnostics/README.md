# 진단

설치 이벤트와 trace 기록을 다룬다. 진단 기록 실패는 hook을 실패시키지 않는다.

구현: `diagnostic_trace.py`, `event_log.py`, `job_telemetry.py`, `event_queries.py`, `event_summary.py`, `slo.py`.

## 이벤트 조회 (읽기 전용)

`event_log.read`가 돌려준 행을 파일 없이 가공한다. 쓰기와 파일 열기는 하지 않는다. `slo.install_size`와 `identity` 줄만 설치의 `route.json`과 위키 root 이름을 읽는다.

- `api.query_events(rows, view, *, hours, event, raw)`: `view`는 `recalls`·`errors`·`events`·`None`이다. `raw=True`면 먼저 가시성 규칙을 적용한다. `diagnostic.part` 행은 버리고, `diagnostic.header`는 고정 metadata만 남기고, 구형 설치가 저장한 `prompt.text`는 지운다.
- `api.summarize_events(rows, hours, *, unified)`: `memvane:logs` summary 화면의 줄 목록이다. 이벤트 수, hook 지연 백분위, recall semantic 비율(`semantic`·`fullCoverage`, `vectorSource`별), 단계 중앙값, sync 연속 실패와 점유 줄(설치별), 마지막 publish·error, teacher·학습 줄을 담는다. 저장된 문자열은 모두 terminal escape를 거친다.
- `api.slo_lines(rows, hours, *, state_root, text)`: 설치 하나의 SLO 줄이다. 기본은 dict 목록이고 `text=True`면 escape된 문자열이다. semantic 줄은 `SEMANTIC_USED = ('complete', 'partial')`, `fullCoverage`, `arenaFullCoverage`, `partial` 건수, `vectorSource == 'arena'` 비율을 센다.
- `api.reassemble_diagnostic(rows, diagnostic_id)`: 명시적으로 요청할 때만 진단 본문을 재조립한다. 결과는 `complete`, `incomplete`(ID 오류·누락·200행 초과·결함), `unavailable`(재조립기 실패) 중 하나다.

escape 규칙: 제어·format(Bidi 제어 포함)·surrogate·줄/문단 구분 문자는 `\xNN`·`\uNNNN` 형태 텍스트가 된다. JSON 출력은 같은 문자를 `\u` escape로 바꾼다. 저장된 바이트는 바꾸지 않는다.

`skills/logs/scripts/logs.py`는 이 함수로 화면을 그린다. `--all`의 다중 대상 해석(`registry.log_targets`)과 `--register`, `--learning-check`는 상위 도메인을 쓰므로 skill에 남는다. `skills/logs/scripts/logs_slo.py`는 `slo.py`의 이름을 다시 내보낸다.

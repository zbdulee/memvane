# MemVane Diagnostic Logging Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** 기본 비활성 상세 진단과 소유자 전용 통합 로그 조회로 오류·경고의 원인과 스택을 추적한다.

**Architecture:** 프로젝트 설정에서 현재 설치의 진단 동의를 읽고 기존 install event log에 유계 진단 조각을 쓴다. 검증된 프로젝트 목록으로 각 install log를 읽어 시간순으로 합치며 상세 원문은 명시적 요청에서만 복원한다. 진단은 원래 작업의 입출력·성패를 바꾸지 않는다.

**Tech Stack:** Python 3.9/3.13, Node 22/24/26 테스트 래퍼, POSIX private file·flock, 기존 `event_log`와 `memvane:logs`.

**Spec:** `docs/superpowers/specs/2026-11-09-diagnostic-logging-design.md`

## Global Constraints

- 기본 off, `settings.json`에서 boolean true로만 opt-in; 설정 불명·권한 오류는 off. 정상 이벤트는 계속 기록한다.
- 상세 on은 예외 메시지 원문을 허용한다. 독립 프롬프트·본문·locals 자동 수집과 외부 모델 전송은 금지한다.
- 운영 9개 설치에 켜고 PR10 완료 뒤 끈다. 구현 시험은 임시 HOME만 사용하며 운영 DB·상태 파일 직접 접근을 하지 않는다.
- 기존 event 4096 bytes/line·4 MiB rotation·0600/no-follow·best-effort 및 host output/exit 계약을 유지한다.
- 상세 최대 16 KiB, base64 조각 240자 이하·최대 96개. header는 version·ID·count·byte length·SHA-256·truncated를 갖는다.
- 통합 대상 최대 64개, 총 읽기 최대 16 MiB. registry는 `$XDG_CONFIG_HOME/memvane/log-targets.json` 또는 `~/.config/memvane/log-targets.json`, owner-only·0600·no-follow.
- 보호 파일 `core/laya_local_embed_provider.py`, `core/laya_local_embed_worker.mjs`, `hooks/hooks-codex.json`, `scripts/development_guard.py`와 resident service 구현을 변경하지 않는다.
- 전역 development guard 사용, 일반 검사는 순차, 리뷰는 검사 종료 뒤 단독. 실제 모델·운영 데이터를 시험에 쓰지 않는다.
- 각 단위는 base RED와 기존 REG를 남기고 수정 뒤 Python 3.13·3.9 GREEN을 기록한다. 알려진 py3.9 환경 실패와 시간 민감 실패는 계획된 절차대로 분리한다.

## Review Focus

1. 설정을 끄는 도중 이미 시작한 진단은 완료될 수 있으나 그 뒤 새 진단은 기록되지 않는다 → Task 1·2의 전환 경쟁 시험.
2. 메시지에 임의 문서·token·ANSI가 들어가도 off와 기본 조회에 원문이 노출되지 않는다 → Task 2·5의 sentinel·터미널 표시 시험.
3. 회전이나 부분 쓰기로 header·조각·원본 이벤트가 사라져도 완전한 스택으로 표시하지 않는다 → Task 2·5의 누락·중복·digest 시험.
4. 프로젝트 alias·stale route·symlink·권한 거절·64개 제한이 있어도 다른 설치의 유효 결과와 누락 사유를 보존한다 → Task 4·5.
5. resident·shadow 내부처럼 원인을 이미 잃은 경계는 origin stack을 원인 stack으로 속이지 않는다 → Task 3·6.

## File Map

- `core/config.py`: `diagnostics.enabled` 정규화와 기존 minimal 설정 보존.
- `core/event_log.py`, 신규 `core/diagnostic_trace.py`: 동의 판정·유계 예외/경고 캡처·조각 및 연결. 새로운 hook action·wake kind는 만들지 않는다.
- `core/laya_host_response.py`, `core/hook_main.py`, `core/laya_runtime_dispatch.py`, `core/wiki_compile_worker.py`, `hermes_adapter/core_bridge.py`: 기존 오류·경고 경계에서 진단을 연결한다. resident는 기존 이벤트 경계에서 정보가 남을 때만 캡처한다.
- 신규 `core/log_targets.py`, `skills/logs/scripts/logs.py`, `skills/logs/SKILL.md`: 소유자 registry, 안전한 통합 조회와 명시적 상세 표시.
- 신규 `test/diagnostic-trace.test.mjs`, `test/fixtures/diagnostic-trace-checks.py`, `test/diagnostic-targets.test.mjs`, `test/fixtures/diagnostic-targets-checks.py`; 기존 이벤트·host·worker 테스트를 필요한 범위에서 보강한다.
- `docs/settings.md`, `docs/architecture.md`, 사용자 안내: 현재 동작·보안·운영 설정과 조회를 설명한다.

## Task 1: opt-in 설정과 동의 주체

**Interfaces:** `config.load_project_config(cwd)['diagnostics']['enabled'] -> bool`; `diagnostic_trace.enabled(decision) -> bool`. `decision`은 검증된 선택 route 또는 main route를 가진 linked worktree만 허용한다.

- [ ] RED: minimal/full 설정에서 누락·false·true·문자열 `"true"`·손상·읽기 실패를 검사하고 오직 boolean true만 켜진다고 단정한다. main/linked worktree·stale route·설정 재읽기·진행 중 writer 경계를 시험한다.
- [ ] 관련 기존 config/route 테스트를 base에서 통과시키고 RED를 실행해 기대 실패를 기록한다.
- [ ] `core/config.py`, `core/diagnostic_trace.py`에 설정과 fail-closed 동의 판정을 구현한다. route identity·collection digest와 reader 설정 선택은 바꾸지 않는다.
- [ ] `npm run test:focused -- test/config.test.mjs test/laya-index-host-route.test.mjs test/diagnostic-trace.test.mjs`를 양 Python에서 순차 실행하고 명시 경로만 commit한다. 새 진단 옵션이 collection identity와 reader 설정 봉인을 바꾸지 않는지 확인한다.

## Task 2: 유계 traceback 조각과 복원

**Interfaces:** `diagnostic_trace.capture(decision, *, event, reason, exc=None, warning=False) -> str | None`; `diagnostic_trace.reassemble(rows, diagnostic_id) -> dict`. ID는 32자리 hex, 상세 모드 off 또는 안전 증명 실패 시 `None`이다.

- [ ] RED: 중첩 예외와 warning caller stack에서 type·file·function·line·raw message 및 ID 연결을 확인한다. off·중간 off 전환에서는 원문 sentinel이 이벤트 파일에 없음을 확인한다.
- [ ] RED: 16 KiB 초과·UTF-8 분할·96개 상한·4096-byte 행·중복/누락/순서/digest 오류·header 유실·rotation을 주입해 `truncated` 또는 `incomplete`를 단정한다. locals·별도 prompt/body는 기록하지 않는다.
- [ ] 기존 `event_log` REG를 base에서 통과시키고 RED를 기록한다. `capture`는 수집 전과 쓰기 직전에 동의를 확인한다. ID·조각은 기존 event logger와 lock·rotation을 재사용하며 exception code·host 결과에 영향을 주지 않는다.
- [ ] `npm run test:focused -- test/event-log.test.mjs test/diagnostic-trace.test.mjs`를 양 Python에서 실행, 실수로 수집된 실제 민감 데이터 없이 합성값으로만 검증 후 commit한다.

## Task 3: 오류·경고 경계 연결

**Interfaces:** Task 2 `capture`를 기존 이벤트의 `diagnostic_id`에 연결한다. 예외가 없는 경고는 `warning=True`로 origin stack임을 표시하고, 원인이 이미 유실되면 `trace_unavailable`을 기록한다.

- [ ] RED: 합성 `ValueError`가 recall `selected_proof_or_query_failed`로 맵핑될 때 on에서는 원문과 호출 프레임이 같은 진단 ID로 보이고 off에서는 누출되지 않는다. hook 반환·stderr·exit는 base와 같다.
- [ ] RED: sync/compile 자식 비정상 종료에서 기존 tail 4096 bytes의 범위를 명시하고 전체 traceback이라고 주장하지 않는다. exception이 없는 vector/selector fallback은 origin stack, `target_preparing`은 warning, 정상 `no_candidates`·migration gate 대기는 오류가 아니다.
- [ ] RED: verify·bundle·Hermes 경계에서 실패 예외와 진단 기록 실패를 주입해 원래 처리 결과가 같고 각 예외 체인이 보존됨을 단정한다. resident·shadow의 이미 삼킨 예외는 없음으로 표시한다.
- [ ] 기존 host/adapter/worker REG를 base에서 기록하고 지정 경계에 최소 연결한다. protected 파일·resident·shadow 구현은 바꾸지 않는다.
- [ ] `npm run test:focused -- test/event-log.test.mjs test/laya-host-response.test.mjs test/hermes-plugin-adapter.test.mjs test/diagnostic-trace.test.mjs`를 실제 파일 존재 여부를 확인한 뒤 양 Python에서 실행하고 commit한다.

## Task 4: 소유자 registry

**Interfaces:** `log_targets.register(projects: list[str]) -> dict`; `log_targets.resolve() -> list[dict]`. 각 행은 requested project·canonical root·install ID·verified state root 또는 고정 refusal을 가진다.

- [ ] RED: 임시 HOME의 9개 합성 설치 등록, alias 동일 install 중복 제거, 요청별 stale·미온보딩·누락·64개 초과를 단정한다. 한 설치가 실패해도 다른 설치는 반환한다.
- [ ] RED: `XDG_CONFIG_HOME` 경로·owner·mode·symlink·hardlink·변경 경쟁을 주입한다. registry에는 프로젝트 root와 expected install ID 외 메시지·본문이 없다.
- [ ] registry 쓰기는 owner-only·0600, no-follow, 유계·원자적 업데이트로 구현하고 읽을 때 현재 route와 install binding을 재검증한다. 손상·부분 등록은 명시한다. registry는 각 행이 version·upsert/remove·canonical root·install ID를 담는 유계 UTF-8 JSONL이다. registry 파일 자체의 안정된 inode에 flock을 잡고 유효 행을 append·fsync하며 별도 lock 파일이나 inode 교체는 하지 않는다. 중단으로 마지막 행이 불완전하면 lock 아래 그 꼬리만 제거하고 이전 완전 행을 보존한다. 중간 손상·한도 초과는 fail-closed로 표시하고, 9개 등록은 항목별 결과를 보고해 부분 등록을 숨기지 않는다. 동시 등록과 중단 복구를 합성 fixture로 고정한다.
- [ ] `npm run test:focused -- test/diagnostic-targets.test.mjs`를 양 Python에서 실행 후 commit한다.

## Task 5: 통합 조회·원문 공개 경계

**Interfaces:** `logs.py`에 통합 조회 옵션과 명시적 상세 옵션을 추가한다. 기본·일반 JSON은 raw 조각을 출력하지 않는다. 기존 단일 설치 호출과 출력은 호환한다.

- [ ] RED: 9개 로그를 UTC 시각순으로 합치고 호스트·설치·작업·사유·진단 ID를 보여준다. 16 MiB 예산을 공평하게 나누고 손상·누락·권한 거절을 설치별로 표시한다.
- [ ] RED: 일반 텍스트·JSON에서 raw sentinel이 없고 상세 요청에서만 복원된다. ANSI·제어 문자는 화면에서 무해하게 인코딩하고 저장 바이트는 변하지 않는다. 부분 조각은 `incomplete`다.
- [ ] 기존 logs skill REG를 base에서 확인하고 registry·`reassemble`을 연결한다. 자동 추출 wiki tail과 외부 전송에 raw가 들어가지 않게 한다.
- [ ] `npm run test:focused -- test/event-log.test.mjs test/diagnostic-targets.test.mjs test/diagnostic-trace.test.mjs`를 양 Python에서 실행 후 commit한다.

## Task 6: 범위 감사·문서·운영 증거

- [ ] 제품의 오류·경고 경계를 표로 열거해 Task 3가 처리하는 실제 예외, origin stack만 가능한 경로, unavailable 경로와 이유를 구분한다. Claude·Codex·Hermes와 현 Antigravity 지원 범위를 확인한다.
- [ ] `docs/settings.md`, `docs/architecture.md`, `skills/logs/SKILL.md`와 사용자 안내를 현재형으로 갱신한다. 원문에 민감정보가 포함될 수 있으며 off가 과거 기록 삭제를 뜻하지 않음을 명시한다.
- [ ] 본문 없는 synthetic 예외로 host별 실패·경고 통합 검사를 만들고 RED→GREEN, 양 Python 결과를 기록한다. protected 네 파일과 resident·shadow diff 0을 확인한다.
- [ ] 최종 PR head에서 Python 3.13·3.9 전체 검사를 guard 단독 1800초로 실행하고 초과하면 동일 트리의 `--test-shard=1/3`, `2/3`, `3/3`을 순차 실행한다. 실패·재실행을 모두 기록한다. 모든 검사 종료 후 read-only 독립 리뷰를 받고 실제 지적은 수정·재검증·필요한 범위 재리뷰한다. 7개 manifest의 단조 증가 버전 bump를 마지막 커밋으로 둔다. Node22/24/26 상태와 제한시간 실패를 그대로 보고한다. 9개 설치 사전 점검·백업 여유·기존 worker 조율 뒤 호스트 설치 및 진단 opt-in을 적용하고 통합 조회·각 설정을 확인한다. 실제 오류를 만들지 않는다. PR10 완료 뒤 9개 설정을 끄고 각 확인을 기록한다.

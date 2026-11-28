# UI

로컬 웹 대시보드의 백엔드다. `127.0.0.1`에만 바인딩하는 사용자 단위 싱글톤 HTTP/SSE 서버, 읽기 전용 조회 DTO, `entry/run.py ui` 명령을 담는다. 설계는 [로컬 웹 대시보드 설계](../../docs/design-local-web-ui.md)를 따른다. 계층 번호는 12이며 다른 도메인의 `api.py`·`types.py`와 `memvane/kernel/`만 import한다. `memvane/host/`와 `hermes_adapter/`는 이 도메인을 import하지 않는다.

구현: `types.py`(값·상수, I/O 없음), `platform_io.py`(상태 경로·0600 파일·`server.lock`·프로세스·브라우저 오픈), `preferences.py`, `auth.py`, `sse.py`, `lifecycle.py`, `local_client.py`, `read_adapters.py`, `server.py`, `cli.py`.

## 상태 파일

`<state base>/ui/`(`memvane_paths.state_base(home) / 'ui'`, 0700)에 둔다. 모두 1.x 계약 밖 파생물이라 지워도 된다.

- `server.json`(0600, schema `memvane-ui-server-v1`): `pid`·`port`·`bundleRoot`·`bundleVersion`·`startedAt`·`instanceNonce`·`controlSecret`. 서버가 listen한 뒤 직접 쓰고, 종료할 때 자기 `instanceNonce`일 때만 지운다.
- `server.lock`: 기동·교체를 직렬화하는 `flock`.
- `preferences.json`(0600, schema `memvane-ui-preferences-v1`): 기본값은 `autoOpenOnSessionStart: false`, `idleShutdownMinutes: 30`, `autoOpenCooldownMinutes: 240`, `reconnectGraceSeconds: 10`, `readOnlyDefault: true`. 없거나 형식이 틀리면 기본값으로 읽는다(자동 열기는 꺼진 채).
- `auto-open-state.json`(0600): `lastAutoOpenAt`·`lastUserClosedAt`.

시험 프로세스(`QMD_SETUP_GUARD_FIXTURE=1` 또는 `MEMVANE_TEST_RUNTIME_ROOT`)는 HOME이 계정 실제 홈이면 이 디렉터리를 읽거나 쓰지 않고(`ui_fixture_live_home`), 브라우저를 열지 않는다.

## 수명주기

`lifecycle.ensure_server`는 `server.lock` 아래에서 기록된 서버의 pid 생존과 `GET /api/internal/status`의 `instanceNonce` 일치를 확인해 재사용한다. 실행 중인 번들보다 버전이 엄격히 높거나(`version_key` 비교, `bundle_lifecycle.newer`와 같은 규칙) 실행 중인 `bundleRoot`가 사라졌거나 손상됐을 때만 drain 후 교체한다. 같은 버전이나 낮은 버전의 호스트는 기존 서버를 재사용한다. 새 서버는 `python3 -B`, `DEVNULL`, `close_fds=True`, 새 세션으로 분리 기동한다.

SSE 연결이 0개이고 마지막 연결 종료 후 `reconnectGraceSeconds`가 지났으며 마지막 요청 후 `idleShutdownMinutes`가 지나면 서버가 스스로 종료한다.

## 요청 보안

- `Host`는 정확히 `127.0.0.1:<port>` 또는 `localhost:<port>`여야 한다(정적 파일 포함).
- `/api/health`를 뺀 모든 `/api/*`는 `X-MemVane-UI: 1`이 필요하다. `Sec-Fetch-Site`가 있으면 `same-origin`, `Origin`이 있으면 페이지 origin과 같아야 한다.
- 브라우저 엔드포인트는 `POST /api/auth/exchange`로 1회성 code(`secrets.token_urlsafe(32)`, 60초 또는 수동 URL 120초, 한 번만 소비)를 `memvane_ui_session` 쿠키(`HttpOnly; SameSite=Strict; Path=/`)로 바꾼 뒤 쓴다. 최근 16개 세션을 LRU로 유지한다.
- `/api/internal/*`는 `server.json`의 `controlSecret`을 `X-MemVane-Control`로 보내야 한다(`hmac.compare_digest`).
- 모든 응답에 `Cross-Origin-Resource-Policy: same-origin`, `X-Content-Type-Options: nosniff`, `Cache-Control: no-store`, `Referrer-Policy: no-referrer`를 붙이고 HTML에는 CSP를 더한다.
- 요청 본문은 1 MiB, 동시 요청은 32개, SSE 연결은 16개까지다(초과 시 각각 `413`·`503`·`429`). `OPTIONS`·`DELETE`·`PATCH`·`HEAD`는 `405`다.

`local_client.py`는 `socket.create_connection(('127.0.0.1', port))` 위의 HTTP/1.1 요청(`Connection: close`)만 보낸다.

## 엔드포인트 (Phase 1)

| 경로 | 인증 | 내용 |
|---|---|---|
| `GET /api/health` | 없음 | `{"status": "ok"}`만 |
| `POST /api/auth/exchange` | code | 세션 쿠키 발급 |
| `GET /api/session` | 세션 | 번들 버전, SSE 수, 설치 캐시 여부, 읽기 전용, preferences |
| `GET /api/overview`, `POST /api/overview/reconcile` | 세션 | 레지스트리 스냅샷(`reconcile=False`/`True`), 설치별 24시간 요약, 경고 배너, 카드 수 |
| `GET /api/installs/<id>/summary` | 세션 | SLO 줄, summary 줄, 위키 원장 대기 수 |
| `GET /api/installs/<id>/recalls` | 세션 | `recall`·`recall.edit` 타임라인(`hours`·`limit`·`outcome`·`host`·`target`) |
| `GET /api/installs/<id>/errors?details=0\|1` | 세션 | 오류·거절 행, `details=1`일 때만 진단 본문 재조립 |
| `GET /api/targets/<targetId>/wiki` | 세션 | 위키 카드 목록(`status`·`q`·`limit`)과 원장 요약. 카드마다 `wiki.card.check_card`의 `stale` 판정과 `staleReason`(`source_deleted`: 원문 파일 없음, `source_changed`: 해시 불일치)을 준다. `unknown` 판정은 stale이 아니다 |
| `GET /api/targets/<targetId>/wiki/card?rel=` | 세션 | 카드 메타데이터·auto/수동 블록·연관 카드·근거 위치 |
| `GET /api/targets/<targetId>/wiki/source?rel=&highlights=` | 세션 | 체크아웃 경계(`O_NOFOLLOW`) 내 원문 파일과 강조 범위 |
| `POST /api/targets/<targetId>/query-playground` | 세션 | 부작용 없는 dry run(`prompt`·`useSemantic`·`useSelector`) 4단계 비교 |
| `GET /api/runtime` | 세션 | wake 대기열(read-only), resident 증명·worker. 디스크 집계 없이 즉시 응답하며 `disk`는 빈 배열 |
| `GET /api/runtime/disk` | 세션 | 디스크 사용량. 60초 TTL 프로세스 캐시를 쓰고 `?refresh=1`이면 다시 집계(`refresh`는 `0`·`1`만, 그 밖은 400 `refresh_invalid`) |
| `GET /api/installs/<id>/learning`, `POST /api/installs/<id>/learning/check` | 세션 | Selector 승격 게이트·지표·teacher 및 on-demand 무결성 점검 |
| `GET /api/events/stream` | 세션 | SSE(`heartbeat`·`session_started`·`events_updated`·`runtime_updated`·`server_draining`) |
| `PUT /api/preferences`, `POST /api/shutdown` | 세션 | preferences 변경, 서버 종료 |
| `/api/internal/status`·`code`·`shutdown`·`session-started` | control secret | CLI 제어 |

`ui/dist/`가 없으면 `/`는 안내 HTML을 주고 API는 그대로 동작한다.

## 조회 경계

`read_adapters.py`는 쓰기·잠금·프로세스 기동을 하지 않는다. `targetId`는 main checkout이 `<installId>`, 연결 worktree가 `<installId>:<target_root SHA-256 앞 12 hex>`다. 요청마다 레지스트리 행으로 후보 경로를 찾고 `journal.api.select_route`로 route 상태·`installId`·`scopeId`를 다시 증명한 뒤에만 그 설치의 파일을 읽는다. Overview는 git 프로세스를 실행하지 않는다. main checkout은 연결 worktree 탐색 없이 증명하고, worktree 행은 레지스트리 행으로 나열하며, 브랜치명은 체크아웃 루트에서 위로 올라가며 처음 만난 `.git`의 `<gitdir>/HEAD`를 `O_NOFOLLOW`로 4 KiB까지 읽어 `<name>`·`detached@<short7>`·`non-git`·`unknown`으로 표시한다. 탐색은 git의 기본 탐색처럼 파일 시스템 경계에서 멈추고, HOME 아래 루트는 HOME 위로 올라가지 않는다(코어 `checkout_view._dotgit`은 `/`까지 올라가지만 경계 너머에서는 `git rev-parse`가 실패해 브랜치를 얻지 못한다). 저장소 하위 폴더 프로젝트는 그 저장소의 브랜치를 보인다. `non-git`은 그 범위에 `.git`이 없다는 뜻이다. 브랜치와 카드 수는 루트 디렉터리가 있는 `active` 행만 읽고, 그 밖의 행은 `unknown`이며 프런트가 상태 라벨로 바꿔 보인다.

체크아웃 노드(main·worktree)의 `viewState`는 레지스트리 status·reason과 route 증명으로 정한다. `ready`(active, 증명됨), `proof_failed`(루트는 있으나 route 재증명 실패), `unreadable`(active인데 루트를 관측할 수 없음, 예: 권한 거부), `path_missing`(`missing_pending`, `detached/missing`, 루트가 사라진 active 행), `released`(`detached/released`·`detached/withdrawn`), `moved`(`detached/root_moved`), `conflict`, `unknown`(그 밖의 status·reason 조합)이다. `stateReason`은 `proof_failed`의 이유 코드, `unreadable`의 errno 이름, `unknown`의 `<status>/<reason>`을 담는다. main은 Overview가 실시간으로 증명하고, 증명을 건너뛴 행(`max_installs` 초과)은 레지스트리의 `proof`(`unproved:<code>`)를 쓴다. `view_preparing`은 프런트 계약에 있으나 레지스트리만 읽는 Overview는 내지 않는다. 프런트는 `path_missing`·`released`·`moved`·`conflict` 행을 기본으로 숨기고 "해제·없는 체크아웃 n개 보기" 토글(사이드바·Overview 공용, 브라우저 localStorage에 저장)로 펼친다. 선택된 행과, 보이는 worktree가 남은 숨김 main(그룹 머리)은 늘 보인다. `ready`·`view_preparing`이 아닌 행은 서버 target 증명에 실패하므로 선택과 Wiki·Recall 바로 가기를 막고 툴팁에 상태를 적는다. 이벤트 행은 `diagnostics.api`의 가시성 규칙(`diagnostic.part`·저장된 `prompt.text` 제외)을 거친다.

## 명령

`entry/run.py ui {open,status,stop,serve,pref}`. 모든 명령은 stdout에 JSON 하나를 쓴다. `open`의 `url`에는 1회성 code가 없다. 브라우저를 열지 않았을 때(`--no-open`, 헤드리스·SSH·시험 환경, 오픈 실패)만 120초 `manualUrl`을 준다. `serve`는 포그라운드 실행이며 사람이 보는 터미널(stderr)에만 수동 접속 URL을 한 번 출력한다.

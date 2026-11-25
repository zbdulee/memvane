# `docs/design-local-web-ui.md` 제1차 독립 아키텍처·보안·호환성 리뷰

- **검토 일시**: 2026-11-24
- **검토 대상**: [design-local-web-ui.md](../design-local-web-ui.md) (1차 초안)
- **검토 방식**: 독립 컨텍스트(`UI Architecture Reviewer`, Read-Only)로 저장소 구조 가드·계약·도메인 API·보안·크로스 플랫폼 교차 대조

---

## 종합 판정: **REVISE**

전체적인 2분할 구조(`ui/` + `memvane/ui/`), 1.x 영속 계약 무변경 원칙, localhost 바인딩 및 fragment 기반 세션 교환 방향은 타당하다. 그러나 현재 초안은 저장소의 구조 가드(`G3`/`G5`/`G6`/`G9`, `static.test.mjs`, `manual-skills.test.mjs`, `doc-links.test.mjs`), 실제 도메인 API 계약(`recall`, `learning`, `runtime`, `registry`, `wake_entry`), worktree 식별 모델, HTTP/SSE/번들 혼재 수명주기, 크로스 플랫폼 가정에서 다수의 **P0 충돌**을 내포하고 있어 구현 착수 전 설계 개정이 필수적이다.

---

## 심각도별 지적 사항

### P0 (구현 전 설계 수정 필수 / 치명적 충돌·취약점)

#### [P0-01] `memvane/kernel/` G3 한도 고갈 및 미존재 헬퍼 전제
- **설계 문서 위치**: §5.1, §8.2
- **실제 코드 근거**:
  - `tests/structure/v1_guard.py:1154-1162` (`G3`: `memvane/kernel/` 내 `__init__.py` 제외 모듈 ≤ **14개**, 총 줄 수 ≤ **1,500줄**, 파일당 ≤ 250줄)
  - `memvane/kernel/*.py` 실측: `__init__.py` 제외 **14개 모듈 / 총 1,499줄** (모듈 여유 **0개**, 줄 수 여유 **1줄**)
  - `memvane/kernel/memvane_paths.py:17,36-43,74,93,140,146,166` (top-level `import pwd`, `os.getuid()` 사용; `support_base()`·`cache_base()` 함수 미존재)
- **문제 설명**: 설계는 §8.2에서 OS 종속 경로(`state_base`, `support_base`, `cache_base`), 파일 잠금, PID 생존 확인, detached spawn 추상화를 `memvane/kernel/`에 두어 `memvane/ui/`가 소비한다고 가정하나, `kernel/`은 이미 모듈 수·줄 수 한도(14개/1,499줄)에 도달해 2줄만 추가해도 `G3`가 실패한다. 또한 `memvane_paths.py`에 `support_base`/`cache_base`가 실제로 없다.
- **구체적 수정 권고**:
  1. `ui` 전용 플랫폼 추상화(브라우저 오픈, 프로세스 생존 확인, TCP 소켓/`server.lock`)는 `memvane/ui/` 내부(예: `platform_io.py`)에 격리하고, 1.x 단계에서 `kernel/` 모듈 수·줄 수 증설을 전제하지 말 것.
  2. Windows 본격 포팅 시 `G3` 상한 조정 또는 비핵심 유틸의 도메인 이관 계획을 별도 항목으로 명시할 것.

#### [P0-02] 도메인 `api.py` G5 30개 한도 고갈 및 G1/G9 경계 충돌 (`learning`, `runtime`, `store`, `settings`, `wiki/workspace`)
- **설계 문서 위치**: §2.2, §4.1.4, §6.2
- **실제 코드 근거**:
  - `tests/structure/v1_guard.py:462,973-1036,1150,1199-1201`, `tests/structure/static.test.mjs:196-200`
  - `v1_guard.py --strict` 기준 속성 수: `memvane/learning/api.py` **30/30**(여유 0), `memvane/settings/api.py` **30/30**(여유 0), `memvane/store/api.py` **30/30**(여유 0), `memvane/provider/api.py` **29/30**(여유 1), `memvane/journal/api.py` **51/51**(고정), `memvane/runtime/api.py` **42/42**(고정), `memvane/wiki/workspace/api.py` **35/35**(고정), `memvane/diagnostics/api.py` **20/30**(여유 10), `memvane/recall/api.py` **17/30**(여유 13)
  - `skills/logs/scripts/logs.py:706` (`from memvane.learning.context_learning import invariants`)
  - `memvane/runtime/runtime_work.py:128-145,199-203` (`WakeQueue.__init__`·`pending()`)
- **문제 설명**:
  1. `skills/logs/scripts/logs.py`는 `memvane/` 밖이라 `invariants.project_counts`를 직접 import하지만, `memvane/ui/`는 `G1` 대상이므로 `learning/api.py`·`types.py` 외 모듈(`invariants`, `teacher_state`, `learning_db`, `learning_profile`)을 import할 수 없다. 그러나 `learning/api.py`는 이미 **30/30**이라 새 공개 함수(`learning_snapshot`, `learning_check` 등)를 추가하면 `G5`가 실패한다.
  2. `memvane/ui/`가 `learning.sqlite3`나 `runtime-wakes.sqlite3`를 직접 SQL로 조회하면 `G9`(`v1_guard.py:1039-1074`)가 실패한다.
  3. `runtime.api.wake_queue().pending()`을 호출하면 `WakeQueue.__init__`이 **`require_cached_plugin_for_live_write()`**를 강제해 개발 체크아웃 실행 시 `ValueError`로 실패하고(`runtime_work.py:131-134`), `pending()` 자체도 DB/테이블 생성(`O_CREAT|O_RDWR`) 및 `_drop_retired`/`_promote_overflow` **쓰기**를 수행한다(`runtime_work.py:139-151,201-202`). 또한 `runtime/api.py`는 42개로 핀 고정되어 있어(`static.test.mjs:197`) 새 함수를 임의로 추가할 수 없다.
- **구체적 수정 권고**:
  1. `learning/api.py`, `runtime/api.py`의 기존 세부 함수 통합 여부(예: 좁은 상태 조회 함수를 단일 snapshot 엔트리로 통합해 슬롯 확보)를 설계에 명시해 `G5` 한도를 지키는 방법을 확정할 것.
  2. `runtime-wakes.sqlite3` 조회를 위해 `runtime` 도메인에 쓰기·`require_cached_plugin_for_live_write` 없는 순수 read-only 조회 경로(`_query_only_uri` + `PRAGMA query_only=ON`)를 추가하고 `runtime/api.py` 및 `static.test.mjs`의 핀 기준 처리 방안을 결정할 것.

#### [P0-03] `static.test.mjs`의 `DOWNLOADERS` 및 `ADDRESSES` 검사와 로컬 HTTP 통신·링크 검증 충돌
- **설계 문서 위치**: §3.4, §5.2, §5.4, §9.1
- **실제 코드 근거**:
  - `tests/structure/static.test.mjs:225-241,361-373` (`fetchedCode()`가 `memvane/`, `entry/`, `hooks/`, `hermes_adapter/`, `skills/*/scripts/`, `scripts/`의 모든 `.py` 스캔)
  - `tests/structure/static.test.mjs:402-423` (`ADDRESSES`에 등록되지 않은 **모든 `https://` 문자열**과 `127.0.0.1`/`localhost` 외의 `http://` 문자열을 주석·문자열 포함 전면 거부)
  - `tests/structure/static.test.mjs:472-501` (`DOWNLOADERS` 정규식이 `http.client`, `urllib.request.Request`, `urlopen`, `https?.(request|get)(`를 전부 탐지해 `TOOL_CALLS`에 없으면 실패)
- **문제 설명**:
  1. `entry/run.py ui open/status/stop` 및 `SessionStart` opt-in 게이트(`memvane/host/`)가 실행 중인 서버의 `GET /api/health`·`POST /api/shutdown`에 HTTP 요청을 보낼 때 표준 라이브러리 `http.client`나 `urllib.request`를 사용하면 `static.test.mjs:487`이 **즉시 실패**한다.
  2. `memvane/ui/*.py` 내부의 주석·docstring·CSP/Origin/마크다운 검증 문자열에 `https://` 리터럴이 단 한 번이라도 들어가면 `ADDRESSES` 검사(`static.test.mjs:409-414`)가 실패한다. IPv6 `http://[::1]` 리터럴 역시 line 408 정규식(`/http:\/\/(?!127\.0\.0\.1|localhost)/`)에 걸려 실패한다.
- **구체적 수정 권고**:
  1. `memvane/ui/`·`memvane/host/`의 로컬 loopback(`127.0.0.1`) health/control 프로브는 `http.client`/`urllib.request` 대신 **원시 `socket.create_connection(('127.0.0.1', port))` 기반 최소 HTTP/1.1 클라이언트**로 구현해 `DOWNLOADERS` 접촉을 원천 제거하거나, `static.test.mjs`에 loopback 전용 명시적 예외 규칙을 설계에 포함할 것.
  2. Python 코드 내 `https://` 문자열이 필요하지 않도록 링크 스킴 검증은 프론트엔드(`ui/src/`)에 두거나 `('http', 'https')` 분리 스킴 검사를 사용할 것.

#### [P0-04] `installId` 단일 키 모델이 연결 git worktree를 구분하지 못함
- **설계 문서 위치**: §4, §6.2, §7.4
- **실제 코드 근거**:
  - `memvane/registry/memvane_registry.py:76-92` (`UNIQUE(install_id, target_root)`)
  - `memvane/registry/memvane_registry.py:492-527` (worktree 행의 `install_id`는 부모 main checkout과 동일한 `proof['installId']`이며 `target_root`만 다름)
  - `memvane/journal/route_selection.py:229-232` (`worktree` route는 자체 `installId`가 없고 `mainRoute`의 `installId`를 공유하며 `worktreeProjectRoot`로 구분)
- **문제 설명**: 하나의 `installId`(`[0-9a-f]{32}`) 아래에 main checkout 1개와 연결 git worktree 여러 개가 공존한다. 따라서 `/api/projects/<installId>/wiki`, `/api/projects/<installId>/wiki/source`, `/api/projects/<installId>/query-playground`, `#overview?project=<installId>`는 어느 worktree의 `.memvane/wiki/`, 원문 파일, schema3 view를 조회/질의할지 특정할 수 없다.
- **구체적 수정 권고**:
  - 프로젝트/체크아웃 식별자를 `installId` 단독에서 **`targetId` (`main`: `<installId>`, `worktree`: `<installId>:<worktreeHash12>` 또는 `installId` + `?target=<worktreeHash12>`)**로 재설계하고, 매 요청마다 `target_root`를 `select_route(target_root)`로 재증명하여 `installId` 및 `scopeId` 일치를 검증할 것.

#### [P0-05] Query Playground가 실제 `memvane/recall` 파이프라인과 불일치 (중간 단계 손실·side-effect·전역 deadline 오염)
- **설계 문서 위치**: §4.1.2, §6.2, §10.2.4
- **실제 코드 근거**:
  - `memvane/recall/reader_dispatch.py:29-43,474,486-487` (`_fused_sources`가 `lexical`과 `nearest`를 RRF 융합해 상위 15개만 남기고, `ReaderResult`는 고정 8개 `__slots__`만 보유)
  - `memvane/recall/domain/rules.py:96-99` (`_fuse_verified_lists`가 최종 `score`를 `1 / rank`로 덮어쓰며 원래 BM25/RRF 점수 및 개별 `lexical` 순위를 폐기)
  - `memvane/recall/selected_recall.py:160,165,175,184,190,245,259,277,298,303`
  - `memvane/recall/recall_scope.py:102-117,145-244` (`_wake_provider`가 `writer_allowed=False`여도 `_restart_withdrawn_owner` → `resident.api.ensure_running` 호출)
  - `memvane/recall/verified_rows.py:150,159-163,181,186,196` (`ensure_service`, `hook_budget.deadline()`, `print(..., file=sys.stderr)`)
  - `memvane/kernel/hook_budget.py:10,26-67` (`_deadline`이 스레드 격리가 아닌 **프로세스 전역 변수**)
  - `memvane/learning/context_learning/selection_read.py:66-105` (`select_published`가 `ensure_service`를 통해 수 GB 학습 추론 서비스를 기동할 수 있음)
- **문제 설명**:
  1. `serve_endpoint_recall` / `query_endpoint`는 ① `host in ('claude','codex','hermes')`만 허용하고, ② 중간 lexical/semantic/RRF 점수와 전체 후보 목록을 반환하지 않으며(`ReaderResult`는 `_VALUE_SOURCES`로 `v1_guard.py:499`에 잠겨 있음), ③ `set_writer_allowed(False)`만으로는 `_wake_provider`의 `ensure_running()` 호출 및 `sys.stderr` 출력을 막지 못한다.
  2. `selected_recall.py:277`과 `verified_rows.py:163`은 `hook_budget`을 참조하는데 `hook_budget._deadline`은 프로세스 전역 상태라 멀티스레드 HTTP 서버에서 호출하면 위험하다.
  3. `verified_rows._select_verified_cards`가 `ensure_service`를 넘기므로 Playground 요청 1번이 유휴 상태에서 학습 추론/임베딩 resident 데몬을 백그라운드 스폰할 수 있다(§1.2.4 제로 오버헤드 및 순수 dry-run 원칙 위배 가능).
- **구체적 수정 권고**:
  1. `memvane/recall/api.py`(현재 17/30, 13슬롯 여유)에 기존 `serve_endpoint_recall`/`ReaderResult`와 분리된 전용 **`dry_run_recall(project_root, prompt, *, allow_wake=False, use_semantic=True, use_selector=False, deadline=...)`** 진입점을 신설하고, `reader_dispatch` 내부에 4단계(`lexicalLists`, `semanticList`, `fusedList` with RRF score, `selectorResult`)를 캡처하는 읽기 전용 경로를 둘 것.
  2. dry-run 경로에서는 `_recall_sync_wake`, `_wake_provider`, `ensure_running`, `ensure_service`(기본 off 또는 명시 옵션 시에만 기동, `skip_busy=True`), `sys.stderr` 출력, `hook_budget` 전역 참조를 완전히 우회하고 호출자 `deadline`만 인자로 전달할 것.

#### [P0-06] Phase 2의 `entry/run.py wake` 오용 (큐잉 CLI가 아니라 동기 실행 worker)
- **설계 문서 위치**: §2.3, §4.2.2, §6.3
- **실제 코드 근거**:
  - `entry/run.py:20` (`'wake': ('memvane.app.wake_entry:main', (), False)`)
  - `memvane/app/wake_entry.py:27-53` (`kind, project = argv`를 받아 `memvane.sync.api.run(Path(project))`를 **포그라운드 동기 실행**하며 `QMD_LAYA_WAKE_OWNER_PIPE=1` FIFO를 가정)
  - `memvane/app/runtime_dispatch.py:94-103` (`wake_entry`는 `runtime_work` 디스패처가 자식으로 스폰하는 내부 부트스트랩)
  - `skills/update/scripts/update.sh:27-28` (실제 큐잉은 `hooks/run-hook update` 또는 `runtime_work.request('laya-sync', ...)` 경로 사용)
- **문제 설명**: 설계가 Phase 2 수동 sync wake 큐잉을 위해 `entry/run.py wake`를 호출한다고 적었으나, `entry/run.py wake`는 `runtime-wakes.sqlite3`에 행을 넣는 커맨드가 아니라 sync 전체 pass(임베딩 fill 포함 수 분 소요)를 직접 실행하는 내부 워커 진입점이다. 직접 호출 시 `runtime_work` 큐·dispatch lease를 우회하고 API 요청 또는 서브프로세스를 장시간 블로킹한다.
- **구체적 수정 권고**:
  - `entry/run.py`에 선택된 route 증명 후 `runtime.api.request('laya-sync', project_root)`만 기록하고 즉시 반환하는 전용 서브커맨드(예: `queue-sync` 또는 `ui` 하위 액션, `require_cached_plugin_for_live_write()` 통과)를 정의하고 `wake` 참조를 제거할 것.

#### [P0-07] 번들 버전 혼재 시(1.x 정상 상태) UI 서버 핑퐁 교체·다운그레이드 루프 및 내부 제어 인증 부재
- **설계 문서 위치**: §5.2.2, §5.4.4, §6.2, §6.3
- **실제 코드 근거**:
  - `docs/ROADMAP.md:12` ("한 기계에서 Claude·Codex·Hermes가 서로 다른 1.x 버전을 로드하는 상태가 정상이다.")
  - `memvane/resident/bundle_lifecycle.py:5-10` (`newer`: 동일 버전은 교체하지 않고 `left > right`인 더 높은 버전만 교체)
  - `memvane/runtime/host_bundle.py:36-46` (`BundleIdentity`: `plugin.json` 버전 및 파일 노드 무결성 확인)
- **문제 설명**:
  1. §5.2.2는 실행 중인 `bundleVersion` **또는** `bundleRoot`가 다르면 기존 서버를 종료하고 새 서버를 띄운다고 설계했다. 그러나 Claude(`~/.claude/plugins/cache/...`), Codex(`~/.codex/plugins/cache/...`), Hermes(`~/.hermes/plugins/memvane`)는 **동일 버전이어도 `bundleRoot`가 항상 다르며**, 호스트마다 다른 1.x 버전을 로드할 수도 있다. `!=` 비교로 교체하면 ① 동일 버전 호스트 간 전환마다 불필요한 `server_draining` 핑퐁이 발생하고, ② 구버전 호스트(예: 1.1.1)가 신버전 서버(1.1.2)를 강제 **다운그레이드 교체**한다.
  2. `entry/run.py ui stop` 및 버전 인계 시 `POST /api/shutdown`을 보낸다고 되어 있으나, `POST /api/shutdown`은 브라우저 세션 쿠키 + CSRF 헤더를 요구하므로 CLI는 이를 호출할 수 없다. 마찬가지로 §5.4.4에서 `ui open --from-session-start`가 기존 서버에 `session_started` SSE를 푸시하도록 요청할 내부 엔드포인트와 인증 수단이 §6에 누락되어 있다.
- **구체적 수정 권고**:
  1. 교체 조건을 `version_key(candidate) > version_key(running)` 또는 `running bundleRoot` 훼손/삭제(`BundleIdentity.intact() == False`)로 변경하고, 동일·하위 1.x 버전 호스트는 기존 서버를 재사용할 것(`bundle_lifecycle.newer` 패턴 준수).
  2. `0600` `server.json`에 `controlSecret`(`secrets.token_urlsafe(32)`)을 저장하고, 로컬 CLI 전용 제어 엔드포인트(`POST /api/internal/shutdown`, `POST /api/internal/session-started`)는 `X-MemVane-Control` 헤더의 constant-time 비교로만 인증하게 명세할 것(정지 무응답 시 `pid`+`startedAt` 검증 후 `SIGTERM` 폴백 포함).

#### [P0-08] 로컬 HTTP 보안 경계 허점: `GET` CSRF·교차포트 `127.0.0.1` 쿠키 공유·`/api/health` 경로 유출·브라우저 HTTP/1.1 6-연결 SSE 데드락
- **설계 문서 위치**: §5.2, §5.3, §6.1–6.2, §7.1–7.3
- **문제 설명**:
  1. **§7.2 CSRF 검증 범위 모호**: "모든 상태 변경 및 민감 조회 `POST`/`PUT`/`DELETE` 요청과 `/api/events/stream` 외 API는..."이라는 문구가 `GET /api/...` 엔드포인트(`wiki/source`, `errors?details=1`, `overview`)에 대한 `Origin`/`Sec-Fetch-Site`/`X-MemVane-UI` 검증을 배제하거나 불명확하게 만든다. 특히 브라우저는 **동일 호스트 다른 포트(`http://127.0.0.1:3000` 로컬 dev 서버 vs `http://127.0.0.1:51842`) 간에 `SameSite=Strict` 쿠키를 공유**한다(RFC 6265 쿠키는 포트 격리 없음). 따라서 로컬 3000번 포트의 악성 웹앱/서드파티 스크립트가 `GET` 요청 시 브라우저가 쿠키를 자동 첨부하며, `Sec-Fetch-Site`는 `same-site`가 된다. `GET` API에서도 `Sec-Fetch-Site == 'same-origin'` 및 `X-MemVane-UI: 1`을 강제하지 않으면 교차포트 공격 표면에 노출된다.
  2. **`/api/health` 정보 노출**: §5.2는 `/api/health`가 `bundleRoot`(OS 사용자 홈 경로 포함)와 `instanceNonce`를 반환한다고 서술하고, §6.2는 `/api/health`를 인증 면제 대상으로 둔다. `Sec-Fetch-Site`/`Origin`/`Cross-Origin-Resource-Policy: same-origin` 및 `controlSecret` 없이 `/api/health`에 `bundleRoot`나 `activeClients`를 노출하면 외부 웹페이지의 로컬 포트 스캐닝·OS 경로/버전 핑거프린팅에 악용된다.
  3. **`ui open` stdout `#code=` 유출**: §5.2(`entry/run.py ui open` JSON `url` 출력)와 §5.3(에이전트 응답에서 코드 없는 베이스 URL만 보고)이 상충한다. `ui open`의 stdout JSON에 `#code=`가 포함되면, `skills/ui`를 실행한 에이전트의 LLM 컨텍스트·세션 트랜스크립트에 일회성 인증 코드가 기록된다.
  4. **브라우저 HTTP/1.1 6-연결 상한 SSE 데드락**: Python `ThreadingHTTPServer`는 HTTP/1.1만 지원하며, 브라우저는 origin당 동시 HTTP/1.1 연결을 **6개**로 제한한다. 사용자가 대시보드 탭 6개를 열면 각 탭의 `EventSource`(`/api/events/stream`)가 연결 슬롯 6개를 모두 점유해, 모든 탭의 `fetch('/api/...')` 호출이 영구 대기(데드락)에 빠진다. 또한 §6.2의 "초과 시 오래된 idle SSE 정리"는 브라우저 `EventSource`의 자동 재연결과 결합해 탭 간 재연결 핑퐁 폭주를 일으킨다.
- **구체적 수정 권고**:
  1. **모든 `/api/*` (`GET` 포함, `/api/health` 제외)**에 대해 ① `Host` 정확 일치(`127.0.0.1:<port>` 또는 `localhost:<port>`, 대소문자/userinfo/후행점 차단), ② `Sec-Fetch-Site`가 존재하면 `same-origin`만 허용(`same-site`·`cross-site` 거부), ③ `Origin`이 존재하면 `http://<Host>`와 정확 일치, ④ `fetch` 기반 API는 `X-MemVane-UI: 1` 필수, ⑤ 모든 응답에 `Cross-Origin-Resource-Policy: same-origin` 헤더 부여 및 `OPTIONS` 차단(`405`/`403`)을 명시할 것.
  2. `GET /api/health`는 무인증 시 `{"status":"ok"}`만 반환하고, `bundleRoot`·`bundleVersion`·`instanceNonce`·`activeClients` 상세는 유효한 세션 쿠키 또는 `X-MemVane-Control`(CLI용)이 있을 때만 반환하도록 분리할 것.
  3. `entry/run.py ui open`이 브라우저 자동 오픈에 성공(`opened: true`)한 경우 stdout JSON `url`에서 `#code=`를 반드시 제거하고, `--no-open` 또는 오픈 실패 시에만 수동 접속용 `manualUrl`(TTL 120초)을 반환할 것.
  4. 프론트엔드에서 **`BroadcastChannel` 기반 단일 탭 SSE 리더 선출**을 채택해 브라우저 프로필당 SSE 연결을 1개로 제한하고, 서버는 SSE 상한 초과 시 기존 스트림을 끊지 말고 `429`를 반환(`EventSource` 대신 `fetch` 스트림 또는 명시적 재연결 차단)하도록 처리할 것.

---

### P1 (구현 전 보완 필요)

#### [P1-01] `tests/manual-skills.test.mjs`, `doc-links.test.mjs`, `test-parts.test.mjs`, `G5` 필수 파일 및 `G6` 훅 closure 누락
- **설계 문서 위치**: §2.1, §4.1.1, §4.2, §5.4, §8.2, §9.2–9.3
- **실제 코드 근거**:
  - `tests/manual-skills.test.mjs:12,18-33` (`skills` 디렉터리 6개 고정 및 5개 매니페스트 `description` 정규식 단정)
  - `tests/structure/doc-links.test.mjs:31-48` (추적된 모든 `.md`의 상대 링크 유효성 검사)
  - `tests/structure/test-parts.test.mjs:32-49`, `tests/support/parts.mjs:15,46-66` (도메인별 `run.test.mjs` 및 `unit`/`integration`/`statemachine` tier 강제)
  - `tests/structure/v1_guard.py:1188-1193` (`G5`: 도메인마다 `README.md`, `api.py`, `types.py` 필수)
  - `tests/structure/v1_guard.py:83-110,1229-1234`, `tests/structure/v1-baseline.json:2-57` (`G6`: `hook-classify`, `hook-serve`의 top-level import closure 고정)
- **문제 설명**:
  1. `skills/ui/` 추가 시 `tests/manual-skills.test.mjs` 2개 테스트가 실패하므로, 5개 매니페스트(`plugin.json`, `.claude-plugin/plugin.json`, `.codex-plugin/plugin.json`, `.claude-plugin/marketplace.json`, `.agents/plugins/marketplace.json`)와 `tests/manual-skills.test.mjs` 갱신 계획이 누락되어 있다.
  2. 현재 `docs/design-local-web-ui.md` 자체에 `doc-links.test.mjs`가 거부하는 링크가 3곳 있다: `skills/logs/SKILL.md`(`../skills/logs/SKILL.md`여야 함), `file:///Users/dulee/.../setup_entry.py`, `file:///Users/dulee/.../memvane_paths.py`(로컬 절대 `file://` URI; `doc-links.test.mjs`는 `file:` 스킴은 제외하나 다른 머신/이식성 및 relative link 규칙에 어긋남).
  3. §2.1의 `memvane/ui/` 트리에 `G5` 필수 파일인 **`memvane/ui/README.md`**와 `entry/run.py`가 참조하는 **`memvane/ui/cli.py`**(`import_all.py:39-48` 필수)가 누락되어 있다.
  4. `memvane/ui/tests/`는 `tests/support/parts.mjs` 규약에 맞게 `run.test.mjs` + `unit/`·`integration/` 구조(`test_*.py`)를 갖춰야 `test-parts.test.mjs`를 통과한다.
  5. `memvane/host/host_response.py`에 SessionStart opt-in 분기를 추가할 때 `subprocess`·`json` 외 새 모듈이나 `memvane.host.ui_auto_open`을 **top-level로 import하면 `G6`(`hook_closures`)이 즉시 실패**한다.
- **구체적 수정 권고**:
  - §2.1 파일 트리에 `memvane/ui/README.md`, `memvane/ui/cli.py`, `memvane/ui/tests/run.test.mjs` 및 `unit/`·`integration/` tier를 명시할 것.
  - §5.4에 "opt-in 게이트 헬퍼 및 비기초 stdlib(`subprocess` 등)는 `host_response.py`의 `action == 'update-queued'` 분기 내부에서만 지연(lazy) import해 `v1-baseline.json`의 `hook-serve` closure를 0바이트 변화로 유지한다"고 명문화할 것.
  - §9에 `tests/manual-skills.test.mjs` 및 5개 매니페스트 갱신 항목을 포함하고, 설계 문서 내 broken/절대 `file://` 링크 3건을 상대 경로로 수정할 것.

#### [P1-02] SessionStart Opt-in: `QMD_SETUP_GUARD_FIXTURE=1` 누락, 파이프 FD 상속 행(hang), Hermes 경로 차이, 쿨다운 휘발
- **설계 문서 위치**: §5.2–5.4, §6.1
- **실제 코드 근거**:
  - `docs/dev/contexts/working-rules.md:50`, `package.json:15`, `tests/structure/static.test.mjs:14-15` (`QMD_SETUP_GUARD_FIXTURE=1`이 `npm test` 기본 환경)
  - `hooks/hooks.json:5`, `hooks/hooks-codex.json:5` (`SessionStart` timeout = **5초**)
  - `hooks/run-hook:29,69-97` (`legacy`는 `hook-serve` 미진입, `candidate|preview|worktree`만 `exec ... hook-serve`)
  - `hermes_adapter/core_bridge.py:71-81,101-106,255-284` (`HERMES_SANDBOX`, `HERMES_HEADLESS=1` 검사; `session_update`는 `_laya_route_state("update", ...)`를 호출해 `preview`를 받지 않으며 `candidate|worktree`일 때 `serve_selected("update-queued", "hermes", ...)` 호출)
- **문제 설명**:
  1. §5.4.2 환경 가드에 **`QMD_SETUP_GUARD_FIXTURE=1`**, **`MEMVANE_TEST_RUNTIME_ROOT`**, **`HERMES_SANDBOX`**, **`HERMES_HEADLESS=1`**이 빠져 있어 테스트 중 `autoOpenOnSessionStart=true` 픽스처에서 실제 브라우저/서버가 스폰될 위험이 있다.
  2. `host_response.py`가 detached 스폰(`ui open`) 시, 그리고 `ui open`이 `ui serve`를 detached 스폰할 때 **`stdin=DEVNULL, stdout=DEVNULL, stderr=DEVNULL, close_fds=True`**를 닫아 주지 않으면, 자식 서버가 Claude/Codex의 hook stdout/stderr 파이프를 상속받아 5초 hook timeout까지 호스트가 멈춘다.
  3. `lastAutoOpenAt`(240분 쿨다운)을 `server.json`에만 두면, 유휴 종료(30분) 시 `server.json` 정리와 함께 쿨다운 기록이 소실되어 35분 뒤 새 세션마다 브라우저가 다시 뜬다.
  4. §6.1 인증 흐름에서 SPA가 `POST /api/auth/exchange` 성공을 확인하기 전에 `history.replaceState`로 `#code=`를 먼저 지우면 일시적 네트워크/타임아웃 실패 시 복구가 불가능하다. 또한 다중 탭 인증 시 새 코드 교환이 기존 탭의 세션 쿠키를 무효화하는지(단일 토큰 vs 다중 세션 슬롯) 명세가 빠져 있다.
- **구체적 수정 권고**:
  1. 환경 가드에 `QMD_SETUP_GUARD_FIXTURE=1`, `MEMVANE_TEST_RUNTIME_ROOT`, `HERMES_SANDBOX`, `HERMES_HEADLESS=1`을 추가하고, `ui open/serve`도 `MEMVANE_TEST_RUNTIME_ROOT` 지원 및 `QMD_SETUP_GUARD_FIXTURE=1`일 때 실 브라우저 오픈 금지(`--no-open` 강제 또는 거부)를 적용할 것.
  2. 모든 detached `Popen`에 `stdin=DEVNULL, stdout=DEVNULL, stderr=DEVNULL`(또는 서버 로그 파일 FD), `close_fds=True`, `start_new_session=True`(Windows: `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP`)를 명시할 것.
  3. `lastAutoOpenAt` 및 `lastUserClosedAt`은 `server.json`이 아니라 영속 파일(`preferences.json` 또는 `ui/auto-open-state.json`)에 보관하고, 다중 탭 허용을 위해 서버 메모리에 최근 N개(예: 16개) 세션 토큰을 LRU 보관할 것.
  4. `history.replaceState`는 `POST /api/auth/exchange` 응답 수신 직후(성공 또는 401 확정 후) 실행할 것.

#### [P1-03] 크로스 플랫폼 누락: 하위 도메인 POSIX 강결합, Windows `SO_REUSEADDR` 포트 하이재킹, WSL/SSH 브라우저 오픈 인젝션·행(hang)
- **설계 문서 위치**: §1.2.1, §7.1, §8.2–8.4
- **실제 코드 근거**:
  - `memvane/diagnostics/event_log.py:67,142-168,281-304` (top-level `import fcntl`, `os.sep` 순회, `os.getuid()`)
  - `memvane/runtime/runtime_work.py:31`, `memvane/resident/embed_resident.py:12,170`, `memvane/resident/resident_service.py:29,170` (top-level `import fcntl`, `AF_UNIX`, `SO_PEERCRED`/`getpeereid`)
  - `memvane/provider/embed_runtime.py:53,70-72` (top-level `import fcntl`, `@node-llama-cpp/mac-arm64-metal` 하드코딩)
  - `memvane/kernel/atomic_rename.py:18-23`, `memvane/kernel/safe_rmtree.py:25-35` (Windows 미지원)
- **문제 설명**:
  1. §8.2는 `memvane/kernel/`만 추상화하면 `memvane/ui/`가 Windows에서 그대로 동작할 것처럼 서술하지만, 실제 `ui`가 호출하는 `diagnostics/event_log`, `registry`, `resident`, `runtime`, `learning`, `provider`, `install` 대부분이 top-level `fcntl`·`pwd`·`os.getuid()`·`AF_UNIX`를 직접 사용한다. Windows 네이티브 지원은 하위 도메인 포팅 없이는 불가능하다.
  2. Python `ThreadingHTTPServer`는 기본값 `allow_reuse_address = True`(`SO_REUSEADDR`)를 쓰는데, **Windows에서 `SO_REUSEADDR`은 실행 중인 동일 `127.0.0.1:<port>` 위에 다른 프로세스가 덮어 바인딩(포트 하이재킹)하는 것을 허용**한다.
  3. `ThreadingHTTPServer`에 요청 타임아웃(`request_timeout`), 헤더/본문 크기 상한, 스레드/연결 수 상한(`daemon_threads = True`, `max_workers`/세마포어)이 명시되지 않아 Slowloris나 대용량 `POST` 요청에 취약하다.
  4. WSL 환경의 `wslview`/Windows 런처 폴백 시 URL에 `#code=...&project=...&tab=...`의 `&` 문자가 포함되므로 `cmd.exe /c start` 형태를 쓰면 `&`가 셸 명령 구분자로 해석된다. 또한 SSH 원격 터미널에서 `webbrowser.open()`을 무검증 호출하면 `lynx`/`w3m` 같은 TUI 브라우저가 실행되어 프로세스가 행(hang)에 걸릴 수 있다.
- **구체적 수정 권고**:
  1. §1.2.1·§8에서 "네이티브 Windows 실행은 하위 도메인(`event_log`·`runtime`·`resident`·`learning`)의 POSIX 의존 제거가 선행 조건이며, Phase 1~3은 macOS·Linux·WSL(Linux 백엔드+Windows 브라우저) 검증 범위"임을 명확히 구분할 것.
  2. `server.py`는 `allow_reuse_address = False`(Windows에서는 `SO_EXCLUSIVEADDRUSE` 설정)를 강제하고, `BaseHTTPRequestHandler.timeout`(예: 10초), `Content-Length` 상한(예: 1MB), `daemon_threads = True`, 동시 요청 워커 상한을 명시할 것.
  3. 브라우저 오픈 전 환경 가드(`SSH_CONNECTION`/`SSH_TTY`, Linux `DISPLAY`/`WAYLAND_DISPLAY` 부재, TUI `$BROWSER`)를 적용해 비GUI 환경에서는 즉시 `--no-open` 경로로 폴백하고, WSL 폴백은 `shell=False`로 `wslview` 또는 URL 안전 검증 후 `powershell.exe -NoProfile -Command Start-Process` / `explorer.exe`만 사용할 것.

#### [P1-04] 도메인 API 세부 불일치 (`snapshot` truncation, `learning` 임계치/런타임 디스크, `wiki/source` 경계, `diagnostic` 프라이버시)
- **설계 문서 위치**: §4.1.1, §4.1.3, §4.1.4, §6.2, §7.4
- **실제 코드 근거**:
  - `memvane/registry/memvane_registry.py:970-991` (`snapshot()`은 `MAX_ROWS = 8192`에서 묵시 절단하며, `reconcile=True` 시 32행/2초 예산 제한이 있고 실패/busy 반환값을 호출자에게 전달하지 않음)
  - `skills/logs/scripts/logs.py:123-153` (`_visible`이 `diagnostic.part` 제외 + 구형 `prompt['text']` 제거), `logs.py:547-548` (승격 임계치 4종: `candidateTrain` 100, `candidateEval` 50, `candidateEvalNonEmpty` 10, `candidateEvalEmpty` 10)
  - `memvane/install/state_cleanup.py:243-288` (`cleanup(everything=False)`는 `installs/`만 보고 `Application Support/memvane` 세부 내역은 계산하지 않음)
  - `memvane/learning/context_learning/runtime_setup.py:52-58,92-104` (`learning` 런타임 경로)
  - `memvane/registry/log_targets.py:151-169` (`_system_link`: macOS `/var` 등 루트 소유 시스템 symlink 허용 방식)
- **문제 설명**:
  1. `snapshot()`은 `absent`·`busy`·`recovery_required` 상태를 반환할 수 있고 32행 단위 부분 reconcile이므로, UI가 이를 "프로젝트 0개"로 오표시하지 않도록 상태 배너 및 `stale`/`complete` 표기를 설계에 반영해야 한다.
  2. `wiki/source` 검사 시 §7.4처럼 경로 **전체 구성 요소**에 대해 `O_NOFOLLOW`를 적용하면 macOS `/var/folders/...`·`/tmp` 등 루트 소유 시스템 symlink나 사용자 홈 상위 symlink 아래의 프로젝트가 통째로 거부된다.
  3. `wiki/source`는 `skipPaths`뿐 아니라 `DENIED_SOURCE_SEGMENTS`(`.git`, `.memvane`, `node_modules` 등), worktree의 자체 `skipPaths` 병합(`host_response.py:1268-1270`), 정규 파일 + `st_nlink == 1` 또는 카드 `sourceRevisions` 인용 출처 제한을 함께 적용해야 `.env` 등 프로젝트 내 민감 파일 임의 열람을 막을 수 있다.
  4. `events.jsonl` 파싱을 `memvane/diagnostics/`로 승격할 때 `logs.py:148-153`의 **구형 `prompt.text` 제거**와 `Bidi`/제어문자 정규화를 반드시 함께 승격해야 `docs/privacy.md` 경계를 유지한다.
- **구체적 수정 권고**:
  1. `wiki/source`는 `decision['projectRoot']`(canonical) 디렉터리 FD를 연 뒤 그 **하위 상대 경로 컴포넌트만** `O_NOFOLLOW | O_NONBLOCK`으로 순회해 일반 파일(`S_ISREG`)만 읽고, `excluded_source` + `DENIED_SOURCE_SEGMENTS` + `.memvane/` 차단을 적용할 것.
  2. `state_cleanup`/`provider.api.collect` 외에 `Application Support/memvane`(`runtime_setup.runtime_base()`) 크기 산출은 `memvane/ui/read_adapters.py`에서 `state_cleanup._size`와 동일한 `follow_symlinks=False` 유계 `os.scandir`로 별도 계산할 것.
  3. `learning` 모니터의 승격 진행률은 단일 "100건"이 아니라 `logs.py:547-548`의 4개 지표(`candidateTrain/100`, `candidateEval/50`, `candidateEvalNonEmpty/10`, `candidateEvalEmpty/10`)와 1,800행 상한(`caseRows/caseLimit`)을 그대로 노출할 것.

#### [P1-05] `ui/dist/` 결정적 빌드, `static.test.mjs` SVG 네임스페이스 오탐, CSP `style-src` 제약
- **설계 문서 위치**: §3.2–3.3, §7.3, §9.2.2
- **실제 코드 근거**:
  - `.github/workflows/test.yml:23-98` (`ubuntu-latest` + `macos-15` 병렬 CI)
  - `tests/structure/static.test.mjs:408-412`
- **문제 설명**:
  1. §9.2.2는 `ui/dist/`에서 `http://`/`https://` 부재를 단정하겠다고 했으나, §3.2가 권장하는 자체 SVG 아이콘(Preact/Vite JSX 컴파일)은 번들 내부에 W3C XML 네임스페이스 문자열 **`http://www.w3.org/2000/svg`**를 필연적으로 포함한다. 단순 문자열 검사 시 즉시 오탐이 난다. 또한 Preact 코어 런타임에 `dangerouslySetInnerHTML` 분기가 남으면 `ui/dist/` 검사에서 걸릴 수 있으므로 `ui/src/` 검사만 하되 AST/정규식 범위를 분명히 해야 한다.
  2. `ui/package-lock.json`을 macOS arm64에서 생성할 때 `esbuild`/`rollup`의 Linux/Windows optional binary 메타데이터가 누락되면 CI `ubuntu-latest`에서 `npm --prefix ui ci --ignore-scripts`가 실패할 수 있고, Vite 기본 설정은 OS별 개행/해시 파일명으로 `git diff --exit-code ui/dist`가 깨지기 쉽다.
  3. §7.3 CSP의 `style-src 'self'`는 HTML 인라인 `style="..."` 속성(`style-src-attr`)을 차단하므로, SVG 속성이나 정적 HTML에 `style="..."`가 들어가지 않도록(JS `el.style.setProperty` 또는 `<svg>` 기하 속성/CSS 클래스만 사용) 규약을 명시해야 한다.
- **구체적 수정 권고**:
  1. `ui/dist/` 외부 주소 가드에서 `http://www.w3.org/2000/svg`(및 `1999/xhtml`, `1998/Math/MathML`) XML 네임스페이스를 명시적으로 allowlist 처리할 것.
  2. `vite.config.ts`에서 고정 파일명(`assets/app.js`, `assets/app.css`), `sourcemap: false`, `cssCodeSplit: false`, `.gitattributes`(`ui/dist/** text eol=lf`)를 강제하고, `ui/package-lock.json`은 다중 플랫폼 optional deps 포함 여부(CI `ubuntu-latest` + `macos-15`에서 `npm --prefix ui ci --ignore-scripts` 통과)를 검증할 것.

#### [P1-06] `logs` / `logs_slo` 도메인 승격과 `memvane/diagnostics/api.py` 및 `sqlite_read_uri_guard.py` 준수
- **설계 문서 위치**: §2.2, §8.3, §9.2.1
- **실제 코드 근거**:
  - `tests/structure/context-map.json:5,14` (`diagnostics: 1`, `registry: 5`)
  - `skills/logs/scripts/logs.py:47` (`from memvane.registry import log_targets`)
  - `skills/logs/scripts/logs_slo.py:107-130` (`install_size`는 `json`, `os`, `Path`만 사용)
  - `tests/structure/sqlite_read_uri_guard.py:9-40` (`mode=ro` 문자열 금지)
  - `memvane/diagnostics/api.py`: 현재 **20/30** 속성 (여유 10개)
- **문제 설명**: `logs_slo.py`의 `install_size`는 상위 도메인(`registry`/`route`)을 import하지 않으므로 `diagnostics` adapter 층에 그대로 둘 수 있다(단 `diagnostics/types.py`나 `domain/`이 아닌 adapter 모듈에 두어야 `G7`을 통과함). 반면 `logs.py`의 `--all` 대상 해석(`log_targets.resolve()`)은 `registry`(계층 5) 의존이므로 `diagnostics`(계층 1)로 옮길 수 없고 `memvane/ui/`와 `skills/logs/` 각자에 남겨야 한다. 또한 `memvane/ui/`는 모든 SQLite read-only 연결에 `memvane.kernel.errors._query_only_uri`를 사용해야 하며 `?mode=ro` 리터럴을 쓰면 `sqlite_read_uri_guard.py`가 실패한다.
- **구체적 수정 권고**:
  - `memvane/diagnostics/`(adapter 층)에 순수 이벤트 요약·SLO·진단 재조립 모듈을 두고 `diagnostics/api.py`에 3~4개 파사드(`query_events`, `summarize_events`, `slo_lines`, `reassemble_diagnostic` — 총 ≤ 24/30으로 `G5` 안전)를 노출하고, `log_targets` 기반 다중 타깃 수집은 `registry`·`ui` 측에 남길 것.

---

### P2 (권고 / 품질 개선)

#### [P2-01] `memvane/ui/` 실행 시 개발 체크아웃 vs 설치된 플러그인 캐시 모드 안내
- **근거**: `memvane/kernel/memvane_execution_guard.py:71-99`, `memvane/app/setup_entry.py:136,174,236`
- **설명**: `entry/setup.py`의 모든 쓰기 명령은 `require_cached_plugin_for_live_write()`로 호스트 캐시 경로(`~/.claude/plugins/cache/...` 등) 여부를 검사한다. 개발자가 로컬 git 체크아웃에서 `entry/run.py ui serve`를 띄우면 Phase 1 읽기는 되지만 Phase 2 적용(`apply`)은 모두 `live_write_requires_plugin_cache`로 거절된다.
- **권고**: `/api/overview` 또는 `/api/health`(인증 후)에 `isInstalledPluginCache: bool` 플래그를 포함해 미설치 체크아웃에서 실행 중일 때 상단 상태바에 `Read-Only (Dev Checkout)` 배지를 표시할 것.

#### [P2-02] 초기 빌드 중(`preview`) 및 `route.json` 복구 필요 상태 UI 표현 보강
- **근거**: `memvane/journal/route_selection.py:51-95` (`_preview_route`는 `action == 'recall'`에서만 동작), `memvane/install/initial_build.py`
- **설명**: 온보딩 중 아직 `activate`되지 않은 프로젝트(`initial-builds/<hash>.json`)는 `projects.sqlite`에 `active` 행으로 들어오지 않을 수 있어 `registry.api.snapshot()`만으로는 "빌드 진행 중 프로젝트"가 Overview 목록에서 누락될 수 있다.
- **권고**: Overview 어댑터에서 `v1/initial-builds/*.json`을 보조 스캔해 `initial_build` 진행 중 프로젝트도 별도 섹션으로 표시하고 진행률(`index.build.progress`)을 연결할 것.

#### [P2-03] SSE 파일 감시(`events.jsonl` · `runtime-wakes.sqlite3`) 및 유휴 종료/다중 탭 세부 정책
- **근거**: §5.2.3, §6.4
- **설명**: `runtime-wakes.sqlite3`는 rollback journal 또는 WAL을 쓸 수 있고, `events.jsonl`은 4MiB에서 `.1`/`.2`로 로테이션된다(`memvane/diagnostics/event_log.py:85-86`). `mtime`/`size`만 폴링할 경우 inode 변경(로테이션)을 놓칠 수 있으므로 `(st_ino, st_size, st_mtime_ns)` 튜플을 비교하고, 등록 프로젝트가 많을 때 파일 디스크립터/stat 스캔을 현재 UI가 보고 있는 프로젝트 + 최근 활성 N개로 제한해 유휴 CPU/디스크 I/O를 낮추는 편이 안전하다. 또한 `BroadcastChannel` 리더 탭이 `visibilitychange`·`pagehide`로 닫힐 때 백업 탭이 즉시 재연결할 수 있도록 유휴 종료 직전 최소 5~10초 `reconnectGraceSeconds`를 서버 측 유휴 판정에도 동일하게 적용하는 것을 권장한다.

#### [P2-04] 구현 스코프 분해 (마일스톤 리스크 완화)
- **근거**: §10.1
- **설명**: 현재 Milestone 1~2에 HTTP/SSE/보안 서버, 4개 전체 탭, dry-run 4단계 Playground, `logs`/`logs_slo` 승격, `learning`·`runtime` 어댑터, `skills/ui`까지 모두 몰려 있어 단일 PR/릴리스 단위로 과대하다.
- **권고**: Milestone 1–2를 **M1(보안 서버 + Overview/Logs/Errors + `skills/ui`) → M2a(Wiki Browser + 원문 대조) → M2b(Query Playground + Runtime/Learning)**로 쪼개어 각 단계마다 `v1_guard.py`·`static.test.mjs`·`node --test` 게이트를 통과시키는 편이 안전하다.

---

## 설계의 긍정적 평가

1. **`ui/`와 `memvane/ui/` 분리 및 Python stdlib 백엔드 선택 (§2.1)**: `tests/structure/static.test.mjs:225-241,492`가 `memvane/`·`hooks/`·`entry/`의 `.js`/`.mjs` 내 `fetch(` 및 외부 다운로더를 금지하고 `v1_guard.py`가 `memvane/`를 스캔하므로, 프론트엔드를 루트 `ui/`에 격리하고 백엔드를 Python `ThreadingHTTPServer`(외부 pip 의존성 0)로 둔 결정은 저장소 제약에 정확히 부합한다.
2. **1.x 영속 계약 보존 원칙 (§1.3, §4.2, §9.1)**: `.memvane/settings.json` 5키 계약(`memvane/settings/config.py:72-73`)을 건드리지 않고 UI 상태를 `v1/ui/`(`docs/dev/contexts/release.md` 계약 밖 파생물)로 분리했으며, Phase 2 쓰기를 직접 파일 수정이 아닌 기존 `entry/setup.py` + `previewSha256` 패턴(`memvane/app/setup_entry.py:93-135`)으로 위임한 점은 기존 lock·증명 불변성을 지키는 올바른 접근이다.
3. **Hook 격리 원칙 (§2.2, §5.4)**: `memvane/host/`가 `memvane/ui/`를 import하지 않아 평소 `recall`·`gate`·`posttool`의 `hook_import_graph.py` closure와 지연 시간에 영향을 주지 않도록 잡은 방향은 `hooks/hooks.json`의 5s/22s 예산과 부합한다.
4. **프론트엔드 XSS·오프라인 보안 원칙 (§3.2–3.4, §7.3)**: 외부 CDN/폰트/Tailwind 런타임 없이 자체 CSS + SVG를 쓰고, `innerHTML`/`dangerouslySetInnerHTML` 금지 및 `javascript:`/`data:` 링크 스킴 차단, Bidi 제어문자 이스케이프를 명시한 점은 저장소 보안 기준(`skills/logs/scripts/logs.py`, `memvane/host/host_response.py:_UNSAFE_BODY`)과 일관된다.

---

## 열린 결정 사항(§10.2)에 대한 리뷰어 의견

1. **TypeScript 뷰 레이어 (`§10.2.1`)**: **Preact + Vite** 권장. `ui/dist/`가 git 저장소와 3개 호스트 플러그인 캐시에 통째로 복사되므로 gzip ~30KB(React 대비 절반 이하) 이점이 크며, `preact/hooks`만으로 4개 탭과 확인 모달 상태를 유지하기에 충분하다. (단 JSX 런타임의 SVG XML NS `http://www.w3.org/2000/svg`를 정적 가드에서 allowlist 처리 필요)
2. **빌드 산출물(`ui/dist/`) 관리 방식 (`§10.2.2`)**: **저장소 사전 빌드 커밋 + CI drift 검사** 외 현실적 대안이 없다. `.claude-plugin/marketplace.json`, `.agents/plugins/marketplace.json`, Hermes `plugin.yaml` 모두 post-install 빌드 훅이 없는 스냅샷/git 기반 설치다. 단 `vite.config.ts`에서 해시 없는 고정 엔트리명과 `.gitattributes` LF 고정으로 Linux/macOS 간 빌드 결정성을 반드시 보장해야 한다.
3. **`logs` / `logs_slo` 도메인 승격 방식 (`§10.2.3`)**: **안 A 채택, 단 계층 분리 필수**. `memvane/diagnostics/api.py`는 현재 20/30으로 10슬롯 여유가 있다. `logs_slo.py:107-130`의 `install_size`는 `registry`를 쓰지 않고 `<state_root>/route.json`과 `sourceRoot`만 읽으므로 `diagnostics`의 `adapter` 모듈(단 `types.py`/`domain/` 금지 — `G7`)로 올릴 수 있고, `logs.py`의 `--all`(`log_targets`)은 `registry` 계층 5 의존이므로 `memvane/ui/`와 `skills/logs/`에 남기면 `G2` 충돌 없이 100% 단일 진실 원천을 유지할 수 있다.
4. **Query Playground dry-run API 위치 및 동작 (`§10.2.4`)**: `memvane/recall/api.py`는 현재 **17/30**으로 13슬롯 여유가 충분하다. 전용 `dry_run_recall` 함수와 결과 타입을 `recall/api.py`·`recall/types.py`에 추가할 것. `admit_query` 거부 또는 임베딩/학습 resident 미기동 시에는 **사이드이펙트(데몬 자동 스폰) 없이** 즉시 lexical 결과 + 정확한 fallback 사유(`provider_unavailable`, `resource_deferred`, `selector_not_prepared` 등)를 반환하고, warm selector/provider 호출은 명시적 UI 옵션(`includeSelector` 토글 + `skip_busy=True`)으로 제어하는 편이 안전하다.
5. **SessionStart 자동 오픈 세부 정책 (`§10.2.5`)**:
   - **미온보딩(`legacy`) 제외 권장**: `hooks/run-hook:69-85`와 `hermes_adapter/core_bridge.py:262` 모두 `legacy`에서는 `hook-serve`를 아예 부르지 않으며 `docs/privacy.md:5-7` 원칙("온보딩 전 프로젝트에서 작업 0")에도 맞는다. 선택된 프로젝트(`candidate`, `worktree`)에서만 opt-in 자동 오픈을 허용하는 편이 깔끔하다.
   - **타이머 기본값**: `idleShutdownMinutes = 30`, `autoOpenCooldownMinutes = 240`, `reconnectGraceSeconds = 10` 모두 적정하다. 단 `lastAutoOpenAt`은 휘발성 `server.json`이 아닌 영속 상태 파일에 저장해야 30분 유휴 종료 후 쿨다운이 무력화되는 버그를 피할 수 있다.


---

## 2차 개정(r2) 재검증 결과

- **재검증 일시**: 2026-11-24
- **재검증 대상**: [design-local-web-ui.md](../design-local-web-ui.md) (2차 개정본, r2)
- **2차 종합 판정**: **APPROVE WITH CHANGES**

1차 리뷰의 대형 아키텍처·보안·구조 가드 충돌은 r2에서 핵심 설계 수준으로 해소되었고, Preact+TypeScript+Vite · 전역 단일 통합 대시보드 · 오르카 스타일 트리 · M0~M2 분할 전략도 저장소 제약과 정합한다. 다만 **r2 문서 41행 자체가 `tests/memvane-naming-guard.test.mjs`를 1건 실패시키고 있고**, 기존 열린 서버 재사용 시 `#code=` 발급 내부 API 누락 등 구현 착수(M0/M1a) 직전 문서·계약 수정이 필요한 소수 항목이 남아 있다.

### 1차 지적 항목 해소 현황

| ID | 1차 지적 요약 | 상태 | 근거 (r2 섹션) 및 잔여 확인 사항 |
|---|---|---|---|
| `P0-01` | `kernel/` G3 한도 고갈 & 미존재 `support_base` | **RESOLVED** | §2.1, §2.2, §8.2: `memvane/ui/platform_io.py` 격리 및 `runtime_setup.runtime_base()` 사용 |
| `P0-02` | 도메인 `api.py` 30개 한도 & `WakeQueue` 쓰기·`runtime` 42 핀 | **PARTIAL** | §2.2, §4.3.4: `diagnostics` 24/30, `recall` 18/30, `runtime_main snapshot-readonly`, `project_inspection.py`+1슬롯 정리. 단 `runtime_main` 반환형/`service_root(create=False)` 계약·`shared.py` 줄 수(`2,319/2,319`)·`learning/api.py` 슬롯 확정 보완 필요(`N-04`, `N-05`) |
| `P0-03` | `static.test.mjs` `DOWNLOADERS` / `ADDRESSES` 충돌 | **RESOLVED** | §2.1, §2.2: raw socket `local_client.py` + Python 파일 내 `https://`·`[::1]` 리터럴 금지 |
| `P0-04` | `installId` 단독 키의 linked git worktree 미구분 | **PARTIAL** | §4.1, §6.2: `targetId` 및 `select_route` 재증명 도입. 단 해시 역조회 절차 및 worktree `.memvane/compile/`·`events.jsonl` 소유 구분 보완 필요(`N-06`) |
| `P0-05` | Query Playground vs `selected_recall` 불일치 | **PARTIAL** | §4.3.2: 전용 `dry_run_recall` + `DryRunRecallResult` 신설. 단 `use_selector=True` 시 `_SERVICE_STARTER`·`service_root(create=True)`·`VerifiedLayaTrainer` 스폰 차단 및 worktree 분기/`_VALUE_SOURCES` 명세 필요(`N-03`) |
| `P0-06` | `entry/run.py wake` 오용 | **RESOLVED** | §4.4.2, §5.2: 전용 `entry/run.py ui action-sync --target <targetId>` 분리 |
| `P0-07` | 번들 혼재 핑퐁 & 내부 제어 인증 부재 | **PARTIAL** | §5.1, §5.2, §6.3: `bundle_lifecycle.newer` 기준 상위 버전만 교체 + `X-MemVane-Control`. 단 재사용 시 `#code=` 발급용 내부 API 누락(`N-02`) |
| `P0-08` | `GET` CSRF·교차포트 쿠키·`/api/health` 유출·6-연결 SSE 데드락 | **PARTIAL** | §5.2, §6.1–6.4, §7.1–7.2: `/api/health` 최소화, `GET /api/*` 헤더·출처 검증, `#code=` stdout 제거, `BroadcastChannel`+`429`. 단 `/api/events/stream` `Sec-Fetch-Site` 부재 시 fail-open 및 throttling 경합 보완 필요(`N-07`) |
| `P1-01` | `manual-skills`·`doc-links`·`test-parts`·`G5`·`G6` 누락 | **PARTIAL** | §2.1, §5.4, §9.2 반영(`doc-links.test.mjs` 통과). 단 r2 41행 옛 저장소 이름이 `tests/memvane-naming-guard.test.mjs` 실패 유발(`N-01`) |
| `P1-02` | SessionStart fixture·pipe hang·Hermes·쿨다운 | **RESOLVED** | §5.1, §5.2.4, §5.4, §6.1 (`auto-open-state.json`, `DEVNULL`+`close_fds`, 6중 가드, 16 LRU) |
| `P1-03` | 크로스 플랫폼 POSIX 결합·Windows `SO_REUSEADDR`·WSL/SSH | **RESOLVED** | §1.2.1, §7.1, §8 (Phase 1~3 범위, `allow_reuse_address=False`·`SO_EXCLUSIVEADDRUSE`, 요청 상한, WSL/SSH `shell=False`) |
| `P1-04` | 도메인 API 세부(`snapshot`, `learning` 4지표/디스크, `wiki/source`, `prompt.text`) | **RESOLVED** | §4.3.1, §4.3.4, §7.3 (`MAX_ROWS`/`stale`, 4종 임계치+1,800, `runtime_base()`, `projectRoot` 하위 `O_NOFOLLOW`, `prompt.text` 제거) |
| `P1-05` | `ui/dist/` 결정성, SVG NS 오탐, CSP `style-src` | **PARTIAL** | §3.2–3.3, §9.2.2 (고정 파일명·LF·multi-OS CI·SVG NS allowlist·인라인 `style` 금지; 단 `esbuild.legalComments: 'none'` 추가 필요 `N-09`) |
| `P1-06` | `logs`/`logs_slo` 승격 & `sqlite_read_uri_guard` | **RESOLVED** | §2.2 (`diagnostics` adapter층 승격 ≤24/30, `log_targets` 분리, `_query_only_uri`) |
| `P2-01`~`P2-04` | Dev 체크아웃 배지, 초기 빌드 스캔, SSE inode 감시, 스코프 분해 | **RESOLVED** | §4.2, §4.3.1, §4.4, §5.1, §5.2.3, §6.2, §6.4, §10.1 |

### 2차 재검증 신규·잔여 지적 사항

- **[N-01 / P0]** `docs/design-local-web-ui.md:41`의 트리 루트 표기(옛 레포 이름)가 `tests/memvane-naming-guard.test.mjs:10,30-49`를 실패시킴 → `memvane/` 또는 프로젝트 루트 일반 표기로 수정 필요.
- **[N-02 / P0]** 기존 실행 중 싱글톤 서버 재사용 시 새 1회성 `#code=` 발급 경로 누락 → §6.3에 `X-MemVane-Control` 인증 **`POST /api/internal/code`** 추가 및 `ui serve` 초기 URL 1회 출력 명세 필요.
- **[N-03 / P1]** `dry_run_recall(use_selector=True)` 시 `ensure_service=None`만으로는 `_SERVICE_STARTER`·`service_root(create=True)`(`inference_service.py:499-501,761-766`) 및 `VerifiedLayaTrainer` 스폰(`live_select.py:156-163`)이 막히지 않음 → 소켓·`admit()` 사전 확인 후 cold 상태면 즉시 반환 + no-op reject 콜백 전달, `worktree`/`preview` 분기 명시 및 `DryRunRecallResult`의 `v1_guard.py:_VALUE_SOURCES` 등재 필요.
- **[N-04 / P1]** `runtime.api.runtime_main(['snapshot-readonly', ...])`는 항상 `int`를 반환하고(`runtime_work.py:739-759`, `entry/run.py:50-54`), `default_root()`가 `service_root(create=True)`로 디렉터리를 생성함 → 콜백 인자로 dict 전달(`main`은 `0`/`2` int 반환 유지), `service_root(create=False)` 및 `QMD_SETUP_GUARD_FIXTURE=1`·보조 JSON(`failures`/`drops`/`overflow`/`preparing`/`deferred`) 읽기 명세 필요.
- **[N-05 / P1]** `shared.py` 실측 줄 수는 `2,319/2,319`(여유 0)로 정정 필요하며, `learning/api.py` 30/30 슬롯 확보는 테스트 mock 대상(`learning_runtime_*` 등)을 제외한 무mock 단일 호출처(`card_budget_failure_fields` 등)로 지정 필요.
- **[N-06 / P1]** `targetId` → `target_root` 2단계 역조회(`registry.snapshot`+`initial-builds/*.json` → `select_route`) 및 worktree 선택 시 `.memvane/compile/*.jsonl`·`wiki.backfill`·`events.jsonl`은 main(`installId`) 소유임을 명시 필요.
- **[N-07 / P1]** `/api/events/stream`이 `X-MemVane-UI: 1` 없이 `Sec-Fetch-Site` 부재 시 fail-open 위험 및 `BroadcastChannel` 백그라운드 throttling 경합 → `fetch`+`ReadableStream` SSE 파서(또는 `Sec-Fetch-Site == same-origin` 필수) + `navigator.locks` 기반 리더 선출 권장.
- **[N-08 / P2]** `host_response.py` main(`:1223`)·worktree(`:1473`) 양쪽 `update-queued` 분기에 opt-in 연결, `$HOME` 기준 `state_base(home)` 정합 및 `_fixture_live_home` 방어, detached `Popen`에 `-B` 플래그 명시.
- **[N-09 / P2]** `vite.config.ts` `esbuild: { legalComments: 'none' }`(서드파티 라이선스 URL 오탐 방지), `local_client.py` `Connection: close`, `skills/ui/` `ui.py` 포워더 권장.
- **[N-10 / P2]** `autoOpenScope`는 Phase 1에서 `onboarded_sessions`만 구현 권장(`all_sessions`는 `hooks/run-hook` `legacy` 경로 및 `docs/privacy.md:5-7` 수정 수반).


---

## 3차 개정(r3) 반영 체크

- **반영 일시**: 2026-11-24
- **대상 문서**: [design-local-web-ui.md](../design-local-web-ui.md) (r3)

### 2차 리뷰 잔여 항목 (`N-01` ~ `N-10`) 반영 확인
- `N-01` (P0): §2.1 디렉터리 트리 루트를 `<plugin-root>/`로 변경하여 `tests/memvane-naming-guard.test.mjs` 위반 제거.
- `N-02` (P0): §5.2 및 §6.3에 `X-MemVane-Control` 인증 `POST /api/internal/code` 추가, `ui serve` 초기 URL 1회 출력 명시.
- `N-03` (P1): §4.3.2 `dry_run_recall(use_selector=True)` 시 추론 소켓·`admit()` 사전 확인 및 no-op reject 콜백으로 cold 스폰 완전 차단, `worktree`/`preview` route 분기 및 `DryRunRecallResult` `_VALUE_SOURCES` 등재(§9.2).
- `N-04` (P1): §2.2 `runtime_main(['snapshot-readonly', ...], execute=collector)`의 `int` 반환·콜백 dict 전달·`service_root(create=False)`·`QMD_SETUP_GUARD_FIXTURE=1`·보조 JSON 읽기 명시.
- `N-05` (P1): §2.2 및 §10.2 `shared.py` 실측 `2,319/2,319`(여유 0) 정정 및 `learning/api.py` 무mock 단일 호출처 통합 명시.
- `N-06` (P1): §4.1 `targetId → target_root` 2단계 역조회 및 §4.3.3 worktree 선택 시 `.memvane/compile/`·`events.jsonl`은 main(`installId`) 소유임을 명시.
- `N-07` (P1): §6.4 및 §7.2 `/api/events/stream`을 `fetch` + `ReadableStream` SSE(`X-MemVane-UI: 1` 필수)로 통일하고 Web Locks(`navigator.locks`) + `BroadcastChannel` 리더 선출 채택.
- `N-08` (P2): §5.1, §5.2.4, §5.4에 `host_response.py` main/worktree 양쪽 연결, `$HOME` 기준 `state_base(home)` + `_fixture_live_home` 방어, detached `Popen` `-B` 명시.
- `N-09` (P2): §3.2 `vite.config.ts` `esbuild: { legalComments: 'none' }`, §2.2 `local_client.py` `Connection: close`, §5.3 `skills/ui/scripts/ui.py` 포워더 반영.
- `N-10` (P2): §5.4 Phase 1 `autoOpenScope`를 `onboarded_sessions`만 구현으로 확정(`all_sessions`는 M2 후속으로 분리).

### 사용자 UI 가안 피드백 반영 확인
1. **사이드바 `Initial Builds` 제거**: §4.2에서 좌측 사이드바 미활성 `Initial Builds` 섹션 제거, 온보딩 프로젝트 + 하위 worktree 트리만 유지.
2. **학습 모니터 단계·지표 한글 설명**: §4.3.4에 4종 승격 임계치·케이스 순환·평가 지표 한글 명칭과 1줄 의미 설명 명시.
3. **Wake 큐 사용자 언어 표현**: §4.3.4에 `작업 종류 | 대상 프로젝트 | 현재 상태 | 다음 실행 / 재시도` 및 하단 상태 범례 명시.

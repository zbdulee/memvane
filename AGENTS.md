# AGENTS.md

This file provides guidance to Codex and other coding agents when working with code in this repository. (Claude Code reads `CLAUDE.md`; shared collaboration rules live here, while host-specific details may differ.)

qmd 기반 자동 컨텍스트 주입 훅을 **Claude Code · Codex · Hermes Agent · Antigravity(Gemini)** 에서 동작시키는 플러그인. 사용자 안내는 `README.md` 참고. 이 문서는 코드 작업 시 알아야 할 구조·명령·함정에 집중한다.

사용자용 문서와 응답에서는 설치 후 운영 인터페이스를 **자연어 요청 또는 노출된 skill 호출** 중심으로 안내한다. `core/`·`skills/`의 shell command는 유지보수/디버깅 인터페이스이므로, 사용자가 명시적으로 요청한 경우나 개발자 문서가 아닌 이상 README 같은 public docs에 직접 실행 흐름으로 노출하지 않는다.

개발 시에는 **Claude Code · Codex · Hermes Agent** 세 host의 동작성을 모두 고려한다. 공통 로직은 `core/`에 두고, host별 adapter/hook 차이(Claude/Codex command hook, Hermes Python plugin 및 observer-only `post_tool_call`)가 깨지지 않는지 함께 확인한다.

## 개발 협업 방식과 Claude 활용

이 절은 프로젝트 개발 작업의 협업 방식이다. 기존 권한·보안·자원 규칙을 완화하지 않으며, 사용자 승인 범위를 넘는 실행은 별도로 확인한다. 현재 사용자의 다른 에이전트 토큰 예산이 부족하므로 Claude를 우선 활용한다.

- 구현, 결함 수정, 테스트 작성, 문서 정리, 근본 원인 분석, 설계·코드 리뷰는 대부분 Claude Opus에 우선 맡긴다. 단순한 한 파일 수정에 과도한 위임을 만들지 않는다.
- 주 에이전트는 작업 범위와 의존성을 정하고 각 Claude 호출에 맡길 범위를 배정한다. 결과의 조율·통합·검토, 필수 저장소 가드 검증, 릴리스 관리, 사용자 보고는 주 에이전트가 직접 책임진다.
- 새 서브에이전트 생성이나 자동 에이전트 분산을 권장하지 않는다. 이미 진행 중인 작업은 결과와 변경을 안전하게 회수하고 파일 충돌 없이 정리한다.
- 각 Claude 호출에는 변경 가능한 파일·모듈, 기대 입력·출력, 완료 조건과 금지 범위를 명시한다. Claude는 위임받은 정확한 범위와 저장소 가드·기존 파일시스템·네트워크·보안 승인 안에서만 파일을 수정하거나 명령을 실행한다. 주 에이전트에게 거부된 접근이나 권한을 Claude를 통해 우회하지 않는다.
- 좁은 Claude 호출은 서로 다른 파일을 다룰 때만 병행한다. 같은 파일을 동시에 수정하지 않는다. 공용 가드 테스트·백업·해시·실제 모델 작업은 위 공용 개발 가드와 자원 규칙에 따라 순차 실행한다. 독립 리뷰도 모든 검사 종료 후 순서대로 진행한다. 같은 대상에 대한 중복 리뷰, 이미 끝난 기초 리뷰의 반복, 토큰 소진 자체를 목표로 한 호출은 피한다.
- Claude 호출 전 해당 실행 환경의 도구가 지원하는 모델과 옵션을 확인한다(`claude --help` 또는 제공된 모델 목록). 확인되지 않은 모델 이름을 추정하지 않는다. 실제 응답의 `modelUsage`와 실행 결과를 작업 기록에 남긴다. 긴 호출은 코드·질문 범위를 줄이고 유한 timeout을 둔다. timeout이나 불명확한 종료 뒤에는 기존 실행 상태·산출물을 확인하고 중복 호출을 피한다.
- 로그인·인증 실패와 샌드박스 DNS·네트워크 관측 실패를 구분한다. 승인된 정상 실행 경로만 사용하고 권한 거부나 개발 가드를 우회하지 않는다. Claude가 불가능하면 정확한 사유와 대안을 보고하며 사용한 척하지 않는다.
- 외부 모델에는 작업에 필요한 코드와 합성 자료만 전달한다. 비밀·인증 정보·원고·운영 DB 내용은 보내지 않는다. Claude 결과는 그대로 채택하지 않고 코드와 계약을 직접 검토하며, 구체적 지적은 재현한 뒤 최종 코드에 맞는 가드 검사를 수행한다.
- 구현 상태, 검증 근거, 실패한 검사와 그 출력, 남은 차단 항목을 구분해 정직하게 보고한다.

## 명령

로컬 `.development-hold.json`이 true인 동안 일반 검사·백업/해시·모델 작업·운영 활성화를 재개하지 않는다. 해당 보류를 해제하는 사용자 지시가 있어야 재개하며, 실행 허가는 자원 기준이나 소유 프로세스 정리 가드를 우회하는 권한이 아니다. 세션별 승인 기록과 기계 상태는 이 공개 문서 대신 로컬 작업 기록에 남긴다.

테스트·백업·해시 같은 무거운 개발 작업은 사용자 전역 `scripts/development_guard.py` 잠금 1개를 공유한다. 다른 worktree/에이전트의 검사를 동시에 시작하지 않는다. 테스트 파일 병렬도는 1이며, 내부 자식 프로세스까지 합산한다. 독립 리뷰도 모든 검사 종료 후 순서대로 진행한다. 보호 장치 자체의 작은 합성 단위 검사(`test:guard-unit`)만 bootstrap 예외다. 실제 프로세스·모델 검증은 OS 관측이 가능한 승인된 일반 호스트에서 가드를 통해 실행한다. 샌드박스의 관측 거부는 명시적으로 보고하고 가드를 끄지 않는다. 런타임 조율은 `docs/runtime-resource-coordination.md`, 보호 장치 기준·체크포인트·샘플링 한계는 `docs/development-resource-safety.md`를 읽는다.

```bash
npm run test:guard-unit                    # 작은 합성 보호 장치 단위 검사만 순차 실행
npm test                                   # guard + 파일 병렬 1
npm run test:focused -- test/recall.test.mjs # 같은 guard/잠금/병렬 1
npm run dev:guard -- --kind backup -- <명령> # 테스트와 같은 잠금
npm run dev:guard -- --kind hash -- <명령>   # 같은 잠금
npm run dev:guard -- --kind model-test -- node --test <실제 모델 시험> # 학습용 자원 제한 유지

bash scripts/agy-local-hook-install.sh <프로젝트>  # Gemini(agy): 공식 payload adapter 전까지 stale qmd AGY hook cleanup only
bash scripts/cleanup-legacy.sh --dry-run    # 기존 글로벌 qmd 훅/managed LaunchAgent cleanup 계획 확인
bash scripts/cleanup-legacy.sh              # 기존 글로벌 qmd 훅/managed LaunchAgent cleanup 실행

bash skills/sync/scripts/sync.sh <프로젝트>  # 수동 CUD sync: snapshot 비교 → dirty queue enqueue
bash skills/query/scripts/query.sh <프로젝트> "<질문>"  # 수동 recall query(core/recall.py 경유)
bash skills/update/scripts/update.sh <프로젝트>  # 수동 SessionStart update(core/update.sh 경유)
bash skills/wiki-compile/scripts/wiki-compile.sh <프로젝트> < compact.json  # compact durable summary → wiki_compile.py

bash core/update.sh --recommend [<경로>]              # 추천 확인 (read-only, 파일 변경 없음)
bash core/update.sh --recommend --json [<경로>]       # 추천 결과를 JSON으로 출력
bash core/update.sh --optin --recommended [<경로>]    # 추천 적용 → .auto-context/settings.json 원자 생성
bash core/update.sh --optout [<경로>]                 # 로컬 decision store에 거절 기록(프로젝트 파일 수정 없음)
bash core/update.sh --migrate-config [<경로>]         # 레거시 .auto-context.json → .auto-context/settings.json 이동
bash core/update.sh --init-wiki [<경로>]              # .auto-context/wiki scaffold 생성 + wiki recall 활성화
bash core/update.sh --enable-compile [<경로>]         # wiki scaffold + compile 설정 한 번에 (recommended 온보딩에서 기본 활성화)
bash core/update.sh --skip [<경로>]                   # 이 프로젝트 임시 gate 통과 마커 (TTL 2h, cwd 단위)
```

일반 모의·회귀·통합 검사(`--kind test`, CI 포함)는 학습용 OS 압력·CPU/RSS·headroom·swap·load 제한을 적용하지 않는다. 전역 단독 실행, 파일 병렬도 1, 유한 timeout, 프로세스 수와 소유 식별·정리는 유지한다. 실제 모델을 로딩/학습하는 개발 검사는 `--kind model-test`로 명시해 기존 자원 제한을 유지한다. 명시적 calibration envelope도 항상 자원 검사를 유지한다. 제품 런타임의 학습 admission은 이 구분과 무관하게 그대로 적용된다.

테스트/설치를 격리 검증할 때 쓰는 env 가드 (테스트 코드가 이걸로 부작용을 막는다):
- `QMD_FAKE_PLATFORMS=claude,codex,gemini` — 실제 감지 대신 플랫폼 목록 강제 (`none`도 가능)
- `QMD_SETUP_GUARD_FIXTURE=1` — `npm test`가 오래된 합성 fixture의 hook/worker 경로만 통과시키는 테스트 전용 override. 새 upgrade guard 회귀는 이를 비우고 실제 `setup_required` 경로를 검증한다. 운영 hook에서는 설정하지 말 것.
- `QMD_BACKEND_MANAGER=/path/to/manager.sh` — tests/hooks에서 backend manager override
- `QMD_CLEANUP_LEGACY=1` — managed legacy LaunchAgent cleanup opt-in
- `QMD_SKIP_BACKGROUND_WORKER=1` — `update.sh`의 **detached worker fork 자체**를 건너뛴다. `QMD_SKIP_BACKGROUND_EMBED`와 역할이 다르다: 그쪽은 "fork 는 하되 embed·dedup 스캔을 건너뛰라"이고 이쪽은 "fork 하지 말라"다. 왜 둘이 필요한가 — worker 는 스크립트를 **처음부터 재실행**하므로 선두의 `mkdir -p "$_QMD_CACHE_DIR"`가 테스트가 이미 지운 임시 디렉터리를 되살린다(`removeTemp`는 성공하는데 그 **뒤에** 되살아나 정리 쪽에서는 막을 수 없다). 끄지 않으면 테스트 실행마다 `~/.cache`에 빈 디렉터리가 누적된다. 켠 파일: healthcheck·update·orphan-reclaim·deprecated-keys. **worker 가 실제로 돌아야 하는 테스트는 그 자리에서만 `''`로 해제한다**(fork 회귀 가드, 손상 원장 격리) — 파일 전역으로 켜고 예외를 명시하는 형태가 per-site 로 켜는 것보다 낫다(다음에 추가되는 테스트가 자동으로 보호된다). **실환경에서 절대 설정하지 말 것** — 인덱스 갱신·소실 스캔·dedup 이 전부 이 worker 에 붙어 있어 SessionStart 가 아무 유지보수도 하지 않는다
- `QMD_QUERY_FIXTURE=test/fixtures/*.json` — 데몬 응답을 파일로 주입 (라이브 데몬 없이 결정적 검증)
- `QMD_SANDBOX` / `GEMINI_SANDBOX` / `--sandbox` — 디스패처·코어 즉시 무출력 종료

**테스트는 세션 env 의 `QMD_RECALL_LOG` 를 물려받으면 안 된다** — 62개 테스트 파일 중 51개가 `{...process.env}` 를 그대로 자식에 넘기고 이 변수를 한 번도 주지 않으므로, 사용자가 그 env 를 전역으로 켜 두면 테스트가 **라이브 진단 로그에 쓴다**(실측: `hook-main.test.mjs` 1회 실행에 57줄, `~/.cache/qmd/recall-diag.jsonl` 에 traceback 610건 누적. 전부 첫 프레임이 `hook_main.py`). 파일별로 고치지 않고 **러너 진입점**에서 막는다 — `npm test` 는 `QMD_RECALL_LOG= node --test` 다(빈 문자열이면 `os.environ.get` 이 falsy 라 미설정과 같고, 테스트가 명시로 주는 경로는 그대로 먹는다). 직접 `node --test <파일>` 로 돌리면 이 가드가 없으므로, 예외를 일부러 던지는 파일은 자기 헬퍼에서도 비운다(`hook-main.test.mjs` 의 `cleanEnv`). 이건 `managerEnv` 가 포트 8483 을 assert 로 거부하는 것과 같은 격리 규칙이다 — 테스트가 제품 데이터를 건드리면 안 된다. **worker 부작용을 단정하는 테스트는 `QMD_LOCK_BASE`를 격리한다.** `update.sh`의 worker 는 전역 writer 락(`$TMPDIR/qmd-auto-context-locks-<user>` — `_QMD_UID`는 `id -un`이라 **사용자 이름**이다)을 잡지 못하면 `acquire_lock`이 즉시 실패해 `SKIP: qmd update already running`으로 **exit 0** 한다 — 기다리지 않는다. 다투는 상대는 같은 스위트의 다른 worker 와 **실제 세션의 SessionStart 훅**이므로, 격리하지 않으면 단독 실행은 늘 통과하고 부하 중에만 터진다(flake 로 보인다). **대기 예산(`QMD_TEST_WAIT_TIMEOUT_MS`)을 늘리는 것으로는 고쳐지지 않는다** — worker 가 포기하고 나가므로 아무리 기다려도 산출물이 생기지 않는다. 전역 락을 살아 있는 pid 로 점유하면 100% 재현된다. 짝이 되는 규칙: **worker 가 필요 없는 테스트는 `QMD_SKIP_BACKGROUND_WORKER`로 fork 자체를 끈다**(락 경합도 임시 디렉터리 잔해도 사라진다). 즉 worker 를 쓸 것이면 락을 격리하고, 쓰지 않을 것이면 fork 를 끈다 — 둘 중 하나는 반드시 한다. 테스트 작성 시 주의: `execFileSync`는 반드시 `encoding:'utf8'`을 줄 것 (없으면 Buffer 반환 → `.trim()` 에러). 병렬 실행에서 `core/__pycache__`가 spurious 실패를 유발할 수 있어 `.gitignore` 처리돼 있다.

## 아키텍처 (큰 그림)

**3층 구조: 플랫폼 무관 코어 1벌 + 얇은 host adapter + plugin-managed 백엔드.**

```
core/      ← 모든 로직. backend_manager.sh + 플랫폼/도메인 무관 core. stdin {prompt,cwd} → stdout 훅 JSON
hooks/     ← Claude/Codex/Gemini run-hook 디스패처 + hooks.json/hooks-codex.json. backend ensure/kick 후 코어로 패스스루
hermes_adapter/ ← Hermes Agent plugin hook adapter(pre_llm_call/on_session_start/pre_tool_call/post_tool_call) → core로 패스스루
backend/   ← qmd MCP HTTP 데몬(:8483) launcher + keepalive/logrotate/index worker one-shot scripts
```

편집 후 자동 인덱싱: PostToolUse 훅이 편집 파일을 dirty 큐(기본 `~/Library/Application Support/qmd-auto-context/backend/dirty-queue`)에 원자 append → backend manager가 one-shot worker(`backend/index_worker.sh`)를 비동기로 kick → worker가 claim/ACK로 큐를 drain하며 재인덱싱.

host route: `hooks/run-hook`은 등록된 모든 action을 먼저 `core/laya_index_host_route.py classify-hook`으로 분류한다(`legacy|candidate|preview|blocked`). 선택된 Laya route(candidate/preview)는 `core/laya_host_response.py serve-stdin`이 응답하고, 그 검색은 위키 전용이며 질의 시점에 원문을 다시 읽지 않는다. blocked는 QMD로 폴백하지 않고 exit 3으로 끝난다. 이 문서의 QMD 데몬·dirty 큐 경로는 legacy route다. 수동 skill(query/sync/update/wiki-compile)은 route 분류 없이 QMD 경로를 실행한다.

코드·테스트 주석의 "로드맵 N단계"는 주입 품질 로드맵의 단계 번호다: 1=주입에 카드 본문·경로 포함, 2=sources.path 주입, 3=source_missing 정책, 4=verify 엔진 분리(crossEngine), 5=crowding_probe 측정 도구, 6=verify 처리량·per-run cap, 7=role source, 8=raw 인덱스 유지 판정과 orphan 벡터 회수, F1=write-time dedup 교차 엔진. "3.8"은 sourceRevisions 백필(scripts/wiki-revision-backfill.py)이다. 테스트 주석의 "계획 2026-08-19 §N"(recall 효용 회복 계획)은 §2.1=사라진 worktree를 가리키는 컬렉션 경로, §3.4=죽은 설정 키(sourceScan·sourceMissingPath·maxCardsPerSource) 처리, §3.5=설정 경로와 qmd 레지스트리 경로 대조를 가리킨다.

### 코어 (`core/`)
- `recall.py` — UserPromptSubmit 핵심. 흐름: `config.load_project_config(cwd)` → 키워드 추출(`keywords.py`) → 데몬 `/query`(lex+vec 하이브리드, 또는 `QMD_QUERY_FIXTURE`) → skipPaths/minScore 필터 → topN → `additionalContext` 포맷. **CLI fallback은 없음** — 데몬 죽었거나 timeout이면 graceful하게 빈 출력(에러 아님).
- `update.sh` — qmd 인덱스 갱신. SessionStart hook은 `update_hook_queue.py enqueue`만 하고, 이 스크립트는 그 detached worker와 수동 `update` skill이 실행한다. canonical `.auto-context/settings.json`의 collection root가 사라진 경우 먼저 `qmd collection remove`를 호출하고 성공한 collection만 settings에서 원자 제거한 뒤 update를 계속한다. `--resolve-only`, `--migrate-config`, `--init-wiki`, `--enable-compile` 모드 있음(`--init-wiki`는 scaffold와 wiki collection/role/hierarchical recall 설정을, `--enable-compile`은 wiki scaffold + compile 설정까지 함께 적용).
- `update_hook_queue.py` — SessionStart enqueue(로컬 setup 안내 1회)와 detached worker. worker는 인덱스 갱신 뒤 `setup_readiness.refresh_adapter_attestation`으로, 선택된 Laya 세대가 adapter 해시만 어긋날 때 같은 세대·모델로 재증명한다(`runtime_installer.reattest_active`, `previous` 보존). 증명된 adapter 파일 경로만 사라졌고 현재 adapter 내용 해시가 같으면 스모크 없이 경로만 갱신한다(`repointed`). 다른 드리프트가 섞이면 손대지 않고, 자원 부족은 다음 세션으로 미룬다. 스모크 실패는 activation 잠금 안에서 검사한 포인터(adapter·runtime·실행 파일·모델) 기준으로 queue의 `laya-adapter-refresh-*.failed.json`에 1회 기록해 안내를 되살리고, 그 조합이 증명되면(재증명·`current`·`repointed`) 그 조합의 기록만 지운다. 증명한 플러그인 버전은 포인터가 아니라 `adapter-owner.json`에 adapter 해시와 함께 남긴다(구버전 호스트가 `active.json` 키 집합을 엄격 검사한다). 활성화·롤백·재증명 모두 포인터보다 먼저 쓰고, 해시가 포인터와 다르면 무시한다. SemVer(prerelease 포함)로 더 오래된 호스트는 새 증명을 되돌리지 않고(`older_host`) 안내만 띄운다. 이 보호는 0.36.22 이상 호스트끼리만 성립한다 — 그 이전 호스트는 기록을 모르므로 함께 쓰면 갱신해야 한다. refresh는 완료가 확인된 실제 update 뒤에만 돈다 — `QMD_CORE_UPDATE_SCRIPT` override, `QMD_SETUP_GUARD_FIXTURE=1`, `QMD_SANDBOX`, update 실패면 건너뛴다(이 override를 쓰는 테스트가 실제 HOME을 넘겨 실제 런타임 포인터를 worktree 경로로 바꾼 실측이 있다). 그 외 HOME을 격리하지 않은 worker 테스트는 `_refresh_laya_adapter`를 patch한다.
- `laya_qmd_vector_reuse.py` — 마이그레이션 전용. 초기 빌드에서 문서 해시·연속 seq·청크 수·증가하는 pos·모델 파일명·차원이 기존 QMD DB와 정확히 같으면 그 벡터를 복사한다. 원천은 QMD 원본 빌드면 identity의 `sourceDb`, fresh(wiki 전용) 빌드면 `qmd_shared_store.select()`가 고른 플러그인 공용 QMD DB이며 읽기 전용으로 열고 빌드 identity에는 넣지 않는다(선택된 저장소가 없으면 임베딩). fresh 빌드는 재사용할 수 있을 때 문서가 모델을 필요로 하는 시점에야 상주 임베딩 서비스를 띄운다. 각 실행의 첫 문서와 25개마다 하나는 다시 임베딩해 청크별 코사인 0.999 이상인지 확인한다(미달 시 그 실행에서 재사용 중단). vec0 로드 실패·원본 부재는 조용히 임베딩으로 돌아간다. QMD 마이그레이션 경로를 지울 때 함께 지운다.
- `posttool.py` — 편집 후 연속성 힌트. `is_story_path`는 config의 `collectionPaths`로 판별(하드코딩 없음). 이벤트명 `PostToolUse`(claude/codex)와 `AfterTool`(gemini) **둘 다** 수용. 내부적으로 recall.py를 subprocess로 위임.
- `config.py` — 사용자 로컬 optout marker(`~/.config/qmd/optout`)를 먼저 확인한 뒤, `.auto-context/settings.json`(없으면 레거시 `.auto-context.json`, `.agents/qmd-recall.json`) 로드 + 기본값 병합. 숫자 필드(minScore/topN/queryTimeout) 보수적 coercion, 실패 시 기본값. legacy novel 컬렉션명(`*-manuscript`/`*-plot`)은 `lexicalPatterns:["ep"]` 자동 활성화.
- `resolve_paths.py` — collectionPaths→경로 매핑 + risky path / allowRoots traversal 검증.
- `dirty_queue.py` — dirty 큐 append SSOT. 기본 경로는 `~/Library/Application Support/qmd-auto-context/backend/dirty-queue`(`QMD_DIRTY_QUEUE` override)이고, 레거시 `~/.config/qmd/` 아래 큐를 가리키면 `legacy_dirty_queue_alias_forbidden`으로 거부한다(`QMD_SETUP_GUARD_FIXTURE=1` fixture 모드만 예외). 행은 `<collection-name>\t<collection-path>`이고 선택적 3번째 `\t<project_root>` 필드가 붙는다. 소비는 `backend/index_worker.sh`가 `core/dirty_queue_claim.py`로 큐 prefix를 durable claim한 뒤 처리하고, 성공하면 sidecar lock 아래 원자 교체로 ACK한다. 실패·busy면 claim을 보존해 다음 kick이 재시도한다.
- `backend_manager.sh` — plugin runtime backend lifecycle SSOT. qmd version check(`>=2.5.3 <3.0.0`), daemon ensure/reload, health-only keepalive, logrotate, async `index_worker.sh` kick, explicit legacy cleanup. Hooks must keep stdout silent; manual skills may print install guidance. qmd 자동 설치/업그레이드는 하지 않는다.
- `index_enqueue.py` — PostToolUse hook. config 게이팅(collections 미설정/indexing:false/event 비활성/collectionPaths 밖) 후 편집 파일이 속한 (컬렉션명, 절대경로)를 dirty 큐에 원자 append. stdout 무출력.
- `sync.py` — 수동/skill 기반 missed CUD 복구. `.auto-context/settings.json`의 `collectionPaths`를 snapshot(`mtime_ns + size`)과 비교해 변경된 collection만 dirty 큐에 append한다. `skipPaths`는 recall 필터라 sync/delete cleanup에는 적용하지 않는다.
- `file_change_queue.py` — Claude의 성공한 Bash PostToolUse는 프로젝트별 pending 표시만 기록한다. 기존 Stop/SessionStart handoff가 실제 파일 snapshot·Markdown hash를 비교해 내부 컬렉션의 CUD/rename을 기존 owner-bound 큐에 합친다. 명령 문자열은 읽거나 저장하지 않으며 외부 allowRoots·실패/미확인/백그라운드 작업은 제외한다. 별도 sync wrapper 실행 없이 기존 큐와 compile 정책을 따른다.
- `collection_match.py` — 편집 경로 → collectionPaths longest-prefix 컬렉션 선정. 복수 컬렉션 지원, 컬렉션 밖 편집은 빈 결과.
- `recommend_config.py` — `--recommend`용 추천 생성. read-only(`.auto-context/settings.json` 쓰지 않음). `docs/current`·`docs/plans`·`docs` 등 좁은 경로를 탐색해 크기 가드(200파일/5MB) 통과 경로만 추천. `{available, config}` JSON 출력. 쓰기는 `--optin`/`--optin --recommended`에서만.
- `preflight_gate.py` — PreToolUse hook. pending 프로젝트에서 Edit/Write/apply_patch 등 편집 도구를 deny로 차단(Claude·Codex). sandbox·skip 마커·pending 아닌 상태면 즉시 통과.
- `wiki_compile_defaults.py` — recommended 온보딩·`--enable-compile`·SessionStart notice가 공유하는 compile 설정 블록 생성 헬퍼. generated config에는 절대 adapter 경로를 쓰지 않고 `compile.extractor.builtins`에 `claude`/`codex`/`hermes` 같은 symbolic engine만 저장한다.
- `wiki_compile_enqueue.py` / `wiki_compile_worker.py` — 자동 compile source는 `raw`/`session` Markdown만 허용하며, relative path의 어느 segment라도 `.`로 시작하면 skip/reject한다. `.agents/skills`, `.claude`, `.codex`, `.github`, `.auto-context` 같은 agent/tooling metadata를 wiki로 승격하지 않기 위한 기본 안전장치다.
- `extractors/` — host-CLI wiki extractor adapters (`claude_adapter.py`, `codex_adapter.py`, `hermes_adapter.py`) + `lib.py`. Pure `payload→{candidates}` functions run in an isolated temp cwd with tools/custom context disabled. Worker는 `compile.extractor.argv`(legacy explicit) → `compile.extractor.backends[engine]`(explicit) → `compile.extractor.builtins`(runtime-resolved adapter) → `extractor.default` 순서로 선택한다. **플러그인 install = consent**: 설치 후 첫 SessionStart에 일회성 안내 notice가 표시되며, recommended 온보딩(`--optin --recommended`)·`--enable-compile`이 기본으로 compile을 활성화한다. Exit 127 = host CLI absent (worker then tries `extractor.default`). 격리: `lib.run_isolated`가 자식 env에 `QMD_SANDBOX=1`을 넣어 nested CLI의 qmd 훅을 즉시 무력화(재귀 차단)하고, claude는 `--safe-mode`·`--tools ""`·`--no-session-persistence`, codex는 `--ephemeral`·`--ignore-user-config`·`--ignore-rules`, hermes는 `--safe-mode`·`--ignore-user-config`·`--ignore-rules`·`-t ""`로 MCP/skills/plugins/rules/tool context를 최소화한다(hermes는 동등 persistence-off 플래그 없어 ~/.hermes에 1회성 기록이 남을 수 있음).

### skills (`skills/`)
- `sync` — agent-facing 수동 동기화 workflow. wrapper가 qmd 설치/버전을 확인하고 `core/sync.py --json` 실행 후 실제 변경이면 `backend_manager.sh kick-index`를 호출한다. 자동 hook이 아니며 사용자가 sync/resync를 요청할 때만 쓴다.
- `query` — legacy route의 recall과 같은 `core/recall.py`(QMD 경로)를 수동 실행한다. Laya route 분류는 하지 않는다. 실행 전 `setup_guard.py check`와 backend manager가 qmd/daemon을 확인한다. qmd 데몬 직접 호출을 중복 구현하지 말 것.
- `update` — `core/update.sh`(QMD 경로)를 수동 실행한다. Laya route 분류는 하지 않는다. 실행 전 backend manager가 qmd/daemon/warm/logrotate를 처리한다. qmd 인덱스 갱신 요청에는 이 skill을 쓴다.
- `setup` — 명시적 설치·managed runtime 준비·보존 업그레이드 진입점. `core/setup_entry.py`로 설정을 탐색해 결정적 setup plan을 만들고, 적용·롤백은 명시 요청 시에만 한다. SessionStart의 setup 안내가 가리키는 skill이다.
- `enable-compile` — opt-in된 프로젝트에 자동 wiki compile을 켠다(`core/update.sh --enable-compile`). 편집 시 host CLI가 백그라운드로 실행된다는 점을 사용자에게 알린다.
- `wiki-compile` — compact durable summary/candidate JSON을 `core/wiki_extract.py` → `core/wiki_compile.py`로 수동 실행한다. raw transcript를 입력하지 않으며 query-time hook에서는 쓰지 않는다.
- `wiki-dedup` — retroactive scan(`core/wiki_dedup_scan.py`)이 `dedup-needed.jsonl`에 큐한 이미 존재하는 wiki 카드 쌍을 `agents/wiki-dedup-resolver.md` 에이전트로 정리한다. write-time `merge-needed.jsonl`은 passive diagnostic이라 이 workflow 밖이다.
- `wiki-source-repair` — `source-missing.jsonl`의 원문 소실 카드에 명시된 `repoint`/`dismiss`를 적용한다. 카드를 삭제하거나 status를 바꾸지 않는다.
- `hint`에 해당하는 skill은 만들지 않는다. PostToolUse posttool은 편집 직후 자동 실행되는 hook-only 연속성 힌트다.
- `gate`에 해당하는 skill은 만들지 않는다. gate는 pending 프로젝트 편집 차단용 내부 안전장치다.

> **dogfooding**: 이 저장소 자체의 `.auto-context/settings.json`은 `indexing: true`이고, raw 컬렉션 `qmd-auto-context-plans` = `docs/plans`, wiki 컬렉션 `qmd-auto-context-wiki` = `.auto-context/wiki`를 인덱싱한다. 그래서 `docs/plans`의 문서는 이 저장소에서 일하는 에이전트의 recall에 주입된다.

### hooks (`hooks/`) — 유일한 훅 진입점
> Claude/Codex/Gemini의 훅 진입점은 `hooks/run-hook` 단일 디스패처다. Hermes Agent는 별도 host protocol이므로 Python plugin adapter(`plugin.yaml`, `__init__.py`, `hermes_adapter/`)가 같은 core 스크립트를 호출한다. **도메인 로직은 여전히 core/가 SSOT**다.

- `run-hook` — 공통 디스패처(bash). 호출: `run-hook <action> <engine>` (engine: claude|codex|gemini). `hooks.json`·`hooks-codex.json`이 등록한 action은 `update-queued`·`topical-reconcile`(SessionStart), `recall`(UserPromptSubmit), `gate`(PreToolUse), `posttool`·`index`·`compile`·`topical-event`(PostToolUse 편집 도구), `topical-stop`(Stop), Claude 전용 `file-change-event`(PostToolUse Bash)다. 처리 순서: sandbox/headless 가드 → 모든 action을 `core/laya_index_host_route.py classify-hook`으로 분류(`legacy`면 아래 QMD 경로로 계속, `candidate|preview`면 `core/laya_host_response.py serve-stdin`으로 exec, `blocked`면 "QMD fallback refused"로 exit 3) → `update-queued`면 `core/context_learning_host_ack.py` → `core/setup_guard.py hook`(topical은 `hook-root`) 가드 → action별 backend manager ensure/kick와 `core/<script>` stdin 패스스루. SessionStart의 `update-queued`는 `core/update_hook_queue.py enqueue`만 하고 인덱스 갱신은 detached worker가 한다. 플러그인 루트는 env `CLAUDE_PLUGIN_ROOT`/`PLUGIN_ROOT`가 우선이고 없으면 `dirname "$0"`이다. **도메인 로직은 core/가 SSOT.**
- `hooks.json` — Claude hooks (`${CLAUDE_PLUGIN_ROOT}`). `hooks-codex.json` — Codex hooks도 **`${CLAUDE_PLUGIN_ROOT}`**를 쓴다(형태: `"${CLAUDE_PLUGIN_ROOT}/hooks/run-hook" <action> codex`). Codex가 신뢰성 있게 주입하는 plugin-root 변수가 CLAUDE_PLUGIN_ROOT이고, 로컬 캐시의 OpenAI codex·zax·superpowers 등 **모든 codex 플러그인이 codex hooks에서 이 형태를 쓴다**(shell 확장이든 텍스트 템플릿 치환이든 양쪽 메커니즘에서 입증된 유일한 형태). `${PLUGIN_ROOT}`(codex 바이너리가 인식하는 alias지만 간헐적으로만 주입)를 단독으로 쓰면 비었을 때 command가 `/hooks/run-hook`으로 해석돼 **exit 127**(command not found)이 난다 — 실패 지점이 command 확장 단계라 run-hook 내부 dirname fallback으로는 못 막는다. **novel한 `${VAR:-fallback}` 파라미터 확장이나 `;`/`&&` 제어 연산자는 쓰지 않는다**: 어떤 codex 플러그인도 안 쓰는 문법이라, codex가 shell이 아니라 템플릿 치환을 하면 매칭/해석에 실패할 수 있다. run-hook 내부는 여전히 `CLAUDE_PLUGIN_ROOT→PLUGIN_ROOT→dirname` 순서로 방어한다(도달만 하면). CLAUDE_PLUGIN_ROOT 자체가 비면 여전히 127인데, 이는 codex-level 상태로 OpenAI 공식 포함 모든 codex 플러그인에 동일하게 영향한다(우리만의 문제 아님). PreToolUse `gate`도 이 경우 127로 죽어 deny를 못 내므로 pending 편집 차단이 fail-open된다(수용 가능한 트레이드오프). Codex 공식 문서상 same-event command hooks는 동시 실행될 수 있으므로 posttool/index/compile은 서로 순서 의존하면 안 된다.
- Antigravity(agy)는 공식 PostToolUse payload에 edited file path/tool input이 문서화되어 있지 않고 stdout contract도 `{}`라서, 현재 `scripts/agy-local-hook-install.sh`는 깨진 qmd run-hook 항목을 정리만 한다. AGY 자동 posttool/index/compile은 전용 payload adapter가 생긴 뒤 재활성화한다.
- `hooks.json`은 **표준 구조** `{hooks:[{type:"command",command}]}`를 따라야 한다. 비표준 구조면 호스트(Claude/Codex)가 훅을 인식 못 함.
- 매니페스트: `.claude-plugin/plugin.json`, `.codex-plugin/plugin.json`(+`interface`, `hooks` 경로 명시).

### Hermes Agent plugin (`plugin.yaml`, `__init__.py`, `hermes_adapter/`)
- Hermes는 Claude/Codex hook JSON을 읽지 않고 Python plugin system을 쓴다. root `plugin.yaml` + `__init__.py`가 `hermes_adapter.plugin.register(ctx)`를 노출한다.
- Hook mapping: `pre_llm_call`→`core/recall.py`, `on_session_start`→`core/update.sh`, `pre_tool_call`→`core/preflight_gate.py`, `post_tool_call`→`core/posttool.py` best-effort + `core/index_enqueue.py` + `core/wiki_compile_enqueue.py`.
- Hermes `post_tool_call` 반환값은 observer-only라 같은 턴 모델 컨텍스트에 posttool 힌트를 주입하지 않는다. 따라서 Hermes 경로의 편집 후 동작은 자동 인덱싱 중심이며, Claude/Codex posttool 컨텍스트 주입과 동일하다고 문서화하면 안 된다.

### 백엔드 (`backend/`)
- `daemon.sh` — qmd HTTP MCP daemon foreground launcher. manager가 필요 시 `nohup`으로 시작한다.
- `keepalive.sh` — one-shot health-only keepalive. 기본은 `/health`만 확인하고, 전역 vec warm ping은 `QMD_KEEPALIVE_VEC_WARM=1` opt-in일 때만 실행한다.
- `logrotate.sh` — one-shot log size guard. 대상이 둘이고 **회전 방식이 다르다**: 데몬 로그는 데몬이 fd 를 잡고 있어 mv 후 graceful reload 가 필요하지만, recall 진단 로그(`QMD_RECALL_LOG`)는 훅이 append 마다 open/close 하므로 mv 만으로 끝이고 **reload 하지 않는다**(진단 로그가 커졌다고 유료 경로를 흔들면 안 된다. 회전 순간에 쓰던 프로세스는 옛 inode=`.1` 에 그 줄을 쓰고 끝나 유실·섞임이 없다). 이 진단 로그는 프로젝트 무관 전역 파일 하나라 방치하면 계속 자란다(실측 14.6MB·26,493줄). **두 판정은 독립이어야 한다** — 데몬 로그 부재 시 첫 줄에서 `exit 0` 하는 식으로 판정을 딸리게 두면 데몬 로그가 없는 것만으로 진단 로그가 영원히 회전되지 않는다(`test/backend.test.mjs` 가 고정). 세대는 `.1` 하나뿐이라 총량은 대상당 ~2×MAX 다. `QMD_DAEMON_LOG`/`QMD_RECALL_LOG`/`QMD_BACKEND_MANAGER`/`QMD_DAEMON_PID`를 지원한다.
- `index_worker.sh` — one-shot dirty 큐 drain. `dirty_queue_claim.py claim`으로 큐 prefix를 durable claim → writer lock 획득 → `qmd collection add`+`update`+`embed`(embed lock으로 update.sh와 직렬화) → 새 임베딩/삭제 있으면 manager reload → `ack`. busy/실패 시 claim과 큐를 보존해 다음 kick에 재시도(coalesce).

### 설치 / cleanup
Claude·Codex는 marketplace plugin install이 제품 경로다. 제품용 `install.sh`/`uninstall.sh`는 없다. AGY는 공식 PostToolUse payload adapter 전까지 자동 qmd hook을 설치하지 않으며, `scripts/agy-local-hook-install.sh`는 과거 stale qmd run-hook cleanup만 수행한다. 기존 레거시 qmd 글로벌 훅/managed LaunchAgent 정리는 `scripts/cleanup-legacy.sh` 또는 `backend_manager.sh cleanup-legacy` 같은 명시적 cleanup에서만 수행한다. 일반 hook 실행 중 LaunchAgent cleanup은 `QMD_CLEANUP_LEGACY=1` opt-in일 때만 허용한다.

### 버전 bump 체크리스트
릴리스 버전을 올릴 때는 모든 host manifest와 테스트 기대값을 같은 버전으로 맞춘다.

- `package.json` — project/npm version
- `plugin.json` — root AGY/Gemini plugin metadata
- `plugin.yaml` — Hermes Agent plugin metadata
- `.claude-plugin/plugin.json` — Claude plugin metadata
- `.codex-plugin/plugin.json` — Codex plugin metadata
- `.claude-plugin/marketplace.json` — Claude marketplace entry
- `.agents/plugins/marketplace.json` — Codex marketplace entry
- `core/context_learning/runtime_authorization.py` — reviewed adapter version identity; a bump does not carry an old learning grant forward
- `test/probe-manifest.test.mjs` — marketplace/root manifest version asserts

`docs/plans/` 문서 안의 버전 문자열은 bump 대상이 아니다. 버전 변경 후 최소 `node --test test/probe-manifest.test.mjs`를 실행하고, 릴리스 전에는 `npm test`를 실행한다.

### 설정 해석 우선순위
`config.load_project_config`는 먼저 사용자 로컬 optout marker(`~/.config/qmd/optout/<hash>.json`)를 확인한다. marker가 있으면 프로젝트 설정 파일이 있어도 `indexing:false`로 해석한다. marker가 없으면 cwd에서 HOME 경계까지 위로 올라가며 `.auto-context/settings.json`을 찾고(없으면 레거시 `.auto-context.json`, `.agents/qmd-recall.json`), **둘 다 없으면 빈 설정(`collections=[]`)을 반환**한다(하위호환 `indexing:false`도 동일). 컬렉션이 비면 recall은 `no_collections`로 **빈 출력** — 즉 미설정/미동의/거절 프로젝트는 무동작이다(cwd 폴더명을 컬렉션으로 삼는 fallback은 없다). update-time 경로(`--migrate-config`)가 `.auto-context.json`을 `.auto-context/settings.json`으로 검증 후 이동하며, query-time recall은 read-only fallback만 수행한다.

## 운영상 함정 (디버깅 시 참고)

- **데몬은 single-thread (Node).** recall query가 다른 query나 opt-in vec warm ping과 겹치면 직렬로 밀려 timeout → 간헐적 빈 출력이 날 수 있다. 기본 keepalive는 health-only이며, `QMD_HEALTH_TIMEOUT` 기본값은 2초(잘못된 값은 2초 fallback)다. 코드 버그로 오인하지 말 것. 측정할 때 연속/동시 호출로 데몬을 폭격하면 이 현상이 증폭된다.
- **`index.sqlite-wal` 비대화 주의.** 대량 임베딩 쓰기(마이그레이션) 후 데몬이 떠 있으면 WAL이 checkpoint 안 되고 수 GB로 누적 → 모든 vec query가 느려진다. manager reload는 SIGTERM 후 bounded wait로 clean close를 유도한다. 평상시 검색은 WAL을 거의 안 키운다.
- **빈 출력은 정상 동작일 수 있다.** 데몬 부재/timeout/결과 0건/sandbox/yield — 모두 의도적으로 무출력 종료한다. 빈 출력 ≠ 버그. **정상인지 버그인지 가르려면 `QMD_RECALL_LOG=<파일>`을 켜고 `qmd_recall_selection` 줄의 `reason`을 보라** (`event_disabled`/`no_keywords`/`no_collections`/`daemon_unreachable`/`query_failed`/`no_results_after_filter`/`selected`). **모든 줄에 `project_root` 귀속이 붙는다.** `QMD_RECALL_LOG`는 프로젝트별 파일이 아니라 **전역 파일 하나**라(env 하나가 한 경로를 준다) 귀속이 없으면 여러 프로젝트의 진단이 섞여 "이 프로젝트의 recall이 나아졌는가"를 물을 수 없다. 값은 `find_project_config`의 `projectRoot`이고 **cwd가 아니다**(하위 디렉터리에서 시작한 세션이 한 프로젝트로 묶여야 한다. 단 그 불변성의 범위는 HOME 하위다 — `_project_search_dirs`가 HOME 밖에서는 부모를 탐색하지 않는다. 거기엔 설정도 없어 `no_collections`로 끝나므로 묶을 진단이 애초에 없다). **인자가 아니라 모듈 상태**(`set_log_project_root`)로 두는 이유는 기록 지점이 20곳이 넘고 절반이 조기 return이라 스레딩하면 다음에 추가되는 지점이 빠뜨리기 때문이다(`hook_main.py`가 per-site try/except를 한 곳으로 모은 것과 같은 클래스). writer는 `log_recall_event`와 `log_score_observation` **두 벌**이므로 한쪽만 고치면 조용히 갈린다. cwd를 알기 전 줄(`empty_stdin`·`invalid_payload_json`·`prompt_too_short`)은 키를 두고 값만 `null`이다 — 키 부재(귀속 도입 전 포맷)와 "아직 모른다"는 다른 사실이다. 이 로그는 파일에만 쓰고 stdout(모델 컨텍스트)엔 절대 안 나가며, env가 없으면 no-op다. index_enqueue도 게이팅으로 skip하는 경우(pending/optout/event 비활성/non-collection-path)는 정상 무출력이다.

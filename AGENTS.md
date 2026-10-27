# AGENTS.md

This file provides guidance to Codex and other coding agents when working with code in this repository. (Claude Code reads `CLAUDE.md`; shared collaboration rules live here, while host-specific details may differ.)

Laya 색인 기반 자동 컨텍스트 주입 훅을 **Claude Code · Codex · Hermes Agent · Antigravity(Gemini)** 에서 동작시키는 플러그인(`qmd-auto-context`). 사용자 안내는 `README.md` 참고. 이 문서는 코드 작업 시 알아야 할 구조·명령·함정에 집중한다.

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
npm run test:focused -- test/laya-runtime-provision.test.mjs # 같은 guard/잠금/병렬 1
npm run dev:guard -- --kind backup -- <명령> # 테스트와 같은 잠금
npm run dev:guard -- --kind hash -- <명령>   # 같은 잠금
npm run dev:guard -- --kind model-test -- node --test <실제 모델 시험> # 학습용 자원 제한 유지

bash skills/query/scripts/query.sh <프로젝트> "<질문>"  # 선택된 Laya 경로의 수동 recall
bash skills/update/scripts/update.sh <프로젝트>          # 선택된 Laya 색인의 full-scan sync wake 큐잉
bash skills/wiki-compile/scripts/wiki-compile.sh <프로젝트> < compact.json  # compact durable summary → wiki_compile.py

# ROOT = 설치된 플러그인 루트(예: 이 버전의 Claude plugin cache 경로). 아래 동작은 --project 없이 실행한다.
python3 -B "$ROOT/core/setup_entry.py" index-runtime-status                    # Laya embed runtime 상태(read-only)
python3 -B "$ROOT/core/setup_entry.py" provision-index-runtime --approve-index-runtime-download [--provider-model-file <gguf>]  # 새 머신의 runtime 구성(npm registry·모델 다운로드)
python3 -B "$ROOT/core/setup_entry.py" gc-index-runtime [--approve-index-runtime-gc]  # 지울 세대 목록 / 승인 시 삭제
python3 -B "$ROOT/core/setup_entry.py" plan-legacy-cleanup --include-user-qmd-data    # 은퇴한 QMD 백엔드 정리 계획(read-only)
python3 -B "$ROOT/core/setup_entry.py" apply-legacy-cleanup --approve-legacy-cleanup --include-user-qmd-data  # owner 1회 정리 실행
```

일반 모의·회귀·통합 검사(`--kind test`, CI 포함)는 학습용 OS 압력·CPU/RSS·headroom·swap·load 제한을 적용하지 않는다. 전역 단독 실행, 파일 병렬도 1, 유한 timeout, 프로세스 수와 소유 식별·정리는 유지한다. 실제 모델을 로딩/학습하는 개발 검사는 `--kind model-test`로 명시해 기존 자원 제한을 유지한다. 명시적 calibration envelope도 항상 자원 검사를 유지한다. 제품 런타임의 학습 admission은 이 구분과 무관하게 그대로 적용된다.

테스트/설치를 격리 검증할 때 쓰는 env 가드 (테스트 코드가 이걸로 부작용을 막는다):
- `QMD_SETUP_GUARD_FIXTURE=1` — `npm test`가 켜는 테스트 전용 스위치(`QMD_SANDBOX`와 같은 자리에서 읽힌다). 켜져 있으면 runtime wake를 시작하지 않고, 실제 계정 홈의 Laya embed runtime·legacy cleanup 대상을 읽거나 쓰지 않는다. 운영 hook에서는 설정하지 말 것.
- `QMD_SANDBOX` / `GEMINI_SANDBOX` / `--sandbox` — 디스패처·코어 즉시 무출력 종료

**테스트는 세션 env 의 `QMD_RECALL_LOG` 를 물려받으면 안 된다** — 62개 테스트 파일 중 51개가 `{...process.env}` 를 그대로 자식에 넘기고 이 변수를 한 번도 주지 않으므로, 사용자가 그 env 를 전역으로 켜 두면 테스트가 **라이브 진단 로그에 쓴다**(실측: `hook-main.test.mjs` 1회 실행에 57줄, 진단 로그에 traceback 610건 누적. 전부 첫 프레임이 `hook_main.py`). 파일별로 고치지 않고 **러너 진입점**에서 막는다 — `npm test` 는 `QMD_RECALL_LOG= node --test` 다(빈 문자열이면 `os.environ.get` 이 falsy 라 미설정과 같고, 테스트가 명시로 주는 경로는 그대로 먹는다). 직접 `node --test <파일>` 로 돌리면 이 가드가 없으므로, 예외를 일부러 던지는 파일은 자기 헬퍼에서도 비운다(`hook-main.test.mjs` 의 `cleanEnv`). 테스트가 제품 데이터를 건드리면 안 된다. 테스트 작성 시 주의: `execFileSync`는 반드시 `encoding:'utf8'`을 줄 것 (없으면 Buffer 반환 → `.trim()` 에러). 병렬 실행에서 `core/__pycache__`가 spurious 실패를 유발할 수 있어 `.gitignore` 처리돼 있다.

## 아키텍처 (큰 그림)

**2층 구조: 플랫폼 무관 코어 1벌 + 얇은 host adapter.** 인덱스·임베딩 런타임은 Laya가 소유한다(아래 host route, `laya_resident_service.py`).

```
core/      ← 모든 로직. 플랫폼/도메인 무관 core. stdin {prompt,cwd} → stdout 훅 JSON
hooks/     ← Claude/Codex/Gemini run-hook 디스패처 + hooks.json/hooks-codex.json. route 분류 후 선택된 route만 코어로 패스스루
hermes_adapter/ ← Hermes Agent plugin hook adapter(pre_llm_call/on_session_start/pre_tool_call/post_tool_call) → core로 패스스루
```

편집 후 자동 인덱싱: 선택된 route의 PostToolUse `index` action이 Laya full-scan sync owner를 깨운다(`laya_host_response.py`). 성공한 agent Bash(`file-change-event`)도 같은 full-scan wake를 낸다. 실제 인덱싱은 hook 밖의 durable worker가 한다.

host route: `hooks/run-hook`은 등록된 모든 action을 먼저 `core/laya_index_host_route.py classify-hook`으로 분류한다(`legacy|candidate|preview|worktree|blocked`). 선택된 Laya route(candidate/preview)는 `core/laya_host_response.py serve-stdin`이 응답하고, 그 검색은 위키 전용이며 질의 시점에 원문을 다시 읽지 않는다. `worktree`는 선택된 main checkout의 연결 git 워크트리(같은 common git dir, 같은 settings 유도 정체성, 워크트리 안에 route 기록 없음)다. main checkout의 게시 세대를 읽기 전용으로 검색하고 결과 앞에 그 사실을 적으며, sync·compile·index wake·학습·setup 안내는 하지 않는다. blocked는 QMD로 폴백하지 않고 exit 3으로 끝난다. `legacy`(선택된 route 없음, 즉 미온보딩)는 인덱스·recall·compile·학습 작업을 하지 않는다. SessionStart(`update-queued`)에서만 `.auto-context/settings.json`이 있는 프로젝트에 setup skill 온보딩을 안내하는 한 줄을 낸다(`laya_index_host_route.py onboarding-notice`, Codex는 SessionStart JSON으로 감싼다). 수동 query/update skill과 wiki-compile wrapper도 선택된 route에서만 동작한다.

코드·테스트 주석의 "로드맵 N단계"는 주입 품질 로드맵의 단계 번호다: 1=주입에 카드 본문·경로 포함, 2=sources.path 주입, 3=원문 소실 정책(현재 `wiki_lifecycle`), 4=verify 엔진 분리(crossEngine), 5=crowding_probe 측정 도구, 6=verify 처리량·per-run cap, 7=role source, 8=raw 인덱스 유지 판정과 orphan 벡터 회수, F1=write-time dedup 교차 엔진. 테스트 주석의 "계획 2026-08-19 §N"(recall 효용 회복 계획)은 §2.1=사라진 worktree를 가리키는 컬렉션 경로, §3.4=죽은 설정 키(sourceScan·sourceMissingPath·maxCardsPerSource) 처리, §3.5=설정 경로와 qmd 레지스트리 경로 대조를 가리킨다.

### 코어 (`core/`)
- `laya_host_response.py` — 선택된 Laya route의 hook 응답(`recall`·`update`·`update-queued`·`gate`·`posttool`·`index`·`compile` 등). 설치 journal이 설치된 플러그인 전체를 증명한 뒤에만 runtime을 열고, 증명이 바뀌면 실패한다(fail-closed). `wiki_card.py`는 선택 경로와 위키 worker가 공유하는 카드 읽기 헬퍼(frontmatter 파싱·source 해석·본문 추출·recall selection log)다.
- `laya_ledger_common.py` — 선택 ledger 공용 primitive(비공개 JSON 읽기·쓰기, ledger lock, 프로젝트 단위 operation lock `laya-qmd-cutover-operation.lock`, multi settings preflight). fresh-wiki ledger·`_select_route`·active/bound/inactive 업그레이드가 이 모듈을 import한다. state root에 QMD-origin ledger(`qmd-cutover.json`)가 남아 있으면 `refuse_qmd_origin`이 `qmd_origin_detach_required`로 거절하고, `_select_route`는 QMD-origin marker를 같은 사유의 `blocked`로 분류한다. lock 파일 이름은 구버전 호스트 사본과의 상호 배제 계약이라 바꾸지 않는다.
- `laya_bundle_upgrade.py` — 호스트 플러그인 업데이트 뒤 laya-db 설치를 실행 중인 번들로 옮기는 자동 업그레이드. 선택 프로젝트의 SessionStart(`laya_host_response`)가 `request`로 상태 파일(`<state>/bundle-auto-upgrade.json`)을 읽고 필요할 때만 detached worker(`run`)를 띄운다. worker는 프로젝트의 route·build pointer가 가리키는 state root만 다룬다: inactive(sealed는 freshness 요청 후)→`laya_inactive_upgrade`, bound→`laya_bound_upgrade`, 선택(fresh-wiki)→`laya_active_upgrade` 후 sync wake. QMD-origin ledger가 남은 state root는 `qmd_origin_detach_required`로 거절한다(재시도로 풀리지 않는 `REFUSALS`). 소스는 호스트 marketplace clone(Claude `~/.claude/plugins/marketplaces/<mkt>`, Codex `~/.codex/.tmp/marketplaces/<mkt>`)뿐이며 HEAD가 호스트 기록(Claude `installed_plugins.json` `gitCommitSha`, Codex 캐시 clone HEAD)의 설치 커밋과 같고 추적 파일이 깨끗할 때만 쓴다. provider review는 다시 돌리고 이전 attestation과 번들 파일 경로(`providerPath`·`workerPath`)만 다를 때 자동 수락한다. 모델·런타임·closure·provider 바이트가 바뀌면 `provider_changed_rebuild_required`로 거절하고 안내만 남긴다. 설치 커밋이 바인딩 커밋의 조상이면 `older_host_bundle`로 거절한다(호스트 간 왕복 방지). 같은 커밋의 다른 호스트 캐시는 route를 바꾸지 않고 `seal_companion_host_copy`로 봉인한다.
- `laya_bound_upgrade.py` — bound 빌드(`prepared` fresh-wiki 선택 ledger)의 번들 교체. route·attestation·journal pending과 이를 가리키는 ledger·marker·build record 다이제스트만 바꾸고 세대·벡터 프로필·저장소는 그대로 둔다. 전환 중에는 selector가 `blocked`로 읽고, 중단되면 intent로 같은 동작이 이어서 끝낸다.
- `laya_active_upgrade.py` — 선택된 laya-db의 번들 교체. fresh-wiki(`fresh-wiki-cutover.json`) ledger를 intent·fence(`upgrading`)·backup 경로로 처리한다. `origin: qmd` intent는 `qmd_origin_detach_required`로 거절한다. 이전 번들 캐시가 이미 지워졌으면 전진만 허용하고 이전 번들 복원은 거절한다.
- 번들 불일치 중 읽기 — 전체 증명이 번들 정체성(`laya_install_journal.BUNDLE_DRIFT_CODES`)에서만 실패하면 `verify_selected_route_read_only`가 route·journal·저장소 incarnation·봉인된 attestation만 다시 증명하고, recall은 마지막 게시 세대를 읽기 전용으로 제공한다(warm owner가 없으면 lexical만). sync·compile·index wake는 기록하지 않고, SessionStart는 실제 사유를 담은 한 줄 안내를 낸다. hook stderr는 `JournalEvidenceError` 코드를 그대로 보고한다.
- `posttool.py` — 편집 hook payload helper(편집 경로·apply_patch 추가 텍스트 추출, `is_story_path`는 config의 `collectionPaths`로 판별). hook entrypoint가 아니고 `wiki_compile_enqueue`·`wiki_topical_reconcile`·`edit_context`가 import한다.
- `config.py` — `.auto-context/settings.json` 로드 + 기본값 병합. 숫자 필드(minScore/topN/queryTimeout) 보수적 coercion, 실패 시 기본값. legacy novel 컬렉션명(`*-manuscript`/`*-plot`)은 `lexicalPatterns:["ep"]` 자동 활성화. 키가 `name`·`skipPaths`뿐인 canonical `.auto-context/settings.json`(minimal settings)은 `expand_minimal_settings`가 legacy 모양(raw `"."` + wiki `.auto-context/wiki`, collection 이름은 `name`의 slug와 slug-wiki, `wikiOnly`, host builtin auto-wiki compile, contextLearning 켜짐, `indexing:true`)으로 펼친 뒤 정규화하고 `settingsModel:"minimal"`을 붙인다. 다른 키가 하나라도 있는 legacy 모양 파일은 펼치지 않고 그대로 정규화한다(온보딩은 이를 `minimal_settings_required`로 거절한다). `load_project_config_raw`와 laya route·origin plan·cutover preflight의 raw 검사도 같은 펼침을 쓴다.
- `resolve_paths.py` — collectionPaths→경로 매핑 + risky path / allowRoots traversal 검증.
- `collection_match.py` — 편집 경로 → collectionPaths longest-prefix 컬렉션 선정. 복수 컬렉션 지원, 컬렉션 밖 편집은 빈 결과.
- `wiki_compile_defaults.py` — compile 설정 블록 생성 헬퍼(제품 코드 호출부는 없고 테스트가 쓴다). generated config에는 절대 adapter 경로를 쓰지 않고 `compile.extractor.builtins`에 `claude`/`codex`/`hermes` 같은 symbolic engine만 저장한다.
- `wiki_compile_enqueue.py` / `wiki_compile_worker.py` — 자동 compile source는 `raw`/`session` Markdown만 허용하며, relative path의 어느 segment라도 `.`로 시작하면 skip/reject한다. `.agents/skills`, `.claude`, `.codex`, `.github`, `.auto-context` 같은 agent/tooling metadata를 wiki로 승격하지 않기 위한 기본 안전장치다.
- `wiki_lifecycle.py` — wiki 수명 주기. 선택된 route의 SessionStart·프로젝트 안 편집·성공한 agent Bash가 `wiki_lifecycle_request`로 프로젝트당 요청을 남기고 `laya-compile` wake를 offer하면, compile pass(`wiki_selected_laya.run`)가 이 모듈을 시간 제한 자식으로 먼저 돌린 뒤 같은 pass에서 재생성 행을 compile한다(대기 요청이 있으면 묶음 처리, 실패해도 hook 응답 불변, 공유 UID 큐의 구버전 worker 때문에 전용 wake kind는 두지 않는다). plugin 소유 카드(`createdBy: qmd-auto-context` + auto 블록)만 기록된 `sourceRevisions`로 판정한다: 원문 없음·`sourceRevisions` 없음 → `retired-wiki/`에 보관 후 삭제하고 `laya-sync`를 깨운다, 해시 변경 → compile source 큐에 재생성(legacy settings는 `session_start_source` 동의 필요, minimal settings는 불필요), 같음 → 유지. 원문 소실은 git이 HEAD 계보의 삭제로 증명할 때만 조치하고, 다른 브랜치에만 있는 원문·연결 워크트리·rebase 등 진행 중은 보류한다(`git_branch_safety`). 선택된 laya-db route에서만 조치하고(route가 없으면 `ineligible`만 센다), 순환 커서 창·run당 퇴역 상한·대량 삭제 가드(원문 소실 카드 비율)·재생성 재시도 간격으로 유계·멱등이다. 심볼릭 링크·dot 경로·wiki root 밖은 건드리지 않는다.
- `extractors/` — host-CLI wiki extractor adapters (`claude_adapter.py`, `codex_adapter.py`, `hermes_adapter.py`) + `lib.py`. Pure `payload→{candidates}` functions run in an isolated temp cwd with tools/custom context disabled. Worker는 `compile.extractor.argv`(legacy explicit) → `compile.extractor.backends[engine]`(explicit) → `compile.extractor.builtins`(runtime-resolved adapter) → `extractor.default` 순서로 선택한다. minimal settings는 host builtin auto-wiki compile을 켠 모양으로 펼쳐진다(`config.expand_minimal_settings`). Exit 127 = host CLI absent (worker then tries `extractor.default`). 격리: `lib.run_isolated`가 자식 env에 `QMD_SANDBOX=1`을 넣어 nested CLI의 qmd 훅을 즉시 무력화(재귀 차단)하고, claude는 `--safe-mode`·`--tools ""`·`--no-session-persistence`, codex는 `--ephemeral`·`--ignore-user-config`·`--ignore-rules`, hermes는 `--safe-mode`·`--ignore-user-config`·`--ignore-rules`·`-t ""`로 MCP/skills/plugins/rules/tool context를 최소화한다(hermes는 동등 persistence-off 플래그 없어 ~/.hermes에 1회성 기록이 남을 수 있음).

### skills (`skills/`)
- `query` — 프롬프트와 같은 Laya recall hook을 선택된 route에 수동 실행한다(선택된 main checkout의 연결 워크트리는 읽기 전용). 미온보딩 프로젝트는 온보딩 안내와 함께 exit 1, 증명이 실패한 선택 route는 exit 3이다. 다른 경로로 검색하지 않는다.
- `update` — 선택된 Laya 색인의 full-scan sync wake를 큐잉한다(생성·변경·삭제 파일 reconcile, 수동 sync·resync 요청도 이 skill). wake는 queued로 보고하고 실행은 hook 밖 durable worker가 한다. 미온보딩 프로젝트는 exit 1, 증명 실패는 exit 3이다.
- `setup` — Laya 색인 온보딩(minimal settings → `plan-fresh-wiki-origin` → `review-index-provider` → `prepare-fresh-wiki-origin` → `finalize-index-origin` → `activate-index-origin`, 되돌리기는 `restore-index-origin`), Laya embed runtime 준비·GC, 명시적 학습 프로필, owner의 은퇴 QMD 백엔드 1회 정리 진입점. 모든 동작은 `core/setup_entry.py`로 하고 적용은 명시 요청 시에만 한다. SessionStart 온보딩 안내가 가리키는 skill이다.
- `wiki-compile` — compact durable summary/candidate JSON을 `core/wiki_extract.py` → `core/wiki_compile.py`로 수동 실행한다. raw transcript를 입력하지 않으며 query-time hook에서는 쓰지 않는다.
- `wiki-dedup` — `dedup-needed.jsonl`에 남아 있는 이미 존재하는 wiki 카드 쌍(그 큐를 채우던 retroactive scan은 없다)을 `agents/wiki-dedup-resolver.md` 에이전트로 정리한다. write-time `merge-needed.jsonl`은 passive diagnostic이라 이 workflow 밖이다.
- `hint`에 해당하는 skill은 만들지 않는다. 선택된 route의 PostToolUse `posttool`은 힌트를 주입하지 않는 observer이고 durable sync는 `index`가 맡는다.
- `gate`에 해당하는 skill은 만들지 않는다. gate는 선택된 route에서 편집 이벤트의 route 증명을 확인하는 내부 hook이고, 증명이 유지되면 allow를 낸다.

> **dogfooding**: 이 저장소 자체의 `.auto-context/settings.json`은 minimal settings(`{"name": "qmd-auto-context", "skipPaths": []}`)다. 온보딩해 Laya route가 선택된 체크아웃에서는 raw 컬렉션 `qmd-auto-context`(프로젝트 루트)와 wiki 컬렉션 `qmd-auto-context-wiki`(`.auto-context/wiki`)를 쓴다.

### hooks (`hooks/`) — 유일한 훅 진입점
> Claude/Codex/Gemini의 훅 진입점은 `hooks/run-hook` 단일 디스패처다. Hermes Agent는 별도 host protocol이므로 Python plugin adapter(`plugin.yaml`, `__init__.py`, `hermes_adapter/`)가 같은 core 스크립트를 호출한다. **도메인 로직은 여전히 core/가 SSOT**다.

- `run-hook` — 공통 디스패처(bash). 호출: `run-hook <action> <engine>` (engine: claude|codex|gemini). `hooks.json`·`hooks-codex.json`이 등록한 action은 `update-queued`·`topical-reconcile`(SessionStart), `recall`(UserPromptSubmit), `gate`(PreToolUse), `posttool`·`index`·`compile`·`topical-event`(PostToolUse 편집 도구), `topical-stop`(Stop), Claude 전용 `file-change-event`(PostToolUse Bash)다. 처리 순서: sandbox/headless 가드 → 모든 action을 `core/laya_index_host_route.py classify-hook`으로 분류 → `candidate|preview|worktree`면 `core/laya_host_response.py serve-stdin`으로 exec, `blocked`(또는 분류 실패)면 "QMD fallback refused"로 exit 3, `legacy`면 무작업 exit 0. `legacy`의 `update-queued`만 `core/context_learning_host_ack.py`를 돌리고 온보딩 안내 한 줄(`onboarding-notice`)을 출력한다. 플러그인 루트는 env `CLAUDE_PLUGIN_ROOT`/`PLUGIN_ROOT`가 우선이고 없으면 `dirname "$0"`이다. **도메인 로직은 core/가 SSOT.**
- `hooks.json` — Claude hooks (`${CLAUDE_PLUGIN_ROOT}`). `hooks-codex.json` — Codex hooks도 **`${CLAUDE_PLUGIN_ROOT}`**를 쓴다(형태: `"${CLAUDE_PLUGIN_ROOT}/hooks/run-hook" <action> codex`). Codex가 신뢰성 있게 주입하는 plugin-root 변수가 CLAUDE_PLUGIN_ROOT이고, 로컬 캐시의 OpenAI codex·zax·superpowers 등 **모든 codex 플러그인이 codex hooks에서 이 형태를 쓴다**(shell 확장이든 텍스트 템플릿 치환이든 양쪽 메커니즘에서 입증된 유일한 형태). `${PLUGIN_ROOT}`(codex 바이너리가 인식하는 alias지만 간헐적으로만 주입)를 단독으로 쓰면 비었을 때 command가 `/hooks/run-hook`으로 해석돼 **exit 127**(command not found)이 난다 — 실패 지점이 command 확장 단계라 run-hook 내부 dirname fallback으로는 못 막는다. **novel한 `${VAR:-fallback}` 파라미터 확장이나 `;`/`&&` 제어 연산자는 쓰지 않는다**: 어떤 codex 플러그인도 안 쓰는 문법이라, codex가 shell이 아니라 템플릿 치환을 하면 매칭/해석에 실패할 수 있다. run-hook 내부는 여전히 `CLAUDE_PLUGIN_ROOT→PLUGIN_ROOT→dirname` 순서로 방어한다(도달만 하면). CLAUDE_PLUGIN_ROOT 자체가 비면 여전히 127인데, 이는 codex-level 상태로 OpenAI 공식 포함 모든 codex 플러그인에 동일하게 영향한다(우리만의 문제 아님). PreToolUse `gate`도 이 경우 127로 죽어 deny를 못 내므로 pending 편집 차단이 fail-open된다(수용 가능한 트레이드오프). Codex 공식 문서상 same-event command hooks는 동시 실행될 수 있으므로 posttool/index/compile은 서로 순서 의존하면 안 된다.
- Antigravity(agy)는 공식 PostToolUse payload에 edited file path/tool input이 문서화되어 있지 않고 stdout contract도 `{}`라서, AGY 자동 hook은 설치하지 않는다. AGY 자동 posttool/index/compile은 전용 payload adapter가 생긴 뒤 재활성화한다.
- `hooks.json`은 **표준 구조** `{hooks:[{type:"command",command}]}`를 따라야 한다. 비표준 구조면 호스트(Claude/Codex)가 훅을 인식 못 함.
- 매니페스트: `.claude-plugin/plugin.json`, `.codex-plugin/plugin.json`(+`interface`, `hooks` 경로 명시).

### Hermes Agent plugin (`plugin.yaml`, `__init__.py`, `hermes_adapter/`)
- Hermes는 Claude/Codex hook JSON을 읽지 않고 Python plugin system을 쓴다. root `plugin.yaml` + `__init__.py`가 `hermes_adapter.plugin.register(ctx)`를 노출한다.
- Hook mapping: `pre_llm_call`→`recall`, `on_session_start`→`update-queued`, `pre_tool_call`→`gate`, `post_tool_call`→`posttool`+`index`. `hermes_adapter/core_bridge.py`가 route를 분류해 선택된 route에서만 `laya_index_host_route.select_route` → `laya_host_response.serve_selected`로 넘긴다. `legacy`는 무작업이고, 그 밖의 상태는 경고만 남기며 QMD로 폴백하지 않는다(gate는 block).
- Hermes `post_tool_call` 반환값은 observer-only라 같은 턴 모델 컨텍스트에 posttool 힌트를 주입하지 않는다. 따라서 Hermes 경로의 편집 후 동작은 자동 인덱싱 중심이며, Claude/Codex posttool 컨텍스트 주입과 동일하다고 문서화하면 안 된다.

### 설치 / cleanup
Claude·Codex는 marketplace plugin install이 제품 경로다. 제품용 `install.sh`/`uninstall.sh`는 없다. AGY는 공식 PostToolUse payload adapter 전까지 자동 hook을 설치하지 않는다. 새 머신의 Laya embed runtime은 `setup_entry.py provision-index-runtime --approve-index-runtime-download`가 만들고(`core/laya_embed_runtime.py`: PATH 우선 Node 22/24/26 실제 바이너리, `locks/laya-embed-runtime`의 `npm ci --ignore-scripts`, sha256 고정 GGUF), `gc-index-runtime --approve-index-runtime-gc`는 active·previous·install attestation 밖 세대만 지운다.

은퇴한 QMD 백엔드 정리는 owner가 한 번 수동으로 하는 작업이다(`core/legacy_cleanup.py`, hook·worker·업데이트에서는 돌지 않음). Claude·Codex 호스트 캐시가 모두 이 릴리스를 로드하고, 모든 설치가 이 번들로 옮겨지고 Laya embed runtime으로 재증명된 뒤 `plan-legacy-cleanup`(read-only) → `apply-legacy-cleanup --approve-legacy-cleanup` 순으로 실행한다. 명령줄이 정확히 검증된 plugin QMD 데몬에만 SIGTERM을 보내고(SIGKILL·패턴 검색 없음), `managed-by: qmd-auto-context` 마커가 있는 LaunchAgent만 bootout·삭제하고, `~/Library/Application Support/qmd-auto-context/{runtimes/qmd,shared-qmd,backend}`를 지운다. `~/.cache/qmd`·`~/.config/qmd`(사용자 자신의 QMD 데이터 포함)는 `--include-user-qmd-data`일 때만 지운다. 대상 안 파일을 가리키는 attestation(`attestation_references_legacy_path`), `core/backend_manager.sh`가 있는 구 번들을 가리키는 `reviewed-route.json`(`older_host_bundle_bound`), 심볼릭 링크 대상(`legacy_cleanup_target_symlink`)이 있으면 거절한다. 상세는 `CLAUDE.md`의 같은 절.

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
- `test/plugin-manifest.test.mjs` — manifest의 `description`·`version`이 비어 있지 않은지만 단정한다(정확한 버전 일치는 검사하지 않으므로 위 목록을 손으로 맞춘다)

`docs/plans/` 문서 안의 버전 문자열은 bump 대상이 아니다. 버전 변경 후 최소 `npm run test:focused -- test/plugin-manifest.test.mjs`를 실행하고, 릴리스 전에는 `npm test`를 실행한다.

### 설정 해석 우선순위
`config.load_project_config`는 cwd에서 HOME 경계까지 위로 올라가며 `.auto-context/settings.json`을 찾고, **없으면 빈 설정(`collections=[]`)을 반환**한다(`indexing:false`도 동일). cwd 폴더명을 컬렉션으로 삼는 fallback은 없다. settings가 minimal(`name`·`skipPaths`뿐)이면 indexing은 켜짐이다. 설정 파일만으로는 아무 작업도 하지 않는다 — hook·skill 작업은 setup 온보딩으로 Laya route가 선택된 프로젝트에서만 돈다.

## 운영상 함정 (디버깅 시 참고)

- **빈 출력은 정상 동작일 수 있다.** 미온보딩 프로젝트(`legacy` route)·sandbox·결과 0건은 모두 의도적으로 무출력 종료한다. 빈 출력 ≠ 버그. 선택된 route의 recall은 `QMD_RECALL_LOG=<파일>`이 있으면 `qmd_recall_selection` 줄(`route: "laya"`, `reason`은 `selected`/`no_results_after_filter`, `selected[]`는 실제 주입한 문서)을 파일에만 남긴다. **모든 줄에 `project_root` 귀속이 붙는다**(`QMD_RECALL_LOG`는 전역 파일 하나다). 이 로그는 stdout(모델 컨텍스트)엔 나가지 않고 env가 없으면 no-op다.

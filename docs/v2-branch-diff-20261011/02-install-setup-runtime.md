# 설치·setup·QMD 런타임 관리 — main(0f277e2) ↔ feat/v2-pre-release-validation(92ecd89)

## 1. 한 줄 요약

플러그인은 이제 QMD 2.5.3을 **사용자 전역 관리형 런타임**(`~/Library/Application Support/qmd-auto-context/runtimes/qmd`)에 lock 고정으로 설치·채택할 수 있다. 다만 어떤 hook도 이를 실행하지 않고, `setup` skill → `core/setup_entry.py` → `core/install_update.py`의 명시적 inspect/plan/prepare/activate/rollback 단계와 별도 승인 플래그 두 개를 거친다. 동시에 `core/setup_guard.py`가 **프로젝트 로컬 v2 인덱스 포인터가 없는 모든 opt-in 프로젝트의 hook·worker·manual skill을 막는다.** 그래서 main에서 동작하던 기존 프로젝트는 업그레이드 직후 setup을 마칠 때까지 무동작이고, setup은 macOS arm64에서만 할 수 있다.

## 2. 새로 생긴 것 / 바뀐 것

| 파일 | 역할 | 핵심 함수 |
|---|---|---|
| `core/qmd_runtime.py` (신규 201줄) | 관리형 QMD 세대 선택. `active.json` 포인터 → `generations/g-<uuid>/` 의 wrapper·`prepared.json` 증명을 매 선택마다 재검증한다 | `managed_root` :34, `_probe` :56, `prepare_existing` :112, `activate` :147, `select` :169, `rollback` :184 |
| `core/qmd_installer.py` (신규 149줄) | 체크인된 lock으로 `npm ci`를 새 비활성 세대에서 실행한다. 세대를 직접 활성화하지 않는다 | `inspect_lock` :24, `install_new` :89, `_native_probe` :78 |
| `locks/qmd-2.5.3/{package.json,package-lock.json}` | 고정 의존성. `@tobilu/qmd: 2.5.3` 하나, lockfileVersion 3, 289 entry | — |
| `core/qmd_route.py` (신규 124줄) | QMD 실행 파일과 프로젝트 DB/config/cache 튜플을 고르는 단일 계약 | `binary_info` :28, `project_paths` :65, CLI `resolve-bin`/`daemon-spec`/`resolve-env` :103-124 |
| `core/runtime_update.py` (신규 378줄) | 프로젝트 shadow 인덱스 생성·포인터 선택·롤백. 원본 DB는 건드리지 않는다 | `inspect_index` :39, `plan_update` :65, `stage_shadow_index` :94, `activate_shadow_index` :214, `select_runtime` :264, `rollback_shadow_index` :332 |
| `core/install_update.py` (신규 977줄) | 저널 기반 setup 코디네이터(prepare/activate/rollback, 데몬 handoff, legacy settings 복사) | `supported_platform` :58, `inspect` :155, `load_request` :197, `prepare` :566, `activate` :873, `_rollback_under_lock` :807, `rollback` :945 |
| `core/setup_entry.py` (신규 354줄) | 사용자 진입점. 탐색 결과로 strict request JSON을 직접 만든다 | `discover` :48, `_qmd_choice` :83, `check_update` :175, `plan` :202, `prepare` :287 |
| `core/setup_guard.py` (신규 106줄) | 읽기 전용 업그레이드 게이트. `ready`/`setup_required`/`optout` | `status` :20, `hook_project` :69, `main` :83 |
| `core/local_runtime_env.py` (신규 82줄) | 플러그인 루트의 `.qmd-local-runtime.env`를 셸 코드가 아닌 데이터로 파싱한다(허용 키만, PATH·NODE_OPTIONS는 인식만 하고 적용하지 않음) | `parse` :24 |
| `core/qmd_project_query.py` (신규 44줄) | 선택된 프로젝트 인덱스에 `qmd query … --format json --no-rerank` CLI 질의를 보낸다 | `query` :14 |
| `core/qmd_path.sh` | `resolve_qmd_bin`이 `QMD_BIN` 단축 경로와 `command -v qmd`를 버리고 `qmd_route.py resolve-bin`에 위임한다 | :55-58 |
| `backend/daemon.sh` | `daemon-spec`로 entry·고정 Node·package bin을 받는다. 관리형이면 PINNED_NODE만 ABI probe하고, 실패하면 fnm 탐색으로 폴백하지 않고 FATAL로 끝난다. `$HOME/.bun/bin/qmd` 폴백을 제거했다 | :17-31, :56-60 |
| `core/backend_manager.sh` | `identity` 서브커맨드 신설: pid 입양 없이 `pid\tcommand`를 출력한다. rc 0=healthy, 1=부재, 2=PID 없이 health만 응답, 3=PID는 있으나 unhealthy. `kick_wiki_compile`은 `QMD_AUTO_COMPILE_DISABLED`면 즉시 반환한다 | :508-521, :461 |
| `skills/setup/SKILL.md` (신규) | setup 흐름 안내(관리형 설치·shadow 마이그레이션·empty bootstrap·롤백) | — |
| `skills/{query,sync,update,wiki-compile,enable-compile,wiki-source-repair}/scripts/*.sh` | 선두에서 `setup_guard.py check`를 호출하고, 통과하지 못하면 stderr `[qmd] setup required…`와 함께 exit 1 | 각 파일 +4줄 |

## 3. 제어·데이터 흐름

### 3.1 hook 경로 (`hooks/run-hook`)

```
host hook → run-hook
  ├ sandbox/headless → 즉시 exit 0 (topical-stop만 '{}' 출력)        :19-31
  ├ $ROOT/.qmd-local-auto-compile-disabled → QMD_AUTO_COMPILE_DISABLED=1   :42
  ├ $ROOT/.qmd-local-runtime.env → local_runtime_env.py 파싱              :45-55
  │     파싱 실패 → exit 0 (모든 action 무출력)
  ├ action ≠ update-queued:
  │     payload를 임시 파일에 저장 → setup_guard.py hook|hook-root     :65, :71
  │     ready 아님 → exit 0 (무출력)
  └ update-queued(SessionStart) → update_hook_queue.py enqueue          :122
        guard setup_required → _setup_notice() 1회 출력 후 job 미생성
        guard ready → job 생성 → worker가 qmd_route.project_paths로 경로 선택
```

SessionStart는 이제 `update`가 아니라 `update-queued`(timeout 5s)와 `topical-reconcile`(3s)를 호출한다(`hooks/hooks.json`, `hooks/hooks-codex.json` diff). 큐·topical 상세는 다른 영역 소관이다.

### 3.2 QMD 실행 파일 선택 (`qmd_route.binary_info`, `core/qmd_route.py:28-62`)

1. `HOME`의 관리형 `active.json`이 있으면(심볼릭 링크 포함) **반드시** `qmd_runtime.select()`를 통과해야 한다(:34-35, try 없음). 실패하면 예외가 나고 CLI `resolve-bin`은 rc 1로 끝난다(:123-124). PATH의 시스템 qmd로 폴백하지 않는다. 이 검사는 `QMD_BIN` 판정(:36)보다 앞이라 `QMD_BIN`을 지정해도 우회되지 않는다.
2. 관리형이 활성이고 `QMD_BIN`이 없거나 wrapper와 같으면 관리형을 쓴다. `QMD_NODE_BIN` override가 고정 Node와 다르면 `managed_qmd_node_conflict`(:41-43).
3. 관리형이 활성(select 성공)이어도 `QMD_BIN`이 wrapper와 **다르면 `QMD_BIN`이 이긴다**(:36 조건이 거짓 → :51). 비교는 `Path(explicit) == Path(wrapper)`라 resolve 없는 경로 표기 비교다. 같은 파일을 가리키는 심볼릭 링크나 다른 표기여도 `QMD_BIN` 쪽으로 간다. activate 단계의 `_check_qmd_route`(`install_update.py:341-345`)는 이 경우를 `managed_qmd_route_mismatch`로 거부한다.
4. 관리형이 없으면 `QMD_BIN`(절대 경로·실행 가능해야 함), 그다음 `shutil.which('qmd', PATH)`를 쓴다. `qmd_path.sh normalize_qmd_path`가 PATH 앞에 bun/npm-global/volta/homebrew/fnm/nvm 후보 디렉터리를 붙이는 동작은 main과 같다(:4-53).

실측: 이 머신(관리형 미설치, PATH에 qmd 없음)에서 `python3 core/qmd_route.py resolve-bin` → rc=1.

`qmd_runtime.select`는 `_prepared` → `_probe`를 거치고, `_probe`는 매번 다음을 한다(`qmd_runtime.py:56-81`, :131-144): `node --version`·`node qmd --version`·`node qmd --help` subprocess 3회, node·qmd·package.json·wrapper SHA-256. 저장된 증명과 하나라도 다르면 `qmd_runtime_changed`. Node 바이너리를 교체하거나 지우면(버전 매니저 업그레이드 등) 관리형 선택이 fail-closed되고, 1번 규칙 때문에 플러그인 전체가 qmd를 찾지 못한다.

### 3.3 프로젝트 인덱스 선택 (`qmd_route.project_paths`, `runtime_update.select_runtime`)

- 포인터: `<project>/.auto-context/qmd-index-active.json`(schema `qmd-index-pointer-v1`). 선택 결과는 `generation/index.sqlite`, `generation/config`, `generation/cache` 튜플이다(`runtime_update.py:322-323`).
- 포인터가 있으면 상속된 `INDEX_PATH`/`QMD_CONFIG_DIR`/`XDG_CACHE_HOME`보다 항상 우선한다. 손상된 포인터는 전역 DB로 폴백하지 않고 예외를 낸다(`runtime_update.py:282-321`).
- 포인터가 없으면 main과 같은 env/기본값(`~/.cache/qmd/index.sqlite`, `~/.config/qmd`)으로 간다(`qmd_route.py:91-100`).
- 소비자: `core/update.sh:1117-1124`(`resolve-env`, 실패 시 ABORT 후 exit 0), `backend/index_worker.sh:122-133`, `core/recall.py:2150-2185`, `core/dirty_queue.py:36-42`(3컬럼 owner), `core/orphan_reclaim.py:187,214`, `core/update_hook_queue.py:189-190`.
- **선택된 프로젝트는 전역 데몬을 거치지 않는다.** recall은 질의마다 `qmd_project_query.query`로 새 `qmd` 프로세스를 띄우고(`recall.py:2172-2185`, `qmd_project_query.py:28-41`), 이 경로에서는 DF 좁히기 프로브와 shadow query를 끈다(`lex_df="project_local_cli"`, `shadow_on=False`, :2174-2175). guard가 opt-in 프로젝트에 포인터를 요구하므로(§3.4), 정상 v2 운영에서 recall이 데몬 경로를 타는 프로젝트는 사실상 없다(추정 — guard 우회 fixture를 제외한 모든 ready opt-in 프로젝트는 포인터를 가진다). 그런데도 run-hook은 recall마다 `backend_manager ensure`를 백그라운드로 호출한다.

### 3.4 setup_guard 판정 (`core/setup_guard.py:20-66`)

| 조건 | 결과 |
|---|---|
| env `QMD_SETUP_GUARD_FIXTURE=1` | `ready` (테스트 전용, :21-22) |
| 로컬 optout marker | `optout` |
| 가장 가까운 설정 파일이 존재하지만 `find_project_config`가 그것을 고르지 못함(파싱 불가·심볼릭 링크) | `setup_required/unreadable_project_config` (:31-39) |
| `.auto-context`·settings·pointer·journal 중 심볼릭 링크 | `unsafe_setup_state` |
| 레거시 `.auto-context.json` / `.agents/qmd-recall.json` | `legacy_config` |
| `.auto-context/settings.json` 있음 + 포인터 없음 | `project_index_not_selected` |
| 포인터 64KiB 초과 또는 `schema ≠ qmd-index-pointer-v1` | `invalid_project_pointer` (schema만 본다. 상세 검증은 소비자의 `select_runtime`이 맡는다) |
| settings 파일이 있으나 유효하지 않음 | `invalid_modern_settings` |
| journal 있음 + phase ∉ {activated, rolled_back} | `setup_transition_pending` |
| 설정 없음 | `ready` (이후 recall은 `no_collections`) |

막는 대상은 Claude/Codex/Gemini run-hook의 recall·posttool·index·compile·gate·update·topical-*, manual skill 6개, Hermes 4개 hook(`hermes_adapter/core_bridge.py:72-84`, 호출부 :211, :245, :275, :318), 그리고 worker 네 종류다. `wiki_compile_worker.py:1027-1028`, `sync.py:378-380`, `context_learning_auto.py:61,113`, `topical_hook_queue.py:152,299`, `update_hook_queue.py:183-187`은 job을 보존한 채 `setup_required`를 기록한다. `index_worker.sh:122-125`는 claim을 보존하고 exit 1한다.

실측: 이 저장소 자체(모던 settings, `indexing:false`)도 `QMD_SETUP_GUARD_FIXTURE`를 비운 상태에서 `status()`가 `{'status':'setup_required','reason':'project_index_not_selected'}`를 돌려주고 CLI `setup_guard.py check`는 stdout 없이 rc 3으로 끝난다(`check`는 판정을 출력하지 않는다, :85-86, :99). 임시 HOME 격리 실측에서 모던 settings만 있는 프로젝트는 같은 결과, 레거시 `.auto-context.json`은 `legacy_config`(rc 3), 설정 없는 디렉터리는 `ready`(rc 0)다.

### 3.5 setup 흐름 (`setup_entry.py` → `install_update.py`)

```
inspect (읽기 전용)     : 플랫폼·config 종류·legacyWiki·전역/관리형 QMD·Laya·프로젝트 포인터·node/npm 후보
check-update (읽기 전용): reasons[] → up_to_date | update_review_required             :175-199
plan (읽기 전용)        : request 생성 + blockers, migrationScope                     :202-284
prepare                 : .auto-context/generated-install-request-<sha>.json(0600, hard link 발행)
                          → install_update.prepare: journal 'preparing' → qmd/laya/index 세대 준비
                          → 'prepared' | 'awaiting_legacy_review' | 'failed_preparing'  :566-622
activate (별도 요청)    : 저널·위키 뮤테이션 락 → 소스·증명 재대조 → baseline 스냅샷
                          → qmd 포인터 → route 확인 → (copy_legacy면 settings 복사)
                          → index 포인터 → laya 포인터 → 데몬 reload + identity 대조
                          → 'activated'                                                  :873-942
                          실패 시 해시 가드 롤백 → rolled_back_after_activation_failure | recovery_required
rollback (별도 요청)    : laya→index→qmd 순으로 baseline 바이트 복원, 복사한 settings는 해시가
                          맞을 때만 삭제, 데몬 reload                                    :807-846, :945-955
```

QMD 선택지(`_qmd_choice` :83-107):
- `auto`: 관리형 활성이면 `active`, 아니면 `reuse`. `reuse`는 `--qmd-entry` 또는 PATH의 `qmd`가 필요하고, 정확히 2.5.3 + 지원 Node + capability 6종을 증명해야 한다. 실패하면 `qmd_reuse_incompatible`.
- `install`: `--npm`과 두 승인 플래그 `--approve-package-execution`, `--approve-lifecycle-scripts`가 모두 필요하다. 없으면 plan blocker `package_execution_approval_required`(:266-267)이고, 설치기 자체도 `qmd_install_execution_not_enabled`/`qmd_native_lifecycle_approval_required`로 다시 막는다(`qmd_installer.py:100-103`).
- PATH에 qmd가 없는 신규 사용자는 `auto`가 `reuse`로 가서 `qmd_entry_required`로 막힌다. 실제로 쓸 수 있는 경로는 `--qmd-mode install`이다.

인덱스 선택지:
- `shadow`(기본): v2 opt-in(`.qmd-topical-v2-project` 0600 marker, `wiki_topical.opted_in` :31-39), wiki collection, **게시된 v2 카드 1장 이상**, 프로젝트 내부 QMD config, 모델 캐시, 기대 모델·차원이 필요하다(`setup_entry.py:232-262`). `stage_shadow_index`는 새 세대에서 `qmd update` → `qmd embed` → 합성 `vsearch` 프로브를 돌리고 차원·벡터 형식·모델 지문을 기록한다(`runtime_update.py:94-192`). 모델 캐시는 복사하지 않고 `cache/qmd/models` 심볼릭 링크로 재사용한다(:124-127).
- `--bootstrap-empty-wiki`: 새 모던 opt-in 프로젝트 전용이다. 레거시 설정·프로젝트 DB·source index·wiki Markdown이 하나라도 있으면 거부한다. 공유 전역 DB는 컬렉션 경로가 이 프로젝트를 덮지 않는다고 증명될 때만 허용한다(`install_update.py:399-428`). 합성 문서 1개로 임베딩 모델을 증명한 뒤 문서·벡터를 지우고 빈 DB를 활성화한다(`runtime_update.py:135-157`). scope 라벨은 `v2_empty_bootstrap`.
- `none`: `runtime_only_no_v2_index`. 포인터를 만들지 않으므로 활성화 뒤에도 guard는 `project_index_not_selected`로 남는다(§3.4 표에서 유도한 결론. 전용 테스트는 확인하지 못함).

v1 → v2 마이그레이션:
- `_legacy_wiki`(`install_update.py:123-140`)는 `.auto-context/wiki/**/*.md` 중 SCHEMA/index/log와 `topical-v2/`를 뺀 카드를 `schemaVersion`으로 분류한다. 없거나 `1`이면 legacy, `2`면 `v2_without_publication_proof`, 그 밖은 unsupported. 이런 카드가 하나라도 있으면 사람이 검토한 `--reviewed-v2-mappings`(owner-private, ≤64KiB, 행마다 `legacyPath`/`legacySha256`/`generationId`/`cardId`)가 있어야 활성화할 수 있다. 원본 카드는 그 자리에 남는다.
- 레거시 설정 파일은 `config: copy_legacy`일 때 바이트·정규화 증명을 거쳐 `.auto-context/settings.json`으로 **복사**된다(원본은 유지, `_copy_legacy_settings` :742-772). SessionStart의 레거시 자동 이동(`config.migrate_config_json`)은 `.install-update.lock`을 공유하고, journal이 있으면 `managed_setup_in_progress`로 이동하지 않는다(`core/config.py:1321-1337`).
- prepare와 activate 사이에는 config 바이트, legacyWiki 목록·해시, source index SQLite 스냅샷 지문을 동결해 두고 다시 대조한다(`_frozen_sources`/`_check_sources` :288-316).

## 4. host별 영향

- **Claude Code / Codex**: 같은 `hooks/run-hook` guard를 탄다. SessionStart `update-queued`가 setup이 필요한 프로젝트에 (project, plugin version, reason) 조합당 한 번 stdout 안내를 낸다(`update_hook_queue.py:37-104`, 문구 :100-102). 나머지 hook은 무출력이다. `skills/setup/SKILL.md`는 두 host 모두에 노출된다.
- **Hermes**: 4개 hook 모두 `_setup_allowed`에서 조기 반환한다. `session_update`는 어떤 경우에도 `update_hook_queue`를 거치지 않고 `update.sh`를 직접 부르며(`core_bridge.py:245-258`, `QMD_SUPPRESS_NOTICE=1`), `_setup_notice`는 `update_hook_queue.py`에만 있다. 따라서 Hermes에서는 setup 안내에 **도달하는 코드 경로가 없다.** `pre_llm_call`은 컨텍스트를 주입할 수 있는 채널이지만 `recall_context`가 guard 실패 시 `None`을 돌려줄 뿐 안내를 싣지 않는다(`core_bridge.py:211`). 채널 부재가 아니라 구현 부재다. Hermes만 쓰는 사용자는 기존 프로젝트가 조용히 무동작이 되는 이유를 hook에서 들을 수 없다.
- **Antigravity(Gemini)**: run-hook의 gemini 엔진 라벨도 같은 guard를 탄다. AGY 자동 hook 설치 자체가 cleanup-only 상태라 실영향은 수동 skill뿐이다.
- **플랫폼**: 관리형 setup은 `sys.platform=='darwin' and machine=='arm64'`만 허용한다(`install_update.py:58-59`). 다른 플랫폼에서는 inspect/prepare/activate가 `unsupported_managed_platform`을 반환하고 아무것도 바꾸지 않는다(:157, :569-571, :875). README와 SKILL.md는 다른 플랫폼에 "전역 qmd 수동 설치 경로"를 안내한다. 그러나 프로젝트 포인터는 `activate_shadow_index`로만 생기고 그 호출은 이 게이트 뒤에만 있다. 따라서 **Linux·Intel Mac에서는 opt-in 프로젝트가 영구히 `setup_required`다**(코드 경로 추론. 해당 플랫폼 실측은 없음). `managed_root`의 경로 자체도 macOS 전용 `~/Library/Application Support`다(`qmd_runtime.py:34-35`).

## 5. 신규·변경 설정 키, env, 파일 경로

**env**

| 이름 | 의미 |
|---|---|
| `QMD_SETUP_GUARD_FIXTURE=1` | guard를 `ready`로 고정한다. `npm test`가 전역으로 켠다(`package.json`). AGENTS.md에는 문서화됐고 CLAUDE.md에는 없다 |
| `QMD_AUTO_COMPILE_DISABLED` | run-hook `compile` action과 `kick_wiki_compile`을 끈다. 플러그인 루트의 `.qmd-local-auto-compile-disabled`가 있으면 켜진다 |
| `QMD_BIN` / `QMD_NODE_BIN` | 의미가 바뀌었다. `QMD_BIN`은 관리형 wrapper와 다르면 관리형보다 우선한다. `QMD_NODE_BIN`은 관리형일 때 고정 Node와 다르면 오류다. 비관리형에서는 `daemon.sh`의 Node 선택에 쓰인다 |
| `QMD_TOPICAL_PROJECT_ROOT` / `QMD_TOPICAL_SANDBOX_ROOT` | topical-* action의 guard 대상 루트를 명시한다(`setup_guard.py:70-72`) |
| `.qmd-local-runtime.env` 허용 키 | `QMD_BIN, QMD_NODE_BIN, INDEX_PATH, QMD_CONFIG_DIR, XDG_CACHE_HOME, QMD_DIRTY_QUEUE, QMD_BACKEND_STATE_DIR, QMD_LOCK_BASE, QMD_CACHE_DIR, QMD_*_LOG, QMD_RECALL_LOG, HF_HUB_OFFLINE, TRANSFORMERS_OFFLINE, QMD_CLEANUP_LEGACY, QMD_DAEMON_PORT(1024-65535), QMD_DAEMON_URL(localhost만)` (`local_runtime_env.py:13-19`). 파일은 owner 소유·단일 링크·≤8KiB여야 한다 |

**파일·경로**

| 경로 | 범위 | 내용 |
|---|---|---|
| `~/Library/Application Support/qmd-auto-context/runtimes/qmd/active.json` | 사용자 전역 | `qmd-managed-runtime-v1` 포인터(`generation`, `previous`) |
| `…/runtimes/qmd/generations/g-<uuid>/{qmd,prepared.json,package/,npm-cache/,.npmrc,install.log}` | 사용자 전역 | 0700 세대. 실측 세대 하나가 약 227MB(`docs/qmd-installer-scope-20261006.md`) |
| `…/runtimes/qmd/activation.lock` | 사용자 전역 | 포인터 교체 flock |
| `<project>/.auto-context/qmd-index-active.json` | 프로젝트 | `qmd-index-pointer-v1` (`previous`에 원본 index 경로·지문) |
| `<project>/.auto-context/qmd-index-generations/g-<uuid>/{index.sqlite,config/index.yml,cache/,prepared.json}` | 프로젝트 | shadow 인덱스 세대 |
| `<project>/.auto-context/.qmd-index-pointer.lock` | 프로젝트 | 인덱스 포인터 flock |
| `<project>/.auto-context/install-update-journal.json` | 프로젝트 | `qmd-install-update-v1` 저널(phase: preparing/failed_preparing/awaiting_legacy_review/prepared/activating/activated/recovery_required/rolled_back) |
| `<project>/.auto-context/.install-update.lock` | 프로젝트 | 저널 flock. SessionStart 레거시 migration과 공유 |
| `<project>/.auto-context/install-update-history/<uuid>.json` | 프로젝트 | 다른 request로 넘어갈 때 보관하는 이전 저널 |
| `<project>/.auto-context/generated-install-request-<sha>.json` | 프로젝트 | setup_entry가 생성한 request(0600) |
| `<project>/.qmd-topical-v2-project` | 프로젝트 | v2 opt-in marker(0600). shadow·bootstrap의 전제 |
| `<update-hook-queue>/setup-notice-<token>.json`, `setup-version-<hash>.json` | 사용자 | setup 안내 1회 억제 marker, 마지막으로 본 플러그인 버전 |
| `$TMPDIR/qmd-source-snapshots-v1-<uid>/job-<token>/snapshot.sqlite` | 사용자 임시 | 원본 DB 지문 계산용 백업. 크래시하면 다음 진입 때까지 남는다(문서 근거, 코드 미확인 — `sqlite_read.py`) |
| `$ROOT/.qmd-local-runtime.env`, `$ROOT/.qmd-local-auto-compile-disabled` | 플러그인 설치 루트 | 로컬 트라이얼 노브 |

**버전 고정**: QMD 정확히 `2.5.3`(`qmd_runtime.py:22`). Node major 22/24/26만 허용하므로 20·홀수·27은 거부된다(:24, :29-31). lock SHA-256 `a31cc40f0cbaa413d0b9988c6d7617daa60bb902379fac7f432e77b5b3f7cf3e`(`qmd_installer.py:20`). 설치기는 lock의 모든 entry가 `https://registry.npmjs.org/` 출처 + `sha512-` integrity인지 검사하고(:42-49), `npm ci --include=optional` 뒤 lock 해시를 다시 대조한다(:135-136). npm cache와 userconfig는 세대 전용이고 레지스트리는 고정이다(:118-125). 여유 공간 512MiB 미만이면 거부한다(:107). 일반 백엔드의 버전 정책은 그대로 `>=2.5.3 <3`이다(`backend_manager.sh check_qmd`). 즉 관리형만 정확히 2.5.3이다. 플러그인 버전은 `0.35.2-dev.20261004`이고 manifest 6곳과 `test/probe-manifest.test.mjs`가 모두 일치한다.

## 6. main 대비 동작 변화

1. **"qmd 자동 설치/업그레이드는 하지 않는다"는 hook 기준으로 여전히 참이다.** 어떤 hook도 `install_update`/`setup_entry`를 import하거나 실행하지 않는다(`install_update.py:2`, `setup_entry.py:4-6`). 그러나 플러그인은 이제 **사용자가 setup skill에서 명시 승인하면 QMD를 관리 설치한다.** `CLAUDE.md:86`의 문장은 바뀌지 않았고 신규 모듈 설명도 없다. `backend_manager.sh:100-104 install_hint`는 여전히 `bun add -g @tobilu/qmd@2.5.3`을 안내한다(문서·힌트 드리프트).
2. **업그레이드하면 기존 프로젝트가 즉시 전면 비활성이 된다.** main에서 opt-in된 프로젝트는 모두 레거시 설정이거나 포인터 없는 모던 설정이므로 guard가 `setup_required`를 낸다. recall 주입·편집 후 인덱싱·compile·gate·SessionStart update·manual skill이 모두 멈춘다. 데이터(설정·DB·wiki·모델)는 보존되고 큐 job·dirty claim은 남아 있다. 복구 경로는 setup skill 하나다.
3. **setup을 완료하려면 v2 wiki가 필요하다.** 기존 데이터가 있는 프로젝트의 shadow 경로는 게시된 v2 카드 1장 이상, v2 opt-in marker, 프로젝트 내부 QMD config를 요구한다. v1 카드가 있으면 사람이 검토한 매핑도 필요하다. v2 카드 생성은 다른 영역(topical wiki)의 산출물에 의존한다. `--index-mode none`은 guard를 풀지 못한다(§3.5).
4. **롤백은 main 동작으로 되돌리지 않는다.** 첫 활성화를 롤백하면 포인터가 삭제되고(`runtime_update.py:357`) guard는 다시 `project_index_not_selected`가 된다(`test/setup-upgrade-guard.test.mjs:84-122`가 고정). main처럼 동작하게 하려면 플러그인 코드 자체를 내려야 한다.
5. **관리형 포인터는 사용자 전역이다.** 한 프로젝트 setup에서 `qmd.mode=install|reuse`로 활성화한 QMD 세대는 같은 사용자의 모든 프로젝트와 전역 데몬의 실행 파일이 된다(`binary_info`는 프로젝트를 보지 않는다). 데몬이 실행 중이고 QMD 세대가 바뀌면(`qmd.mode != active` 그리고 `daemonBefore` 존재) activate가 전역 데몬(:8483)을 reload한다(`install_update.py:915-918`).
6. **관리형 상태에서 qmd 실행 파일 선택 비용이 커진다(추정).** `binary_info` 한 번에 subprocess 3회와 node 바이너리 해시(이 머신의 node 24.14.0은 119MB, 해시 0.07s, `node --version` 0.29s 실측)가 든다. `qmd_project_query`는 질의마다 `_binary()`를 부르고 질의마다 새 `qmd` 프로세스를 띄운다. vec 검색이면 매번 모델을 로드할 가능성이 있다. recall hook timeout이 22초로 새로 명시된 것과 짝을 이루지만 실측 지연은 없다.
7. **데몬 시작이 엄격해졌다.** `resolve_qmd_bin`이 실패하면 `daemon.sh`가 FATAL로 끝난다(이전에는 `~/.bun/bin/qmd` 폴백). 관리형이면 고정 Node 하나만 시도한다.
8. **SessionStart 안내 잡음이 늘어난다.** (a) `indexing:false`인 모던 설정 프로젝트도 포인터가 없으면 `setup_required`라 플러그인 버전마다 한 번 "설치 상태를 검토하라"는 안내를 받는다. `_setup_notice`는 guard reason만 보고 settings 내용은 읽지 않는다. 이 저장소 자체가 첫 사례다(§3.4 실측). (b) guard가 `ready`인 프로젝트도 job을 enqueue한 뒤 `_setup_notice`를 호출하므로(`update_hook_queue.py:135`), 플러그인 버전이 바뀌면 `plugin_version_changed` 안내를 한 번 받는다(:77-78). 이것은 hook을 막지 않는다.
9. **로컬 트라이얼 파일이 잘못되면 조용히 전면 무동작한다.** `.qmd-local-runtime.env` 파싱에 실패하면 run-hook이 모든 action에서 `exit 0` 무출력으로 끝나고 로그도 남기지 않는다(`run-hook:45-55`).

## 7. 테스트 커버리지

| 테스트 | 고정하는 것 | CI(ubuntu) 실행 여부 |
|---|---|---|
| `test/setup-upgrade-guard.test.mjs` (8 test) | guard를 비운 상태에서 두 host × 6 action 무출력·레거시 바이트 보존·manual skill exit 1, SessionStart 1회 안내, worker job 보존(`setup_required` status), 활성화 후 재개, 롤백·optout 재차단, 버전 변경 안내, unreadable 설정, prepared journal·잘못된 포인터 안내 | 실행 |
| `test/install-update-coordinator.test.mjs` → `fixtures/install-update-coordinator-checks.py` (1153줄) | 신규 install CLI·기존 reuse·활성화 전 비활성·레거시 설정 보존·원본 DB/학습 데이터 보존·중단 재시도·활성화 실패 롤백·v1 보류·반복 prepare/activate/rollback·Laya·구버전 QMD 거부·WAL 드리프트 fail-closed·미지원 플랫폼 무변경 등(:1141-1153). npm은 주입 runner이고 외부 호출 0 | 실행. 400fd06 이후 플랫폼 게이트는 wrapper로 우회해 Linux에서도 lock 경로를 검사한다 |
| `test/qmd-installer.test.mjs` | lock 검증(entry >100, integrity 고정값), 두 승인 게이트, 비활성 세대, Node major 정책(20/23/25/27/invalid 거부, 22/24/26 허용) | Node가 있으면 실행. 가짜 runner라 실제 `npm ci`는 돌지 않는다 |
| `test/qmd-managed-runtime.test.mjs` | 기존 전용 QMD 채택, wrapper 변조 fail-closed | `/Users/dulee/work/.qmd-tools/bin/qmd`를 하드코딩해 CI에서 skip. **현재 로컬에서도 경로가 없어 skip** |
| `test/qmd-pointer-real-cli.test.mjs` | 실제 QMD 2.5.3 typed query JSON | 같은 하드코딩 경로와 `laya-search-experiments` 스냅샷이 필요해 CI·현재 로컬 모두 skip |
| `test/qmd-pointer-e2e.test.mjs` | 선택된 인덱스로 recall·update, 외부 컬렉션 라우팅, 손상 포인터 fail-closed, 원본 DB 바이트 보존, 롤백(가짜 qmd) | 실행 |
| `test/runtime-update.test.mjs` | `plan_update` 분기, shadow 빌드 실패 시 포인터 미생성, 원본 DB 보존, 활성화 | 실행 |
| `test/review3-runtime-routing.test.mjs` | F1 관리형 데몬 entry·package native probe, F2 publisher 선택 튜플·충돌 가드, F3 검색 캡처 지문, F4 패배한 child job drain | 실행 |
| `test/qmd-lex-probe-routing.test.mjs` | 선택된 DB의 lex 게이트가 데몬이 아닌 그 DB로 가고 CLI 오류 시 fail-open | 실행 |
| `test/empty-bootstrap.test.mjs` (2 test) | 신규 plan, 기존 config/DB/wiki/source/custom·default cache DB/전역 컬렉션 차단, 합성 프로브 제거, 첫 모델 결속, 중단 재개, 늦게 생긴 DB 거부, 롤백 | 실행 |
| `test/manual-skills.test.mjs` | `setup` skill 존재·메타데이터·`inspect…prepare…activate…rollback` 언급 | 실행 |

`npm test`는 `QMD_SETUP_GUARD_FIXTURE=1`이므로 **기존 스위트 전체가 guard를 우회한 채 돈다.** guard의 실제 경로를 검증하는 것은 guard를 비우는 테스트(`setup-upgrade-guard`, empty-bootstrap fixture 2개, `wiki-topical-stop-batch-e2e.py`, `wiki_topical_reconcile_cases.py`)뿐이다.

CI 매트릭스: Node `[20, 22]` → `[22, 24, 26]`이고 `runs-on: ubuntu-latest` 단일이다(`.github/workflows/test.yml`). macOS runner는 없으므로 관리형 setup의 유일한 지원 플랫폼(darwin arm64)은 CI가 돌지 않는다. `package.json`에 `engines.node: ^22 || ^24 || ^26`이 추가됐다.

## 8. 위험·미해결·TODO

1. **비지원 플랫폼 영구 차단(코드 추론).** Linux와 Intel Mac에서는 포인터를 만들 경로가 없어 opt-in 프로젝트가 영구 `setup_required`다. `activate_shadow_index`의 호출부는 `install_update.py:796` 하나이고 `activate`의 플랫폼 게이트(:875) 뒤에 있다. `runtime_update.py`의 CLI는 `resolve-env`만 받는다(:366-371). guard를 푸는 다른 수단은 테스트 전용 env `QMD_SETUP_GUARD_FIXTURE=1`뿐이다. README와 SKILL.md의 "전역 qmd 수동 경로" 안내와 모순된다. 테스트는 미지원 플랫폼에서 "무변경"만 확인하고 그 뒤 hook이 동작하는지는 확인하지 않는다.
2. **Hermes 무안내 비활성.** guard에서 막히면 setup 안내가 한 번도 나가지 않는다(§4).
3. **관리형 포인터 fail-closed의 전역 파급.** Node를 업그레이드하거나 지우거나 qmd package를 바꾸면 `select()`가 실패하고, `binary_info`가 PATH 폴백 없이 예외를 낸다. 그러면 모든 프로젝트의 update·index·데몬이 멈춘다. `QMD_BIN`을 지정해도 select가 먼저 실패하므로 우회되지 않는다(§3.2-1). 복구 수단은 `qmd_runtime.rollback`인데 이 함수를 호출하는 곳은 없다(grep 결과 0건, `setup_entry.py:346`의 rollback은 `install_update.rollback`이다). 사용자에게 노출되는 복구 명령도 없다.
4. **세대 정리 수단 없음.** prepare/롤백이 남긴 QMD 세대(약 227MB씩)와 프로젝트 shadow 세대는 지워지지 않는다. 문서도 "explicit cleanup is separate"라고만 적는다. 크래시 후 남는 원본 DB 스냅샷 백업도 다음 진입 때까지 남는다(문서 근거).
5. **실제 `npm ci` 경로는 스위트 밖이다.** 문서는 1회 수동 승인 설치(Node 24.21.0, 세대 `g-dde1d942…`)를 기록하지만, 코디네이터를 거친 신규 설치는 "untested"로 명시한다(`install-update-writer-error-audit-20261006.md` 말미). lifecycle 스크립트의 네트워크 상한도 증명되지 않았다(locked tarball 상한 731MiB, darwin-arm64 기대치 48.9MiB, `qmd-installer-scope-20261006.md`).
6. **Node 26 문서 불일치.** `docs/install-update-contract-20261006.md`는 "Node 22.23.3과 26.10.0이 격리 native 프로브를 통과"했다고 쓰고, README와 `qmd-installer-scope`는 "Node 26 native 호환은 아직 실제 프로브가 필요"하다고 쓴다.
7. **문서 드리프트.** CLAUDE.md에 신규 모듈·`QMD_SETUP_GUARD_FIXTURE`·`QMD_BIN` 의미 변화가 없다(AGENTS.md에만 env 한 줄). `install_hint`는 전역 bun/npm 설치를 계속 안내한다.
8. **`index_worker.sh` head-of-line 차단(코드 확인 + 격리 실측).** 배치(`sort -u` 결과라 위치 무관) 안의 `setup_required` entry 하나가 qmd 호출 전에 `exit 1`로 claim 전체를 보존한다(`backend/index_worker.sh:117-125`). 다음 run은 같은 claim prefix를 재사용해(`core/dirty_queue_claim.py:104-108`) 같은 지점에서 멈추고, 그 뒤 enqueue된 라인은 배치에 들어오지 못한다. 건너뛰거나 드롭하는 분기는 없다. 상세와 실측은 영역 01 §8-3. 의도인지는 확인하지 못했다.
9. **hook 지연 증가(추정, 측정 필요).** §6-6. 관리형 + 선택 프로젝트 조합에서 recall 질의마다 `binary_info` 재검증과 qmd 프로세스 기동이 붙는다.
10. **guard의 포인터 검사는 schema 필드만 본다.** 포인터가 손상돼도 guard는 `ready`를 주고(격리 실측: `{"schema":"qmd-index-pointer-v1"}`만 담은 포인터 → guard `ready`, `qmd_route.project_paths` → `ValueError('unsafe_index_pointer')`), 각 소비자가 `select_runtime` 예외로 fail-closed한다. recall은 `project_index_invalid`, update.sh는 `ABORT`, index_worker는 claim 보존이다. 이때 사용자 안내는 SessionStart `_setup_notice`의 reason 계산에만 의존한다(`update_hook_queue.py:55-64`는 guard가 `ready`면 포인터 내용을 다시 검증하지 않는다. 다만 `setup-upgrade-guard`의 "invalid index pointer" 테스트는 schema 불일치 케이스만 다룬다).

## 다른 영역과의 인터페이스

- `update-queued` / `core/update_hook_queue.py` worker 생명주기와 `_setup_notice`(SessionStart 영역)
- `hooks/hooks*.json`의 timeout 신설과 topical-*·Stop action, `core/topical_hook_queue.py`(topical wiki 영역)
- `wiki_topical.opted_in` marker와 게시된 v2 카드·publication proof(`install_update._reviewed_wiki`, `_wiki_corpus`)(topical wiki 영역)
- `core/dirty_queue.py`의 3컬럼 owner 필드, `backend/index_worker.sh` claim/ack 재설계(인덱싱 영역)
- `core/recall.py`의 project-local CLI 분기(DF 프로브·shadow 비활성)(recall 영역)
- `core/sqlite_read.py`(`snapshot_fingerprint`, `cutover_guard`, 읽기 전용 연결)
- `wiki_mutation_lock`(activate가 `.topical-publish.lock`을 잡는다), Laya `context_learning/runtime_installer`·`runtime_setup`(학습 영역)

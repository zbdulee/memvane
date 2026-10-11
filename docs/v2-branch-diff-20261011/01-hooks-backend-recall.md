# hook·백엔드·큐·recall 경로: main(0f277e2) ↔ feat/v2-pre-release-validation(92ecd89)

## 1. 한 줄 요약

모든 host hook 앞에 `setup_guard`가 붙어 **프로젝트 index pointer(`.auto-context/qmd-index-active.json`)가 없는 기존 프로젝트는 recall·편집 hook이 조용히 멈추고**, pointer가 있는 프로젝트는 전역 데몬 대신 프로젝트별 shadow DB에 `qmd query` CLI로 질의한다. recall/posttool에는 18초 절대 deadline이 생겼고, SessionStart update는 durable job + detached worker로 비동기화됐으며(그 결과 기존 SessionStart 알림이 host 턴에 닿지 않는다), Stop hook과 topical 작업 큐가 추가됐다. dirty queue는 truncate+requeue 방식에서 claim/ack 방식으로 바뀌었다.

## 2. 무엇이 새로 생겼나 / 바뀌었나

### 2.1 hook manifest (`hooks/hooks.json`, `hooks/hooks-codex.json`)
- SessionStart: `update` 1개 → `update-queued`(timeout 5) + `topical-reconcile`(timeout 3) (`hooks/hooks.json:3-7`, `hooks/hooks-codex.json:3-7`).
- UserPromptSubmit `recall` timeout 22 (`hooks/hooks.json:9`).
- PreToolUse `gate` timeout 3. PostToolUse: `posttool` 22, `index` 5, `compile` 5에 **`topical-event`(5) 추가** (`hooks/hooks.json:20-24`).
- **새 이벤트 `Stop` → `topical-stop`**(timeout 3) (`hooks/hooks.json:27-29`, `hooks/hooks-codex.json:27-29`).
- matcher는 그대로다(Claude `Edit|Write|MultiEdit|NotebookEdit`, Codex `apply_patch|Edit|Write`).
- Codex의 `timeout` 필드 지원은 docs 주장이다(`docs/hook-budget-and-recovery-20261006.md:3-9`) (문서 근거, 코드 미확인).

### 2.2 디스패처 `hooks/run-hook`
- action 추가: `update-queued`, `topical-event`, `topical-stop`, `topical-reconcile` (`hooks/run-hook:4-5`).
- sandbox/headless 가드를 플러그인 루트 해석보다 **앞**으로 옮겼다. `topical-stop`은 가드에서도 `{}`를 출력한다(Stop은 JSON 출력 필요) (`hooks/run-hook:19-31`).
- `$ROOT/.qmd-local-auto-compile-disabled` 파일이 있으면 `QMD_AUTO_COMPILE_DISABLED=1` export (`hooks/run-hook:42`). `compile` action, `backend_manager.sh kick_wiki_compile`, `update.sh`의 SessionStart flush가 이 값을 보고 건너뛴다 (`hooks/run-hook:157`, `core/backend_manager.sh:461`, `core/update.sh:1421`).
- `$ROOT/.qmd-local-runtime.env`가 있으면 `core/local_runtime_env.py`로 검증한 `KEY=VALUE`만 export한다(셸 코드 아님) (`hooks/run-hook:45-54`).
- **`update-queued` 외 모든 action은 stdin을 임시 파일에 받아 `setup_guard.py`를 먼저 통과해야 한다.** topical 3종은 `hook-root`(opt-in 마커까지 확인하고 root 출력), 나머지(recall/posttool/index/compile/gate/update)는 `hook`. 통과 못 하면 exit 0, 무출력 (`hooks/run-hook:58-79`).
- topical action은 일반 로그 설정(`QMD_RECALL_LOG` 기본값) 이전에 처리하고 종료한다: `topical-event` → `wiki_topical_reconcile.py event`(프로젝트에 `.topical-reconcile-hook.json`이 있을 때만), `topical-stop`/`topical-reconcile` → `topical_hook_queue.py boundary|reconcile <root>` (`hooks/run-hook:84-105`).
- `update-queued` → `python3 core/update_hook_queue.py enqueue` (`hooks/run-hook:122-125`).
- `posttool`의 backend 준비가 `ensure --wait`(동기)에서 `ensure &`(비동기)로 바뀌었다 (`hooks/run-hook:146`).
- 직접 `update` action은 남아 있다(manifest에서는 쓰지 않음) (`hooks/run-hook:132-144`).

### 2.3 시간 예산 `core/hook_budget.py` (신규)
- 기본 18초, `QMD_HOOK_TOTAL_SECONDS` 2~20초 범위만 수용, 범위 밖·비수치는 18초 (`core/hook_budget.py:7-22`).
- `arm()`은 절대 monotonic deadline을 `QMD_HOOK_DEADLINE_MONO`로 export해 자식 프로세스가 상속한다. 자식은 이미 소진된 부모 예산을 리셋하지 못한다 (`core/hook_budget.py:25-36`).
- `watchdog=True`면 `SIGALRM` + `setitimer`로 남은 시간 뒤 `HookDeadlineExceeded`(BaseException)를 던진다 (`core/hook_budget.py:40-42`).
- `remaining(cap)`은 `min(left, cap)`을 돌려주고 소진 시 예외 (`core/hook_budget.py:46-52`).
- `hook_main.run`이 `HookDeadlineExceeded`를 잡아 `QMD_RECALL_LOG`에 `qmd_hook_deadline_exceeded` 한 줄(평문)을 쓰고 exit 0 (`core/hook_main.py:45-52`).

### 2.4 SessionStart 비동기화 `core/update_hook_queue.py` (신규)
- `enqueue`: payload cwd 검증 → `~/.cache/qmd/update-hook-jobs`(0700, 소유자 확인) 준비 → `setup_guard.status(cwd)`가 `ready`가 아니면 job을 만들지 않고, `setup_required`면 setup 안내만 1회 출력 (`core/update_hook_queue.py:102-119`).
- ready면 `<sha256(cwd)[:32]>.job.json`을 `O_EXCL`로 생성(프로젝트당 pending 1개, 중복은 합쳐짐), 버전 변경/설정 상태 안내(`_setup_notice`), 그리고 **`update.sh --resolve-only --cwd`를 1초 상한으로 동기 실행해 `reason == pending`일 때만 opt-in 미결정 안내를 출력**한다 (`core/update_hook_queue.py:120-149`).
- 이어 `worker <key>`를 `start_new_session=True`로 detach (`core/update_hook_queue.py:150-152`).
- `worker`: per-key flock → setup 재확인(아니면 status `setup_required`, job 보존) → 프로젝트 index가 선택되지 않았을 때만 `backend_manager.sh ensure/warm/rotate` → `bash update.sh`(stdin `{"cwd":…}`, timeout 120초) → stdout/stderr 각 2048자까지 `<key>.log`에 기록 → `<key>.status.json`에 `completed|failed` → 성공 시에만 job 삭제 (`core/update_hook_queue.py:155-211`).
- 로그는 1 MiB 넘으면 `.log.1`로 회전. enqueue 실패는 `enqueue.log`/`enqueue.status.json` (`core/update_hook_queue.py:177-178, 224-243`).

### 2.5 `core/update.sh`
`git diff --stat`의 657줄 중 공백 무시(`-w`) 실변경은 69+/192−다. 대부분은 `with wiki_mutation_lock.lock(...)` 아래로 들여쓰기된 것과 `--init-wiki` 인라인 Python을 `core/wiki_init.py`로 옮긴 것이다. **update.sh 내부 흐름 자체는 비동기화되지 않았다**(비동기화는 2.4의 바깥 층). 실변경:
- `run_update` 선두에서 `qmd_route.py resolve-env`로 프로젝트 shadow DB를 해석해 `INDEX_PATH`/`QMD_CONFIG_DIR`/`XDG_CACHE_HOME`/`QMD_PROJECT_SHADOW_INDEX=1`을 export한다. pointer가 손상되면 전역 DB로 폴백하지 않고 `ABORT` (`core/update.sh:1114-1124`).
- shadow index일 때는 embed 뒤 데몬 reload를 하지 않는다 (`core/update.sh:1263`).
- `contextLearning.autoCycle`이 켜져 있으면 embed 성공 뒤 `context_learning_auto.py launch-current`, 실패면 deferred 로그 (`core/update.sh:1272-1278`). 동기 경로 끝에서 `launch-historical` (`core/update.sh:1970-1977`). 판정은 `config_json` 문자열 매칭이다(`*'"autoCycle": true'*`).
- `QMD_AUTO_COMPILE_DISABLED`면 SessionStart compile flush를 하지 않는다 (`core/update.sh:1421`).
- `_file_mtime`: GNU `stat -c` 우선, BSD `stat -f`, 숫자 검증 (`core/update.sh:157-165`). notice marker mtime 판정이 이것을 쓴다.
- prune·`--enable-compile`·`--optin --recommended`·`--optout` 계열의 settings 쓰기를 `wiki_mutation_lock.lock`으로 감쌌다 (`core/update.sh:1033, 2108, 2227, 2303`).
- `--optin --recommended`는 wiki가 필요하면 `wiki_init.init_wiki`로 scaffold를 먼저 만들고 settings를 마지막에 게시한다(기존에는 settings 쓰기 뒤 `--init-wiki`를 실패 무시로 호출) (`core/update.sh:2250-2257`).
- `--init-wiki`는 `core/wiki_init.py` 호출 한 줄 (`core/update.sh:2042`).

### 2.6 dirty queue
- `core/dirty_queue.py`: enqueue가 고정 sidecar 락(`<queue>.lock`)을 먼저 잡고, append 뒤 `fsync`. **선택된 프로젝트 index가 있으면 3번째 컬럼 `project_root`를 붙인다**(`enqueue_project_collections`) (`core/dirty_queue.py:16-43`). CLAUDE.md는 여전히 "2컬럼 프로토콜 유지"라고 적혀 있다.
- `core/dirty_queue_claim.py`(신규): `claim`은 큐 원본을 그대로 둔 채 현재 내용 전체를 base64·sha256·inode와 함께 `<queue>.claim.json`(phase `processing`)으로 fsync 게시한다. `ack`는 phase를 `done`으로 바꾼 뒤 원본에서 그 prefix만 잘라 원자 교체한다. inode가 이미 바뀌었으면 교체가 끝난 것으로 보고 claim만 지운다. `reloadRequired` 플래그를 claim에 영속화한다 (`core/dirty_queue_claim.py:35-45, 81-93, 96-130, 133-146, 149-162`). CLI exit code: 0 성공, 3 "없음/false", 1 오류.
- `core/index_enqueue.py`: projectRoot를 찾아 `enqueue_project_collections`. 실패하면 2컬럼 라인을 쓰지 않고 그냥 종료한다(전역 라우팅 방지) (`core/index_enqueue.py:58-64`).
- `core/sync.py`: 진입 시 `setup_guard.status != ready`면 `{"ok":false,"reason":"setup_required"}` + exit 1, dirty enqueue는 `enqueue_project_collections` (`core/sync.py:378-381, 437`).

### 2.7 `backend/index_worker.sh`
- snapshot→truncate→requeue 구조를 버리고 claim/ack 사용. 모든 단계(add/update/embed/reload)가 성공해야 `ack_claim` (`backend/index_worker.sh:74-91, 237`).
- worker/writer lock은 `find -mmin +10` 나이 판정 대신 `reclaim_dead_lock`(pid 생존 확인)으로 회수 (`backend/index_worker.sh:31-43, 66-70, 101-108`).
- lock busy는 **exit 75**, add/update/embed/reload 실패는 **exit 1**, 둘 다 claim과 원본 큐 보존 (`backend/index_worker.sh:107, 192, 200, 212, 220`).
- 엔트리마다 `setup_guard.py check`와 `qmd_route.py resolve-env`를 돈다. 하나라도 setup 미완·pointer 손상이면 **그 run 전체가 exit 1**(claim 유지) (`backend/index_worker.sh:116-134`).
- 프로젝트 index 엔트리가 하나라도 있으면 엔트리별 서브셸에서 각자의 `INDEX_PATH` 등으로 `collection add` → `update` → `embed`. 전역 엔트리가 섞이면 reload 의무를 claim에 기록 (`backend/index_worker.sh:135-172`).
- reload 실패가 `return 1`로 바뀌었고, 실패한 reload 의무는 claim의 `reloadRequired`로 다음 run(0/0 incremental이어도)에 이어진다 (`backend/index_worker.sh:52-59, 229-236`).

### 2.8 recall `core/recall.py` (+283줄)
- 예산: `main()`에서 `hook_budget.arm()`(watchdog 켬) (`core/recall.py:2007`). health, 본 질의, raw backfill, lex 프로브, DF 프로브(`deadline = min(..., hook_budget.deadline())`), shadow 질의가 전부 `hook_budget.remaining()`로 잘린다 (`core/recall.py:68, 1351, 1857, 2200-2214`).
- **프로젝트 index 라우팅**: `qmd_route.project_paths(projectRoot)`. 예외면 `project_index_invalid`로 기록하고 무출력 (`core/recall.py:2149-2153`). `selected`면 데몬 health를 건너뛰고 `qmd_project_query.query`(= `qmd query` CLI를 선택 DB/config/cache env로 subprocess, `--no-rerank`, `-n`, `-c` 컬렉션)로 질의한다 (`core/recall.py:2173-2186`, `core/qmd_project_query.py:13-48`). 이 경로에서는 `shadow_on=False`, `lex_df="project_local_cli"`이고 **DF 좁히기(`narrow_general_lex`)를 하지 않는다** (`core/recall.py:2174-2175, 2243`). lex 게이트 프로브도 같은 CLI로 간다 (`core/recall.py:1289-1295, 2775`).
- 전략 분기(`hierarchical`/`wikiOnly`) 코드가 데몬 분기 밖으로 나와 두 경로가 공유한다 (`core/recall.py:2229-2241`).
- **live selection**(`contextLearning.liveSelection: true`): 질의 limit 8→15, 카드 15장까지 실제 파일 snapshot·QMD active 문서 hash 일치를 확인한 뒤 `context_learning.live_select.choose`(로컬 Laya)에 넘겨 0~3장을 고른다. 전제 실패는 기존 QMD topN(최대 3)으로 폴백. 이벤트 `qmd_laya_selection`, 새 reason `laya_selected_zero` (`core/recall.py:2046-2047, 2653-2712, 2798`).
- **capture**(`contextLearning.capture: true`): 출력 print **뒤**에 `context_learning.seam.observe`로 후보 풀을 private DB에 기록한다. `candidateTopK != 8`이면 `context_learning.pool.expand`가 추가 질의(1초·256KB 상한)로 풀을 넓힌다. 실패는 `learning_expansion_failed`. 주입 출력은 바꾸지 않는다 (`core/recall.py:2969-3039`).
- `bounded_shadow` 인자가 `read_card_text_full`/`read_wiki_meta`/`annotate_wiki_result`에 추가됐다(학습 관측용 크기 제한 읽기) (`core/recall.py:171-194`).
- topical 카드 전용 분기는 recall.py diff에 없다. topical 카드는 v2 publisher가 shadow DB에 색인한 wiki 카드로 일반 wiki 경로에서 주입된다 (`docs/operational-flow-map-20261006.md:90`) (문서 근거, recall 쪽 코드는 일반 경로와 동일함을 확인).

### 2.9 `core/posttool.py`
- `hook_budget.arm(watchdog=False)` 후 recall 자식을 `Popen(start_new_session=True)` + `communicate(timeout=remaining())`, timeout이면 `killpg(SIGKILL)` 후 무출력 (`core/posttool.py:152-218`).
- 자식 비정상 종료면 출력 버림 (`core/posttool.py:219`).
- apply_patch: `*** Move to:` 경로 포함, Codex의 `tool_input.command`를 patch로 해석, 일반 텍스트 후보에서 `command` 키 제거(Bash 명령이 검색어가 되지 않음), `+++`로 시작하는 줄도 추가 줄로 취급 (`core/posttool.py:54-56, 62, 75-89`).

### 2.10 `core/sqlite_read.py` (신규)
QMD index SQLite를 **데몬을 거치지 않고 직접** 연다.
- `connect`: `mode=ro` + `PRAGMA query_only`. WAL 형식인데 `-wal`이 없는(닫힌) DB는 `immutable=1`로 읽고, 읽는 동안 WAL 출현·stat 변화가 있으면 `qmd_index_changed_during_read` (`core/sqlite_read.py:151-193`).
- `snapshot_fingerprint`: `sqlite3.backup`으로 owner-only 임시 디렉터리(`$TMPDIR/qmd-source-snapshots-v1-<euid>/job-<token>`)에 복사해 sha256. SIGKILL로 남은 job은 다음 호출이 flock으로 죽은 것만 회수 (`core/sqlite_read.py:45-132, 196-222`).
- `cutover_guard`: **읽기 전용이 아니다.** `mode=rw`로 열어 `BEGIN IMMEDIATE`로 writer 예약을 잡은 채 fingerprint를 전후 비교하고 항상 ROLLBACK (`core/sqlite_read.py:225-251`).
- 소비자: `core/orphan_reclaim.py:200`(orphan 수 count), `core/context_learning/corpus.py:35`(`documents`의 `collection,path,hash WHERE active=1`), `core/runtime_update.py:43,177,183,209,229,350`, `core/install_update.py:294,314,412,492`. orphan_reclaim은 main에서도 DB를 직접 읽었다 — 새로운 것은 공용 reader와 `qmd_route` 기반 DB 선택이다.

### 2.11 `core/orphan_reclaim.py`
- DB 경로를 `INDEX_PATH`/XDG 직접 해석 대신 `qmd_route.project_paths(cwd)['INDEX_PATH']`로 정한다(프로젝트 shadow DB 대상) (`core/orphan_reclaim.py:184-190`).
- qmd 바이너리 기본값이 `"qmd"`에서 `qmd_route.binary_info()['QMD_BIN']`(관리형 runtime)으로 (`core/orphan_reclaim.py:214`).

### 2.12 `backend/daemon.sh`, `core/backend_manager.sh`
- daemon.sh: `resolve_qmd_bin` 실패 시 `~/.bun/bin/qmd` 폴백 없이 FATAL (`backend/daemon.sh:17-20`). entry·pinned node·package bin을 `qmd_route.py daemon-spec`에서 받는다(관리형 QMD runtime 지원) (`backend/daemon.sh:27-31`). node 선택 순서 pinned → `QMD_NODE_BIN` → fnm, ABI probe는 `createRequire(<package bin>)`로 better-sqlite3를 그 설치 기준으로 찾는다(`$HOME` cwd 의존 제거) (`backend/daemon.sh:43-72`). PATH 앞에 붙이지 않고 node 절대 경로로 exec (`backend/daemon.sh:84-85`). dist 진입점 부재 시 launcher 폴백 경고는 `qmd_route.binary_info`의 무경고 폴백으로 대체됐다 (`core/qmd_route.py:42-45, 57-59`).
- backend_manager.sh: `identity` 서브커맨드(pid 입양 없이 관리 데몬 pid·cmdline 출력, 반환 0 정상/1 없음/2 health만 정상/3 pid 있으나 unhealthy) (`core/backend_manager.sh:508-521`). 소비자는 `core/install_update.py:644`. `kick_wiki_compile`에 `QMD_AUTO_COMPILE_DISABLED` 가드 (`core/backend_manager.sh:461`).

### 2.13 `core/config.py`
- `contextLearning` 블록 정규화: `capture`/`autoCycle`/`liveSelection`은 `is True`만, `candidateTopK`는 liveSelection이면 15, 아니면 1~15 int, 그 외 8, `stateRoot` 문자열 (`core/config.py:1004-1016`).
- `migrate_legacy_config`가 `.auto-context/.install-update.lock`(0600, flock NB)을 잡고, `install-update-journal.json`이 있으면 `managed_setup_in_progress`로 이주하지 않는다 (`core/config.py:1320-1385`).

### 2.14 `hermes_adapter/core_bridge.py`
- `_setup_allowed(cwd)`: 어댑터 자신의 `core/setup_guard.py check`를 2초 상한으로 실행 (`hermes_adapter/core_bridge.py:72-83`). `recall_context`·`session_update`·`pre_edit_gate`·`post_edit_sync`가 실패 시 조기 반환 (`hermes_adapter/core_bridge.py:211, 245, 275, 318`).

## 3. 제어·데이터 흐름

```
SessionStart (Claude/Codex)
 ├─ run-hook update-queued ─(setup_guard 생략, 내부에서 판정)─► update_hook_queue enqueue
 │     ├─ setup_guard != ready → (setup_required면 안내 1회) 끝, job 없음
 │     ├─ ~/.cache/qmd/update-hook-jobs/<key>.job.json (O_EXCL)
 │     ├─ update.sh --resolve-only (≤1s) → pending이면 opt-in 안내 stdout
 │     └─ Popen detached: update_hook_queue worker <key>
 │            ├─ (pointer 없음) backend_manager ensure/warm/rotate
 │            └─ bash update.sh (≤120s) ─ stdout(알림) → <key>.log[:2048]
 │                   └─ update.sh 자체 --worker fork: add/update/embed(shadow DB면 reload 생략)
 │                        → autoCycle launch-current → dedup scan → orphan reclaim
 └─ run-hook topical-reconcile ─ setup_guard hook-root(+opt-in 마커) ─► topical_hook_queue reconcile <root>

UserPromptSubmit ─ run-hook recall ─ setup_guard hook ─► recall.py (arm 18s, SIGALRM)
     ├─ pointer 선택됨 → qmd_project_query (qmd query CLI, shadow DB)   [DF 좁히기·shadow 없음]
     └─ 아니면        → 데몬 :8483 /query
     → 필터/게이트 → (liveSelection: Laya choose) → stdout 주입 → (capture: observe)

PostToolUse ─ posttool(22s: recall 자식, killpg) / index(→dirty queue 3컬럼 → kick-index)
            / compile / topical-event(wiki_topical_reconcile event)
Stop ─ run-hook topical-stop ─ setup_guard hook-root ─► topical_hook_queue boundary <root> ; stdout '{}'

index_worker.sh: claim(.claim.json fsync) → 엔트리별 setup_guard·route → add/update/embed(+reload)
                 → ack(prefix 원자 제거). 실패 = claim·원본 보존(exit 1/75)
```

## 4. host별 영향

- **Claude Code**: manifest가 Stop 이벤트를 등록한다. SessionStart 알림 중 opt-in 미결정 안내와 setup 안내만 같은 턴에 출력된다. Claude teardown 시 detached worker 생존은 "untested" (`docs/hook-budget-and-recovery-20261006.md:50-51`) (문서 근거).
- **Codex**: Claude와 같은 action 집합. Stop hook이 실제 Codex host 세션에서 관측되지 않았다는 기록이 있다(`docs/operational-flow-map-20261006.md:120`) (문서 근거, 코드 미확인). Codex에서 hook 종료 후 worker가 2.99s·4.23s 뒤 완료됐다는 실측 기록 (`docs/hook-budget-and-recovery-20261006.md:45-50`) (문서 근거).
- **Hermes**: Stop/topical/`update-queued` 경로가 없다. `session_update`는 여전히 update.sh를 직접 동기 호출한다. 네 hook 모두 `_setup_allowed`로 setup_guard를 거친다. recall의 18초 예산은 `recall.py` 내부에서 걸리므로 Hermes에도 적용된다.
- **Antigravity(Gemini)**: manifest 변경 없음. `run-hook`의 sandbox 가드 위치와 setup_guard 게이트는 engine과 무관하게 적용된다.

## 5. 신규·변경 설정 키, env 변수, 파일 경로

설정 키
- `contextLearning.{capture, autoCycle, liveSelection, candidateTopK, stateRoot}` (`core/config.py:1004-1016`).

env
- `QMD_HOOK_TOTAL_SECONDS`(2~20, 기본 18), `QMD_HOOK_DEADLINE_MONO`(내부 상속용) (`core/hook_budget.py`).
- `QMD_UPDATE_HOOK_QUEUE_DIR` (`core/update_hook_queue.py:15`).
- `QMD_AUTO_COMPILE_DISABLED` (`hooks/run-hook:41`).
- `QMD_PROJECT_SHADOW_INDEX`, 그리고 shadow DB용 `INDEX_PATH`/`QMD_CONFIG_DIR`/`XDG_CACHE_HOME` export (`core/update.sh:1123`).
- `QMD_NODE_BIN` (daemon node override) (`backend/daemon.sh:62`).
- `QMD_SETUP_GUARD_FIXTURE=1` — 테스트 전용 setup_guard 우회 (`core/setup_guard.py:21`, `package.json:10`, `AGENTS.md`).
- `QMD_TOPICAL_PROJECT_ROOT`/`QMD_TOPICAL_SANDBOX_ROOT` (`core/setup_guard.py:70-73`).

파일 경로
- `~/.cache/qmd/update-hook-jobs/` : `<key>.job.json`, `<key>.lock`, `<key>.log(.1)`, `<key>.status.json`, `enqueue.log`, `enqueue.status.json`, `setup-version-<hash>.json`, `setup-notice-<token>.json` (`core/update_hook_queue.py`).
- dirty queue sidecar: `<queue>.lock`, `<queue>.claim.json` (`core/dirty_queue_claim.py:22-24`).
- `$TMPDIR/qmd-source-snapshots-v1-<euid>/job-<token>/` (`core/sqlite_read.py:45-53`).
- 프로젝트: `.auto-context/qmd-index-active.json`(pointer), `.auto-context/qmd-index-generations/<gen>/{index.sqlite,prepared.json,config/index.yml,cache}` (`core/runtime_update.py:281-304`), `.auto-context/install-update-journal.json`, `.auto-context/.install-update.lock` (`core/config.py:1322`).
- 플러그인 루트: `.qmd-local-auto-compile-disabled`, `.qmd-local-runtime.env` (`hooks/run-hook:41-44`).
- topical opt-in 마커 `.qmd-topical-v2-project`/`.qmd-topical-sandbox` (`core/wiki_topical.py:27-28`), `.topical-reconcile-hook.json` (`hooks/run-hook:94`).

## 6. main 대비 동작 변화

1. **기존 opt-in 프로젝트 전부가 업그레이드 직후 멈춘다(breaking).** `setup_guard.status`는 `auto-context-dir` 프로젝트에 pointer가 없으면 `project_index_not_selected`를 낸다 (`core/setup_guard.py:48-50`). legacy 설정은 `legacy_config` (`core/setup_guard.py:46-47`). main에서 만든 프로젝트는 pointer가 없으므로 recall·posttool·index·compile·gate가 exit 0 무출력이 되고 (`hooks/run-hook:71-73`), SessionStart update job도 만들어지지 않는다 (`core/update_hook_queue.py:115-119`). 해제 경로는 setup skill의 명시 실행뿐이다. 설정 없는 프로젝트(`configFormat == none`)는 `ready`라 pending gate는 그대로 동작한다.
2. **pointer가 있는 프로젝트는 전역 데몬을 쓰지 않는다.** ready = pointer 존재이므로 운영 recall의 실질 경로는 `qmd query` CLI다. 데몬 경로는 테스트(`QMD_SETUP_GUARD_FIXTURE=1`)와 pointer 없는 예외 경우에만 남는다. update/index도 shadow DB에 쓰고 데몬 reload를 생략한다.
3. **"전체 훅 deadline은 의도적으로 없다" 원칙이 뒤집혔다.** recall·posttool은 18초 절대 deadline + host timeout 22초다. main의 구조적 최악(≈14초, deadline 없음)과 달리 이제 상한이 있고, 소진 시 주입 0건 + `qmd_hook_deadline_exceeded` 로그 줄이 종점이다. CLAUDE.md의 "전체 훅 deadline은 의도적으로 없다 … ≈14초" 문단은 갱신되지 않았다(CLAUDE.md diff 0줄).
4. **SessionStart 알림이 host 턴에 닿지 않는다.** worker가 update.sh stdout을 로그 파일에만 쓴다 (`core/update_hook_queue.py:202`). `notice_once` 고유 키 16개·호출 17곳(wiki-dedup, source-missing, dead-registration, daemon-down, stale-queue 등. main도 16개)과 무조건 emit되는 dedup resolver 스폰 지시(`core/update.sh:1651-1659`)가 모두 여기에 해당한다. worker는 `subprocess.run`에 `env=`를 주지 않아(`core/update_hook_queue.py:150-152, 197-199`) run-hook env를 그대로 상속하고, run-hook은 `QMD_SUPPRESS_NOTICE`를 설정하지 않는다. 같은 턴에 나오는 것은 `_setup_notice` 안내(setup_required 사유, ready 프로젝트의 `plugin_version_changed`)와 opt-in 미결정 안내뿐이다. 문서도 이 제한을 인정한다 (`docs/hook-budget-and-recovery-20261006.md:30-32`).
5. SessionStart hook 반환은 "작업 접수"만 뜻한다. 인덱스 갱신 완료는 같은 턴에 보장되지 않는다.
6. dirty queue: 실패·lock busy 시 큐 유실 없이 claim 보존·재시도. 라인 형식은 3컬럼일 수 있다(외부 소비자가 2컬럼을 가정하면 깨진다).
7. posttool이 데몬 기동을 기다리지 않는다(`ensure --wait` 제거). 데몬이 내려가 있던 첫 편집에서는 힌트가 비어 나올 가능성이 늘어난다(추정).
8. posttool 검색어에서 Bash `command`가 빠지고, Codex apply_patch는 추가 줄만 쓴다.
9. daemon.sh: `~/.bun/bin/qmd` 폴백 제거. PATH에서 qmd를 못 찾고 관리형 runtime도 없으면 데몬이 뜨지 않는다.
10. 플러그인 루트에 `.qmd-local-runtime.env`가 있으면 모든 hook이 그 env를 상속한다.

## 7. 테스트 커버리지

- `test/hook-structure.test.mjs`: 두 manifest의 이벤트 집합에 `Stop` 포함, UserPromptSubmit/PostToolUse[0] timeout 22·`async` 아님, SessionStart[0] timeout 5·`update-queued`, Stop timeout 3.
- `test/codex-hook-root.test.mjs`: Codex command 형태를 `^"${CLAUDE_PLUGIN_ROOT}/hooks/run-hook" [a-z][a-z-]* codex$`로 고정(하이픈 action 허용).
- `test/dispatcher.test.mjs`: sandbox/headless 가드가 stdin을 읽기 전에 종료하므로 파일 fd로 payload 공급(EPIPE 경합 회피).
- `test/run-hook-backend-manager.test.mjs`: posttool이 `ensure`(no `--wait`)를 백그라운드로 호출.
- `test/posttool.test.mjs`: `QMD_HOOK_TOTAL_SECONDS=2`에서 5초 자고 있는 가짜 recall 자식을 끊고 무출력·`<3500ms`; apply_patch 추가 줄만 추출.
- `test/update-hook-queue.test.mjs` → `test/fixtures/update-hook-queue-checks.py`: 빠른 enqueue, setup 안내 1회, 중복 합침, durable status, 실패 재시도, 외부 호출 0.
- `test/dirty-queue-durable.test.mjs`(5개): add/update 오류 시 claim 보존, 읽기 전용 디렉터리, ack 쓰기 실패, embed 중 SIGKILL + 동시 enqueue, reload 실패의 0/0 재시도·재시작 승계.
- `test/index-worker.test.mjs`: lock busy exit 75, embed 실패 후 재시도, writer lock busy에도 claim(`phase: processing`)과 원본 큐 보존.
- `test/backend.test.mjs`: index_worker가 `dirty_queue_claim.py`를, claim이 `fcntl.flock`을 쓰는지(소스 문자열).
- `test/backend-manager.test.mjs`: `identity`가 unhealthy 관리 데몬(3)·pid 파일 없음 + 발견(3, pid 파일 미생성)·부재(1) 구분.
- `test/cooldown-guards.test.mjs`: index_worker의 lock 회수 근거를 `-mmin +10`에서 `reclaim_dead_lock "$WORKER_LOCK"`로 교체.
- `test/crowding-probe.test.mjs`: `"limit": DAEMON_QUERY_LIMIT` 리터럴 3→2, 본 질의는 `limit=DAEMON_QUERY_LIMIT` 기본 인자.
- `test/sqlite-read-wal.test.mjs`: sidecar 없는 offline WAL 읽기, DB 무변경, live WAL은 최신 내용.
- `test/sqlite-source-snapshot-cleanup.test.mjs`: SIGKILL 잔해 회수, 살아 있는 동시 백업·무관 파일 보존, 원본 무변경.
- 프로젝트 라우팅 lex 프로브: `test/fixtures/qmd-lex-probe-routing.py`. live selection 실 hook: `test/fixtures/context-learning-live-recall-e2e.py`. setup guard 실경로: `test/setup-upgrade-guard.test.mjs`.

## 8. 위험·미해결·TODO

1. **CLAUDE.md 미갱신**: "전체 훅 deadline은 의도적으로 없다", "dirty 큐 2컬럼 프로토콜 유지", "SessionStart 알림 지시형 설계", "update.sh가 SessionStart에서 인덱스 갱신" 서술이 코드와 맞지 않는다. 다음 작업자가 CLAUDE.md를 근거로 잘못 판단할 수 있다.
2. **SessionStart 알림 소멸 + marker 소진**: worker가 `QMD_SUPPRESS_NOTICE` 없이 update.sh를 돌리므로 `notice_once`가 TTL marker를 쓰고, 알림은 로그로만 간다. 결과: 전달 없이 4시간 억제. 자율 처리형 dedup 지시(resolver 스폰)는 Claude/Codex에서 사실상 비활성이다. Hermes가 marker 선점을 막으려 `QMD_SUPPRESS_NOTICE=1`을 쓰는 것과 같은 문제가 이제 모든 host에 생긴다.
3. **index_worker head-of-line 정지(코드 확인 + 격리 실측)**: 배치(`sort -u` 결과라 위치 무관) 안 엔트리 하나라도 `setup_guard check` 실패면 qmd 호출 전에 run 전체가 exit 1이고 claim이 `processing`으로 남는다 (`backend/index_worker.sh:117-125`). 다음 run의 `claim`은 기존 claim의 같은 prefix를 재사용하므로(`core/dirty_queue_claim.py:104-108`) 같은 엔트리에서 다시 멈추고, 그 뒤 enqueue된 라인은 배치에 들어오지도 못한다. 건너뛰거나 드롭하는 분기는 없다. 임시 HOME 격리 실측: 미설정 프로젝트 1줄 + 설정 없는 경로 1줄 → 2회 모두 rc=1, 가짜 qmd 호출 0, 추가 append 라인 미처리. 업그레이드 전 큐에 남은 미설정 프로젝트 라인 하나가 **모든 프로젝트의 편집 후 인덱싱**을 막는다(SessionStart `update.sh`의 프로젝트별 갱신은 별개 경로라 계속 돈다). 해소 경로는 해당 프로젝트 setup, 그 경로 삭제, 수동 큐·claim 편집이다. optout은 guard가 `optout`(≠ready)을 내므로 해소하지 못한다 (`core/setup_guard.py:26-27`).
4. **진단 계약 위반**: setup_guard 차단(`hooks/run-hook:71-73`)은 `QMD_RECALL_LOG` 기본값 설정(`hooks/run-hook:113-116`) 이전이고 core 스크립트를 실행하지 않으므로, `QMD_RECALL_LOG`를 명시해도 로그 줄이 남지 않는다. 격리 실측: 레거시·포인터 없는 모던 설정 × recall/posttool/index/compile/gate 10건 모두 rc=0·무출력·캐시 디렉터리 파일 0. CLAUDE.md의 "줄이 하나도 없다 = 훅이 실행되지 않았다" 판독이 `setup_required`와 구분되지 않는다.
5. **JSONL 비JSON 줄 추가**: `hook_main`이 recall 진단 로그(JSONL)에 평문 `qmd_hook_deadline_exceeded`를 쓴다 (`core/hook_main.py:45-50`). JSON 줄 파서가 그 줄에서 실패한다. 이 클래스는 신규가 아니다 — main의 `_log_exception`도 평문 `qmd_hook_uncaught_exception` + traceback을 같은 파일에 쓴다(`git show main:core/hook_main.py`, 현재 `core/hook_main.py:61-68`). 이 브랜치는 평문 줄 종류를 하나 더한다.
6. **프로젝트 CLI 지연(추정)**: hierarchical에서 프롬프트당 `qmd query` 프로세스가 최대 3회(본 질의·lex 프로브·raw backfill) 뜨고, 각각 Node + better-sqlite3 + (vec) 임베딩 모델 로드를 거친다. 데몬이 없애려던 비용이다. 18초 예산 안에서 잘리지만 이 transcript 범위에서 지연 실측은 확인하지 못했다(문서는 Claude/Codex 합성 세션의 주입 성공만 기록).
7. **DF 좁히기·게이트 distrust 상실(코드 확인)**: 프로젝트 경로는 `narrow_general_lex`를 건너뛰어 위치 컷(`GENERAL_LEX_TERM_CAP=3`, `core/keywords.py:327`)만 쓴다 (`core/recall.py:2242-2253`). `lex_df="project_local_cli"`, `lex_terms_absent_in_cut=0`, `lex_identifier_present=False` 초기값이 그대로 남아(`core/recall.py:2134-2135, 2175`) `narrowing_distrust`가 항상 None이다(`core/recall.py:1505-1517, 2745`). 결과 두 가지. (a) main이 고친 "구어체 군더더기 → lex 0건 → 게이트 발동 → 본문 손실"이 운영 경로에서 다시 열린다(위치 컷 질의가 그대로 프로브되므로 코드상 확정, 라이브 빈도는 미측정). (b) 일반 term이 1개뿐인 잡담(코드 주석 실측 `엉 그렇게 해줘.~` → `그렇게` 히트 5, `core/recall.py:1448`)은 main에서 `lone_survivor` distrust로 게이트가 발동했지만 프로젝트 경로에서는 CLI 프로브 히트로 게이트가 열려 본문이 주입된다. 위치 컷 안 부재 term은 AND 0건으로 어차피 `no_hits` 발동하므로 `absent_terms` 사유의 상실은 실질 영향이 없다.
8. **테스트가 운영 경로를 덜 덮는다**: `npm test`가 `QMD_SETUP_GUARD_FIXTURE=1`을 전역으로 켠다 (`package.json:10`). 기존 hook·recall 테스트 대부분은 guard를 우회한 데몬 경로를 검증하고, 실제 운영 경로(pointer + CLI)는 일부 fixture(`qmd-lex-probe-routing.py`, `context-learning-live-recall-e2e.py`, `setup-upgrade-guard.test.mjs`)만 덮는다.
9. **벽시계 단정**: `test/posttool.test.mjs`의 `performance.now() - started < 3500`은 `QMD_TEST_TIMING` 게이트 밖이다. CLAUDE.md의 "벽시계 단정은 opt-in" 규칙과 어긋난다.
10. **Stop 실 host 검증 미완**: Codex host에서 Stop hook 실행 미관측, Claude teardown 생존 미검증 (`docs/operational-flow-map-20261006.md:120`, `docs/hook-budget-and-recovery-20261006.md:51`) (문서 근거).
11. `.qmd-local-runtime.env` 파서(`core/local_runtime_env.py`)를 직접 검증하는 테스트는 `test/local-trial-compile-gate.test.mjs` 하나에서만 이름이 확인된다. 이 파일은 모든 hook의 env를 바꾸므로 거부 규칙 커버리지를 따로 확인할 필요가 있다(미확인).
12. autoCycle 판정이 `config_json` 문자열 매칭이다 (`core/update.sh:1176-1178`). JSON 직렬화 공백이 바뀌면 조용히 꺼진다(추정).
13. update_hook_queue worker는 update.sh stdout/stderr를 각 2048자로 자른다. 긴 진단은 로그에서도 잘린다.
14. CLAUDE.md "버전 bump 체크리스트": `package.json`이 `0.35.2-dev.20261004`로 바뀌었다. 다른 manifest 정합은 이 영역 밖이다.
15. **로그 회전 경로 축소(코드 확인)**: `update_hook_queue.worker`는 프로젝트 index가 선택되지 **않았을 때만** `backend_manager.sh ensure/warm/rotate`를 부른다 (`core/update_hook_queue.py:189-195`). opt-in 프로젝트는 ready가 곧 포인터 선택이므로 그 세션의 SessionStart에서는 `rotate`가 호출되지 않는다. 반면 설정 없는 폴더(`configFormat == none`)는 ready이면서 `selected=False`라(격리 실측) 그 cwd의 SessionStart worker는 여전히 `rotate`를 부르고, 상속된 `QMD_RECALL_LOG`(그 engine의 `qmd-<engine>-hook.log`)와 데몬 로그를 자른다. 즉 회전은 "미설정 폴더에서 세션이 열릴 때"에만 일어난다. 그 밖의 호출자는 manifest가 쓰지 않는 `run-hook update`(`hooks/run-hook:141`), Hermes `session_update`(`hermes_adapter/core_bridge.py:250`), 수동 `update` skill(`skills/update/scripts/update.sh:18`)뿐이다. `rotate` → `backend/logrotate.sh`가 기본으로 켜진 `QMD_RECALL_LOG`(`hooks/run-hook:115`)와 데몬 로그를 자르는 유일한 경로이므로, opt-in 프로젝트에서만 세션을 여는 사용자는 진단 로그가 다시 무제한으로 자란다(CLAUDE.md 실측 14.6MB 사례와 같은 클래스). 수동 `update` skill 경로는 `QMD_RECALL_LOG`를 export하지 않으므로 데몬 로그만 회전한다(`backend/logrotate.sh:41-44`).
16. `run-hook recall/posttool`은 여전히 매 호출 `backend_manager.sh ensure &`를 띄운다 (`hooks/run-hook:128, 146`). pointer 경로는 데몬을 질의하지 않으므로 쓰지 않는 데몬을 기동·유지한다. 6-7(posttool `--wait` 제거)의 영향도 데몬 경로, 즉 테스트·비운영 경로에만 해당한다.

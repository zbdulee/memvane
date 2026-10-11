# topical wiki 파이프라인 (main 0f277e2 → feat/v2-pre-release-validation 92ecd89)

## 1. 한 줄 요약

topical(v2) wiki는 기존 per-source compile(`wiki_compile_worker`)을 **대체하지 않고 병행**한다. 프로젝트 루트에 손으로 만든 owner-only marker와 자동 정책 파일이 있을 때만 켜지고, Stop hook이 남긴 job을 detached worker가 처리한다. 순서는 원문 전체 scan → batch claim → Codex 생성 → 카드별 검수 → 사람 승인 유사성 게이트 → 게시(`status: verified` stamp) → 프로젝트 전용 QMD update/embed다. 유료 호출은 턴(turnKey)당 12작업·추정 640센트로 제한된다.

## 2. 무엇이 새로 생겼나 / 바뀌었나

### 신규 모듈 (전부 main에 없음)

| 파일 | 역할 | 핵심 함수 |
|---|---|---|
| `core/wiki_topical.py` | v2 카드 스키마 검증과 불변 generation staging. 모델·데몬·hook은 호출하지 않는다(`core/wiki_topical.py:1-6`) | `opted_in`(:31), `safe_source`(:152), `valid_span`(:192), `normalize`(:245), `compile_sandbox`(:324) |
| `core/wiki_topical_experiment.py` | 생성 계약(prompt payload), staged 카드 재검증, per-span verdict 도출. 이름은 experiment지만 **운영 경로가 import한다** | `require_sandbox`(:31), `generation_contract`(:38), `load_staged_card`(:105), `mock_semantic_verdict`(:160). `render_hook`/`invoke`(:398,:443)는 CLI·테스트 전용 |
| `core/wiki_topical_backend.py` | 기존 host adapter(`wiki_compile_worker.run_extractor`)로 가는 다리. 호출 전 비용을 예약하는 attempt 원장과 불변 pass attestation을 둔다 | `_attempt`(:87), `stage_generation_response`(:175), `verification_payload`(:245), `validate_verification_response`(:269), `publish_pass_attestation`(:313), `run_generation_backend`(:355), `run_verification_backend`(:396), `run_verification_bundle_backend`(:442, 테스트에서만 호출) |
| `core/wiki_topical_reconcile.py` | 원문 snapshot·작업 queue·카드 역투영(projection)을 원자적 상태 파일 하나로 관리. 모델은 호출하지 않는다 | `_scan`(:64), `_projection`(:154), `turn_key`(:191), `reconcile`(:209), `enqueue_hook_hint`(:336), `start_batch`(:414), `finish_backend_batch`(:522), `finish_created_batch`(:590), `rollback`(:640) |
| `core/wiki_topical_create.py` | 카드가 없는 새 원문 1개 → 카드 N장 생성. journal로 복구할 수 있다 | `create_one`(:41), `_create_locked`(:64) |
| `core/wiki_topical_refresh.py` | stale 카드 1장을 교체 카드 1장으로 갱신. 자동 정책을 읽고 다음 작업 1건을 고른다 | `refresh_one`(:163), `recover_pending`(:345), `read_auto_policy`(:362), `auto_refresh_pending`(:399) |
| `core/wiki_topical_publish.py` | attestation 재검증 → 유사성 게이트 → `verified` stamp → `.auto-context/wiki/topical-v2/<gid>/<cid>.md`에 배타 link. 퇴역(retire)과 격리 QMD sync를 담당한다 | `publish`(:181), `retire_stale`(:245), `_qmd_runtime`(:332), `sync`(:362) |
| `core/wiki_topical_similarity.py` | 격리 QMD `vsearch`로 후보를 찾고, 사람 검토 파일의 `distinct` 판정 없이는 통과시키지 않는다 | `retrieve`(:55), `evaluate`(:125), `_review`(:26) |
| `core/wiki_topical_selection.py` | exact identity(사실+출처 바이트 동일)만 중복으로 보는 선택기. reconcile이 `claim_identity`를 쓴다 | `claim_identity`(:20), `exact_card_identity`(:39), `select`(:53) |
| `core/topical_hook_queue.py` | Stop/SessionStart job을 durable하게 기록하고 detached worker를 실행한다 | `enqueue`(:150), `_run`(:195), `_drain_auto`(:71), `worker`(:261) |
| `core/topical_stop_budget.py` | 턴 단위 예산 원장(v3, 턴별 shard). v1/v2 단일 원장은 이 형식으로 이행한다 | `Budget`(:283), `reserve`(:333), `finish`(:361), `_migrate`(:185) |
| `core/wiki_topical_fake_pipeline.py`, `_fake_qmd.py`, `_fake_turn_worker.py` | 합성 전용 경로. `.qmd-topical-fake-only` marker가 필수다(`core/wiki_topical_fake_pipeline.py:34-35`) | `drain`, `sync_qmd`, `run`(`core/wiki_topical_fake_turn_worker.py:11`) |
| `core/wiki_mutation_lock.py` | 프로젝트 wiki 변경용 재진입 flock(`<root>/.topical-publish.lock`) | `lock`(:33), `guard_path`(:64), `for_path`(:27) |
| `core/wiki_init.py` | `update.sh --init-wiki`의 inline heredoc을 모듈로 분리. lock을 잡고, scaffold 파일을 원자 게시하며, 미완성 prefix를 거부하고, `initial_config`(recommended opt-in)를 지원한다 | `ensure_project_file`(:16), `init_wiki`(:60) |

### 기존 파일 diff

- `core/wiki_compile.py`: `write_text_atomic`·`rewrite_generated_card`·`insert_source_revisions`·`patch_frontmatter_fields`·`stamp_verification`·`update_index`·`remove_from_index`·`append_log`에 `@wiki_mutation_lock.guard_path`를 붙였다. `main`이 `card_write_lock`에 이어 `wiki_mutation_lock.lock(root)`를 잡는다(`core/wiki_compile.py:1448`). `index.md`/`log.md` 최초 생성은 `ensure_project_file`로 바뀌었고 `enqueue_collections`는 `enqueue_project_collections(root, …)`로 바뀌었다.
- `core/wiki_verify_worker.py`: verdict 적용을 `wiki_mutation_lock.lock(root)` 아래로 옮겼다(`core/wiki_verify_worker.py:851`). dirty enqueue도 프로젝트 단위로 바뀌었다.
- `core/wiki_dedup_resolve.py`: `resolve_entry` 전체를 `wiki_mutation_lock.lock(root)`로 감쌌다(:106-108).
- `core/wiki_compile_worker.py`: `main`이 `setup_guard.status(cwd) != 'ready'`이면 바로 `return 0` 한다(:1027-1029). 기존 compile 경로에 setup gate가 추가된 것이다.
- `core/extractors/lib.py`: topical task 3종(`generate_topical_candidates_sandbox_only`, `verify_topical_claims_sandbox_only`, `verify_topical_bundle_sandbox_only`)에 대응하는 prompt builder와 JSON 추출기를 추가했다(:261-394, :587-657). `record_topical_raw_response`는 raw stdout·stderr·prompt를 credential-redaction한 뒤 `topical-backend-audit/`에 남긴다. **공통 경로 변화**도 있다: `run_isolated_detailed`가 timeout 시 `(None, 1)` 대신 `(partial_stdout, 124, partial_stderr)`를 반환하고 stderr를 4000자로 자르지 않는다(:491-497). 기존 extract/verify/dedup에서는 adapter exit code가 1에서 124로 바뀌지만, 소비자 셋 모두 127만 특별 취급하고 나머지 비0은 `extractor_failed`로 같게 다루므로 분기는 같다(`core/wiki_compile_worker.py:609-610,835`, `core/wiki_verify_worker.py:776,780`, `core/wiki_dedup_judge.py:415,427`. 1이나 124를 따로 보는 분기는 없다). 부수 변화가 하나 있다: timeout 때 stdout이 `None`이 아니라 부분 출력이고 stderr도 부분 출력이 넘어가므로, effort 재시도 조건(`code != 0 and code != 127` + 거부 패턴, `core/extractors/lib.py:611-618`)이 timeout에서도 평가된다. 부분 stderr가 거부 패턴과 맞으면 두 번째 CLI 실행이 가능하다(코드 추론, 발생 실측 없음).

## 3. 제어·데이터 흐름

### hook 등록 (Claude/Codex 공통 구조)

- SessionStart: `update-queued` 다음에 `topical-reconcile`(timeout 3s)이 온다(`hooks/hooks.json:5-6`, `hooks/hooks-codex.json` 동일).
- PostToolUse: 기존 posttool/index/compile 뒤에 `topical-event`(timeout 5s)가 추가됐다(`hooks/hooks.json:24`).
- Stop(신규 이벤트): `topical-stop`(timeout 3s)이다(`hooks/hooks.json:27-28`).

### 디스패처 gate (`hooks/run-hook`)

1. sandbox/headless 상태이면 바로 종료한다. Stop은 반드시 `{}`를 출력한다(:28-31).
2. 플러그인 루트에 `.qmd-local-auto-compile-disabled`가 있으면 `QMD_AUTO_COMPILE_DISABLED=1`을 export한다. 기존 compile만 막고 topical에는 영향이 없다(:42).
3. `setup_guard.py hook-root`가 프로젝트를 고른다. `QMD_TOPICAL_PROJECT_ROOT`/`QMD_TOPICAL_SANDBOX_ROOT` env가 우선하고, 없으면 payload `cwd`를 쓴다(`core/setup_guard.py:69-80`). setup 상태가 `ready`이고 `wiki_topical.opted_in(root)`이 참일 때만 경로를 출력한다(`core/setup_guard.py:92-96`). 그 밖의 경우 hook은 무출력으로 끝난다(:64-68).
4. 분기(:84-105):
   - `topical-event`: `<root>/.topical-reconcile-hook.json`이 **있을 때만** `wiki_topical_reconcile.py event`를 실행한다. 이 파일은 sandbox용 레거시 설정이다. 자동 정책만 둔 일반 opt-in 프로젝트에서는 **이 hook이 아무 일도 하지 않는다**. 실제 변경 검출은 Stop의 전체 scan이 맡는다.
   - `topical-stop` → `topical_hook_queue.py boundary <root>`, `topical-reconcile` → `topical_hook_queue.py reconcile <root>`. 둘 다 `|| true`이고 stdout은 버린다.

### Stop → detached worker → 게시

```
Stop hook (3s) ── topical_hook_queue.enqueue
   │  setup_guard ready? / auto policy enabled:false면 아무것도 안 만듦 (:153-160)
   │  정책 파일이 없거나 무효여도 job·worker는 만든다 (:161-173) → _run이 pending_review로 끝냄
   │  .topical-hook-jobs/<uuid>.json {action, turnKey}  (원문·대화 본문 미포함)
   └─ Popen(start_new_session) worker ─────────────────────────────┐
                                                                    ▼
worker (.topical-hook-jobs/.worker.lock LOCK_NB, 최대 64 job/wake)
 └ _run: policy 검사 → reconcile.start_batch (전체 _scan → queue → inFlight batch)
    └ _drain_auto: Budget(turnKey, snapshot) 잠금 아래 반복 (최대 2*12+4회)
       ├ budget.reserve(state)  ← 모델 호출 전에 fsync 기록
       ├ refresh.auto_refresh_pending  → 작업 1건 선택
       │    ├ refresh journal 있음 → refresh_one (이어서 진행)
       │    ├ create journal 있음 → create_one
       │    ├ excluded_stale 카드 있음 → refresh_one(가장 앞 카드)
       │    └ claim 안 된 새 원문 있음 → create_one
       └ budget.finish(before, after)
 결과 → topical-hook-status.json, topical-hook-worker.log (1MiB 회전)
```

SessionStart `topical-reconcile` job(`_run`, :236-248): 정책이 있으면 `reconcile.reconcile` 후 `_drain_auto(turn_key=None)`을 실행한다. 턴 키는 active 포인터의 턴을 재사용하고, 포인터가 없으면 `'recovery'`를 쓴다(`core/topical_stop_budget.py:304-305`). 즉 **SessionStart도 유료 호출을 할 수 있고**, 직전 턴과 예산을 공유한다. 정책이 없으면 `refresh.recover_pending`만 실행한다. 이 함수는 기록된 응답을 재사용할 뿐 새 외부 호출은 하지 않는다(`core/wiki_topical_refresh.py:345-359`).

marker는 있고 `.topical-auto-refresh.json`이 **없는** 프로젝트(02 문서 기준 setup shadow 경로가 이 marker를 요구한다)에서는 Stop마다 `enqueue`가 job 파일과 detached worker를 만들고(`core/topical_hook_queue.py:161-173`), `_run`이 `pending_review: auto_teacher_policy_required`로 끝낸다(:234-235). 모델 호출은 없지만 Stop마다 프로세스 1개와 `.topical-hook-jobs/`·`topical-hook-status.json`·`topical-hook-worker.log`가 생긴다.

### 단계별 책임

| 단계 | 담당 | 결과물 |
|---|---|---|
| reconcile | `wiki_topical_reconcile.reconcile`/`start_batch` | `topical-reconcile-state.json`(sources, settledSources, queue, projection, inFlight). 이전 revision은 `topical-reconcile-history/<rev>.json`에 보관. 원문 하나가 바뀌면 그 원문을 인용한 모든 카드가 `excluded_stale` 또는 `excluded_pending_refresh`가 된다(:154-176, :274-284) |
| create | `wiki_topical_create._create_locked` | journal `topical-create-state.json`의 phase: claimed → pre_retrieved → (generation_failed_wait) → generated → verified → (awaiting_similarity) → published → qmd_synced. 끝나면 `finish_created_batch`가 settledSources를 전진시킨다 |
| refresh | `wiki_topical_refresh._refresh_locked` | journal `topical-refresh-state.json`의 phase: claimed → pre_retrieved → generated → verified → similarity_clear/awaiting_similarity → new_published → old_retired → qmd_synced. 교체 카드는 **정확히 같은 cardId 1장**이어야 한다(:295-296). 원문이 모두 사라지면 생성 없이 퇴역만 한다(:276, :329) |
| generation | `backend.run_generation_backend` | `topical-generations/<gid>/{manifest.json, cards/<cid>.md, cards/<cid>.evidence.json}`(0700/0600, 불변, gid는 내용 해시 앞 24자). 카드 status는 `generated`이고 `schemaVersion: 2`(`core/wiki_topical.py:344-351`) |
| verification | `backend.run_verification_backend` | `topical-attestations/backend/<gid>/<cid>.json`(pass일 때만, 배타 link라 덮어쓰지 않음). 실패·불확실은 `topical-backend-audit/*.attempt.json` 상태로만 남는다 |
| similarity | `similarity.retrieve`/`evaluate` | `topical-similarity-review.json`(사람이 쓰는 owner-only 파일)에 쌍 해시별 `distinct`가 없으면 `pending_review: similarity_unresolved` |
| publish | `publisher.publish` | `.auto-context/wiki/topical-v2/<gid>/<cid>.md`(verified stamp), `topical-publication-proofs/<gid>/<cid>.json` |
| retire | `publisher.retire_stale` | 기존 페이지를 `topical-retired/<gid>/<cid>.md`로 옮긴다(wiki collection 밖, 되돌릴 수 있음) |
| QMD sync | `publisher.sync` | 프로젝트 전용 runtime(`qmd_route.project_paths(isolated=True)`)에서 `collection add`/`update` → `embed -c` → (`cleanup`) 실행. 그 뒤 SQLite에서 활성 문서 hash와 벡터 존재를 확인한다(:388-417) |

## 4. host별 영향

- **Claude Code**: SessionStart·PostToolUse·Stop 세 곳에 topical hook이 등록돼 있다(`hooks/hooks.json`). Stop hook은 opt-in 여부와 관계없이 프로젝트마다 실행된다. opt-in이 없으면 `setup_guard` 단계에서 `{}`만 출력하고 끝난다. turnKey는 `claude:` + sha256(session_id + last_assistant_message)의 앞 24자다(`core/wiki_topical_reconcile.py:191-202`).
- **Codex**: 등록 구조는 같다(`hooks/hooks-codex.json`). turnKey는 `codex:<turn_id>`다. 실제 Codex 세션에서 Stop hook 실행은 관측되지 않았고, 로컬 dispatcher를 직접 실행한 경우만 확인됐다(문서 근거, `docs/operational-flow-map-20261006.md:69`). 자동 정책의 engine은 **`codex`만 허용**된다(`core/wiki_topical_refresh.py:383`). 따라서 Claude 세션의 Stop도 **생성**은 `compileCfg`가 `codex`로 해석하는 argv로 수행한다(`core/wiki_topical_backend.py:374`). **검수**는 Codex 고정이 아니다: `plan_verify_attempts(compile_cfg, vcfg, 'codex')`의 첫 후보 하나만 쓰고(`core/wiki_topical_backend.py:408-414`), 기본 `crossEngine: prefer`는 생성 엔진을 순서의 **마지막**에 둔다(`core/wiki_verify_worker.py:108-113,153`). 검수 풀에 claude 등 다른 엔진이 있으면 그 엔진이 검수하고, 없을 때만 codex가 검수한다. 같은 run 안의 127 폴백은 없다.
- **Hermes**: `hermes_adapter/`에 topical 매핑이 없다(grep 0건). 이 host에서는 topical 파이프라인이 실행되지 않는다. 다만 다른 host에서 게시된 topical-v2 카드는 같은 wiki collection에 있으므로 Hermes recall이 주입할 수 있다(추정). 그 경우에도 Hermes `pre_llm_call`은 `_setup_allowed`(`setup_guard check`, 2초 상한, 실패·timeout이면 무동작)를 통과해야 한다(`hermes_adapter/core_bridge.py:72-83,211`).
- **Antigravity/Gemini**: topical 연동이 없다. 이 저장소는 Gemini용 hook을 등록하지 않는다(root `plugin.json`에 hooks 없음, `core/agy_local_install.py`는 stale qmd hook 정리만 한다). `run-hook`의 gemini sandbox 가드는 사람이 `run-hook … gemini`를 직접 연결했을 때만 의미가 있다.

## 5. 신규 설정·env·파일 경로

### opt-in·정책 파일 (프로젝트 루트, 사람이 직접 만든다. 이 파일을 만드는 skill이나 setup 코드는 없다(grep 근거))

- `.qmd-topical-v2-project`: 운영 opt-in. 일반 파일이고 owner uid이며 mode `& 0o077 == 0`(0600)이어야 한다(`core/wiki_topical.py:35-39`).
- `.qmd-topical-sandbox`: 합성 sandbox opt-in. 일반 파일이기만 하면 되어 조건이 더 느슨하다(:32-34). raw adapter stdout 감사도 이 marker가 있을 때만 남는다(`core/extractors/lib.py:367`).
- `.topical-auto-refresh.json`: 자동 정책. 키는 정확히 `{schema:"qmd-topical-auto-refresh-v1", enabled:bool, sourceRoots:[…], engine:"codex", compileCfg:{…}, maxEstimatedCents:20..160}`이고 0600이어야 한다(`core/wiki_topical_refresh.py:362-396`).
- `.topical-reconcile-hook.json`: sandbox용 레거시 설정 `{sourceRoots, cardsFile, trustedCardIds, skipPaths}`. 자동 정책이 있으면 sourceRoots가 일치해야 한다(`core/topical_hook_queue.py:177-207`).
- `topical-similarity-review.json`: 사람의 유사성 판정 `{schema:"qmd-topical-similarity-review-v1", decisions:[{pairSha256, verdict, reviewerId, reason}]}`.
- `.qmd-topical-fake-only`: fake 경로 전용 marker.

### env

- `QMD_TOPICAL_PROJECT_ROOT`, `QMD_TOPICAL_SANDBOX_ROOT`: hook 프로젝트를 명시적으로 지정한다(`core/setup_guard.py:71`).
- `QMD_TOPICAL_FAKE_AUTODRAIN=1`: `.qmd-topical-fake-only`와 함께 있으면 운영 코드인 `topical_hook_queue._run`이 fake turn worker를 실행한다(`core/topical_hook_queue.py:246,249-257`). 운영 코드 안에 들어 있는 테스트 경로다.
- `QMD_TOPICAL_SYNTHETIC_RUNTIME=1`: `qmd_route.project_paths(fixture_env=…)`(`core/wiki_topical_publish.py:335`).

### 상태·원장·락 (프로젝트 루트, 대부분 0700/0600)

`topical-reconcile-state.json`, `topical-reconcile-history/`, `.topical-reconcile.lock`, `topical-generations/<gid>/`(+`.commit.lock`), `topical-attestations/{backend,mock}/<gid>/<cid>.json`, `topical-backend-audit/`(`<kind>.<reqsha>.<attemptId>.attempt.json`, `<reqsha>.generation.json`/`.verification.json`, sandbox에서만 `*.raw-stdout.txt` 등), `topical-create-state.json`, `topical-refresh-state.json`, `.topical-refresh.lock`(create/refresh 공용, LOCK_NB), `topical-publication-proofs/`, `topical-retired/`, `.topical-hook-jobs/`(+`.worker.lock`), `topical-hook-status.json`, `topical-hook-worker.log`(+`.1`), `.topical-stop-budgets/<sha(turnKey)>.json`, `.topical-stop-budget-active.json`, `.topical-stop-budget.lock`, `.topical-stop-budget.legacy.json`(이행 archive), `.topical-publish.lock`(wiki mutation lock).

게시 카드: `.auto-context/wiki/topical-v2/<gid>/<cid>.md`.

## 6. main 대비 동작 변화

### 기존 per-source compile과의 관계: 병행이며 대체가 아니다

- 기존 `wiki_compile_worker` 경로(PostToolUse `compile` → compile 큐 → extractor → `wiki_compile` → verify worker)는 그대로 남아 있다. topical opt-in이 이 경로를 끄는 코드는 없다(`wiki_compile_enqueue.py`·`wiki_compile_worker.py`·`wiki_compile.py`·`wiki_verify_worker.py`·`index_enqueue.py`·`posttool.py`·`update.sh`·`backend_manager.sh`에서 `topical`/`opted_in` grep 0건). 결합은 반대 방향에 있다: 설정이 있는 기존 프로젝트가 기존 compile을 계속 쓰려면 setup `ready`(pointer)가 필요하고(`core/wiki_compile_worker.py:1027-1029`), pointer를 만드는 shadow 경로는 같은 v2 marker를 요구한다(`core/install_update.py:572-573`, `core/runtime_update.py:100-101,218-219`, 02 문서). 기존 compile을 막는 것은 플러그인 루트의 `.qmd-local-auto-compile-disabled`(local trial 설치, `hooks/run-hook:42`, `core/backend_manager.sh:461`) 하나이고, 이 marker는 프로젝트 단위가 아니다.
- 차이점:
  - 입력: 기존은 편집된 `.md` 1개를 `maxSourceChars`로 잘라 쓴다. topical은 `sources/` 아래 원문 전체를 줄 번호와 함께 넣고 최대 3개, 1MiB까지 받는다(`core/wiki_topical.py:44,:157`, `core/wiki_topical_experiment.py:41`).
  - 카드: 기존은 auto 블록 요약이다. topical은 claim 단위(`state ∈ plan/actual/observation/unknown/rule`, timeScope, condition)이고 모든 claim에 줄 범위와 축자 quoteAnchor 증거가 붙는다. quote SHA·원문 SHA는 컴파일러가 계산한다(`core/wiki_topical_backend.py:206-211`). lead는 claim 줄을 이어 만들고 600자를 넘으면 `semantic_split_required`로 거부한다(`core/wiki_topical.py:303-305`).
  - 트리거: 기존은 편집 직후 PostToolUse이고, topical은 턴 경계(Stop)와 SessionStart 복구다.
  - dedup: 기존은 LLM judge와 큐다. topical은 LLM을 쓰지 않고 사람 `distinct` 판정만 받는다.
  - 삭제·갱신: 기존은 verify fail 시 카드를 삭제한다. topical은 퇴역 archive로 옮겨 되돌릴 수 있게 하고 교체 카드를 게시한다.
- 같은 wiki collection(`.auto-context/wiki`)을 공유한다. 게시 전제 조건은 `collectionPaths`에 `.auto-context/wiki`가 정확히 하나 있고, role이 `wiki`이며, `recallStrategy ∈ {wikiOnly, hierarchical}`, `indexing: true`인 것이다(`core/wiki_topical_publish.py:41-58`).

### trust 경계와의 관계

- staged 카드는 `topical-generations/`(wiki collection 밖)에 `status: generated`로만 존재하므로 recall에 노출되지 않는다.
- 검수 근거는 카드 밖에 있다. 근거는 세 겹이다: ① 불변 backend attestation(카드 markdown SHA, evidence SHA, sourceRevisions SHA, request/response SHA에 결속, `core/wiki_topical_backend.py:302-310`), ② publication proof(`core/wiki_topical_publish.py:105-151`), ③ reconcile projection.
- pass 판정 기준은 기존보다 엄격하다. 모든 (claimId, sourcePath, quoteSha256) span에 대해 check가 정확히 하나씩 있어야 하고, quoteAnchor가 원문과 축자 일치해야 한다. verdict는 `false` 하나라도 있으면 fail, `null`이 있으면 inconclusive, 전부 true일 때만 pass로 도출되며 모델이 낸 verdict와 같아야 한다(`core/wiki_topical_experiment.py:160-183`, `core/wiki_topical_backend.py:287-299`). 기존 verify의 "claims>0이면 pass 인정"보다 강하다.
- 게시할 때 기존 trust 필드를 그대로 채운다. `wiki_compile.markdown_page`가 `createdBy: qmd-auto-context`와 `sourceRevisions`(collection 리터럴 `sandbox-source`, `core/wiki_topical.py:186`)를 쓰고, `wiki_compile.stamp_verification(stage, "verified", engine, VERIFIED_MODE_UNKNOWN, card_body_hash)`가 status, `verifiedBy`, `verifiedAt`, `verifiedMode=unknown`, `verifiedBodyHash`를 한 번에 쓴다(`core/wiki_topical_publish.py:195,:215-217`). 따라서 **recall은 topical 전용 분기 없이**(`git diff main...HEAD -- core/recall.py`에 `topical` 0건. recall.py 자체는 hook budget·context learning으로 바뀌었다) 이 카드를 `is_auto_trusted_card` 통과 카드로 보고 `wiki:verified`로 주입한다(`test/fixtures/wiki-topical-publish-e2e.py:95-99`). recall의 freshness 대조는 `sourceRevisions[].path`를 프로젝트 루트 기준으로 resolve하며 collection 값은 보지 않는다(`core/recall.py:2523-2541`). 원문이 바뀐 뒤 주입이 차단되는 것은 테스트로 확인돼 있다(`test/fixtures/wiki-topical-publish-e2e.py:102-106`).
- `verifiedMode`는 실제 cross/self 여부와 관계없이 항상 `unknown`이다(`core/wiki_topical_publish.py:216`).

### 사용자 관점과 호환성

- opt-in marker와 정책이 없는 프로젝트에서는 Stop hook이 추가로 실행되지만 `setup_guard`에서 무출력으로 끝난다. 동작 변화는 없다.
- 기존 compile·verify·dedup-resolve·init-wiki를 실행하는 **모든 프로젝트**의 루트에 `.topical-publish.lock`(0600)이 생긴다(`core/wiki_mutation_lock.py:48-49`). topical opt-in과 무관하고 `.gitignore` 처리 코드도 없다.
- `--init-wiki` scaffold는 이제 기존 파일을 덮어쓰지 않는다. 이전 비원자 writer가 남긴 미완성 prefix가 있으면 `incomplete wiki scaffold preserved` 오류로 중단한다(`core/wiki_init.py:25-35`).
- 기존 compile worker는 setup 상태가 `ready`가 아니면 아무것도 하지 않는다(`core/wiki_compile_worker.py:1027-1029`). 기존 프로젝트는 setup을 다시 하기 전까지 자동 compile이 멈춘다(setup 영역).

### 유료 호출 상한

| 단위 | 상한 | 근거 |
|---|---|---|
| 모델 응답 카드 수 | ≤12/응답 | `core/wiki_topical_backend.py:185` |
| 검수 1회의 원문 수 | ≤3 | `core/wiki_topical_backend.py:249` |
| attempt 원장(요청 SHA별) | `max_attempts` 1..3, 추정 1..1000센트. 같은 attemptId는 completed 응답만 재사용하고, reserved나 uncertain 상태는 **자동 재호출하지 않는다** | `core/wiki_topical_backend.py:96-157` |
| create 1회 | 생성 1회(+실패가 명확하고 정책 예산이 30센트 이상이면 `retry1` 1회) + 카드당 검수 1회. `10×(생성시도+카드수) > maxEstimatedCents`이면 검수 전에 `pending_review`. 최대 1~2+12 ≈ 14회 | `core/wiki_topical_create.py:122-171` |
| refresh 1회 | 생성 1 + 검수 1 = 2회. retry 없음 | `core/wiki_topical_refresh.py:112-160` |
| 턴(turnKey) 단위 | 작업 ≤12, Σ정책 `maxEstimatedCents` ≤640. job·snapshot·SessionStart 복구·동시 worker가 같은 턴 누계를 공유한다. 예약은 모델 호출 전에 fsync한다. 진행(queue·settledSources)이 바뀌지 않은 미완료 작업은 기존 예약을 재사용하고 새 작업을 세지 않는다(`:338-341`). 그 재시도의 중복 호출은 attempt 원장이 막는다 | `core/topical_stop_budget.py:20-21,:333-359` |
| 정책 1작업당 추정액 | 20..160센트 | `core/wiki_topical_refresh.py:386-387` |

- 도출되는 최악값(계산, 추정): 작업 수는 min(12, ⌊640/c⌋)이고 작업당 호출은 ≲ min(14, c/10)이다. 따라서 턴당 약 56~64회가 상한이다(c=80일 때 8작업×8회). 생성 호출은 카드 수 검사 **전에** 이미 지불되므로 검수만 상한으로 막힌다.
- "센트"는 로컬 예약 단위일 뿐 공급자 과금 한도가 아니다(`core/wiki_topical_backend.py:91-95`).
- 턴 수에는 상한이 없다. 새 정상 턴은 새 예산을 받는다(문서 근거, `docs/operational-flow-map-20261006.md:73`).
- 묶음 검수(`run_verification_bundle_backend`, 원문 1개의 모든 카드를 호출 1회로 검수)는 테스트에서만 호출된다. 운영 경로는 카드마다 검수한다.

### fake / experiment 모드 분류

- **테스트 전용**: `wiki_topical_fake_pipeline`/`_fake_qmd`/`_fake_turn_worker`(`.qmd-topical-fake-only` 필수), `experiment.render_hook`/`invoke`/CLI, `reconcile.finish_mock_batch`, mock attestation(`mode: mock`).
- **운영 경로가 import하는 "experiment"**: `require_sandbox`, `generation_contract`, `load_staged_card`, `mock_semantic_verdict`(backend verdict 도출에도 쓰인다). 이름과 실제 역할이 맞지 않는다.
- 운영 코드 안의 fake 분기: `topical_hook_queue._run`의 `QMD_TOPICAL_FAKE_AUTODRAIN` 분기(:246-257). env와 marker 두 가지가 모두 있어야 동작한다.

## 7. 테스트 커버리지

| 테스트 | 고정하는 것 |
|---|---|
| `test/wiki-topical-experiment.test.mjs` (11) | 생성 계약, 다중 topic 카드, mock pass/inconclusive/fail 구분, lead 무절단 예산, plan/actual 분리, 원문 drift 재검사, 위조된 sidecar 거부, 실패 시 이전 generation 보존, 레거시 마이그레이션 dry-run 분류 |
| `test/wiki-topical-backend.test.mjs` (11) | attestation이 카드·원문 바이트에 결속되는지, 비활성 transport가 호출을 0회 하는지, attempt ID·예산, 빈 응답의 명시 실패, uncertain 재호출 금지, Codex adapter의 topical task 라우팅, 묶음 검수, raw stdout 보존 |
| `test/wiki-topical-reconcile.test.mjs` + `test/wiki_topical_reconcile_cases.py` (21) | 다중 출처 stale 투영, 개명·삭제 coalesce, apply_patch payload, 턴 crash 재개·rollback, detached worker의 부모 kill 생존, 자동 정책 Stop 실행, disabled·invalid 정책이 batch를 만들지 않는지, 중간 실패 복구, 동시 enqueue, **턴 예산 공유(중복 Stop·정책 변경·동시 worker)**, 레거시 예산 원장 이행, 무작업 9,000턴/유료 10,000턴 shard 크기 |
| `test/wiki-topical-selection.test.mjs` | exact identity만 dedup, 상태·조건·시점·출처 차이 보존, 예산·top3 |
| `test/wiki-topical-fake-pipeline.test.mjs`, `test/wiki-topical-fake-qmd.test.mjs` | fake 경로 hook → publish → 검색, crash 복구, opt-in이 없으면 무동작, fake QMD 증분 embed |
| `test/wiki-topical-multicard-settlement.test.mjs`, `test/wiki-topical-refresh-recovery.test.mjs` | 한 카드 refresh가 다른 카드의 pending을 지우지 않는지, uncertain 호출을 반복하지 않는지 |
| `test/wiki-topical-publish.test.mjs`, `-refresh-backend`, `-similarity`, `-two-card-qmd` | 실제 격리 QMD 상태에서 게시 → 기존 recall 주입, 다중 출처 refresh·퇴역, 유사성 보류, 두 카드 독립 정산. **실제 qmd와 오프라인 모델이 없으면 `t.skip`** |
| `test/wiki-topical-create-hook.test.mjs`, `-create-races`, `-stop-batch` | detached hook → fake teacher → 게시, 수정·삭제 경합, Stop 한 번으로 신규 2건 처리와 4-way 혼합 처리. **QMD가 없으면 `result.skipped`로 단정이 통과**(skip이 아니라 green) |
| `test/wiki-mutation-lock.test.mjs` | compile·verify writer가 cutover lock을 공유하고 self-deadlock이 없는지 |
| `test/wiki-optin-cutover.test.mjs` | recommended opt-in이 scaffold 동안 lock을 유지하는지, 실패·kill 후 재시도, 미완성 scaffold 보존 |
| `test/local-trial-compile-gate.test.mjs` | `.qmd-local-auto-compile-disabled`가 기존 compile과 SessionStart의 compile kick을 막는지 |

## 8. 위험·미해결·TODO

1. **원문은 `sources/` 아래만 허용된다.** `safe_source`는 첫 경로 segment가 문자 그대로 `sources`가 아니면 `unsafe_source_path`로 거부한다(`core/wiki_topical.py:157`). 그런데 `read_auto_policy`는 임의의 sourceRoots(예: `docs`)를 받아들인다(`core/wiki_topical_refresh.py:389-395`). reconcile의 `_scan`은 `safe_rel`만 검사하므로(`core/wiki_topical_reconcile.py:81`) batch는 claim되지만, 이어지는 `source_snapshot`/`generation_contract`가 매번 실패한다. create 경로에서는 이 예외가 `_create_locked`(`core/wiki_topical_create.py:111`)→`create_one`→`auto_refresh_pending`→`_drain_auto` 어디에서도 잡히지 않고 worker의 `except Exception`에서 `failed:TopicalError:…` 상태가 된다(`core/topical_hook_queue.py:309-310`). 실패한 job 파일은 지워지지 않아(`path.unlink()`는 `_run` 성공 뒤, :306-307) 다음 wake에서 같은 실패를 반복한다. `test/` 아래 `sourceRoots` 리터럴 16곳 중 15곳이 `['sources']`이고, `['other']` 1곳은 불일치 거부 테스트다(grep 근거).
2. **유사성 게이트는 사람 검토가 사실상 필수다.** 게시할 때 다른 모든 fresh v2 카드가 후보가 되고(`core/wiki_topical_publish.py:77-98`), 하나하나 사람의 `distinct` 판정이 있어야 한다. 그래서 2번째 카드부터는 자동으로 끝나지 않는다(`test/fixtures/wiki-topical-two-card-qmd-e2e.py:107`). 다른 fresh 카드가 8장을 넘으면 `similarity_review_budget_exceeded`로 새 게시가 막힌다(`core/wiki_topical_publish.py:97-98`, `core/wiki_topical_similarity.py:118-119`). **이 상한은 테스트가 없다.** 또 vsearch 결과에 topical-v2가 아닌 기존 wiki 카드가 하나라도 있으면 `similarity_unreviewed_wiki_hit`로 보류된다(`core/wiki_topical_similarity.py:84-91`). `retrieve`는 `--min-score`를 넘기지 않으므로 qmd 2.5.3 `vsearch`의 기본값 0.3이 걸린다(로컬 설치본 `~/node_modules/@tobilu/qmd/dist/cli/qmd.js:3945-3947`, 저장소 밖 근거). 따라서 질의(원문 앞 600자 또는 카드 lead)와 점수 0.3 이상인 기존 비topical wiki 문서가 하나라도 있으면 생성·refresh가 보류된다. `index.md`·`log.md` 같은 scaffold 문서도 같은 collection에 있으면 이 판정 대상이다(코드 추론). 또 vsearch 결과가 `SEARCH_LIMIT`(32)개 이상이면 `similarity_search_unavailable`로 실패한다(`core/wiki_topical_similarity.py:22,70-71`).
3. **PostToolUse `topical-event` hook의 fail-safe 결함.** `.topical-reconcile-hook.json`이 있는데 reconcile 상태가 아직 없으면 `enqueue_hook_hint`가 `ValueError("reconcile_baseline_required")`를 던진다(`core/wiki_topical_reconcile.py:361-363`). `main`은 `hook_main.run`을 거치지 않고(`core/wiki_topical_reconcile.py:735-736`) `run-hook`이 stdout만 버리고 `|| exit $?`로 non-zero를 그대로 전달하므로(`hooks/run-hook:92-94`) host에 "hook failed"가 표시된다. 격리 임시 HOME에서 sandbox marker + 이 설정 파일 + 상태 없음으로 `run-hook topical-event claude`를 실행하면 traceback이 stderr로 나오고 exit 1이다(실측). 같은 경로의 다른 예외(`invalid_sandbox_settings`, sourceRoots 불일치, payload JSON 오류)도 같은 방식으로 새어 나간다(코드 추론). CLAUDE.md의 "모든 Python hook entrypoint는 hook_main 경계" 규칙에서 벗어난다. 영향 범위는 sandbox용 설정 파일이 있는 프로젝트로 한정된다.
4. **기존 wiki 도구가 topical-v2 카드를 구분하지 않는다.** `wiki_dedup_scan`, `wiki_dedup_resolve`, `wiki_source_scan`에는 topical 관련 처리가 없다(grep 0건). 기존 dedup resolver가 topical-v2 카드를 병합하거나 수정하면 `verifiedBodyHash`가 어긋나 recall에서 빠진다(fail-closed). 그 결과 publish proof나 reconcile projection과 디스크 상태도 갈라진다(추정, 코드 경로 근거).
5. **`wiki_mutation_lock.guard_path`는 경로에 `.auto-context/wiki`가 들어 있을 때만 lock을 잡는다**(`core/wiki_mutation_lock.py:19-29`). 커스텀 `wikiPath`에서는 조용히 `nullcontext`가 된다. compile·verify·dedup-resolve는 명시적 `lock(root)`도 잡으므로 영향은 다른 호출자(예: source repair)로 한정된다(추정).
6. **raw adapter 출력 감사는 sandbox marker에서만 남는다.** 운영 opt-in(`.qmd-topical-v2-project`)에서는 `*.attempt.json`의 파싱된 `adapterResponse`만 남고 raw stdout·stderr·prompt는 남지 않는다(`core/extractors/lib.py:367-368`).
7. **실환경 커버리지 공백.** 실제 QMD가 필요한 e2e 7개는 QMD가 없으면 skip되거나 통과로 처리된다. 일부는 개발 머신 경로(`/Users/dulee/work/.qmd-tools/bin/qmd`)에 의존한다(`test/fixtures/wiki-topical-two-card-qmd-e2e.py:21`). Codex host의 Stop 자동 실행은 관측되지 않았다. Claude host 재실행은 수정 후 미검증으로 남아 있다(문서 근거, `docs/operational-flow-map-20261006.md:59,:69`).
8. **opt-in 경로에 UI가 없다.** `.qmd-topical-v2-project`, `.topical-auto-refresh.json`, `topical-similarity-review.json`을 만드는 skill이나 setup 코드가 없다(grep 근거). 사용자가 스키마를 알고 0600 파일을 직접 써야 한다.
9. **프로젝트 루트에 산출물이 많이 생긴다.** opt-in 프로젝트 루트에 상태·원장 파일과 디렉터리가 15개 이상 생기고, `.gitignore`를 처리하는 코드는 없다.
10. 턴 수에는 총량 상한이 없다. `'recovery'` 턴 키는 영구히 쓰이는 단일 bucket이라 한 번 소진되면 이후 active 포인터 없이 도는 SessionStart 복구 job은 새 Stop 턴 전까지 호출하지 않는다(`core/topical_stop_budget.py:304-305`). 의도인지는 문서에 없다.

# context learning (로컬 학습) — main ↔ feat/v2-pre-release-validation

범위: `core/context_learning/**`, `core/context_learning_cli.py`, `core/context_learning_auto.py`, `docs/continual-learning.md`, `test/context-learning-*.test.mjs`, `test/fixtures/context-learning-*`. 이 영역 전체가 main에 없는 신규 코드다(`git ls-tree main`에서 `context_learning` 0건, main `recall.py`/`config.py`에서 `contextLearning` 0건). 63파일 +7,488줄, 도입 커밋은 `73986ad`("Development branch only. Operational migration, activation, and rollback remain separate.").

## 1. 한 줄 요약

recall이 고른 wiki 카드 후보 풀과 사용자 프롬프트를 opt-in으로 로컬 SQLite에 기록하고, 사람이 검수한 "이 요청에 필요한 카드 0–3장" 정답으로 로컬 Laya 모델(encoder 동결, head만)을 학습·검증·승격한 뒤, `liveSelection`을 켠 프로젝트에서는 QMD 상위 15장 중 0–3장을 그 모델이 고르게 하는 기능이다. `contextLearning` 블록이 없으면 이 영역의 코드는 recall 출력에 관여하지 않는다. 다만 같은 브랜치의 setup_guard(pointer 없는 기존 프로젝트의 hook 정지)·프로젝트 index 라우팅·전역 hook deadline 때문에 recall 출력 자체는 main과 다를 수 있다(01 문서 영역).

## 2. 무엇이 새로 생겼나

### 2.1 무엇을 학습하나

- **모델**: PyPI `laya==0.3.20` 패키지의 `laya.common.build_model`로 만든 모델과 로컬 체크포인트 디렉터리(`model.safetensors`, `rl_agent_config.json`, `encoder/config.json`, `tokenizer/*`)를 쓴다(`core/context_learning/laya_adapter.py:22-23,81-107`). `laya.Agent`는 일부러 쓰지 않는다. tokenizer 복구 경로가 공유 HF snapshot을 고칠 수 있기 때문이다(`laya_adapter.py:92-93`). 문서는 이 체크포인트를 `models--convaiinnovations--laya`(HF 캐시) multilingual 체크포인트로 적는다(`docs/continual-learning.md:132-137`, 문서 근거). 코드로 확인되는 것은 "encoder + scorer head를 가진 Laya 모델에 mask-marker 선택지 질문을 던지는 구조"까지다.
- **과제**: 후보 카드마다 이진 질문 "Is this wiki card needed to answer the user request?"를 던진다(`laya_adapter.py:20-21`). 입력 문자열은 `'User request:\n'+prompt+'\nWiki card body:\n'+body`다(`:119`). 토큰이 모델 `max_len`을 넘으면 자르지 않고 `model_token_budget_exceeded`로 거부한다(`:131-138`).
- **학습 방식**: encoder는 동결하고(`:105`) 학습 가능한 head 파라미터만 SGD(lr 0.01, batch 2, seed 17)로 1 epoch 돈다(`:222-248`). 산출물은 encoder를 뺀 head-only safetensors이고, 메타데이터에 base 모델 해시와 정책 이름을 담는다(`:181-188`).
- **선택 정책**: `p>=0.5`인 후보를 점수순으로 최대 3장 고르고, 동점이면 풀 순서를 따른다(`laya_adapter.py:173-178`, POLICY `binary-needed-p-ge-0.5-top3-v1` `:17`). 0장도 유효한 예측이다.
- **정답(gold)**: 두 층이 따로 있다.
  - pair label `necessary/supporting/irrelevant`(+abstain, evidence span, rationale): `contracts.py:57-94`, `offline.py:20-52`
  - 요청 단위 선택 gold(0–3 ID): `selection_gold.py:16-43`. "검수자 ID + reference + 캡처 입력 해시"가 있어야 저장되고 변경할 수 없다.
  - 모델 예측이 gold로 올라가는 경로는 없다(`selection_gold.py:1-5`). 선택 gold에 `irrelevant` 카드가 들어 있으면 학습 큐가 거부한다(`selection_queue.py:78-80`).
- **학습 입력**: 캡처 때 고정한 **전체 compact body**를 쓴다. frontmatter 뒤 본문 ≤16 KiB, 원본 파일 ≤64 KiB이고 자르지 않는다(`compact_input.py:10-26`). 본문이 없거나 크기 초과·형식 오류인 요청은 학습에서 빠진다(`selection_queue.py:61-71`).

### 2.2 모듈별 역할

| 파일 | 역할 / 핵심 함수 |
|---|---|
| `contracts.py` | 캡처 sample 스키마 v1/v2/v3. `request()`, `validate_request()`, `validate_label()`, `validate_sampling()`. prompt ≤2000자, excerpt ≤600자(`:10-12`). 허용 host는 `claude/codex/hermes`(`:27`) |
| `store.py` | `database()`: owner-only state dir 검사, `secure_delete=ON`, 테이블 생성(`:16-46`). `capture()`: fail-open 저장(`:49-90`) |
| `compact_input.py` | `derive()`: frontmatter를 빼고 전체 본문을 해시와 함께 고정 |
| `seam.py` | `_snapshot()`: 64 KiB 상한 + `O_NOFOLLOW` 읽기(`:11-22`). `_private_state()`: stateRoot 검증(`:25-53`). `observe()`: recall 후보를 capture로 변환(`:63-151`) |
| `pool.py` | `expand()`: candidateTopK를 8이 아닌 값으로 둘 때 30개까지 재질의하고 정책 판정을 붙이는 shadow 풀(`:8-76`) |
| `corpus.py` | `snapshot()`: 프로젝트 QMD DB의 active 문서(collection, path, hash), `index.yml` SHA, 검색 설정으로 fingerprint를 만든다(`:22-61`). `reconcile()`: fingerprint가 바뀌면 옛 sample을 `corpus_review_queue`에 넣는다. 옛 sample은 다시 쓰지 않는다(`:64-96`). `current_evaluation_ready()`(`:99-116`) |
| `offline.py` | 라벨 저장·검수 승인(`approve_review`), 불변 manifest와 family split 누수 차단(`build_manifest` `:83-147`), `delete_case`(tombstone 기록 + manifest revoke, `:158-173`), `review_view` |
| `dataset.py` | `read_private_json()`(0600·nlink 1·16 MiB, `:36-54`), pair 분류 export와 오프라인 예측 채점(`export_dataset`, `evaluate_predictions`) |
| `laya_raw.py` | Laya raw 데이터 형식 export와 채점. **0.3.23 commit pin을 "assumed-candidate-not-user-confirmed"로 표시한다**(`:10-19`). 검증된 0.3.20 경로와는 별개다 |
| `selection_queue.py` | `build_queue()`: manifest + 선택 gold + 현재 revision을 대조한다. train split에만 gold를 노출한다 |
| `selection_metrics.py` | 판정된 universe 기준 누락률 계산(순수 함수) |
| `cycle_policy.py` | `selection_score`, `wilson_lower`, `next_interval`(24h–14d, 목표 0.95, `:56-96`), `promotion_decision`(`:99-119`) |
| `local_cycle.py` | `LocalTrainer`: 파일 기반 train/predict 프로토콜. 자식 env는 PATH/LANG/TMPDIR와 HF offline 변수만 받는다. 벽시계·RSS 상한이 있다(`:63-127`). `VerifiedLayaTrainer`: adapter 경로 고정, runtime probe, 합성 증명 캐시(`:130-199`). `run_cycle()`(`:263-351`), `rollback()`(`:356-371`) |
| `schedule.py` | `run_due_cycle()`: `learning-schedule.lock` 아래에서 due를 판정하고 cycle을 실행한다. 실패하면 24h 뒤로 미룬다(`:18-75`) |
| `live_select.py` | `choose()`: recall용 실시간 선택(`:30-115`) |
| `laya_adapter.py` | 신뢰 실행 파일. `train`/`predict`/`smoke` 모드, `model_identity()`, `prepare_base_artifact()` |
| `runtime_setup.py` | 읽기 전용 runtime probe(`probe_runtime`), `attest_runtime`(실제 합성 smoke 실행), `choose_runtime`(managed/reuse), `managed_install_plan` |
| `runtime_installer.py` | `stage_generation`(uv로 Python과 hash-locked wheel 설치), `activate_generation`, `rollback_generation` |
| `locks/laya-macos-arm64-py312.lock` | **mutex가 아니다.** pip/uv `--require-hashes` 요구사항 파일이다(아래 5.4) |
| `teacher.py` | teacher 프롬프트(`prompt`)와 응답 검증(`validate_response`). transport는 없다 |
| `transport.py` | `run_teacher()`: claude/codex CLI 1회 호출과 stream 검증. `label_request()`(`:312-342`) |
| `claude_adapter.py` / `codex_adapter.py` | teacher CLI argv와 event 안전 검사 |
| `teacher_budget.py` | 프로젝트 단위 예산 원장: `reserve_teacher`, `budgeted_label_request`, `budgeted_review_request` |
| `jobs.py` | `JobLease`(teacher job flock + lease ≤60s), `label_job`, `job_status`, `cancel_job` |
| `capabilities.py` | teacher preflight 선언 보고서. 실행하지 않는다 |
| `core/context_learning_cli.py` | 명시적으로 호출하는 운영 CLI(서브커맨드 목록 `:19-64`) |
| `core/context_learning_auto.py` | SessionStart launcher와 one-shot due-cycle 자식(`:30-162`) |

## 3. 제어·데이터 흐름

### 3.1 recall 경로 (UserPromptSubmit) — `core/recall.py`

```
hook_budget.arm()  (recall.py:2007, 학습 설정과 무관)
 ├ live_selection = contextLearning.liveSelection is True        (:2046)
 │   primary_limit = 15 if live else DAEMON_QUERY_LIMIT(8)       (:2047, :35)
 ├ daemon /query limit=primary_limit → results[:primary_limit]   (:2263-2264)
 ├ apply_freshness_guard: live면 topN이 아니라 15장까지 카드 freshness 판독 (:2550)
 ├ learning_phases (capture=True일 때만)                         (:2569, :2620)
 ├ final_results = filtered[:min(topN,3) if live else topN]      (:2653)
 ├ if live:                                                      (:2656-2719)
 │    state = seam._private_state ; corpus.snapshot(QMD DB 읽기 전용)
 │    no state → private_state_unavailable / 후보 0 → no_candidates
 │    풀에 wiki가 아닌 결과가 있으면 → non_wiki_pool (fallback)  (:2675)
 │    15장 각각: 경로 해석 → _snapshot(≤64KiB) → QMD active hash와 실제 파일 hash 대조
 │       불일치 → candidate_index_stale → candidate_unavailable (fallback)
 │    live_select.choose(state, prompt, rows, corpus_fingerprint, deadline=hook_budget.deadline())
 │       selected_ids 가 None 아님 → final_results = 그 ID들(0–3장, Laya 순서)
 │    log: event qmd_laya_selection (후보/선택 ID는 sha256 앞 16자, fingerprint, elapsed, peak RSS)
 ├ lex 게이트·format·stdout print (기존)
 ├ selection_reason에 "laya_selected_zero" 추가                  (:2798)
 └ capture=True: stdout print **뒤에** seam.observe(...)           (:2970-3040)
      candidateTopK≠8이고 live가 아니면 pool.expand가 limit 30으로 daemon 재질의
```

`live_select.choose()`(`live_select.py:30-115`) 내부 순서는 다음과 같다.

1. `<state>/live-selector.json`(schema `qmd-live-selector-v1`, `trainerArgv` 3개, `timeoutSeconds` 1–30, `maxRssMib` 128–8192)을 읽는다. `trainerArgv[1]`은 저장소의 `laya_adapter.py`여야 한다(`:43-58`).
2. `<state>/active-checkpoint.json`을 읽고 artifact sha256을 맞춰 본다(`:59-64`).
3. 각 후보 원문을 `derive()`로 compact body로 바꾼다(`:65-77`).
4. `VerifiedLayaTrainer.call('predict')`를 부른다. 이 안에서 매번 adapter 해시, `model_identity`(모델 파일 5개 전체 SHA-256), `probe_runtime`(인터프리터 자식 실행, `import laya, torch`)를 돈다. 합성 증명 캐시(`laya-synthetic-attestation.json`, 24h, `local_cycle.py:165-190`)가 없거나 낡았으면 **hook 안에서 실제 smoke(train 1 step + reload + predict 2회)**를 돌린다.
5. 응답을 정확히 대조하고 0–3개 ID를 검증한다(`:92-102`). 예외는 모두 `None` + 사유(`timeout`/`memory_budget`/`runtime_unavailable`/`selector_unavailable`)로 바뀌어 기존 QMD 선택으로 돌아간다(`:103-115`).

capture(`seam.observe`)는 stdout을 내보낸 뒤에 돌고 예외를 전부 삼킨다(`recall.py:2970-3040`, `seam.py:152-153`). 테스트는 capture on/off 출력 바이트가 같음을 단정한다(`test/context-learning-pool.test.mjs` `assert run()==before`).

### 3.2 SessionStart 자동 cycle — `core/update.sh` → `core/context_learning_auto.py`

```
update.sh main (동기)
 └ autoCycle 문자열 매칭(:1973) → context_learning_auto.py launch-historical (dataMode가 historical_snapshot일 때만 실제 launch)
update.sh --worker → embed 자식 서브셸 (:1259-1280)
 └ embed rc==0 && QMD_AUTO_CYCLE_ENABLED=1 → context_learning_auto.py launch-current
     embed 실패 → "AUTO-CYCLE: deferred" 로그
launch():  setup_guard.status == ready ? → _policy() → <state>/auto-cycle.log(0600) 열기
           → env 정리(PATH/LANG/TMPDIR + qmd_route PATH_KEYS + HF offline) → 분리된 자식 `run` 실행 (:112-145)
run():     current 모드면 corpus.current_evaluation_ready 실패 → last-auto-cycle.json "pending_review"
           cycle_id = "auto-" + sha256([versionId, 이전 next_due])[:20]
           trainer = VerifiedLayaTrainer(argv가 laya_adapter이면) / LocalTrainer(그 밖)
           schedule.run_due_cycle → local_cycle.run_cycle (:60-109)
```

`run_cycle` 게이트(`local_cycle.py:263-351`):

- 데이터 하한: train ≥100, validation ≥50, evaluation ≥50. validation과 evaluation은 각각 none(0장) ≥10, family ≥5(`:278-282`)
- 학습 입력: trainer에는 train만 넘긴다(`:315-317`)
- 비교: challenger와 incumbent가 같은 validation으로 예측한다(`:326-327`)
- 승격 기준: exact 개선, necessary recall과 none 정확률 비퇴행, irrelevant 주입 비증가(`cycle_policy.py:99-119`)
- evaluation: 결정 뒤에 보고만 한다(`:333-335`)
- 승격 동작: `active-checkpoint.json` 포인터만 원자 교체하고 `previous`를 남긴다(`:345-349`)
- 재진입·충돌: 같은 cycle ID는 기록된 판정을 돌려주거나 `queued`에서 재개한다. queue/manifest/trainer 핀이 다르면 `cycle_pin_conflict`다(`:288-307`)

### 3.3 명시적 CLI (자동 호출 없음) — `core/context_learning_cli.py`

`--state-dir`은 필수다. 서브커맨드는 다음과 같다: `list-requests`, `inspect-request`, `inspect-labels`, `job-status`, `cancel-job`, `label-request`, `budgeted-label-request`, `budgeted-second-opinion`, `review-selection`, `inspect-selection`, `run-local-cycle`, `run-due-cycle`, `rollback-local-checkpoint`, `prepare-local-laya-base`, `review-label`, `build-manifest`, `inspect-manifest`, `export-dataset`, `inspect-export`, `evaluate`, `export-laya-raw`, `check-laya-raw`, `evaluate-laya`(`:19-64`). 예외는 `operation_rejected`로만 직렬화하고 원문 예외는 내보내지 않는다(`:158-160`).

## 4. host별 영향

capture·liveSelection·autoCycle 셋은 모든 host에서 **setup `ready` 전제**다. Claude/Codex는 `run-hook`이 `update-queued` 외 모든 action 앞에서 `setup_guard.py hook`을 돌려 ready가 아니면 exit 0 무출력으로 끝낸다(`hooks/run-hook:59-79`). Hermes는 네 hook(`pre_llm_call`/`on_session_start`/`pre_tool_call`/`post_tool_call`)이 모두 `_setup_allowed`를 거친다. 이 함수는 `setup_guard.py check`를 2초 상한으로 실행하고 실패·timeout이면 False를 돌려 hook을 무동작으로 만든다(`hermes_adapter/core_bridge.py:72-83,211,245,275,318`). autoCycle은 여기에 더해 launcher가 `setup_guard.status == ready`를 다시 확인한다(`context_learning_auto.py:62-63,114-115`). 설정 파일이 있는 기존 프로젝트의 ready는 `.auto-context/qmd-index-active.json` pointer가 있어야 성립한다(`core/setup_guard.py:48-55`).

| host | capture | liveSelection | autoCycle | teacher |
|---|---|---|---|---|
| Claude Code | setup ready 전제로 됨. `QMD_ENGINE=claude`(`hooks/run-hook:112`) | setup ready 전제로 됨(recall 공통) | setup ready 전제로 됨(update.sh 공통) | 됨(`claude -p`, sonnet) |
| Codex | setup ready 전제로 됨 | setup ready 전제로 됨 | setup ready 전제로 됨 | 됨(`codex exec`, `--optional-hooks-audited` 필수 `transport.py:98-99`) |
| Hermes | setup ready + `_setup_allowed` 2초 안 통과 전제로 됨. host=`hermes`(`hermes_adapter/core_bridge.py:52`) | 같은 전제로 됨(`pre_llm_call`→recall) | 같은 전제로 됨(`on_session_start`→`session_update`→update.sh 직접 실행, `core_bridge.py:234-258`) | **불가**: policy host는 `codex/claude`만 받고 sample host와 같아야 한다(`teacher_budget.py:24`, `transport.py:94`, `jobs.py:108`) |
| Antigravity(gemini) | 이 저장소는 Gemini hook을 등록하지 않는다(root `plugin.json`에 hooks 없음, `core/agy_local_install.py`는 정리만). 사람이 `run-hook recall gemini`를 직접 연결하면 `request()`가 host `gemini`를 `ValueError`로 거부해 `observe`가 `capture_unavailable`로 조용히 끝난다(`contracts.py:27`, `seam.py:147,152`, 코드 추론). 문서에는 이 사실이 없다 | 직접 연결한 경우에만 recall 경로로 동작(host 무관, 코드 추론) | 해당 없음 | 불가 |

teacher CLI 자식 env는 HOME/PATH/TMPDIR/LANG/USER/LOGNAME/CODEX_HOME + `QMD_SANDBOX=1`만 받는다. 그래서 중첩 qmd 훅이 꺼진다(`codex_adapter.py:37-44`, claude 경로도 같은 함수를 쓴다 `transport.py:109`).

## 5. 설정 키, env, 파일 경로

### 5.1 설정 키 (`.auto-context/settings.json` → `config.normalize_config`)

`contextLearning` 블록은 `DEFAULT_CONFIG`에 없다. 블록이 있을 때만 정규화한다(`core/config.py:1004-1016`). 블록이 없으면 `config.get('contextLearning', {})`가 빈 dict라 모든 기능이 꺼진다. **기본 off, opt-in이다.**

| 키 | 의미 | 정규화 |
|---|---|---|
| `contextLearning.capture` | 후보 풀 기록 | `is True`일 때만 |
| `contextLearning.liveSelection` | recall에서 Laya 선택 사용 | `is True`. 켜면 primary limit 15, 최종 ≤3 |
| `contextLearning.autoCycle` | SessionStart due cycle | `is True` |
| `contextLearning.candidateTopK` | capture 풀 크기 | 1–15 int, 아니면 8. live면 강제 15 |
| `contextLearning.stateRoot` | private state 부모 디렉터리 | str. 검증은 `seam._private_state` |

`stateRoot` 요건(`seam.py:25-53`):

- 절대 경로이고 이미 존재해야 한다.
- 경로 구성요소에 심볼릭 링크가 없어야 한다.
- 프로젝트 root 하위가 아니어야 하고, 어떤 `collectionPaths` 하위도 아니어야 한다.
- 소유자가 본인이고 모드가 0700이어야 한다.
- 실제 state 위치는 `<stateRoot>/<sha256(str(projectRoot))>/`이다.

### 5.2 state 디렉터리 내 파일 (모두 0600/0700)

| 경로 | 쓰는 쪽 / 의미 |
|---|---|
| `learning.sqlite3` | `samples`, `tombstones`, `labels`, `manifests`, `manifest_cases`, `family_splits`, `compact_inputs`(`store.py:36-42`). 추가 테이블: `corpus_snapshots`, `corpus_state`, `corpus_review_queue`(`corpus.py:69-76`), `selection_gold`, `teacher_project`, `teacher_attempts`(`teacher_budget.py:45-47`), `teacher_second_opinions`, `teacher_jobs`(`jobs.py:14-15`). `user_version` 0/1만 허용 |
| `live-selector.json` | 사람이 둔다. live 선택 정책 |
| `active-checkpoint.json` | 승격·rollback 포인터(`local_cycle.py:345-349,367`) |
| `laya-synthetic-attestation.json` | live 선택의 합성 증명 캐시. TTL 24h(`local_cycle.py:172-190`) |
| `auto-cycle.json` | 사람이 둔다. 필드: schema `qmd-auto-cycle-policy-v1`, `versionId`, `revisionsJson`, `trainerArgvJson`, `incumbentArtifact`, `maxSeconds` ≤3600, `maxRssMib` ≤65536, `dataMode`(`context_learning_auto.py:45-56`) |
| `last-auto-cycle.json`, `auto-cycle.log` | auto 결과와 자식 stdout/stderr |
| `learning-schedule.json` / `learning-schedule.lock` | 간격·next_due 상태와 flock(`schedule.py:20-23,38`) |
| `cycles/cycle.lock`, `cycles/<id>/{state.json,candidate.artifact,train-result.json,*-predictions.json}` | cycle 직렬화와 산출물(`local_cycle.py:269-321`) |
| `teacher-job-<sha256(request_id)>.lock` | teacher job flock(`jobs.py:39-45`) |
| `export-<manifest sha>/`, `evaluation-<sha>.json` | 불변 export와 평가 보고(`dataset.py:136-186`) |
| `laya-base-<model sha>.artifact` | `prepare-local-laya-base`가 만드는 base head 참조 JSON. 가중치는 복사하지 않는다(`context_learning_cli.py:74-86`) |

### 5.3 관리형 Laya runtime (설치·다운로드 위치)

- root: macOS `~/Library/Application Support/qmd-auto-context/runtimes/laya`, 그 밖 `~/.local/share/qmd-auto-context/runtimes/laya`(`runtime_setup.py:43-47`). 0700이어야 한다.
- `generations/g-<uuid>/`: 실행하는 명령은 아래 셋이다(`runtime_installer.py:88-100,113-142`).
  - `uv python install 3.12.14 --install-dir <gen>/python --no-bin --no-cache --no-config`
  - `uv venv <gen>/venv`
  - `uv pip sync <lock> --require-hashes --no-build --default-index https://pypi.org/simple`
  - uv 캐시는 `<gen>/cache`다(`:105`). 기존 generation은 수정하지 않는다.
- `generations/g-*/prepared.json`: staging 완료 표식(runtime identity, uv SHA, lock SHA).
- `active.json`: 활성 포인터. 실제 합성 smoke를 통과해야 바뀌고 `previous` 1단계를 보존한다(`runtime_installer.py:193-223`). `activation.lock`은 flock이다(`:180-190`).
- **다운로드 출처**: Astral uv가 받는 Python 3.12.14 배포본과 PyPI wheel뿐이다. uv 실행 파일은 caller가 SHA를 지정해야 하고 버전 `0.12.20`을 검사한다(`:75-85`).
- **모델 가중치는 내려받지 않는다.** `convaiinnovations`, `snapshot_download`, `hf_hub_download`를 grep하면 0건이다. `laya_adapter`는 `local_files_only=True`로 읽는다(`laya_adapter.py:94`). setup은 `--laya-model-dir`에 이미 있는 디렉터리를 요구한다(`core/setup_entry.py:118-120`). 런타임만 설치하고 체크포인트는 사용자가 따로 갖춰야 한다.
- 진입점: `core/install_update.py:_stage_laya`(`:349-370`). `--laya-mode` 기본값은 `defer`이고, `install`은 별도 `--approve-laya-install`이 필요하다(`core/setup_entry.py:336-339`, `:270-271`).

### 5.4 lock 파일의 의미

`core/context_learning/locks/laya-macos-arm64-py312.lock`은 해시 고정 요구사항 파일이다. 헤더는 "uv 0.12.20; Python 3.12.14; macOS arm64; official PyPI wheels only"이고, 패키지 35개에 `--hash=sha256:` 줄 549개가 있다. SHA-256 `354c3c98…6447`은 문서와 같다(`docs/continual-learning.md:199-200`, `shasum` 실측). `_locked_requirements()`는 다음 경우 거부한다(`runtime_installer.py:48-72`).

- 파일 SHA가 caller가 준 값과 다르다.
- 해시가 없는 항목이 있다.
- 항목이 중복된다.
- `MANAGED_DEPENDENCIES` 핀(laya 0.3.20, torch 2.14.0, transformers 5.17.0, safetensors 0.8.0, huggingface-hub 1.33.0, numpy 2.5.3, tokenizers 0.23.2, `runtime_setup.py:22-24`)과 다르다.

macOS arm64 외 플랫폼용 lock은 없다.

### 5.5 env 변수

| env | 의미 / 경계 |
|---|---|
| `QMD_HOOK_TOTAL_SECONDS` | `core/hook_budget.py` 전역 deadline(기본 18s, 2–20s). live 선택이 `deadline()`을 소비한다(`recall.py:2695`) |
| `QMD_HOOK_DEADLINE_MONO` | 부모 deadline을 자식에 상속한다(`hook_budget.py:30-36`) |
| `QMD_AUTO_CYCLE_ENABLED` | update.sh 내부. embed 자식에 넘긴다(`update.sh:1175-1178,1259`) |
| `QMD_ENGINE` | capture sample host(`seam.py:147`) |
| `HF_HUB_OFFLINE`/`TRANSFORMERS_OFFLINE`/`HF_DATASETS_OFFLINE` | trainer·probe·auto 자식에 강제한다. 문서는 이것이 OS 네트워크 샌드박스가 아니라고 명시한다(`docs/continual-learning.md:53-56`) |
| `QMD_LOCAL_TRAINER=1` | trainer 자식 표식(`local_cycle.py:91`) |
| `QMD_SETUP_GUARD_FIXTURE=1` | **테스트 전용**. `setup_guard.status`를 무조건 ready로 만든다(`core/setup_guard.py:21-22`). `npm test` 스크립트가 전역으로 켠다(`package.json:10`). auto launcher는 이 값을 자식 env에 그대로 넘긴다(`context_learning_auto.py:131-132`) |
| `QMD_LAYA_LIVE=1`, `QMD_LAYA_PYTHON`, `QMD_LAYA_MODEL` | **테스트 전용** 실제 Laya smoke opt-in(`test/context-learning-real-laya.test.mjs`, `fixtures/context-learning-real-laya-smoke.py:12-13`) |
| `QMD_LIVE_CASE`, `INDEX_PATH`, `QMD_CONFIG_DIR` | fixture가 쓴다. `INDEX_PATH`/`QMD_CONFIG_DIR`는 `qmd_route`가 읽는 실경로 키다 |

### 5.6 teacher(유료 host CLI) 예산과 상한

자동 호출 경로는 0건이다. capture, local cycle, schedule, recall, SessionStart 어디에서도 teacher를 부르지 않는다(`transport.py:1`, `context_learning_auto.py:1-7`). 진입점은 CLI의 `label-request`, `budgeted-label-request`, `budgeted-second-opinion` 셋뿐이다.

- **policy 필수 필드**(`teacher_budget.py:15-42`):
  - `enabled: true`, `project_id`(정규식), `host`(codex|claude)
  - `allowed_request_ids`(1–2000개, 해당 요청 포함)
  - `max_calls` 1–1000, `max_input_bytes_per_call` 1–10⁷, `max_total_input_bytes` 1–10⁷
  - `max_reserved_usd` 0–10000, `reserve_usd_per_call` ≤ max
  - `creation_model`, `review_model`
- **예약 시점**: transport 전에 원장에 예약한다(`reserve_teacher` `:50-84`).
  - policy가 바뀌면 `teacher_policy_changed`, 다른 project ID면 `project_root_mismatch`다(`:63-66`).
  - 같은 attempt ID를 다시 쓰면 `duplicate_teacher_attempt`다. 재시도에는 새 ID가 필요하고 새 예약을 소모한다.
  - 누적 `calls_used`/`input_bytes_used`/`reserved_usd_used`가 상한을 넘으면 `teacher_budget_exhausted`다(`:73-75`).
  - 샘플을 삭제해도 프로젝트 누적 카운터는 남는다(`offline.py:169-171`은 attempt 행만 지운다).
- **USD 한계**: USD는 예약값이고 실제 청구액이 아니다(`teacher_budget.py:3-5`, `docs/continual-learning.md:113-116`).
- **호출당 상한**(`transport.py:25-26,100,106,116-120`):
  - timeout ≤45s, stdout+stderr ≤128 KiB, 응답 ≤32 KiB(`teacher.py:7`), 프로세스 내 직렬 lock
  - Claude: `--max-turns 1`, `--effort low`, `CLAUDE_CODE_MAX_OUTPUT_TOKENS=1200`, `CLAUDE_CODE_MAX_RETRIES=0`, 기본 모델 `sonnet`
  - Codex: 기본 모델 `gpt-6-luna`(`codex_adapter.py:7`), `--sandbox read-only`, `--ephemeral`
- **도구·훅 격리**:
  - Claude: `--tools ""`, `--disallowedTools *`, 빈 MCP, `disableAllHooks`
  - 응답 stream에 tool/hook event가 보이면 실패 처리한다(`claude_adapter.py:72-107`, `codex_adapter.py:47-83`).
- **출력 처리**: teacher 라벨은 `provisional`로만 저장된다. second opinion은 비교 결과만 남기고 gold가 되지 않는다(`teacher_budget.py:126-170`).

## 6. main 대비 동작 변화

- **opt-in 미설정 프로젝트**: `contextLearning` 경로는 전부 건너뛴다. 다만 같은 브랜치에서 recall에 섞인 변화가 있다.
  - daemon 결과를 `results[:DAEMON_QUERY_LIMIT]`로 로컬에서 자른다(`recall.py:2264`, 요청 limit과 같아 대개 no-op).
  - 보조 질의 응답을 256 KiB로 제한한다.
  - 전역 `hook_budget.arm()`이 추가됐다(`recall.py:2007`). 이것은 hook-budget 영역이다.
- **`liveSelection: true`**:
  - 사용자 `topN`이 3보다 커도 **fallback 포함 최종 주입은 최대 3장**이다(`recall.py:2653`).
  - freshness guard가 최대 15장의 카드 파일을 읽는다(`:2550`). 매 프롬프트에 QMD DB 읽기 전용 조회, 15개 파일 스냅샷, 모델 파일 전체 해시, runtime probe 자식, predict 자식이 붙는다.
  - Laya가 0장을 고르면 **무주입**이고 사유는 `laya_selected_zero`다(`:2798`). 기존 QMD 선택으로 돌아가지 않는다. 이것은 의도된 동작이다(`live_select.py:33-34` "A real 0-card prediction is []").
  - 풀에 wiki 카드가 아닌 결과가 하나라도 있으면 `non_wiki_pool`로 fallback한다(`:2675`). flat이나 hierarchical raw backfill로 raw 결과가 섞이는 프로젝트에서는 Laya가 사실상 돌지 않는다.
- **`capture: true`**:
  - 출력 뒤에 SQLite 쓰기가 붙는다. 출력 바이트는 그대로다.
  - `candidateTopK`가 8이 아니면 출력 뒤에 daemon 재질의(limit 30)가 1회 붙는다(`recall.py:2998-3033`, `pool.py:24`).
  - 사용자 프롬프트 원문(≤2000자)과 후보 본문(excerpt ≤600자, compact body ≤16 KiB)이 **평문으로** state SQLite에 저장된다.
- **`autoCycle: true`**: setup v2 ready일 때만 실행된다. 설정 파일이 있는 프로젝트는 `.auto-context/qmd-index-active.json` 포인터가 있어야 하고, journal은 **있을 때만** `activated`/`rolled_back` phase여야 한다(`core/setup_guard.py:48-65`). ready가 아니면 `setup_required`로 끝난다(`context_learning_auto.py:61-63,113-115`). 즉 v2 setup을 거치지 않은 프로젝트에서는 켜도 돌지 않는다.
- **호환성과 마이그레이션**: 새 키 블록이라 기존 설정을 깨지 않는다. state SQLite는 `user_version` 0/1만 허용하고 다른 값이면 fail-closed다(`store.py:32-34`). capture는 fail-open, 오프라인 작업은 fail-closed다(`store.py:1`).
- **개인정보 문서 대조**: `docs/privacy.md`는 "local processing"과 "optional wiki compile"만 다룬다. `contextLearning`, Laya, teacher, stateRoot, 프롬프트 저장 언급은 0건이다(grep). `docs/settings.md`, `README.md`에도 `contextLearning` 언급이 없다. 사용자용 문서는 `docs/continual-learning.md` 하나이고 제목이 "(development only)"다(`:1`).

## 7. 테스트 커버리지

`npm test`는 `QMD_SETUP_GUARD_FIXTURE=1 QMD_RECALL_LOG= node --test`로 돈다(`package.json:10`). 아래 테스트는 모두 합성 데이터만 쓰고 `externalCalls/external_calls == 0`을 단정한다.

| 테스트 | 고정하는 계약 |
|---|---|
| `context-learning-offline.test.mjs` | 라벨 검수, 불변 manifest, family split 누수 차단(동일 query/content 교차 차단), stale 제외, abstain 제외, `delete_case` tombstone·manifest revoke·재캡처 차단 |
| `context-learning-pool.test.mjs` | 실제 `core/recall.py` 실행에서 capture on/off **출력 동일**. v2 풀 메타(skip 제외, over-return, 스냅샷 실패, 버전 오류 시 출력 불변), hierarchical fallback, freshness/unverified/excluded 사유, contradiction span 검증 |
| `context-learning-top15.test.mjs` | candidateTopK 확장이 hook 바이트를 보존하고 증거가 bounded임 |
| `context-learning-corpus.test.mjs` | QMD active 문서 C/U/D, 플러그인 설정·qmd 모델 설정 변화 감지, 과거 sample 불변, current 평가 제외(review queue 5건) |
| `context-learning-dataset.test.mjs` / `-laya-raw.test.mjs` | export 재현성, 오프라인 채점, raw 형식 class 순서와 provenance |
| `context-learning-cycle.test.mjs` | fixture 5개: 선택 gold·cadence·승격 정책 / 200문항 e2e(mock trainer 1회 실패 → 재개 → 승격 → rollback → stale 거부) / teacher 예산(creation·review 모델 분리, cache 재사용) / runtime 탐색·합성 호환 게이트 / 관리형 generation stage·activate·rollback(가짜 uv, 설치 없음). wrapper는 `report.ok`와 `external_calls`만 보고 실제 단정은 fixture 안에 있다 |
| `context-learning-auto-hook.test.mjs` | 실제 SessionStart → 분리된 due worker → 승격, 두 번째 hook not_due, cadence 48/24/24h, provenance 없는 current 차단, QMD worker 전 current 지연, 격리 index env 전달 |
| `context-learning-live-recall.test.mjs` | 실제 recall에서 15장 전체 본문 전달, 선택 순서 반영, 0장 → 무주입, timeout fallback, 2초 deadline 무주입, 실제 adapter + 모델 부재 → `runtime_unavailable`/`timeout` fallback, capture 풀 = live 풀, stale index fallback. `choose`는 stub으로 바꿔 쓴다 |
| `context-learning-live-selector.test.mjs` | `choose()` 전체 본문 입력, 순위, 0장, 잘못된 출력·timeout fallback, checkpoint 핀, corpus 게이트 |
| `context-learning-proof-cache.test.mjs` | 프롬프트 2회에 smoke 1회, 모델 변경·24h 경과 시 무효화(모두 monkeypatch) |
| `context-learning-teacher.test.mjs` | claude argv와 startup catalog 검사, 보안 정책 plugin 허용 범위, preflight, 응답 스키마 검증 |
| `context-learning-transport.test.mjs` | mock CLI 기반 transport와 provisional 원자 저장 |
| `context-learning-codex-adapter.test.mjs` | codex argv와 env 화이트리스트, action·error event 거부 |
| `context-learning-real-laya.test.mjs` | `QMD_LAYA_LIVE=1`일 때만 실행. 실제 Laya smoke와 trainer 프로토콜 |

`fixtures/context-learning-top15-benchmark.py`는 어느 테스트도 참조하지 않는 수동 스크립트다. `fixtures/context-learning-mock-trainer.py`는 cycle-e2e와 auto-hook-e2e가 쓴다.

## 8. 위험·미해결·TODO

1. **teacher 무예산 경로**: CLI `label-request --enable-teacher`가 `jobs.label_job` → `transport.label_request` → `run_teacher`로 이어진다. 이 경로는 `teacher_budget.reserve_teacher`를 거치지 않는다(`context_learning_cli.py:120-123`, `jobs.py:98-123`, `transport.py:312-327`). 문서는 "budgeted-* require an explicit private policy"라고만 적고 이 경로를 언급하지 않는다(`docs/continual-learning.md:106-116`). 명시 플래그가 필요한 수동 경로이긴 하지만 호출 수·USD 원장에 남지 않는 유료 호출이다.
2. **live 선택의 hook 안 비용**: 프롬프트마다 다음이 순서대로 돈다(`local_cycle.py:138-140,159-162`, `runtime_setup.py:61-69`).
   - `model_identity`(모델 파일 5개 전체 SHA-256)를 생성자와 `call()`에서 **2회**
   - adapter 해시
   - `probe_runtime` 자식(최대 30s timeout, torch import)
   - predict 자식(모델 load)

   문서의 실측은 합성 1카드 predict 2.347s, peak RSS 2,674,032 KiB다(`docs/requirements-acceptance-trace-20261006.md:11`, 문서 근거). 15장과 해시·probe를 합친 시간은 측정되지 않았다(추정 위험). 증명 캐시가 없거나 24h가 지나면 smoke(train + reload + predict 2회)를 hook 안에서 돌린다. 이 smoke가 `min(timeoutSeconds≤30, hook 잔여)` 안에 끝나지 않으면 캐시가 써지지 않아, 다음 프롬프트마다 다시 시도하고 timeout으로 fallback하는 상태가 반복될 수 있다(추정, 코드 경로 `local_cycle.py:164-190`).
3. **품질 미검증**: 미학습 base head가 관련 합성 카드에도 0장을 골랐다(`docs/requirements-acceptance-trace-20261006.md:11`, 문서 근거). 운영 checkpoint 연결과 실문서 추론은 수행하지 않았다(`docs/operational-flow-map-20261006.md:39`). `liveSelection`에서 0장 예측은 무주입이므로, 승격 게이트를 통과하지 않은 base head를 `active-checkpoint.json`으로 지정하면 recall이 사실상 꺼질 수 있다(추정).
4. **docstring과 실제 동작 불일치**: `local_cycle.py:3` "No hook starts this worker."와 `schedule.py:4` "Only a caller that explicitly invokes run_due_cycle..."는 현재 SessionStart(`update.sh:1275`, `context_learning_auto.py`)가 cycle을 띄우는 사실과 어긋난다. `context_learning_cli.py:2` "no automatic capture or worker"는 CLI 자체에 대해서는 참이다.
5. **deletion에 CLI 진입점이 없다**: `offline.delete_case`(`offline.py:158-173`)는 있지만 CLI 서브커맨드 목록(`context_learning_cli.py:19-64`)과 hook·skill 어디에서도 호출하지 않는다(grep). 문서의 deletion 서술(`docs/continual-learning.md:20-23`)은 Python API로만 성립한다(문서 근거, CLI 미노출).
6. **Laya pin 두 벌**: adapter와 runtime은 0.3.20이고 `laya_raw.py`는 0.3.23 가정 pin이다(`laya_raw.py:10-19`, 문서 `:43-44`가 인정).
7. **플랫폼**: lock은 macOS arm64 하나뿐이다. 비-mac에는 기본 root 경로만 있고 설치 lock이 없다. MPS 아니면 CPU로 동작한다(`laya_adapter.py:77-78`).
8. **autoCycle 판정이 문자열 매칭이다**: `update.sh:1176,1973`은 `config_json`에서 `"autoCycle": true`를 부분 문자열로 찾는다. launcher가 정규화된 설정으로 다시 확인하므로(`context_learning_auto.py:35`) 오탐의 결과는 launcher 호출 1회에 그친다.
9. **선언만 있는 보호 장치**: `teacher.require_tool_free_transport`와 `capabilities.preflight`는 테스트에서만 호출된다(`authorization: report-only-not-runtime-attestation`, `capabilities.py:27`). Codex transport는 빈 tool catalog를 증명하지 않는다(`codex_adapter.py:3-4`, `transport.py:82`).
10. **문서 공백**: `docs/privacy.md`, `docs/settings.md`, `README.md`에 contextLearning 저장 데이터(프롬프트 평문 ≤2000자, 카드 본문 ≤16 KiB), stateRoot 규칙, teacher 외부 전송이 없다. Gemini hook을 직접 연결했을 때 capture가 조용히 실패하는 사실도 문서에 없다(출하된 Gemini 등록 경로는 없다).
11. **테스트 이식성**: `live-recall-e2e.py:11`과 `auto-hook-e2e.py:25`는 `Path.home()/'work'`를 임시 디렉터리 부모로 쓴다. GitHub runner는 `/home/runner/work`가 있어 통과하지만, `~/work`가 없는 개발 머신에서는 `FileNotFoundError`로 실패한다(추정. HOME 하위 config 탐색 경계 때문에 HOME 하위를 고른 것으로 보인다).
12. **가중치 unlearning 없음**: 문서가 명시한다(`docs/continual-learning.md:23`, `offline.py:159`). 삭제는 manifest revoke와 원장 정리까지이고, 이미 학습된 head는 그대로 남는다.
13. **문서 자인 미검증 항목**(`docs/requirements-acceptance-trace-20261006.md:22`): 운영 `active.json` 미생성, 새 coordinator에서 실제 uv·모델 호출 미실행, 배포 채널별 wheel 재현성 미검증.

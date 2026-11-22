# 학습 상태 기계

이 문서는 `memvane/learning/context_learning/state_model.py`에서 만든다. 손으로 고치지 않는다.
모델을 고친 뒤 `scripts/render_learning_state.py`로 다시 만든다. 구조 시험이 둘의 일치를 단정한다.

상태 모델은 순수 데이터다. hook과 worker는 전이를 정하려고 이 데이터를 읽지 않는다. 전이를 쓰는
코드는 표의 "쓰는 곳"에 있고, `memvane/learning/tests/statemachine`의 전이표 시험이 표의 전이마다
실제 쓰는 코드를 돌려 끝 상태를 대조한다. 표에 없는 (상태, 사건)은 상태를 바꾸지 않는다.

`(absent)`는 행이 없는 상태다. 종결 상태는 행이 지워질 때만 떠난다. 표는 제품 경로(default
teacher)만 담는다.

## M1 case

- 저장 위치: learning DB cases.status
- lock: project-worker.lock
- 쓰는 주체: hook capture child (insert), teacher pass, rotation and migration (delete)
- 상태: `(absent)`, `queued`, `first_running`, `first_done`, `gold` (종결), `pending_review` (종결)

| 상태 | 사건 | 다음 | 쓰는 곳 |
|---|---|---|---|
| `(absent)` | `capture` | `queued` | `shared.enqueue` |
| `queued` | `claim` | `first_running` | `default_teacher._default_case` |
| `queued` | `git_migrated` | `(absent)` | `edit_git_migration.migrate` |
| `queued` | `local_check_failed` | `pending_review` | `default_teacher._default_case` |
| `queued` | `prompt_retired` | `(absent)` | `prompt_retirement.retire` |
| `queued` | `rotation_historical` | `(absent)` | `case_rotation.rotate` |
| `first_running` | `answer_committed` | `gold` | `default_teacher._call` |
| `first_running` | `answer_kept` | `first_done` | `default_teacher._park` |
| `first_running` | `content_refused` | `pending_review` | `default_teacher._failed` |
| `first_running` | `git_migrated` | `(absent)` | `edit_git_migration.migrate` |
| `first_running` | `not_sent` | `queued` | `default_teacher._failed` |
| `first_running` | `not_sent_exhausted` | `pending_review` | `default_teacher._failed` |
| `first_running` | `pass_start` | `pending_review` | `default_teacher.process_pending_default` |
| `first_running` | `prompt_retired` | `(absent)` | `prompt_retirement.retire` |
| `first_running` | `provider_refused` | `queued` | `default_teacher._failed` |
| `first_running` | `uncertain` | `pending_review` | `default_teacher._failed` |
| `first_done` | `consent_withdrawn` | `pending_review` | `default_teacher._review_parked` |
| `first_done` | `git_migrated` | `(absent)` | `edit_git_migration.migrate` |
| `first_done` | `kept_confirmed` | `gold` | `default_teacher._confirm_parked` |
| `first_done` | `kept_rejected` | `pending_review` | `default_teacher._confirm_parked` |
| `first_done` | `prompt_retired` | `(absent)` | `prompt_retirement.retire` |
| `gold` | `git_migrated` | `(absent)` | `edit_git_migration.migrate` |
| `gold` | `prompt_retired` | `(absent)` | `prompt_retirement.retire` |
| `gold` | `rotation` | `(absent)` | `case_rotation.rotate` |
| `gold` | `rotation_historical` | `(absent)` | `case_rotation.rotate` |
| `pending_review` | `git_migrated` | `(absent)` | `edit_git_migration.migrate` |
| `pending_review` | `prompt_retired` | `(absent)` | `prompt_retirement.retire` |
| `pending_review` | `rotation` | `(absent)` | `case_rotation.rotate` |
| `pending_review` | `rotation_historical` | `(absent)` | `case_rotation.rotate` |

## M1U case use

- 저장 위치: learning DB cases.use_state
- lock: project-worker.lock
- 쓰는 주체: hook capture child (insert), teacher pass
- 상태: `(absent)`, `active`, `historical` (종결)

| 상태 | 사건 | 다음 | 쓰는 곳 |
|---|---|---|---|
| `(absent)` | `capture` | `active` | `shared.enqueue` |
| `active` | `captured_before_grant` | `historical` | `default_teacher._migration_ready` |
| `active` | `scope_changed` | `historical` | `default_teacher._default_case` |

## M1T case training mark

- 저장 위치: learning DB cases.trained_cycle
- lock: learning-claim.lock
- 쓰는 주체: learning cycle of the project worker
- 상태: `unmarked`, `marked` (종결)

| 상태 | 사건 | 다음 | 쓰는 곳 |
|---|---|---|---|
| `unmarked` | `cycle_staged` | `marked` | `automatic_staging._train_pinned` |

## M2 gold ledger

- 저장 위치: learning DB project_gold_attempts.stage
- lock: project-worker.lock
- 쓰는 주체: teacher pass
- 상태: `(absent)`, `reserved`, `dispatch_unknown`, `dispatched`, `answer_received`, `answer_parked`, `committed` (종결), `pre_send_rejected`, `provider_refused`, `dispatched_uncertain` (종결)

| 상태 | 사건 | 다음 | 쓰는 곳 |
|---|---|---|---|
| `(absent)` | `reserve` | `reserved` | `general_gold_ledger.reserve` |
| `reserved` | `before_launch` | `dispatch_unknown` | `default_teacher._call` |
| `reserved` | `recover` | `dispatched_uncertain` | `general_gold_ledger.recover` |
| `reserved` | `settle_not_sent` | `pre_send_rejected` | `default_teacher._failed` |
| `reserved` | `settle_refused` | `provider_refused` | `default_teacher._failed` |
| `reserved` | `settle_uncertain` | `dispatched_uncertain` | `default_teacher._failed` |
| `dispatch_unknown` | `after_launch` | `dispatched` | `default_teacher._call` |
| `dispatch_unknown` | `recover` | `dispatched_uncertain` | `general_gold_ledger.recover` |
| `dispatch_unknown` | `settle_not_sent` | `pre_send_rejected` | `default_teacher._failed` |
| `dispatch_unknown` | `settle_refused` | `provider_refused` | `default_teacher._failed` |
| `dispatch_unknown` | `settle_uncertain` | `dispatched_uncertain` | `default_teacher._failed` |
| `dispatched` | `answer` | `answer_received` | `default_teacher._call` |
| `dispatched` | `recover` | `dispatched_uncertain` | `general_gold_ledger.recover` |
| `dispatched` | `settle_not_sent` | `pre_send_rejected` | `default_teacher._failed` |
| `dispatched` | `settle_refused` | `provider_refused` | `default_teacher._failed` |
| `dispatched` | `settle_uncertain` | `dispatched_uncertain` | `default_teacher._failed` |
| `answer_received` | `commit` | `committed` | `default_teacher._call` |
| `answer_received` | `park` | `answer_parked` | `default_teacher._park` |
| `answer_received` | `recover` | `dispatched_uncertain` | `general_gold_ledger.recover` |
| `answer_received` | `settle_not_sent` | `pre_send_rejected` | `default_teacher._failed` |
| `answer_received` | `settle_refused` | `provider_refused` | `default_teacher._failed` |
| `answer_received` | `settle_uncertain` | `dispatched_uncertain` | `default_teacher._failed` |
| `answer_parked` | `confirm` | `committed` | `default_teacher._confirm_parked` |
| `answer_parked` | `reject_kept` | `dispatched_uncertain` | `default_teacher._review_parked` |
| `pre_send_rejected` | `reserve` | `reserved` | `general_gold_ledger.reserve` |
| `provider_refused` | `reserve` | `reserved` | `general_gold_ledger.reserve` |

## M3 learning job

- 저장 위치: learning DB learning_jobs.status
- lock: learning-claim.lock (one attempt), database transaction (each step)
- 쓰는 주체: learning cycle of the project worker
- 상태: `(absent)`, `queued`, `running`, `deferred`, `staged` (종결), `blocked`

| 상태 | 사건 | 다음 | 쓰는 곳 |
|---|---|---|---|
| `(absent)` | `plan` | `queued` | `learning_queue.LearningQueue` |
| `queued` | `claim` | `running` | `learning_queue.LearningQueue` |
| `queued` | `complete` | `staged` | `learning_queue.LearningQueue` |
| `queued` | `consumed_elsewhere` | `blocked` | `learning_queue.LearningQueue` |
| `queued` | `defer` | `deferred` | `learning_queue.LearningQueue` |
| `queued` | `fail` | `deferred` | `learning_queue.LearningQueue` |
| `queued` | `fail_limit` | `blocked` | `learning_queue.LearningQueue` |
| `queued` | `source_revoked` | `blocked` | `learning_queue.LearningQueue` |
| `queued` | `unrepeatable_failure` | `blocked` | `learning_queue.LearningQueue` |
| `running` | `claim_released` | `deferred` | `learning_queue.LearningQueue` |
| `running` | `complete` | `staged` | `learning_queue.LearningQueue` |
| `running` | `consumed_elsewhere` | `blocked` | `learning_queue.LearningQueue` |
| `running` | `defer` | `deferred` | `learning_queue.LearningQueue` |
| `running` | `fail` | `deferred` | `learning_queue.LearningQueue` |
| `running` | `fail_limit` | `blocked` | `learning_queue.LearningQueue` |
| `running` | `source_revoked` | `blocked` | `learning_queue.LearningQueue` |
| `running` | `unrepeatable_failure` | `blocked` | `learning_queue.LearningQueue` |
| `deferred` | `claim` | `running` | `learning_queue.LearningQueue` |
| `deferred` | `complete` | `staged` | `learning_queue.LearningQueue` |
| `deferred` | `consumed_elsewhere` | `blocked` | `learning_queue.LearningQueue` |
| `deferred` | `fail_limit` | `blocked` | `learning_queue.LearningQueue` |
| `deferred` | `source_revoked` | `blocked` | `learning_queue.LearningQueue` |
| `deferred` | `unrepeatable_failure` | `blocked` | `learning_queue.LearningQueue` |
| `blocked` | `rearm` | `queued` | `learning_queue.LearningQueue` |

## M4 cycle

- 저장 위치: learning DB cycles.status
- lock: learning-claim.lock (training), evaluation claim (evaluation), apply journal (apply)
- 쓰는 주체: learning cycle of the project worker
- 상태: `(absent)`, `training`, `failed`, `deferred`, `staged`, `staged_evaluated`, `applied` (종결)

| 상태 | 사건 | 다음 | 쓰는 곳 |
|---|---|---|---|
| `(absent)` | `train_started` | `training` | `automatic_staging._train_pinned` |
| `training` | `train_deferred` | `deferred` | `automatic_staging._train_pinned` |
| `training` | `train_failed` | `failed` | `automatic_staging._train_pinned` |
| `training` | `trained` | `staged` | `automatic_staging._train_pinned` |
| `failed` | `train_deferred` | `deferred` | `automatic_staging._train_pinned` |
| `failed` | `trained` | `staged` | `automatic_staging._train_pinned` |
| `deferred` | `train_failed` | `failed` | `automatic_staging._train_pinned` |
| `deferred` | `trained` | `staged` | `automatic_staging._train_pinned` |
| `staged` | `evaluated` | `staged_evaluated` | `automatic_staging._evaluate_impl` |
| `staged_evaluated` | `applied` | `applied` | `candidate_apply._finish_cycle` |
| `staged_evaluated` | `carried` | `staged` | `candidate_close.carry_candidate` |
| `staged_evaluated` | `reevaluation_needed` | `staged` | `automatic_staging._prepare_evaluation` |

## M5 evaluation

- 저장 위치: learning DB learning_evaluations.status
- lock: evaluation-claim.lock (one attempt), database transaction (each step)
- 쓰는 주체: learning cycle of the project worker
- 상태: `(absent)`, `queued`, `running`, `deferred`, `waiting_quality`, `completed`, `blocked` (종결)

| 상태 | 사건 | 다음 | 쓰는 곳 |
|---|---|---|---|
| `(absent)` | `ensure` | `queued` | `evaluation_queue.EvaluationQueue` |
| `(absent)` | `reject` | `blocked` | `evaluation_queue.EvaluationQueue` |
| `queued` | `block` | `blocked` | `evaluation_queue.EvaluationQueue` |
| `queued` | `carried` | `(absent)` | `candidate_close.carry_candidate` |
| `queued` | `claim` | `running` | `evaluation_queue.EvaluationQueue` |
| `queued` | `defer` | `deferred` | `evaluation_queue.EvaluationQueue` |
| `queued` | `evaluated` | `completed` | `evaluation_queue.EvaluationQueue` |
| `queued` | `fail_limit` | `blocked` | `evaluation_queue.EvaluationQueue` |
| `queued` | `reject` | `blocked` | `evaluation_queue.EvaluationQueue` |
| `queued` | `too_few_cases` | `waiting_quality` | `evaluation_queue.EvaluationQueue` |
| `running` | `block` | `blocked` | `evaluation_queue.EvaluationQueue` |
| `running` | `carried` | `(absent)` | `candidate_close.carry_candidate` |
| `running` | `claim_released` | `deferred` | `evaluation_queue.EvaluationQueue` |
| `running` | `defer` | `deferred` | `evaluation_queue.EvaluationQueue` |
| `running` | `evaluated` | `completed` | `evaluation_queue.EvaluationQueue` |
| `running` | `fail_limit` | `blocked` | `evaluation_queue.EvaluationQueue` |
| `running` | `reject` | `blocked` | `evaluation_queue.EvaluationQueue` |
| `running` | `too_few_cases` | `waiting_quality` | `evaluation_queue.EvaluationQueue` |
| `deferred` | `block` | `blocked` | `evaluation_queue.EvaluationQueue` |
| `deferred` | `carried` | `(absent)` | `candidate_close.carry_candidate` |
| `deferred` | `claim` | `running` | `evaluation_queue.EvaluationQueue` |
| `deferred` | `evaluated` | `completed` | `evaluation_queue.EvaluationQueue` |
| `deferred` | `fail_limit` | `blocked` | `evaluation_queue.EvaluationQueue` |
| `deferred` | `reject` | `blocked` | `evaluation_queue.EvaluationQueue` |
| `deferred` | `too_few_cases` | `waiting_quality` | `evaluation_queue.EvaluationQueue` |
| `waiting_quality` | `block` | `blocked` | `evaluation_queue.EvaluationQueue` |
| `waiting_quality` | `carried` | `(absent)` | `candidate_close.carry_candidate` |
| `waiting_quality` | `evaluated` | `completed` | `evaluation_queue.EvaluationQueue` |
| `waiting_quality` | `refresh` | `queued` | `evaluation_queue.EvaluationQueue` |
| `waiting_quality` | `reject` | `blocked` | `evaluation_queue.EvaluationQueue` |
| `waiting_quality` | `validation_changed` | `queued` | `evaluation_queue.EvaluationQueue` |
| `completed` | `apply_failure_limit` | `blocked` | `evaluation_queue.EvaluationQueue` |
| `completed` | `carried` | `(absent)` | `candidate_close.carry_candidate` |
| `completed` | `refresh` | `queued` | `evaluation_queue.EvaluationQueue` |
| `completed` | `reject` | `blocked` | `evaluation_queue.EvaluationQueue` |
| `blocked` | `carried` | `(absent)` | `candidate_close.carry_candidate` |

## M6 application

- 저장 위치: candidate-apply.json phase
- lock: candidate-apply.lock, cycles/cycle.lock
- 쓰는 주체: learning cycle of the project worker
- 상태: `(absent)`, `intent`, `prepared`, `pointer_committed`, `activated` (종결), `rejected` (종결)

| 상태 | 사건 | 다음 | 쓰는 곳 |
|---|---|---|---|
| `(absent)` | `intent_formed` | `intent` | `candidate_apply.apply_candidate` |
| `intent` | `ended` | `rejected` | `candidate_apply.apply_candidate` |
| `intent` | `prepared_by_service` | `prepared` | `candidate_apply.apply_candidate` |
| `prepared` | `ended` | `rejected` | `candidate_apply.apply_candidate` |
| `prepared` | `intent_formed` | `intent` | `candidate_apply.apply_candidate` |
| `prepared` | `pointer_committed` | `pointer_committed` | `candidate_apply.apply_candidate` |
| `pointer_committed` | `activated_by_service` | `activated` | `candidate_apply.apply_candidate` |

## M7 active checkpoint

- 저장 위치: active-checkpoint.json
- lock: cycles/cycle.lock
- 쓰는 주체: learning cycle of the project worker
- 상태: `incumbent`, `candidate`

| 상태 | 사건 | 다음 | 쓰는 곳 |
|---|---|---|---|
| `incumbent` | `pointer_written` | `candidate` | `candidate_apply.apply_candidate` |

## M8 worker request

- 저장 위치: full-worker-request.json, full-worker-state.json
- lock: full-worker.lock
- 쓰는 주체: hook launch (request), dispatcher step and owned child (state)
- 상태: `(absent)`, `open`, `last_start`, `settled`, `acknowledged`

| 상태 | 사건 | 다음 | 쓰는 곳 |
|---|---|---|---|
| `(absent)` | `launch` | `open` | `shared.launch` |
| `open` | `child_result` | `settled` | `shared._settle_owned` |
| `open` | `third_start` | `last_start` | `shared._full_step` |
| `last_start` | `child_result` | `settled` | `shared._settle_owned` |
| `last_start` | `child_retry_result` | `open` | `shared._settle_owned` |
| `last_start` | `launch` | `open` | `shared.launch` |
| `last_start` | `step` | `acknowledged` | `shared._full_step` |
| `settled` | `launch` | `open` | `shared.launch` |
| `settled` | `step` | `acknowledged` | `shared._full_step` |
| `acknowledged` | `launch` | `open` | `shared.launch` |

## M15 teacher host

- 저장 위치: teacher-host-state.json hosts.<host>
- lock: teacher-host-state.lock
- 쓰는 주체: teacher pass (a SessionStart hook writes notice times only)
- 상태: `ready`, `resting`, `provider_waiting`, `failing`

| 상태 | 사건 | 다음 | 쓰는 곳 |
|---|---|---|---|
| `ready` | `failure_limit` | `failing` | `default_teacher._failed` |
| `ready` | `host_not_ready` | `resting` | `default_teacher._default_pass` |
| `ready` | `provider_refused` | `provider_waiting` | `default_teacher._failed` |
| `resting` | `host_ready` | `ready` | `default_teacher._default_pass` |
| `provider_waiting` | `wait_expired` | `ready` | `teacher_state.host_waiting` |
| `failing` | `wait_expired` | `ready` | `teacher_state.host_waiting` |

## worker pass의 결과 상태

pass의 결과는 cycle 단계의 결과다. 모든 결과 상태는 아래 다섯 분류 중 하나다.
`retry`는 worker가 다음 wake를 요청하는 상태이고, `held`는 pass 밖의 무엇이 바뀔 때까지
매 pass 같은 답이 나오는 상태라 처음 그렇게 끝난 pass가 `learning.cycle` `held` 행을 남긴다.

| 결과 | 분류 | 이벤트 | pass가 남기는 것 |
|---|---|---|---|
| `busy` | retry(다시 깨움 요청) | — | nothing: another worker holds the lock |
| `setup_in_progress` | retry(다시 깨움 요청) | — | nothing: the setup has not finished |
| `deferred_resource` | retry(다시 깨움 요청) | `learning.cycle deferred_resource` | the job and the evaluation deferred with a backoff |
| `deferred_quota` | retry(다시 깨움 요청) | — | nothing (explicit profile only) |
| `staged_evaluation_deferred` | retry(다시 깨움 요청) | — | the evaluation deferred with a backoff |
| `apply_deferred` | retry(다시 깨움 요청) | — | the application journal with its retry time and failures |
| `disabled` | waiting(입력 대기) | — | nothing: capture or the cycle is off in the settings |
| `waiting_fresh_train` | waiting(입력 대기) | `learning.score` | gold cases below the training size |
| `waiting_fresh_batch` | waiting(입력 대기) | `learning.score` | no new job: too few fresh gold cases |
| `staged_waiting_quality_cohort` | waiting(입력 대기) | `learning.score` | a staged cycle waiting for evaluation cases |
| `staged_evaluated` | waiting(입력 대기) | `learning.promotion evaluated` | an evaluated cycle (applied by the next step when the profile applies) |
| `already_staged_evaluated` | waiting(입력 대기) | — | an evaluated cycle, unchanged |
| `applied` | done(적용 완료) | `learning.promotion applied` | the active pointer is the candidate |
| `already_applied` | done(적용 완료) | — | the applied cycle, unchanged |
| `failed` | closed(사유와 함께 닫힘) | `learning.cycle failed` | the job deferred with one more failure (blocked at three) |
| `learning_job_blocked` | closed(사유와 함께 닫힘) | `learning.cycle blocked` | the blocked job until its rearm time |
| `staged_source_revoked` | closed(사유와 함께 닫힘) | `learning.cycle blocked` | the job or the candidate closed as source_revoked |
| `apply_blocked` | closed(사유와 함께 닫힘) | `learning.promotion rejected` | a rejected journal or a counted failure on the evaluation |
| `staged_cohort_changed` | closed(사유와 함께 닫힘) | — | the evaluation deferred: its rows moved under the attempt |
| `staged_authorization_expired` | closed(사유와 함께 닫힘) | — | the evaluation deferred until renewal carries or closes it |
| `staged_artifact_changed` | held(멈춤(밖의 변화 필요)) | `learning.cycle held` | a staged cycle whose artifact or evidence no longer matches |
| `cohort_cycle_conflict` | held(멈춤(밖의 변화 필요)) | `learning.cycle held` | a cycle row in a state training cannot enter |
| `evaluation_stage_unavailable` | held(멈춤(밖의 변화 필요)) | `learning.cycle held` | an evaluation whose cycle or job row is missing |
| `experiment_paused` | held(멈춤(밖의 변화 필요)) | `learning.cycle held` | no cycle: the training authorization is refused |
| `existing_trial_requires_review` | held(멈춤(밖의 변화 필요)) | `learning.cycle held` | an experimental checkpoint from an older flow |
| `multilingual_policy_required` | held(멈춤(밖의 변화 필요)) | `learning.cycle held` | a policy with another model input view |
| `cohort_source_revoked` | held(멈춤(밖의 변화 필요)) | `learning.cycle held` | more than 2,000 admitted rows |
| `runtime_approval_required` | held(멈춤(밖의 변화 필요)) | `learning.cycle held` | no valid runtime approval |
| `synthetic_training_budget_exhausted` | held(멈춤(밖의 변화 필요)) | `learning.cycle held` | the synthetic QA profile used its budget |

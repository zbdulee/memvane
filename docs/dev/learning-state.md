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
| `queued` | `local_check_failed` | `pending_review` | `default_teacher._default_case` |
| `first_running` | `answer_committed` | `gold` | `default_teacher._call` |
| `first_running` | `answer_kept` | `first_done` | `default_teacher._park` |
| `first_running` | `content_refused` | `pending_review` | `default_teacher._failed` |
| `first_running` | `not_sent` | `queued` | `default_teacher._failed` |
| `first_running` | `not_sent_exhausted` | `pending_review` | `default_teacher._failed` |
| `first_running` | `pass_start` | `pending_review` | `default_teacher.process_pending_default` |
| `first_running` | `provider_refused` | `queued` | `default_teacher._failed` |
| `first_running` | `uncertain` | `pending_review` | `default_teacher._failed` |
| `first_done` | `consent_withdrawn` | `pending_review` | `default_teacher._review_parked` |
| `first_done` | `kept_confirmed` | `gold` | `default_teacher._confirm_parked` |
| `first_done` | `kept_rejected` | `pending_review` | `default_teacher._confirm_parked` |
| `gold` | `rotation` | `(absent)` | `case_rotation.rotate` |
| `pending_review` | `rotation` | `(absent)` | `case_rotation.rotate` |

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

## M15 teacher host

- 저장 위치: teacher-host-state.json hosts.<host>
- lock: teacher-host-state.lock (under project-worker.lock)
- 쓰는 주체: teacher pass
- 상태: `ready`, `resting`, `provider_waiting`, `failing`

| 상태 | 사건 | 다음 | 쓰는 곳 |
|---|---|---|---|
| `ready` | `failure_limit` | `failing` | `default_teacher._failed` |
| `ready` | `host_not_ready` | `resting` | `default_teacher._default_pass` |
| `ready` | `provider_refused` | `provider_waiting` | `default_teacher._failed` |
| `resting` | `host_ready` | `ready` | `default_teacher._default_pass` |
| `provider_waiting` | `wait_expired` | `ready` | `teacher_state.host_waiting` |
| `failing` | `wait_expired` | `ready` | `teacher_state.host_waiting` |

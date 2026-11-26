# Wiki verify

기계 검수는 `wiki-pipeline.sqlite3`의 `verify_jobs` 행 하나를 작업 하나로 처리한다. 위키 폴더에는 검수 중인 사본이나 숨김 파일을 두지 않는다. 카드 파일을 바꾸는 쓰기는 모두 pipeline의 `publish_card`를 거친다([pipeline](../pipeline/README.md)).

## 행 모델

- compile이 행을 만든다(`review_queue.py`). payload의 `kind`는 둘 중 하나다.
  - `review`: 첫 카드. compile은 `review` 행을 먼저 넣고 `publish_card(expected=None)`로 `generated` 카드를 바로 게시한다(`publish_first_card`). 카드는 검수 전에도 보이고, `recallVerifiedOnly` 기본값 아래에서는 recall에 나오지 않는다. 행만 있고 게시 전에 멈추면 worker는 `card_missing`으로 행을 닫고, claim이 남은 compile job은 다음 claim이 되돌려 다시 compile한다.
  - `replace`: 기존 카드의 교체본. 새 이미지는 행의 `pending_blob`에 있고 `expectedTargetHash`는 교체 대상 카드의 SHA-256이다. 보이는 카드는 통과 게시 전까지 그대로다.
- 종결 상태: `done`(+`reason`: `verified`·`kept`·`card_missing`·`changed_before_verify`·`changed_during_verify`·`stale_revision`·`stale_job`·`superseded`·`invalid_verdict`·`replacement_invalid`·`not_generated`·`source_folder_excluded`·`unsafe_target_path`), `queued`(식힘·127·timeout·잠금 경합·게시 보류로 보존), `dropped`(`requeue_exhausted`). `done`과 `dropped` 행은 `pending_blob`을 비운다.
- compile은 같은 원문 revision의 열린 `replace` 행이 있고 live 카드가 그 행의 기대값과 같으면 다시 추출하지 않는다(`pending_source_review`, `awaiting_pending_verification`).

## 한 run

`run`은 compile worker가 끝날 때 한 번 돈다.

1. `recover_publishes`로 중단된 게시를 복구하고(복구할 수 없는 txn 하나는 `aborted`로 닫고 나머지와 이 pass는 계속한다. 원장이나 lock 파일을 열 수 없을 때만 `publish_recovery_unavailable`로 pass를 끝낸다), `recover_dead_claims('verify')`로 죽은 worker의 claim을 되돌린다. 네 번째 claim에 이른 행은 `dropped/requeue_exhausted`로 닫는다.
2. `claim_verify_batch`로 예산만큼 claim한다. 같은 대상에 더 새 행이 있으면(상태 무관) 옛 행은 `done/superseded`다. 그 run이 만든 카드를 먼저 처리하고, backlog가 있으면 1건을 예약한다.
3. 행마다 `process_row`가 재확인 → 유료 검수 1회 → 판정 기록 → 게시를 한다. 결과를 `finish_verify_job`으로 남기고, 끝에 `release_verify_claim`이 남은 claim을 `queued`로 돌린다.

재확인은 유료 호출 전에 한다. `replace`는 live 카드 SHA가 `expectedTargetHash`와 같아야 하고, `review`는 카드가 있고 `generated`이며 기대값이 있으면 그 값과 같아야 한다. 다르면 사람이 고친 것으로 보고 `changed_before_verify`로 닫는다. 카드의 `sourceHash`가 행과 다르면 `stale_job`, 미해결 refresh가 있으면 보존한다.

판정은 `_verdict`로 행 payload에 기록한다. 판정 뒤 게시 전에 멈춘 행은 다음 run이 기록된 판정을 재사용해 추가 유료 호출 없이 게시한다. 렌더한 stamp 바이트(`rendered`)도 같이 기록해, 게시가 이미 반영된 재개 행은 live SHA 대조만으로 `verified`로 닫는다(`verified_replay`). 유료 호출 뒤 판정 기록 전에 멈추면 다시 검수하고, claim 횟수 상한이 반복을 막는다.

검수 중 원문이 바뀌면 게시하지 않고 `stale_revision`으로 닫는다. 새 revision은 refresh 원장과 compile이 새 행으로 낸다. 게시 CAS가 실패하면(검수 중 사람 편집) 사람 파일을 두고 `changed_during_verify`로 닫는다. 같은 행을 다시 검수하지 않는다.

## 판정

- `pass`: `verification_stamp.render_verification_text`로 `status: verified`와 증명 필드 4개(`verifiedBy`·`verifiedAt`·`verifiedMode`·`verifiedBodyHash`)를 한 번에 렌더하고, `publish_card(expected=검수한 바이트의 SHA)`로 교환 게시한다. 교체는 기존 카드의 권한 비트를 유지한다.
- `fail`·`inconclusive`: 카드를 지우지 않는다(`done/kept`). 첫 카드는 `generated`로 남고, 교체본은 행 종결과 함께 버려진다. 첫 카드의 같은 revision은 `already_compiled_revision`이 재추출을 막고, 교체의 같은 revision은 수명 주기의 refresh 재시도(7일)에서만 다시 들어간다. `compile.verify.onFail`·`onInconclusive` 설정 키는 허용 목록에 남아 있으나 동작에 쓰이지 않는다.
- `pass`는 근거를 요구한다. `claims`가 비거나 항목이 전부 퇴화한 값(`null`·`""`·`{}`)이면 `invalid_verdict`다. 이 경로는 카드를 그대로 두고 행을 닫는다. 근거 없는 `pass`를 `fail`·`inconclusive`로 바꾸지 않는다. `supported`·`quote` 값은 보지 않는다.
- 카드를 고쳐 `generated`로 리셋할 때 증명 필드는 키째 사라진다. 리셋 경로의 frontmatter merge가 `VERIFY_PROOF_FIELDS`의 키를 옛 frontmatter에서 건너뛴다. 새 증명 키는 이 튜플에 넣는다.

## 예산과 엔진

- per-run 예산은 `max(verify.maxPerRun, min(그 run이 만든 카드 수, VERIFY_PRODUCED_HARD_CAP))`다. 생산량은 모델 출력에서 나오므로 상한(30)을 씌운다. worker는 개수가 아니라 경로 목록(`produced_targets`)을 넘기고, verify는 그 카드를 먼저 처리한다. 생산이 없는 run에서는 예산이 커지지 않는다.
- 검수 근거의 권위는 행이 갖는다. `wiki_compile_worker`가 실제 원문을 목록 앞에 두고 같은 경로의 모델 항목을 버리며(`is_same_file_source`), 실제 원문을 `authoritativeSources`에 따로 적어 `load_sources`가 `MAX_SOURCES`(3)와 무관하게 읽는다. 이 필드는 행에만 있고 frontmatter로 나가지 않는다.
- 카드를 만든 엔진이 별도 호출로 검수한다(`plan_verify_attempts`, `verifiedMode: self`). 다른 CLI로 대신 검수하지 않는다. 생산 엔진 라벨은 행의 `engine`이 정하고, worker는 `candidate.engine`을 행 값으로 덮어쓴다. 해석할 수 없는 라벨은 `producing_engine_unavailable`로 행을 보존한다.
- 유료 호출은 행당 run당 1회다. 실패(인증 안 된 CLI·verdict 미출력·timeout)는 그 엔진을 식히고(`set_engine_cooldown`, 24시간 상한) 식힘 중이면 `engines_cooling`으로 보존한다. 이 경로들은 판정을 만들지 않는다. `invalid_extractor_json`·`invalid_verdict`는 같은 입력에서 재현되므로 행을 닫는다. 기존 카드의 자동 재검증은 없다.
- 판정은 `audit_events`의 `verify_log` 종류로 남는다(조회: `entry/run.py wiki-ledger verify-log`). 로그 키 `producedBy`(생성)·`engine`(검수)·`verifiedMode`(달성)의 의미는 고정이다. recall은 `verifiedMode`를 읽지 않는다.

## compile intent

선택된 설치에서 행에 `sourceFingerprints`가 있으면, `pass` 분기는 stamp 바이트를 만든 뒤 `_provenance_gate`가 checkout을 새로 capture해 view와 (worktree, branch)가 지문과 같을 때만 intent를 `<install state root>/provenance-intents/compile-<id>.json`에 쓴다. intent에는 경로·카드 SHA·`expectedTargetHash`·지문·`lines-v1` span·verdict/result digest·`verifiedMode`·검수 엔진·생산 엔진이 들고 원문과 본문은 없다. 게시가 `published`가 아니면 intent를 `.superseded`로 바꾼다. checkout capture 실패는 `provenance_checkout_unavailable`, branch 변경은 `provenance_checkout_changed`, digest 입력 오류는 `provenance_value_invalid`로 로그에 남기고 intent 없이 진행한다. intent 쓰기가 실패하면 행은 `provenance_intent_write_failed`로 보존된다. sync가 intent를 소비해 compiled attestation을 기록한다([sync](../../sync/README.md)).

## 모듈

- `wiki_verify_worker.py` — `run`·`process_row`·유료 검수(`_paid_verdict`)·통과 게시(`_publish_pass`).
- `review_queue.py` — compile 쪽 행 생산(`enqueue_review`·`publish_first_card`)과 추출 전 대기 판정(`pending_source_review`).
- `verification_stamp.py` — 검수 status와 증명 필드를 한 벌의 바이트로 렌더하고 쓰는 유일한 writer(`render_verification_text`·`stamp_verification`·`write_rendered_verification`). 프런트매터 렌더 규칙은 card의 `render_frontmatter_fields`가 소유한다.
- `verify_ledger.py` — 원장 쓰기 가능성 preflight(`ledger_writable`)와 억제 키의 본문 해시(`source_body_hash`).
- `intent_ack.py` — compile intent id와 `.superseded` 처리.

수명 주기(`wiki_lifecycle`)는 [lifecycle](../lifecycle/README.md)에 있다.

# 위키 원문 불일치·방치 generated 카드 수정 진행 기록

## 레인 A: 원문을 고치지 않은 옛 브랜치 tip은 원문 보유 근거가 아니다

### 규칙

`git_branch_safety.LineageJudge._on_other_branch`는 HEAD에 병합되지 않은 로컬 브랜치 tip 중 원문 경로를 가진 tip을 센다. HEAD 계보에 그 경로의 삭제 커밋(`_deletion_commit`)이 있고, 원문을 가진 모든 tip이 그 경로를 `merge-base --all(tip, HEAD)`의 모든 merge-base와 같은 entry(mode·type·blob)로 가지면 그 tip들은 근거에서 빠진다. 그 브랜치를 병합해도 HEAD의 삭제가 남기 때문이다. 다음은 그대로 보류한다.

- merge-base에 그 경로가 없다(tip이 원문을 새로 추가했다).
- tip의 entry가 merge-base와 다르다(tip이 원문을 고쳤다).
- merge-base가 없다(공통 조상 없는 브랜치).
- HEAD 계보에서 삭제 커밋을 찾지 못한다.
- 미병합 tip이 `MAX_BRANCH_TIPS`(64)를 넘거나 git이 답하지 못한다(`GitUnavailable`, run deadline).

git 호출: tip별 `ls-tree`는 기존과 같은 수(entry를 함께 읽는다)다. 근거 판정이 필요한 tip마다 `merge-base --all` 1회, merge-base마다 그 tip이 가진 원문 경로 묶음에 `ls-tree` 1회(`MAX_PATHSPECS_PER_CALL`개씩)를 더한다. 결과는 tip·merge-base·경로 단위로 캐시한다. 모든 호출은 judge의 run deadline을 따른다.

`wiki_source_retire.retire_deleted_source`의 묶음 보류는 그대로다. 원문을 인용한 카드가 둘 이상이고 그중 한 장이 보류되면, 막은 카드 경로와 사유 코드를 `wiki` 활동 이벤트(`outcome: held`)로 남긴다. 이 이벤트는 추출기 `logTail`(`_WIKI_OUTCOMES`)에 들어가지 않는다.

### 상태 조합표

판정 입력은 카드 frontmatter `sourceRevisions`와 git뿐이다. 원장(`wiki-pipeline.sqlite3`)을 읽거나 쓰지 않는다. 1.0.x~1.1.6이 쓴 카드는 같은 `sourceRevisions` 모양이므로 쓴 버전 축의 차이가 없다. 은퇴의 영속 쓰기(`retired-wiki/` 보관본, 카드 unlink, `retired` 활동 이벤트)는 바뀌지 않는다. 유료 호출(추출 E·검수 V)은 전 칸 0이다.

| # | 카드 | 이 view의 원문 | 미병합 tip | 수정 전 | 기대(수정 후) | 유료 | 시험 |
|---|---|---|---|---|---|---|---|
| D1 | 단일 원문, 삭제 전 커밋 | 삭제 커밋이 HEAD 계보 | 없음 | 은퇴 | 은퇴 1, 보관본 1 | 0 | `test_d1_deletion_after_card_commit_retires` |
| D2 | 단일 원문, 삭제 뒤 커밋 | 삭제 커밋이 HEAD 계보 | 삭제 전에 갈라졌고 원문 미수정 | 영구 HOLD | 은퇴 1, 보관본 1, tip의 원문은 그대로 | 0 | `test_d2_stale_snapshot_tip_no_longer_holds_committed_card`, `test_d2_dry_run_counts_the_card_as_deletable` |
| D3 | 단일 원문, 삭제 뒤 커밋 | 삭제 커밋이 HEAD 계보 | 갈라진 뒤 원문을 고침 | HOLD | HOLD `source_on_other_branch`, 카드 유지 | 0 | `test_d3_tip_that_edited_the_source_still_holds` |
| D4 | 단일 원문 | HEAD 계보에 없음, 다른 브랜치가 추가 | 원문을 추가 | HOLD | HOLD(기존 시험 2개) | 0 | `test_source_delete_job_on_other_branch_is_held_not_retired`, `test_dry_run_reports_held_cards_without_writing` |
| D4' | 단일 원문, 삭제 뒤 커밋 | 삭제 커밋이 HEAD 계보 | 삭제 뒤 갈라져 같은 경로를 다시 추가(merge-base에 없음) | HOLD | HOLD `source_on_other_branch` | 0 | `test_d4_tip_that_re_added_the_path_after_deletion_still_holds` |
| D5 | 단일 원문 | working tree에서만 삭제(HEAD·index에 있음) | 원문 미수정 tip | HOLD `source_tracked_in_head` | 같음 | 0 | `test_d5_working_tree_only_deletion_still_holds` |
| D6 | 같은 원문 카드 2장(삭제 전·뒤 커밋) | 삭제 | D2형 tip | 전부 HOLD | 2장 모두 은퇴, `held` 이벤트 0 | 0 | `test_d6_bundle_with_stale_tip_retires_every_card` |
| D7 | 같은 원문 카드 2장(삭제 전·뒤 커밋) | 삭제 | D3형 tip | 전부 HOLD | 전부 HOLD(묶음 유지), 막은 카드 1장을 `held` 이벤트로 기록, `logTail`에 없음 | 0 | `test_d7_bundle_with_edited_tip_holds_and_names_the_blocking_card` |
| D8 | 공유 카드(원문 2개 중 1개 삭제, 삭제 뒤 커밋) | 한쪽 삭제 | D2형 tip | `held_branch` | `shared_pending`(survivor refresh 경로), 카드 유지 | 이 시험 0. 이후 compile은 survivor 원문당 E1 + 카드당 V1 | `test_d8_shared_card_with_stale_tip_goes_to_shared_pending` |
| D9 | 커밋 안 한 카드 | 삭제 커밋이 HEAD 계보 | D2형 tip | 3a HOLD | 3b 은퇴 1 | 0 | `test_d9_uncommitted_card_with_stale_tip_retires` |

추가 단정:

- 호출 수: 같은 원문을 인용한 카드 3장을 D2형 tip 2개로 판정할 때 두 번째 이후 카드의 판정은 git 호출 0회다(`test_stale_tip_lookups_are_cached_per_run`).
- tip 상한: 미병합 tip이 `MAX_BRANCH_TIPS`를 넘으면 D2형이어도 HOLD(`test_too_many_unmerged_tips_still_holds`).

중단 지점: 판정은 읽기 전용이다. 은퇴 쓰기의 중단 지점(보관 전, 보관 뒤 unlink 전, unlink 뒤 sync wake 전)은 기존 `retire_deleted_source`·`retire_card`와 같고 이 변경이 바꾸지 않는다. `held` 이벤트 쓰기는 판정이 읽지 않는 진단 행이라 실패해도 다음 run이 다시 남긴다.

### 호출부 (CodeGraphContext, `repo_path`=이 worktree)

바뀐 기호: `LineageJudge.prefetch`, `LineageJudge._on_other_branch`, 새 `LineageJudge._unchanged_since_base`·`_merge_bases`·`_base_entry`, `wiki_source_retire.lineage_hold`(새 `_lineage_blocker`에 위임), 새 `_record_held`.

제품 호출부:

- `LineageJudge._judge` → `_on_other_branch`(규칙 2c·3a). `judge`는 `wiki_lifecycle.hold_invisible_sources`(`run`·`dry_run`)와 `wiki_source_retire.lineage_hold`가 부른다.
- `LineageJudge.prefetch`: `wiki_lifecycle.hold_invisible_sources`, 내부 `_in_index`·`_on_other_branch`. `DeletionProof`(schema3)는 상속한 `prefetch`를 `_in_index`로 쓴다. tip `ls-tree` 호출 수는 그대로이고 `DeletionProof`는 tip을 판정하지 않는다.
- `wiki_source_retire.lineage_hold`: `retire_deleted_source`. `retire_deleted_source`는 `wiki_lifecycle.run`(`MISSING`)과 `wiki_compile_worker.process_job`(`sourceDelete` 행)이 부른다. 반환 상태(`held_branch`·`retired`·`shared_pending`·`blocked`)의 집합은 그대로다.
- `laya_index_host_route`는 `repo_facts`만 쓴다. 영향 없음.

테스트 호출부:

- `memvane/wiki/lifecycle/tests/integration/test_lifecycle_git_boundaries.py`(기존 D4 시험 2개 포함)
- `memvane/wiki/lifecycle/tests/integration/test_wiki_source_retire.py`(`run_retire`)
- `memvane/wiki/lifecycle/tests/integration/test_git_deletion_proof.py`(`test_prefetch_uses_protected_git_environment`)
- 새 `memvane/wiki/lifecycle/tests/integration/test_lifecycle_stale_branch_tips.py`

영속 데이터 writer·reader:

- 카드 frontmatter `sourceRevisions`: writer는 compile(`wiki_card.dump_source_revisions`·`insert_source_revisions`, 출시 1.x 동일). reader는 lifecycle 판정. 모양 변화 없음.
- `wiki` 활동 이벤트(`<install state root>/events.jsonl`): 새 `outcome: held`. reader는 `event_log.wiki_tail`(현재·v1.1.4·v1.1.5 모두 `_WIKI_OUTCOMES` 밖의 outcome을 버린다)과 `memvane:logs` 스킬(일반 이벤트로 표시)이다.

### 검증

- `npm run test:focused -- memvane/wiki/lifecycle/tests/run.test.mjs`: 11개 파일 통과. 새 파일의 시험은 12개이고 `test_lifecycle_git_boundaries.py`의 9개(기존 D4 시험 2개 포함)가 통과한다.
- 같은 검사를 수정 전 `git_branch_safety.py`로 돌리면 새 시험 6개(D2 2개, D6, D8, D9, 캐시)가 실패하고 나머지(D1, D3, D4', D5, D7, tip 상한)는 통과한다.
- `npm run test:focused -- memvane/wiki/compile/tests/run.test.mjs`(`sourceDelete` 호출부): 24개 파일 통과.
- `/usr/bin/python3 -B`(3.9.6)로 `git_branch_safety`·`wiki_source_retire` import 확인.

## 레인 D: 검수 행 없이 남은 generated 카드 재적재와 kept 억제

### 범위

- cutover 이관: `pipeline.migrate_from_v1(reviews=...)`가 1.x `verify-queue.jsonl`·`verify-queue.jsonl.claimed.*`의 미완료 행 중 verify `review_queue.v1_reviews`가 고른 행을 마커와 같은 트랜잭션에 `verify_jobs` `kind: review` 행으로 넣는다.
- 1회 재적재: compile worker가 cutover 확인 직후 `reload_unreviewed_cards`를 부른다. verify `review_queue.reload_payloads`가 기계 소유·fresh `generated` 카드마다 행을 만들고, pipeline `review_reload.reload_reviews`가 정리된 카드를 빼고 넣은 뒤 같은 트랜잭션에 `pipeline_meta` `review_reload_v1_at`을 쓴다.
- kept 억제: verify가 `done/kept`로 닫기 전에 검수한 바이트의 SHA-256을 키·값으로 `suppressions` `verify_kept` 행을 쓴다. DDL은 그대로다.

### 결정

- 이관 조건: 교체 행(`pendingPath`)은 버린다(교체 이미지는 cutover가 숨김 파일과 함께 지운다). 대상 카드가 기계 소유 `generated`이고 관리 블록 `sourceHash`·`sourceRevisions`가 행과 같고(`candidate_matches_job`), 행에 `expectedTargetHash`가 있으면 바이트 SHA가 그 값과 같아야 한다. 1.0.x~1.1.0의 첫 카드 행에는 `expectedTargetHash`가 없으므로 관리 블록·revision 일치가 "행의 기대값"이다. 같은 대상의 행이 여럿이면 마지막 행만 본다. 원문이 fresh가 아니면 넣지 않는다(검수 worker가 유료 호출 없이 `stale_revision`으로 닫을 행이고, 그 카드는 refresh compile이 이어받는다).
- 생산 엔진: 카드 frontmatter와 1.x manifest 행에는 엔진이 없다. 재적재 행의 `engine`은 같은 대상의 최근 verify 행 엔진, 없으면 프로젝트 verify 행의 유일한 엔진이다. 둘 다 없거나 verifier가 부를 수 없는 엔진이면 원장 트랜잭션이 열린 행·같은 SHA의 종결 행·`verify_kept` 억제를 먼저 본 뒤, 그중 어느 것도 없는 카드만 넣지 않고 `engineUnknown`으로 센다. 부를 수 없는 엔진의 행은 `producing_engine_unavailable`로 매 pass 보류되고, verify는 다른 엔진으로 대신 검수하지 않기 때문이다. 관측(11-28): qmd-auto-context와 service-engineering의 verify 행 엔진은 모두 `claude`다.
- 억제 키: 카드 바이트 SHA-256(키와 `body_hash` 모두). 교체 행의 kept는 버려진 교체 이미지의 SHA라 재적재 판정에 닿지 않는다. `verify_kept` 행은 보존 정리가 지우지 않는다(현재와 출시된 1.1.4~1.1.6의 `source_gone`이 모르는 kind를 `False`로 본다).
- `done` 행 판정: 같은 대상의 `done`·`dropped` 행 중 `expectedTargetHash`가 live SHA와 같은 행. `dropped`(claim 4회 소진)도 같은 바이트를 다시 사지 않도록 포함한다.
- 검수가 꺼진 설정에서는 재적재를 하지 않고 마커도 쓰지 않는다. 켜지면 그 회차에 한 번 돈다.
- 쓰기 반환값 분류: `_record_kept`는 1번(실패하면 행을 `queued`로 돌려 기록된 판정으로 다시 쓴다), 재적재·이관 행은 마커와 한 트랜잭션이라 별도 확인이 없다, `migration` 로그 행(`reviewReload`)은 3번.

### 상태 조합표 (보고서 5.3 보완, 축: 행 상태 × 보존 정리 전후 × 쓴 버전)

유료 V = 검수 호출. "다음 pass" 열은 같은 입력으로 한 번 더 돌린 결과다.

| # | 상황 | 보존 정리 | 쓴 버전 | 기대 | 유료 | 다음 pass | 시험 |
|---|---|---|---|---|---|---|---|
| G1a | 2.0 첫 카드, 열린 `review` 행 | 전 | 현재 compile | 재적재가 `open`으로 건너뜀, 행 1개 유지 | V1 | V0 | `test_g1_an_open_row_is_not_queued_twice` |
| G1b | 행만 있고 카드 없음(G6) | 전 | 현재 compile | 재적재 0행, verify `card_missing` | V0 | V0 | `test_g6_a_row_without_its_card_closes_without_a_call_and_reloads_nothing` |
| G2a | 1.x 대기 행, 카드 그대로 | — | v1.0.24(claim 파일)·v1.1.0(큐 파일) writer | cutover가 `review` 행으로 이관, 통과 게시 | V1/카드 | V0, 재적재 0 | `test_g2_cutover_imports_release_rows_of_fresh_unchanged_generated_cards` |
| G2b | 1.x 대기 행, 카드 삭제됨 | — | 같음 | 이관 안 함 | V0 | V0 | 같은 시험(gamma) |
| G2c | 1.x 대기 행 2개(같은 카드 refresh) | — | 같음 | 마지막 행만 이관 | V1 | V0 | 같은 시험(delta) |
| G2d | 이미 출시된 cutover가 행을 버린 프로젝트, fresh 카드 | — | v1.1.0 writer + v1.1.4/v1.1.6 cutover | 재적재 1회, 출시된 1.1.4·1.1.6 verifier가 처리 | V1/카드 | 마커로 0행, V0 | `test_g2_reload_queues_rowless_fresh_cards_once_and_released_verifiers_settle_them` |
| G2e | G2d인데 엔진을 알 수 없음(verify 행 0) | — | 같음 | 넣지 않음, `engineUnknown`, 마커 | V0 | 0행 | `test_reload_leaves_cards_whose_producing_engine_is_unknown` |
| G2f | 사람 소유 `generated` 카드 | — | 사람 | 대상 아님 | V0 | V0 | G2d 시험(human) |
| G3 | G2인데 원문이 이후 바뀜 | — | 1.x writer | 이관·재적재 안 함(`stale`). refresh compile이 이어받음(레인 C) | V0 | 레인 D는 보류 행을 만들지 않음 | G2a·G2d 시험(beta) |
| G4a | `done/kept` | 전 | 현재 verify | 재적재가 `reviewed`로 건너뜀 | V1(최초) | V0 | `test_g1_g4_a_kept_card_is_never_reviewed_again_before_or_after_retention` |
| G4b | `done/kept`, 행이 보존 정리로 지워짐 | 후 | 현재 verify | `verify_kept` 억제로 건너뜀 | V1(최초) | V0 | 같은 시험(retention) |
| G4c | `done/kept`를 출시된 1.1.4~1.1.6 verifier가 씀(억제 없음) | 전 | 1.1.4~1.1.6 | `done` 행으로 건너뜀 | V0 | V0 | G4a와 같은 판정 경로 |
| G4d | G4c인데 재적재 첫 실행 전에 행이 지워짐 | 후 | 1.1.4~1.1.6 | 한 번 다시 검수(재적재는 1회라 반복 없음) | V1 | V0 | 알려진 칸. 원장에 그 판정의 근거가 남지 않는다 |
| G4f | G4b인데 보존 정리로 verify 행이 모두 지워져 엔진을 알 수 없음 | 후 | 현재 verify | `kept`로 센다(억제가 엔진 판정보다 먼저) | V1(최초) | V0 | `test_g4_a_kept_card_whose_engine_is_gone_counts_as_kept` |
| G4e | 억제 쓰기 실패 | — | 현재 verify | 행 `queued`(`kept_suppression_write_failed`), 다음 run이 기록된 판정으로 억제·종결 | V1(최초) | V0 | `KeptSuppression.test_a_failed_suppression_write_keeps_the_row_and_the_retry_does_not_pay` |
| G5a | `done/stale_revision`, 원문이 새 revision | 전 | 현재 verify | 재적재 `stale` | V0 | V0 | `test_g5_…`(stale) |
| G5b | 원문이 옛 바이트로 되돌아감 | 전 | 현재 verify | `done` 행(같은 SHA)으로 건너뜀 | V0 | V0 | `test_g5_…`(reverted) |
| G5c | G5b인데 행이 지워짐, 뒤의 compile이 엔진을 남김 | 후 | 현재 verify | 재적재 1행 | V1 | V0 | `test_g5_…`(reverted_after_retention) |
| G5d | G5b인데 행이 모두 지워지고 다른 행도 없음 | 후 | 현재 verify | `engineUnknown`, 넣지 않음 | V0 | V0 | G2e와 같은 판정(`test_reload_leaves_cards_whose_producing_engine_is_unknown`) |
| G6 | 행만 있고 게시 전 중단 | 전 | 현재 compile | G1b와 같음 | V0 | V0 | G1b 시험 |
| G7 | 판정 뒤 게시 전 중단 | — | 현재 verify | 기존 `verified_replay`·기록 판정 재사용 | V0 | V0 | 기존 `RecordedVerdict` 시험 |
| R1 | 재적재 커밋 전 실패 | — | — | 마커·행 없음, 다음 pass가 정확히 1회 넣음 | V0 | 마커로 0행 | `test_a_failed_reload_writes_nothing_and_the_next_pass_reloads_once` |
| R2 | 검수 꺼짐 | — | — | 재적재·마커 없음 | V0 | 켜질 때 1회 | `test_reload_is_skipped_while_review_is_off` |

### 호출부 (CodeGraphContext `find_callers`, repo_path=이 worktree, `rg` 보완)

- `pipeline.migrate_from_v1`: 제품 `wiki_compile_worker.migrate_v1_state`(이번에 `reviews=wiki_verify.v1_reviews(root, config)`를 넘긴다). 시험 `test_v1_migration.py` 5곳(인자 없이 호출, 1.x 행을 버리는 기존 동작 유지), `test_review_reload.py`.
- `PipelineStore.import_v1_ledgers`: 제품 `migrate.migrate_from_v1` 하나. `reviews` 키워드는 기본값 빈 튜플.
- `migrate_v1_state`: 제품 `wiki_compile_worker._main`. 시험 `test_migration_entry.py` 5곳(검수 꺼짐 설정이라 필터가 엔진 확인 전에 행이 없다).
- `process_row`(kept 억제 추가): 제품 `wiki_verify_worker.run`. 시험 `test_excluded_selected_sources.py`(제외 경로라 억제 전에 끝난다, `store=None`), `test_wiki_provenance_intent.py` 2곳, `tests/e2e/wiki_db_provenance.py`.
- `candidate_matches_job`(재사용): 제품 `review_queue.pending_source_review`, `wiki_verify_worker.process_row`, 새 `review_queue.v1_reviews`.
- `SUPPRESSION_KINDS`(`verify_kept` 추가): 제품 `decisions.set_suppression`(허용 kind 검사), `types.SUMMARY_GROUPS`→`summary.pipeline_summary`(표에 0 포함 새 kind). `suppressions('verify')` reader `wiki_compile_worker.verify_suppression_hash`는 kind가 달라 영향 없다. 시험 `test_pipeline_summary.py`(기대값에 `verify_kept: 0`), `test_pipeline_store.py`(`tombstone` 거절 그대로).
- 새 기호: `pipeline.api.review_reload_marked`·`reload_reviews`, `review_reload.reload_reviews`, `review_queue.v1_reviews`·`reload_payloads`·`reload_unreviewed`, `verify.api.v1_reviews`·`reload_unreviewed`, `wiki_compile_worker.reload_unreviewed_cards`, `wiki_verify_worker._record_kept`. 제품 호출부는 위 진입 하나씩이다.
- 영속 데이터 writer·reader:
  - `verify_jobs` `review` 행 writer: `review_queue.publish_first_card`(현재 compile), `decisions.import_v1_ledgers`(cutover), `review_reload.reload_reviews`(재적재). reader: `wiki_verify_worker`(현재·출시된 1.1.4~1.1.6, 같은 payload 키를 읽는다), `review_queue.pending_source_review`, `summary`, `retention.prune_terminal`.
  - `suppressions` `verify_kept` writer: `wiki_verify_worker._record_kept`. reader: `review_reload.reload_reviews`, `summary`. 보존 정리(현재·1.1.4~1.1.6)는 지우지 않는다.
  - `pipeline_meta` `review_reload_v1_at` writer: `review_reload.reload_reviews`. reader: `pipeline.api.review_reload_marked`, 같은 트랜잭션의 재확인.
  - 1.x `verify-queue.jsonl` writer: 1.0.x~1.1.0 `wiki_compile`(첫 카드·refresh 모두 `expectedTargetHash` 없는 행). 1.1.1~1.1.2는 첫 카드 검수를 `.handoff/v1/hold/`에 두고 이 파일에 쓰지 않는다. reader: `migrate.v1_review_rows`.

### SE `queued` 9·`claimed` 6 정체 (읽기 전용 확인, 11-28)

`errors._query_only_uri`와 `PRAGMA query_only=1`로 service-engineering 원장을 읽었다. 정체가 아니라 진행 중인 backlog다.

- 관측 시점의 열린 행은 `queued` 9(`review` 8·`replace` 1, 모두 `attempt_count` 0, 생성 1.4~1.5시간 전, 엔진 `claude`)이고 `claimed`는 0이다. verify 쿨다운·fence는 없다.
- 최근 12시간에 verify 행 311개가 생기고 302개가 종결됐다. 마지막 `verify_log`는 12분 전이고 최근 40행은 `verified` 35·`inconclusive` 5다.
- compile worker pid 7411이 살아서 compile 행 50개를 claim하고 있다. verify pass는 compile worker 회차 끝에만 돈다(`wiki_compile_worker._run_verify_pass`). 그동안 compile이 만든 행은 `queued`로 기다리고, 보고서 시점의 `claimed` 6은 진행 중이던 verify 배치로 보인다.
- 코드는 바꾸지 않았다.

### 검사

- `npm run test:focused -- --test-name-pattern test_review_reload memvane/wiki/compile/tests/run.test.mjs`: 12/12 통과.
- teeth: `_record_kept`를 no-op, cutover `reviews=[]`, 재적재 마커 쓰기 제거를 한 번에 넣자 `test_review_reload` 9개 칸과 `KeptSuppression` 3개가 실패했다(원복 후 통과).
- `npm run test:focused -- memvane/wiki/verify/tests/run.test.mjs memvane/wiki/pipeline/tests/run.test.mjs`: 모듈 11/11, Python 시험 123개 통과.
- `npm run test:focused -- memvane/wiki/compile/tests/run.test.mjs`: 모듈 25/25, Python 시험 187개 통과.
- `npm run test:focused -- tests/structure/static.test.mjs tests/structure/doc-links.test.mjs`: 27/27 통과. `tests/structure/v1_guard.py --strict`·`--tests`: G1·G5·G9·시험 G1·G8 0.
- `npm run test:focused -- tests/e2e/wiki-db-provenance.test.mjs`: 5/5 통과.
- `/usr/bin/python3 -B -c "import …"`(3.9.6): 바꾼 모듈 9개 import 성공.
- 전체 검사(`npm test`)는 돌리지 않았다(머지 직전 최종 커밋에서 감독이 실행).

### 남은 위험

- 생산 엔진을 원장에서 찾을 수 없는 카드는 재적재하지 않는다(G2e·G5d). 1.x만 쓰다 처음 이관하는 프로젝트에서 1.x 큐 행도 없는 카드가 여기에 걸린다.
- G4d: 출시된 1.1.4~1.1.6 verifier가 `done/kept`로 닫은 행이 재적재 첫 실행 전에 보존 정리로 지워졌다면 그 카드는 한 번 다시 유료 검수된다. 현재 관측된 원장은 11-27 생성이라 30일에 닿지 않았다.
- 1.1.1~1.1.2의 첫 카드 검수 대기(`.handoff/v1/hold/`)는 cutover가 가져오지 않는다. 그 카드는 원문이 fresh면 재적재가 덮는다.
- service-engineering(카드 5,828장)은 재적재 첫 회차에 위키 카드 전체를 한 번 읽는다. 유료 호출은 없다.

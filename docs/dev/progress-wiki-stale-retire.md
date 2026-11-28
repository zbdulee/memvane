# 위키 원문 불일치·방치 generated 카드 수정 진행 기록

## 레인 A: 원문을 고치지 않은 옛 브랜치 tip은 원문 보유 근거가 아니다

### 규칙

`git_branch_safety.LineageJudge._on_other_branch`는 HEAD에 병합되지 않은 로컬 브랜치 tip 중 원문 경로를 가진 tip을 센다. HEAD 계보에 그 경로의 삭제 커밋(`_deletion_commit`)이 있고, 원문을 가진 모든 tip이 그 경로를 `merge-base --all(tip, HEAD)`의 모든 merge-base와 같은 entry(mode·type·blob)로 가지면 그 tip들은 근거에서 빠진다. 그 브랜치를 병합해도 HEAD의 삭제가 남기 때문이다. 다음은 그대로 보류한다.

- merge-base에 그 경로가 없다(tip이 원문을 새로 추가했다).
- tip의 entry가 merge-base 중 하나라도와 다르다(tip이 원문을 고쳤거나 mode를 바꿨다. criss-cross에서 한쪽 merge-base와만 같다).
- merge-base가 없다(공통 조상 없는 브랜치).
- tip이 다른 worktree에 checkout돼 있다(`git worktree list --porcelain`의 다른 worktree `HEAD`). 그 checkout에 원문을 고친 미커밋 변경이 있을 수 있다.
- HEAD 계보에서 삭제 커밋을 찾지 못한다.
- 미병합 tip이 `MAX_BRANCH_TIPS`(64)를 넘거나 git이 답하지 못한다(`GitUnavailable`, run deadline 20초 소진 포함).

증명 순서와 git 호출:

1. `for-each-ref --no-merged=HEAD` 1회로 tip을 모은다.
2. 삭제 커밋이 없으면 tip별 `ls-tree`로 원문 보유를 본다(이전과 같은 경로, tip당 1회).
3. 삭제 커밋이 있으면 `worktree list --porcelain` 1회(run 캐시)로 다른 worktree의 tip을 찾고, 그 tip만 `ls-tree`로 원문 보유를 본다.
4. `log --full-history --no-renames -m <tips> --not HEAD -- <run의 경로들>` 1회(run 캐시, 경로 200개 묶음)가 그 경로를 바꾼 커밋을 내지 않으면 모든 tip이 merge-base와 같은 entry를 가진다. 증명: tip에서 merge-base까지의 경로 위 커밋은 모두 HEAD에서 닿지 않는 커밋이고(merge-base는 최선 공통 조상이다), `--full-history -m`은 한 부모와라도 다른 merge를 낸다. 그 커밋이 모두 부모와 같은 entry면 tip과 각 merge-base의 entry가 같다.
5. 바꾼 커밋이 있으면 그 경로만 `log --format=%H`와 `for-each-ref --contains`로 그 커밋을 가진 tip을 고른다. 그 tip만 `ls-tree`·`merge-base --all`·merge-base `ls-tree`로 정확히 대조하고, 첫 보유 tip에서 멈춘다. 바꾼 커밋이 200개를 넘으면 모든 tip을 대조한다.

실측(임시 git 저장소: 원문 1개, 삭제 뒤 커밋한 카드 1장, 삭제 전에 갈라진 미병합 tip 64개. `LineageJudge` 생성·`prefetch`·`judge` 1회의 git 호출 수와 5회 중 최소 시간. 시간은 같은 머신의 부하에 따라 흔들린다):

| 버전 | tip 64개 모두 미수정 | 1개만 수정 | 64개 모두 수정 |
|---|---|---|---|
| v1.1.6(`25666d85`, 근거 필터 없음) | 72회 1.28–1.84초, HOLD | 72회 1.11초, HOLD | 72회 1.39초, HOLD |
| `eb0613bb` | 137회 1.98–3.51초, 은퇴 | 114회 1.98초, HOLD | 74회 1.41초, HOLD |
| 현재 | 10회 0.17–0.21초, 은퇴 | 15회 0.31초, HOLD | 15회 1.09초, HOLD |

`wiki_source_retire.retire_deleted_source`의 묶음 보류는 그대로다. 인용 카드를 모두 판정해 막은 카드 집합을 구한다. 인용 카드가 둘 이상이고 집합이 마지막으로 남긴 집합과 다를 때만 막은 카드마다 `wiki` 활동 이벤트(`outcome: held`, 사유 코드)를 남긴다. 마지막 집합은 `scan_state.state_dir()/<project_key(root)>-wiki-held.json`(`{"version": 1, "held": {원문: [카드]}}`, 4,096개 상한)에 둔다. 판정이 읽지 않는 파생물이고 1.x 계약 목록에 없다. 이벤트를 모두 쓴 뒤에만 집합을 기록하고(실패하면 다음 pass가 다시 남긴다), 보류가 풀리면 그 원문 항목을 지운다. 기록은 카드·위키 잠금 안에서 하므로 lifecycle과 compile worker가 같은 상태를 본다. 이벤트는 추출기 `logTail`(`_WIKI_OUTCOMES`)에 들어가지 않는다.

은퇴 사유: 규칙 2에서 삭제 커밋이 카드 커밋보다 앞서고 원문을 가진 tip이 없으면 `deleted_before_card_commit`이다(D2). 삭제 커밋이 없으면 `untracked_source_gone`이다. 은퇴 사유는 `judge`의 반환값으로만 쓰이고, 읽는 코드(로그·UI·원장)가 없다. 보류 사유(`heldReasons`, lifecycle 로그)는 바뀌지 않는다.

### 상태 조합표

판정 입력은 카드 frontmatter `sourceRevisions`와 git뿐이다. 원장(`wiki-pipeline.sqlite3`)을 읽거나 쓰지 않는다. 1.0.x~1.1.6이 쓴 카드는 같은 `sourceRevisions` 모양이므로 쓴 버전 축의 차이가 없다. 은퇴의 영속 쓰기(`retired-wiki/` 보관본, 카드 unlink, `retired` 활동 이벤트)는 바뀌지 않는다. 유료 호출(추출 E·검수 V)은 전 칸 0이다.

| # | 카드 | 이 view의 원문 | 미병합 tip | v1.1.6 | 기대(현재) | 유료 | 시험 |
|---|---|---|---|---|---|---|---|
| D1 | 단일 원문, 삭제 전 커밋 | 삭제 커밋이 HEAD 계보 | 없음 | 은퇴 | 은퇴 1, 보관본 1 | 0 | `test_d1_deletion_after_card_commit_retires` |
| D2 | 단일 원문, 삭제 뒤 커밋 | 삭제 커밋이 HEAD 계보 | 삭제 전에 갈라졌고 원문 미수정 | 영구 HOLD | 은퇴 1(`deleted_before_card_commit`), 보관본 1, tip의 원문은 그대로 | 0 | `test_d2_stale_snapshot_tip_no_longer_holds_committed_card`, `test_d2_dry_run_counts_the_card_as_deletable` |
| D3 | 단일 원문, 삭제 뒤 커밋 | 삭제 커밋이 HEAD 계보 | 갈라진 뒤 원문을 고침 | HOLD | HOLD `source_on_other_branch`, 카드 유지 | 0 | `test_d3_tip_that_edited_the_source_still_holds` |
| D3m | 단일 원문, 삭제 뒤 커밋 | 삭제 커밋이 HEAD 계보 | 갈라진 뒤 mode만 바꿈(100644→100755) | HOLD | HOLD | 0 | `test_tip_that_only_changed_the_mode_holds` |
| D4 | 단일 원문 | HEAD 계보에 없음, 다른 브랜치가 추가 | 원문을 추가 | HOLD | HOLD(기존 시험 2개) | 0 | `test_source_delete_job_on_other_branch_is_held_not_retired`, `test_dry_run_reports_held_cards_without_writing` |
| D4' | 단일 원문, 삭제 뒤 커밋 | 삭제 커밋이 HEAD 계보 | 삭제 뒤 갈라져 같은 경로를 다시 추가(merge-base에 없음) | HOLD | HOLD `source_on_other_branch` | 0 | `test_d4_tip_that_re_added_the_path_after_deletion_still_holds` |
| D5 | 단일 원문 | working tree에서만 삭제(HEAD·index에 있음) | 원문 미수정 tip | HOLD `source_tracked_in_head` | 같음 | 0 | `test_d5_working_tree_only_deletion_still_holds` |
| D6 | 같은 원문 카드 2장(삭제 전·뒤 커밋) | 삭제 | D2형 tip | 전부 HOLD | 2장 모두 은퇴 | 0 | `test_d6_bundle_with_stale_tip_retires_every_card` |
| D7 | 같은 원문 카드 2장(삭제 전·뒤 커밋) | 삭제 | D3형 tip | 전부 HOLD | 전부 HOLD(묶음 유지), 막은 카드를 `held` 이벤트로 기록, `logTail`에 없음 | 0 | `test_d7_bundle_with_edited_tip_holds_and_names_the_blocking_card` |
| D7r | 같은 원문 카드: 은퇴 가능 2장 + 막는 카드 1장, 이어 막는 카드 1장 추가, 이어 D3형 tip 삭제 | 삭제 | D3형 tip → 없음 | 전부 HOLD, 이벤트 없음 | lifecycle 2회: `held` 1건. 막는 카드 추가 뒤 2회: 2건 추가(누적 3). tip 삭제 뒤: 4장 은퇴, `held` 추가 0, `retired` 4 | 0 | `test_d7_held_bundle_logs_once_per_change_of_blocking_cards` |
| D8 | 공유 카드(원문 2개 중 1개 삭제, 삭제 뒤 커밋) | 한쪽 삭제 | D2형 tip | `held_branch` | `shared_pending`(survivor refresh 경로), 카드 유지 | 이 시험 0. 이후 compile은 survivor 원문당 E1 + 카드당 V1 | `test_d8_shared_card_with_stale_tip_goes_to_shared_pending` |
| D9 | 커밋 안 한 카드 | 삭제 커밋이 HEAD 계보 | D2형 tip | 3a HOLD | 3b 은퇴 1 | 0 | `test_d9_uncommitted_card_with_stale_tip_retires` |
| D13 | 단일 원문, 삭제 뒤 커밋 | 삭제 커밋이 HEAD 계보 | D2형 tip이 다른 worktree에 checkout됨(미커밋 원문 변경) | HOLD | HOLD. worktree 제거 뒤 은퇴 1 | 0 | `test_d13_stale_tip_checked_out_in_another_worktree_holds` |
| X1 | 단일 원문, 삭제 뒤 커밋 | 삭제 커밋이 HEAD 계보 | criss-cross tip(merge-base 2개), 원문을 고쳤다 되돌림 | HOLD | 은퇴 1(정밀 대조 경로, merge-base 2개 모두 같음) | 0 | `test_criss_cross_tip_that_restored_the_source_retires` |
| X2 | 단일 원문, 삭제 뒤 커밋 | 삭제 커밋이 HEAD 계보 | criss-cross tip, 원문이 한쪽 merge-base와만 같음 | HOLD | HOLD | 0 | `test_criss_cross_tip_matching_only_one_merge_base_holds` |
| B1 | 단일 원문, 삭제 뒤 커밋 | 삭제 커밋이 HEAD 계보 | D2형 tip 64개 → 65개 | HOLD | 64개: 은퇴 판정(`wouldDelete` 1). 65개: HOLD `source_on_other_branch` | 0 | `test_branch_tip_bound_is_exact_at_max_branch_tips` |

D10(사람이 고친 카드)·D11(카드 2,048장 초과)·D12(rename)는 보고서 5.1의 칸이고 이 레인이 동작을 바꾸지 않는다.

추가 단정: D2형 tip 2개와 같은 원문 카드 3장을 판정할 때 tip 관련 git 호출은 `for-each-ref` 1, `worktree list` 1, union `log` 1회이고 `merge-base --all`과 tip `ls-tree`는 0회다(`test_stale_tips_are_proved_with_one_log_call_per_run`).

중단 지점: 판정은 읽기 전용이다. 은퇴 쓰기의 중단 지점(보관 전, 보관 뒤 unlink 전, unlink 뒤 sync wake 전)은 기존 `retire_deleted_source`·`retire_card`와 같고 이 변경이 바꾸지 않는다. `held` 이벤트 뒤 상태 파일 기록 전에 중단되면 다음 pass가 같은 이벤트를 한 번 더 남긴다. 상태 파일 기록 실패도 같다(`write_state_atomic` 예외는 무시한다. 손해는 진단 행 중복뿐이다).

### 호출부 (CodeGraphContext, `repo_path`=이 worktree, 새 기호는 `rg`로 보완)

바뀐 기호: `LineageJudge.prefetch`(tip 조회를 하지 않고 경로만 등록), `LineageJudge._on_other_branch`, `LineageJudge._judge`(사유 `deleted_before_card_commit`), 새 `_tip_entry`·`_touched_on_tips`·`_touching_tips`·`_busy_tips`·`_is_here`·`_unchanged_since_base`·`_merge_bases`·`_base_entry`·`_z_entries`. `wiki_source_retire`: 새 `_lineage_blockers`·`_log_held_change`·`_held_state`·`_save_held_state`·`_held_state_path`, `_record_held`(bool 반환). `lineage_hold`는 제품·테스트 호출부가 0이라 지웠다(`rg -n lineage_hold` 0건).

모듈 배치: tip 소유 판정(`_on_other_branch`·`_tip_entry`·`_touched_on_tips`·`_touching_tips`·`_busy_tips`·`_is_here`·`_unchanged_since_base`·`_merge_bases`·`_base_entry`)과 git 출력 파서(`_nfc`·`_z_paths`·`_z_entries`), `MAX_PATHSPECS_PER_CALL`은 `memvane/wiki/lifecycle/branch_tips.py`의 mixin `BranchTips`에 있다. `LineageJudge(BranchTips)`가 `_git`(run deadline)·캐시·`_unmerged_tips`·`_deletion_commit`을 제공한다. G4 파일 상한 800줄 안에 두기 위한 배치다(`git_branch_safety.py` 779줄, `branch_tips.py` 153줄).

제품 호출부:

- `LineageJudge._judge` → `_on_other_branch`(규칙 2c·3a). `judge`는 `wiki_lifecycle.hold_invisible_sources`(`run`·`dry_run`)와 `wiki_source_retire._lineage_blockers`(`retire_deleted_source`)가 부른다.
- `LineageJudge.prefetch`: `wiki_lifecycle.hold_invisible_sources`, 내부 `_in_index`·`_on_other_branch`. `DeletionProof`(schema3)는 상속한 `prefetch`를 `_in_index`로 쓴다. `prefetch`가 tip `ls-tree`를 하지 않으므로 `DeletionProof`의 prefetch 호출은 tip 수만큼 줄고, `DeletionProof`는 tip을 판정하지 않는다.
- `wiki_source_retire.retire_deleted_source`: `wiki_lifecycle.run`(`MISSING`)과 `wiki_compile_worker.process_job`(`sourceDelete` 행). 반환 상태(`held_branch`·`retired`·`shared_pending`·`blocked`·`source_reappeared`)와 `reason`(첫 보류 카드의 사유)은 그대로다.
- `laya_index_host_route`는 `repo_facts`만 쓴다. 영향 없음.

테스트 호출부:

- `memvane/wiki/lifecycle/tests/integration/test_lifecycle_git_boundaries.py`(기존 D4 시험 2개 포함)
- `memvane/wiki/lifecycle/tests/integration/test_wiki_source_retire.py`(`run_retire`)
- `memvane/wiki/lifecycle/tests/integration/test_git_deletion_proof.py`(`test_prefetch_uses_protected_git_environment`)
- `memvane/wiki/lifecycle/tests/integration/test_lifecycle_stale_branch_tips.py`(17개)

영속 데이터 writer·reader:

- 카드 frontmatter `sourceRevisions`: writer는 compile(`wiki_card.dump_source_revisions`·`insert_source_revisions`, 출시 1.x 동일). reader는 lifecycle 판정. 모양 변화 없음.
- `wiki` 활동 이벤트(`<install state root>/events.jsonl`): `outcome: held`. writer는 `_record_held`. reader는 `event_log.wiki_tail`(현재·v1.1.4·v1.1.5 모두 `_WIKI_OUTCOMES` 밖의 outcome을 버린다)과 `memvane:logs` 스킬(일반 이벤트로 표시)이다.
- `<project_key>-wiki-held.json`: writer·reader 모두 `_log_held_change`. 출시 버전은 이 파일을 모른다.

### 검증

- `npm run test:focused -- memvane/wiki/lifecycle/tests/run.test.mjs`: 11개 파일 통과(새 파일 17개 시험, `test_lifecycle_git_boundaries.py` 9개).
- 같은 검사를 `eb0613bb`의 `git_branch_safety.py`·`wiki_source_retire.py`로 돌리면 새 시험 5개(`test_d2_stale_snapshot_tip_no_longer_holds_committed_card`, `test_d7_held_bundle_logs_once_per_change_of_blocking_cards`, `test_d13_stale_tip_checked_out_in_another_worktree_holds`, `test_criss_cross_tip_that_restored_the_source_retires`, `test_stale_tips_are_proved_with_one_log_call_per_run`)가 실패한다.
- `npm run test:focused -- memvane/wiki/compile/tests/run.test.mjs`(`sourceDelete` 호출부): 24개 파일 통과.
- `/usr/bin/python3 -B`(3.9.6)로 `git_branch_safety`·`wiki_source_retire` import 확인.

## 레인 D: 검수 행 없이 남은 generated 카드의 cutover 이관과 1회 재적재

### 범위

- cutover 이관: `pipeline.migrate_from_v1(reviews=...)`가 1.x `verify-queue.jsonl`·`verify-queue.jsonl.claimed.*`의 미완료 행 중 verify `review_queue.v1_reviews`가 고른 행을 마커와 같은 트랜잭션에 `verify_jobs` `kind: review` 행으로 넣는다. 필터를 받은 cutover는 같은 트랜잭션에 재적재 마커 `review_reload_v1_at`도 쓴다. 이 버전이 이관하는 프로젝트는 재적재를 하지 않는다.
- 1회 재적재: 출시된 1.1.4~1.1.6 cutover가 1.x 검수 큐를 버린 프로젝트에만 해당한다. compile worker가 cutover 확인 직후 `reload_unreviewed_cards`를 부른다. verify `review_queue.reload_payloads`가 기계 소유·fresh `generated` 카드마다 행을 만들고, pipeline `review_reload.reload_reviews`가 정리된 카드를 빼고 넣은 뒤 같은 트랜잭션에 `review_reload_v1_at`을 쓴다.
- 억제 기록은 두지 않는다. kept 판정은 재적재 마커가 찍힌 뒤에 내려지므로 그 기록을 읽을 쪽이 없다.

### 결정

- 이관 행 읽기: `.claimed.*` 파일(수정 시각 순) 다음 `verify-queue.jsonl` 순으로 오래된 행부터 읽는다. 중복 키가 있는 줄은 버린다(`edit-inbox` 규칙과 같다). 1.x `verify-log.jsonl`에 그 대상의 `deferred` 아닌 기록이 행의 `ts` 이후(초 단위 행 시각과 마이크로초 로그 시각을 datetime으로 비교)에 있으면 이미 판정된 행이므로 뺀다. 1.x 검수가 죽거나 정리하지 못해 남긴 claim 파일의 처리 끝난 행이 여기에 걸린다. 로그가 trim돼 그 기록이 없으면 판정 여부를 알 수 없어 가져온다.
- 이관 조건: 교체 행(`pendingPath`)은 버린다. 대상 카드가 기계 소유 `generated`이고 관리 블록 `sourceHash`·`sourceRevisions`가 행과 같고(`candidate_matches_job`), 행에 `expectedTargetHash`가 있으면 바이트 SHA가 그 값과 같아야 한다. 1.0.x~1.1.0의 첫 카드 행에는 `expectedTargetHash`가 없으므로 관리 블록·revision 일치가 "행의 기대값"이다. 같은 대상은 카드와 맞는 가장 새 행 하나만 가져온다. 맞지 않는 새 행이 맞는 옛 행을 가리지 않는다. 원문이 fresh가 아니면 넣지 않는다.
- 이관 프로젝트의 재적재 금지: 1.x 큐에 행이 없는 `generated` 카드는 대개 1.x 검수가 이미 소비한 카드다(`onFail`·`onInconclusive`가 `none`인 kept, `invalid_verdict`, 로그 trim). 그래서 이관과 같은 트랜잭션에 재적재 마커를 쓴다. 1.x 파일이 없는 새 프로젝트도 이 cutover를 지나므로 재적재를 하지 않는다.
- 생산 엔진: 카드 frontmatter와 1.x manifest 행에는 엔진이 없다. 재적재 행의 `engine`은 같은 대상의 최근 verify 행 엔진, 없으면 프로젝트 verify 행의 유일한 엔진이다. 둘 다 없거나 verifier가 부를 수 없는 엔진이면 원장 트랜잭션이 열린 행과 같은 SHA의 종결 행을 먼저 본 뒤, 그중 어느 것도 없는 카드만 `engineUnknown`으로 세고 넣지 않는다. 부를 수 없는 엔진의 행은 `producing_engine_unavailable`로 매 pass 보류되고, verify는 다른 엔진으로 대신 검수하지 않기 때문이다.
- 종결 행 판정: 같은 대상의 `done`·`dropped` `kind: review` 행 중 `expectedTargetHash`가 live SHA와 같은 행만 "이미 검수됨"이다. `replace` 행의 `expectedTargetHash`는 교체 대상 live 카드를 가리키지만 그 행이 검수한 것은 교체본(pending blob)이다. `dropped`(claim 4회 소진)는 같은 바이트를 다시 사지 않도록 포함한다.
- 보존 정리: 재적재 첫 실행 전에 30일 지난 `done/kept` `review` 행이 지워졌다면 그 카드는 한 번 다시 검수된다(G4b). 재적재는 1회라 반복되지 않는다.
- 검수가 꺼진 설정에서는 재적재를 하지 않고 마커도 쓰지 않는다. 켜지면 그 회차에 한 번 돈다.
- 쓰기 반환값 분류: 이관·재적재 행은 마커와 한 트랜잭션이라 별도 확인이 없다. `migration` 로그 행(`reviewReload`)은 3번.

### 상태 조합표 (보고서 5.3 보완, 축: 행 상태 × 보존 정리 전후 × 쓴 버전)

유료 V = 검수 호출. "다음 pass" 열은 같은 입력으로 한 번 더 돌린 결과다.

| # | 상황 | 보존 정리 | 쓴 버전 | 기대 | 유료 | 다음 pass | 시험 |
|---|---|---|---|---|---|---|---|
| G1a | 2.0 첫 카드, 열린 `review` 행 | 전 | 현재 compile | 재적재가 `open`으로 건너뜀 | V1 | V0 | `test_g1_an_open_row_is_not_queued_twice` |
| G1b | 행만 있고 카드 없음(G6) | 전 | 현재 compile | 재적재 0행, verify `card_missing` | V0 | V0 | `test_g6_a_row_without_its_card_closes_without_a_call_and_reloads_nothing` |
| G2a | 1.x 대기 행, 카드 그대로 | — | v1.0.24(claim 파일)·v1.1.0(큐 파일) writer | cutover가 이관, 통과 게시, 재적재 마커 | V1/카드 | V0, 재적재 없음 | `test_g2_cutover_imports_release_rows_of_fresh_unchanged_generated_cards` |
| G2b | 1.x 대기 행, 카드 삭제됨 | — | 같음 | 이관 안 함 | V0 | V0 | 같은 시험(gamma) |
| G2c | 1.x 대기 행 2개(같은 카드 refresh) | — | 같음 | 카드와 맞는 마지막 행만 이관 | V1 | V0 | 같은 시험(delta) |
| B1a | 1.x 검수가 이미 소비한 행(카드 kept, 로그 있음), 행 사본이 claim 파일에 남음 | — | v1.0.24·v1.1.0 writer·verifier | 이관 안 함, 재적재 없음 | V0 | V0 | `test_b1_cutover_imports_queued_rows_only_and_never_pays_for_consumed_or_rowless_cards`(alpha) |
| B1b | 1.x 큐 행이 없던 `generated` 카드(검수 꺼진 compile) | — | 같음 | 이관 안 함, 재적재 없음 | V0 | V0 | 같은 시험(delta) |
| B1c | 1.x 큐 행이 있는 카드 | — | 같음 | 이관 1행 | V1 | V0 | 같은 시험(beta) |
| m2 | 같은 카드의 옛 claim 행과 새 큐 행 | — | 같음 | 새 큐 행 이관 1행 | V1 | V0 | 같은 시험(gamma) |
| n1 | 1.x 행에 중복 키 | — | v1.1.0 writer 행 변형 | 그 줄만 버림 | V0 | V0 | `test_a_1x_line_with_a_repeated_key_is_dropped` |
| G2d | 출시된 cutover가 행을 버린 프로젝트, fresh 카드 | — | v1.1.0 writer + v1.1.4/v1.1.6 cutover | 재적재 1회, 출시된 1.1.4·1.1.6 verifier가 처리 | V1/카드 | 마커로 0행, V0 | `test_g2_reload_queues_rowless_fresh_cards_once_and_released_verifiers_settle_them` |
| G2e | G2d인데 엔진을 알 수 없음(verify 행 0) | — | 같음 | 넣지 않음, `engineUnknown`, 마커 | V0 | 0행 | `test_reload_leaves_cards_whose_producing_engine_is_unknown` |
| G2f | 사람 소유 `generated` 카드 | — | 사람 | 대상 아님 | V0 | V0 | G2d 시험(human) |
| G3 | G2인데 원문이 이후 바뀜 | — | 1.x writer | 이관·재적재 안 함(`stale`). refresh compile이 이어받음(레인 C) | V0 | 레인 D는 보류 행을 만들지 않음 | G2a·G2d·B1 시험 |
| G4a | 출시된 1.1.6 verifier의 `done/kept` | 전 | v1.1.6 verifier | 재적재가 `reviewed`로 건너뜀 | V1(최초) | V0 | `test_g4_a_kept_card_is_skipped_until_retention_removes_its_row` |
| G4b | G4a인데 재적재 전에 행이 보존 정리로 지워짐 | 후 | v1.1.6 verifier | 재적재가 한 번 다시 넣음 | V1 | 마커로 V0 | 같은 시험(retention) |
| m1 | live 카드의 `replace` 행만 종결됨(교체본 kept), 원문이 live revision으로 돌아옴 | 전 | v1.1.0 writer + 현재 compile·verify | 재적재가 넣음(`replace`는 live 검수가 아니다) | V1 | V0 | `test_a_settled_replace_row_does_not_count_as_a_review_of_the_live_card` |
| G5a | `done/stale_revision`, 원문이 새 revision | 전 | 현재 verify | 재적재 `stale` | V0 | V0 | `test_g5_…`(stale) |
| G5b | 원문이 옛 바이트로 되돌아감 | 전 | 현재 verify | `done` review 행(같은 SHA)으로 건너뜀 | V0 | V0 | `test_g5_…`(reverted) |
| G5c | G5b인데 행이 지워짐, 뒤의 compile이 엔진을 남김 | 후 | 현재 verify | 재적재 1행 | V1 | V0 | `test_g5_…`(reverted_after_retention) |
| G6 | 행만 있고 게시 전 중단 | 전 | 현재 compile | G1b와 같음 | V0 | V0 | G1b 시험 |
| G7 | 판정 뒤 게시 전 중단 | — | 현재 verify | 기존 `verified_replay`·기록 판정 재사용 | V0 | V0 | 기존 `RecordedVerdict` 시험 |
| R1 | 재적재 커밋 전 실패 | — | — | 마커·행 없음, 다음 pass가 정확히 1회 넣음 | V0 | 마커로 0행 | `test_a_failed_reload_writes_nothing_and_the_next_pass_reloads_once` |
| R2 | 검수 꺼짐 | — | — | 재적재·마커 없음 | V0 | 켜질 때 1회 | `test_reload_is_skipped_while_review_is_off` |

### 호출부 (CodeGraphContext `find_callers`, repo_path=이 worktree, `rg` 보완)

- `pipeline.migrate_from_v1`: 제품 `wiki_compile_worker.migrate_v1_state`(`reviews=wiki_verify.v1_reviews(root, config)`). 시험 `test_v1_migration.py` 5곳(인자 없이 호출, 1.x 행을 버리고 재적재 마커를 쓰지 않는 출시 동작), `test_review_reload.py`.
- `PipelineStore.import_v1_ledgers`: 제품 `migrate.migrate_from_v1` 하나. `reviews`·`extra_markers` 키워드는 기본값이 비어 있다.
- `migrate.v1_review_rows`: 제품 `migrate_from_v1` 하나.
- `migrate_v1_state`: 제품 `wiki_compile_worker._main`. 시험 `test_migration_entry.py` 5곳, `test_review_reload.py`.
- `reload_unreviewed_cards`: 제품 `_main` 하나(cutover 확인 직후). 시험 `test_review_reload.py`.
- `candidate_matches_job`(재사용): 제품 `review_queue.pending_source_review`, `wiki_verify_worker.process_row`, `review_queue.v1_reviews`.
- `wiki_verify_worker.process_row`·`SUPPRESSION_KINDS`: 이 레인의 최종 diff에서 바뀌지 않는다.
- 영속 데이터 writer·reader:
  - `verify_jobs` `review` 행 writer: `review_queue.publish_first_card`(현재 compile), `decisions.import_v1_ledgers`(cutover), `review_reload.reload_reviews`(재적재). reader: `wiki_verify_worker`(현재·출시된 1.1.4~1.1.6, 같은 payload 키), `review_queue.pending_source_review`·`_engines`, `review_reload._reviewed`, `summary`, `retention.prune_terminal`.
  - `pipeline_meta` `review_reload_v1_at` writer: `decisions.import_v1_ledgers`(필터를 받은 cutover), `review_reload.reload_reviews`. reader: `pipeline.api.review_reload_marked`, 두 writer의 트랜잭션 안 재확인.
  - 1.x `verify-queue.jsonl` writer: 1.0.x~1.1.0 `wiki_compile`(첫 카드·refresh 모두 `expectedTargetHash` 없는 행). 1.1.1~1.1.2는 첫 카드 검수를 `.handoff/v1/hold/`에 두고 이 파일에 쓰지 않는다. claim 파일 writer는 1.x `claim_queue`(rename)이고, 1.x 검수는 정리하지 못하면 처리한 행이 담긴 claim 파일을 남긴다. reader: `migrate.v1_review_rows`.
  - 1.x `verify-log.jsonl` writer: 1.x `wiki_verify_worker.log_verdict`(`ts`·`targetPath`·`result`). reader: `migrate._settled_at`(cutover 전 1회).

### 실데이터 재집계 (읽기 전용, 11-28)

원장은 `errors._query_only_uri`와 `PRAGMA query_only=ON`으로 열었다. 카드·원문·1.x 파일은 읽기만 했다. 스크립트는 이 레인 scratchpad의 `recount.py`다.

| 프로젝트 | 상태 | 1.x 큐 줄 | 이관 행 | 재적재 행 | 비고 |
|---|---|---:|---:|---:|---|
| ktlo-check | 1.x(이관 전) | 27 | 25 | 0 | 승인 27 이내. 이관과 함께 마커 |
| 무림을 리팩토링합니다 | 1.x(이관 전) | 91 | 87 | 0 | 승인 91 이내. 이관과 함께 마커 |
| service-engineering | 1.1.4~1.1.6 cutover 완료 | — | 0 | 425 | generated 492, stale 36, 열린 행 18, review 종결 행 13. 425 중 10은 `replace` 종결 행만 있던 카드(m1) |
| qmd-auto-context | 1.1.4~1.1.6 cutover 완료 | — | 0 | 4 | generated 24, stale 19, review 종결 행 1. 4 중 2는 m1 카드 |

승인 대비: ktlo-check와 무림은 승인 안이다. service-engineering은 승인 418보다 7, qmd-auto-context는 승인 2보다 2 많다. 초과분은 m1 수정으로 "검수된 것"에서 빠진 카드(SE 10, qmd 2)와 집계 시점 차이(SE는 운영 중이라 열린 행·generated 수가 바뀐다)다.

### SE `queued` 9·`claimed` 6 정체 (읽기 전용 확인, 11-28)

`errors._query_only_uri`와 `PRAGMA query_only=1`로 service-engineering 원장을 읽었다. 정체가 아니라 진행 중인 backlog다.

- 관측 시점의 열린 행은 `queued` 9(`review` 8·`replace` 1, 모두 `attempt_count` 0, 생성 1.4~1.5시간 전, 엔진 `claude`)이고 `claimed`는 0이다. verify 쿨다운·fence는 없다.
- 최근 12시간에 verify 행 311개가 생기고 302개가 종결됐다. 마지막 `verify_log`는 12분 전이고 최근 40행은 `verified` 35·`inconclusive` 5다.
- compile worker pid 7411이 살아서 compile 행 50개를 claim하고 있다. verify pass는 compile worker 회차 끝에만 돈다(`wiki_compile_worker._run_verify_pass`). 그동안 compile이 만든 행은 `queued`로 기다리고, 보고서 시점의 `claimed` 6은 진행 중이던 verify 배치로 보인다.
- 코드는 바꾸지 않았다.

### 검사

- `npm run test:focused -- memvane/wiki/compile/tests/run.test.mjs memvane/wiki/verify/tests/run.test.mjs memvane/wiki/pipeline/tests/run.test.mjs tests/structure/static.test.mjs tests/structure/doc-links.test.mjs tests/e2e/wiki-db-provenance.test.mjs`: 68개 중 67개 통과, Python 시험 309개. 실패 1개는 시험 G1(`tagged_v1_consumed.py`가 verify 내부 모듈을 import)이었고, `verify.api`로 바꾼 뒤 `test_review_reload`(14/14)와 그 구조 시험을 다시 돌려 통과했다.
- red: 새 시험을 이전 커밋 `e6c8dc3b`의 `migrate.py`·`decisions.py`·`review_reload.py`·`review_queue.py`·`types.py`로 돌리면 `test_b1_…`(2개 태그), `test_a_settled_replace_row_…`, `test_a_1x_line_with_a_repeated_key_is_dropped`, `test_g2_cutover_…`(2개 태그, 재적재 마커 단정)가 실패한다.
- `tests/structure/v1_guard.py --strict`·`--tests`: G1~G9·시험 G1·G8 0.
- `/usr/bin/python3 -B -c "import …"`(3.9.6): 바꾼 모듈 9개 import 성공.
- 전체 검사(`npm test`)는 돌리지 않았다(머지 직전 최종 커밋에서 감독이 실행).

### 남은 위험

- 재적재 행 수가 승인치를 넘는다: service-engineering 425(승인 418), qmd-auto-context 4(승인 2). m1 카드(SE 10, qmd 2)가 더해졌고, SE는 운영 중이라 집계가 바뀐다.
- 생산 엔진을 원장에서 찾을 수 없는 카드는 재적재하지 않는다(G2e).
- G4b: 출시된 1.1.4~1.1.6 verifier가 `done/kept`로 닫은 행이 재적재 첫 실행 전에 보존 정리로 지워졌다면 그 카드는 한 번 다시 유료 검수된다. 현재 관측된 원장은 11-27 생성이라 30일에 닿지 않았다.
- 1.x 판정 제외는 `verify-log.jsonl`에 기대므로, 로그가 trim돼 판정 기록이 없는 claim 파일 행은 가져온다. 관측한 1.x 프로젝트 6개에는 claim 파일이 없다.
- 1.1.1~1.1.2의 첫 카드 검수 대기(`.handoff/v1/hold/`)는 cutover가 가져오지 않고, 이 버전의 cutover는 재적재도 하지 않는다. 그 카드는 원문이 바뀌어 refresh되기 전까지 `generated`로 남는다. 관측한 1.x 프로젝트의 큐는 `verify-queue.jsonl`에 있다.
- service-engineering(카드 5,828장)은 재적재 첫 회차에 위키 카드 전체를 한 번 읽는다. 유료 호출은 없다.

## 레인 C: 형제 증명 short-circuit 축소와 옛 revision 카드 은퇴

### 규칙

- 분류(`memvane/wiki/compile/supersession.citation`, 카드 바이트만 읽는다). 원문의 옛 revision만 인용한 카드를 셋으로 나눈다.
  - `SUPERSEDED`(은퇴 가능): 플러그인 소유(`auto_writable_text`), `triggers`가 비어 있지 않고 `post_tool_source`·`post_sync_source`·`session_start_source` 안, `sourceRevisions` 1개, 관리 블록 밖에 상투구·HTML 주석 말고 본문 없음, `status: verified`이면서 `verifiedBodyHash`가 본문과 일치.
  - `REFRESHABLE`: 위 조건에서 검수 지문만 없는 카드(`generated` 또는 지문 이전 verified). 관리 블록 안 사람 편집과 구별할 수 없어 은퇴하지 않는다. 추출을 부르고 답에 나오면 다시 쓴다.
  - `PROTECTED`: 나머지(지문 불일치, 관리 블록 밖 본문, `createdBy` 변경, manual trigger, 여러 원문, 읽을 수 없는 provenance). 추출을 부르지도 은퇴하지도 않는다.
- short-circuit(`wiki_compile_worker._reuse_source_revision`): partial·force가 아닌 job만 본다.
  - 현재 revision의 verified 증명 카드가 있으면 `already_compiled_revision`으로 끝낸다. 단, `SUPERSEDED`·`REFRESHABLE` 카드가 있고 원장에 이 원문 바이트(경로·sha256)의 시도 기록이 없으면 추출한다.
  - 증명 카드가 없어도 시도 기록이 있으면 `supersession_recorded`로 끝낸다(`_SETTLING`이라 refresh 이벤트도 정산된다).
  - 원문이 바뀌면 sha256이 달라 다시 시도한다. 추출로 가는 모든 job에 `mark_attempt`를 건다.
- 시도 기록: 원장 `suppressions`의 새 kind `supersession`. 기록 시점에 `SUPERSEDED`·`REFRESHABLE` 카드가 있을 때만 쓴다(옛 카드 없는 보통 compile은 기존 재시도 규칙 그대로). 키는 원문 경로, `body_hash`는 원문 sha256, payload는 `outcome`·`collection`·`size`·`written`·`named`(카드 경로 전체 목록)·`wouldRetire`·`superseded`. DDL은 그대로다. 경로당 한 행이고 새 sha256이 옛 행을 바꾼다. 보존 정리는 `verify` 억제처럼 원문이 모든 수집 root에서 없다고 증명될 때만 지운다.
- 추출 뒤 기록(`supersession.record_after`, 추출이 끝나고 `failed_compile`이 아닐 때):
  - `incomplete`: 후보가 없음, `cardsPerSource` 초과, 객체가 아닌 후보, 경로 없는 writer 결과(거절·실패·다른 revision), 이름을 남길 카드가 `MAX_RECORDED_CARDS`(200)장 초과(`reason: too_many_cards`, 제외 목록이 온전하지 않으면 다시 나온 카드가 은퇴할 수 있다).
  - `held_truncated`: 원문이 `maxSourceChars`(기본 12,000자)로 잘렸다. 추출기는 잘린 원문에서 없다고 단정하지 말라는 지시를 받으므로, 다시 나오지 않은 카드가 사라졌다는 근거가 아니다. 진단 행 `superseded_held_truncated`.
  - `held_ratio`: 다시 나오지 않은 대체 대상이 3장 이상이고 대체 대상의 50%를 넘는다. 진단 행 `superseded_retire_guarded`(`wouldRetire`·`supersededCards`). 근거: 완전한 답은 `cardsPerSource`(10)장까지라 한두 장이 빠지는 것은 보통의 주제 병합이다. 큰 원문에서 대부분이 빠지는 것은 나쁜 답이나 좁은 답의 모양이고, 은퇴는 운영자 없이 되돌릴 수 없다. 이 규칙이면 대체 대상이 20장을 넘는 원문은 10장 답으로 항상 보류된다.
  - `held_no_rewrite`: 다시 나오지 않은 대체 대상이 있는데 `written`이 비었다(`merge-needed`만 나온 답). 검수로 답을 뒷받침할 카드가 없어 은퇴하지 않는다.
  - `settled`: 다시 나오지 않은 대체 대상이 없다.
  - `pending`: 그 밖. 바로 은퇴하지 않는다.
  - `failed`: 옛 카드가 있는 원문의 추출 답을 쓸 수 없어 job을 버린 경우(`invalid_extractor_json`·`missing_candidates`, `process_job_settled`가 기록).
  - 일시적 실패(timeout, 예산 유예, 식힘, 추출 중 원문 변경, writer 거절의 partial 재시도)는 기록하지 않고 기존 재시도를 따른다.
- 은퇴(`supersession.settle`, `_run_verify_pass`가 검수 run 직후 부른다): `pending` 행마다 원문이 같은 sha256인지 본다. 다르면 `source_changed`. `written` 카드가 모두 새 revision으로 verified(지문 일치)가 되면 은퇴한다. 열린 검수 행이 있으면 기다린다. 그 밖(검수 `fail`·`inconclusive`·`stale_revision`)은 `held_review_failed`로 끝나 아무것도 은퇴하지 않는다. 은퇴 직전에 비율 가드를 다시 본다(분모는 기록 시점 대체 대상 수와 현재 수 중 큰 값). `written`이 빈 옛 `pending` 행은 `held_no_rewrite`로 끝낸다. 카드마다 은퇴 직전에 원문 snapshot을 다시 보고 sha256이 다르면 `source_changed`로 멈춘다. 은퇴는 `lifecycle.api.retire_card(..., 'superseded_by_revision')`(보관, 잠금 아래 바이트 재대조, unlink, `retired` 활동 이벤트)이다. 한 pass에 최대 25장(`MAX_RETIRE_PER_RUN`, lifecycle과 같음)이고 남은 카드는 다음 pass가 잇는다. 다 끝나면 `retired`. pass당 `pending` 행은 64개까지 본다.
- 판정 스캔은 frontmatter가 없거나 UTF-8이 아닌 위키 페이지를 건너뛰고(`unreadable`로 센다) `record_after`가 진단 행 `superseded_scan_skipped_pages`를 남긴다. 같은 페이지에서 writer `build_identity_index`가 `UnicodeDecodeError`로 죽는 기존 결함은 이 레인 범위 밖이다.
- 종결과 recall: 모든 기록 결과는 같은 바이트에서 종결 상태다. 보류된 카드는 원문이 바뀔 때까지 옛 revision을 인용한다. 선택 경로의 게시는 원문 현재성을 다시 확인하지 않는다(`product_sync_worker`·`blob_store`의 `verified_wiki_locations(..., current_source=False)`). 그래서 보류된 `verified` 카드는 옛 내용으로 recall에 나온다. `generated` 카드는 `recallVerifiedOnly`라 나오지 않는다. 이 낡음은 약한 근거로 은퇴하지 않고 같은 바이트에 다시 과금하지 않는 대가다. UI의 원문 변경 배지가 보여 준다. 재리뷰가 근거로 든 "recall이 stale 카드를 거부한다"(`card/trust.py`의 `source_changed`)는 `current_source=True` 경로(compile 증명, lifecycle 공유 카드 판정)에만 해당한다.
- 예외: `settle`은 `OSError`·`ValueError`·`RuntimeError`·`sqlite3.Error`를 행·카드 단위로 잡아 진단 행(`superseded_retire_error`·`superseded_settle_error`)으로 남기고 다음 행으로 간다. `_run_verify_pass`는 검수 run이 끝난 뒤 `settle`을 부르므로 은퇴 오류가 검수를 건너뛰게 하지 않는다.
- 쓰기 반환값 분류: 시도 기록 쓰기는 1번(실패하면 같은 바이트의 다음 refresh가 추출을 한 번 더 산다. `supersession_record_failed` 진단 행을 남기고 은퇴하지 않는다). `retire_card` 결과는 3번(남은 카드는 다음 `settle` pass가 유료 호출 없이 다시 은퇴시킨다). `request_sync` 실패와 진단 행은 3번.
- 예산(설정 기본값): `extractorPerRun` 10(상한 50), `cardsPerSource` 10(상한 50), `verifyPerRun` 3(이번 run이 만든 카드만큼 늘고 `VERIFY_PRODUCED_HARD_CAP` 30), 추출 실패 식힘 600초, 은퇴 pass당 25장. split 재요청이 있으면 같은 run 예산 안에서 추출 1회가 더 든다. 새 카드는 기존 write-time dedup judge를 거친다.

### 상태 조합표 (보고서 5.2 확정)

E = 추출, V = 검수. 유료 열은 첫 시도와 원문이 그대로일 때의 재시도(lifecycle refresh 7일 주기)를 함께 적는다. 축 "증명": 현재 revision의 verified 카드가 있음(있음)·없음(없음). 아래 표에서 따로 적지 않은 칸은 "있음"이고, 재시도 0은 "없음"에서도 같다(시도 기록이 증명 카드와 무관하게 skip한다). 단, F1·F2·F5·F6은 기록하지 않는 일시적 실패라 두 축 모두 기존 재시도를 따른다. 시험은 모두 `memvane/wiki/compile/tests/integration/test_superseded_revision.py`다. 입력 카드는 현재 compile writer(`_compile_locked`, worker의 `wiki-compile` child와 같은 호출)와 출시 1.1.6 writer(`tagged_superseded_cards.py`)가 쓴다. 검수 행은 실제 verify worker가 쓴다. 원장 기록은 `supersession._record`가 쓴다. "형제 증명 상태"는 출시 worker가 새 revision 추출에서 일부 카드만 냈을 때 남긴 상태로, 그 카드에만 writer를 호출해 만든다.

| # | 상황 | 수정 전 | 기대 | 첫 시도 | 재시도(같은 바이트) | 시험 |
|---|---|---|---|---|---|---|
| H1 | 원문 1 ↔ 카드 1, 다시 나옴 | replace → 검수 | 같음, 은퇴 0 | E1 + V1 | 0 | `test_h1_single_card_refresh_replaces_it_and_retires_nothing` |
| H2 | 현재 1 verified + 옛 2, 둘 다 다시 나옴 | skip, 영구 stale | replace 2, `settled` | E1 + V2 | 0 | `test_h2_every_card_returned_is_replaced_and_none_retires` |
| H3 | 현재 1 verified + 옛 2, 1장만 다시 나옴 | skip, 영구 stale | replace 1, `pending`. 그 카드 검수 통과 뒤 나머지 1장 은퇴(`retired`) | E1 + V1 | 0 | `test_h3_h4_superseded_card_retires_after_the_rewrite_passes_review` |
| H3r | H3, 카드 전부 출시 1.1.6 writer | 같음 | 같음 | E1 + V1 | 0 | `test_h3_cards_of_the_released_writer_converge_the_same_way` |
| H4 | H3 뒤 같은 바이트 refresh(검수 전·후) | skip | 기록으로 `already_compiled_revision` | 0 | 0 | H3 시험 |
| H5 | H3에서 다시 쓴 카드 검수 `fail` | skip | `held_review_failed`, 아무것도 은퇴 안 함(verified 카드 recall 범위 유지). 새 편집은 다시 시도 | E1 + V1 | 0 | `test_h5_failed_replacement_keeps_every_card_and_is_not_extracted_again` |
| H6 | 검수 중 원문 변경 | `stale_revision` 뒤 skip | 옛 `pending`은 `source_changed`로 끝나고 은퇴 0. 새 바이트는 H1~H5 중 하나 | E1 + V | 0 | `test_h6_source_change_ends_a_pending_attempt_without_retirement` |
| G5 | 첫 카드 검수가 모두 `done/stale_revision`, 한 장만 새 revision verified | skip, 영구 generated | `REFRESHABLE`이 추출을 부름. 다시 나온 카드는 replace, 나머지 generated는 은퇴 안 함, `settled` | E1 + V1 | 0 | `test_g5_stale_revision_generated_cards_converge_without_retirement` |
| G3 | G5에서 보존 정리로 검수 행 모두 삭제 | skip | 같음 | E1 + V1 | 0 | `test_g3_rowless_generated_cards_converge_without_retirement` |
| P1 | 옛 카드가 모두 `PROTECTED`(관리 블록 밖 본문, 지문 불일치, `createdBy` 변경, manual trigger, 공유 원문) | skip | 추출 안 함, 은퇴 안 함 | 0 | 0 | `test_human_manual_and_shared_cards_neither_force_extraction_nor_retire` |
| P2 | P1 카드 옆에 `SUPERSEDED` 1장 | skip | 추출 1회, 그 카드만 은퇴, P1 바이트 불변 | E1 + V1 | 0 | 같은 시험 |
| F1 | 추출 timeout | skip | 기록 없음, 은퇴 0, job 유지 | E1 | 식힘 뒤 기존 재시도 E1 | `test_failed_extraction_retires_nothing_and_keeps_the_job` |
| F1b | 쓸 수 없는 답(job 버림) | skip | `failed`, 은퇴 0 | E1 | 0 | `test_unusable_answer_is_recorded_and_not_bought_again` |
| F2 | run 추출 예산 소진 | skip | 기록 없음, job 유지 | 0 | 다음 run | `test_budget_deferral_retires_nothing` |
| F3 | 답이 `cardsPerSource` 초과 | skip | 상한까지 쓰고 `incomplete`, 은퇴 0 | E1 + V(≤10) | 0 | `test_capped_answer_retires_nothing_and_is_not_bought_again` |
| F4 | 빈 답 | skip | `incomplete` | E1 | 0 | `test_empty_answer_retires_nothing_and_is_not_bought_again` |
| F4b | 객체가 아닌 후보 섞임 | skip | `incomplete`, 은퇴 0 | E1 + V | 0 | `test_non_object_candidate_makes_the_answer_incomplete` |
| F5 | writer 거절 | skip | partial 재시도(기존), 은퇴 0 | E1 | 기존 partial 재시도 | `test_rejected_candidate_retires_nothing` |
| F6 | 추출 중 원문 변경 | skip | 은퇴 0, 새 바이트로 재처리 | E1 | 새 바이트 | `test_source_change_during_extraction_retires_nothing` |
| F7 | `merge-needed`(이름 모호) + 다시 쓴 카드 1장 | skip | 다시 쓴 카드 검수 통과 뒤, 이름 붙은 카드는 남고 나머지는 은퇴 | E1 + V1 | 0 | `test_merge_needed_cards_stay_and_the_rest_retire` |
| F7b | `merge-needed`만 나온 답 | skip | `held_no_rewrite`, 검수 0회, 은퇴 0 | E1 | 0 | `test_merge_needed_only_answer_retires_nothing_without_a_reviewed_rewrite` |
| N1 | 증명 없음: 모든 카드가 REV1 verified, REV2에서 다시 쓴 카드 검수 `fail` | 7일마다 E1 + V1 | `held_review_failed`, 재시도는 `supersession_recorded` | E1 + V1 | 0 | `test_no_proof_failed_review_is_not_bought_again` |
| N2 | 증명 없음: 빈 답 | 7일마다 E1 | `incomplete`, 재시도 skip | E1 | 0 | `test_no_proof_empty_answer_is_not_bought_again` |
| N3 | 증명 없음: 쓸 수 없는 답 | 7일마다 E1 | `failed`, 재시도 skip | E1 | 0 | `test_no_proof_unusable_answer_is_recorded` |
| N4 | 기록된 바이트의 force(`forceRecompile`)·partial job | 추출 | 추출(기록을 보지 않음) | E1 | 기존 규칙 | `test_forced_and_partial_jobs_still_extract_recorded_bytes` |
| L1 | 다시 쓴 카드 21장(`cardsPerSource` 25) | 20장까지만 기록, 21번째가 검수 전 은퇴 | 21장 전부 기록, 다시 나오지 않은 1장만 은퇴 | E1 + V21 | 0 | `test_more_than_twenty_rewritten_cards_are_all_kept_out_of_retirement` |
| L2 | 이름 남길 카드가 상한 초과 | 잘린 목록 | `incomplete(too_many_cards)`, 은퇴 0 | E1 + V | 0 | `test_an_attempt_naming_too_many_cards_is_incomplete`(상한 1로 patch) |
| S1 | 은퇴 도중 원문 편집 | 옛 답으로 계속 은퇴 | 다음 카드 전에 `source_changed`로 멈춤 | 0 | 새 바이트로 재시도 | `test_a_source_edit_during_retirement_stops_it` |
| U1 | frontmatter 없거나 UTF-8 아닌 위키 페이지 | 스캔이 None, 기록 확인 우회 | 건너뛰고 `unreadable` 1로 셈 | 0 | 0 | `test_scan_skips_pages_without_frontmatter_or_utf8` |
| T1 | 원문이 `maxSourceChars`로 잘림 | skip | `held_truncated`, 은퇴 0 | E1 + V | 0 | `test_truncated_source_holds_retirement_and_is_not_bought_again` |
| M1 | 대체 대상 5, 다시 나오지 않은 4(위키 기계 카드 18장) | skip | `held_ratio`, 은퇴 0 | E1 + V1 | 0 | `test_ratio_guard_holds_a_source_whose_answer_drops_most_cards` |
| M2 | 대체 대상 6, 다시 나오지 않은 3 | skip | 3장 은퇴 | E1 + V3 | 0 | `test_ratio_guard_lets_half_of_the_cards_retire` |
| C1 | 은퇴할 카드가 pass 상한보다 많음 | — | 상한만큼 은퇴, 나머지 `pending`으로 다음 pass | 0 | 0 | `test_settle_retires_at_most_the_pass_cap_and_finishes_on_the_next_pass`(상한 1로 patch) |
| X1 | 은퇴 writer가 `ValueError` | — | 진단 행, 검수는 그대로, `pending` 유지, 다음 pass 은퇴 | 0 | 0 | `test_settle_error_is_logged_and_the_pass_goes_on` |
| I1 | writer 뒤 기록 전 중단 | — | 기록 없음. 다음 pass는 열린 replace 행으로 `awaiting_pending_verification`(E0). 검수 뒤 같은 바이트 refresh에서 추출 1회 더 | 1회 더 E1 + V | 0 | 판정 경로가 H3와 같음 |
| I2 | 기록 뒤 은퇴 전 중단 | — | `pending`이 남아 다음 `settle`이 잇는다 | 0 | 0 | C1 시험 |
| R1 | 출시 1.1.4·1.1.6 retention·1.1.6 요약이 `supersession` 행을 만남 | — | 지우지 않고(행 1개 유지) 요약은 종류별 수에 센다 | 0 | 0 | `test_released_ledger_readers_keep_and_ignore_the_supersession_row` |

같은 바이트로 다음 pass나 lifecycle 재시도에서 다시 과금하는 칸은 F1·F2·F6(일시적 실패의 기존 재시도), F5(기존 partial 재시도), N4(force·partial), I1(1회)뿐이다. 증명 카드 유무와 무관하게 나머지는 기록으로 0이 된다. 보류 칸(`held_truncated`·`held_ratio`·`held_review_failed`·`held_no_rewrite`·`incomplete`·`failed`)은 같은 바이트에서 종결이고 원문이 바뀔 때만 다시 시도한다. 그동안 보류된 verified 카드는 옛 내용으로 recall에 나온다(규칙의 "종결과 recall").

쓴 버전: 1.0.x~1.1.6 카드 frontmatter는 같은 모양이고 1.1.6 writer 카드로 H3r을 시험했다. `supersession` 행은 이번 릴리스만 쓰고 읽는다.

### 호출부 (CodeGraphContext `find_callers`, repo_path=이 worktree, `rg` 보완)

바뀐 기호: `_reuse_source_revision`(기록 확인, `mark_attempt`), `process_job`(`Outcomes`·`record_after`, 꼬리 `_finish_refresh`), `process_job_settled`(`record_failure`), `_run_verify_pass`(`settle`), `pipeline.types.SUPPRESSION_KINDS`(`supersession` 추가), `pipeline.retention.source_gone`(`supersession` 키를 원문 경로로 판정). 새 기호: `supersession.citation`·`scan`·`recorded`·`mark_attempt`·`record_failure`·`record_after`·`settle`·`_settle_one`·`Outcomes`·`ratio_held`, `lifecycle.api.retire_card`.

- 제품: `_reuse_source_revision`·`record_after` ← `process_job` ← `process_job_settled` ← `_main_locked`. `settle` ← `_run_verify_pass` ← `_main_locked`(4곳). `lifecycle.api.retire_card` ← `supersession._settle_one`. `wiki_source_retire.retire_card`의 다른 제품 호출부는 `wiki_lifecycle.run` 2곳이고 서명·동작 불변.
- `SUPPRESSION_KINDS` reader: `decisions.set_suppression`(허용 검사), `summary`(종류별 수). 기존 `suppressions('verify')`(`verify_suppression_hash`)·`suppressions('dedup')` reader는 kind로 걸러 영향이 없다.
- 시험: `test_superseded_revision.py` 33개. `process_job` 기존 호출 시험(`test_revision_skip.py`·`test_checkout_fingerprints.py`·`test_summary_history.py`·`test_retrieval_overlay.py`·`test_compile_loop_110.py`)은 fake writer 카드가 관리 블록이 없어 분류 대상이 아니다. `test_pipeline_summary.py` 기대값에 `supersession: 0`, `test_retention.py` rt5에 `supersession` 행 판정을 더했다.
- 영속 데이터 writer·reader:
  - `suppressions` kind `supersession`: writer `supersession._record`(현재만). reader `supersession._attempt`·`_pending`, `summary`, `retention`. 출시 1.1.4~1.1.6은 쓰지 않고, retention은 모르는 kind를 남기며(`source_gone`이 `False`), 1.1.6 요약은 종류별 수에 센다(R1 시험).
  - `retired-wiki/` 보관본(sidecar `proofHash: superseded_by_revision`)과 `retired` 활동 이벤트: writer는 기존 `archive_card`·`wiki_activity_log.append`. reader는 `event_log.wiki_tail`과 `memvane:logs`.
  - compile `candidate` 감사 행의 진단 reason(`superseded_held_truncated`·`superseded_retire_guarded`·`superseded_review_failed`·`superseded_retire_error`·`superseded_settle_error`·`superseded_scan_unavailable`·`supersession_record_failed`·`retired/superseded_by_revision`): 판정에 쓰이지 않는다.

### 실제 저장소 추정 (읽기 전용, 11-28)

`/Users/dulee/work/qmd-auto-context` main checkout의 카드를 `python3 -B -I`로 읽어 `supersession.citation`으로 분류했다. 원장·큐·카드에는 쓰지 않았다. 스크립트: `/private/tmp/claude-501/-Users-dulee-work-qmd-auto-context--orca-worktrees-qmd-auto-context-wiki-fix-c/d6de69bf-3fb4-4671-9951-5711202138a8/scratchpad/est/estimate2.py`.

- 카드 648장. 옛 revision 카드 342장: `SUPERSEDED` 325, `REFRESHABLE` 17(지문 없는 generated), `PROTECTED` 0.
- 다시 추출하는 원문 41개(현재 verified 증명이 있어 지금까지 skip하던 원문). 4개는 원래 추출하던 원문이다.
- 잘린 원문(12,000자 초과) 15개, 그 `SUPERSEDED` 162장(`REFRESHABLE` 포함 175장)은 `held_truncated`로 은퇴하지 않는다.
- 은퇴 후보 상한 85장(잘리지 않은 원문에서 비율 가드가 허용하는 최대: 대체 대상 2장 이하면 전부, 3~20장이면 절반, 20장 초과면 0), 하한 0장(답이 모두 다시 내거나 검수가 실패하는 경우). 잘리지 않은 원문 중 대체 대상 20장 초과는 없다.
- 유료 상한: 추출 41회(split 재요청 포함 82회), 검수 343회(원문별 `min(10, 대체·갱신 대상 + 현재 카드)`의 합). run당 추출 10회라 최소 5 run에 나뉜다. 같은 바이트의 재시도 비용은 0이다.
- service-engineering(카드 5,828장)은 증명 스캔 상한 2,048장에 걸려 skip 자체가 없으므로 이번 변경으로 추출 수가 늘지 않고, 은퇴 스캔도 돌지 않는다.

### 검사

- 리뷰 반영 전 코드(`6023b1d6`의 `supersession.py`·`wiki_compile_worker.py`·`pipeline/types.py`·`retention.py`)에서 새 시험 23개 중 16개 실패(red: 13 failure + 3 error, H6 시험은 그 뒤에 추가).
- 재리뷰 반영 전 코드(`965807bd`의 `supersession.py`·`wiki_compile_worker.py`)에서 재리뷰 시험 9개 중 8개 실패(red). 통과한 1개(N4)는 수정 전후 같아야 하는 칸이다.
- `npm run test:focused -- --test-name-pattern test_superseded_revision memvane/wiki/compile/tests/run.test.mjs`: 33/33 통과.
- `npm run test:focused -- memvane/wiki/compile/tests/run.test.mjs memvane/wiki/compile/tests/stale-1015.test.mjs`: 모듈 27/27, Python 시험 235개 통과.
- `npm run test:focused -- tests/structure/static.test.mjs tests/structure/doc-links.test.mjs tests/e2e/wiki-db-provenance.test.mjs`: 32/32 통과. `/usr/bin/python3`(3.9.6) import 통과.
- `npm run test:focused -- memvane/wiki/pipeline/tests/run.test.mjs memvane/wiki/lifecycle/tests/run.test.mjs memvane/wiki/verify/tests/run.test.mjs`: 모듈 22/22, Python 시험 250개 통과.
- `tests/structure/v1_guard.py --strict`·`--tests`: 모든 규칙 0.

### 남은 위험

- 보류 칸은 원문이 바뀔 때까지 stale 카드를 남기고, 보류된 verified 카드는 옛 내용으로 recall에 나온다. 특히 잘린 큰 원문(15개, 175장)과 대체 대상이 20장을 넘는 원문은 이 규칙으로는 은퇴하지 않는다. 운영자가 원문을 나누거나 `maxSourceChars`를 조정해야 수렴한다.
- I1(writer 뒤 기록 전 중단)은 추출 1회를 더 산다.
- 모델이 주제를 빠뜨린 답이 비율 가드 안(3장 미만 또는 절반 이하)이면 그 카드는 검수 통과 뒤 은퇴한다. 보관본은 `retired-wiki/`에 남는다.

## 레인 E: 대시보드 위키 배지, 체크아웃 상태 표시, 상위 `.git` 브랜치

기준 main `57ab7f88`. 대시보드는 레지스트리·카드·원장을 읽기만 한다. 쓰기·잠금·프로세스 기동이 없고 유료 호출도 없다. 영속 원장 상태 조합표는 레지스트리 상태별 표시 표로 대신한다.

### 표 E1. 레지스트리 행 → 체크아웃 상태

입력 행은 모두 `registry.api.after_setup`·`registry.api.reconcile`이 실제 파일·route 변화를 관측해 쓴다. 예외는 E1-11의 다른 claimant 행 하나다. onboarding이 선택된 루트에 두 번째 설치를 거절하므로(`project_selection_already_exists`, `initial_build_already_registered`) 레지스트리 자체 conflict 시험과 같은 방식으로 한 행을 넣는다. 두 행의 `conflict` 상태는 setup 등록 writer가 쓴다.

| # | 만드는 사건 | 레지스트리 status/reason | `viewState` | 브랜치 칸 | 점 | 기본 표시 | 유료 |
|---|---|---|---|---|---|---|---|
| E1-1 | main 선택·등록 | active / – (실시간 증명 성공) | `ready` | 브랜치명 | 녹색 | 보임 | 0 |
| E1-2 | 연결 worktree 등록 | active / – | `ready` | 브랜치명 | 녹색 | 보임 | 0 |
| E1-3 | worktree 디렉터리 삭제, git은 아직 나열 | active / – (루트 없음) | `path_missing` | "경로 없음" | 빨강 | 토글 | 0 |
| E1-4 | `git worktree remove` 뒤 reconcile | detached / released | `released` | "해제됨" | muted | 토글 | 0 |
| E1-5 | main 루트 이동, 관측 1회 | missing_pending / – | `path_missing` | "경로 없음" | 빨강 | 토글 | 0 |
| E1-6 | main 루트 이동, 하루 간격 관측 2회 | detached / missing | `path_missing` | "경로 없음" | 빨강 | 토글 | 0 |
| E1-7 | origin restore 뒤 reconcile | detached / released | `released` | "해제됨" | muted | 토글 | 0 |
| E1-8 | 루트 inode 교체 뒤 reconcile | detached / root_moved | `moved` | "이동됨" | muted | 토글 | 0 |
| E1-9 | route 손상, 루트 있음 | active / – (`proof=unproved:…`, 실시간 증명 실패) | `proof_failed` + `stateReason=route_…` | 브랜치명 | 빨강 | 보임 | 0 |
| E1-10 | E1-9 뒤 route 복구 | active / – (`proof`는 아직 `unproved:…`) | `ready` (실시간 증명이 우선) | 브랜치명 | 녹색 | 보임 | 0 |
| E1-11 | 두 설치가 같은 루트를 주장 | conflict / – (양쪽) | `conflict` | "충돌" | 빨강 | 토글 | 0 |
| E1-12 | `detached/withdrawn` | detached / withdrawn | `released` | "해제됨" | muted | 토글 | 0 |
| E1-13 | cold worktree view | – | `view_preparing` | 브랜치명 | amber | 보임 | 0 |
| E1-14 | worktree 상위 디렉터리 권한 거부(chmod 0) | active / – (루트 lstat EACCES) | `unreadable` + `stateReason=EACCES` | "⎇ unknown" | amber | 보임 | 0 |
| E1-15 | E1-9 행, `max_installs=0`(실시간 증명 생략) | active / – (`proof=unproved:<code>`) | `proof_failed` + `stateReason=<code>` | 브랜치명 | 빨강 | 보임 | 0 |
| E1-16 | DDL 밖 조합(`detached`/None, 미래 status 등) | 그 값 | `unknown` + `stateReason=<status>/<reason>` | 브랜치명 칸 `unknown` | amber | 보임 | 0 |

- E1-1~E1-11, E1-14, E1-15: `memvane/ui/tests/integration/test_overview_states.py::test_registry_states_map_to_view_states`가 status/reason, `viewState`, 브랜치, `stateReason`, 노드 수(12)를 단정한다. 어느 노드도 `non-git`을 쓰지 않는다. E1-14의 worktree는 실제 `git worktree add`와 setup 등록으로 만든다.
- E1-16: 현재 DDL CHECK로는 생기지 않아 실제 writer 행이 없다. `ViewStateMappingTests.test_an_unknown_status_or_reason_is_unknown_with_its_values`가 매핑 함수만 단정한다.
- `ready`·`view_preparing`이 아닌 행은 `resolve_target`이 404(`target_unknown`)나 409(`target_unproved`)로 거절한다. 프런트는 그 행의 선택과 Wiki·Recall 바로 가기를 막고 툴팁에 상태·이유를 적는다. KPI는 토글과 상관없이 숨기지 않은 행만 센다.
- E1-12: DDL은 `withdrawn`을 허용하지만 현재 레지스트리 writer(`memvane_registry.py`)와 `git log -S withdrawn -- memvane/registry`에 쓰는 곳이 없다. 실제 writer 행이 없어 시험하지 않고 매핑만 둔다.
- E1-13: `view_preparing`은 recall reader 오류(`journal_reader`)다. 레지스트리만 읽는 Overview가 worktree별로 알 수단이 없어 백엔드는 내지 않는다. 프런트 계약과 점 색(amber)만 유지한다.
- worktree 행의 `proof`는 레지스트리 `_sync_worktrees`가 항상 `ok`로 쓴다. Overview는 git을 실행하지 않으므로 worktree를 실시간으로 증명하지 않는다. 따라서 worktree의 `proof_failed`는 레지스트리 `proof`가 `unproved:`일 때만 나오고 현재 writer로는 생기지 않는다.
- 점은 체크아웃 상태만 칠한다. SLO는 Overview의 SLO 칸과 KPI 배지에만 남는다.

### 표 E2. 카드 freshness → 배지

카드는 project fixture의 카드 writer(`card.dump_source_revisions`로 `sourceRevisions`를 쓴다)가 쓰고 원문은 실제 파일 변경·삭제로 바꾼다.

| # | 상황 | `check_card` | `stale` / `staleReason` | 배지 | 시험 |
|---|---|---|---|---|---|
| E2-1 | 원문 그대로 | fresh | false / null | 없음 | `test_main_list_filters_and_ledger`, `test_stale_cards_name_a_deleted_or_a_changed_source` |
| E2-2 | 원문 해시 변경 | stale / `content_hash_mismatch` | true / `source_changed` | "원문 변경"(amber) | `test_stale_cards_name_a_deleted_or_a_changed_source` |
| E2-3 | 원문 삭제 | stale / `missing` | true / `source_deleted` | "원문 삭제"(red) | 같음 |
| E2-4 | 원문 2개, 첫째 변경·둘째 삭제 | stale / `content_hash_mismatch`(첫 원문에서 멈춤) | true / `source_deleted`(원문별 재확인) | "원문 삭제" | 같음 |
| E2-5 | 판정 불가(allow root 밖, 읽기 실패 등) | unknown | false / null | 없음 | 기존 동작 유지 |

- 필터는 `stale` 하나다. 버튼 툴팁이 두 종류를 설명한다.
- 판정은 원문마다 `check_card`를 원문 하나로 한 번씩 부르는 단일 루프다. 첫 non-fresh 원문이 stale이면 카드가 stale이고(전체 카드 `check_card`와 같은 판정), 그 뒤의 삭제 원문도 같은 루프에서 찾는다. 원문은 많아야 한 번 해시하고 삭제 원문은 해시하지 않는다. `test_each_source_is_checked_once_in_one_pass`가 호출 수(변경·삭제 순 2, 변경·fresh·변경 3, fresh·삭제 2, fresh 1)와 호출마다 원문 1개를 단정한다.
- 원문 삭제 카드의 lifecycle HOLD 사유는 붙이지 않았다. 사유는 `logs/wiki-lifecycle.log`의 자유 문장과 `dry_run`(git 실행) 결과에만 있다. 공개 `api.py`에 읽기 경로가 없고, 로그 문구는 계약 밖이며, Overview·Wiki는 프로세스를 띄우지 않는다.

### 상위 `.git` 탐색

`read_adapters._dotgit`은 루트에서 위로 첫 `.git`을 찾는다. git 기본 탐색처럼 파일 시스템 경계에서 멈추고, HOME 아래 루트는 HOME 위로 올라가지 않는다. 코어는 import하지 않는다. `.git`이 디렉터리면 그대로, 일반 파일이면 `gitdir:` 포인터를 4 KiB 상한으로 읽고, 그 밖(심볼릭 링크 등)이면 `unknown`이다. `HEAD`는 `O_NOFOLLOW`·4 KiB 상한으로 읽는다. 루트가 없거나 관측할 수 없는 행은 브랜치를 읽지 않는다.

코어와의 차이: `store.checkout.checkout_view._dotgit`은 경계·HOME 없이 `/`까지 올라간다. 장치 경계 너머의 `.git`은 코어도 뒤이은 `git rev-parse`가 실패해 브랜치를 얻지 못하므로 결과는 같다. HOME 위 저장소(예: `/Users/.git`)는 코어와 git이 찾지만 UI는 `non-git`으로 보인다. 코어는 고치지 않았다.

- 시험: `test_sse_and_reads.py::test_the_walk_stops_above_home_and_at_a_file_system_boundary`(HOME 위 저장소 → `non-git`, HOME 자체 저장소 → 그 브랜치, `os.lstat`의 `st_dev`를 바꾼 경계 → `non-git`), `test_sse_and_reads.py::test_a_folder_inside_a_checkout_reads_the_nearest_git_above_it`(저장소 하위 폴더 → `main`, 연결 worktree 하위 폴더 → `side`, 상위 `.git` 심볼릭 링크 → `unknown`), `test_overview_states.py::test_a_project_folder_inside_a_repository_reads_the_repository_branch`(저장소 하위 폴더에 온보딩한 실제 설치 → Overview `main`).

### 호출부 (CodeGraphContext `repo_path`=이 worktree, `rg` 보완)

| 바꾼 기호 | 제품 호출부 | 시험 호출부 |
|---|---|---|
| `wiki_adapters._is_stale` → `_stale_reason` | `wiki_adapters._card_meta` ← `wiki_list` ← `server` | `test_wiki_api` |
| `read_adapters.git_dir` | `read_adapters.read_branch` | (간접) |
| `read_adapters.read_branch` | `read_adapters._target_row` ← `overview` | `test_sse_and_reads.BranchTests` 3개 |
| `read_adapters._prove`(시그니처 유지, `_proof` 위임) | `resolve_target`, `runtime_adapters._proved_installs` | `test_overview`, `test_wiki_api`, `test_recall_api`, `test_runtime_learning_api` |
| `read_adapters._proof`(신규) | `_prove`, `overview` | `test_overview_states` |
| `read_adapters._view_state` | `_node_view` | `test_overview_states` |
| `read_adapters._node_view` | `overview`(main·worktree 2곳) | `test_overview`, `test_overview_states` |
| `read_adapters._target_row` | `overview` | 같음 |
| DTO `viewState`·`stateReason`(모든 체크아웃 노드), `staleReason` | `ui/src`: `checkoutState.ts`, `dormantPref.ts`, `SidebarTree`, `OverviewPage`, `Topbar`, `WikiPage` | – |
| `read_adapters._root_state`(신규, `_present` 대체) | `_target_row` | `test_overview_states` |

체크아웃 상태 타입은 `CheckoutViewState`이고 main과 worktree 노드 모두에 붙는다. 이 DTO는 1.x 영속 계약 밖이며 프런트와 백엔드가 한 번들로 함께 배포된다.

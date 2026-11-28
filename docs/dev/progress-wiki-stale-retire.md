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

- 대체 대상(`SUPERSEDED`): 원문의 옛 revision만 인용하고 다음을 모두 만족하는 카드다(`memvane/wiki/compile/supersession.citation`). 플러그인 소유(`auto_writable_text`), `triggers`가 비어 있지 않고 `post_tool_source`·`post_sync_source`·`session_start_source` 안, `sourceRevisions` 1개, `verifiedBodyHash`가 없거나 본문과 일치, 관리 블록 밖에 상투구·HTML 주석 말고 본문 없음. 원문의 옛 revision을 인용한 나머지 카드는 `PROTECTED`다. 판정 입력은 카드 바이트뿐이고 원장 행은 읽지 않는다.
- short-circuit: 현재 revision을 증명하는 verified 카드가 있어도 `supersession.scan`이 대체 대상을 하나라도 찾거나 스캔이 실패하면 `already_compiled_revision`으로 끝내지 않는다. 그다음 기존 `pending_source_review` 검사(`awaiting_pending_verification`)와 추출로 간다. 스캔 상한은 증명 스캔과 같다(카드 2,048장·24MB·카드당 5MB).
- 은퇴: 추출이 끝나고 `failed_compile`이 아닐 때 `supersession.retire_after`가 돈다. 답의 후보가 1개 이상이고 `cardsPerSource` 이하이며, 모든 writer 결과가 카드 경로를 남겨야 한다(`created`·`updated`는 이번 revision으로 쓴 카드, `merge-needed`는 `targetPath`·`matchedPath`·`targetMatches`, `split-required`는 뒤 회차가 결정). 그 경로에 없는 대체 대상 카드를 `wiki_source_retire.retire_card(..., 'superseded_by_revision')`로 은퇴시킨다. 보관(`retired-wiki/`, sidecar `proofHash: superseded_by_revision`), 잠금 아래 바이트 재대조, unlink, `retired` 활동 이벤트(`reason: superseded_by_revision`)는 기존 writer 그대로다. 은퇴가 있으면 `laya-sync` wake 1회.
- 대량 가드: 한 원문의 은퇴 수가 `MASS_DELETE_MIN`(3) 초과이면서 위키 기계 카드 수의 `MASS_DELETE_FRACTION`(25%) 초과면 그 원문 전체를 보류하고 `superseded_retire_guarded` 행(`wouldRetire`·`decidedCards`·`cards`)을 남긴다. 분모를 원문별이 아니라 위키 전체 기계 카드로 둔 이유: 원문별 비율이면 카드 27장 중 10장만 다시 나오는 정상 H3가 매번 보류되어 영구 보류 칸이 된다.
- 은퇴 직전에 원문을 다시 대조한다. 추출 뒤 바뀌었으면 `superseded_source_changed` 행만 남기고 은퇴하지 않는다.
- 쓰기 반환값 분류: `retire_card` 결과는 3번(남은 카드는 옛 revision을 계속 인용하므로 다음 refresh가 추출 1회 뒤 다시 은퇴시킨다), `request_sync` 실패는 3번(다음 full scan이 부재 파일을 뺀다), 진단 행(`superseded_*`)은 3번.
- 예산(설정 기본값): `extractorPerRun` 10(상한 50), `cardsPerSource` 10(상한 `MAX_CARDS_PER_SOURCE`), `verifyPerRun` 3(이번 run이 만든 카드만큼 늘고 `VERIFY_PRODUCED_HARD_CAP` 30), 추출 실패 식힘 `cooldownSeconds` 600초, lifecycle refresh 재시도 `REFRESH_RETRY_SECONDS` 7일. 한 원문의 추출은 1회이고 split 재요청이 있으면 같은 run 예산 안에서 1회 더 든다. 새 카드는 기존 write-time dedup judge를 거친다(`semanticDedup` 설정).

### 상태 조합표 (보고서 5.2 확정)

E = 추출 호출, V = 검수 호출. "다음 pass"는 같은 입력(같은 원문 바이트)으로 한 번 더 돌린 결과다. 시험은 모두 `memvane/wiki/compile/tests/integration/test_superseded_revision.py`다. 시험 입력은 현재 compile writer(`_compile_locked`, worker의 `wiki-compile` child와 같은 호출)와 출시 1.1.6 writer(`tagged_superseded_cards.py`)가 쓴 카드, 실제 verify worker가 쓴 행이다. "형제 증명 상태"는 출시 worker가 새 revision 추출에서 일부 카드만 냈을 때 남긴 상태로, 그 카드에만 writer를 호출해 만든다.

| # | 상황 | 수정 전 | 기대(수정 후) | 유료 | 다음 pass | 시험 |
|---|---|---|---|---|---|---|
| H1 | 원문 1 ↔ 카드 1, 추출이 다시 냄 | replace → 검수 | 같음, 은퇴 0 | E1 + V1 | 검수 뒤 `already_compiled_revision`, 0 | `test_h1_single_card_refresh_replaces_it_and_retires_nothing` |
| H2 | 원문 1 ↔ 카드 N(현재 1 verified + 옛 2), 추출이 옛 2장을 다 냄 | `already_compiled_revision`, 옛 2장 영구 stale | replace 2, 은퇴 0 | E1 + V2 | 0 | `test_h2_every_card_returned_is_replaced_and_none_retires` |
| H3 | 원문 1 ↔ 카드 N(현재 1 verified + 옛 2), 추출이 옛 1장만 냄 | `already_compiled_revision`, 옛 2장 영구 stale | replace 1, 나머지 1장 은퇴(`superseded_by_revision`), 현재 카드 바이트 불변 | E1 + V1 | 검수 전 `awaiting_pending_verification`, 검수 뒤 `already_compiled_revision`, 둘 다 0 | `test_h3_h4_superseded_card_retires_and_the_next_passes_pay_nothing` |
| H3r | H3인데 카드 전부를 출시 1.1.6 writer가 씀 | 같음 | 같음 | E1 + V1 | 0 | `test_h3_cards_of_the_released_writer_converge_the_same_way` |
| H4 | H3 뒤 refresh | 0, 계속 stale | H3의 다음 pass 열 | 0 | 0 | H3 시험 |
| H5 | H3에서 다시 나온 카드의 교체 검수가 `fail` | 형제 증명이면 skip | 은퇴된 카드는 그대로, 실패한 카드는 옛 revision으로 남음. lifecycle 재시도(7일 뒤)가 추출 1회 더, 그 카드는 다시 나오므로 은퇴하지 않음 | 재시도당 E1 + V1 | 7일 안에는 lifecycle이 넣지 않음 | `test_h5_failed_replacement_stays_and_is_extracted_again_on_retry` |
| H6 | 검수 중 원문 변경 | `stale_revision` 뒤 형제 증명이면 skip | 새 revision 추출이 H1~H3 중 하나로 수렴 | E1 + V | 0 | G5 시험(같은 경로) |
| G5 | 첫 카드 검수가 모두 `done/stale_revision`, 한 장만 새 revision으로 verified | skip, 영구 generated | 다시 나온 1장 replace, 나머지 은퇴 | E1 + V1 | 0 | `test_g5_stale_revision_generated_cards_converge` |
| G3 | G5에서 보존 정리로 검수 행이 모두 지워짐(행 없는 generated) | skip | 같음(판정은 카드만 읽음) | E1 + V1 | 0 | `test_g3_rowless_generated_cards_converge` |
| P1 | 옛 revision 카드가 사람 편집(관리 블록 밖 본문, `verifiedBodyHash` 불일치, `createdBy` 변경)·manual trigger·공유 원문 | skip | 추출을 요구하지 않음(`already_compiled_revision`), 은퇴하지 않음 | 0 | 0 | `test_human_manual_and_shared_cards_neither_force_extraction_nor_retire` |
| P2 | P1 카드 옆에 대체 대상 1장 | skip | 추출 1회, 대체 대상만 은퇴, P1 카드 바이트 불변 | E1 + V1 | 0 | 같은 시험 |
| F1 | 추출 실패(timeout) | skip | 은퇴 0, 행 유지, 식힘 | E1 | 식힘 뒤 재시도 | `test_failed_extraction_retires_nothing` |
| F2 | run 추출 예산 소진 | skip | `extractor_budget_deferred`, 은퇴 0, 행 유지 | 0 | 다음 run | `test_budget_deferral_retires_nothing` |
| F3 | 답이 `cardsPerSource`를 넘음 | skip | 상한까지 쓰고 은퇴 0 | E1 + V(≤ 상한) | lifecycle 재시도마다 E1 | `test_capped_answer_retires_nothing` |
| F4 | 빈 답 | skip | 은퇴 0 | E1 | lifecycle 재시도마다 E1 | `test_empty_answer_retires_nothing` |
| F5 | writer 거절(부분 결과) | skip | `partialCompile`로 행 유지, 은퇴 0 | E1 | 기존 partial 재시도 | `test_rejected_candidate_retires_nothing` |
| F6 | 추출 중 원문 변경 | skip | `source_changed_during_extract`, 은퇴 0 | E1 | 새 revision으로 재처리 | `test_source_change_during_extraction_retires_nothing` |
| F7 | `merge-needed`(이름 모호) | skip | 이름 붙은 카드는 남고 나머지 대체 대상은 은퇴 | E1 | 남은 카드 때문에 lifecycle 재시도마다 E1 | `test_merge_needed_cards_stay_and_the_rest_retire` |
| M1 | 한 원문 은퇴 4장 / 기계 카드 6장 | skip | 전부 보류, `superseded_retire_guarded`(4, 6) | E1 + V1 | lifecycle 재시도마다 E1, 운영자 확인 대상 | `test_mass_guard_holds_a_source_that_would_retire_too_many_cards` |
| M2 | 같은 은퇴 4장 / 기계 카드 18장 | skip | 4장 은퇴 | E1 + V1 | 0 | `test_mass_guard_lets_the_same_retirement_through_in_a_larger_wiki` |
| I1 | writer 뒤, 은퇴 전 중단 | — | 다음 pass는 열린 replace 행으로 `awaiting_pending_verification`. 검수 뒤 남은 대체 대상 때문에 추출 1회 더, 그때 은퇴 | 1회 더 E1 + V(다시 나온 카드) | 0 | 판정 경로가 H3와 같음(별도 시험 없음) |
| I2 | 보관 뒤 unlink 전 중단 | — | 다음 은퇴가 같은 보관본을 재사용(`archive_card` 멱등) | I1과 같음 | 0 | 기존 writer 시험 |

같은 입력으로 다음 pass에서 계속 보류되는 칸: H2·H3·G3·G5·P1·P2·M2는 0회로 수렴한다. F3·F4·F7·M1·H5는 원문이 그대로면 lifecycle refresh 재시도(7일)마다 E1이 든다. 이 칸들은 추출 답이나 운영자 확인이 바뀌어야 수렴한다. 수정 전 현재 경로에서 형제 증명 없는 단일 카드의 H5와 같은 비용 구조다.

쓴 버전: 1.0.x~1.1.6 카드 frontmatter(`sourceRevisions`·`triggers`·`createdBy`·관리 블록)는 같은 모양이다. 1.1.6 writer 카드로 H3r을 시험했다. 판정은 원장을 읽지 않으므로 원장 버전 축의 차이가 없다.

### 호출부 (CodeGraphContext `find_callers`, repo_path=이 worktree, `rg` 보완)

바뀐 기호: `wiki_compile_worker._reuse_source_revision`(대체 대상 검사 추가), `wiki_compile_worker.process_job`(`Outcomes` 기록과 `retire_after` 호출, 꼬리를 새 `_finish_refresh`로 분리). 새 기호: `supersession.citation`·`machine_card`·`guard_tripped`·`scan`·`Outcomes`·`retire_after`·`_diagnostic_row`, `lifecycle.api.retire_card`.

- `_reuse_source_revision`: 제품 `process_job` 하나. 시험 없음(직접 호출).
- `selected_compiled_revision_card`: 바꾸지 않았다. 제품 `_reuse_source_revision`. 시험 `test_revision_skip.py` 6곳.
- `process_job`: 제품 `process_job_settled` ← `_main_locked`(worker main). 시험 `test_checkout_fingerprints.py` 7곳, `test_revision_skip.py` 12곳, `test_summary_history.py` 2곳, `test_retrieval_overlay.py` 1곳, `test_compile_loop_110.py` 1곳, 새 `test_superseded_revision.py`. 기존 시험의 fake writer 카드는 관리 블록이 없어 대체 대상이 아니므로 결과가 같다.
- `wiki_source_retire.retire_card`: 제품 `wiki_lifecycle.run`(`LEGACY_MISSING`·`UNPROVEN`), 새 `supersession.retire_after`(`lifecycle.api.retire_card` 경유). 시험 `test_wiki_source_retire.py` 3곳. 서명·동작 불변.
- 영속 데이터 writer·reader:
  - `retired-wiki/` 보관본과 sidecar: writer `card.archive_card`(`retire_card`·`retire_deleted_source`). 새 `proofHash` 값 `superseded_by_revision`. 제품 reader 없음(운영자 복구용).
  - `wiki` 활동 이벤트 `retired`: writer `wiki_activity_log.append`. reader `event_log.wiki_tail`(`_WIKI_OUTCOMES`에 `retired` 포함, 추출기 `logTail`에 나타난다)과 `memvane:logs`.
  - compile `candidate` 감사 행의 새 reason(`superseded_retire_guarded`·`superseded_scan_unavailable`·`superseded_source_changed`·`retired/superseded_by_revision`): writer `retire_after`. reader `pipeline.summary`(kind별 집계), `retention`, `wiki-ledger` CLI. 판정에 쓰이지 않는다.
  - 카드 frontmatter: 읽기만 한다.

### 실제 저장소 추정 (읽기 전용, 11-28)

`/Users/dulee/work/qmd-auto-context` main checkout의 카드를 `python3 -B -I`로 읽어 `supersession.citation`으로 분류했다. 원문 파일은 읽기만 했고 원장·큐·카드에는 쓰지 않았다. 스크립트: `/private/tmp/claude-501/-Users-dulee-work-qmd-auto-context--orca-worktrees-qmd-auto-context-wiki-fix-c/d6de69bf-3fb4-4671-9951-5711202138a8/scratchpad/est/estimate.py`.

- 카드 648장, 전부 기계 카드. 대체 대상 카드 342장, 보호된 옛 revision 카드 0장.
- 대체 대상이 있는 원문 45개. 그중 41개는 현재 revision의 verified 카드가 있어 지금까지 `already_compiled_revision`으로 끝나던 원문이다(이번 변경으로 추출이 다시 돈다). 나머지 4개는 원래도 추출하던 원문이고 이번 변경으로 은퇴가 붙는다.
- 은퇴 후보 상한 342장(답이 모두 완전할 때 하한 69장 = 원문별 `max(0, 대체 대상 − 10)`의 합). 원문당 최대 27장(`docs/ROADMAP.md`). 대량 가드 기준 162장(648의 25%)을 넘는 원문은 없다.
- 유료 호출 상한: 추출 41회(split 재요청이 붙으면 원문당 1회 더, 최대 82회), 검수 343회(원문별 `min(10, 대체 대상 + 현재 카드)`의 합). run당 추출 10회 예산이라 최소 5 run에 나뉜다.
- service-engineering(카드 5,828장)은 증명 스캔과 같은 2,048장 상한에 걸려 형제 증명 skip 자체가 없다. 이번 변경으로 추출 수는 늘지 않고, 은퇴는 스캔 상한 때문에 돌지 않는다(`superseded_scan_unavailable` 행).

### 검사

- 수정 전 코드(`2fbf4339`)에서 새 시험 17개 중 15개 실패(red). 통과한 2개(H1, 빈 답)는 수정 전후 같은 결과여야 하는 칸이다.
- `npm run test:focused -- --test-name-pattern test_superseded_revision memvane/wiki/compile/tests/run.test.mjs`: 17/17 통과.
- teeth: `citation`의 보호 판정과 `guard_tripped`를 끄면 P1 시험과 M1 시험이 실패한다(원복 후 통과).
- `npm run test:focused -- memvane/wiki/compile/tests/run.test.mjs`: 모듈 26/26, Python 시험 209개 통과.
- `npm run test:focused -- memvane/wiki/lifecycle/tests/run.test.mjs memvane/wiki/verify/tests/run.test.mjs memvane/wiki/pipeline/tests/run.test.mjs`: 모듈 22/22, Python 시험 248개 통과.
- `tests/structure/v1_guard.py --strict`·`--tests`: 모든 규칙 0. 새 모듈 `supersession.py`를 `context-map.json`에 등록했다.
- 레인 D `b7ac41c3`·레인 A `55b3dd43`을 병합한 `4973a5f1`에서 다시 돌렸다. compile(`run.test.mjs`·`stale-1015.test.mjs`) 모듈 27/27·Python 시험 219개, lifecycle·verify·pipeline 모듈 22/22·Python 시험 250개, `wiki-db-provenance` e2e 통과. `static.test.mjs`는 G4 1건 실패: `memvane/wiki/lifecycle/git_branch_safety.py` 913줄이 기준선 871줄을 넘는다. 레인 A 브랜치 단독에서도 같은 값이다. 레인 C 파일은 기준선 안이다(`wiki_compile_worker.py` 1,895/2,006줄).

### 남은 위험

- `docs/git-worktree-search-sync-policy.md` 47행은 "신뢰 카드가 있으면 `already_compiled_revision`"으로 적혀 있다. 이 레인의 수정 범위 밖이라 통합 때 대체 대상 조건을 함께 적어야 한다.
- F3·F4·F7·M1·H5는 원문이 바뀌지 않으면 7일마다 추출 1회가 든다.
- I1(writer 뒤 은퇴 전 중단)은 추출 1회와 다시 나온 카드 검수를 한 번 더 산다.
- 은퇴는 추출 답이 원문의 현재 전체 설명이라는 전제에 기댄다. 모델이 주제를 빠뜨리면 그 카드는 은퇴한다(보관본은 `retired-wiki/`에 남는다).
- "사람이 편집한 카드" 판정은 바이트로 보이는 흔적(관리 블록 밖 본문, 검수 지문 불일치, `createdBy`)만 본다. 관리 블록 안을 고치고 검수 지문이 없는 generated 카드는 기계 카드로 본다.

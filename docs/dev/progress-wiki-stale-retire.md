# 원문 불일치 위키 카드 정리 진행 기록

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

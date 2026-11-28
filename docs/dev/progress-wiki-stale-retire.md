# 원문 불일치 위키 카드 정리 진행 기록

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

# 1.1.4 진행 기록 — Stage 3 wiki pipeline 통합

## 기준 상태

- 브랜치: `claude/1.1.4` (출시 태그 `v1.1.2`)
- 완료 단계: Stage 0+1 `3a3f7001`(stateFormat 2, `~/.local/state/memvane/v2`, 설계서 r4), Stage 2 `41fc37b9`(`memvane/wiki/pipeline/` 신설, 기존 wiki 도메인에서 호출 없음)
- 기준 문서: [공통 개발 규칙](contexts/working-rules.md), [릴리스 체크리스트](contexts/release.md), [설계서 §4.2·§5](../design-wiki-pipeline-v2.md), [pipeline README](../../memvane/wiki/pipeline/README.md)
- 현재 단계: **A. 계획 게이트 — 코디네이터 검토 대기. 제품 코드는 수정하지 않았다.**
- 워킹트리의 미커밋 부분 bump(`package.json`·`plugin.json`·`plugin.yaml` = 1.1.4)는 C1에서 마무리한다.

## 1. 조사로 확인한 사실

### 1.1 실제 프로젝트 상태(이 저장소 `.memvane/`, 읽기 전용 관찰)

| 관찰 | 값 | 의미 |
|---|---|---|
| `.memvane/wiki/concepts/.first-*` | 119개 (11-25 생성) | 첫 카드 게시가 끝나지 않은 임시 파일 |
| `.handoff/v1/hold/<hex32>` | 647개 | 그중 `work` 파일 없는 예약 잔재 335, `native-review` held 159, `deferred-replacement`(`awaitingReferences`) 153 |
| `native-review` held 중 proof에 `device`·`inode`·`temp`가 있는 것 | 134 | `first_card._resume`의 `matches()`가 `(dev, ino)`를 대조한다 → 재부팅으로 `st_dev`가 바뀌면 영구히 `False` |
| `.handoff/v1/copy-quota/allocated-*` | 약 220 | 누수 예약 |
| hold `raw` | verify-queue 행 JSON(`targetPath`·`pendingPath`·`expectedTargetHash`…) | Stage 2 migrate의 `raw` 해석과 일치 |
| hold `work` | `{kind, phase, binding{job,purpose,route}, blobs, proof, queue, transaction, checksum}` | Stage 2 migrate는 이 control을 버리고 `binding={'migrated':True}`로 넣는다 → 재개 불가 |

### 1.2 코드 사실

- 1.x 배제: 프로젝트 `.memvane/route.json`의 `stateFormat`으로 막는다(`memvane/route/index_host_route.py:_foreign_format`, `host_response._major_allowed`). 1.1.4는 `stateFormat: 1` route를 거절하고 재온보딩을 안내한다. 재온보딩 뒤 1.x 호스트는 `running[0] == 2`가 거짓이라 writer·recall이 모두 거절된다. 1.x와 1.1.4는 서로 다른 state base(`v1`/`v2`)를 쓰므로 state base 단위 배제가 아니라 **프로젝트 route 단위 배제**다.
- 1.1.2와 1.1.0 모두 `.compile-run.lock`을 쓴다(`git show v1.1.2:memvane/wiki/workspace/types.py`). compile worker는 이 lock 안에서 `_run_verify_pass`까지 돈다. 진행 중인 1.x worker와는 이 lock으로 상호 배제된다.
- `.topical-publish.lock`은 프로젝트 루트에 있고 `wiki_mutation_lock.lock`이 프로세스 안에서 재진입 가능하게 감싼다. `compile_paths.card_write_lock`은 재진입하지 않는다(매번 새 fd).
- `pipeline/publish.py`의 기본 `_card_locks`는 두 lock 파일을 새 fd로 다시 연다. 이미 `card_write_lock`이나 `wiki_mutation_lock`을 잡은 같은 프로세스에서 부르면 flock이 자기 자신을 기다려 교착한다. `pipeline`은 rank 1이라 rank 5 `workspace`를 import할 수 없으므로 lock은 호출자가 `lock_context`로 주입해야 한다.
- worker·provider SHA 쌍: `v1.1.1`·`v1.1.2`·HEAD 모두 worker `8d6fdd22…`, provider `87cdbd2b…`로 `reattest.PREVIOUS_PAIR`와 같다. 이전 쌍 표에 추가할 새 쌍이 없다.

## 2. writer / reader 전수 (CodeGraphContext `find_callers` + `rg`, repo `/Users/dulee/work/qmd-auto-context` 인덱스 최신 확인)

### 2.1 보이는 카드(`.memvane/wiki/**/*.md`)를 rename·덮어쓰는 writer — `publish_card`로 옮길 대상

| writer | 위치 | 현재 방식 | lock |
|---|---|---|---|
| 첫 카드 게시 | `verify/first_card.py:_resume` (74) | `.first-<hex32>` 작성 → `rename_noreplace` | 호출자(`native_review`/`first_card.recover`)가 card lock + wiki lock |
| 교체 승격 | `verify/promotion.py:_move`·`publish` (141, 226) | `rename_exchange`로 live↔pending 교환, 기존 바이트는 archive 폴더에 보존 | card lock + wiki lock |
| 검수 stamp | `verify/promotion.py:write_stamp` (333) | `write_text_atomic` | card lock |
| 생성 카드 재작성 | `compile/wiki_compile.py:rewrite_generated_card` (679), frontmatter patch (781) | `write_text_atomic` | card lock |
| 후보 숨김 파일 | `verify/candidate_stage.py` (124), `pending_replacement.py` (286, 294) | `.pc-*`/`.pending-*` 숨김 파일 rename | 보이는 카드가 아니므로 이번 범위에서 유지 |
| 보관 | `card/archive.py` (34, 52) | `retired-wiki/`로 `os.link` | 유지 |

제품 호출부: `native_review`, `first_card.recover`, `completed_review`, `pending_handoff`, `upgrade_queue`, `wiki_verify_worker`(stamp), `wiki_compile`(재작성). 테스트 호출부: `verify/tests/integration/test_first_card.py`, `test_promotion.py`, `test_incomplete_stamp.py`, `compile/tests/integration/test_visible_card_handoff.py`, `compile/tests/unit/test_compile_decisions.py`, `tests/support/test_release_smoke.py`, `learning/tests/scripts/project_learning_setup.py`.

### 2.2 hold/work 저장소(`handoff_work.Work`, `copy_quota`, `HOLD_SCAN_LIMIT`) — `staged_cards`로 옮길 대상

- 정의: `verify/handoff_work.py`(`Work.create/_reserve/reclaim_orphans/entries/lookup/archive/release/read/commit`, `HOLD_SCAN_LIMIT=1024`, `entries(max_bytes=8 MiB, max_entries=64)`), `verify/copy_quota.py`(`charge/allocate/release`, `allocated-*`).
- 제품 호출부(모듈별 참조 수): `native_review` 10, `ordinary_slot` 8, `pending_handoff` 7, `first_card` 6, `deferred_review` 5, `queue_batch_prepare` 5, `legacy_handoff` 4, `batch_handoff` 3, `queue_publication` 3, `completed_review` 2, `upgrade_queue` 2, `compile/backfill_work` 1, `compile/wiki_compile_worker` 1. `HOLD_SCAN_LIMIT` 사용: `deferred_review`·`ordinary_slot`·`batch_handoff`·`first_card`·`queue_batch_prepare`·`pending_handoff`·`native_review`.
- 테스트 호출부: `verify/tests/unit/test_handoff_work.py`, `test_batch_handoff.py`, `verify/tests/support.py`, `verify/tests/integration/{test_first_card,test_candidate_stage,test_legacy_handoff,test_pending_cleanup_recovery}.py`, `compile/tests/integration/test_bounded_verify_handoff.py`, `tests/support/wiki_handoff.py`, `runtime/tests/integration/test_runtime_wake_queue.py`.
- `raw` blob은 두 형태다: 일반 바이트, 또는 `batch_handoff`가 쓰는 `{'storage':'inode','ino','length'}`(claimed 큐 파일의 inode 참조). `staged_cards`는 둘 다 담아야 한다.

### 2.3 이번 범위에서 바꾸지 않는 저장소(결정 D1 추천안 기준)

| 저장소 | writer | reader | 유지 이유 |
|---|---|---|---|
| `source-queue.jsonl`·`verify-queue.jsonl`·`*.claimed.*` | `intake/wiki_compile_enqueue`, `intake/source_queue`, `workspace/queue*`, `queue_return`, `upgrade_queue`, `pending_replacement` | compile/verify worker, `host_response.check_compile_queue`, `selected_execution`, `queue_batch_prepare` | 호스트 hook(3 host) 계약 경로. 행 모양은 release.md 23행 계약 |
| refresh 원장(`source-refresh-pending.jsonl`, `.handoff/v1/refresh/events.jsonl`) | `card/wiki_freshness`, `card/refresh_lanes` | compile worker, `refresh_settlement`, `wiki_discovery`, `host_response` | Phase 1 단일 편집 운반체. hook의 0.15초 lock 상한이 걸린 경로 |
| 감사 원장(`generated-manifest`·`tombstones`·`verify-*`·`dedup-*`·`candidates`·`merge-needed`·`discard-ledger`) | `wiki_compile`, `verify_ledger`, `manifest_ledger`, `wiki_dedup_*` | `wiki_freshness`, `wiki_lifecycle`, `manifest_scan`, dedup skill·resolver agent(`dedup-needed.jsonl`을 직접 읽는다) | release.md 23행 계약 + skill/agent 공개 인터페이스 |
| 쿨다운 파일 | compile/verify/dedup worker | 같은 worker | 파생 상태, 이관 이득 없음 |
| `returns/`·`ownership-*`·`review-turn-*`·`publication/`·`.pending-*`·`.pending-ownership.json`·`.verify-swap-*` | 큐 반환·교체 경로 | 같은 경로 | JSONL 큐와 묶인 상태. 큐가 남으면 함께 남아야 한다 |

## 3. 결정 필요

| ID | 쟁점 | 추천안 | 대안 |
|---|---|---|---|
| **D1** | Stage 3 범위. 설계서 §4.2 표는 큐·refresh·감사·쿨다운까지 SQLite로 옮기지만, §5 Phase 2 검증 항목은 게시 중단 복구·`hold > 1024`·마이그레이션만 요구한다. 전부 옮기면 40개 이상 모듈, 3 host hook의 `check_compile_queue`, dedup skill/agent, release.md 23행 계약이 함께 바뀐다. | **게시·hold·복구·마이그레이션만 통합한다.** 보이는 카드 게시 → `publish_card`, `Work` 저장소 → `staged_cards`, worker 시작 시 `recover_publishes` + `migrate_from_v1`. 큐·refresh·감사·쿨다운은 JSONL 유지. `compile_jobs`·`verify_jobs`·`refresh_events`·`audit_events`·`cooldowns` 테이블은 DDL에만 남기고 1.1.4에서 쓰지 않는다(설계서 §4.2에 범위 주석 추가). | 설계서 그대로 전부 이관. 1.1.4 일정과 3 host 회귀 위험이 크고 release.md 23행 계약 개정이 필요하다 |
| **D2** | Stage 2 `migrate_from_v1`은 큐·refresh·감사 JSONL, `.handoff/v1` 전체(`refresh/events.jsonl` 포함), 위키 숨김 파일(`.pending-*` 포함)을 지운다. D1에서 이 파일들은 계속 쓰이므로 그대로 연결하면 미처리 큐 행·pending refresh·교체본이 유실된다. | **마이그레이션 범위를 D1과 맞춘다**: `.handoff/v1/{hold,work,closed}` + `copy-quota/`(+`allocated-*`) + work가 참조하는 `.first-*`만 흡수·정리한다. 나머지는 손대지 않는다. | — |
| **D3** | `publish_card`의 교체 경로는 lock 아래 CAS 뒤 `os.replace`다. lock은 advisory라 편집기 저장은 막지 못하므로 CAS와 rename 사이에 저장된 사람 편집을 덮어쓴다. 기존 `promotion`은 `rename_exchange`로 기존 파일을 보존한다. | **교체는 `rename_exchange`(temp↔live) 후 밀려난 파일 SHA를 `expected_target_sha`와 대조한다.** 같으면 unlink하고 `published`. 다르면(창 안의 사람 편집) 다시 교환해 사람 편집을 live로 되돌리고 `conflict_user_edited`. 복구는 "live가 `(temp_ino, temp_sha)`이고 temp 이름에 다른 파일이 있으면 교환 완료"로 판정해 같은 대조를 한다. 신규 생성은 `rename_noreplace` 유지. 같은 내용이 이미 live에 있으면(같은 초·같은 바이트 재게시) rename 없이 `published`. | `os.replace` 유지(사람 편집 유실 창 허용) |
| **D4** | lock 주입. pipeline은 workspace를 import할 수 없다. | 제품 호출부는 항상 `lock_context`를 넘긴다. card lock을 이미 쥔 호출부는 `wiki_mutation_lock.lock(root)`만, 아닌 호출부는 `card_write_lock` → `wiki_mutation_lock.lock` 순서를 묶은 helper(`workspace`에 추가)를 넘긴다. 기본 `_card_locks`는 테스트 전용으로 남긴다. | — |
| **D5** | 마이그레이션을 누가, 언제, 어떤 lock 아래 1회 실행하나. | **`wiki_compile_worker._main_locked` 진입 직후**, `.compile-run.lock` 아래에서 route가 `stateFormat: 2`로 다시 확인된 뒤 `card_write_lock` → `.topical-publish.lock` 순서로 잡고 `recover_publishes` → `needs_v1_migration`이면 `migrate_from_v1` 순서로 실행한다. 진행 중인 1.x worker는 `.compile-run.lock`으로 배제되고, 이후 1.x 실행은 route에서 거절된다. 마이그레이션은 **한 SQLite 트랜잭션**(행 삽입 + `migrated_from_v1_at` 마커)으로 커밋하고, 파일 정리는 마커 뒤 멱등으로 한다. 새 lock·intent·wake kind·hook action은 필요 없다. | setup 재온보딩 apply에서 실행(설치 journal lock 아래). setup이 위키 상태를 다루게 되고 재온보딩 없이 worker가 먼저 도는 경로를 못 막는다 |
| **D6** | Stage 2는 `raw` 없는 hold·깨진 hold에 `pipeline_fences(global_publish)`를 세운다. 이 fence를 지우는 주체가 없어 모든 게시가 영구 보류된다(계속 보류되는 칸). | 마이그레이션은 fence를 세우지 않는다. `work` 없는 예약 잔재(실측 335)는 기존 `reclaim_orphans`와 같은 의미로 지운다(원본은 verify-queue 행). checksum이 깨진 hold는 `retired-wiki/migrated-handoff/<id>/`로 옮기고 `discard` 감사 행을 남긴다. `global_publish` fence 사용처는 D1 범위에서 없어진다. | fence 유지 + 수동 해제 절차 |
| **D7** | 마이그레이션 합계가 64 MiB를 넘는 경우. | 이관 행은 quota 검사 없이 넣는다(작업 유실 금지). 새 stage는 합계가 내려갈 때까지 `quota_full`로 기다리고, 게시가 진행되면 풀린다. | 초과분 폐기 |
| **D8** | 혼합 버전 전용 장치(`legacy_handoff`, `upgrade_queue`, `queue_publication`, `ownership`/`review-turn`)는 route 단위 배제 뒤 죽은 경로다. | Stage 3에서 지우지 않는다. `Work` 어댑터를 통해 계속 동작하게 두고 별도 정리 단계로 넘긴다. | Stage 3에서 삭제 |

## 4. 커밋 단위 (승인 후)

| 커밋 | 범위 | 변경 파일(예상) | focused 검사 |
|---|---|---|---|
| **C1** 버전 bump 마무리 | 남은 manifest 4종·`teacher_model_tiers.RELEASE`를 1.1.4로. worker·provider 쌍 변화 없음을 이 문서에 기록. tier 표 외부 가격 문서 대조는 **미확인**으로 기록 | `.claude-plugin/{plugin,marketplace}.json`, `.codex-plugin/plugin.json`, `.agents/plugins/marketplace.json`, `teacher_model_tiers.py`, `package.json`, `plugin.json`, `plugin.yaml` | `tests/plugin-manifest.test.mjs` |
| **C2** pipeline 보강 | D2·D3·D4·D5·D6·D7. `staged_cards`에 `raw_blob`(nullable)·`raw_meta`(inode 참조)·`phase`·`purpose`·`queue` 열, Work control 왕복. `publish.py` 교환 경로·동일 바이트 멱등·txn 없는 `.publish-*.tmp` 정리. `migrate.py` 범위 축소·단일 트랜잭션·work control 이관·`.first-*` 흡수·fence 제거 | `memvane/wiki/pipeline/*` + tests | `memvane/wiki/pipeline/tests/run.test.mjs` |
| **C3** `Work` → `staged_cards` | `handoff_work.Work`의 디렉터리·`.w-*`·`copy_quota` 저장을 pipeline store로 교체(공개 메서드 시그니처 유지). `HOLD_SCAN_LIMIT`·`_ScanLimit`·`copy_quota.py` 제거, 대상별 `staged_for_target` 정확 조회. `first_card` proof의 `device` 제거 | `verify/handoff_work.py`, `copy_quota.py`(삭제), HOLD_SCAN_LIMIT 사용 7개 모듈, `workspace` lock helper, verify tests | `verify/tests/run.test.mjs`, `compile/tests/run.test.mjs` |
| **C4** 보이는 카드 게시 통합 | §2.1 writer를 `publish_card`로. `promotion`의 archive 보존은 교환 결과의 밀려난 파일로 유지 | `first_card.py`, `promotion.py`, `wiki_compile.py` | verify·compile run.test.mjs |
| **C5** worker 시작 복구·마이그레이션 | D5 연결 | `compile/wiki_compile_worker.py`, README | compile run.test.mjs, pipeline run.test.mjs |
| **C6** E2E | §6의 E2E + 3 host 경로 | `tests/e2e/wiki_pipeline_*.py` + `.test.mjs`, test-parts 등록 | 새 e2e, `memvane/host/tests/run.test.mjs`, `hermes_adapter` 테스트 |
| **C7** 문서 | pipeline·verify·compile·workspace README, 설계서 §4.2 범위 주석(D1), release.md(계약 변화 없음 확인) | docs | `tests/structure/doc-links.test.mjs` |

각 커밋 뒤: `python3 tests/structure/v1_guard.py --strict`, `--tests --rules G1,G8`, `tests/structure/{static,test-parts,doc-links}.test.mjs` 0 위반. 이 문서의 진행 기록·다음 재개 지점 갱신.

## 5. 상태 조합표

축: 대상 세대 이력(신규·교체·사람 삭제·사람 편집), 영속 쓰기 사이 중단 지점(temp 작성 → staged 커밋 → txn `publishing` 커밋 → rename/교환 → `published` 커밋), 같은 초·같은 바이트, 재부팅 `st_dev` 변화, 행을 쓴 버전(1.1.4, 출시 `v1.0.22`·`v1.1.0`·`v1.1.1`·`v1.1.2`). 유료 호출은 extract·verify 엔진 호출 수(fake engine 호출 개수로 단정). "원래 1회"는 그 작업이 원래 치를 호출이며 추가가 아니다.

### 5.1 게시(publish_card / recover_publishes)

| 칸 | 상황 | 기대 결과 | 유료 호출(추가) | 회귀 시험 |
|---|---|---|---|---|
| P1 | 신규, 중단 없음 | `published`, staged `published` | 0 | `test_publish_new_card` |
| P2 | 신규, temp 작성 후 txn 커밋 전 crash | 다음 worker가 txn 없는 `.publish-*.tmp`를 지우고 staged 행(검수 proof 포함)에서 다시 게시 → `published` | 0 | `test_orphan_temp_swept_and_restaged` |
| P3 | `publishing` 커밋 후 rename 전 crash, live 불변 | roll-forward → `published` | 0 | `test_recover_roll_forward` |
| P4 | P3 + 그사이 사람 편집 | temp unlink, `conflict_user_edited`, 사람 편집 보존, staged `cancelled`(재시도 없음) | 0 | `test_recover_roll_back_user_edit` |
| P5 | rename 후 `published` 커밋 전 crash, live = temp SHA | `published` | 0 | `test_recover_after_rename` |
| P6 | P5 + 사람이 다시 편집 | `published_user_edited`, 덮어쓰지 않음 | 0 | `test_recover_published_user_edited` |
| P7 | 교체: 교환 후 대조 전 crash, 밀려난 파일 = expected | unlink, `published` | 0 | `test_exchange_recover_clean` |
| P8 | 교체: CAS와 교환 사이 사람 저장 | 재교환으로 사람 편집 live 복원, `conflict_user_edited` | 0 | `test_exchange_editor_race_restores_user` |
| P9 | 신규 대상에 사람이 같은 경로 파일 생성 | `rename_noreplace` EEXIST → `conflict_user_edited`, 사람 파일 보존 | 0 | `test_new_card_target_occupied` |
| P10 | 같은 초·같은 바이트 재게시(동일 내용 txn 2건) | 둘째는 live = temp SHA로 rename 없이 `published` | 0 | `test_same_bytes_republish_idempotent` |
| P11 | 교체 대상이 사람에게 삭제됨(live 없음) | CAS 실패 → `conflict_user_edited`, 되살리지 않음 | 0 | `test_replacement_after_human_delete_not_resurrected` |
| P12 | P5 직후 사람이 카드 삭제 | `published_user_edited`, 되살리지 않음 | 0 | `test_recover_after_human_delete` |
| P13 | P3·P5·P7을 재부팅 뒤 복구(`st_dev` 변화) | `(st_ino, sha256)`만 대조하므로 P3·P5·P7과 같은 결과 | 0 | `test_recover_ignores_st_dev`(`os.stat` dev 주입) |

### 5.2 마이그레이션(출시 태그 writer가 만든 상태 입력)

| 칸 | 상황 | 기대 결과 | 유료 호출(추가) | 회귀 시험 |
|---|---|---|---|---|
| M1 | `v1.1.2` native-review held, proof에 `device`·`inode`·`.first-*` | staged held(work control 보존, `device` 제거). `.first-*` 바이트가 image와 같으면 흡수 후 삭제, 다르면 `retired-wiki/migrated-handoff/`로 보존. 다음 pass에서 원래 계획된 검수 후 게시 | 0 (원래 verify 1) | `test_migrate_v112_native_review_first_card` |
| M2 | `work` 없는 예약 잔재 | 삭제, staged 없음, verify-queue 행으로 정상 처리 | 0 | `test_migrate_reservation_residue` |
| M3 | `deferred-replacement`(`awaitingReferences`) | staged held, 참조 확인은 JSONL claim 대조(64 상한 없음) | 0 | `test_migrate_deferred_replacement` |
| M4 | checksum 깨진 hold | `retired-wiki/migrated-handoff/<id>/`로 이동, `discard` 감사 행, fence 없음 | 0 | `test_migrate_corrupt_hold_quarantined` |
| M5 | 마이그레이션 트랜잭션 커밋 전 crash | 롤백, 재실행 결과가 1회 실행과 같은 행 수 | 0 | `test_migrate_crash_before_commit_idempotent` |
| M6 | 마커 커밋 후 파일 정리 전 crash | 다음 실행은 정리만 하고 staged 행 수 불변 | 0 | `test_migrate_crash_after_marker` |
| M7 | 진행 중 1.x worker | `.compile-run.lock` 경합 → 1.1.4는 이번 pass 양보, 다음 wake에 실행 | 0 | `test_migrate_waits_for_run_lock` |
| M8 | route `stateFormat: 2` 뒤 `v1.1.2` worker 실행 | 선택 route 거절로 아무것도 쓰지 않음 | 0 | `test_v112_worker_refuses_v2_route`(태그 번들 실행) |
| M9 | 누수 `allocated-*`(실측 약 220) | 삭제, quota = staged blob 합계 | 0 | `test_migrate_drops_allocated` |
| M10 | 이관 합계 > 64 MiB | 이관은 전부 성공, 새 stage는 `quota_full` 대기, 게시가 진행되면 해소 | 0 | `test_migrate_over_quota_drains` |
| M11 | 사람이 지운 카드의 교체 hold | staged 후 게시 시 P11 → 되살리지 않음 | 0 | `test_migrate_replacement_of_deleted_card` |
| M12 | `v1.0.22`·`v1.1.0`·`v1.1.1` 태그의 hold(있는 버전만) | M1~M3과 같은 규칙. hold 형식이 없는 태그는 마이그레이션 대상 없음(`needs_v1_migration` 거짓) | 0 | `test_migrate_release_tags`(태그 매개변수화) |
| M13 | 큐·refresh·감사 JSONL과 `.pending-*` | 그대로 남음(D2) | 0 | `test_migrate_preserves_jsonl_and_pending` |

### 5.3 hold 규모

| 칸 | 상황 | 기대 결과 | 유료 호출 | 회귀 시험 |
|---|---|---|---|---|
| H1 | held 1,025건 이상 + 무관한 대상 1건의 검수 job | 해당 대상 정확 조회로 처리, 전역 정체 없음 | 원래 verify 1 | `test_hold_over_1024_does_not_stall` |
| H2 | pass당 처리 예산(64건·8 MiB) 초과 backlog | 오래된 순으로 매 pass 진척, 같은 입력에서 계속 보류되는 행 없음 | 원래 호출만 | `test_hold_backlog_progresses_each_pass` |

### 5.4 3 host 경로

| 칸 | 상황 | 기대 결과 | 회귀 시험 |
|---|---|---|---|
| X1 | Claude/Codex PostToolUse `compile`·`index`, SessionStart `update-queued`, Bash `file-change-event` | hook 출력·exit code·wake 행이 Stage 3 전과 같음(hook은 pipeline을 열지 않는다) | 기존 `memvane/host/tests/run.test.mjs` + 새 E2E에서 hook → worker → `publish_card` 1회 |
| X2 | Hermes `index`(`hermes_adapter/core_bridge.py`) | 같은 wake, 같은 게시 결과 | 기존 hermes 테스트 + 새 E2E Hermes 분기 |

## 6. 테스트 계획

- 단위·통합(pipeline): §5.1 P1~P13, §5.2 M1~M11·M13. 중단 지점은 `publish.py`·`migrate.py`의 내부 단계 함수를 테스트에서 예외로 교체해 만든다(제품 코드에 테스트 스위치를 넣지 않는다).
- 출시 태그 writer 입력: `git archive v1.1.2`(M12는 각 태그)를 임시 디렉터리에 풀고, 그 번들의 verify 경로를 fake engine으로 돌려 hold·`.first-*`를 만든다. 손으로 만든 hold 행을 판정 경로에 넣지 않는다. 태그 번들이 hold를 만들 수 없는 버전은 그 사실을 기록한다.
- E2E(C6): 임시 HOME·fixture state root. hook(3 host) → `laya-compile` wake → compile worker(fake engine) → 게시 1회. 중단·재개(P3·P5·P8), `hold > 1024`(H1), 1.x→2.0 마이그레이션 E2E(M1·M8).
- teeth: 새 시험마다 대응 제품 줄을 한 번 되돌려 FAIL을 확인하고 이 문서에 기록한다.
- 시험 개수: C2 시작 전과 C6 뒤 각 run.test.mjs의 pass/fail 개수를 기록한다.
- 전체 `npm test`는 돌리지 않는다(코디네이터가 최종 커밋에서 실행).

## 7. 진행 기록

| 시각 | 단계 | 커밋 | 결과 |
|---|---|---|---|
| 2026-11-26 | A. 계획 게이트 | (이 문서) | 코디네이터 검토 요청 |

## 8. 다음 재개 지점

코디네이터 답(D1~D8 결정)을 받은 뒤 C1부터 시작한다. 답을 받기 전에는 제품 코드를 고치지 않는다.

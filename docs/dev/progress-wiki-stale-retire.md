# 위키 원문 불일치·방치 generated 카드 수정 진행 기록

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

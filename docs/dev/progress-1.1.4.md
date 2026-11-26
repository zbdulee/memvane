# 1.1.4 진행 기록 — Stage 3 wiki pipeline 통합

## 기준 상태

- 브랜치: `claude/1.1.4` (출시 태그 `v1.1.2`)
- 완료 단계: Stage 0+1 `3a3f7001`(stateFormat 2, `~/.local/state/memvane/v2`, 설계서 r4), Stage 2 `41fc37b9`(`memvane/wiki/pipeline/` 신설)
- 기준 문서: [공통 개발 규칙](contexts/working-rules.md), [릴리스 체크리스트](contexts/release.md), [설계서 §4.2·§5](../design-wiki-pipeline-v2.md), [pipeline README](../../memvane/wiki/pipeline/README.md)
- 현재 단계: **A안 구현 중.** A2~A7은 사용자 승인(msg_c81bed7e80ef, 전부 §4 추천안). C4부터 §5 커밋 단위대로 진행한다.

## 1. 확정된 결정 (사용자·코디네이터)

| 항목 | 결정 | 출처 |
|---|---|---|
| D1 범위 | **A**: 설계서 §4.2 원안대로 1.1.4에 위키 파이프라인 상태를 전부 `wiki-pipeline.sqlite3`로 옮긴다 | msg_786865a87709 |
| 원칙 1 | 상태는 원장 한 곳에만 둔다: 큐·refresh·감사·쿨다운·pending 교체본·ownership/review-turn 전부 | 같음 |
| 원칙 2 | 3 host 훅은 SQLite를 열지 않는다. 훅이 쓰는 것은 append-only jsonl inbox(최소 개수)뿐이고, worker가 멱등하게 흡수한다 | 같음, msg_620abf86b69e |
| 원칙 3 | `.memvane/wiki/`에는 카드 파일과 `.memvane-root`만 남는다. 예외는 게시 순간의 `.publish-<txid>.tmp` | msg_786865a87709 |
| 원칙 4 | jsonl을 직접 읽던 공개 인터페이스(dedup skill/agent, logs skill)는 조회 CLI로 바꾸고 release.md 계약을 새 원장 계약으로 개정한다 | 같음 |
| 마이그레이션 | 1.x 진행 중 상태(`.handoff/v1/*`, 큐 jsonl·claim, `source-refresh-pending`, 쿨다운, 위키 숨김 파일)는 **버린다**. 가져오는 것은 `generated-manifest.jsonl` 하나(원문 삭제 시 기계 생성 카드 은퇴 판정용). 가져온 뒤 옛 파일을 지운다(순서 반대 금지). 단일 트랜잭션 + 완료 마커, 출시 태그 writer 행으로 시험 | msg_59f35fefa10b, msg_679fd8b45dd7 |
| tombstone | "사람이 지운 카드를 되살리지 않는" 기능을 **없앤다**. 카드를 없애려면 원문을 지운다. `tombstones.jsonl`은 버린다 | msg_679fd8b45dd7 |
| dedup 병합 | 병합 CLI(`wiki_dedup_resolve.py merge --delete`)가 B를 지우기 전에 B의 `canonicalKey`·`title`·`aliases`를 A의 `aliases`로, B의 `sources`를 A의 `sources`로 코드로 옮긴다(auto 블록 밖 frontmatter, 원자적 쓰기, 중복 없이, 실패하면 삭제하지 않음). 별도 병합 원장은 없다 | msg_79ad343ef990 |
| 로그성 기록 | 병합·검수 삭제 같은 기록은 `audit_events`에 로그로만 남기고 어떤 생성·삭제 판정도 읽지 않는다. 기존 로그성 jsonl(`dedup-deleted`·`verify-log` 등)은 가져오지 않는다. `retired-wiki/` 사용자 복구 아카이브는 보존한다 | msg_dfe04095b7ab |
| D3·D4·D7 | publish 교환·대조·멱등, lock 주입, quota — 구현 완료(①) | msg_8a0b16db2cfe |
| D5 | 마이그레이션은 worker 시작 시 `.compile-run.lock` 아래. `selected_execution`의 idle 판정은 마커 전 "할 일 남음"으로 양보, wiki-compile skill은 `pipeline_migration_pending` 거절 + `laya-compile` wake | msg_620abf86b69e |
| Python 3.11 | 하한 상향은 별도 버전 항목. 1.1.4에서 구현하지 않고 `docs/ROADMAP.md`에 항목만 추가(C15) | msg_786865a87709 |

## 2. 조사로 확인한 사실

### 2.1 실제 프로젝트 상태 (이 저장소 `.memvane/`, 읽기 전용 관찰)

`.memvane/wiki/concepts/.first-*` 119개, `.handoff/v1/hold/` 647개(그중 `work` 없는 예약 잔재 335), `copy-quota/allocated-*` 약 220개. `first_card` proof의 `(dev, ino)` 대조로 134건이 재부팅 뒤 영구 보류될 수 있었다. A안 마이그레이션은 이것을 전부 버린다.

### 2.2 코드 사실 (CodeGraphContext `execute_cypher_query`/`find_all_callers`, repo `/Users/dulee/work/qmd-auto-context`, 코디네이터 재인덱싱 뒤 조회 + `rg` 보완)

- 1.x 배제는 프로젝트 `route.json`의 `stateFormat`으로 한다. 재온보딩 뒤 1.x 호스트는 writer·recall이 모두 거절된다. 1.1.2·1.1.0도 `.compile-run.lock`을 쓴다.
- 훅이 프로젝트 compile 디렉터리에 쓰는 것은 `_record_edit_compile` → `wiki_compile_enqueue.enqueue_payload(pending_only=True)` → `wiki_freshness.record_pending_refresh`(`.handoff/v1/refresh/events.jsonl`, 0.15초 lock) 하나다. 훅이 읽는 것은 SessionStart의 `_request_selected_sync`(`host_response.py:610-646`)가 보는 `source-queue.jsonl`·`verify-queue.jsonl` 크기와 `unresolved_pending_refreshes_strict`다.
- recall은 refresh 원장을 읽지 않는다. `wiki_card.pending_refresh_cutoffs`는 제품 호출부가 없다(`find_all_callers` 0, `rg` 정의뿐).
- 훅이 쓰는 그 밖의 파일은 프로젝트 밖이다: discovery grant·cursor(install state), `external-edits-*.json` inbox(install state), lifecycle request(sync state). 이 셋은 sync/intake 소유의 기존 inbox라 위키 파이프라인 원장 이관 대상이 아니다.
- tombstone 생성: `wiki_compile.py:1169-1201`. 카드가 없고 manifest 마지막 행이 `verify-deleted`가 아니면 `promotion.missing_retirement`가 `'manual'`을 돌려 tombstone을 쓴다. dedup 병합으로 지운 카드도 여기서 tombstone이 된다(코디네이터 지적 재확인).
- `verify-skipped.jsonl`은 로그가 아니라 **비용 판정 원장**이다: `wiki_compile_worker.py:225-233`이 `load_verify_suppressions`로 같은 원문 body hash의 재추출을 막는다(검수 inconclusive 뒤 생성→검수→삭제 반복 방지). `dedup-skipped.jsonl`도 같은 쌍의 유료 judge 반복을 막는 판정 원장이다(`wiki_dedup_scan.load_skip_suppressions`).
- `generated-manifest.jsonl`을 읽는 `learning/*`·`provider/*`의 `MANIFEST`는 이름만 같은 다른 파일(`runtime.json` 등)이다.

### 2.3 이관 대상 전수 — 영속 파일별 writer/reader

제품 호출부는 `memvane/` 기준 상대 경로. 시험 수는 그 파일명·상수를 참조하는 시험 파일 수다. 이 표 전체에 걸쳐 시험 파일 95개가 영향을 받는다.

| 1.x 파일 | 제품 writer·reader | 시험 파일 | 1.1.4 위치 | 판정에 쓰나 | 마이그레이션 |
|---|---|---|---|---|---|
| `source-queue.jsonl`(+`*.claimed.*`) | `host/host_response.py`(크기만), `wiki/intake/wiki_compile_enqueue.py`, `wiki/intake/source_queue.py`, `wiki/workspace/{queue,queue_paths,compile_paths,wiki_selected_laya}.py`, `wiki/workspace/handoff/api.py`, `wiki/compile/{wiki_compile_worker,selected_execution}.py`, `wiki/verify/wiki_verify_worker.py` | 25 | `compile_jobs` | 큐 | 버림 |
| `verify-queue.jsonl`(+claim) | 위 + `wiki/workspace/queue_return.py`, `wiki/verify/pending_replacement.py`, `wiki/compile/wiki_compile.py` | 25 | `verify_jobs`(+교체본 blob) | 큐 | 버림 |
| `source-refresh-pending.jsonl`, `.handoff/v1/refresh/events.jsonl` | `wiki/card/{wiki_freshness,refresh_lanes,legacy_ledgers}.py`, `wiki/compile/refresh_settlement.py`, `wiki/intake/wiki_discovery.py`, `wiki/workspace/manifest_ledger.py`, `host/host_response.py` | 8 | 훅: `edit-inbox.jsonl`(§3) → `refresh_events` | 예 | 버림 |
| `source-refresh-settled.json` | `wiki/compile/refresh_settlement.py` | — | `refresh_events.state` | 예(파생) | 버림 |
| `generated-manifest.jsonl`(+`compact-stamp`) | `wiki/compile/wiki_compile.py`, `wiki/workspace/manifest_ledger.py`, `wiki/verify/{wiki_verify_worker,first_card,promotion}.py`, `wiki/card/{legacy_ledgers,manifest_scan}.py` | 23 | `card_generations`(신규 테이블) | **예**(같은 정체성 이전 생성, 원문 삭제 시 기계 생성 카드 은퇴) | **가져옴** |
| `tombstones.jsonl` | `wiki/compile/wiki_compile.py`, `wiki/card/legacy_ledgers.py`, `wiki/workspace/compile_paths.py` | 7 | 없음(기능 제거) | — | 버림 |
| `verify-skipped.jsonl` | `wiki/verify/{wiki_verify_worker,verify_ledger}.py`, `wiki/compile/wiki_compile_worker.py` | 6 | `suppressions`(신규) | **예**(비용 가드) | 결정 필요 A3 |
| `dedup-skipped.jsonl` | `wiki/dedup/{wiki_dedup_scan,wiki_dedup_resolve,wiki_dedup_judge}.py`, `wiki/verify/{verify_ledger,wiki_verify_worker}.py` | 3 | `suppressions` | **예**(비용 가드) | 결정 필요 A3 |
| `dedup-needed.jsonl` | `wiki/dedup/{wiki_dedup_scan,wiki_dedup_resolve}.py`, `wiki/workspace/queue.py`, `skills/wiki-dedup/SKILL.md`, `agents/wiki-dedup-resolver.md` | 4 | `dedup_pairs`(신규 큐) + 조회 CLI | 큐 | 버림(다음 scan이 다시 넣음) |
| `discard-ledger.jsonl` | `wiki/workspace/queue.py`, `wiki/intake/wiki_backfill.py`(`reconcile_discards`) | 6 | `compile_jobs.state='dropped'` + 사유 | 예(backfill 재조정) | 버림 |
| `verify-log.jsonl`, `verify-deleted.jsonl`, `dedup-deleted.jsonl`, `merge-needed.jsonl`, `candidates.jsonl` | `wiki/verify/{wiki_verify_worker,pending_ownership,legacy_handoff,promotion,verify_ledger}.py`, `wiki/dedup/{wiki_dedup_judge,wiki_dedup_resolve}.py`, `wiki/compile/{wiki_compile,wiki_compile_worker}.py`, `skills/logs/SKILL.md`, `agents/wiki-dedup-resolver.md` | 10·12·1·2·14 | `audit_events`(로그 전용) | **아니오**(판정 코드에서 제거) | 버림 |
| `cooldown`, `verify-cooldown`, `verify-engine-cooldown.json`, `dedup-judge-cooldown`, `dedup-judge-engine-cooldown.json` | `wiki/compile/{wiki_compile_worker,wiki_compile}.py`, `wiki/verify/wiki_verify_worker.py`, `wiki/dedup/wiki_dedup_judge.py`, `wiki/workspace/queue.py` | 13(이름 충돌 포함) | `cooldowns` | 예(실행 보류) | 버림 |
| `.handoff/v1/{hold,closed,publication,legacy}` | `wiki/verify/handoff_work.py` — **①에서 `staged_cards`로 이관 완료** | — | `staged_cards` | 예 | 버림 |
| `.handoff/v1/{returns,ownership-*,review-turn-*,case,backfill,publication}` | `wiki/workspace/queue_return.py`, `wiki/verify/{ordinary_slot,native_review,legacy_handoff,pending_ownership,queue_publication}.py`, `wiki/compile/backfill_work.py` | 다수 | 큐 행 상태·`staged_cards`로 흡수, 장치 자체 삭제(§4 A4) | 예 | 버림 |
| `.handoff/v1/manifest-scan/projection.db` | `wiki/card/manifest_scan.py` | 3 | 파생 캐시. `card_generations` 인덱스로 대체 | 파생 | 버림 |
| `.handoff/v1/staged/<id>/raw` | ①의 inode batch raw | — | claimed 큐 파일이 없어지면 장치 자체 삭제 | — | 버림 |
| 위키 숨김 `.first-*` | `wiki/verify/first_card.py` | 2 | `staged_cards.image_blob` + `publish_card` | — | 버림(삭제) |
| 위키 숨김 `.<card>.pending-<hex16>`, `.pc-*` | `wiki/compile/wiki_compile.py`(`write_pending_card`), `wiki/verify/{pending_replacement,candidate_stage,promotion,ordinary_slot,native_review,pending_handoff,completed_review,upgrade_queue,queue_batch_prepare,legacy_handoff}.py` | 다수 | `verify_jobs` 교체본 blob + `publish_card` 교환 | 예 | 버림(삭제) |
| 위키 숨김 `.pending-ownership.json`, `.verify-swap-*` | `wiki/verify/{pending_ownership,queue_publication,legacy_handoff}.py`, `wiki/workspace/handoff/api.py` | 다수 | 없어짐(원장 트랜잭션이 대신함) | — | 버림(삭제) |
| `retired-wiki/` | `wiki/card/archive.py`, `wiki/verify/promotion.py` | — | 파일 유지(사용자 복구) | 아니오 | 보존 |
| `wiki-retrieval/cards.sqlite` | `wiki/compile/wiki_compile_retrieval.py` | — | 파일 유지(FTS 캐시) | 아니오 | 보존 |
| `.compile-run.lock`, `.card-write.lock`, `.topical-publish.lock` | `wiki/workspace/{compile_paths,wiki_mutation_lock}.py`, `pipeline/publish.py` | — | 유지(계약) | — | — |

### 2.4 시그니처가 바뀌는 함수의 호출부 (CodeGraphContext CALLS, 제품/시험 분리)

| 함수 | 제품 호출 | 시험 호출 | 제품 호출 파일 | 변경 |
|---|---|---|---|---|
| `record_pending_refresh` | 1 | 30 | `wiki/intake/wiki_compile_enqueue.py` | inbox append로 교체(행 모양 유지) |
| `unresolved_pending_refreshes_strict` | 13 | 22 | `host/host_response.py`, `wiki/card/wiki_card.py`, `wiki/compile/{wiki_compile_worker,wiki_compile,selected_execution}.py`, `wiki/intake/wiki_discovery.py`, `wiki/verify/{wiki_verify_worker,first_card}.py`, `wiki/workspace/manifest_ledger.py` | worker 쪽은 원장, 훅 호출부는 제거(§3.3) |
| `unresolved_pending_refreshes` | 2 | 6 | `wiki/compile/{wiki_compile_worker,refresh_settlement}.py` | 원장 |
| `resolve_pending_refresh` | 2 | 9 | `wiki/compile/wiki_compile_worker.py` | 원장 |
| `settle` / `unsettled` | 4 / 4 | 74 / 3 | `wiki/{verify/wiki_verify_worker,compile/wiki_compile_worker}.py`, `runtime/runtime_work.py`, `learning/selector_maintenance.py`, `wiki/compile/selected_execution.py` | `settle`은 이름 충돌(runtime 포함) — 함수별 대조 필요. `unsettled`은 원장 |
| `enqueue_payload` | 1 | 11 | `host/host_response.py` | 훅 경로: inbox만 |
| `claim_source_queue` / `claim_queue` | 1 / 3 | 0 / 28 | `wiki/compile/wiki_compile_worker.py`, `wiki/verify/ordinary_slot.py`, `wiki/dedup/wiki_dedup_resolve.py` | `claim_*_jobs`로 교체 |
| `requeue_lines` | 2 | 4 | `wiki/dedup/{wiki_dedup_scan,wiki_dedup_resolve}.py` | `dedup_pairs` |
| `append_jsonl` / `read_jsonl` / `trim_jsonl` / `compact_manifest` | 14 / 5 / 3 / 2 | 1 / 1 / 1 / 12 | `wiki/{verify/wiki_verify_worker,compile/wiki_compile_worker,compile/wiki_compile,dedup/wiki_dedup_resolve,dedup/wiki_dedup_scan,workspace/queue,workspace/manifest_ledger}.py` | 원장 API로 교체 후 위키 파이프라인 호출부 0 |
| `missing_retirement` | 1 | 0 | `wiki/compile/wiki_compile.py` | tombstone 분기와 함께 제거 |
| `resolve_target` | 1 | 4 | `wiki/compile/wiki_compile.py` | 변경 없음(병합 이름표 이전이 이 색인에 기댄다) |
| `write_pending_card` / `enqueue_replacement` / `create_first_card` / `stage_candidate` | 1 / 1 / 1 / 1 | 0 | `wiki/compile/wiki_compile.py` | 숨김 파일 대신 원장 blob |
| `stamp_verification` | 9 | 5 | `wiki/verify/{wiki_verify_worker,api}.py` | `publish_card`(교체) |
| `record_verify_deletion` | 3 | 1 | `wiki/verify/{wiki_verify_worker,pending_ownership}.py` | `card_generations` 행(판정용) + `audit_events`(로그) |
| `verify_skipped_path` / `load_verify_suppressions` | 3 / 0 | 3 / 3 | `wiki/{compile/wiki_compile_worker,verify/wiki_verify_worker}.py` | `suppressions` |
| `check_compile_queue` | 1 | 1 | `host/host_response.py` | §3.3 |

## 3. inbox 설계 (원칙 2)

### 3.1 파일

- 이름: `.memvane/compile/edit-inbox.jsonl` 하나. lock: 같은 디렉터리의 `edit-inbox.jsonl.lock`(훅은 기존과 같이 `LOCK_EX | LOCK_NB`를 최대 0.15초 폴링).
- 훅 writer: `record_pending_refresh`가 이 파일에 한 행을 `O_APPEND`로 쓴다. 다른 훅 쓰기는 프로젝트 compile 디렉터리에 없다.
- 행 모양 `memvane-edit-inbox-v1`: `{"schema":"memvane-edit-inbox-v1","eventId":<32 hex>,"sourcePath":<프로젝트 상대>,"ts":<RFC3339 Z>,"engine":"claude"|"codex"|"hermes"|"gemini"}` + 기존 private refresh 행의 선택 키를 그대로 허용한다(1.1.2 private lane 행과 같은 키).

### 3.2 소비 규칙 (worker, `.compile-run.lock` 아래)

1. inbox lock을 잡고 `edit-inbox.jsonl`을 `edit-inbox.jsonl.consuming-<token>`으로 rename한 뒤 lock을 놓는다. 이후 훅 append는 새 파일로 간다.
2. `consuming-*` 파일(이번 것 + 이전 중단분)을 읽어 `refresh_events`에 `INSERT OR IGNORE`(PK `eventId`)하고, 같은 트랜잭션에 처리한 파일 이름을 `pipeline_meta`에 남긴다. 깨진 줄은 건너뛰고 개수를 진단 이벤트로 남긴다.
3. 커밋 뒤 `consuming-*` 파일을 지운다. 2와 3 사이 중단은 다음 실행이 같은 파일을 다시 흡수하고 `eventId`가 겹쳐 중복이 없다.
4. 8 MiB를 넘는 inbox에는 훅이 더 쓰지 않고 `pendingRefresh='busy'` trace만 남긴다(기존 8 MiB 상한 유지). worker가 흡수하면 풀린다.

### 3.3 훅이 읽던 것

SessionStart의 `check_compile_queue`는 큐 jsonl과 `unresolved_pending_refreshes_strict`를 읽는다. 훅은 원장을 열 수 없으므로 결정 필요 A2로 둔다.

## 4. 결정 필요

| ID | 쟁점 | 추천안 | 대안 |
|---|---|---|---|
| **A2** | SessionStart 훅이 compile 할 일을 판단할 근거가 없어진다 | **선택된 compile이 켜져 있으면 SessionStart마다 `laya-compile` wake를 낸다.** wake 큐가 같은 target을 합치고 worker는 할 일이 없으면 원장 조회 한 번으로 끝난다. 훅은 inbox 크기만 `stat`해 trace에 남긴다 | worker가 `.memvane/compile/pipeline-pending` 표시 파일을 유지하고 훅이 `stat`한다(상태가 원장 밖에 하나 더 생긴다) |
| **A3** | `verify-skipped`·`dedup-skipped`는 로그가 아니라 유료 호출 반복을 막는 판정 원장이다. 사용자 결정은 `generated-manifest`만 가져온다 | **두 파일도 `suppressions` 테이블로 가져온다**(멱등 키 `(kind, sourcePath 또는 쌍, bodyHash)`). 안 가져오면 이미 억제된 원문마다 추출+검수 1회, 쌍마다 judge 1회가 다시 든다 | 버리고 재과금 1회 감수 |
| **A4** | 혼합 버전 전용 장치(`legacy_handoff`·`upgrade_queue`·`queue_publication`·`pending_ownership`·`ordinary_slot`의 slot 파일·`queue_return`의 private 반환·`batch_handoff`/`batch_consumer`의 inode batch·`deferred_review`)는 JSONL 큐와 1.x 혼재를 전제로 한다 | **큐가 원장으로 가면 이 장치들을 지운다.** claim·반환·교체 소유는 원장 행 상태와 트랜잭션으로 대체한다. 약 2,700줄과 관련 시험(출시 태그 혼재 시험 포함)이 함께 빠진다 | 어댑터로 유지(죽은 경로가 남는다) |
| **A5** | 같은 원문 revision의 생성→검수 실패→삭제 반복 | 현재 규칙 유지: manifest 마지막 행이 `verify-deleted`면 재생성 가능하되, `verify-skipped` 억제(같은 body hash)가 재추출을 막는다. A3을 택하면 마이그레이션 뒤에도 유지된다. 상태 조합표 R4·R5로 고정 | — |
| **A6** | 조회 CLI 이름 | `entry/run.py wiki-ledger <dedup-pairs|dedup-skip|verify-log|audit> [--json]`(읽기 전용, `errors._query_only_uri`). dedup resolver는 쌍 조회·판정 기록을 이 CLI와 기존 `wiki-dedup-resolve`로 한다 | — |
| **A7** | 새 lock `edit-inbox.jsonl.lock`과 inbox 행 모양은 새 영속 계약이다(작업 지시상 구현 전 승인 대상) | §3 설계대로 승인 요청. 기존 `.handoff/v1/refresh/events.jsonl.lock`을 경로만 바꿔 쓰는 것이다 | 기존 경로 `.handoff/v1/refresh/events.jsonl` 유지(`.handoff` 디렉터리가 inbox 하나 때문에 남는다) |

## 5. 커밋 단위 (A안, 승인 후)

| 커밋 | 범위 | 주요 변경 파일 | focused 검사 |
|---|---|---|---|
| **C4** 원장 확장 | `card_generations`·`suppressions`·`dedup_pairs` 테이블, `verify_jobs` 교체본 blob 열, `audit_events` 로그 종류 정리, 조회 API | `wiki/pipeline/*` | pipeline |
| **C5** inbox | 훅 `record_pending_refresh` → `edit-inbox.jsonl`, worker 흡수(§3.2), SessionStart(A2) | `wiki/card/wiki_freshness.py`, `wiki/intake/wiki_compile_enqueue.py`, `host/host_response.py`, `wiki/compile/wiki_compile_worker.py` | card, intake, host, compile, hermes |
| **C6** refresh | `wiki_freshness`·`refresh_lanes`·`refresh_settlement`·`wiki_discovery`·`manifest_ledger`·`legacy_ledgers`의 refresh 읽기·정산을 `refresh_events`로 | 같은 파일 | card, compile, intake, workspace |
| **C7** 큐 | source/verify 큐 → `compile_jobs`/`verify_jobs`. claim·requeue·반환·discard를 행 상태로. `selected_execution` idle 판정 | `wiki/intake/{source_queue,wiki_compile_enqueue,wiki_backfill}.py`, `wiki/workspace/{queue,queue_paths,queue_return,handoff/api,wiki_selected_laya,compile_paths}.py`, `wiki/compile/{wiki_compile_worker,selected_execution,backfill_work}.py`, `wiki/verify/wiki_verify_worker.py` | intake, workspace, compile, verify |
| **C8** 숨김 파일 제거 | 첫 카드·교체본·후보를 원장 blob으로, 게시는 `publish_card`(교체는 교환), `write_stamp`·`rewrite_generated_card`도 `publish_card` | `wiki/compile/wiki_compile.py`, `wiki/verify/{first_card,promotion,pending_replacement,candidate_stage,verification_stamp,native_review,pending_handoff,completed_review}.py` | verify, compile |
| **C9** 혼합 버전 장치 삭제(A4) | `legacy_handoff`·`upgrade_queue`·`queue_publication`·`pending_ownership`·`ordinary_slot`·`batch_handoff`·`batch_consumer`·`deferred_review`·`queue_return`의 private 반환, `.handoff/v1/staged` | 해당 모듈, `context-map.json`, 출시 태그 혼재 시험 | verify, compile, workspace, runtime |
| **C10** tombstone 제거 + dedup 이름표 이전 | `wiki_compile.py` tombstone 분기·`missing_retirement` 제거, `wiki_dedup_resolve.py` merge가 삭제 전 이름표·sources 이전 | `wiki/compile/wiki_compile.py`, `wiki/verify/promotion.py`, `wiki/dedup/wiki_dedup_resolve.py`, `agents/wiki-dedup-resolver.md` | compile, dedup |
| **C11** 로그·억제·쿨다운 | `verify-*`·`dedup-*`·`candidates`·`merge-needed`·쿨다운을 `audit_events`/`suppressions`/`cooldowns`/`dedup_pairs`로 | `wiki/verify/{wiki_verify_worker,verify_ledger}.py`, `wiki/dedup/*`, `wiki/compile/*`, `wiki/workspace/{queue,compile_paths,atomic_io}.py` | verify, dedup, compile, workspace |
| **C12** 마이그레이션 | `migrate_from_v1` 재작성: `generated-manifest`(+A3) 가져오기 → 마커 → 옛 파일·숨김 파일 삭제. worker 시작 연결과 D5 양보 | `wiki/pipeline/migrate.py`, `wiki/compile/{wiki_compile_worker,selected_execution,wiki_compile}.py` | pipeline, compile |
| **C13** 조회 CLI | `entry/run.py wiki-ledger`, skill·agent 문서 | `entry/run.py`, `memvane/wiki/pipeline/query.py`(신규), `skills/wiki-dedup/SKILL.md`, `skills/logs/*`, `agents/wiki-dedup-resolver.md` | pipeline, `tests/structure/static.test.mjs` |
| **C14** E2E | 3 host 훅 → inbox → worker → 원장 → 게시, 중단/재개, `hold > 1024`, 마이그레이션 E2E(출시 태그 writer 입력) | `tests/e2e/wiki_pipeline_*.py` + `.test.mjs` | 새 e2e, host, hermes |
| **C15** 문서 | pipeline·verify·compile·workspace·card·intake·dedup README, 설계서 §4.2(마이그레이션·tombstone 반영), release.md 개정(§7), working-rules 77·79행(tombstone 언급), `docs/ROADMAP.md` Python 3.11 항목 | docs | doc-links |

C4~C11은 서로 의존한다(C7은 C5·C6, C8은 C7, C9는 C7·C8). 각 커밋 뒤 focused 검사 + `v1_guard --strict`·`--tests --rules G1,G8`·`static`·`test-parts`·`doc-links`, `codegraphcontext update /Users/dulee/work/qmd-auto-context`, 이 문서의 진행 기록 갱신.

## 6. 상태 조합표

### 6.1 게시·hold (① 완료, 회귀 시험 매핑)

| 칸 | 상황 | 기대 결과 | 유료 호출(추가) | 회귀 시험 |
|---|---|---|---|---|
| P1 | 신규, 중단 없음 | `published` | 0 | `test_new_create_replace_cas_conflict_and_fence_abort` |
| P2 | temp 작성 후 txn 커밋 전 중단 | txn 없는 60초 넘은 temp를 지운다 | 0 | `test_orphan_temp_without_txn_is_swept_and_recovery_ignores_st_dev` |
| P3·P4·P5·P6 | rename 전/후 중단, 사람 편집 유무 | roll-forward / `conflict_user_edited` / `published` / `published_user_edited` | 0 | `test_four_crash_recovery_cases_without_st_dev` |
| P7 | 교환 뒤 대조 전 중단 | 밀려난 파일 = expected → `published` | 0 | `test_exchange_crash_recovers_clean_and_restores_an_editor_save` |
| P8 | CAS와 교환 사이 편집기 저장 | 재교환으로 사람 편집 복원 | 0 | 같음 |
| P9 | 새 카드 경로에 사람이 먼저 파일 생성(CAS 전/후) | `conflict_user_edited`, 사람 파일 보존 | 0 | `test_occupied_new_target_same_bytes_and_human_delete_are_never_overwritten`, `test_person_creating_the_path_after_the_cas_read_keeps_their_file` |
| P10 | 같은 초·같은 바이트 재게시 | rename 없이 `published` | 0 | `test_occupied_new_target_...` |
| P11·P12 | 교체 대상 사람 삭제 / rename 뒤 삭제 | 그 게시는 되살리지 않는다(다음 원문 변경 compile은 다시 만든다 — R1) | 0 | 같음 |
| P13 | 재부팅으로 `st_dev` 변화 뒤 복구 | `(st_ino, sha256)`만 사용 | 0 | `test_orphan_temp_...` |
| H1 | hold 1,030건 + 독립 대상 | 정체 없음 | 원래 호출만 | `test_hold_beyond_1024_has_no_scan_limit`, `test_interrupted_create_and_over_1024_valid_holds_recover_and_publish_first_cards` |
| Q1 | 복사본 64 MiB 초과 | `ENOSPC handoff_copy_quota`, 해제 시 회복, 새 카드 자체 control은 면제 | 0 | `test_quota_charges_copies_and_release_frees_them` |

### 6.2 inbox 흡수 (C5)

| 칸 | 상황 | 기대 결과 | 유료 호출 | 회귀 시험(예정) |
|---|---|---|---|---|
| I1 | 훅 append 1행 → worker 흡수 | `refresh_events` 1행, inbox·consuming 파일 없음 | 원래 extract 1 + verify 1 | `test_inbox_absorb_once` |
| I2 | rename 뒤 흡수 트랜잭션 전 중단 | 다음 실행이 `consuming-*`를 흡수, 행 1개 | 0 추가 | `test_inbox_crash_before_commit` |
| I3 | 커밋 뒤 `consuming-*` 삭제 전 중단 | 재흡수해도 `eventId`로 행 1개 | 0 추가 | `test_inbox_crash_after_commit` |
| I4 | 훅 동시 append(3 host 병렬, rename과 경합) | 어떤 행도 잃지 않는다(rename 전 쓴 행은 consuming, 뒤 행은 새 inbox) | 행마다 원래 호출 | `test_inbox_concurrent_append`(프로세스 3개) |
| I5 | 같은 초·같은 바이트 행 2개(다른 `eventId`) | 2행, 같은 원문 revision은 compile job 1개로 접힘 | extract 1 + verify 1 | `test_inbox_same_second_rows` |
| I6 | 깨진 줄 | 건너뛰고 진단 개수 기록, 나머지 흡수 | 0 | `test_inbox_malformed_line` |
| I7 | inbox lock 0.15초 경합 | 훅은 `pendingRefresh='busy'`, exit 0, `laya-compile` wake | 0 | 기존 `test_posttool_enqueue` 갱신 |
| I8 | inbox 8 MiB 초과 | 훅 쓰기 중단 + wake, worker 흡수 뒤 재개 | 0 | `test_inbox_size_cap` |

### 6.3 마이그레이션 (C12, 입력은 출시 태그 writer가 쓴 행)

| 칸 | 상황 | 기대 결과 | 유료 호출 | 회귀 시험(예정) |
|---|---|---|---|---|
| M1 | `v1.1.2` writer가 쓴 `generated-manifest.jsonl` | `card_generations`에 같은 행 수, 옛 파일 삭제 | 0 | `test_migrate_manifest_from_release_tags`(`v1.0.22`·`v1.1.0`·`v1.1.1`·`v1.1.2`) |
| M2 | 가져오기 트랜잭션 커밋 전 중단 | 원장 변화 없음, 옛 파일 그대로, 재실행 결과 = 1회 실행 | 0 | `test_migrate_crash_before_commit` |
| M3 | 마커 커밋 뒤 파일 삭제 전 중단 | 재실행은 삭제만, 행 수 불변 | 0 | `test_migrate_crash_after_marker` |
| M4 | 같은 초·같은 바이트 manifest 행 2개 | 두 행 다 가져온다(행 번호를 키에 포함) | 0 | `test_migrate_duplicate_rows_keep_order` |
| M5 | 큐·claim·refresh·쿨다운·`.handoff/v1/*`·숨김 파일 | 전부 삭제, 원장에 행 없음 | 버린 작업은 원문 변경·full-scan sync가 다시 만든다(원래 호출) | `test_migrate_discards_in_progress_state` |
| M6 | `tombstones.jsonl`·로그성 jsonl | 삭제, 원장 행 없음 | 0 | 같음 |
| M7 | `retired-wiki/`, `wiki-retrieval/` | 보존 | 0 | 같음 |
| M8 | 진행 중 1.x worker | `.compile-run.lock` 경합으로 양보, 다음 wake에 실행 | 0 | `test_migrate_waits_for_run_lock` |
| M9 | route `stateFormat: 2` 뒤 `v1.1.2` worker | 선택 route 거절로 쓰기 없음 | 0 | `test_v112_worker_refuses_v2_route` |
| M10 | 마커 전 wiki-compile skill / wake idle 판정 | skill은 `pipeline_migration_pending` 거절 + wake, idle 판정은 "할 일 남음" | 0 | `test_entrypoints_yield_before_migration` |
| M11 | (A3 채택 시) `verify-skipped`·`dedup-skipped` | `suppressions`로 가져오고 억제 유지 | 0 | `test_migrate_suppressions` |
| M12 | `st_dev` 변화 | 파일 정체성 대조에 쓰지 않음 | 0 | `test_migrate_crash_after_marker`(dev 주입) |

### 6.4 재생성·dedup (C10·C11)

| 칸 | 상황 | 기대 결과 | 유료 호출 | 회귀 시험(예정) |
|---|---|---|---|---|
| R1 | 사람이 카드를 지우고 원문 변경 | 다시 생성 | extract 1 + verify 1 | `test_human_deleted_card_regenerates_on_source_change` (a) |
| R2 | dedup 병합(B→A, 이름표·sources 이전) 뒤 B 원문 변경 | `resolve_target`이 A로 resolve → B 미생성, A 갱신 | extract 1 + verify 1 | `test_merged_card_source_updates_keeper` (b) |
| R3 | 이름표 이전이 다른 카드 C의 이름표와 겹침 | merge CLI가 이전 전에 충돌을 검사해 `alias_conflict`로 거절하고 B를 지우지 않는다. 이미 겹친 상태로 compile되면 `resolve_target`의 기존 ambiguous 처리를 따른다 — C10에서 현 동작을 확인해 이 칸을 확정한다 | 0 | `test_merge_alias_conflict_refuses_delete` |
| R4 | 같은 원문 revision에서 생성 → 검수 inconclusive → 기계 삭제 | `verify-skipped` 억제가 같은 body hash 재추출을 막아 반복 없음 | 1회차만 extract 1 + verify 1, 이후 0 | `test_machine_deleted_same_revision_does_not_loop` |
| R5 | R4 뒤 원문 변경 | 새 body hash → 다시 생성 | extract 1 + verify 1 | 같음 |
| R6 | 원문 삭제 | `card_generations`로 기계 생성 카드 은퇴 | 0 | `test_source_delete_retires_generated_card` (c) |
| R7 | merge CLI 이전 쓰기 실패 | B를 지우지 않고 rejected | 0 | `test_merge_transfer_failure_keeps_loser` |
| R8 | dedup judge 같은 쌍 반복 | `suppressions`(dedup) 억제 | 1회차만 judge 1 | `test_dedup_skip_suppresses_rejudge` |

### 6.5 3 host

| 칸 | 상황 | 기대 결과 | 회귀 시험 |
|---|---|---|---|
| X1 | Claude/Codex PostToolUse `compile`·`index`, SessionStart, Bash `file-change-event` | 훅은 원장을 열지 않는다(시험에서 `sqlite3.connect` 금지 패치), inbox 1행, wake 행 | `memvane/host/tests/run.test.mjs` + E2E |
| X2 | Hermes `index`(`hermes_adapter/core_bridge.py`) | 같은 inbox·wake | hermes 시험 + E2E |

## 7. release.md 개정안 (C15)

- 23행 "카드 frontmatter 필드와 `.memvane/compile/*.jsonl` 행 모양" → "카드 frontmatter 필드, `.memvane/compile/wiki-pipeline.sqlite3`의 application id `0x4D565750`·`user_version`·DDL, `edit-inbox.jsonl` 행 모양(`memvane-edit-inbox-v1`)과 lock 이름"으로 바꾼다.
- 21행 lock 목록에 `edit-inbox.jsonl.lock`을 더한다(A7).
- 28행 계약 밖 파생물에서 `source-refresh-settled.json`을 지우고 `wiki-retrieval/cards.sqlite`를 명시한다.
- 조회 CLI `wiki-ledger`의 출력 키를 공개 인터페이스로 적는다(dedup skill/agent·logs skill이 읽는다).
- compile intent(26행)는 그대로다.

## 8. 테스트 계획

- 단위·통합: §6.2~6.4 각 칸. 중단 지점은 내부 단계 함수를 시험에서 예외로 바꿔 만든다(제품 코드에 시험 스위치 없음).
- 출시 태그 writer 입력: `git archive <tag>`를 임시 디렉터리에 풀어 그 번들의 compile 경로를 fake engine으로 돌려 `generated-manifest.jsonl`(+A3 파일)을 만든다. 손으로 만든 행을 판정 경로에 넣지 않는다.
- 훅 무원장 단정: 3 host 훅 시험에서 `sqlite3.connect`를 패치해 호출되면 실패하게 한다.
- teeth: 새 시험마다 대응 제품 줄을 한 번 깨서 FAIL을 확인하고 §9에 기록한다.
- 시험 개수: 각 커밋 전후 run.test.mjs pass/fail과 Python 시험 수를 기록한다. 혼합 버전 장치 삭제(C9)로 줄어드는 시험은 파일명과 개수를 따로 적는다.
- 전체 `npm test`는 돌리지 않는다(코디네이터가 최종 커밋에서 실행).

## 9. 진행 기록

| 단계 | 커밋 | 결과 |
|---|---|---|
| A. 1차 계획 게이트 | `8fe05f3e` | 추천안 D1~D8 |
| C1 버전 bump | `8958837c` | manifest 7종·`RELEASE` 1.1.4. `plugin-manifest.test.mjs` 5/5, `static.test.mjs` 통과. worker·provider 쌍 변화 없음(`v1.1.2`=HEAD=`PREVIOUS_PAIR`). tier 표의 각 호스트 모델·가격 문서 대조는 이 환경에서 **미확인** |
| 진행 기록 | `8f9be8fc`, `a448a9c4` | 코디네이터 1차 결정, Work 진입점 전수 |
| ① pipeline | `af5c9c3d` | `staged_cards` Work 행, `publish_card` 교환·대조·멱등, `recover_publishes` 교환 복구·고아 temp 정리, migrate는 hold 버림·fence 없음 |
| ① verify Work | `a0fad489` | `Work` SQLite backend, `HOLD_SCAN_LIMIT`·`copy_quota.py` 삭제 |
| A안 재계획 | `5737bdbb` | §1~§8, A2~A7 승인 |
| C4 원장 확장 | `7c105463` | `card_generations`·`suppressions`·`dedup_pairs`·`publish_txns.generation_json`·`verify_jobs.pending_blob`·`compile_jobs.reason`, `decisions.py` mixin(store 800줄 상한), `migrate_from_v1` 재작성(가져오기 3종 + 마커 한 트랜잭션 → 삭제), 출시 태그 writer 드라이버 `tagged_v1_ledgers.py`(`v1.1.0`·`v1.1.1`·`v1.1.2`). pipeline 3/3·Python 18, structure 28/28, v1_guard 0, py3.9 import 통과. teeth: 가져오기 전 삭제(M2)·줄 번호 없는 origin key(M4)·충돌 시 generation 기록·`.first-*` 미삭제(M5) 각각 FAIL 확인. 연결(worker 시작·양보)은 C12 |
| C5 inbox + refresh 원장 | `ec84ef63` | 훅 `record_pending_refresh` → `.memvane/compile/edit-inbox.jsonl`(`memvane-edit-inbox-v1`, `edit-inbox.jsonl.lock` 0.15초, 한 번의 `O_APPEND`, 짧은 쓰기는 `ftruncate`로 원복, 8 MiB `busy`). worker `absorb_edit_inbox`(rename→`INSERT OR IGNORE eventId`+파일명 기록 한 트랜잭션→삭제→원문별 최신만 남김). 미해결 판정은 원장+미흡수 inbox를 `ts` 순으로 접는다. 정산(`source-refresh-settled.json`) → `refresh_events.settled_*`. SessionStart는 매번 `laya-compile` wake(A2), 훅은 원장을 열지 않는다. `refresh_lanes.py`·`quarantine_corrupt_pending`·`compact_pending_refreshes`·`read_pending_refreshes`·CLI 삭제(제품 호출부 0). 1.x 공용/전용 refresh 혼재 시험 삭제: `test_markerless_backfill_refresh.py`(4), `test_source_path_line_separators.py`(2), `test_wiki_freshness`의 공용 원장·quarantine 시험(Python 52→46). 검사: card 9/9(py46)·intake 13/13(py189)·host 24/24(py219)·compile 41/41(py264)·verify 18/18(py191)·workspace 6/6·lifecycle 10/10·dedup 6/6·diagnostics 9/9·sync 16/16, structure 28/28, v1_guard 0, py3.9 import. teeth: 원복 없는 짧은 쓰기·도착순 정렬·`OR IGNORE` 제거·8 MiB 상한 제거·SessionStart wake 제거 각각 FAIL 확인 |
| C7a 큐 API | `5dab68fd` | `queues.py` mixin: `claim_jobs`(queued 전부)·`finish_claim`(남은 행을 claim 시점 순서로 앞에 재적재)·`plan_jobs`(planner 한 트랜잭션)·`append_jobs`·`recover_dead_claims`(4번째 claim에서 `dropped`/`requeue_exhausted`)·`job_blob`. `compile_jobs`/`verify_jobs`에 `reason`. pipeline 3/3(py21), structure 26/26, v1_guard 0. worker·enqueue 연결은 C7b |
| C7b source 큐 → 원장 | `6f253093` | `workspace/ledger_queue.py`: `claim_queue`/`read_queue`가 source 큐에서 원장 claim(`LedgerClaim`)·행(`LedgerRows.finish`)을 돌려준다. 죽은 claim은 다음 claim이 되돌리고, 재시도 한도를 넘은 행은 receipt(`write_discard_receipt`)가 기록된 뒤에만 `dropped`(`requeue_exhausted`, `audit_events` discard 로그)로 바꾸며 기록 실패 시 claim을 남긴다. `intake/source_queue.py`는 `plan_source_batch`를 원장 한 트랜잭션에서 돌리고 교체 행은 원래 자리를 지킨다. reader(`selected_execution._backlog`, `_queue_busy`, worker `_busy_report`, `refresh_settlement.queued_events`)는 원장 행을 센다. `safe_queue_path(native=True)`는 원장 큐에서 공개 경로를 돌려준다. 파일 claim·private 반환 전용 시험 삭제: `test_claim_recovery.py`(15), `test_private_backfill_return.py`(1), `test_stale_backlog_1015`의 batch 3개, `test_revision_skip`의 무잠금 대조 1개(원장 claim이 중복 과금을 막아 전제가 사라짐), `test_source_enqueue`의 파일 이미지 9개, `test_wiki_backfill`의 discard 원장 1개, `test_native_queue_rows`의 source 1개. 검사: compile 39/39(py244), intake 13/13(py179), verify 18/18(py191), lifecycle 10/10(py112), workspace 6/6(py15), host·card·pipeline·dedup·sync·diagnostics·runtime 통과, structure 28/28, v1_guard 0, py3.9 import. teeth: finish 순서·교체 자리·receipt 실패 시 claim 유지·queued_events 원장 읽기 각각 FAIL 확인 |
| C7c 계획 게이트 | `37a8a9cb` | §10.3: 제품 verify는 선택된 route 전용이라 기계 삭제가 닿지 않음을 확인, 행 모델·V1~V21·Q1~Q5·커밋 단위 C8a~C9. 문서만 변경 |
| C8a 원장 verify API | (이 커밋) | `pipeline/verify_queue.py`(`VerifyQueueMixin`): `claim_verify_batch`(더 새 행이 상태 무관하게 있으면 `done/superseded`, 생산 대상 우선·backlog 예약 1건), `record_verify_verdict`(payload `_verdict`), `finish_verify_job`(done/dropped는 blob NULL), `release_verify_claim`, `open_verify_jobs`, `verify_job`. `drop_jobs('verify')`도 blob을 비운다. 시험 전용이던 `store.enqueue_verify_job`·`claim_verify_jobs`·`complete_verify_job`·`requeue_verify_jobs`(제품 호출 0, `rg`) 삭제. `test_pipeline_store` 10→15(옛 verify 단정 1블록 삭제, 새 시험 5: V10·V12·V16·budget·release). pipeline 3/3, structure 28/28, v1_guard 0, py3.9 import. teeth: superseded가 done 행을 무시·backlog 예약 제거·판정 기록 무시·terminal blob 유지 각각 FAIL |

①의 검사 결과(커밋 `a0fad489` 워킹트리):

| 검사 | 결과 |
|---|---|
| `npm run test:focused -- memvane/wiki/verify/tests/run.test.mjs` | 18/18 파일 통과, Python 191(기준 195: `test_handoff_work` 14→10 재작성) |
| `… compile/tests/run.test.mjs` | 43/43, Python 270(기준과 같음, `test_native_retry_recovers_only_beta`는 기준과 같이 skip) |
| `… pipeline/tests/run.test.mjs` | 3/3, Python 11(기준 7) |
| `… runtime/tests/run.test.mjs --test-name-pattern test_runtime_wake_queue` | 1/1 |
| `tests/structure/{static,test-parts,doc-links}.test.mjs` | 28/28 |
| `python3 tests/structure/v1_guard.py --strict`, `--tests --rules G1,G8` | exit 0, 0 위반 |
| `/usr/bin/python3`(3.9.6) import | `handoff_work`·`pipeline.{publish,store,migrate}` 통과 |

teeth(제품 줄을 깨서 FAIL 확인, 확인 후 원복):

| 깨뜨린 줄 | FAIL한 시험 |
|---|---|
| `_settle_exchange` 재교환 제거 | `test_exchange_crash_recovers_clean_and_restores_an_editor_save` |
| `_apply` 같은 바이트 분기 제거 | `test_occupied_new_target_same_bytes_and_human_delete_are_never_overwritten` |
| `recover_publishes`의 고아 temp 정리 제거 | `test_orphan_temp_without_txn_is_swept_and_recovery_ignores_st_dev` |
| `_sha256_file`이 `st_dev`를 정체성에 섞음 | 위 + `test_four_crash_recovery_cases_without_st_dev`, `test_exchange_crash_…` |
| `_apply`의 `FileExistsError` 처리 제거 | `test_person_creating_the_path_after_the_cas_read_keeps_their_file` |
| `list_works`에 `LIMIT 1024` | `test_hold_beyond_1024_has_no_scan_limit`, `test_interrupted_create_and_over_1024_valid_holds_recover_and_publish_first_cards` |
| `_charge`가 항상 0 | `test_quota_charges_copies_and_release_frees_them` |

## 10. 다음 재개 지점

**현재 재개 지점(task_004cb80b546e): C8a 완료, 다음은 C8b+C8c(§10.3).** Q1~Q5는 사용자 승인(msg_7ec3d3939641, 전부 추천안). Q2로 지우는 '검수 실패 시 삭제' 서술은 C15에서 working-rules·README·resolver 지침을 현재 동작에 맞게 고친다.

이전 dispatch(task_4eea2e2522db)는 C7b까지 하고 미완으로 종료했다. 코디네이터가 §10.2 질문(msg_dd37abb859e8)에 답하지 않고 멈춰, 재설계 방향은 승인된 원칙(D1=A·원칙 3·A4) 안에 있다고 보되 규모(제품 약 4,000줄 삭제, 시험 약 30파일 재작성)를 고려해 새 worker가 이어받도록 정했다. 다음 worker는 §10.2를 C7c로 시작하고, 이어서 C10(tombstone 제거·dedup 이름표 이전)→C11(로그·억제·쿨다운·dedup 큐)→C12(마이그레이션 연결: worker 시작·`selected_execution` 양보·wiki-compile skill 거절)→C13(조회 CLI)→C14(E2E)→C15(문서·release.md 개정·working-rules 77·79행·ROADMAP Python 3.11)를 진행한다. §5의 C6 refresh는 C5에 포함됐다. C4의 `migrate_from_v1`은 아직 진입 경로에 연결하지 않았다(C12).

### 10.1 C7(큐)+C9(혼재 장치) 설계 메모

- 순수 계획 함수(`wiki/intake/wiki_source_intent.py`의 `operation_key`·`revision_key`·`queue_revision`·`plan_source_batch`·`merge_source_job`)는 저장소와 무관하므로 그대로 쓴다. 저장만 바꾼다.
- `compile_jobs`에 `op_key`(operation_key의 canonical JSON, 없으면 NULL)와 `revision_token` 열을 더한다. 적재(`_append_unqueued` 대체)는 한 트랜잭션: 같은 `op_key`의 `queued` 행 조회 → `plan_source_batch` → 대체 행 삭제·새 행 삽입. 같은 revision 행이 이미 있으면 넣지 않고 wake만 낸다(현행 `wakePending`).
- worker claim은 `queued` 전부를 `claimed`(pid·token)로 바꿔 `(raw_text, job)` 행과 `finish(remaining_lines)`를 가진 객체로 돌려준다. `remaining`에 원문 그대로 남은 행은 `queued`로, 바뀐 job JSON은 payload를 바꿔 `queued`로, 나머지는 `done`. 죽은 pid의 claim은 시작 시 `recover_dead_compile_claims`(최대 3회 뒤 `dropped`, 사유 `requeue_exhausted` — `discard-ledger.jsonl` 대체)로 되돌린다.
- verify 큐도 같은 모양(`verify_jobs`, `target_path` 색인). 교체본은 `pending_blob`.
- 이로써 claimed 파일·private 반환(`queue_return`)·inode batch(`batch_handoff`·`batch_consumer`)·`upgrade_queue`·`queue_publication`·`legacy_handoff`·`ordinary_slot`의 slot 파일·`pending_ownership`·`deferred_review`가 필요 없어진다(A4). 삭제 전후 시험 개수를 §9에 적는다.
- reader: `selected_execution._backlog/_idle_locked`, `refresh_settlement.queued_events`, worker `_busy_report`. writer: `wiki_compile_enqueue`(비훅 경로), `recover_pending_refreshes`, `wiki_backfill`, `wiki_discovery.drain_after_sync`(sync owner), `wiki_dedup_*`의 requeue.

### 10.2 verify 쪽(C7c·C8·C9) 재설계 제안 — 코디네이터 확인 대기

조사 결과 verify 큐는 단순 저장소 교체로 끝나지 않는다. 현재 `wiki_verify_worker.run`은 `upgrade_queue.prepare`(1.x 행·교체본 흡수, `public_ready`), `ordinary_slot`(slot 파일 소유), `queue_batch_prepare.delivery`(source claim inode batch 패킷), `native_review`(hold 검수·회전), `completed_review`, `first_card.recover`, `_pendingCleanup`/`_return_cleanup`(숨김 교체본 정리 영수증)을 엮은 혼합 버전 상태 기계다. 공용 `verify-queue.jsonl`에는 1.1.4 writer가 거의 쓰지 않고(첫 카드·교체본은 `Work` hold), 1.x 행을 받는 입구 역할만 남아 있다.

제안(1.x가 route로 배제된 세계 기준):
- verify 작업 = `verify_jobs` 행 하나(`target_path`, job payload, 교체본·첫 카드 이미지는 `pending_blob`). `.first-*`·`.<card>.pending-*`·`.pc-*`·`.pending-ownership.json`·`.verify-swap-*`와 `Work` hold(`native-review`·`deferred-replacement`·`manual-edit`·`base`)를 이 행 상태(`queued`·`claimed`·`done`·`dropped`)로 대체한다.
- worker: claim → (사람 편집·원문 변경 재확인) → 유료 검수 1회 → `publish_card`(첫 카드 `rename_noreplace`, 교체·stamp는 교환+대조, `generation=` manifest 행) → finish. 같은 대상의 중복 행은 claim 시 대상별 최신만 남긴다(현행 `_dedup_by_target`).
- 삭제: `upgrade_queue`·`legacy_handoff`·`queue_publication`·`ordinary_slot`·`batch_handoff`·`batch_consumer`·`queue_batch_prepare`·`deferred_review`·`pending_ownership`·`native_review`·`completed_review`·`candidate_stage`·`pending_replacement`의 숨김 파일 경로, `workspace/queue_return.py`·`handoff/api.py`의 반환·배달, `verify/handoff_work.py`의 `Work`(대체 후 미사용).
- 규모: 제품 약 4,000줄 삭제·약 500줄 신규, verify 18개·compile 약 12개 시험 파일 재작성. 상태 조합표는 §6.1 P칸(게시)과 새 V칸(검수 중 사람 편집·원문 변경·같은 대상 중복·claim 중단·검수 실패 삭제 R4/R5)으로 다시 쓴다.
- 사용자 승인: 이 방향(행 하나, 숨김 파일·`Work` hold를 행 상태로, claim→재확인→유료 검수 1회→`publish_card`→finish, 혼합 버전 장치 삭제). 세부는 §10.3.

### 10.3 C7c 계획 게이트 — verify 행 모델·V칸·커밋 단위 (task_004cb80b546e, Q1~Q5 추천안 승인)

#### 조사로 확인한 사실 (HEAD `fa12f578`)

- **제품에서 verify는 선택된 route에서만 돈다.** verify `run`의 유일한 제품 호출부는 `wiki_compile_worker.py:1519`이고, compile worker `main`은 `selected_route is None`이면 `QMD_SETUP_GUARD_FIXTURE=1`(시험) 밖에서 바로 끝난다(`wiki_compile_worker.py:1619-1622`, `:1646-1647`). `wiki_verify_worker.main`은 `entry/`·skill 어디서도 부르지 않는다(`rg`).
- **선택된 route는 기계 삭제를 하지 않는다.** `run`이 `onFail`·`onInconclusive`를 `none`으로 덮는다(`wiki_verify_worker.py:1543-1546`, `settings/types.py:253` 주석). 따라서 제품에서 `apply_negative_verdict`의 `delete` 분기, `record_verify_deletion`(`verify-deleted.jsonl`), `record_verify_suppression`(`verify-skipped.jsonl` 쓰기), `promotion.retire`·`finish_retirement`·`missing_retirement`의 verify 쪽, manifest `verify-deleted` 행 쓰기는 시험 fixture에서만 닿는다. 부정 판정의 제품 동작은 "첫 카드는 `generated`로 남고, 교체본은 버리고 옛 카드를 유지"다.
- 부정 판정으로 교체본을 버린 뒤 같은 revision은 `wiki_lifecycle`의 `REFRESH_RETRY_SECONDS`(7일) 뒤에만 다시 들어간다. `pending_source_review`는 큐·claim·대기 hold에 **남아 있는** 행만 본다(`pending_replacement.py:70-147`). 첫 카드는 `already_compiled_revision`(`wiki_compile_worker.py:700-711`)이 같은 revision 재추출을 막는다.
- 첫 카드는 지금 검수 전에 `generated`로 노출된다(`first_card._resume` → rename 후 manifest 행, `wiki_compile.py:1337-1338` 주석). 교체는 검수 통과까지 옛 카드가 남는다.
- `publish_card`는 `generation=`을 `published`·`published_user_edited`일 때만 `card_generations`에 넣는다(`store.finish_publish_txn`). `claim_jobs`는 queued 전부를 claim하고 `finish_claim`은 claim 행을 지운 뒤 남은 것을 다시 넣는다(행별 종결 상태를 남기지 않는다).

#### 호출부 (CodeGraphContext `execute_cypher_query` CALLS, repo `/Users/dulee/work/qmd-auto-context`, + `rg`)

| 기호 | 제품 호출부 | 시험 호출부 |
|---|---|---|
| `verify.api.create_first_card`·`enqueue_replacement`·`stage_candidate`·`pending_name_reserved`·`missing_retirement` | 각 1: `wiki/compile/wiki_compile.py` | 0 |
| `verify.api.pending_source_review` | `wiki/compile/wiki_compile_worker.py` | 0 |
| `verify.api.claim_source_queue` | `wiki/compile/wiki_compile_worker.py` | 0 |
| `verify.api.queue_batch_pending` | `wiki/compile/{selected_execution,wiki_compile_worker}.py` | 0 |
| `verify.api.native_review_pending`·`prepare_queue` | `wiki/compile/selected_execution.py` | `prepare_queue` 7: compile `test_pending_handoff`·`test_tagged_queue_controls`·`tagged_queue_driver`·`publication_child`, verify `test_verify_batch_prepare` |
| `process_verify_job` | `wiki_verify_worker.py` 2 | 9: verify `test_wiki_provenance_intent`·`test_verify_pending`·`test_promotion`·`test_negative_suppression_retry`·`test_excluded_selected_sources`, `tests/e2e/wiki_db_provenance.py` |
| `apply_verify_verdict_locked` | `wiki_verify_worker.py` | 2: `test_wiki_provenance_intent` |
| `record_verify_deletion`·`record_verify_suppression` | `wiki_verify_worker.py`, `pending_ownership.py` | `test_wiki_verify_worker`, `pipeline/tests/tagged_v1_ledgers.py`(출시 태그 writer 드라이버 — 태그 번들의 함수라 영향 없음) |
| `stamp_verification` | `wiki_verify_worker.py`, `verify/api.py` | 5: compile `test_checkout_fingerprints`·`test_revision_skip`, card `test_verification_stamp`, workspace `test_wiki_mutation_lock`, verify `test_verify_pending` |
| 모듈 import(`rg`): `upgrade_queue`·`legacy_handoff`·`queue_publication`·`ordinary_slot`·`batch_handoff`·`batch_consumer`·`queue_batch_prepare`·`deferred_review`·`pending_ownership`·`native_review`·`completed_review`·`candidate_stage`·`pending_replacement`·`pending_handoff`·`first_card`·`handoff_work` | verify 도메인 안끼리 + `verify/api.py`, `tests/structure/context-map.json`. 밖에서는 `workspace/queue_return.py`를 `workspace/{queue_paths,queue,wiki_selected_laya}.py`·`handoff/api.py`가 쓴다 | verify 시험 18파일, compile 시험 약 20파일(아래 표) |

#### 행 모델

- `verify_jobs` 행 하나 = 검수 작업 하나. `payload_json`의 `kind`: `review`(이미 보이는 `generated` 첫 카드를 검수·stamp) 또는 `replace`(교체본 `pending_blob`, `expectedTargetHash` = 교체 대상 live 카드 SHA). DDL은 바꾸지 않는다(필요한 값은 payload에 둔다).
- **첫 카드(추천 Q1-a)**: compile이 `verify_jobs`에 `review` 행을 먼저 넣고, `publish_card(expected=None, generation=manifest 행)`으로 `generated` 카드를 곧바로 게시한다. 현재처럼 검수 전에도 보인다. 행만 있고 게시 전에 죽으면 worker는 `card_missing`으로 끝내고, claim이 남은 compile job은 dead claim 회수로 다시 compile된다.
- **교체**: compile은 `replace` 행(+`pending_blob`)만 넣는다. 옛 카드는 통과 게시 전까지 그대로다. 숨김 `.<card>.pending-*`·`.pc-*`·`.first-*` 쓰기는 없다.
- **worker 한 행**: `recover_publishes` → `recover_dead_claims` → claim → 대상별 최신만 남김 → (재확인: live SHA = 기대값, 원문 revision이 현재와 같고 미해결 refresh 없음) → 유료 검수 1회 → **판정을 행 payload에 기록(추천 Q4)** → pass면 stamp 바이트를 렌더해 `publish_card(expected=검수한 바이트의 SHA)`(교환+대조), compile intent는 게시 전 기록·게시 실패 시 `.superseded` → 행 `done`. 예산(`maxPerRun`·`VERIFY_PRODUCED_HARD_CAP`·fresh 우선·backlog 예약 1건), 엔진 계획·식힘·`defer_or_drop`·`pass` 근거 규칙·`skipPaths` 거절은 그대로 옮긴다.
- 종결 상태: `done`(+`reason`: `verified`·`kept`·`card_missing`·`changed_before_verify`·`changed_during_verify`·`stale_revision`·`superseded`·`invalid_verdict` 등), `queued`(식힘·127·timeout·락 경합으로 보존), `dropped`(`requeue_exhausted`). 행을 지우지 않고 `done`으로 남긴다(같은 대상 판정과 조회 CLI용). 오래된 `done` 행 정리는 C11에서 refresh와 같은 방식으로 한다.

#### V칸 (선택된 route 기준, 유료 = extract/verify 호출 수)

| 칸 | 상황 | 기대 결과 | 유료 | 회귀 시험(예정, verify 또는 compile) |
|---|---|---|---|---|
| V1 | 첫 카드, 중단 없음, pass | `generated` 게시 → `verified` stamp 교환 게시, `card_generations` 1행 | extract 1 + verify 1 | `test_first_card_review_publishes_stamp` |
| V2 | 첫 카드, fail/inconclusive | `generated` 유지, 행 `done/kept`. 같은 revision은 `already_compiled_revision`으로 재추출 없음(R4 대체) | 1회차만 extract 1 + verify 1, 이후 0 | `test_negative_first_card_does_not_loop` |
| V3 | 교체, pass | 통과 전 옛 카드 유지, 교환 게시, manifest `updated` 1행 | extract 1 + verify 1 | `test_replace_pass_exchanges_card` |
| V4 | 교체, fail/inconclusive | 옛 카드 유지, blob 버림(`pending_blob` NULL), 숨김 파일 0. 같은 revision은 7일 refresh 재시도만 | 1회차 extract 1 + verify 1 | `test_negative_replace_keeps_old_card` |
| V5 | claim 전 사람 편집(live SHA ≠ 기대값) | 유료 호출 없이 `done/changed_before_verify`, 사람 파일 그대로 | 0 | `test_human_edit_before_verify_skips_paid_call` |
| V6 | 검수 중 사람 편집(게시 CAS 실패) | `conflict_user_edited`, 사람 파일 보존, `done/changed_during_verify`, 같은 행 재검수 없음 | verify 1(이미 쓴 것) | `test_human_edit_during_verify_keeps_edit` |
| V7 | 교환 창의 편집기 저장 | P8과 같음(재교환 복원) | 0 추가 | 기존 `test_exchange_crash_recovers_clean_...` |
| V8 | claim 전 원문 변경(revision 불일치·미해결 refresh) | 유료 호출 없이 `done/stale_revision`. 새 revision은 refresh 원장의 compile이 새 행을 만든다(추천 Q3) | 0 | `test_source_change_before_verify_drops_without_paid_call` |
| V9 | 검수 중 원문 변경 | 게시하지 않고 `done/stale_revision`. 새 revision은 V8과 같은 경로 | verify 1(이미 쓴 것) | `test_source_change_during_verify_does_not_publish` |
| V10 | 같은 대상 행 2개(다른 revision) | 최신만 검수, 옛 행 `done/superseded`. dead claim에서 되돌아온 옛 행도 더 새 행(상태 무관)이 있으면 superseded | 최신 1건만 verify 1 | `test_same_target_latest_wins_across_requeue` |
| V11 | claim 뒤 유료 호출 전 중단 | 다음 run의 `recover_dead_claims`가 되돌려 다시 처리 | verify 1(총) | `test_dead_claim_before_verdict_requeues` |
| V12 | 판정 기록 뒤 게시 전 중단 | 다음 run이 기록된 판정을 재사용해 게시, 추가 유료 0 | verify 1(총) | `test_recorded_verdict_is_reused_after_crash` |
| V13 | 유료 호출 뒤 판정 기록 전 중단 | 한 번 더 검수(attempt ≤3, 넘으면 `dropped/requeue_exhausted`) | 최대 verify 3 | `test_dead_claim_attempt_limit` |
| V14 | 게시 중 중단(temp·rename·교환) | P2~P8 복구 뒤 V12 경로로 같은 바이트면 `published`(P10) | 0 추가 | `test_publish_crash_then_recorded_verdict_settles` |
| V15 | 원문 변경 뒤 재생성(R5) | 새 revision → 새 `replace` 행 | extract 1 + verify 1 | `test_new_revision_after_negative_regenerates` |
| V16 | queued 1,030행 + 독립 대상 | 스캔 상한 없음, 예산만큼 처리, 남은 행 `queued` | 예산만큼 | `test_verify_backlog_beyond_1024` |
| V17 | 127·timeout·식힘·락 경합 | 행 `queued` 유지, 판정 없음 | 기존 규칙 | 기존 `test_wiki_verify_worker` 이전 |
| V18 | `invalid_verdict` 후보 소진 | `done/invalid_verdict`, 카드 그대로(교체 blob 버림) | 기존 규칙 | 기존 `test_verdict_rules` 이전 |
| V19 | `verify.enabled: false` | 첫 카드는 행 없이 `generated` 게시, 교체는 현행대로 `merge-needed`(`verified_replacement_requires_review`) | extract 1 | 기존 compile 시험 |
| V20 | compile 중 같은 revision의 `replace` 행이 queued/claimed, live = 기대값 | compile은 `awaiting_pending_verification`으로 재추출하지 않음. claimed 행이 이 원문을 인용하면 compile job 보존 | 0 | `test_compile_waits_for_queued_replace_row` |
| V21 | `st_dev` 변화 | 행·게시 대조에 `st_dev`를 쓰지 않음(P13) | 0 | P13 시험 |

#### 결정 요청 (전부 승인된 방향 안의 세부)

| ID | 쟁점 | 추천안 | 대안 |
|---|---|---|---|
| Q1 | 첫 카드 노출 시점 | **a. 현재처럼 compile이 `generated`로 바로 게시하고 `review` 행이 stamp를 교환 게시한다.** 검수 엔진이 없거나 식힘이어도 카드가 보인다(`recallVerifiedOnly: false` 사용자 동작 유지) | b. 검수 뒤에만 게시(§10.2 글자 그대로). 검수가 미뤄지면 카드가 보이지 않는다 |
| Q2 | 기계 삭제 경로 | **제품에서 닿지 않으므로 지운다.** verify는 카드를 지우지 않는다: `apply_negative_verdict`의 `delete` 분기, `record_verify_deletion`, `record_verify_suppression`, `promotion.retire`·`finish_retirement`·`retirement_status`, manifest `verify-deleted` 쓰기, `verify-deleted.jsonl`·`verify-skipped.jsonl` 쓰기. `onFail`·`onInconclusive` 설정 키는 허용 목록에 남기고 무시한다(문서 C15). compile은 C4에서 가져온 `suppressions(kind='verify')`를 계속 읽는다(A3) | 시험 fixture용으로 유지(원장 이관 비용 + 죽은 경로) |
| Q3 | 원문이 바뀐 행 | **`done/stale_revision`으로 끝낸다(V8·V9).** 새 revision은 refresh 원장과 compile이 새 행으로 다시 낸다. 현재는 보존 후 재시도라 같은 낡은 행이 run마다 다시 읽힌다 | 현재처럼 보존 |
| Q4 | 판정 뒤 게시 전 중단 | **판정을 행 payload에 기록하고 재사용한다(V12, 추가 유료 0).** DDL 변경 없음 | 재검수(+1 유료) |
| Q5 | `staged_cards`·`Work`·64 MiB 쿼터 | **C9에서 `handoff_work.py`·`store`의 work API·quota와 `staged_cards` DDL을 지운다.** `wiki-pipeline.sqlite3`는 미출시라 `user_version` 1 그대로 둔다. `publish_card`의 `stage_id` 인자도 지운다 | 테이블만 남겨 둔다 |

#### 커밋 단위

| 커밋 | 범위 | focused 검사 |
|---|---|---|
| **C8a** 원장 verify API | `queues.py`: 행별 `finish_verify_job(job_id, token, state, reason)`, `record_verdict(job_id, token, verdict)`, 대상별 최신 선택(상태 무관 더 새 행이면 superseded), compile용 `verify_rows_for_source(revision)`. pipeline 단위 시험 | pipeline |
| **C8b** compile 생산자 | `wiki_compile.py`: 첫 카드 = `review` 행 + `publish_card(generation=)`, 교체 = `replace` 행 + blob. `write_pending_card`·`replacement_image`의 숨김 이름·`pending_name_reserved` 제거. `wiki_compile_worker`: `pending_source_review` → 원장 조회(V20), `claim_source_queue`의 `queue_batch_prepare` 호출 제거. `selected_execution`의 `prepare_queue`·`native_review_pending`·`queue_batch_pending` → 원장 개수 | compile, intake |
| **C8c** verify worker 재작성 | `wiki_verify_worker.run`·`process_verify_job`을 행 모델로. 부정 판정은 `kept`(Q2). V1~V21 시험. 옛 메커니즘 시험은 같은 커밋에서 삭제·재작성하고 파일별 전후 개수를 §9에 적는다 | verify, compile, card, workspace |
| **C9** 장치 삭제 | 위 16개 verify 모듈, `promotion.py`의 retire·stamp intent 폴더(남는 것이 있으면 `verification_stamp`로), `workspace/queue_return.py`, `handoff/api.py`의 반환·배달, `handoff_work.py`, `staged_cards`(Q5), `context-map.json`, verify README | verify, compile, workspace, pipeline, runtime |

재작성·삭제 대상 시험(전 개수): verify `test_candidate_stage`(5)·`test_first_card`(9)·`test_incomplete_stamp`(6)·`test_legacy_handoff`(7)·`test_native_batch_delivery`(1)·`test_native_occurrence_review`(3)·`test_negative_suppression_retry`(2)·`test_pending_cleanup_recovery`(22)·`test_promotion`(18)·`test_verify_batch_prepare`(7)·`test_verify_pending`(17)·`test_wiki_provenance_intent`(20)·`test_wiki_verify_worker`(44)·`test_batch_handoff`(5)·`test_handoff_work`(10), compile `test_bounded_verify_handoff`(3)·`test_first_card_lock`(1)·`test_ordinary_sibling_recovery`(2)·`test_pending_handoff`(15)·`test_tagged_editor_controls`(1)·`test_candidate_reservations`(4)·`test_write_identity`(8)·`test_publication_recovery`(1)·`test_machine_deletion_recovery`(16)·`test_upgrade_queue`(3)·`test_writer_lifecycle`(6)·`test_visible_card_handoff`(2)·`test_legacy_transition_race`(1)·`test_mixed_ledger_first_review`(2)·`test_revision_skip`(28, 일부)·`test_legacy_online_preservation`(1)·`test_tagged_queue_controls`(7)·`test_manifest_scan`(5, 일부)·`test_summary_history`(2, 일부)·`test_legacy_ledgers`(1). 판정 시험 입력은 현재 compile writer가 낸 행이다. 출시 태그 writer 입력은 마이그레이션(C12) 칸에만 해당한다(1.x verify 행은 버린다, §1).

C8b·C8c는 시험이 서로 물려 한 커밋으로 합칠 수 있다. 그 경우에도 V칸 시험과 teeth를 커밋 안에서 칸별로 기록한다.

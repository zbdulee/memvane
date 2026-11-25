# 위키 파이프라인 v2 구조 개편 설계

**상태**: 3차 개정 (독립 설계 리뷰 r2 차단 1건 `R1` 및 비차단 7건 반영, 코드 변경 전 설계 단계, 2026-11-25)
**관련 문서**: [진행 상황](dev/progress-1.1.2.md), [1.x 영속 계약](dev/contexts/release.md), [compile README](../memvane/wiki/compile/README.md), [verify README](../memvane/wiki/verify/README.md), [host README](../memvane/host/README.md)

---

## 1. 배경과 목표

1-B-2(`discovery lock` 경합)와 1-E(`hold` 고아 디렉터리·`handoff_scan_limit` 64건 정체)에서 드러났듯, 현재 위키 파이프라인 결함은 **상태가 여러 디렉터리·파일(`compile/*.jsonl`, `.handoff/v1/*`, `.first-*`, `.*.pending-*`, `provenance-intents/`)에 분산**되고 **일부 쓰기가 훅 프로세스 안에서 직접 실행**되는 구조에서 반복된다. 반면 `sync`는 단일 SQLite store와 트랜잭션, 세대 단위 게시를 사용해 같은 부류의 잔재 결함이 구조적으로 발생하지 않는다.

목표는 다음 네 가지다.

1. **훅 경량화 (lock 경합 제거 + 단일 편집 운반체)**: 훅([host_response.py](../memvane/host/host_response.py))에서 `sync_owner`·`source-queue.jsonl.lock`·`card_write_lock` 획득과 `_compact`를 제거한다. 편집이 지닌 두 가지 의미 계약—① **freshness 무효화 fence**, ② **명시적 편집 동의 + 호스트 귀속**—은 기존 `record_pending_refresh`(`.handoff/v1/refresh/events.jsonl`, `0.15s` non-blocking `.lock` 상한)를 **단일 편집 운반체**로 삼아 훅에서 가볍게 기록하고 wake(`laya-compile`·`laya-sync`)만 남긴다. 별도 `edit-hints` 파일이나 새 락은 만들지 않는다.
2. **오프훅 job 생성 단일화**:
   - **편집 기원 (`post_tool_source`)**: `wiki_compile_worker.recover_pending_refreshes`([wiki_compile_worker.py](../memvane/wiki/compile/wiki_compile_worker.py#L1460-L1497))가 미정산 `(sourcePath, eventId)` pending refresh를 읽어 `source-queue`에 job을 생성하는 **단일 생산자**다.
   - **비편집·외부 변경 기원 (`post_sync_source` / `session_start_source`)**: `laya-sync` owner(`product_sync_worker` → `external_edits.drain` → `wiki_discovery.drain_after_sync`)가 단일 생산자다. unresolved pending refresh가 있는 경로(settle 여부 무관, 삭제 원문 제외)는 `wiki_discovery.drain_after_sync`가 커서 스냅샷만 갱신하고 중복 `post_sync_source` 적재를 건너뛴다(`unresolved_pending_refreshes_strict`가 `None`이면 안전하게 건너뛰지 않고 적재).
3. **별도 lease의 wiki worker + SQLite 단일 원장**: compile·verify·카드 게시는 기존 `laya-compile` wake kind([1.x 영속 계약](dev/contexts/release.md#L14) 4종 유지)와 별도 `.compile-run.lock` lease를 쓰는 `wiki` worker가 수행해 `sync`의 300초 지연 목표와 분리한다. Phase 2에서는 파이프라인 상태(`hold`·`pending`·`first-card`·큐·반환·감사 원장·쿨다운)를 단일 SQLite 트랜잭션 원장으로 통합하고, 디렉터리 스캔 상한(`HOLD_SCAN_LIMIT`) 병목을 제거한다.
4. **단일 rename 게시**: 사람이 읽고 편집하는 `.memvane/wiki/**/*.md`는 파일로 유지한다. 카드 게시는 동일 디렉터리 임시 파일(`.publish-<txid>.tmp`) 준비 → SQLite 커밋 → CAS(`expectedTargetHash` under `.card-write.lock` → `.topical-publish.lock`) → 단일 rename 순서로 진행하며, 중단 시 roll-forward / roll-back / `published(user_edited)`가 결정적으로 복구한다.

---

## 2. 현재 경계와 상태 파일 전수표

### 2.1 훅 vs 오프훅 워커 현재 경계

- **Wake kind 집합**: 계약상 4종(`laya-sync`, `laya-compile`, `learning`, `auto` — [1.x 영속 계약](dev/contexts/release.md#L14)). 선택 route 디스패처([runtime_dispatch.py](../memvane/app/runtime_dispatch.py#L20))는 `laya-sync`, `laya-compile` 2종을 처리한다.
- **훅 액션별 현재 쓰기** ([host_response.py](../memvane/host/host_response.py), [host_rules.py](../memvane/host/host_rules.py#L23-L31), [core_bridge.py](../hermes_adapter/core_bridge.py#L344-L358)):
  - `update` / `update-queued` (SessionStart): `wiki_discovery.record_grant(..., 'session_start')`, `_request_wiki_lifecycle`, `check_compile_queue`(`unresolved_pending_refreshes_strict` + `source-queue`/`verify-queue` 크기 검사 → `laya-compile` wake), `laya-sync` wake.
  - `file-change-event` (Claude PostToolUse Bash): `wiki_discovery.record_grant(..., 'sync')`, `_request_wiki_lifecycle`, `laya-sync` wake (`check_compile_queue` 없음).
  - `index` (Claude/Codex/Hermes PostToolUse Edit): discovery grant 없음(`host_rules.py:24`), `check_compile_queue` 없음. 프로젝트 밖 편집이면 `external_edits.notify`로 수신 프로젝트의 `external-edits-<scope>-<incarnation>.json` inbox에 기록 후 해당 프로젝트의 `laya-sync` wake(`host_response.py:1189-1202`). 내부 편집이면 `_request_selected_sync`(`_request_wiki_lifecycle` + `laya-sync` wake).
  - `compile` (Claude/Codex PostToolUse Edit; **Hermes는 호출하지 않음**): `_verified_edit` + `verify_selected_route` 검증 후 `sync_owner` + `owner_scope`를 직접 잡아 `wiki_compile_enqueue.enqueue_payload`(`record_pending_refresh` + `source-queue.jsonl` append + `laya-compile` wake) 실행. `owner_busy` 시 `wiki_discovery.record_grant(..., 'sync', engine=host)` + `_accept_wiki_grant`로 폴백.

### 2.2 상태 파일·잠금 전수 (Writer / Reader / Lock / 1.x 계약)

| 분류 | 경로 / 패턴 | 주요 Writer | 주요 Reader | Lock / 순서 | 1.x 영속 계약 여부 |
|---|---|---|---|---|---|
| **위키 게시 락** | `.topical-publish.lock` | `wiki_mutation_lock`, setup | 모든 카드 writer, setup | `card/ledger lock -> .topical-publish.lock` (setup은 `install journal lock -> .topical-publish.lock`) | **계약** (`release.md:13,21`) |
| **공용 큐 & Claim** | `.memvane/compile/{source,verify}-queue.jsonl`, `<queue>.claimed.<pid>.<token>` (상한 64) | `wiki_compile_enqueue`, `queue_return`, `queue_paths.requeue_lines`, `upgrade_queue`, `pending_replacement` | `wiki_{compile,verify}_worker`, `host_response`, `selected_execution`, `queue_batch_prepare` | `*.jsonl.lock` (`flock`) | **계약** (행 모양, 출시 worker 공유) |
| **원장 (감사/결과)** | `.memvane/compile/{generated-manifest,tombstones,candidates,merge-needed,discard-ledger}.jsonl`, `generated-manifest.jsonl.compact-stamp` | `wiki_compile_worker`, `wiki_compile`, `first_card`, `promotion`, `backfill_work`, `manifest_ledger` | `wiki_freshness`, `wiki_lifecycle`, `manifest_scan`, `wiki_verify_worker` | `.card-write.lock` / queue lock | **계약** (`.memvane/compile/*.jsonl`) |
| **검수/중복 원장** | `.memvane/compile/{verify-log,verify-deleted,verify-skipped,dedup-needed,dedup-skipped,dedup-deleted}.jsonl` | `wiki_verify_worker`, `verify_ledger`, `wiki_dedup_*` | `wiki_compile_worker`(억제 마커), `wiki_dedup_resolve`, 진단 | file append + `.card-write.lock` / sidecar | **계약** (`.memvane/compile/*.jsonl`) |
| **쿨다운 & 실행 락** | `.memvane/compile/{cooldown,verify-cooldown,verify-engine-cooldown.json,dedup-judge-*}`, `.card-write.lock`, `.compile-run.lock` | `wiki_compile_worker`, `wiki_verify_worker`, `wiki_dedup_*`, `compile_paths` | 각 worker | sidecar `flock` / `O_NOFOLLOW` | 운영 락 / 파생 상태 |
| **Refresh 원장** | `.memvane/compile/source-refresh-pending.jsonl` (공용) / `.handoff/v1/refresh/events.jsonl` (private) | `wiki_freshness`, `refresh_lanes.append_private`, `first_card` | `wiki_freshness`, `host_response`, `wiki_compile_worker`, `refresh_settlement`, `wiki_discovery` (discovery lock 아래 unlocked strict 읽기) | `source-refresh-pending.jsonl.lock` | 공용: **계약** / private: 1.1.1+ |
| **Refresh 종결 캐시** | `.memvane/compile/source-refresh-settled.json` + `.lock` | `refresh_settlement` | `refresh_settlement` (compile 쪽만) | sidecar `.lock` + 원자적 replace | **계약 밖 파생물** (`release.md:28`) |
| **Handoff 작업** | `.memvane/compile/.handoff/v1/{hold,work,closed}/<hex32>/{.w-*,raw,image,work}`, `copy-quota/` + `allocated-<id>` (64 MiB), `publication/`, `ownership-<hex24>`, `review-turn-<hex24>` | `handoff_work.Work`, `copy_quota`, `first_card`, `promotion`, `deferred_review`, `native_review`, `queue_publication`, `legacy_handoff` | `first_card.recover`, `queue_batch_prepare`, `upgrade_queue`, `pending_handoff`, `completed_review` | `verify-queue.jsonl.lock` / dirfd `O_EXCL` | 1.1.1+ private (구버전 비노출) |
| **Handoff 반환/기타** | `.memvane/compile/.handoff/v1/returns/<sha24>/{native.claimed.*,ready-*,offer-*,fragment-*}`, `backfill/`, `manifest-scan/` | `queue_return`, `handoff.api`, `batch_handoff`, `ordinary_slot`, `backfill_work`, `manifest_scan` | `selected_execution`, `queue_batch_prepare`, `ordinary_slot`, `backfill_work`, `manifest_scan` | `*.jsonl.lock` / `.compile-run.lock` | 1.1.1+ private |
| **위키 임시/대기/카드** | `.memvane/wiki/**/{.first-<hex32>,.<card>.pending-<hex16>,.pending-ownership.json,.verify-swap-*,*.md}` | `first_card`, `wiki_compile`, `pending_replacement`, `promotion`, `legacy_handoff`, `verification_stamp`, `wiki_source_retire` | `memvane-db` sync, `recall`, `wiki_verify_worker`, `wiki_lifecycle`, 사용자 | `.card-write.lock` -> `.topical-publish.lock` | `.md`: 사용자 산출물 / 임시·대기: 내부 상태 |
| **퇴역 아카이브 & FTS** | `.memvane/compile/retired-wiki/`, `.memvane/compile/wiki-retrieval/cards.sqlite` | `archive`, `promotion`, `wiki_lifecycle` / `wiki_compile_retrieval` | 사용자 복구 / `wiki_compile_retrieval`, `wiki_dedup_*` | archive dir / retrieval sqlite | `retired-wiki`: 복구 보관 / `cards.sqlite`: 일회성 FTS 오버레이 |
| **Compile Intent** | `<install state>/provenance-intents/compile-<id>.json` (`.consumed`/`.rejected`/`.superseded`), `provenance-intents.cursor` | `wiki_verify_worker`, `verification_stamp`, `intent_ack` | `sync/compile_intents.consume` (`INTENT_SCAN_LIMIT=512`) | `O_NOFOLLOW` + atomic rename | **계약** (`release.md:26`, 호스트 간 공유) |
| **Discovery & Inbox** | `<install state>/...wiki-discovery*` (cursor+grants), `external-edits-<scope>-<incarnation>.json` + `.lock`, `<sync state>/<key>-wiki-lifecycle-{request,served}.json` | `host_response`, `wiki_discovery`, `external_edits`, `lifecycle_requests` | `product_sync_worker`, `wiki_discovery`, `wiki_lifecycle` | discovery lock (`DiscoveryBusy`) / inbox `.lock` (0.15s) | discovery·inbox: **1.x 공유 fail-closed 상태** / lifecycle req: 설치 상태 |

---

## 3. 상태 조합표 (출시 태그·중단·경합·기대 결과·유료 호출 수)

| 시나리오 / 축 (`1.0.22`~`1.1.2`, 중단 지점, 동일 초/바이트, `st_dev`) | 기대 결과 | 유료 호출 수 |
|---|---|---|
| **1. 편집 → compile/sync 전 → 진행 중 `verify` stamp / `first_card` 게시** | 훅이 `record_pending_refresh(..., compact=False)`를 `.handoff/v1/refresh/events.jsonl`에 선기록하므로, `1.1.1+` `verify`/`first_card`의 `unresolved_pending_refreshes_strict`가 구 원문 카드 stamp·게시를 즉시 보류한다(`1.0.22~1.1.0`은 private lane을 읽지 않는 기존 동작과 동일). | 기존 진행분 외 **추가 0회** (갱신 후 extract 1 + verify 1) |
| **2. 훅 `record_pending_refresh` `0.15s` `.lock` 경합 또는 쓰기 실패** | 5초 hook timeout 보호를 위해 `source-refresh-pending.jsonl.lock`은 `LOCK_EX \| LOCK_NB`를 최대 `0.15s`만 폴링한다. 실패(`None`) 시 job을 적재하지 않고 넓은 `'sync'` grant 폴백도 만들지 않으며, `pendingRefresh='busy'\|'failed'` trace를 남기고 best-effort `laya-compile` wake를 요청해(8 MiB 상한 초과 시 오프훅 compaction 실행) exit 0 반환(`index`는 `laya-sync`도 wake). | **0회** (compaction/다음 편집 또는 SessionStart/Bash discovery에서 처리) |
| **3. `recover_pending_refreshes` vs `wiki_discovery.drain_after_sync` 동시/혼합 버전 (`1.1.0~1.1.2`) trigger 중복** ([wiki_source_intent.py](../memvane/wiki/intake/wiki_source_intent.py#L64-L97)) | 단일 Phase 1에서는 `wiki_discovery.drain_after_sync`가 unresolved pending refresh가 있는 경로(settle 여부 무관, 삭제 제외)의 커서 스냅샷만 갱신하고 `post_sync_source` 적재를 건너뛰어 `recover_pending_refreshes`의 `post_tool_source` 1건만 생성된다(`unresolved_pending_refreshes_strict`가 `None`이면 유실 방지를 위해 적재). `1.1.0~1.1.2`와 혼합 실행돼 두 행이 공존하면, 첫 job이 카드를 만든 뒤에는 `already_compiled_revision`/`awaiting_pending_verification`으로 스킵되고, 첫 job이 0장(discard·split)으로 끝난 경우만 두 번째가 재시도한다. | 단일 Phase 1: extract 1 + verify 1 / 혼합(카드 생성 시): +0회, (0장 종료 후 재시도 시): +1 extract, verify 0~1 |
| **3-1. job이 옛 바이트 읽는 중 원문 변경 + discovery skip** ([wiki_freshness.py](../memvane/wiki/card/wiki_freshness.py#L343-L344)) | `resolve_pending_refresh`는 현재 원문 SHA가 캡처 시점과 다르면 실패하고, `refresh_settlement`도 현재 원문 SHA 기준으로만 성립한다. 따라서 discovery skip 뒤에도 해당 pending은 unresolved로 남아 다음 compile pass가 새 바이트를 다시 컴파일한다. | 새 바이트 extract 1 + verify 1 (revision 유실 0) |
| **4. 호스트 비대칭** (Claude/Codex `posttool`+`index`+`compile`+`file-change-event` vs Hermes `posttool`+`index`, [core_bridge.py](../hermes_adapter/core_bridge.py#L344-L358)) | Phase 1에서 Hermes `index` 내부 편집 경로도 `compile_gate(POST_TOOL_TRIGGER)`·`event_enabled('postToolUse')`·`_source_record`·`incompatible_source` 동일 게이트를 통과할 때만 `record_pending_refresh(..., compact=False)` + `laya-compile` wake를 남긴다(Hermes에도 자동 편집 compile 경로가 동일하게 열림). | 세 호스트 동일: 변경당 extract 1 + verify 1 |
| **5. 구 owner(`1.0.22~1.1.2`)가 cache/resident 때문에 wake 선점** ([host_bundle.py](../memvane/runtime/host_bundle.py#L530-L541)) | **Phase 1에서는 `writer_floor`를 올리지 않는다.** 출시된 `1.1.1~1.1.2` compile worker는 `refresh_lanes.paths`로 `.handoff/v1/refresh/events.jsonl`을 이미 읽으므로 `recover_pending_refreshes`로 즉시 복구·컴파일한다. `1.0.22~1.1.0`은 private lane을 보지 못하므로 `1.1.1+` worker가 실행될 때까지 대기한다(유실 0). | `1.1.1~1.1.2`: extract 1 + verify 1 (중복 0) / `1.0.22~1.1.0`: 대기 중 **0회** |
| **6. 전역 보류 원인 전체** (`HOLD_SCAN_LIMIT`, `max_bytes=8 MiB` [handoff_work.py](../memvane/wiki/verify/handoff_work.py#L462), 세대 scan 64 `:471`, 깨진 HEX hold, unparked/opaque inode batch [queue_batch_prepare.py](../memvane/wiki/verify/queue_batch_prepare.py#L34-L49)) | Phase 2 SQLite 원장은 일반 hold를 `WHERE state='held' AND target_path=?`로 $O(\log N)$ 격리해 개수/바이트 스캔 상한 정체를 없앤다. 단 대상 집합을 알 수 없는 opaque inode batch가 남아 있는 전환 구간에 한해 `pipeline_fences(scope='global_publish')` 전역 fence 행으로 fail-closed를 유지한다. | 보류 중 **0회** |
| **7. `.compile-run.lock` 경합 및 락 순서** ([wiki_mutation_lock.py](../memvane/wiki/workspace/wiki_mutation_lock.py#L3-L5)) | `try_compile_run_lock`은 non-blocking으로 즉시 양보하고 `laya-compile`을 재시도한다. 모든 writer는 `card/ledger lock -> .topical-publish.lock` 순서를 지켜 교착을 막는다. | 경합 시 **0회** |
| **8. Phase 2 commit → `.publish-<txid>.tmp` → CAS → rename 중단 & 사용자 동시 편집** | rename 전 crash 시 다음 pass가 `publish_txns(state='publishing')`의 temp inode·SHA와 live 카드 `expectedTargetHash`를 재확인해 일치하면 roll-forward rename, 사용자가 그사이 편집했으면 temp를 unlink하고 `conflict_user_edited`로 roll-back(사용자 편집 보존). rename 후 SQLite 커밋 전 crash 시 `.publish-<txid>.tmp` **부재**로 rename 완료를 확정한다(live SHA가 temp SHA와 일치하면 `published`, 사용자가 그사이 다시 편집해 다르면 덮어쓰지 않고 `published(user_edited)`로 종결). | 복구 시 **0회** |
| **9. 같은 초·같은 바이트 반복 편집 & 재부팅 `st_dev` 변경(APFS)** | 동일 원문 해시·경로는 `(sourcePath, eventId)`·`wiki_discovery` 스냅샷으로 접힌다. 파일 신원 대조는 `(st_ino, size, sha256)`만 사용하고 프로세스·재부팅 간 `st_dev`를 배제한다([1.x 영속 계약](dev/contexts/release.md#L11)). | 중복 편집 **0회** |
| **10. `compile_intents` `INTENT_SCAN_LIMIT=512` 백로그 & 1.x/2.0 공존** ([compile_intents.py](../memvane/sync/compile_intents.py#L25)) | `stateFormat=1` 동안은 파일 intent + 커서 회전을 유지해 백로그를 이어 소비한다. Phase 2(`stateFormat=2`)에서는 `same_major`로 1.x 호스트를 배제한 뒤 전환한다. | 백로그 처리 **0회** |
| **11. UID 공유 `runtime-wakes.sqlite3`에서 1.x·2.0 공존** | Phase 1·2 모두 기존 `laya-compile` wake kind를 그대로 사용하므로 1.x owner가 모르는 kind 행이 큐에 쌓이지 않는다(향후 새 kind를 추가할 경우 1.x owner는 `release.md:14`에 따라 남김). | **0회** |

---

## 4. 목표 아키텍처와 Phase 1 / Phase 2 상세 계약

```
[Host Hooks: update-queued / index / file-change-event / (1.x compile)]
   │  - sync_owner / source-queue.jsonl.lock / card_write_lock / _compact 미실행
   │  - 편집 시(동일 게이트): record_pending_refresh(0.15s .lock) + laya-compile wake
   │  - 세션/Bash/편집: laya-sync wake만 기록 후 즉시 반환
   ▼
[laya-sync worker (sync_owner lease, ~300s SLO)]
   │  1) memvane-db view 게시 + bounded raw snapshot
   │  2) external_edits.drain -> wiki_discovery.drain_after_sync
   │     (unresolved pending refresh 경로는 커서만 갱신하고 중복 적재 생략; strict=None 시 적재)
   ▼
[laya-wiki worker (기존 laya-compile wake kind + 별도 .compile-run.lock lease)]
   │  - Phase 1: recover_pending_refreshes가 편집 기원 job 단일 생산(기존 1.x 원장/.handoff/v1)
   │  - Phase 2: .memvane/compile/wiki-pipeline.sqlite3 트랜잭션 원장 + 단일 rename 게시
```

### 4.1 Phase 1 (1.1.x 호환, 1.x 영속 계약 무저촉)

- **`compile` 액션 & Hermes `index` 내부 편집**:
  - `compile` 액션(Claude/Codex)은 5초 hook timeout 안에서 `_verified_edit` + `verify_selected_route` 검증을 유지하되, `sync_owner`·`source-queue.jsonl.lock` 획득과 직접 `source-queue.jsonl` append를 제거한다. 대신 기존 게이트(`compile_gate(POST_TOOL_TRIGGER)` · `event_enabled('postToolUse')` · `_source_record` · `incompatible_source`)를 통과한 대상에 한해 `record_pending_refresh(..., compact=False)`(`0.15s` lock polling)만 기록하고, 성공 시 `laya-compile` wake를 요청한다(구버전 `hooks.json` 호환 유지).
  - Hermes `index` 내부 편집(`core_bridge.py` → `host_response.py`)에도 **동일한 게이트 체인**을 적용해 통과 시 `record_pending_refresh(..., compact=False)` + `laya-compile` wake를 수행한다.
  - `record_pending_refresh`가 lock busy 또는 오류로 `None`을 반환하면 job 적재·`'sync'` grant 폴백 없이 `pendingRefresh='busy'|'failed'` trace만 남기고, 8 MiB 상한 초과 등 오프훅 compaction 해소를 위해 best-effort `laya-compile` wake를 요청한 뒤 정상 종료한다.
- **SessionStart (`update`/`update-queued`)**:
  - `_request_wiki_lifecycle`([host_response.py](../memvane/host/host_response.py#L654-L666))는 이미 best-effort 원자 쓰기라 훅에 유지한다.
  - `check_compile_queue`([host_response.py](../memvane/host/host_response.py#L623-L640)): symlink/소유권 안전 검사(`selected_compile_queue_unsafe`)만 훅에서 fail-closed로 유지하고, `unresolved_pending_refreshes_strict` 읽기 실패(`wiki_pending_refresh_unknown`)는 SessionStart 훅 차단(exit 3) 대신 diagnostic 이벤트 기록 + best-effort `laya-compile` wake로 전환한다.
- **오프훅 컴팩션·중복 억제·`writer_floor`**:
  - `_compact`는 훅 밖(`recover_pending_refreshes` / `wiki_compile_worker`)으로 이동한다.
  - `wiki_discovery.drain_after_sync`는 unresolved pending refresh에 이미 있는 경로(settle 여부 무관, 삭제 제외)를 커서 스냅샷에 반영하되 `post_sync_source` 중복 적재를 건너뛴다(`unresolved_pending_refreshes_strict`가 `None`이면 기존대로 적재).
  - `source-refresh-settled.json`은 계약 밖 파생 캐시([compile README](../memvane/wiki/compile/README.md#L9), [1.x 영속 계약](dev/contexts/release.md#L28))이므로 그대로 재계산한다.
  - **Phase 1에서는 `writer_floor`를 올리지 않는다**(`1.1.1+` 출시 worker가 `.handoff/v1/refresh/events.jsonl`을 이미 복구 가능).

### 4.2 Phase 2 (MemVane 2.0 메이저 계약 변경)

- **1.x 호스트 배제 장치 (`stateFormat: 2` 사용자 state base 단위 영향)**:
  - `layout.json`의 `stateFormat`을 `2`로 올린다([memvane_version.py](../memvane/kernel/memvane_version.py#L35-L42) `same_major`, [host_response.py](../memvane/host/host_response.py#L1124-L1125), [dispatch_target.py](../memvane/journal/dispatch_target.py#L108-L114), `product_sync_worker.py:329-331`).
  - `layout.json`은 **사용자 state base 단위**([1.x 영속 계약](dev/contexts/release.md#L22))이므로 `stateFormat: 2`로 올리면 위키뿐 아니라 해당 사용자의 모든 프로젝트와 store·journal·route 계약이 2.0 범위로 들어가며, 같은 머신의 미업그레이드 1.x 호스트 CLI는 모든 프로젝트에서 recall이 비고 `_major_mismatch_response` 안내를 받는다. 따라서 Phase 2는 **MemVane 2.0 메이저 릴리스에서만** 배포하며, 기존 1.x 프로젝트는 일회성 state base 마이그레이션 또는 명시적 재온보딩으로 전환한다.
- **SQLite 원장 위치·규칙·FTS 분리**:
  - 위치: `.memvane/compile/wiki-pipeline.sqlite3` (`.memvane/wiki/` 아래에 두지 않음).
  - 읽기 전용 열기는 `errors._query_only_uri` + `PRAGMA query_only=ON`을 사용하고, 파일 신원 대조에서 프로세스·재부팅 간 `st_dev`를 제외한다.
  - `.memvane/compile/wiki-retrieval/cards.sqlite`([wiki_compile_retrieval.py](../memvane/wiki/compile/wiki_compile_retrieval.py))는 일회성 FTS 오버레이 캐시이므로 내구 트랜잭션 원장(`wiki-pipeline.sqlite3`)과 분리 유지한다.
- **원장 스키마 및 `copy_quota`·게시·intent 처리**:
  - 주요 테이블: `compile_jobs`, `verify_jobs`, `staged_cards`(이미지 블롭 포함, `SUM(LENGTH(image_blob)) <= 64 MiB` 쿼리로 기존 `copy_quota` / `allocated-*` 대체), `refresh_events`, `audit_events`(manifest/tombstones/verify-deleted/verify-skipped/dedup-skipped), `cooldowns`, `pipeline_fences`(`scope='global_publish'` 등), `publish_txns`(`temp_name`, `temp_ino`, `expected_target_sha`, `state`).
  - **게시 순서**: `.publish-<txid>.tmp`(`O_CREAT|O_EXCL|O_NOFOLLOW`, `fsync`) → `publish_txns(state='publishing')` 커밋 → `.card-write.lock` → `.topical-publish.lock` → live 카드 CAS(`expectedTargetHash`) 확인 → `os.replace`/`rename_noreplace` + `fsync(parent)` → `publish_txns(state='published')` 커밋.
  - **compile intent 경계**: `stateFormat=1` 전환기에는 파일 기반 `provenance-intents/compile-<id>.json`을 그대로 쓰고, `stateFormat=2`로 1.x sync를 완전 배제한 뒤 내부 브리지(또는 동일 파일 포맷 유지)로 정리한다.
- **1회성 1.x → 2.0 마이그레이션 범위**:
  - 공용 큐 + `*.claimed.*`, `.memvane/compile/*.jsonl` 전체 + `compact-stamp` + `source-refresh-settled.json`, 쿨다운 파일, `.handoff/v1/*` 전체(`hold`/`work`/`closed`/`returns`+`fragment-*`/`refresh`/`manifest-scan`/`backfill`/`copy-quota`+`allocated-*`/`publication`/`ownership-*`/`review-turn-*`), 위키 숨김 파일(`.first-*`, `.*.pending-*`, `.pending-ownership.json`, `.verify-swap-*`)을 원장으로 흡수하고 구형 파일을 정리한다.
  - `copy_quota`는 이관된 `staged_cards` 바이트 합계로 재산정해 기존 누수 `allocated-*`를 0으로 정리한다. `.memvane/compile/retired-wiki/`의 사용자 복구용 아카이브 파일은 보존한다.

---

## 5. 단계별 이행 요약

| 단계 | 범위 | 1.x 계약 / 배제 | 핵심 검증 |
|---|---|---|---|
| **Phase 0 (현재)** | 설계서 r3 확정 및 독립 재리뷰 승인 | 없음 | 설계 전수·조합표 리뷰 `APPROVE` |
| **Phase 1 (1.1.x)** | 훅 경량화(단일 `record_pending_refresh` 0.15s lock + `_compact` 오프훅 + `laya-compile`/`laya-sync` wake) 및 `wiki_discovery` 중복 적재 skip | **무저촉** (`stateFormat=1`, `writer_floor` 무변경, `laya-compile` 유지) | 3호스트 편집/세션/Bash·`pendingRefresh` lock 실패·혼합 버전 E2E + `v1_guard --strict` |
| **Phase 2 (2.0 후보)** | `wiki-pipeline.sqlite3` 단일 원장, 단일 rename 게시, `HOLD_SCAN_LIMIT` 제거, 1회성 마이그레이션 | **메이저** (`stateFormat: 2`, 1.x 배제) | 중단/재개 roll-forward·roll-back·`published(user_edited)`, `hold > 1024`, 1.x→2.0 마이그레이션 E2E |

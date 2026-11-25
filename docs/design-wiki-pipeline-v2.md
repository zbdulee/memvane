# 위키 파이프라인 v2 구조 개편 설계

**상태**: 2차 개정 (독립 설계 리뷰 `REQUEST_CHANGES` 반영, 코드 변경 전 설계 단계, 2026-11-25)
**관련 문서**: [진행 상황](dev/progress-1.1.2.md), [1.x 영속 계약](dev/contexts/release.md), [compile README](../memvane/wiki/compile/README.md), [verify README](../memvane/wiki/verify/README.md), [host README](../memvane/host/README.md)

---

## 1. 배경과 목표

1-B-2(`discovery lock` 경합)와 1-E(`hold` 고아 디렉터리·`handoff_scan_limit` 64건 정체)에서 드러났듯, 현재 위키 파이프라인 결함은 **상태가 여러 디렉터리·파일(`compile/*.jsonl`, `.handoff/v1/*`, `.first-*`, `.*.pending-*`, `provenance-intents/`)에 분산**되고 **일부 쓰기가 훅 프로세스 안에서 직접 실행**되는 구조에서 반복된다. 반면 `sync`는 단일 SQLite store와 트랜잭션, 세대 단위 게시를 사용해 같은 부류의 잔재 결함이 구조적으로 발생하지 않는다.

목표는 다음 네 가지다.

1. **훅 경량화 (lock 경합 제거 + 의미 계약 보존)**: 훅([host_response.py](../memvane/host/host_response.py))에서 `sync_owner`·`source-queue.jsonl.lock`·`card_write_lock` 획득을 제거한다. 단, 훅이 지닌 두 가지 의미 계약—① **freshness 무효화 fence**(`record_pending_refresh` → `.handoff/v1/refresh/events.jsonl`), ② **명시적 편집 동의 + 호스트 귀속**(`edit-hints` inbox)—은 훅에서 가볍게 보존하고 `laya-sync` wake만 남긴다.
2. **변경 감지·job 생성 단일화**: 원문 변경 감지와 `source-queue` job 생성을 `sync` owner(`post_sync_source` / `edit-hints` drain / `wiki_discovery` 경로)로 단일화한다.
3. **별도 lease의 wiki worker + SQLite 단일 원장**: compile·verify·카드 게시는 기존 `laya-compile` wake kind([1.x 영속 계약](dev/contexts/release.md#L14) 4종 유지)와 별도 `.compile-run.lock` lease를 쓰는 `wiki` worker가 수행해 `sync`의 300초 지연 목표와 분리한다. Phase 2에서는 파이프라인 상태(`hold`·`pending`·`first-card`·큐·반환·감사 원장·쿨다운)를 단일 SQLite 트랜잭션 원장으로 통합하고, 디렉터리 스캔 상한(`HOLD_SCAN_LIMIT`) 병목을 제거한다.
4. **단일 rename 게시**: 사람이 읽고 편집하는 `.memvane/wiki/**/*.md`는 파일로 유지한다. 카드 게시는 동일 디렉터리 임시 파일(`.publish-<txid>.tmp`) 준비 → SQLite 커밋 → CAS(`expectedTargetHash` under `.card-write.lock` → `.topical-publish.lock`) → 단일 rename 순서로 진행하며, 중단 시 roll-forward / roll-back이 결정적으로 복구한다.

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
| **Refresh 원장** | `.memvane/compile/source-refresh-pending.jsonl` (공용) / `.handoff/v1/refresh/events.jsonl` (private) | `wiki_freshness`, `refresh_lanes.append_private`, `first_card` | `wiki_freshness`, `host_response`, `wiki_compile_worker`, `refresh_settlement` | `source-refresh-pending.jsonl.lock` | 공용: **계약** / private: 1.1.1+ |
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
| **1. 편집 → sync 전 → 진행 중 `verify` stamp / `first_card` 게시** | 훅이 `record_pending_refresh`를 `.handoff/v1/refresh/events.jsonl`에 선기록하므로, `verify`/`first_card`의 `unresolved_pending_refreshes_strict`가 구 원문 카드 stamp·게시를 즉시 보류한다. | 기존 진행분 외 **추가 0회** (갱신 후 extract 1 + verify 1) |
| **2. Phase 1 `edit-hints` cap(64) 초과 또는 `.lock` 경합** | [external_edits.py](../memvane/wiki/intake/external_edits.py#L337-L348) 선례대로 0.15s lock 경합·cap 초과 시 해당 원문을 유실하지 않고 `wiki_discovery.record_grant(..., 'sync', engine=host)` 폴백으로 전환한다. | 변경 원문당 extract 1 + verify 1 (중복 0) |
| **3. `edit-hints` vs 1.1.x 혼합 버전 trigger 중복** (`post_tool_source` vs `post_sync_source`, [wiki_source_intent.py](../memvane/wiki/intake/wiki_source_intent.py#L64-L97)) | 단일 Phase 1 버전에서는 `sync`가 `edit-hints`를 `wiki_discovery`보다 먼저 drain해 커서에 반영하므로 `post_tool_source` 1건만 생성된다. `1.1.0~1.1.2` 훅과 혼합 시 두 행이 공존해도 `wiki_compile_worker`가 동일 revision 카드/pending을 보고 두 번째 행을 스킵한다. | 단일 버전: extract 1 + verify 1 / 혼합(직렬 도달): +0회, (동시 선점 최악): +1 extract, verify 1 |
| **4. 호스트 비대칭** (Claude/Codex `posttool`+`index`+`compile`+`file-change-event` vs Hermes `posttool`+`index`, [core_bridge.py](../hermes_adapter/core_bridge.py#L344-L358)) | Phase 1에서 Hermes `index` 내부 편집 경로도 `record_pending_refresh` + `edit-hints(host='hermes')`를 남기도록 통일해, 세 호스트 모두 동일한 편집 → freshness fence → `laya-sync` 경로를 탄다. | 호스트 무관 변경당 extract 1 + verify 1 |
| **5. 구 owner(`1.0.22~1.1.2`)가 cache/resident 때문에 `laya-sync` wake 선점** ([host_bundle.py](../memvane/runtime/host_bundle.py#L530-L541)) | 구 sync는 `edit-hints` 파일을 알지 못해 건드리지 않고 보존한다. 최신 Phase 1 sync가 다음 wake에서 drain한다(신규 훅 형식 강제 차단이 필요하면 `writer_floor` 상향으로 구 owner를 거절). | 유실·중복 **0회** |
| **6. 전역 보류 원인 전체** (`HOLD_SCAN_LIMIT`, `max_bytes=8 MiB` [handoff_work.py](../memvane/wiki/verify/handoff_work.py#L462), 세대 scan 64 `:471`, 깨진 HEX hold, unparked/opaque inode batch [queue_batch_prepare.py](../memvane/wiki/verify/queue_batch_prepare.py#L34-L49)) | Phase 2 SQLite 원장은 일반 hold를 `WHERE state='held' AND target_path=?`로 $O(\log N)$ 격리해 개수/바이트 스캔 상한 정체를 없앤다. 단 대상 집합을 알 수 없는 opaque inode batch가 남아 있는 전환 구간에 한해 `pipeline_fences(scope='global_publish')`로 fail-closed를 유지한다. | 보류 중 **0회** |
| **7. `.compile-run.lock` 경합 및 락 순서** ([wiki_mutation_lock.py](../memvane/wiki/workspace/wiki_mutation_lock.py#L3-L5)) | `try_compile_run_lock`은 non-blocking으로 즉시 양보하고 `laya-compile`을 재시도한다. 모든 writer는 `card/ledger lock -> .topical-publish.lock` 순서를 지켜 교착을 막는다. | 경합 시 **0회** |
| **8. Phase 2 commit → `.publish-<txid>.tmp` → CAS → rename 중단 & 사용자 동시 편집** | rename 전 crash 시 다음 pass가 `publish_txns`의 temp inode·SHA와 live 카드 `expectedTargetHash`를 재확인해 일치하면 roll-forward rename, 사용자가 그사이 편집했으면 temp를 unlink하고 `conflict_user_edited`로 roll-back(사용자 편집 보존). rename 후 SQLite 커밋 전 crash 시 live 카드 SHA 일치로 `published` 멱등 완료. | 복구 시 **0회** |
| **9. 같은 초·같은 바이트 반복 편집 & 재부팅 `st_dev` 변경(APFS)** | `(path, content_sha256)` 동일 행은 `edit-hints`/원장에서 접힌다. 파일 신원 대조는 `(st_ino, size, sha256)`만 사용하고 프로세스·재부팅 간 `st_dev`를 배제한다([1.x 영속 계약](dev/contexts/release.md#L11)). | 중복 편집 **0회** |
| **10. `compile_intents` `INTENT_SCAN_LIMIT=512` 백로그 & 1.x/2.0 공존** ([compile_intents.py](../memvane/sync/compile_intents.py#L25)) | `stateFormat=1` 동안은 파일 intent + 커서 회전을 유지해 백로그를 이어 소비한다. Phase 2(`stateFormat=2`)에서는 `same_major`로 1.x 호스트를 배제한 뒤 전환한다. | 백로그 처리 **0회** |
| **11. UID 공유 `runtime-wakes.sqlite3`에서 1.x·2.0 공존** | Phase 1·2 모두 기존 `laya-compile` wake kind를 그대로 사용하므로 1.x owner가 모르는 kind 행이 큐에 쌓이지 않는다(향후 새 kind를 추가할 경우 1.x owner는 `release.md:14`에 따라 남김). | **0회** |

---

## 4. 목표 아키텍처와 Phase 1 / Phase 2 상세 계약

```
[Host Hooks: update-queued / index / file-change-event / (1.x compile)]
   │  - sync_owner / source-queue.jsonl.lock / card_write_lock 미획득
   │  - 편집 시: record_pending_refresh(freshness fence) + edit-hints(host,path,blob)
   │  - 세션/Bash/편집: laya-sync wake만 기록 후 즉시 반환
   ▼
[laya-sync worker (sync_owner lease, ~300s SLO)]
   │  1) memvane-db view 게시 + bounded raw snapshot
   │  2) external_edits.drain -> edit_hints.drain -> wiki_discovery.drain_after_sync
   │  3) 변경 감지와 job enqueue 단일화 -> 큐 적재 시 laya-compile wake 요청
   ▼
[laya-wiki worker (기존 laya-compile wake kind + 별도 .compile-run.lock lease)]
   │  - Phase 1: 기존 1.x 큐/원장/.handoff/v1 유지
   │  - Phase 2: .memvane/compile/wiki-pipeline.sqlite3 트랜잭션 원장 + 단일 rename 게시
```

### 4.1 Phase 1 (1.1.x 호환, 1.x 영속 계약 무저촉)

- **`compile` 액션 & Hermes `index` 내부 편집**:
  - `compile` 액션은 5초 hook timeout 안에서 `_verified_edit` + `verify_selected_route` 검증을 유지하되, `sync_owner`·`source-queue.jsonl.lock` 획득을 제거하고 `record_pending_refresh` + `edit-hints-<scope>-<incarnation>.json`(`0600`, `O_NOFOLLOW`, 0.15s `.lock`, 상한 64, 초과/경합 시 `wiki_discovery` `'sync'` grant 폴백) + `laya-sync` wake만 수행한다. 구버전 `hooks.json`이 계속 `compile`을 호출해도 동일하게 동작한다.
  - Hermes `index` 내부 편집(`core_bridge.py`)도 같은 `record_pending_refresh` + `edit-hints(host='hermes')` 경로를 탄다.
- **SessionStart (`update`/`update-queued`)**:
  - `_request_wiki_lifecycle`([host_response.py](../memvane/host/host_response.py#L654-L666))는 이미 best-effort 원자 쓰기라 훅에 유지한다.
  - `check_compile_queue`의 `wiki_pending_refresh_unknown`·`selected_compile_queue_unsafe` hard-fail([host_response.py](../memvane/host/host_response.py#L623-L640))은 symlink/소유권 안전 검사만 남기거나 `laya-sync` 이후 off-hook 진단으로 옮겨 SessionStart 차단을 없앤다.
- **`sync` owner 단일화 & 파생 캐시**:
  - `product_sync_worker` 게시 후 `external_edits.drain` → `edit_hints.drain`(동일 `(path, blob)`을 `wiki_discovery` 커서에 반영해 `post_tool_source` 1건으로 합침) → `wiki_discovery.drain_after_sync` 순으로 실행하고, 큐가 늘면 `laya-compile` wake를 건다.
  - `source-refresh-settled.json`은 계약 밖 파생 캐시([release.md](../memvane/wiki/compile/README.md#L9), `release.md:28`)이므로 `eventId` 생성 시점이 바뀌어도 재계산으로 정상 동작한다.

### 4.2 Phase 2 (MemVane 2.0 메이저 계약 변경)

- **1.x 호스트 배제 장치**:
  - `layout.json`의 `stateFormat`을 `2`로 올린다([memvane_version.py](../memvane/kernel/memvane_version.py#L35-L42) `same_major`, [host_response.py](../memvane/host/host_response.py#L1124-L1125), [dispatch_target.py](../memvane/journal/dispatch_target.py#L108-L114), `product_sync_worker.py:329-331`). `writer_floor`만으로는 1.x 호스트가 공용 `.jsonl`/intent 포맷을 읽고 쓰는 것을 막을 수 없으므로 `stateFormat: 2`로 1.x 호스트 전체를 배제하고 `_major_mismatch_response` 안내를 내보낸다.
- **SQLite 원장 위치·규칙·FTS 분리**:
  - 위치: `.memvane/compile/wiki-pipeline.sqlite3` (`.memvane/wiki/` 아래에 두지 않음).
  - 읽기 전용 열기는 `errors._query_only_uri` + `PRAGMA query_only=ON`을 사용하고, 파일 신원 대조에서 프로세스·재부팅 간 `st_dev`를 제외한다.
  - `.memvane/compile/wiki-retrieval/cards.sqlite`([wiki_compile_retrieval.py](../memvane/wiki/compile/wiki_compile_retrieval.py))는 일회성 FTS 오버레이 캐시이므로 내구 트랜잭션 원장(`wiki-pipeline.sqlite3`)과 분리 유지한다.
- **원장 스키마 및 `copy_quota`·게시·intent 처리**:
  - 주요 테이블: `compile_jobs`, `verify_jobs`, `staged_cards`(이미지 블롭 포함, `SUM(LENGTH(image_blob)) <= 64 MiB` 쿼리로 기존 `copy_quota` / `allocated-*` 대체), `refresh_events`, `audit_events`(manifest/tombstones/verify-deleted/verify-skipped/dedup-skipped), `cooldowns`, `pipeline_fences`(`global_publish_fence` 등), `publish_txns`(`temp_name`, `temp_ino`, `expected_target_sha`, `state`).
  - **게시 순서**: `.publish-<txid>.tmp`(`O_CREAT|O_EXCL|O_NOFOLLOW`, `fsync`) → `publish_txns(state='publishing')` 커밋 → `.card-write.lock` → `.topical-publish.lock` → live 카드 CAS(`expectedTargetHash`) 확인 → `os.replace`/`rename_noreplace` + `fsync(parent)` → `publish_txns(state='published')` 커밋.
  - **compile intent 경계**: `stateFormat=1` 전환기에는 파일 기반 `provenance-intents/compile-<id>.json`을 그대로 쓰고, `stateFormat=2`로 1.x sync를 완전 배제한 뒤 내부 브리지(또는 동일 파일 포맷 유지)로 정리한다.
- **1회성 1.x → 2.0 마이그레이션 범위**:
  - 공용 큐 + `*.claimed.*`, `.memvane/compile/*.jsonl` 전체 + `compact-stamp` + `source-refresh-settled.json`, 쿨다운 파일, `.handoff/v1/*` 전체(`hold`/`work`/`closed`/`returns`+`fragment-*`/`refresh`/`manifest-scan`/`backfill`/`copy-quota`+`allocated-*`/`publication`/`ownership-*`/`review-turn-*`), 위키 숨김 파일(`.first-*`, `.*.pending-*`, `.pending-ownership.json`, `.verify-swap-*`)을 원장으로 흡수하고 구형 파일을 정리한다.
  - `copy_quota`는 이관된 `staged_cards` 바이트 합계로 재산정해 기존 누수 `allocated-*`를 0으로 정리한다. `.memvane/compile/retired-wiki/`의 사용자 복구용 아카이브 파일은 보존한다.

---

## 5. 단계별 이행 요약

| 단계 | 범위 | 1.x 계약 / 배제 | 핵심 검증 |
|---|---|---|---|
| **Phase 0 (현재)** | 설계서 r2 확정 및 독립 재리뷰 승인 | 없음 | 설계 전수·조합표 리뷰 `APPROVE` |
| **Phase 1 (1.1.x)** | 훅 경량화(`edit-hints` + `record_pending_refresh` + `laya-sync` wake) 및 `sync` discovery 단일화 | **무저촉** (`stateFormat=1`, `laya-compile` 유지) | 3호스트 편집/세션/Bash·`DiscoveryBusy`·혼합 버전 E2E + `v1_guard --strict` |
| **Phase 2 (2.0 후보)** | `wiki-pipeline.sqlite3` 단일 원장, 단일 rename 게시, `HANDOFF_SCAN_LIMIT` 제거, 1회성 마이그레이션 | **메이저** (`stateFormat: 2`, 1.x 배제) | 중단/재개 roll-forward·roll-back, `hold > 1024`, 1.x→2.0 마이그레이션 E2E |

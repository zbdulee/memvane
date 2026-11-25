# 위키 파이프라인 v2 구조 개편 설계

**상태**: 초안 (2순위 코드 변경 전 설계 단계, 2026-11-25)
**관련 문서**: [진행 상황](dev/progress-1.1.2.md), [1.x 영속 계약](dev/contexts/release.md), [compile README](../memvane/wiki/compile/README.md), [verify README](../memvane/wiki/verify/README.md), [host README](../memvane/host/README.md)

---

## 1. 배경과 목표

1-B-2(`discovery lock` 경합)와 1-E(`hold` 고아 디렉터리·`handoff_scan_limit` 64건 정체)에서 드러났듯, 현재 위키 파이프라인 결함은 **상태가 여러 디렉터리·파일(`compile/*.jsonl`, `.handoff/v1/*`, `.first-*`, `.*.pending-*`, `provenance-intents/`)에 분산**되고 **일부 쓰기가 훅 프로세스 안에서 직접 실행**되는 구조에서 반복된다. 반면 `sync`는 단일 SQLite store와 트랜잭션, 세대 단위 게시를 사용해 같은 부류의 잔재 결함이 구조적으로 발생하지 않는다.

목표는 다음 네 가지다.

1. **훅 경량화**: 훅([host_response.py](../memvane/host/host_response.py))은 원문 변경 사실과 wake 요청만 남기고, discovery·compile enqueue·freshness pending 기록을 훅에서 제거한다.
2. **변경 감지·job 생성 단일화**: 원문 변경 감지와 compile job 생성을 `sync` owner(`post_sync_source` / `wiki_discovery` 경로)로 단일화한다.
3. **별도 lease의 wiki worker + SQLite 단일 원장**: compile·verify·카드 게시는 `sync`의 300초 지연 목표와 분리된 별도 lease를 쓰는 `wiki` worker가 수행하고, 파이프라인 상태(`hold`·`pending`·`first-card`·큐·반환)를 단일 SQLite 트랜잭션 원장으로 통합한다. `handoff_scan_limit`으로 무관한 대상의 게시가 막히는 병목도 원장 쿼리로 제거한다.
4. **단일 rename 게시**: 사람이 읽고 편집하는 `.memvane/wiki/**/*.md`는 파일로 유지하고, 카드 게시는 원장 커밋 뒤 동일 디렉터리 원자적 rename 한 번으로 끝낸다.

---

## 2. 현재 경계와 상태 파일 전수표

### 2.1 훅 vs 오프훅 워커 현재 경계

- **Runtime wake 종류** ([runtime_dispatch.py](../memvane/app/runtime_dispatch.py#L20)): `laya-sync`, `laya-compile` 2종.
- **훅 프로세스 직접 쓰기** ([host_response.py](../memvane/host/host_response.py)):
  - `update` / `update-queued` / `index` / `file-change-event`: `wiki_discovery.record_grant` / `activate_grant` / `revoke_grant` 직접 호출(lock 경합 시 `busy_skipped`), `_request_wiki_lifecycle`로 `-wiki-lifecycle-request.json` 기록, `_request_selected_sync`에서 `unresolved_pending_refreshes_strict` 및 queue 파일 크기 확인 후 `laya-compile` wake 요청.
  - `compile`: `sync_owner` + `owner_scope` 획득 시도 후 `wiki_compile_enqueue.enqueue_payload` 직접 실행(`owner_busy` 시 `record_grant` + `accept_wiki_grant` 폴백).

### 2.2 상태 파일 전수 (Writer / Reader / Lock / 1.x 계약)

| 분류 | 경로 / 패턴 | 주요 Writer | 주요 Reader | Lock / 직렬화 | 1.x 영속 계약 여부 |
|---|---|---|---|---|---|
| **공용 큐/원장** | `.memvane/compile/source-queue.jsonl` | `wiki_compile_enqueue`, `queue_return`, `queue_paths.requeue_lines` | `wiki_compile_worker`, `host_response`, `selected_execution` | `source-queue.jsonl.lock` (`flock`) | **계약** (행 모양, 1.x 호환) |
| **공용 큐/원장** | `.memvane/compile/verify-queue.jsonl` | `wiki_compile_worker`, `queue_return`, `upgrade_queue`, `pending_replacement` | `wiki_verify_worker`, `host_response`, `first_card`, `queue_batch_prepare` | `verify-queue.jsonl.lock` (`flock`) | **계약** (행 모양, 1.x 호환) |
| **원장(감사/결과)** | `.memvane/compile/generated-manifest.jsonl`, `tombstones.jsonl`, `candidates.jsonl`, `merge-needed.jsonl`, `discard-ledger.jsonl` | `wiki_compile_worker`, `wiki_compile`, `first_card`, `promotion`, `backfill_work` | `wiki_freshness`, `wiki_lifecycle`, `manifest_scan`, `wiki_verify_worker` | `card_write_lock` / queue lock | **계약** (`.memvane/compile/*.jsonl`) |
| **검수/중복 원장** | `.memvane/compile/verify-log.jsonl`, `verify-deleted.jsonl`, `verify-skipped.jsonl`, `dedup-needed.jsonl`, `dedup-skipped.jsonl`, `dedup-deleted.jsonl` | `wiki_verify_worker`, `verify_ledger`, `wiki_dedup_*` | `wiki_compile_worker`(억제 마커), `wiki_dedup_resolve`, 진단 | queue / file append + sidecar | **계약** (`.memvane/compile/*.jsonl`) |
| **쿨다운** | `.memvane/compile/cooldown`, `verify-cooldown`, `verify-engine-cooldown.json`, `dedup-judge-*` | `wiki_compile_worker`, `wiki_verify_worker`, `wiki_dedup_*` | 각 worker | sidecar `flock` | 파생 운영 상태 |
| **Refresh 원장** | `.memvane/compile/source-refresh-pending.jsonl` (공용) / `.handoff/v1/refresh/events.jsonl` (private) | `wiki_freshness`, `refresh_lanes.append_private`, `first_card` | `wiki_freshness`, `host_response`, `wiki_compile_worker`, `refresh_settlement` | `.card-write.lock` / private atomic replace | 공용: **계약** / private: 1.1.1+ |
| **Refresh 종결 캐시** | `.memvane/compile/source-refresh-settled.json` | `refresh_settlement` | `refresh_settlement` (compile 쪽만) | 원자적 replace | **계약 밖 파생물** |
| **Handoff 작업** | `.memvane/compile/.handoff/v1/{hold,work,closed}/<hex32>/{.w-*,raw,image,work}` 및 `allocated-<id>` | `handoff_work.Work`, `copy_quota`, `first_card`, `promotion`, `deferred_review`, `native_review` | `first_card.recover`, `queue_batch_prepare`, `upgrade_queue`, `pending_handoff` | `verify-queue.jsonl.lock` / dirfd `O_EXCL` | 1.1.1+ private (구버전 비노출) |
| **Handoff 반환/제안** | `.memvane/compile/.handoff/v1/returns/<sha24>/{native.claimed.*,ready-*,offer-*}` | `queue_return`, `handoff.api`, `batch_handoff`, `ordinary_slot` | `selected_execution`, `queue_batch_prepare`, `ordinary_slot` | `source/verify-queue.jsonl.lock` | 1.1.1+ private |
| **Handoff 기타** | `.memvane/compile/.handoff/v1/{backfill,manifest-scan}/...` | `backfill_work`, `manifest_scan` | `backfill_work`, `manifest_scan` | `.compile-run.lock` / queue lock | 1.1.1+ private |
| **위키 임시/대기 파일** | `.memvane/wiki/**/.first-<hex32>` | `first_card._resume` | `first_card._resume` | `card_write_lock` + `O_EXCL` | 1.1.1+ 임시 파일 |
| **위키 대기 교체본** | `.memvane/wiki/**/.<card>.pending-<hex16>` | `wiki_compile`, `pending_replacement` | `wiki_verify_worker`, `pending_replacement`, `promotion`, `pending_ownership` | `.card-write.lock` | 1.x 교체 대기 파일 |
| **최종 카드** | `.memvane/wiki/**/*.md` | `wiki_compile`, `first_card`, `promotion`, `verification_stamp`, `wiki_source_retire` | `memvane-db` sync, `recall`, `wiki_lifecycle`, 사용자 | `.card-write.lock` | **사용자 가시 산출물** |
| **Compile Intent** | `<install state>/provenance-intents/compile-<id>.json` (`.consumed`/`.rejected`/`.superseded`) | `wiki_verify_worker`, `verification_stamp`, `intent_ack` | `sync/compile_intents.consume` (`provenance-intents.cursor`) | `O_NOFOLLOW` + atomic rename | **계약** (호스트 간 공유) |
| **Discovery / Lifecycle** | `<state>/...wiki-discovery*`, `<sync state>/<key>-wiki-lifecycle-{request,served}.json` | `host_response`, `wiki_discovery`, `lifecycle_requests` | `product_sync_worker`, `wiki_compile_worker`, `wiki_lifecycle` | discovery lock (`DiscoveryBusy`) | 설치 파생 상태 |

---

## 3. 상태 조합표 (실패·혼합 버전·경합 매트릭스)

| 시나리오 / 조합 | 현재 구조 (v1.1.2)의 동작과 잔재 위험 | v2 목표 설계에서의 해소 |
|---|---|---|
| **1. `Work._reserve` / `first_card` 중간 crash** | `work` 커밋 전이면 `.w-*`/`raw`/`image`/빈 `hold` 디렉터리와 `allocated-*` 잔존(v1.1.2에서 `reclaim_orphans` 60초 만료 회수로 완화). `rename_noreplace` 직전 crash 시 `.first-*` 남고 `unknown` 배열 64개 한도 존재. | SQLite 단일 트랜잭션으로 예약·복구 상태를 기록하므로 반쪽 디렉터리/`allocated-*`/`.first-*` 자체가 생기지 않음. |
| **2. `hold`·`pending` 다량 누적 (`> 64` 또는 `> 1,024`)** | 디렉터리 `scandir` 상한(`HOLD_SCAN_LIMIT=1024`)으로 확인하므로 상한 초과 시 `queue_batch_prepare.unresolved=True`로 무관한 카드의 첫 게시까지 보류 가능. | SQLite 인덱스 기반 상태 조회(`WHERE state='held' AND target_path=?`)로 전체 건수와 무관하게 $O(\log N)$ 판정, 스캔 상한 제거. |
| **3. 훅 실행 중 `DiscoveryBusy` / `owner_busy` 경합** | `host_response`가 훅 안에서 lock을 잡으려다 경합(`busy_skipped` 또는 `owner_busy` 폴백). 동시 다발 편집 시 훅 지연 및 grant 철회 레이스 발생. | 훅은 변경 힌트 + `laya-sync` wake만 기록하고 종료. `sync` owner가 단일 스캔·변경 감지로 job을 생성하므로 훅 경합 제거. |
| **4. `sync`와 `compile/verify`의 실행 시간 충돌** | 현재 이미 `laya-sync`와 `laya-compile`이 분리돼 있으나, `compile` 훅 액션이 `sync_owner`를 직접 잡으려 해 sync와 경합. | `laya-sync`는 색인·변경 감지(목표 300초 내), `laya-wiki`는 별도 lease로 유료 LLM CLI(compile/verify) 실행. 훅은 어느 lock도 잡지 않음. |
| **5. `1.0.22~1.1.2` 구 worker와 혼합 실행** | 공용 `source-queue.jsonl`·`verify-queue.jsonl`·`*.jsonl`·`provenance-intents/`를 구버전이 동시에 읽고 쓸 수 있어 `.handoff/v1` private 격리 및 `release_smoke`/호환 규칙 유지 필요. | **1.x 경계 보존**: SQLite 원장 전환(기존 `.memvane/compile/*.jsonl`·`.handoff/v1` 대체)은 2.0 메이저 계약 변경으로 분류해 1.x 호환 브리지/마이그레이션과 분리. |
| **6. 카드 게시 + manifest + 검증 stamp 원자성** | `first_card`·`promotion`·`verification_stamp`가 `.memvane/wiki/**` 파일과 `generated-manifest.jsonl`·`hold` 상태를 여러 파일에 나눠 기록. | v2 `wiki` worker가 SQLite 원장에 게시 예정 상태와 manifest를 단일 트랜잭션으로 커밋한 뒤, `.memvane/wiki/**/*.md`를 동일 디렉터리 임시 파일에서 단일 `os.replace`/`rename_noreplace`로 게시. |

---

## 4. 목표 아키텍처

```
[Host Hooks: update-queued / index / file-change-event]
   │  (락 없음: 변경 경로/힌트 + wake row만 기록 후 즉시 반환)
   ▼
[laya-sync worker (sync_owner lease, ~300s SLO)]
   │  1) memvane-db view 게시 + bounded raw snapshot
   │  2) 변경 감지·신규/수정/삭제 판정 단일화 (wiki_discovery 통합)
   │  3) SQLite wiki 원장에 compile job / refresh event enqueue
   │  4) compile intent 소비(또는 동일 설치 DB 브리지)
   ▼
[laya-wiki worker (별도 wiki lease, 유료 CLI 격리)]
   │  - SQLite 단일 원장(jobs, staged_cards, holds, audit_ledgers, cooldowns)
   │  - extract -> dedup -> verify(crossEngine) 상태 전이를 트랜잭션으로 처리
   │  - 카드 게시는 원장 커밋 후 .memvane/wiki/**/*.md 단일 rename
```

- **설계 원칙**:
  - `compile`·`verify`·`dedup`의 유료 CLI 호출은 `laya-sync`와 **절대 같은 lease나 pass에 합치지 않는다**.
  - `.memvane/wiki/**/*.md`는 사용자가 직접 읽고 수정할 수 있는 Markdown 파일로 유지한다.
  - 기존 `HOLD_SCAN_LIMIT` 같은 디렉터리 스캔 상한 의존을 없애고 SQLite 인덱스 조회로 대체한다.

---

## 5. 1.x 영속 계약 영향 및 단계별 이행 계획

[1.x 영속 계약](dev/contexts/release.md)상 `.memvane/compile/*.jsonl` 행 모양과 `provenance-intents/` 스키마는 출시된 1.x 호스트 간 공유 계약이다. 따라서 구현을 두 단계로 나눈다.

| 단계 | 대상 | 1.x 계약 영향 | 내용 |
|---|---|---|---|
| **Phase 0 (현재)** | 설계·전수표·리뷰 | 없음 | 본 설계서 작성 및 독립 리뷰(1.x vs 2.0 경계 확정). |
| **Phase 1 (1.1.x 호환 가능)** | 훅 경량화 & discovery 단일화 | **없음** | `host_response.py`의 `index`/`file-change-event`/`compile` 훅 내부 직접 큐/grant 쓰기를 최소화하고, `sync` owner의 `post_sync_source` 경로로 job 생성을 일원화(기존 1.x 큐/원장 형식 유지). |
| **Phase 2 (2.0 후보)** | SQLite 파이프라인 원장 전환 | **메이저 계약 변경** | `.memvane/compile/*.jsonl`·`.handoff/v1/*`·`.first-*`·`.*.pending-*`를 SQLite 단일 원장으로 대체. 전환 시 기존 잔여 큐/카드를 1회성 마이그레이션으로 원장에 흡수하고 구형 파일 정리. |

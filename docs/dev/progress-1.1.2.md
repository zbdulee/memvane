# 1.1.2 진행 기록

## 기준 상태

- 브랜치: `claude/1.1.2` (기준 `main`: `dd314fce8`, 출시 태그: `v1.1.1`)
- 마지막 갱신: 2026-11-25 (Step 1-E 구현 및 focused·카나리 복사본 실증 완료)
- 기본 규칙: [공통 개발 규칙](contexts/working-rules.md), [릴리스 체크리스트](contexts/release.md)

## 전체 진행 순서와 확정 결정

1. **0순위 — 로컬 웹 UI r4 설계·목업·로드맵 반영 (완료, 커밋 `4897c334`)**
   - 대상: [UI 설계서](../design-local-web-ui.md), [프로토타입](../mockups/local-web-ui-prototype.html), [ROADMAP](../ROADMAP.md)
   - 결정: 사이드바(`S1-sidebar-tree`)와 상단바(`T1-topbar`)에 main·worktree 모두 현재 git 브랜치명을 표시하며 `<gitdir>/HEAD`를 `O_NOFOLLOW` 4 KiB로 직접 읽는다.
2. **1순위 — `1.1.2` 패치 (진행 중)**
   - **1-A Recall 구조적 식별자 단독 어휘 질의 (완료, 커밋 `0ef64e92`)**: **B안 확정** — 한 줄 지시문 정규식 확장을 제외하고 `memvane/recall/domain/rules.py`에서 구조적 식별자 단독 FTS 질의만 프롬프트·편집/학습 공통으로 유지한다.
   - **1-B-1 `resident` 진단 3종 (완료, 커밋 `90829663`)**: `production_attestation_unavailable` 읽기 훅 보호(`host_response.py`), 로컬 증명 이력 + 냉기동 실패 뒤 `'starting'` 잔류 해소(`embed_resident.py`), 반복 `adopt` 시 공유 cooldown 예약 보존(`resident_service.py`).
   - **1-B-2 활성화 중 `file-change-event` grant 경합 + `resident` 번들 인계 (완료, 커밋 `3893063d`)**:
     - `memvane/wiki/intake/types.py`, `wiki_discovery.py`, `wiki_backfill.py`: `DiscoveryBusy`를 `types.py`로 옮기고 `seed_source_snapshot`의 `_raw_snapshot` 스캔을 lock 밖으로 이동해 lock 점유를 축소하며, `discover` 첫 생성 직후 중복 `_eligible` 스캔을 제거한다.
     - `memvane/host/host_response.py`: `file-change-event`/`update-queued`가 discovery lock 경합(`DiscoveryBusy`)을 만나면 `laya-sync` wake는 유지하고 compile grant만 `busy_skipped`로 건너뛰어 exit 0으로 마친다(비-busy 실패·`GrantUncertain`·`compile` `owner_busy` 경로는 fail-closed 유지).
     - `memvane/runtime/host_bundle.py`: `_host_listing` timeout을 `120`초(`HOST_LISTING_TIMEOUT_SECONDS`)로 올리고, `background_installed()`가 조회 실패 시 `3600`초(`INVENTORY_CACHE_SECONDS`) 대신 `60`초(`INVENTORY_RETRY_SECONDS`) 후 재시도하도록 분리한다.
     - 검증: Python 3.9 import(`PY39_OK`), `v1_guard.py --strict`(exit 0), `doc-links.test.mjs`·`memvane-naming-guard.test.mjs`(5/5), `memvane/runtime/tests/run.test.mjs`(25/25), `memvane/wiki/intake/tests/run.test.mjs`(13/13), `memvane/host/tests/run.test.mjs`(24/24), `memvane/resident/tests/run.test.mjs`(13/13) 통과.
   - **1-C 출시 smoke 도구 결함 정리 (완료, 커밋 `1080aca9`)**:
     - [smoke_fixture.py](../../tests/support/smoke_fixture.py): `P3-claude`/`P3-codex` 호스트별 고유 문단·도구 명시(`Edit`/`apply_patch`, 셸 명령 금지) 추가, `delta()`의 `P4` 단일 3토큰 질의를 `P4-added`·`P4-changed`·`P4-deleted` 개별 프로브 3개로 분리.
     - [release_smoke.py](../../scripts/release_smoke.py): `RS-ID` 제품 파일 vs 문서 전용(`_is_doc_only`) 차이 분리 판정, `judge_sync()`에서 `indexed` 뒤 `unchanged` pass 허용 및 `P4-*` 개별 판정(구형 `P4` 하위 호환 유지), `judge()`/`judge_edit()`에 Codex 첫 턴 `SessionStart`·셸 편집 안내 비고 추가, `send()` 호스트별 프롬프트 자동 해석(`P3-<host>`) 및 1,000자 이상 붙여넣기 3초 정지(`LONG_PASTE_STABLE_READS = 4`) 판정 적용.
     - [release.md](../release.md), [test_smoke_fixture.py](../../tests/support/test_smoke_fixture.py), [test_release_smoke.py](../../tests/support/test_release_smoke.py): 문서 및 회귀 테스트 6건 반영.
     - 검증: Python 3.9 import(`PY39_OK`), `v1_guard.py --strict`(exit 0), `release-smoke.test.mjs`·`doc-links.test.mjs`·`memvane-naming-guard.test.mjs`(7/7), `npm run test:focused -- tests/support/release-smoke.test.mjs`(`test_smoke_fixture` 4/4, `test_release_smoke` 38/38) 통과.
   - **1-D `1.1.2` 마무리 검증 (완료, 커밋 `585a2f53`)**:
     - [ROADMAP](../ROADMAP.md) 완료 항목 정리(`d355a0a2`), macOS 첫 실행 지연 테스트 2건(`test_setup_entry_inventory_119.py`, `checkout_cache_e2e.py`) shim 예열 수정(`3ef9f51c`).
     - Orca Claude 독립 리뷰(`claude-opus-5-5`, `APPROVE`, 차단 0건, 비차단 8건) 반영(`585a2f53`) 및 `npm test` 5파트 전체 통과(`110/0/2`, `113/0/2`, `113/0/2`, `116/0/2`, `220/0/4`).
   - **1-E 1.1.1 회귀: 첫 카드 게시 정체 (구현·focused·카나리 복사본 실증 완료)**:
     - **원인**: `Work._reserve`가 `copy_quota.allocate`와 `mkdir(identity)`·`_publish(.w-*)` 중 `budget.charge()` 예외 등으로 중단되면 `work` 없는 고아 `hold` 디렉터리와 `allocated-*` quota가 남고, `Work.entries` 기본 상한(`max_entries=64`) 밖의 고아·정상 `hold`가 `queue_batch_prepare.unresolved`를 영구 `True`로 잠근다.
     - **상태 조합**:
       - `Work._reserve` 예외(`work` 없음): `.w-*`·`raw`/`image`·빈 디렉터리·`copy_quota` 즉시 롤백(호출 0추가).
       - `Work._reserve` 중 `work` rename 완료 후 `fsync` 실패: 커밋된 세대 보존.
       - 기존 고아 `hold`(`work` 없음, 허용 파일 `.w-*`/`raw`/`image`, 나이 ≥ `ORPHAN_MIN_AGE_SECONDS` 60초): `first_card.recover`·`upgrade_queue.prepare`가 잠금 아래 `Work.reclaim_orphans`로 회수·`copy_quota` 반환.
       - 알 수 없는 파일 포함·60초 미만·벽시계 역전 고아: 보존(미회수).
       - `hold` 복구·판정 스캔: `HOLD_SCAN_LIMIT`(1,024)까지 읽어 카나리 기존 정상 `hold` 181건도 해소.
     - **구현**: [copy_quota.py](../../memvane/wiki/verify/copy_quota.py), [handoff_work.py](../../memvane/wiki/verify/handoff_work.py), [first_card.py](../../memvane/wiki/verify/first_card.py), [upgrade_queue.py](../../memvane/wiki/verify/upgrade_queue.py), [queue_batch_prepare.py](../../memvane/wiki/verify/queue_batch_prepare.py), [deferred_review.py](../../memvane/wiki/verify/deferred_review.py), [native_review.py](../../memvane/wiki/verify/native_review.py), [batch_handoff.py](../../memvane/wiki/verify/batch_handoff.py), [pending_handoff.py](../../memvane/wiki/verify/pending_handoff.py), [ordinary_slot.py](../../memvane/wiki/verify/ordinary_slot.py), [README.md](../../memvane/wiki/verify/README.md), [test_handoff_work.py](../../memvane/wiki/verify/tests/unit/test_handoff_work.py), [test_first_card.py](../../memvane/wiki/verify/tests/integration/test_first_card.py).
     - **검증**: `v1_guard.py --strict`(exit 0), `/usr/bin/python3` import(`PY39_OK`), `memvane/wiki/verify/tests/run.test.mjs`(18/18 통과), 카나리 임시 복사본(`min_age_seconds=0.0`, 원본 `.memvane/` 무변경)에서 `orphans: 219→0`, `allocs: 160→152`, `unresolved: True→False` 확인.
     - **다음**: 1-E 커밋 기준 `npm test` 5파트 전체 실행 → Orca Claude 증분 리뷰 → 릴리스 버전 bump(사용자 확인 후).
3. **2순위 — 위키 파이프라인 구조 개편 (설계 전, 1.2 후보, 사용자 방향·순위 확정 2026-11-25)**
   - 진행 방식: 1-E 최소 수정·1.1.2 출시와 병행한다. 첫 단계는 코드 변경 없는 설계다. 위키 파이프라인 상태 파일(handoff hold·`.first-*`·`.*.pending-*`·`.memvane/compile/*.jsonl`·compile intent)의 writer·reader 전수(출시된 1.x 포함)와 상태 조합표를 먼저 쓰고, 설계 리뷰 뒤 구현에 들어간다. 스캔 상한(`handoff_scan_limit`)이 무관한 대상의 게시를 막는 문제도 여기서 다룬다.
   - 배경: 1-E처럼 위키 쪽 결함은 상태가 여러 파일(handoff hold 디렉터리, `.w-*`, `.first-*`, `.*.pending-*`, `.memvane/compile/*.jsonl` 원장)에 나뉘어 단계 사이 실패마다 잔재와 경로별 정리 코드가 생기는 데서 반복된다. sync는 SQLite 하나와 트랜잭션, 세대 단위 게시를 써서 같은 부류의 결함이 구조적으로 생기지 않는다.
   - 현재 경계: runtime dispatcher의 wake 종류는 `laya-sync`·`laya-compile` 둘이고(`memvane/app/runtime_dispatch.py` `KINDS`), compile·verify는 hook 밖 `laya-compile` worker가 돈다. hook(`memvane/host/host_response.py`)은 discovery, compile enqueue, freshness pending 기록, 외부 편집 알림을 hook 안에서 직접 쓴다(1-B-2 discovery lock 경합의 위치).
   - 방향:
     1. hook은 원문 변경 사실과 wake만 남긴다. discovery·enqueue·pending 기록을 hook에서 뺀다.
     2. 변경 감지와 compile job 생성은 sync owner로 단일화한다(현재 `post_sync_source` 경로).
     3. compile·verify·카드 게시는 sync와 다른 lease를 쓰는 별도 worker로 두고, 파이프라인 상태(hold·pending·first-card·원장)를 SQLite 하나의 트랜잭션 원장으로 옮긴다.
     4. 사람이 보고 고치는 카드 파일(`.memvane/wiki/**/*.md`)은 파일로 유지하고, 게시는 원장 커밋 뒤 rename 한 번으로 끝낸다.
   - sync와 한 프로세스·한 pass로 합치지 않는다: compile·verify는 외부 LLM CLI를 분 단위로 호출하는 유료 작업이라 sync 지연 목표(300초)와 lease를 묶으면 위키 작업 하나의 정체가 색인 갱신까지 멈춘다.
   - 제약: `.memvane/compile/*.jsonl`·compile intent 등 1.x 영속 계약([release context](contexts/release.md))과 출시된 1.x 버전이 공유하는 state를 바꾸는 부분은 메이저 판단 대상이다. 착수 전 writer·reader 전수와 상태 조합표를 먼저 쓴다.
4. **3순위 — 구조 병목 해소 (예정)**: `shared.py`(2,319줄)·`checkout_view.py` 분해 ([ROADMAP](../ROADMAP.md)).
5. **4순위 — 로컬 웹 대시보드 구현 (예정)**: `M0 → M1a → M1b → M1c → M1d` (`1.1.3` 목표, [UI 설계서](../design-local-web-ui.md) r4 기준).

## 즉시 재개 체크리스트 (Step 1-D)

- [x] [ROADMAP](../ROADMAP.md)에서 1.1.2에 해결된 항목(`식별자 질의의 어휘 검색`, `resident 진단`, `활성화 중 편집 거절`, `resident 번들 인계`, `출시 smoke 도구`, `새 실행 파일의 첫 실행 지연`) 정리
- [x] 전체 검사(`test:part1~5`, `BypassSandbox: true`, 단독 실행) + `v1_guard.py --strict` + `/usr/bin/python3` import 확인
- [x] 독립 리뷰(Orca Claude 세션) 수행 및 비차단 8건 반영(`585a2f53`)
- [x] 1-E 첫 카드 게시 정체(1.1.1 회귀) 수정·focused 검증·카나리 복사본 실증
- [ ] 1-E 커밋 기준 `npm test` 5파트 + Orca Claude 증분 리뷰 후 `1.1.2` 버전 bump 여부 사용자 확인

## 유지해야 할 제약

- `.memvane/wiki/**`의 미추적 카나리 파일은 절대 스테이징하거나 삭제하지 않는다. `.first-*` 임시 파일과 `.memvane/compile/.handoff/v1/hold`의 고아 디렉터리도 손으로 지우지 않는다(1-E 복구 확인 대상).
- Python 3.9 하한(`from __future__ import annotations`, 3.10+ 전용 문법 금지)과 SQLite read-only(`errors._query_only_uri` + `PRAGMA query_only=ON`) 규칙을 지킨다.
- `tests/structure/v1_guard.py --strict` 및 `tests/structure/v1-baseline.json` 한도(`hook-serve` top-level import closure, `_serve_selected_once` 169줄, `memvane/wiki/intake/api.py` 30/30)를 유지한다.
- 모든 테스트는 `scripts/development_guard.py`(`npm run test:focused` / `npm test`)를 경유해 실행한다.
- 마일스톤(커밋) 완료 시 이 문서를 함께 갱신한다.

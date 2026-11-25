# 1.1.2 진행 기록

## 기준 상태

- 브랜치: `claude/1.1.2` (기준 `main`: `dd314fce8`, 출시 태그: `v1.1.1`)
- 마지막 갱신: 2026-11-25 (Step 1-C 완료)
- 기본 규칙: [공통 개발 규칙](contexts/working-rules.md), [릴리스 체크리스트](contexts/release.md)

## 전체 진행 순서와 확정 결정

1. **0순위 — 로컬 웹 UI r4 설계·목업·로드맵 반영 (완료, 커밋 `4897c334`)**
   - 대상: [UI 설계서](../design-local-web-ui.md), [프로토타입](../mockups/local-web-ui-prototype.html), [ROADMAP](../ROADMAP.md)
   - 결정: 사이드바(`S1-sidebar-tree`)와 상단바(`T1-topbar`)에 main·worktree 모두 현재 git 브랜치명을 표시하며 `<gitdir>/HEAD`를 `O_NOFOLLOW` 4 KiB로 직접 읽는다.
2. **1순위 — `1.1.2` 패치 (진행 중)**
   - **1-A Recall 구조적 식별자 단독 어휘 질의 (완료, 커밋 `0ef64e92`)**: **B안 확정** — 한 줄 지시문 정규식 확장을 제외하고 `memvane/recall/domain/rules.py`에서 구조적 식별자 단독 FTS 질의만 프롬프트·편집/학습 공통으로 유지한다.
   - **1-B-1 `resident` 진단 3종 (완료, 커밋 `90829663`)**: `production_attestation_unavailable` 읽기 훅 보호(`host_response.py`), 로컬 증명 이력 + 냉기동 실패 뒤 `'starting'` 잔류 해소(`embed_resident.py`), 반복 `adopt` 시 공유 cooldown 예약 보존(`resident_service.py`).
   - **1-B-2 활성화 중 `file-change-event` grant 경합 + `resident` 번들 인계 (완료)**:
     - `memvane/wiki/intake/types.py`, `wiki_discovery.py`, `wiki_backfill.py`: `DiscoveryBusy`를 `types.py`로 옮기고 `seed_source_snapshot`의 `_raw_snapshot` 스캔을 lock 밖으로 이동해 lock 점유를 축소하며, `discover` 첫 생성 직후 중복 `_eligible` 스캔을 제거한다.
     - `memvane/host/host_response.py`: `file-change-event`/`update-queued`가 discovery lock 경합(`DiscoveryBusy`)을 만나면 `laya-sync` wake는 유지하고 compile grant만 `busy_skipped`로 건너뛰어 exit 0으로 마친다(비-busy 실패·`GrantUncertain`·`compile` `owner_busy` 경로는 fail-closed 유지).
     - `memvane/runtime/host_bundle.py`: `_host_listing` timeout을 `120`초(`HOST_LISTING_TIMEOUT_SECONDS`)로 올리고, `background_installed()`가 조회 실패 시 `3600`초(`INVENTORY_CACHE_SECONDS`) 대신 `60`초(`INVENTORY_RETRY_SECONDS`) 후 재시도하도록 분리한다.
     - 검증: Python 3.9 import(`PY39_OK`), `v1_guard.py --strict`(exit 0), `doc-links.test.mjs`·`memvane-naming-guard.test.mjs`(5/5), `memvane/runtime/tests/run.test.mjs`(25/25), `memvane/wiki/intake/tests/run.test.mjs`(13/13), `memvane/host/tests/run.test.mjs`(24/24), `memvane/resident/tests/run.test.mjs`(13/13) 통과.
   - **1-C 출시 smoke 도구 결함 정리 (완료)**:
     - [smoke_fixture.py](../../tests/support/smoke_fixture.py): `P3-claude`/`P3-codex` 호스트별 고유 문단·도구 명시(`Edit`/`apply_patch`, 셸 명령 금지) 추가, `delta()`의 `P4` 단일 3토큰 질의를 `P4-added`·`P4-changed`·`P4-deleted` 개별 프로브 3개로 분리.
     - [release_smoke.py](../../scripts/release_smoke.py): `RS-ID` 제품 파일 vs 문서 전용(`_is_doc_only`) 차이 분리 판정, `judge_sync()`에서 `indexed` 뒤 `unchanged` pass 허용 및 `P4-*` 개별 판정(구형 `P4` 하위 호환 유지), `judge()`/`judge_edit()`에 Codex 첫 턴 `SessionStart`·셸 편집 안내 비고 추가, `send()` 호스트별 프롬프트 자동 해석(`P3-<host>`) 및 1,000자 이상 붙여넣기 3초 정지(`LONG_PASTE_STABLE_READS = 4`) 판정 적용.
     - [release.md](../release.md), [test_smoke_fixture.py](../../tests/support/test_smoke_fixture.py), [test_release_smoke.py](../../tests/support/test_release_smoke.py): 문서 및 회귀 테스트 6건 반영.
     - 검증: Python 3.9 import(`PY39_OK`), `v1_guard.py --strict`(exit 0), `release-smoke.test.mjs`·`doc-links.test.mjs`·`memvane-naming-guard.test.mjs`(7/7), `npm run test:focused -- tests/support/release-smoke.test.mjs`(`test_smoke_fixture` 4/4, `test_release_smoke` 38/38) 통과.
   - **1-D `1.1.2` 마무리 검증 (다음 단계)**: [ROADMAP](../ROADMAP.md) 완료 항목 정리, 전체 검사 `npm test`(5파트 `test:part1~5`), 구조 가드, 독립 리뷰(Claude Opus), 릴리스 버전 bump(사용자 확인 후).
3. **2순위 — 구조 병목 해소 (예정)**: `shared.py`(2,319줄)·`checkout_view.py` 분해 ([ROADMAP](../ROADMAP.md#L118)).
4. **3순위 — 로컬 웹 대시보드 구현 (예정)**: `M0 → M1a → M1b → M1c → M1d` (`1.1.3` 목표, [UI 설계서](../design-local-web-ui.md) r4 기준).

## 즉시 재개 체크리스트 (Step 1-D)

- [ROADMAP](../ROADMAP.md)에서 1.1.2에 해결된 항목(`식별자 질의의 어휘 검색`, `resident 진단`, `활성화 중 편집 거절`, `resident 번들 인계`, `출시 smoke 도구`) 정리
- 전체 검사(`npm test`, `BypassSandbox: true`, 단독 실행) + `v1_guard.py --strict` + `/usr/bin/python3` import 확인
- 독립 리뷰(Claude Opus, `claude-exec` bridge) 수행 후 릴리스 버전 bump 여부 사용자 확인

## 유지해야 할 제약

- `.memvane/wiki/**`의 미추적 카나리 파일은 절대 스테이징하거나 삭제하지 않는다.
- Python 3.9 하한(`from __future__ import annotations`, 3.10+ 전용 문법 금지)과 SQLite read-only(`errors._query_only_uri` + `PRAGMA query_only=ON`) 규칙을 지킨다.
- `tests/structure/v1_guard.py --strict` 및 `tests/structure/v1-baseline.json` 한도(`hook-serve` top-level import closure, `_serve_selected_once` 169줄, `memvane/wiki/intake/api.py` 30/30)를 유지한다.
- 모든 테스트는 `scripts/development_guard.py`(`npm run test:focused` / `npm test`)를 경유해 실행한다.
- 마일스톤(커밋) 완료 시 이 문서를 함께 갱신한다.

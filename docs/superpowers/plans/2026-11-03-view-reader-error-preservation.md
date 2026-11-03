# Candidate reader 오류 보존 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 비활성 candidate reader에서 deadline 뒤 발생한 무결성·checkout·I/O 거절을 원래 사유로 전달하고, 실제 deadline 중단만 semantic defer로 처리한다.

**Architecture:** `ReaderMixin._reading`이 시간 경과만으로 모든 오류를 재분류하지 않도록 한다. 명시적인 deadline 오류는 그대로 전달하고, SQLite progress handler의 실제 interruption만 deadline 만료와 함께 확인한다. `BlobStore._session`의 공통 오류·transaction 계약은 유지한다.

**Tech Stack:** Python 3.9 이상, sqlite3, unittest, Node test runner.

**Spec:** `docs/development-view-candidate.md`, `docs/plans/2026-11-01-memvane-db-git-blob-model.md` §11·§12·§14, 외부 검토 항목 GB6.

## Global Constraints

- candidate를 운영 route·hook에 연결하지 않는다. PR5 bridge와 PR6/7 활성화는 별도 구현 범위다.
- 보호 파일 `core/laya_local_embed_provider.py`, `core/laya_local_embed_worker.mjs`, `hooks/hooks-codex.json`은 수정하지 않는다.
- 실제 모델·운영 DB를 사용하지 않는다. realpath 임시 HOME·runtime/state와 합성 Git·SQLite fixture만 사용한다.
- 검사는 전역 development guard, 파일 병렬도 1, 유한 timeout으로 순차 실행한다.
- 기존 검사·assertion·예산·무결성 검증·세대 봉인 규칙을 약화하지 않는다.

## Review Focus

- 실제 embedding set 손상을 검출한 직후 deadline을 넘겨도 손상 거절을 유지한다.
- 실제 checkout/파일 identity 거절 직후 deadline을 넘겨도 그 사유를 유지한다.
- SQLite I/O·busy·integrity 오류를 시간 경과만으로 정상 defer로 바꾸지 않는다.
- SQLite progress handler의 실제 interrupt는 기한이 지난 경우에만 deadline으로 변환한다.
- Python 3.9의 sqlite3 예외에 숫자 error code가 없어도 정확한 interruption만 식별한다.

## Task 1: 오류 보존과 deadline 대조군

**Files:**
- Create: `test/fixtures/laya-view-review-regressions-checks.py`
- Create: `test/laya-view-review-regressions.test.mjs`
- Modify: `core/laya_view_reader.py`

**Interfaces:**
- Consumes: `VectorCase.ready()`, `VectorCase.damage()`, 실제 `ViewStore.search_exact`·`search_hybrid`, `BlobStoreError.reason`.
- Produces: 기존 `_reading(deadline)` signature와 반환 계약을 유지한다. 오류 전달 외 새 public API는 없다.

- [x] **Step 1: 실제 검증 경로의 회귀와 대조군을 작성한다.** 합성 embedding set의 digest를 훼손하고 실제 검증이 예외를 낸 순간에만 가짜 시계를 deadline 뒤로 옮긴다. hybrid lexical 검색은 실제로 수행한다. checkout 거절과 SQLite 오류도 별도 단언하며 정상 interruption 대조군은 실제 progress handler를 거친다.
- [x] **Step 2: base RED를 보관한다.** `npm run test:focused -- test/laya-view-review-regressions.test.mjs`를 격리 환경에서 실행한다. 실제 손상 사유 대신 `deferred_deadline`이나 `fts_deadline_exceeded`가 나오는 assertion 실패와 정상 대조군 결과를 구분한다. 테스트 harness 오류는 제품 RED로 세지 않는다.
- [x] **Step 3: `_reading`의 재분류 범위를 제한한다.** `BlobStoreError` 자체의 무결성·checkout·busy·I/O reason은 유지한다. `_session`이 감싼 SQLite interruption은 원래 예외 연결과 숫자 `SQLITE_INTERRUPT`를 확인한다. 숫자 code가 없는 Python은 정확한 `sqlite3.OperationalError` interruption만 호환 처리한다. 재분류에는 deadline 만료도 필요하며 다른 SQLite 오류를 대신 수락하지 않는다.
- [x] **Step 4: focused GREEN을 확인한다.** 새 회귀와 `test/laya-view-reader.test.mjs`, `test/laya-view-vectors.test.mjs`, `test/laya-view-generations.test.mjs`를 동일 guard 아래 순차 실행한다. 손상·checkout·기타 SQLite 거절과 정상 deadline 대조군이 모두 통과해야 한다.
- [x] **Step 5: 변경과 근거를 검토한다.** 신규 regression과 reader 수정만 별도 commit으로 묶는다. 전체 suite·독립 exact-head 리뷰·manifest 버전·릴리스 운영 검증은 PR5 통합 게이트에서 수행한다. focused 통과를 전체 PR5 완료로 보고하지 않는다.

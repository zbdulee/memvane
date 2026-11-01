# Immutable blob/recipe/provenance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Existing user authorization selects inline work, scoped Claude implementation and one independent Codex PR-head review.

**Goal:** git-blob 설계 §12 PR2의 별도 candidate 저장 형식과 불변 content/recipe/provenance API를 구현한다.

**Architecture:** `laya_blob_store.py`는 private explicit SQLite 파일과 설정 기반 wiki admission을 소유한다. `laya_blob_schema.py`는 별도 application id/schema와 구조적 제약을, `laya_blob_values.py`는 canonical 값·digest·recipe·set·provenance 검증을 소유한다. 기존 v6와 live hook/route는 새 모듈을 호출하지 않는다.

**Tech Stack:** Python 표준 라이브러리, SQLite, 기존 PR1 checkout capture와 설정 해석 헬퍼. 모델 호출·새 dependency 없음.

**Spec:** `docs/plans/2026-11-01-memvane-db-git-blob-model.md` §3.1–4, §12 PR2, §14.

## Global Constraints

- 별도 format `memvane-git-blob-candidate-v1`, application id `0x4d564231`, schema version `1`. v6 파일을 변경하거나 user_version만 올리지 않는다.
- 보호3 `core/laya_local_embed_provider.py`, `core/laya_local_embed_worker.mjs`, `hooks/hooks-codex.json` 불변. 활성화·generation/FTS·migration·GC·PR3 제외.
- Tests: realpath 임시 HOME, global development guard, 파일 병렬1, 유한 timeout. 모델/리뷰와 검사 병행 금지. Claude advisor 금지.
- SHA256은 소문자64hex, UUID는32hex, canonical JSON은 sort_keys/ensure_ascii=False/separators(',', ':')/allow_nan=False. bool은 정수가 아니다.
- Blob은 정확한 UTF-8 byte 정체성이다. BOM/개행/Unicode를 정규화하지 않으며 decode/encode가 일치한다.
- Python은 `-B`와 `PYTHONDONTWRITEBYTECODE=1`. 실제 사용자 프로젝트 상태는 fixture로 사용하지 않는다.
- 최초 base e677382의 기존 전체 검사는 PR131 CI22/24/26에서661/pass658/fail0/skip3로 검증되어 있다. 같은 tree의 baseline 전체 검사를 반복하지 않는다.

## Review Focus

1. wiki root·설정·worktree·파일이 읽는 도중 바뀌면 admission 거절; snapshot 원자성을 주장하지 않는다.
2. 같은 body의 다른 path/provenance는 공유하지만 다른 recipe/profile/set은 공유하지 않는다.
3. SQL 구조적 제약과 API 의미 검증을 구분한다. candidate receipt는 compiler 사실·운영 권한의 증명이 아니다.
4. UTF-16 chunk offset이 surrogate pair 중간을 가리키거나 chunk text가 카드 slice와 다르면 거절한다.
5. transaction 중 오류·busy·동시 중복 삽입은 완성 set만 남기며 동일 replay만 no-op이다.

## Task 1: private candidate와 wiki admission

**Files:** Create `core/laya_blob_store.py`, `core/laya_blob_schema.py`, common helpers in `core/laya_blob_values.py`, `test/laya-blob-store.test.mjs`, `test/fixtures/laya-blob-store-checks.py`.

**Interfaces:** `BlobStore.create(path, *, project_root, repository, worktree, scope_id)`, `BlobStore.open(path, *, repository, scope_id)`, `register_worktree(project_root, worktree) -> worktree_id`, `ingest_card(worktree_id, path) -> blob_sha`, `document(blob_sha) -> {body,path,title}`, `counts()`, `close()`. `BlobStoreError.reason` is a bounded code. Implementation modules may expose private helpers only for tests.

- [x] Write failing cases for distinct format/no-clobber, private file/path/sidecars, v6 unchanged refusal, exact bytes/replay, configured wiki membership/raw traversal/meta-page rejection, settings/root/file races, path reuse, same-common-dir worktree reuse, wrong scope.
- [x] Run new suite on base and preserve expected missing-module failures; no product module before RED.
- [x] Implement creation/opening and fixed store/worktree binding, settings-derived raw/wiki roles and FD-bound bounded wiki-only reads. Capture and settings bytes/root/file stat proof must agree before/after. Persist metadata-only blob admission separately from blob identity.
- [x] Verify focused tests and existing checkout/host-route/index contracts. Commit this independently useful foundation after API/meaningful edge checks pass.

## Task 2: explicit recipes/profiles and complete sets

**Files:** Create `core/laya_blob_values.py`; extend store/schema and existing new fixture.

**Interfaces:** `register_recipe(spec) -> recipe_id`, `register_profile(spec) -> profile_id`, `put_chunk_set(blob_sha, recipe_id, rows) -> set_digest`, `chunks(blob_sha, recipe_id)`, `vector_input_sha(blob_sha, recipe_id, profile_id, seq)`, `put_embedding_set(blob_sha, recipe_id, profile_id, rows) -> set_digest`, `vectors(...)`.

- [x] RED cases: canonical/unknown field/type failures, title-less rename stable input, changed recipe/model bytes distinct, gaps/duplicate seq/position, bad text/hash/input hash, float32 dimensions/NaN/Inf/zero norm, partial/fault rollback, exact replay/conflict and separate recipe/profile keys, concurrent duplicate insertion.
- [x] Implement recipe/profile digest and pure value validation; atomic sets use one `BEGIN IMMEDIATE` for lookup/replay/insertion, never REPLACE. Counts/seq/positions/input/vector provenance are checked before commit and on read.
- [x] Provider document template is exactly `title: {title} | text: {text}`; query template is `task: search result | query: {text}`. Synthetic path is `blobs/<blob_sha>.md`; title follows the exact pinned JS qmd whitespace/line rules. Offsets use UTF-16 code units, matching current chunker. No provider/model execution.
- [x] Run focused RED→GREEN and atomic/rollback boundaries, then commit.

## Task 3: immutable source metadata and card attestations

**Files:** Extend three modules and fixture; create `docs/development-blob-candidate.md`; update AGENTS/CLAUDE module map and seven manifests to next patch.

**Interfaces:** `put_fingerprint(spec) -> fingerprint_id`, `fingerprint(id)`, `put_attestation(spec) -> trust_digest`, `attestation(id)`, `verify()`. `attestation_sources` gives each attestation its own ordered fingerprint/span references. Generation `card_sources` belongs to later PRs.

- [x] RED cases: same blob with distinct source attestations, source SHA changes do not rewrite old provenance, legacy_unbound retained as such, rebound must reference same-blob prior trust and match all path/SHA/spans/policy, unknown/mismatch rebound refusal, spans bounds and raw/transcript receipt fields rejected, SQL no-REPLACE/immutability/FK/schema tamper checks.
- [x] Implement metadata-only fingerprints and allowlisted versioned receipts. Source observations are caller-owned evidence claims; this candidate API does not read original source bodies or authorize deletion, compilation or trust. Locator bounds are metadata checks, not verified quotations. Rebound is a new immutable commitment, never automatic operational trust.
- [x] Add developer API/current-state docs and explicit limitations, keep README user interface unchanged. Preserve original git-blob design decisions.
- [x] Run focused related suites, then full suite once final source is fixed. Confirm protected3 diff empty and all7 versions match.
- [ ] Commit/push/PR, independent detached Codex Astra high read-only review; reproduce/fix findings by TDD and narrow re-review as original handoff requires. Merge commit, install both hosts first, Claude9 upgrade, Codex9 companion seal, full proof18/recall18/selected/process/cache checks and CI22/24/26.
- [ ] Write final REPORT and PROGRESS-CODEX evidence and limitations. Complete this goal after PR2 only; no PR3.

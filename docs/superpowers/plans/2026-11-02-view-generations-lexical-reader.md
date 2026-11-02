# View generation and lexical reader Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans inline. Existing user authorization approves the git-blob design and rollout through PR10; preserve the original native implementation and independent Codex PR-head review method.

**Goal:** §12 PR3의 checkout별 immutable generation·lexical reader·same-view CAS를 별도 비활성 candidate 형식에 구현한다.

**Architecture:** PR2 `BlobStore`를 확장하는 `laya_view_store.ViewStore`가 public candidate API를 제공한다. `laya_view_schema.py`는 generation DDL과 상태·seal guards, `laya_view_values.py`는 manifest/binding 값 계약, `laya_view_fts.py`는 세대별 FTS 구조·내용 검증과 기존 CJK/BM25 동등성을 소유한다. 운영 v6 reader/hook/route에는 연결하지 않는다.

**Tech Stack:** Python 표준 라이브러리, SQLite FTS5/rollback journal, PR1 capture, PR2 content/provenance, 기존 v6 FTS text normalization. 모델/새 dependency 없음.

**Spec:** `docs/plans/2026-11-01-memvane-db-git-blob-model.md` §3–6, §12 PR3, §14.

## Global Constraints

- 형식 `memvane-git-blob-candidate-v2`, app id `0x4d564231`, schema2. v1과 v6는 read-only preflight에서 bytes 변경 없이 거절한다. 이 PR에는 migration/activation/GC/vector fill이 없다.
- 보호3 `core/laya_local_embed_provider.py`, `core/laya_local_embed_worker.mjs`, `hooks/hooks-codex.json` 불변. Claude advisor 금지.
- 원문 body API 없음. source fingerprints/receipts는 caller-owned metadata claims다. seal은 구조·일관성 commitment이며 compiler 사실/원문 관측/운영 권한의 증명이 아니다. 검색 결과에 `evidence: commitment-only`, `owner_verified: false`를 표시한다.
- config/checkout/file observation은 검증 시점의 일치이며 filesystem/DB 원자 snapshot 주장이 아니다. 기존 candidate scope/policy/worktree binding을 유지한다.
- 세대당 wiki≤20,000, source≤20,000, wiki body 합계≤64MiB, card당 source≤64/spans≤256, query UTF8≤4096byte, query limit1..100, default deadline2초/max30초. 개별 blob4MiB/기존 PR2 bounds는 그대로다.
- Python `-B`·`PYTHONDONTWRITEBYTECODE=1`, 모든 검사 realpath 임시 HOME/guard/파일병렬1/유한timeout. 모델/검사/해시/백업·독립 리뷰 순차.
- v6 FTS tokenizer `porter unicode61`, path/title/body BM25 weights1.5/4.0/1.0, CJK spacing과 결정적 path tie-break를 유지한다. 전역 FTS topK→view filter 금지.
- baseline main9de80a3 tree53ba662와 PR2 finalhead563f0b6 tree가 동일하다. 해당 tree의 local full664/pass661/fail0/skip3 및 CI22/24/26success 근거를 재사용한다.

## Review Focus

1. 같은 blob의 다른 view/source trust를 빌리지 않는다. zero/held_missing/legacy_unbound/다른 SHA·worktree·branch·skipped source는 candidate 검색에서 제외하고 provenance는 고치지 않는다.
2. head ABA를 epoch로 잡는다. `(view,incarnation,head,epoch)` CAS와 prepare의 원래 capture/settings/manifest binding을 publish에서 같은 트랜잭션으로 확인한다.
3. pin의 membership/source/trust/FTS 모두 같은 sealed generation에 고정한다. head swap은 기존 pin 검색을 유지하고 checkout/settings 변화는 결과를 폐기한다.
4. FTS의 ordinal 소유·DDL·shadow·내용의 일대일 대응과 seal 후 수정 거절을 함께 증명한다. API 외 SQL은 지원하지 않지만 손상은 read/seal verifier가 거절한다.
5. unknown/released/다른 handle의 pin, partial generation, malformed manifest, query deadline/busy·중간 fault는 기존 head/검색 결과를 훼손하지 않는다.

## Task1: v2 generation schema와 strict FTS

**Files:** Create `core/laya_view_schema.py`, `core/laya_view_fts.py`, `test/laya-view-schema.test.mjs`, `test/fixtures/laya-view-schema-checks.py`; modify `core/laya_blob_schema.py`, `core/laya_blob_store.py` version constant usage, PR2 format assertions, checkout no-live guard allowlist.

**Interfaces:** `laya_view_schema.tables(text,sha,uuid,integer) -> dict`, `statements() -> list[str]`; `laya_view_fts.table(ordinal)`, `create(db,ordinal)`, `schema_rows(db)`, `assert_rows(db,ordinal,expected_rows,deadline=None)`, `storage_digest(db,ordinal,deadline=None)`, `proof(db,ordinal,rows,deadline=None)`; `ConnectionGuard(db).building(ordinal)` owns one commit/rollback scope through FTS sync, never nests. All candidate rw connections install its authorizer before their first SQL with `cached_statements=0`; no shadow triggers. schema2 all fixed tables retain exact manifest, dynamic FTS+shadows only for registered ordinal with exact expected schema.

- [x] Write schema RED cases for missing view module, schema2/v1 refusal unchanged, generation state/PK/FK/noREPLACE/sealed mutation, registered FTS ordinal ownership and bad DDL/extra/missing/shadow mutation. Synthetic SQLite capability check pins legal FTS shadow guards before relying on them.
- [x] Run new guarded Node suite on base; preserve expected failures before product files exist.
- [x] Implement schema2 and immutable generations/membership/sources/card_sources/seals; views unique(worktree,branch), head epoch/CAS structural guards, building→prepared→published transitions only. FTS ordinal is allocated by SQLite. Prepared/published FTS/shadows reject mutation with enforced supported mechanism; verifier checks exact rows/digest.
- [x] Run new schema + PR2 suites + PR1 no-live + existing CJK parity; commit independently testable schema/FTS deliverable.

## Task2: capture-bound manifests, prepare and publish

**Files:** Create `core/laya_view_values.py`, `core/laya_view_store.py`, `test/laya-view-generations.test.mjs`, `test/fixtures/laya-view-generations-checks.py`.

**Interfaces:** `ViewStore(BlobStore)`, `capture_view(worktree_id) -> handle`, `head(handle) -> {view_id,gen_id,epoch,incarnation}`, `prepare_generation(handle, manifest, *, expected_head) -> gen_id`, `publish_generation(handle, gen_id, *, expected_head) -> head`. Handles are store-issued frozen objects/registry-bound, never arbitrary caller view IDs. `manifest = {scan_id,policy_digest,revocation_epoch,complete,sources,members}`; source `{path,fingerprint_id,presence}`, member `{path,blob_sha,display_title,trust_digest}`. All keys exact; complete must be true. Eligibility is derived, never caller-supplied.

- [x] RED: source/worktree/branch/path/scan binding mismatch; two views same blob/different trust; zero/held/legacy/stale/skipped source; malformed fields/bounds; checkout/settings mutation; same-view stale base and ABA; other-view publication unaffected; building/prepare/publish injected fault rollback; repeated publish exact replay.
- [x] Implement bounded manifest normalization. Each source fingerprint belongs to handle worktree/branch/path/scan; present observed source head matches capture, held remains held. Attestation policy/blob and ordered original fingerprints/spans must agree. Historical card_sources keep their original metadata; current-source absence/change/unknown causes ineligible. Rebound must be explicit PR2 evidence, never implicit.
- [x] Prepare uses two bounded write transactions: first persist complete building members/source refs/FTS under the generation-specific capability through commit/FTS sync; then read stable physical FTS commitment, compute expected roots before assigning final `g-<uuid>-b<binding>` identity, verify manifest/seal/captured binding and commit prepared state. Unsealed staging rows never become reader heads; an interrupted build leaves the old head intact. Generation identity allocation must account for stable post-commit FTS proof without rewriting sealed metadata. No external scan/model work inside SQLite writer transaction.
- [x] Publish revalidates original handle/capture/settings and seal then same-view expected(head,epoch,incarnation) CAS/state transition/head increment atomically. Other views do not affect CAS; published rows remain immutable. Busy uses existing2second bounded refusal/retry code, no arbitrary kills.
- [x] Focused schema/generation/PR2/checkout suites pass; commit.

## Task3: pinned lexical reader, docs and release

**Files:** Extend view modules; create `test/laya-view-reader.test.mjs`, `test/fixtures/laya-view-reader-checks.py`, `docs/development-view-candidate.md`; update current module map and seven manifests main+patch.

**Interfaces:** `pin_view(handle) -> pin` (or bounded `view_preparing` when no head), `search(pin, fts_query, limit, *, path_prefixes=None, excluded_paths=None, deadline_seconds=2) -> rows`, `release(pin)`, `verify_views() -> True`. pin carries immutable generation and all seal roots in store-owned registry, while DB read transactions stay short. search returns path/blob/body/display_title/score/view_id/gen_id/evidence/owner_verified. `close()` invalidates pins. No deletion/GC API exists.

- [x] RED: A edit leaves B unchanged; pin survives same-view head swap and never mixes generations; in-flight checkout/settings switch discards result; arbitrary/released/cross-store pin rejected; view/trust/source/skip filtering before LIMIT; zero/held source hidden; CJK/path/title/BM25 parity with v6; fault/tamper/deadline/busy; legacy v6 bridge query still works unchanged.
- [x] Resolve current handle to head+seal/membership/source/trust/FTS in one read transaction. Keep only immutable snapshot references in pin registry; no long DB reader lock, no WAL, no ineligible cross-view fallback. Query uses same generation, verifies committed rows and returned blob bytes, joins only generation-owned metadata, filters before LIMIT, then rechecks checkout/settings before return.
- [x] Add 1,800-card/10view and >2,048-card synthetic budget/latency/DB-growth report without loading models. Enforce exact generation bounds; do not claim PR1 warm50ms target met (prior p95247.125ms remains a measured limitation unless new evidence improves it).
- [x] Document current candidate API/proof limits; preserve user docs interface and protected3. Version7 manifests to next main patch.
- [ ] Final focused/full and exact Git packaging, independent detached Codex Astra high read-only PR-head review; reproduce/fix concrete findings and narrow re-review under original handoff. Merge commit then both hosts install before upgrade loop. Claude9/Codex9 proof18/recall18/SE+rccar selected/no fallback/new Claude service/embed/no_pycache, exact-head CI22/24/26 allsuccess.
- [ ] REPORT/PROGRESS-CODEX/rollout ledger evidence complete; continue to PR4 under the active PR10 goal.

### Task2 slot and handle contract

`ViewHandle` is a frozen dataclass with `view_id,worktree_id,branch_key,head_oid,checkout_digest,policy_digest,incarnation,token`. A handle registry binds the exact issued object to original capture/settings/binding evidence. `head` has exactly `view_id,gen_id,epoch,incarnation` (none/0 before first publish). Non-Git observed sources with unknown tracked state can satisfy candidate eligibility; they never gain deletion authority.

`generation_slots` reserves SQLite ordinal/build UUID/view/metadata-only stage JSON+digest. Stage1 inserts the slot and generation-owned FTS under capability through commit; no generation row exists yet. Stage2 computes stable physical/lexical proof, assigns final gen ID, inserts full generation/membership/source/card-source/seal rows and prepares atomically. State is COALESCE(generation.state,building) for a registered slot. Interrupted slots are unservable staging, no implicit GC. Slot manifest contains no source or wiki bodies, only hashes/paths/titles/receipts and file/capture metadata.

Present source members require same worktree/branch/path/current scan/current head; held missing can retain last observation. All historical card source refs preserve original SHA/span/IDs; same-view fresh observations with same path/SHA/length do not rewrite provenance. Non-null foreign-worktree/branch attestations are refused; caller may supply null trust for unverified membership. Zero/held/stale/skipped/legacy-unbound evidence stays ineligible. Candidate result does not imply compiler truth/owner authority.

### Task2 verified boundary rules

Handle fields are compared with original registry capture, not merely frozen-object identity. A bounded fresh Git/settings capture is checked inside each final write transaction before commit/CAS; stat/settings file observations are checked there too. This is a verified observation point, not a filesystem/DB atomic snapshot. Source scans/models remain outside writer locks. Non-Git observed/unknown tracked metadata remains searchable as candidate evidence only.

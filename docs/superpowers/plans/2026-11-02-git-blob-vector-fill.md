# Git-blob shared vector fill Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans for the tightly coupled store/schema integration. The human explicitly authorizes subagents: delegate disjoint synthetic fixtures and the new content-fill modules after parent-confirmed RED. All real checks remain serialized by the parent. Use checkbox steps and the plan ledger; do not ask again about settled §14 decisions.

**Goal:** 별도 비활성 candidate에서 content별 임베딩 재사용, 유계 view fill cursor, 완전 vector coverage 봉인과 lexical 우선 검색을 제공한다.
**Architecture:** format/schema3만 새로 만들며 이전 candidate와 v6를 변환하지 않는다. ContentFiller는 기존 BlobStore의 불변 complete set을 사용하고 private content flock을 보유한 동안만 합성 callback을 실행한다. ViewStore는 고정 manifest의 durable cursor와 세대별 set digest 참조를 소유한다. 운영 route·hook·migration·GC는 연결하지 않는다.
**Tech Stack:** Existing Python stdlib SQLite/fcntl/struct, existing Node test wrappers. No new dependency or real model.
**Spec:** `docs/plans/2026-11-01-memvane-db-git-blob-model.md` §6/§10/§12 PR4/§14; existing `docs/development-blob-candidate.md`; handoff `context/memvane-pr4-{contract-proposal,test-map}.md` and guarded Astra advice `context/memvane-pr4-advice.txt`.

## Global Constraints
- Base main9f5421e/.43.32, clean managed review-coordinated-release checkout, branch feat/git-blob-vector-fill. Exact base tree equals full-tested6dd4aac: local670/666/0/4 plus actual npm-entry supplement2pass; CI22/24/26 each670/667/0/3. Reuse exact-tree baseline; new tests must show RED before implementation.
- Candidate schema3 exact manifest; v1/v2/v6 first readonly refusal without DDL/write connection/automatic migration. New format does not claim production reader/writer or compiler authority. Source fingerprints/receipts remain caller commitments.
- Protected3 unchanged: local embed provider, local embed worker, Codex hooks. No live data/model/provider/network in fixtures. Global guard, realpath temp HOME/runtime, files1, finite timeout. Hold/ownership/resource refusal remains binding.
- Same common Git dir and project sharing only. No raw source body API, runtime selection, deletion, GC, worktree unregister, bulk card rewrite, ANN or chunk truncation.
- Defaults8 members/2,000,000 UTF8 body bytes per fill step; explicit max_files1..64 and max_bytes1..64MiB. One body above the call's byte budget returns deferred without cursor advance; caller may explicitly enlarge budget up to existing MAX_BODY4MiB for that body. No implicit oversized-body exception. Existing MAX_CHUNKS1024, dimension1..2048, packed MAX_SET_BYTES16MiB remain.
- All callback input is canonical blob heading/body/synthetic path. Recipe/profile IDs do not prove callback implementation semantics. Callbacks execute outside SQLite transactions and must be bounded by their caller; elapsed checks do not preempt arbitrary Python callbacks.
- Exact float work is sum(chunk_count*dimension) for the pin's filtered eligible members; default explicit4,000,000, not a card cap. Finite shared query deadline. Budget/provider absence preserves lexical results; tamper/foreign pin/profile mismatch remains refusal.
- First candidate DB mutation of each fill call invokes a mandatory write_fence callback once. Lock-directory/file coordination can precede that fence on a cache miss; a complete cache hit creates neither. Completed unchanged job and per-content complete cache hit perform zero DB mutations/fences/providers. Pending cursor repair is progress: provider0, first-DB-mutation fence1. Set and cursor commits are separate; no crash-wide exactly-once inference claim.
- Every claim is cooperative store/filler-issued object identity plus internally retained acquired FD and named/root inode checks. Caller PID/TTL/nonce/FD number alone grants nothing. Same-UID arbitrary FD introspection/close+same-inode reopen is not an OS security guarantee. Never expose lease FD to callbacks, unlink locks, steal by TTL or kill processes.

## Review Focus
1. Two processes and two views of the same content coalesce normally; loser defers/rechecks without writes, crash releases OS lease, no cross-project reuse.
2. Replaced DB/root/lock, stale/cross-store/forged claim, callback-induced checkout change and partial/invalid output never advance a cursor or publish a generation.
3. Completed cache no-op is read-only; lost-cursor repair does not reembed and is fenced; SQL transaction never spans flock wait or a callback.
4. Coverage is zero or complete, profile/recipe/set digests and path→blob binding are sealed, later shared-cache growth cannot change an old pin.
5. Explicit float/deadline refusal retains the same pinned lexical view, and all candidate metadata remains commitment-only/owner_verified=false.

## Task1: Content lease, format3 foundation and complete fill
**Files:** Create `core/laya_blob_lease.py`, `core/laya_blob_fill.py`, `test/fixtures/laya-blob-fill-checks.py`, `test/laya-blob-fill.test.mjs`; modify `core/laya_blob_schema.py`, `core/laya_blob_store.py` and relevant schema expectations. Parent owns common schema/store edits; delegate owns only named new modules/fixtures.
**Interfaces:**
- `BlobStore.embedding_inputs(blob_sha, recipe_id, profile_id) -> list[str]`; shared internal generator also powers existing vector_input_sha/put validation. Exact `title: {heading or none} | text: {chunk}` strings.
- `ContentFiller(store).claim_content(blob_sha, recipe_id, profile_id)` context manager yields opaque claim; `validate_claim(claim)` refuses forged/stale/foreign claims. Default private sibling `<candidate filename>.content-locks` and content-key SHA lock files, mode0700/0600/nofollow/single-link/currentUID/canonical inode checks. Registry/FD stay internal.
- `ContentFiller(store).ensure_content(blob_sha, recipe_id, profile_id, *, chunker, embedder, write_fence, deadline_seconds=30) -> dict`: status cache_hit|filled, IDs, set_digest, chunk_count. Claim busy/error uses bounded BlobStoreError reason; no implicit retry loop. Deadline finite0<seconds<=300.
- chunker(document, recipe)->existing complete chunk rows; document is BlobStore.document, path `blobs/<sha>.md`. embedder(inputs, profile)->N×dimension finite nonzero numeric array; filler constructs seq/input SHA/little-endian float32, then uses existing complete atomic insertion. Output N<=1024 and packed bytes<=16MiB. Invalid/overflow/count/dimension/refusal leaves no partial embedding parent/children.
- No DB lease row is required: OS lock is execution ownership, later fill_jobs are progress only.
- [x] Write actual-format v1/v2/v6 no-write refusal, samebody/rename inputs, recipe/profile changes, two-process contention, inode/claim failures, crash release, no SQL transaction in callbacks, provider count/partial/NaN/zero/output-bound and cache no-op tests.
- [x] Parent runs guarded RED and records precise missing API/schema reasons; delegate code only after that signal.
- [x] Implement format3 header/check/manifests and shared inputs; preserve existing immutable digest/PK contracts. Capture exact schema2 fixture before updating expectations; no live schema fixture.
- [x] Implement acquired claim→cache recheck→first fence→complete chunks→callback outside TX→claim/DB recheck→whole embedding set commit. Cache validation uses existing digest/provenance APIs, not row count alone.
- [x] Focused new suite + blob/schema/set regressions pass; commit independently testable slice.

## Task2: Capture-bound bounded view fill
**Files:** Create `core/laya_view_fill.py`, `core/laya_fill_schema.py`, `test/fixtures/laya-view-fill-checks.py`, `test/laya-view-fill.test.mjs`; integrate `core/laya_view_store.py` and exact schema manifest.
**Interfaces:**
- `ViewStore.fill_status(handle, manifest, recipe_id, profile_id) -> dict(job_id,cursor,complete)` is readonly; absent job reports cursor0/completefalse. job_id commits incarnation/view/capture/manifest/recipe/profile. Target order is sorted eligible member path, duplicate content reuses the global content set.
- `ViewStore.fill_view(handle, manifest, recipe_id, profile_id, *, chunker, embedder, write_fence, expected_cursor, max_files=8, max_bytes=2000000, deadline_seconds=30) -> dict` includes job_id/cursor/complete/status/filled/cache_hits/deferred. expected_cursor is exact nonnegative integer CAS; stale cursor refuses. One per-call fence latch covers job metadata/content writes/cursor updates.
- Immutable job identity/target manifest metadata, monotonic cursor0..target_count, complete iff cursor==target_count; UPDATE guards forbid identity mutation/rewind/REPLACE. No raw body in job table. Empty target can be marked complete with one fenced initialization; completed repeat is readonly.
- [x] RED for defaults/explicit limits, oversized first member defer, restart, SQL busy, content commit followed by cursor failure, cached replay provider0, cursor CAS/manifest/view/recipe/profile binding, branch/settings change during callback and completed no-op0writes/fence0.
- [x] Implement short metadata/cursor TX, no callbacks/flock waits inside TX. Recheck capture/settings before each cursor advance; stale callback may retain valid immutable content cache but cannot progress stale view job. Each member's complete set commits before cursor, failure never marks member complete.
- [x] Focused fill/content + existing capacity/incremental regressions pass; commit.

## Task3: Complete coverage seal and pinned exact search
**Files:** Create `core/laya_view_vectors.py`, `test/fixtures/laya-view-vectors-checks.py`, `test/laya-view-vectors.test.mjs`; modify view schema/values/store/reader integration and developer candidate documentation.
**Interfaces:**
- `prepare_generation(handle, manifest, *, expected_head, vector_spec=None)` keeps lexical default. vector_spec exact `{recipe_id,profile_id}` requires complete valid sets for every eligible member. None produces zero vector rows. Stage/binding commit vector_spec and explicit mode; actual member_vectors references path/blob/recipe/profile/chunk+embedding digests. Partial/mixed/tampered coverage refuses before publish.
- Existing stage fields/version and roots incorporate coverage without modifying old files. New format3 reader never opens format2. `_stored`/seal verification compares actual vector rows; row triggers are immutable and building-only. Shared sets added later cannot alter any existing generation root/pin.
- `search_exact(pin, query_vector, limit, *, profile_id, max_float_work=4000000, deadline_seconds=2, path_prefixes=None, excluded_paths=None)` returns pinned wiki rows/score plus existing commitment-only fields. It filters view/path/eligibility before work/count/scoring, validates every referenced set/profile, and ranks cosine with deterministic path tie-break. No candidates from another view, no early chunk cutting.
- `search_hybrid(pin, fts_query, query_vector, limit, *, profile_id=None, max_float_work=4000000, deadline_seconds=2, path_prefixes=None, excluded_paths=None)` returns `{lexical, semantic, semantic_status}`. query_vector=None / lexical-only / explicit semantic budget or deadline defer leaves lexical intact; malformed input/tamper/wrong profile/pin refuses. One deadline covers lexical+semantic; no ANN/RRF behavior beyond exposing both ordered lanes.
- [x] RED for zero/full coverage, holes/mixed/digest spoof, titleless rename sharing, old pin during cache growth/head swap, stale sameviewCAS and other-view invariance, filtered work budget, count*dimension exact boundary, finite/dimension query validation, lexical preservation on semantic defer/deadline/provider absence.
- [x] Integrate coverage into pre-identity roots and actual insert/seal validation; publish no incomplete vector generation. Reuse existing vector validation/scoring contracts where compatible; no operational provider import.
- [x] Focused blob/view/vector/capacity/incremental/FTS/Hermes regressions and synthetic growth/work report pass; commit.

## Task4: Whole-PR verification and release
- [ ] Docs present current inactive APIs, proof limits, budgets and no automatic conversion/activation. Seven manifests monotonically .32→.33. Final committed tree full npm test under global guard/realpath HOME/files1; exact Git package22; retain base RED and failures.
- [ ] Exact PR-head detached gpt-6-astra/high/read-only review after all checks. Reproduce/fix concrete findings, focused/full as needed, narrow re-review. Latest-head CI22/24/26 success before merge commit; never direct main push/force/rebase/reset.
- [ ] Both hosts install first under coordinated bundle locks; Claude9 actual preferred owners; explicitly verify newer-version hello/request_adopt through normal API and actual new-Claude service before Codex9 companion. No fake version/reason/Python or same-version drain override.
- [ ] Full writeproof18, normal warm then original verifier18 (strict count/exit/stderr), latest SE/rccar selected/no fallback, actual new-Claude resident/embed, both7versions/no_pycache. Preserve cold warm fallback failures separately; no latency fix claim.
- [ ] REPORT + handoff progress/audit record implementation, synthetic provider evidence/bytes/work/growth, guard failures and remaining performance/activation gates. Continue PR5 bridge; do not complete overall goal until PR10.

## Rulings
- Content cache no-op and completed-job no-op are readonly; pending cursor repair is fenced progress, not a no-op. This resolves the prep's overbroad “all cached cursor writes zero” wording and follows guarded Astra advice; it preserves crash recovery without asserting impossible cursor progress.
- Default byte budget does not silently admit an oversized member. Explicit larger call budget supports a body up to existing4MiB, otherwise defer without cursor advance. One body+one set memory and bounded callback output remain; arbitrary callback execution is not preempted by this library.
- Existing user design/§14 and roadmap authorization cover these implementation choices. No new product preference or external action approval is required. Native fixture delegation is scoped; implementation/review/test jobs never race on shared files/guard.

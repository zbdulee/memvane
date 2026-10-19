# Laya source capacity: durable step-bounded source catalog (2026-10-19)

Status: independent slice only. The four initial files of the bounded
prototype were committed at `83bd2d2` (base `077f8186`) on
`feat/laya-capacity-batched-scan-20261019`. The current six-file candidate
and catalog revision extends `83bd2d2` locally:

* the strict full-proof oracle (`search_catalog_verified`, `CatalogPending`);
* the returned-candidate verification API (`GenerationSourceBinding`,
  `capture_generation_binding`, `CandidateQuery`,
  `search_candidates_verified`);
* review fixes: bound stage/publish no longer inherit the stat-hint cap
  and are not starved by a pending newer scan ("Bound indexing rule");
  an out-of-selection row poisons the query; `from_record` rejects
  non-string keys with `ValueError`.

This revision passed sequential guarded focused tests on 2026-10-19:
18 catalog Python cases and 15 candidate Python cases, each Node wrapper
1/1. Guard runs: catalog `63eae5de55434e228c6fc313670bddac`; candidate
`34292197de12492fbf542ee8f5317150` (rerun
`4d36a144a5794371a91a2e969a11bf94`).
Production sync, query, install and host-response paths are **not** wired to
any of it, and overall product integration is **not** done.

## Two separate correctness claims

The earlier revision merged two claims. They have different costs and must
be kept apart.

| | Generation correctness | Returned-candidate correctness |
|---|---|---|
| Object | one immutable published A-int generation | each row one query returns |
| Claim | the generation's full membership and bytes equal one COMPLETE catalog snapshot | the returned body is the file's *current* bytes, and they hash to the row's indexed hash |
| Evidence | multi-step scan + stage rehash + atomic publish (scan-time) | one no-follow re-read of that candidate at query time |
| Cost | O(N + B) per generation, spread over bounded steps | O(returned candidates) per query, bounded by `max_query_files` / `max_query_bytes` |
| Not claimed | that the live tree still equals it | that the ranking reflects the live corpus |

### Index lag vs. stale exposure

* **Index lag (allowed, documented).** A file created or changed after the
  generation was sealed cannot be found until async sync stages and
  publishes a newer generation. Ranking, BM25 statistics, vector neighbours
  and "which rows exist" all reflect the pinned generation. A query that
  returns `[]` because the new file is not indexed yet is a normal empty
  answer. Nothing here claims fresh corpus ranking.
* **Stale exposure (forbidden).** A returned row must never carry a body
  that differs from the file's current bytes. A row whose file was
  rewritten, deleted, replaced by a symlink, moved off-device or made
  unsafe is **dropped**. The check is a byte hash, never a stat signature
  and never a TTL/max-age. That includes a rewrite that keeps size and
  mtime.

### Where the impossibility argument applies

The earlier argument is still correct, but only for **whole-corpus
ranking freshness**. Without a trusted change feed or an immutable source
snapshot, proving that the *unreturned* files are unchanged at query time
costs Ω(B) reads plus a full enumeration on every query:

1. If the query reads fewer than B bytes, some byte position p is unread.
2. A writer can change p and keep size, mtime and even ctime.
3. Both worlds look identical to the query, so its ranking is stale in one
   of them.

The argument says nothing about **returned bodies**. The query reads every
byte it returns, so a returned body can be proved current by hashing it.
That cost is bounded by the number and size of returned candidates, not by
the corpus. The full-proof path is therefore kept only as an optional
audit/test oracle (below), not as the normal large-corpus query gate.

## Production query design: returned-candidate verification

### `GenerationSourceBinding` (immutable, explicit)

```python
GenerationSourceBinding(scope, generation, source_manifest,
                        catalog_scan_id, catalog_raw_digest, db_incarnation,
                        roots=((collection, canonical_root, dev, ino), ...))
binding.as_record()                       # JSON-ready dict
GenerationSourceBinding.from_record(rec)  # strict syntactic validation
capture_generation_binding(index, catalog, snapshot, generation)
```

* It is captured from a **COMPLETE, current** catalog snapshot.
  `capture_generation_binding` takes the root `(dev, ino)` identities that
  the snapshot itself listed (`catalog.root_nodes(snapshot)`), checks them
  against the live roots, computes the manifest with the same exclusions
  rule as stage, and reads `index.incarnation()`.
* `stage_from_catalog(..., binding=b)` and
  `publish_from_catalog(..., binding=b)` refuse (`index_stale`, nothing
  staged/published) unless `b` describes exactly that snapshot, manifest,
  generation, roots and incarnation. `binding=None` keeps the earlier
  strict behaviour for existing callers.

### Bound indexing rule (stage, publish, capture, `iter_documents`)

This is **scan-time indexing evidence, not live corpus freshness**.
Returned bodies are proved current per candidate at query time; nothing
here claims the live tree still equals the snapshot.

* **No stat-hint cap.** With an explicit binding, stage and publish do
  not run `verify_unchanged`, so `DEFAULT_VERIFY_STATS = 10,000` (or any
  `max_verify_stats`) is not a corpus cap on the bound path. With
  `binding=None` the hint still runs and still fails closed above its
  bound, as before.
* **A pending newer scan does not starve indexing.** The bound path takes
  the current COMPLETE snapshot through `catalog.require_complete()` and
  re-checks it with `require_current()` / `allow_pending=True`. A newer
  scan in `enumerate`/`hash`/`verify_*` writes only its own rows; the
  snapshot's rows are dropped only in the transaction that publishes the
  replacement. The binding must still match that snapshot's scan id,
  raw digest, manifest, roots and incarnation.
* **Fail closed and retry.** A blocked newer scan fails closed. So does a
  newer scan that fully publishes and replaces the snapshot, whether that
  happens before capture, between capture and stage/publish (binding
  mismatch), or during `iter_documents`. In the last case the dropped rows
  end the pages early and the final `require_current` check raises, so a
  partial list never reaches `LayaIndexA.stage` and nothing is deleted.
  The caller captures a new binding from the new snapshot and retries.
* **Rehash is unchanged.** Stage still re-reads and rehashes every member
  against the bound snapshot; any mismatch stages nothing.
* `require_servable()`, `status()['servable']` and the audit oracle keep
  the strict rule: a pending or blocked newer scan is `index_stale`.
* The caller passes the binding **explicitly** to every query. The query
  path never opens the catalog and never uses "the latest snapshot" for an
  older pinned generation. Construction validates syntax only. Filesystem
  identity is checked by each query.
* **Storage is not chosen here.** No schema migration is made. The parent
  picks the durable owner: a route/journal record, or an A-int change.

### Integration requirements (parent-owned; stage/publish crash windows)

1. **Persist before publish.** Write the binding durably (fsynced, keyed
   by `(scope, generation, db_incarnation)`) *before* `A-int publish`.
   Alternatively, add it atomically in the same A-int transaction once
   A-int owns it. Never publish first and persist later.
2. **Prepared-but-unpublished is harmless.** If a crash happens after the
   binding is written but before stage or publish, the binding is orphaned.
   No published head references it, and a query pins only published heads,
   so it is never served. It is GC-able by the owner.
3. **No binding, no serving.** A query on a published head without a
   matching durable binding must fail closed (`index_stale`). The binding
   cannot be rebuilt from a later catalog snapshot, because that snapshot
   may describe different bytes. Recovery is a fresh scan, a new
   generation and a new binding.
4. **Lookup by pinned generation.** The query side loads the binding for
   `view_seal(token)['gen']`, never for "current". `CandidateQuery` rejects
   any mismatch in scope, generation, manifest or incarnation.
5. **DB incarnation.** A recreated A-int file with the same path has a
   new `incarnation`. Bindings of the old incarnation are rejected.

### `CandidateQuery` / `search_candidates_verified`

```python
with CandidateQuery(index, token, binding,
                    max_file_bytes=1_000_000, max_query_bytes=5_000_000,
                    max_query_files=200) as q:
    wiki = q.search(fts, 5, collection_names=('wiki',))          # lexical phase
    vec  = q.phase(lambda n: vector.search_exact(token, qv, ..., limit=n,
                                                 path_prefixes=...), 5)  # any phase
    raw  = q.search(fts, 5, collection_names=('raw',))           # raw backfill
    stats = q.finish()   # rows are deliverable only if this returns
search_candidates_verified(index, token, binding, fts, limit, collection_names=None,
                           refill_factor=4, max_file_bytes=..., max_query_bytes=...,
                           max_query_files=...)
#   -> [{'scope','generation','path','hash','body','score','snapshot'}]
```

**Whole-query checks.** Any failure raises `index_stale` and returns zero
rows.

1. When the query opens:
   * a binding is present and is a `GenerationSourceBinding`;
   * the view-seal scope, generation and manifest equal the binding;
   * `index.incarnation()` equals the binding;
   * every bound root is canonical and opened no-follow, and its live
     `(dev, ino)` equals the recorded one. A missing root is
     `collection root unavailable`. A replaced root (another worktree or a
     re-created directory, even with identical bytes) is
     `collection root replaced`.
2. Exclusions are loaded once.
3. At `finish()`, the seal (including `rev_epoch`), the incarnation and
   every live root identity are checked again. An exclusion added
   mid-query, a root swapped mid-query or a recreated DB fails the whole
   query, even after phases returned rows.
4. A candidate row whose scope or generation differs from the pin, a row
   whose collection is outside the phase's selected names (or outside the
   bound roots), a malformed row, or an error from `fetch` is an A-int or
   caller contract violation. The phase raises `index_stale` (not
   `candidates_unverified`) and the session is poisoned: `finish()` then
   raises `candidate query already failed`, so rows verified by earlier
   phases cannot be delivered either.

**Per-candidate checks.** A failing candidate is *dropped*, never counted
as a source deletion, and never passed through. The checks run in order:

1. **Path vocabulary.** The path goes through A-int `_valid_relative_path`
   and port `_safe_rel` per component, must end in `.md`, and is held to
   `MAX_DEPTH`. Failures are dropped as `unsafe_path` (this covers hidden
   components). A vocabulary-valid path whose collection is not one of the
   phase's selected names fails the whole query (whole-query check 4).
2. **Exclusions.** A path or row-hash exclusion is dropped as `excluded`.
3. **Memo.** Each path has one verdict per query, shared by every
   lexical, vector, wiki and raw phase. A repeated path is not re-read; its
   memoized hash is compared with the new row's hash.
4. **Walk.** The file is reached under the held root fd one component at a
   time: `lstat` (`symlink`, `missing`, `crosses_device`), then
   `O_DIRECTORY|O_NOFOLLOW` open with a node check. **No directory is ever
   listed.**
5. **File.** The final component must be a regular, non-symlink file on
   the root device, within `max_file_bytes` (`file_budget`) and the
   remaining `max_query_bytes` / `max_query_files` (`query_budget`). It is
   opened `O_NOFOLLOW`, and the node and size are checked before reading.
   The read stops at `size + 1` bytes; the extra byte detects growth by
   content. `fstat` must be unchanged afterwards. Any mismatch is
   `changed_during_read`.
6. **Accept.** The buffer is decoded as strict UTF-8 first (otherwise
   `invalid_utf8`); that verdict, or `(sha256, text)`, is memoized per
   path. Then the SHA-256 of that buffer must equal the row hash
   (otherwise `hash_mismatch`). A non-UTF-8 file is therefore always
   `invalid_utf8`, whatever the row hash. The body is decoded from **the
   same buffer** that was hashed.

**Bounded refill and pending.** `phase(fetch, limit)` makes at most two
rounds:

1. Round 1 fetches `limit` rows.
2. Round 2 runs only if round 1 was full, dropped at least one row and
   left fewer than `limit` valid rows. It fetches
   `min(100, limit * refill_factor)`. 100 is A-int's
   `search_many`/`search_scored`/`search_exact` limit. Memoized paths are
   not re-read.
3. The outcome is one of:
   * some valid rows: up to `limit` of them, in A-int rank order;
   * A-int returned no candidates: `[]`, which is normal (index lag);
   * every candidate dropped: `CatalogPending` with zero rows:

```python
exc.detail == {'reason': 'candidates_unverified', 'scope', 'generation',
               'snapshot', 'limit', 'rounds', 'fetched': [sizes...],
               'candidates': distinct_dropped_paths,
               'dropped': {reason: count_of_distinct_paths}}
```

`finish()` returns query stats: `candidates`, `memo_hits`, `files_opened`,
`bytes_read`, `rounds`, and `dropped`, which counts occurrences per reason.

**What this does not fix.** These A-int costs are unchanged; see "Costs"
below:

* `LayaIndexA.search_many` still re-tokenizes every eligible body of the
  generation per query (`_generation_fts_rows_match`);
* vector `search_exact` still counts and scans every scoped chunk, under
  `MAX_CANDIDATES = 20_000` / `MAX_FLOAT_WORK`.

Candidate verification bounds only the *source* side of a query.

**Residual window.** The answer is exact as of each candidate's read and
the final check. A write after `finish()` (TOCTOU) is not seen, the same as
the port. A hard link inside a root that points outside it is accepted,
as in the port.

## Optional audit/test oracle: `search_catalog_verified`

It is kept unchanged for backward-compatible tests and optional audits.
Within one call it performs:

* one fresh full enumeration and byte hash of every root through the
  port's `scan_collections`;
* a raw-digest comparison with the snapshot;
* a manifest comparison with the seal.

Rows are served from the scanned buffers. Above
`max_proof_files`/`max_proof_bytes` (defaults 200 / 5,000,000) it raises
`CatalogPending(reason='proof_budget_exceeded')`. That happens before any
source read, stat hint or A-int search. This strict pending result is
**never** converted into normal rows. It is not the production query
gate: production uses the candidate path.

```python
exc.detail == {'reason': 'proof_budget_exceeded', 'scan_id': ...,
               'snapshot_files': N, 'snapshot_bytes': B,
               'max_proof_files': F, 'max_proof_bytes': M,
               'files_over': max(0, N-F), 'bytes_over': max(0, B-M),
               'requires': 'trusted_change_feed_or_immutable_source_snapshot'}
```

The oracle's sequence is:

1. The seal scope equals the catalog scope, then `require_servable()`.
2. Budget pre-check from snapshot metadata.
3. Exclusions are read once, and the catalog manifest must equal the seal.
4. The stat hint runs (it can reject, but never proves anything).
5. `scan_collections(roots, …, max_proof_files, max_proof_bytes)`.
6. The raw digest must equal the snapshot's, and the manifest must equal
   the seal.
7. `index.search`, with every row matched to the proven file.
8. `require_same` and an unchanged seal.

Unlike the candidate path, the oracle also treats a pending or blocked
newer scan as `index_stale`.

## Problem

`core/laya_index_source_port.py` proves source state by one synchronous full
scan per operation (stage, publish, every verified search). It holds every
file's bytes in memory and enforces `max_files=200` / `max_bytes=5_000_000`
as a **whole-corpus** cap. A corpus above either bound is permanently
`index_stale`. 200 files / 5 MB was a per-call work bound, not an agreed
product corpus limit. This slice moves *indexing* onto a per-step work
budget over a durable catalog. It also moves the *query* source cost onto
a per-returned-candidate budget.

## Catalog API (indexing side)

```python
catalog = LayaSourceCatalog(db_path, {"plans": "/abs/plans", "wiki": "/abs/wiki"},
                            scope, create=True)
catalog.request_scan()   # {'status': 'started'|'coalesced'|'busy', 'scan_id'}
catalog.step(max_files=200, max_bytes=5_000_000, max_stats=2000)
#   -> {'status': 'pending'|'deferred'|'complete'|'blocked'|'idle'|'busy',
#       'scan_id', 'phase', 'work': {...}, 'blocked'?, 'deferred'?, 'published'?}
catalog.status()           # servable/reason/current/active + backlog counts
snap = catalog.require_servable()   # raises IndexContractError('index_stale')
snap = catalog.require_complete()   # bound path: a pending newer scan is allowed
catalog.require_current(snap)       # still the current pointer (pending allowed)
excl = load_exclusions(index, scope)
catalog.manifest(snap, excl)        # == scan_collections(...).manifest for same bytes
catalog.root_nodes(snap)            # {collection: (dev, ino)} listed by the snapshot
catalog.iter_documents(snap, excl)  # re-reads + rehashes each member
#   manifest/root_nodes/iter_documents take allow_pending=True on the bound path
catalog.read_verified(snap, path, sha256)
catalog.verify_unchanged(snap, max_stats=10_000)  # stat HINT; binding=None only

binding = capture_generation_binding(index, catalog, snap, gen)
stage_from_catalog(index, catalog, gen, base=..., claim_digest=..., claim_epoch=...,
                   binding=binding)
publish_from_catalog(index, catalog, expected, gen, claim, claim_digest=...,
                     claim_epoch=..., binding=binding)
```

`servable` means only that no newer scan was requested. It is **not** a
freshness claim and is not consulted by the candidate query path or by
bound stage/publish (see "Bound indexing rule").

### Step work budget

* `max_files` work units: one directory listing or one file hash is one unit.
* `max_bytes` file bytes read and hashed, streamed (SHA-256 plus an
  incremental UTF-8 decoder). No whole-file or whole-corpus buffer is kept.
* `max_stats` stat verifications in the verify phases.
* One directory listing is atomic. Its `lstat` calls are bounded by the
  per-scan entry bound (`MAX_ENTRIES`), not by `max_stats`.

## Safety invariants

1. **Atomic full-membership generation.** `current` changes only in the
   transaction that marks a scan `complete`, which happens after every
   directory was listed and every file was hashed and stat-verified.
   A-int stage stores the whole view in one transaction. Publish CAS-binds
   the seal to the snapshot manifest. A crashed or failed step rolls back.
2. **No false delete.** Add, change and delete counts are computed only at
   publication, from two complete scans. A partial listing, a pending
   scan, a dropped query candidate, and either `CatalogPending` outcome
   delete nothing and never shrink the snapshot.
3. **Explicit backlog.** `status()['active']['backlog']` reports the known,
   pending and hashed dirs/files of the newest scan. Pending, deferred and
   blocked scans are visible, never silent.
4. **Returned bodies are byte-verified, never stat-verified.** A
   same-size/mtime rewrite of a returned candidate is caught by its
   current SHA-256. `verify_unchanged`, `status()['servable']` and any TTL
   are never acceptance evidence.
5. **Binding gate.** Without a matching `GenerationSourceBinding`
   (scope, generation, manifest, incarnation, live root identities) a
   query returns nothing.
6. **Scan-time mutation detection** (keeps a scan from publishing an
   inconsistent snapshot; not query-time freshness):
   * each directory's `fstat` signature is compared before and after
     `listdir`;
   * each file's signature must equal its enumeration signature before and
     after streaming, and the byte count must equal the size;
   * the verify phases re-check every directory and file signature;
   * directory chains are opened `O_NOFOLLOW|O_DIRECTORY` with recorded
     `(dev, ino)`.
7. **Preserved validation.** The catalog reuses the port's `_check_root`,
   `_open_root_fd`, `_safe_rel`, `_sig`, `_node`, `MAX_DEPTH`,
   `MAX_ENTRIES` and flags. Hidden entries are skipped. The scan is blocked
   by:
   * symlinks;
   * device crossing;
   * non-regular `.md` files;
   * unsafe or non-UTF-8 names;
   * exceeding the depth or entry bounds;
   * invalid UTF-8 content;
   * overlapping roots.

   The candidate path applies the same vocabulary per returned path.
8. **Stable namespaced identity.** Paths are `collection/relative.md`, and
   the manifest is `_digest(scope, [(path, sha256, eligible)])`, identical
   to the port's for the same bytes.
9. **Restart and duplicates.** Progress is committed per step. A
   restarted process resumes from the durable phase and cursor. Concurrent
   writers get `busy`. Duplicate requests coalesce. A request during a
   running scan chains one more scan.
10. **Oversize single file.** A file larger than one step's byte budget
    defers the scan, which stays pending, because stdlib `hashlib` state
    cannot be persisted. This is a work-unit constraint, not a product
    limit.

## Costs the parent must budget (A-int, unchanged by this slice)

* **Full-generation stage.** `stage_from_catalog` builds the full
  `documents` list, holding every body in memory, and `LayaIndexA.stage`
  inserts every row and builds FTS in one transaction. That is O(N + B)
  memory and write time per generation. There is no incremental stage.
* **Full-corpus FTS check per query.** Every `LayaIndexA.search` /
  `search_many` call runs `_generation_fts_rows_match`, which re-tokenizes
  every eligible body of the generation: O(N + B) CPU and memory per query.
  Candidate verification does **not** fix this.
* **Vector candidate cap.** `search_exact` scores every scoped chunk and
  fails above `MAX_CANDIDATES = 20_000` / `MAX_FLOAT_WORK`. Candidate
  verification does **not** fix this.
* **Candidate query.** Each query reads at most `max_query_files` files
  and `max_query_bytes` bytes (+1 growth-detection byte per file). It holds
  only those buffers.
* **Audit oracle.** Holds all proven bytes (≤ `max_proof_bytes`) per call.
* **Install initial cap.** `core/laya_product_install.py` still refuses an
  initial source above `MAX_INITIAL_FILES = 200` /
  `MAX_INITIAL_SOURCE_BYTES = 5_000_000`. This slice does not change it.

## Integration dependencies (parent coordination; not edited here)

1. **Durable binding owner** (required before any product query uses the
   candidate path). See "Integration requirements" above: persist before
   publish, lookup by pinned generation, fail closed when missing.
2. `core/laya_index_sync.py` / `laya_product_sync_worker.py`: within its
   lease, the owner worker:
   1. calls `catalog.step()`;
   2. on `complete`, calls `capture_generation_binding`;
   3. stages with the binding;
   4. persists the binding;
   5. publishes with the binding.

   On any failure nothing is published and the worker calls
   `request_scan()`.
3. `core/index_enqueue.py` / dirty queue / `file_change_queue.py`: each
   detected CUD calls `catalog.request_scan()`. This shortens index lag; it
   is not a freshness proof and is not required for stale-exposure safety.
4. `core/laya_index_runtime.py` `query`, `core/laya_host_response.py`
   `_v2_phase` / `query_selected_hybrid`: open one `CandidateQuery` per user
   query with the persisted binding for the pinned generation. Run the
   wiki, raw, lexical and vector phases through `search`/`phase` and call
   `finish()` before formatting. Treat `CatalogPending` as "no results for
   this phase, index pending". Never fall back to unverified A-int bodies.
5. `laya_index_vector.search_vector_verified` and `laya_qmd_cutover` still
   use full port scans. Moving them to `CandidateQuery.phase` is parent
   work.
6. **A-int capacity:** incremental stage and a per-query FTS integrity
   check that does not re-read every body.
7. **Install decision:** whether to lift the install-time initial cap and
   bootstrap by stepping the catalog.
8. `core/laya_index_source_port.py`: no change required. The catalog
   imports its private helpers, and also A-int `_valid_relative_path`.
   A later change should promote them to a public validation surface.

The catalog DB path must be installer-owned (next to the A-int DB, distinct
file) and bound to the same scope. Nothing here selects or creates it in
production.

## Acceptance criteria (synthetic tests; passed)

`test/fixtures/laya-source-catalog-checks.py` (18 tests, oracle and
catalog). Its search expectations **did change** relative to `83bd2d2`
in the strict-oracle revision (an over-budget corpus is now
`CatalogPending` with zero rows and no source read; the `ProofTests`
full-byte-proof cases were added). The candidate and review-fix
revisions do not edit it: every test there uses `binding=None`, whose
behaviour is unchanged. See that file. They cover:

* bounded steps over a >200 file / >5 MB corpus;
* oracle `CatalogPending` with no reads;
* budget boundaries;
* a partial rescan that deletes nothing;
* spoofed-signature rewrite, add and delete, all failing closed;
* add/update/delete published only on completion;
* blocked mutations;
* restart and crash resume;
* busy/coalesce;
* backlog draining to zero;
* validation;
* oversize defer;
* corrupt catalog.

New `test/fixtures/laya-source-candidate-checks.py` (temp source + temp A-int
only):

* **Same-size/mtime rewrite** of the returned candidate, with `_sig_text`
  patched to a constant: dropped as `hash_mismatch`, `CatalogPending` with
  zero rows. The restored bytes are served again.
* **Deleted candidate:** dropped as `missing`, `CatalogPending`. The
  catalog snapshot is unchanged (no false delete).
* **New file index lag:** a new matching file is undiscoverable, and the
  query returns a normal `[]` with zero fd-based `listdir` calls and no
  `scan_collections`. An *updated* indexed file is found by its old term
  and dropped.
* **Wrong root / worktree inode:**
  * swapping a byte-identical copy into the root path gives
    `collection root replaced`;
  * a missing root gives `collection root unavailable`;
  * a swap mid-query fails `finish()`.
* **Binding, seal and incarnation:**
  * a missing binding is rejected;
  * a wrong generation or scope (seal mismatch) is rejected;
  * a foreign or recreated DB incarnation is rejected;
  * a binding that does not match the snapshot is refused by stage and
    publish;
  * an exclusion added mid-query fails `finish()`;
  * `as_record`/`from_record` round-trips, and malformed records
    (including non-string keys) are rejected with `ValueError`;
  * capture succeeds while a newer scan is pending and fails once that
    scan has published.
* **Unsafe candidates:**
  * a symlinked file and a symlinked directory give `symlink`;
  * a device crossing, via patched `_lstat_at`, gives `crosses_device`;
  * a hidden path gives `unsafe_path`;
  * path and hash exclusions give `excluded`;
  * a foreign-generation row fails the phase and poisons `finish()`.
* **Out-of-selection row** (`test_out_of_selection_row_poisons_query`): a
  fetch returning another selected-out or unbound collection fails the
  phase with `index_stale` (not `candidates_unverified`), drops nothing,
  and poisons `finish()`.
* **Bound indexing under a pending scan**
  (`test_pending_scan_does_not_starve_bound_indexing`):
  * `binding=None` stage still fails `scan_pending`;
  * capture, stage and publish with a binding succeed from the current
    COMPLETE snapshot, and the query serves;
  * after the newer scan publishes, capture with the old snapshot and
    stage/publish with the old binding fail closed; a fresh capture
    retries;
  * a newer scan publishing *during* `iter_documents` (from a second
    catalog instance) fails stage with `catalog snapshot changed`;
  * a same-size rewrite of a member still fails the bound stage rehash.
* **No stat-hint cap on the bound path**
  (`test_bound_indexing_has_no_stat_hint_cap`): 10,001 synthetic
  metadata-only `scan_dirs` rows are inserted into a tiny snapshot (no
  large temp corpus). `binding=None` stage fails
  `freshness sweep exceeds max_verify_stats`; bound stage and publish
  succeed with `verify_unchanged` patched to raise.
* **Mixed valid/invalid plus bounded refill:** two fetch rounds of sizes
  `[2, 8]`, and only verified rows come back, in rank order. When every
  candidate is invalid, exactly two fetches are made and the result is
  `CatalogPending` with counts. `query_budget` and `file_budget` drops are
  also covered.
* **Large corpus (>200 files, >5 MB):** a candidate query reads exactly
  one 25,000-byte file, never lists a directory, and runs with the catalog
  closed.
* **Memoization:** a duplicate path across the lexical, vector-style
  (`search_scored`), wiki and raw phases is read once, and later phases
  are served from the memo.

## Honest limitations

* Ranking freshness is **not** claimed. New or changed files lag until async
  sync. A dropped top candidate can let a lower-ranked one through, and
  that ordering reflects the pinned generation.
* `max_file_bytes` defaults to 1 MB (and `max_query_bytes`/
  `max_query_files` to 5 MB/200). These are **arbitrary technical budgets**
  that keep per-query reads finite. They are not an agreed product cap.
  When a candidate exceeds one, it is dropped as `file_budget` or
  `query_budget`. It stays indexed and is not deleted, and lower-ranked
  valid candidates are still served. If no candidate of the phase survives,
  the phase raises `CatalogPending` (`candidates_unverified`) with those
  counts and zero rows. Only an explicit larger budget from the caller
  serves the file; no product limit has been chosen.
* Whole-corpus ranking is the pinned generation's (index lag). Candidate
  freshness is a separate claim: each returned body was hashed from current
  bytes at read time.
* The residual TOCTOU window after `finish()` remains, as with the port.
  A path memoized in an earlier phase is served from the bytes read then.
  It is not re-read in later phases of the same query.
* A-int's per-query full FTS check and vector `MAX_CANDIDATES` remain
  O(N + B) and capped respectively.
* No durable binding storage exists yet. Product callers must not use the
  candidate path until the parent provides it.
* Hidden entries change their directory's signature, so the stat hint
  fails closed until the next rescan (conservative; `binding=None`
  indexing only, since the bound path does not run the hint).
* Bound stage cannot see files added after the snapshot (it rehashes
  members only). That is index lag, closed by the next scan and
  generation, never stale exposure.
* A single directory listing is not split across steps.
* Python 3.9-compatible syntax and the stdlib only. POSIX `dir_fd` support
  is required.

## Test commands (shared guard; both passed sequentially on 2026-10-19)

```bash
npm run test:focused -- test/laya-source-catalog.test.mjs    # 18 unittests
npm run test:focused -- test/laya-source-candidate.test.mjs  # 15 candidate unittests
```

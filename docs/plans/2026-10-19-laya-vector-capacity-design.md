# Laya vector capacity path: design (2026-10-19)

Status: fill step and seal implemented locally (sections 1 and 2); worker,
host response and bundle (sections 3 and 4) are not. Tests:
`test/fixtures/laya-vector-capacity-checks.py` (13 cases). This is item 7 of the
follow-up list and the prerequisite for wiring the product sync worker
(item 4), host response (item 5) and bundle `ARTIFACTS` (item 6) to the
capacity path from `docs/plans/2026-10-19-laya-source-batched-scan.md`.

## Why the other items wait for this

`core/laya_product_sync_worker.py:311` calls
`sync_full(..., vector_batch=batch, require_vector=True)`. The selected
product route always requires a vector seal. `sync_step(require_vector=True)`
returns `deferred`/`vector_capacity_path_unavailable`
(`core/laya_index_sync.py:388`). If the worker switched to `sync_step` now,
every product sync would defer and the selected route would stop updating.
So item 4 has to wait for a vector path, and items 5 and 6 only make sense
in the same change as item 4.

The capacity modules are imported lazily, only on the new paths
(`laya_index_runtime.py:122` when `catalog_path` is given,
`laya_index_flow.py:109` inside `_run_bound_leased`,
`laya_index_sync.py:391` inside `sync_step`). The current installed path
never imports them, so PR #18 does not break an installed bundle that lacks
the two new files.

## Current vector path and its capacity limits

`prepare_candidate_batch_off_hook` (`laya_index_sync.py:44`):

1. full port scan (`_scan`) that reads every source body;
2. embeds every eligible `(sha256, title)` that has no cached chunk set;
3. holds **all** chunk vectors in one in-memory `batch`;
4. full port scan again to check the manifest did not move;
5. `_validated_batch` checks coverage against the scan.

`sync_full` then stages the generation and `_prepare_vector` inserts the
chunk sets and calls `seal_generation`.

The cost is O(N + B) reads twice, all vectors in memory, and a model run
whose length is set by the corpus in one admission. That is what this
design removes.

## Facts the design rests on

* `LayaVectorIndex.insert_chunk_set` (`laya_index_vector.py:333`) commits
  one `(hash, title, profile)` chunk set and its completeness seal in one
  transaction. It is idempotent: an identical replay returns the cached
  count, a different one raises `vector_chunk_conflict`.
* `cached_chunk_set` (`:313`) returns the sealed rows or `None`.
* `seal_generation` (`:379`) reads the **prepared** generation's eligible
  membership from the DB, requires a complete cached chunk set for every
  member (`vector_coverage_incomplete` otherwise), writes
  `gen_vector_rows` and the seal, and is idempotent per generation. It
  needs no model and no source read.
* The chunk-set cache is keyed by content hash and title, not by
  generation, so work done for one snapshot is reused by the next one for
  every unchanged file.
* A `vector_chunks` row requires its `content` row (trigger in
  `laya_index_a.py`, `NOT EXISTS (SELECT 1 FROM content WHERE hash=...)`).
  Content is normally written by stage, so the fill writes it first with
  the new `LayaIndexA.ensure_content`. Stage accepts an existing row with
  the same body (`hash_content_conflict` otherwise), so nothing a later
  stage writes changes. A content row with no membership is served by no
  view. Content is immutable and has no GC, so a file that changes before
  it is staged leaves its old content row, as old generations already do.

## Design

Split the vector work into a model-bound **fill** step and a model-free
**seal** step.

### 1. `vector_fill_step` (model calls, bounded)

```
vector_fill_step(runtime, provider, *, max_files, max_bytes,
                 should_yield) -> dict
```

* Reads the catalog's current COMPLETE snapshot (`require_complete`).
  No snapshot means `pending`/`no_snapshot`.
* Lists members as `(path, sha256, eligible)` without reading bodies, in
  path order. `LayaSourceCatalog.manifest` only returns the digest, so this
  needs a new read-only method (`members(snapshot, exclusions, after=,
  limit=)`) built on the same `scan_files` query and eligibility rule as
  `manifest` (`laya_source_catalog.py:899`), so the two cannot disagree. It
  resumes from a durable cursor in the catalog DB keyed by
  `(snapshot seq, profile_id)` (`vector_fill`/`set_vector_fill`). The
  table is created on first write with `CREATE TABLE IF NOT EXISTS`; the
  catalog checks only `application_id`/`user_version`, so the schema
  version is unchanged and an older catalog file reads as no progress. A
  write drops rows of other snapshots. A new snapshot or profile restarts
  the cursor.
* For each eligible member with no `cached_chunk_set` for
  `(sha256, qmd_markdown_title(text, path), profile_id)`:
  `read_verified(snapshot, path, sha256)` (one body in memory), compute the
  title, embed, then `insert_chunk_set` (durable per file).
* Stops at `max_files` embedded files, `max_bytes` read bytes, or when
  `should_yield()` is true (`_higher_priority_wake`). The cursor advances
  only past files whose chunk set is committed.
* A member whose bytes no longer match (`read_verified` fails) is skipped
  and counted as `changed`, and a rescan is requested. The next snapshot
  will carry the new hash.
* Answers: `pending`/`vector_backlog` with `remaining`, or
  `complete` when every eligible member of the snapshot is cached.
* Holds at most one body plus one file's chunk vectors in memory.

The title has to be computed from the body, so a cache check costs one
verified read per eligible file. Within one snapshot the cursor keeps that
to once per file. A new snapshot re-reads every eligible file once (bounded
per step by `max_bytes`) but embeds only changed content. A title memo was
considered and not built.

### 2. Seal inside `sync_step(require_vector=True)`

`sync_step` keeps its order (claims, capture, stage, publish). With
`require_vector=True`:

* Before capture, check the fill state for the snapshot. If it is not
  `complete`, return `pending`/`vector_backlog` and do not stage. No
  generation is staged that cannot be sealed. Exception: while a newer
  scan is pending, `sync_step` steps that scan instead. Without this, a
  member that changed after the snapshot stops the fill
  (`source_changed`) and the gate stops the scan, and neither moves
  (found by the changed-member test).
* After the bound streaming stage, call `seal_generation(gen, profile_id)`
  before `before_publish` and publish. This is DB-only.
* If the seal raises `vector_coverage_incomplete` (fill state says done but
  a chunk set is missing), do not publish: the claim is `replan`
  (`index_stale`), and `sync_step` requests a rescan as for any replan. A
  member that changed is normally caught earlier, by the stage re-read.
* A receipt that is ACKed without re-staging must carry a seal with the
  same profile (as on the legacy path).
* A head that already serves the snapshot is `unchanged` without a seal
  check. A lexical-only bound head is not repaired here and fails closed
  on vector queries.
* The profile comes from the runtime (the worker's reviewed provider
  profile), never from the queue.

### 3. Worker loop (`laya_product_sync_worker.run`)

```
with runtime(catalog_path, binding_store_path):
    loop until a terminal answer or should_yield():
        r = sync_step(runtime, require_vector=True, ...)
        if r is pending/vector_backlog:
            start the embed service once (same admission and attestation
            checks as today), then vector_fill_step(...)
        elif r is pending (scan/claims): continue
        else: return r
```

* The embed service starts only when there is a backlog. A sync with no new
  content does no model load.
* Accept `pending` and `blocked` as results (today only
  `indexed`/`unchanged`/`deferred` pass `run()`'s result check).
* Keep every existing proof check (`verify_selected_route` before and after,
  `cas_mark_new_write` inside `before_publish`).

### 4. Host response and bundle (implemented)

* `serve_selected` opens the capacity runtime (all collection roots, both
  stores) only when `capacity_store_paths(dbPath)` finds both stores; the
  same helper gates the worker. Otherwise the runtime is unchanged.
* `query_selected_v2` opens one `open_pinned_candidate_query` per user query
  (fails closed when the pinned generation has no stored binding), passes it
  to both the wiki and the raw phase, and delivers rows only after
  `finish()`. It is closed on every path.
* `_v2_phase` with a candidate query skips the per-query
  `scan_collections` of every root. It does **not** use `CandidateQuery.phase`
  per query: the host batches the DF probes and the lexical queries into one
  `search_many`, so the generation's full FTS check runs once; one `phase`
  per query would repeat it for every probe. Instead the new
  `CandidateQuery.verify_rows` checks the rows of that one batch (same
  per-row and poisoning rules as `phase`, no refill round, no
  `CatalogPending`). Vector rows from `search_exact` go through the same
  verifier. Rows are reshaped to the host's `_ROW_KEYS`.
* Difference from the full-scan path: a candidate whose file changed after
  sync is dropped and the query continues. The full-scan path fails the
  whole query on the same change.
* Bundle: the three capacity modules are in `laya_product_bundle.ARTIFACTS`
  and the full-plugin required files; the A-int package carries the two the
  flow needs.

## Limits that remain

* `seal_generation` still walks the whole generation in one transaction and
  is capped at `MAX_CANDIDATES` (20,000) rows. That is item 9 (A-int
  capacity), not this item.
* `search_exact` still scores every scoped chunk per query.
* `laya_qmd_cutover` still uses a full port scan. Moving it is a separate
  change.
* The first fill pass reads every eligible file once to get titles.

## Tests to write (synthetic, fake provider, no model)

1. Fill resumes across steps: N files, `max_files=2`, call until
   `complete`; each file embedded exactly once (fake provider counts calls).
2. Unchanged files are not re-embedded after a rescan that changes one file;
   exactly one embed call.
3. A file changed after fill and before stage: seal fails, nothing is
   published, a rescan is requested, and the next fill embeds only that file.
4. `should_yield` true mid-step: cursor stops after the last committed file.
5. Crash between `insert_chunk_set` and cursor advance: replay is
   idempotent (no `vector_chunk_conflict`, no double count).
6. `sync_step(require_vector=True)` with a complete fill publishes a sealed
   generation; `verify_generation_seal` passes.
7. Worker: no backlog means no embed service start.
8. Worker accepts `pending`/`blocked`; existing proof-change checks still
   raise.
9. Memory: peak held bodies is one (instrument the fake catalog reads).

## Decisions taken while implementing

* Fill cursor: catalog DB (same scope as the snapshot).
* A changed member stops the step and requests a rescan at once
  (`pending`/`source_changed`); it is not skipped.
* One fill step defaults to 8 embedded files and 2 MB read
  (`DEFAULT_FILL_FILES`/`DEFAULT_FILL_BYTES`). These are technical defaults,
  not a product cap.
* Contract change: `sync_step(require_vector=True)` now needs
  `vector_profile_id` (`TypeError('vector_profile_required')` without it)
  instead of returning `deferred`/`vector_capacity_path_unavailable`.

## Open for review

* `ensure_content` writes content outside a stage. `LayaIndexA.inspect`
  only requires `content` and `content_fts` to match, and `ensure_content`
  writes both; the fill test runs `inspect()` before any stage. Other
  readers of `content` should still be reviewed.
* Worker loop (section 3) is not written; the test helper `sync()` is the
  intended shape.

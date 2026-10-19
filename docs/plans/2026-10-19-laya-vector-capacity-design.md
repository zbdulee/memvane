# Laya vector capacity path: design (2026-10-19)

Status: design only. Nothing here is implemented. This is item 7 of the
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
  resumes from a durable cursor stored
  in the catalog DB and keyed by `(snapshot seq, profile_id)`. A new
  snapshot or profile resets the cursor.
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
verified read per eligible file on the first pass. To keep later passes
cheap, the step records `(sha256, title)` for each member as it goes; a
member whose `sha256` is unchanged in the next snapshot reuses the
recorded title without a read.

### 2. Seal inside `sync_step(require_vector=True)`

`sync_step` keeps its order (claims, capture, stage, publish). With
`require_vector=True`:

* Before capture, check the fill state for the snapshot. If it is not
  `complete`, return `pending`/`vector_backlog` and do not stage. No
  generation is staged that cannot be sealed.
* After the bound streaming stage, call `seal_generation(gen, profile_id)`
  before `before_publish` and publish. This is DB-only.
* If the seal raises `vector_coverage_incomplete` (a member changed between
  fill and stage, or the cache was damaged), do not publish. Request a
  rescan and return `pending`/`vector_backlog`. The prepared generation is
  handled like any other unpublishable bound generation.
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

### 4. Host response and bundle (items 5 and 6, same change as item 4)

* `query_selected_hybrid`/`_v2_phase` open one
  `open_pinned_candidate_query` per user query and run the wiki, raw,
  lexical and vector phases through it, then `finish()`.
  `CatalogPending` means "no rows for this phase, index pending".
* Vector phase: `search_exact` still scores the pinned generation's rows
  (bounded by `MAX_CANDIDATES`). Returned candidates go through
  `CandidateQuery.phase` so their bodies are verified like lexical ones.
* Add `laya_source_catalog.py` and `laya_index_capacity.py` to
  `laya_product_bundle.ARTIFACTS` and `laya_package.ARTIFACTS`. PR #17 also
  changes `laya_product_bundle.py` and the bundle checks, so this lands
  after #17 or is rebased onto it.

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

## Open questions for review

* Where the fill cursor lives: catalog DB (proposed, same scope as the
  snapshot) or the binding store.
* Whether a member skipped as `changed` should request a rescan
  immediately or wait for the fill pass to finish.
* Default `max_files`/`max_bytes` for one fill step, given that one embed
  call per file is the expensive part.

# Laya vector capacity path: design (2026-10-19)

`core/laya_vector_capacity.py` implements the fill steps, and
`sync_step(require_vector=True)` seals the generation before publish. The
product sync worker and the selected host response use this capacity path
when both capacity stores exist next to the A-int DB
(`laya_index_runtime.capacity_store_paths`). Without them the worker keeps the
full-batch path below (`sync_full` with `prepare_candidate_batch_off_hook`).

## Full-batch vector path (no capacity stores)

`prepare_candidate_batch_off_hook` (`laya_index_sync.py`):

1. full port scan (`_scan`) that reads every source body;
2. embeds every eligible `(sha256, title)` that has no cached chunk set;
3. holds **all** chunk vectors in one in-memory `batch`;
4. full port scan again to check the manifest did not move;
5. `_validated_batch` checks coverage against the scan.

`sync_full` then stages the generation and `_prepare_vector` inserts the
chunk sets and calls `seal_generation`.

The cost is O(N + B) reads twice, all vectors in memory, and a model run
whose length is set by the corpus in one admission. The capacity path below
avoids this.

## Facts the design rests on

* `LayaVectorIndex.insert_chunk_set` (`laya_index_vector.py`) commits
  one `(hash, title, profile)` chunk set and its completeness seal in one
  transaction. It is idempotent: an identical replay returns the cached
  count, a different one raises `vector_chunk_conflict`.
* `cached_chunk_set` returns the sealed rows or `None`.
* `seal_generation` reads the **prepared** generation's eligible
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
  path order. `LayaSourceCatalog.manifest` only returns the digest, so the
  fill uses the read-only `members(snapshot, exclusions, after=, limit=)`,
  built on the same `scan_files` query and eligibility rule as `manifest`,
  so the two cannot disagree. It
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
* A member whose bytes no longer match (`read_verified` fails) stops the
  step with `pending`/`source_changed`, and a rescan is requested. The next
  snapshot carries the new hash and restarts the fill.
* Answers: `pending`/`vector_backlog` with `remaining`, or
  `complete` when every eligible member of the snapshot is cached.
* Holds at most one body plus one file's chunk vectors in memory.

The title has to be computed from the body, so a cache check costs one
verified read per eligible file. Within one snapshot the cursor keeps that
to once per file. A new snapshot re-reads every eligible file once (bounded
per step by `max_bytes`) but embeds only changed content.

### 2. Seal inside `sync_step(require_vector=True)`

`sync_step` keeps its order (claims, capture, stage, publish). With
`require_vector=True`:

* Before capture, check the fill state for the snapshot. If it is not
  `complete`, return `pending`/`vector_backlog` and do not stage. No
  generation is staged that cannot be sealed. Exception: while a newer
  scan is pending, `sync_step` steps that scan instead. Without this, a
  member that changed after the snapshot stops the fill
  (`source_changed`) and the gate stops the scan, and neither moves.
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

### 3. Worker loop

With capacity stores, the product sync worker repeats `sync_step(...,
require_vector=True)` and runs `vector_fill_step` (and
`pending_vector_fill_step` while a scan is still enumerating) when the step
reports a vector backlog. `sync_step` never loads a model; the embed
service starts on the first embed call of a fill step, so a sync with no
new content loads no model.

### 4. Host response and bundle

* `serve_selected` opens the capacity runtime only when
  `capacity_store_paths(dbPath)` finds both stores; the same helper gates the
  worker.
* Selected recall is wiki-only. It pins one generation and opens one
  `open_pinned_candidate_query` per user query (fails closed when the pinned
  generation has no stored binding). The host batches the DF probes and the
  lexical queries into one `search_many`, so the generation's full FTS check
  runs once, and checks that batch and the `search_exact` vector rows with
  `CandidateQuery.verify_indexed_rows`: published content joined to eligible
  membership is hash-checked against the row hash without reading current
  source files. Rows are delivered only after `require_returned_verified()`
  and `finish()`, which recheck the seal, incarnation, exclusions and live
  root identities.
* A source file that changed after the last publish does not drop its row;
  the last completed wiki snapshot stays visible until the sync worker
  publishes a replacement (index lag). The selected path without a binding
  store still runs a per-query `scan_collections` of every root.
* Bundle: the three capacity modules are in `laya_product_bundle.ARTIFACTS`
  and the full-plugin required files; the A-int package carries the two the
  flow needs.

## Limits that remain

* `seal_generation` walks the whole generation in one transaction. It
  streams rows (`MAX_GENERATION_SEAL_ROWS = None`), so it has no
  whole-generation row bound; `MAX_CANDIDATES` (20,000) bounds one
  document's chunk set and explicit query budgets.
* `search_exact` still scores every scoped chunk per query, bounded by the
  hook deadline and any explicit caller budget.
* `laya_qmd_cutover` v1/v2 ledgers bind a candidate published by a full port
  scan. A v3 binding (an initial build sealed on the capacity path) is proved
  from the stored binding, catalog snapshot and vector seal without scanning
  the source tree.
* The first fill pass reads every eligible file once to get titles.

## Implementation defaults

* Fill cursor: catalog DB (same scope as the snapshot).
* A changed member stops the step and requests a rescan at once
  (`pending`/`source_changed`); it is not skipped.
* One fill step defaults to 8 embedded files and 2 MB read
  (`DEFAULT_FILL_FILES`/`DEFAULT_FILL_BYTES`). These are technical defaults,
  not a product cap.
* `sync_step(require_vector=True)` requires `vector_profile_id`
  (`TypeError('vector_profile_required')` without it).

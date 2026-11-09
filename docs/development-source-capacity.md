# Laya source capacity: durable step-bounded source catalog (2026-10-19)

`core/laya_source_catalog.py` (durable step-bounded catalog, `CandidateQuery`,
audit oracle) and `core/laya_index_capacity.py` (durable binding sidecar)
form the product capacity path. `laya_index_sync.sync_step` indexes through
them, the product sync worker uses that path when both capacity stores exist
next to the A-int DB (`laya_index_runtime.capacity_store_paths`), the selected
host response opens one pinned candidate query per user query, and both
modules ship in the product bundle. Without the capacity stores the worker
and host keep the full port-scan path (`core/laya_index_source_port.py`).

## Two separate correctness claims

Generation correctness and returned-candidate correctness have different
costs and are kept apart.

| | Generation correctness | Returned-candidate correctness |
|---|---|---|
| Object | one immutable published A-int generation | each row one query returns |
| Claim | the generation's full membership and bytes equal one COMPLETE catalog snapshot | the row belongs to the pinned bound generation and selection, and its body hashes to its indexed hash |
| Evidence | multi-step scan + stage rehash + atomic publish (scan-time) | indexed mode: hash of the published body, no source read; re-read mode: one no-follow re-read of the candidate's current bytes |
| Cost | O(N + B) per generation, spread over bounded steps | O(returned candidates) per query; re-read mode is bounded by `max_query_files` / `max_query_bytes` |
| Not claimed | that the live tree still equals it | that the ranking reflects the live corpus |

Returned-candidate verification has two explicit modes:

* **Indexed mode (selected wiki recall).** `laya_host_response.py` checks the
  rows of its batched `search_many` and `search_exact` calls with
  `CandidateQuery.verify_indexed_rows`: immutable published content joined to
  eligible membership is hash-checked without reading current source bytes.
  Binding, seal, incarnation, exclusions and root identities still fail
  closed. A source file changed after the last publish is served from the
  last completed wiki snapshot until the sync worker publishes a replacement.
* **Re-read mode (audit and `LayaIndexRuntime.query_bound`).** `phase`,
  `verify_rows` and `search_candidates_verified` re-read each returned
  candidate's current bytes no-follow and drop a row whose bytes changed.
  No host hook uses this mode.

### Index lag vs. stale exposure

* **Index lag (allowed, documented).** A file created or changed after the
  generation was sealed cannot be found until async sync stages and
  publishes a newer generation. Ranking, BM25 statistics, vector neighbours
  and "which rows exist" all reflect the pinned generation. A query that
  returns `[]` because the new file is not indexed yet is a normal empty
  answer. Nothing here claims fresh corpus ranking.
* **Stale exposure (forbidden in re-read mode).** In re-read mode a returned
  row never carries a body that differs from the file's current bytes. A row whose file was
  rewritten, deleted, replaced by a symlink, moved off-device or made
  unsafe is **dropped**. The check is a byte hash, never a stat signature
  and never a TTL/max-age. That includes a rewrite that keeps size and
  mtime. In indexed mode the returned body is the published snapshot's body,
  which can lag the file (index lag).

### Where the impossibility argument applies

The impossibility argument holds only for **whole-corpus ranking
freshness**. Without a trusted change feed or an immutable source
snapshot, proving that the *unreturned* files are unchanged at query time
costs Ω(B) reads plus a full enumeration on every query:

1. If the query reads fewer than B bytes, some byte position p is unread.
2. A writer can change p and keep size, mtime and even ctime.
3. Both worlds look identical to the query, so its ranking is stale in one
   of them.

The argument says nothing about **returned bodies**. In re-read mode the
query reads every byte it returns, so a returned body can be proved current by hashing it.
That cost is bounded by the number and size of returned candidates, not by
the corpus. The full-proof path is therefore kept only as an optional
audit/test oracle (below), not as the normal large-corpus query gate.

## Candidate query design

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
  generation, roots and incarnation. `binding=None` keeps the strict
  unbound behaviour.

### Bound indexing rule (stage, publish, capture, `iter_documents`)

This is **scan-time indexing evidence, not live corpus freshness**. Nothing
here claims the live tree still equals the snapshot.

* **No stat-hint cap.** With an explicit binding, stage and publish do
  not run `verify_unchanged`, so `DEFAULT_VERIFY_STATS = 10,000` (or any
  `max_verify_stats`) is not a corpus cap on the bound path. With
  `binding=None` the hint runs and fails closed above its bound.
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
* **Storage.** No A-int schema migration is made. A standalone SQLite
  sidecar (`GenerationBindingStore`, below) is the durable owner, and the
  generation name `g-<uuid32>-b<digest64>` commits to the binding.
  `sync_step`, `query_bound` and the selected host response use both.

### Binding persistence rules (stage/publish crash windows)

1. **Persist before publish.** Write the binding durably (fsynced, keyed
   by `(scope, generation, db_incarnation)`) *before* `A-int publish`.
   Alternatively, add it atomically in the same A-int transaction once
   A-int owns it. Never publish first and persist later. On the integrated
   path `publish_bound` enforces it: it refuses (`index_stale`, nothing
   published) unless the store already holds exactly this binding. A direct
   `publish_from_catalog` caller is still unchecked; such a head only fails
   closed at query time (requirement 3).
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
   any mismatch in scope, generation, manifest or incarnation. The pinned
   name must be a bound name whose digest equals the stored binding's;
   a legacy unsuffixed head fails closed.
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

Whole-query checks apply to both modes. The per-candidate checks and the
bounded refill below are the re-read mode; `verify_indexed_rows` instead
drops only unsafe-path and excluded rows and fails the whole query on a body
hash mismatch.

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

**What this does not fix.** Candidate verification leaves these A-int costs
in place; see "A-int and query costs" below:

* `LayaIndexA.search_many` re-tokenizes every eligible body of the
  generation per query (`_generation_fts_rows_match`);
* vector `search_exact` scans every scoped chunk, bounded by the deadline
  and explicit caller budgets (`max_candidates`, `max_float_work`).

Candidate verification bounds only the *source* side of a query.

**Residual window.** The answer is exact as of each candidate's read and
the final check. A write after `finish()` (TOCTOU) is not seen, the same as
the port. A hard link inside a root that points outside it is accepted,
as in the port.

## Durable binding sidecar (`core/laya_index_capacity.py`)

### Storage design

The sidecar is SQLite: one primary-key lookup per query (`EXPLAIN QUERY
PLAN` is asserted to be `USING PRIMARY KEY`), no whole-store read on the
query or write path and no byte cap. The binding is committed into the
**generation name** that A-int already stores immutably, so no A-int schema
change is needed and a writer who recomputes the sidecar cannot rebind an
existing generation (below).

### Bound generation names

```
generation = 'g-' + nonce32 + '-b' + binding_digest(binding)       # 100 chars
binding_digest(b) = sha256(b'laya.generation-name-binding.v1\0'
                           + canonical(b.as_record() minus 'generation'))
```

* `canonical` is `json.dumps(sort_keys=True, separators=(',', ':'),
  ensure_ascii=True, allow_nan=False)`.
* `nonce32` is `uuid4().hex`, so two generations with identical bindings
  still get different names.
* The digest covers `scope`, `source_manifest`, `catalog_scan_id`,
  `catalog_raw_digest`, `db_incarnation` and every root
  `(collection, canonical root, dev, ino)`. It excludes only `generation`,
  which would otherwise have to contain its own hash.
* A recreated A-int DB therefore yields different names even for the same
  nonce, and a byte-identical worktree at other root paths yields a
  different digest.

**Why a rewrite cannot rebind a generation.** A-int never renames a
generation, and the pinned lookup takes the name from `view_seal(token)`.
A sidecar row only serves that name if `binding_digest(row) ==
suffix(name)`. A self-consistent rewrite (record, digest column and store
metadata all recomputed) can only:

* keep the A-int name, so the digest check fails
  (`generation name does not commit to binding`); or
* use a new name, so the A-int head has no binding
  (`generation source binding missing`).

Either way nothing is served. Binding another root to the existing name
needs a SHA-256 second preimage. The tests cover both cases and a
CHECK-violating attempt.

### API

```python
# Naming (pure)
binding_digest(binding) -> str                       # 64 hex
bound_generation_name(binding, nonce=None) -> str    # 'g-<nonce32>-b<digest64>'
bind_generation(binding, nonce=None) -> GenerationSourceBinding   # same fields, bound name
generation_digest(name) -> Optional[str]             # suffix digest, None if not a bound name
require_bound_generation(binding) -> str             # IndexContractError
                                                     #   'generation_name_unbound' / '..._mismatch'
capture_bound_generation_binding(index, catalog, snapshot, *, nonce=None)
    # capture_generation_binding under a provisional 'g-<nonce>' that is never
    # queued, staged or stored; returns bind_generation(binding, nonce)

# Claim-queue allocation (calls the existing ClaimQueue.enqueue_next unchanged)
enqueue_bound_generation(queue, binding, base_gen) -> job
    # require_bound_generation(binding), then
    # queue.enqueue_next(binding.scope, binding.generation, binding.source_manifest, base_gen)
    # and the returned job must echo scope/gen/source_manifest/base_gen

# Store
store = open_binding_store(path, index, catalog, create=False)
store = GenerationBindingStore(path, scope=..., db_incarnation=..., roots=..., create=False)
store.get(scope, generation, db_incarnation) -> Optional[GenerationSourceBinding]
store.put(binding) -> {'status': 'stored'|'exists', 'generation', 'binding_digest'}
store.verify() -> {'store_id', 'records', 'generations'}   # audit only, O(records)
store.close()
persist_generation_binding(store, index, binding)          # after stage, BEFORE publish
load_pinned_binding(index, token, store)                   # binding of view_seal(token)['gen']
open_pinned_candidate_query(index, token, store, **budgets)   # -> CandidateQuery
search_pinned_candidates(index, token, store, fts, limit, **kw)
```

**Generation allocation sequence** (one owner worker inside its lease):

```python
snapshot = catalog.require_complete()
binding  = capture_bound_generation_binding(index, catalog, snapshot)
job      = enqueue_bound_generation(queue, binding, base_gen=head_gen_or_None)
stage_from_catalog(index, catalog, job['gen'], base=job['base_gen'],
                   claim_digest=job['claim_digest'], claim_epoch=job['claim_epoch'],
                   binding=binding)                                  # 'prepared'
persist_generation_binding(store, index, binding)                    # 'stored'|'exists'
publish_from_catalog(index, catalog, job['base_gen'], job['gen'], job['claim'],
                     claim_digest=job['claim_digest'], claim_epoch=job['claim_epoch'],
                     binding=binding)                                # 'published'
```

The name must be allocated **after** capture, because it commits to the
snapshot. Queue, stage, persist and publish all use the same
`binding.generation`. Any exception means nothing is published. The worker
calls `request_scan()` and later captures a new binding, which gets a new
name. Queue replay (`lease`/`recover`) keeps the job's `gen`, so a replayed
job must carry, or re-read from the store, the binding with exactly that
name. Re-capturing would give a different name, and `put` would refuse it.

`sync_step` uses a stricter order than the sequence above:
capture → **persist** → enqueue → streamed stage → `publish_bound`. Persisting
before the claim exists means every queued bound claim can find its binding
on replay (`load_job_binding`); a crash between persist and enqueue only
leaves a harmless orphan row.

### SQLite sidecar

One file next to the A-int DB, distinct from it, `application_id` =
`0x4c594742` (`LYGB`), `user_version` = 1.

```sql
meta(key TEXT PRIMARY KEY, value TEXT)    -- format, version, store_id, scope,
                                          -- db_incarnation, roots (canonical JSON)
bindings(scope, generation, db_incarnation, binding_digest, record,
         PRIMARY KEY(scope, generation, db_incarnation),
         CHECK(length(binding_digest) = 64),
         CHECK(length(generation) = 100 AND substr(generation,1,2) = 'g-'
               AND substr(generation,35,2) = '-b'
               AND substr(generation,37) = binding_digest),
         CHECK(length(record) <= 262144)) WITHOUT ROWID
-- triggers: meta insert-once (6 keys), no UPDATE/DELETE on meta;
--           no UPDATE/DELETE/REPLACE on bindings ('binding_immutable')
```

* **Durability.** The store uses a rollback journal (`journal_mode=DELETE`)
  with `synchronous=FULL` and `fullfsync=ON` (`F_FULLFSYNC` on macOS).
  `put` returns only after `COMMIT`.
* **Transactions.** Every operation is one transaction. Writers use
  `BEGIN IMMEDIATE`; readers use `BEGIN`. `put` reads the key, then inserts,
  then commits. Any error rolls back.
* **Insert-only.** An identical re-put returns `exists` with no write.
  A binding whose name does not commit to it raises
  `generation_name_unbound` or `generation_name_mismatch` before any write.
  `binding_conflict` remains as a defensive check.
* **Safe creation.** The schema and metadata are built in an `O_EXCL`
  temp file, which is fsynced, published with a no-clobber `link`, and the
  directory is fsynced (both through the `F_FULLFSYNC`-aware `_fsync`).
  Creation refuses:
  * an existing path (`binding_store_exists`);
  * a leftover `-journal`/`-wal`/`-shm` (`stale_binding_store_sidecar`).
* **Identity checks.**
  * At open: the path is absolute, its parent is a directory, and it is
    not a symlink. `lstat` must show a regular file. The raw header must
    carry the SQLite magic and rollback-journal bytes 18–19 = 1, checked
    *before* SQLite can roll a journal or open a WAL.
  * Every transaction:
    * `lstat`s the path and requires the node seen at open with
      `st_nlink == 1`. A second hard link (for example the temp name left
      by a crash between `link` and its unlink) would let SQLite keep an
      independent `-journal` per name, so it is refused
      (`binding store has multiple links`) until the alias is removed;
    * re-checks `application_id`, `user_version`, the exact
      `sqlite_master` rows and `journal_mode`;
    * requires the exact metadata keys and values: scope, incarnation and
      roots against the caller, and `store_id` against the value at open.
* **Row decode on lookup.**
  * The record must be canonical JSON and pass `from_record`.
  * Its `generation` must equal the key.
  * Its scope, incarnation and root paths must match the store.
  * The digest column, `binding_digest(record)` and the name suffix must
    all be equal.

  A failure is `index_stale` (`binding store corrupt: ...`). A writer
  never re-confirms a corrupt row.
* Root `(dev, ino)` may differ between rows: a recreated root directory is
  a legal new generation with a new name. `put` checks only the name
  commitment and the store identity (scope, incarnation, root paths); it
  does **not** prove the recorded `(dev, ino)` are the live roots. Capture
  checks them at capture time, and `CandidateQuery` checks the live node at
  open and `finish()`.
* **Busy.** A `database is locked` error on `BEGIN` or inside a transaction
  is `index_stale` with `binding store busy` (retryable). Other SQLite
  errors stay `binding store unavailable` / `unreadable`.

**Recovery.**

* **Crash before `COMMIT`.** SQLite rolls back the hot journal on the next
  open. Nothing is stored and nothing was acknowledged, so nothing was
  published.
* **Crash after `COMMIT`, before the acknowledgement.** The row is durable
  and harmless: no head references it until publish. Re-persisting returns
  `exists`.
* **Recreated A-int DB.** It has a new incarnation, so the old store is
  refused. The owner creates a new store file.
* **Legacy (unsuffixed) heads.** They are never served through this path
  (`generation name is not bound`). Recovery is a new scan and a new bound
  generation.

**What triggers and CHECKs do not prove.** Anyone who can write the file
can drop them. They only stop accidental misuse. Integrity against a
recomputing writer comes from the A-int name suffix, as described above.
`CandidateQuery` still re-checks seal, incarnation and live roots. Deleting
rows or the whole file loses bindings, and that only fails closed.

**Costs.**

* A lookup is one primary-key search plus constant-size checks
  (`sqlite_master`, six `meta` rows).
* A `put` is one search and one insert.
* `verify()` (`quick_check` plus a decode of every row) is O(records) and is
  for audit only.
* There is no byte or row cap. There is no GC: orphan and old-generation
  rows stay until a parent-owned GC removes them. GC is not implemented,
  and the triggers would have to allow it.

## Product-path integration

### What is wired

```python
runtime = LayaIndexRuntime(source_root={...}, scope=..., db_path=..., queue_path=...,
                           db_incarnation=..., catalog_path=..., binding_store_path=...)
sync_step(runtime, *, before_publish, require_vector=False,
          max_files=200, max_bytes=5_000_000, max_stats=2_000,
          max_claims=8, rescan=False)          # one finite unit; call repeatedly
runtime.query_bound(fts, limit, *, collection_names=None,
                    max_file_bytes=None, max_query_bytes=None, max_query_files=None)
runtime.request_scan()                          # catalog.request_scan()
runtime.capacity_status()                       # catalog.status(): durable backlog
```

* **Runtime.** `catalog_path`/`binding_store_path` are optional, given
  together, absolute, distinct from the DB and queue paths, and only valid
  with Mapping roots. The runtime opens (never creates) the catalog for the
  same scope and roots and the store for the same scope, roots and DB
  incarnation. Both modules are imported lazily, only on this path.
* **`sync_step`**, under the existing queue owner lock and `before_publish`
  fence (called once before the first queue/store/A-int write):
  1. at most `max_claims` retained claims through `process_bound_one`; a
     larger queue returns `pending`/`claim_backlog` with `claims_pending`;
  2. if the head does not serve the current COMPLETE snapshot (manifest,
     root `(dev, ino)`, incarnation, exclusion epoch; scan id and raw
     digest are ignored, so an unchanged rescan does not publish), capture
     a bound binding, persist it, enqueue its claim, stream-stage and
     `publish_bound`. This also runs **while a newer scan is pending**
     (the `allow_pending` rule), so an exclusion, or an unbound (legacy
     `sync_full`), binding-less or unreadable-binding head, is replaced by a
     bound generation even when that scan is long or deferred on an
     oversized file;
  3. `rescan=True` requests a scan unless one is pending; an empty catalog
     bootstraps one;
  4. one `catalog.step(max_files, max_bytes, max_stats)`. A step that
     completes the scan goes back to 2 once.
* **Results.** `state` is `indexed`, `unchanged`, `pending`, `deferred` or
  `blocked`, plus `catalog` (the durable `status()` with `active.backlog`).
  Reasons include `scan_pending` (with `work`), `claim_backlog`, `replan`,
  `scan_restarted`, `no_complete_snapshot`, `snapshot_unusable`,
  `owner_busy`, `catalog_busy`, `file_exceeds_step_bytes`,
  `binding_store_busy` and `binding_store_unavailable`.
* **Liveness.** A snapshot that failed to capture or stage (replaced root,
  changed member: the claim is `replan`) requests a rescan. While that (or
  any) newer scan is pending, the runtime keeps an in-process verdict per
  `(snapshot, head generation, exclusion epoch)`: a snapshot already found
  served or unusable is not captured or staged again, and the answer
  repeats `snapshot_unusable` (`bound_claim_replanned` for a stage
  failure). A new head or a new exclusion changes the key, so one new
  attempt is made. With no newer scan pending every call re-captures (this
  is what notices a replaced root while idle). The verdict is not durable:
  a restarted worker repeats one failed attempt per unusable snapshot.
  A scan blocked by `source_changed` is restarted; any other block
  (`source_invalid`, `root_unavailable`, ...) is reported as `blocked`
  until a later `request_scan`.
* **Unreadable head binding.** The head's stored binding is evidence, not a
  precondition. If `store.get` fails, the head is not treated as served:
  a damaged row (or any non-busy failure) leads to a replacement bound
  generation; a busy store skips publishing for this call. If the new
  binding cannot be persisted either, nothing is published. In every case
  the scan is still stepped and the answer carries `binding_store`
  (`reason`, `detail`); with no scan to step the result is
  `deferred`/`binding_store_busy` or `binding_store_unavailable`, never
  `unchanged`. A retained claim whose own binding cannot be read is retired
  as `replan` (`deferred` if the store is only busy), so it cannot defer
  every later step.
* **Large single file.** A file above `max_bytes` returns `deferred` with
  `reason='file_exceeds_step_bytes'`, `path` and
  `required={'max_bytes': size}`. Calling again with the same budget returns
  the same answer with zero bytes read; only a larger explicit budget
  advances. No loop is hidden inside one call.
* **`process_bound_one`** (flow): same lease/ACK/release rules as
  `process_one`. A receipt is ACKed without restaging. Otherwise the job's
  own stored binding is loaded (`load_job_binding`); an unbound legacy name
  or a missing binding retires the claim as `replan` (it can never
  publish). A busy binding store, at load or at `publish_bound`'s stored
  binding check, is classified first: `deferred`, the claim is retained,
  no rescan is requested and the snapshot is not marked unusable.
  Otherwise `head_moved`/`index_stale` (newer snapshot published, member
  changed, binding mismatch, exclusion epoch) is `replan`; nothing is
  published.
* **`publish_bound`** refuses unless the job matches the binding and the
  store already holds exactly that binding, then calls
  `publish_from_catalog(binding=...)`.
* **`query_bound`** pins one generation and calls `search_pinned_candidates`
  (re-read mode; no host hook calls it).
  `status` is `ok` (verified rows; `[]` is normal, including index lag),
  `pending` (`no_published_generation`, `candidates_unverified` with the
  drop counts, `index_busy`, `binding_store_busy`) or `stale` (fail closed
  with the diagnostic: unbound, missing or corrupt binding, replaced root,
  recreated DB, `exclusion_epoch_changed`). `pending` and `stale` always
  carry zero rows.
* **A-int lock contention.** `pin` and `release` each take the A-int write
  lock (`BEGIN IMMEDIATE`, 2 s busy timeout), which a `sync_step` stage
  holds for a whole corpus pass. A busy pin is `pending`/`index_busy`
  instead of a raw `database is locked`. If the release stays busy, the
  verified rows are withheld (`ok` becomes `pending`/`index_busy`; a
  `pending`/`stale` answer keeps its status) and the answer carries
  `pin_released: False`. **Pin-cleanup limitation:** the unreleased token
  is retried only by later `query_bound` calls on the same runtime, after
  their own release succeeds. If the process exits first, the pin row stays
  in A-int; nothing removes it. It affects no answer; it only keeps its
  generation listed as active in the read-only `gc_plan` census (no
  deletion path exists).
* **Compatibility.** `stage`, `sync_full`, `process_one`, `query` and
  `enqueue_claim` keep their signatures and behaviour; a runtime without the
  new paths is unchanged, and `sync_step`/`query_bound` on it raise
  `capacity_not_configured`.

### Streaming A-int stage (`LayaIndexA.stage_stream`, `stage_bound_streaming`)

* `stage_bound_streaming` runs the same snapshot and binding checks as
  bound `stage_from_catalog`, then passes the `catalog.iter_documents`
  generator straight to `LayaIndexA.stage_stream`. No document list is
  built: peak Python memory is one body (plus its FTS-normalized copies),
  not the corpus.
* **Every member is re-read and rehashed** against the snapshot, on first
  stage and on replay. Nothing is reused because a hash or manifest
  matched. A replay of an identical prepared/published generation consumes
  every re-read document, compares each `(path, hash, eligible)` with the
  stored membership in path order, checks the FTS rows in lockstep, and
  only then returns `already_staged`.
* The manifest digest is computed incrementally (`_StreamDigest`, equal to
  `_digest` for strictly increasing paths); unsorted or duplicate paths are
  `invalid_document`. The generation row carries the claimed manifest until
  the last row; a final mismatch is `manifest_mismatch` and rolls back.
* **Stage remains one full-corpus transaction.** The A-int write lock is
  held for the whole re-read, insert and FTS build: O(N + B) time and WAL
  growth per generation, no incremental stage, and other A-int writers wait
  (readers continue under WAL; `query_bound`'s pin/release are writers and
  answer `pending`/`index_busy` after 2 s). Streaming only removes the O(B) Python
  memory of the old document list.
* **Candidate current-byte checks do not prove whole-corpus ranking.**
  `query_bound` proves each *returned* body is the file's current bytes. It
  does not prove the ranking, BM25 statistics or "which rows exist" match
  the live corpus; those are the pinned generation's (index lag).

## Optional audit/test oracle: `search_catalog_verified`

It serves tests and optional audits.
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
gate.

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
scan per operation and holds every file's bytes in memory, with a per-call
`max_files=200` / `max_bytes=5_000_000` bound. On the capacity path indexing
runs over a per-step work budget on a durable catalog instead (`sync_step`
uses the same numbers as step budgets, not as a corpus cap), and the
installer gives a source above one scan's bound a background build plan
rather than a refusal. The selected path without capacity stores still runs
a full port scan per query.

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
4. **Returned bodies are hash-verified, never stat-verified.** In re-read
   mode a same-size/mtime rewrite of a returned candidate is caught by its
   current SHA-256; in indexed mode the published body must hash to its row
   hash. `verify_unchanged`, `status()['servable']` and any TTL
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

## A-int and query costs

* **Full-generation stage.** `stage_from_catalog` builds the full
  `documents` list, holding every body in memory, and `LayaIndexA.stage`
  inserts every row and builds FTS in one transaction. That is O(N + B)
  memory and write time per generation. The integrated path uses
  `stage_bound_streaming` / `stage_stream` instead: O(one body) Python
  memory, but still O(N + B) time in one transaction holding the A-int write
  lock. There is no incremental stage.
* **Full-corpus FTS check per query.** Every `LayaIndexA.search` /
  `search_many` call runs `_generation_fts_rows_match`, which re-tokenizes
  every eligible body of the generation: O(N + B) CPU and memory per query.
  Candidate verification does **not** fix this.
* **Exact vector scan.** `search_exact` scores every scoped chunk per query,
  bounded by the deadline and explicit caller budgets. Candidate
  verification does **not** fix this.
* **Candidate query (re-read mode).** Each query reads at most `max_query_files` files
  and `max_query_bytes` bytes (+1 growth-detection byte per file). It holds
  only those buffers.
* **Audit oracle.** Holds all proven bytes (≤ `max_proof_bytes`) per call.
* **Install.** `core/laya_product_install.py` tries one source scan within
  `SYNC_ORIGIN_MAX_FILES = 200` files; a larger source gets a background
  build plan on the capacity path.

## Honest limitations

* Ranking freshness is **not** claimed. New or changed files lag until async
  sync. A dropped top candidate can let a lower-ranked one through, and
  that ordering reflects the pinned generation.
* In re-read mode `max_file_bytes`, `max_query_bytes` and
  `max_query_files` default to 5 MB, 5 MB and 200. These are **arbitrary technical budgets**
  that keep per-query reads finite. They are not an agreed product cap.
  When a candidate exceeds one, it is dropped as `file_budget` or
  `query_budget`. It stays indexed and is not deleted, and lower-ranked
  valid candidates are still served. If no candidate of the phase survives,
  the phase raises `CatalogPending` (`candidates_unverified`) with those
  counts and zero rows. Only an explicit larger budget from the caller
  serves the file; no product limit has been chosen.
* Whole-corpus ranking is the pinned generation's (index lag). In re-read
  mode candidate freshness is a separate claim: each returned body was
  hashed from current bytes at read time.
* The residual TOCTOU window after `finish()` remains, as with the port.
  A path memoized in an earlier phase is served from the bytes read then.
  It is not re-read in later phases of the same query.
* A-int's per-query full FTS check and exact vector scan remain O(N + B)
  per query.
* The generation stage is one full-corpus A-int transaction, and a
  `sync_step` that publishes is bounded by the corpus, not by the step
  budget; only scan steps are budgeted.
* A head with an unsuffixed `g-<uuid>` name (written by `sync_full`) fails
  closed on the sidecar path until a new bound generation is published.
  No migration renames heads.
* The name suffix is unkeyed SHA-256: it binds content, not authorship. A
  writer who can write the A-int DB itself is outside this threat model.
  So is one who deletes sidecar rows, which only denies service.
* The sidecar has no GC, and its insert-only triggers would block one.
  Retention of old and orphan rows is a parent decision.
* Crash recovery relies on SQLite's rollback journal. The tests model it with
  child processes that die before and after `COMMIT`, and with a spilled
  uncommitted transaction. They do not simulate power loss or a lying disk
  cache.
* Hidden entries change their directory's signature, so the stat hint
  fails closed until the next rescan (conservative; `binding=None`
  indexing only, since the bound path does not run the hint).
* Bound stage cannot see files added after the snapshot (it rehashes
  members only). That is index lag, closed by the next scan and
  generation, never stale exposure.
* A single directory listing is not split across steps.
* Python 3.9-compatible syntax and the stdlib only. POSIX `dir_fd` support
  is required.

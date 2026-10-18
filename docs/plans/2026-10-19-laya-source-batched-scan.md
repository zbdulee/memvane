# Laya source capacity: durable step-bounded source catalog (2026-10-19)

Status: independent slice implemented as new files only. Production sync,
query, install and host-response paths are **not** wired to it yet. Base
`077f8186` on `feat/laya-capacity-batched-scan-20261019`.

## Problem

`core/laya_index_source_port.py` proves source state by one synchronous full
scan per operation (stage, publish, every verified search). It holds every
file's bytes in memory and enforces `max_files=200` / `max_bytes=5_000_000`
as a **whole-corpus** cap. A corpus above either bound is permanently
`index_stale`. 200 files / 5 MB was a per-call work bound, not an agreed
product corpus limit. This slice converts it into a per-step work budget over a
durable catalog.

## Plan

1. `core/laya_source_catalog.py`: a separate SQLite catalog file. The catalog
   cannot live in the A-int database because `LayaIndexA._check_header`
   rejects any extra schema object. The catalog is bound to an exact
   `(scope, sorted collection roots)` pair. It has these parts:
   * `request_scan()` creates (or coalesces) one pending scan. It does not touch
     the source.
   * `step(max_files, max_bytes, max_stats)` runs one bounded `BEGIN IMMEDIATE`
     transaction. The step lists directories, then streams hashes, then
     stat-verifies directories, then stat-verifies files. Every completed
     verification ends with an atomic snapshot publication in the same
     transaction.
   * `status()` / `require_servable()` report the serving state and the
     explicit backlog.
   * Integration helpers `stage_from_catalog`, `publish_from_catalog` and
     `search_catalog_verified` keep A-int sealed-manifest semantics.
2. `test/fixtures/laya-source-catalog-checks.py` plus
   `test/laya-source-catalog.test.mjs`: synthetic temp-dir corpus and a
   temp A-int DB only.

## API

```python
catalog = LayaSourceCatalog(db_path, {"plans": "/abs/plans", "wiki": "/abs/wiki"},
                            scope, create=True)
catalog.request_scan()   # {'status': 'started'|'coalesced'|'busy', 'scan_id'}
catalog.step(max_files=200, max_bytes=5_000_000, max_stats=2000)
#   -> {'status': 'pending'|'deferred'|'complete'|'blocked'|'idle'|'busy',
#       'scan_id', 'phase', 'work': {...}, 'blocked'?, 'deferred'?, 'published'?}
catalog.status()           # servable/reason/current/active + backlog counts
snap = catalog.require_servable()   # raises IndexContractError('index_stale')
excl = load_exclusions(index, scope)
catalog.manifest(snap, excl)        # == scan_collections(...).manifest for same bytes
catalog.iter_documents(snap, excl)  # re-reads + rehashes each file, yields (path, text, eligible)
catalog.read_verified(snap, path, sha256)
catalog.verify_unchanged(snap, max_stats=10_000)  # stat sweep; raises index_stale

stage_from_catalog(index, catalog, gen, base=..., claim_digest=..., claim_epoch=...,
                   max_verify_stats=10_000)
publish_from_catalog(index, catalog, expected, gen, claim, claim_digest=..., claim_epoch=...,
                     max_verify_stats=10_000)
search_catalog_verified(index, catalog, token, fts_query, limit,
                        collection_names=None, max_verify_bytes=5_000_000,
                        max_verify_stats=10_000)
```

Work budget per step:
* `max_files` work units. One directory listing or one file hash is one unit.
* `max_bytes` file bytes read and hashed, using streaming SHA-256 and an
  incremental UTF-8 decoder. No whole file or corpus buffer is kept.
* `max_stats` stat verifications in the verify phases.
* One directory listing is atomic. Its `lstat` calls are bounded by the
  retained per-scan entry bound (`MAX_ENTRIES`), not by `max_stats`.

## Safety invariants

1. **Atomic complete snapshot.** `current` changes only in the transaction that
   marks a scan `complete`, after every directory was listed and every file was
   hashed and stat-verified. A crashed or failed step rolls back. A blocked,
   deferred or pending scan never replaces `current`.
2. **Partial enumeration is never evidence of deletion.** Add, change and
   delete counts are computed only at publication, by comparing two complete
   scans. During a pending scan, the old snapshot keeps all of its files.
3. **No silent old-snapshot serving.** The catalog can serve only when
   `active == current`. Any requested, pending, deferred or blocked newer scan
   makes `require_servable()` and every helper raise
   `IndexContractError('index_stale')`. A request that arrives during a
   running scan sets `rerequested`. Publication then chains a new pending
   scan in the same transaction, so serving stays closed.
4. **Mutation detection.**
   * Each directory compares its `fstat` signature before and after `listdir`.
   * Each file's `fstat` signature must equal its enumeration `lstat`
     signature before and after streaming, and the byte count must equal the
     size.
   * The verify phases re-check every directory signature (entry
     add/remove/rename) and every file signature after hashing.
   * Directory chains are opened `O_NOFOLLOW|O_DIRECTORY` component by
     component, with recorded `(dev, ino)` identities. A substituted root or
     directory blocks the scan.
   * The signature is `(dev, ino, mode, size, mtime_ns, ctime_ns)`, the same
     as the port's `_sig`.
5. **Preserved validation.** The catalog reuses the port's `_check_root`,
   `_open_root_fd`, `_safe_rel`, `_sig`, `MAX_DEPTH`, `MAX_ENTRIES` and
   flags. It keeps:
   * the hidden-entry skip;
   * rejection of non-hidden symlinks, device crossing, non-regular `.md`,
     unsafe or non-UTF-8 names, overlong paths, depth and entry bounds, and
     invalid UTF-8 content;
   * disjoint-root and collection-name checks.

   Each violation blocks the scan (`source_invalid`). Nothing is skipped
   silently.
6. **Stable namespaced identity.** Paths are `collection/relative.md`, exactly
   as in `scan_collections`. The A-int manifest is `_digest(scope,
   [(path, sha256, eligible)])`, computed from the catalog with exclusions read
   once per operation. The same bytes give the same manifest as the port.
7. **Restart and duplicates.** All progress is committed per step. A process
   that restarts resumes from the durable phase and cursor. A concurrent
   writer gets `busy` (SQLite `BEGIN IMMEDIATE`) and never duplicates
   committed work. Duplicate `request_scan()` calls coalesce.
8. **Oversize single file.** If a file is larger than the whole step byte
   budget, the step returns `deferred` with reason `file_exceeds_step_bytes`.
   The scan stays pending, so serving stays closed, and a later step with a
   larger `max_bytes` resumes it. Technical reason: the stdlib `hashlib`
   state cannot be persisted across processes, so one file's hash cannot
   span steps. This is a work-unit constraint, **not** an agreed product
   corpus or file-size limit.
9. **Freshness at use (review fix).** A servable snapshot alone does not
   prove that the source is unchanged now, because an out-of-band edit with no
   `request_scan()` leaves `active == current`. So `search_catalog_verified`,
   `stage_from_catalog` and `publish_from_catalog` all call
   `verify_unchanged(snap, max_verify_stats)`. It runs in one read
   transaction and checks that the snapshot is still current. It then
   re-opens every snapshot directory through the no-follow identity chain and
   compares its `fstat` signature. Finally it `lstat`s every snapshot file and
   compares its signature. Any add, delete, rename or in-place edit of an
   *unreturned* file therefore raises `index_stale`. Search runs the sweep
   after row verification and before the final snapshot and seal checks.
   If the snapshot has more than `max_verify_stats` (default 10,000)
   directories plus files, the call **fails closed**. It does not sweep
   partially. That cap is a per-call work bound, not a corpus limit. Serving
   above it needs the change-feed seam described below.

## Acceptance criteria (synthetic tests)

* A corpus with more than 200 files and more than 5 MB completes over at least
  two default-budget steps. Every step stays within its budget. The catalog
  manifest equals the port manifest computed with larger test-only bounds,
  and the default port scan still raises `index_stale`.
* Stage, publish and search work through the catalog.
* Add, update and delete changes are published only on completion.
* A partial rescan never deletes from `current`.
* A change while enumerating, hashing or verifying blocks the scan. The old
  snapshot is kept but not served.
* Crash mid-step: an injected fault rolls back, and a new instance resumes.
* `busy` and coalesced duplicate requests.
* Backlog counts fall monotonically to zero.
* Symlinks, invalid UTF-8 and a replaced root block the scan.
* An oversize file defers.
* Pending, blocked or out-of-band-modified rows cannot be served as normal.
* An out-of-band edit, a same-bytes rewrite, an add or a delete of an
  *unreturned* file, with no rescan requested, makes search, stage and
  publish raise `index_stale` from the freshness sweep. A sweep over budget
  also fails closed.
* A corrupt catalog file raises `catalog_unreadable`.

## Integration seams (parent coordination; not edited here)

| Existing file | Needed change (compatible) |
|---|---|
| `core/laya_index_sync.py` / `laya_product_sync_worker.py` | Owner worker calls `catalog.step()` repeatedly within its lease. It may *attempt* stage and publish once `step()` returns `complete` (without `next_scan_id`) or `status()['servable']` is true, but these are scheduling hints only, not freshness proof. Stage and publish go only through `stage_from_catalog`/`publish_from_catalog`. Each of those runs `require_servable`, `manifest` and `verify_unchanged(snap, max_verify_stats)` on one snapshot. Any `IndexContractError` from them means nothing is staged or published. The worker treats it as stale (no retry with an older manifest) and calls `request_scan()` before stepping again. Mapping-root scopes only. |
| `core/index_enqueue.py` / dirty queue / `file_change_queue.py` handoff | Each detected source CUD must call `catalog.request_scan()` so that serving fails closed until a complete rescan. This shortens the stale window. It is not a substitute for the per-use `verify_unchanged` sweep, because an edit the queue misses never calls `request_scan()`. |
| `core/laya_index_runtime.py` `query`, `core/laya_host_response.py` `_v2_phase` and `query_selected_hybrid` | Replace the per-query full `scan_collections` with `search_catalog_verified` behind an explicit opt-in flag. A reimplementation must keep its full sequence: view-seal scope == catalog scope, `require_servable`, exclusions read once, catalog manifest == view seal, every returned row matched to the snapshot and re-read/rehashed within `max_verify_bytes`, `verify_unchanged(snap, max_verify_stats)`, then `require_same(snap)` and an unchanged view seal. `status()['servable']` must never gate serving by itself. |
| `core/laya_index_runtime.enqueue_claim`, `laya_index_vector` source evidence, `laya_qmd_cutover` | Use catalog evidence instead of a full rescan only through the exact sequence in "Catalog source evidence for non-helper callers" below. Never call `catalog.manifest(...)` on the strength of `status()['servable']` alone. `manifest()` does not run `verify_unchanged`. `servable` only means that no newer scan was requested. Neither one shows that the source is unchanged now. |
| `core/laya_product_install.py` initial scan | Install bootstrap would create the catalog and step it to completion. The installer currently caps with `MAX_INITIAL_FILES/BYTES` and needs a product decision. |
| `core/laya_index_source_port.py` | None required. The catalog imports its private helpers (`_check_root`, `_open_root_fd`, `_safe_rel`, `_sig`, `_load_exclusions`, `_seal`, `_positive_int`, `_raise_stale`, `_require_platform`, flags, bounds). Renaming them later should promote them to a public `source_validation` surface. |

### Catalog source evidence for non-helper callers

`status()['servable']` (and `require_servable()`) means only that `active ==
current`, i.e. no newer scan was requested. It is **not** a source freshness
proof: an out-of-band edit with no `request_scan()` leaves it true.
`manifest(snap, excl)` only re-checks that `snap` is still the current and
active snapshot, then digests catalog metadata. It never touches the source.
Callers that need source evidence outside the three helpers
(`enqueue_claim`, vector source evidence, cutover) must run this sequence,
once per decision:

```python
snap = catalog.require_servable()                 # 1. raises index_stale if not servable
excl = load_exclusions(index, catalog.scope)      # 2. read once for this operation
catalog.verify_unchanged(snap, max_stats=cap)     # 3. finite positive cap (default
                                                  #    DEFAULT_VERIFY_STATS = 10_000)
manifest = catalog.manifest(snap, excl)           # 4. same `snap` object as step 1
# use `manifest` as the source evidence for this one decision only
```

* **Fail closed on any step.** If any step raises, the caller produces no
  source evidence and returns or propagates `index_stale`. Examples are
  `index_stale` from `require_servable`, a stat mismatch, a sweep above `cap`,
  a snapshot change seen by `manifest`, or `catalog_unreadable`. It must not
  substitute a previously computed manifest, the `status()` result, a
  partial sweep or a manifest from another snapshot.
* **Same snapshot.** Steps 3 and 4 take the `snap` returned by step 1.
  Each step re-checks that `snap` is still current and active, so a
  `request_scan()` between steps fails closed rather than mixing snapshots.
* **Finite budget.** `cap` is a per-call work bound. A snapshot above it
  fails closed (see the change-feed section). It is not a reason to skip
  the sweep.
* **Point-in-time only.** The evidence is valid only for the decision it
  was computed for. A long-running cutover or vector job re-runs the whole
  sequence at each commit point. It does not reuse an earlier `manifest`.
* **Residual limits still apply.** The sweep is a stat-signature check,
  not a rehash. A TOCTOU window remains after step 3 (see "Honest
  limitations").

The catalog DB path must be installer-owned (next to the A-int DB, distinct
file) and bound to the same scope. Nothing here selects or creates it in
production.

### Change-feed interface required above the sweep budget (not implemented)

A snapshot with more than `max_verify_stats` entries fails closed. A caller
may raise the cap, at O(entries) stats per call. Serving such a snapshot
without a per-call sweep requires a change feed that the catalog can check.
The exact interface the integration must provide:

```python
class SourceChangeFeed(Protocol):
    def watermark(self, scope: str, roots: Mapping[str, str]) -> tuple[str, int]:
        """(feed_epoch, seq). `seq` increases durably for every observed create,
        update, delete or rename under any root. `feed_epoch` changes whenever
        the watcher restarted, dropped or overflowed events, or lost coverage of
        a root, so a gap is never reported as "no change"."""
```

Catalog contract when this is wired. This is a future change in this
module only; no caller exists yet:
* `request_scan()` records the feed watermark when the scan **starts**.
* Publication keeps that watermark with the snapshot.
* A serve may skip `verify_unchanged` only if the current
  `watermark() == recorded`. Otherwise it fails closed and the caller must
  call `request_scan()`.

The feed owner is the dirty queue / `file_change_queue.py` handoff row above.
Until that exists, the stat sweep is the only freshness proof.

## Honest limitations

* **Serving freshness is a stat check, not the old per-query full rehash.**
  A search through `search_catalog_verified` proves four things:
  1. the A-int view seal equals the manifest of the last complete, verified
     catalog scan;
  2. no newer scan has been requested;
  3. every *returned* row's current bytes rehash to the indexed hash. The
     bodies are served from that re-read;
  4. every snapshot directory and file still has its scan-time no-follow
     identity and stat signature `(dev, ino, mode, size, mtime_ns,
     ctime_ns)`.

  For unreturned files, item 4 is a signature check, not a hash. It
  catches every normal write, because writes update `ctime`, which user
  space cannot set. It does not catch a raw-device or filesystem-level
  rewrite that preserves the signature. The port's rehash would catch that.
* Hidden entries (`.DS_Store`, editor swap files) change their directory's
  signature. They therefore make serving and scans fail closed until the
  next rescan. This is conservative, not a silent skip.
* A TOCTOU window remains after the final sweep and seal check (same class
  as the port). During a scan, a directory changed after its
  `verify_dirs` stat but before publication can go unnoticed by the scan
  itself. That window can span several steps. The serve-time sweep catches
  it, because the published signature no longer matches.
* The A-int stage still materializes the whole generation:
  `stage_from_catalog` builds the full `documents` list because
  `LayaIndexA.stage` holds all rows in one transaction. A-int `search_many`
  also re-reads all eligible bodies per query (`_generation_fts_rows_match`).
  These are A-int bounds this slice does not change. The catalog itself only
  holds per-file buffers when reading (`read_verified`, `iter_documents`).
* `manifest()` builds the `(path, sha, eligible)` list for `_digest` in
  memory. This is metadata only and bounded by `MAX_ENTRIES` (50,000), which
  is retained from the port as a technical metadata bound.
* A single directory listing is not split across steps.
* The catalog keeps at most the current complete scan plus one active scan,
  and deletes older catalog metadata at publication. It never deletes source
  or A-int data.
* Python 3.9-compatible syntax and stdlib only. POSIX `dir_fd` support is
  required (same as the port).

## Test command (shared guard; 1 node test wrapping 14 Python unittests)

```bash
npm run test:focused -- test/laya-source-catalog.test.mjs
```

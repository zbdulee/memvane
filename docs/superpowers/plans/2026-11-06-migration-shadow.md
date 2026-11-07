# Migration Shadow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Build a resumable v6-to-view shadow copy and parity receipt without switching the reader.

**Architecture:** Use the existing v5 journal endpoint, reader leases, verified common snapshot, publication revisions and view/blob store. Copy only proved wiki data into a private candidate; keep the selected v6 route authoritative. Re-pin and revalidate the endpoint tuple for each bounded batch, and restart or catch up when it changes. A published indexed body is not replaced with a later disk version.

**Tech Stack:** Python 3.9/3.13, SQLite transactions, existing reader and installer locks, Node test runner.

**Spec:** `docs/plans/2026-11-01-memvane-db-git-blob-model.md` §§8.1–8.2, 12 (PR6), 14.

## Global Constraints

- Main checkout only; never activate an endpoint or change a route in PR6.
- Exclude raw body, raw FTS and incompatible vector bytes from the new database.
- Apply sidecar shadowing before exclusions; an excluded shadow must not revive an older base row.
- Preserve old verified card bytes and trust; unknown historical compile commit is `legacy_unbound`.
- Use short, bounded transactions and detect endpoint, generation, revision and policy changes. The spec proposes 100 cards or 8 MB and a 1-second step as starting points; choose the final step budget from measured reader/writer latency, physical-lock duration and recovery behavior rather than treating 1 second as an absolute release gate.
- Reuse existing SQLite transactions, reader leases, installer locks and ViewStore; do not introduce SQLite file-format parsing or parallel lock hierarchies.
- Product diagnostics contain only opaque IDs, bounded stage/result codes and numbers; never document or prompt content.

## Review Focus

1. Completed sidecar rows with identical hashes, excluded paths or changed body while copying.
2. Source receipt gaps, stale endpoints, generation changes and process death between cursor commits.
3. Import of retained wiki bytes when the working tree has changed or the card no longer exists.
4. Out-of-space, busy and malformed storage leave the old reader available and never record a false parity result.
5. Repeated starts and concurrent writers cannot publish or advance a stale shadow head.

## Tasks

### 1. Pinned effective membership and bounded capture

**Files:** `core/laya_shadow_migration.py`; `test/laya-shadow-migration.test.mjs`; `test/fixtures/laya-shadow-migration-checks.py`.

- [ ] Add a real SQLite fixture with base and completed sidecar rows. Assert selected-v6 shadow behavior for changed and identical hashes, exclusions, absent sidecar, invalid paths, wiki-only admission, and sidecar updates between pins.
- [ ] Run the focused test through the development guard and confirm that the missing migration entry point causes the expected failure.
- [ ] Implement bounded capture over the existing issued endpoint and pinned snapshot, with generation/revision/policy identity checked on every batch. Verify sidecar documents with the existing journal reader and base members with existing seal/FTS proof. Return no raw body data.
- [ ] Run the focused test plus `test/laya-incremental-docs.test.mjs` through the guard and record the results.

### 2. Private candidate, import and cursor recovery

**Files:** `core/laya_shadow_migration.py`, existing blob/view schema and admission code only where needed, migration fixture.

- [ ] Add tests for indexed wiki body versus changed or missing disk card, unknown legacy source commit, verified trust, interrupted batch replay, SQLite busy and full-disk failure, and private store containing no raw content or raw FTS.
- [ ] Add an admission proof tied to the verified published endpoint and exact wiki body hash; retain the existing filesystem admission contract for normal ingestion. Never synthesize a past commit from current HEAD.
- [ ] Record migration identity, source tuple and cursor in a private atomic intent using the existing install and operation locks. Commit immutable candidate bodies first, then replace the intent; a crash between them replays the batch idempotently. Never mark parity on cursor progress alone. Revalidate the selected endpoint and candidate identity before every batch.
- [ ] Run focused migration and blob/view tests through the guard.

### 3. Catch-up and parity receipt

**Files:** `core/laya_shadow_migration.py`, existing publication-revision and view-store modules where necessary, migration fixture.

- [ ] Test writer changes during copy, lost receipt detection by full path/hash comparison, sidecar-over-base selection, stale policy/endpoint refusal, and crash before/after parity receipt.
- [ ] Consume existing publication revision evidence and perform final bounded membership reconciliation. Seal a parity receipt only after old/new path, hash, eligibility, trust, spans and lexical results agree; do not perform reader switching.
- [ ] Record bounded stage, reason, retry and outcome diagnostics without source text or secrets. Run focused migration and incremental-docs suites.

### 4. Integrated PR6–7 delivery

- [ ] Complete the migration failure cases and parity evidence, then implement the PR7 view reader/writer selection in dependency order. During integration, run the necessary focused tests for each changed behavior.
- [ ] At the final integrated PR head, run the complete suite under the shared guard plus crash/recovery, concurrency, and search/vector parity checks. Update all seven version manifests consistently, obtain one independent exact-head review, and fix verified findings.
- [ ] Merge the integrated PR and perform one two-host installation, project proof, and recall verification. Defer final CI checks until PR10.

**Ruling:** Keep the existing v3 candidate schema and v5 journal readable by prior bridge versions. Do not attach a second writable database by bypassing the existing FTS authorizer. A cursor update after the candidate commit may repeat a batch; content-addressed inserts and parity reconciliation make that gap recoverable. The production coordinator must take install and operation locks before acquiring a reader lifetime.

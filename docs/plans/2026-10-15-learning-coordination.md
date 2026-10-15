# Durable learning and inference priority implementation plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Consume fresh verified local learning evidence once, persist recoverable jobs, and yield bounded training to inference without changing authorization or publishing checkpoints.

**Architecture:** Extend the existing namespace-routed shared SQLite database with semantic consumption and pinned batch records. Kernel leases fence live owners, including inherited trainer children; a private user controller coordinates inference demand and one trainer. Existing approved worker wakes and a bounded training-only retry waiter resume pending work without repeating teacher calls or installing a service.

**Tech Stack:** Python stdlib SQLite/flock, existing Laya head adapter, existing private shared worker; synthetic mock trainer tests only.

## Approved scope and alternatives

The previously approved singleton next-commits design is the input. Reuse routed per-namespace history for bodies and jobs; common state contains only demand/lease metadata. An in-memory queue would lose crash state, while a new always-running learning daemon would add unnecessary lifecycle/rollout scope. Use existing workers with persisted retry eligibility and a bounded waiter. Reboot recovery starts at the next ordinary authorized hook, not a new login service.

## Task 1: Pinned fresh batches and consumption

Files: new `core/context_learning/learning_queue.py`, `automatic_staging.py`, synthetic queue fixture/wrapper and existing auto-staging fixture.

1. RED: UUID/time changes do not make new evidence; source/gold/model/scope changes are distinct; holdout never enters training.
2. Add private DB schema for jobs, members and consumption, immutable authorization/model/cohort/input pins, bounded fresh/replay manifests and retry limits.
3. Claim under a stable kernel lock, never timeout-steal a live owner. Inherit the claim FD into a real trainer so a surviving child prevents recovery. A reboot/free lock permits recovering the same batch without another reservation.
4. Persist immutable success artifact/result before atomically consuming evidence and staging. Recover success output after a worker crash rather than training again; no success receipt means resume a saved update cursor.
5. Integrate the v2 staged path only, retaining authorization/source/cohort checks and forbidden publication. Do not change legacy private 24h cadence.

## Task 2: Resource controller and cooperative cursor

Files: new `resource_coordination.py` and `training_cursor.py`, `inference_service.py`, `local_cycle.py`, `laya_adapter.py`, `memory_policy.py`.

1. RED: queued inference defers training; a live claim survives metadata age; dead owners release leases; independent namespaces share only resource coordination.
2. Register inference demand before RPC, hold worker demand through actual compute, and acquire a deadline-bounded compute gate. Training acquires the same gate for a single safe optimizer update, never across the entire batch.
3. Honor existing CPU/available-memory/pressure/device and configured memory estimates. Fit microbatch 2 or shrink to 1; unknowns and insufficient reserve defer. No forced CPU/model switch or arbitrary GPU reservation claim.
4. Commit head plus CPU/MPS RNG and next-group cursor at each completed update for the existing momentum-free SGD contract. Persist before releasing the gate; defer/yield before another update on inference demand. Resumption verifies input/model/view/head hashes and optimizer contract. Interrupted uncommitted work may be recomputed; committed updates are not replayed.
5. Bound updates, elapsed time, retries and replay; no new data produces no new train job. No caller may automatically promote the result.

## Task 3: Pending retry integration

Files: `shared.py`, queue status and tests.

1. Persist resource-deferred next_retry with bounded backoff.
2. A single bounded waiter invokes training-only retry under existing scope/worker lock; it never calls gold/teacher. Duplicate wakes coalesce; process exit leaves durable pending state.
3. Revalidate policy/grant/source at every recovery. Revocation blocks, not retries into expanded scope. Completion and next authorized hook can drain further fresh batches; no timer invents new evidence.

## Task 4: Independent review and CI

Run focused synthetic lifecycle/resource/host regressions, including real disposable subprocess/flock crash tests with mock trainer only. Request independent review; fix blockers with regressions. Commit and push the same feature branch; confirm Node22/24/26 CI. No operating ML or installation.

## Separate design commit: defaults and candidate apply

Document new approved-profile defaults while preserving existing explicit false settings and no teacher/provider/billing expansion. Current v2 train/evaluate grants cannot apply a candidate. Define a separately scoped quality/authorization/CAS publication transaction; do not implement implicit grant or pointer changes in this slice.

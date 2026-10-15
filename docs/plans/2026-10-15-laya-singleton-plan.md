# Laya singleton implementation plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Share one persistent plugin-owned inference worker across hosts/projects while preserving prediction proof and project guards.

**Architecture:** Owner-private user Unix socket + startup/kernel leases, serial loaded-generation engine and bounded deadline queue. Client and worker both validate current project policy/checkpoint/evidence; no training side effects. Ordinary selector calls the service after read-only admission.

**Tech Stack:** Python stdlib sockets/threads/queue/flock, existing adapter/model validation; Node test wrapper and synthetic Python fixtures.

## Task 1: Correct handoff and preserve scope

- Replace the newly reintroduced automation/PID mandatory conditions with the actual keep-sessions/inbox instruction.
- Commit design and plan from 447d16e on feat/user-singleton-laya-service; no installed changes.

## Task 2: Test and implement service core

Files: new core/context_learning/inference_service.py, new test/fixtures/context-learning-inference-service-checks.py and test/context-learning-inference-service.test.mjs; existing resident.py helpers.

1. Add failing synthetic tests for shared user root, namespace validation, pinned generation reuse and failed/successful/headroom-constrained swap; run targeted fixture and record expected RED.
2. Implement private root/signature, fixed schema errors, one loaded generation with injected synthetic loader/forward only in tests. Pin one runtime/base/view family; reject unapproved family mixing.
3. Add bounded FIFO deadline/disconnect tests before implementing dispatcher. Queue capacity 8 plus running job; no prompt persistence, no retries.
4. Add real short-path socket/lease synthetic concurrency, startup/crash recovery and protocol tests before launch/server logic. Stub only ML loader/forward. Server must remain responsive to handshake while scoring; no unbounded threads.
5. Run node --test test/context-learning-inference-service.test.mjs and commit service separately once green.

## Task 3: Connect ordinary selector without training

Files: core/context_learning/local_cycle.py (extract validate_prediction from existing guarded preparation), core/context_learning/live_select.py (both branches call shared service), affected selector/proof/binding fixtures.

1. Add failing regression for ordinary choose routing to service across namespaces and both hosts; prove expired/missing/changed proof never launches service or attestation.
2. Extract read-only prediction admission; explicit training still uses full prover. Preserve recheck after queued waiting, original timestamps, five checks and TTL.
3. Validate current policy, pointer and artifact on server; normalize only verified official adapter binding. No arbitrary executable/namespace override in wire. Worker uses its already approved interpreter and checks runtime compatibility.
4. Update existing transport stubs at the new service boundary; keep real guards and file checks. Run proof-cache, live-selector, adapter-binding, adaptive-memory, cycle and singleton tests under isolated HOME/locks with empty QMD_RECALL_LOG. Record no actual ML/provider calls.
5. Commit selector connection separately.

## Task 4: Review and report

- Independent reviewer reads service security/concurrency/replacement and selector guard diffs; fix findings with regression first.
- git diff --check, focused final tests once after changes, clean worktree and separate local commits.
- Record installed rc45 files/settings/pointers/proofs unchanged; no sessions/inbox operations, no deployment or models.
- Report implemented behavior, synthetic coverage, unexecuted real-host/model tests and remaining training scheduler/default-on/automatic-apply gaps.

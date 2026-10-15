# Predict-only proof validation implementation plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make every verified prediction reuse valid evidence or fail closed without creating training evidence.

**Architecture:** Read/validate prediction evidence separately inside `VerifiedLayaTrainer`, retain original proof age and exact model/adapter/runtime checks, and map the explicit error to ordinary selector fallback. Explicit training proof generation remains separate.

**Tech Stack:** Python private JSON/model-hash validation, Node test runner and small synthetic fixtures; no ML packages or host sessions.

1. Verify the reviewed rc45 base with the existing proof-cache, selector and adaptive-memory unit tests. Use the existing sibling worktree convention under the authorized workspace; no dependency installation or full-suite repetition is needed for this local fix.
2. Replace the old proof-cache regression that expects prediction to auto-smoke with valid preprepared evidence and no-smoke assertions. Add a focused regression for expired/missing/unsafe evidence and exact `inference_attestation_required` fallback. Run against the base and retain expected RED evidence before modifying production code.
3. Add read-only prediction proof loading/freshness validation to `core/context_learning/local_cycle.py`; track verification time for proofs already established by explicit training. Validate model/adapter/runtime as before, but make prediction unable to reach attestation creation. Map the new error in `core/context_learning/live_select.py`.
4. Add real-file synthetic coverage for two host cache roots/cross-host routing, corruption, denied proof access, same-instance expiry/revocation, and explicit train-to-predict compatibility. Test spies must fail if prediction reaches smoke/train; no real ML/teacher calls.
5. Update `docs/continual-learning.md` to state expiry behavior and the distinction from full training attestation. Add a bounded privacy-preserving timing/stderr proposal; do not implement telemetry in this patch.
6. Run the affected proof/selector/adapter-binding/runtime/adaptive regressions, inspect diffs, obtain independent read-only review, fix concrete findings and commit locally. Do not push, merge main, change package version or install/update operating code in this task.

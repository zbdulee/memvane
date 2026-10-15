# Scoped automatic candidate application implementation plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Connect explicitly selected installation learning profiles to durable evaluation and safe candidate publication without changing operating settings or existing grants.

**Architecture:** Keep current execution/data authorization unchanged. A separate owner-private accepted profile binds one project/namespace, exact shared/live policy and runtime/model/view, existing execution approval, settings preferences and optional apply authority. Persist evaluation work in the routed namespace DB. Publish through a private CAS/journal protocol and serialized prepare/activate commands in the existing inference worker. No new daemon, OS service or teacher path.

**Tech Stack:** Python stdlib SQLite/flock, existing inference identity receipts and model owner, deterministic fixture models/fault injection only.

## Design decision and authorization

The approved defaults/application design and latest delegated implementation request authorize this slice. Reuse the singleton's serial owner so a candidate can be prepared without mutating the incumbent; a second long-running worker would duplicate memory/lifecycle. A pointer-only swap would leave inference evidence and live objects inconsistent. Therefore use a prepared object plus durable journal and expected-old pointer compare-and-swap.

The new profile does not grant gold/provider access, install software, replace the base model or amend the existing execution grant. Missing runtime/cohort/identity preparation returns a waiting/review state. New-install recommendations and an explicitly accepted profile are distinct. Existing false capture/autoCycle/shared-learning/application choices remain false in preserving migration; a normal install/update never accepts a profile on the user's behalf.

## Task 1: Profile and preserving setup entry

Create `core/context_learning/learning_profile.py`; modify `core/setup_entry.py`, `core/shared_install.py`, `core/config.py`, setup skill and profile fixtures.

1. RED: recommendation is read-only, new explicit local-auto-apply choice enables only approved local preferences, existing false remains false, missing/wrong acceptance fails, changed project/model/policy/grant/settings and revoked/expired profile fail closed.
2. Build deterministic proposal from actual prepared files and existing runtime train/evaluate grant. Bind source settings digest and proposed preference subset; exact accepted digest is required to write a profile/settings. Use private exclusive locks, durable activation journal and CAS/recovery for profile installation. No arbitrary raw request JSON or caller-selected code trust root.
3. Expose recommended local-auto-apply profile in ordinary installation plan; add explicit profile plan/activate options and optional install selection. Default omission preserves current behavior. Profiles operate only through a training/evaluation-only worker path, not existing teacher processing. Preserve explicit false flags and no new capture/provider scope.
4. GREEN focused profile/setup/config regressions; commit.

## Task 2: Evaluation-only jobs and retry

Create `evaluation_queue.py`; refactor `automatic_staging.py`, `learning_retry.py`, `shared.py` and synthetic fixtures.

1. RED: a staged candidate can resume evaluation with quality permission alone, resource-deferred evaluation is durable/backed off, duplicate wakes/child crash cannot duplicate evaluation ownership, no training/teacher on evaluation retry, changed source/grant/actual-batch quality never passes.
2. Pin training/validation evidence, artifact and approval. Retain existing disjoint case/family/empty thresholds. Recheck before and after inference. Persist metrics/time/validation digest for exact candidate. Bound errors and retries under a stable inherited kernel lease and common resource controller.
3. Dispatch pending evaluation before new training in explicitly selected local profiles; waiting quality may wake only on changed eligible data or normal hook, never busy-loop. Add evaluation to bounded training/evaluation-only waiter without gold calls.
4. GREEN focused staging/queue/resource tests; commit.

## Task 3: Durable candidate publication

Create `candidate_apply.py`; extend `inference_service.py`, `live_select.py`, `recall.py` and fixtures.

1. RED: no profile/no apply selection cannot publish; failed quality/source/hash/grant and low memory preserve old pointer/model; successful preparation does not change incumbent; CAS loses safely; concurrent namespace token mismatch rejected.
2. Journal `intent`, `prepared`, `pointer_committed`, `activated` with old/new pointer and inference-receipt hashes, artifact/quality/profile pins. Use stable private lock and fsync writes. Prepare only same runtime/base/view family in singleton under measured overlap admission and a synthetic read-only forward. No train/smoke/proof/grant renewal.
3. Keep the old service reference until pointer/receipt CAS has committed and activation is revalidated at the serial request boundary. During an incomplete commit, ordinary calls fail closed rather than serve an old head with a new label. Derive candidate inference evidence only from verified unchanged runtime/content plus successful candidate forward; never overwrite the manifest or training approval anchor.
4. Recover crashes before/after each journal/pointer/receipt boundary. Recreate a lost prepared object after worker restart under the same validated intent. Reject foreign pointer edits, stale profile/source/quality and changed generation. Preserve old artifact/object before commit; post-commit revocation requires reviewed recovery, never operating DB rollback.
5. RED/GREEN every fault boundary, caller disconnect, pending inference/resource deferral, retained incumbent, same/different namespace and resumed worker. Commit.

## Task 4: Wiring, review and evidence

Wire evaluation completion into optional scoped auto-apply and selected profile into ordinary shared selection. Keep legacy behavior when no profile exists. Document simultaneous requests: training yields cooperatively at optimizer boundaries; inference compute is serial; prepare/apply uses a request-boundary transaction and may defer for resource/inference demand. No forced kernel preemption or hard GPU reservation.

Run focused isolated tests with fake models (normal host only for actual private socket/flock fixtures), request independent review, repair blockers, commit/push this same feature branch and confirm Node22/24/26 CI. No actual teacher/train/MPS experiment, operating install or main merge. List runtime/model/grant readiness, actual hardware verification, generic corpus enrollment limits, service registration/login startup and product rollout work still outstanding before operation.

## Implementation verification

Tasks 1–3 and wiring are implemented. Final independent read-only review: APPROVE, no remaining correctness/security/data-safety blocker found. It checked profile scope/revocation, actual-batch quality, evaluation-only recovery, journal/CAS/receipt bindings, transient IPC versus authority failures and inference demand priority. Setup reference baseline lacked three selected-profile/reacceptance scenarios; the revised skill passes that same retrieval review.

Focused validation covers 87 Node tests across sixteen related files, with internal Python cases for profiles (12), evaluation queue (10), publication (20), inference service (25) and existing coordination/identity/admission suites. These include real disposable Unix-socket publication with fake models, inherited evaluation-child leases, nine persistence/crash boundaries, stale-quality fresh predictions, source revocation, settings CAS, foreground inference demand and the ordinary capture-false shared-profile recall route. The first aggregate invocation omitted the repository's legacy `QMD_SETUP_GUARD_FIXTURE=1` test setting: 21 recall cases failed closed; rerunning that file with the same fixture setting as `npm test` passed all35. The52 other cases had already passed; no production guard or permission was altered. Earlier isolated setup/config/shared-install/readiness checks also passed. CI and final commit identities are recorded in the task evidence, not inferred from earlier successful runs.

A final two-namespace shared-SQLite regression exposed evaluation pending enumeration across namespaces. The follow-up adds namespace filtering to every evaluation read/write/claim and the staged fallback, preserves an older unscoped feature table for explicit migration review, and verifies that a foreign stage cannot block the current project's next train batch. The affected six-file regression set passes and independent read-only re-review approves this fix. No operating migration was performed.

No actual model/teacher/training/API, operating installation, project flag/grant/proof/pointer, session or inbox change was performed. The remaining reviewed rollout prerequisites and scheduling/resource limitations are listed in `docs/continual-learning.md`. The user's additional service-engineering operating-handoff request is documented separately; historical PIDs are not action targets without fresh identity verification.

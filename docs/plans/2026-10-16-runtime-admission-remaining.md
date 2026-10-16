# Remaining runtime admission implementation plan

Goal: connect the four audited gaps without changing installation, grants, models,
user sessions or inbox scheduling. This is the already authorized continuation.

Architecture: keep the existing UID coordination root and compute lease. Reserve
compute only during a bounded cold start and cooperative preparation. Remote
provider CLIs share a separate nonblocking provider lease, inherited by owned
children; awaiting a response never holds compute. Recheck before reserving each
new teacher attempt; uncertainty after dispatch retains existing no-retry receipts.
An optional warm request without a server cancellation/deadline contract is
deferred, leaving the cheap health check available.

Alternatives considered: one outer compute lock would recurse and block inference
during remote waits; per-project locks would permit aggregate overload. Separate
UID provider ownership plus short compute checkpoints preserves both exclusions.

1. Add fake-signal regressions in `test/runtime_entrypoints_test.py`, observe red.
   Test resource refusal before child/budget creation, cross-session exclusion,
   foreground priority, preparation yield/budget, startup release/reuse and warm.
2. Add shared lane admission to `resource_coordination.py` and a small
   `runtime_admission.py` facade. Bound startup to ten seconds total, preparation
   to thirty seconds with 1 MiB hash checkpoints. Unknown observations defer.
3. Connect `qmd_endpoint.py`, teacher transports, wiki custom/builtin transports,
   `automatic_staging.py` and `project_scope.py` preparation; retain scope, budgets,
   queue ownership and uncertain-send semantics. Propagate resource deferral as
   retryable, never as an authorization loss or a completed task.
4. Run only small sequential fake-signal Python batches under the shared
   development lease. No OS probes, provider/model calls or full suite. Then an
   independent read-only review, repair with regression if needed.
5. Update the audit/coordination documents with resolved coverage and remaining
   real-host validation. Keep the 60-second waiter/next-hook limitation explicit.

No commit/push, install/activation, stress test, bulk backup or new OS service.

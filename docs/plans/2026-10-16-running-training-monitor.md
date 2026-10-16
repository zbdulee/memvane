# Running training and daemon monitor follow-up

This implements the user's two newer requirements: evaluate pressure/demand
during training, and wake retained work without a hook while the shared daemon
is alive. It supersedes the hook-only recovery limitation for that daemon's
lifetime, not for reboot/login or when the daemon is absent.

Existing path confirmed: `laya_adapter.train -> run_updates -> TrainingGuard.step`
owns compute through one optimizer update and its two-slot cursor commit. It
already chooses microbatch2/1 and yields at whole-group boundaries. The parent's
group RSS/deadline watchdog exists but skips routine OS pressure for coordinated
children; a long forward/backward has no bounded cooperative response. Final
head creation before completion is another crash boundary to preserve safely.

Plan (all source/mock work, existing operating hold remains):

1. Add a bounded stop pipe from the existing LocalTrainer parent to its owned
   child. Check at each microbatch boundary before optimizer mutation, while
   retaining whole-group commit and exact RNG/head cursor recovery.
2. Parent polling classifies routine demand/load/warning as a cooperative yield
   request, with a bounded grace period; critical memory/RSS or unavailable
   required monitoring uses owned-process-group emergency cleanup. No SIGSTOP,
   external PID discovery or user-process/session changes. Unknown cleanup is
   an unresolved reservation, never success or permission to overlap training.
3. Test interrupted writes at the inactive head slot and final publication;
   preserve the last normal committed cursor, do not delete uncertain artifacts
   or duplicate a queue claim. Distinguish cursor-safe retry from review-needed
   final publication uncertainty if exact recovery cannot be proved.
4. Add one model-free monitor child to the existing shared inference daemon.
   A UID kernel lease prevents duplicates; an inherited lifecycle pipe ties exit
   to that daemon. Sampling/queue wake runs separately from model work and
   compute/writer/trainer locks. Use existing bounded dispatch and backoff; no
   teacher replay, new grants, OS login service or new security permission.
5. Add fake-clock/signal/mock-trainer tests first. Run only small sequential
   checks under the shared development lease, then one independent review.

Proposed timing must be reported as cooperative/sampled limits, not proven hard
real-time behavior: parent stats/probe budgets, yield grace, TERM/KILL waits and
monitor interval all need explicit tests. Real model/GPU release and OS latency
are unmeasured and remain held. Record self-cost estimates and the exact lifetime
boundary. Existing sessions/inbox stay active; no main, push, install/activation.

Source/mock execution status: steps1–5 implemented;123 small checks passed
sequentially. Independent review found two P2 issues (leader-only cleanup with
no durable unknown barrier; cursor count trusting itself). Both now have red
regressions and repairs: group absence verification plus a durable common
cleanup reservation, and a separately pinned actual group plan. Narrow
re-review found no remaining P1/P2 in this repair scope (conditional source
approval). Real host/GPU training, costs and release latency remain
unverified; operating hold remains true.

Existing sessions and inbox automation remain enabled per the parent's latest
repeated instruction. An older handoff message was mistakenly treated as newer;
the conflicting pause prerequisite has been withdrawn. No automation or process
was changed. Previously reported PIDs are historical and must never be used as
an unverified termination list.

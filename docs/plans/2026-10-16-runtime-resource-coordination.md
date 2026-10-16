# Runtime resource coordination implementation plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make the plugin, rather than its operator, defer and resume background work while preserving retrieval priority and durable progress.

**Architecture:** Reuse the per-OS-user inference Coordinator and its control/compute/trainer lock order. Add a small persistent wake queue for existing update/index/learning handlers; a bounded waiter sleeps without writer/compute/trainer locks. No new login service or permission. Existing content queues and training cursors remain authoritative; the wake queue contains paths and handler kinds only.

**Tech Stack:** Python stdlib SQLite/flock, existing Bash workers, synthetic sequential Python unittest.

## Findings and design decisions

- Existing `resource_coordination.py` serializes trainers and optimizer steps, gives Laya inference demand priority and shrinks microbatches. `training_cursor.py` commits whole optimizer groups. The inference service is one user-level process.
- Gaps: ordinary QMD retrieval does not advertise priority; QMD update/embed use writer/embed locks independently of learning compute; non-adaptive reports may ignore OS pressure. The learning waiter lasts at most 60 seconds and other retained queues usually wait for another hook.
- Reuse Coordinator instead of adding another resource semaphore. Use OS-wide pressure/load/headroom to account for all sessions/projects; do not sum the same model estimate in multiple schedulers. Existing memory policy owns model peak admission and measured resident credit.
- Serialize background compute at one operation/update boundary. Shrink training microbatch on CPU/headroom pressure. QMD CLI operations cannot be safely suspended internally: finish the current command and defer the next boundary, retaining the exact durable claim. Never SIGSTOP a process holding SQLite/writer locks.
- A UID-global durable wake queue coalesces index/update/learning requests, version-checks ACK against concurrent enqueue and recovers interrupted dispatch after obtaining its kernel worker lease. One bounded waiter rechecks resources every five seconds, without content/compute locks. Healthy resumption within that window needs no additional hook. After expiry, logout or reboot, the next normal hook wakes the persistent queue; no always-on or boot service is claimed.
- Runtime wake paths revalidate existing setup, scope, consent and approval. Learning resource retries use only local training/evaluation; teacher calls remain controlled by the existing normal worker. No source bodies or arbitrary executable argv in the new queue.

## Sequential implementation and verification

1. Write failing small tests for fake OS pressure/recovery, CPU/memory batch reduction, cross-session priority and compute exclusion, persistent wake recovery, revision-safe ACK and duplicate consumer exclusion. Use private temporary roots and tiny Python children only.
2. Extend existing Coordinator admission/OS signals and connect foreground recall; retain current model admission ownership. Ensure coordinated training yields at optimizer boundaries instead of routine pressure killing from the parent.
3. Add the persistent bounded wake queue and fixed existing handlers; connect update enqueue, index worker exit and local learning deferral. Route QMD writes through the same compute lease without recursive acquisition.
4. Run only the approved lightweight tests sequentially. Keep development heavy hold true, no models/teacher/API/operating configuration changes. Read-only independent review starts only after all tests finish.
5. Document exact coverage, boundary limitations and hook-dependent recovery outside the waiter lifetime. Keep changes on the current feature branch; no main changes or push.

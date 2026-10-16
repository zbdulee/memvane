# Development resource safety

## Authorization and local holds

The machine-local, ignored `.development-hold.json` can refuse every child job,
including ordinary tests, backups and hashes. Release a hold only within explicit
user authorization. There is no command-line bypass. Keep session-specific
approvals and current OS observations in local evidence rather than this document.
Authorization never overrides resource, process ownership or cleanup checks.
Sampler-only mode launches no child; its completion cannot complete a prior job.

Interrupted test logs and partial backups remain incomplete until their own
completion evidence exists. A historical WindowServer watchdog panic did not
establish CPU or memory load as its cause.

## Commands covered

`npm test` and `npm run test:focused -- test/NAME.test.mjs` route through
`scripts/development_guard.py`, force `node --test --test-concurrency=1`, and
preserve the existing QMD fixture/log isolation. Backup and hash work must use
`npm run dev:guard -- --kind backup -- COMMAND ...` or `--kind hash`.
The wrapper launches an argv directly, never constructs a shell command.
The complete `npm test` suite has a finite1,800-second budget; focused checks
keep the wrapper's900-second default. This accommodates the observed sequential
CI duration without allowing unbounded execution.

Ordinary mock, regression and integration tests (`--kind test`, including CI)
do not use model-work OS pressure, CPU/RSS, relative headroom, swap or system-load
admission limits. They retain one global lease, file concurrency1, finite wall
time, a process-count limit, verified ownership and abnormal-child cleanup.
Actual model-loading/training tests must use `--kind model-test`; an explicit
calibration envelope also always retains resource limits. This development-test
distinction does not change any installed runtime learning admission.

All kinds and all worktrees share `~/Library/Logs/qmd-development-guard/heavy.lock`
on macOS (`~/.local/state/qmd-development-guard/heavy.lock` on Linux). The home
comes from the OS UID database, so an isolated `HOME` variable cannot create a
different global lease. Checkpoints and logs remain on disk across a reboot.
One process holds the lease across preflight, execution, monitoring and cleanup.
Tests must not overlap backup/hash work or independent review. Direct shell
commands and older checkouts are not automatically intercepted: agents must use
the wrapper. `npm run test:guard-unit` is the narrow bootstrap exception for the
small Python-only safety tests, which perform no real host probes or model work.

## Resource-limited work policy, not measured safe thresholds

The 35% available-memory proposal is conservative and uncalibrated. On a 24 GiB
machine that is approximately 8.4 GiB, but macOS `memory_pressure -Q` measures a
different notion of headroom than free pages alone. Admission and continuation
also require normal OS memory pressure, swap growth no greater than 1% of
physical RAM since admission, and load per CPU no greater than 0.75.

The aggregate observed child tree is initially limited to 15% of physical RAM
RSS, 25% of total CPU capacity and 32 processes. CPU is the OS `ps %cpu`
lifetime-average estimate, not an instantaneous hard CPU quota. Thread pool
environment defaults are set to one thread. Shared pages can be counted more
than once in summed RSS, so rejection can be conservative. A single process
limit is not used as a substitute for the sum of parent and descendants.

Sampling defaults to once every five seconds (allowed range 2–10 seconds), with
a finite 900-second job budget (maximum 1800). Sleep and each OS probe are bounded
by the remaining wall-time budget; cleanup has a separate ten-second probe budget.
Invalid or unreadable OS/process observations refuse
admission or stop the owned task. Sandbox EPERM is not treated as a dead process
or permission to weaken the guard. Linux uses MemAvailable and memory PSI;
unsupported monitors refuse execution.

## Ownership, stop and checkpoints

The child starts in its own session. The monitor follows its verified process
group and all observed descendants, including descendants that create another
group or later reparent. Once the last verified original-group anchor disappears,
that numeric PGID is permanently closed to adoption. Each PID is bound to its
observed start time; different-start reused PIDs are excluded.
Before every signal, identity is read again. Only those verified owned PIDs
receive TERM, followed by KILL if still alive. No user application, process name,
unverified process group or existing session is terminated.

Private `runs/ID/samples.jsonl` under the persistent development log directory records system pressure/headroom/swap/load and
each observed child's CPU/RSS/start identity, without command arguments or
environment secrets. `checkpoint.json` and `active.json` distinguish passed,
failed, stopped and unsettled cleanup. A resource stop is never completion.
Unknown root identity/failed cleanup or a killed supervisor leaves an unsettled
checkpoint and blocks the next guarded job pending manual inspection. Child
data, test logs and partial backups are retained; cleanup does not delete them
or roll back operating databases or models.

## Limits and resumption

This is a cooperative, sampled guard, not a macOS aggregate hard memory/CPU
containment facility. A child that forks, detaches and loses its parent entirely
between samples may evade ancestry discovery; short peaks can also occur between
samples. `ps lstart` has second precision and a check-to-signal race remains, so
this is not a proof against every possible PID-reuse race. Detaching jobs are
therefore not approved merely because this wrapper
exists. They need explicit lifecycle/ownership containment before heavy work is
resumed. Missing process identity makes cleanup unsettled rather than justifying
signals against user processes. Sudden OS failure can interrupt checkpoint
writes; an existing interrupted run must be inspected, never relabeled passed.

A resource stop requires fresh acceptable OS observations before another
resource-limited job. An authorization hold additionally requires explicit permission to resume.
Use a finite sampler when necessary; do not create background polling by default.
Keep existing user sessions and automation running unless their interruption is
separately authorized. Record the current checks and blockers in local evidence.

## Historical validation recorded 2026-10-16

The 16 small sequential guard unit tests passed in 0.016 seconds. These use
mocks and tiny temporary files, not real host termination or resource stress.
The final independent read-only review found no remaining P1/P2 in the reviewed
changes, conditional on keeping the heavy-work hold. A direct guarded test
request exited 75 with `work_remains_on_hold` before launching a child.

The authorized normal-host sampler completed with two observations (0.13s and
3.31s) within its six-second budget: available-memory indicator 53%, OS pressure
normal, swap zero, load per CPU 0.239, sampler RSS approximately 19.4–20.2 MiB.
Record: `~/Library/Logs/qmd-development-guard/runs/sampler-eda89a5dc76645c68d4884e5ac756d61/`.
These observations do not establish safe heavy-work thresholds or explain the
earlier reboot. No full regression, bulk backup/hash, model operation, real
process termination or operating configuration change was performed here.

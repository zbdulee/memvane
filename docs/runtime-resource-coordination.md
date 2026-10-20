# Plugin runtime resource coordination

Initial scope audit: [remaining entry points and hardening](plans/2026-10-16-runtime-resource-audit.md).
The source follow-up below connects the four identified gaps. Neither the earlier
56-case result nor the new fake-signal checks establish operating safety.
The newer user instruction also adds [running-training control and daemon
monitoring](plans/2026-10-16-running-training-monitor.md), described at the end.

This change belongs to `feat/general-project-auto-learning`. It has not been
installed or activated. Existing sessions and inbox automation remain enabled.
The development execution guard and its heavy-work hold remain separate.

## Existing coverage and gaps

`context_learning/resource_coordination.py` already provided one per-OS-user
trainer lease, inference-demand slots and a compute lease. Laya optimizer groups
used that compute lease and `training_cursor.py` committed their heads/RNG before
advancing. The singleton inference service shared those leases, and additive
model-memory policy reduced microbatch size without changing the effective group.

Ordinary QMD retrieval did not advertise inference demand. QMD update/embed had
writer/embed locks independent of model compute. Some non-adaptive workers did
not collect OS pressure; retained jobs often depended on another hook after a
short learning-only waiter expired. These were the runtime gaps, separate from
development test/backup scheduling.

## Connected runtime paths

- `recall.main` advertises demand throughout retrieval and context construction;
  it does not acquire compute or wait for a background QMD command to finish.
  The existing Laya service still owns model inference serialization. Optional
  priority-registration failure does not discard otherwise available retrieval.
- `update.sh` update/embed and `backend/index_worker.sh` QMD operations enter the
  same Coordinator compute lease as learning. A busy lease, foreground demand or
  resource refusal returns 75; the worker releases writer/embed locks and retains
  its durable job/claim. Resource refusal bypasses in-lock shell retry sleeps.
- Post-embed orphan cleanup uses that same lease. Resource refusal preserves
  its pending marker and a routed cleanup wake without consuming a failure
  cooldown. Retry resolves the current project's index again, instead of
  inheriting another project's INDEX_PATH.
- The queued SessionStart worker waits for its embed child and ACKs only the
  actual `completed` phase. Lexical-ready or embed-failed cannot erase that job.
- `LocalTrainer.call` also admits old explicit adapter calls through the shared
  compute lease. Already coordinated v2 adapters skip that outer lease and use
  their existing per-group TrainingGuard, avoiding recursive acquisition. Raw
  QMD and legacy children inherit their operation lease, so parent exit cannot
  release compute while that child is still running.
- Runtime OS reports include macOS pressure level plus `memory_pressure -Q`,
  total/available memory and system load. Linux keeps PSI semantics rather than
  inventing a macOS free percentage. Denied/unknown observations defer work.
  Foreground inference has its existing lower absolute admission floor and now
  also shrinks its batch at high CPU load.
- Background compute concurrency is at most one at the shared boundary. CPU
  load over 75% of its configured load threshold shrinks training microbatch
  to one. Memory admission requires additional task memory plus an explicit
  system reserve. The earlier unconditional35% admission/40% recovery floors
  have been removed;35% is a soft post-allocation expansion goal. Unverified
  batch shapes stay at one. Existing peak estimates and measured owned resident
  credit stay with memory policy; the wake queue does not reserve or add that
  model peak a second time. System observations include other projects,
  sessions and user workloads without scanning or terminating them. See the
  platform policy section below for estimate provenance and compatibility.

## Durable wake and restart behavior

`runtime_work.py` stores only fixed handler kinds, project paths/update keys,
revision, due time and attempt state in private `runtime-wakes.sqlite3` beside
the UID-global Laya coordination locks. The root comes from the OS UID home,
not overridden HOME/CODEX_HOME. It stores no prompts, bodies, arbitrary argv or
credentials. Original content queues, claims and model cursors own actual work.

Dirty enqueue, SessionStart enqueue and local learning launch persist wake hints
before detaching. Python hook completion and SessionStart enqueue wake a single
bounded waiter. The worker lease is passed through spawn to prevent one waiter
per session. Index readiness work precedes learning. Version-matched wake ACKs
preserve an enqueue that arrived during dispatch. After a crash/reboot, obtaining
the worker kernel lease allows interrupted `running` hints to become pending;
the original handler still revalidates setup/scope/authorization before work.
Wake revisions increase across row deletion and recreation. When the primary
queue is full, a bounded private overflow table retains coalesced wakes for
promotion as slots open; when both stores are full the caller receives an
explicit capacity deferral. The learning request file remains for the next
normal hook in that case.

The waiter polls at five-second intervals for **at most 60 seconds of dispatch
loop lifetime and eight dispatches**. It sleeps with only its own dispatch lease,
never compute, trainer, writer or embed locks. Resource recovery during that
window can resume without another hook. A dispatch already executing at the end
of that budget finishes its current operation, so this is not a hard task wall
time. Backoff grows from five seconds to at most 300 seconds, and pending work
remains durable when the waiter exits. **With the shared Laya daemon and its new
monitor alive, retained wake hints can now restart a waiter without a new hook.
When that daemon/monitor is absent, after logout or reboot the next normal hook
is still needed.** No boot/login service,
LaunchAgent, new credential or security permission is installed.

Learning retries recheck the common setup guard and retain the existing history
worker fence. The runtime dispatcher starts at most one owned learning child
per private learning root and returns immediately, so index and update wakes
can progress while the child runs. A general/legacy full request is persisted
before its child starts; after a crash the request is retried under a bounded
restart count. The teacher's existing uncertain-send receipts still decide
whether any external call may be repeated. Once the teacher phase completes,
a resource-deferred training phase hands off to a training-only child, which
never calls the teacher. The selected-profile retry also uses that child.
Training pressure stops at the existing committed group boundary and the
next admitted child resumes from the training cursor. Existing consent,
quota and approval controls remain in the worker. Legacy auto-cycle resource
refusal retains its phase receipts and cadence instead of becoming a 24-hour
quality failure. An install/update transition preserves the wake until setup
is ready; no new project enrollment or broader grant is inferred.

## Safe boundaries and limits

Routine pressure yielding happens before the next full optimizer group, after
the previous head/cursor was committed. The parent does not independently kill
an adapter during that resource yield. Existing emergency RSS, monitor failure
and wall-time failure cleanup remain failure paths, not successful checkpoints.
QMD CLI and legacy non-cursor adapter calls are indivisible boundaries: they
finish the current call, then refuse the next one if pressure has risen. The
plugin never SIGSTOPs a job holding shared locks, and signals no existing user
session or inbox process for scheduling.

This is cooperative scheduling, not an OS hard aggregate CPU/RSS cap. System
load is a moving average; brief peaks or another app allocating memory inside
an indivisible command can outpace the next observation. QMD batch sizing has
not been invented through undocumented CLI flags. Foreground retrieval gets
admission priority but cannot preempt an already running atomic QMD operation;
SQLite/backend contention may still affect latency. Compatibility requires all
participating installed copies to use the same Coordinator. Explicit operator
tools outside these plugin paths are not intercepted. Real model/load/overload
and full-suite validation remain unexecuted and operating rollout remains held.

## Lightweight verification

Only sequential synthetic unit checks are authorized for this change. They use
fake OS signals, private temporary SQLite/files and tiny Python children; they
load no model and send no source/API requests. Tests cover pressure rise/recovery,
per-group cursor resume, cross-session priority and compute exclusion, inherited
lease lifetime, durable running-job recovery, coalescing, revision-safe ACK,
single consumer, queue backoff/budget, hook wake, dirty wake persistence and real
embed-phase ACK. Existing additive-memory and learning coordination checks are
run separately, sequentially. Independent read-only review follows all checks.

Recorded result: 25 runtime unit checks, 17 existing coordination/cursor checks
and 14 analytical adaptive-memory checks passed sequentially (56 cases total).
The final runtime check run took 0.194 seconds; no live model or stress test was
run. Bash syntax and the modified Python sources were also parsed. Independent
read-only review identified cleanup lease/cooldown and setup-transition gaps;
the added regressions cover those fixes, and the final review reported no
remaining P1/P2 within its scope, conditional on keeping operating rollout held.
The Node full-suite wrapper was added for future CI but not executed here.

One intermediate failing cleanup test omitted its temporary wake-root patch.
Opening the real runtime-wakes file was denied by the workspace sandbox before
creation/write; no operating DB was changed. The test now explicitly pins the
temporary root and mocks wake spawning. No sandbox permission was expanded.

## Remaining entry points follow-up

Implementation stays on the same dirty feature branch (HEAD `a3cb36c`); it is
uncommitted and uninstalled. Existing sessions and inbox remain running. No old
PID list is an execution prerequisite. The development hold remains true.

| Entry point | Ownership and admission | Bounded refusal/fallback |
|---|---|---|
| QMD cold start/reload | Existing endpoint ownership lock plus common fresh resource admission and compute lease, only through bind/health proof or cleanup; a durable private startup reservation survives parent loss | Endpoint ownership lock and compute contention defer immediately. New startup scanning/readiness shares a ten-second budget. No compute FD is inherited by the service daemon. A healthy proved endpoint bypasses cold admission. Reload checks admission before stopping its owned daemon. Cold start may satisfy a foreground demand; ordinary retrieval keeps its existing timeout/empty-context fallback. |
| general/synthetic/manual teacher and quota CLI | One private UID `provider.lock`, separate from compute. Admission precedes source/budget mutation; every new teacher phase rechecks before a reservation and every actual Popen rechecks immediately before launch, even for inherited/nested lanes. The owned CLI inherits the provider FD. A nested builtin adapter verifies an already-held inode/lock and reuses it. | New work never waits for this lane. Initial refusal retains queued content and returns `deferred_resource`. A late pre-send rejection preserves its reservation/receipt for review, described below. Awaiting a remote response does not hold compute; remote launch while owning compute is refused. Existing per-call timeouts, consent, quota and uncertain-send/no-repeat receipts remain authoritative. A sent call is not automatically replayed by the wake waiter. |
| wiki extract/verify/dedup, custom argv and topical provider attempts | Shared provider transport for custom children and direct builtin adapters. Explicit topical and manual budget flows acquire admission before reservation. | Pre-send resource refusal is exit75/`resource_deferred`; compile/verify/dedup keep jobs without failure cooldown or changing engine. Owned children use a bounded process group. An uncertain explicit attempt remains subject to its original receipt and retry policy. |
| optional vector warm | Health probe remains cheap; opt-in warm reaches the common admission facade | QMD has no attested server-side cancellation/deadline contract, so vector POST is withheld with `warm_server_deadline_unavailable`, including on a healthy host. A curl timeout is not treated as server completion. |
| model/artifact hashes, installed binding/inventory and cohort preparation | Common compute lease in a reentrant preparation scope; existing worker/namespace locks retained | Each 1MiB hash read and cohort/installation checkpoint releases and rechecks compute. Fresh observations are reused for at most0.5s within the same preparation, still subject to the two-second freshness rule. Background preparation has a thirty-second budget. The verified owner of a foreground inference slot may prepare without rejecting its own demand, using nonblocking compute and the smaller of one second and the remaining hook budget. All other background contexts still yield to demand. Partial hashes are never trusted or published; a later attempt recomputes them. |

The cold-start 2048MiB estimate and preparation/provider 512MiB estimates are
admission estimates, not measured peaks or enforced RSS caps. Startup cleanup may
add the existing bounded child waits to the readiness budget. Cooperative checks
cannot interrupt a kernel file read. A preparation denied partway through may
have added cohort exposure assignments; these are conservative history, not a
completed learning job, and are never deleted to obtain fresh identities.

Preparation ends before trainer dispatch, so nested local trainer admission does
not acquire compute recursively. Resource deferral is kept distinct from an
authorization failure in the affected train/evaluation and worker return paths.
The common coordinator still does not preempt an indivisible already-admitted
QMD/model operation or impose an OS hard CPU limit. Raw operator tools outside
the supported plugin entry points remain outside this contract.

The original **60-second bounded waiter** is unchanged. The newer daemon monitor
can now wake another bounded waiter while the shared daemon is alive; outside
that lifetime the next-normal-hook dependency remains. No login/boot service was
created. No provider/model was called to validate this admission follow-up;
synthetic provider responses are in-memory mocks. Installation, activation,
full regression, bulk backup and overload tests
remain held. In particular, no claim about current rc46 behavior follows from
these source edits.

The initial focused checks passed sequentially:21 new entry-point cases,10 prior
hardening cases,25 prior runtime cases and3 existing mocked gold receipt cases.
The new batch took0.176s and the reused gold cases0.070s; test RSS was not measured.
Twenty-two affected Python sources parsed. Independent review is in progress;
this paragraph will be updated with the final reviewed result.

Before operating use, retain the earlier minimal plan: finish source review and
appropriate later CI of the complete dirty feature; then, only under future
explicit authorization, a normal-host tiny synthetic index/query/update pilot
with existing models and process-tree/system measurements. Check two-session
contention, worker restart/queue retention and real hook loading. No teacher or
training call is needed for that first pilot, and no operating DB pointer change
or user-session restart is implied. Real cold peaks can be GiB; the prior
model-free waiter tens-of-MiB estimate remains unmeasured.

Final source review corrections and limits:

- Startup publishes/fsyncs `startup-reservation.json` before a child can be
  launched or an owned reload can stop its daemon. A parent crash or unproved
  cleanup leaves this barrier after the kernel compute lease closes. All common
  compute, inference-compute, provider and new-start admissions then return
  `qmd_startup_recovery_required`; they never wait indefinitely or duplicate the
  start. Only the current nonce owner removes it after proved publication or a
  known child exit. **There is no automatic orphan-reservation recovery, TTL
  clearing or blind PID adoption.** A surviving reservation needs explicit
  ownership/proof review before a later repair. This conservative fallback can
  defer optional selector inference; available QMD retrieval keeps its fallback.
- A provider's durable dispatch-intent write is followed by another fresh check.
  `PreSendDeferred` is emitted only before that Popen was called. For gold work,
  a late resource refusal records `pre_send_rejected/resource_deferred`, retains
  its already consumed conservative budget reservation and leaves the case in
  `pending_review`. It is not automatically sent again. Initial refusal, before
  reservation, remains queued/retryable. This does not claim the configured
  reservation was an actual provider charge. Existing uncertain dispatched
  requests keep their no-repeat receipts. A remote lane is never borrowed as
  permission to send while holding compute.
- Preparation can borrow only the verified current process/root/FD compute
  ownership (including the service's direct apply gate), preserving the owning
  request deadline and leaving that parent lease intact. Foreground preparation
  verifies its own inference-slot capability rather than ignoring global demand.
  Oversized cold identity work may exhaust its short budget and return the
  original QMD result; real selector frequency/latency remains to be measured.
- Probe time is included. `resource_report` shares one deadline across all macOS
  subprocess probes: at most two seconds or the caller's shorter remaining
  budget. Exhaustion starts no later probe, makes the report unready, and is
  checked again before preparation admission. Process creation/cleanup and a
  kernel file read are still cooperative boundaries, not hard real-time caps.
- An early training-preparation deferral retains the existing private diagnostic
  `resource-defer.json` with `completionAck:false`; it does not invent a planned
  batch or consume a training operation.

Final sequential verification:32 new entry-point cases (last run0.226s),10 prior
hardening,25 runtime,17 coordination/cursor and3 in-memory gold-receipt cases
passed (87 unique cases). The87-case run preceded the last one-line diagnostic
receipt restoration; all32 new cases were then rerun with that assertion and
passed. Twenty-two affected Python sources parsed, and the final receipt change
was parsed again. No actual OS resource probe, model, account/provider CLI or
operating workload ran. The existing runtime batches may create one tiny Python
fixture child at a time. Node backend expectations were updated for deferred warm
but the Node wrapper/full suite and the larger staging/teacher fixture suites were
not run. Test RSS and real resource peaks remain unmeasured.

The sequential independent read-only review found and resolved startup-parent
loss, final-send freshness, compute self-contention, foreground self-demand and
probe-budget gaps. Final review found no remaining P1/P2 in that reviewed scope,
conditional on the orphan fallback and held real-host verification. The final
diagnostic restoration is a reuse of the existing receipt writer, covered by the
last new-test run. None of this is an operating rollout approval.

## Running training and daemon lifetime monitoring

The user subsequently authorized continuous decision-making during the existing
daemon's lifetime. The earlier unconditional hook-only limitation is superseded
for **the shared Laya inference daemon with its monitor attached**. A QMD HTTP
daemon running alone does not host this new monitor. No daemon was started or
updated in this work; current operating behavior remains unchanged.

`inference_service._serve` supervises one separate model-free monitor child.
Sampling therefore does not wait behind its model scoring thread, GPU work or
compute/writer/trainer locks. A UID kernel lease prevents duplicate monitors.
An inherited pipe carries only the daemon lifetime: EOF stops the monitor after
an in-progress bounded observation. The daemon supervises monitor exit with a
30-second restart backoff. Its existing version response exposes monitor status;
spawn denial remains unavailable, never a permission bypass. Shutdown closes the
pipe and reaps only the monitor it created, with bounded cleanup.

The child checks registered durable wake hints every5seconds when work exists;
with an empty queue it waits30seconds and performs no OS pressure probe or worker
spawn. Observations occur before the nonblocking coordination check. Healthy
recovery requires the task/reserve budget, load below65% of its configured
threshold and five observed stable seconds, then wakes the
existing bounded60-second/eight-dispatch worker. Queue capacity, revision ACK,
claim leases and five-to300-second backoff are unchanged. It does not launch a
teacher, create new training examples, broaden grants or scan arbitrary projects.
Jobs lacking a registered wake hint still need the ordinary enqueue/hook flow.
No login/reboot service is installed; the monitor stops when its daemon stops.

The real v2 trainer path is `laya_adapter.train -> run_updates -> TrainingGuard`.
Each full optimizer group holds compute through its two-slot head/RNG cursor
commit. Resource admission can reduce microbatch2to1 while retaining effective
batch2. The next group reevaluates system memory/load and foreground demand.
New stop-pipe polling also occurs before each microbatch and before optimizer
mutation; parent EOF is a one-way yield request. An interrupted partial group's
gradients are discarded, then a fresh process resumes its last committed cursor.
It never leaves a SIGSTOPed process holding model memory or locks.

The existing LocalTrainer parent now observes coordinated train/evaluation
children while they run, rather than skipping their OS pressure checks. Process
group RSS/CPU is sampled at most twice a second with a0.25s probe timeout.
Internal system probes share a0.5s budget and run about once a second; child
per-group admission retains its own short freshness contract. CPU load is an
OS moving average, not instant app-launch detection. Foreground search demand
is an explicit live kernel slot; no app surveillance permission was added.

Routine warning/low background headroom, CPU overload or foreground demand sends
one cooperative stop byte. If the child has not yielded within2seconds of that
observation, cleanup sends TERM to its newly created process group, waits0.5s,
then KILL and another0.5s. Critical OS memory pressure, free percentage<=5%,
remaining memory below256MiB, RSS overflow or unavailable required
monitoring skip that grace and use KILL plus a0.5s reap wait. After either path,
an additional bounded0.5s group-presence check must prove the process group
absent; leader exit alone never establishes success. These are initial
conservative policy values, not measured safety thresholds. Normal scheduling
and bounded probe assumptions suggest roughly2–3seconds for an emergency
observation/stop cycle and up to about5–6seconds for an unresponsive routine
yield. **They are not proven OS/GPU real-time upper bounds.** Process creation,
kernel stalls, cleanup denial and delayed GPU memory release can exceed them.
No existing user/inbox process or unrelated PID is selected or signaled.

Cleanup failure returns `trainer_cleanup_unavailable`, retains a non-completion
diagnostic and creates/fsyncs a private `cleanup-reservation.json` in the common
UID root before returning. Common compute, training, provider and startup paths
refuse `qmd_cleanup_recovery_required` even after the trainer claim is released.
An existing reservation is preserved. There is no TTL deletion or automatic
recovery: normal-host proof and explicit resolution are required. Even a normal
leader's successful output receives no ACK until its group cleanup is proved.
Audited coordinated children also inherit the trainer/claim lease.
The parent stop pipe's EOF lets a surviving child yield at its next boundary.
An OS refusal or a child stuck inside an uninterruptible driver still needs
normal-host diagnosis; no guard is disabled to force cleanup.

Only fully committed optimizer groups advance the durable cursor. Killing a
write to the inactive head slot keeps the preceding normal head/RNG intact.
Cursor rows now also pin total group count. Before any update, the adapter pins
the actual generated groups' hash/count and request identity independently in
private `group-plan.json` (exclusive creation, file/directory fsync). Existing
plans must match actual groups and are never overwritten. Fast final recovery
also requires that independent plan; a cursor with only its own count damaged
cannot claim completion. Older cursors without a plan must pass actual adapter
group validation first. If all groups completed, this exact
request/plan/cursor/head agreement can produce the final private candidate/completion
without another model call. Final copying uses a private scratch file and atomic
publish; an unknown existing final artifact is preserved for review instead of
retrained or overwritten. Candidate completion does not change an active model
pointer. Original queue claims, training cursors and completion receipts prevent
duplicate consumption; recovery never resets an external teacher receipt.

The monitor adds one Python process with no model, plus an existing bounded
dispatcher only when a hint is due. Idle polling is cheap file/SQLite work; its
RSS is expected to be tens of MiB, **unmeasured**. Active training has extra
process-stat/system probes and cursor writes; exact CPU cost and GPU/RSS peaks
remain unmeasured. No performance certification follows from mock clocks.

Verification after review repairs:25 running-watch/monitor cases and
32 entry-point,10 hardening,25 runtime,17 coordination/cursor and14 analytical
memory cases passed sequentially (123cases). The new batch took0.303s; other
batches were below0.52s. The existing three mocked gold receipt
cases were retained as earlier evidence rather than rerun. Real trainer-loop
wiring was checked using an analytical fake model/optimizer, and the daemon
attach/tick/close boundary used a fake socket, not a real daemon/model. Eleven
affected training/monitor Python sources parsed. Full regression, actual
model/training, GPU/OS overload, install/activation and operating recovery remain
unexecuted and held. The first independent review found the exited-leader and
cursor-count defects above; both were reproduced before repair. Narrow
independent re-review confirmed both resolved, with no remaining P1/P2 in that
scope. This is conditional source approval, not operational readiness: actual
GPU release, OS latency and monitor lifetime are still unverified.

One cursor regression initially entered the default preparation path without a
temporary coordination-root patch. It ran small read-only OS resource probes and
was then denied opening the actual control lock by the sandbox. No operating DB
or model changed. Recovery preparation now runs only when its cursor exists;
the no-cursor uncertainty check completes before that path, and the17-case batch
passed without expanding permissions. This accidental probe attempt is distinct
from the deliberately fake observations in the final verification batches.

## Explicit CPU parallelism follow-up

`runtime_cpu.py` centralizes the25% CPU fraction used by the development guard
and the managed child's nominal parallelism reservation. A private UID kernel
pool contains `floor(logical CPUs*0.25)` one-thread shares:18logical CPUs admit
4shares, not4.5 rounded up. Every LocalTrainer child and provider Popen, Laya
shared/legacy resident worker, QMD daemon and guarded QMD operation reserves one.
Busy/unknown capacity defers without stealing a lease. Spawned children inherit
CPU descriptors; the parent's close does not release a share while an inherited
copy remains open. No daemon inherits the startup parent's compute descriptor.
Daemons conservatively retain their CPU share while idle. Fewer than4 or unknown
logical CPUs cannot fit one share and defer rather than assume capacity.
At4–7logical CPUs, QMD can occupy the only share; at8–11logical CPUs, QMD+Laya
can occupy both. Training/provider work can then remain deferred until a share
is actually released. Normal OS pressure alone cannot release an occupied
share; the policy does not automatically stop daemons or expand its fraction.

Before dependent numerical imports, managed child environments overwrite unsafe
parent parallelism values with1:OpenMP, MKL, OpenBLAS (including its default
thread setting), NumExpr, Rayon and optional vecLib hints. Nested OpenMP levels
are limited to1 and tokenizer parallelism is disabled. Installed NumPy's
`__config__.py` identifies scipy-openblas with MAX_THREADS64; setting only OMP
would not establish that backend's pool size. The adapter's `_torch()` also sets
CPU intra-op and inter-op threads explicitly and reads back both values. It does
this once per process/module before model construction, eager or autograd work;
later getter drift or unavailable/too-late API calls fail closed. All train,
predict, smoke and loaded inference paths use it. Direct script execution finds
the helper before the request's training guard runs. No real Torch was imported
to verify these settings in this follow-up.

The installed PyTorch2.14 `torch/_torch_docs.py` (setters near10185), installed
Transformers `pipelines/base.py` (tokenizer setting near1206) and installed NumPy
build metadata were read as text. Official references:
[Torch intra-op](https://docs.pytorch.org/docs/2.14/generated/torch.set_num_threads.html),
[Torch inter-op](https://docs.pytorch.org/docs/2.14/generated/torch.set_num_interop_threads.html),
[OMP/MKL](https://docs.pytorch.org/docs/2.14/threading_environment_variables.html),
[NumPy BLAS controls](https://numpy.org/doc/stable/reference/global_state.html),
[OpenBLAS runtime controls](https://www.openmathlib.org/OpenBLAS/docs/runtime_variables/),
[Rayon pool defaults](https://docs.rs/rayon/latest/rayon/struct.ThreadPoolBuilder.html).
Optional portability hints are not verified controls for the installed backend.

Each trainer's whole observed process group is compared to its allocated
100%-per-share CPU allowance (100%means one logical CPU in `ps`). Exceeding it
uses the existing cooperative yield/grace/TERM/KILL route. OS load/pressure,
foreground demand, RSS, durable cursor and unknown-cleanup gates remain binding.
QMD refusal before Popen uses an explicit pre-send rejection and settles its
startup reservation, because no child exists; a launched/unknown child never
gets that exemption. Provider CPU refusal also precedes dispatch-intent marking.

This is a nominal shared reservation plus sampled response, **not a CPU quota**.
Independent native pools may run simultaneously even with each pool set to1.
Torch CPU setters do not limit GPU/MPS work, driver/system threads, arbitrary
provider programs, explicit library overrides or unknown descendants. A native
program may close its inherited descriptor, ending that nominal reservation
before all its work ends. The QMD shell/Node/native FD lifetime and effective
native settings are still unverified. The development guard's observed tree
threshold uses the same fraction but is not an observed aggregate meter across
every operating plugin and development process. No hard global25% guarantee is
claimed. Missing observations/cleanup continue to defer; no OS guard is disabled.

Verification:19 new CPU cases plus25 running-watch,32 entrypoint,10 hardening,
16 guard,14 analytical memory and1 analytical adapter-resume case passed
sequentially (117total). The largest batch took0.475s. Popen/Torch/system probes
were mocks; files, pipes and kernel locks were private temporary state. Tests
cover aggregate slot contention, inherited-FD retention, settings/readback,
idempotence/drift/failure, standalone bootstrap, launch environment, provider
pre-send refusal, daemon CPU-only inheritance and unlaunched QMD reservation
cleanup. Fixture checks prove the default UID root is not opened before the
test-only short circuit; all operating processes must leave that flag unset.
Fake CPU counts make these tests independent of the CI host's capacity. One
existing analytical fake lacked the newly required setters; its test double
was completed and the batch rerun. Ten sources parsed. Independent read-only
review, including the final fixture-isolation fix, found no remaining P1/P2 in
this scope (conditional source approval). Real library application/CPU behavior, GPU/native
limits, installation and actual learning remain unexecuted and held.

## Platform observation, controls and memory admission (2026-10-16 follow-up)

The approved [source plan](plans/2026-10-16-platform-resource-policy.md) separates
`resource_platform.py` observations, `resource_controls.py` library controls and
the common memory/coordinator policy. `shared.resource_report` retains its
injectable path, subprocess and clock boundary and one shared query deadline.
This source has not been installed. The development hold remains true and its
separate historical memory-pressure threshold is unchanged.

The common start condition is **available >= additional task budget + system
reserve**, alongside independent pressure/load/freshness/lease vetoes. No
unconditional35% total-RAM admission or40% recovery floor remains. Recovery
still requires five observed stable seconds and its lower CPU load threshold.
The35% value is only a soft post-allocation headroom goal for expanding an
explicitly verified microbatch shape; missing verification defaults to batch1.
Effective optimizer batch2 is unchanged. A low ratio can fit on a large machine;
an adequate ratio cannot substitute for missing task memory on a small one.

Existing `minAvailableMib` and `maxRssMib` values are preserved. Non-adaptive
commands label the RSS cap as `configured_max_rss_proxy`, an unmeasured upper
proxy rather than a model minimum. Adaptive profiles can declare a sourced
`systemReserveMib` and uncertainty. This does not calibrate the reserve or prove
the source statement. Existing profiles without it retain their explicit legacy
`reserveFraction` RAM reserve, labeled `legacy_configured_ratio_not_calibrated`;
the source never silently converts installed policies to a smaller reserve.
The same fraction also retains its separate MPS working-set check meaning.
No new operating reserve/profile has been selected or installed.

Cold starts and new-process resume use the full configured envelope. Another
inference daemon's allocation is already reflected in host availability and
receives no reuse credit. Same-process loaded work receives only measured owned
credit, capped at the profile's fixed amount; an explicitly sourced warm
increment additionally requires positive owned credit. A warm profile or phase
name alone cannot authorize reuse. Finalization of an already completed cursor
remains model-free. A running child needs the configured system reserve, not a
second allocation of its entire peak or a fixed fraction of total RAM. Unknown
or malformed required budgets fail closed. Existing pressure/CPU/owned-group
RSS monitoring, checkpoint boundaries and cleanup barriers remain active.

The historical singleton budget is
`ceil(2633.515625 * 1.10) + ceil(249.09375 * 1.25) = 3209MiB`.
The first maximum includes a failed cold reload; the second term is entire
update/checkpoint RSS growth, not a universal per-pair linear measurement. The
successful training lifetime peak2601.09375MiB already includes model loading.
These were short63/67-token MPS examples on an older adapter, with heuristic
margins. They establish neither an exact minimum nor current-adapter safety.
The new1024MiB reserve and200MiB warm values in unit fixtures are synthetic
examples only; they are not recommended operating values.

| Platform | Observations / memory interpretation | Controls and validation status |
| --- | --- | --- |
| macOS Apple Silicon | vm_stat free+inactive+speculative estimate; pressure level and memory_pressure percentage are separate; unified memory | CPU library soft controls; MPS own-process memory check; prior narrow MacBook evidence, current model path unverified |
| macOS Intel | Same host memory probes; GPU memory topology unknown | MPS profile deferred without supported host unified-memory proof; CPU profile unaffected; mocked metadata only |
| Linux | MemAvailable, total RAM and PSI full avg10; no invented macOS percentage | Current model adapter CPU path; no CUDA path; fake /proc tests, no native live verification |
| WSL | Linux guest observations only | Windows host RAM/GPU/thermal visibility and end-to-end operation unverified |
| Native Windows | Unsupported; observations stay None | Existing POSIX runtime is not ported by this refactor; no native execution claim |

`memory_pressure` percentage is not vm_stat available bytes divided by RAM.
Inactive/reclaimable memory is a heuristic, not guaranteed immediately free
capacity. GPU utilization and thermal state remain `None`/`unknown`, never0.
CPU setters/environment limits are soft controls, not an OS CPU quota. MPS
driver allocation and recommended working set describe own-process memory;
they are neither global GPU utilization nor free VRAM. No allocator-limit API,
GPU compute quota, thermal bridge, privileged probe, service or permission is
added. RSS and MPS counters overlap and are not summed as independent memory.
Unknown telemetry does not support a GPU/thermal safety claim; current source
stays at unverified-batch1 and the actual model trial remains held. Sampled
checkpoint/stop timing cannot preempt one GPU kernel or guarantee OS/GPU latency.

Verification:179 distinct mocked/analytical cases passed sequentially under
the shared read-only development lock: platform25,
CPU19, running-watch25, entrypoint32, hardening10, guard16, adaptive-memory14,
runtime resources22 and coordination16. Four existing actual-child cases were
explicitly excluded. There were no real model, GPU, OS-load, installation,
operating DB or session/automation changes. The earlier117-case CPU result is
historical; it is not a separate additional179 cases or operating acceptance.

Independent read-only review found one P2: observed Intel/unknown GPU topology
could still admit an MPS profile from host RAM alone. A regression first
reproduced refusal/metadata errors; live MPS reports now require supported
`darwin + host + unified` evidence. Unknown/unsupported/guest/malformed topology
defers, while CPU profiles retain their separate admission. Reports without
platform metadata remain compatible only with old injected analytical tests;
the actual shared OS reporter always attaches that metadata. Final narrow
read-only re-review confirmed this P2 resolved and found no remaining P1/P2 in
the reviewed scope: conditional source approval only. Real resource calibration,
models/GPU/OS trials, installation and operating activation remain unexecuted
and held. No operating readiness is claimed.

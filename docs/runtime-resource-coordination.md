# Plugin runtime resource coordination

`context_learning/resource_coordination.py` provides the Coordinator: one
per-OS-user trainer lease, inference-demand slots and a compute lease. Laya
optimizer groups use that compute lease, and `training_cursor.py` commits their
heads/RNG before advancing. `runtime_work.py` owns the durable wake queue. The
development execution guard is separate; see
[development resource safety](development-resource-safety.md).

## Connected runtime paths

- Retired wake kinds: `index`, `update` and `cleanup` rows that an older
  installed bundle queued are acknowledged once by `runtime_handlers.retired`
  without any work.
- `LocalTrainer.call` also admits old explicit adapter calls through the shared
  compute lease. Already coordinated v2 adapters skip that outer lease and use
  their existing per-group TrainingGuard, avoiding recursive acquisition.
  Legacy adapter children inherit their operation lease, so parent exit cannot
  release compute while that child is still running.
- Selected Laya route wakes: `core/laya_host_response.py` queues `laya-sync` and
  `laya-compile` through `runtime_work.request`. Both are kinds in
  `runtime_wake_policy.KINDS`; `needs_compute_admission` is false for them
  because their handlers own admission. `runtime_handlers.laya_sync` and
  `laya_compile` call `laya_runtime_dispatch.run`, which routes the wake into the
  sealed installed bundle with the wake's lease FD; the target process repeats
  the full owner proof before any model or store work.
- Selected Laya route sync: `laya_product_sync_worker.py` holds a Coordinator
  `activity('provider', EMBED_POLICY)` around embedding work and rechecks the
  policy immediately before the embed service loads its model. `EMBED_POLICY`
  (`maxRssMib` 2048, `minAvailableMib` 768, `maxLoadPerCpu` 1,
  `targetHeadroomFraction` 0.35) is a conservative cold-start proxy, not a
  measured peak. One per-user resident service
  (`laya_resident_service.py`, private root
  `<account home>/.local/state/laya/service/`, same-owner peer check on every
  socket) hosts the embedding model for every selected or pending
  installation and the learning inference service. It loads one exact worker
  per vector profile (provider identity, model and dimension) through
  `start_owned_embed_service`, one cold load at a time across the service;
  the cold load passes `Coordinator.check(EMBED_POLICY)` (control lock,
  learning demand, memory/pressure admission: under 10% free memory refuses)
  and never holds the provider lane. The service-wide load lock is held from
  that check until the worker is ready, so two loads never pass one check.
  Every new worker start (a restart included) first proves the installation's
  full attestation; a read-only proof (bundle drift) only attaches to a
  running worker. Client connections are capped service-wide (16) before
  any byte is read; at the cap the listeners stop accepting, peers wait in a
  backlog of 8, and a full backlog refuses the connect (macOS; on Linux the
  connect waits for the client's own timeout), which the provider reads as
  `provider_unavailable` (lexical results). An idle listener holds no slot. A request is read within
  10 seconds of accept and answered within 15 (query) or 320 (document). A
  worker no binding uses and no request is using stops after 60 seconds. A refusal records `resource_deferred` (not an attempt)
  for each waiting installation and the service retries the load itself
  after 5–60 seconds. The service adopts an installation by holding its
  `.resident.lock`/`.lock`, binding its sealed `runtime/laya-embed.sock` and
  writing its owner state; a route change (upgrade fence, restore,
  withdrawal) releases that installation within one second. A dead worker
  releases its installations as `worker_exited`; the service itself re-loads
  and re-adopts them from its registry (bounded by the same three-failure
  budget), without waiting for a hook. The full artifact rehash
  (`verify_worker_attestation`) runs once per installation identity, only when
  the service binds it. It forwards at
  most three queries and three documents to the worker at once, so a burst
  from many projects waits at the service instead of overflowing the
  worker's seven-request queue. A failed query or probe outlives the client's
  timeout (read as unavailable, served lexically), never an empty answer.
  A selected recall that finds no owner socket with owner state `starting`
  or `resource_deferred` asks the service to adopt the installation
  (detached, behind the owner state's 5-second backoff) and pulls a
  backed-off `laya-sync` wake's due time forward without changing its
  attempt count. A sync result deferred by `laya_service_preparing` is
  recorded as
  `target_preparing`: retried after 15 seconds and not counted as an attempt.
  A failed sync/compile child's last 4 KB of stderr is kept per wake in the
  private `runtime-wake-failures.json` beside the wake queue; a deferred
  child result records its fixed `state`/`reason` codes there instead.
- A selected sync yields (`laya_sync_yield_priority`) only to a due `index` or
  `update` wake that has failed fewer than 3 times. Those retired kinds are
  acknowledged on dispatch without work.
- Selected Laya route queries: `laya_embed_resident.admit_query` checks
  `EMBED_POLICY` with `Coordinator.check` within the hook's two-second provider
  budget and acquires no compute lease. Refusal reports
  `laya_query_resource_deferred`, keeps the loaded model, and persists no prompt.
  Laya recall does not call `runtime_work.foreground`, so it advertises no
  foreground demand to background learning work.
- Runtime OS reports include macOS pressure level plus `memory_pressure -Q`,
  total/available memory and system load. Linux keeps PSI semantics rather than
  inventing a macOS free percentage. Denied/unknown observations defer work.
  Foreground inference has a lower absolute admission floor and also shrinks
  its batch at high CPU load.
- Background compute concurrency is at most one at the shared boundary. CPU
  load over 75% of its configured load threshold shrinks training microbatch
  to one. Memory admission requires additional task memory plus an explicit
  system reserve. There is no unconditional35% admission or40% recovery floor;
  35% is a soft post-allocation expansion goal. Unverified
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

Selected-route hook wakes (`laya-sync`, `laya-compile`) and local learning
launch persist wake hints before detaching. A hook wake starts a single bounded
waiter. The worker lease is passed through spawn to prevent one waiter
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
remains durable when the waiter exits. **While the shared selector inference
daemon and its monitor are alive, retained wake hints restart a waiter without a
new hook. When that daemon/monitor is absent, after logout or reboot the next
normal hook is needed.** No boot/login service,
LaunchAgent, extra credential or security permission is used.

Learning retries recheck `laya_learning.ready` and retain the existing history
worker fence. The runtime dispatcher starts at most one owned learning child
per private learning root and returns immediately, so `laya-sync` wakes
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
Legacy non-cursor adapter calls are indivisible boundaries: they
finish the current call, then refuse the next one if pressure has risen. The
plugin never SIGSTOPs a job holding shared locks, and signals no existing user
session or inbox process for scheduling.

This is cooperative scheduling, not an OS hard aggregate CPU/RSS cap. System
load is a moving average; brief peaks or another app allocating memory inside
an indivisible command can outpace the next observation. Foreground retrieval
cannot preempt an already running atomic embedding or model operation; SQLite
contention may still affect latency. Compatibility requires all
participating installed copies to use the same Coordinator. Explicit operator
tools outside these plugin paths are not intercepted.

## Additional entry points

| Entry point | Ownership and admission | Bounded refusal/fallback |
|---|---|---|
| general/manual teacher and quota CLI | One private UID `provider.lock`, separate from compute. Admission precedes source/budget mutation; every new teacher phase rechecks before a reservation and every actual Popen rechecks immediately before launch, even for inherited/nested lanes. The owned CLI inherits the provider FD. A nested builtin adapter verifies an already-held inode/lock and reuses it. | New work never waits for this lane. Initial refusal retains queued content and returns `deferred_resource`. A late pre-send rejection preserves its reservation/receipt for review, described below. Awaiting a remote response does not hold compute; remote launch while owning compute is refused. Existing per-call timeouts, consent, quota and uncertain-send/no-repeat receipts remain authoritative. A sent call is not automatically replayed by the wake waiter. |
| wiki extract/verify/dedup and custom argv provider attempts | Shared provider transport for custom children and direct builtin adapters. Explicit manual budget flows acquire admission before reservation. | Pre-send resource refusal is exit75/`resource_deferred`; compile/verify/dedup keep jobs without failure cooldown or changing engine. Owned children use a bounded process group. An uncertain explicit attempt remains subject to its original receipt and retry policy. |
| model/artifact hashes, installed binding/inventory and cohort preparation | Common compute lease in a reentrant preparation scope; existing worker/namespace locks retained | Each 1MiB hash read and cohort/installation checkpoint releases and rechecks compute. Fresh observations are reused for at most0.5s within the same preparation, still subject to the two-second freshness rule. Background preparation has a thirty-second budget. The verified owner of a foreground inference slot may prepare without rejecting its own demand, using nonblocking compute and the smaller of one second and the remaining hook budget. All other background contexts still yield to demand. Partial hashes are never trusted or published; a later attempt recomputes them. |

The cold-start 2048MiB estimate and preparation/provider 512MiB estimates are
admission estimates, not measured peaks or enforced RSS caps. Cooperative checks
cannot interrupt a kernel file read. A preparation denied partway through may
have added cohort exposure assignments; these are conservative history, not a
completed learning job, and are never deleted to obtain fresh identities.

Preparation ends before trainer dispatch, so nested local trainer admission does
not acquire compute recursively. Resource deferral is kept distinct from an
authorization failure in the affected train/evaluation and worker return paths.
The common coordinator still does not preempt an indivisible already-admitted
model operation or impose an OS hard CPU limit. Raw operator tools outside
the supported plugin entry points remain outside this contract.

Startup, provider, preparation and probe limits:

- `startup-reservation.json` in the common UID root is a durable startup
  barrier. No current plugin path writes it; one left by an older bundle's
  daemon startup makes all common compute, inference-compute, provider and
  new-start admissions return `qmd_startup_recovery_required`; they never wait
  indefinitely. **There is no automatic orphan-reservation recovery, TTL
  clearing or blind PID adoption.** A surviving reservation needs explicit
  ownership/proof review before a later repair. This conservative fallback can
  defer optional selector inference; recall keeps the verified rows' hybrid
  rank.
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
  Oversized cold identity work may exhaust its short budget; recall then keeps
  the verified rows' hybrid rank.
- Probe time is included. `resource_report` shares one deadline across all macOS
  subprocess probes: at most two seconds or the caller's shorter remaining
  budget. Exhaustion starts no later probe, makes the report unready, and is
  checked again before preparation admission. Process creation/cleanup and a
  kernel file read are still cooperative boundaries, not hard real-time caps.
- An early training-preparation deferral retains the existing private diagnostic
  `resource-defer.json` with `completionAck:false`; it does not invent a planned
  batch or consume a training operation.

## Running training and daemon lifetime monitoring

The monitor runs only inside the shared Laya selector inference daemon
(`context_learning/inference_service.py`, hosted by the per-user resident
service).

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
Stop-pipe polling also occurs before each microbatch and before optimizer
mutation; parent EOF is a one-way yield request. An interrupted partial group's
gradients are discarded, then a fresh process resumes its last committed cursor.
It never leaves a SIGSTOPed process holding model memory or locks.

The LocalTrainer parent observes coordinated train/evaluation children while
they run, including their OS pressure checks. Process
group RSS/CPU is sampled at most twice a second with a0.25s probe timeout.
Internal system probes share a0.5s budget and run about once a second; child
per-group admission retains its own short freshness contract. CPU load is an
OS moving average, not instant app-launch detection. Foreground search demand
is an explicit live kernel slot; no app surveillance permission is used.

Routine warning/low background headroom, CPU overload or foreground demand sends
one cooperative stop byte. If the child has not yielded within2seconds of that
observation, cleanup sends TERM to its newly created process group, waits0.5s,
then KILL and another0.5s. Critical OS memory pressure, free percentage<=5%,
remaining memory below256MiB, RSS overflow or unavailable required
monitoring skip that grace and use KILL plus a0.5s reap wait. After either path,
an additional bounded0.5s group-presence check must prove the process group
absent; leader exit alone never establishes success. These are
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
Cursor rows also pin total group count. Before any update, the adapter pins
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

Recovery preparation runs only when its cursor exists; the no-cursor
uncertainty check completes before that path.

## Explicit CPU parallelism

`runtime_cpu.py` centralizes the25% CPU fraction used by the development guard
and the managed child's nominal parallelism reservation. A private UID kernel
pool contains `floor(logical CPUs*0.25)` one-thread shares:18logical CPUs admit
4shares, not4.5 rounded up. Every LocalTrainer child, provider Popen and the
learning inference service hosted by the resident service reserves one; the
resident service's embedding host does not.
Busy/unknown capacity defers without stealing a lease. Spawned children inherit
CPU descriptors; the parent's close does not release a share while an inherited
copy remains open. No daemon inherits the startup parent's compute descriptor.
The hosted learning inference service conservatively retains its CPU share
while idle. Fewer than4 or unknown logical CPUs cannot fit one share and defer
rather than assume capacity. At4–7logical CPUs, the learning inference service
can occupy the only share. Training/provider work can then remain deferred
until a share is actually released. Normal OS pressure alone cannot release an
occupied share; the policy does not automatically stop the service or expand
its fraction.

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
the helper before the request's training guard runs.

Official references:
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
A CPU-share refusal before Popen uses an explicit pre-send rejection, because
no child exists; a launched/unknown child never gets that exemption. Provider CPU refusal also precedes dispatch-intent marking.

This is a nominal shared reservation plus sampled response, **not a CPU quota**.
Independent native pools may run simultaneously even with each pool set to1.
Torch CPU setters do not limit GPU/MPS work, driver/system threads, arbitrary
provider programs, explicit library overrides or unknown descendants. A native
program may close its inherited descriptor, ending that nominal reservation
before all its work ends. The development guard's observed tree
threshold uses the same fraction but is not an observed aggregate meter across
every operating plugin and development process. No hard global25% guarantee is
claimed. Missing observations/cleanup continue to defer; no OS guard is disabled.

## Platform observation, controls and memory admission

`resource_platform.py` owns OS observations, `resource_controls.py` library
controls, and `context_learning/memory_policy.py` the additive memory admission
used by the Coordinator. `shared.resource_report` keeps an injectable path,
subprocess and clock boundary and one shared query deadline. The development
guard's memory-pressure threshold is separate.

The common start condition is **available >= additional task budget + system
reserve**, alongside independent pressure/load/freshness/lease vetoes. There is
no unconditional35% total-RAM admission or40% recovery floor. Recovery
requires five observed stable seconds and its lower CPU load threshold.
The35% value is only a soft post-allocation headroom goal for expanding an
explicitly verified microbatch shape; missing verification defaults to batch1.
Effective optimizer batch2 is unchanged. A low ratio can fit on a large machine;
an adequate ratio cannot substitute for missing task memory on a small one.

Configured `minAvailableMib` and `maxRssMib` values apply as declared. Non-adaptive
commands label the RSS cap as `configured_max_rss_proxy`, an unmeasured upper
proxy rather than a model minimum. Adaptive profiles can declare a sourced
`systemReserveMib` and uncertainty. This does not calibrate the reserve or prove
the source statement. Profiles without it use their `reserveFraction` RAM
reserve, labeled `legacy_configured_ratio_not_calibrated`; a policy is never
silently converted to a smaller reserve. The same fraction also keeps its
separate MPS working-set check meaning.

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

Estimates are configured, not measured: admission reports carry
`estimate_kind: configured_estimate_not_measurement`. Reserve and warm values in
unit fixtures are synthetic examples, not recommended operating values.

`context_learning/memory_policy.py` is the source of truth for the
`adaptiveMemory` profile. Unknown keys fail with `invalid_adaptive_memory`.

| Field | Required | Rule |
| --- | --- | --- |
| `device` | yes | `cpu` or `mps` |
| `fixedMib`, `perPairMib` | yes | integers 1–65536; peak estimate is `fixedMib + perPairMib × microbatch` |
| `estimateSource` | yes | string, 1–256 characters |
| `modelIdentitySha256` | yes | 64 lowercase hex characters |
| `reserveFraction` | no | 0.10–0.50, default 0.10; used only when `systemReserveMib` is absent |
| `systemReserveMib` + `reserveSource` | no, paired | integer 1–65536 with a 1–256 character source; the reserve is the larger of it and `minAvailableMib` |
| `warmAdditionalMib` + `warmEstimateSource` | no, paired | integer 0–65536 with a 1–256 character source; used only for `loaded_self` with positive owned credit |
| `uncertainty` | no | string, 1–512 characters |
| `maxVerifiedMicrobatchSize` | no | 1 or 2, default 1 |

| Platform | Observations / memory interpretation | Controls and validation status |
| --- | --- | --- |
| macOS Apple Silicon | vm_stat free+inactive+speculative estimate; pressure level and memory_pressure percentage are separate; unified memory | CPU library soft controls; MPS own-process memory check; current model path unverified |
| macOS Intel | Same host memory probes; GPU memory topology unknown | MPS profile deferred without supported host unified-memory proof; CPU profile unaffected; mocked metadata only |
| Linux | MemAvailable, total RAM and PSI full avg10; no invented macOS percentage | Current model adapter CPU path; no CUDA path; fake /proc tests, no native live verification |
| WSL | Linux guest observations only | Windows host RAM/GPU/thermal visibility and end-to-end operation unverified |
| Native Windows | Unsupported; observations stay None | The POSIX runtime is not ported; no native execution claim |

`memory_pressure` percentage is not vm_stat available bytes divided by RAM.
Inactive/reclaimable memory is a heuristic, not guaranteed immediately free
capacity. GPU utilization and thermal state remain `None`/`unknown`, never0.
CPU setters/environment limits are soft controls, not an OS CPU quota. MPS
driver allocation and recommended working set describe own-process memory;
they are neither global GPU utilization nor free VRAM. No allocator-limit API,
GPU compute quota, thermal bridge, privileged probe, service or permission is
used. RSS and MPS counters overlap and are not summed as independent memory.
Unknown telemetry does not support a GPU/thermal safety claim; unverified shapes
stay at batch1. Sampled checkpoint/stop timing cannot preempt one GPU kernel or guarantee OS/GPU latency.

Live MPS reports require supported `darwin + host + unified` platform evidence.
Unknown, unsupported, guest or malformed topology defers, while CPU profiles keep
their separate admission. Reports without platform metadata are accepted only
from injected analytical tests; the shared OS reporter always attaches it.

## Per-user resident service lifecycle

- Start: the first `laya-sync` wake (queued by SessionStart) or the first
  learning prediction registers its project under `service/projects/` and
  starts the service detached; hooks never wait for it. It never idle-exits.
  A login item is not installed by the plugin.
- Identity: the service reports protocol, plugin version, code digest and
  interpreter. Two host caches of one release share the service.
- Replacement: a client from a strictly newer version asks the service to
  drain. Listeners close first (clients see an absent socket and serve lexical
  results, never exit 3), in-flight requests and the learning queue finish
  (bounded at 330 seconds), a cold load in progress settles, and every phase
  fits one 480-second budget from drain start, 120 seconds below the
  successor's 600-second lease wait. A cold load still running at the budget
  is hung: the service kills and reaps its own child processes, records
  `service/forced-exit.json` and exits, so the kernel releases the lease; it
  never releases the lease in-process while the load runs. Then owner leases
  are released with owner state
  `starting` (not an attempt), then one successor holding `successor.lock`
  takes the service lease and re-adopts every registered installation. An
  older or equal version never drains a newer one (a rewrite of the same
  version leaves the running service in place until it is stopped); it
  attaches while the
  protocol matches and otherwise reports `selected_provider_protocol_mismatch`.
  A learning client of the same version that needs its trainer interpreter
  replaces the service under that interpreter, which is then remembered.
- An installation still owned by a per-project resident of an earlier bundle
  is adopted after the shared worker is ready: the service signals only the
  one Python process whose real argv (macOS `KERN_PROCARGS2`, Linux
  `/proc/<pid>/cmdline`, never a space-joined `ps` string) is exactly that
  installation's `laya_embed_resident.py serve <project> <socket>` owner,
  re-verified by UID, executable and argv just before SIGTERM.
- Learning: the hosted inference service refuses the socket `shutdown`
  command; an idle learning-only service of an earlier bundle is retired as
  before.
- Embedding runtime: the embedding worker runs the Laya embedding runtime under
  `~/.local/state/laya/embed-runtime` (`active.json`, `previous.json`,
  content-addressed `generations/g-<id>/`). The bundle upgrade worker fills it
  from an install's sealed provider attestation. On a new machine the owner
  runs `core/setup_entry.py provision-index-runtime
  --approve-index-runtime-download [--provider-model-file <local gguf>]`
  (network: npm registry and model host); `index-runtime-status` re-hashes the
  active generation, and `gc-index-runtime [--approve-index-runtime-gc]`
  removes generations that are neither active, previous, nor named by an
  install attestation. `runtimeFilesSha256` is the digest of a generation's
  sealed file rows (every executed file, JS dependencies included). An
  attestation seals it at install or re-attestation; equivalence and
  re-attestation compare it (an attestation without the field uses the
  digest of the generation it names), and a worker start re-hashes every
  file of that generation first. A new generation becomes durable (its
  directories and its entry in `generations/` fsynced) before `active.json`
  names it.

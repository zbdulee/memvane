# Local continual context learning

Capture and training remain opt-in through `contextLearning.capture` and
`autoCycle`. An enabled `autoCycle` project can launch a detached, one-shot
local cycle at SessionStart. Card selection is a required stage of selected
Laya recall and is not a project setting. The synthetic tests leave the operating
index, plugin configuration, and global hooks unchanged. Use an owner-only
absolute `state-dir` dedicated to one project. Do not put that directory in the
repository.

Capture and corpus snapshots apply to the selected Laya route. The selected
Laya response path (`core/laya_host_response.py`) reads at most 15 proved,
published wiki candidates. With a prepared selector (an approved shared
trial/profile or a local `active-checkpoint.json`) the selector's choice is
final, including zero cards. Without one, or when the prepared selector cannot
answer at query time, recall injects the verified candidates in hybrid rank (at
most 3) and logs the reason. Learning capture
(`context_learning.laya_learning.capture_recall`) runs only after the hook
response is flushed, never raises, and records only a fixed status.

## Evidence and labels

`contextLearning.capture` records a bounded candidate pool only when enabled.
Candidate relevance labels (`necessary`, `supporting`, `irrelevant`) and the
reviewed request-level 0–3 final-card selection are separate records. A model
prediction is never written as selection gold. `review-selection` requires the
captured input hash, reviewer ID and reference. `build-manifest` pins reviewed
pair labels, revisions and train/calibration/validation/evaluation family splits.
The local selection queue also requires an intact reviewed selection, every
candidate reviewed, complete captured input text, a complete returned pool for
v2/v3 samples, and current revision hashes. Deletion revokes dependent manifests
and removes local labels and request-linked teacher ledger entries; aggregate
project budget counters remain to prevent deleted calls from reopening spend.
Deletion does not unlearn weights.

The teacher-facing capture stores at most 2,000 prompt characters and 600
characters per candidate. For an eligible wiki card, those 600 characters are
from the **body after frontmatter**, so a long title or metadata cannot consume
the body budget. Separately, the exact complete compact body is pinned in the
private `compact_inputs` table with the source-file and body SHA-256 values.
Its hard limit is 16 KiB of body and 64 KiB of source-file bytes. The body is
never silently truncated: missing, malformed or oversized snapshots exclude
the request from local training. A 600-character lead is an authoring guideline
for immediate hook context, not an arbitrary Laya input cutoff. Local training
uses the complete compact body when available; the explicit `inspect-labels`
review view exposes that same pinned body to a local reviewer. Long original novel manuscripts
are still unsupported; they need a separate full-source contract. The Laya
0.3.20 adapter tokenizes the exact prompt and full compact body, checks both
option markers and the complete state token IDs, and rejects a case if the
model's configured sequence budget cannot hold it. The multilingual
checkpoint declares `max_len=1024` even though its tokenizer advertises 8192;
the adapter uses the **model's 1024-token budget**. Thus 16 KiB is a storage
ceiling, not a guarantee that every body fits Laya. There is no silent
truncation. `laya_raw.py` is a separate raw-data adapter with an assumed 0.3.23
candidate pin, not this verified 0.3.20 integration.

## One local cycle

`run-local-cycle` accepts an immutable manifest version, a private revisions
JSON file, a private trainer argv JSON file, and an owner-only incumbent artifact.
The fixed local executable receives `train` with only reviewed train cases, or
`predict` with unlabeled validation/evaluation cases. It writes one opaque
artifact file and bounded JSON predictions. The parent monitors wall time and
sampled process-group RSS, caps row/file sizes, strips credentials and proxy environment
variables, and requests offline Hugging Face/Transformers mode. The executable
must itself be trusted local code; these environment variables are not an OS
network sandbox. Only one cycle holds the state lock. A repeated cycle ID
returns its recorded verdict, and interrupted local training can resume from
the queued phase. Changed source revisions or labels reject the cycle.

The first 100 reviewed questions are the minimum train count. Validation and
independent evaluation each need at least 50 questions, 10 reviewed `none`
questions and 5 families. Validation compares challenger and incumbent on the
same questions. Promotion requires exact selection improvement without a drop
in necessary-card recall or `none` success, and without an increase in
irrelevant-card injection. Evaluation is reported after the decision and is
not used to select the checkpoint. Reusing those questions across cycles
makes later reports exploratory rather than a fresh independent estimate.
Promotion atomically changes only the
private `active-checkpoint.json` pointer. `rollback-local-checkpoint` restores
its previous artifact pointer; no weights are deleted.

`run-due-cycle` is a schedule gate, not a daemon. The 95%
goal adjusts the **interval between learning cycles**. The interval is 24 hours
to 14 days, can at most double or halve per assessment, and shortens on weak
recent incumbent validation quality, new families, source churn or recent
failure. It needs at least 50 validation questions including 10 `none` cases
and five families before extending. For pacing, it divides the observed Wilson
lower bound by the best attainable bound at the same sample count and caps
that ratio by exact accuracy. A score near 95% extends smoothly; at 95% it
doubles the interval. This normalized signal is not a claim that true accuracy
exceeds 95%, and it never activates a model. Insufficient evidence cannot
extend the interval.

## Current corpus and live selection

Each opt-in capture records a SHA-256 fingerprint of the sealed Laya head
(learning policy, scope, vector profile and every eligible, non-excluded
`[collection, relative, sha256]` row) when that snapshot is available. A
captured candidate whose revision differs from that snapshot is recorded as a
`candidate_index_stale` failure.
Changing the active corpus or index configuration creates a new immutable
snapshot and queues old candidate pools for review; it does not rewrite old
samples. Automatic cycles default to `dataMode: current` and refuse an
evaluation manifest containing older or unavailable corpus fingerprints.
`historical_snapshot` must be chosen explicitly for frozen-snapshot work.
Re-search, whole-body review, and approval of replacement labels remain human
steps. Capture or an attempted current auto cycle performs reconciliation;
there is no continuous corpus watcher.

The selected route passes at most 15 verified candidates. The local Laya
adapter sees each candidate's complete compact wiki body and returns 0–3 card
IDs. The
hook logs hashed candidate and selected IDs, snapshot fingerprint, fallback
reason and observed time/RSS. Missing state, invalid model output, changed
checkpoint or runtime failure keeps the verified candidates in hybrid rank.
`live-selector.json` and an active local checkpoint are separately required.

Installed-package byte identity, a `ready` setup gate and resolution of an
existing state directory do not prove a successful Laya prediction. A policy
that retains an unaccepted adapter path falls back with `unsupported_adapter`;
readiness is not an execution or quality guarantee.

For a different host/version path, the live selector accepts only a retained
adapter under the current user's official `memvane-marketplace`
Claude/Codex cache namespace. The owner-controlled regular files and host
manifest/package version must match that namespace, the current host must
report an official installation from `zbdulee/memvane`, and both
adapter files must have identical SHA-256 content. Only then is the adapter
argument replaced **in memory** with the current package's adapter. The policy
file, interpreter, model directory, checkpoint and authorization stay intact;
the normal trainer/runtime/attestation checks still run.

Missing historical cache files, modified adapters, unsafe paths, ambiguous or
unavailable install metadata, local/directory marketplace relocations and
custom Codex homes fail back to hybrid-rank selection. The exact
current-adapter case is supported. Cross-path Codex inventory lookup is
bounded to two seconds or the caller’s remaining deadline, whichever is shorter.
Expired budgets or less than one second remaining return `timeout` without
starting inventory. Identical Claude bindings across multiple scopes are
accepted; conflicting or malformed records are rejected. Inventory invokes
only the existing plugin inventory command,
without provider API keys or a model session. This compatibility check does
not authorize a new model/runtime or bypass a failed attestation. A separate
bounded synthetic inference check is needed before claiming live execution.

Verified prediction only reads existing compatibility evidence. A missing,
expired, unsafe, unreadable or incompatible proof returns
`inference_attestation_required` and preserves the hybrid-rank fallback. Prediction
never calls the training attestation prover, runs synthetic smoke/training,
renews a proof timestamp or writes a training grant. Capture/autoCycle settings
do not change this prediction-only boundary.

The full compatibility attestation includes one-step training and checkpoint
reload checks and is an explicit setup/training concern, with a 24-hour TTL,
adapter/model SHA and runtime/check predicates. Every prediction rereads a
configured private cache and rechecks evidence before launching; deletion,
replacement or expiry during probing fails closed. Without a cache, the same
trainer can reuse a fresh proof already established by explicit training,
retaining its original verification time. Prediction never issues an
inference-only proof; that comes only from the explicit no-training
preparation described under the shared inference service below.

Shared project settings should omit `contextLearning.stateRoot`. The private
parent then defaults to the current user's plugin directory:
`~/Library/Application Support/memvane/live-selection` on macOS,
or `~/.local/share/memvane/live-selection` elsewhere, using the same
home-location rule as the managed runtime. An account whose managed runtime
is provisioned under the earlier `qmd-auto-context` support directory keeps
the runtime, `live-selection` and `laya-inference` together there
(`runtime_setup.support_base`): the runtime is a virtual environment whose
absolute interpreter paths are sealed into the inference proofs. These displayed `~` prefixes mean
the current user's home; literal `~` strings in `stateRoot` are not expanded.
Each canonical project root still gets its own SHA-256 namespace beneath that
parent. Existing state/checkpoint files at this default are reused in place;
changing the project location changes its namespace, not its search scope.
An explicit private absolute `stateRoot` remains supported. Explicit invalid
values do not fall back to the default.

The parent directory must already have been prepared locally with owner-only
permissions, outside indexed collection roots. Resolving an omitted setting
does not create that parent, move/copy state, prepare a model/checkpoint, enable
capture/training, or call a provider. Missing local preparation retains the
hybrid-rank fallback. Other users need their own local runtime/state setup;
do not commit a personal absolute home path to shared settings. Before removing
an existing explicit root, verify the default resolves to the same prepared
namespace; custom roots require retaining their explicit setting.

The explicit private-to-shared history migration flow still requires an
explicit `stateRoot` binding in project settings. Omitted-root resolution here
does not change that migration contract; a migration request without that
binding is rejected as `project_history_binding_changed`.

## Optional external teacher

No teacher call occurs during capture, local cycles or the schedule gate.
`budgeted-label-request` and `budgeted-second-opinion` require an explicit
private policy with project ID, host, exact request IDs, separate creation and
review models, maximum calls, bytes per call, total bytes and a conservative
USD reservation per call. The project ID binds to the private state root.
Attempt IDs prevent duplicate paid calls; retries need a new ID and consume a
new reservation. Provider billing cannot be hard-capped by this local ledger;
the configured USD reservation is an upper-bound assumption supplied by the
operator. Second opinions are advisory and never become reviewed gold.

## Verification

`test/context-learning-cycle.test.mjs` runs synthetic selection-gold,
cadence/promotion, teacher-budget and 200-question end-to-end fixtures.
The mock trainer fails once, resumes, compares on validation, reports separate
evaluation, promotes, rolls back, and rejects a stale source. No private data,
Laya runtime, external model or operating index state is used by these tests.
`test/context-learning-real-laya.test.mjs` is opt-in and runs an actual local
Laya runtime and local multilingual checkpoint on synthetic cards only. It
exercises runtime attestation, train/predict protocol, a head-only update,
private temporary checkpoint save/reload and a >600-byte complete body.

## Real Laya 0.3.20 adapter

The runtime pins are Python 3.12.14, Laya 0.3.20 and Torch 2.14.0
(`core/context_learning/runtime_setup.py`).
`core/context_learning/laya_adapter.py` implements the trusted local
`train`/`predict` executable. The `LocalTrainer` command prefix is the
absolute Python interpreter path, the absolute adapter path, then the absolute
multilingual checkpoint directory. It reads Laya's local tokenizer, config
and safetensors directly; it does not call `laya.Agent`, whose tokenizer repair
may write inside a shared model snapshot. A SHA-256 bundle identity covers
model weights, Laya config, encoder config and tokenizer files. Training
validates every exact input before any update, freezes the encoder and uses
only reviewed train choices for a head-only SGD epoch. A single safetensors
artifact holds the changed head and pinned base/policy identity. Prediction
loads the base plus that head, rejects held-out gold fields and returns 0–3
candidate IDs using the frozen `p>=0.5`, score-ranked policy. A private base
manifest may reference the unchanged checkpoint without copying its weights;
`prepare-local-laya-base` creates that owner-only artifact for an explicit
state directory. The threshold is not claimed to be calibrated for wiki
selection: held-out validation decides whether a trained head is promoted.

## Runtime ownership and installation gate

`runtime_setup.py` provides read-only discovery, an installation plan, a real
synthetic compatibility proof and an explicit runtime-selection gate. Importing
it does not download Python, install packages or change an active runtime.
The default selection mode is a plugin-owned generation under
`~/Library/Application Support/memvane/runtimes/laya` on macOS. A
managed interpreter must resolve inside that owner-only root and match the
pinned Python 3.12.14 and package versions. An existing interpreter may be
reused only with an explicit path and an attestation bound to the interpreter,
base-model and adapter hashes. That attestation must record successful
synthetic compact-wiki tokenization without truncation, inference, one-step
training, checkpoint reload and fixed selection mapping. `attest_runtime()`
generates that proof by running the real local synthetic smoke; it hashes
the adapter and base bundle before and after. Attestation does not activate a
project, a global hook or a background worker.

`runtime_installer.py` contains the explicit managed staging, activation
and rollback functions. Staging requires a SHA-pinned uv 0.12.20 executable
and a complete hash-locked PyPI requirements file. It installs Python 3.12.14
inside a new owner-only generation with no global executable link, creates a
venv whose interpreter resolves inside that generation, syncs only hashed
wheels using a private cache and checks the exact managed package versions.
Activation runs the real synthetic proof before atomically changing the
owner-only active pointer. The pointer records the runtime, adapter and model
hashes; a later managed selection rechecks them. Rollback rechecks the previous
generation and restores the pointer. Old generations are kept, never updated
in place. Base-model cache reuse is read-only and requires verified hashes;
mutable trained checkpoints belong to private per-project state. The proposed
sources are Astral uv's managed Python distribution and `laya==0.3.20` from
PyPI; `managed_install_plan()` records the proposed exact dependency pins.
Staging installs generations under
`~/Library/Application Support/memvane/runtimes/laya/generations/`.
The lock is `core/context_learning/locks/laya-macos-arm64-py312.lock` (SHA-256
`354c3c985a390d5045019e98871cc8a223237f25d891f36525e49f9a42e46447`), and
wheels are synced with `uv pip sync --require-hashes --no-build` against
`https://pypi.org/simple`. Staging alone writes no Laya `active.json`, project
selector, or plugin/hook activation. Tests exercise stage/activate/rollback
with a fake uv runner. The repository ships one lock, for macOS arm64 with
Python 3.12.

## Shared inference service

`live_select.choose` checks explicitly prepared inference-only evidence when present; an unprepared state retains read-only VerifiedLayaTrainer proof admission. It then sends ordinary and experimental selection to `inference_service.py`. One plugin-owned Unix socket/startup lock/kernel lease under the OS account home serves both host cache copies and project namespaces. A process HOME override does not choose another service root. The per-user Laya resident service (`laya_resident_service.py`) hosts this service in its own process together with the embedding model and owns its lifecycle; it starts with the first selected sync wake or eligible request and never idle-exits.

The controller has one synchronous model owner and at most eight pending inference requests. Each request retains its own canonical private namespace, current policy digest, checkpoint/artifact hash, input view, case hash and deadline. Owner UID/private paths and current scope/evidence are checked again after queueing and after inference. Overflow, expired/disconnected callers and denied sockets fall back explicitly; no requests or source bodies are stored in service files. A crashed worker's released kernel lease allows only the next request to reclaim stale socket/PID files. A hosted service refuses the `shutdown` command; a learning request that needs another interpreter or a newer release asks the resident service to drain and restart. A learning-only worker from another code version is replaced only while it reports idle (no busy request, empty queue): the next request asks it to shut down over the socket and reports `starting` until the current version's worker is up. A worker without the shutdown command receives SIGTERM only when its private PID file matches the current socket inode and that PID runs this account's inference worker command for the same root; anything else stays untouched and the request reports `starting`. A busy old worker is never interrupted.

Projects with the same approved runtime/base/view/head reuse one loaded object. A new head in the same family is prepared separately only with estimated overlap headroom and within the total process RSS policy; first prediction, scope/deadline/response validation and final current-RSS checks precede atomic publication. Failures preserve the incumbent reference; recovered current memory permits incumbent requests again. Admission uses current self RSS (macOS task_info, Linux proc self), not historical ru_maxrss. MPS/global memory pressure still matters; footprint estimates are not hard GPU reservations and a catastrophic process failure remains possible. Different runtime/base/view families are rejected while an incumbent is loaded, rather than silently migrating models or allocating several resident bases.

The per-project resident module (`context_learning/resident.py`) provides compatibility helpers; ordinary selection does not call its launch path.

Training compatibility's 24h/five-check rules apply unchanged. Explicit inference preparation uses a currently valid approval anchor, then separately verifies a read-only synthetic load/forward and binds content, code, private namespace/policy/checkpoint, closed runtime generation and device. Unchanged prepared inference has no daily training requirement. Missing/revoked/changed inference evidence fails closed; any evidence file makes the complete binding mandatory. Changed evidence requires a fresh worker, except same-family head preparation/application under the candidate protocol below. Runtime/base/view changes require reviewed preparation and a fresh worker. Learning grants, source/gold authorization and teacher transmission remain separate. Prediction never invokes prepare/smoke/train or renews proof/grants.

`core/context_learning/inference_identity.py` defines the inference evidence; three private files in the project state directory hold it. `laya-inference-manifest.json` records content hashes of the runtime roots and model directory. `laya-inference-proof.json` (schema `qmd-laya-inference-proof-v1`) records the namespace, executable, model directory, input view, verified identities, code SHA-256 (a hash of every `core/**/*.py`), device, manifest SHA-256, the checks `read_only_load`, `synthetic_forward` and `fixed_selection_mapping`, and the SHA-256 of `live-selector.json`, `laya-synthetic-attestation.json` and `active-checkpoint.json`. `laya-inference-binding.json` holds the proof's SHA-256. Only `prepare()` creates all three, run explicitly through `core/prepare_laya_inference.py` (at most 120 seconds). Candidate application rewrites the proof and binding with the new checkpoint digest and leaves the manifest unchanged. Prediction (`admit()`) only reads them: if any of the three exists, all must match or selection fails closed with an `inference_identity_*` reason. A plugin version change changes the code SHA-256, so existing evidence stops matching until it is prepared again.

The policy's `trainerArgv[1]` anchors the approved adapter. Hosts remove superseded plugin caches, including every cache of the pre-rename plugin id, so the anchor file can disappear. When it no longer exists, selection uses the running bundle's adapter only if that adapter is an installed official copy (or the registered local source) whose bytes equal `adapter_sha256` in `laya-synthetic-attestation.json`; otherwise selection serves the hybrid rank with `adapter_anchor_missing`. The recall hook prints the reason: `[memvane] local selection unavailable (<reason>); serving hybrid rank`.

### Selector heal

A release changes the code SHA-256 that prepared inference evidence binds, the anchor's cache disappears and the synthetic attestation counts for 24 hours, so a prepared selector stops admitting predictions after an update. `core/context_learning/selector_heal.py` heals it in the background, with the same verified steps an owner runs by hand, in dependency order:

0. Before anything executes, the approved scope is checked (`selector_heal.check_scope`). The interpreter must be the activated official learning runtime (`runtimes/laya/active.json`, which the installer writes after a verified generation) and the model directory its model directory when the pointer names one; when the last prepared evidence recorded the interpreter's content hash, the interpreter must still have it (`learning_runtime_not_official`, `learning_runtime_changed`). With an accepted learning profile the acceptance must be intact and name exactly this policy: every `trainerArgv` element, `livePolicySha256`, the model identity and a recorded interpreter hash; otherwise `learning_profile_rebind_required` and nothing runs.
1. `live-selector.json` `trainerArgv[1]` is rebound to the running bundle's adapter, only when that adapter is an installed official copy (or the registered local source the host executes) and the rebound policy passes the scope check. The interpreter and the model directory stay as written. A namespace with an accepted learning profile is never rebound: the profile's acceptance digest covers the policy, so that change needs the owner (`learning_profile_rebind_required`).
2. The synthetic attestation is refreshed through `VerifiedLayaTrainer` (its real synthetic checks, at most 120 seconds) when it is missing, older than 24 hours or names another adapter. An adaptive memory policy defers this step (`trainer_adaptive_attestation_required`) as before.
3. Inference evidence is prepared with `core/prepare_laya_inference.py` in the policy interpreter (at most 120 seconds) when `admit` no longer accepts the existing evidence.

Steps 2 and 3 run only under the per-user prepare lease (`~/.local/state/memvane/learning-prepare.lock`, taken non-blocking; a learning-state move never renames that base) and only when memory admits one model load (`resident._admission`); a held lease defers with `learning_prepare_busy`, pressure with `learning_prepare_memory_pressure`, so two installs never load the model at once. The bundle upgrade worker runs the heal for its project after every run (a release always runs it through the SessionStart bundle step), and a recall whose selection reports `adapter_anchor_missing`, `unsupported_adapter` or `inference_attestation_required` requests that worker (at most once per 10 minutes per install). Recall serves the hybrid rank until the evidence admits again. The event log records `learning.prepare` (`started`, `selector_ready`, `selector_prepared`, `failed` with its reason). Every check that admits a prediction is unchanged; the heal only runs the verification steps again.

### Learning state location

The learning state (`live-selection`, `laya-inference`) lives in `~/Library/Application Support/memvane` (`~/.local/share/memvane` elsewhere). An account that started before the rename moves it once (`core/context_learning/state_move.py`, run by the bundle upgrade worker before the heal): the mover holds the resident service's `successor.lock`, asks a running service to drain (`reason: learning_state_move`), waits up to 120 seconds for its lease and holds it, takes every lane lock in `laya-inference` non-blocking, renames `live-selection` and then `laya-inference` (same volume; lock files keep their inodes) and leaves a read-only regular file (`memvane-learning-state-moved-v1`, naming the new tree) in place of each, so a process of an earlier bundle that resolved the old base fails its `mkdir -p` or lock `open` instead of creating a second lane or selection tree. It then rebases checkpoint pointers that named the old `live-selection`, writes `learning-state.json` and removes its intent; a remaining intent keeps the move pending, so a stop anywhere resumes the same tail. A busy lane or a draining service defers the move to a later run; a namespace bound by the synthetic release-QA setup journal refuses it. A service elected before the move serves the moved root once it holds its lease, and `inference_service.service_root` never leaves a lane directory at the old base. The moved namespace paths and checkpoint digests no longer match prepared evidence, so the heal prepares it again. The learning runtime (`runtimes/laya`) stays where it is.

Process RSS is not total MPS memory. Blocking framework calls are cooperatively checked between batches, not forcibly preempted. Runtime probe/editable-origin subprocess timeouts respect the remaining deadline.

## Durable learning coordination

The authorized v2 staged-training path pins a bounded batch in the existing namespace-routed SQLite history. Its semantic consumption key includes namespace/model family, canonical source path, prompt, candidate revisions/body digests and verified selection. Capture UUIDs, capture times and receipt retries alone are not new evidence. A batch needs `batchMin` fresh admitted train cases, never cohort holdouts. Replay is capped at 32 cases, at most the fresh count and within `batchMax`. Historical `trained_cycle` rows remain consumed. The exact staged batch must satisfy the existing quality minimum and family coverage; having enough labels elsewhere does not qualify a smaller trained artifact.

One stable namespace kernel claim is inherited into the trainer child. A surviving child prevents duplicate ownership even if its parent exits; a released lease resumes the same pinned job without another training reservation. At each completed momentum-free SGD update, two alternating private head slots plus CPU/MPS RNG and next-group cursor are persisted before releasing compute. Resume validates input/group/optimizer/head identity. Committed updates are not replayed; an interrupted uncommitted update may be recomputed. A completed artifact receipt is fsynced before atomic staging and consumption. An uncertain final artifact without its completion receipt is blocked for review instead of deleted and retrained.

The account-private controller shares only hashed namespace/deadline metadata and kernel locks. It registers inference demand before client RPC and holds worker demand through scoring; one trainer uses the same compute gate at safe load/update/prediction boundaries. Pending inference defers the next training boundary. CPU pressure can reduce microbatch 2 to 1; unknown resources, pressure or insufficient OS reserve defer. Loaded-model credit comes only from observed allocation, avoiding counting the same fixed footprint twice. Existing pressure/device/RSS policies still apply. This is cooperative priority, with no hard GPU reservation or forced kernel preemption; inference may exhaust its deadline during a current framework call and fall back.

Resource deferral records a next retry with 30–300 second backoff. The existing project worker may wait up to 60 seconds to start at most two training-only retries; each started call retains its separate policy runtime cap. A single waiter lease coalesces wakes. It rechecks configuration and local-training permission, never invokes the teacher, and leaves pending state durable when it exits. After reboot, the next authorized ordinary hook resumes it; no login service or continuous timer is installed. Three actual trainer failures block a job; resource deferrals do not consume that failure budget. No fresh evidence means no repeated optimization. Legacy private 24h cadence applies.

Synthetic tests cover semantic duplication, holdout fences, inherited-child claim recovery, CPU/memory decisions, inference contention/deadlines, head/RNG resume, immutable completion and teacher-free retry. Those tests do not exercise actual MPS training/resume or performance. Candidate publication requires separate apply authorization.

## Scoped profiles and candidate application

Ordinary setup offers `local-auto-apply`, `local-staged` and `off` without accepting any profile. Exact acceptance binds one prepared project/namespace, existing train/evaluate grant, local runtime/base/view and code identities, shared/live policy bytes, settings preferences and expiry (at most fourteen days). New-profile defaults keep capture false. Preserving migration retains explicit false capture, autoCycle, shared learning and application choices. `off` disables local learning and checkpoint application; selected Laya recall then serves its verified candidates in hybrid rank. No profile adds data sources, external teacher access, billing, package execution or permission to change model/device. An interrupted profile installation resumes the same accepted settings CAS/journal; changed settings require a new review even when index onboarding already succeeded.

The selected worker processes admitted local training and quality evaluation only. It does not run the legacy timed cycle, historical sync or teacher pipeline. Automatic application waits for at least the quality minimum of fresh train cases (currently 100), within the already approved batch cap. An insufficient cap blocks profile planning for budget review; it is never silently raised and quality thresholds are never lowered. Actual trained cases must cover five train families; disjoint validation still needs fifty cases, five families and ten empty-choice cases. No fresh admitted data means waiting.

Project enrollment is explicit per project. `core/project_learning_setup.py` runs `prepare` (read-only plan, `--mode default|synthetic-staged`), `activate` (an exact plan bound by `--accepted-sha256`) and `recover` (configuration-only compare-and-swap restore). Activation writes `project-learning-authorization.json` with scope `single-project-local-learning` (`core/context_learning/project_scope.py`): operations `gold`, `local_train` and `quality_evaluate`, the default cohort `general-v1` bound to the project's collection settings, and a lifetime of at most fourteen days. Teacher consent is separate. Consented project gold uses the signed-in Codex CLI (`general_teacher.py`), and `egress_guard.py` excludes credential-shaped cases before transmission. A profile cannot widen the authorized scope.

Evaluation jobs are durable in the selected history DB and hold an inherited kernel lease. All evaluation reads, changes, claims and pending/staged dispatch retain the namespace boundary when projects share that DB. An unscoped evaluation table is preserved and reports `evaluation_namespace_migration_required` for explicit review; it is never silently adopted by the current project. Jobs bind the staged artifact, approval and train/validation evidence; quality-only recovery needs no new training permission or optimizer update. Stale quality (over one hour), changed validation or a changed incumbent triggers new predictions under a distinct cache epoch. Metrics include exact selection, necessary recall, empty-choice accuracy and irrelevant injections; regressions or missing thresholds block publication. Resource/IPC saturation and startup/timeout deferrals use 30–300 second backoff. Three actual evaluation failures block a candidate, allowing a later fresh batch to proceed. Resource deferrals do not spend the failure budget. The existing bounded waiter handles evaluation and training; it never invokes the teacher.

Application separately revalidates exact apply permission, source/quality evidence, artifact and incumbent. The private journal records `intent`, `prepared`, `pointer_committed`, `activated`, or a precommit `rejected` result. The service loads and validates a synthetic request against a same-family candidate while preserving the incumbent reference. Under the existing pointer writer lock, an expected-old CAS publishes the candidate pointer and the exact derived inference receipt/binding; it preserves the runtime manifest and training approval anchor. Ordinary requests fail closed during an incomplete publication. A second prepare can reconstruct a lost pending object after worker restart; matching token, namespace and generation are required for activation. Lost acknowledgements and ledger completion resume idempotently. Foreign pointer edits and postcommit scope revocation remain closed for reviewed recovery, with no automatic operating DB rollback or deletion of the old artifact.

Resident inference and a separate local trainer may coexist only with current admitted memory/CPU headroom. Their compute gates serialize scoring, loading and optimizer boundaries; queued inference takes priority over new training or candidate loading. A running framework call yields cooperatively at its next boundary, so deadlines can still expire. Preparing a candidate retains at most one pending object, with a bounded expiry, alongside the incumbent and blocks a third generation load. Activation swaps at the serialized service boundary and rechecks finite current RSS and resource limits. There is no hard GPU reservation, allocator guarantee, forced preemption or automatic cross-model replacement.

Installing or updating the plugin grants no training, capture, autoCycle or model download. The code has no OS service registration, login/reboot startup, SessionStart prewarm or continuous retry timer. The first eligible call starts the inference worker; a later authorized hook resumes durable work after reboot.

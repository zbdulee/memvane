# Local continual context learning (development only)

This code is opt-in. An enabled `autoCycle` project can launch a detached,
one-shot local cycle at SessionStart; `liveSelection` can choose wiki cards
at recall. Neither is enabled by installing this development checkout alone.
These synthetic tests leave the operating QMD database, plugin configuration,
and global hooks unchanged. Use an owner-only absolute `state-dir` dedicated
to one project. Do not put that directory in the repository.

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
model's configured sequence budget cannot hold it. The Mini's multilingual
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

Each opt-in capture records a SHA-256 fingerprint of active QMD document
paths/hashes and search configuration when that snapshot is available.
Changing the active corpus or index configuration creates a new immutable
snapshot and queues old candidate pools for review; it does not rewrite old
samples. Automatic cycles default to `dataMode: current` and refuse an
evaluation manifest containing older or unavailable corpus fingerprints.
`historical_snapshot` must be chosen explicitly for frozen-snapshot work.
Re-search, whole-body review, and approval of replacement labels remain human
steps. Capture or an attempted current auto cycle performs reconciliation;
there is no continuous corpus watcher.

`contextLearning.liveSelection: true` changes the primary QMD request cap to
15. After source freshness and eligibility checks, the local Laya adapter sees
each candidate's complete compact wiki body and returns 0–3 card IDs. The
hook logs hashed candidate and selected IDs, snapshot fingerprint, fallback
reason and observed time/RSS. Missing state, invalid model output, changed
checkpoint or runtime failure restores the ordinary bounded QMD choice.
`live-selector.json` and an active local checkpoint are separately required;
this development checkout does not install either in an operating project.

Installed-package byte identity, a `ready` setup gate and resolution of an
existing state directory do not prove a successful Laya prediction. The rc44
installation validation covered those checks without exercising real selector
inference. Policies retaining an rc43 adapter path could therefore still fall
back with `unsupported_adapter`; that earlier readiness result must not be
read as an execution or quality guarantee.

For a different host/version path, the live selector accepts only a retained
adapter under the current user's official `qmd-auto-context-marketplace`
Claude/Codex cache namespace. The owner-controlled regular files and host
manifest/package version must match that namespace, the current host must
report an official installation from `zbdulee/qmd-auto-context`, and both
adapter files must have identical SHA-256 content. Only then is the adapter
argument replaced **in memory** with the current package's adapter. The policy
file, interpreter, model directory, checkpoint and authorization stay intact;
the normal trainer/runtime/attestation checks still run.

Missing historical cache files, modified adapters, unsafe paths, ambiguous or
unavailable install metadata, local/directory marketplace relocations and
custom Codex homes fail back to ordinary QMD selection. The existing exact
current-adapter case remains supported. Cross-path Codex inventory lookup is
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
`inference_attestation_required` and preserves ordinary QMD fallback. Prediction
never calls the training attestation prover, runs synthetic smoke/training,
renews a proof timestamp or writes a training grant. Capture/autoCycle settings
do not change this prediction-only boundary.

The full compatibility attestation still includes one-step training and
checkpoint reload checks and remains an explicit setup/training concern. Its
24-hour TTL, adapter/model SHA and runtime/check predicates are unchanged.
Every prediction rereads a configured private cache and rechecks evidence
before launching; deletion, replacement or expiry during probing fails closed.
Without a cache, the same trainer can reuse a fresh proof already established
by explicit training, retaining its original verification time. This change
does not issue a new inference-only proof or promise Laya scoring after expiry;
that needs a separately reviewed no-training verification protocol. The local
feature branch now implements that separate explicit protocol; see the shared
inference candidate below. The installed legacy behavior is unchanged.

Shared project settings should omit `contextLearning.stateRoot`. The private
parent then defaults to the current user's plugin directory:
`~/Library/Application Support/qmd-auto-context/live-selection` on macOS,
or `~/.local/share/qmd-auto-context/live-selection` elsewhere, using the same
home-location rule as the managed runtime. These displayed `~` prefixes mean
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
ordinary QMD fallback. Other users need their own local runtime/state setup;
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
Laya runtime, external model or operating QMD state is used by these tests.
`test/context-learning-real-laya.test.mjs` is opt-in and runs the actual Mini
Laya runtime and local multilingual checkpoint on synthetic cards only. It
exercises runtime attestation, train/predict protocol, a head-only update,
private temporary checkpoint save/reload and a >600-byte complete body.

## Real Laya 0.3.20 adapter

The Mini has a separate existing runtime at
`~/work/local-llm/data/decision-eval/envs/laya/bin/python` (Python 3.12.14,
Laya 0.3.20, Torch 2.14.0). Its editable Laya source is at
`~/work/local-llm/data/decision-eval/repos/laya` commit `23a1752`, and the
existing multilingual checkpoint is under
`~/work/local-llm/data/decision-eval/hf/hub/models--convaiinnovations--laya`.
This is distinct from an iCloud `laya-runtime/.venv` whose `bin/python`
points to a removed pyenv 3.12.6 interpreter. Do not repair that old link or
copy the old environment. Past Mini reports identify Laya 0.3.20 and MPS but
do not record the exact Python executable for each run.

`core/context_learning/laya_adapter.py` implements the trusted local
`train`/`predict` executable. The `LocalTrainer` command prefix is the
absolute Mini Python path, the absolute adapter path, then the absolute
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
The one-update synthetic MPS smoke saved a 59,880,984-byte head artifact,
reloaded it into a fresh model and compared its logits. No private novel
training or new external model call was part of that check.

## Runtime ownership and installation gate

`runtime_setup.py` provides read-only discovery, an installation plan, a real
synthetic compatibility proof and an explicit runtime-selection gate. Importing
it does not download Python, install packages or change an active runtime.
The default selection mode is a plugin-owned generation under
`~/Library/Application Support/qmd-auto-context/runtimes/laya` on macOS. A
managed interpreter must resolve inside that owner-only root and match the
pinned Python 3.12.14 and package versions. An existing interpreter may be
reused only with an explicit path and an attestation bound to the interpreter,
base-model and adapter hashes. That attestation must record successful
synthetic compact-wiki tokenization without truncation, inference, one-step
training, checkpoint reload and fixed selection mapping. `attest_runtime()`
now generates that proof by running the real local synthetic smoke; it hashes
the adapter and base bundle before and after. The Mini runtime passed and can
be explicitly selected for a local cycle with that proof. This did not
activate a project, a global hook or a background worker.

`runtime_installer.py` now contains the explicit managed staging, activation
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
Managed stage/activate/rollback was tested with a fake uv runner. On the Mac
mini, the official uv 0.12.20 executable was hash-checked and reused, then a
new inactive owner-only generation was actually installed with managed Python
3.12.14 and 35 SHA-256 locked PyPI packages. The lock is
`core/context_learning/locks/laya-macos-arm64-py312.lock` (SHA-256
`354c3c985a390d5045019e98871cc8a223237f25d891f36525e49f9a42e46447`).
The installed generation is under `~/Library/Application Support/qmd-auto-context/runtimes/laya/generations/g-7687971256c743dd88b286718ea8195f`.
The real local synthetic MPS smoke and trainer protocol passed. No Laya
`active.json`, project selector, or plugin/hook activation followed. The
installed wheels were fetched with `uv pip sync --require-hashes --no-build`
against `https://pypi.org/simple`; this is an observed Mini installation, not
a claim about every target platform or a production upgrade.


## Shared inference service candidate (local branch, not installed)

`live_select.choose` checks explicitly prepared inference-only evidence when present; an unprepared state retains read-only VerifiedLayaTrainer proof admission. It then sends ordinary and experimental selection to `inference_service.py`. One plugin-owned Unix socket/startup lock/kernel lease under the OS account home serves both host cache copies and project namespaces. A process HOME override does not choose another service root. The worker persists after its first eligible request; this slice does not add SessionStart prewarm or an OS login service. QMD retrieval/daemon remains separate.

The controller has one synchronous model owner and at most eight pending inference requests. Each request retains its own canonical private namespace, current policy digest, checkpoint/artifact hash, input view, case hash and deadline. Owner UID/private paths and current scope/evidence are checked again after queueing and after inference. Overflow, expired/disconnected callers, mismatched code versions and denied sockets fall back explicitly; no requests or source bodies are stored in service files. A crashed worker's released kernel lease allows only the next request to reclaim stale socket/PID files. No existing PID/session is signaled.

Projects with the same approved runtime/base/view/head reuse one loaded object. A new head in the same family is prepared separately only with estimated overlap headroom and within the total process RSS policy; first prediction, scope/deadline/response validation and final current-RSS checks precede atomic publication. Failures preserve the incumbent reference; recovered current memory permits incumbent requests again. Admission uses current self RSS (macOS task_info, Linux proc self), not historical ru_maxrss. MPS/global memory pressure still matters; footprint estimates are not hard GPU reservations and a catastrophic process failure remains possible. Different runtime/base/view families are rejected while an incumbent is loaded, rather than silently migrating models or allocating several resident bases.

The old project resident module remains as compatibility helpers/tests; ordinary product selection no longer calls its launch path. A future operating rollout must inspect any actual running older worker/version conflict without inventing session/inbox shutdown prerequisites. This local implementation does not retire an operating worker or install a new package.

The no-hidden-training fix and training compatibility's existing 24h/five-check rules remain intact. Explicit inference preparation uses a currently valid approval anchor, then separately verifies a read-only synthetic load/forward and binds content, code, private namespace/policy/checkpoint, closed runtime generation and device. Unchanged prepared inference has no daily training requirement. Missing/revoked/changed inference evidence fails closed; any evidence file makes the complete binding mandatory. The original inference slice requires a fresh worker for changed evidence; the separately authorized candidate protocol below adds same-family head preparation/application. Runtime/base/view changes still require reviewed preparation and a fresh worker. Learning grants, source/gold authorization and teacher transmission remain separate. Prediction never invokes prepare/smoke/train or renews proof/grants. See [protocol and evidence](plans/2026-10-15-inference-identity.md).

Isolated real selectors/IPC/model measurements for the inference identity commit reduced sequential warm requests from 3.56–3.70s to 0.87–0.92s with zero metadata subprocesses. Cold still verifies full content and loads the model (about 8.4s). Those measurements precede the learning coordination changes below. Actual installed-host hook IPC and full hardware/allocator behavior remain unverified; process RSS is not total MPS memory. Blocking framework calls are cooperatively checked between batches, not forcibly preempted. Runtime probe/editable-origin subprocess timeouts respect the remaining deadline.

## Durable learning coordination candidate (feature branch, not installed)

The authorized v2 staged-training path now pins a bounded batch in the existing namespace-routed SQLite history. Its semantic consumption key includes namespace/model family, canonical source path, prompt, candidate revisions/body digests and verified selection. Capture UUIDs, capture times and receipt retries alone are not new evidence. A batch needs `batchMin` fresh admitted train cases, never cohort holdouts. Replay is capped at 32 cases, at most the fresh count and within `batchMax`. Historical `trained_cycle` rows remain consumed. The exact staged batch must satisfy the existing quality minimum and family coverage; having enough labels elsewhere does not qualify a smaller trained artifact.

One stable namespace kernel claim is inherited into the trainer child. A surviving child prevents duplicate ownership even if its parent exits; a released lease resumes the same pinned job without another training reservation. At each completed momentum-free SGD update, two alternating private head slots plus CPU/MPS RNG and next-group cursor are persisted before releasing compute. Resume validates input/group/optimizer/head identity. Committed updates are not replayed; an interrupted uncommitted update may be recomputed. A completed artifact receipt is fsynced before atomic staging and consumption. An uncertain final artifact without its completion receipt is blocked for review instead of deleted and retrained.

The account-private controller shares only hashed namespace/deadline metadata and kernel locks. It registers inference demand before client RPC and holds worker demand through scoring; one trainer uses the same compute gate at safe load/update/prediction boundaries. Pending inference defers the next training boundary. CPU pressure can reduce microbatch 2 to 1; unknown resources, pressure or insufficient OS reserve defer. Loaded-model credit comes only from observed allocation, avoiding counting the same fixed footprint twice. Existing pressure/device/RSS policies still apply. This is cooperative priority, with no hard GPU reservation or forced kernel preemption; inference may exhaust its deadline during a current framework call and fall back.

Resource deferral records a next retry with 30–300 second backoff. The existing project worker may wait up to 60 seconds to start at most two training-only retries; each started call retains its separate policy runtime cap. A single waiter lease coalesces wakes. It rechecks configuration and local-training permission, never invokes the teacher, and leaves pending state durable when it exits. After reboot, the next authorized ordinary hook resumes it; no login service or continuous timer is installed. Three actual trainer failures block a job; resource deferrals do not consume that failure budget. No fresh evidence means no repeated optimization. Legacy private 24h cadence is unchanged.

Synthetic tests cover semantic duplication, holdout fences, inherited-child claim recovery, CPU/memory decisions, inference contention/deadlines, head/RNG resume, immutable completion and teacher-free retry. Actual MPS training/resume and post-change performance are not established by those tests. Candidate publication remains forbidden without separate apply authorization; no installed package, existing project flag, grant, proof or active pointer is changed. See [implementation plan](plans/2026-10-15-learning-coordination.md) and [separate defaults/apply design](plans/2026-10-15-learning-defaults-apply-design.md).

## Scoped profiles and candidate application (feature branch, not installed)

Ordinary setup offers `local-auto-apply`, `local-staged` and `off` without accepting any profile. Exact acceptance binds one prepared project/namespace, existing train/evaluate grant, local runtime/base/view and code identities, shared/live policy bytes, settings preferences and expiry (at most fourteen days). New-profile defaults keep capture false. Preserving migration retains explicit false capture, autoCycle, live selection, shared learning and application choices. `off` explicitly disables local learning and selection. No profile adds data sources, external teacher access, billing, package execution or permission to change model/device. An interrupted profile installation resumes the same accepted settings CAS/journal; changed settings require a new review even when QMD installation already succeeded.

The selected worker processes admitted local training and quality evaluation only. It does not run the legacy timed cycle, historical sync or teacher pipeline. Automatic application waits for at least the quality minimum of fresh train cases (currently 100), within the already approved batch cap. An insufficient cap blocks profile planning for budget review; it is never silently raised and quality thresholds are never lowered. Actual trained cases must cover five train families; disjoint validation still needs fifty cases, five families and ten empty-choice cases. No fresh admitted data means waiting. Generic project enrollment remains incomplete: the existing reviewed execution/data grant currently accepts the specific synthetic story cohort, and a profile cannot extend it to arbitrary operating corpora.

Evaluation jobs are durable in the selected history DB and hold an inherited kernel lease. All evaluation reads, changes, claims and pending/staged dispatch retain the namespace boundary when projects share that DB. An older unscoped feature table is preserved and reports `evaluation_namespace_migration_required` for explicit review; it is never silently adopted by the current project. Jobs bind the staged artifact, approval and train/validation evidence; quality-only recovery needs no new training permission or optimizer update. Stale quality (over one hour), changed validation or a changed incumbent triggers new predictions under a distinct cache epoch. Metrics include exact selection, necessary recall, empty-choice accuracy and irrelevant injections; regressions or missing thresholds block publication. Resource/IPC saturation and startup/timeout deferrals use 30–300 second backoff. Three actual evaluation failures block a candidate, allowing a later fresh batch to proceed. Resource deferrals do not spend the failure budget. The existing bounded waiter handles evaluation and training; it never invokes the teacher.

Application separately revalidates exact apply permission, source/quality evidence, artifact and incumbent. The private journal records `intent`, `prepared`, `pointer_committed`, `activated`, or a precommit `rejected` result. The service loads and validates a synthetic request against a same-family candidate while preserving the incumbent reference. Under the existing pointer writer lock, an expected-old CAS publishes the candidate pointer and the exact derived inference receipt/binding; it preserves the runtime manifest and training approval anchor. Ordinary requests fail closed during an incomplete publication. A second prepare can reconstruct a lost pending object after worker restart; matching token, namespace and generation are required for activation. Lost acknowledgements and ledger completion resume idempotently. Foreign pointer edits and postcommit scope revocation remain closed for reviewed recovery, with no automatic operating DB rollback or deletion of the old artifact.

Resident inference and a separate local trainer may coexist only with current admitted memory/CPU headroom. Their compute gates serialize scoring, loading and optimizer boundaries; queued inference takes priority over new training or candidate loading. A running framework call yields cooperatively at its next boundary, so deadlines can still expire. Preparing a candidate retains at most one pending object, with a bounded expiry, alongside the incumbent and blocks a third generation load. Activation swaps at the serialized service boundary and rechecks finite current RSS and resource limits. There is no hard GPU reservation, allocator guarantee, forced preemption or automatic cross-model replacement.

Remaining before operation: install a reviewed package and rebind its changed code/adapter through the normal authorized evidence flow; prepare compatible runtime/model/base and fresh inference/training proofs; explicitly approve each project/data scope, resource/batch/disk budget and profile; validate real hardware training/resume, generation replacement and actual Claude/Codex hooks. A plugin version bump changes the reviewed adapter path and manifest identity; an earlier runtime grant does not automatically carry over and needs an explicit authorization update. The release itself grants no training, capture, autoCycle or model download. The code has no OS service registration, login/reboot startup, SessionStart prewarm or continuous retry timer. The first eligible call starts the inference worker; a later authorized hook resumes durable work after reboot. Mixed installed versions/older workers require actual conflict inspection. No new real model, training, teacher, API, operating install or setting change was used for this slice's validation. Earlier inference timing does not measure these changes. The [handoff record](handoff-claude-20261013.md) preserves the current sessions/inbox-maintenance agreement; the reintroduced pause/old-PID prerequisite and its user-request attribution are withdrawn. No automation or session was stopped by this implementation.

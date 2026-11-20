# Local continual context learning

Capture and training remain opt-in through `contextLearning.capture` and
`autoCycle`. An enabled `autoCycle` project can launch a detached, one-shot
local cycle at SessionStart. Card selection is a required stage of selected
Laya recall and is not a project setting. The synthetic tests leave the operating
index, plugin configuration, and global hooks unchanged. Use an owner-only
absolute `state-dir` dedicated to one project. Do not put that directory in the
repository.

Capture and corpus snapshots apply to the selected Laya route. The selected
Laya response path (`memvane/host/host_response.py`) reads at most 15 proved,
published wiki candidates. With a prepared selector (an approved shared
trial/profile or a local `active-checkpoint.json`) the selector's choice is
final, including zero cards. Without one, or when the prepared selector cannot
answer at query time, recall injects the verified candidates in hybrid rank (at
most 3) and logs the reason. Learning capture
(`context_learning.laya_learning.capture_recall`) runs only after the hook
response is flushed, never raises, and records only a fixed status.

Cases come only from edit recall (the PostToolUse `posttool` tail, see
`docs/architecture.md`); a prompt recall stores none (`prompt_capture_retired`).
An edit case is sample schema v5 (`context_origin: tool_edit`): the text the
edit added (at most 8,192 characters) as the query, the verified pool of at
most 15 cards with their frontmatter-free bodies, the selector's choice or the
ranked card as `baseline_selected_ids`, and an `edit_event` of hashes and ids
(`path_sha256`, `after_sha256`, `operation`, `session_id`, `tool_use_id`,
`truncated`, `injected_ids`). The edited path itself is not stored. The case's
`request_id` is the `recall.edit` event's. Only the main checkout of an
enrolled project captures, with a pool of at least two cards; a git-ignored
edited file, a teacher input over 65,536 bytes or credential-shaped text stores
nothing. In a default-teacher namespace v5 cases are the default teacher's
input (see Default teacher); explicit profiles' cohort admission skips them.
The first worker pass of a default
namespace deletes, once, the prompt cases of schema 4 or lower (shared `cases`
rows and the private `samples` of the same request ids), writes
`prompt-cases-retired.json` and one `learning.migration` event with the count.

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
option markers and the complete state token IDs, and rejects a training case
if the model's configured sequence budget cannot hold it. The multilingual
checkpoint declares `max_len=1024` even though its tokenizer advertises 8192;
the adapter uses the **model's 1024-token budget**. Thus 16 KiB is a storage
ceiling, not a guarantee that every body fits Laya.

The edit view (`tool-edit-8192-choice-v1`, every live namespace) pairs a
request and a card under the versioned pairing `chunked-max-v1`
(`memvane/learning/context_learning/pairing.py`, limits in `adapter.py`).
Training and prediction call the same plan. A query over 286 tokens becomes
at most four query chunks: the edit lane keeps the first four, the prompt lane
the first three and the tail. A card body over 700 tokens becomes at most three
card chunks; every chunk after the first starts with the card's heading line.
Chunk ends prefer a sentence or paragraph end in the last quarter of the
window and always fall on a token offset, so no UTF-8 sequence is split. The
tokens left outside the chunk limits are counted (`dropped_query`,
`dropped_card`). The pairs run in a fixed order: every card with the first
query and card chunks, then the remaining query chunks (the prompt lane takes
its tail chunk first), each over every card in rank order, then the remaining
card chunks. A request's `pair_budget` (at most 96) drops the tail
of that order and counts it (`dropped_pairs`); a card left without a pair is
not selected. A card scores the highest probability of its pairs and the
fixed policy (p ≥ 0.5, top three) maps the scores to cards. In training, a
gold card keeps one positive pair, the one the base head every run starts
from scores highest, and drops its other pairs; every pair of another card is
negative. Content checks (mask token, complete input, pool size and unique
ids) run on the whole input before chunking. Prediction sorts pairs by length
before batching; the resident service's memory admission sets the batch size
and padded total within the adapter's limits (16 pairs, 16,384 padded tokens).

The legacy and experiment views keep their leading-prefix pairing
(`leading-body-prefix-v1` semantics): training input is never truncated, and
prediction keeps the whole user request plus a leading prefix of the card body.
The body is tokenized once with offsets and cut at a body-token end sized from
the token room left beside the request; the whole state is then re-tokenized
and, while still too long, the cut steps back by the measured excess (at most
16 checks). The prefix keeps the card's whole first heading line, found after
any leading blank lines: a request that leaves no room for it fails closed
(`input_budget_exceeded`). The edit view uses the same fit only as a safety
net for a single chunk pair. Missing or malformed tokenizer offsets fail
closed. For selection, a body over 16 KiB is first cut to its leading 16 KiB on
a codepoint boundary.

The inference receipt records the pairing in its `checks`
(`prediction_chunked_max_v1`); a receipt without it fails admission and the
selector heal prepares it again. Preparation proves the pairing on the
model's own view: an edit-view synthetic case whose query and card each split
into two or more chunks must score every planned pair, and a legacy-view case
whose request fills all but a small room must cut its card. The adapter smoke
also checks the edit-view plan on the tokenizer: over-budget input becomes
chunks and the length beyond the chunk limits is counted.

### Selector budget

Recall sends the selector the ten best ranked cards of the verified pool; the
capture keeps the whole pool (at most 15). A prompt recall gives the selector
`min(queryTimeout, 3 s)`, kept one second inside the hook deadline, and 24 pairs; an edit recall gives it its own tail
deadline (hook entry + 10 s, less one second) and 91 pairs. Past the budget a
prompt recall serves the hybrid rank and an edit recall its first ranked card.
A prompt over 64 KiB keeps its first 48 KiB and its last 16 KiB. The values
come from the 1.0.19 synthetic measurement on an Apple M5 Pro: about 62 ms per
1,024-token pair whatever the batch size, and about 0.8 s of identity checks
outside the forward before the per-process caches below. `pairs = (budget -
0.8 s) / 0.063 s x 0.7`; the candidate count is the largest of 6, 8 and 10
whose single-chunk forward fits 1.5 s with that fixed cost. Recall events
record each part (`selectorCandidates`, `selectorLane`, `selectorBudgetMs`,
`selectorQueryChunks`, `selectorDroppedPairs`, `selectorPairLenP95`,
`selectorValidationSeconds`, `selectorRevalidationSeconds` and the rest of
`recall/verified_rows._SELECTOR_MEASURES`).

The inference service keeps per-process results it would otherwise repeat for
every request, without changing what is proven. Code files are hashed again
only when their stat stamp (device, inode, size, mtime and ctime) moves, and
the client's wire signature reuses those file hashes. Loaded modules already
placed inside the runtime roots are not walked again. A request whose model
was already loaded revalidates after its forward without a second runtime stat
sweep and module walk: receipt, policy, attestation, pointer, artifact,
adapter and code checks still run, every request's initial validation sweeps
the runtime and model directory, and a request that loads a model sweeps again
after the load and after its forward. A runtime change during a warm forward is
therefore found by the next request's initial validation. `raw.py` is a separate raw-data adapter with an assumed 0.3.23
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
home-location rule as the managed runtime. A managed runtime activated under the earlier `qmd-auto-context` support directory
stays there while the MemVane directory has no activated runtime
(`runtime_setup.runtime_base`): the runtime is a virtual environment whose
absolute interpreter paths are sealed into the inference proofs, so it is
re-provisioned, never moved. `live-selection` and `laya-inference` follow
`runtime_setup.learning_state_base` separately (see Learning state location
below). These displayed `~` prefixes mean
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

## Default teacher

A default namespace has one of four profile kinds
(`default_enrollment.profile_kind`): `default-local`, `default-teacher`,
`synthetic` or `explicit`. Onboarding enrolls `default-teacher`; with the
teacher switched off it enrolls `default-local`. The worker and renewal pick
their branch from this kind.

### Authority and consent

Three records carry the teacher authority; the enrollment `choice` stays
`local-auto-apply`.

- The setup journal plan mode is `default-teacher`.
- The grant (`project-learning-authorization.json`) has operations
  `gold_default`, `local_train` and `quality_evaluate`
  (`project_scope.DEFAULT_TEACHER_OPERATIONS`). `gold_default` admits only
  the default teacher; the explicit `gold` operation stays separate.
- The consent file `project-teacher-consent.json` (namespace root, 0600) is
  `{acceptedSha256, proposal}` with the fixed template of
  `teacher_consent.proposal`: schema `memvane-teacher-consent-v2`, purpose
  `selection-gold`, recipients `claude`/`claude-cli` and `codex`/`codex-cli`,
  `modelPolicy: lowest-tier`, one pass, fields `edit_text`, `candidate_body`,
  `candidate_id` and `revision_sha256`, input and output at most 65,536
  bytes, 60 seconds and 4,096 MiB per call, `grantedBy`
  (`default-enrollment` or `default-enrollment-upgrade`), `grantedAtUnixMs`
  and `enabled: true`. It has no call limit and no model id.

The shared consent `consents/<project_key>.json` has `externalTeacher: true`.
The learning code accepts exactly this shape or the default-local one.
Bundles older than 1.0.21 refuse both the shared consent and the grant, so an
older host copy neither captures into nor trains this namespace, and its
recall answers in hybrid rank until that host updates. The policy file keeps
`goldModel` and `maxGoldCalls`; the default teacher reads neither, and status
and `memvane:logs` report the model each call resolved instead.

### Enabling an existing default-local install

`memvane/learning/default_teacher_enable.py` is the only code that widens a
default namespace. Selector maintenance runs it after renewal, heal and warm,
inside the same prepare lease, and requests maintenance while a default-local
namespace still owes the change or an enable intent remains. It needs an
intact default-local setup, no renewal intent, no teacher switch, the running
bundle equal to the grant binding's and a free `project-worker.lock`;
otherwise it records `enable_deferred` with the reason and writes nothing.

The change takes renewal's locks, writes the intent
`project-teacher-enable.json` with the old and new bytes of every target, and
replaces each target by compare-and-swap. While the intent exists the grant,
profile and selection checks refuse (`project_scope.require`,
`learning_profile.require`, `selection_enabled`): selected recall serves its
verified candidates in hybrid rank and capture continues. A later run finishes
the change forward when its preconditions still hold and rolls it back
otherwise. Afterwards the grant without `operations`, the profile without
`runtimeApprovalSha256`, the journal's `profileProposal` and
`written[profile.*]`, and both policy files must equal their old bytes, and
the consent file must equal the template; any difference rolls the change
back.

A target that is neither its old nor its new bytes is
`teacher_enable_mixed_state`. The fence stays, nothing more is written, and
SessionStart names the recovery request. The setup skill's
`restore-teacher-enable` (with `--plan` read-only) writes every target back to
its old bytes and removes the intent. Five failures in a row with the same
reason stop the requests until the plugin version changes or the restore
command runs; the count lives in `teacher-host-state.json`. Each outcome is a
`learning.teacher` row: `enabled` (`grantedBy: default-enrollment-upgrade`),
`enable_deferred`, `enable_failed` or `enable_restored`.

### Renewal

Renewal keeps every authority field of both kinds byte for byte: the grant
changes only its binding and the profile only its five derived fields, so
renewal cannot turn a local grant into a teacher grant. It never writes the
consent file or the shared consent. It checks the profile kind, which compares
both files with the plan, when it starts and again before it removes its
intent. A renewal does not start while an enable intent exists, and the enable
change does not start while a renewal intent exists.

### Labeling

Labeling runs only in the learning worker
(`general_teacher.process_pending_default`); hooks never import the teacher,
transport, isolation or tier modules (structure test X-1). One pass makes at
most eight calls and examines at most 64 cases. Cases that fail the local
checks do not count.

- **Host.** A case goes to the CLI of the host that captured it
  (`sample.host`), never to the other host. The executable comes from the
  worker's `PATH` and passes the host inventory checks.
- **Model.** `teacher_model_tiers.py` is the bundled tier table, reviewed each
  release: Claude requests `haiku` and accepts only `claude-haiku-5-5`
  (optionally with a date suffix); Codex uses the first of `gpt-6-luna` and
  `gpt-5.6-luna` listed in the user's `models_cache.json`, with `low`
  effort. A model outside the table is never requested.
- **Isolation.** Before each call `teacher_isolation.py` checks the argv
  (each host adapter's isolation options), the child environment allow list
  and an empty temporary working folder with no `.git`, `CLAUDE.md` or
  `AGENTS.md` above it; any miss sends nothing. The CLIs run with the user's
  own `~/.codex` and Claude settings. Claude gets the body only after its
  initialize answer reports the allowed model and no commands; a tool, MCP
  server, plugin or skill in its init catalog, or a tool event, ends the call
  as an isolation violation (`violated`). A built-in tool the host cannot
  disable that runs during a Codex
  call is a warning (`toolWarnings`); the label is kept. Codex adds the
  user's global `AGENTS.md` to the model input; its size is recorded as
  `userInstructionsBytes`.
- **Input.** The payload holds the instruction, the case's edit text and the
  candidates as `{id, revision_sha256, body}` from the capture snapshot. No
  source file, card file or path is read or attached.
- **One pass.** A valid answer is gold at once (`review: single_pass`); an
  empty selection is a valid label. A local check that fails sets the case
  aside without a call (`pending_review` with `teacher_sensitive_input`,
  `teacher_input_budget` or `duplicate_input`). A failure before sending
  requeues the case and counts a retry (five make it `pending_review`,
  `gold_attempts_exhausted`); a provider refusal for quota, sign-in or model
  requeues it without a count and rests the host for an hour; a refusal of
  the content, an uncertain failure or an invalid answer is `pending_review`
  and is not called again. Three uncertain or invalid results in a row rest
  that host for a day.
- **Waits.** `teacher-host-state.json` (namespace root, 0600) keeps each
  host's wait, reason, failure count, queued cases and notice times. Codex
  also rests a pass when its weekly remaining quota is 30 % or less or cannot
  be read (`deferred_quota`). A resting host's cases stay `queued`; the other
  host's cases and the local training cycle continue.

### Evaluation and promotion

Gold cases train without a holdout split (`prequential.py`). A case carries
the selection Laya made when it was captured, which the then active
checkpoint had not trained on, so its gold scores that checkpoint: exact
match, micro F1 and empty-selection agreement over the cases the selector
answered (cases served in default rank are counted apart). A candidate trained
on at least 100 cases stays fixed until at least 50 gold cases captured after
its training cutoff, at least ten of them with an empty gold and ten with a
non-empty gold, compare it with the
active checkpoint under the existing promotion rule. An applied or rejected
candidate is closed and not evaluated again; the next job then trains on all
gold, up to the policy's `batchMax`. `checkpoint-history.json` keeps the
application times that map cases to checkpoints. Whether the selector answered
a case comes from the `recall.edit` event with the same `request_id`; the
worker stores it in the learning database on its first pass after capture.
In a project without its own `.git` inside a parent work tree, every edit
case captured before the teacher consent's `grantedAtUnixMs` stays out of
labeling and training as `historical` (`pre_teacher_capture`); none is
deleted.

### Turning the teacher off

`MEMVANE_TEACHER=off` in the worker's environment, or the setup skill's
`teacher-off` (namespace file `teacher-switch.json`, 0600), keeps the teacher
from calling and stops the default-local change; `teacher-on` removes the
switch's effect. An already enabled profile keeps its files. Capture, recall
and local training continue. Project `settings.json` has no teacher key.

### SessionStart notices and logs

Claude and Codex SessionStart reads only `teacher-host-state.json` and the
enable intent's presence; it starts no process and opens no database. It adds
at most these lines:

- The session host's CLI is missing and cases wait for it: once per host and
  reason every seven days.
- The session host has rested for more than a day on
  `teacher_isolation_incomplete` or `teacher_model_unresolved` with cases
  waiting: once per host and reason every seven days.
- An enable intent older than ten minutes, or enable requests stopped by
  repeated failures: every session, until resolved.

A seven-day line appears only after its notice time is written. Quota,
provider waits and the failure breaker are not announced. Hermes has no
SessionStart output and is not a teacher host.

Each call writes one `learning.teacher` row (`gold`, `pending_review`,
`pre_send_rejected` or `provider_refused`) with the case's `request_id`
(the `recall.edit` row's), host, model and tier, usage (`usageIn`,
`usageOut`, `usageCached`, `usageReasoning`, `usageOverhead`), the selected
cards as `[{path, sha}]`, the isolation result and the prequential values.
Each pass writes an `outcome: pass` row with the host waits and queued counts,
a `learning.score` row per scored checkpoint, and a `learning.promotion` row
per promotion decision. Rows never hold the edit text, card bodies, source
paths or global instructions. The `memvane:logs` summary aggregates them.

## Explicit external teacher requests

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

The `context_learning_cycle`, `context_learning_cycle_e2e` and `teacher_budget` rows of
`memvane/learning/tests/integration/test_scripts.py` run synthetic selection-gold,
cadence/promotion, teacher-budget and 200-question end-to-end fixtures.
The mock trainer fails once, resumes, compares on validation, reports separate
evaluation, promotes, rolls back, and rejects a stale source. No private data,
Laya runtime, external model or operating index state is used by these tests.
`memvane/learning/tests/model/test_real_laya.py` is opt-in and runs an actual local
Laya runtime and local multilingual checkpoint on synthetic cards only. It
exercises runtime attestation, train/predict protocol, a head-only update,
private temporary checkpoint save/reload and a >600-byte complete body.

## Real Laya 0.3.20 adapter

The runtime pins are Python 3.12.14, Laya 0.3.20 and Torch 2.14.0
(`memvane/learning/context_learning/runtime_setup.py`).
`memvane/learning/context_learning/adapter.py` implements the trusted local
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
The lock is `memvane/learning/context_learning/locks/laya-macos-arm64-py312.lock` (SHA-256
`354c3c985a390d5045019e98871cc8a223237f25d891f36525e49f9a42e46447`), and
wheels are synced with `uv pip sync --require-hashes --no-build` against
`https://pypi.org/simple`. Staging alone writes no Laya `active.json`, project
selector, or plugin/hook activation. Tests exercise stage/activate/rollback
with a fake uv runner. The repository ships one lock, for macOS arm64 with
Python 3.12.

## Shared inference service

`live_select.choose` checks explicitly prepared inference-only evidence when present; an unprepared state retains read-only VerifiedLayaTrainer proof admission. It then sends ordinary and experimental selection to `inference_service.py`. One plugin-owned Unix socket/startup lock/kernel lease under the OS account home serves both host cache copies and project namespaces. A process HOME override does not choose another service root. The per-user Laya resident service (`resident_service.py`) hosts this service in its own process together with the embedding model and owns its lifecycle; it starts with the first selected sync wake or eligible request and never idle-exits. A selection request first asks the hosted service for its protocol and code signature; the signature covers the whole bundle's code (`core/*.py`, `core/*.mjs`, `core/context_learning/*.py`), so a release that changes only host code still reads as another service and the request asks the resident service to replace an earlier bundle (`ensure_service`, newer version only) instead of using it.

The controller has one synchronous model owner and at most eight pending inference requests. Each request retains its own canonical private namespace, current policy digest, checkpoint/artifact hash, input view, case hash and deadline. Owner UID/private paths and current scope/evidence are checked again after queueing and after inference. Overflow, expired/disconnected callers and denied sockets fall back explicitly; no requests or source bodies are stored in service files. A crashed worker's released kernel lease allows only the next request to reclaim stale socket/PID files. A hosted service refuses the `shutdown` command; a learning request that needs another interpreter or a newer release asks the resident service to drain and restart. A learning-only worker from another code version is replaced only while it reports idle (no busy request, empty queue): the next request asks it to shut down over the socket and reports `starting` until the current version's worker is up. A worker without the shutdown command receives SIGTERM only when its private PID file matches the current socket inode and that PID runs this account's inference worker command for the same root; anything else stays untouched and the request reports `starting`. A busy old worker is never interrupted.

Projects with the same approved runtime/base/view/head reuse one loaded object. A new head in the same family is prepared separately only with estimated overlap headroom and within the total process RSS policy; first prediction, scope/deadline/response validation and final current-RSS checks precede atomic publication. Failures preserve the incumbent reference; recovered current memory permits incumbent requests again. With no incumbent, a first load stays loaded when its request runs out of time (during revalidation or scoring) and current RSS, checked again at that moment, is observable and within policy, so a later request (validated as its own request, and only for that exact generation) finds the service warm instead of loading again; any other first-load failure publishes nothing. A loaded model that current RSS no longer fits when a request starts is released (the request reports `resource_deferred`) and a later request loads it again. Admission uses current self RSS (macOS task_info, Linux proc self), not historical ru_maxrss. MPS/global memory pressure still matters; footprint estimates are not hard GPU reservations and a catastrophic process failure remains possible. Different runtime/base/view families are rejected while an incumbent is loaded, rather than silently migrating models or allocating several resident bases.

The per-project resident module (`context_learning/resident.py`) provides compatibility helpers; ordinary selection does not call its launch path.

Training compatibility's 24h/five-check rules apply unchanged. Explicit inference preparation uses a currently valid approval anchor, then separately verifies a read-only synthetic load/forward and binds content, code, private namespace/policy/checkpoint, closed runtime generation and device. Unchanged prepared inference has no daily training requirement. Missing/revoked/changed inference evidence fails closed; any evidence file makes the complete binding mandatory. Changed evidence requires a fresh worker, except same-family head preparation/application under the candidate protocol below. Runtime/base/view changes require reviewed preparation and a fresh worker. Learning grants, source/gold authorization and teacher transmission remain separate. Prediction never invokes prepare/smoke/train or renews proof/grants.

`memvane/learning/context_learning/inference_identity.py` defines the inference evidence; three private files in the project state directory hold it. `laya-inference-manifest.json` records content hashes of the runtime roots and model directory. `laya-inference-proof.json` (schema `qmd-laya-inference-proof-v1`) records the namespace, executable, model directory, input view, verified identities, plugin version, code SHA-256 (a hash of every `core/**/*.py`), device, manifest SHA-256, the checks `read_only_load`, `synthetic_forward`, `fixed_selection_mapping` and the prediction pairing version (`prediction_chunked_max_v1`), and the SHA-256 of `live-selector.json`, `laya-synthetic-attestation.json` and `active-checkpoint.json`. `laya-inference-binding.json` holds the proof's SHA-256. Only `prepare()` creates all three, run explicitly through `memvane/learning/prepare_laya_inference.py` (at most 120 seconds). Candidate application rewrites the proof and binding with the new checkpoint digest and leaves the manifest unchanged. Prediction (`admit()`) only reads them: if any of the three exists, all must match or selection fails closed with an `inference_identity_*` reason. The receipt is usable only by the plugin version it records; other versions fall back until their own evidence is prepared.

The policy's `trainerArgv[1]` anchors the approved adapter. Hosts remove superseded plugin caches, including every cache of the pre-rename plugin id, so the anchor file can disappear. When it no longer exists, selection uses the running bundle's adapter only if that adapter is an installed official copy (or the registered local source) whose bytes equal `adapter_sha256` in `laya-synthetic-attestation.json`; otherwise selection serves the hybrid rank with `adapter_anchor_missing`. The recall hook prints the reason: `[memvane] local selection unavailable (<reason>); serving hybrid rank`.

### Selector heal

A release changes the code SHA-256 that prepared inference evidence binds, the anchor's cache disappears and the synthetic attestation counts for 24 hours, so a prepared selector stops admitting predictions after an update. `memvane/learning/context_learning/selector_heal.py` heals it in the background, with the same verified steps an owner runs by hand, in dependency order:

0. Before anything executes, the approved scope is checked (`selector_heal.check_scope`). The interpreter must be the activated official learning runtime, re-verified the way the installer verifies a generation: the activation pointer passes `runtime_setup._activated`, its executable is a prepared generation's own venv interpreter (`runtime_installer._generation_executable`, `prepared.json` naming exactly it), and the runtime identity recorded at staging (`prepared.json`), at activation (`active.json`) and in the activation attestation (which must be present; an older pointer without it is not enough) agree (`learning_runtime_not_installed`, `learning_runtime_changed`). The model directory must be the pointer's model directory when it names one; when the last prepared evidence recorded the interpreter's content hash, the interpreter must still have it (`learning_runtime_not_official`, `learning_runtime_changed`). With an accepted learning profile the acceptance must be intact and name exactly this policy: every `trainerArgv` element, `livePolicySha256`, the model identity and a recorded interpreter hash; otherwise `learning_profile_rebind_required` and nothing runs.
1. `live-selector.json` `trainerArgv[1]` is rebound to the running bundle's adapter, only when that adapter is an installed official copy (or the registered local source the host executes) and the rebound policy passes the scope check. An anchor that exists and is another host's identical official copy (the Claude and Codex caches of one release) is kept as written: rewriting it would invalidate the prepared evidence that the other host's heal then rewrites back. A binding check without time left defers (`adapter_binding_timeout`). With a kept anchor, attestation and preparation run with this host's verified adapter; the policy keeps the anchor. The interpreter and the model directory stay as written. A namespace with an accepted learning profile is never rebound: the profile's acceptance digest covers the policy, so that change needs the owner (`learning_profile_rebind_required`).
2. The synthetic attestation is refreshed through `VerifiedLayaTrainer` (its real synthetic checks, at most 120 seconds) when it is missing, older than 24 hours or names another adapter. An adaptive memory policy defers this step (`trainer_adaptive_attestation_required`).
3. Inference evidence is prepared with `memvane/learning/prepare_laya_inference.py` in the policy interpreter (at most 120 seconds) when `admit` no longer accepts the existing evidence.

Steps 2 and 3 use the per-user prepare lease and memory admission. A held lease or insufficient memory defers preparation. The selector heal checks the current model and adapter evidence, and warm sends one synthetic selection through the ordinary resident-service path. Warm-ups coalesce by namespace and service identity. Learning and selection failures fall back to hybrid ranking; they do not fail recall.

### Learning state location

The learning state (`live-selection`, `laya-inference`) lives in `~/Library/Application Support/memvane` (`~/.local/share/memvane` elsewhere). The learning runtime (`runtimes/laya`) is stored separately.

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

Project enrollment is explicit per project. `memvane/learning/project_learning_setup.py` runs `prepare` (read-only plan, `--mode default|synthetic-staged`), `activate` (an exact plan bound by `--accepted-sha256`) and `recover` (configuration-only compare-and-swap restore). Activation writes `project-learning-authorization.json` with scope `single-project-local-learning` (`memvane/learning/context_learning/project_scope.py`): operations `gold`, `local_train` and `quality_evaluate`, the default cohort `general-v1` bound to the project's collection settings, and a lifetime of at most fourteen days. Teacher consent is separate. Explicitly consented project gold uses the signed-in Codex CLI (`general_teacher.py`), and `egress_guard.py` excludes credential-shaped cases before transmission. The default namespaces' `default-local` and `default-teacher` modes and their authority are described under Default teacher. A profile cannot widen the authorized scope.

Evaluation jobs are durable in the selected history DB and hold an inherited kernel lease. All evaluation reads, changes, claims and pending/staged dispatch retain the namespace boundary when projects share that DB. An unscoped evaluation table is preserved and reports `evaluation_namespace_migration_required` for explicit review; it is never silently adopted by the current project. Jobs bind the staged artifact, approval and train/validation evidence; quality-only recovery needs no new training permission or optimizer update. Stale quality (over one hour), changed validation or a changed incumbent triggers new predictions under a distinct cache epoch. Metrics include exact selection, necessary recall, empty-choice accuracy and irrelevant injections; regressions or missing thresholds block publication. Resource/IPC saturation and startup/timeout deferrals use 30–300 second backoff. Three actual evaluation failures block a candidate, allowing a later fresh batch to proceed. Resource deferrals do not spend the failure budget. The existing bounded waiter handles evaluation and training; it never invokes the teacher.

Application separately revalidates exact apply permission, source/quality evidence, artifact and incumbent. The private journal records `intent`, `prepared`, `pointer_committed`, `activated`, or a precommit `rejected` result. The service loads and validates a synthetic request against a same-family candidate while preserving the incumbent reference. Under the existing pointer writer lock, an expected-old CAS publishes the candidate pointer and the exact derived inference receipt/binding; it preserves the runtime manifest and training approval anchor. Ordinary requests fail closed during an incomplete publication. A second prepare can reconstruct a lost pending object after worker restart; matching token, namespace and generation are required for activation. Lost acknowledgements and ledger completion resume idempotently. Foreign pointer edits and postcommit scope revocation remain closed for reviewed recovery, with no automatic operating DB rollback or deletion of the old artifact.

Resident inference and a separate local trainer may coexist only with current admitted memory/CPU headroom. Their compute gates serialize scoring, loading and optimizer boundaries; queued inference takes priority over new training or candidate loading. A running framework call yields cooperatively at its next boundary, so deadlines can still expire. Preparing a candidate retains at most one pending object, with a bounded expiry, alongside the incumbent and blocks a third generation load. Activation swaps at the serialized service boundary and rechecks finite current RSS and resource limits. There is no hard GPU reservation, allocator guarantee, forced preemption or automatic cross-model replacement.

Installing or updating the plugin grants no training, capture, autoCycle or model download. The code has no OS service registration, login/reboot startup, SessionStart prewarm or continuous retry timer. The first eligible call starts the inference worker; a later authorized hook resumes durable work after reboot.

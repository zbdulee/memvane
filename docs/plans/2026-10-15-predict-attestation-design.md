# Predict must not create training attestations

The user authorized a separate local fix, synthetic tests and independent review. Operating settings, proofs, model files, sessions, inbox, installs and main remain untouched. This is a design and implementation phase, not deployment.

## Purpose and current call graph

`runtime_setup.attest_runtime()` proves the full Laya training protocol: tokenization, synthetic inference, one-step training, checkpoint reload and fixed selection mapping. It launches adapter `smoke`, which invokes `train(max_updates=1)`. Its existing schema, checks and trust meaning must not be relabeled as inference-only evidence.

Explicit runtime installation/preparation invokes that prover directly. Explicit cycle trainers may create/reuse it for `train`, then predict against the resulting head. Private `live_select.choose()` also constructs `VerifiedLayaTrainer`, but asks only for `predict`. Today the shared method can create a training proof on missing/expired/incompatible cache, even with capture and autoCycle off. A trainer with an in-memory proof also lacks a per-predict freshness check.

## Chosen minimal design

Separate read-only prediction proof validation from training proof creation inside `VerifiedLayaTrainer`:

- Every `predict` requires an existing, fresh full compatibility proof. A private proof cache, when configured, is authoritative and is reread on each prediction. Missing, unsafe, unreadable, malformed, expired, future-dated or incompatible evidence returns `trainer_inference_attestation_required`; ordinary live selection reports `inference_attestation_required` and preserves its QMD fallback.
- Reject basic invalid cache evidence before runtime metadata probes or prediction subprocesses. Preserve actual model/adapter hashes and the complete existing runtime compatibility predicate. No output, cache timestamp, training grant or receipt is created to make prediction admissible.
- When an explicit `train` already created/loaded a proof in the same trainer without a cache directory, record its real verification timestamp in memory. `predict` can reuse that exact proof only while fresh and still compatible. Never retime evidence on prediction. Cached-proof replacement/revocation remains authoritative when a cache directory exists.
- Keep explicit training attestation semantics and all resource, model, runtime and checkpoint checks. No mode/setting can silently permit prediction to enter `attest_runtime`, `smoke` or `train`.

The user explicitly allowed clear fast fallback if no equivalently strong inference-only proof can be issued. A new no-training proof format is technically a separate protocol, but cannot honestly satisfy the existing required `synthetic_one_step_train` check without running training. This patch therefore separates verification from creation and falls back rather than minting/renewing a weaker proof. This fixes hidden training; it does not promise Laya continues scoring after the current proof expires.

Alternatives rejected: renewing `verifiedAt` from unchanged hashes (weakens freshness), removing training checks from the current proof (changes its meaning), retaining implicit smoke behind capture/autoCycle flags (does not guarantee predict-only behavior and misses non-live callers).

## Verification and instrumentation boundary

Use real tiny model files, private proof files and real trainer construction. Stub only runtime metadata and final trainer execution; use a fail-on-entry attestation spy and assert every executed mode is prediction. Cover both official host cache roots and cross-host binding, fresh/expired/missing/unsafe/permission-denied evidence, modified model/adapter/runtime, schema/check corruption, and same-instance expiry/cache revocation. Preserve explicit train-to-predict compatibility with synthetic transport stubs only.

Timeout instrumentation is a separate proposal: numeric stage timings, effective deadlines and allowlisted error categories, with no source text, raw stderr, filenames or credentials persisted. Do not add product instrumentation or execute live inference in this phase.

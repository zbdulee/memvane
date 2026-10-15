# Laya singleton: measured inference and next commits

This document records the approved isolated real-inference check and proposes subsequent commits. It does not authorize operating installation, teacher transmission, real training, proof renewal, OS service registration or main merge.

## Isolated real check of 06d6764

The existing approved local runtime/base/checkpoint and an unchanged, still-valid compatibility proof were used read-only. Both reviewed branch host package copies were loaded in a private lab HOME; no installed host registry or hook trust was changed. Only socket-root selection was bound to the lab home through the original root validator. Loader/forward observers delegated to the actual implementation; namespace, policy, proof, runtime/model identity, resources and response validation ran unchanged. This tests real selectors/IPC/model execution in both host package layouts, not newly opened paid Claude/Codex sessions or a production hook/QMD retrieval flow.

One empty model process, PID 83089, served five two-card synthetic requests with MPS. All selected `synthetic/cobalt.md`, with no fallback. Exactly one actual model load and five forwards were observed; no retries or extra benchmark runs occurred. Cold describes an empty service process, not a flushed filesystem cache.

| Request | Choose wall seconds | Model load seconds | Forward seconds | Queue wait seconds |
| --- | ---: | ---: | ---: | ---: |
| Cold Claude / namespace one | 8.777 | 3.682 | 0.373 | 0.004 |
| Warm Codex / namespace two | 3.562 | 0 | 0.061 | 0.004 |
| Warm Claude / namespace one | 3.704 | 0 | 0.132 | 0.007 |
| Concurrent Codex / namespace two | 3.470 | 0 | 0.052 | 0.003 |
| Concurrent Claude / namespace one | 5.708 | 0 | 0.042 | 2.214 |

Choose times include client proof/identity/metadata and IPC, worker validation, queue and scoring. They exclude lab preparation/preflight and production QMD retrieval; each client had the existing 18-second choose budget. Forward timing synchronizes actual MPS result readback. Service peak process RSS was 2,417.33 MiB; observed current RSS after forwards ranged 276.53–506.16 MiB. These are process RSS, not complete MPS allocator/device peaks. CPU was measured as cumulative user+system time: service own CPU 13.317 seconds and terminated metadata children 52.008 seconds over 22.556 seconds of service lifetime. No instantaneous peak CPU percentage was sampled.

Metadata probes remain expensive: 11 server probes (three for cold, two per warm) plus five client probes. Model weights are resident, but metadata subprocesses still import torch/laya. The concurrent second request waited about 2.21 seconds while the first request was validated/scored/revalidated; its forward itself took 0.042 seconds. This is evidence for removing redundant imports under a sound inference-identity protocol, not evidence of a GPU forward bottleneck.

A deliberately expired **lab copy** of the receipt was then presented by both host packages while that same model remained warm. Both returned `inference_attestation_required` in about 0.239 seconds, before any metadata child or service forward; load/forward counts stayed one/five. The original operating proof was never modified or reissued. The owned service acknowledged shutdown, exited 0 and removed its socket; no operating service root was created. Watched operating file bytes/modes/inodes and full model identity were unchanged. Smoke/training/teacher calls: zero.

## Why TTL remains today

The current 24h clock is a freshness limit on a cached full synthetic compatibility check, not the lifetime of learned model weights or permission to learn. The check includes one-step training; its schema cannot be reused dishonestly as inference-only evidence. The safety patch retained that existing policy while separating predict from proof creation. Thus an expired receipt still blocks every inference, even for a warm model. It is not technically established that identical weights lose compatibility after 24 hours; retaining TTL here limits this patch's trust-protocol change, not a claim that daily training is necessary.

Operation grants have their own scope/revocation/expiry and must remain separate. Private 24h learning cadence is yet another policy. None should be extended merely because a model is resident or a user submits a prompt.

## Commit 1: Distinct identity-bound inference eligibility

Files: runtime_setup.py, local_cycle.py, inference_service.py, runtime_installer.py plus a small inference receipt module/tests.

- Define a separate `qmd-laya-inference-proof-v1` protocol with read-only load/forward/fixed-choice evidence. Create it only in an explicit approved prepare/validation flow; never train/smoke, retime a full receipt, renew an operation grant or send data externally from prediction.
- Bind interpreter and approved runtime generation/dependency content manifest, adapter/service protocol, model/tokenizer/config manifest, input view, checkpoint hash/provenance, OS/architecture/device/backend identity and check contract. Existing runtime identity hashes interpreter+version metadata/editable commit, not every library byte or device; simply removing TTL would not close those gaps.
- On worker startup verify the complete generation and bind the loaded objects to its immutable-by-protocol identity. Per request cheaply check current policy/pointer/revocation and generation file identity stamps; any observed content/generation/device change invalidates evidence and requires full revalidation. File stamps are change detectors, not replacements for content identity or protection against a privileged attacker.
- Use creation time as provenance rather than a daily inference expiration for an unchanged verified generation. Keep explicit training compatibility and training/data/teacher grant expiry independently enforced. Missing/changed/unknown identity fails with a fixed inference-specific reason; no silent CPU/model switch.
- Acceptance: unchanged generation survives a clock jump without training; same-version dependency mutation, tokenizer/model/head change, unsafe file, revoked receipt, changed device or mixed worker code fails. Three ordinary metadata import subprocesses disappear on valid warm reuse. Measure a few calls only after those checks and independent review.

## Commit 2: Fresh-data ledger and bounded replay

Files: automatic_cohort.py, automatic_staging.py, shared.py and private schema migration/tests.

- Preserve existing safeguards: stable evidence-derived cycle_id, `trained_cycle`, batchMin *new* cases, source-active/cohort provenance and already-staged/evaluated short-circuits. Repeated wakes of an unchanged completed cohort must not create a new cycle.
- Add an explicit consumption ledger keyed by namespace, canonical case/source revisions, verified gold receipt and model family; do not treat a new UUID, capture time, filesystem mtime or timer tick as new learning evidence. Changed labels/revisions require appropriate fresh verification; invalid/revoked sources are excluded.
- A cycle contains sufficient fresh admitted data plus a bounded documented replay sample, not an unbounded cumulative pass over all historical gold each wake. Pin the exact manifest, split/family fences, max updates/CPU wall/memory budget and resulting artifact digest. No fresh data means waiting, never recurring optimization of the same cohort.
- Recovery resumes the same leased cycle/update cursor and persisted optimizer/RNG/checkpoint state; a crash must not count another reservation or replay committed updates forever. Poison jobs have bounded retries and explicit blocked status. Tests cover duplicate events, unchanged cohort, changed source/gold, crash before/after update commit and no holdout leakage.

## Commit 3: Durable resource-aware learning coordination

Files: inference_service.py, shared.py, memory_policy.py, local_cycle.py, jobs.py/new scheduling module and synthetic contention tests.

- Keep the inference request queue ephemeral; persist learning jobs/leases/next retry and evidence pins in a private user scheduler, referring to namespace-private records without copying raw bodies into common telemetry. Coalesce wakes by evidence key. A kernel lease and durable state reconcile worker crashes; no PID guesses or session/inbox pause prerequisite.
- The common controller exposes active/queued inference demand and admission leases. New training defers when inference waits or total resident+trainer+swap+OS reserve cannot fit. Unknown measurements defer; adequate headroom permits one bounded trainer, with microbatch reduction from existing 2 to 1 under pressure. No forced CPU, extra model download or guard bypass.
- A running trainer checks demand/pressure at safe optimizer boundaries, persists a resumable cursor and yields. GPU kernels are not forcibly preempted. Strict latency priority cannot be claimed until this cooperative path exists; independent resource snapshots today do not coordinate the two workloads.
- Wake on fresh eligible data, index ACK, job completion and a bounded backed-off resource retry while pending work exists. No busy loop, repeated teacher call or fresh train cycle from periodic wakes alone. Replace the private scheduler's hard 24h floor with evidence/resource-driven admission under explicit budgets, not with continuous retraining.
- Tests: queued inference defers new training; active training shrinks/yields; ample resources run bounded background work; restart preserves pending work; duplicate wakes do not duplicate workers; starvation/backoff and revoked scope are explicit. No real stress load is needed for these contracts.

## Commit 4: Validated candidate application transaction

Files: automatic_staging.py, local_cycle.py, runtime_authorization.py, inference_service.py and a narrow publication journal module/tests.

- Current authorized v2 automation intentionally stops at staged/staged_evaluated with `forbidden_without_separate_apply_authorization`. Do not route it into the legacy publisher or reinterpret a train/evaluate grant as apply permission.
- Define a separate scoped auto-apply policy/authorization. Recheck fresh disjoint validation, empty choices, source/cohort validity, no regression, artifact identity and current grant immediately before publication. Insufficient evidence stays staged.
- Prepare/validate a separate model object only with overlap headroom while old inference stays available. Pin expected-old pointer, new artifact, quality evidence and authorization in a durable apply intent. Drain at a request boundary, CAS the project pointer and publish the prepared service generation with explicit prepared/pointer_committed/activated recovery states.
- The current atomic service reference alone is not a cross-file/database publication transaction. A failed new-head request presently falls back rather than silently using an old head under a new version label. Recovery must reconcile exact journal/pointer hashes; preserve the incumbent before commit, never migrate/rollback operating history DB as an application experiment.
- Tests inject failure at every journal/CAS boundary, caller disconnect, resource loss, concurrent namespace requests, policy revocation and process crash. No automatic application merely because training finished.

## Commit 5: Product defaults and scoped migration

Files: config.py, approved setup/enrollment flow, project policy defaults and docs/tests.

- Make automatic local learning the default within an approved new-project learning profile, with the above freshness/resource/quality bounds. Preserve explicit existing capture=false/autoCycle=false until an approved migration changes them; do not silently enroll every project or historical corpus.
- Local learning and candidate auto-apply defaults do not authorize new raw prompt collection, external teacher transmission, API billing or a model change. Reuse verified eligible labels where available; missing authorization/data produces a clear waiting state. Existing training grant expiry/revocation remains effective.
- Update setup/diagnostics to distinguish waiting_fresh_data, deferred_inference, deferred_memory, blocked_authorization, staged_waiting_quality, prepared and applied. No source text or credentials in common diagnostics.

Each implementation commit requires focused synthetic RED/GREEN checks and independent review. Only a later separately approved operating rollout may change installed plugins, project flags or active pointers.

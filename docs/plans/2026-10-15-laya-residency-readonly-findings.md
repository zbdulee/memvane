# Laya residency, learning and evidence: read-only findings

Scope: code and installed rc45 inspection only. No service launch/registration, model call, learning, teacher, permission change or deployment was performed for this investigation. The separate prediction safety patch remains local. Base: `8126f93335f2ce9e8c48592bbed1a674f81e136f`.

## Result

An existing Laya resident worker is present, but ordinary live selection does not use it. It is a project-state-scoped, demand-started worker for the multilingual experimental edit model. This does not meet the agreed single inference service per PC/OS user shared by Claude, Codex and multiple projects, kept available across sessions.

Read-only inspection found both installed hosts still on rc45. Installed `live_select.py`, `resident.py`, `local_cycle.py` and `laya_adapter.py` match the base commit. Service-engineering and rccar both use ordinary private `active-checkpoint.json`, have no input-view marker, and have capture=false, autoCycle=false, liveSelection=true. Neither of these private namespaces contains a resident socket/PID file. This is scoped filesystem evidence, not a system-wide process inventory.

## Exact paths

Ordinary Claude/Codex inference:

1. Host `UserPromptSubmit` command invokes `hooks/run-hook recall <host>` (`hooks/hooks*.json`).
2. `run-hook` invokes backend-manager ensure in the background, then executes `core/recall.py`. That backend lifecycle is QMD retrieval, not Laya model residency.
3. `recall.py` obtains the QMD candidate pool and fresh complete wiki snapshots, then calls `context_learning.live_select.choose`. With no eligible shared trial, the default pointer is `active-checkpoint.json`.
4. `live_select.choose` validates the private policy, installed adapter binding and checkpoint; the ordinary branch constructs `VerifiedLayaTrainer`.
5. Its prediction guard reads proof and probes runtime metadata. In deployed rc45, absent/expired proof can invoke `attest_runtime` and `smoke` (including one-step synthetic training). The separate local safety patch forbids that path for predict.
6. `LocalTrainer.call` writes a private temporary request and runs `[python, adapter, model_dir, 'predict', request, result]` with `subprocess.Popen`.
7. `laya_adapter.main -> predict -> _load`: local tokenizer, `build_model`, safetensors base/head loading, device transfer, then `predict_loaded`. The child exits after this request. There is no model reuse in this ordinary branch.

Existing resident branch:

1. `recall.py` must select an eligible shared trial; `choose` must receive `experimental-checkpoint.json` with `model_input_view == EDIT_INPUT_VIEW`.
2. `choose -> resident.predict -> _launch` starts `python -m context_learning.resident worker ...` only when needed.
3. `_worker` checks base/head, calls `_load` once before binding an owner-private Unix socket, then repeatedly calls `predict_loaded` on those same model objects.
4. Socket, PID record, startup lock and kernel lease all live under the supplied project/shared-state directory. Two distinct state directories can create two workers. A shared directory can reuse one worker across hosts, but there is no user-wide singleton invariant.
5. `server.listen(4)` provides a bounded connection backlog; one synchronous accept/scoring loop executes requests serially. Version handshakes can reject a busy worker as `resident_busy_or_starting` before a new prediction joins the backlog. This is not a durable fair scheduling queue.
6. `IDLE_SECONDS=90`: worker exits after an idle accept timeout. No SessionStart Laya warmup or launchd supervision was found. A released kernel lease permits stale socket cleanup and a later request can relaunch after a crash; this is demand recovery, not immediate restart.

QMD has its own backend daemon/endpoint lifecycle (`backend/daemon.sh`, backend manager). Its existence says nothing about Laya being loaded.

## Existing capability versus missing connection

| Requirement | Existing code | Missing part |
| --- | --- | --- |
| One shared inference service | Owner-private socket, same-owner peer check, startup lock and live kernel lease in `resident.py` | User-wide plugin service root/identity and startup coordination across project states/host versions; regular selector routing |
| Model reuse | Worker loads once and reuses `predict_loaded` | Ordinary active-checkpoint path bypasses it; 90s expiry conflicts with continuous availability |
| Request scheduling | Serial worker, bounded backlog, deadlines, caller-disconnect cancellation | Cross-project fair bounded queue, explicit per-project request/checkpoint binding and inference-priority admission |
| CPU/memory adaptation | Resident admission chooses microbatch 1/2/4 and token cap; load/free-memory/pressure floors. `memory_policy.py` offers estimated training microbatch 2/1, reserve and monitored defer | No common scheduler coordinates inference and a separate training process or reserves replacement overlap; batching is not adaptive simultaneous inference concurrency |
| Durable learning/recovery | Private SQLite cases/cycles, shared-worker lock, staged artifacts, source/cohort checks, post-index/event wakes; schedule persists phase/due state | Event wakes are not a guaranteed continuously running retry scheduler. Private schedule has 24h minimum cadence and no retry loop. Some shared wakes are explicitly scoped to an approved synthetic owner |
| Automatic learning | `context_learning_auto.py`/`schedule.py`, shared worker and `automatic_staging.py` exist | Current two projects are disabled; normalized capture/autoCycle require explicit true. Universal default-on, fresh-data/resource-driven behavior is not implemented |
| Safe candidate publication | Private cycle atomic pointer/promotion recovery; legacy shared evaluation publication/rollback | Current authorized v2 automatic cohorts deliberately stop at `staged`/`staged_evaluated`; publication requires separate apply authorization. No resident seamless handoff is wired |
| Model replacement | Resident compares requested artifact SHA/model path/view; mismatch returns `model_changed` and exits | It does not prepare/swap a validated head while old inference remains available; the triggering call falls back and a later call starts the replacement |

`automatic_staging.py` trains through its own VerifiedLayaTrainer process; it does not mutate the resident model in place. That is a useful starting boundary for concurrent inference and background learning, but simultaneous resource safety and priority are not established by having separate processes.

Tests inspected: `context-learning-live-selector-checks.py` explicitly routes only EDIT experimental selection to a stub resident. `edit-content-hook-checks.py` exercises stale lease, protocol bounds/upgrade, peer/deadline/disconnect, constrained admission, cancellation and timing. These are useful contract tests; they do not demonstrate user-wide singleton reuse, real loaded-model reuse across both hosts, concurrent training priority or a no-gap checkpoint swap.

## What expires, and what does not

There are separate clocks:

- **Compatibility cache:** `laya-synthetic-attestation.json` wrapper `verifiedAt`, age 0..86400 in `VerifiedLayaTrainer`. The nested attestation has no time field. It reports a past synthetic check of tokenization/no truncation, inference, one-step training, checkpoint reload and fixed selection mapping. This is not a learned checkpoint's lifetime, data freshness, quality guarantee or permission to train.
- **Operation authorization:** `runtime_authorization.py` verifies the exact project/model/adapter/operation/cohort grant and `expiresAtUnixMs`. That independently limits permitted work. Reading a compatibility proof must never mint or renew a grant.
- **Learning cadence:** `schedule.py`/`cycle_policy.py` use a separate 24h minimum interval, potentially longer; this is scheduling policy, not either evidence expiration above. Shared learning also has fresh-gold batch/quality thresholds.

The 86400 value is a hard-coded cache freshness policy; the inspected code does not establish why every unchanged runtime must repeat a train-capability smoke each day. Passing 24 hours does not itself alter model weights or prove lost compatibility. The current safety patch intentionally retains this policy and all checks while removing hidden proof creation from prediction. It therefore falls back explicitly after expiry; it does not solve continuous inference availability.

An identity-bound inference protocol could replace daily inference expiration as a separately reviewed design. Do not simply remove TTL or relabel the full five-check training proof:

1. Keep installation-time full train compatibility evidence for the explicit training path. Define distinct read-only inference eligibility and original issuance provenance; prediction must never train or write an authorization renewal.
2. Pin approved interpreter/runtime generation, model/tokenizer/config bytes, adapter/protocol/input view, checkpoint/head hash/provenance and device compatibility. Preserve owner/private-file checks, revocation, caller scope, checkpoint/source validation, deadlines and resource guards on reuse and immediately before execution.
3. Existing runtime identity hashes interpreter bytes plus version metadata and editable-origin commit; it does **not** hash every installed library. Device/MPS availability is probed separately and not included in that identity hash. Same-version library changes, OS/device changes and mixed host versions need an explicit identity/immutable-generation story before claiming unchanged identity is sufficient.
4. Bind the running worker's loaded generation/adapter/model/head to that receipt, invalidate on an observed identity/config/revocation change, and use a bounded no-training load/forward check where fresh inference evidence is required. Any persistent receipt creation must have a defined authorized flow, not hidden timestamp refresh.
5. Training scope/teacher transmission authorization remains independent and revocable. A long-lived inference receipt cannot authorize new data transmission or unrestricted learning.

## Minimum next implementation, not executed

Reuse the resident worker's socket/lease/protocol foundations in one plugin-owned service directory per OS user. Both hosts connect to this single service and carry a validated project namespace, deadline and explicitly pinned model/head generation with every request. A central queue/admission controller must serialize or cap inference work and coordinate separate durable learning jobs, with inference priority and smaller training batches/defer under pressure. Preserve project policy/data isolation even though the process is shared.

Both current private projects reference the same base/artifact bytes, so a shared loaded base is feasible. Different English/multilingual bases are not interchangeable: a single process does not guarantee one model's memory use if incompatible bases are loaded. Future model slots/activation must be explicit; no automatic model replacement is authorized here.

Candidate learning should use a separate process and immutable staged head, validate before publication, then safely transfer a checkpoint generation at a request boundary. Keep the old inference generation until the replacement is ready, account for temporary duplicate model/device memory, drain/retire old work and recover failed publication. At insufficient capacity, retain inference and leave training/replacement queued; do not force CPU, loosen guards or evict the current model just to make room.

An ordinary detached plugin worker can use existing user permissions without installing launchd, root helpers or new software. Permanent OS-supervised startup/restart would be a distinct service-install choice, not assumed necessary for the first connection. OS sandbox restrictions still apply: managed shell process inspection/Metal denial is not worked around; any future real live test must use an explicitly authorized normal-host entrypoint. Installing a fixed plugin version, changing inference service/active-checkpoint routing, enabling automatic capture/training or widening data/teacher authorization are operating changes requiring a concrete separately reviewed rollout. None was performed.

This finding corrects the earlier assumption that the current operating Laya selector was already resident. The local no-hidden-training fix remains valid and does not remove automatic background learning.

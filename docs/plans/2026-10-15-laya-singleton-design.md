# One Laya inference service per OS user

Authorized scope: preserve 447d16e; implement user singleton plus regular Claude/Codex selector connection in an isolated local branch. Synthetic/mock tests and independent review only. Keep sessions/inbox. No OS service registration, installed plugin update, real model/training/teacher, new credentials, push or merge.

## Selected architecture

Use the existing resident socket/peer/lease primitives with one plugin-owned root under the OS account home (independent of process HOME overrides), not one project root. A persistent controller accepts bounded JSON over one owner-private Unix socket; a single scoring thread keeps exactly one active model generation. Startup lock plus kernel process lease prevents duplicate startup, and a freed lease permits cleanup/restart only on the next request. No PID signaling, ps-based guesses, launchd, port or background teacher work. First valid inference starts the service; the loaded model stays until service exit. SessionStart prewarm is not part of this first slice.

A serial controller with bounded in-memory FIFO admission was selected over a new HTTP/launchd service (unnecessary installation/attack surface) and over moving the old socket path alone (would not solve blocked handshakes, namespace validation or generation handoff). Running requests + at most 8 pending requests; queue overflow, expiry or disconnect falls back with fixed codes. Inference request bodies are never written to service storage. Loss of pending inference on crash is explicit fallback, not durable replay of prompts.

Every request binds canonical private state namespace, actual on-disk policy, checkpoint pointer, artifact hash, input view, adapter/protocol identity and an epoch deadline converted to server monotonic time. Both client and worker validate the project evidence. Extract a read-only VerifiedLayaTrainer prediction admission method and reuse it; no implicit smoke/train, proof refresh or grant creation. Missing proof fails before service launch. Worker rechecks queued proof/pointer/policy at execution. Existing full TTL/five-check semantics remain unchanged. Experimental requests do not get a proof bypass; missing private evidence explicitly falls back.

Only identical verified runtime/base/input-view families share the active model; same artifact reuses it across project namespaces. A different head in that family may prepare a second candidate object, then assign one active generation reference only after load/validation and a final scope/deadline recheck. Failed preparation leaves the prior reference intact. Different base/runtime/view families fail closed while an incumbent is resident; do not thrash or silently replace English with multilingual models. Supporting multiple approved families is a later user policy decision, not permission to allocate multiple resident bases.

## Resource and learning boundary

Keep existing host load/free-memory/pressure inference admission and 1/2/4 batching. Measure self RSS without external process inspection. For replacement, use the observed retained generation footprint plus conservative reserve, require headroom for overlap and the request's total process RSS limit; if unavailable/unknown, keep the incumbent and defer. Estimates cannot guarantee allocator peaks. Catch candidate allocation/load failures; a catastrophic process crash is recovered on a later request. No forced CPU or OS guard changes. One active inference model does not mean learning or replacement preparation uses no extra memory.

This slice handles inference FIFO/admission and replacement only. Current learning already persists cases/cycles and staged artifacts, but its OS/load guards do not reserve shared inference demand or preempt a running trainer. Minimum next coordination slice: a private common inference activity/lease record, checked before training admission and at existing pressure checkpoints; queued inference makes new training defer, persistent training rows remain pending and later event/worker wakes retry. A crash-safe wake/backoff mechanism and cooperative safe-update yielding are required for continuous automatic retry. Do not claim that an independent resource snapshot gives strict inference priority. This implementation does not launch/reconfigure learning.

Current capture/autoCycle defaults require explicit true; private learning cadence has a 24h floor, shared learning has batch/cohort thresholds, and authorized v2 automatic training intentionally stops at staged/staged_evaluated. Product-default automatic capture/learning and validated automatic pointer application still need scoped policy/authorization changes. These are recorded gaps, not silently activated here.

## Safety and validation

Use a short owner-private temporary service root and synthetic object loader/forward callbacks in tests. Exercise real POSIX lease/startup race and sockets where permitted, not a fake PID liveness guard. No actual model imports/calls. Cover two namespaces/hosts one process/model, queue capacity/order/expired and disconnected callers, changed/revoked evidence while waiting, malformed/oversized protocol, unsafe paths/peer, version mismatch, failed/insufficient-memory replacement preserving old results, successful generation swap, crash/stale lease and subsequent restart. Verify real selector proof gates for both installed-host fixtures and assert no training/attest entry. Keep installation/runtime/project settings unchanged.


## Review corrections and rollout limits

Independent review found and regression tests fixed two boundaries: final scope/evidence/RSS checks must also apply to warm reuse, and admission must use current RSS so a failed high-water allocation does not permanently reject the incumbent. The native current-self monitor fails closed if unavailable. All model publication also waits for a valid first response, not only a successful load.

Ordinary live calls no longer launch the old project resident, but old code remains for compatibility helpers/tests. This phase has not inspected/retired any operating legacy worker. Future rollout must handle actual mixed-version/legacy-worker conflicts without reintroducing blanket session/inbox pause conditions. Different global service code is rejected without stopping the existing service.

This is lazy start on the first admitted inference with persistent residency, not SessionStart prewarm or launchd restart supervision. Actual installed-host/model timing is unexecuted. Warm requests still incur at least three runtime metadata import probes; reusing model weights does not prove latency improvement. The full proof's 24h rule is deliberately retained; identity-bound inference evidence is a separate protocol decision.


The atomic replacement is the service's in-memory active reference. The service does not write a project's checkpoint pointer or authorize learner publication. A request pinned to a failed new head falls back; it never silently reports the old head as the requested new generation. The incumbent remains available for requests whose current project pointer still approves that generation. Coordinating durable publication with background learning is part of the follow-up apply/scheduler work.

Final local evidence: 10 Node tests passed (including 20 singleton Python cases and 14 adaptive-memory cases); independent read-only review approved after the two corrections. All inference/training/teacher work used synthetic objects/transports. Real installed-host IPC, model latency and allocator behavior remain unexecuted.
